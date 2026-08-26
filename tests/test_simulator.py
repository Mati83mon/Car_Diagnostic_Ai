"""The ECU simulator's UDS protocol conformance.

The simulator is the reference vehicle for the whole suite, so its own
protocol behaviour has to be right: if it answers 0x19 wrongly, every test
above it is measuring the wrong thing.
"""

from __future__ import annotations

import pytest

from majster_ai.mcp_servers.car_interface.dtc import decode_dtc_records
from majster_ai.mcp_servers.car_interface.pids import load_signal_catalogue
from majster_ai.mcp_servers.car_interface.simulator import (
    OBD_PID_ENCODERS,
    SESSION_DEFAULT,
    SESSION_EXTENDED,
    EcuSimulator,
    SimulatedDtc,
    build_freelander2_simulator,
    build_healthy_simulator,
)


class TestReadDtcInformation:
    def test_report_by_status_mask(self, ecm) -> None:
        response = ecm.handle_request(b"\x19\x02\xff")[0]
        assert response[:3] == b"\x59\x02\xff"
        assert len(decode_dtc_records(response[3:])) == 3

    def test_mask_selects_confirmed_only(self, ecm) -> None:
        response = ecm.handle_request(b"\x19\x02\x08")[0]
        assert [d.code for d in decode_dtc_records(response[3:])] == ["P0299", "P2015"]

    def test_mask_selects_pending_only(self, ecm) -> None:
        response = ecm.handle_request(b"\x19\x02\x04")[0]
        codes = [d.code for d in decode_dtc_records(response[3:])]
        assert "P0401" in codes

    def test_report_number_of_dtcs(self, ecm) -> None:
        response = ecm.handle_request(b"\x19\x01\xff")[0]
        assert (response[4] << 8 | response[5]) == 3

    def test_unsupported_subfunction(self, ecm) -> None:
        assert ecm.handle_request(b"\x19\x77\xff")[0] == b"\x7f\x19\x12"

    def test_short_request(self, ecm) -> None:
        assert ecm.handle_request(b"\x19")[0] == b"\x7f\x19\x13"


class TestReadDataByIdentifier:
    def test_known_did(self, ecm) -> None:
        response = ecm.handle_request(b"\x22\xf1\x90")[0]
        assert response[:3] == b"\x62\xf1\x90"
        assert response[3:] == b"SALFA2BB8AH100001"

    def test_unknown_did(self, ecm) -> None:
        assert ecm.handle_request(b"\x22\x12\x34")[0] == b"\x7f\x22\x31"


class TestObdServices:
    def test_current_data(self, ecm) -> None:
        response = ecm.handle_request(b"\x01\x0c")[0]
        assert response[:2] == b"\x41\x0c"

    def test_unsupported_pid(self, ecm) -> None:
        assert ecm.handle_request(b"\x01\xfe")[0] == b"\x7f\x01\x31"

    def test_module_without_obd_refuses(self, vehicle) -> None:
        assert vehicle.get(0x760).handle_request(b"\x01\x0c")[0] == b"\x7f\x01\x11"

    @pytest.mark.parametrize("pid", sorted(OBD_PID_ENCODERS))
    def test_every_encoder_round_trips_through_the_real_decoder(self, ecm, pid: int) -> None:
        """The simulator's encoders are the inverse of the production decoders.

        If they ever disagree, every live-data test is validating a formula
        against itself. This catches that.
        """
        if pid not in ecm.live_data:
            pytest.skip(f"PID 0x{pid:02X} is not part of the scenario")
        catalogue = load_signal_catalogue()
        signal = next(s for s in catalogue if s.identifier == pid and s.source.value == "obd_pid")
        response = ecm.handle_request(bytes((0x01, pid)))[0]
        decoded = signal.decode(response[2:])
        expected = ecm.live_data[pid]
        assert decoded == pytest.approx(expected, rel=0.02, abs=0.6), (
            f"{signal.name}: simulator encoded {expected} but the production "
            f"decoder read back {decoded}"
        )


class TestSessions:
    def test_default_to_extended_and_back(self, ecm) -> None:
        assert ecm.session == SESSION_DEFAULT
        ecm.handle_request(b"\x10\x03")
        assert ecm.session == SESSION_EXTENDED
        ecm.handle_request(b"\x10\x01")
        assert ecm.session == SESSION_DEFAULT

    def test_response_carries_p2_timings(self, ecm) -> None:
        response = ecm.handle_request(b"\x10\x03")[0]
        assert len(response) == 6  # SID + subfn + P2 + P2*

    def test_unsupported_session(self, ecm) -> None:
        assert ecm.handle_request(b"\x10\x09")[0] == b"\x7f\x10\x12"

    def test_returning_to_default_locks_security(self, ecm) -> None:
        ecm.handle_request(b"\x10\x03")
        ecm.handle_request(b"\x27\x02\x11\x22")
        assert ecm.security_unlocked is True
        ecm.handle_request(b"\x10\x01")
        assert ecm.security_unlocked is False


class TestTesterPresent:
    def test_answers(self, ecm) -> None:
        assert ecm.handle_request(b"\x3e\x00")[0] == b"\x7e\x00"

    def test_suppressed_response_stays_silent(self, ecm) -> None:
        assert ecm.handle_request(b"\x3e\x80") == []


class TestClear:
    def test_clear_all(self, ecm) -> None:
        assert ecm.handle_request(b"\x14\xff\xff\xff")[0] == b"\x54"
        assert ecm.dtcs == []

    def test_clear_one(self, ecm) -> None:
        ecm.handle_request(b"\x14\x02\x99\x00")
        assert [d.code for d in ecm.dtcs] == ["P2015", "P0401"]

    def test_extended_session_requirement(self, vehicle) -> None:
        abs_module = vehicle.get(0x760)
        assert abs_module.handle_request(b"\x14\xff\xff\xff")[0] == b"\x7f\x14\x22"
        abs_module.handle_request(b"\x10\x03")
        assert abs_module.handle_request(b"\x14\xff\xff\xff")[0] == b"\x54"


class TestEcuReset:
    def test_refused_in_the_default_session(self, ecm) -> None:
        assert ecm.handle_request(b"\x11\x01")[0] == b"\x7f\x11\x22"

    def test_allowed_in_the_extended_session(self, ecm) -> None:
        ecm.handle_request(b"\x10\x03")
        assert ecm.handle_request(b"\x11\x01")[0] == b"\x51\x01"


class TestFaultInjection:
    def test_drop(self, ecm) -> None:
        ecm.inject_faults(drop_next=2)
        assert ecm.handle_request(b"\x3e\x00") == []
        assert ecm.handle_request(b"\x3e\x00") == []
        assert ecm.handle_request(b"\x3e\x00") != []

    def test_pending_prefix(self, ecm) -> None:
        ecm.inject_faults(pending_next=3)
        responses = ecm.handle_request(b"\x19\x02\xff")
        assert responses[:3] == [b"\x7f\x19\x78"] * 3
        assert responses[3][:2] == b"\x59\x02"

    def test_busy(self, ecm) -> None:
        ecm.inject_faults(busy_next=1)
        assert ecm.handle_request(b"\x19\x02\xff")[0] == b"\x7f\x19\x21"

    def test_clear_faults(self, ecm) -> None:
        ecm.inject_faults(drop_next=5)
        ecm.clear_faults()
        assert ecm.handle_request(b"\x3e\x00") != []


class TestMisc:
    def test_unknown_service(self, ecm) -> None:
        assert ecm.handle_request(b"\xaa\x00")[0] == b"\x7f\xaa\x11"

    def test_empty_request(self, ecm) -> None:
        assert ecm.handle_request(b"") == []

    def test_requests_are_logged(self, ecm) -> None:
        ecm.handle_request(b"\x3e\x00")
        assert ecm.request_log[-1] == b"\x3e\x00"

    def test_add_dtc(self, ecm) -> None:
        ecm.add_dtc("P0234")
        assert "P0234" in [d.code for d in ecm.dtcs]

    def test_snapshot(self, ecm) -> None:
        assert ecm.snapshot()["dtc_count"] == 3


class TestVehicle:
    def test_addresses(self, vehicle) -> None:
        assert vehicle.addresses() == [0x731, 0x760, 0x7E0, 0x7E1]

    def test_missing_address(self, vehicle) -> None:
        assert vehicle.get(0x999) is None
        assert 0x999 not in vehicle

    def test_healthy_vehicle_has_no_faults(self, healthy_vehicle) -> None:
        assert all(not healthy_vehicle.get(address).dtcs for address in healthy_vehicle.addresses())

    def test_reset(self, vehicle) -> None:
        vehicle.get(0x7E0).handle_request(b"\x10\x03")
        vehicle.reset()
        assert vehicle.get(0x7E0).session == 1
        assert vehicle.get(0x7E0).request_log == []

    def test_scenario_is_a_coherent_story(self, vehicle) -> None:
        # An underboost code and a swirl-flap code together is a realistic and
        # *related* presentation -- the agent should have something to reason
        # about, not one obvious answer.
        codes = {dtc.code for dtc in vehicle.get(0x7E0).dtcs}
        assert {"P0299", "P2015"} <= codes

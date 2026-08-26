"""UDS session behaviour on a flaky bus: retries, timeouts, NRCs, stale frames."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from majster_ai.errors import (
    UdsNegativeResponse,
    UdsProtocolError,
    UdsTimeoutError,
    TransportNotOpenError,
)
from majster_ai.mcp_servers.car_interface.simulator import EcuSimulator, SimulatedDtc
from majster_ai.mcp_servers.car_interface.transport import SimulatedTransport, UdsTransport
from majster_ai.mcp_servers.car_interface.uds_client import (
    SESSION_EXTENDED,
    UdsSession,
    nrc_name,
)


@pytest.fixture
def slept() -> list[float]:
    return []


@pytest.fixture
def session(ecm, slept):
    """A session against the simulated ECM, with instant backoff."""
    uds = UdsSession(
        SimulatedTransport(ecm),
        name="ECM",
        retries=2,
        backoff=0.25,
        timeout=0.05,
        sleep=slept.append,
    )
    uds.open()
    yield uds
    uds.close()


class TestHappyPath:
    def test_reads_dtcs(self, session) -> None:
        codes = [dtc.code for dtc in session.read_dtc_by_status_mask(0xFF)]
        assert codes == ["P0299", "P2015", "P0401"]

    def test_status_mask_is_honoured_on_the_wire(self, session, ecm) -> None:
        session.read_dtc_by_status_mask(0x08)
        assert ecm.request_log[-1] == b"\x19\x02\x08"

    def test_dtc_count(self, session) -> None:
        assert session.read_dtc_count(0xFF) == 3

    def test_read_data_by_identifier(self, session) -> None:
        assert session.read_data_by_identifier(0xF190) == b"SALFA2BB8AH100001"

    def test_read_obd_pid(self, session) -> None:
        assert session.read_obd_pid(0x0C) == b"\x0c\xb0"

    def test_session_control(self, session, ecm) -> None:
        session.start_session(SESSION_EXTENDED)
        assert session.current_session == SESSION_EXTENDED
        assert ecm.session == SESSION_EXTENDED

    def test_tester_present(self, session) -> None:
        assert session.tester_present()[0] == 0x7E

    def test_suppressed_response_returns_immediately(self, session, ecm) -> None:
        assert session.tester_present(suppress_response=True) == b""
        assert ecm.request_log[-1] == b"\x3e\x80"

    def test_probe_finds_a_present_module(self, session) -> None:
        assert session.probe() is True


class TestResponsePending:
    """NRC 0x78 is routine, not a failure."""

    def test_absorbs_pending_frames(self, session, ecm) -> None:
        ecm.inject_faults(pending_next=3)
        assert len(session.read_dtc_by_status_mask(0xFF)) == 3

    def test_pending_does_not_consume_a_retry(self, session, ecm, slept) -> None:
        ecm.inject_faults(pending_next=5)
        session.read_dtc_by_status_mask(0xFF)
        assert slept == [], "response-pending must not trigger backoff"

    def test_endless_pending_eventually_gives_up(self, ecm) -> None:
        # An ECU stuck in a pending loop must not hang the agent forever.
        uds = UdsSession(
            SimulatedTransport(ecm),
            name="ECM",
            retries=0,
            timeout=0.05,
            max_response_pending=3,
            sleep=lambda _: None,
        )
        uds.open()
        ecm.inject_faults(pending_next=10)
        with pytest.raises(UdsTimeoutError, match="response pending"):
            uds.read_dtc_by_status_mask(0xFF)


class TestRetries:
    def test_recovers_after_transient_timeouts(self, session, ecm) -> None:
        ecm.inject_faults(drop_next=2)
        assert len(session.read_dtc_by_status_mask(0xFF)) == 3

    def test_backoff_is_exponential(self, session, ecm, slept) -> None:
        ecm.inject_faults(drop_next=2)
        session.read_dtc_by_status_mask(0xFF)
        assert slept == [0.25, 0.5]

    def test_gives_up_after_exhausting_retries(self, session, ecm) -> None:
        ecm.inject_faults(drop_next=99)
        with pytest.raises(UdsTimeoutError) as info:
            session.read_dtc_by_status_mask(0xFF)
        assert info.value.details["attempts"] == 3

    def test_timeout_message_is_actionable(self, session, ecm) -> None:
        ecm.inject_faults(drop_next=99)
        with pytest.raises(UdsTimeoutError, match="asleep|absent|ignition"):
            session.read_dtc_by_status_mask(0xFF)

    def test_busy_repeat_request_is_retried(self, session, ecm) -> None:
        ecm.inject_faults(busy_next=1)
        assert len(session.read_dtc_by_status_mask(0xFF)) == 3

    def test_retries_disabled_means_one_attempt(self, ecm) -> None:
        uds = UdsSession(SimulatedTransport(ecm), retries=0, timeout=0.05, sleep=lambda _: None)
        uds.open()
        ecm.inject_faults(drop_next=1)
        with pytest.raises(UdsTimeoutError) as info:
            uds.read_dtc_by_status_mask(0xFF)
        assert info.value.details["attempts"] == 1


class TestNegativeResponses:
    def test_definitive_nrc_is_not_retried(self, session, ecm, slept) -> None:
        # Retrying "security access denied" wastes time and can trip an ECU's
        # anti-scan lockout.
        with pytest.raises(UdsNegativeResponse) as info:
            session.read_data_by_identifier(0x1234)
        assert info.value.nrc == 0x31
        assert info.value.nrc_name == "RequestOutOfRange"
        assert slept == []

    def test_conditions_not_correct_surfaces(self, ecm) -> None:
        ecm.clear_requires_extended_session = True
        uds = UdsSession(SimulatedTransport(ecm), retries=0, timeout=0.05, sleep=lambda _: None)
        uds.open()
        with pytest.raises(UdsNegativeResponse) as info:
            uds.clear_diagnostic_information()
        assert info.value.nrc == 0x22

    def test_unknown_service(self, session) -> None:
        with pytest.raises(UdsNegativeResponse) as info:
            session.request(b"\xaa\x01")
        assert info.value.nrc == 0x11

    def test_nrc_names(self) -> None:
        assert nrc_name(0x78) == "RequestCorrectlyReceived_ResponsePending"
        assert nrc_name(0x33) == "SecurityAccessDenied"
        assert "0xEE" in nrc_name(0xEE)


class TestProtocolIntegrity:
    """Guards against attributing one signal's data to another."""

    def test_mismatched_did_echo_is_rejected(self) -> None:
        transport = Mock(spec=UdsTransport)
        transport.recv.return_value = b"\x62\xf1\x91DIFFERENT"  # asked F190
        session = UdsSession(transport, retries=0, sleep=lambda _: None)
        with pytest.raises(UdsProtocolError, match="echoed"):
            session.read_data_by_identifier(0xF190)

    def test_mismatched_pid_echo_is_rejected(self) -> None:
        transport = Mock(spec=UdsTransport)
        transport.recv.return_value = b"\x41\x0d\x64"  # asked 0x0C
        session = UdsSession(transport, retries=0, sleep=lambda _: None)
        with pytest.raises(UdsProtocolError, match="echoed"):
            session.read_obd_pid(0x0C)

    def test_stale_positive_frame_is_skipped(self) -> None:
        # A late reply to a previous request must not be read as this answer.
        transport = Mock(spec=UdsTransport)
        transport.recv.side_effect = [
            b"\x41\x0c\x0c\xb0",  # stale OBD reply
            b"\x62\xf1\x90SALFA2BB8AH100001",  # the one we asked for
        ]
        session = UdsSession(transport, retries=0, sleep=lambda _: None)
        assert session.read_data_by_identifier(0xF190) == b"SALFA2BB8AH100001"

    def test_stale_negative_frame_is_skipped(self) -> None:
        transport = Mock(spec=UdsTransport)
        transport.recv.side_effect = [
            b"\x7f\x22\x31",  # NRC for a different service
            b"\x59\x02\xff",
        ]
        session = UdsSession(transport, retries=0, sleep=lambda _: None)
        assert session.read_dtc_by_status_mask() == []

    def test_malformed_negative_response(self) -> None:
        transport = Mock(spec=UdsTransport)
        transport.recv.return_value = b"\x7f\x19"  # missing the NRC byte
        session = UdsSession(transport, retries=0, sleep=lambda _: None)
        with pytest.raises(UdsProtocolError, match="malformed"):
            session.read_dtc_by_status_mask()

    def test_truncated_dtc_response(self) -> None:
        transport = Mock(spec=UdsTransport)
        transport.recv.return_value = b"\x59"  # no availability mask
        session = UdsSession(transport, retries=0, sleep=lambda _: None)
        with pytest.raises(UdsProtocolError, match="too short"):
            session.read_dtc_by_status_mask()

    def test_empty_request_rejected(self, session) -> None:
        with pytest.raises(UdsProtocolError):
            session.request(b"")

    @pytest.mark.parametrize("did", [-1, 0x10000])
    def test_did_range_checked(self, session, did: int) -> None:
        with pytest.raises(UdsProtocolError):
            session.read_data_by_identifier(did)

    def test_dtc_group_range_checked(self, session) -> None:
        with pytest.raises(UdsProtocolError):
            session.clear_diagnostic_information(0x1000000)


class TestFlushing:
    def test_buffer_is_flushed_before_each_request(self, ecm) -> None:
        transport = SimulatedTransport(ecm)
        session = UdsSession(transport, retries=0, timeout=0.05, sleep=lambda _: None)
        session.open()
        # Leave an unread reply behind, as a slow ECU would.
        transport.send(b"\x3e\x00")
        assert session.read_dtc_by_status_mask(0xFF) != []

    def test_send_before_open_is_an_error(self, ecm) -> None:
        with pytest.raises(TransportNotOpenError):
            SimulatedTransport(ecm).send(b"\x3e\x00")


class TestClear:
    def test_clear_all(self, session, ecm) -> None:
        session.clear_diagnostic_information()
        assert ecm.dtcs == []

    def test_clear_one_code(self, session, ecm) -> None:
        from majster_ai.mcp_servers.car_interface.dtc import encode_dtc_code

        session.clear_diagnostic_information(int.from_bytes(encode_dtc_code("P0299"), "big"))
        assert [dtc.code for dtc in ecm.dtcs] == ["P2015", "P0401"]

    def test_clearing_an_absent_code_is_harmless(self, session, ecm) -> None:
        from majster_ai.mcp_servers.car_interface.dtc import encode_dtc_code

        before = len(ecm.dtcs)
        session.clear_diagnostic_information(int.from_bytes(encode_dtc_code("P0123"), "big"))
        assert len(ecm.dtcs) == before


class TestDescribe:
    def test_reports_state(self, session) -> None:
        described = session.describe()
        assert described["module"] == "ECM"
        assert described["retries"] == 2
        assert described["transport"]["open"] is True

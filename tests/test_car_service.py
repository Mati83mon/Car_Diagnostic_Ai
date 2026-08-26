"""The car interface service -- and above all, the safety gate.

If any test in TestSafetyGate ever fails, the project has stopped being safe
to point at a vehicle. They are written to be blunt about that.
"""

from __future__ import annotations

import pytest

from majster_ai.config import load_settings
from majster_ai.mcp_servers.car_interface.backends import TransportFactory
from majster_ai.mcp_servers.car_interface.service import (
    CONFIRMATION_TTL_SECONDS,
    CarInterfaceService,
)


class TestReadDtc:
    def test_reads_the_simulated_faults(self, car) -> None:
        result = car.read_dtc("ECM")
        assert result["ok"] is True
        assert result["count"] == 3
        assert {dtc["code"] for dtc in result["dtcs"]} == {"P0299", "P2015", "P0401"}

    def test_summary_separates_confirmed_from_pending(self, car) -> None:
        summary = car.read_dtc("ECM")["summary"]
        assert "2 confirmed" in summary and "1 pending only" in summary

    def test_status_mask_filters(self, car) -> None:
        result = car.read_dtc("ECM", status_mask="confirmed")
        assert {dtc["code"] for dtc in result["dtcs"]} == {"P0299", "P2015"}

    def test_resolves_aliases_and_ids(self, car) -> None:
        for token in ("ECM", "engine", "0x7E0"):
            assert car.read_dtc(token)["module"] == "ECM"

    def test_healthy_module(self, settings, healthy_vehicle) -> None:
        service = CarInterfaceService(settings, factory=TransportFactory(settings, healthy_vehicle))
        result = service.read_dtc("ECM")
        assert result["count"] == 0
        assert "No DTCs" in result["summary"]
        service.close()

    def test_unknown_module_returns_an_error_with_alternatives(self, car) -> None:
        result = car.read_dtc("FLUX_CAPACITOR")
        assert result["ok"] is False
        assert result["error"] == "unknown_module"
        assert "ECM" in result["details"]["known_modules"]

    def test_absent_module_times_out_gracefully(self, car) -> None:
        result = car.read_dtc("PAM")
        assert result["ok"] is False
        assert result["error"] == "uds_timeout"
        # PAM's address is unverified, so silence is ambiguous -- say so.
        assert "UNVERIFIED" in result["hint"]

    def test_verified_flag_is_reported(self, car) -> None:
        assert car.read_dtc("ECM")["address_verified"] is True

    def test_read_all_modules(self, car) -> None:
        result = car.read_all_dtcs()
        assert result["ok"] is True
        assert "ECM" in result["modules_responded"]
        assert "PAM" in result["modules_not_responding"]
        assert result["total_dtcs"] >= 3

    def test_bad_status_mask(self, car) -> None:
        assert car.read_dtc("ECM", status_mask="recently")["ok"] is False


class TestReadLiveData:
    def test_reads_multiple_signals(self, car) -> None:
        result = car.read_live_data(["RPM", "COOLANT_TEMP", "MAF"])
        assert result["ok"] is True
        values = {entry["signal"]: entry["value"] for entry in result["values"]}
        assert values["RPM"] == 812.0
        assert values["COOLANT_TEMP"] == 88

    def test_accepts_a_comma_separated_string(self, car) -> None:
        assert len(car.read_live_data("RPM,COOLANT_TEMP")["values"]) == 2

    def test_partial_success_is_preserved(self, car) -> None:
        # One unsupported PID must not void the ones that worked.
        result = car.read_live_data(["RPM", "NOT_A_SIGNAL", "COOLANT_TEMP"])
        assert len(result["values"]) == 2
        assert len(result["failures"]) == 1
        assert result["ok"] is True

    def test_unsupported_pid_reported_per_signal(self, car) -> None:
        result = car.read_live_data(["RPM", "OIL_TEMP"], module_id="ABS")
        assert result["failures"], "ABS does not implement OBD PIDs"

    def test_empty_list_rejected(self, car) -> None:
        assert car.read_live_data([])["ok"] is False

    def test_implausible_reading_is_flagged_not_hidden(self, car, ecm) -> None:
        # An impossible value is itself diagnostic: sensor unplugged, open circuit.
        ecm.set_live_value(0x05, 215)
        entry = car.read_live_data(["COOLANT_TEMP"])["values"][0]
        assert entry["value"] == 215
        ecm.set_live_value(0x05, -40)
        entry = car.read_live_data(["COOLANT_TEMP"])["values"][0]
        assert entry["value"] == -40

    def test_summary_lists_readings(self, car) -> None:
        assert "RPM=812.0 rpm" in car.read_live_data(["RPM"])["summary"]

    def test_vehicle_info(self, car) -> None:
        values = {v["signal"]: v["value"] for v in car.vehicle_info()["values"]}
        assert values["VIN"] == "SALFA2BB8AH100001"


class TestReadDid:
    def test_raw_did(self, car) -> None:
        result = car.read_did("ECM", "F190")
        assert result["ok"] is True
        assert result["as_ascii"] == "SALFA2BB8AH100001"

    def test_unsupported_did(self, car) -> None:
        assert car.read_did("ECM", "1234")["ok"] is False

    def test_invalid_did_string(self, car) -> None:
        assert car.read_did("ECM", "ZZZZ")["error"] == "invalid_did"


class TestDiscovery:
    def test_scan_finds_the_populated_addresses(self, car) -> None:
        result = car.scan_modules(timeout=0.02)
        responding = {entry["module"] for entry in result["responding"]}
        assert {"ECM", "TCM", "ABS", "HALDEX"} <= responding
        assert "PAM" in {entry.get("module") for entry in result["not_responding"]}

    def test_scan_tells_you_what_to_do_with_the_result(self, car) -> None:
        assert "verified" in car.scan_modules(timeout=0.02)["next_step"]

    def test_list_modules_warns_about_unverified_addresses(self, car) -> None:
        result = car.list_modules()
        assert result["count"] == 11
        assert "verified=true" in result["note"]

    def test_list_signals(self, car) -> None:
        assert car.list_signals()["count"] > 20

    def test_status(self, car) -> None:
        status = car.status()
        assert status["safety_mode"] == "read_only"
        assert status["interface"]["backend"] == "virtual"


class TestSafetyGate:
    """Every one of these protects a real vehicle. Do not weaken them."""

    def test_read_only_refuses_writes_outright(self, car, ecm) -> None:
        result = car.clear_dtc("ECM")
        assert result["ok"] is False
        assert result["error"] == "safety_violation"
        assert len(ecm.dtcs) == 3, "codes were cleared in READ_ONLY mode"

    def test_read_only_issues_no_token_at_all(self, car) -> None:
        # No token means there is nothing for a confused model to redeem.
        result = car.clear_dtc("ECM")
        assert "confirmation_token" not in result
        assert car.pending_confirmations() == []

    def test_read_only_message_says_the_agent_cannot_change_it(self, car) -> None:
        assert "cannot change this itself" in car.clear_dtc("ECM")["message"]

    def test_first_call_refuses_and_returns_the_impact(self, write_car, ecm) -> None:
        result = write_car.clear_dtc("ECM")
        assert result["ok"] is False
        assert result["requires_confirmation"] is True
        assert result["confirmation_token"]
        assert len(ecm.dtcs) == 3, "phase 1 must not touch the vehicle"

    def test_impact_lists_exactly_what_would_be_erased(self, write_car) -> None:
        impact = write_car.clear_dtc("ECM")["impact"]
        assert {dtc["code"] for dtc in impact["dtcs_that_will_be_erased"]} == {
            "P0299",
            "P2015",
            "P0401",
        }
        assert impact["reversible"] is False
        assert any("freeze-frame" in risk.lower() for risk in impact["risks"])

    def test_impact_for_a_single_code_lists_only_that_code(self, write_car) -> None:
        impact = write_car.clear_dtc("ECM", dtc_code="P0299")["impact"]
        assert [dtc["code"] for dtc in impact["dtcs_that_will_be_erased"]] == ["P0299"]

    def test_safety_critical_modules_carry_an_extra_warning(self, write_car) -> None:
        impact = write_car.clear_dtc("ABS")["impact"]
        assert any("SAFETY-CRITICAL" in risk for risk in impact["risks"])

    def test_unverified_address_carries_a_warning(self, write_car) -> None:
        impact = write_car.clear_dtc("HALDEX")["impact"]
        assert any("UNVERIFIED" in risk for risk in impact["risks"])

    def test_valid_token_executes(self, write_car, ecm) -> None:
        token = write_car.clear_dtc("ECM")["confirmation_token"]
        result = write_car.clear_dtc("ECM", confirmation_token=token)
        assert result["ok"] is True
        assert result["cleared_count"] == 3
        assert ecm.dtcs == []

    def test_forged_token_refused(self, write_car, ecm) -> None:
        result = write_car.clear_dtc("ECM", confirmation_token="i-made-this-up")
        assert result["error"] == "safety_violation"
        assert len(ecm.dtcs) == 3

    def test_token_is_single_use(self, write_car) -> None:
        token = write_car.clear_dtc("ECM")["confirmation_token"]
        assert write_car.clear_dtc("ECM", confirmation_token=token)["ok"] is True
        assert write_car.clear_dtc("ECM", confirmation_token=token)["ok"] is False

    def test_token_cannot_be_replayed_on_another_module(self, write_car, vehicle) -> None:
        # Approval to clear the engine must never clear the airbag module.
        token = write_car.clear_dtc("ECM")["confirmation_token"]
        result = write_car.clear_dtc("ABS", confirmation_token=token)
        assert result["error"] == "safety_violation"
        assert "different set of arguments" in result["message"]
        assert len(vehicle.get(0x760).dtcs) == 1

    def test_token_cannot_be_widened_from_one_code_to_all(self, write_car, ecm) -> None:
        # Approving "clear P0299" must not authorise "clear everything".
        token = write_car.clear_dtc("ECM", dtc_code="P0299")["confirmation_token"]
        result = write_car.clear_dtc("ECM", confirmation_token=token)
        assert result["ok"] is False
        assert len(ecm.dtcs) == 3

    def test_token_expires(self, write_settings, vehicle) -> None:
        clock = {"now": 0.0}
        service = CarInterfaceService(
            write_settings,
            factory=TransportFactory(write_settings, vehicle),
            clock=lambda: clock["now"],
        )
        token = service.clear_dtc("ECM")["confirmation_token"]
        clock["now"] = CONFIRMATION_TTL_SECONDS + 1
        assert service.clear_dtc("ECM", confirmation_token=token)["ok"] is False
        assert len(vehicle.get(0x7E0).dtcs) == 3
        service.close()

    def test_pending_confirmations_are_listed(self, write_car) -> None:
        write_car.clear_dtc("ECM")
        pending = write_car.pending_confirmations()
        assert len(pending) == 1
        assert pending[0]["operation"] == "clear_dtc"

    def test_unattended_mode_skips_the_handshake(self, tmp_path, vehicle) -> None:
        # Documented bench-rig behaviour: both gates must be opened explicitly.
        settings = load_settings(
            write_enabled=True,
            require_approval=False,
            uds_timeout=0.05,
            uds_retries=0,
            log_level="CRITICAL",
        )
        service = CarInterfaceService(settings, factory=TransportFactory(settings, vehicle))
        assert service.clear_dtc("ECM")["ok"] is True
        assert vehicle.get(0x7E0).dtcs == []
        service.close()

    def test_single_code_clear(self, write_car, ecm) -> None:
        token = write_car.clear_dtc("ECM", dtc_code="P0299")["confirmation_token"]
        result = write_car.clear_dtc("ECM", dtc_code="P0299", confirmation_token=token)
        assert result["ok"] is True
        assert {dtc.code for dtc in ecm.dtcs} == {"P2015", "P0401"}

    def test_invalid_code_rejected_before_any_token(self, write_car) -> None:
        assert write_car.clear_dtc("ECM", dtc_code="NONSENSE")["ok"] is False

    def test_result_warns_that_clearing_repairs_nothing(self, write_car) -> None:
        token = write_car.clear_dtc("ECM")["confirmation_token"]
        result = write_car.clear_dtc("ECM", confirmation_token=token)
        assert "still present" in result["summary"] or "still present" in result["next_step"]

    def test_module_requiring_an_extended_session_still_clears(
        self, write_settings, vehicle
    ) -> None:
        service = CarInterfaceService(
            write_settings, factory=TransportFactory(write_settings, vehicle)
        )
        token = service.clear_dtc("ABS")["confirmation_token"]
        assert service.clear_dtc("ABS", confirmation_token=token)["ok"] is True
        assert vehicle.get(0x760).dtcs == []
        service.close()


class TestResilience:
    def test_transient_bus_faults_are_ridden_out(self, car, ecm) -> None:
        ecm.inject_faults(drop_next=1)
        assert car.read_dtc("ECM")["ok"] is True

    def test_persistent_failure_is_reported_not_raised(self, car, ecm) -> None:
        ecm.inject_faults(drop_next=99)
        result = car.read_dtc("ECM")
        assert result["ok"] is False and result["error"] == "uds_timeout"

    def test_service_can_be_closed_twice(self, car) -> None:
        car.close()
        car.close()

    def test_context_manager(self, settings, vehicle) -> None:
        with CarInterfaceService(settings, factory=TransportFactory(settings, vehicle)) as service:
            assert service.read_dtc("ECM")["ok"] is True

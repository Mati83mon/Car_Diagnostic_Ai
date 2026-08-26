"""The human-in-the-loop approval layer.

Every ambiguity resolves to refusal. That is the property under test.
"""

from __future__ import annotations

import pytest

from majster_ai.agent.hitl import (
    SAFETY_CRITICAL_MODULES,
    WRITE_TOOLS,
    ApprovalDecision,
    ApprovalRequest,
    AutoApproveApprover,
    AutoDenyApprover,
    CallbackApprover,
    ConsoleApprover,
    RiskLevel,
    assess_risk,
    build_approver,
    is_write_tool,
    record_approval,
    summarise_approvals,
)


@pytest.fixture
def impact() -> dict:
    return {
        "module": "ECM",
        "module_description": "Engine Control Module",
        "address": "0x7E0",
        "address_verified": True,
        "scope": "ALL stored DTCs",
        "reversible": False,
        "dtcs_that_will_be_erased": [
            {"full_code": "P0299-00", "code": "P0299", "description": "Underboost"},
            {"full_code": "P2015-00", "code": "P2015", "description": "Swirl flap"},
        ],
        "risks": ["Freeze-frame data will be lost.", "Readiness monitors reset."],
    }


@pytest.fixture
def request_(impact: dict) -> ApprovalRequest:
    return ApprovalRequest(
        "clear_dtc",
        {"module_id": "ECM"},
        impact,
        "tok",
        assess_risk("clear_dtc", {"module_id": "ECM"}, impact),
    )


class TestClassification:
    def test_clear_dtc_is_a_write(self) -> None:
        assert is_write_tool("clear_dtc")

    @pytest.mark.parametrize(
        "name",
        [
            "read_dtc",
            "read_all_dtcs",
            "read_live_data",
            "read_did",
            "scan_modules",
            "list_modules",
            "list_signals",
            "vehicle_info",
            "interface_status",
            "search_manual",
            "search_web",
            "ingest_manuals",
        ],
    )
    def test_read_tools_are_not_writes(self, name: str) -> None:
        assert not is_write_tool(name)

    def test_write_list_is_an_allow_list_of_writes(self) -> None:
        # A new tool added without updating this set is treated as read-only,
        # so forgetting causes a needless prompt, never an unguarded write.
        assert WRITE_TOOLS == frozenset({"clear_dtc"})


class TestRisk:
    def test_safety_critical_modules_are_high(self) -> None:
        for module in SAFETY_CRITICAL_MODULES:
            assert assess_risk("clear_dtc", {"module_id": module}, {}) is RiskLevel.HIGH

    def test_unverified_address_is_high(self, impact: dict) -> None:
        assert (
            assess_risk("clear_dtc", {"module_id": "CJB"}, {**impact, "address_verified": False})
            is RiskLevel.HIGH
        )

    def test_clear_all_is_medium(self, impact: dict) -> None:
        assert assess_risk("clear_dtc", {"module_id": "ECM"}, impact) is RiskLevel.MEDIUM

    def test_single_code_is_lower(self, impact: dict) -> None:
        assert (
            assess_risk("clear_dtc", {"module_id": "ECM", "dtc_code": "P0299"}, impact)
            is RiskLevel.LOW
        )


class TestRendering:
    def test_shows_what_will_be_erased(self, request_: ApprovalRequest) -> None:
        text = request_.render()
        assert "P0299-00" in text and "P2015-00" in text

    def test_shows_the_consequences(self, request_: ApprovalRequest) -> None:
        assert "Freeze-frame data will be lost." in request_.render()

    def test_marks_irreversibility(self, request_: ApprovalRequest) -> None:
        assert "Reversible: NO" in request_.render()

    def test_marks_an_unverified_address(self, impact: dict) -> None:
        request = ApprovalRequest(
            "clear_dtc", {"module_id": "CJB"}, {**impact, "address_verified": False}, "t"
        )
        assert "UNVERIFIED ADDRESS" in request.render()

    def test_handles_an_empty_code_list(self, impact: dict) -> None:
        request = ApprovalRequest("clear_dtc", {}, {**impact, "dtcs_that_will_be_erased": []}, "t")
        assert "may have no effect" in request.render()

    def test_token_is_not_rendered(self, request_: ApprovalRequest) -> None:
        assert "tok" not in request_.to_dict()["arguments"].values()


class TestConsoleApprover:
    @staticmethod
    def _approver(answer: str) -> ConsoleApprover:
        return ConsoleApprover(input_fn=lambda _p: answer, output_fn=lambda _t: None)

    @pytest.mark.parametrize("answer", ["yes", "YES", " Yes ", "approve", "tak"])
    def test_explicit_agreement_approves(self, answer: str, request_) -> None:
        assert self._approver(answer).request(request_).approved is True

    @pytest.mark.parametrize(
        "answer", ["y", "", "no", "n", "maybe", "ok", "sure", "1", "true", "Y"]
    )
    def test_everything_else_refuses(self, answer: str, request_) -> None:
        # A bare "y" is not enough: the operator must type the whole word.
        assert self._approver(answer).request(request_).approved is False

    def test_eof_refuses(self, request_) -> None:
        def raise_eof(_prompt: str) -> str:
            raise EOFError

        approver = ConsoleApprover(input_fn=raise_eof, output_fn=lambda _t: None)
        assert approver.request(request_).approved is False

    def test_keyboard_interrupt_refuses(self, request_) -> None:
        def raise_interrupt(_prompt: str) -> str:
            raise KeyboardInterrupt

        approver = ConsoleApprover(input_fn=raise_interrupt, output_fn=lambda _t: None)
        assert approver.request(request_).approved is False

    def test_high_risk_gets_an_extra_warning(self, impact: dict) -> None:
        lines: list[str] = []
        request = ApprovalRequest("clear_dtc", {"module_id": "RCM"}, impact, "t", RiskLevel.HIGH)
        ConsoleApprover(input_fn=lambda _p: "no", output_fn=lines.append).request(request)
        assert any("HIGH RISK" in line for line in lines)


class TestOtherApprovers:
    def test_auto_deny(self, request_) -> None:
        assert AutoDenyApprover().request(request_).approved is False

    def test_auto_approve(self, request_) -> None:
        assert AutoApproveApprover().request(request_).approved is True

    def test_callback_boolean(self, request_) -> None:
        assert CallbackApprover(lambda _r: True).request(request_).approved is True
        assert CallbackApprover(lambda _r: False).request(request_).approved is False

    def test_callback_returning_a_decision(self, request_) -> None:
        decision = CallbackApprover(lambda _r: ApprovalDecision.allow("because")).request(request_)
        assert decision.approved and decision.reason == "because"

    def test_callback_that_raises_fails_closed(self, request_) -> None:
        # An approver that crashes must never be read as consent.
        def boom(_r: ApprovalRequest) -> bool:
            raise RuntimeError("kaboom")

        assert CallbackApprover(boom).request(request_).approved is False

    def test_callback_receives_the_request(self, request_) -> None:
        seen: list[ApprovalRequest] = []
        CallbackApprover(lambda r: seen.append(r) or False).request(request_)
        assert seen[0].module == "ECM"


class TestApproverSelection:
    def test_non_interactive_session_auto_denies(self, monkeypatch) -> None:
        # An unattended run must never be able to authorise itself.
        monkeypatch.setattr("sys.stdin", None)
        assert isinstance(build_approver(), AutoDenyApprover)

    def test_approval_disabled_gives_auto_approve(self) -> None:
        assert isinstance(build_approver(require_approval=False), AutoApproveApprover)

    def test_interactive_tty_gives_the_console(self, monkeypatch) -> None:
        class FakeStdin:
            @staticmethod
            def isatty() -> bool:
                return True

        monkeypatch.setattr("sys.stdin", FakeStdin())
        assert isinstance(build_approver(), ConsoleApprover)


class TestAudit:
    def test_record_contains_both_sides(self, request_) -> None:
        entry = record_approval(request_, ApprovalDecision.allow("operator said yes"))
        assert entry["request"]["tool"] == "clear_dtc"
        assert entry["decision"]["approved"] is True
        assert entry["timestamp"] > 0

    def test_summary(self, request_) -> None:
        entries = [
            record_approval(request_, ApprovalDecision.allow()),
            record_approval(request_, ApprovalDecision.deny()),
        ]
        assert summarise_approvals(entries) == (
            "2 write operation(s) proposed: 1 approved, 1 declined."
        )

    def test_empty_summary(self) -> None:
        assert "No write operations" in summarise_approvals([])

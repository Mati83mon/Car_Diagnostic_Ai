"""End-to-end orchestrator tests.

These are the ones that matter most: they drive the real LangGraph graph, the
real MCP tool definitions and the real service layer against a simulated
vehicle, and assert that nothing reaches the bus without a human saying yes.
"""

from __future__ import annotations

import pytest
from fakes import ScriptedChatModel, tool_call

from majster_ai.agent.graph import _decision_from, build_graph, interrupts_from_result
from majster_ai.agent.hitl import (
    ApprovalDecision,
    AutoDenyApprover,
    CallbackApprover,
    ConsoleApprover,
)
from majster_ai.agent.runner import DiagnosticSession
from majster_ai.agent.toolkit import build_local_toolkit
from majster_ai.errors import MajsterError
from majster_ai.mcp_servers.car_interface.backends import TransportFactory
from majster_ai.mcp_servers.car_interface.service import CarInterfaceService


@pytest.fixture
def toolkit(settings, vehicle):
    car = CarInterfaceService(settings, factory=TransportFactory(settings, vehicle))
    kit = build_local_toolkit(settings, car=car, include_rag=False, include_web=False)
    yield kit
    kit.close()


@pytest.fixture
def write_toolkit(write_settings, vehicle):
    car = CarInterfaceService(write_settings, factory=TransportFactory(write_settings, vehicle))
    kit = build_local_toolkit(write_settings, car=car, include_rag=False, include_web=False)
    yield kit
    kit.close()


def session(settings, toolkit, responses, approver=None) -> DiagnosticSession:
    return DiagnosticSession(
        settings=settings,
        toolkit=toolkit,
        llm=ScriptedChatModel(responses),
        approver=approver or AutoDenyApprover(),
    )


class TestToolkit:
    def test_wraps_every_mcp_tool(self, toolkit) -> None:
        assert "read_dtc" in toolkit.names()
        assert "clear_dtc" in toolkit.names()

    def test_tool_returns_data_not_exceptions(self, toolkit) -> None:
        # A raised exception ends the session; an error dict lets the model
        # recover and try something else.
        result = toolkit.get("read_dtc").invoke({"module_id": "NOT_A_MODULE"})
        assert result["ok"] is False and result["error"] == "unknown_module"

    def test_schema_is_carried_over_from_mcp(self, toolkit) -> None:
        assert set(toolkit.get("read_dtc").args) == {"module_id", "status_mask"}


class TestReadOnlyConversation:
    def test_reads_faults_and_answers(self, settings, toolkit) -> None:
        chat = session(
            settings,
            toolkit,
            [
                [tool_call("read_dtc", {"module_id": "ECM"})],
                "Three codes: P0299, P2015 and a pending P0401.",
            ],
        )
        result = chat.ask("What faults are stored?")
        assert result.tools_used == ["read_dtc"]
        assert "P0299" in result.answer
        assert result.interrupted is False

    def test_multi_step_investigation(self, settings, toolkit) -> None:
        chat = session(
            settings,
            toolkit,
            [
                [tool_call("read_dtc", {"module_id": "ECM"})],
                [tool_call("read_live_data", {"pid_list": ["MAP", "BAROMETRIC_PRESSURE"]})],
                "Manifold pressure equals barometric at idle, consistent with underboost.",
            ],
        )
        result = chat.ask("Is the turbo actually making boost?")
        assert result.tools_used == ["read_dtc", "read_live_data"]

    def test_parallel_tool_calls_in_one_turn(self, settings, toolkit) -> None:
        chat = session(
            settings,
            toolkit,
            [
                [
                    tool_call("read_dtc", {"module_id": "ECM"}, "a"),
                    tool_call("read_dtc", {"module_id": "TCM"}, "b"),
                ],
                "Faults in both modules.",
            ],
        )
        assert len(chat.ask("check both").tool_calls) == 2

    def test_unknown_tool_is_reported_to_the_model(self, settings, toolkit) -> None:
        chat = session(
            settings,
            toolkit,
            [
                [tool_call("fly_to_the_moon", {})],
                "That tool does not exist.",
            ],
        )
        result = chat.ask("do something impossible")
        assert "unknown_tool" in str(result.messages[-2].content)

    def test_iteration_ceiling_stops_a_loop(self, settings, toolkit) -> None:
        # A model looping on read_dtc forever is a real, and on a metered API
        # expensive, failure mode.
        graph = build_graph(
            ScriptedChatModel([[tool_call("read_dtc", {"module_id": "ECM"})]] * 50),
            toolkit,
            max_iterations=3,
        )
        chat = DiagnosticSession(
            settings=settings, toolkit=toolkit, llm=ScriptedChatModel([]), graph=graph
        )
        result = chat.ask("loop forever")
        assert "limit of 3 tool calls" in result.answer


class TestWriteGate:
    """Nothing may reach the bus without an explicit human yes."""

    CLEAR_SCRIPT = [
        [tool_call("clear_dtc", {"module_id": "ECM"})],
        "Done.",
    ]

    def test_read_only_never_reaches_the_approver(self, settings, toolkit, ecm) -> None:
        def must_not_run(_request):
            raise AssertionError("the approver was consulted in READ_ONLY mode")

        chat = session(settings, toolkit, list(self.CLEAR_SCRIPT), CallbackApprover(must_not_run))
        result = chat.ask("clear the codes")
        assert result.interrupted is False
        assert len(ecm.dtcs) == 3

    def test_approval_pauses_the_graph(self, write_settings, write_toolkit) -> None:
        seen = []
        chat = session(
            write_settings,
            write_toolkit,
            list(self.CLEAR_SCRIPT),
            CallbackApprover(lambda r: seen.append(r) or True),
        )
        result = chat.ask("clear the codes")
        assert result.interrupted is True
        assert seen[0].tool_name == "clear_dtc"

    def test_human_sees_exactly_what_would_be_erased(self, write_settings, write_toolkit) -> None:
        seen = []
        chat = session(
            write_settings,
            write_toolkit,
            list(self.CLEAR_SCRIPT),
            CallbackApprover(lambda r: seen.append(r) or False),
        )
        chat.ask("clear the codes")
        assert set(seen[0].affected_codes()) == {"P0299-00", "P2015-00", "P0401-00"}
        assert seen[0].risks()

    def test_approval_executes_the_write(self, write_settings, write_toolkit, ecm) -> None:
        chat = session(
            write_settings,
            write_toolkit,
            list(self.CLEAR_SCRIPT),
            CallbackApprover(lambda _r: True),
        )
        result = chat.ask("clear the codes")
        assert ecm.dtcs == []
        assert result.approvals[0]["decision"]["approved"] is True

    def test_refusal_writes_nothing(self, write_settings, write_toolkit, ecm) -> None:
        chat = session(
            write_settings,
            write_toolkit,
            list(self.CLEAR_SCRIPT),
            CallbackApprover(lambda _r: False),
        )
        result = chat.ask("clear the codes")
        assert len(ecm.dtcs) == 3
        assert result.approvals[0]["decision"]["approved"] is False

    def test_refusal_tells_the_model_not_to_retry(self, write_settings, write_toolkit) -> None:
        chat = session(
            write_settings,
            write_toolkit,
            list(self.CLEAR_SCRIPT),
            CallbackApprover(lambda _r: False),
        )
        result = chat.ask("clear the codes")
        tool_message = next(
            message for message in result.messages if getattr(message, "name", None) == "clear_dtc"
        )
        assert "Do not retry" in str(tool_message.content)

    def test_no_human_present_denies(self, write_settings, write_toolkit, ecm) -> None:
        chat = session(write_settings, write_toolkit, list(self.CLEAR_SCRIPT), AutoDenyApprover())
        chat.ask("clear the codes")
        assert len(ecm.dtcs) == 3

    def test_crashing_approver_denies(self, write_settings, write_toolkit, ecm) -> None:
        def boom(_request):
            raise RuntimeError("the UI fell over")

        chat = session(
            write_settings, write_toolkit, list(self.CLEAR_SCRIPT), CallbackApprover(boom)
        )
        chat.ask("clear the codes")
        assert len(ecm.dtcs) == 3

    def test_console_refusal_by_default(self, write_settings, write_toolkit, ecm) -> None:
        approver = ConsoleApprover(input_fn=lambda _p: "", output_fn=lambda _t: None)
        chat = session(write_settings, write_toolkit, list(self.CLEAR_SCRIPT), approver)
        chat.ask("clear the codes")
        assert len(ecm.dtcs) == 3

    FORGED_SCRIPT = [
        [tool_call("clear_dtc", {"module_id": "ECM", "confirmation_token": "totally-legitimate"})],
        "Hmm.",
    ]

    def test_a_model_supplied_token_does_not_skip_the_human(
        self, write_settings, write_toolkit
    ) -> None:
        """The graph discards any token the model supplies and runs its own
        handshake. Honouring one would let a model replay an approval granted
        for some earlier operation."""
        seen = []
        chat = session(
            write_settings,
            write_toolkit,
            list(self.FORGED_SCRIPT),
            CallbackApprover(lambda r: seen.append(r) or False),
        )
        chat.ask("clear with my token")
        assert seen, "the model's token bypassed the approval pause"

    def test_forged_token_with_refusal_writes_nothing(
        self, write_settings, write_toolkit, ecm
    ) -> None:
        chat = session(
            write_settings,
            write_toolkit,
            list(self.FORGED_SCRIPT),
            CallbackApprover(lambda _r: False),
        )
        chat.ask("clear with my token")
        assert len(ecm.dtcs) == 3

    def test_forged_token_reaching_the_service_directly_is_refused(
        self, write_toolkit, ecm
    ) -> None:
        # Belt and braces: even bypassing the graph entirely, the service
        # rejects a token it did not issue.
        result = write_toolkit.get("clear_dtc").invoke(
            {"module_id": "ECM", "confirmation_token": "totally-legitimate"}
        )
        assert result["error"] == "safety_violation"
        assert len(ecm.dtcs) == 3

    def test_single_code_clear_is_scoped(self, write_settings, write_toolkit, ecm) -> None:
        chat = session(
            write_settings,
            write_toolkit,
            [[tool_call("clear_dtc", {"module_id": "ECM", "dtc_code": "P0299"})], "Done."],
            CallbackApprover(lambda _r: True),
        )
        chat.ask("clear P0299 only")
        assert {dtc.code for dtc in ecm.dtcs} == {"P2015", "P0401"}

    def test_read_tools_are_not_gated(self, write_settings, write_toolkit) -> None:
        def must_not_run(_request):
            raise AssertionError("a read tool asked for approval")

        chat = session(
            write_settings,
            write_toolkit,
            [[tool_call("read_dtc", {"module_id": "ECM"})], "Three codes."],
            CallbackApprover(must_not_run),
        )
        assert chat.ask("what faults?").interrupted is False

    def test_approval_is_recorded_for_audit(self, write_settings, write_toolkit) -> None:
        chat = session(
            write_settings,
            write_toolkit,
            list(self.CLEAR_SCRIPT),
            CallbackApprover(lambda _r: True),
        )
        entry = chat.ask("clear").approvals[0]
        assert entry["request"]["module"] == "ECM"
        assert entry["timestamp"] > 0


class TestDecisionParsing:
    """An ambiguous resume value must never be read as consent."""

    @pytest.mark.parametrize(
        ("value", "approved"),
        [
            (True, True),
            (False, False),
            ({"approved": True}, True),
            ({"approved": False}, False),
            ({}, False),
            ("yes", True),
            ("no", False),
            ("", False),
            ("maybe", False),
            (None, False),
            (123, False),
            ([], False),
            (ApprovalDecision.allow(), True),
            (ApprovalDecision.deny(), False),
        ],
    )
    def test_only_explicit_agreement_approves(self, value, approved: bool) -> None:
        assert _decision_from(value).approved is approved


class TestSessionPlumbing:
    def test_empty_question(self, settings, toolkit) -> None:
        assert "Ask me something" in session(settings, toolkit, []).ask("").answer

    def test_history_is_kept_across_turns(self, settings, toolkit) -> None:
        chat = session(settings, toolkit, ["First answer.", "Second answer."])
        chat.ask("one")
        chat.ask("two")
        assert len(chat.history()) >= 4

    def test_context_tells_the_model_it_is_a_simulator(self, settings, toolkit) -> None:
        chat = session(settings, toolkit, ["ok"])
        assert "SIMULATED VEHICLE" in chat._session_context()

    def test_context_states_read_only(self, settings, toolkit) -> None:
        assert "Writes are disabled" in session(settings, toolkit, [])._session_context()

    def test_context_lists_unverified_modules(self, settings, toolkit) -> None:
        assert "UNVERIFIED" in session(settings, toolkit, [])._session_context()

    def test_graph_needs_tools(self, settings) -> None:
        from majster_ai.agent.toolkit import Toolkit

        with pytest.raises(MajsterError, match="no tools"):
            build_graph(ScriptedChatModel([]), Toolkit())

    def test_model_without_tool_support_is_rejected_clearly(self, toolkit) -> None:
        class NoTools(ScriptedChatModel):
            def bind_tools(self, tools, **kwargs):
                raise NotImplementedError("this model cannot call tools")

        with pytest.raises(MajsterError, match="tool-calling model"):
            build_graph(NoTools([]), toolkit)

    def test_answer_extraction_from_claude_block_content(self, settings, toolkit) -> None:
        from langchain_core.messages import AIMessage

        chat = session(
            settings,
            toolkit,
            [
                AIMessage(
                    content=[
                        {"type": "thinking", "thinking": "hidden reasoning"},
                        {"type": "text", "text": "The visible answer."},
                    ]
                )
            ],
        )
        assert chat.ask("q").answer == "The visible answer."

    def test_no_interrupts_in_a_plain_result(self) -> None:
        assert interrupts_from_result({"messages": []}) == []

"""The console REPL and the session loop around the graph."""

from __future__ import annotations

import pytest
from fakes import ScriptedChatModel, tool_call

from majster_ai.agent.hitl import AutoDenyApprover, CallbackApprover
from majster_ai.agent.runner import (
    MAX_APPROVAL_ROUNDS,
    DiagnosticSession,
    TurnResult,
    run_console,
)
from majster_ai.agent.toolkit import build_local_toolkit
from majster_ai.mcp_servers.car_interface.backends import TransportFactory
from majster_ai.mcp_servers.car_interface.service import CarInterfaceService


@pytest.fixture
def toolkit(settings, vehicle):
    car = CarInterfaceService(settings, factory=TransportFactory(settings, vehicle))
    kit = build_local_toolkit(settings, car=car, include_rag=False, include_web=False)
    yield kit
    kit.close()


def make_session(settings, toolkit, responses, approver=None) -> DiagnosticSession:
    return DiagnosticSession(
        settings=settings,
        toolkit=toolkit,
        llm=ScriptedChatModel(responses),
        approver=approver or AutoDenyApprover(),
    )


class TestConsole:
    def test_greets_and_exits(self, settings, toolkit) -> None:
        lines: list[str] = []
        session = make_session(settings, toolkit, [])
        assert (
            run_console(
                session, settings=settings, input_fn=lambda _p: "exit", output_fn=lines.append
            )
            == 0
        )
        text = "\n".join(lines)
        assert "Majster-AI" in text
        assert "READ_ONLY" in text

    def test_warns_that_the_vehicle_is_simulated(self, settings, toolkit) -> None:
        lines: list[str] = []
        run_console(
            make_session(settings, toolkit, []),
            settings=settings,
            input_fn=lambda _p: "quit",
            output_fn=lines.append,
        )
        assert any("simulator" in line for line in lines)

    def test_warns_when_writes_are_enabled(self, write_settings, toolkit) -> None:
        lines: list[str] = []
        run_console(
            make_session(write_settings, toolkit, []),
            settings=write_settings,
            input_fn=lambda _p: "exit",
            output_fn=lines.append,
        )
        assert any("writes are ENABLED" in line for line in lines)

    def test_answers_a_question(self, settings, toolkit) -> None:
        lines: list[str] = []
        answers = iter(["what faults?", "exit"])
        run_console(
            make_session(
                settings,
                toolkit,
                [[tool_call("read_dtc", {"module_id": "ECM"})], "Three codes are stored."],
            ),
            settings=settings,
            input_fn=lambda _p: next(answers),
            output_fn=lines.append,
        )
        text = "\n".join(lines)
        assert "Three codes are stored." in text
        assert "tools: read_dtc" in text

    def test_blank_input_is_ignored(self, settings, toolkit) -> None:
        answers = iter(["", "  ", "exit"])
        assert (
            run_console(
                make_session(settings, toolkit, []),
                settings=settings,
                input_fn=lambda _p: next(answers),
                output_fn=lambda _t: None,
            )
            == 0
        )

    def test_eof_ends_the_session(self, settings, toolkit) -> None:
        def raise_eof(_prompt: str) -> str:
            raise EOFError

        assert (
            run_console(
                make_session(settings, toolkit, []),
                settings=settings,
                input_fn=raise_eof,
                output_fn=lambda _t: None,
            )
            == 0
        )

    def test_keyboard_interrupt_ends_the_session(self, settings, toolkit) -> None:
        def raise_interrupt(_prompt: str) -> str:
            raise KeyboardInterrupt

        assert (
            run_console(
                make_session(settings, toolkit, []),
                settings=settings,
                input_fn=raise_interrupt,
                output_fn=lambda _t: None,
            )
            == 0
        )

    def test_a_failing_turn_does_not_kill_the_session(self, settings, toolkit) -> None:
        """One bad answer must not end a diagnostic session -- the mechanic is
        halfway through a job."""

        class Exploding(ScriptedChatModel):
            def _generate(self, *args, **kwargs):
                raise RuntimeError("model exploded")

        session = DiagnosticSession(
            settings=settings, toolkit=toolkit, llm=Exploding([]), approver=AutoDenyApprover()
        )
        lines: list[str] = []
        answers = iter(["a question", "exit"])
        assert (
            run_console(
                session,
                settings=settings,
                input_fn=lambda _p: next(answers),
                output_fn=lines.append,
            )
            == 0
        )
        assert any("model exploded" in line for line in lines)


class TestSession:
    def test_turn_result_reports_tools(self, settings, toolkit) -> None:
        session = make_session(
            settings, toolkit, [[tool_call("read_dtc", {"module_id": "ECM"})], "done"]
        )
        assert session.ask("q").tools_used == ["read_dtc"]

    def test_thread_id_is_stable(self, settings, toolkit) -> None:
        session = make_session(settings, toolkit, ["a", "b"])
        first = session.thread_id
        session.ask("one")
        assert session.thread_id == first

    def test_separate_sessions_do_not_share_history(self, settings, toolkit) -> None:
        one = make_session(settings, toolkit, ["first"])
        two = make_session(settings, toolkit, ["second"])
        one.ask("q")
        assert two.history() == []

    def test_close_is_safe_twice(self, settings, vehicle) -> None:
        car = CarInterfaceService(settings, factory=TransportFactory(settings, vehicle))
        kit = build_local_toolkit(settings, car=car, include_rag=False, include_web=False)
        session = make_session(settings, kit, [])
        session.close()
        session.close()

    def test_context_manager(self, settings, vehicle) -> None:
        car = CarInterfaceService(settings, factory=TransportFactory(settings, vehicle))
        kit = build_local_toolkit(settings, car=car, include_rag=False, include_web=False)
        with make_session(settings, kit, ["answer"]) as session:
            assert session.ask("q").answer == "answer"

    def test_approval_loop_has_a_ceiling(self, write_settings, vehicle) -> None:
        """Repeated approval prompts would wear down exactly the human
        attention the gate depends on."""
        car = CarInterfaceService(write_settings, factory=TransportFactory(write_settings, vehicle))
        kit = build_local_toolkit(write_settings, car=car, include_rag=False, include_web=False)
        script = [[tool_call("clear_dtc", {"module_id": "ECM"})]] * (MAX_APPROVAL_ROUNDS + 5)
        session = DiagnosticSession(
            settings=write_settings,
            toolkit=kit,
            llm=ScriptedChatModel(script),
            approver=CallbackApprover(lambda _r: False),
            max_approval_rounds=2,
        )
        result = session.ask("clear repeatedly")
        assert "Nothing was written" in result.answer or result.interrupted
        kit.close()


class TestTurnResult:
    def test_tools_used(self) -> None:
        result = TurnResult(answer="x", tool_calls=[{"tool": "read_dtc", "ok": True}])
        assert result.tools_used == ["read_dtc"]

    def test_defaults(self) -> None:
        result = TurnResult(answer="x")
        assert result.approvals == [] and result.interrupted is False

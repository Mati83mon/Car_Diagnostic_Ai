"""Driving the graph: one turn, an approval pause, a resume, an answer.

:class:`DiagnosticSession` owns the interrupt/resume loop so that callers --
the console REPL, a test, or some future GUI -- only have to supply an
:class:`~majster_ai.agent.hitl.Approver`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.types import Command

from majster_ai.agent.graph import build_graph, interrupts_from_result
from majster_ai.agent.hitl import (
    ApprovalRequest,
    Approver,
    RiskLevel,
    build_approver,
    summarise_approvals,
)
from majster_ai.agent.llm import build_llm, describe_llm
from majster_ai.agent.prompts import WELCOME_BANNER
from majster_ai.agent.toolkit import Toolkit, build_local_toolkit
from majster_ai.config import Settings, get_settings
from majster_ai.errors import MajsterError
from majster_ai.logging_setup import get_logger

log = get_logger("agent.runner")

#: A turn should never need more than a handful of approval pauses. More than
#: this means something is looping, and looping on approval prompts would wear
#: down exactly the human attention the gate depends on.
MAX_APPROVAL_ROUNDS = 10


def _request_from_payload(payload: dict[str, Any]) -> ApprovalRequest:
    """Rebuild an :class:`ApprovalRequest` from an interrupt payload."""
    impact = dict(payload.get("impact") or {})
    try:
        risk = RiskLevel(str(payload.get("risk", "medium")))
    except ValueError:
        risk = RiskLevel.MEDIUM
    return ApprovalRequest(
        tool_name=str(payload.get("tool", "unknown")),
        arguments=dict(payload.get("arguments") or {}),
        impact=impact,
        # The token stays server-side; the graph redeems it on resume.
        confirmation_token="",
        risk=risk,
    )


@dataclass
class TurnResult:
    """What one question produced."""

    answer: str
    messages: list[BaseMessage] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    interrupted: bool = False

    @property
    def tools_used(self) -> list[str]:
        return [call["tool"] for call in self.tool_calls]


class DiagnosticSession:
    """A conversation with the agent, with the approval loop handled."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        toolkit: Toolkit | None = None,
        llm: Any | None = None,
        approver: Approver | None = None,
        thread_id: str | None = None,
        graph: Any | None = None,
        max_approval_rounds: int = MAX_APPROVAL_ROUNDS,
    ) -> None:
        self.settings = settings or get_settings()
        self.toolkit = toolkit or build_local_toolkit(self.settings)
        self.llm = llm or build_llm(self.settings)
        self.approver = approver or build_approver(require_approval=self.settings.require_approval)
        self.thread_id = thread_id or str(uuid.uuid4())
        self.max_approval_rounds = max_approval_rounds
        self.graph = graph or build_graph(
            self.llm,
            self.toolkit,
            extra_context=self._session_context(),
        )

    def _session_context(self) -> str:
        """Facts about this run that the model should know up front."""
        car = self.toolkit.car
        lines = [
            f"Interface backend: {self.settings.can_backend.value}"
            + (
                "  (SIMULATED VEHICLE - readings are synthetic, not from a real car. "
                "Say so if the operator seems to think otherwise.)"
                if not self.settings.can_backend.is_physical
                else "  (LIVE VEHICLE)"
            ),
            f"Safety mode: {self.settings.safety_mode.value}",
        ]
        if self.settings.is_read_only:
            lines.append(
                "Writes are disabled entirely. clear_dtc will be refused - do not "
                "offer it as an option; tell the operator it is disabled by "
                "configuration if they ask."
            )
        if car is not None:
            unverified = [module.name for module in car.modules.unverified()]
            if unverified:
                lines.append(
                    "Modules with UNVERIFIED addresses (silence from these may just "
                    "mean the address is wrong): " + ", ".join(unverified)
                )
        return "\n".join(f"- {line}" for line in lines)

    @property
    def config(self) -> dict[str, Any]:
        return {"configurable": {"thread_id": self.thread_id}}

    # -- one turn -----------------------------------------------------------
    def ask(self, question: str) -> TurnResult:
        """Put a question to the agent, pausing for approval where required."""
        if not question or not question.strip():
            return TurnResult(answer="Ask me something about the vehicle.")

        try:
            result = self.graph.invoke({"messages": [HumanMessage(content=question)]}, self.config)
        except MajsterError as exc:
            return TurnResult(answer=f"I could not run that: {exc.message}")

        rounds = 0
        interrupted = False
        while True:
            payloads = interrupts_from_result(result)
            if not payloads:
                break
            interrupted = True
            rounds += 1
            if rounds > self.max_approval_rounds:
                log.error("Too many approval rounds (%d) - abandoning the turn", rounds)
                return TurnResult(
                    answer=(
                        "I stopped: this turn kept asking for write approval in a "
                        "loop, which suggests something is wrong. Nothing was "
                        "written to the vehicle."
                    ),
                    interrupted=True,
                )

            decision = self.approver.request(_request_from_payload(payloads[0]))
            result = self.graph.invoke(Command(resume=decision.to_dict()), self.config)

        messages = list(result.get("messages", []))
        return TurnResult(
            answer=self._final_text(messages),
            messages=messages,
            approvals=list(result.get("approvals", [])),
            tool_calls=list(result.get("tool_calls", [])),
            interrupted=interrupted,
        )

    @staticmethod
    def _final_text(messages: list[BaseMessage]) -> str:
        """The last assistant text, flattening Claude's block content."""
        for message in reversed(messages):
            if not isinstance(message, AIMessage):
                continue
            content = message.content
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                # Claude returns a list of blocks; thinking blocks are skipped.
                parts = [
                    str(block.get("text", ""))
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                joined = "\n".join(part for part in parts if part.strip())
                if joined.strip():
                    return joined.strip()
        return "(no answer produced)"

    def history(self) -> list[BaseMessage]:
        """The full conversation so far."""
        try:
            state = self.graph.get_state(self.config)
        except Exception:  # pragma: no cover - checkpointer dependent
            return []
        return list((state.values or {}).get("messages", []))

    def close(self) -> None:
        self.toolkit.close()

    def __enter__(self) -> DiagnosticSession:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def run_console(
    session: DiagnosticSession | None = None,
    *,
    settings: Settings | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> int:
    """Interactive REPL. Returns a process exit code."""
    settings = settings or get_settings()
    owned = session is None
    try:
        session = session or DiagnosticSession(settings=settings)
    except MajsterError as exc:
        output_fn(f"Cannot start: {exc.message}")
        return 1

    llm_info = describe_llm(settings)
    output_fn(
        WELCOME_BANNER.format(
            vehicle="Land Rover Freelander 2 (2010, 2.2 TD4)",
            backend=settings.can_backend.value,
            channel=settings.can_channel,
            safety_mode=settings.safety_mode.value.upper(),
        )
    )
    output_fn(f"LLM: {llm_info['provider']} / {llm_info['model']}")
    if not settings.can_backend.is_physical:
        output_fn("NOTE: running against the built-in simulator. Readings are synthetic.")
    if settings.is_read_only:
        output_fn("NOTE: READ_ONLY. Write operations will be refused.\n")
    else:
        output_fn(
            "WARNING: writes are ENABLED. Every write still needs your explicit " "approval.\n"
        )

    try:
        while True:
            try:
                question = input_fn("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                output_fn("")
                break
            if not question:
                continue
            if question.lower() in {"exit", "quit", "q"}:
                break

            try:
                result = session.ask(question)
            except MajsterError as exc:
                output_fn(f"\nerror: {exc.message}\n")
                continue
            except Exception as exc:  # a bad turn must not kill the session
                log.exception("Turn failed")
                output_fn(f"\nerror: {type(exc).__name__}: {exc}\n")
                continue

            output_fn(f"\nmajster> {result.answer}\n")
            if result.tools_used:
                output_fn(f"  (tools: {', '.join(result.tools_used)})\n")
    finally:
        if owned:
            approvals = []
            try:
                state = session.graph.get_state(session.config)
                approvals = (state.values or {}).get("approvals", [])
            except Exception:  # pragma: no cover - checkpointer dependent
                pass
            if approvals:
                output_fn(summarise_approvals(approvals))
            session.close()
    return 0


def iter_messages(result: TurnResult) -> Iterator[str]:
    """Render a turn's messages for debugging."""
    for message in result.messages:
        yield f"{type(message).__name__}: {str(message.content)[:200]}"


__all__ = ["DiagnosticSession", "TurnResult", "run_console", "MAX_APPROVAL_ROUNDS"]

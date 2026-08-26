"""The LangGraph orchestrator.

Shape of the graph::

            +-----------+
            |   agent   |  LLM with tools bound
            +-----------+
              |       ^
     tool_calls?      | tool results
              v       |
            +-----------+
            |   tools   |  read tools run; write tools stop and ask
            +-----------+
                  |
             interrupt()  <- execution pauses here for a human
                  |
                (resume)

The interesting node is ``tools``. Read tools execute straight away. A write
tool is handled in two phases:

1. The tool is called **without** a confirmation token. The service refuses and
   returns an impact summary plus a single-use token. Nothing has touched the
   vehicle.
2. ``interrupt()`` suspends the graph and hands that summary to the caller. A
   human decides. On resume, an approval redeems the token; a refusal returns
   a plain "declined" result to the model and the conversation carries on.

The dry run in phase 1 is deliberately side-effect free, which matters because
LangGraph re-executes a node from the top when it resumes: any work before
``interrupt()`` happens twice. Reading DTCs twice is harmless. Clearing them
twice would not be, and that is precisely why the clear cannot happen until
after the interrupt.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from majster_ai.agent.hitl import (
    ApprovalDecision,
    ApprovalRequest,
    assess_risk,
    is_write_tool,
    record_approval,
)
from majster_ai.agent.prompts import build_system_prompt
from majster_ai.agent.state import DiagnosticState
from majster_ai.agent.toolkit import Toolkit
from majster_ai.errors import MajsterError
from majster_ai.logging_setup import get_logger, log_agent_step

log = get_logger("agent.graph")

#: Hard ceiling on agent/tool round-trips per turn. A model that loops calling
#: read_dtc forever is a real failure mode, and on a metered API it is an
#: expensive one.
DEFAULT_MAX_ITERATIONS = 25

#: Returned to the model when the operator declines a write.
DENIED_RESULT_TEMPLATE = {
    "ok": False,
    "error": "approval_denied",
    "requires_confirmation": True,
}


def _tool_lookup(tools: Sequence[BaseTool]) -> dict[str, BaseTool]:
    return {tool.name: tool for tool in tools}


def _as_dict(result: Any) -> dict[str, Any]:
    """Tool results are dictionaries by contract; be tolerant if one is not."""
    return result if isinstance(result, dict) else {"ok": True, "result": result}


def handle_write_tool(
    tool: BaseTool, name: str, arguments: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The two-phase write handshake, with the human in the middle.

    Returns ``(result, approval_record)``. The record is ``None`` when no
    approval was needed -- either writes are disabled entirely, or the operator
    has switched approval off for a bench rig.

    Module-level rather than nested in :func:`build_graph` so it can be tested
    on its own, and so the graph builder stays readable.
    """
    # Phase 1: a side-effect-free dry run that yields the impact and a token.
    # Any token the model supplied is discarded here: honouring one would let
    # a model replay an approval granted for some earlier operation.
    preview = _as_dict(tool.invoke({**arguments, "confirmation_token": None}))

    if not preview.get("requires_confirmation"):
        # Either writes are disabled entirely (a refusal we pass straight
        # back), or approval is switched off and the tool already ran.
        return preview, None

    token = str(preview.get("confirmation_token") or "")
    impact = dict(preview.get("impact") or {})
    request = ApprovalRequest(
        tool_name=name,
        arguments=arguments,
        impact=impact,
        confirmation_token=token,
        risk=assess_risk(name, arguments, impact),
    )

    log_agent_step(
        "safety.pause",
        f"Execution paused: {name} on {request.module} needs human approval",
        tool=name,
        risk=request.risk.value,
    )

    # Phase 2: suspend the graph. Everything above this line runs again
    # verbatim on resume, which is safe precisely because nothing above it
    # writes to the vehicle.
    answer = interrupt(
        {
            "type": "approval_request",
            "tool": name,
            "arguments": request.to_dict()["arguments"],
            "module": request.module,
            "risk": request.risk.value,
            "impact": impact,
            "prompt": request.render(),
            "message": (
                f"{name} on {request.module} requires explicit human approval "
                f"before it can run."
            ),
        }
    )

    decision = _decision_from(answer)
    record = record_approval(request, decision)

    if not decision.approved:
        return (
            {
                **DENIED_RESULT_TEMPLATE,
                "tool": name,
                "module": request.module,
                "message": (
                    f"The operator declined this operation ({decision.reason}). "
                    f"Nothing was written to the vehicle. Do not retry it; "
                    f"continue diagnosing, or ask what they would prefer."
                ),
            },
            record,
        )

    # Phase 3: redeem the token. This is the only line that writes.
    result = _as_dict(tool.invoke({**arguments, "confirmation_token": token}))
    return result, record


def build_graph(
    llm: Any,
    toolkit: Toolkit,
    *,
    system_prompt: str | None = None,
    checkpointer: Any | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    extra_context: str | None = None,
) -> Any:
    """Compile the diagnostic graph.

    Args:
        llm: A chat model supporting tool calling.
        toolkit: The tools to expose.
        system_prompt: Override the default prompt.
        checkpointer: LangGraph checkpointer. One is required for ``interrupt``
            to work, so an :class:`InMemorySaver` is created if none is given.
        max_iterations: Ceiling on agent/tool round-trips per turn.
        extra_context: Session context appended to the system prompt.

    Returns:
        A compiled graph. Invoke it with a ``config`` carrying a
        ``thread_id``; resume an interrupt with ``Command(resume=...)``.
    """
    tools = list(toolkit.tools)
    if not tools:
        raise MajsterError("Cannot build the agent graph with no tools available.")

    lookup = _tool_lookup(tools)
    prompt = SystemMessage(content=system_prompt or build_system_prompt(extra_context))

    try:
        model = llm.bind_tools(tools)
    except Exception as exc:
        raise MajsterError(
            f"This LLM does not support tool calling: {exc}. Majster-AI needs a "
            f"tool-calling model - with Ollama, pick one that supports tools "
            f"(e.g. qwen2.5, llama3.1)."
        ) from exc

    # -- nodes --------------------------------------------------------------
    def agent_node(state: DiagnosticState) -> dict[str, Any]:
        """Ask the model what to do next."""
        iterations = int(state.get("iterations", 0))
        messages: list[BaseMessage] = [prompt, *state.get("messages", [])]

        if iterations >= max_iterations:
            log.warning("Iteration ceiling (%d) reached - stopping tool use", max_iterations)
            return {
                "messages": [
                    AIMessage(
                        content=(
                            f"I have reached the limit of {max_iterations} tool calls "
                            f"for this question without converging. Here is where I "
                            f"got to - tell me which thread to pull on and I will "
                            f"continue."
                        )
                    )
                ],
                "iterations": iterations,
            }

        log_agent_step("agent.think", f"Reasoning (round {iterations + 1})")
        response = model.invoke(messages)

        calls = getattr(response, "tool_calls", None) or []
        if calls:
            log_agent_step(
                "agent.plan",
                "Calling: " + ", ".join(call["name"] for call in calls),
                tools=[call["name"] for call in calls],
            )
        return {"messages": [response], "iterations": iterations + 1}

    def tools_node(state: DiagnosticState) -> dict[str, Any]:
        """Execute tool calls, pausing for approval before any write."""
        messages = state.get("messages", [])
        last = messages[-1] if messages else None
        calls = list(getattr(last, "tool_calls", None) or [])
        if not calls:  # pragma: no cover - the router guarantees calls exist
            return {}

        outputs: list[ToolMessage] = []
        approvals: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []

        for call in calls:
            name = call["name"]
            arguments = dict(call.get("args") or {})
            call_id = call.get("id") or name

            tool = lookup.get(name)
            if tool is None:
                outputs.append(
                    ToolMessage(
                        content=str(
                            {
                                "ok": False,
                                "error": "unknown_tool",
                                "message": f"No tool named {name!r}. Available: "
                                f"{', '.join(sorted(lookup))}",
                            }
                        ),
                        tool_call_id=call_id,
                        name=name,
                    )
                )
                continue

            if is_write_tool(name):
                result, approval = handle_write_tool(tool, name, arguments)
                if approval is not None:
                    approvals.append(approval)
            else:
                result = _as_dict(tool.invoke(arguments))

            records.append({"tool": name, "arguments": arguments, "ok": bool(result.get("ok"))})
            outputs.append(ToolMessage(content=str(result), tool_call_id=call_id, name=name))

        return {"messages": outputs, "approvals": approvals, "tool_calls": records}

    # -- routing ------------------------------------------------------------
    def route(state: DiagnosticState) -> str:
        messages = state.get("messages", [])
        last = messages[-1] if messages else None
        return "tools" if getattr(last, "tool_calls", None) else END

    graph = StateGraph(DiagnosticState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")

    # A checkpointer is not optional: interrupt() needs somewhere to persist
    # the paused state, and the HITL gate is built on interrupt().
    return graph.compile(checkpointer=checkpointer or InMemorySaver())


def _decision_from(answer: Any) -> ApprovalDecision:
    """Interpret whatever the caller resumed the graph with.

    Anything unrecognised is a refusal. When the subject is writing to a car,
    an ambiguous answer must never be read as consent.
    """
    if isinstance(answer, ApprovalDecision):
        return answer
    if isinstance(answer, bool):
        return ApprovalDecision.allow() if answer else ApprovalDecision.deny()
    if isinstance(answer, dict):
        approved = bool(answer.get("approved", False))
        reason = str(answer.get("reason", "")) or (
            "approved by operator" if approved else "declined by operator"
        )
        operator = str(answer.get("operator", "unknown"))
        return (
            ApprovalDecision.allow(reason, operator)
            if approved
            else ApprovalDecision.deny(reason, operator)
        )
    if isinstance(answer, str):
        approved = answer.strip().lower() in {"yes", "approve", "approved", "tak", "true"}
        return (
            ApprovalDecision.allow(f"operator said {answer!r}")
            if approved
            else ApprovalDecision.deny(f"operator said {answer!r}")
        )
    return ApprovalDecision.deny(f"unrecognised approval response: {answer!r}")


def pending_interrupt(state: Any) -> dict[str, Any] | None:
    """Extract the approval payload from a paused graph state, if any.

    LangGraph's interrupt representation has shifted between versions, so this
    handles the shapes rather than pinning one.
    """
    tasks = getattr(state, "tasks", None) or ()
    for task in tasks:
        for item in getattr(task, "interrupts", None) or ():
            value = getattr(item, "value", item)
            if isinstance(value, dict):
                return value
    interrupts = getattr(state, "interrupts", None) or ()
    for item in interrupts:
        value = getattr(item, "value", item)
        if isinstance(value, dict):
            return value
    return None


def interrupts_from_result(result: Any) -> list[dict[str, Any]]:
    """Pull approval payloads out of an invoke() result that paused."""
    if not isinstance(result, dict):
        return []
    raw: Iterable[Any] = result.get("__interrupt__") or ()
    payloads: list[dict[str, Any]] = []
    for item in raw:
        value = getattr(item, "value", item)
        if isinstance(value, dict):
            payloads.append(value)
    return payloads


__all__ = [
    "build_graph",
    "handle_write_tool",
    "pending_interrupt",
    "interrupts_from_result",
    "DEFAULT_MAX_ITERATIONS",
]

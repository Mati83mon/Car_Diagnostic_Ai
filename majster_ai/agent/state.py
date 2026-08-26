"""Graph state for the Majster-AI orchestrator."""

from __future__ import annotations

import operator
from typing import Annotated, Any, Sequence, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class DiagnosticState(TypedDict, total=False):
    """State threaded through the diagnostic graph.

    ``messages`` uses LangGraph's ``add_messages`` reducer so nodes append to
    the conversation rather than replacing it.
    """

    messages: Annotated[Sequence[BaseMessage], add_messages]

    #: Every write the operator was asked about, and what they decided. This is
    #: the audit trail: on a vehicle, "who authorised this and when" is a
    #: question that gets asked after something goes wrong.
    approvals: Annotated[list[dict[str, Any]], operator.add]

    #: Tool calls made, for the session summary.
    tool_calls: Annotated[list[dict[str, Any]], operator.add]

    #: Effective safety posture, copied in at start so it is visible in traces.
    safety_mode: str

    #: Guard against a model that loops forever on tool calls.
    iterations: int


def new_state(safety_mode: str) -> DiagnosticState:
    """A fresh, empty state."""
    return DiagnosticState(
        messages=[], approvals=[], tool_calls=[], safety_mode=safety_mode, iterations=0
    )


__all__ = ["DiagnosticState", "new_state"]

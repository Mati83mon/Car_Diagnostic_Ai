"""The Majster-AI orchestrator: a LangGraph agent with a hard safety gate."""

from __future__ import annotations

from majster_ai.agent.graph import build_graph
from majster_ai.agent.hitl import (
    ApprovalDecision,
    ApprovalRequest,
    Approver,
    AutoDenyApprover,
    CallbackApprover,
    ConsoleApprover,
    build_approver,
)
from majster_ai.agent.llm import build_llm, describe_llm
from majster_ai.agent.prompts import SYSTEM_PROMPT, build_system_prompt
from majster_ai.agent.runner import DiagnosticSession, TurnResult, run_console
from majster_ai.agent.state import DiagnosticState
from majster_ai.agent.toolkit import Toolkit, build_local_toolkit, build_mcp_toolkit

__all__ = [
    "build_graph",
    "DiagnosticSession",
    "TurnResult",
    "run_console",
    "DiagnosticState",
    "Toolkit",
    "build_local_toolkit",
    "build_mcp_toolkit",
    "build_llm",
    "describe_llm",
    "SYSTEM_PROMPT",
    "build_system_prompt",
    "Approver",
    "ApprovalRequest",
    "ApprovalDecision",
    "ConsoleApprover",
    "AutoDenyApprover",
    "CallbackApprover",
    "build_approver",
]

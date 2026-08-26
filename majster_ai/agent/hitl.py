"""Human-in-the-loop approval for vehicle write operations.

The safety guarantee is layered, deliberately:

1. :mod:`majster_ai.config` -- ``write_enabled`` is false by default, so the
   service refuses writes outright with no prompt at all.
2. :mod:`majster_ai.mcp_servers.car_interface.service` -- when writes are
   enabled, a mutating call still needs a server-issued token bound to that
   exact operation. This holds even if something other than our agent is
   driving the MCP server.
3. This module and the graph -- the agent pauses, shows a human what would
   happen, and waits for a decision before redeeming that token.

Layer 2 is the one that actually protects the car; layers 1 and 3 make the
protection usable. A prompt-only guard would be no guard at all.
"""

from __future__ import annotations

import abc
import enum
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Final, Iterable

from majster_ai.logging_setup import get_logger, log_agent_step

log = get_logger("agent.hitl")

#: Tools that change the vehicle. Anything not listed here is read-only.
#: Kept as an explicit allow-list of *writes* rather than of reads: a new tool
#: added without updating this set is treated as read-only, so the failure mode
#: of forgetting is a needless prompt, never an unguarded write.
WRITE_TOOLS: Final[frozenset[str]] = frozenset({"clear_dtc"})

#: Modules where an unnecessary write can hurt somebody.
SAFETY_CRITICAL_MODULES: Final[frozenset[str]] = frozenset({"RCM", "ABS", "PBM"})


class RiskLevel(str, enum.Enum):
    """How much care a proposed operation deserves."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """A write operation put to the operator for a decision."""

    tool_name: str
    arguments: dict[str, Any]
    impact: dict[str, Any]
    """The service's own description of what would happen."""

    confirmation_token: str
    risk: RiskLevel = RiskLevel.MEDIUM

    @property
    def module(self) -> str:
        return str(self.arguments.get("module_id") or self.impact.get("module") or "?")

    def affected_codes(self) -> list[str]:
        return [
            str(entry.get("full_code") or entry.get("code"))
            for entry in self.impact.get("dtcs_that_will_be_erased", [])
        ]

    def risks(self) -> list[str]:
        return [str(risk) for risk in self.impact.get("risks", [])]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool_name,
            "arguments": {k: v for k, v in self.arguments.items() if k != "confirmation_token"},
            "module": self.module,
            "risk": self.risk.value,
            "affected_codes": self.affected_codes(),
            "impact": self.impact,
        }

    def render(self) -> str:
        """A console-ready block for a human to read before deciding."""
        lines = [
            "",
            "=" * 72,
            "  WRITE OPERATION - HUMAN APPROVAL REQUIRED",
            "=" * 72,
            f"  Operation : {self.tool_name}",
            f"  Module    : {self.module} ({self.impact.get('module_description', 'unknown')})",
            f"  Address   : {self.impact.get('address', '?')}"
            + ("" if self.impact.get("address_verified") else "   [UNVERIFIED ADDRESS]"),
            f"  Scope     : {self.impact.get('scope', 'unknown')}",
            f"  Risk      : {self.risk.value.upper()}",
            f"  Reversible: {'yes' if self.impact.get('reversible') else 'NO'}",
        ]

        codes = self.affected_codes()
        if codes:
            lines.append(f"  Will erase {len(codes)} code(s):")
            for entry in self.impact.get("dtcs_that_will_be_erased", []):
                description = str(entry.get("description", ""))[:60]
                lines.append(f"      - {entry.get('full_code', '?'):<12} {description}")
        else:
            lines.append("  No stored codes match - this may have no effect.")

        risks = self.risks()
        if risks:
            lines.append("")
            lines.append("  Consequences:")
            for risk in risks:
                lines.append(f"      ! {risk}")

        lines.append("=" * 72)
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """The operator's answer."""

    approved: bool
    reason: str = ""
    operator: str = "console"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "reason": self.reason,
            "operator": self.operator,
            **self.metadata,
        }

    @classmethod
    def allow(
        cls, reason: str = "approved by operator", operator: str = "console"
    ) -> ApprovalDecision:
        return cls(approved=True, reason=reason, operator=operator)

    @classmethod
    def deny(
        cls, reason: str = "declined by operator", operator: str = "console"
    ) -> ApprovalDecision:
        return cls(approved=False, reason=reason, operator=operator)


def assess_risk(tool_name: str, arguments: dict[str, Any], impact: dict[str, Any]) -> RiskLevel:
    """Classify how dangerous a proposed write is."""
    module = str(arguments.get("module_id") or impact.get("module") or "").upper()
    if module in SAFETY_CRITICAL_MODULES:
        return RiskLevel.HIGH
    if not impact.get("address_verified", True):
        # Writing to an address we are not sure about is the classic way to
        # hit a module you did not mean to.
        return RiskLevel.HIGH
    if tool_name in WRITE_TOOLS and not arguments.get("dtc_code"):
        # Clearing everything is coarser than clearing one code.
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def is_write_tool(tool_name: str) -> bool:
    """True when a tool can change the vehicle."""
    return tool_name in WRITE_TOOLS


class Approver(abc.ABC):
    """Asks a human to approve a write."""

    name: str = "approver"

    @abc.abstractmethod
    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        """Obtain a decision. Must never raise; deny instead."""


class ConsoleApprover(Approver):
    """Prompts on the terminal and reads the answer from stdin.

    Requires the operator to type ``yes`` in full. A bare ``y``, an empty line,
    a closed stdin or an interrupt are all treated as refusal: the default
    answer to "shall I write to the car" is no.
    """

    name = "console"

    #: Only these, in full, count as approval.
    AFFIRMATIVE: Final[frozenset[str]] = frozenset({"yes", "approve", "tak"})

    def __init__(
        self,
        input_fn: Callable[[str], str] | None = None,
        output_fn: Callable[[str], None] | None = None,
    ) -> None:
        # Resolved at call time rather than bound here, so redirecting stdin
        # (a test harness, a wrapper script) actually takes effect. A default
        # argument of ``input`` would capture the builtin at import time.
        self._input = input_fn
        # Prompts go to stderr so they stay visible when stdout is piped.
        self._output = output_fn or (lambda text: print(text, file=sys.stderr, flush=True))

    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        self._output(request.render())
        if request.risk is RiskLevel.HIGH:
            self._output(
                "  This is a HIGH RISK operation. Do not approve unless you are "
                "certain the repair is complete."
            )
        self._output("")
        read = self._input if self._input is not None else input
        try:
            answer = read("  Type 'yes' to authorise, anything else to decline: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            self._output("\n  No input available - declining.")
            return ApprovalDecision.deny("no operator input available", operator=self.name)

        if answer in self.AFFIRMATIVE:
            self._output("  -> Approved. Executing.\n")
            return ApprovalDecision.allow(f"operator typed {answer!r}", operator=self.name)
        self._output("  -> Declined. Nothing was written.\n")
        return ApprovalDecision.deny(
            f"operator typed {answer!r}" if answer else "operator gave no answer",
            operator=self.name,
        )


class AutoDenyApprover(Approver):
    """Refuses everything. The correct default when no human is present.

    Used for non-interactive runs, CI, and any context where a prompt would go
    unanswered. Silence must never mean consent when the subject is a car.
    """

    name = "auto-deny"

    def __init__(self, reason: str = "no human operator available to approve a write") -> None:
        self._reason = reason

    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        log.warning("Auto-denying %s: %s", request.tool_name, self._reason)
        return ApprovalDecision.deny(self._reason, operator=self.name)


class CallbackApprover(Approver):
    """Delegates to a callable -- for a GUI, a chat bot, or a test."""

    name = "callback"

    def __init__(self, callback: Callable[[ApprovalRequest], bool | ApprovalDecision]) -> None:
        self._callback = callback

    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        try:
            outcome = self._callback(request)
        except Exception as exc:
            # An approver that crashes must fail closed.
            log.error("Approval callback raised (%s) - denying", exc)
            return ApprovalDecision.deny(f"approval callback failed: {exc}", operator=self.name)
        if isinstance(outcome, ApprovalDecision):
            return outcome
        return (
            ApprovalDecision.allow(operator=self.name)
            if outcome
            else ApprovalDecision.deny(operator=self.name)
        )


class AutoApproveApprover(Approver):
    """Approves everything. Bench rigs only -- never on a vehicle.

    Exists so an automated test bench can run unattended. It logs loudly every
    single time, because if this ever appears in a workshop log something has
    gone badly wrong.
    """

    name = "auto-approve"

    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        log.warning(
            "AUTO-APPROVING %s on %s WITHOUT human review - this must only ever " "be a bench rig.",
            request.tool_name,
            request.module,
        )
        return ApprovalDecision.allow("auto-approved (approval disabled)", operator=self.name)


def build_approver(*, interactive: bool = True, require_approval: bool = True) -> Approver:
    """Pick an approver for the current environment.

    A non-interactive session (piped stdin, CI, a daemon) gets
    :class:`AutoDenyApprover`, so an unattended run can never authorise itself.
    """
    if not require_approval:
        return AutoApproveApprover()
    if interactive and sys.stdin is not None and sys.stdin.isatty():
        return ConsoleApprover()
    return AutoDenyApprover(
        "this session is not interactive, so no operator can approve a write. "
        "Run the agent from a terminal to authorise write operations."
    )


def record_approval(
    request: ApprovalRequest, decision: ApprovalDecision, *, timestamp: float | None = None
) -> dict[str, Any]:
    """Build the audit-trail entry for one approval decision."""
    import time as _time

    entry = {
        "timestamp": timestamp if timestamp is not None else _time.time(),
        "request": request.to_dict(),
        "decision": decision.to_dict(),
    }
    log_agent_step(
        "safety.decision",
        f"{request.tool_name} on {request.module}: "
        f"{'APPROVED' if decision.approved else 'DENIED'} ({decision.reason})",
        tool=request.tool_name,
        approved=decision.approved,
    )
    return entry


def summarise_approvals(approvals: Iterable[dict[str, Any]]) -> str:
    """One-line summary of a session's write decisions, for the closing report."""
    entries = list(approvals)
    if not entries:
        return "No write operations were proposed."
    approved = sum(1 for entry in entries if entry["decision"]["approved"])
    return (
        f"{len(entries)} write operation(s) proposed: "
        f"{approved} approved, {len(entries) - approved} declined."
    )


__all__ = [
    "WRITE_TOOLS",
    "SAFETY_CRITICAL_MODULES",
    "RiskLevel",
    "ApprovalRequest",
    "ApprovalDecision",
    "Approver",
    "ConsoleApprover",
    "AutoDenyApprover",
    "AutoApproveApprover",
    "CallbackApprover",
    "assess_risk",
    "is_write_tool",
    "build_approver",
    "record_approval",
    "summarise_approvals",
]

"""Human-in-the-loop approval over a WebSocket.

:class:`WebSocketApprover` is an ordinary
:class:`~majster_ai.agent.hitl.Approver`. The agent, the graph and the service
are untouched: the browser simply becomes another way of answering the same
question the console asks.

What the client can and cannot do
--------------------------------
The service's confirmation token -- the credential that actually authorises a
write -- is created and redeemed inside the server process and never appears in
any frame. The client receives an opaque ``approval_id`` and can send back one
boolean. So the worst a malicious or buggy client can do is *answer* a question
the operator was already being asked; it can never pose one, and it can never
authorise a write the server did not independently decide to offer.

Everything ambiguous is a refusal:

* an id that is not the one currently outstanding -- ignored, not accepted;
* no answer inside the window -- denied;
* a socket that drops mid-decision -- denied;
* a second answer to an already-answered request -- ignored.
"""

from __future__ import annotations

import queue
import secrets
import threading
import time
from typing import Callable

from majster_ai.agent.hitl import ApprovalDecision, ApprovalRequest, Approver, RiskLevel
from majster_ai.logging_setup import get_logger
from majster_ai.mcp_servers.car_interface.service import CONFIRMATION_TTL_SECONDS
from majster_ai.web.protocol import ApprovalRequestFrame

log = get_logger("web.approvals")

#: Margin left between the browser's decision window and the service token's
#: own expiry, so a decision made at the last second still has time to redeem.
TOKEN_REDEMPTION_MARGIN_SECONDS = 20.0


def effective_timeout(configured: float) -> float:
    """Clamp the decision window so the token cannot expire mid-redemption.

    Without this, an operator who deliberates for the full configured window
    gets a confusing "invalid or expired token" instead of their write.
    """
    ceiling = max(CONFIRMATION_TTL_SECONDS - TOKEN_REDEMPTION_MARGIN_SECONDS, 5.0)
    return max(min(configured, ceiling), 5.0)


class WebSocketApprover(Approver):
    """Ask the browser, block the agent thread until it answers.

    Args:
        emit: Thread-safe callable that puts a frame on the socket. Called from
            the agent's worker thread, so the implementation must marshal onto
            the event loop itself.
        timeout: Seconds to wait for a decision. Clamped by
            :func:`effective_timeout`.
        on_state: Optional hook fired when a request opens or closes, used to
            drive the UI's agent-status indicator.
    """

    name = "websocket"

    def __init__(
        self,
        emit: Callable[[ApprovalRequestFrame], None],
        *,
        timeout: float = 300.0,
        on_state: Callable[[bool], None] | None = None,
    ) -> None:
        self._emit = emit
        self._timeout = effective_timeout(timeout)
        self._on_state = on_state
        self._inbox: queue.Queue[tuple[str, bool, str]] = queue.Queue()
        self._lock = threading.Lock()
        self._pending_id: str | None = None

    # -- called from the agent's worker thread ------------------------------
    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        """Put the decision to the operator and wait."""
        approval_id = secrets.token_urlsafe(18)
        impact = request.impact

        with self._lock:
            self._pending_id = approval_id
            self._drain_locked()

        frame = ApprovalRequestFrame(
            approval_id=approval_id,
            tool=request.tool_name,
            module=request.module,
            risk=request.risk.value if isinstance(request.risk, RiskLevel) else "medium",
            scope=str(impact.get("scope", "unknown")),
            reversible=bool(impact.get("reversible", False)),
            affected_codes=list(impact.get("dtcs_that_will_be_erased", [])),
            risks=[str(risk) for risk in impact.get("risks", [])],
            address_verified=bool(impact.get("address_verified", True)),
            expires_in_seconds=self._timeout,
        )

        if self._on_state is not None:
            self._on_state(True)
        try:
            self._emit(frame)
        except Exception as exc:
            # If we cannot even ask, we certainly cannot proceed.
            log.error("Could not deliver the approval request (%s) - denying", exc)
            self._finish(approval_id)
            return ApprovalDecision.deny(
                "the approval request could not be delivered to the operator",
                operator=self.name,
            )

        log.info(
            "Awaiting operator approval for %s on %s (%.0fs window)",
            request.tool_name,
            request.module,
            self._timeout,
        )

        try:
            return self._await_decision(approval_id)
        finally:
            self._finish(approval_id)

    def _await_decision(self, approval_id: str) -> ApprovalDecision:
        deadline = time.monotonic() + self._timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning("Approval window elapsed with no answer - denying")
                return ApprovalDecision(
                    approved=False,
                    reason="the operator did not respond within the approval window",
                    operator=self.name,
                    metadata={"approval_id": approval_id},
                )
            try:
                answered_id, approved, reason = self._inbox.get(timeout=remaining)
            except queue.Empty:
                continue

            if answered_id != approval_id:
                # A late answer to a request that is no longer outstanding.
                # Discard it rather than let it stand in for this one.
                log.warning("Discarding an approval response for a stale request id")
                continue

            # Carry the id into the decision so the audit trail, and the
            # closing `approval.resolved` frame, can name which request this
            # was. Without it the frame goes out with an empty id and means
            # nothing to the client.
            metadata = {"approval_id": approval_id}
            if approved:
                return ApprovalDecision(
                    approved=True,
                    reason=reason or "approved by the operator in the web UI",
                    operator=self.name,
                    metadata=metadata,
                )
            return ApprovalDecision(
                approved=False,
                reason=reason or "declined by the operator in the web UI",
                operator=self.name,
                metadata=metadata,
            )

    def _finish(self, approval_id: str) -> None:
        with self._lock:
            if self._pending_id == approval_id:
                self._pending_id = None
        if self._on_state is not None:
            self._on_state(False)

    def _drain_locked(self) -> None:
        """Discard queued answers left over from an earlier request."""
        while True:
            try:
                self._inbox.get_nowait()
            except queue.Empty:
                return

    # -- called from the WebSocket receive loop ------------------------------
    def submit(self, approval_id: str, approved: bool, reason: str = "") -> bool:
        """Deliver the operator's decision.

        Returns:
            True if the id matched the outstanding request. False for an
            unknown, stale or already-answered id -- which the caller should
            report rather than treat as a silent success, so a UI bug does not
            look like an unresponsive agent.
        """
        with self._lock:
            pending = self._pending_id
        if pending is None or approval_id != pending:
            log.warning("Rejecting an approval response that matches no open request")
            return False
        self._inbox.put((approval_id, bool(approved), reason))
        return True

    def cancel(self, reason: str = "the operator's connection closed") -> None:
        """Deny any outstanding request -- used when the socket drops.

        A pending write must not survive the disappearance of the human who was
        being asked about it.
        """
        with self._lock:
            pending = self._pending_id
        if pending is not None:
            log.info("Cancelling the outstanding approval: %s", reason)
            self._inbox.put((pending, False, reason))

    @property
    def pending_id(self) -> str | None:
        with self._lock:
            return self._pending_id

    @property
    def timeout(self) -> float:
        return self._timeout


__all__ = [
    "WebSocketApprover",
    "effective_timeout",
    "TOKEN_REDEMPTION_MARGIN_SECONDS",
]

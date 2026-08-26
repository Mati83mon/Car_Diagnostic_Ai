"""HTTP and WebSocket layer for the Majster-AI Cyber-HUD.

The web UI is another client of the same services the CLI and the MCP servers
use. In particular it is another :class:`~majster_ai.agent.hitl.Approver`: the
browser answers the approval question, but the credential that authorises a
write never leaves this process.
"""

from __future__ import annotations

from majster_ai.web.app import create_app, run
from majster_ai.web.approvals import WebSocketApprover
from majster_ai.web.session import ConnectionSession, DiagnosticHub

__all__ = ["create_app", "run", "DiagnosticHub", "ConnectionSession", "WebSocketApprover"]

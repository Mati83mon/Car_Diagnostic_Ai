"""The ``/ws/diagnostics`` message contract.

One schema module, imported by the server and mirrored by the TypeScript
client, so the two cannot drift silently. Every frame is
``{"type": "<name>", ...}`` -- a flat discriminated union, which is the shape a
TypeScript ``switch`` narrows most cleanly.

The security-relevant detail is in :class:`ApprovalRequestFrame`: it carries an
opaque ``approval_id``, never the service's confirmation token. The token stays
in the server process. A browser -- or anything else on the socket -- can say
"the operator approved request X"; it can never mint the credential that
actually authorises a write.
"""

from __future__ import annotations

import enum
import time
from typing import Any, Literal

from pydantic import BaseModel, Field


class ServerMessage(str, enum.Enum):
    """Frames the server sends."""

    HELLO = "hello"
    MODULES = "modules"
    TELEMETRY = "telemetry"
    AGENT_STATUS = "agent.status"
    AGENT_TOOL = "agent.tool"
    AGENT_MESSAGE = "agent.message"
    APPROVAL_REQUEST = "approval.request"
    APPROVAL_RESOLVED = "approval.resolved"
    ERROR = "error"
    PONG = "pong"


class ClientMessage(str, enum.Enum):
    """Frames the client sends."""

    CHAT = "chat"
    APPROVAL_RESPONSE = "approval.response"
    REFRESH = "refresh"
    PING = "ping"


class AgentState(str, enum.Enum):
    """What the agent is doing, for the status indicator."""

    IDLE = "idle"
    THINKING = "thinking"
    TOOL = "tool"
    AWAITING_APPROVAL = "awaiting_approval"
    ERROR = "error"


class ModuleHealth(str, enum.Enum):
    """A module's traffic-light state, driving the 3D pin colour."""

    #: Answered, no stored faults.
    ONLINE = "online"
    #: Answered, faults stored.
    FAULT = "fault"
    #: Did not answer. Not the same as healthy -- and on an unverified
    #: address, not the same as absent either.
    OFFLINE = "offline"
    #: Not yet queried.
    UNKNOWN = "unknown"


class Frame(BaseModel):
    """Base for every frame."""

    type: str
    ts: float = Field(default_factory=time.time)


# ---------------------------------------------------------------------------
# server -> client
# ---------------------------------------------------------------------------
class InterfaceInfo(BaseModel):
    backend: str
    channel: str
    bitrate: int
    physical: bool
    """False for the simulator. The UI must say so plainly: a mechanic looking
    at synthetic readings and believing they came from the car is the single
    worst outcome this interface can produce."""

    safety_mode: str
    write_enabled: bool
    require_approval: bool


class ModuleState(BaseModel):
    name: str
    description: str
    address: str
    verified: bool
    """False for community-derived addresses. Silence from one of these may
    just mean the address is wrong."""

    health: ModuleHealth = ModuleHealth.UNKNOWN
    dtc_count: int = 0
    dtcs: list[dict[str, Any]] = Field(default_factory=list)
    detail: str = ""


class SignalReading(BaseModel):
    signal: str
    value: float | int | str | None
    unit: str = ""
    description: str = ""
    warning: str | None = None
    """Set when the value is physically implausible -- an unplugged sensor, a
    short. Surfaced, never hidden: it is itself diagnostic evidence."""

    verified_scaling: bool = True


class HelloFrame(Frame):
    type: Literal["hello"] = "hello"
    project: str
    version: str
    vehicle: str
    interface: InterfaceInfo
    modules: list[ModuleState]
    telemetry_signals: list[str]
    telemetry_interval_ms: int


class ModulesFrame(Frame):
    type: Literal["modules"] = "modules"
    modules: list[ModuleState]
    total_dtcs: int


class TelemetryFrame(Frame):
    type: Literal["telemetry"] = "telemetry"
    readings: list[SignalReading]
    stale: bool = False
    """True when the poll failed and these are the previous values."""


class AgentStatusFrame(Frame):
    type: Literal["agent.status"] = "agent.status"
    state: AgentState
    detail: str = ""


class AgentToolFrame(Frame):
    type: Literal["agent.tool"] = "agent.tool"
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    summary: str = ""


class Citation(BaseModel):
    """A source the agent leaned on, so a claim can be traced."""

    kind: Literal["manual", "web", "vehicle"]
    label: str
    detail: str = ""
    url: str | None = None
    score: float | None = None


class AgentMessageFrame(Frame):
    type: Literal["agent.message"] = "agent.message"
    role: Literal["user", "assistant", "system"]
    text: str
    citations: list[Citation] = Field(default_factory=list)
    tools_used: list[str] = Field(default_factory=list)


class ApprovalRequestFrame(Frame):
    """A write is paused, waiting for a human.

    Carries everything the operator needs to decide, and nothing that would let
    the client authorise the write by itself.
    """

    type: Literal["approval.request"] = "approval.request"
    approval_id: str
    tool: str
    module: str
    risk: Literal["low", "medium", "high"]
    scope: str
    reversible: bool
    affected_codes: list[dict[str, Any]] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    address_verified: bool = True
    expires_in_seconds: float = 300.0


class ApprovalResolvedFrame(Frame):
    type: Literal["approval.resolved"] = "approval.resolved"
    approval_id: str
    approved: bool
    reason: str = ""


class ErrorFrame(Frame):
    type: Literal["error"] = "error"
    code: str
    message: str


class PongFrame(Frame):
    type: Literal["pong"] = "pong"


# ---------------------------------------------------------------------------
# client -> server
# ---------------------------------------------------------------------------
class ChatCommand(BaseModel):
    type: Literal["chat"]
    text: str = Field(min_length=1, max_length=4000)


class ApprovalResponseCommand(BaseModel):
    """The operator's decision.

    Deliberately minimal: an id and a boolean. There is no field here through
    which a client could supply a credential, because there is no credential a
    client is trusted to hold.
    """

    type: Literal["approval.response"]
    approval_id: str = Field(min_length=1, max_length=128)
    approved: bool


class RefreshCommand(BaseModel):
    type: Literal["refresh"]
    modules: list[str] | None = None


class PingCommand(BaseModel):
    type: Literal["ping"]


__all__ = [
    "ServerMessage",
    "ClientMessage",
    "AgentState",
    "ModuleHealth",
    "Frame",
    "InterfaceInfo",
    "ModuleState",
    "SignalReading",
    "HelloFrame",
    "ModulesFrame",
    "TelemetryFrame",
    "AgentStatusFrame",
    "AgentToolFrame",
    "Citation",
    "AgentMessageFrame",
    "ApprovalRequestFrame",
    "ApprovalResolvedFrame",
    "ErrorFrame",
    "PongFrame",
    "ChatCommand",
    "ApprovalResponseCommand",
    "RefreshCommand",
    "PingCommand",
]

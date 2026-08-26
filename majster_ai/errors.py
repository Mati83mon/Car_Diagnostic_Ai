"""Exception hierarchy for Majster-AI.

Every failure the agent can encounter is modelled as a subclass of
:class:`MajsterError` carrying a stable ``code``. Tool layers convert these
into structured payloads instead of tracebacks, so the LLM sees an actionable
message ("ECU did not answer") rather than a Python stack.
"""

from __future__ import annotations

from typing import Any


class MajsterError(Exception):
    """Base class for every error raised by this project."""

    #: Stable, machine-readable identifier surfaced to the LLM.
    code = "majster_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        """Render as a JSON-safe payload for MCP tool results."""
        payload: dict[str, Any] = {
            "ok": False,
            "error": self.code,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({self.message!r}, {self.details!r})"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
class ConfigError(MajsterError):
    """Configuration is missing, malformed or internally inconsistent."""

    code = "config_error"


# --------------------------------------------------------------------------
# Vehicle interface
# --------------------------------------------------------------------------
class CarInterfaceError(MajsterError):
    """Base class for anything that goes wrong talking to the vehicle."""

    code = "car_interface_error"


class TransportError(CarInterfaceError):
    """The underlying CAN/serial/J2534 link failed."""

    code = "transport_error"


class TransportNotOpenError(TransportError):
    """A request was attempted before the transport was opened."""

    code = "transport_not_open"


class UdsTimeoutError(CarInterfaceError):
    """The ECU did not answer within the configured timeout.

    This is the single most common real-world failure: a module that is
    asleep, on a different bus, or simply not present at that address.
    """

    code = "uds_timeout"


class UdsNegativeResponse(CarInterfaceError):
    """The ECU answered with a negative response (0x7F)."""

    code = "uds_negative_response"

    def __init__(
        self, message: str, *, service: int, nrc: int, nrc_name: str, **details: Any
    ) -> None:
        super().__init__(message, service=service, nrc=nrc, nrc_name=nrc_name, **details)
        self.service = service
        self.nrc = nrc
        self.nrc_name = nrc_name


class UdsProtocolError(CarInterfaceError):
    """A response was received but violates the UDS encoding rules."""

    code = "uds_protocol_error"


class UnknownModuleError(CarInterfaceError):
    """The requested ECU name/id is not in the module map."""

    code = "unknown_module"


class UnknownSignalError(CarInterfaceError):
    """The requested live-data signal is not in the PID/DID catalogue."""

    code = "unknown_signal"


# --------------------------------------------------------------------------
# Safety / human-in-the-loop
# --------------------------------------------------------------------------
class SafetyViolation(MajsterError):
    """A write operation was attempted while the agent is READ_ONLY."""

    code = "safety_violation"


class ApprovalDenied(MajsterError):
    """The human operator refused a write operation."""

    code = "approval_denied"


class ApprovalTimeout(MajsterError):
    """No answer from the operator within the approval window."""

    code = "approval_timeout"


# --------------------------------------------------------------------------
# Knowledge sources
# --------------------------------------------------------------------------
class RagError(MajsterError):
    """Workshop-manual retrieval failed."""

    code = "rag_error"


class IndexNotBuiltError(RagError):
    """A search was issued before any manual was ingested."""

    code = "index_not_built"


class WebSearchError(MajsterError):
    """Every configured web-search provider failed."""

    code = "web_search_error"


class LlmError(MajsterError):
    """No usable LLM provider could be constructed or the call failed."""

    code = "llm_error"


__all__ = [
    "MajsterError",
    "ConfigError",
    "CarInterfaceError",
    "TransportError",
    "TransportNotOpenError",
    "UdsTimeoutError",
    "UdsNegativeResponse",
    "UdsProtocolError",
    "UnknownModuleError",
    "UnknownSignalError",
    "SafetyViolation",
    "ApprovalDenied",
    "ApprovalTimeout",
    "RagError",
    "IndexNotBuiltError",
    "WebSearchError",
    "LlmError",
]

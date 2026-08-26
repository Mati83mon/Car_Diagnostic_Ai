"""Logging for Majster-AI.

Two audiences, one configuration:

* **INFO** -- the agent's reasoning trail. Which tool was chosen, which module
  was queried, what the safety layer decided. This is what you read in the
  garage.
* **DEBUG** -- every CAN/UDS frame, Tx and Rx, with the service name decoded.
  This is what you read when the ECU is being difficult.

Frame tracing can also be enabled independently of the log level with
``MAJSTER_LOG_CAN_FRAMES=true``, which keeps the reasoning trail readable while
still capturing the bus.

A redaction filter is installed on every handler so an API key can never reach
a log file, even if some third-party library decides to log its own config.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from majster_ai.config import Settings, get_settings

#: Logger dedicated to raw bus traffic. Kept separate from the reasoning trail
#: so you can silence one without losing the other.
CAN_LOGGER_NAME = "majster_ai.can"
#: Logger for agent decisions, tool selection and safety rulings.
AGENT_LOGGER_NAME = "majster_ai.agent"

_ROOT_LOGGER_NAME = "majster_ai"
_configured = False

#: Environment variable names whose values must never appear in a log record.
_SECRET_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "MAJSTER_ANTHROPIC_API_KEY",
    "TAVILY_API_KEY",
    "MAJSTER_TAVILY_API_KEY",
)


class SecretRedactingFilter(logging.Filter):
    """Replace known secret values with ``***REDACTED***`` in every record.

    Defence in depth: settings hold secrets as ``SecretStr``, but third-party
    libraries do not, and a stray ``logger.debug(config)`` should not be able
    to write a live API key to disk.
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = {s for s in secrets if s and len(s) >= 8}

    def add_secret(self, secret: str | None) -> None:
        if secret and len(secret) >= 8:
            self._secrets.add(secret)

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "***REDACTED***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        if isinstance(record.msg, str):
            record.msg = self._scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._scrub(v) if isinstance(v, str) else v for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._scrub(a) if isinstance(a, str) else a for a in record.args
                )
        return True


class JsonFormatter(logging.Formatter):
    """Minimal structured formatter for shipping logs off-device."""

    _RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()) | {
        "message",
        "asctime",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-friendly console output with a short logger name."""

    _DEFAULT_FMT = "%(asctime)s %(levelname)-7s %(shortname)-16s %(message)s"

    def __init__(self) -> None:
        super().__init__(fmt=self._DEFAULT_FMT, datefmt="%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        # "majster_ai.mcp_servers.car_interface" -> "car_interface"
        record.shortname = record.name.rsplit(".", 1)[-1]
        return super().format(record)


def configure_logging(settings: Settings | None = None, *, force: bool = False) -> logging.Logger:
    """Install handlers on the ``majster_ai`` logger tree.

    Idempotent: calling it twice will not duplicate handlers unless ``force``
    is set. Returns the package root logger.
    """
    global _configured
    settings = settings or get_settings()
    root = logging.getLogger(_ROOT_LOGGER_NAME)

    if _configured and not force:
        return root

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    level = getattr(logging, settings.log_level, logging.INFO)
    root.setLevel(level)
    # Own our subtree; do not spam the application's root logger.
    root.propagate = False

    redactor = SecretRedactingFilter()
    if settings.anthropic_api_key is not None:
        redactor.add_secret(settings.anthropic_api_key.get_secret_value())
    if settings.tavily_api_key is not None:
        redactor.add_secret(settings.tavily_api_key.get_secret_value())

    formatter: logging.Formatter = JsonFormatter() if settings.log_json else ConsoleFormatter()

    # MCP servers speak JSON-RPC on stdout -- logs must go to stderr or they
    # corrupt the protocol stream.
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    stream.addFilter(redactor)
    root.addHandler(stream)

    if settings.log_file is not None:
        path = Path(settings.log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(
            JsonFormatter()
            if settings.log_json
            else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
        )
        file_handler.addFilter(redactor)
        root.addHandler(file_handler)

    # Frame tracing is opt-in: DEBUG on the whole tree would also pull in
    # python-can's internals, which is rarely what you want.
    can_logger = logging.getLogger(CAN_LOGGER_NAME)
    can_logger.setLevel(logging.DEBUG if settings.can_trace_enabled else logging.INFO)

    _configured = True
    return root


def reset_logging() -> None:
    """Tear down handlers -- used by tests to keep runs isolated."""
    global _configured
    root = logging.getLogger(_ROOT_LOGGER_NAME)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    _configured = False


def get_logger(name: str) -> logging.Logger:
    """Return a logger inside the ``majster_ai`` namespace."""
    if name.startswith(_ROOT_LOGGER_NAME):
        return logging.getLogger(name)
    return logging.getLogger(f"{_ROOT_LOGGER_NAME}.{name}")


# --------------------------------------------------------------------------
# CAN / UDS frame tracing
# --------------------------------------------------------------------------
#: Attribute names already used by :class:`logging.LogRecord`. Passing any of
#: these through ``extra=`` raises ``KeyError``, and "module" -- the single most
#: natural field name in this domain -- is one of them.
_RESERVED_RECORD_KEYS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()) | {
    "message",
    "asctime",
    "taskName",
}


def _safe_extra(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Prefix any field that would collide with a built-in LogRecord attribute.

    ``{"module": "ECM"}`` becomes ``{"ctx_module": "ECM"}`` rather than blowing
    up inside ``logging`` at call time.
    """
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        safe[f"ctx_{key}" if key in _RESERVED_RECORD_KEYS else key] = value
    return safe


def format_hex(data: bytes | bytearray | Iterable[int]) -> str:
    """``b'\\x22\\xf1\\x90'`` -> ``'22 F1 90'``."""
    return " ".join(f"{byte:02X}" for byte in bytes(data))


def trace_frame(
    direction: str,
    can_id: int,
    data: bytes | bytearray,
    *,
    note: str = "",
    logger: logging.Logger | None = None,
) -> None:
    """Log a single CAN frame or assembled ISO-TP payload.

    Args:
        direction: ``"TX"`` or ``"RX"``.
        can_id: 11- or 29-bit arbitration id.
        data: Payload bytes.
        note: Optional decoded description, e.g. the UDS service name.
    """
    log = logger or logging.getLogger(CAN_LOGGER_NAME)
    if not log.isEnabledFor(logging.DEBUG):
        return
    payload = bytes(data)
    suffix = f"  ({note})" if note else ""
    log.debug(
        "%s %03X [%2d] %s%s",
        direction,
        can_id,
        len(payload),
        format_hex(payload),
        suffix,
        extra=_safe_extra(
            {
                "can_direction": direction,
                "can_id": f"0x{can_id:X}",
                "can_dlc": len(payload),
                "can_data": format_hex(payload),
            }
        ),
    )


def log_agent_step(
    step: str,
    message: str,
    *,
    logger: logging.Logger | None = None,
    **fields: Any,
) -> None:
    """Record one step of the agent's reasoning at INFO level."""
    log = logger or logging.getLogger(AGENT_LOGGER_NAME)
    log.info("[%s] %s", step, message, extra=_safe_extra({"agent_step": step, **fields}))


def log_settings_banner(settings: Settings, logger: logging.Logger | None = None) -> None:
    """Log the redacted effective configuration at startup."""
    log = logger or logging.getLogger(_ROOT_LOGGER_NAME)
    described: Mapping[str, Any] = settings.describe()
    log.info("Majster-AI configuration:")
    for key, value in described.items():
        log.info("  %-18s %s", key, value)


__all__ = [
    "CAN_LOGGER_NAME",
    "AGENT_LOGGER_NAME",
    "SecretRedactingFilter",
    "JsonFormatter",
    "ConsoleFormatter",
    "configure_logging",
    "reset_logging",
    "get_logger",
    "format_hex",
    "trace_frame",
    "log_agent_step",
    "log_settings_banner",
]

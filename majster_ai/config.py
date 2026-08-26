"""Central configuration for Majster-AI.

All settings come from environment variables, optionally seeded from a ``.env``
file via ``python-dotenv``. Nothing is hard-coded and no secret ever appears in
source: API keys are held as :class:`~pydantic.SecretStr` so they cannot leak
into a log line, a traceback or a ``repr()``.

Environment variables use the ``MAJSTER_`` prefix (``MAJSTER_CAN_BACKEND``,
``MAJSTER_LOG_LEVEL``, ...). Provider API keys keep their conventional
unprefixed names (``ANTHROPIC_API_KEY``, ``TAVILY_API_KEY``) so they work with
whatever tooling you already have.

Safety posture
--------------
Two independent gates protect the vehicle, and *both* must be open before a
single write frame reaches the bus:

1. ``MAJSTER_WRITE_ENABLED`` (default ``false``) -- the master switch. While
   this is false the agent is strictly READ_ONLY and write tools refuse
   outright, without ever prompting.
2. ``MAJSTER_REQUIRE_APPROVAL`` (default ``true``) -- human-in-the-loop. Even
   with writes enabled, every mutating call pauses the graph and waits for an
   explicit operator confirmation.

Turning gate 2 off is deliberately awkward and is meant only for automated
bench rigs. It is never appropriate on a vehicle you care about.
"""

from __future__ import annotations

import enum
import functools
import os
import re
from pathlib import Path
from typing import Annotated, Any

from dotenv import load_dotenv
from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from majster_ai.errors import ConfigError

#: Repository root -- the directory containing ``pyproject.toml``.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class CanBackend(str, enum.Enum):
    """Supported vehicle-interface backends.

    ``virtual`` is the default on purpose: a freshly cloned repository must
    never be able to put traffic on a real car's bus by accident.
    """

    #: In-process Freelander 2 ECU simulator. No hardware, safe, CI-friendly.
    VIRTUAL = "virtual"
    #: Linux SocketCAN (``can0``, ``vcan0``) -- USB2CAN, CANable, Raspberry Pi.
    SOCKETCAN = "socketcan"
    #: Serial line CAN (``slcan``) -- CANtact / CANable in slcan firmware.
    SLCAN = "slcan"
    #: Generic serial adapter supported by python-can's ``serial`` bus.
    SERIAL = "serial"
    #: SAE J2534 PassThru DLL/shared object -- Tactrix Openport 2.0.
    J2534 = "j2534"
    #: ELM327-class adapter over an RFCOMM Bluetooth serial port.
    RFCOMM = "rfcomm"

    @property
    def is_physical(self) -> bool:
        """True when selecting this backend can energise a real vehicle bus."""
        return self is not CanBackend.VIRTUAL


class LlmProvider(str, enum.Enum):
    """Which LLM drives the orchestrator."""

    #: Claude via the Anthropic API. Best tool-calling reliability.
    ANTHROPIC = "anthropic"
    #: A local Ollama daemon -- works in a garage with no signal.
    OLLAMA = "ollama"
    #: Prefer Anthropic, fall back to Ollama when no API key is present.
    AUTO = "auto"


class SafetyMode(str, enum.Enum):
    """Effective safety posture, derived from the two gates."""

    #: Writes refused outright. The default.
    READ_ONLY = "read_only"
    #: Writes permitted, but each one needs explicit operator approval.
    HITL = "hitl"
    #: Writes permitted with no prompt. Bench rigs only -- never on a car.
    UNATTENDED = "unattended"


class Settings(BaseSettings):
    """Runtime configuration, assembled from the environment."""

    model_config = SettingsConfigDict(
        env_prefix="MAJSTER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- vehicle interface -------------------------------------------------
    can_backend: CanBackend = Field(
        default=CanBackend.VIRTUAL,
        description="Vehicle interface backend. Defaults to the offline simulator.",
    )
    can_channel: str = Field(
        default="vcan0",
        description="Interface channel: 'can0', '/dev/ttyUSB0', '/dev/rfcomm0', ...",
    )
    can_bitrate: int = Field(default=500_000, ge=10_000, le=1_000_000)
    j2534_library: str | None = Field(
        default=None,
        description="Absolute path to the J2534 PassThru shared library (Tactrix: op20pt32).",
    )

    # -- UDS timing --------------------------------------------------------
    uds_timeout: float = Field(
        default=2.0,
        gt=0,
        le=60,
        description="P2 client timeout in seconds for a single UDS request.",
    )
    uds_extended_timeout: float = Field(
        default=5.0,
        gt=0,
        le=120,
        description="P2* timeout applied after the ECU sends NRC 0x78 (response pending).",
    )
    uds_retries: int = Field(
        default=2,
        ge=0,
        le=10,
        description="Retry attempts for a timed-out or transiently-refused request.",
    )
    uds_retry_backoff: float = Field(
        default=0.25,
        ge=0,
        le=10,
        description="Base seconds for exponential backoff between UDS retries.",
    )
    uds_max_response_pending: int = Field(
        default=10,
        ge=0,
        le=100,
        description="How many consecutive NRC 0x78 frames to tolerate before giving up.",
    )

    # -- safety ------------------------------------------------------------
    write_enabled: bool = Field(
        default=False,
        description="Master switch. While false the agent is strictly READ_ONLY.",
    )
    require_approval: bool = Field(
        default=True,
        description="Require explicit human approval for every write operation.",
    )
    approval_timeout: float = Field(
        default=300.0,
        gt=0,
        description="Seconds to wait for an operator decision before aborting.",
    )

    # -- LLM ---------------------------------------------------------------
    llm_provider: LlmProvider = Field(default=LlmProvider.AUTO)
    llm_model: str = Field(
        default="claude-opus-5",
        description="Anthropic model id used when the provider resolves to 'anthropic'.",
    )
    llm_max_tokens: int = Field(default=16_000, gt=0)
    llm_temperature: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Left unset by default: current Claude models reject sampling params.",
    )
    llm_thinking: bool = Field(
        default=True,
        description="Enable adaptive extended thinking on models that support it.",
    )
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "MAJSTER_ANTHROPIC_API_KEY"),
    )
    ollama_base_url: str = Field(default="http://localhost:11434")
    ollama_model: str = Field(default="qwen2.5:7b-instruct")

    # -- RAG ---------------------------------------------------------------
    manuals_dir: Path = Field(
        default=PROJECT_ROOT / "data" / "manuals",
        description="Directory scanned for workshop manuals (PDF, TXT, MD, HTML).",
    )
    vector_dir: Path = Field(
        default=PROJECT_ROOT / "data" / "vectorstore",
        description="Where the persistent vector index lives.",
    )
    rag_collection: str = Field(default="workshop_manuals")
    rag_embedding_model: str = Field(default="sentence-transformers/all-MiniLM-L6-v2")
    rag_chunk_size: int = Field(default=1200, gt=0)
    rag_chunk_overlap: int = Field(default=200, ge=0)
    rag_top_k: int = Field(default=5, gt=0, le=50)

    # -- web search --------------------------------------------------------
    tavily_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("TAVILY_API_KEY", "MAJSTER_TAVILY_API_KEY"),
    )
    web_max_results: int = Field(default=5, gt=0, le=25)
    web_timeout: float = Field(default=20.0, gt=0)
    # NoDecode: without it pydantic-settings tries to JSON-parse the env var
    # before our validator runs, so the natural "a.com,b.com" form fails.
    web_preferred_domains: Annotated[tuple[str, ...], NoDecode] = Field(
        default=(
            "freel2.com",
            "landyzone.co.uk",
            "difflock.com",
            "landroverforums.com",
        ),
        description="Automotive forums whose results are ranked higher, comma-separated.",
    )

    # -- logging -----------------------------------------------------------
    log_level: str = Field(default="INFO")
    log_file: Path | None = Field(default=None)
    log_can_frames: bool = Field(
        default=False,
        description="Trace every CAN/UDS frame (Tx/Rx). Implied by DEBUG log level.",
    )
    log_json: bool = Field(default=False, description="Emit machine-readable JSON logs.")

    # -- validation --------------------------------------------------------
    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("log_level")
    @classmethod
    def _known_log_level(cls, value: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if value not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return value

    @field_validator("rag_collection")
    @classmethod
    def _valid_collection_name(cls, value: str) -> str:
        """Reject names ChromaDB will not accept.

        Without this, an invalid name makes the Chroma store fail to open and
        the service silently falls back to the in-memory index -- a real drop
        in capability that the operator would never be told about.
        """
        name = value.strip()
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{1,510}[a-zA-Z0-9]", name):
            raise ValueError(
                f"rag_collection={value!r} is not a usable collection name. Use "
                f"3-512 characters from [a-zA-Z0-9._-], starting and ending with "
                f"a letter or digit."
            )
        return name

    @field_validator("web_preferred_domains", mode="before")
    @classmethod
    def _split_domains(cls, value: Any) -> Any:
        """Accept a comma-separated string so it round-trips through .env."""
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @model_validator(mode="after")
    def _check_backend_requirements(self) -> Settings:
        if self.can_backend is CanBackend.J2534 and not self.j2534_library:
            raise ValueError(
                "can_backend='j2534' requires MAJSTER_J2534_LIBRARY to point at the "
                "PassThru shared library (Tactrix Openport 2.0: op20pt32.dll / "
                "libop20pt32.so)."
            )
        if self.rag_chunk_overlap >= self.rag_chunk_size:
            raise ValueError("rag_chunk_overlap must be smaller than rag_chunk_size")
        return self

    # -- derived -----------------------------------------------------------
    @property
    def safety_mode(self) -> SafetyMode:
        """The effective posture implied by the two safety gates."""
        if not self.write_enabled:
            return SafetyMode.READ_ONLY
        return SafetyMode.HITL if self.require_approval else SafetyMode.UNATTENDED

    @property
    def is_read_only(self) -> bool:
        """True when no write may reach the bus under any circumstance."""
        return self.safety_mode is SafetyMode.READ_ONLY

    @property
    def can_trace_enabled(self) -> bool:
        """Frame tracing is on explicitly, or implied by a DEBUG log level."""
        return self.log_can_frames or self.log_level == "DEBUG"

    @property
    def has_anthropic_key(self) -> bool:
        return self.anthropic_api_key is not None and bool(
            self.anthropic_api_key.get_secret_value().strip()
        )

    @property
    def has_tavily_key(self) -> bool:
        return self.tavily_api_key is not None and bool(
            self.tavily_api_key.get_secret_value().strip()
        )

    def resolved_llm_provider(self) -> LlmProvider:
        """Turn ``auto`` into a concrete provider based on available credentials."""
        if self.llm_provider is not LlmProvider.AUTO:
            return self.llm_provider
        return LlmProvider.ANTHROPIC if self.has_anthropic_key else LlmProvider.OLLAMA

    def describe(self) -> dict[str, Any]:
        """A redacted summary safe to log or show in a banner."""
        return {
            "project": "Car_Diagnostic_AI (CDA) / Majster-AI",
            "can_backend": self.can_backend.value,
            "can_channel": self.can_channel,
            "can_bitrate": self.can_bitrate,
            "safety_mode": self.safety_mode.value,
            "write_enabled": self.write_enabled,
            "require_approval": self.require_approval,
            "llm_provider": self.resolved_llm_provider().value,
            "llm_model": (
                self.llm_model
                if self.resolved_llm_provider() is LlmProvider.ANTHROPIC
                else self.ollama_model
            ),
            "anthropic_api_key": "set" if self.has_anthropic_key else "unset",
            "tavily_api_key": "set" if self.has_tavily_key else "unset",
            "manuals_dir": str(self.manuals_dir),
            "vector_dir": str(self.vector_dir),
            "log_level": self.log_level,
            "can_trace": self.can_trace_enabled,
        }


def load_settings(env_file: str | os.PathLike[str] | None = None, **overrides: Any) -> Settings:
    """Build a :class:`Settings` instance, seeding the environment from ``.env``.

    ``python-dotenv`` is applied first so that values are visible to any child
    process we later spawn (the MCP servers run as subprocesses over stdio and
    inherit our environment).

    Raises:
        ConfigError: if the resulting configuration is invalid.
    """
    dotenv_path = Path(env_file) if env_file is not None else PROJECT_ROOT / ".env"
    if dotenv_path.is_file():
        load_dotenv(dotenv_path, override=False)

    try:
        return Settings(**overrides)
    except Exception as exc:  # pydantic ValidationError and friends
        raise ConfigError(f"Invalid configuration: {exc}") from exc


#: Explicitly installed settings, if any. Set by :func:`set_settings`.
_override: Settings | None = None


@functools.lru_cache(maxsize=1)
def _cached_settings() -> Settings:
    return load_settings()


def get_settings() -> Settings:
    """Process-wide settings.

    Returns whatever :func:`set_settings` installed, otherwise a cached
    instance built from the environment. Services constructed deep in a call
    stack use this, so the CLI installs its resolved settings here and every
    layer below sees the same overrides.
    """
    return _override if _override is not None else _cached_settings()


def set_settings(settings: Settings | None) -> None:
    """Install process-wide settings, or clear the override with ``None``.

    Used by the CLI to propagate ``--backend`` / ``--log-level`` and by tests
    to point everything at a temporary configuration.
    """
    global _override
    _override = settings


def reset_settings() -> None:
    """Drop the override and the cache so the next call re-reads the environment."""
    set_settings(None)
    _cached_settings.cache_clear()


__all__ = [
    "PROJECT_ROOT",
    "CanBackend",
    "LlmProvider",
    "SafetyMode",
    "Settings",
    "load_settings",
    "get_settings",
    "set_settings",
    "reset_settings",
]

"""LLM provider selection: Claude first, local Ollama as the offline fallback.

A diagnostic agent is often used in a garage with a metal roof and no signal,
so "the API is unreachable" has to be a supported state rather than a crash.
``MAJSTER_LLM_PROVIDER=auto`` picks Claude when an API key is present and
Ollama otherwise; either can be forced explicitly.
"""

from __future__ import annotations

from typing import Any

from majster_ai.config import LlmProvider, Settings, get_settings
from majster_ai.errors import LlmError
from majster_ai.logging_setup import get_logger

log = get_logger("agent.llm")

#: Claude models that take adaptive thinking and reject a fixed token budget.
#: Sending `budget_tokens` to one of these is a 400, so we never do.
_ADAPTIVE_THINKING_PREFIXES = (
    "claude-opus-5",
    "claude-fable-5",
    "claude-sonnet-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
)


def _supports_adaptive_thinking(model: str) -> bool:
    return any(model.startswith(prefix) for prefix in _ADAPTIVE_THINKING_PREFIXES)


def build_anthropic_llm(settings: Settings) -> Any:
    """Construct a ``ChatAnthropic`` client.

    Raises:
        LlmError: if the package is missing or no API key is configured.
    """
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError as exc:
        raise LlmError(
            "langchain-anthropic is not installed. Install it with "
            "pip install 'car-diagnostic-ai[agent]'"
        ) from exc

    if not settings.has_anthropic_key:
        raise LlmError(
            "ANTHROPIC_API_KEY is not set. Put it in your .env file, or set "
            "MAJSTER_LLM_PROVIDER=ollama to run against a local model instead."
        )

    kwargs: dict[str, Any] = {
        "model": settings.llm_model,
        "max_tokens": settings.llm_max_tokens,
        "api_key": settings.anthropic_api_key.get_secret_value(),  # type: ignore[union-attr]
        "timeout": 120,
        "max_retries": 3,
    }
    # Current Claude models reject sampling parameters, so only send one if the
    # operator explicitly asked for it.
    if settings.llm_temperature is not None:
        kwargs["temperature"] = settings.llm_temperature

    if settings.llm_thinking and _supports_adaptive_thinking(settings.llm_model):
        kwargs["thinking"] = {"type": "adaptive"}

    try:
        return ChatAnthropic(**kwargs)
    except Exception as exc:
        # Older langchain-anthropic builds may not accept `thinking`. Losing
        # extended thinking is much better than failing to start.
        if "thinking" in kwargs:
            log.warning("Could not enable adaptive thinking (%s) - continuing without it", exc)
            kwargs.pop("thinking")
            try:
                return ChatAnthropic(**kwargs)
            except Exception as retry_exc:
                raise LlmError(f"Cannot construct the Anthropic client: {retry_exc}") from retry_exc
        raise LlmError(f"Cannot construct the Anthropic client: {exc}") from exc


def build_ollama_llm(settings: Settings) -> Any:
    """Construct a ``ChatOllama`` client against a local daemon.

    Raises:
        LlmError: if the package is missing.
    """
    try:
        from langchain_ollama import ChatOllama
    except ImportError as exc:
        raise LlmError(
            "langchain-ollama is not installed. Install it with "
            "pip install 'car-diagnostic-ai[agent]'"
        ) from exc

    kwargs: dict[str, Any] = {
        "model": settings.ollama_model,
        "base_url": settings.ollama_base_url,
        # Diagnosis is multi-step tool use; a long context avoids the model
        # losing the fault codes it read three turns ago.
        "num_ctx": 8192,
    }
    if settings.llm_temperature is not None:
        kwargs["temperature"] = settings.llm_temperature

    try:
        return ChatOllama(**kwargs)
    except Exception as exc:
        raise LlmError(
            f"Cannot construct the Ollama client at {settings.ollama_base_url}: {exc}. "
            f"Is the daemon running? Try 'ollama serve' and "
            f"'ollama pull {settings.ollama_model}'."
        ) from exc


def build_llm(settings: Settings | None = None) -> Any:
    """Build the configured chat model, falling back where sensible.

    With ``MAJSTER_LLM_PROVIDER=auto`` an Anthropic failure falls through to
    Ollama, because being offline is a normal condition in a workshop and the
    agent should degrade rather than stop.

    Raises:
        LlmError: if no provider can be constructed.
    """
    settings = settings or get_settings()
    provider = settings.resolved_llm_provider()

    if provider is LlmProvider.ANTHROPIC:
        try:
            llm = build_anthropic_llm(settings)
            log.info("LLM: Anthropic %s", settings.llm_model)
            return llm
        except LlmError:
            if settings.llm_provider is not LlmProvider.AUTO:
                raise
            log.warning(
                "Anthropic is unavailable - falling back to local Ollama (%s). "
                "Expect weaker multi-step tool use.",
                settings.ollama_model,
            )

    llm = build_ollama_llm(settings)
    log.info("LLM: Ollama %s at %s", settings.ollama_model, settings.ollama_base_url)
    return llm


def describe_llm(settings: Settings | None = None) -> dict[str, Any]:
    """Report the configured provider without constructing a client."""
    settings = settings or get_settings()
    provider = settings.resolved_llm_provider()
    return {
        "provider": provider.value,
        "model": (
            settings.llm_model if provider is LlmProvider.ANTHROPIC else settings.ollama_model
        ),
        "configured": settings.llm_provider.value,
        "anthropic_key": "set" if settings.has_anthropic_key else "unset",
        "adaptive_thinking": (
            settings.llm_thinking
            and provider is LlmProvider.ANTHROPIC
            and _supports_adaptive_thinking(settings.llm_model)
        ),
    }


__all__ = ["build_llm", "build_anthropic_llm", "build_ollama_llm", "describe_llm"]

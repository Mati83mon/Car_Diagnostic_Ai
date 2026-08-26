"""LLM provider selection and construction."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from majster_ai.agent.llm import (
    build_anthropic_llm,
    build_llm,
    build_ollama_llm,
    describe_llm,
)
from majster_ai.config import LlmProvider, load_settings
from majster_ai.errors import LlmError


class TestDescribe:
    def test_reports_ollama_without_a_key(self) -> None:
        described = describe_llm(load_settings())
        assert described["provider"] == "ollama"
        assert described["anthropic_key"] == "unset"

    def test_reports_claude_with_a_key(self) -> None:
        described = describe_llm(load_settings(anthropic_api_key="sk-ant-abcdefghij"))
        assert described["provider"] == "anthropic"
        assert described["model"] == "claude-opus-5"
        assert described["adaptive_thinking"] is True

    def test_does_not_construct_a_client(self) -> None:
        # `doctor` must be able to report the provider without a network call.
        with patch("langchain_anthropic.ChatAnthropic") as constructor:
            describe_llm(load_settings(anthropic_api_key="sk-ant-abcdefghij"))
        constructor.assert_not_called()

    def test_key_is_never_echoed(self) -> None:
        described = describe_llm(load_settings(anthropic_api_key="sk-ant-secret-value-x"))
        assert "sk-ant-secret-value-x" not in str(described)


class TestAnthropic:
    def test_requires_a_key(self) -> None:
        with pytest.raises(LlmError, match="ANTHROPIC_API_KEY"):
            build_anthropic_llm(load_settings())

    def test_error_suggests_the_offline_alternative(self) -> None:
        with pytest.raises(LlmError, match="ollama"):
            build_anthropic_llm(load_settings())

    def test_constructs_with_the_configured_model(self) -> None:
        settings = load_settings(anthropic_api_key="sk-ant-abcdefghij")
        llm = build_anthropic_llm(settings)
        assert llm.model == "claude-opus-5"

    def test_adaptive_thinking_enabled(self) -> None:
        # budget_tokens is rejected by current Claude models; adaptive is the
        # correct shape.
        settings = load_settings(anthropic_api_key="sk-ant-abcdefghij")
        assert build_anthropic_llm(settings).thinking == {"type": "adaptive"}

    def test_thinking_can_be_disabled(self) -> None:
        settings = load_settings(anthropic_api_key="sk-ant-abcdefghij", llm_thinking=False)
        assert not getattr(build_anthropic_llm(settings), "thinking", None)

    def test_no_sampling_params_by_default(self) -> None:
        # Current Claude models reject temperature; only send one if asked.
        settings = load_settings(anthropic_api_key="sk-ant-abcdefghij")
        with patch("langchain_anthropic.ChatAnthropic") as constructor:
            build_anthropic_llm(settings)
        assert "temperature" not in constructor.call_args.kwargs

    def test_explicit_temperature_is_passed(self) -> None:
        settings = load_settings(anthropic_api_key="sk-ant-abcdefghij", llm_temperature=0.2)
        with patch("langchain_anthropic.ChatAnthropic") as constructor:
            build_anthropic_llm(settings)
        assert constructor.call_args.kwargs["temperature"] == 0.2

    def test_falls_back_when_thinking_is_unsupported(self) -> None:
        """Losing extended thinking is much better than failing to start."""
        settings = load_settings(anthropic_api_key="sk-ant-abcdefghij")
        constructor = Mock(side_effect=[TypeError("unexpected kwarg 'thinking'"), "client"])
        with patch("langchain_anthropic.ChatAnthropic", constructor):
            assert build_anthropic_llm(settings) == "client"
        assert "thinking" not in constructor.call_args.kwargs

    def test_hard_construction_failure_is_reported(self) -> None:
        settings = load_settings(anthropic_api_key="sk-ant-abcdefghij", llm_thinking=False)
        with patch("langchain_anthropic.ChatAnthropic", Mock(side_effect=RuntimeError("nope"))):
            with pytest.raises(LlmError, match="Cannot construct"):
                build_anthropic_llm(settings)


class TestOllama:
    def test_constructs(self) -> None:
        llm = build_ollama_llm(load_settings(ollama_model="qwen2.5:7b-instruct"))
        assert llm.model == "qwen2.5:7b-instruct"

    def test_failure_message_names_the_daemon_command(self) -> None:
        with patch("langchain_ollama.ChatOllama", Mock(side_effect=RuntimeError("refused"))):
            with pytest.raises(LlmError, match="ollama serve"):
                build_ollama_llm(load_settings())


class TestSelection:
    def test_auto_uses_claude_when_keyed(self) -> None:
        settings = load_settings(anthropic_api_key="sk-ant-abcdefghij")
        assert type(build_llm(settings)).__name__ == "ChatAnthropic"

    def test_auto_uses_ollama_without_a_key(self) -> None:
        assert type(build_llm(load_settings())).__name__ == "ChatOllama"

    def test_auto_falls_back_when_claude_cannot_be_built(self) -> None:
        # Being offline is a normal condition in a workshop with a metal roof.
        settings = load_settings(anthropic_api_key="sk-ant-abcdefghij")
        with patch(
            "majster_ai.agent.llm.build_anthropic_llm",
            Mock(side_effect=LlmError("network unreachable")),
        ):
            assert type(build_llm(settings)).__name__ == "ChatOllama"

    def test_explicit_anthropic_does_not_fall_back(self) -> None:
        # If the operator asked for Claude explicitly, silently downgrading to
        # a weaker local model would be worse than failing.
        settings = load_settings(
            llm_provider=LlmProvider.ANTHROPIC, anthropic_api_key="sk-ant-abcdefghij"
        )
        with patch(
            "majster_ai.agent.llm.build_anthropic_llm",
            Mock(side_effect=LlmError("network unreachable")),
        ):
            with pytest.raises(LlmError):
                build_llm(settings)

    def test_explicit_ollama_is_respected(self) -> None:
        settings = load_settings(
            llm_provider=LlmProvider.OLLAMA, anthropic_api_key="sk-ant-abcdefghij"
        )
        assert type(build_llm(settings)).__name__ == "ChatOllama"

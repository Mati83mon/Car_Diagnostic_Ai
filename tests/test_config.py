"""Configuration and the safety defaults it encodes."""

from __future__ import annotations

import pytest

from majster_ai.config import (
    CanBackend,
    LlmProvider,
    SafetyMode,
    get_settings,
    load_settings,
    reset_settings,
    set_settings,
)
from majster_ai.errors import ConfigError


class TestSafetyDefaults:
    """The defaults are the safety story; assert them explicitly."""

    def test_defaults_to_the_simulator(self) -> None:
        # A freshly cloned repo must not be able to transmit on a real bus.
        assert load_settings().can_backend is CanBackend.VIRTUAL
        assert load_settings().can_backend.is_physical is False

    def test_defaults_to_read_only(self) -> None:
        settings = load_settings()
        assert settings.write_enabled is False
        assert settings.is_read_only is True
        assert settings.safety_mode is SafetyMode.READ_ONLY

    def test_approval_is_required_by_default(self) -> None:
        assert load_settings().require_approval is True

    def test_enabling_writes_still_requires_approval(self) -> None:
        settings = load_settings(write_enabled=True)
        assert settings.safety_mode is SafetyMode.HITL
        assert settings.is_read_only is False

    def test_unattended_needs_both_gates_opened(self) -> None:
        settings = load_settings(write_enabled=True, require_approval=False)
        assert settings.safety_mode is SafetyMode.UNATTENDED

    def test_disabling_approval_alone_is_not_enough(self) -> None:
        # require_approval=False must not, by itself, permit any write.
        settings = load_settings(require_approval=False)
        assert settings.safety_mode is SafetyMode.READ_ONLY
        assert settings.is_read_only is True


class TestEnvironment:
    def test_reads_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAJSTER_CAN_BACKEND", "socketcan")
        monkeypatch.setenv("MAJSTER_CAN_CHANNEL", "can0")
        monkeypatch.setenv("MAJSTER_UDS_RETRIES", "5")
        settings = load_settings()
        assert settings.can_backend is CanBackend.SOCKETCAN
        assert settings.can_channel == "can0"
        assert settings.uds_retries == 5

    def test_write_enabled_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAJSTER_WRITE_ENABLED", "true")
        assert load_settings().write_enabled is True

    def test_api_keys_use_conventional_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value")
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret-value")
        settings = load_settings()
        assert settings.has_anthropic_key
        assert settings.has_tavily_key

    def test_secrets_never_appear_in_repr_or_describe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-super-secret-abcdef")
        settings = load_settings()
        assert "sk-ant-super-secret-abcdef" not in repr(settings)
        assert "sk-ant-super-secret-abcdef" not in str(settings.describe())
        assert settings.describe()["anthropic_api_key"] == "set"

    def test_comma_separated_domains(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAJSTER_WEB_PREFERRED_DOMAINS", "a.com, b.co.uk ,c.net")
        assert load_settings().web_preferred_domains == ("a.com", "b.co.uk", "c.net")


class TestValidation:
    def test_j2534_requires_a_library_path(self) -> None:
        with pytest.raises(ConfigError, match="MAJSTER_J2534_LIBRARY"):
            load_settings(can_backend=CanBackend.J2534)

    def test_j2534_accepted_with_a_library(self) -> None:
        settings = load_settings(can_backend=CanBackend.J2534, j2534_library="/x/lib.so")
        assert settings.can_backend is CanBackend.J2534

    def test_rejects_bad_log_level(self) -> None:
        with pytest.raises(ConfigError):
            load_settings(log_level="CHATTY")

    def test_normalises_log_level_case(self) -> None:
        assert load_settings(log_level="debug").log_level == "DEBUG"

    def test_overlapping_chunk_config_rejected(self) -> None:
        with pytest.raises(ConfigError):
            load_settings(rag_chunk_size=100, rag_chunk_overlap=100)

    @pytest.mark.parametrize("name", ["ab", "has space", "-lead", "trail-"])
    def test_rejects_collection_names_chroma_cannot_use(self, name: str) -> None:
        # Otherwise the Chroma store fails to open and we silently degrade to
        # the in-memory index without telling anybody.
        with pytest.raises(ConfigError):
            load_settings(rag_collection=name)

    def test_rejects_out_of_range_numbers(self) -> None:
        with pytest.raises(ConfigError):
            load_settings(uds_timeout=-1)
        with pytest.raises(ConfigError):
            load_settings(can_bitrate=99)


class TestLlmResolution:
    def test_auto_prefers_anthropic_when_keyed(self) -> None:
        settings = load_settings(anthropic_api_key="sk-ant-abcdefghij")
        assert settings.resolved_llm_provider() is LlmProvider.ANTHROPIC

    def test_auto_falls_back_to_ollama_without_a_key(self) -> None:
        assert load_settings().resolved_llm_provider() is LlmProvider.OLLAMA

    def test_explicit_provider_is_respected(self) -> None:
        settings = load_settings(
            llm_provider=LlmProvider.OLLAMA, anthropic_api_key="sk-ant-abcdefghij"
        )
        assert settings.resolved_llm_provider() is LlmProvider.OLLAMA

    def test_blank_key_does_not_count(self) -> None:
        assert load_settings(anthropic_api_key="   ").has_anthropic_key is False


class TestTracing:
    def test_debug_level_implies_frame_tracing(self) -> None:
        assert load_settings(log_level="DEBUG").can_trace_enabled is True

    def test_tracing_can_be_enabled_without_debug(self) -> None:
        settings = load_settings(log_level="INFO", log_can_frames=True)
        assert settings.can_trace_enabled is True

    def test_off_by_default(self) -> None:
        assert load_settings().can_trace_enabled is False


class TestProcessSettings:
    def test_override_is_visible_to_get_settings(self) -> None:
        custom = load_settings(can_channel="can9")
        set_settings(custom)
        try:
            assert get_settings().can_channel == "can9"
        finally:
            reset_settings()

    def test_reset_clears_the_override(self) -> None:
        set_settings(load_settings(can_channel="can9"))
        reset_settings()
        assert get_settings().can_channel != "can9"

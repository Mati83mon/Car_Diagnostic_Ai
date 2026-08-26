"""Logging: frame tracing, secret redaction, and stdout hygiene."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from conftest import attach_log_capture

from majster_ai.config import load_settings
from majster_ai.logging_setup import (
    CAN_LOGGER_NAME,
    JsonFormatter,
    SecretRedactingFilter,
    configure_logging,
    format_hex,
    get_logger,
    log_agent_step,
    log_settings_banner,
    reset_logging,
    trace_frame,
)


class TestSetup:
    def test_is_idempotent(self) -> None:
        settings = load_settings()
        configure_logging(settings, force=True)
        before = len(logging.getLogger("majster_ai").handlers)
        configure_logging(settings)
        assert len(logging.getLogger("majster_ai").handlers) == before

    def test_never_writes_to_stdout(self) -> None:
        """MCP servers speak JSON-RPC on stdout; a log line there corrupts the
        protocol stream and the client simply disconnects."""
        import sys

        configure_logging(load_settings(), force=True)
        for handler in logging.getLogger("majster_ai").handlers:
            stream = getattr(handler, "stream", None)
            assert stream is not sys.stdout

    def test_does_not_touch_the_root_logger(self) -> None:
        configure_logging(load_settings(), force=True)
        assert logging.getLogger("majster_ai").propagate is False

    def test_file_handler(self, tmp_path: Path) -> None:
        path = tmp_path / "logs" / "majster.log"
        configure_logging(load_settings(log_file=path, log_level="INFO"), force=True)
        get_logger("test").info("a message")
        for handler in logging.getLogger("majster_ai").handlers:
            handler.flush()
        assert path.is_file() and "a message" in path.read_text()

    def test_reset_removes_handlers(self) -> None:
        configure_logging(load_settings(), force=True)
        reset_logging()
        assert logging.getLogger("majster_ai").handlers == []


class TestFrameTracing:
    def test_frames_logged_at_debug(self, caplog) -> None:
        configure_logging(load_settings(log_level="DEBUG"), force=True)
        captured = attach_log_capture(caplog)
        trace_frame("TX", 0x7E0, b"\x22\xf1\x90", note="ReadDataByIdentifier")
        assert "22 F1 90" in captured.text
        assert "7E0" in captured.text

    def test_frames_suppressed_when_tracing_is_off(self, caplog) -> None:
        configure_logging(load_settings(log_level="INFO"), force=True)
        captured = attach_log_capture(caplog)
        logging.getLogger(CAN_LOGGER_NAME).setLevel(logging.INFO)
        trace_frame("TX", 0x7E0, b"\x22\xf1\x90")
        assert "22 F1" not in captured.text

    def test_hex_formatting(self) -> None:
        assert format_hex(b"\x22\xf1\x90") == "22 F1 90"
        assert format_hex(b"") == ""

    def test_structured_fields_attached(self, caplog) -> None:
        configure_logging(load_settings(log_level="DEBUG"), force=True)
        captured = attach_log_capture(caplog)
        trace_frame("RX", 0x7E8, b"\x62")
        record = captured.records[-1]
        assert record.can_direction == "RX"
        assert record.can_id == "0x7E8"


class TestAgentSteps:
    def test_step_logged_at_info(self, caplog) -> None:
        configure_logging(load_settings(log_level="INFO"), force=True)
        captured = attach_log_capture(caplog)
        log_agent_step("plan", "Reading DTCs from ECM")
        assert "[plan] Reading DTCs from ECM" in captured.text

    def test_reserved_field_names_do_not_crash(self, caplog) -> None:
        """'module' is the most natural field name in this domain and also a
        reserved LogRecord attribute. Passing it must not raise."""
        configure_logging(load_settings(log_level="INFO"), force=True)
        captured = attach_log_capture(caplog)
        log_agent_step("read", "done", module="ECM", filename="x", lineno=1, args=())
        assert captured.records[-1].ctx_module == "ECM"

    def test_banner_redacts_secrets(self, caplog) -> None:
        settings = load_settings(anthropic_api_key="sk-ant-super-secret-abcdef")
        configure_logging(settings, force=True)
        captured = attach_log_capture(caplog)
        log_settings_banner(settings)
        assert "sk-ant-super-secret-abcdef" not in captured.text
        assert "set" in captured.text


class TestRedaction:
    def test_replaces_a_secret_in_the_message(self) -> None:
        record = logging.LogRecord(
            "t", logging.INFO, "f", 1, "key is sk-ant-abcdefghij", None, None
        )
        SecretRedactingFilter(["sk-ant-abcdefghij"]).filter(record)
        assert "sk-ant-abcdefghij" not in record.msg
        assert "***REDACTED***" in record.msg

    def test_replaces_a_secret_in_args(self) -> None:
        record = logging.LogRecord(
            "t", logging.INFO, "f", 1, "key is %s", ("sk-ant-abcdefghij",), None
        )
        SecretRedactingFilter(["sk-ant-abcdefghij"]).filter(record)
        assert record.args == ("***REDACTED***",)

    def test_short_strings_are_not_treated_as_secrets(self) -> None:
        # Redacting "abc" would mangle ordinary log text.
        record = logging.LogRecord("t", logging.INFO, "f", 1, "abc def", None, None)
        SecretRedactingFilter(["abc"]).filter(record)
        assert record.msg == "abc def"

    def test_no_secrets_configured_is_a_no_op(self) -> None:
        record = logging.LogRecord("t", logging.INFO, "f", 1, "hello", None, None)
        assert SecretRedactingFilter().filter(record) is True

    def test_installed_on_every_handler(self) -> None:
        settings = load_settings(
            anthropic_api_key="sk-ant-abcdefghij", tavily_api_key="tvly-abcdefghij"
        )
        configure_logging(settings, force=True)
        for handler in logging.getLogger("majster_ai").handlers:
            assert any(isinstance(f, SecretRedactingFilter) for f in handler.filters)


class TestJsonFormatter:
    def test_produces_valid_json(self) -> None:
        record = logging.LogRecord("majster_ai.test", logging.INFO, "f", 1, "hello", None, None)
        payload = json.loads(JsonFormatter().format(record))
        assert payload["message"] == "hello"
        assert payload["level"] == "INFO"

    def test_includes_extra_fields(self) -> None:
        record = logging.LogRecord("t", logging.INFO, "f", 1, "m", None, None)
        record.can_id = "0x7E0"
        assert json.loads(JsonFormatter().format(record))["can_id"] == "0x7E0"

    def test_includes_exceptions(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = logging.LogRecord("t", logging.ERROR, "f", 1, "m", None, sys.exc_info())
        assert "boom" in json.loads(JsonFormatter().format(record))["exception"]

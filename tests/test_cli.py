"""The command-line interface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from majster_ai.cli import build_parser, main


@pytest.fixture(autouse=True)
def _simulator_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every CLI test runs against the simulator with quiet logs."""
    monkeypatch.setenv("MAJSTER_CAN_BACKEND", "virtual")
    monkeypatch.setenv("MAJSTER_LOG_LEVEL", "CRITICAL")
    monkeypatch.setenv("MAJSTER_UDS_TIMEOUT", "0.05")
    monkeypatch.setenv("MAJSTER_UDS_RETRIES", "0")
    monkeypatch.setenv("MAJSTER_MANUALS_DIR", str(tmp_path / "manuals"))
    monkeypatch.setenv("MAJSTER_VECTOR_DIR", str(tmp_path / "vs"))


class TestParser:
    def test_requires_a_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_version(self) -> None:
        with pytest.raises(SystemExit) as info:
            build_parser().parse_args(["--version"])
        assert info.value.code == 0

    @pytest.mark.parametrize(
        "command",
        [
            "doctor",
            "dtc",
            "live RPM",
            "scan",
            "clear",
            "ingest",
            "search query",
            "ask what is wrong",
            "chat",
            "serve car_interface",
        ],
    )
    def test_every_subcommand_parses(self, command: str) -> None:
        args = build_parser().parse_args(command.split())
        assert callable(args.func)

    def test_rejects_an_unknown_server(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["serve", "nonsense"])

    def test_rejects_an_unknown_backend(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--backend", "carrier-pigeon", "doctor"])


class TestDoctor:
    def test_reports_configuration(self, capsys) -> None:
        assert main(["doctor"]) == 0
        output = capsys.readouterr().out
        assert "Car_Diagnostic_AI" in output
        assert "Freelander 2" in output
        assert "READ_ONLY" in output

    def test_probe_lists_modules(self, capsys) -> None:
        assert main(["doctor", "--probe"]) == 0
        output = capsys.readouterr().out
        assert "[OK]     ECM" in output
        assert "[silent]" in output

    def test_backend_override_is_reported(self, capsys) -> None:
        main(["--backend", "socketcan", "--channel", "can0", "doctor"])
        output = capsys.readouterr().out
        assert "can0" in output
        assert "will open real hardware" in output


class TestDtc:
    def test_reads_codes(self, capsys) -> None:
        assert main(["dtc", "--module", "ECM"]) == 0
        assert "P0299-00" in capsys.readouterr().out

    def test_json_output_is_valid(self, capsys) -> None:
        main(["dtc", "--module", "ECM", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["count"] == 3

    def test_status_filter(self, capsys) -> None:
        main(["dtc", "--module", "ECM", "--status", "confirmed", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert {dtc["code"] for dtc in payload["dtcs"]} == {"P0299", "P2015"}

    def test_all_modules(self, capsys) -> None:
        assert main(["dtc", "--all"]) == 0
        assert "module(s)" in capsys.readouterr().out

    def test_unknown_module_exits_nonzero(self, capsys) -> None:
        assert main(["dtc", "--module", "NOPE"]) == 1


class TestLive:
    def test_reads_signals(self, capsys) -> None:
        assert main(["live", "RPM", "COOLANT_TEMP"]) == 0
        output = capsys.readouterr().out
        assert "RPM" in output and "812.0" in output

    def test_unknown_signal_is_reported(self, capsys) -> None:
        main(["live", "RPM", "NONSENSE"])
        assert "unavailable" in capsys.readouterr().out


class TestScanAndClear:
    def test_scan(self, capsys) -> None:
        assert main(["scan"]) == 0
        assert "module(s) answered" in capsys.readouterr().out

    def test_clear_refused_in_read_only(self, capsys) -> None:
        # Exit code 2 so a script can tell "refused" from "failed".
        assert main(["clear", "--module", "ECM"]) == 2
        assert "READ_ONLY" in capsys.readouterr().out

    def test_clear_declined_at_the_prompt_writes_nothing(self, monkeypatch, capsys) -> None:
        monkeypatch.setenv("MAJSTER_WRITE_ENABLED", "true")
        monkeypatch.setattr("builtins.input", lambda _prompt: "no")
        assert main(["clear", "--module", "ECM"]) == 3
        assert "Nothing was written" in capsys.readouterr().out

    def test_clear_approved_at_the_prompt(self, monkeypatch, capsys) -> None:
        monkeypatch.setenv("MAJSTER_WRITE_ENABLED", "true")
        monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
        assert main(["clear", "--module", "ECM"]) == 0
        assert "Cleared" in capsys.readouterr().out


class TestManuals:
    def test_ingest_with_no_manuals(self, capsys) -> None:
        assert main(["ingest"]) == 1
        assert "manuals" in capsys.readouterr().out.lower()

    def test_ingest_then_search(self, monkeypatch, tmp_path: Path, capsys) -> None:
        manuals = tmp_path / "manuals"
        manuals.mkdir(parents=True, exist_ok=True)
        (manuals / "m.md").write_text(
            "Turbocharger underboost P0299. Inspect the VGT actuator rod.",
            encoding="utf-8",
        )
        assert main(["ingest"]) == 0
        capsys.readouterr()
        assert main(["search", "turbocharger actuator"]) == 0
        assert "VGT actuator" in capsys.readouterr().out

    def test_search_without_an_index(self, capsys) -> None:
        assert main(["search", "anything"]) == 1

    def test_search_accepts_an_unquoted_multi_word_query(self, tmp_path: Path, capsys) -> None:
        # `ask` takes nargs="+", so `search` does too -- an inconsistency here
        # is exactly the kind of thing that makes a CLI feel unreliable.
        manuals = tmp_path / "manuals"
        manuals.mkdir(parents=True, exist_ok=True)
        (manuals / "m.md").write_text(
            "Swirl flap removal procedure: disconnect the actuator linkage.",
            encoding="utf-8",
        )
        assert main(["ingest"]) == 0
        capsys.readouterr()
        assert main(["search", "swirl", "flap", "removal"]) == 0
        assert "actuator linkage" in capsys.readouterr().out


class TestErrorHandling:
    def test_bad_configuration_exits_two(self, monkeypatch, capsys) -> None:
        monkeypatch.setenv("MAJSTER_CAN_BACKEND", "j2534")
        monkeypatch.delenv("MAJSTER_J2534_LIBRARY", raising=False)
        assert main(["doctor"]) == 2
        assert "configuration error" in capsys.readouterr().err

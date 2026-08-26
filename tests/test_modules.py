"""The ECU address map and its overlay loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from majster_ai.errors import ConfigError, UnknownModuleError
from majster_ai.mcp_servers.car_interface.modules import (
    DEFAULT_MODULES,
    EcuModule,
    ModuleMap,
    load_module_map,
    module_from_dict,
)


class TestResolution:
    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("ECM", "ECM"),
            ("ecm", "ECM"),
            ("engine", "ECM"),
            ("0x7E0", "ECM"),
            ("7E0", "ECM"),
            ("gearbox", "TCM"),
            ("brakes", "ABS"),
            ("airbag", "RCM"),
        ],
    )
    def test_by_name_alias_and_id(self, token: str, expected: str) -> None:
        assert load_module_map().resolve(token).name == expected

    def test_unknown_lists_alternatives(self) -> None:
        with pytest.raises(UnknownModuleError) as info:
            load_module_map().resolve("TARDIS")
        assert "ECM" in info.value.details["known_modules"]
        assert "scan_modules" in info.value.details["hint"]

    def test_empty_token(self) -> None:
        with pytest.raises(UnknownModuleError):
            load_module_map().resolve("  ")


class TestVerification:
    def test_only_legislated_addresses_are_marked_verified(self) -> None:
        # Claiming certainty we do not have is how somebody writes to the
        # wrong module.
        verified = {module.name for module in load_module_map().verified()}
        assert verified == {"ECM", "TCM"}

    def test_unverified_entries_say_so_in_their_notes(self) -> None:
        for module in load_module_map().unverified():
            assert "UNVERIFIED" in module.notes

    def test_safety_critical_modules_are_flagged_in_notes(self) -> None:
        assert "SAFETY-CRITICAL" in load_module_map().resolve("RCM").notes


class TestIntegrity:
    def test_request_ids_are_unique(self) -> None:
        ids = [module.request_id for module in load_module_map()]
        assert len(ids) == len(set(ids))

    def test_duplicate_request_id_is_rejected(self) -> None:
        # Two modules on one address would silently misroute every request.
        with pytest.raises(ConfigError, match="both claim request id"):
            ModuleMap(
                [
                    EcuModule("A", "", 0x700, 0x708),
                    EcuModule("B", "", 0x700, 0x709),
                ]
            )

    def test_duplicate_name_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="Duplicate module name"):
            ModuleMap(
                [
                    EcuModule("A", "", 0x700, 0x708),
                    EcuModule("A", "", 0x701, 0x709),
                ]
            )

    def test_empty_map_is_rejected(self) -> None:
        with pytest.raises(ConfigError):
            ModuleMap([])

    def test_extended_addressing_detected(self) -> None:
        assert EcuModule("X", "", 0x18DA10F1, 0x18DAF110).is_extended_addressing
        assert not EcuModule("Y", "", 0x7E0, 0x7E8).is_extended_addressing


class TestOverlay:
    def test_overlay_overrides_an_entry(self, tmp_path: Path) -> None:
        path = tmp_path / "modules.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "name": "ABS",
                        "request_id": "0x765",
                        "response_id": "0x76D",
                        "verified": True,
                        "notes": "confirmed by scan on my car",
                    }
                ]
            )
        )
        module = load_module_map(path).resolve("ABS")
        assert module.request_id == 0x765
        assert module.verified is True

    def test_overlay_adds_a_module(self, tmp_path: Path) -> None:
        path = tmp_path / "modules.json"
        path.write_text(
            json.dumps([{"name": "DSM", "request_id": "0x740", "response_id": "0x748"}])
        )
        assert load_module_map(path).resolve("DSM").request_id == 0x740

    def test_object_form(self, tmp_path: Path) -> None:
        path = tmp_path / "m.json"
        path.write_text(
            json.dumps(
                {"modules": [{"name": "DSM", "request_id": "0x740", "response_id": "0x748"}]}
            )
        )
        assert "DSM" in load_module_map(path).names()

    def test_missing_overlay_falls_back(self, tmp_path: Path) -> None:
        assert len(load_module_map(tmp_path / "nope.json")) == len(DEFAULT_MODULES)

    def test_strict_mode(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            load_module_map(tmp_path / "nope.json", strict=True)

    def test_bad_json(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("[[[")
        with pytest.raises(ConfigError, match="not valid JSON"):
            load_module_map(path)

    def test_wrong_top_level_type(self, tmp_path: Path) -> None:
        path = tmp_path / "m.json"
        path.write_text('"a string"')
        with pytest.raises(ConfigError, match="must be a JSON list"):
            load_module_map(path)

    @pytest.mark.parametrize(
        "entry",
        [
            {"request_id": "0x700", "response_id": "0x708"},  # no name
            {"name": "X", "response_id": "0x708"},  # no request id
            {"name": "X", "request_id": "ZZZ", "response_id": "0x708"},
            {"name": "X", "request_id": True, "response_id": "0x708"},
            {"name": "", "request_id": "0x700", "response_id": "0x708"},
        ],
    )
    def test_malformed_entries_rejected(self, entry: dict) -> None:
        with pytest.raises(ConfigError):
            module_from_dict(entry)

    def test_integer_ids_accepted(self) -> None:
        module = module_from_dict({"name": "X", "request_id": 0x700, "response_id": 0x708})
        assert module.request_id == 0x700

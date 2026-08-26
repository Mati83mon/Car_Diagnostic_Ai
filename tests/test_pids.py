"""Live-data signal decoding, plausibility checks and the overlay loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from majster_ai.errors import ConfigError, UdsProtocolError, UnknownSignalError
from majster_ai.mcp_servers.car_interface.pids import (
    SignalSource,
    load_signal_catalogue,
    signal_from_dict,
)


@pytest.fixture
def catalogue():
    return load_signal_catalogue()


class TestStandardScaling:
    """The J1979 formulas are legislated; these are the reference values."""

    @pytest.mark.parametrize(
        ("name", "raw", "expected"),
        [
            ("RPM", b"\x0f\xa0", 1000.0),  # (256*15+160)/4
            ("RPM", b"\x00\x00", 0.0),
            ("COOLANT_TEMP", b"\x5a", 50),  # 90 - 40
            ("COOLANT_TEMP", b"\x00", -40),  # the sensor-unplugged reading
            ("SPEED", b"\x64", 100),
            ("MAF", b"\x01\xf4", 5.0),  # 500/100
            ("MODULE_VOLTAGE", b"\x37\x04", 14.084),
            ("FUEL_RAIL_PRESSURE", b"\x0a\x28", 26000),
            ("ENGINE_LOAD", b"\xff", 100.0),
            ("THROTTLE_POS", b"\x00", 0.0),
            ("TIMING_ADVANCE", b"\x80", 0.0),  # 128/2 - 64
            ("ENGINE_TORQUE_PCT", b"\x7d", 0),  # 125 - 125
            ("EGR_ERROR", b"\x80", 0.0),
            ("FUEL_RATE", b"\x00\x14", 1.0),
        ],
    )
    def test_decodes_to_engineering_units(self, catalogue, name, raw, expected) -> None:
        assert catalogue.resolve(name).decode(raw) == expected

    def test_vin_is_ascii(self, catalogue) -> None:
        assert catalogue.resolve("VIN").decode(b"SALFA2BB8AH100001") == "SALFA2BB8AH100001"

    def test_ascii_strips_padding(self, catalogue) -> None:
        assert catalogue.resolve("ECU_SERIAL").decode(b"ABC123\x00\x00 ") == "ABC123"


class TestResolution:
    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("RPM", "RPM"),
            ("rpm", "RPM"),
            ("engine_speed", "RPM"),
            ("0x0C", "RPM"),
            ("coolant", "COOLANT_TEMP"),
            ("ect", "COOLANT_TEMP"),
            ("battery_voltage", "MODULE_VOLTAGE"),
            ("vin", "VIN"),
        ],
    )
    def test_names_aliases_and_ids(self, catalogue, token, expected) -> None:
        assert catalogue.resolve(token).name == expected

    def test_hyphen_and_space_tolerated(self, catalogue) -> None:
        assert catalogue.resolve("coolant temp").name == "COOLANT_TEMP"
        assert catalogue.resolve("coolant-temp").name == "COOLANT_TEMP"

    def test_unknown_signal_lists_alternatives(self, catalogue) -> None:
        with pytest.raises(UnknownSignalError) as info:
            catalogue.resolve("BOOST_PSI")
        assert "RPM" in info.value.details["known_signals"]

    def test_empty_token(self, catalogue) -> None:
        with pytest.raises(UnknownSignalError):
            catalogue.resolve("")


class TestDecodeSafety:
    def test_short_payload_raises_rather_than_guessing(self, catalogue) -> None:
        # A two-byte signal decoded from one byte would give a plausible,
        # wrong number -- the worst outcome for a diagnostic reading.
        with pytest.raises(UdsProtocolError, match="needs 2 byte"):
            catalogue.resolve("RPM").decode(b"\x0f")

    def test_empty_payload_raises(self, catalogue) -> None:
        with pytest.raises(UdsProtocolError):
            catalogue.resolve("COOLANT_TEMP").decode(b"")

    def test_extra_bytes_tolerated(self, catalogue) -> None:
        # ECUs sometimes pad; using the leading bytes is correct.
        assert catalogue.resolve("RPM").decode(b"\x0f\xa0\xff\xff") == 1000.0


class TestPlausibility:
    def test_flags_impossible_high(self, catalogue) -> None:
        warning = catalogue.resolve("COOLANT_TEMP").plausibility_warning(300)
        assert warning and "above the plausible maximum" in warning

    def test_flags_impossible_low(self, catalogue) -> None:
        warning = catalogue.resolve("RPM").plausibility_warning(-5)
        assert warning and "below the plausible minimum" in warning

    def test_normal_values_unflagged(self, catalogue) -> None:
        assert catalogue.resolve("COOLANT_TEMP").plausibility_warning(88) is None

    def test_boundaries_are_inclusive(self, catalogue) -> None:
        signal = catalogue.resolve("ENGINE_LOAD")
        assert signal.plausibility_warning(0) is None
        assert signal.plausibility_warning(100) is None

    def test_strings_not_range_checked(self, catalogue) -> None:
        assert catalogue.resolve("VIN").plausibility_warning("SALFA2BB8AH100001") is None


class TestVerificationFlags:
    def test_legislated_pids_are_verified(self, catalogue) -> None:
        assert catalogue.resolve("RPM").verified is True
        assert catalogue.resolve("VIN").verified is True

    def test_overlay_entries_default_to_unverified(self) -> None:
        signal = signal_from_dict(
            {"name": "DPF_DP", "identifier": "2C05", "data_length": 2, "scale": 0.1}
        )
        assert signal.verified is False


class TestOverlay:
    def test_linear_scaling(self) -> None:
        signal = signal_from_dict(
            {
                "name": "DPF_DIFF_PRESSURE",
                "description": "DPF delta p",
                "unit": "mbar",
                "source": "uds_did",
                "identifier": "0x2C05",
                "data_length": 2,
                "scale": 0.1,
                "offset": -50,
                "min": -50,
                "max": 1000,
                "aliases": ["dpf_delta_p"],
            }
        )
        assert signal.source is SignalSource.UDS_DID
        assert signal.identifier == 0x2C05
        assert signal.decode(b"\x03\xe8") == pytest.approx(50.0)  # 1000*0.1 - 50
        assert signal.matches("dpf_delta_p")

    def test_signed_values(self) -> None:
        signal = signal_from_dict(
            {
                "name": "T",
                "identifier": "10",
                "source": "obd_pid",
                "data_length": 1,
                "scale": 1,
                "signed": True,
            }
        )
        assert signal.decode(b"\xff") == -1

    def test_overlay_merges_and_overrides(self, tmp_path: Path) -> None:
        path = tmp_path / "signals.json"
        path.write_text(
            json.dumps(
                [
                    {"name": "CUSTOM", "identifier": "1234", "data_length": 1},
                    {
                        "name": "RPM",
                        "identifier": "0x0C",
                        "data_length": 2,
                        "scale": 0.25,
                        "source": "obd_pid",
                        "verified": True,
                    },
                ]
            )
        )
        catalogue = load_signal_catalogue(path)
        assert catalogue.resolve("CUSTOM").identifier == 0x1234
        assert catalogue.resolve("RPM").decode(b"\x0f\xa0") == 1000.0
        assert len(catalogue) == len(load_signal_catalogue(tmp_path / "absent.json")) + 1

    def test_object_form_accepted(self, tmp_path: Path) -> None:
        path = tmp_path / "s.json"
        path.write_text(json.dumps({"signals": [{"name": "X", "identifier": "01"}]}))
        assert load_signal_catalogue(path).resolve("X").identifier == 1

    def test_missing_overlay_is_not_an_error(self, tmp_path: Path) -> None:
        assert len(load_signal_catalogue(tmp_path / "nope.json")) > 0

    def test_strict_mode_requires_the_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            load_signal_catalogue(tmp_path / "nope.json", strict=True)

    def test_malformed_json_reported_clearly(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        with pytest.raises(ConfigError, match="not valid JSON"):
            load_signal_catalogue(path)

    @pytest.mark.parametrize(
        "entry",
        [
            {"identifier": "01"},  # no name
            {"name": "X"},  # no identifier
            {"name": "X", "identifier": "ZZZZ"},  # bad hex
            {"name": "X", "identifier": "01", "source": "carrier"},  # bad source
            {"name": "X", "identifier": "FFFF", "source": "obd_pid"},  # out of range
        ],
    )
    def test_malformed_entries_rejected(self, entry: dict) -> None:
        with pytest.raises(ConfigError):
            signal_from_dict(entry)

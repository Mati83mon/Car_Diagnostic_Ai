"""DTC encoding, decoding and status-bit interpretation."""

from __future__ import annotations

import pytest

from majster_ai.errors import UdsProtocolError
from majster_ai.mcp_servers.car_interface.dtc import (
    MASK_ALL,
    MASK_CONFIRMED,
    Dtc,
    DtcStatus,
    decode_dtc_code,
    decode_dtc_record,
    decode_dtc_records,
    describe_dtc,
    encode_dtc_code,
    resolve_status_mask,
)


class TestCodec:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (b"\x02\x99\x00", "P0299"),  # powertrain
            (b"\x52\x34\x00", "C1234"),  # chassis
            (b"\x9a\x2f\x00", "B1A2F"),  # body, hex digits
            (b"\xc1\x00\x00", "U0100"),  # network
            (b"\x00\x00\x00", "P0000"),
            (b"\xff\xff\xff", "U3FFF"),  # every bit set
        ],
    )
    def test_decode_covers_all_four_systems(self, raw: bytes, expected: str) -> None:
        assert decode_dtc_code(raw)[0] == expected

    @pytest.mark.parametrize(
        "code", ["P0299", "C1234", "B1A2F", "U0100", "P2015", "P244B", "P0000", "U3FFF"]
    )
    def test_round_trip(self, code: str) -> None:
        assert decode_dtc_code(encode_dtc_code(code))[0] == code

    def test_failure_type_byte(self) -> None:
        assert decode_dtc_code(b"\x02\x99\x64") == ("P0299", 0x64)
        assert encode_dtc_code("P0299-64") == b"\x02\x99\x64"

    def test_explicit_failure_type_overrides_the_suffix(self) -> None:
        assert encode_dtc_code("P0299-64", failure_type=0x11) == b"\x02\x99\x11"

    def test_short_payload_is_an_error(self) -> None:
        # Silently decoding 2 bytes would invent a code that is not stored.
        with pytest.raises(UdsProtocolError, match="3 bytes"):
            decode_dtc_code(b"\x02\x99")

    @pytest.mark.parametrize("bad", ["", "X0299", "P9299", "P02", "PO299", "not a code"])
    def test_invalid_codes_rejected(self, bad: str) -> None:
        with pytest.raises(UdsProtocolError):
            encode_dtc_code(bad)

    def test_lowercase_accepted(self) -> None:
        assert encode_dtc_code("p0299") == encode_dtc_code("P0299")


class TestStatus:
    def test_all_bits_named(self) -> None:
        status = DtcStatus.from_byte(0xFF)
        assert len(status.flags) == 8
        assert status.is_confirmed and status.is_pending and status.warning_indicator

    def test_no_bits(self) -> None:
        status = DtcStatus.from_byte(0x00)
        assert status.flags == ()
        assert not status.is_confirmed and not status.is_active

    @pytest.mark.parametrize(
        ("raw", "confirmed", "pending"),
        [(0x08, True, False), (0x04, False, True), (0x0C, True, True), (0x01, False, False)],
    )
    def test_confirmed_versus_pending(self, raw: int, confirmed: bool, pending: bool) -> None:
        status = DtcStatus.from_byte(raw)
        assert status.is_confirmed is confirmed
        assert status.is_pending is pending

    def test_out_of_range_rejected(self) -> None:
        with pytest.raises(UdsProtocolError):
            DtcStatus.from_byte(0x100)

    def test_explanations_match_set_bits(self) -> None:
        assert len(DtcStatus.from_byte(0x09).describe()) == 2


class TestRecords:
    def test_single_record(self) -> None:
        dtc = decode_dtc_record(b"\x02\x99\x00\x2f")
        assert dtc.code == "P0299"
        assert dtc.full_code == "P0299-00"
        assert dtc.system == "powertrain"
        assert dtc.status.is_confirmed

    def test_multiple_records(self) -> None:
        payload = b"\x02\x99\x00\x2f" + b"\x20\x15\x00\x08" + b"\xc1\x00\x00\x08"
        codes = [dtc.code for dtc in decode_dtc_records(payload)]
        assert codes == ["P0299", "P2015", "U0100"]

    def test_empty_block_is_no_faults(self) -> None:
        assert decode_dtc_records(b"") == []

    def test_truncated_block_is_an_error_not_a_silent_drop(self) -> None:
        # A partial record means ISO-TP reassembly went wrong. Dropping it
        # quietly would report fewer faults than the car actually has.
        with pytest.raises(UdsProtocolError, match="multiple of 4"):
            decode_dtc_records(b"\x02\x99\x00\x2f\x20\x15")

    def test_module_tagging(self) -> None:
        dtc = decode_dtc_record(b"\x02\x99\x00\x2f").with_module("ECM")
        assert dtc.module == "ECM"
        assert dtc.to_dict()["module"] == "ECM"


class TestDescriptions:
    @pytest.mark.parametrize(
        ("code", "fragment"),
        [
            ("P0299", "Underboost"),
            ("P2015", "Intake Manifold Runner"),
            ("P0401", "Recirculation Flow Insufficient"),
            ("U0100", "Lost Communication"),
            ("P244B", "Differential Pressure"),
        ],
    )
    def test_known_generic_codes(self, code: str, fragment: str) -> None:
        assert fragment in describe_dtc(code)

    @pytest.mark.parametrize("code", ["P1234", "P3456"])
    def test_manufacturer_codes_admit_ignorance(self, code: str) -> None:
        # Inventing a meaning here is how somebody replaces a good part.
        text = describe_dtc(code)
        assert "Manufacturer-specific" in text
        assert "search_manual" in text

    def test_unknown_generic_code_admits_ignorance(self) -> None:
        assert "No description available" in describe_dtc("P0999")

    def test_suffix_tolerated(self) -> None:
        assert describe_dtc("P0299-00") == describe_dtc("P0299")

    def test_generic_flag(self) -> None:
        assert Dtc("P0299", 0, DtcStatus.from_byte(0), b"").is_generic is True
        assert Dtc("P1299", 0, DtcStatus.from_byte(0), b"").is_generic is False


class TestStatusMask:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, MASK_ALL),
            ("all", MASK_ALL),
            ("confirmed", MASK_CONFIRMED),
            ("CONFIRMED", MASK_CONFIRMED),
            (0x08, 0x08),
            ("0x08", 0x08),
            ("8", 8),
        ],
    )
    def test_accepted_forms(self, value: object, expected: int) -> None:
        assert resolve_status_mask(value) == expected  # type: ignore[arg-type]

    def test_unknown_name_lists_the_valid_ones(self) -> None:
        with pytest.raises(UdsProtocolError, match="confirmed"):
            resolve_status_mask("recent")

    def test_out_of_range(self) -> None:
        with pytest.raises(UdsProtocolError):
            resolve_status_mask(300)

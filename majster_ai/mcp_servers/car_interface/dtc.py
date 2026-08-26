"""Diagnostic Trouble Code encoding, decoding and interpretation.

Implements the 3-byte DTC format used by UDS ``ReadDTCInformation`` (0x19),
the ISO 14229-1 Annex D status byte, and the ISO 15031-6 / SAE J2012 textual
form (``P0299-00``).

Byte layout of a UDS DTC record::

    +--------+--------+--------+--------+
    |  high  | middle |  low   | status |
    +--------+--------+--------+--------+
       |        |         |        |
       |        |         |        +-- ISO 14229-1 Annex D status bits
       |        |         +----------- Failure Type Byte (the "-00" suffix)
       |        +--------------------- 3rd and 4th characters
       +------------------------------ system + 1st and 2nd characters

High byte::

    bit 7..6  system:  00=P powertrain  01=C chassis  10=B body  11=U network
    bit 5..4  first digit (0-3)
    bit 3..0  second digit (0-F)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final, Iterable

from majster_ai.errors import UdsProtocolError

#: Index -> DTC system letter, per ISO 15031-6.
_SYSTEM_LETTERS: Final = ("P", "C", "B", "U")
_LETTER_TO_INDEX: Final = {letter: index for index, letter in enumerate(_SYSTEM_LETTERS)}

#: ``P0299`` or ``P0299-00`` / ``P0299-64``.
_DTC_PATTERN: Final = re.compile(
    r"^([PCBU])([0-3])([0-9A-F])([0-9A-F])([0-9A-F])(?:-([0-9A-F]{2}))?$"
)

#: ISO 14229-1 Annex D, Table D.1 -- DTC status availability/report mask bits.
STATUS_BITS: Final[tuple[tuple[int, str, str], ...]] = (
    (0, "testFailed", "Failed at the most recent test"),
    (1, "testFailedThisOperationCycle", "Failed during the current driving cycle"),
    (2, "pendingDTC", "Failed this cycle but not yet confirmed"),
    (3, "confirmedDTC", "Stored: failed enough times to be confirmed"),
    (4, "testNotCompletedSinceLastClear", "Not tested since the last clear"),
    (5, "testFailedSinceLastClear", "Has failed at least once since the last clear"),
    (6, "testNotCompletedThisOperationCycle", "Not yet tested this driving cycle"),
    (7, "warningIndicatorRequested", "Warning lamp / message requested"),
)

#: Common status masks for the 0x19 0x02 ``reportDTCByStatusMask`` request.
MASK_ALL: Final = 0xFF
MASK_CONFIRMED: Final = 0x08
MASK_PENDING: Final = 0x04
MASK_TEST_FAILED: Final = 0x01
MASK_CONFIRMED_OR_PENDING: Final = 0x0C

#: Human-readable names for the standard status masks, used in tool output.
MASK_NAMES: Final[dict[str, int]] = {
    "all": MASK_ALL,
    "confirmed": MASK_CONFIRMED,
    "pending": MASK_PENDING,
    "test_failed": MASK_TEST_FAILED,
    "confirmed_or_pending": MASK_CONFIRMED_OR_PENDING,
}

#: Generic SAE J2012 descriptions for powertrain/network codes relevant to the
#: 2.2 TD4 (DW12 / PSA-Ford) and to JLR CAN architecture.
#:
#: These are the *legislated generic* meanings. A manufacturer may refine them,
#: and every P1xxx / most B and C codes are manufacturer-specific and absent
#: here on purpose -- for those the agent must consult the workshop manual via
#: the RAG server rather than guess. ``describe_dtc`` says so explicitly.
GENERIC_DTC_DESCRIPTIONS: Final[dict[str, str]] = {
    # --- fuel and air metering ---
    "P0087": "Fuel Rail/System Pressure - Too Low",
    "P0088": "Fuel Rail/System Pressure - Too High",
    "P0089": "Fuel Pressure Regulator 1 Performance",
    "P0090": "Fuel Pressure Regulator 1 Control Circuit",
    "P0101": "Mass or Volume Air Flow Circuit Range/Performance",
    "P0102": "Mass or Volume Air Flow Circuit Low Input",
    "P0103": "Mass or Volume Air Flow Circuit High Input",
    "P0107": "Manifold Absolute Pressure/Barometric Pressure Circuit Low Input",
    "P0108": "Manifold Absolute Pressure/Barometric Pressure Circuit High Input",
    "P0112": "Intake Air Temperature Sensor 1 Circuit Low",
    "P0113": "Intake Air Temperature Sensor 1 Circuit High",
    "P0116": "Engine Coolant Temperature Circuit Range/Performance",
    "P0117": "Engine Coolant Temperature Circuit Low",
    "P0118": "Engine Coolant Temperature Circuit High",
    "P0128": "Coolant Thermostat - Temperature Below Regulating Temperature",
    "P0180": "Fuel Temperature Sensor A Circuit",
    "P0190": "Fuel Rail Pressure Sensor A Circuit",
    "P0191": "Fuel Rail Pressure Sensor A Circuit Range/Performance",
    "P0192": "Fuel Rail Pressure Sensor A Circuit Low",
    "P0193": "Fuel Rail Pressure Sensor A Circuit High",
    # --- injectors and misfire ---
    "P0201": "Injector Circuit/Open - Cylinder 1",
    "P0202": "Injector Circuit/Open - Cylinder 2",
    "P0203": "Injector Circuit/Open - Cylinder 3",
    "P0204": "Injector Circuit/Open - Cylinder 4",
    "P0300": "Random/Multiple Cylinder Misfire Detected",
    "P0301": "Cylinder 1 Misfire Detected",
    "P0302": "Cylinder 2 Misfire Detected",
    "P0303": "Cylinder 3 Misfire Detected",
    "P0304": "Cylinder 4 Misfire Detected",
    # --- boost / turbo ---
    "P0234": "Turbocharger/Supercharger A Overboost Condition",
    "P0235": "Turbocharger/Supercharger Boost Sensor A Circuit",
    "P0243": "Turbocharger/Supercharger Wastegate Solenoid A",
    "P0244": "Turbocharger/Supercharger Wastegate Solenoid A Range/Performance",
    "P0245": "Turbocharger/Supercharger Wastegate Solenoid A Low",
    "P0246": "Turbocharger/Supercharger Wastegate Solenoid A High",
    "P0299": "Turbocharger/Supercharger A Underboost Condition",
    "P2262": "Turbocharger Boost Pressure Not Detected - Mechanical",
    "P2263": "Turbocharger/Supercharger Boost System Performance",
    # --- sensors ---
    "P0335": "Crankshaft Position Sensor A Circuit",
    "P0336": "Crankshaft Position Sensor A Circuit Range/Performance",
    "P0340": "Camshaft Position Sensor A Circuit",
    "P0341": "Camshaft Position Sensor A Circuit Range/Performance",
    "P0380": "Glow Plug/Heater Circuit A",
    "P0381": "Glow Plug/Heater Indicator Circuit",
    "P0500": "Vehicle Speed Sensor A",
    "P0504": "Brake Switch A/B Correlation",
    # --- EGR ---
    "P0401": "Exhaust Gas Recirculation Flow Insufficient Detected",
    "P0402": "Exhaust Gas Recirculation Flow Excessive Detected",
    "P0403": "Exhaust Gas Recirculation Control Circuit",
    "P0404": "Exhaust Gas Recirculation Control Circuit Range/Performance",
    "P0405": "Exhaust Gas Recirculation Sensor A Circuit Low",
    "P0406": "Exhaust Gas Recirculation Sensor A Circuit High",
    "P0409": "Exhaust Gas Recirculation Sensor A Circuit",
    # --- electrical / module ---
    "P0480": "Fan 1 Control Circuit",
    "P0481": "Fan 2 Control Circuit",
    "P0562": "System Voltage Low",
    "P0563": "System Voltage High",
    "P0603": "Internal Control Module Keep Alive Memory (KAM) Error",
    "P0605": "Internal Control Module ROM Error",
    "P0606": "ECM/PCM Processor Fault",
    "P062F": "Internal Control Module EEPROM Error",
    "P0670": "Glow Plug Module Control Circuit",
    # --- DPF / after-treatment ---
    "P2002": "Diesel Particulate Filter Efficiency Below Threshold - Bank 1",
    "P2015": "Intake Manifold Runner Position Sensor/Switch Circuit Range/Performance "
    "(swirl flap actuator - a known weak point on this engine)",
    "P2016": "Intake Manifold Runner Position Sensor/Switch Circuit Low - Bank 1",
    "P242F": "Diesel Particulate Filter Restriction - Ash Accumulation",
    "P2452": "Diesel Particulate Filter Pressure Sensor A Circuit",
    "P2453": "Diesel Particulate Filter Pressure Sensor A Circuit Range/Performance",
    "P2454": "Diesel Particulate Filter Pressure Sensor A Circuit Low",
    "P2455": "Diesel Particulate Filter Pressure Sensor A Circuit High",
    "P2458": "Diesel Particulate Filter Regeneration Duration",
    "P2459": "Diesel Particulate Filter Regeneration Frequency",
    "P2463": "Diesel Particulate Filter Restriction - Soot Accumulation",
    "P244A": "Diesel Particulate Filter Differential Pressure Too Low",
    "P244B": "Diesel Particulate Filter Differential Pressure Too High",
    # --- network (very common on a 15-year-old JLR car) ---
    "U0001": "High Speed CAN Communication Bus",
    "U0073": "Control Module Communication Bus A Off",
    "U0100": "Lost Communication With ECM/PCM A",
    "U0101": "Lost Communication With TCM",
    "U0121": "Lost Communication With ABS Control Module",
    "U0140": "Lost Communication With Body Control Module",
    "U0155": "Lost Communication With Instrument Panel Cluster Control Module",
    "U0401": "Invalid Data Received From ECM/PCM A",
    "U0415": "Invalid Data Received From ABS Control Module",
}


@dataclass(frozen=True, slots=True)
class DtcStatus:
    """The ISO 14229-1 Annex D status byte, unpacked."""

    raw: int
    flags: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_byte(cls, value: int) -> DtcStatus:
        if not 0 <= value <= 0xFF:
            raise UdsProtocolError(f"DTC status byte out of range: {value}")
        flags = tuple(name for bit, name, _ in STATUS_BITS if value & (1 << bit))
        return cls(raw=value, flags=flags)

    def has(self, flag: str) -> bool:
        return flag in self.flags

    @property
    def is_confirmed(self) -> bool:
        """Stored in memory -- the code a workshop tester would call 'current'."""
        return self.has("confirmedDTC")

    @property
    def is_pending(self) -> bool:
        """Seen this cycle but not yet confirmed."""
        return self.has("pendingDTC")

    @property
    def is_active(self) -> bool:
        """Failing right now, as opposed to a historic entry."""
        return self.has("testFailed") or self.has("testFailedThisOperationCycle")

    @property
    def warning_indicator(self) -> bool:
        """The ECU is asking for a lamp/message in the cluster (MIL)."""
        return self.has("warningIndicatorRequested")

    def describe(self) -> list[str]:
        """Long-form explanations for each set bit."""
        return [text for bit, _, text in STATUS_BITS if self.raw & (1 << bit)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": f"0x{self.raw:02X}",
            "flags": list(self.flags),
            "confirmed": self.is_confirmed,
            "pending": self.is_pending,
            "active": self.is_active,
            "warning_indicator": self.warning_indicator,
            "explanations": self.describe(),
        }


@dataclass(frozen=True, slots=True)
class Dtc:
    """One decoded Diagnostic Trouble Code."""

    code: str
    """The four-character code, e.g. ``P0299``."""

    failure_type: int
    """Failure Type Byte -- the ``-00`` suffix. 0 when unspecified."""

    status: DtcStatus
    raw: bytes
    """The original 3 DTC bytes, for round-tripping and for clear-by-code."""

    module: str | None = None
    """Which ECU reported it, filled in by the service layer."""

    @property
    def full_code(self) -> str:
        """``P0299-00`` -- code plus failure type byte."""
        return f"{self.code}-{self.failure_type:02X}"

    @property
    def system(self) -> str:
        """``powertrain`` / ``chassis`` / ``body`` / ``network``."""
        return {
            "P": "powertrain",
            "C": "chassis",
            "B": "body",
            "U": "network",
        }[self.code[0]]

    @property
    def is_generic(self) -> bool:
        """True for legislated SAE codes, False for manufacturer-specific ones.

        The second character encodes this: ``0`` and ``2`` are SAE-defined for
        P-codes, ``1`` and ``3`` are manufacturer-specific.
        """
        return self.code[1] in ("0", "2")

    def description(self) -> str:
        return describe_dtc(self.code)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "full_code": self.full_code,
            "failure_type": f"0x{self.failure_type:02X}",
            "system": self.system,
            "generic": self.is_generic,
            "description": self.description(),
            "module": self.module,
            "status": self.status.to_dict(),
            "raw": self.raw.hex().upper(),
        }

    def with_module(self, module: str) -> Dtc:
        """Return a copy tagged with the reporting ECU."""
        return Dtc(
            code=self.code,
            failure_type=self.failure_type,
            status=self.status,
            raw=self.raw,
            module=module,
        )


def decode_dtc_code(data: bytes | bytearray | Iterable[int]) -> tuple[str, int]:
    """Decode 3 DTC bytes into ``("P0299", failure_type)``.

    Raises:
        UdsProtocolError: if fewer than 3 bytes are supplied.
    """
    payload = bytes(data)
    if len(payload) < 3:
        raise UdsProtocolError(f"A DTC needs 3 bytes, got {len(payload)}: {payload.hex().upper()}")
    high, middle, failure_type = payload[0], payload[1], payload[2]

    letter = _SYSTEM_LETTERS[(high >> 6) & 0x03]
    digit1 = (high >> 4) & 0x03
    digit2 = high & 0x0F
    digit3 = (middle >> 4) & 0x0F
    digit4 = middle & 0x0F
    return f"{letter}{digit1}{digit2:X}{digit3:X}{digit4:X}", failure_type


def encode_dtc_code(code: str, failure_type: int | None = None) -> bytes:
    """Encode ``"P0299"`` / ``"P0299-00"`` back into its 3 raw bytes.

    Used to clear one specific code and to build simulator fixtures.

    Raises:
        UdsProtocolError: if the code is not a valid DTC string.
    """
    text = code.strip().upper()
    match = _DTC_PATTERN.match(text)
    if not match:
        raise UdsProtocolError(f"Not a valid DTC: {code!r}. Expected e.g. 'P0299' or 'P0299-00'.")
    letter, d1, d2, d3, d4, ftb = match.groups()

    high = (_LETTER_TO_INDEX[letter] << 6) | (int(d1) << 4) | int(d2, 16)
    middle = (int(d3, 16) << 4) | int(d4, 16)
    if failure_type is None:
        failure_type = int(ftb, 16) if ftb else 0x00
    if not 0 <= failure_type <= 0xFF:
        raise UdsProtocolError(f"Failure type byte out of range: {failure_type}")
    return bytes((high, middle, failure_type))


def decode_dtc_record(record: bytes | bytearray) -> Dtc:
    """Decode one 4-byte ``ReadDTCInformation`` record (3 DTC bytes + status)."""
    payload = bytes(record)
    if len(payload) < 4:
        raise UdsProtocolError(
            f"A DTC record needs 4 bytes (3 DTC + status), got {len(payload)}: "
            f"{payload.hex().upper()}"
        )
    code, failure_type = decode_dtc_code(payload[:3])
    return Dtc(
        code=code,
        failure_type=failure_type,
        status=DtcStatus.from_byte(payload[3]),
        raw=payload[:3],
    )


def decode_dtc_records(payload: bytes | bytearray) -> list[Dtc]:
    """Decode the record list from a 0x19/0x02 positive response body.

    ``payload`` is everything *after* the status-availability mask byte. A
    trailing partial record is a protocol error rather than something to
    silently drop: on a flaky bus a truncated response usually means the
    ISO-TP reassembly went wrong, and hiding it would produce phantom results.
    """
    data = bytes(payload)
    if len(data) % 4 != 0:
        raise UdsProtocolError(
            f"DTC record block is not a multiple of 4 bytes ({len(data)} bytes): "
            f"{data.hex().upper()} - likely a truncated or corrupted response."
        )
    return [decode_dtc_record(data[i : i + 4]) for i in range(0, len(data), 4)]


def describe_dtc(code: str) -> str:
    """Best-known meaning for a DTC.

    Returns the generic SAE J2012 text when the code is legislated. For
    manufacturer-specific codes it says so plainly instead of inventing a
    description -- the agent is expected to look those up in the workshop
    manual through the RAG server.
    """
    key = code.strip().upper().split("-")[0]
    known = GENERIC_DTC_DESCRIPTIONS.get(key)
    if known:
        return known
    if len(key) >= 2 and key[1] in ("1", "3"):
        return (
            "Manufacturer-specific code - no generic SAE definition exists. "
            "Consult the JLR workshop manual (search_manual) for the meaning."
        )
    return (
        "No description available in the built-in catalogue. "
        "Consult the workshop manual (search_manual) or a marque forum."
    )


def resolve_status_mask(mask: int | str | None) -> int:
    """Turn ``"confirmed"`` / ``0x08`` / ``None`` into a status mask byte.

    Accepts a friendly name from :data:`MASK_NAMES`, an int, or a hex string.
    """
    if mask is None:
        return MASK_ALL
    if isinstance(mask, int):
        if not 0 <= mask <= 0xFF:
            raise UdsProtocolError(f"DTC status mask out of range: {mask}")
        return mask
    text = str(mask).strip().lower()
    if text in MASK_NAMES:
        return MASK_NAMES[text]
    try:
        value = int(text, 16) if text.startswith("0x") else int(text)
    except ValueError as exc:
        raise UdsProtocolError(
            f"Unknown DTC status mask {mask!r}. Use one of "
            f"{sorted(MASK_NAMES)} or a byte value like '0x08'."
        ) from exc
    return resolve_status_mask(value)


__all__ = [
    "STATUS_BITS",
    "MASK_ALL",
    "MASK_CONFIRMED",
    "MASK_PENDING",
    "MASK_TEST_FAILED",
    "MASK_CONFIRMED_OR_PENDING",
    "MASK_NAMES",
    "GENERIC_DTC_DESCRIPTIONS",
    "DtcStatus",
    "Dtc",
    "decode_dtc_code",
    "encode_dtc_code",
    "decode_dtc_record",
    "decode_dtc_records",
    "describe_dtc",
    "resolve_status_mask",
]

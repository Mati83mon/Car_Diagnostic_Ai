"""Live-data signal catalogue: OBD-II PIDs and UDS Data Identifiers.

Two families of signal live here, and the distinction matters:

**OBD-II Mode 0x01 PIDs** are legislated by SAE J1979 / ISO 15031-5. Their
identifiers *and* their scaling formulas are standard across every compliant
vehicle, so they are hard-coded here and marked ``verified=True``.

**Manufacturer UDS DIDs** (service 0x22) are proprietary. A DID that returns
rail pressure on one ECU returns something entirely different on another.
Inventing them would produce confident, wrong numbers -- the worst possible
failure mode for a diagnostic tool. So the built-in catalogue ships only
the standardised identification DIDs (0xF190 VIN and friends, ISO 14229-1
Annex C) and loads everything else from a user-supplied overlay
(``data/signals.json``) with ``verified=False``.

To discover DIDs for your own car, use the read-only ``read_did`` escape hatch
and log what comes back -- see docs/FREELANDER2.md.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Iterator, Mapping

from majster_ai.errors import ConfigError, UdsProtocolError, UnknownSignalError

_DATA_DIR: Final = Path(__file__).resolve().parent / "data"
DEFAULT_SIGNAL_MAP_PATH: Final = _DATA_DIR / "signals.json"


class SignalSource(str, enum.Enum):
    """Which UDS/OBD service carries this signal."""

    #: OBD-II service 0x01 -- legislated, one-byte PID.
    OBD_PID = "obd_pid"
    #: UDS service 0x22 ReadDataByIdentifier -- two-byte DID.
    UDS_DID = "uds_did"


# ---------------------------------------------------------------------------
# Scaling helpers.
#
# Each takes the raw data bytes for the signal (excluding the PID/DID echo) and
# returns an engineering value. They are module-level named functions rather
# than lambdas so they are individually unit-testable and show up in tracebacks.
# ---------------------------------------------------------------------------
def _u8(data: bytes) -> int:
    return data[0]


def _u16(data: bytes) -> int:
    return (data[0] << 8) | data[1]


def _percent_255(data: bytes) -> float:
    """``A * 100 / 255`` -- the standard J1979 percentage scaling."""
    return round(data[0] * 100.0 / 255.0, 2)


def _temp_offset_40(data: bytes) -> int:
    """``A - 40`` degrees Celsius."""
    return data[0] - 40


def _rpm(data: bytes) -> float:
    """``(256A + B) / 4`` revolutions per minute."""
    return round(_u16(data) / 4.0, 1)


def _maf(data: bytes) -> float:
    """``(256A + B) / 100`` grams per second."""
    return round(_u16(data) / 100.0, 2)


def _fuel_rail_gauge_kpa(data: bytes) -> int:
    """``(256A + B) * 10`` kPa -- diesel common-rail range."""
    return _u16(data) * 10


def _fuel_rail_rel_kpa(data: bytes) -> float:
    """``(256A + B) * 0.079`` kPa, relative to manifold vacuum."""
    return round(_u16(data) * 0.079, 2)


def _module_voltage(data: bytes) -> float:
    """``(256A + B) / 1000`` volts."""
    return round(_u16(data) / 1000.0, 3)


def _egr_error(data: bytes) -> float:
    """``A * 100 / 128 - 100`` percent."""
    return round(data[0] * 100.0 / 128.0 - 100.0, 2)


def _timing_advance(data: bytes) -> float:
    """``A / 2 - 64`` degrees before TDC."""
    return round(data[0] / 2.0 - 64.0, 1)


def _absolute_load(data: bytes) -> float:
    """``(256A + B) * 100 / 255`` percent."""
    return round(_u16(data) * 100.0 / 255.0, 2)


def _fuel_rate(data: bytes) -> float:
    """``(256A + B) / 20`` litres per hour."""
    return round(_u16(data) / 20.0, 2)


def _torque_percent(data: bytes) -> int:
    """``A - 125`` percent."""
    return data[0] - 125


def _ascii_text(data: bytes) -> str:
    """Decode an ASCII identification block (VIN, part numbers)."""
    return data.decode("ascii", errors="replace").strip("\x00 \t\r\n")


@dataclass(frozen=True, slots=True)
class SignalDefinition:
    """One readable live-data signal."""

    name: str
    description: str
    unit: str
    source: SignalSource
    identifier: int
    """One-byte PID for OBD, two-byte DID for UDS."""

    data_length: int
    """Expected payload length in bytes. 0 means variable (e.g. ASCII)."""

    formula: str
    decoder: Callable[[bytes], Any]
    aliases: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    """Plausibility range. Values outside it are flagged, not discarded --
    an impossible reading is itself diagnostic information (unplugged sensor,
    open circuit), so we surface it with a warning rather than hide it."""

    verified: bool = False

    @property
    def key(self) -> str:
        return self.name.upper()

    @property
    def identifier_hex(self) -> str:
        width = 2 if self.source is SignalSource.OBD_PID else 4
        return f"0x{self.identifier:0{width}X}"

    def matches(self, token: str) -> bool:
        needle = token.strip().upper().replace(" ", "_").replace("-", "_")
        if needle == self.key or needle in {a.upper() for a in self.aliases}:
            return True
        # Allow addressing by raw identifier: "0x0C" / "0C" / "0xF190".
        try:
            value = int(needle, 16)
        except ValueError:
            return False
        return value == self.identifier

    def decode(self, data: bytes | bytearray) -> Any:
        """Apply the scaling formula to raw payload bytes.

        Raises:
            UdsProtocolError: if the payload is shorter than the signal needs.
                A short payload means the response was truncated; decoding it
                anyway would silently produce a plausible-looking wrong number.
        """
        payload = bytes(data)
        if self.data_length and len(payload) < self.data_length:
            raise UdsProtocolError(
                f"Signal {self.name} ({self.identifier_hex}) needs "
                f"{self.data_length} byte(s), got {len(payload)}: "
                f"{payload.hex().upper() or '<empty>'}"
            )
        if not payload:
            raise UdsProtocolError(f"Signal {self.name} ({self.identifier_hex}) returned no data")
        try:
            return self.decoder(payload)
        except UdsProtocolError:
            raise
        except Exception as exc:  # a malformed frame must not crash the server
            raise UdsProtocolError(
                f"Cannot decode {self.name} from {payload.hex().upper()}: {exc}"
            ) from exc

    def plausibility_warning(self, value: Any) -> str | None:
        """Return a warning when a decoded value is physically implausible."""
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        if self.minimum is not None and value < self.minimum:
            return (
                f"{value} {self.unit} is below the plausible minimum "
                f"({self.minimum} {self.unit}) - suspect an open circuit, an "
                f"unplugged sensor, or that this identifier means something "
                f"else on this ECU."
            )
        if self.maximum is not None and value > self.maximum:
            return (
                f"{value} {self.unit} is above the plausible maximum "
                f"({self.maximum} {self.unit}) - suspect a short to power, a "
                f"scaling mismatch, or that this identifier means something "
                f"else on this ECU."
            )
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "unit": self.unit,
            "source": self.source.value,
            "identifier": self.identifier_hex,
            "data_length": self.data_length,
            "formula": self.formula,
            "aliases": list(self.aliases),
            "verified": self.verified,
            "plausible_range": (
                None
                if self.minimum is None and self.maximum is None
                else {"min": self.minimum, "max": self.maximum}
            ),
        }


#: Legislated SAE J1979 / ISO 15031-5 Mode 0x01 PIDs. Identifiers *and*
#: scaling are standard, so these are marked verified.
STANDARD_OBD_PIDS: Final[tuple[SignalDefinition, ...]] = (
    SignalDefinition(
        "ENGINE_LOAD",
        "Calculated engine load",
        "%",
        SignalSource.OBD_PID,
        0x04,
        1,
        "A * 100 / 255",
        _percent_255,
        aliases=("load", "calculated_load"),
        minimum=0,
        maximum=100,
        verified=True,
    ),
    SignalDefinition(
        "COOLANT_TEMP",
        "Engine coolant temperature",
        "degC",
        SignalSource.OBD_PID,
        0x05,
        1,
        "A - 40",
        _temp_offset_40,
        aliases=("ect", "coolant", "water_temp"),
        minimum=-40,
        maximum=215,
        verified=True,
    ),
    SignalDefinition(
        "MAP",
        "Intake manifold absolute pressure",
        "kPa",
        SignalSource.OBD_PID,
        0x0B,
        1,
        "A",
        _u8,
        aliases=("manifold_pressure", "boost_abs"),
        minimum=0,
        maximum=255,
        verified=True,
    ),
    SignalDefinition(
        "RPM",
        "Engine speed",
        "rpm",
        SignalSource.OBD_PID,
        0x0C,
        2,
        "(256A + B) / 4",
        _rpm,
        aliases=("engine_speed", "revs"),
        minimum=0,
        maximum=8000,
        verified=True,
    ),
    SignalDefinition(
        "SPEED",
        "Vehicle speed",
        "km/h",
        SignalSource.OBD_PID,
        0x0D,
        1,
        "A",
        _u8,
        aliases=("vehicle_speed", "vss"),
        minimum=0,
        maximum=255,
        verified=True,
    ),
    SignalDefinition(
        "TIMING_ADVANCE",
        "Timing advance before TDC",
        "deg",
        SignalSource.OBD_PID,
        0x0E,
        1,
        "A / 2 - 64",
        _timing_advance,
        aliases=("ignition_timing",),
        minimum=-64,
        maximum=63.5,
        verified=True,
    ),
    SignalDefinition(
        "INTAKE_TEMP",
        "Intake air temperature",
        "degC",
        SignalSource.OBD_PID,
        0x0F,
        1,
        "A - 40",
        _temp_offset_40,
        aliases=("iat", "air_temp"),
        minimum=-40,
        maximum=215,
        verified=True,
    ),
    SignalDefinition(
        "MAF",
        "Mass air flow rate",
        "g/s",
        SignalSource.OBD_PID,
        0x10,
        2,
        "(256A + B) / 100",
        _maf,
        aliases=("mass_air_flow", "airflow"),
        minimum=0,
        maximum=655.35,
        verified=True,
    ),
    SignalDefinition(
        "THROTTLE_POS",
        "Throttle position",
        "%",
        SignalSource.OBD_PID,
        0x11,
        1,
        "A * 100 / 255",
        _percent_255,
        aliases=("tps", "throttle"),
        minimum=0,
        maximum=100,
        verified=True,
    ),
    SignalDefinition(
        "RUN_TIME",
        "Run time since engine start",
        "s",
        SignalSource.OBD_PID,
        0x1F,
        2,
        "256A + B",
        _u16,
        aliases=("runtime", "engine_run_time"),
        minimum=0,
        maximum=65535,
        verified=True,
    ),
    SignalDefinition(
        "DISTANCE_MIL_ON",
        "Distance travelled with MIL on",
        "km",
        SignalSource.OBD_PID,
        0x21,
        2,
        "256A + B",
        _u16,
        aliases=("mil_distance",),
        minimum=0,
        maximum=65535,
        verified=True,
    ),
    SignalDefinition(
        "FUEL_RAIL_PRESSURE_REL",
        "Fuel rail pressure relative to manifold vacuum",
        "kPa",
        SignalSource.OBD_PID,
        0x22,
        2,
        "(256A + B) * 0.079",
        _fuel_rail_rel_kpa,
        aliases=("rail_pressure_rel",),
        minimum=0,
        maximum=5177.27,
        verified=True,
    ),
    SignalDefinition(
        "FUEL_RAIL_PRESSURE",
        "Fuel rail gauge pressure (diesel common rail)",
        "kPa",
        SignalSource.OBD_PID,
        0x23,
        2,
        "(256A + B) * 10",
        _fuel_rail_gauge_kpa,
        aliases=("rail_pressure", "common_rail_pressure"),
        minimum=0,
        maximum=655350,
        verified=True,
    ),
    SignalDefinition(
        "COMMANDED_EGR",
        "Commanded EGR",
        "%",
        SignalSource.OBD_PID,
        0x2C,
        1,
        "A * 100 / 255",
        _percent_255,
        aliases=("egr_commanded",),
        minimum=0,
        maximum=100,
        verified=True,
    ),
    SignalDefinition(
        "EGR_ERROR",
        "EGR error",
        "%",
        SignalSource.OBD_PID,
        0x2D,
        1,
        "A * 100 / 128 - 100",
        _egr_error,
        aliases=("egr_err",),
        minimum=-100,
        maximum=99.2,
        verified=True,
    ),
    SignalDefinition(
        "FUEL_LEVEL",
        "Fuel tank level input",
        "%",
        SignalSource.OBD_PID,
        0x2F,
        1,
        "A * 100 / 255",
        _percent_255,
        aliases=("fuel", "tank_level"),
        minimum=0,
        maximum=100,
        verified=True,
    ),
    SignalDefinition(
        "DISTANCE_SINCE_CLEAR",
        "Distance since DTCs were cleared",
        "km",
        SignalSource.OBD_PID,
        0x31,
        2,
        "256A + B",
        _u16,
        aliases=("distance_cleared",),
        minimum=0,
        maximum=65535,
        verified=True,
    ),
    SignalDefinition(
        "BAROMETRIC_PRESSURE",
        "Absolute barometric pressure",
        "kPa",
        SignalSource.OBD_PID,
        0x33,
        1,
        "A",
        _u8,
        aliases=("baro", "ambient_pressure"),
        minimum=0,
        maximum=255,
        verified=True,
    ),
    SignalDefinition(
        "MODULE_VOLTAGE",
        "Control module supply voltage",
        "V",
        SignalSource.OBD_PID,
        0x42,
        2,
        "(256A + B) / 1000",
        _module_voltage,
        aliases=("battery_voltage", "vbat", "voltage"),
        minimum=0,
        maximum=65.535,
        verified=True,
    ),
    SignalDefinition(
        "ABSOLUTE_LOAD",
        "Absolute load value",
        "%",
        SignalSource.OBD_PID,
        0x43,
        2,
        "(256A + B) * 100 / 255",
        _absolute_load,
        aliases=("abs_load",),
        minimum=0,
        maximum=25700,
        verified=True,
    ),
    SignalDefinition(
        "AMBIENT_TEMP",
        "Ambient air temperature",
        "degC",
        SignalSource.OBD_PID,
        0x46,
        1,
        "A - 40",
        _temp_offset_40,
        aliases=("outside_temp", "ambient"),
        minimum=-40,
        maximum=215,
        verified=True,
    ),
    SignalDefinition(
        "OIL_TEMP",
        "Engine oil temperature",
        "degC",
        SignalSource.OBD_PID,
        0x5C,
        1,
        "A - 40",
        _temp_offset_40,
        aliases=("engine_oil_temp",),
        minimum=-40,
        maximum=210,
        verified=True,
    ),
    SignalDefinition(
        "FUEL_RATE",
        "Engine fuel rate",
        "L/h",
        SignalSource.OBD_PID,
        0x5E,
        2,
        "(256A + B) / 20",
        _fuel_rate,
        aliases=("consumption",),
        minimum=0,
        maximum=3212.75,
        verified=True,
    ),
    SignalDefinition(
        "ENGINE_TORQUE_PCT",
        "Actual engine percent torque",
        "%",
        SignalSource.OBD_PID,
        0x62,
        1,
        "A - 125",
        _torque_percent,
        aliases=("torque_pct",),
        minimum=-125,
        maximum=130,
        verified=True,
    ),
)

#: ISO 14229-1 Annex C standardised identification DIDs. The *identifiers* are
#: standard; the content is an ASCII string whose exact layout is the
#: manufacturer's choice, hence a permissive decoder.
STANDARD_UDS_DIDS: Final[tuple[SignalDefinition, ...]] = (
    SignalDefinition(
        "VIN",
        "Vehicle Identification Number",
        "",
        SignalSource.UDS_DID,
        0xF190,
        0,
        "ASCII",
        _ascii_text,
        aliases=("vehicle_id", "chassis_number"),
        verified=True,
    ),
    SignalDefinition(
        "ECU_SERIAL",
        "ECU serial number",
        "",
        SignalSource.UDS_DID,
        0xF18C,
        0,
        "ASCII",
        _ascii_text,
        aliases=("serial",),
        verified=True,
    ),
    SignalDefinition(
        "ECU_HARDWARE_NUMBER",
        "ECU hardware part number",
        "",
        SignalSource.UDS_DID,
        0xF191,
        0,
        "ASCII",
        _ascii_text,
        aliases=("hardware_number", "hw_number"),
        verified=True,
    ),
    SignalDefinition(
        "ECU_SOFTWARE_NUMBER",
        "ECU software part number",
        "",
        SignalSource.UDS_DID,
        0xF194,
        0,
        "ASCII",
        _ascii_text,
        aliases=("software_number", "sw_number"),
        verified=True,
    ),
    SignalDefinition(
        "ECU_MANUFACTURING_DATE",
        "ECU manufacturing date",
        "",
        SignalSource.UDS_DID,
        0xF18B,
        0,
        "BCD/ASCII",
        _ascii_text,
        aliases=("manufacturing_date",),
        verified=True,
    ),
)


class SignalCatalogue:
    """Resolvable collection of :class:`SignalDefinition` entries."""

    def __init__(self, signals: Iterable[SignalDefinition]) -> None:
        self._signals: tuple[SignalDefinition, ...] = tuple(signals)
        seen: set[str] = set()
        for signal in self._signals:
            if signal.key in seen:
                raise ConfigError(f"Duplicate signal name in catalogue: {signal.name!r}")
            seen.add(signal.key)

    def __iter__(self) -> Iterator[SignalDefinition]:
        return iter(self._signals)

    def __len__(self) -> int:
        return len(self._signals)

    @property
    def signals(self) -> tuple[SignalDefinition, ...]:
        return self._signals

    def resolve(self, token: str) -> SignalDefinition:
        """Look up a signal by name, alias or identifier.

        Raises:
            UnknownSignalError: listing the available names so the LLM can
                correct itself in one step.
        """
        if not token or not str(token).strip():
            raise UnknownSignalError("No signal specified.", known_signals=self.names()[:40])
        for signal in self._signals:
            if signal.matches(str(token)):
                return signal
        raise UnknownSignalError(
            f"Unknown signal {token!r}.",
            known_signals=self.names(),
            hint="Use list_signals() to see every supported name, or read_did() "
            "to read a raw identifier that is not in the catalogue.",
        )

    def names(self) -> list[str]:
        return [s.name for s in self._signals]

    def by_source(self, source: SignalSource) -> list[SignalDefinition]:
        return [s for s in self._signals if s.source is source]

    def to_dict(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self._signals]

    def with_overrides(self, overrides: Iterable[SignalDefinition]) -> SignalCatalogue:
        merged: dict[str, SignalDefinition] = {s.key: s for s in self._signals}
        for signal in overrides:
            merged[signal.key] = signal
        return SignalCatalogue(merged.values())


def _linear_decoder(
    scale: float, offset: float, length: int, signed: bool
) -> Callable[[bytes], Any]:
    """Build a decoder for ``raw * scale + offset`` over ``length`` bytes."""

    def decode(data: bytes) -> float:
        raw = int.from_bytes(data[:length], byteorder="big", signed=signed)
        value = raw * scale + offset
        return round(value, 4)

    return decode


def signal_from_dict(payload: Mapping[str, Any]) -> SignalDefinition:
    """Build a :class:`SignalDefinition` from a JSON overlay entry.

    Overlay entries describe a linear scaling::

        {
          "name": "DPF_DIFF_PRESSURE",
          "description": "DPF differential pressure",
          "unit": "mbar",
          "source": "uds_did",
          "identifier": "0x2C05",
          "data_length": 2,
          "scale": 0.1, "offset": 0, "signed": false,
          "min": -50, "max": 1000,
          "aliases": ["dpf_delta_p"]
        }

    Raises:
        ConfigError: on any missing or malformed field.
    """
    try:
        name = str(payload["name"]).strip().upper()
        source = SignalSource(str(payload.get("source", "uds_did")))
        identifier_raw = payload["identifier"]
    except KeyError as exc:
        raise ConfigError(f"Signal entry is missing {exc.args[0]!r}: {dict(payload)!r}") from exc
    except ValueError as exc:
        raise ConfigError(
            f"Signal {payload.get('name')!r}: source must be 'obd_pid' or 'uds_did'"
        ) from exc
    if not name:
        raise ConfigError("Signal entry has an empty 'name'")

    if isinstance(identifier_raw, int):
        identifier = identifier_raw
    else:
        try:
            identifier = int(str(identifier_raw), 16)
        except ValueError as exc:
            raise ConfigError(
                f"Signal {name!r}: identifier {identifier_raw!r} is not valid hex"
            ) from exc

    limit = 0xFF if source is SignalSource.OBD_PID else 0xFFFF
    if not 0 <= identifier <= limit:
        raise ConfigError(
            f"Signal {name!r}: identifier 0x{identifier:X} out of range for {source.value}"
        )

    length = int(payload.get("data_length", 1))
    if length < 0:
        raise ConfigError(f"Signal {name!r}: data_length must be >= 0")

    scale = float(payload.get("scale", 1.0))
    offset = float(payload.get("offset", 0.0))
    signed = bool(payload.get("signed", False))
    aliases = payload.get("aliases", ())
    if isinstance(aliases, str):
        aliases = [aliases]

    return SignalDefinition(
        name=name,
        description=str(payload.get("description", "")),
        unit=str(payload.get("unit", "")),
        source=source,
        identifier=identifier,
        data_length=length,
        formula=f"raw * {scale} + {offset}" if length else "ASCII",
        decoder=_ascii_text if length == 0 else _linear_decoder(scale, offset, length, signed),
        aliases=tuple(str(a).upper() for a in aliases),
        minimum=None if payload.get("min") is None else float(payload["min"]),
        maximum=None if payload.get("max") is None else float(payload["max"]),
        # Overlay entries are user-supplied and unproven by definition, unless
        # the user has explicitly confirmed them against their own vehicle.
        verified=bool(payload.get("verified", False)),
    )


def load_signal_catalogue(
    path: str | Path | None = None, *, strict: bool = False
) -> SignalCatalogue:
    """Load the built-in catalogue, merged with a JSON overlay if present.

    Args:
        path: Overlay file. ``None`` uses the packaged ``data/signals.json``.
        strict: Raise when the overlay is missing rather than falling back.

    Raises:
        ConfigError: if the overlay exists but cannot be parsed.
    """
    base = SignalCatalogue(STANDARD_OBD_PIDS + STANDARD_UDS_DIDS)
    overlay_path = Path(path) if path is not None else DEFAULT_SIGNAL_MAP_PATH

    if not overlay_path.is_file():
        if strict:
            raise ConfigError(f"Signal overlay not found: {overlay_path}")
        return base

    try:
        raw = json.loads(overlay_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Signal overlay {overlay_path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read signal overlay {overlay_path}: {exc}") from exc

    if isinstance(raw, Mapping):
        raw = raw.get("signals", [])
    if not isinstance(raw, list):
        raise ConfigError(
            f"Signal overlay {overlay_path} must be a JSON list, or an object "
            f"with a 'signals' list."
        )
    return base.with_overrides(signal_from_dict(entry) for entry in raw)


__all__ = [
    "DEFAULT_SIGNAL_MAP_PATH",
    "SignalSource",
    "SignalDefinition",
    "SignalCatalogue",
    "STANDARD_OBD_PIDS",
    "STANDARD_UDS_DIDS",
    "signal_from_dict",
    "load_signal_catalogue",
]

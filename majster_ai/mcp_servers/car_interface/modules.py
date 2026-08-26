"""ECU address map for the Land Rover Freelander 2 (2010, 2.2 TD4).

Honesty about provenance
------------------------
Only two diagnostic addresses on any vehicle are *legislated* and therefore
safe to hard-code: the OBD-II functional broadcast (0x7DF) and the powertrain
physical addresses 0x7E0/0x7E8 and 0x7E1/0x7E9 (ISO 15765-4). Everything else
in a JLR car is manufacturer-specific and varies by model year, market and
build.

So every entry below carries a :attr:`EcuModule.verified` flag:

* ``verified=True``  -- legislated by ISO 15765-4. Trustworthy.
* ``verified=False`` -- community-derived starting point. **Confirm before
  relying on it.** An unverified address that happens to belong to a different
  module is how people brick things.

Rather than pretending to certainty we do not have, the car interface exposes
``scan_modules()``: it probes the address range with a harmless TesterPresent
and reports which addresses actually answer *on your car*. Use it once, then
write the results into a JSON overlay (see :func:`load_module_map`) and you
have a verified map for your specific vehicle.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Iterable, Iterator, Mapping

from majster_ai.errors import ConfigError, UnknownModuleError

#: ISO 15765-4 functional ("broadcast") request address. Every OBD-compliant
#: ECU listens here; responses come back on 0x7E8..0x7EF.
OBD_FUNCTIONAL_REQUEST_ID: Final = 0x7DF

#: Where the packaged default overlay lives.
_DATA_DIR: Final = Path(__file__).resolve().parent / "data"
DEFAULT_MODULE_MAP_PATH: Final = _DATA_DIR / "modules.json"


@dataclass(frozen=True, slots=True)
class EcuModule:
    """One addressable control module."""

    name: str
    """Canonical short name, e.g. ``ECM``."""

    description: str
    request_id: int
    """Physical request CAN id (tester -> ECU)."""

    response_id: int
    """Physical response CAN id (ECU -> tester)."""

    aliases: tuple[str, ...] = ()
    """Other names the operator might type, e.g. ``engine``, ``pcm``."""

    verified: bool = False
    """True only for ISO 15765-4 legislated addresses."""

    extended_session_required: bool = False
    """Whether reads beyond DTCs typically need session 0x03 on this module."""

    notes: str = ""

    @property
    def is_extended_addressing(self) -> bool:
        """True when the ids are 29-bit rather than 11-bit."""
        return self.request_id > 0x7FF or self.response_id > 0x7FF

    def matches(self, token: str) -> bool:
        """Case-insensitive match against the name, an alias, or a hex id."""
        needle = token.strip().lower()
        if needle == self.name.lower() or needle in {a.lower() for a in self.aliases}:
            return True
        # Allow addressing by request id: "0x7e0" or "7e0".
        try:
            value = int(needle, 16) if not needle.startswith("0x") else int(needle, 16)
        except ValueError:
            return False
        return value == self.request_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "request_id": f"0x{self.request_id:03X}",
            "response_id": f"0x{self.response_id:03X}",
            "aliases": list(self.aliases),
            "verified": self.verified,
            "extended_session_required": self.extended_session_required,
            "notes": self.notes,
        }


#: Built-in map for the Freelander 2. Read the module docstring before trusting
#: any entry whose ``verified`` flag is False.
DEFAULT_MODULES: Final[tuple[EcuModule, ...]] = (
    EcuModule(
        name="ECM",
        description="Engine Control Module - 2.2 TD4 (DW12B/DW12C, Denso/Siemens)",
        request_id=0x7E0,
        response_id=0x7E8,
        aliases=("engine", "pcm", "ecu", "dde"),
        verified=True,
        notes="ISO 15765-4 legislated powertrain address. Safe.",
    ),
    EcuModule(
        name="TCM",
        description="Transmission Control Module (Aisin AWF21 6-speed automatic)",
        request_id=0x7E1,
        response_id=0x7E9,
        aliases=("transmission", "gearbox", "auto"),
        verified=True,
        notes=(
            "ISO 15765-4 legislated secondary powertrain address. Only present on "
            "automatic cars; a manual TD4 will simply not answer here."
        ),
    ),
    EcuModule(
        name="ABS",
        description="Anti-lock Braking System / DSC / Hill Descent Control",
        request_id=0x760,
        response_id=0x768,
        aliases=("brakes", "dsc", "esp", "hdc"),
        verified=False,
        extended_session_required=True,
        notes="UNVERIFIED community address - confirm with scan_modules().",
    ),
    EcuModule(
        name="RCM",
        description="Restraints Control Module (airbags, pretensioners)",
        request_id=0x737,
        response_id=0x73F,
        aliases=("srs", "airbag", "restraints"),
        verified=False,
        extended_session_required=True,
        notes=(
            "UNVERIFIED community address - confirm with scan_modules(). "
            "SAFETY-CRITICAL MODULE: read-only. Never write here."
        ),
    ),
    EcuModule(
        name="CJB",
        description="Central Junction Box / Body Control Module",
        request_id=0x726,
        response_id=0x72E,
        aliases=("bcm", "body", "junction"),
        verified=False,
        extended_session_required=True,
        notes="UNVERIFIED community address - confirm with scan_modules().",
    ),
    EcuModule(
        name="IPC",
        description="Instrument Panel Cluster",
        request_id=0x720,
        response_id=0x728,
        aliases=("cluster", "dash", "instrument"),
        verified=False,
        notes="UNVERIFIED community address - confirm with scan_modules().",
    ),
    EcuModule(
        name="HALDEX",
        description="Haldex Gen4 rear-axle coupling (AWD clutch controller)",
        request_id=0x731,
        response_id=0x739,
        aliases=("awd", "4wd", "rdm", "coupling"),
        verified=False,
        extended_session_required=True,
        notes=(
            "UNVERIFIED community address - confirm with scan_modules(). "
            "Pre-charge pump and filter servicing is the usual reason to look here."
        ),
    ),
    EcuModule(
        name="TRM",
        description="Terrain Response Module",
        request_id=0x733,
        response_id=0x73B,
        aliases=("terrain", "terrain_response"),
        verified=False,
        notes="UNVERIFIED community address - confirm with scan_modules().",
    ),
    EcuModule(
        name="PBM",
        description="Parking Brake Module (electric park brake)",
        request_id=0x72B,
        response_id=0x72F,
        aliases=("epb", "handbrake", "parkbrake"),
        verified=False,
        extended_session_required=True,
        notes="UNVERIFIED community address - confirm with scan_modules().",
    ),
    EcuModule(
        name="HVAC",
        description="Heating, Ventilation and Air Conditioning control",
        request_id=0x7A3,
        response_id=0x7AB,
        aliases=("climate", "ac", "heater"),
        verified=False,
        notes="UNVERIFIED community address - confirm with scan_modules().",
    ),
    EcuModule(
        name="PAM",
        description="Parking Aid Module (reverse sensors)",
        request_id=0x736,
        response_id=0x73E,
        aliases=("parking", "pdc", "sensors"),
        verified=False,
        notes="UNVERIFIED community address - confirm with scan_modules().",
    ),
)


class ModuleMap:
    """A resolvable collection of :class:`EcuModule` entries."""

    def __init__(self, modules: Iterable[EcuModule]) -> None:
        self._modules: tuple[EcuModule, ...] = tuple(modules)
        if not self._modules:
            raise ConfigError("Module map is empty - at least one ECU must be defined.")
        seen: dict[str, str] = {}
        by_request_id: dict[int, str] = {}
        for module in self._modules:
            key = module.name.lower()
            if key in seen:
                raise ConfigError(f"Duplicate module name in map: {module.name!r}")
            seen[key] = module.name
            # A physical request id addresses exactly one ECU. Two modules
            # claiming the same id is always a data error -- in our defaults or
            # in a user overlay -- and would silently misroute every request.
            if module.request_id in by_request_id:
                raise ConfigError(
                    f"Modules {by_request_id[module.request_id]!r} and {module.name!r} "
                    f"both claim request id 0x{module.request_id:03X}. A physical "
                    f"request id addresses exactly one ECU."
                )
            by_request_id[module.request_id] = module.name

    def __iter__(self) -> Iterator[EcuModule]:
        return iter(self._modules)

    def __len__(self) -> int:
        return len(self._modules)

    def __contains__(self, token: object) -> bool:
        return isinstance(token, str) and any(m.matches(token) for m in self._modules)

    @property
    def modules(self) -> tuple[EcuModule, ...]:
        return self._modules

    def resolve(self, token: str) -> EcuModule:
        """Look up a module by name, alias or request id.

        Raises:
            UnknownModuleError: with the list of valid names, so the LLM can
                immediately retry with a correct one instead of guessing again.
        """
        if not token or not token.strip():
            raise UnknownModuleError(
                "No module specified.",
                known_modules=[m.name for m in self._modules],
            )
        for module in self._modules:
            if module.matches(token):
                return module
        raise UnknownModuleError(
            f"Unknown module {token!r}.",
            known_modules=[m.name for m in self._modules],
            hint="Use one of the known names, or run scan_modules() to discover "
            "which addresses actually answer on this vehicle.",
        )

    def names(self) -> list[str]:
        return [m.name for m in self._modules]

    def verified(self) -> list[EcuModule]:
        return [m for m in self._modules if m.verified]

    def unverified(self) -> list[EcuModule]:
        return [m for m in self._modules if not m.verified]

    def to_dict(self) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self._modules]

    def with_overrides(self, overrides: Iterable[EcuModule]) -> ModuleMap:
        """Return a new map where ``overrides`` replace or extend existing entries."""
        merged: dict[str, EcuModule] = {m.name.lower(): m for m in self._modules}
        for module in overrides:
            merged[module.name.lower()] = module
        return ModuleMap(merged.values())


def _parse_can_id(value: Any, *, field: str, module: str) -> int:
    """Accept ``"0x7E0"``, ``"7E0"`` or ``2016`` for a CAN id."""
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly
        raise ConfigError(f"Module {module!r}: {field} must be a CAN id, got a boolean")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        try:
            parsed = int(text, 16) if text.lower().startswith("0x") else int(text, 16)
        except ValueError as exc:
            raise ConfigError(
                f"Module {module!r}: {field}={value!r} is not a valid hex CAN id"
            ) from exc
    else:
        raise ConfigError(f"Module {module!r}: {field} must be a string or int")
    if not 0 <= parsed <= 0x1FFFFFFF:
        raise ConfigError(f"Module {module!r}: {field}=0x{parsed:X} is out of CAN id range")
    return parsed


def module_from_dict(payload: Mapping[str, Any]) -> EcuModule:
    """Build an :class:`EcuModule` from a JSON object.

    Raises:
        ConfigError: on any missing or malformed field.
    """
    try:
        name = str(payload["name"]).strip()
    except KeyError as exc:
        raise ConfigError(f"Module entry is missing 'name': {dict(payload)!r}") from exc
    if not name:
        raise ConfigError("Module entry has an empty 'name'")

    for required in ("request_id", "response_id"):
        if required not in payload:
            raise ConfigError(f"Module {name!r} is missing {required!r}")

    aliases = payload.get("aliases", ())
    if isinstance(aliases, str):
        aliases = [aliases]

    return EcuModule(
        name=name,
        description=str(payload.get("description", "")),
        request_id=_parse_can_id(payload["request_id"], field="request_id", module=name),
        response_id=_parse_can_id(payload["response_id"], field="response_id", module=name),
        aliases=tuple(str(a) for a in aliases),
        verified=bool(payload.get("verified", False)),
        extended_session_required=bool(payload.get("extended_session_required", False)),
        notes=str(payload.get("notes", "")),
    )


def load_module_map(path: str | Path | None = None, *, strict: bool = False) -> ModuleMap:
    """Load the module map, optionally merged with a JSON overlay.

    The overlay lets you correct the unverified addresses for *your* car
    without editing the package::

        [
          {"name": "ABS", "request_id": "0x760", "response_id": "0x768",
           "verified": true, "notes": "confirmed by scan on my 2010 TD4"}
        ]

    Args:
        path: Overlay file. When ``None`` the packaged
            ``data/modules.json`` is used if it exists.
        strict: Raise if the overlay file is missing, instead of falling back
            to the built-in map.

    Raises:
        ConfigError: if the overlay exists but cannot be parsed.
    """
    base = ModuleMap(DEFAULT_MODULES)
    overlay_path = Path(path) if path is not None else DEFAULT_MODULE_MAP_PATH

    if not overlay_path.is_file():
        if strict:
            raise ConfigError(f"Module map overlay not found: {overlay_path}")
        return base

    try:
        raw = json.loads(overlay_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Module map {overlay_path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read module map {overlay_path}: {exc}") from exc

    if isinstance(raw, Mapping):
        raw = raw.get("modules", [])
    if not isinstance(raw, list):
        raise ConfigError(
            f"Module map {overlay_path} must be a JSON list of modules, or an "
            f"object with a 'modules' list."
        )
    return base.with_overrides(module_from_dict(entry) for entry in raw)


def mark_verified(module: EcuModule, *, notes: str = "") -> EcuModule:
    """Return a copy marked as verified -- used after a successful scan."""
    return replace(module, verified=True, notes=notes or module.notes)


__all__ = [
    "OBD_FUNCTIONAL_REQUEST_ID",
    "DEFAULT_MODULE_MAP_PATH",
    "EcuModule",
    "DEFAULT_MODULES",
    "ModuleMap",
    "module_from_dict",
    "load_module_map",
    "mark_verified",
]

"""An in-process UDS ECU simulator modelled on a Freelander 2 (2.2 TD4).

Why a simulator rather than ``unittest.mock``
---------------------------------------------
Mocking ``send_request`` would let us assert that the code *called* something.
It would not catch a wrong status-mask byte, a mis-ordered DID echo, or a
retry loop that mishandles NRC 0x78 -- exactly the bugs that only appear when
you are lying under a car with the engine running.

So the simulator speaks real UDS bytes. Every layer above it (session
handling, retries, DTC decode, the MCP tools, the LangGraph agent) runs
against the same byte stream it will see on a real bus. Tests then use
``unittest.mock`` on top of this for the narrow cases where a *transport*
failure needs simulating.

Fault injection
---------------
CAN is flaky, and code that has never met a flaky bus is not production code.
:class:`EcuSimulator` can be told to drop responses, answer "busy", or stall
with "response pending" so the retry logic is exercised deterministically::

    ecu.inject_faults(drop_next=2)            # two silent timeouts, then fine
    ecu.inject_faults(pending_next=3)         # three NRC 0x78, then the answer
    ecu.inject_faults(busy_next=1)            # one NRC 0x21 busyRepeatRequest
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Final, Iterable, Mapping

from majster_ai.logging_setup import get_logger
from majster_ai.mcp_servers.car_interface.dtc import encode_dtc_code

log = get_logger("mcp_servers.car_interface.simulator")

# --- UDS service ids -------------------------------------------------------
SID_OBD_CURRENT_DATA: Final = 0x01
SID_OBD_READ_DTC: Final = 0x03
SID_OBD_CLEAR_DTC: Final = 0x04
SID_DIAGNOSTIC_SESSION_CONTROL: Final = 0x10
SID_ECU_RESET: Final = 0x11
SID_CLEAR_DIAGNOSTIC_INFORMATION: Final = 0x14
SID_READ_DTC_INFORMATION: Final = 0x19
SID_READ_DATA_BY_IDENTIFIER: Final = 0x22
SID_SECURITY_ACCESS: Final = 0x27
SID_TESTER_PRESENT: Final = 0x3E

#: Positive responses are the request SID + 0x40.
POSITIVE_RESPONSE_OFFSET: Final = 0x40
NEGATIVE_RESPONSE_SID: Final = 0x7F

# --- Negative response codes we emit ---------------------------------------
NRC_SUB_FUNCTION_NOT_SUPPORTED: Final = 0x12
NRC_INCORRECT_LENGTH: Final = 0x13
NRC_BUSY_REPEAT_REQUEST: Final = 0x21
NRC_CONDITIONS_NOT_CORRECT: Final = 0x22
NRC_REQUEST_OUT_OF_RANGE: Final = 0x31
NRC_SECURITY_ACCESS_DENIED: Final = 0x33
NRC_SERVICE_NOT_SUPPORTED: Final = 0x11
NRC_RESPONSE_PENDING: Final = 0x78

#: Diagnostic sessions.
SESSION_DEFAULT: Final = 0x01
SESSION_PROGRAMMING: Final = 0x02
SESSION_EXTENDED: Final = 0x03


# ---------------------------------------------------------------------------
# Encoders: engineering value -> raw OBD PID bytes.
#
# These are the exact inverses of the decoders in `pids.py`. Keeping both
# directions means a round-trip through the simulator is itself a check on the
# scaling formulas: if an encoder and a decoder disagree, a test fails.
# ---------------------------------------------------------------------------
def _e_u8(value: float) -> bytes:
    return bytes((int(round(value)) & 0xFF,))


def _e_u16(value: float) -> bytes:
    raw = int(round(value)) & 0xFFFF
    return bytes((raw >> 8, raw & 0xFF))


def _e_percent_255(value: float) -> bytes:
    return _e_u8(value * 255.0 / 100.0)


def _e_temp_offset_40(value: float) -> bytes:
    return _e_u8(value + 40)


def _e_rpm(value: float) -> bytes:
    return _e_u16(value * 4)


def _e_maf(value: float) -> bytes:
    return _e_u16(value * 100)


def _e_rail_kpa(value: float) -> bytes:
    return _e_u16(value / 10)


def _e_voltage(value: float) -> bytes:
    return _e_u16(value * 1000)


def _e_timing(value: float) -> bytes:
    return _e_u8((value + 64) * 2)


def _e_egr_error(value: float) -> bytes:
    return _e_u8((value + 100) * 128.0 / 100.0)


def _e_fuel_rate(value: float) -> bytes:
    return _e_u16(value * 20)


def _e_torque(value: float) -> bytes:
    return _e_u8(value + 125)


def _e_absolute_load(value: float) -> bytes:
    return _e_u16(value * 255.0 / 100.0)


#: PID -> (encoder, byte length). Mirrors STANDARD_OBD_PIDS in pids.py.
OBD_PID_ENCODERS: Final[dict[int, Callable[[float], bytes]]] = {
    0x04: _e_percent_255,  # engine load
    0x05: _e_temp_offset_40,  # coolant temp
    0x0B: _e_u8,  # MAP
    0x0C: _e_rpm,  # RPM
    0x0D: _e_u8,  # speed
    0x0E: _e_timing,  # timing advance
    0x0F: _e_temp_offset_40,  # intake air temp
    0x10: _e_maf,  # MAF
    0x11: _e_percent_255,  # throttle
    0x1F: _e_u16,  # run time
    0x21: _e_u16,  # distance with MIL on
    0x23: _e_rail_kpa,  # fuel rail gauge pressure
    0x2C: _e_percent_255,  # commanded EGR
    0x2D: _e_egr_error,  # EGR error
    0x2F: _e_percent_255,  # fuel level
    0x31: _e_u16,  # distance since clear
    0x33: _e_u8,  # barometric pressure
    0x42: _e_voltage,  # module voltage
    0x43: _e_absolute_load,  # absolute load
    0x46: _e_temp_offset_40,  # ambient temp
    0x5C: _e_temp_offset_40,  # oil temp
    0x5E: _e_fuel_rate,  # fuel rate
    0x62: _e_torque,  # engine torque %
}


@dataclass
class SimulatedDtc:
    """A stored fault in the simulator's memory."""

    code: str
    status: int = 0x2F
    """Default: failed + failed-this-cycle + pending + confirmed + failed-since-clear."""

    def to_record(self) -> bytes:
        return encode_dtc_code(self.code) + bytes((self.status,))


@dataclass
class EcuSimulator:
    """One simulated control module.

    Args:
        name: Module short name, used in log lines.
        dtcs: Faults in memory at power-on.
        live_data: OBD PID values in engineering units, keyed by PID byte.
        identification: UDS DIDs (0x22) -> raw bytes.
        supports_obd: Whether legislated OBD-II service 0x01/0x03/0x04 works.
        clear_requires_extended_session: Realistic for non-powertrain modules.
    """

    name: str = "ECM"
    dtcs: list[SimulatedDtc] = field(default_factory=list)
    live_data: dict[int, float] = field(default_factory=dict)
    identification: dict[int, bytes] = field(default_factory=dict)
    supports_obd: bool = True
    clear_requires_extended_session: bool = False
    security_unlocked: bool = False

    session: int = SESSION_DEFAULT
    #: Every request the ECU has seen -- lets tests assert on the wire protocol.
    request_log: list[bytes] = field(default_factory=list)

    # --- fault injection ---
    _drop_remaining: int = 0
    _pending_remaining: int = 0
    _busy_remaining: int = 0

    def inject_faults(
        self,
        *,
        drop_next: int = 0,
        pending_next: int = 0,
        busy_next: int = 0,
    ) -> None:
        """Arm deterministic transport faults for the next N requests."""
        self._drop_remaining = max(0, drop_next)
        self._pending_remaining = max(0, pending_next)
        self._busy_remaining = max(0, busy_next)

    def clear_faults(self) -> None:
        self._drop_remaining = self._pending_remaining = self._busy_remaining = 0

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _negative(sid: int, nrc: int) -> bytes:
        return bytes((NEGATIVE_RESPONSE_SID, sid, nrc))

    @staticmethod
    def _positive(sid: int, *payload: Iterable[int] | int | bytes) -> bytes:
        out = bytearray((sid + POSITIVE_RESPONSE_OFFSET,))
        for part in payload:
            if isinstance(part, int):
                out.append(part & 0xFF)
            else:
                out.extend(bytes(part))
        return bytes(out)

    # -- the wire -----------------------------------------------------------
    def handle_request(self, payload: bytes) -> list[bytes]:
        """Process one UDS request and return zero or more response payloads.

        Returning a *list* models reality: an ECU may answer with several
        "response pending" frames before the real answer, or -- when it is
        asleep or the request was suppressed -- with nothing at all.
        """
        self.request_log.append(bytes(payload))

        if not payload:
            return []

        # Fault injection takes precedence, so tests can force the retry paths.
        if self._drop_remaining > 0:
            self._drop_remaining -= 1
            log.debug("%s: dropping request (fault injection)", self.name)
            return []

        sid = payload[0]

        if self._busy_remaining > 0:
            self._busy_remaining -= 1
            log.debug("%s: answering busyRepeatRequest (fault injection)", self.name)
            return [self._negative(sid, NRC_BUSY_REPEAT_REQUEST)]

        prefix: list[bytes] = []
        if self._pending_remaining > 0:
            prefix = [self._negative(sid, NRC_RESPONSE_PENDING)] * self._pending_remaining
            self._pending_remaining = 0
            log.debug("%s: %d response-pending frames (fault injection)", self.name, len(prefix))

        handler = {
            SID_OBD_CURRENT_DATA: self._obd_current_data,
            SID_OBD_READ_DTC: self._obd_read_dtc,
            SID_OBD_CLEAR_DTC: self._obd_clear_dtc,
            SID_DIAGNOSTIC_SESSION_CONTROL: self._session_control,
            SID_ECU_RESET: self._ecu_reset,
            SID_CLEAR_DIAGNOSTIC_INFORMATION: self._clear_diagnostic_information,
            SID_READ_DTC_INFORMATION: self._read_dtc_information,
            SID_READ_DATA_BY_IDENTIFIER: self._read_data_by_identifier,
            SID_SECURITY_ACCESS: self._security_access,
            SID_TESTER_PRESENT: self._tester_present,
        }.get(sid)

        if handler is None:
            return prefix + [self._negative(sid, NRC_SERVICE_NOT_SUPPORTED)]

        response = handler(payload)
        return prefix + ([] if response is None else [response])

    # -- service handlers ---------------------------------------------------
    def _session_control(self, payload: bytes) -> bytes:
        if len(payload) < 2:
            return self._negative(payload[0], NRC_INCORRECT_LENGTH)
        requested = payload[1] & 0x7F
        if requested not in (SESSION_DEFAULT, SESSION_PROGRAMMING, SESSION_EXTENDED):
            return self._negative(payload[0], NRC_SUB_FUNCTION_NOT_SUPPORTED)
        self.session = requested
        if requested == SESSION_DEFAULT:
            self.security_unlocked = False
        log.debug("%s: diagnostic session -> 0x%02X", self.name, requested)
        # P2 = 50 ms, P2* = 5000 ms, as ISO 14229-1 requires in the response.
        return self._positive(payload[0], requested, b"\x00\x32\x01\xf4")

    def _tester_present(self, payload: bytes) -> bytes | None:
        subfunction = payload[1] if len(payload) > 1 else 0x00
        # Bit 7 set means "suppress positive response" -- a real ECU stays silent.
        if subfunction & 0x80:
            return None
        return self._positive(payload[0], 0x00)

    def _ecu_reset(self, payload: bytes) -> bytes:
        if len(payload) < 2:
            return self._negative(payload[0], NRC_INCORRECT_LENGTH)
        if self.session == SESSION_DEFAULT:
            return self._negative(payload[0], NRC_CONDITIONS_NOT_CORRECT)
        log.info("%s: ECU reset requested (subfunction 0x%02X)", self.name, payload[1])
        self.session = SESSION_DEFAULT
        return self._positive(payload[0], payload[1] & 0x7F)

    def _read_dtc_information(self, payload: bytes) -> bytes:
        if len(payload) < 2:
            return self._negative(payload[0], NRC_INCORRECT_LENGTH)
        subfunction = payload[1]

        # 0x01 reportNumberOfDTCByStatusMask
        if subfunction == 0x01:
            if len(payload) < 3:
                return self._negative(payload[0], NRC_INCORRECT_LENGTH)
            matching = self._matching_dtcs(payload[2])
            count = len(matching)
            return self._positive(
                payload[0], subfunction, 0xFF, 0x01, (count >> 8) & 0xFF, count & 0xFF
            )

        # 0x02 reportDTCByStatusMask
        if subfunction == 0x02:
            if len(payload) < 3:
                return self._negative(payload[0], NRC_INCORRECT_LENGTH)
            records = b"".join(d.to_record() for d in self._matching_dtcs(payload[2]))
            # statusAvailabilityMask (0xFF) then 4-byte records.
            return self._positive(payload[0], subfunction, 0xFF, records)

        # 0x0A reportSupportedDTC
        if subfunction == 0x0A:
            records = b"".join(d.to_record() for d in self.dtcs)
            return self._positive(payload[0], subfunction, 0xFF, records)

        return self._negative(payload[0], NRC_SUB_FUNCTION_NOT_SUPPORTED)

    def _matching_dtcs(self, mask: int) -> list[SimulatedDtc]:
        """DTCs whose status shares at least one bit with the requested mask."""
        return [d for d in self.dtcs if d.status & mask]

    def _clear_diagnostic_information(self, payload: bytes) -> bytes:
        if len(payload) < 4:
            return self._negative(payload[0], NRC_INCORRECT_LENGTH)
        if self.clear_requires_extended_session and self.session != SESSION_EXTENDED:
            return self._negative(payload[0], NRC_CONDITIONS_NOT_CORRECT)

        group = (payload[1] << 16) | (payload[2] << 8) | payload[3]
        if group == 0xFFFFFF:
            cleared = len(self.dtcs)
            self.dtcs.clear()
        else:
            target = bytes(payload[1:4])
            before = len(self.dtcs)
            self.dtcs = [d for d in self.dtcs if encode_dtc_code(d.code) != target]
            cleared = before - len(self.dtcs)
        log.info("%s: cleared %d DTC(s) (group 0x%06X)", self.name, cleared, group)
        return self._positive(payload[0])

    def _read_data_by_identifier(self, payload: bytes) -> bytes:
        if len(payload) < 3:
            return self._negative(payload[0], NRC_INCORRECT_LENGTH)
        did = (payload[1] << 8) | payload[2]
        if did in self.identification:
            return self._positive(payload[0], payload[1], payload[2], self.identification[did])
        return self._negative(payload[0], NRC_REQUEST_OUT_OF_RANGE)

    def _security_access(self, payload: bytes) -> bytes:
        if len(payload) < 2:
            return self._negative(payload[0], NRC_INCORRECT_LENGTH)
        subfunction = payload[1]
        if self.session != SESSION_EXTENDED:
            return self._negative(payload[0], NRC_CONDITIONS_NOT_CORRECT)
        if subfunction % 2 == 1:  # requestSeed
            return self._positive(payload[0], subfunction, b"\x11\x22\x33\x44")
        # sendKey -- the simulator accepts any key; a real ECU would not.
        self.security_unlocked = True
        return self._positive(payload[0], subfunction)

    def _obd_current_data(self, payload: bytes) -> bytes | None:
        if not self.supports_obd:
            return self._negative(payload[0], NRC_SERVICE_NOT_SUPPORTED)
        if len(payload) < 2:
            return self._negative(payload[0], NRC_INCORRECT_LENGTH)
        pid = payload[1]
        if pid not in self.live_data:
            return self._negative(payload[0], NRC_REQUEST_OUT_OF_RANGE)
        encoder = OBD_PID_ENCODERS.get(pid)
        if encoder is None:  # pragma: no cover - guarded by scenario construction
            return self._negative(payload[0], NRC_REQUEST_OUT_OF_RANGE)
        return self._positive(payload[0], pid, encoder(self.live_data[pid]))

    def _obd_read_dtc(self, payload: bytes) -> bytes:
        """Legislated OBD service 0x03: confirmed emissions DTCs only."""
        if not self.supports_obd:
            return self._negative(payload[0], NRC_SERVICE_NOT_SUPPORTED)
        confirmed = [d for d in self.dtcs if d.status & 0x08]
        body = b"".join(encode_dtc_code(d.code)[:2] for d in confirmed)
        return self._positive(payload[0], len(confirmed), body)

    def _obd_clear_dtc(self, payload: bytes) -> bytes:
        if not self.supports_obd:
            return self._negative(payload[0], NRC_SERVICE_NOT_SUPPORTED)
        self.dtcs.clear()
        return self._positive(payload[0])

    # -- state --------------------------------------------------------------
    def set_live_value(self, pid: int, value: float) -> None:
        """Change a live value -- lets tests simulate a fault developing."""
        self.live_data[pid] = value

    def add_dtc(self, code: str, status: int = 0x2F) -> None:
        self.dtcs.append(SimulatedDtc(code=code, status=status))

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "session": f"0x{self.session:02X}",
            "dtc_count": len(self.dtcs),
            "dtcs": [d.code for d in self.dtcs],
            "requests_seen": len(self.request_log),
        }


class VehicleSimulator:
    """A whole car: several :class:`EcuSimulator` instances behind CAN ids."""

    def __init__(self, ecus: Mapping[int, EcuSimulator] | None = None) -> None:
        self._ecus: dict[int, EcuSimulator] = dict(ecus or {})

    def add(self, request_id: int, ecu: EcuSimulator) -> None:
        self._ecus[request_id] = ecu

    def get(self, request_id: int) -> EcuSimulator | None:
        return self._ecus.get(request_id)

    def addresses(self) -> list[int]:
        return sorted(self._ecus)

    def __contains__(self, request_id: object) -> bool:
        return request_id in self._ecus

    def __len__(self) -> int:
        return len(self._ecus)

    def reset(self) -> None:
        for ecu in self._ecus.values():
            ecu.session = SESSION_DEFAULT
            ecu.clear_faults()
            ecu.request_log.clear()


def build_freelander2_simulator() -> VehicleSimulator:
    """A 2010 Freelander 2 2.2 TD4 with a plausible set of faults.

    The fault set is deliberately realistic for this engine: an underboost
    code, a swirl-flap code and a pending EGR code together are a very common
    presentation, and they are *related* -- which gives the agent something
    worth reasoning about rather than a single obvious answer.
    """
    ecm = EcuSimulator(
        name="ECM",
        dtcs=[
            # Confirmed + active: the driver has a limp-mode complaint.
            SimulatedDtc("P0299", status=0x2F),
            # Confirmed: swirl flaps, a known weak point on the DW12.
            SimulatedDtc("P2015", status=0x2F),
            # Pending only: EGR is starting to fail but has not confirmed yet.
            SimulatedDtc("P0401", status=0x04),
        ],
        live_data={
            0x04: 24.7,  # engine load %
            0x05: 88,  # coolant degC - warmed up
            0x0B: 101,  # MAP kPa - atmospheric at idle
            0x0C: 812.0,  # rpm - idle
            0x0D: 0,  # km/h - stationary
            0x0F: 27,  # intake air degC
            0x10: 5.6,  # MAF g/s - idle for a 2.2 diesel
            0x11: 14.5,  # throttle %
            0x1F: 942,  # run time s
            0x21: 137,  # distance with MIL on km
            0x23: 26_000,  # fuel rail 260 bar
            0x2C: 31.4,  # commanded EGR %
            0x2D: -4.2,  # EGR error %
            0x2F: 62.7,  # fuel level %
            0x31: 1483,  # distance since clear km
            0x33: 100,  # barometric kPa
            0x42: 14.1,  # module voltage V - alternator charging
            0x43: 27.8,  # absolute load %
            0x46: 18,  # ambient degC
            0x5C: 91,  # oil temp degC
            0x5E: 1.35,  # fuel rate L/h
            0x62: 22,  # torque %
        },
        identification={
            0xF190: b"SALFA2BB8AH100001",  # VIN (a valid-format example)
            0xF18C: b"ECM-SIM-0000001",
            0xF191: b"6H52-12A650-BC",
            0xF194: b"9H52-14C204-AD",
            0xF18B: b"20100317",
        },
    )

    tcm = EcuSimulator(
        name="TCM",
        dtcs=[SimulatedDtc("U0100", status=0x08)],
        live_data={0x05: 86, 0x0C: 810.0},
        identification={0xF190: b"SALFA2BB8AH100001", 0xF18C: b"TCM-SIM-0000001"},
        supports_obd=True,
    )

    abs_module = EcuSimulator(
        name="ABS",
        dtcs=[SimulatedDtc("C1095", status=0x08)],
        identification={0xF18C: b"ABS-SIM-0000001"},
        supports_obd=False,
        clear_requires_extended_session=True,
    )

    haldex = EcuSimulator(
        name="HALDEX",
        dtcs=[],
        identification={0xF18C: b"HLD-SIM-0000001"},
        supports_obd=False,
        clear_requires_extended_session=True,
    )

    return VehicleSimulator({0x7E0: ecm, 0x7E1: tcm, 0x760: abs_module, 0x731: haldex})


def build_healthy_simulator() -> VehicleSimulator:
    """The same car with no stored faults -- for testing the 'all clear' path."""
    vehicle = build_freelander2_simulator()
    for address in vehicle.addresses():
        ecu = vehicle.get(address)
        if ecu is not None:
            ecu.dtcs.clear()
    return vehicle


__all__ = [
    "SID_READ_DTC_INFORMATION",
    "SID_READ_DATA_BY_IDENTIFIER",
    "SID_CLEAR_DIAGNOSTIC_INFORMATION",
    "SID_DIAGNOSTIC_SESSION_CONTROL",
    "SID_TESTER_PRESENT",
    "SID_ECU_RESET",
    "SID_OBD_CURRENT_DATA",
    "NEGATIVE_RESPONSE_SID",
    "NRC_RESPONSE_PENDING",
    "NRC_BUSY_REPEAT_REQUEST",
    "NRC_CONDITIONS_NOT_CORRECT",
    "SESSION_DEFAULT",
    "SESSION_EXTENDED",
    "OBD_PID_ENCODERS",
    "SimulatedDtc",
    "EcuSimulator",
    "VehicleSimulator",
    "build_freelander2_simulator",
    "build_healthy_simulator",
]

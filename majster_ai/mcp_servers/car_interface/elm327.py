"""UDS over an ELM327-class adapter, including Bluetooth RFCOMM.

This is the Termux path. A phone cannot open a SocketCAN interface without
root, but it can open ``/dev/rfcomm0`` and talk to a Bluetooth OBD dongle, so
for many people this is the only way the project runs at all.

How it works
------------
With ``AT CAF1`` (CAN Auto Formatting) the ELM327 performs ISO-TP segmentation
and reassembly itself: we write a hex UDS payload and read a hex UDS payload
back. What we must get right is the *framing* around that -- the initialisation
sequence, the flow-control setup that multi-frame responses depend on, and the
several shapes an ELM327 response can take.

Response shapes handled by :func:`parse_elm_response`::

    single frame      "62F19053414C"
    multi-frame       "014"            <- total length, hex
                      "0:62F1905341"
                      "1:4C46413242"
    with ATH1         "7E8 62 F1 90 53 41 4C"
    error             "NO DATA", "CAN ERROR", "?", ...

Clone adapters
--------------
Cheap ELM327 clones frequently report v1.5 while implementing a subset of
v1.3, and some mishandle multi-frame reads entirely. The initialisation here
is deliberately conservative and every failure names the likely cause, because
"it does not work" on a 4 EUR dongle is otherwise impossible to debug.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Final, Sequence

from majster_ai.errors import TransportError, TransportNotOpenError
from majster_ai.logging_setup import get_logger, trace_frame
from majster_ai.mcp_servers.car_interface.transport import UdsTransport

log = get_logger("mcp_servers.car_interface.elm327")

#: The ELM327 prompt character that terminates every response.
PROMPT: Final = b">"

#: Responses that mean "this did not work", mapped to an actionable cause.
ELM_ERRORS: Final[dict[str, str]] = {
    "NO DATA": (
        "the ECU did not answer. It may be asleep, absent at this address, or "
        "the ignition may be off."
    ),
    "CAN ERROR": (
        "the adapter could not put the frame on the bus. Check the OBD plug, "
        "the protocol setting, and that the bit rate is 500 kbit/s."
    ),
    "BUS ERROR": "bus fault - check wiring and termination.",
    "BUS BUSY": "the bus was busy; another tester may be connected.",
    "BUS INIT: ERROR": "bus initialisation failed - wrong protocol for this vehicle.",
    "DATA ERROR": "the adapter received a corrupted frame.",
    "UNABLE TO CONNECT": (
        "the adapter cannot establish a link. Wrong protocol, or the vehicle " "is not powered up."
    ),
    "STOPPED": "the operation was interrupted by the adapter.",
    "BUFFER FULL": "the adapter's buffer overflowed - the response was too long.",
    "FB ERROR": "feedback error - wiring fault.",
    "?": (
        "the adapter did not understand the command. Many clone ELM327s "
        "implement only a subset of the AT command set."
    ),
}

#: Lines that carry no data and should simply be skipped.
_NOISE: Final = frozenset({"", "OK", "SEARCHING...", "SEARCHING"})

#: "0:62F190..." -- an ISO-TP segment index prefix.
_SEGMENT_RE: Final = re.compile(r"^([0-9A-F]):\s*([0-9A-F\s]+)$")
#: A bare 3-hex-digit line: the declared total length of a multi-frame reply.
_LENGTH_RE: Final = re.compile(r"^[0-9A-F]{3}$")
#: A leading 11-bit CAN id when ATH1 is in effect.
_HEADER_RE: Final = re.compile(r"^([0-9A-F]{3})\s+(.*)$")


class SerialPortProtocol:
    """The subset of ``pyserial``'s API this transport needs.

    Declared explicitly so tests can inject a fake port and the ELM framing
    logic gets real coverage without a Bluetooth dongle in the room.
    """

    def write(self, data: bytes) -> int: ...  # pragma: no cover
    def read(self, size: int = 1) -> bytes: ...  # pragma: no cover
    def close(self) -> None: ...  # pragma: no cover
    @property
    def is_open(self) -> bool: ...  # pragma: no cover


def _raise_on_adapter_error(lines: Sequence[str]) -> None:
    """Raise if any line is one of the ELM327's error strings.

    Raises:
        TransportError: naming the likely physical cause, because "it does not
            work" on a 4 EUR clone dongle is otherwise impossible to debug.
    """
    for line in lines:
        for marker, explanation in ELM_ERRORS.items():
            if line == marker or line.startswith(marker):
                raise TransportError(f"ELM327 reported '{marker}': {explanation}")


def _collect_payload_hex(lines: Sequence[str], *, expect_header: bool) -> tuple[str, int | None]:
    """Assemble the hex payload from an ELM327 reply's data lines.

    Returns ``(payload_hex, declared_length)``. ``declared_length`` is the
    total byte count the adapter announced for a multi-frame reply, used to
    trim the zero padding at the end.
    """
    declared_length: int | None = None
    segments: dict[int, str] = {}
    flat: list[str] = []

    for line in lines:
        if line in _NOISE:
            continue

        if expect_header:
            header_match = _HEADER_RE.match(line)
            if header_match:
                line = header_match.group(2).strip()
                if not line:
                    continue

        segment_match = _SEGMENT_RE.match(line)
        if segment_match:
            segments[int(segment_match.group(1), 16)] = segment_match.group(2).replace(" ", "")
            continue

        compact = line.replace(" ", "")
        if _LENGTH_RE.match(compact) and not segments and not flat:
            # A bare 3-digit line before any data is the declared total length.
            declared_length = int(compact, 16)
            continue

        flat.append(compact)

    payload_hex = (
        "".join(segments[index] for index in sorted(segments)) if segments else "".join(flat)
    )
    return payload_hex, declared_length


def parse_elm_response(raw: str, *, expect_header: bool = False) -> bytes:
    """Turn an ELM327 reply into the UDS payload bytes.

    Args:
        raw: Everything the adapter sent back, prompt excluded.
        expect_header: True when ``ATH1`` is active and each line is prefixed
            with the responding CAN id.

    Returns:
        The assembled UDS payload.

    Raises:
        TransportError: when the adapter reported an error, or the reply
            cannot be parsed as hex. Never returns partial data silently: a
            half-decoded DTC list is worse than no DTC list.
    """
    text = raw.replace("\r", "\n").replace(PROMPT.decode(), "")
    lines = [line.strip().upper() for line in text.split("\n")]

    _raise_on_adapter_error(lines)
    payload_hex, declared_length = _collect_payload_hex(lines, expect_header=expect_header)

    if not payload_hex:
        raise TransportError(f"ELM327 returned no usable data. Raw reply: {raw!r}")
    if len(payload_hex) % 2:
        raise TransportError(
            f"ELM327 returned an odd number of hex digits ({len(payload_hex)}); "
            f"the reply was probably truncated. Raw: {raw!r}"
        )
    try:
        payload = bytes.fromhex(payload_hex)
    except ValueError as exc:
        raise TransportError(f"Cannot parse ELM327 reply as hex: {raw!r} ({exc})") from exc

    if declared_length is not None and len(payload) > declared_length:
        # Multi-frame replies are zero-padded to the frame boundary.
        payload = payload[:declared_length]
    return payload


class Elm327Transport(UdsTransport):
    """UDS transport over an ELM327-compatible adapter.

    Args:
        port_factory: Zero-argument callable returning an open serial port.
            Injected so tests can supply a fake.
        request_id: Physical request CAN id (``AT SH``).
        response_id: Physical response CAN id (``AT CRA`` filter).
        protocol: ELM protocol number. ``6`` is ISO 15765-4 CAN 11-bit
            500 kbit/s, which is what a Freelander 2 uses.
        headers: Enable ``ATH1``. Off by default: with a CRA filter set we
            already know who is answering, and fewer bytes means fewer clone
            adapter bugs.
        read_timeout: Seconds to wait for the ``>`` prompt.
    """

    def __init__(
        self,
        port_factory: Callable[[], SerialPortProtocol],
        *,
        request_id: int = 0x7E0,
        response_id: int = 0x7E8,
        protocol: int = 6,
        headers: bool = False,
        read_timeout: float = 5.0,
        init_delay: float = 0.1,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._port_factory = port_factory
        self.request_id = request_id
        self.response_id = response_id
        self._protocol = protocol
        self._headers = headers
        self._read_timeout = read_timeout
        self._init_delay = init_delay
        self._sleep = sleep
        self._port: SerialPortProtocol | None = None
        self._buffer: bytes | None = None

    @property
    def is_open(self) -> bool:
        return self._port is not None

    # -- low-level AT plumbing ---------------------------------------------
    def _read_until_prompt(self, timeout: float) -> str:
        """Read until the ``>`` prompt or the deadline."""
        port = self._port
        if port is None:
            raise TransportNotOpenError("ELM327 port is not open")
        deadline = time.monotonic() + max(timeout, 0.0)
        chunks = bytearray()
        while time.monotonic() < deadline:
            byte = port.read(1)
            if not byte:
                continue
            if byte == PROMPT:
                return chunks.decode("ascii", errors="replace")
            chunks.extend(byte)
        raise TransportError(
            f"ELM327 did not return a prompt within {timeout:g}s. "
            f"Partial reply: {bytes(chunks)!r}. Check the RFCOMM link "
            f"(rfcomm bind) and the adapter's baud rate."
        )

    def _command(self, command: str, *, timeout: float | None = None) -> str:
        """Send one AT or hex command and return the raw reply."""
        port = self._port
        if port is None:
            raise TransportNotOpenError("ELM327 port is not open")
        log.debug("ELM327 >> %s", command)
        try:
            port.write(command.encode("ascii") + b"\r")
        except Exception as exc:
            raise TransportError(f"ELM327 write failed: {exc}") from exc
        reply = self._read_until_prompt(self._read_timeout if timeout is None else timeout)
        log.debug("ELM327 << %s", reply.replace("\r", " ").strip())
        return reply

    def _at(self, command: str, *, tolerate_error: bool = False) -> str:
        """Send an AT command and check it was accepted."""
        reply = self._command(command)
        if "?" in reply and not tolerate_error:
            raise TransportError(
                f"ELM327 rejected '{command}'. {ELM_ERRORS['?']} "
                f"Consider a genuine adapter or an OBDLink for UDS work."
            )
        return reply

    # -- lifecycle ----------------------------------------------------------
    def open(self) -> None:
        if self._port is not None:
            return
        try:
            self._port = self._port_factory()
        except Exception as exc:
            raise TransportError(
                f"Cannot open the ELM327 serial port: {exc}. For Bluetooth, bind "
                f"it first, e.g. 'sudo rfcomm bind 0 <MAC> 1', then use "
                f"MAJSTER_CAN_CHANNEL=/dev/rfcomm0."
            ) from exc

        try:
            self._initialise()
        except Exception:
            self.close()
            raise

    def _initialise(self) -> None:
        """Bring the adapter into a known state for UDS work."""
        # ATZ reboots the adapter and is slow; give it its own generous window.
        self._command("ATZ", timeout=max(self._read_timeout, 5.0))
        self._sleep(self._init_delay)

        self._at("ATE0")  # echo off - otherwise every reply repeats the request
        self._at("ATL0")  # no linefeeds
        self._at("ATS0")  # no spaces - smaller, faster replies
        self._at("ATH1" if self._headers else "ATH0")
        self._at("ATCAF1")  # CAN auto formatting: the adapter does ISO-TP
        self._at(f"ATSP{self._protocol}")  # ISO 15765-4, 11-bit, 500 kbit/s
        self._at(f"ATSH{self.request_id:03X}")  # our request id
        self._at(f"ATCRA{self.response_id:03X}", tolerate_error=True)  # rx filter

        # Flow control: required for multi-frame replies. Some clones reject
        # these, in which case CAF1's automatic flow control has to do.
        self._at(f"ATFCSH{self.request_id:03X}", tolerate_error=True)
        self._at("ATFCSD300000", tolerate_error=True)  # BS=0, STmin=0
        self._at("ATFCSM1", tolerate_error=True)  # use the settings above

        log.info(
            "ELM327 ready: tx=0x%03X rx=0x%03X protocol=%d",
            self.request_id,
            self.response_id,
            self._protocol,
        )

    def close(self) -> None:
        port, self._port = self._port, None
        self._buffer = None
        if port is None:
            return
        try:
            port.close()
        except Exception:  # pragma: no cover - best-effort
            log.debug("Ignoring error while closing the ELM327 port", exc_info=True)

    # -- I/O ----------------------------------------------------------------
    def flush(self) -> None:
        self._buffer = None

    def send(self, payload: bytes) -> None:
        if self._port is None:
            raise TransportNotOpenError("Elm327Transport.send() before open()")
        trace_frame("TX", self.request_id, payload)
        # The ELM327 is strictly request/response, so we transmit and capture
        # the reply in one step, then hand it to recv().
        reply = self._command(bytes(payload).hex().upper())
        self._buffer = parse_elm_response(reply, expect_header=self._headers)

    def recv(self, timeout: float) -> bytes | None:
        if self._port is None:
            raise TransportNotOpenError("Elm327Transport.recv() before open()")
        payload, self._buffer = self._buffer, None
        if payload is None:
            return None
        trace_frame("RX", self.response_id, payload)
        return payload

    def describe(self) -> dict[str, Any]:
        details = super().describe()
        details.update({"protocol": f"ELM327 SP{self._protocol}", "headers": self._headers})
        return details


__all__ = [
    "PROMPT",
    "ELM_ERRORS",
    "SerialPortProtocol",
    "parse_elm_response",
    "Elm327Transport",
]

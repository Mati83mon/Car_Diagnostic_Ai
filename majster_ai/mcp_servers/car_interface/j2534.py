"""SAE J2534-1 PassThru support (Tactrix Openport 2.0 and compatibles).

A ``ctypes`` binding to the v04.04 PassThru API, plus a
:class:`~majster_ai.mcp_servers.car_interface.transport.UdsTransport`
implementation on top of it.

Why ISO15765 rather than raw CAN
--------------------------------
The PassThru spec defines an ``ISO15765`` protocol that performs ISO-TP
segmentation, flow control and reassembly *inside the interface*. Using it
means we hand the DLL a complete UDS payload and get a complete one back --
no Python-side timing loop fighting the GIL for 20 ms flow-control windows.
On a Raspberry Pi or a phone running Termux, that difference is the
difference between working and not.

Testability
-----------
The whole ctypes surface is reachable through an injectable library object, so
:class:`J2534Transport` is exercised by the test-suite against a fake PassThru
library. No Tactrix required to prove the framing, the filter setup, or the
error handling.
"""

from __future__ import annotations

import ctypes
import time
from typing import Any, Final

from majster_ai.errors import TransportError, TransportNotOpenError
from majster_ai.logging_setup import get_logger
from majster_ai.logging_setup import trace_frame
from majster_ai.mcp_servers.car_interface.transport import UdsTransport

log = get_logger("mcp_servers.car_interface.j2534")

# --- protocol ids ----------------------------------------------------------
PROTOCOL_CAN: Final = 5
PROTOCOL_ISO15765: Final = 6

# --- connect flags ---------------------------------------------------------
CAN_29BIT_ID: Final = 0x00000100
ISO15765_FRAME_PAD: Final = 0x00000040

# --- filter types ----------------------------------------------------------
PASS_FILTER: Final = 1
BLOCK_FILTER: Final = 2
FLOW_CONTROL_FILTER: Final = 3

# --- ioctl ids -------------------------------------------------------------
IOCTL_SET_CONFIG: Final = 0x02
IOCTL_CLEAR_TX_BUFFER: Final = 0x07
IOCTL_CLEAR_RX_BUFFER: Final = 0x08

# --- RxStatus bits ---------------------------------------------------------
RX_STATUS_TX_MSG_TYPE: Final = 0x00000001
"""Set on the loopback echo of our own transmission -- must be discarded."""
RX_STATUS_START_OF_MESSAGE: Final = 0x00000002
"""ISO15765 first-frame indication carrying no payload -- must be discarded."""

# --- return codes ----------------------------------------------------------
STATUS_NOERROR: Final = 0x00
ERR_BUFFER_EMPTY: Final = 0x10
ERR_TIMEOUT: Final = 0x11

#: Human-readable PassThru error codes, so a failure names itself.
J2534_ERRORS: Final[dict[int, str]] = {
    0x01: "ERR_NOT_SUPPORTED - the device does not support this function",
    0x02: "ERR_INVALID_CHANNEL_ID",
    0x03: "ERR_INVALID_PROTOCOL_ID",
    0x04: "ERR_NULL_PARAMETER",
    0x05: "ERR_INVALID_IOCTL_VALUE",
    0x06: "ERR_INVALID_FLAGS",
    0x07: "ERR_FAILED - an unspecified internal error",
    0x08: "ERR_DEVICE_NOT_CONNECTED - check the USB cable and the OBD plug",
    0x09: "ERR_TIMEOUT",
    0x0A: "ERR_INVALID_MSG",
    0x0B: "ERR_INVALID_TIME_INTERVAL",
    0x0C: "ERR_EXCEEDED_LIMIT - too many filters or periodic messages",
    0x0D: "ERR_INVALID_MSG_ID",
    0x0E: "ERR_DEVICE_IN_USE - another program has the interface open",
    0x0F: "ERR_INVALID_IOCTL_ID",
    0x10: "ERR_BUFFER_EMPTY - no message available",
    0x11: "ERR_BUFFER_FULL",
    0x12: "ERR_BUFFER_OVERFLOW - messages were lost, the bus is faster than we read",
    0x13: "ERR_PIN_INVALID",
    0x14: "ERR_CHANNEL_IN_USE",
    0x15: "ERR_MSG_PROTOCOL_ID",
    0x16: "ERR_INVALID_FILTER_ID",
    0x17: "ERR_NO_FLOW_CONTROL",
    0x18: "ERR_NOT_UNIQUE",
    0x19: "ERR_INVALID_BAUDRATE",
    0x1A: "ERR_INVALID_DEVICE_ID",
}

#: PassThru caps a message at 4128 payload bytes.
MAX_MESSAGE_DATA: Final = 4128


class PassThruMsg(ctypes.Structure):
    """The ``PASSTHRU_MSG`` struct from the J2534-1 v04.04 specification."""

    _fields_ = [
        ("ProtocolID", ctypes.c_ulong),
        ("RxStatus", ctypes.c_ulong),
        ("TxFlags", ctypes.c_ulong),
        ("Timestamp", ctypes.c_ulong),
        ("DataSize", ctypes.c_ulong),
        ("ExtraDataIndex", ctypes.c_ulong),
        ("Data", ctypes.c_ubyte * MAX_MESSAGE_DATA),
    ]

    def set_data(self, payload: bytes) -> None:
        if len(payload) > MAX_MESSAGE_DATA:
            raise TransportError(
                f"J2534 message too long: {len(payload)} bytes (max {MAX_MESSAGE_DATA})"
            )
        self.DataSize = len(payload)
        for index, byte in enumerate(payload):
            self.Data[index] = byte

    def get_data(self) -> bytes:
        size = min(int(self.DataSize), MAX_MESSAGE_DATA)
        return bytes(self.Data[:size])


def describe_error(code: int) -> str:
    """Turn a PassThru return code into something a human can act on."""
    return J2534_ERRORS.get(code, f"unknown PassThru error 0x{code:02X}")


def load_passthru_library(path: str) -> Any:
    """Load a PassThru shared library with ctypes.

    Raises:
        TransportError: with an actionable message when the library will not
            load -- by far the most common J2534 setup problem.
    """
    try:
        return ctypes.cdll.LoadLibrary(path)
    except OSError as exc:
        raise TransportError(
            f"Cannot load the J2534 PassThru library at {path!r}: {exc}. "
            f"Check MAJSTER_J2534_LIBRARY. For a Tactrix Openport 2.0 this is "
            f"'op20pt32.dll' on Windows or the matching .so on Linux, and the "
            f"library's word size (32/64-bit) must match your Python."
        ) from exc


class J2534Transport(UdsTransport):
    """UDS over ISO15765 through a J2534 PassThru interface.

    Args:
        library: Path to the PassThru shared library, or a preloaded library
            object (used by tests to inject a fake).
        request_id: Physical request CAN id.
        response_id: Physical response CAN id.
        bitrate: CAN bit rate.
        extended_addressing: Use 29-bit identifiers.
        device_name: Optional device selector passed to ``PassThruOpen``.
    """

    def __init__(
        self,
        library: str | Any,
        *,
        request_id: int = 0x7E0,
        response_id: int = 0x7E8,
        bitrate: int = 500_000,
        extended_addressing: bool = False,
        device_name: str | None = None,
    ) -> None:
        self._library_path = library if isinstance(library, str) else None
        self._lib: Any | None = None if isinstance(library, str) else library
        self.request_id = request_id
        self.response_id = response_id
        self._bitrate = bitrate
        self._extended = extended_addressing
        self._device_name = device_name
        self._device_id = ctypes.c_ulong(0)
        self._channel_id = ctypes.c_ulong(0)
        self._filter_id = ctypes.c_ulong(0)
        self._open = False

    # -- helpers ------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._open

    def _check(self, code: int, operation: str) -> None:
        """Raise a descriptive error unless the call succeeded."""
        if code == STATUS_NOERROR:
            return
        detail = describe_error(code)
        extra = self._last_error_text()
        suffix = f" ({extra})" if extra else ""
        raise TransportError(f"J2534 {operation} failed: {detail}{suffix}")

    def _last_error_text(self) -> str:
        """Ask the library for its own description of the last failure."""
        if self._lib is None or not hasattr(self._lib, "PassThruGetLastError"):
            return ""
        try:
            buffer = ctypes.create_string_buffer(80)
            self._lib.PassThruGetLastError(buffer)
            return buffer.value.decode("ascii", errors="replace").strip()
        except Exception:  # pragma: no cover - a broken DLL must not mask the real error
            return ""

    def _can_id_bytes(self, can_id: int) -> bytes:
        """ISO15765 messages carry the 4-byte big-endian CAN id up front."""
        return can_id.to_bytes(4, "big")

    def _new_message(self, payload: bytes = b"", *, tx_flags: int | None = None) -> PassThruMsg:
        message = PassThruMsg()
        message.ProtocolID = PROTOCOL_ISO15765
        if tx_flags is None:
            tx_flags = ISO15765_FRAME_PAD | (CAN_29BIT_ID if self._extended else 0)
        message.TxFlags = tx_flags
        message.set_data(payload)
        return message

    # -- lifecycle ----------------------------------------------------------
    def open(self) -> None:
        if self._open:
            return
        if self._lib is None:
            if not self._library_path:
                raise TransportError(
                    "No J2534 library configured. Set MAJSTER_J2534_LIBRARY to the "
                    "PassThru shared library path."
                )
            self._lib = load_passthru_library(self._library_path)

        name = ctypes.c_char_p(self._device_name.encode("ascii")) if self._device_name else None
        self._check(self._lib.PassThruOpen(name, ctypes.byref(self._device_id)), "PassThruOpen")

        flags = CAN_29BIT_ID if self._extended else 0
        self._check(
            self._lib.PassThruConnect(
                self._device_id,
                PROTOCOL_ISO15765,
                flags,
                self._bitrate,
                ctypes.byref(self._channel_id),
            ),
            "PassThruConnect",
        )

        try:
            self._install_flow_control_filter()
        except TransportError:
            # Never leave a half-open device behind: the next run would get
            # ERR_DEVICE_IN_USE and the user would have to unplug the cable.
            self._teardown()
            raise

        self._open = True
        log.info(
            "J2534 channel up: tx=0x%03X rx=0x%03X @ %d bit/s (%s-bit ids)",
            self.request_id,
            self.response_id,
            self._bitrate,
            29 if self._extended else 11,
        )

    def _install_flow_control_filter(self) -> None:
        """Tell the interface which ids form this ECU's ISO-TP conversation.

        Without a flow-control filter an ISO15765 channel silently discards
        every multi-frame response -- the classic "it works for short reads and
        not for DTCs" J2534 symptom.
        """
        mask = self._new_message(self._can_id_bytes(0xFFFFFFFF if self._extended else 0x7FF))
        pattern = self._new_message(self._can_id_bytes(self.response_id))
        flow_control = self._new_message(self._can_id_bytes(self.request_id))
        self._check(
            self._lib.PassThruStartMsgFilter(
                self._channel_id,
                FLOW_CONTROL_FILTER,
                ctypes.byref(mask),
                ctypes.byref(pattern),
                ctypes.byref(flow_control),
                ctypes.byref(self._filter_id),
            ),
            "PassThruStartMsgFilter",
        )

    def _teardown(self) -> None:
        lib = self._lib
        if lib is None:
            return
        for call, args, label in (
            (
                getattr(lib, "PassThruStopMsgFilter", None),
                (self._channel_id, self._filter_id),
                "PassThruStopMsgFilter",
            ),
            (getattr(lib, "PassThruDisconnect", None), (self._channel_id,), "PassThruDisconnect"),
            (getattr(lib, "PassThruClose", None), (self._device_id,), "PassThruClose"),
        ):
            if call is None:
                continue
            try:
                call(*args)
            except Exception:  # pragma: no cover - best-effort cleanup
                log.debug("Ignoring error during J2534 %s", label, exc_info=True)

    def close(self) -> None:
        if not self._open:
            return
        self._open = False
        self._teardown()
        log.info("J2534 channel closed")

    # -- I/O ----------------------------------------------------------------
    def flush(self) -> None:
        if not self._open or self._lib is None:
            return
        ioctl = getattr(self._lib, "PassThruIoctl", None)
        if ioctl is None:  # pragma: no cover - non-conforming library
            return
        try:
            ioctl(self._channel_id, IOCTL_CLEAR_RX_BUFFER, None, None)
        except Exception:  # pragma: no cover - best-effort
            log.debug("Ignoring error clearing the J2534 Rx buffer", exc_info=True)

    def send(self, payload: bytes) -> None:
        if not self._open or self._lib is None:
            raise TransportNotOpenError("J2534Transport.send() before open()")
        trace_frame("TX", self.request_id, payload)
        message = self._new_message(self._can_id_bytes(self.request_id) + bytes(payload))
        count = ctypes.c_ulong(1)
        self._check(
            self._lib.PassThruWriteMsgs(
                self._channel_id, ctypes.byref(message), ctypes.byref(count), 1000
            ),
            "PassThruWriteMsgs",
        )

    def recv(self, timeout: float) -> bytes | None:
        if not self._open or self._lib is None:
            raise TransportNotOpenError("J2534Transport.recv() before open()")

        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            remaining_ms = max(int((deadline - time.monotonic()) * 1000), 1)
            message = PassThruMsg()
            message.ProtocolID = PROTOCOL_ISO15765
            count = ctypes.c_ulong(1)
            code = self._lib.PassThruReadMsgs(
                self._channel_id, ctypes.byref(message), ctypes.byref(count), remaining_ms
            )

            if code in (ERR_BUFFER_EMPTY, ERR_TIMEOUT):
                if time.monotonic() >= deadline:
                    return None
                continue
            self._check(code, "PassThruReadMsgs")

            if int(count.value) == 0:
                if time.monotonic() >= deadline:
                    return None
                continue

            status = int(message.RxStatus)
            # Our own transmission echoed back, or a first-frame indication
            # with no payload. Both are normal; neither is the answer.
            if status & RX_STATUS_TX_MSG_TYPE or status & RX_STATUS_START_OF_MESSAGE:
                if time.monotonic() >= deadline:
                    return None
                continue

            data = message.get_data()
            if len(data) <= 4:
                # Just the CAN id, no UDS payload.
                if time.monotonic() >= deadline:
                    return None
                continue

            can_id = int.from_bytes(data[:4], "big")
            payload = data[4:]
            if can_id != self.response_id:
                log.debug(
                    "J2534: ignoring frame from 0x%03X (want 0x%03X)", can_id, self.response_id
                )
                if time.monotonic() >= deadline:
                    return None
                continue

            trace_frame("RX", can_id, payload)
            return payload

    def describe(self) -> dict[str, Any]:
        details = super().describe()
        details.update(
            {
                "library": self._library_path or "<injected>",
                "bitrate": self._bitrate,
                "protocol": "ISO15765",
                "addressing": "29-bit" if self._extended else "11-bit",
            }
        )
        return details


__all__ = [
    "PROTOCOL_CAN",
    "PROTOCOL_ISO15765",
    "CAN_29BIT_ID",
    "ISO15765_FRAME_PAD",
    "FLOW_CONTROL_FILTER",
    "RX_STATUS_TX_MSG_TYPE",
    "RX_STATUS_START_OF_MESSAGE",
    "STATUS_NOERROR",
    "ERR_BUFFER_EMPTY",
    "ERR_TIMEOUT",
    "J2534_ERRORS",
    "PassThruMsg",
    "describe_error",
    "load_passthru_library",
    "J2534Transport",
]

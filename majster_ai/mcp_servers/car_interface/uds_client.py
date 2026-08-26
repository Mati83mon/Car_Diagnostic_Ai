"""UDS request/response handling with the error tolerance a real bus demands.

This is where "CAN bus communications are flaky" is turned into code. A single
call to :meth:`UdsSession.request` transparently handles:

* **Timeouts** -- the ECU is asleep, absent, or on another bus. Retried with
  exponential backoff, then reported as a clean
  :class:`~majster_ai.errors.UdsTimeoutError`.
* **NRC 0x78 requestCorrectlyReceivedResponsePending** -- the ECU needs more
  time (routine for DTC reads on a busy module). We keep waiting on the
  extended P2* timeout instead of treating it as a failure.
* **NRC 0x21 busyRepeatRequest** -- the ECU is busy; retry after a backoff.
* **Stale frames** -- a late reply to a *previous* request. Detected by
  comparing the response SID and discarded rather than mis-parsed.
* **Every other negative response** -- reported as a
  :class:`~majster_ai.errors.UdsNegativeResponse` carrying the decoded NRC
  name, so the agent can say "security access denied" instead of "0x33".

``udsoncan`` supplies the authoritative service ids and NRC name table.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Final, Iterable

from udsoncan import Response, services

from majster_ai.errors import (
    UdsNegativeResponse,
    UdsProtocolError,
    UdsTimeoutError,
)
from majster_ai.logging_setup import get_logger
from majster_ai.mcp_servers.car_interface.dtc import Dtc, decode_dtc_records
from majster_ai.mcp_servers.car_interface.transport import UdsTransport

log = get_logger("mcp_servers.car_interface.uds")

NEGATIVE_RESPONSE_SID: Final = 0x7F
POSITIVE_RESPONSE_OFFSET: Final = 0x40

#: Service ids, taken from udsoncan so they cannot drift from the standard.
SID_DIAGNOSTIC_SESSION_CONTROL: Final[int] = services.DiagnosticSessionControl.request_id()
SID_ECU_RESET: Final[int] = services.ECUReset.request_id()
SID_CLEAR_DIAGNOSTIC_INFORMATION: Final[int] = services.ClearDiagnosticInformation.request_id()
SID_READ_DTC_INFORMATION: Final[int] = services.ReadDTCInformation.request_id()
SID_READ_DATA_BY_IDENTIFIER: Final[int] = services.ReadDataByIdentifier.request_id()
SID_TESTER_PRESENT: Final[int] = services.TesterPresent.request_id()
SID_OBD_CURRENT_DATA: Final = 0x01

#: NRC 0x78 -- "I heard you, I need longer". Not an error.
NRC_RESPONSE_PENDING: Final[int] = Response.Code.RequestCorrectlyReceived_ResponsePending
#: NRC 0x21 -- "I am busy, ask again". Worth a retry.
NRC_BUSY_REPEAT_REQUEST: Final[int] = Response.Code.BusyRepeatRequest

#: Negative responses that a retry might plausibly resolve. Everything else is
#: a definitive answer -- retrying "security access denied" just wastes time and
#: can trip an ECU's anti-scan lockout.
RETRYABLE_NRCS: Final[frozenset[int]] = frozenset({NRC_BUSY_REPEAT_REQUEST})

#: Diagnostic session ids.
SESSION_DEFAULT: Final = 0x01
SESSION_PROGRAMMING: Final = 0x02
SESSION_EXTENDED: Final = 0x03

#: Clear-all group, per ISO 14229-1.
CLEAR_ALL_DTC_GROUP: Final = 0xFFFFFF


def nrc_name(code: int) -> str:
    """Human-readable name for a negative response code.

    ``udsoncan`` returns the code's decimal digits for values it does not
    recognise, so "238" would reach the operator instead of something they can
    act on. Anything non-alphabetic is replaced with an explicit unknown-NRC
    label carrying the hex value.
    """
    try:
        name = str(Response.Code.get_name(code) or "")
    except Exception:  # pragma: no cover - defensive, udsoncan is well-behaved
        name = ""
    if not name or not any(character.isalpha() for character in name):
        return f"UnknownNRC_0x{code:02X}"
    return name


class UdsSession:
    """A UDS conversation with one ECU over a :class:`UdsTransport`.

    Args:
        transport: The byte channel to the module.
        timeout: P2 client timeout for a first response, in seconds.
        extended_timeout: P2* timeout applied after NRC 0x78.
        retries: Extra attempts after a timeout or a retryable NRC.
        backoff: Base seconds for exponential backoff (0.25 -> 0.25, 0.5, 1.0).
        max_response_pending: Consecutive NRC 0x78 frames tolerated. Prevents
            an ECU stuck in a pending loop from hanging the agent forever.
        name: Module name, used in log messages.
        sleep: Injectable sleep, so tests exercise the backoff logic instantly.
    """

    def __init__(
        self,
        transport: UdsTransport,
        *,
        timeout: float = 2.0,
        extended_timeout: float = 5.0,
        retries: int = 2,
        backoff: float = 0.25,
        max_response_pending: int = 10,
        name: str = "ECU",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport = transport
        self.timeout = timeout
        self.extended_timeout = extended_timeout
        self.retries = max(0, retries)
        self.backoff = max(0.0, backoff)
        self.max_response_pending = max(0, max_response_pending)
        self.name = name
        self._sleep = sleep
        self._session = SESSION_DEFAULT

    # -- lifecycle ----------------------------------------------------------
    def open(self) -> None:
        self.transport.open()

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> UdsSession:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def current_session(self) -> int:
        return self._session

    # -- the core request loop ---------------------------------------------
    def request(
        self,
        payload: bytes,
        *,
        expect_response: bool = True,
        timeout: float | None = None,
    ) -> bytes:
        """Send a UDS request and return the positive response payload.

        Args:
            payload: Full request, starting with the service id.
            expect_response: False for suppressed-response requests (e.g.
                TesterPresent with bit 7 set), where silence is success.
            timeout: Override the P2 timeout for this request only.

        Returns:
            The complete positive response payload, including the response SID.
            An empty ``bytes`` when ``expect_response`` is False.

        Raises:
            UdsTimeoutError: no answer after every retry.
            UdsNegativeResponse: the ECU refused, with a decoded NRC.
            UdsProtocolError: the answer violates UDS encoding.
        """
        if not payload:
            raise UdsProtocolError("Cannot send an empty UDS request")

        request_sid = payload[0]
        expected_sid = request_sid + POSITIVE_RESPONSE_OFFSET
        first_timeout = self.timeout if timeout is None else timeout
        attempts = self.retries + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            # Drop anything left over from a previous exchange: a late reply to
            # an earlier request must never be read as this request's answer.
            self.transport.flush()
            self.transport.send(payload)

            if not expect_response:
                return b""

            try:
                response = self._await_response(
                    expected_sid=expected_sid,
                    request_sid=request_sid,
                    timeout=first_timeout,
                )
            except UdsNegativeResponse as exc:
                if exc.nrc not in RETRYABLE_NRCS or attempt == attempts:
                    raise
                last_error = exc
                log.warning(
                    "%s: %s (NRC 0x%02X) on attempt %d/%d - retrying",
                    self.name,
                    exc.nrc_name,
                    exc.nrc,
                    attempt,
                    attempts,
                )
            else:
                if response is not None:
                    if attempt > 1:
                        log.info("%s: recovered on attempt %d/%d", self.name, attempt, attempts)
                    return response
                last_error = UdsTimeoutError(
                    f"{self.name} did not respond to service 0x{request_sid:02X} "
                    f"within {first_timeout:g}s",
                    module=self.name,
                    service=f"0x{request_sid:02X}",
                    timeout=first_timeout,
                )
                log.warning(
                    "%s: timeout on service 0x%02X, attempt %d/%d",
                    self.name,
                    request_sid,
                    attempt,
                    attempts,
                )

            if attempt < attempts and self.backoff:
                delay = self.backoff * (2 ** (attempt - 1))
                log.debug("%s: backing off %.3fs before retry", self.name, delay)
                self._sleep(delay)

        assert last_error is not None  # the loop always sets it before exiting
        if isinstance(last_error, UdsTimeoutError):
            raise UdsTimeoutError(
                f"{self.name} did not respond to service 0x{request_sid:02X} after "
                f"{attempts} attempt(s). The module may be asleep, absent, on a "
                f"different bus, or the ignition may be off.",
                module=self.name,
                service=f"0x{request_sid:02X}",
                attempts=attempts,
                timeout=first_timeout,
            )
        raise last_error

    def _await_response(
        self, *, expected_sid: int, request_sid: int, timeout: float
    ) -> bytes | None:
        """Wait for this request's answer, absorbing 0x78 and stale frames.

        Returns ``None`` on timeout so the caller can decide whether to retry.
        """
        pending_seen = 0
        current_timeout = timeout

        while True:
            response = self.transport.recv(current_timeout)
            if response is None:
                return None
            if not response:
                raise UdsProtocolError(f"{self.name} returned an empty response payload")

            if response[0] == NEGATIVE_RESPONSE_SID:
                if len(response) < 3:
                    raise UdsProtocolError(
                        f"{self.name}: malformed negative response "
                        f"{response.hex().upper()} (expected 3 bytes)"
                    )
                echoed_sid, nrc = response[1], response[2]
                if echoed_sid != request_sid:
                    # A negative response to somebody else's request.
                    log.debug(
                        "%s: ignoring stale negative response for service 0x%02X",
                        self.name,
                        echoed_sid,
                    )
                    continue

                if nrc == NRC_RESPONSE_PENDING:
                    pending_seen += 1
                    if pending_seen > self.max_response_pending:
                        raise UdsTimeoutError(
                            f"{self.name} sent {pending_seen} consecutive "
                            f"'response pending' frames for service "
                            f"0x{request_sid:02X} without ever answering.",
                            module=self.name,
                            service=f"0x{request_sid:02X}",
                            response_pending_count=pending_seen,
                        )
                    log.debug(
                        "%s: response pending (%d/%d), extending timeout to %gs",
                        self.name,
                        pending_seen,
                        self.max_response_pending,
                        self.extended_timeout,
                    )
                    current_timeout = self.extended_timeout
                    continue

                raise UdsNegativeResponse(
                    f"{self.name} rejected service 0x{request_sid:02X}: "
                    f"{nrc_name(nrc)} (NRC 0x{nrc:02X})",
                    service=request_sid,
                    nrc=nrc,
                    nrc_name=nrc_name(nrc),
                    module=self.name,
                )

            if response[0] != expected_sid:
                # Positive response to a different service: stale traffic.
                log.debug(
                    "%s: ignoring stale response 0x%02X (waiting for 0x%02X)",
                    self.name,
                    response[0],
                    expected_sid,
                )
                continue

            return response

    # -- convenience wrappers ----------------------------------------------
    def start_session(self, session: int = SESSION_EXTENDED) -> bytes:
        """Switch diagnostic session (0x10). Read-only: no vehicle state changes."""
        response = self.request(bytes((SID_DIAGNOSTIC_SESSION_CONTROL, session)))
        self._session = session
        log.info("%s: entered diagnostic session 0x%02X", self.name, session)
        return response

    def tester_present(self, *, suppress_response: bool = False) -> bytes:
        """Keep-alive (0x3E). Harmless, and the standard ECU presence probe."""
        subfunction = 0x80 if suppress_response else 0x00
        return self.request(
            bytes((SID_TESTER_PRESENT, subfunction)),
            expect_response=not suppress_response,
        )

    def read_dtc_by_status_mask(self, mask: int = 0xFF) -> list[Dtc]:
        """ReadDTCInformation 0x19 subfunction 0x02.

        Raises:
            UdsProtocolError: if the response is too short to contain the
                mandatory status-availability mask byte.
        """
        response = self.request(bytes((SID_READ_DTC_INFORMATION, 0x02, mask & 0xFF)))
        if len(response) < 3:
            raise UdsProtocolError(
                f"{self.name}: ReadDTCInformation response too short: " f"{response.hex().upper()}"
            )
        # response = [0x59, 0x02, statusAvailabilityMask, <4-byte records...>]
        return decode_dtc_records(response[3:])

    def read_dtc_count(self, mask: int = 0xFF) -> int:
        """ReadDTCInformation 0x19 subfunction 0x01 -- just the count."""
        response = self.request(bytes((SID_READ_DTC_INFORMATION, 0x01, mask & 0xFF)))
        if len(response) < 6:
            raise UdsProtocolError(
                f"{self.name}: DTC count response too short: {response.hex().upper()}"
            )
        return (response[4] << 8) | response[5]

    def clear_diagnostic_information(self, group: int = CLEAR_ALL_DTC_GROUP) -> bytes:
        """ClearDiagnosticInformation 0x14.

        **This is a write operation.** Nothing in this class enforces the
        safety policy -- that is the job of the service layer above, which is
        the single choke point every write must pass through.
        """
        if not 0 <= group <= 0xFFFFFF:
            raise UdsProtocolError(f"DTC group out of range: 0x{group:X}")
        payload = bytes(
            (
                SID_CLEAR_DIAGNOSTIC_INFORMATION,
                (group >> 16) & 0xFF,
                (group >> 8) & 0xFF,
                group & 0xFF,
            )
        )
        log.warning("%s: sending ClearDiagnosticInformation (group 0x%06X)", self.name, group)
        return self.request(payload)

    def read_data_by_identifier(self, did: int) -> bytes:
        """ReadDataByIdentifier 0x22. Returns the payload after the DID echo.

        Raises:
            UdsProtocolError: if the ECU echoes a different DID -- which would
                otherwise silently attribute one signal's value to another.
        """
        if not 0 <= did <= 0xFFFF:
            raise UdsProtocolError(f"DID out of range: 0x{did:X}")
        response = self.request(bytes((SID_READ_DATA_BY_IDENTIFIER, (did >> 8) & 0xFF, did & 0xFF)))
        if len(response) < 3:
            raise UdsProtocolError(
                f"{self.name}: ReadDataByIdentifier response too short: "
                f"{response.hex().upper()}"
            )
        echoed = (response[1] << 8) | response[2]
        if echoed != did:
            raise UdsProtocolError(
                f"{self.name}: asked for DID 0x{did:04X} but the ECU echoed "
                f"0x{echoed:04X} - refusing to attribute this data to the wrong signal."
            )
        return response[3:]

    def read_obd_pid(self, pid: int) -> bytes:
        """OBD-II service 0x01. Returns the payload after the PID echo.

        Raises:
            UdsProtocolError: if the ECU echoes a different PID.
        """
        if not 0 <= pid <= 0xFF:
            raise UdsProtocolError(f"OBD PID out of range: 0x{pid:X}")
        response = self.request(bytes((SID_OBD_CURRENT_DATA, pid)))
        if len(response) < 2:
            raise UdsProtocolError(f"{self.name}: OBD response too short: {response.hex().upper()}")
        if response[1] != pid:
            raise UdsProtocolError(
                f"{self.name}: asked for PID 0x{pid:02X} but the ECU echoed "
                f"0x{response[1]:02X} - refusing to attribute this data to the "
                f"wrong signal."
            )
        return response[2:]

    def probe(self, *, timeout: float | None = None) -> bool:
        """Is anything answering at this address?

        Uses TesterPresent, the standard harmless presence probe. A negative
        response still proves presence: something is there and it understood
        enough to refuse.
        """
        try:
            self.request(bytes((SID_TESTER_PRESENT, 0x00)), timeout=timeout)
            return True
        except UdsNegativeResponse:
            return True
        except (UdsTimeoutError, UdsProtocolError):
            return False

    def describe(self) -> dict[str, Any]:
        return {
            "module": self.name,
            "session": f"0x{self._session:02X}",
            "timeout": self.timeout,
            "extended_timeout": self.extended_timeout,
            "retries": self.retries,
            "transport": self.transport.describe(),
        }


def iter_service_names(payload: bytes | Iterable[int]) -> str:
    """Best-effort service name for a request payload -- used in frame traces."""
    data = bytes(payload)
    if not data:
        return "<empty>"
    sid = data[0]
    for service in services.BaseService.__subclasses__():
        try:
            if service.request_id() == sid:
                return service.__name__
        except Exception:  # pragma: no cover - some udsoncan classes are abstract
            continue
    if sid == SID_OBD_CURRENT_DATA:
        return "OBD_CurrentData"
    return f"Service_0x{sid:02X}"


__all__ = [
    "UdsSession",
    "nrc_name",
    "iter_service_names",
    "SESSION_DEFAULT",
    "SESSION_EXTENDED",
    "SESSION_PROGRAMMING",
    "CLEAR_ALL_DTC_GROUP",
    "RETRYABLE_NRCS",
    "NRC_RESPONSE_PENDING",
    "NRC_BUSY_REPEAT_REQUEST",
    "SID_READ_DTC_INFORMATION",
    "SID_READ_DATA_BY_IDENTIFIER",
    "SID_CLEAR_DIAGNOSTIC_INFORMATION",
    "SID_DIAGNOSTIC_SESSION_CONTROL",
    "SID_TESTER_PRESENT",
    "SID_OBD_CURRENT_DATA",
]

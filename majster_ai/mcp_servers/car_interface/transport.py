"""Transport abstraction between the UDS client and the physical bus.

The UDS layer above only ever needs two operations -- put a request on the
wire, take a response off it -- so that is the whole interface:

.. code-block:: python

    transport.send(b"\\x22\\xF1\\x90")
    response = transport.recv(timeout=2.0)

Keeping ``send`` and ``recv`` separate (rather than a single blocking
``request``) is what lets :class:`~majster_ai.mcp_servers.car_interface.uds_client.UdsSession`
implement NRC 0x78 "response pending" correctly: one request may legitimately
produce several response frames, and only the last one is the real answer.

Two implementations ship here:

* :class:`SimulatedTransport` -- an in-process ECU, used by the ``virtual``
  backend and by the entire test-suite. No hardware, no kernel modules.
* :class:`IsoTpCanTransport` -- real traffic over ``python-can`` + ISO-TP.
"""

from __future__ import annotations

import abc
import logging
import queue
import time
from typing import Any, TYPE_CHECKING

from majster_ai.errors import TransportError, TransportNotOpenError
from majster_ai.logging_setup import CAN_LOGGER_NAME, trace_frame

if TYPE_CHECKING:  # pragma: no cover - typing only
    from majster_ai.mcp_servers.car_interface.simulator import EcuSimulator

_can_log = logging.getLogger(CAN_LOGGER_NAME)


class UdsTransport(abc.ABC):
    """A bidirectional UDS payload channel to one ECU."""

    #: Physical request id (tester -> ECU), for logging.
    request_id: int = 0x7E0
    #: Physical response id (ECU -> tester), for logging.
    response_id: int = 0x7E8

    @abc.abstractmethod
    def open(self) -> None:
        """Acquire the underlying hardware/resources. Must be idempotent."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release the underlying resources. Must be safe to call twice."""

    @property
    @abc.abstractmethod
    def is_open(self) -> bool:
        """True when :meth:`send` and :meth:`recv` may be called."""

    @abc.abstractmethod
    def send(self, payload: bytes) -> None:
        """Transmit one complete UDS request payload (SID + data)."""

    @abc.abstractmethod
    def recv(self, timeout: float) -> bytes | None:
        """Return the next complete UDS response payload, or ``None`` on timeout.

        ``None`` -- rather than an exception -- because a timeout is an
        expected, routine event in UDS: it is how you discover that a module
        is asleep or absent. The retry policy lives one layer up.
        """

    def flush(self) -> None:
        """Discard any buffered responses.

        Called before each request so a late reply to a *previous* request
        cannot be mistaken for the answer to this one -- a genuine and
        confusing failure mode on a busy bus.
        """

    def describe(self) -> dict[str, Any]:
        """Human-readable transport details for diagnostics output."""
        return {
            "type": type(self).__name__,
            "request_id": f"0x{self.request_id:03X}",
            "response_id": f"0x{self.response_id:03X}",
            "open": self.is_open,
        }

    def __enter__(self) -> UdsTransport:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class SimulatedTransport(UdsTransport):
    """In-process transport backed by an :class:`EcuSimulator`.

    This is what makes the whole project testable and safe by default: the
    agent, the MCP server, the retry logic and the HITL gate all run against a
    real UDS byte stream, just one produced by Python instead of by a car.
    """

    def __init__(
        self,
        simulator: EcuSimulator,
        *,
        request_id: int = 0x7E0,
        response_id: int = 0x7E8,
    ) -> None:
        self._simulator = simulator
        self.request_id = request_id
        self.response_id = response_id
        self._open = False
        self._queue: queue.SimpleQueue[bytes] = queue.SimpleQueue()

    @property
    def simulator(self) -> EcuSimulator:
        return self._simulator

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False
        self.flush()

    @property
    def is_open(self) -> bool:
        return self._open

    def flush(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def send(self, payload: bytes) -> None:
        if not self._open:
            raise TransportNotOpenError("SimulatedTransport.send() before open()")
        trace_frame("TX", self.request_id, payload, logger=_can_log)
        for response in self._simulator.handle_request(bytes(payload)):
            self._queue.put(response)

    def recv(self, timeout: float) -> bytes | None:
        if not self._open:
            raise TransportNotOpenError("SimulatedTransport.recv() before open()")
        try:
            # The simulator answers synchronously, so anything queued is already
            # here; the timeout only matters for the deliberate no-reply case.
            payload = self._queue.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None
        trace_frame("RX", self.response_id, payload, logger=_can_log)
        return payload


class IsoTpCanTransport(UdsTransport):
    """UDS over ISO-TP over CAN, using ``python-can`` and ``can-isotp``.

    Imports are deferred to :meth:`open` so that the ``virtual`` backend -- and
    therefore CI -- never needs ``python-can`` installed.

    Args:
        bus_factory: Zero-argument callable returning a configured
            ``can.BusABC``. Supplied by :mod:`.backends`, which owns the
            per-backend configuration.
        request_id: Physical request CAN id.
        response_id: Physical response CAN id.
        extended_addressing: Use 29-bit identifiers.
        close_bus: Whether shutting the transport also shuts the bus. False
            when the bus is shared between several module transports.
    """

    def __init__(
        self,
        bus_factory: Any,
        *,
        request_id: int = 0x7E0,
        response_id: int = 0x7E8,
        extended_addressing: bool = False,
        close_bus: bool = True,
        stmin: int = 0,
        blocksize: int = 8,
    ) -> None:
        self._bus_factory = bus_factory
        self.request_id = request_id
        self.response_id = response_id
        self._extended = extended_addressing
        self._close_bus = close_bus
        self._stmin = stmin
        self._blocksize = blocksize
        self._bus: Any | None = None
        self._stack: Any | None = None

    @property
    def is_open(self) -> bool:
        return self._stack is not None

    def open(self) -> None:
        if self._stack is not None:
            return
        try:
            import isotp
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise TransportError(
                "can-isotp is required for hardware backends. "
                "Install it with: pip install 'car-diagnostic-ai[car]'"
            ) from exc

        try:
            bus = self._bus_factory()
        except Exception as exc:
            raise TransportError(f"Cannot open CAN interface: {exc}") from exc

        try:
            address = isotp.Address(
                (
                    isotp.AddressingMode.Normal_29bits
                    if self._extended
                    else isotp.AddressingMode.Normal_11bits
                ),
                txid=self.request_id,
                rxid=self.response_id,
            )
            stack = isotp.NotifierBasedCanStack(
                bus=bus,
                notifier=self._make_notifier(bus),
                address=address,
                params={
                    "stmin": self._stmin,
                    "blocksize": self._blocksize,
                    "tx_padding": 0x00,
                    "rx_flowcontrol_timeout": 1000,
                    "rx_consecutive_frame_timeout": 1000,
                    # Wrong-frame handling: on a busy bus we would rather see a
                    # timeout than silently accept a mis-assembled payload.
                    "wftmax": 4,
                },
            )
            stack.start()
        except AttributeError:
            # Older can-isotp exposes CanStack rather than NotifierBasedCanStack.
            stack = isotp.CanStack(
                bus=bus,
                address=isotp.Address(
                    (
                        isotp.AddressingMode.Normal_29bits
                        if self._extended
                        else isotp.AddressingMode.Normal_11bits
                    ),
                    txid=self.request_id,
                    rxid=self.response_id,
                ),
                params={"stmin": self._stmin, "blocksize": self._blocksize, "tx_padding": 0x00},
            )
            if hasattr(stack, "start"):
                stack.start()
        except Exception as exc:
            self._shutdown_bus(bus)
            raise TransportError(f"Cannot start the ISO-TP stack: {exc}") from exc

        self._bus = bus
        self._stack = stack
        _can_log.info(
            "ISO-TP stack up: tx=0x%03X rx=0x%03X (%s-bit)",
            self.request_id,
            self.response_id,
            29 if self._extended else 11,
        )

    @staticmethod
    def _make_notifier(bus: Any) -> Any:  # pragma: no cover - hardware path
        import can

        return can.Notifier(bus, listeners=[], timeout=0.1)

    def _shutdown_bus(self, bus: Any) -> None:  # pragma: no cover - hardware path
        if bus is not None and self._close_bus:
            try:
                bus.shutdown()
            except Exception:
                _can_log.debug("Ignoring error while shutting down the CAN bus", exc_info=True)

    def close(self) -> None:
        stack, self._stack = self._stack, None
        bus, self._bus = self._bus, None
        if stack is not None:
            try:
                stack.stop()
            except Exception:  # pragma: no cover - hardware path
                _can_log.debug("Ignoring error while stopping the ISO-TP stack", exc_info=True)
        self._shutdown_bus(bus)

    def flush(self) -> None:  # pragma: no cover - hardware path
        stack = self._stack
        if stack is None:
            return
        while True:
            try:
                if stack.available():
                    stale = stack.recv()
                    _can_log.debug(
                        "Discarding stale ISO-TP payload: %s", bytes(stale).hex().upper()
                    )
                else:
                    return
            except Exception:
                return

    def send(self, payload: bytes) -> None:  # pragma: no cover - hardware path
        if self._stack is None:
            raise TransportNotOpenError("IsoTpCanTransport.send() before open()")
        trace_frame("TX", self.request_id, payload, logger=_can_log)
        try:
            self._stack.send(bytes(payload))
        except Exception as exc:
            raise TransportError(f"ISO-TP transmit failed: {exc}") from exc

    def recv(self, timeout: float) -> bytes | None:  # pragma: no cover - hardware path
        if self._stack is None:
            raise TransportNotOpenError("IsoTpCanTransport.recv() before open()")
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            try:
                if self._stack.available():
                    payload = bytes(self._stack.recv())
                    trace_frame("RX", self.response_id, payload, logger=_can_log)
                    return payload
            except Exception as exc:
                raise TransportError(f"ISO-TP receive failed: {exc}") from exc
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.002)


class SilentTransport(UdsTransport):
    """A transport that accepts requests and never answers.

    Models an ECU that is not fitted, is asleep, or lives on a different bus --
    the single most common real-world "failure" and one the agent must handle
    gracefully rather than crash on. Used by the virtual backend for addresses
    the simulated vehicle does not populate.
    """

    def __init__(
        self,
        *,
        request_id: int = 0x7E0,
        response_id: int = 0x7E8,
        reason: str = "no module is fitted at this address",
    ) -> None:
        self.request_id = request_id
        self.response_id = response_id
        self.reason = reason
        self._open = False

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def send(self, payload: bytes) -> None:
        if not self._open:
            raise TransportNotOpenError("SilentTransport.send() before open()")
        trace_frame("TX", self.request_id, payload, note="no listener", logger=_can_log)

    def recv(self, timeout: float) -> bytes | None:
        if not self._open:
            raise TransportNotOpenError("SilentTransport.recv() before open()")
        # Burn the timeout the way a real bus would, but never longer than asked.
        time.sleep(min(max(timeout, 0.0), 0.01))
        return None

    def describe(self) -> dict[str, Any]:
        details = super().describe()
        details["reason"] = self.reason
        return details


__all__ = ["UdsTransport", "SimulatedTransport", "SilentTransport", "IsoTpCanTransport"]

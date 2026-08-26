"""Backend selection: one configuration switch, five ways to reach a car.

``MAJSTER_CAN_BACKEND`` picks the physical layer; everything above this module
is written against :class:`~majster_ai.mcp_servers.car_interface.transport.UdsTransport`
and neither knows nor cares which one is in use.

===========  ==========================================================
``virtual``  In-process Freelander 2 simulator. Default, safe, CI-ready.
``socketcan``Linux SocketCAN (``can0``/``vcan0``) via python-can + ISO-TP.
``slcan``    CANable/CANtact in slcan firmware, on a serial port.
``serial``   python-can's generic serial transport.
``j2534``    SAE J2534 PassThru -- Tactrix Openport 2.0.
``rfcomm``   ELM327-class adapter over Bluetooth RFCOMM. The Termux path.
===========  ==========================================================

One CAN bus is shared by every module transport, because opening ``can0``
several times is both wasteful and, on some drivers, an error.
"""

from __future__ import annotations

from typing import Any, Callable

from majster_ai.config import CanBackend, Settings
from majster_ai.errors import ConfigError, TransportError
from majster_ai.logging_setup import get_logger
from majster_ai.mcp_servers.car_interface.elm327 import Elm327Transport
from majster_ai.mcp_servers.car_interface.j2534 import J2534Transport
from majster_ai.mcp_servers.car_interface.modules import EcuModule
from majster_ai.mcp_servers.car_interface.simulator import (
    VehicleSimulator,
    build_freelander2_simulator,
)
from majster_ai.mcp_servers.car_interface.transport import (
    IsoTpCanTransport,
    SilentTransport,
    SimulatedTransport,
    UdsTransport,
)

log = get_logger("mcp_servers.car_interface.backends")

#: python-can interface name for each backend that uses python-can.
_PYTHON_CAN_INTERFACES: dict[CanBackend, str] = {
    CanBackend.SOCKETCAN: "socketcan",
    CanBackend.SLCAN: "slcan",
    CanBackend.SERIAL: "serial",
}


def make_can_bus_factory(settings: Settings) -> Callable[[], Any]:
    """Build a zero-argument factory that opens the configured python-can bus.

    Deferred rather than eager so that merely *constructing* a factory never
    touches hardware -- important because the MCP server builds its transport
    factory at import time, long before any tool is called.
    """
    interface = _PYTHON_CAN_INTERFACES.get(settings.can_backend)
    if interface is None:  # pragma: no cover - guarded by the caller
        raise ConfigError(f"Backend {settings.can_backend.value!r} does not use python-can")

    def factory() -> Any:
        try:
            import can
        except ImportError as exc:
            raise TransportError(
                "python-can is required for hardware backends. Install it with: "
                "pip install 'car-diagnostic-ai[car]'"
            ) from exc

        kwargs: dict[str, Any] = {
            "interface": interface,
            "channel": settings.can_channel,
        }
        # SocketCAN takes its bit rate from the kernel (`ip link set can0 up
        # type can bitrate 500000`); passing one here is an error on some
        # versions of python-can.
        if settings.can_backend is not CanBackend.SOCKETCAN:
            kwargs["bitrate"] = settings.can_bitrate

        log.info("Opening CAN bus: %s on %s", interface, settings.can_channel)
        try:
            return can.Bus(**kwargs)
        except Exception as exc:
            raise TransportError(
                f"Cannot open {interface} channel {settings.can_channel!r}: {exc}. "
                + _bus_hint(settings)
            ) from exc

    return factory


def _bus_hint(settings: Settings) -> str:
    """A concrete next step for the most common interface failures."""
    if settings.can_backend is CanBackend.SOCKETCAN:
        return (
            f"Bring the interface up first, e.g. "
            f"'sudo ip link set {settings.can_channel} up type can "
            f"bitrate {settings.can_bitrate}'. For a virtual bus: "
            f"'sudo modprobe vcan && sudo ip link add dev vcan0 type vcan && "
            f"sudo ip link set up vcan0'. SocketCAN needs a real Linux kernel -- "
            f"it is not available on stock Termux."
        )
    return (
        f"Check that {settings.can_channel!r} exists and that your user can "
        f"read it (on Linux: add yourself to the 'dialout' group)."
    )


def make_serial_port_factory(settings: Settings, *, baudrate: int = 115_200) -> Callable[[], Any]:
    """Build a factory that opens the ELM327 serial/RFCOMM port."""

    def factory() -> Any:
        try:
            import serial
        except ImportError as exc:
            raise TransportError(
                "pyserial is required for the rfcomm backend. Install it with: "
                "pip install 'car-diagnostic-ai[car]'"
            ) from exc
        log.info("Opening serial port %s at %d baud", settings.can_channel, baudrate)
        return serial.Serial(
            settings.can_channel,
            baudrate=baudrate,
            # Short read timeout: the transport polls for the '>' prompt and
            # applies its own overall deadline.
            timeout=0.2,
            write_timeout=2.0,
        )

    return factory


class TransportFactory:
    """Creates and owns one :class:`UdsTransport` per ECU.

    Args:
        settings: Effective configuration.
        vehicle: Simulator to use for the ``virtual`` backend. Defaults to a
            fresh Freelander 2. Injectable so tests can supply their own state.
    """

    def __init__(self, settings: Settings, vehicle: VehicleSimulator | None = None) -> None:
        self._settings = settings
        self._vehicle = vehicle
        self._transports: dict[str, UdsTransport] = {}
        self._bus_factory: Callable[[], Any] | None = None
        self._shared_bus: Any | None = None

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def vehicle(self) -> VehicleSimulator:
        """The simulated vehicle, created on first use."""
        if self._vehicle is None:
            self._vehicle = build_freelander2_simulator()
        return self._vehicle

    @property
    def backend(self) -> CanBackend:
        return self._settings.can_backend

    def _shared_bus_factory(self) -> Callable[[], Any]:
        """One physical bus, opened once, reused by every module transport."""
        if self._bus_factory is None:
            self._bus_factory = make_can_bus_factory(self._settings)

        def factory() -> Any:
            if self._shared_bus is None:
                self._shared_bus = self._bus_factory()  # type: ignore[misc]
            return self._shared_bus

        return factory

    def create(self, module: EcuModule) -> UdsTransport:
        """Return (and cache) the transport for one ECU."""
        cached = self._transports.get(module.name)
        if cached is not None:
            return cached

        transport = self._build(module)
        self._transports[module.name] = transport
        return transport

    def _build(self, module: EcuModule) -> UdsTransport:
        backend = self._settings.can_backend

        if backend is CanBackend.VIRTUAL:
            ecu = self.vehicle.get(module.request_id)
            if ecu is None:
                # Not every module in the map exists on every car. Modelling
                # that as silence -- rather than an exception -- is what the
                # real bus does, and exercises the agent's timeout handling.
                log.debug(
                    "Virtual backend: nothing simulated at 0x%03X (%s)",
                    module.request_id,
                    module.name,
                )
                return SilentTransport(
                    request_id=module.request_id,
                    response_id=module.response_id,
                    reason=f"{module.name} is not present in the simulated vehicle",
                )
            return SimulatedTransport(
                ecu, request_id=module.request_id, response_id=module.response_id
            )

        if backend is CanBackend.J2534:
            if not self._settings.j2534_library:
                raise ConfigError(
                    "The j2534 backend needs MAJSTER_J2534_LIBRARY set to the "
                    "PassThru shared library path."
                )
            return J2534Transport(
                self._settings.j2534_library,
                request_id=module.request_id,
                response_id=module.response_id,
                bitrate=self._settings.can_bitrate,
                extended_addressing=module.is_extended_addressing,
            )

        if backend is CanBackend.RFCOMM:
            return Elm327Transport(
                make_serial_port_factory(self._settings),
                request_id=module.request_id,
                response_id=module.response_id,
            )

        if backend in _PYTHON_CAN_INTERFACES:
            return IsoTpCanTransport(
                self._shared_bus_factory(),
                request_id=module.request_id,
                response_id=module.response_id,
                extended_addressing=module.is_extended_addressing,
                # The bus is shared; only close_all() may shut it down.
                close_bus=False,
            )

        raise ConfigError(f"Unsupported CAN backend: {backend!r}")

    def close_all(self) -> None:
        """Close every transport and the shared bus. Safe to call repeatedly."""
        for name, transport in list(self._transports.items()):
            try:
                transport.close()
            except Exception:  # pragma: no cover - teardown must not raise
                log.debug("Ignoring error closing transport for %s", name, exc_info=True)
        self._transports.clear()

        bus, self._shared_bus = self._shared_bus, None
        if bus is not None:
            try:
                bus.shutdown()
            except Exception:  # pragma: no cover - teardown must not raise
                log.debug("Ignoring error shutting down the shared CAN bus", exc_info=True)

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self._settings.can_backend.value,
            "channel": self._settings.can_channel,
            "bitrate": self._settings.can_bitrate,
            "physical": self._settings.can_backend.is_physical,
            "open_transports": sorted(self._transports),
        }

    def __enter__(self) -> TransportFactory:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close_all()


__all__ = [
    "TransportFactory",
    "make_can_bus_factory",
    "make_serial_port_factory",
]

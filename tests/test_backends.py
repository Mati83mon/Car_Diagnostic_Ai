"""Backend selection: the right transport for each configuration, lazily."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from majster_ai.config import CanBackend, load_settings
from majster_ai.errors import ConfigError, TransportError
from majster_ai.mcp_servers.car_interface.backends import (
    TransportFactory,
    make_can_bus_factory,
    make_serial_port_factory,
)
from majster_ai.mcp_servers.car_interface.elm327 import Elm327Transport
from majster_ai.mcp_servers.car_interface.j2534 import J2534Transport
from majster_ai.mcp_servers.car_interface.modules import load_module_map
from majster_ai.mcp_servers.car_interface.transport import (
    IsoTpCanTransport,
    SilentTransport,
    SimulatedTransport,
)


@pytest.fixture
def modules():
    return load_module_map()


class TestVirtualBackend:
    def test_populated_module_gets_a_simulator(self, settings, vehicle, modules) -> None:
        factory = TransportFactory(settings, vehicle)
        assert isinstance(factory.create(modules.resolve("ECM")), SimulatedTransport)

    def test_unpopulated_module_gets_silence(self, settings, vehicle, modules) -> None:
        # Modelling an unfitted module as silence -- not an exception -- is what
        # a real bus does, and exercises the agent's timeout handling.
        transport = TransportFactory(settings, vehicle).create(modules.resolve("PAM"))
        assert isinstance(transport, SilentTransport)

    def test_transports_are_cached(self, settings, vehicle, modules) -> None:
        factory = TransportFactory(settings, vehicle)
        module = modules.resolve("ECM")
        assert factory.create(module) is factory.create(module)

    def test_vehicle_is_created_on_demand(self, settings) -> None:
        factory = TransportFactory(settings)
        assert len(factory.vehicle) > 0

    def test_describe_reports_it_is_not_physical(self, settings) -> None:
        assert TransportFactory(settings).describe()["physical"] is False


class TestHardwareBackendSelection:
    """Constructing a transport must never touch hardware."""

    @pytest.mark.parametrize(
        ("backend", "expected"),
        [
            (CanBackend.SOCKETCAN, IsoTpCanTransport),
            (CanBackend.SLCAN, IsoTpCanTransport),
            (CanBackend.SERIAL, IsoTpCanTransport),
            (CanBackend.RFCOMM, Elm327Transport),
        ],
    )
    def test_backend_maps_to_transport(self, backend, expected, modules) -> None:
        settings = load_settings(can_backend=backend, can_channel="/dev/null")
        transport = TransportFactory(settings).create(modules.resolve("ECM"))
        assert isinstance(transport, expected)
        assert transport.is_open is False, "constructing must not open hardware"

    def test_j2534(self, modules) -> None:
        settings = load_settings(can_backend=CanBackend.J2534, j2534_library="/x/lib.so")
        assert isinstance(TransportFactory(settings).create(modules.resolve("ECM")), J2534Transport)

    def test_j2534_without_a_library_is_rejected_at_config_time(self) -> None:
        with pytest.raises(ConfigError):
            load_settings(can_backend=CanBackend.J2534)

    def test_module_addresses_are_propagated(self, modules) -> None:
        settings = load_settings(can_backend=CanBackend.SOCKETCAN, can_channel="can0")
        transport = TransportFactory(settings).create(modules.resolve("TCM"))
        assert transport.request_id == 0x7E1
        assert transport.response_id == 0x7E9


class TestBusFactory:
    def test_socketcan_omits_bitrate(self) -> None:
        # SocketCAN takes its bit rate from the kernel; passing one errors on
        # some python-can versions.
        settings = load_settings(can_backend=CanBackend.SOCKETCAN, can_channel="can0")
        bus_module = Mock()
        with patch.dict("sys.modules", {"can": bus_module}):
            make_can_bus_factory(settings)()
        kwargs = bus_module.Bus.call_args.kwargs
        assert kwargs["interface"] == "socketcan"
        assert "bitrate" not in kwargs

    def test_slcan_passes_bitrate(self) -> None:
        settings = load_settings(
            can_backend=CanBackend.SLCAN, can_channel="/dev/ttyUSB0", can_bitrate=500000
        )
        bus_module = Mock()
        with patch.dict("sys.modules", {"can": bus_module}):
            make_can_bus_factory(settings)()
        assert bus_module.Bus.call_args.kwargs["bitrate"] == 500000

    def test_failure_message_names_the_fix(self) -> None:
        settings = load_settings(can_backend=CanBackend.SOCKETCAN, can_channel="can0")
        bus_module = Mock()
        bus_module.Bus.side_effect = OSError("No such device")
        with patch.dict("sys.modules", {"can": bus_module}):
            with pytest.raises(TransportError, match="ip link set"):
                make_can_bus_factory(settings)()

    def test_termux_limitation_is_mentioned(self) -> None:
        settings = load_settings(can_backend=CanBackend.SOCKETCAN, can_channel="can0")
        bus_module = Mock()
        bus_module.Bus.side_effect = OSError("nope")
        with patch.dict("sys.modules", {"can": bus_module}):
            with pytest.raises(TransportError, match="Termux"):
                make_can_bus_factory(settings)()

    def test_virtual_backend_rejects_the_bus_factory(self, settings) -> None:
        with pytest.raises(ConfigError):
            make_can_bus_factory(settings)

    def test_serial_factory_configures_the_port(self) -> None:
        settings = load_settings(can_backend=CanBackend.RFCOMM, can_channel="/dev/rfcomm0")
        serial_module = Mock()
        with patch.dict("sys.modules", {"serial": serial_module}):
            make_serial_port_factory(settings)()
        assert serial_module.Serial.call_args.args[0] == "/dev/rfcomm0"


class TestSharedBus:
    def test_one_bus_serves_every_module(self, modules) -> None:
        # Opening can0 once per ECU is wasteful and errors on some drivers.
        settings = load_settings(can_backend=CanBackend.SOCKETCAN, can_channel="can0")
        factory = TransportFactory(settings)
        bus = Mock()
        bus_module = Mock()
        bus_module.Bus.return_value = bus
        with patch.dict("sys.modules", {"can": bus_module}):
            first = factory.create(modules.resolve("ECM"))
            second = factory.create(modules.resolve("TCM"))
            first._bus_factory()
            second._bus_factory()
        assert bus_module.Bus.call_count == 1

    def test_close_all_shuts_the_bus_down(self, modules) -> None:
        settings = load_settings(can_backend=CanBackend.SOCKETCAN, can_channel="can0")
        factory = TransportFactory(settings)
        bus = Mock()
        bus_module = Mock()
        bus_module.Bus.return_value = bus
        with patch.dict("sys.modules", {"can": bus_module}):
            factory.create(modules.resolve("ECM"))._bus_factory()
            factory.close_all()
        bus.shutdown.assert_called_once()

    def test_close_all_is_idempotent(self, settings, vehicle, modules) -> None:
        factory = TransportFactory(settings, vehicle)
        factory.create(modules.resolve("ECM"))
        factory.close_all()
        factory.close_all()

    def test_teardown_survives_a_failing_transport(self, settings, vehicle, modules) -> None:
        factory = TransportFactory(settings, vehicle)
        factory.create(modules.resolve("ECM"))
        list(factory._transports.values())[0].close = Mock(side_effect=RuntimeError("boom"))
        factory.close_all()  # must not raise

    def test_context_manager(self, settings, vehicle, modules) -> None:
        with TransportFactory(settings, vehicle) as factory:
            factory.create(modules.resolve("ECM"))
        assert factory._transports == {}

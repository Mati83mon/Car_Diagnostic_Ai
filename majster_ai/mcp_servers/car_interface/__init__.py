"""Car_Interface_MCP -- UDS/CAN access to the vehicle's control modules.

Layering, from the metal upwards::

    backends.TransportFactory      picks the physical layer from config
      -> transport.UdsTransport    send()/recv() of UDS payloads
           SimulatedTransport      in-process Freelander 2 (default)
           IsoTpCanTransport       python-can + ISO-TP (socketcan/slcan/serial)
           J2534Transport          Tactrix Openport 2.0 and friends
           Elm327Transport         Bluetooth RFCOMM dongles (Termux)
           SilentTransport         a module that is not fitted
      -> uds_client.UdsSession     retries, timeouts, NRC 0x78, stale frames
      -> service.CarInterfaceService  the safety gate and the tool surface
      -> server                    MCP over stdio
"""

from __future__ import annotations

from majster_ai.mcp_servers.car_interface.dtc import Dtc, DtcStatus, decode_dtc_records
from majster_ai.mcp_servers.car_interface.modules import EcuModule, ModuleMap, load_module_map
from majster_ai.mcp_servers.car_interface.pids import SignalCatalogue, load_signal_catalogue
from majster_ai.mcp_servers.car_interface.service import CarInterfaceService
from majster_ai.mcp_servers.car_interface.uds_client import UdsSession

__all__ = [
    "CarInterfaceService",
    "UdsSession",
    "Dtc",
    "DtcStatus",
    "decode_dtc_records",
    "EcuModule",
    "ModuleMap",
    "load_module_map",
    "SignalCatalogue",
    "load_signal_catalogue",
]

"""MCP server exposing the vehicle interface as tools.

Run it directly (stdio transport)::

    python -m majster_ai.mcp_servers.car_interface.server

The tool docstrings below are the LLM's entire understanding of how to drive a
car's diagnostic bus, so they are written for that reader: what the tool does,
what the arguments mean, and -- for the write tool -- exactly why it will be
refused the first time and what to do about it.

Logging goes to stderr. stdout carries JSON-RPC and must not be written to.
"""

from __future__ import annotations

import sys
from typing import Any

from majster_ai.config import get_settings
from majster_ai.logging_setup import configure_logging, get_logger
from majster_ai.mcp_servers.car_interface.service import CarInterfaceService

log = get_logger("mcp_servers.car_interface.server")

SERVER_NAME = "car_interface"
SERVER_INSTRUCTIONS = """\
Diagnostic access to a Land Rover Freelander 2 (2010, 2.2 TD4) over UDS/CAN.

Read tools (read_dtc, read_all_dtcs, read_live_data, read_did, scan_modules,
list_modules, list_signals, vehicle_info) are always safe and never change the
vehicle.

clear_dtc is a WRITE. It is refused by default and, when enabled, always
requires explicit human approval through a two-step confirmation handshake.
Never present a clear as routine: erasing codes destroys freeze-frame evidence
and does not repair anything.

Addresses marked address_verified=false are community guesses, not facts.
Confirm them with scan_modules() before trusting a silence or a reading.
"""


def build_server(service: CarInterfaceService | None = None) -> Any:
    """Construct the FastMCP server.

    Args:
        service: Injected service, used by tests. Defaults to one built from
            the process configuration.

    Raises:
        ImportError: if the ``mcp`` package is not installed.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ImportError(
            "The 'mcp' package is required to run an MCP server. Install it "
            "with: pip install 'car-diagnostic-ai[mcp]'"
        ) from exc

    car = service or CarInterfaceService()
    mcp = FastMCP(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)

    @mcp.tool()
    def read_dtc(module_id: str = "ECM", status_mask: str = "all") -> dict[str, Any]:
        """Read Diagnostic Trouble Codes (fault codes) from one control module.

        This is the first thing to do when investigating a complaint. Safe and
        read-only.

        Args:
            module_id: Which module to query. Use a name (ECM, TCM, ABS, RCM,
                CJB, IPC, HALDEX, TRM, PBM, HVAC, PAM), an alias (engine,
                gearbox, brakes, airbag), or a request id such as "0x7E0".
            status_mask: Which codes to return.
                "all" - everything stored (default).
                "confirmed" - faults the ECU has confirmed; what a garage
                    tester would call current faults.
                "pending" - seen once this drive cycle, not yet confirmed;
                    an early warning of a developing fault.
                "test_failed" - failing at this instant.
                "confirmed_or_pending" - both of the above.

        Returns:
            The decoded codes with their status bits, the generic SAE meaning
            where one exists, and whether the module's address is verified.
            Manufacturer-specific codes carry no built-in description - look
            those up with the search_manual tool.
        """
        return car.read_dtc(module_id=module_id, status_mask=status_mask)

    @mcp.tool()
    def read_all_dtcs() -> dict[str, Any]:
        """Read DTCs from every known module -- a whole-vehicle health scan.

        Use this when the complaint is vague ("warning lights", "runs badly")
        and you do not yet know which system is at fault. Slower than read_dtc
        because unfitted modules must time out. Safe and read-only.

        Returns:
            Per-module results plus a list of modules that did not answer.
            Silence usually means the module is not fitted to this car, is
            asleep, or the address is one of the unverified guesses.
        """
        return car.read_all_dtcs()

    @mcp.tool()
    def read_live_data(pid_list: list[str], module_id: str = "ECM") -> dict[str, Any]:
        """Read live sensor values from a module. Safe and read-only.

        Use this to confirm or refute a hypothesis from a fault code. For
        example, with P0299 (underboost) stored, compare MAP against
        BAROMETRIC_PRESSURE at idle and under load.

        Args:
            pid_list: Signal names, e.g. ["RPM", "COOLANT_TEMP", "MAF"].
                Call list_signals() for everything available. Unknown names are
                reported individually and do not fail the whole request.
            module_id: Which module to read from. Defaults to ECM.

        Returns:
            Each signal with its decoded value, unit and raw bytes. A
            "warning" field marks physically implausible readings (an
            unplugged sensor, a short); those are diagnostic evidence, not
            errors. A "caution" field marks signals whose scaling is
            unverified for this vehicle.
        """
        return car.read_live_data(pid_list=pid_list, module_id=module_id)

    @mcp.tool()
    def read_did(module_id: str, did: str) -> dict[str, Any]:
        """Read a raw UDS Data Identifier with no scaling applied.

        The escape hatch for identifiers that are not in the catalogue. Use it
        to explore manufacturer-specific DIDs on a car you are characterising.
        Safe and read-only.

        Args:
            module_id: Which module to query.
            did: Four hex digits, e.g. "F190" for the VIN.

        Returns:
            The raw bytes plus ASCII and unsigned-integer interpretations.
            Interpreting them correctly needs the manufacturer's definition -
            do not guess a scaling and present it as fact.
        """
        return car.read_did(module_id=module_id, did=did)

    @mcp.tool()
    def scan_modules() -> dict[str, Any]:
        """Discover which ECU addresses actually answer on this vehicle.

        Safe and read-only: probes each mapped address with TesterPresent, the
        standard harmless presence check. Run this once per car to turn the
        unverified addresses in the module map into confirmed ones, and
        whenever a module unexpectedly fails to answer.
        """
        return car.scan_modules()

    @mcp.tool()
    def list_modules() -> dict[str, Any]:
        """List the control modules this tool can address, with their status.

        Check the "verified" flag: only ECM and TCM sit at legislated OBD-II
        addresses. The rest are community-derived starting points.
        """
        return car.list_modules()

    @mcp.tool()
    def list_signals() -> dict[str, Any]:
        """List every live-data signal available to read_live_data.

        Includes the identifier, unit, scaling formula and plausible range for
        each, and whether the scaling is verified for this vehicle.
        """
        return car.list_signals()

    @mcp.tool()
    def vehicle_info(module_id: str = "ECM") -> dict[str, Any]:
        """Read the VIN and the module's hardware/software part numbers.

        Safe and read-only. Use it to confirm you are talking to the car you
        think you are, and to pin down the exact ECU variant before searching
        the workshop manual.
        """
        return car.vehicle_info(module_id=module_id)

    @mcp.tool()
    def interface_status() -> dict[str, Any]:
        """Report the interface backend and the current safety posture.

        Tells you whether you are talking to a real car or the simulator, and
        whether writes are permitted at all.
        """
        return car.status()

    @mcp.tool()
    def clear_dtc(
        module_id: str = "ECM",
        dtc_code: str | None = None,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Erase stored fault codes from a module. THIS IS A WRITE OPERATION.

        It changes the vehicle and cannot be undone. It is therefore governed
        by a mandatory two-step handshake:

        Step 1 - call without confirmation_token. The tool will REFUSE and
        return an "impact" section listing exactly which codes would be
        erased, plus the risks. Present that to the human operator verbatim
        and ask them to approve. Do not paraphrase away the warnings.

        Step 2 - only after the human has explicitly agreed, call again with
        the confirmation_token from step 1.

        The token is single-use, expires, and is bound to this exact module and
        code. Never invent one: a token you did not receive from step 1 is
        refused. If the whole agent is READ_ONLY the call is refused outright
        and no token is issued - that is an operator setting you cannot change.

        Before proposing a clear at all, consider whether it is the right
        action. Clearing destroys the freeze-frame data recorded when the fault
        occurred, resets emissions readiness monitors, and repairs nothing. A
        code whose root cause is still present will simply return.

        Args:
            module_id: Which module to clear.
            dtc_code: A single code such as "P0299" to clear only that one.
                Omit to clear every stored code in the module.
            confirmation_token: The token from step 1, after human approval.
        """
        return car.clear_dtc(
            module_id=module_id, dtc_code=dtc_code, confirmation_token=confirmation_token
        )

    log.info(
        "car_interface MCP server ready (backend=%s, safety=%s)",
        car.settings.can_backend.value,
        car.settings.safety_mode.value,
    )
    return mcp


def main() -> int:
    """Entry point for ``python -m majster_ai.mcp_servers.car_interface.server``."""
    settings = get_settings()
    configure_logging(settings)
    try:
        server = build_server()
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())

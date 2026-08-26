"""The vehicle-facing service layer, and the only door writes can pass through.

Every tool the LLM can call lands here. That makes this module the right --
and only -- place to enforce the safety policy, because a check that lives in
the prompt is a suggestion and a check that lives in the graph can be bypassed
by calling the MCP server directly.

The two-phase write protocol
----------------------------
A mutating call never executes on first request. Instead:

1. The agent calls ``clear_dtc(module_id="ECM")``.
2. The service refuses, and returns *what would happen* -- the exact DTCs that
   would be erased, the module, the risks -- together with a single-use
   ``confirmation_token`` bound to that precise operation.
3. A human sees that summary and approves.
4. The agent calls ``clear_dtc(module_id="ECM", confirmation_token=...)`` and
   only now does anything reach the bus.

The token is generated server-side, expires, is single-use, and is bound to a
fingerprint of the operation's arguments. A token issued for "clear the ECM"
cannot be replayed to clear the ABS module, and a model that hallucinates a
token gets a refusal rather than a write. This holds even when the MCP server
is driven by something other than our LangGraph agent.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from majster_ai.config import Settings, get_settings
from majster_ai.errors import (
    MajsterError,
    SafetyViolation,
    UdsNegativeResponse,
    UdsProtocolError,
    UdsTimeoutError,
    UnknownSignalError,
)
from majster_ai.logging_setup import get_logger, log_agent_step
from majster_ai.mcp_servers.car_interface.backends import TransportFactory
from majster_ai.mcp_servers.car_interface.dtc import Dtc, encode_dtc_code, resolve_status_mask
from majster_ai.mcp_servers.car_interface.modules import EcuModule, ModuleMap, load_module_map
from majster_ai.mcp_servers.car_interface.pids import (
    SignalCatalogue,
    SignalSource,
    load_signal_catalogue,
)
from majster_ai.mcp_servers.car_interface.simulator import VehicleSimulator
from majster_ai.mcp_servers.car_interface.uds_client import (
    CLEAR_ALL_DTC_GROUP,
    SESSION_EXTENDED,
    UdsSession,
)

log = get_logger("mcp_servers.car_interface.service")

#: How long an approval token stays valid. Long enough for a human to read the
#: summary and decide; short enough that a stale approval cannot be replayed
#: after the vehicle's state has moved on.
CONFIRMATION_TTL_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class PendingWrite:
    """A write operation waiting for human approval."""

    token: str
    operation: str
    fingerprint: str
    """Hash of the operation arguments. Binds the token to this exact call."""

    summary: dict[str, Any]
    created_at: float
    expires_at: float

    def is_expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) > self.expires_at


class CarInterfaceService:
    """Read and (under strict conditions) write vehicle diagnostic data.

    Args:
        settings: Effective configuration. Defaults to the process settings.
        module_map: ECU address map. Defaults to the built-in Freelander 2 map
            merged with any user overlay.
        catalogue: Live-data signal catalogue.
        factory: Transport factory. Injected by tests to supply a simulator.
        vehicle: Simulated vehicle for the ``virtual`` backend.
        clock: Injectable monotonic clock, for testing token expiry.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        module_map: ModuleMap | None = None,
        catalogue: SignalCatalogue | None = None,
        factory: TransportFactory | None = None,
        vehicle: VehicleSimulator | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self.settings = settings or get_settings()
        self.modules = module_map or load_module_map()
        self.signals = catalogue or load_signal_catalogue()
        self.factory = factory or TransportFactory(self.settings, vehicle)
        self._sessions: dict[str, UdsSession] = {}
        self._pending: dict[str, PendingWrite] = {}
        self._clock = clock

    # -- session plumbing ---------------------------------------------------
    def _session_for(self, module: EcuModule) -> UdsSession:
        """Get (and cache) an open UDS session for one module."""
        existing = self._sessions.get(module.name)
        if existing is not None:
            return existing

        session = UdsSession(
            self.factory.create(module),
            timeout=self.settings.uds_timeout,
            extended_timeout=self.settings.uds_extended_timeout,
            retries=self.settings.uds_retries,
            backoff=self.settings.uds_retry_backoff,
            max_response_pending=self.settings.uds_max_response_pending,
            name=module.name,
        )
        session.open()
        self._sessions[module.name] = session
        return session

    def _ensure_extended_session(self, session: UdsSession, module: EcuModule) -> None:
        """Enter the extended session when the module needs one.

        Failure is not fatal: plenty of modules answer perfectly well in the
        default session, and refusing to try would turn a working read into an
        error for no reason.
        """
        if not module.extended_session_required:
            return
        if session.current_session == SESSION_EXTENDED:
            return
        try:
            session.start_session(SESSION_EXTENDED)
        except MajsterError as exc:
            log.warning(
                "%s: could not enter the extended session (%s) - continuing in "
                "the default session",
                module.name,
                exc.message,
            )

    def close(self) -> None:
        """Close every session and release the interface."""
        for name, session in list(self._sessions.items()):
            try:
                session.close()
            except Exception:  # pragma: no cover - teardown must not raise
                log.debug("Ignoring error closing session for %s", name, exc_info=True)
        self._sessions.clear()
        self.factory.close_all()

    def __enter__(self) -> CarInterfaceService:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- read: DTCs ---------------------------------------------------------
    def read_dtc(self, module_id: str = "ECM", status_mask: str | int = "all") -> dict[str, Any]:
        """Read Diagnostic Trouble Codes from one module.

        Args:
            module_id: Module name, alias or request id (``ECM``, ``engine``,
                ``0x7E0``).
            status_mask: ``all``, ``confirmed``, ``pending``, ``test_failed``,
                ``confirmed_or_pending``, or a raw byte such as ``0x08``.

        Returns:
            A JSON-safe payload. On failure, ``ok`` is False and ``message``
            explains what to do next -- the tool never raises into the agent.
        """
        try:
            module = self.modules.resolve(module_id)
            mask = resolve_status_mask(status_mask)
        except MajsterError as exc:
            return exc.to_dict()

        log_agent_step(
            "car.read_dtc",
            f"Reading DTCs from {module.name}",
            ctx_module=module.name,
            mask=f"0x{mask:02X}",
        )

        try:
            session = self._session_for(module)
            self._ensure_extended_session(session, module)
            dtcs: list[Dtc] = session.read_dtc_by_status_mask(mask)
        except MajsterError as exc:
            payload = exc.to_dict()
            payload["module"] = module.name
            payload.setdefault("details", {})["address"] = f"0x{module.request_id:03X}"
            if isinstance(exc, UdsTimeoutError) and not module.verified:
                payload["hint"] = (
                    f"The address for {module.name} (0x{module.request_id:03X}) is "
                    f"UNVERIFIED for this vehicle. Run scan_modules() to find the "
                    f"addresses that actually answer."
                )
            return payload

        tagged = [d.with_module(module.name) for d in dtcs]
        return {
            "ok": True,
            "module": module.name,
            "module_description": module.description,
            "address": f"0x{module.request_id:03X}",
            "address_verified": module.verified,
            "status_mask": f"0x{mask:02X}",
            "count": len(tagged),
            "dtcs": [d.to_dict() for d in tagged],
            "summary": self._summarise_dtcs(tagged, module),
        }

    @staticmethod
    def _summarise_dtcs(dtcs: Sequence[Dtc], module: EcuModule) -> str:
        if not dtcs:
            return f"No DTCs stored in {module.name}."
        confirmed = [d for d in dtcs if d.status.is_confirmed]
        pending = [d for d in dtcs if d.status.is_pending and not d.status.is_confirmed]
        parts = [f"{len(dtcs)} DTC(s) in {module.name}"]
        if confirmed:
            parts.append(
                f"{len(confirmed)} confirmed: " + ", ".join(d.full_code for d in confirmed)
            )
        if pending:
            parts.append(f"{len(pending)} pending only: " + ", ".join(d.full_code for d in pending))
        unknown = [d for d in dtcs if not d.is_generic]
        if unknown:
            parts.append(
                "manufacturer-specific and needing the workshop manual: "
                + ", ".join(d.full_code for d in unknown)
            )
        return ". ".join(parts) + "."

    def read_all_dtcs(self, modules: Iterable[str] | None = None) -> dict[str, Any]:
        """Read DTCs from every known module -- a whole-vehicle health scan."""
        names = list(modules) if modules is not None else self.modules.names()
        results: list[dict[str, Any]] = []
        total = 0
        for name in names:
            result = self.read_dtc(name)
            results.append(result)
            if result.get("ok"):
                total += int(result.get("count", 0))
        responded = [r["module"] for r in results if r.get("ok")]
        silent = [r.get("module", "?") for r in results if not r.get("ok")]
        return {
            "ok": True,
            "modules_scanned": len(names),
            "modules_responded": responded,
            "modules_not_responding": silent,
            "total_dtcs": total,
            "results": results,
            "summary": (
                f"{total} DTC(s) across {len(responded)} responding module(s). "
                f"Not responding: {', '.join(silent) if silent else 'none'}."
            ),
        }

    # -- read: live data ----------------------------------------------------
    def read_live_data(
        self, pid_list: Sequence[str] | str, module_id: str = "ECM"
    ) -> dict[str, Any]:
        """Read one or more live-data signals.

        Partial success is a first-class outcome: one unsupported PID must not
        void the twelve that worked, because on a real car you routinely ask
        for signals a given ECU does not implement.
        """
        if isinstance(pid_list, str):
            pid_list = [part.strip() for part in pid_list.split(",") if part.strip()]
        if not pid_list:
            return {
                "ok": False,
                "error": "no_signals_requested",
                "message": "pid_list was empty. Call list_signals() to see what is available.",
            }

        try:
            module = self.modules.resolve(module_id)
        except MajsterError as exc:
            return exc.to_dict()

        log_agent_step(
            "car.read_live_data",
            f"Reading {len(pid_list)} signal(s) from {module.name}",
            ctx_module=module.name,
            signals=list(pid_list),
        )

        try:
            session = self._session_for(module)
            self._ensure_extended_session(session, module)
        except MajsterError as exc:
            return exc.to_dict()

        values: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []

        for token in pid_list:
            try:
                signal = self.signals.resolve(str(token))
            except UnknownSignalError as exc:
                failures.append({"requested": token, "error": exc.code, "message": exc.message})
                continue

            try:
                if signal.source is SignalSource.OBD_PID:
                    raw = session.read_obd_pid(signal.identifier)
                else:
                    raw = session.read_data_by_identifier(signal.identifier)
                value = signal.decode(raw)
            except (UdsNegativeResponse, UdsTimeoutError, UdsProtocolError) as exc:
                failures.append(
                    {
                        "requested": token,
                        "signal": signal.name,
                        "identifier": signal.identifier_hex,
                        "error": exc.code,
                        "message": exc.message,
                    }
                )
                continue

            entry: dict[str, Any] = {
                "signal": signal.name,
                "description": signal.description,
                "value": value,
                "unit": signal.unit,
                "identifier": signal.identifier_hex,
                "source": signal.source.value,
                "raw": raw.hex().upper(),
                "verified_scaling": signal.verified,
            }
            warning = signal.plausibility_warning(value)
            if warning:
                entry["warning"] = warning
            if not signal.verified:
                entry["caution"] = (
                    "This identifier's scaling is UNVERIFIED for this vehicle. "
                    "Treat the number as indicative until you confirm it."
                )
            values.append(entry)

        return {
            "ok": bool(values) or not failures,
            "module": module.name,
            "address": f"0x{module.request_id:03X}",
            "requested": list(pid_list),
            "values": values,
            "failures": failures,
            "summary": self._summarise_values(values, failures, module),
        }

    @staticmethod
    def _summarise_values(
        values: Sequence[dict[str, Any]], failures: Sequence[dict[str, Any]], module: EcuModule
    ) -> str:
        if not values:
            return f"No signals could be read from {module.name}."
        readings = ", ".join(
            f"{v['signal']}={v['value']}{(' ' + v['unit']) if v['unit'] else ''}" for v in values
        )
        text = f"{module.name}: {readings}."
        if failures:
            text += f" {len(failures)} signal(s) unavailable."
        flagged = [v["signal"] for v in values if "warning" in v]
        if flagged:
            text += f" Implausible readings on: {', '.join(flagged)}."
        return text

    def read_did(self, module_id: str, did: str | int) -> dict[str, Any]:
        """Read a raw Data Identifier -- the read-only escape hatch.

        Lets you probe identifiers that are not in the catalogue, which is how
        you discover the manufacturer DIDs for your own car without guessing.
        """
        try:
            module = self.modules.resolve(module_id)
            identifier = did if isinstance(did, int) else int(str(did), 16)
        except MajsterError as exc:
            return exc.to_dict()
        except ValueError:
            return {
                "ok": False,
                "error": "invalid_did",
                "message": f"{did!r} is not a valid hexadecimal DID (e.g. 'F190').",
            }

        try:
            session = self._session_for(module)
            self._ensure_extended_session(session, module)
            raw = session.read_data_by_identifier(identifier)
        except MajsterError as exc:
            return exc.to_dict()

        printable = raw.decode("ascii", errors="replace").strip("\x00 \t\r\n")
        return {
            "ok": True,
            "module": module.name,
            "did": f"0x{identifier:04X}",
            "length": len(raw),
            "raw": raw.hex().upper(),
            "as_ascii": printable if printable.isprintable() else None,
            "as_uint": int.from_bytes(raw, "big") if 0 < len(raw) <= 8 else None,
            "note": (
                "Raw data with no scaling applied. Interpreting it requires the "
                "manufacturer's DID definition."
            ),
        }

    # -- discovery ----------------------------------------------------------
    def scan_modules(self, timeout: float = 0.5) -> dict[str, Any]:
        """Probe every mapped address and report which ones answer.

        Read-only: uses TesterPresent, the standard harmless presence probe.
        This is how you turn the unverified addresses in the module map into
        verified ones for your specific vehicle.
        """
        log_agent_step("car.scan_modules", "Probing every mapped ECU address")
        present: list[dict[str, Any]] = []
        absent: list[dict[str, Any]] = []

        for module in self.modules:
            try:
                session = self._session_for(module)
                # One quick attempt per address: a full retry cycle on a dozen
                # absent modules would take minutes.
                probe_session = UdsSession(
                    session.transport,
                    timeout=timeout,
                    retries=0,
                    backoff=0,
                    name=module.name,
                )
                responded = probe_session.probe(timeout=timeout)
            except MajsterError as exc:
                absent.append({"module": module.name, "reason": exc.message})
                continue

            entry = {
                "module": module.name,
                "address": f"0x{module.request_id:03X}",
                "description": module.description,
                "previously_verified": module.verified,
            }
            (present if responded else absent).append(entry)

        return {
            "ok": True,
            "responding": present,
            "not_responding": absent,
            "summary": (
                f"{len(present)} module(s) answered: "
                f"{', '.join(m['module'] for m in present) or 'none'}. "
                f"{len(absent)} did not."
            ),
            "next_step": (
                "Addresses that answered are correct for this vehicle. Record them "
                'in data/modules.json with "verified": true so future runs trust them.'
            ),
        }

    def vehicle_info(self, module_id: str = "ECM") -> dict[str, Any]:
        """Read the identification block: VIN, hardware and software numbers."""
        return self.read_live_data(
            ["VIN", "ECU_HARDWARE_NUMBER", "ECU_SOFTWARE_NUMBER", "ECU_SERIAL"],
            module_id=module_id,
        )

    def list_modules(self) -> dict[str, Any]:
        return {
            "ok": True,
            "count": len(self.modules),
            "modules": self.modules.to_dict(),
            "note": (
                "Only addresses with verified=true are legislated and certain. "
                "Confirm the rest with scan_modules() before relying on them."
            ),
        }

    def list_signals(self) -> dict[str, Any]:
        return {
            "ok": True,
            "count": len(self.signals),
            "signals": self.signals.to_dict(),
        }

    # ==================================================================
    #  WRITE OPERATIONS -- everything below this line touches the vehicle
    # ==================================================================
    def _fingerprint(self, operation: str, **arguments: Any) -> str:
        """A stable hash of an operation and its arguments.

        Binds a confirmation token to one exact call, so approval to clear the
        ECM cannot be replayed to clear the airbag module.
        """
        parts = [operation] + [f"{k}={arguments[k]!r}" for k in sorted(arguments)]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]

    def _issue_confirmation(
        self, operation: str, summary: dict[str, Any], **arguments: Any
    ) -> dict[str, Any]:
        """Refuse the write and hand back a token that would authorise it."""
        self._prune_expired()
        now = self._clock()
        pending = PendingWrite(
            token=secrets.token_urlsafe(24),
            operation=operation,
            fingerprint=self._fingerprint(operation, **arguments),
            summary=summary,
            created_at=now,
            expires_at=now + CONFIRMATION_TTL_SECONDS,
        )
        self._pending[pending.token] = pending
        log_agent_step(
            "safety.confirmation_required",
            f"{operation} requires human approval",
            operation=operation,
        )
        return {
            "ok": False,
            "requires_confirmation": True,
            "operation": operation,
            "confirmation_token": pending.token,
            "expires_in_seconds": CONFIRMATION_TTL_SECONDS,
            "impact": summary,
            "message": (
                f"'{operation}' is a WRITE operation and will not run without "
                f"explicit human approval. Show the operator the 'impact' section "
                f"above, obtain their confirmation, then call this tool again with "
                f"confirmation_token set. Do not invent a token: an invalid one is "
                f"refused."
            ),
        }

    def _prune_expired(self) -> None:
        now = self._clock()
        for token in [t for t, p in self._pending.items() if p.is_expired(now)]:
            del self._pending[token]

    def _redeem(self, token: str, operation: str, **arguments: Any) -> PendingWrite:
        """Validate and consume a confirmation token.

        Raises:
            SafetyViolation: if the token is unknown, expired, or was issued
                for a different operation.
        """
        self._prune_expired()
        pending = self._pending.get(token)
        if pending is None:
            raise SafetyViolation(
                "Invalid or expired confirmation token. Request the operation "
                "again without a token to obtain a fresh one, and have a human "
                "approve it.",
                operation=operation,
            )
        if pending.operation != operation:
            raise SafetyViolation(
                f"This token authorises '{pending.operation}', not '{operation}'.",
                operation=operation,
            )
        expected = self._fingerprint(operation, **arguments)
        if pending.fingerprint != expected:
            raise SafetyViolation(
                "This token was issued for a different set of arguments. "
                "Approval is bound to one exact operation and cannot be reused "
                "for another module or code.",
                operation=operation,
            )
        # Single use: consume it whether or not the write then succeeds.
        del self._pending[token]
        return pending

    def _guard_write(self, operation: str) -> None:
        """The master switch. Raises unless writes are enabled at all."""
        if not self.settings.write_enabled:
            raise SafetyViolation(
                f"'{operation}' was refused: this agent is READ_ONLY. Writing to a "
                f"vehicle is disabled by default. An operator must set "
                f"MAJSTER_WRITE_ENABLED=true and restart before any write is "
                f"possible. The agent cannot change this itself.",
                operation=operation,
                safety_mode=self.settings.safety_mode.value,
            )

    def clear_dtc(
        self,
        module_id: str = "ECM",
        dtc_code: str | None = None,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Erase stored DTCs from a module. **This is a write operation.**

        Args:
            module_id: Which module to clear.
            dtc_code: Clear one specific code (``P0299``). ``None`` clears all.
            confirmation_token: Token from a previous unconfirmed call,
                after a human has approved it.

        Returns:
            On the first call: a refusal carrying the impact summary and a
            token. With a valid token: the result of the operation.
        """
        operation = "clear_dtc"
        try:
            self._guard_write(operation)
            module = self.modules.resolve(module_id)
        except MajsterError as exc:
            return exc.to_dict()

        group = CLEAR_ALL_DTC_GROUP
        if dtc_code:
            try:
                group = int.from_bytes(encode_dtc_code(dtc_code), "big")
            except MajsterError as exc:
                return exc.to_dict()

        arguments = {"module": module.name, "dtc_code": (dtc_code or "").upper() or None}

        if self.settings.require_approval:
            if not confirmation_token:
                return self._issue_confirmation(
                    operation, self._describe_clear_impact(module, dtc_code), **arguments
                )
            try:
                self._redeem(confirmation_token, operation, **arguments)
            except MajsterError as exc:
                return exc.to_dict()
        else:
            log.warning(
                "clear_dtc running WITHOUT human approval "
                "(MAJSTER_REQUIRE_APPROVAL=false). This is a bench-rig setting."
            )

        log_agent_step(
            "car.clear_dtc",
            f"APPROVED - clearing {'all DTCs' if not dtc_code else dtc_code} from {module.name}",
            ctx_module=module.name,
            dtc_code=dtc_code,
        )

        before = self.read_dtc(module.name)
        try:
            session = self._session_for(module)
            self._ensure_extended_session(session, module)
            session.clear_diagnostic_information(group)
        except MajsterError as exc:
            payload = exc.to_dict()
            payload["module"] = module.name
            if isinstance(exc, UdsNegativeResponse) and exc.nrc == 0x22:
                payload["hint"] = (
                    "conditionsNotCorrect usually means the engine is running, "
                    "the vehicle is moving, or the module requires an extended "
                    "diagnostic session. Stop the engine, switch the ignition to "
                    "position II, and try again."
                )
            return payload

        after = self.read_dtc(module.name)
        cleared = int(before.get("count", 0)) - int(after.get("count", 0))
        return {
            "ok": True,
            "operation": operation,
            "module": module.name,
            "scope": dtc_code.upper() if dtc_code else "all DTCs",
            "dtcs_before": before.get("dtcs", []),
            "dtcs_after": after.get("dtcs", []),
            "cleared_count": max(cleared, 0),
            "summary": (
                f"Cleared {max(cleared, 0)} DTC(s) from {module.name}. "
                f"{after.get('count', 0)} code(s) remain - codes that return "
                f"immediately indicate a fault that is still present."
            ),
            "next_step": (
                "Re-read the DTCs after a drive cycle. A code that comes straight "
                "back has an active root cause that clearing will not fix."
            ),
        }

    def _describe_clear_impact(self, module: EcuModule, dtc_code: str | None) -> dict[str, Any]:
        """Everything a human needs to decide whether to approve a clear."""
        current = self.read_dtc(module.name)
        doomed = current.get("dtcs", [])
        if dtc_code:
            wanted = dtc_code.upper().split("-")[0]
            doomed = [d for d in doomed if d["code"] == wanted]

        risks = [
            "Freeze-frame data captured when the fault occurred will be lost. "
            "That data is often the most useful evidence you have.",
            "Readiness monitors reset. The vehicle may fail an emissions test "
            "until a full drive cycle completes.",
            "Clearing does not repair anything. If the fault is still present the "
            "code will return.",
        ]
        if not module.verified:
            risks.append(
                f"The address for {module.name} (0x{module.request_id:03X}) is "
                f"UNVERIFIED for this vehicle. Confirm it with scan_modules() "
                f"before writing to it."
            )
        if module.name in {"RCM", "ABS", "PBM"}:
            risks.append(
                f"{module.name} is a SAFETY-CRITICAL system. Clearing its faults "
                f"can hide a genuine defect in the brakes, restraints or park brake. "
                f"Do not do this unless the repair is complete."
            )

        return {
            "module": module.name,
            "module_description": module.description,
            "address": f"0x{module.request_id:03X}",
            "address_verified": module.verified,
            "scope": dtc_code.upper() if dtc_code else "ALL stored DTCs in this module",
            "dtcs_that_will_be_erased": doomed,
            "count": len(doomed),
            "risks": risks,
            "reversible": False,
        }

    def pending_confirmations(self) -> list[dict[str, Any]]:
        """Outstanding approval requests -- used by the agent's HITL node."""
        self._prune_expired()
        return [
            {
                "token": p.token,
                "operation": p.operation,
                "impact": p.summary,
                "expires_in_seconds": round(p.expires_at - self._clock(), 1),
            }
            for p in self._pending.values()
        ]

    def status(self) -> dict[str, Any]:
        """Interface and safety status -- what ``doctor`` reports."""
        return {
            "ok": True,
            "safety_mode": self.settings.safety_mode.value,
            "write_enabled": self.settings.write_enabled,
            "require_approval": self.settings.require_approval,
            "interface": self.factory.describe(),
            "modules_known": len(self.modules),
            "modules_verified": len(self.modules.verified()),
            "signals_known": len(self.signals),
            "open_sessions": sorted(self._sessions),
            "pending_confirmations": len(self._pending),
        }


__all__ = ["CarInterfaceService", "PendingWrite", "CONFIRMATION_TTL_SECONDS"]

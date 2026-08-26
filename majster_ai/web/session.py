"""Bridging one WebSocket connection to the diagnostic agent and the vehicle.

Two long-running concerns share a connection:

* a **telemetry poller** reading live signals on a fixed cadence, and
* the **agent**, which runs a whole multi-step turn in a worker thread.

Both reach the same CAN bus, so both go through
:class:`~majster_ai.mcp_servers.car_interface.service.CarInterfaceService`,
whose reentrant lock serialises the exchanges. The poller yields while the
agent works: a diagnostic answer matters more than a gauge refreshing on time.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Awaitable, Callable, Iterable, Sequence

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel

from majster_ai import PROJECT_NAME, TARGET_VEHICLE, __version__
from majster_ai.agent.runner import DiagnosticSession
from majster_ai.agent.toolkit import Toolkit, build_local_toolkit
from majster_ai.config import Settings, get_settings
from majster_ai.errors import MajsterError
from majster_ai.logging_setup import get_logger
from majster_ai.mcp_servers.car_interface.service import CarInterfaceService
from majster_ai.web.approvals import WebSocketApprover
from majster_ai.web.protocol import (
    AgentMessageFrame,
    AgentState,
    AgentStatusFrame,
    AgentToolFrame,
    ApprovalResolvedFrame,
    Citation,
    ErrorFrame,
    HelloFrame,
    InterfaceInfo,
    ModuleHealth,
    ModuleState,
    ModulesFrame,
    SignalReading,
    TelemetryFrame,
)

log = get_logger("web.session")

#: Signals the dashboard streams. The first three drive the radial gauges; the
#: rest feed the live plot and the detail readout.
TELEMETRY_SIGNALS: tuple[str, ...] = (
    "RPM",
    "THROTTLE_POS",
    "MODULE_VOLTAGE",
    "COOLANT_TEMP",
    "MAP",
    "BAROMETRIC_PRESSURE",
    "MAF",
    "ENGINE_LOAD",
    "SPEED",
    "INTAKE_TEMP",
    "FUEL_RAIL_PRESSURE",
    "OIL_TEMP",
)

#: Default telemetry cadence. Fast enough to feel live, slow enough to leave
#: the bus available for the agent.
DEFAULT_TELEMETRY_INTERVAL = 0.5

#: Modules polled for health. Reading every mapped address on every refresh
#: would spend most of the cycle waiting for absent ones to time out.
DEFAULT_HEALTH_MODULES: tuple[str, ...] = ("ECM", "TCM", "ABS", "HALDEX")

Emitter = Callable[[BaseModel], Awaitable[None]]


class DiagnosticHub:
    """Process-wide services shared by every connection.

    One vehicle, one bus, one set of services. Connections get their own
    conversation, but they all talk to the same car.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        car: CarInterfaceService | None = None,
        toolkit_factory: Callable[[], Toolkit] | None = None,
        health_modules: Sequence[str] = DEFAULT_HEALTH_MODULES,
        telemetry_interval: float = DEFAULT_TELEMETRY_INTERVAL,
    ) -> None:
        self.settings = settings or get_settings()
        self.car = car or CarInterfaceService(self.settings)
        self.health_modules = tuple(health_modules)
        self.telemetry_interval = max(telemetry_interval, 0.1)
        self._toolkit_factory = toolkit_factory
        self._modules: list[ModuleState] = []
        self._modules_lock = asyncio.Lock()

    # -- toolkit ------------------------------------------------------------
    def build_toolkit(self) -> Toolkit:
        """A toolkit sharing this hub's car service.

        The RAG and web servers are per-connection but stateless enough that it
        does not matter; the car service must be shared, because it owns the
        bus lock.
        """
        if self._toolkit_factory is not None:
            return self._toolkit_factory()
        return build_local_toolkit(self.settings, car=self.car)

    # -- state --------------------------------------------------------------
    def interface_info(self) -> InterfaceInfo:
        described = self.car.factory.describe()
        return InterfaceInfo(
            backend=str(described["backend"]),
            channel=str(described["channel"]),
            bitrate=int(described["bitrate"]),
            physical=bool(described["physical"]),
            safety_mode=self.settings.safety_mode.value,
            write_enabled=self.settings.write_enabled,
            require_approval=self.settings.require_approval,
        )

    def known_modules(self) -> list[ModuleState]:
        """Every mapped module, unqueried."""
        return [
            ModuleState(
                name=module.name,
                description=module.description,
                address=f"0x{module.request_id:03X}",
                verified=module.verified,
            )
            for module in self.car.modules
        ]

    async def refresh_modules(self, names: Iterable[str] | None = None) -> list[ModuleState]:
        """Read DTCs from the health modules and rebuild the status list."""
        wanted = tuple(names) if names is not None else self.health_modules
        async with self._modules_lock:
            states = {state.name: state for state in (self._modules or self.known_modules())}
            for name in wanted:
                result = await asyncio.to_thread(self.car.read_dtc, name)
                state = states.get(name)
                if state is None:
                    continue
                if result.get("ok"):
                    dtcs = list(result.get("dtcs", []))
                    states[name] = state.model_copy(
                        update={
                            "health": ModuleHealth.FAULT if dtcs else ModuleHealth.ONLINE,
                            "dtc_count": len(dtcs),
                            "dtcs": dtcs,
                            "detail": str(result.get("summary", "")),
                        }
                    )
                else:
                    states[name] = state.model_copy(
                        update={
                            "health": ModuleHealth.OFFLINE,
                            "dtc_count": 0,
                            "dtcs": [],
                            "detail": str(result.get("message", "No response.")),
                        }
                    )
            self._modules = list(states.values())
            return list(self._modules)

    @property
    def modules(self) -> list[ModuleState]:
        return list(self._modules or self.known_modules())

    async def read_telemetry(self) -> list[SignalReading]:
        """One telemetry frame."""
        result = await asyncio.to_thread(self.car.read_live_data, list(TELEMETRY_SIGNALS), "ECM")
        readings: list[SignalReading] = []
        for entry in result.get("values", []):
            readings.append(
                SignalReading(
                    signal=str(entry["signal"]),
                    value=entry.get("value"),
                    unit=str(entry.get("unit", "")),
                    description=str(entry.get("description", "")),
                    warning=entry.get("warning"),
                    verified_scaling=bool(entry.get("verified_scaling", True)),
                )
            )
        return readings

    def close(self) -> None:
        self.car.close()


def _extract_citations(tool_name: str, result: Any) -> list[Citation]:
    """Pull traceable sources out of a tool result.

    The point of the citation cards in the UI is that a mechanic can check a
    claim before undoing a bolt, so this only reports what the tool actually
    returned -- never an inferred source.
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return []

    citations: list[Citation] = []
    if tool_name == "search_manual":
        for entry in result.get("results", []):
            citations.append(
                Citation(
                    kind="manual",
                    label=str(entry.get("citation", "workshop manual")),
                    detail=str(entry.get("text", ""))[:1200],
                    score=entry.get("score"),
                )
            )
    elif tool_name == "search_web":
        for entry in result.get("results", []):
            citations.append(
                Citation(
                    kind="web",
                    label=str(entry.get("title", entry.get("url", "web result"))),
                    detail=str(entry.get("snippet", ""))[:800],
                    url=entry.get("url"),
                    score=entry.get("score"),
                )
            )
    elif tool_name in ("read_dtc", "read_all_dtcs", "read_live_data", "read_did"):
        summary = str(result.get("summary", "")).strip()
        if summary:
            citations.append(
                Citation(
                    kind="vehicle",
                    label=f"{result.get('module', 'vehicle')} - live read",
                    detail=summary,
                )
            )
    return citations


def instrument_toolkit(
    toolkit: Toolkit, on_event: Callable[[str, dict[str, Any], Any], None]
) -> Toolkit:
    """Wrap every tool so the UI can show what the agent is doing, live.

    Without this the interface sits silent through a multi-step turn and then
    produces an answer, which reads as a hang. Each tool keeps its name,
    description and schema, so the model sees exactly the same surface.
    """
    wrapped: list[BaseTool] = []
    for tool in toolkit.tools:
        wrapped.append(_wrap_tool(tool, on_event))
    toolkit.tools = wrapped
    return toolkit


def _wrap_tool(tool: BaseTool, on_event: Callable[[str, dict[str, Any], Any], None]) -> BaseTool:
    name = tool.name
    inner = tool

    def call(**kwargs: Any) -> Any:
        result = inner.invoke(kwargs)
        try:
            on_event(name, dict(kwargs), result)
        except Exception:  # pragma: no cover - telemetry must never break a tool
            log.debug("Tool event hook failed for %s", name, exc_info=True)
        return result

    return StructuredTool.from_function(
        func=call,
        name=name,
        description=tool.description,
        args_schema=tool.args_schema,
        handle_tool_error=False,
    )


class ConnectionSession:
    """One connected client: its agent, its telemetry loop, its approvals."""

    def __init__(
        self,
        hub: DiagnosticHub,
        emit: Emitter,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        telemetry_interval: float | None = None,
    ) -> None:
        self.hub = hub
        self._emit = emit
        self._loop = loop or asyncio.get_event_loop()
        self._interval = telemetry_interval or hub.telemetry_interval
        self._telemetry_task: asyncio.Task[None] | None = None
        self._agent_busy = asyncio.Lock()
        self._closed = False
        self._last_readings: list[SignalReading] = []

        self.approver = WebSocketApprover(
            self._emit_threadsafe,
            timeout=hub.settings.approval_timeout,
            on_state=self._approval_state_changed,
        )
        self._toolkit: Toolkit | None = None
        self._agent: DiagnosticSession | None = None
        self._tool_events: list[tuple[str, dict[str, Any], Any]] = []

    # -- emitting from other threads ----------------------------------------
    def _emit_threadsafe(self, frame: BaseModel) -> None:
        """Put a frame on the socket from the agent's worker thread."""
        if self._closed:
            return
        asyncio.run_coroutine_threadsafe(self._emit(frame), self._loop)

    def _approval_state_changed(self, waiting: bool) -> None:
        self._emit_threadsafe(
            AgentStatusFrame(
                state=AgentState.AWAITING_APPROVAL if waiting else AgentState.THINKING,
                detail=(
                    "Waiting for the operator to authorise a write." if waiting else "Continuing."
                ),
            )
        )

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        """Send the opening state and start streaming telemetry."""
        modules = await self.hub.refresh_modules()
        await self._emit(
            HelloFrame(
                project=PROJECT_NAME,
                version=__version__,
                vehicle=TARGET_VEHICLE,
                interface=self.hub.interface_info(),
                modules=modules,
                telemetry_signals=list(TELEMETRY_SIGNALS),
                telemetry_interval_ms=int(self._interval * 1000),
            )
        )
        await self._emit(
            ModulesFrame(
                modules=modules,
                total_dtcs=sum(module.dtc_count for module in modules),
            )
        )
        self._telemetry_task = asyncio.create_task(self._telemetry_loop())

    async def close(self) -> None:
        self._closed = True
        # A pending write must not outlive the operator who was being asked.
        self.approver.cancel("the operator's connection closed")
        if self._telemetry_task is not None:
            self._telemetry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._telemetry_task
            self._telemetry_task = None
        if self._toolkit is not None:
            # The car service belongs to the hub and is deliberately not closed
            # here: other connections are still using it.
            self._toolkit.car = None  # type: ignore[assignment]
            self._toolkit._closers.clear()
            self._toolkit = None
        self._agent = None

    # -- telemetry ----------------------------------------------------------
    async def _telemetry_loop(self) -> None:
        consecutive_failures = 0
        while not self._closed:
            started = time.monotonic()
            try:
                if self._agent_busy.locked():
                    # Yield the bus: an answer matters more than a gauge tick.
                    await asyncio.sleep(self._interval)
                    continue
                readings = await self.hub.read_telemetry()
                if readings:
                    self._last_readings = readings
                    consecutive_failures = 0
                    await self._emit(TelemetryFrame(readings=readings))
                else:
                    consecutive_failures += 1
                    await self._emit(TelemetryFrame(readings=self._last_readings, stale=True))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_failures += 1
                log.warning("Telemetry poll failed: %s", exc)
                await self._emit(TelemetryFrame(readings=self._last_readings, stale=True))

            # Back off when the vehicle is not answering, so a disconnected
            # interface does not spin the bus at full rate.
            delay = self._interval * min(2 ** min(consecutive_failures, 4), 16)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(delay - elapsed, 0.05))

    # -- agent --------------------------------------------------------------
    def _ensure_agent(self) -> DiagnosticSession:
        if self._agent is not None:
            return self._agent

        toolkit = self.hub.build_toolkit()
        instrument_toolkit(toolkit, self._record_tool_event)
        self._toolkit = toolkit
        self._agent = DiagnosticSession(
            settings=self.hub.settings, toolkit=toolkit, approver=self.approver
        )
        return self._agent

    def _record_tool_event(self, name: str, arguments: dict[str, Any], result: Any) -> None:
        self._tool_events.append((name, arguments, result))
        ok = bool(result.get("ok")) if isinstance(result, dict) else True
        summary = ""
        if isinstance(result, dict):
            summary = str(result.get("summary") or result.get("message") or "")[:400]
        self._emit_threadsafe(
            AgentToolFrame(tool=name, arguments=arguments, ok=ok, summary=summary)
        )

    async def handle_chat(self, text: str) -> None:
        """Run one agent turn, streaming status, tool calls and the answer."""
        if self._agent_busy.locked():
            await self._emit(
                ErrorFrame(
                    code="agent_busy",
                    message="The agent is still working on the previous question.",
                )
            )
            return

        async with self._agent_busy:
            await self._emit(AgentMessageFrame(role="user", text=text))
            await self._emit(
                AgentStatusFrame(state=AgentState.THINKING, detail="Reading the vehicle.")
            )
            self._tool_events.clear()

            try:
                agent = await asyncio.to_thread(self._ensure_agent)
                result = await asyncio.to_thread(agent.ask, text)
            except MajsterError as exc:
                await self._emit(AgentStatusFrame(state=AgentState.ERROR, detail=exc.message))
                await self._emit(ErrorFrame(code=exc.code, message=exc.message))
                return
            except Exception as exc:
                log.exception("Agent turn failed")
                await self._emit(AgentStatusFrame(state=AgentState.ERROR, detail=str(exc)))
                await self._emit(
                    ErrorFrame(code="agent_failed", message=f"{type(exc).__name__}: {exc}")
                )
                return

            citations: list[Citation] = []
            for name, _arguments, tool_result in self._tool_events:
                citations.extend(_extract_citations(name, tool_result))

            await self._emit(
                AgentMessageFrame(
                    role="assistant",
                    text=result.answer,
                    citations=citations[:12],
                    tools_used=result.tools_used,
                )
            )

            for approval in result.approvals:
                decision = approval.get("decision", {})
                await self._emit(
                    ApprovalResolvedFrame(
                        approval_id=str(decision.get("approval_id", "")),
                        approved=bool(decision.get("approved")),
                        reason=str(decision.get("reason", "")),
                    )
                )

            # A write may have changed what is stored: refresh the panel.
            if any(name == "clear_dtc" for name, _a, _r in self._tool_events):
                modules = await self.hub.refresh_modules()
                await self._emit(
                    ModulesFrame(
                        modules=modules,
                        total_dtcs=sum(module.dtc_count for module in modules),
                    )
                )

            await self._emit(AgentStatusFrame(state=AgentState.IDLE))

    async def handle_approval(self, approval_id: str, approved: bool) -> None:
        """Relay the operator's decision to the waiting agent thread."""
        accepted = self.approver.submit(
            approval_id,
            approved,
            reason="approved in the web UI" if approved else "declined in the web UI",
        )
        if not accepted:
            await self._emit(
                ErrorFrame(
                    code="unknown_approval",
                    message=(
                        "That approval request is no longer open. It may have "
                        "expired or already been answered. Nothing was written."
                    ),
                )
            )
            return
        await self._emit(ApprovalResolvedFrame(approval_id=approval_id, approved=approved))

    async def handle_refresh(self, names: list[str] | None = None) -> None:
        modules = await self.hub.refresh_modules(names)
        await self._emit(
            ModulesFrame(modules=modules, total_dtcs=sum(module.dtc_count for module in modules))
        )


__all__ = [
    "DiagnosticHub",
    "ConnectionSession",
    "TELEMETRY_SIGNALS",
    "DEFAULT_TELEMETRY_INTERVAL",
    "DEFAULT_HEALTH_MODULES",
    "instrument_toolkit",
]

"""FastAPI application serving the Majster-AI Cyber-HUD.

Endpoints
---------
``GET  /api/health``        liveness and the effective safety posture
``GET  /api/state``         everything the UI needs to render its first frame
``GET  /api/modules``       module health, optionally refreshed
``GET  /api/signals``       the live-data catalogue
``WS   /ws/diagnostics``    the live channel: telemetry, agent, approvals

Binding
-------
The server defaults to ``127.0.0.1``. Anything that can open this socket can
ask the agent to propose a write to a vehicle -- the approval gate still holds,
but the approval prompt would then be answered by whoever reached the port. Use
``--host 0.0.0.0`` only on a network you control, and read docs/SAFETY.md first.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from majster_ai import PROJECT_NAME, TARGET_VEHICLE, __version__
from majster_ai.config import Settings, get_settings
from majster_ai.logging_setup import configure_logging, get_logger
from majster_ai.web.protocol import (
    ApprovalResponseCommand,
    ChatCommand,
    ErrorFrame,
    PingCommand,
    PongFrame,
    RefreshCommand,
)
from majster_ai.web.session import ConnectionSession, DiagnosticHub

log = get_logger("web.app")

#: Where the built frontend lands. Served only if it has been built.
FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"

#: Dev-server origins allowed through CORS. The API is localhost-only by
#: default, so this is about Vite's port, not about opening the app up.
DEV_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
)

#: Client frames, by discriminator.
_COMMANDS: dict[str, type[BaseModel]] = {
    "chat": ChatCommand,
    "approval.response": ApprovalResponseCommand,
    "refresh": RefreshCommand,
    "ping": PingCommand,
}


def create_app(settings: Settings | None = None, *, hub: DiagnosticHub | None = None) -> FastAPI:
    """Build the application.

    Args:
        settings: Effective configuration. Defaults to the process settings.
        hub: Injected service hub, used by tests to supply a simulated vehicle.
    """
    settings = settings or get_settings()
    app = FastAPI(
        title=f"{PROJECT_NAME} API",
        version=__version__,
        description=(
            "Live diagnostic channel for the Majster-AI Cyber-HUD. "
            "Vehicle writes remain gated by the human-in-the-loop approval "
            "handshake; this API cannot bypass it."
        ),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(DEV_ORIGINS),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    app.state.settings = settings
    app.state.hub = hub

    def get_hub() -> DiagnosticHub:
        if app.state.hub is None:
            app.state.hub = DiagnosticHub(settings)
        return app.state.hub

    _register_rest(app, get_hub)
    _register_websocket(app, get_hub)
    _register_frontend(app)

    return app


def _safe_asset(path: str) -> Path | None:
    """Resolve a request path to a file inside the frontend bundle, or None.

    The containment check is the whole point. Joining an untrusted path onto a
    directory and calling ``is_file()`` on the result is an arbitrary file read:
    ``/../../.env`` resolves happily outside the bundle, and that file holds the
    API keys. Resolve first, then require the result to sit under the bundle
    root -- which also closes the symlink route, since ``resolve()`` follows
    links before the check.
    """
    if not path:
        return None
    try:
        root = FRONTEND_DIST.resolve(strict=False)
        candidate = (FRONTEND_DIST / path).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):  # loops, bad encodings
        return None
    if candidate == root or not candidate.is_relative_to(root):
        return None
    return candidate if candidate.is_file() else None


def _register_rest(app: FastAPI, get_hub: Any) -> None:
    """Read-only JSON endpoints, for a first paint without the socket."""

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        """Liveness plus the safety posture, so the UI can label itself."""
        active = get_hub()
        return {
            "ok": True,
            "project": PROJECT_NAME,
            "version": __version__,
            "vehicle": TARGET_VEHICLE,
            "interface": active.interface_info().model_dump(),
        }

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        """Everything needed for a first paint without opening the socket."""
        active = get_hub()
        modules = await active.refresh_modules()
        return {
            "ok": True,
            "project": PROJECT_NAME,
            "version": __version__,
            "vehicle": TARGET_VEHICLE,
            "interface": active.interface_info().model_dump(),
            "modules": [module.model_dump() for module in modules],
        }

    @app.get("/api/modules")
    async def modules(refresh: bool = False) -> dict[str, Any]:
        active = get_hub()
        found = await active.refresh_modules() if refresh else active.modules
        return {
            "ok": True,
            "modules": [module.model_dump() for module in found],
            "total_dtcs": sum(module.dtc_count for module in found),
        }

    @app.get("/api/signals")
    async def signals() -> dict[str, Any]:
        return get_hub().car.list_signals()


def _register_websocket(app: FastAPI, get_hub: Any) -> None:
    """The live channel: telemetry, agent turns and approvals."""

    @app.websocket("/ws/diagnostics")
    async def diagnostics(websocket: WebSocket) -> None:
        await websocket.accept()
        active = get_hub()
        loop = asyncio.get_running_loop()
        send_lock = asyncio.Lock()

        async def emit(frame: BaseModel) -> None:
            # Serialise sends: the telemetry loop and the agent thread both
            # emit, and interleaved writes corrupt a WebSocket frame.
            async with send_lock:
                try:
                    await websocket.send_text(frame.model_dump_json())
                except (WebSocketDisconnect, RuntimeError):
                    raise
                except Exception:  # pragma: no cover - transport-level noise
                    log.debug("Dropping a frame for a closing socket", exc_info=True)

        session = ConnectionSession(active, emit, loop=loop)
        log.info("Client connected to /ws/diagnostics")

        try:
            await session.start()
            while True:
                raw = await websocket.receive_text()
                await _dispatch(session, emit, raw)
        except WebSocketDisconnect:
            log.info("Client disconnected from /ws/diagnostics")
        except Exception:
            log.exception("WebSocket session failed")
        finally:
            await session.close()


def _register_frontend(app: FastAPI) -> None:
    """Serve the built UI, or explain how to build it."""
    if not FRONTEND_DIST.is_dir():  # pragma: no cover - depends on the build

        @app.get("/")
        async def index_missing() -> JSONResponse:
            return JSONResponse(
                {
                    "ok": False,
                    "message": (
                        "The frontend has not been built. Run 'npm install && "
                        "npm run build' in ./frontend, or 'npm run dev' for the "
                        "Vite dev server on port 5173."
                    ),
                    "api": ["/api/health", "/api/state", "/ws/diagnostics"],
                },
                status_code=503,
            )

        return

    assets = FRONTEND_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", response_model=None)
    async def index() -> FileResponse:
        return FileResponse(FRONTEND_DIST / "index.html")

    # response_model=None: the union return type is not a Pydantic field,
    # and FastAPI would otherwise refuse to build the route.
    @app.get("/{path:path}", response_model=None)
    async def spa(path: str) -> FileResponse | JSONResponse:
        # Serve real files; fall back to index.html for client-side routes.
        if path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        resolved = _safe_asset(path)
        if resolved is not None:
            return FileResponse(resolved)
        return FileResponse(FRONTEND_DIST / "index.html")


async def _dispatch(session: ConnectionSession, emit: Any, raw: str) -> None:
    """Parse and route one client frame.

    A malformed frame is answered with an error and the connection stays open:
    dropping the socket would take the telemetry stream and any pending
    approval down with it.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        await emit(ErrorFrame(code="bad_json", message="Frame was not valid JSON."))
        return

    if not isinstance(payload, dict):
        await emit(ErrorFrame(code="bad_frame", message="Frame must be a JSON object."))
        return

    kind = payload.get("type")
    model = _COMMANDS.get(str(kind))
    if model is None:
        await emit(
            ErrorFrame(
                code="unknown_command",
                message=f"Unknown frame type {kind!r}. Expected one of "
                f"{', '.join(sorted(_COMMANDS))}.",
            )
        )
        return

    try:
        command = model.model_validate(payload)
    except ValidationError as exc:
        await emit(
            ErrorFrame(
                code="invalid_command",
                message=f"Malformed {kind!r} frame: {exc.error_count()} problem(s).",
            )
        )
        return

    if isinstance(command, ChatCommand):
        # Detached: a turn can run for a long time and the socket must stay
        # responsive, not least so the approval answer can get back in.
        asyncio.create_task(session.handle_chat(command.text))
    elif isinstance(command, ApprovalResponseCommand):
        await session.handle_approval(command.approval_id, command.approved)
    elif isinstance(command, RefreshCommand):
        asyncio.create_task(session.handle_refresh(command.modules))
    elif isinstance(command, PingCommand):
        await emit(PongFrame())


def run(
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    settings: Settings | None = None,
    reload: bool = False,
) -> None:  # pragma: no cover - process entry point
    """Start the server with uvicorn."""
    import uvicorn

    settings = settings or get_settings()
    configure_logging(settings)

    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "Binding to %s exposes the diagnostic API beyond this machine. "
            "Anything that can reach it can ask the agent to propose a vehicle "
            "write, and the approval prompt would be answered by whoever is "
            "there. Only do this on a network you control.",
            host,
        )

    uvicorn.run(
        "majster_ai.web.app:create_app" if reload else create_app(settings),
        host=host,
        port=port,
        reload=reload,
        factory=reload,
        log_level=settings.log_level.lower(),
    )


__all__ = ["create_app", "run", "FRONTEND_DIST", "DEV_ORIGINS"]

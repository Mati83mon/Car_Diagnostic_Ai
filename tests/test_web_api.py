"""The FastAPI layer, and above all the approval gate over a WebSocket.

The browser becomes another Approver. What must remain true is that it is only
an *answering* device: it can say yes to a question the server chose to ask,
and it can never mint the credential that authorises a write.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

import pytest
from fakes import ScriptedChatModel, tool_call
from fastapi.testclient import TestClient

from majster_ai.agent.toolkit import build_local_toolkit
from majster_ai.mcp_servers.car_interface.backends import TransportFactory
from majster_ai.mcp_servers.car_interface.service import CarInterfaceService
from majster_ai.web.app import create_app
from majster_ai.web.session import DiagnosticHub

CLEAR_SCRIPT = [
    [tool_call("clear_dtc", {"module_id": "ECM"})],
    "Done - codes cleared.",
]
READ_SCRIPT = [
    [tool_call("read_dtc", {"module_id": "ECM"})],
    "Three codes stored in the ECM.",
]


def build_hub(settings, vehicle, script: list[Any] | None = None) -> DiagnosticHub:
    """A hub bound to the simulated vehicle and a scripted model."""
    car = CarInterfaceService(settings, factory=TransportFactory(settings, vehicle))

    def toolkit_factory():
        return build_local_toolkit(settings, car=car, include_rag=False, include_web=False)

    hub = DiagnosticHub(settings, car=car, toolkit_factory=toolkit_factory, telemetry_interval=0.1)
    hub._script = list(script or [])  # type: ignore[attr-defined]
    return hub


@pytest.fixture
def make_client(monkeypatch):
    """Build a TestClient whose agent uses a scripted model."""

    def factory(settings, vehicle, script: list[Any] | None = None) -> Iterator[TestClient]:
        hub = build_hub(settings, vehicle, script)

        # The session builds its own DiagnosticSession; give it our model.
        import majster_ai.web.session as session_module

        real_session = session_module.DiagnosticSession

        def patched(*args: Any, **kwargs: Any):
            kwargs["llm"] = ScriptedChatModel(list(script or []))
            return real_session(*args, **kwargs)

        monkeypatch.setattr(session_module, "DiagnosticSession", patched)
        return TestClient(create_app(settings, hub=hub))

    return factory


def collect(ws, count: int, *, want: str | None = None, limit: int = 80) -> list[dict]:
    """Read frames until ``count`` of type ``want`` have arrived.

    Frames already consumed by an earlier call are gone, so ``count`` is what
    is expected *from here on*. Asking for more than will arrive burns through
    ``limit`` telemetry frames before giving up, which shows up as a very slow
    test rather than a failing one -- so count what actually remains.
    """
    seen: list[dict] = []
    matched = 0
    for _ in range(limit):
        frame = json.loads(ws.receive_text())
        seen.append(frame)
        if want is None or frame.get("type") == want:
            matched += 1
            if matched >= count:
                break
    return seen


def first(frames: list[dict], kind: str) -> dict | None:
    return next((f for f in frames if f.get("type") == kind), None)


class TestRest:
    def test_health(self, settings, vehicle, make_client) -> None:
        with make_client(settings, vehicle) as client:
            payload = client.get("/api/health").json()
        assert payload["ok"] is True
        assert payload["interface"]["safety_mode"] == "read_only"

    def test_health_reports_the_simulator_honestly(self, settings, vehicle, make_client) -> None:
        # A mechanic believing synthetic readings came from the car is the
        # worst outcome this interface can produce.
        with make_client(settings, vehicle) as client:
            payload = client.get("/api/health").json()
        assert payload["interface"]["physical"] is False
        assert payload["interface"]["backend"] == "virtual"

    def test_state_includes_modules(self, settings, vehicle, make_client) -> None:
        with make_client(settings, vehicle) as client:
            payload = client.get("/api/state").json()
        names = {module["name"] for module in payload["modules"]}
        assert {"ECM", "TCM", "ABS", "HALDEX"} <= names

    def test_modules_report_health_and_faults(self, settings, vehicle, make_client) -> None:
        with make_client(settings, vehicle) as client:
            payload = client.get("/api/modules?refresh=true").json()
        ecm = next(m for m in payload["modules"] if m["name"] == "ECM")
        assert ecm["health"] == "fault"
        assert ecm["dtc_count"] == 3
        assert payload["total_dtcs"] >= 3

    def test_unverified_addresses_are_flagged(self, settings, vehicle, make_client) -> None:
        with make_client(settings, vehicle) as client:
            payload = client.get("/api/state").json()
        by_name = {m["name"]: m for m in payload["modules"]}
        assert by_name["ECM"]["verified"] is True
        assert by_name["HALDEX"]["verified"] is False

    def test_signals(self, settings, vehicle, make_client) -> None:
        with make_client(settings, vehicle) as client:
            assert client.get("/api/signals").json()["count"] > 20


class TestWebSocketBasics:
    def test_hello_frame(self, settings, vehicle, make_client) -> None:
        with make_client(settings, vehicle) as client:
            with client.websocket_connect("/ws/diagnostics") as ws:
                hello = json.loads(ws.receive_text())
        assert hello["type"] == "hello"
        assert hello["vehicle"].startswith("Land Rover Freelander 2")
        assert hello["interface"]["physical"] is False
        assert "RPM" in hello["telemetry_signals"]

    def test_telemetry_streams(self, settings, vehicle, make_client) -> None:
        with make_client(settings, vehicle) as client:
            with client.websocket_connect("/ws/diagnostics") as ws:
                frames = collect(ws, 1, want="telemetry")
        telemetry = first(frames, "telemetry")
        assert telemetry is not None
        readings = {r["signal"]: r["value"] for r in telemetry["readings"]}
        assert readings["RPM"] == 812.0
        assert readings["COOLANT_TEMP"] == 88

    def test_ping_pong(self, settings, vehicle, make_client) -> None:
        with make_client(settings, vehicle) as client:
            with client.websocket_connect("/ws/diagnostics") as ws:
                ws.send_text(json.dumps({"type": "ping"}))
                assert first(collect(ws, 1, want="pong"), "pong") is not None

    def test_refresh(self, settings, vehicle, make_client) -> None:
        with make_client(settings, vehicle) as client:
            with client.websocket_connect("/ws/diagnostics") as ws:
                ws.send_text(json.dumps({"type": "refresh"}))
                modules = [f for f in collect(ws, 2, want="modules") if f["type"] == "modules"]
        assert modules and modules[-1]["total_dtcs"] >= 3

    @pytest.mark.parametrize(
        "raw",
        [
            "not json at all",
            '"a string"',
            '{"type":"nonsense"}',
            '{"type":"chat"}',
            '{"type":"approval.response"}',
        ],
    )
    def test_malformed_frames_are_answered_not_fatal(
        self, settings, vehicle, make_client, raw: str
    ) -> None:
        # Dropping the socket would take the telemetry stream and any pending
        # approval down with it.
        with make_client(settings, vehicle) as client:
            with client.websocket_connect("/ws/diagnostics") as ws:
                ws.send_text(raw)
                assert first(collect(ws, 1, want="error"), "error") is not None
                ws.send_text(json.dumps({"type": "ping"}))
                assert first(collect(ws, 1, want="pong"), "pong") is not None


class TestAgentOverWebSocket:
    def test_chat_produces_an_answer(self, settings, vehicle, make_client) -> None:
        with make_client(settings, vehicle, READ_SCRIPT) as client:
            with client.websocket_connect("/ws/diagnostics") as ws:
                ws.send_text(json.dumps({"type": "chat", "text": "what faults?"}))
                frames = collect(ws, 2, want="agent.message")
        messages = [f for f in frames if f["type"] == "agent.message"]
        assert messages[0]["role"] == "user"
        assert messages[-1]["role"] == "assistant"
        assert "Three codes" in messages[-1]["text"]

    def test_tool_calls_are_streamed_live(self, settings, vehicle, make_client) -> None:
        # Without this the UI sits silent through a multi-step turn, which
        # reads as a hang.
        with make_client(settings, vehicle, READ_SCRIPT) as client:
            with client.websocket_connect("/ws/diagnostics") as ws:
                ws.send_text(json.dumps({"type": "chat", "text": "what faults?"}))
                frames = collect(ws, 1, want="agent.tool")
        tool = first(frames, "agent.tool")
        assert tool is not None and tool["tool"] == "read_dtc" and tool["ok"] is True

    def test_status_transitions(self, settings, vehicle, make_client) -> None:
        with make_client(settings, vehicle, READ_SCRIPT) as client:
            with client.websocket_connect("/ws/diagnostics") as ws:
                ws.send_text(json.dumps({"type": "chat", "text": "hi"}))
                frames = collect(ws, 2, want="agent.status")
        states = [f["state"] for f in frames if f["type"] == "agent.status"]
        assert "thinking" in states


class TestApprovalGateOverWebSocket:
    """The browser may answer the question. It may never ask it, nor forge one."""

    def test_read_only_never_asks_and_never_writes(self, settings, vehicle, make_client) -> None:
        with make_client(settings, vehicle, CLEAR_SCRIPT) as client:
            with client.websocket_connect("/ws/diagnostics") as ws:
                ws.send_text(json.dumps({"type": "chat", "text": "clear the codes"}))
                frames = collect(ws, 2, want="agent.message")
        assert first(frames, "approval.request") is None
        assert len(vehicle.get(0x7E0).dtcs) == 3

    def test_approval_request_carries_no_token(self, write_settings, vehicle, make_client) -> None:
        with make_client(write_settings, vehicle, CLEAR_SCRIPT) as client:
            with client.websocket_connect("/ws/diagnostics") as ws:
                ws.send_text(json.dumps({"type": "chat", "text": "clear the codes"}))
                request = first(collect(ws, 1, want="approval.request"), "approval.request")
                ws.send_text(
                    json.dumps(
                        {
                            "type": "approval.response",
                            "approval_id": request["approval_id"],
                            "approved": False,
                        }
                    )
                )
        assert request is not None
        serialised = json.dumps(request)
        assert "confirmation_token" not in serialised
        # The opaque id must not be a service token by another name.
        assert set(request) >= {"approval_id", "impact"} or "affected_codes" in request

    def test_request_shows_what_would_be_erased(self, write_settings, vehicle, make_client) -> None:
        with make_client(write_settings, vehicle, CLEAR_SCRIPT) as client:
            with client.websocket_connect("/ws/diagnostics") as ws:
                ws.send_text(json.dumps({"type": "chat", "text": "clear"}))
                request = first(collect(ws, 1, want="approval.request"), "approval.request")
                ws.send_text(
                    json.dumps(
                        {
                            "type": "approval.response",
                            "approval_id": request["approval_id"],
                            "approved": False,
                        }
                    )
                )
        codes = {entry["code"] for entry in request["affected_codes"]}
        assert codes == {"P0299", "P2015", "P0401"}
        assert request["reversible"] is False
        assert any("freeze-frame" in risk.lower() for risk in request["risks"])

    def test_approving_writes(self, write_settings, vehicle, make_client) -> None:
        with make_client(write_settings, vehicle, CLEAR_SCRIPT) as client:
            with client.websocket_connect("/ws/diagnostics") as ws:
                ws.send_text(json.dumps({"type": "chat", "text": "clear"}))
                request = first(collect(ws, 1, want="approval.request"), "approval.request")
                ws.send_text(
                    json.dumps(
                        {
                            "type": "approval.response",
                            "approval_id": request["approval_id"],
                            "approved": True,
                        }
                    )
                )
                collect(ws, 1, want="agent.message")
        assert vehicle.get(0x7E0).dtcs == []

    def test_declining_writes_nothing(self, write_settings, vehicle, make_client) -> None:
        with make_client(write_settings, vehicle, CLEAR_SCRIPT) as client:
            with client.websocket_connect("/ws/diagnostics") as ws:
                ws.send_text(json.dumps({"type": "chat", "text": "clear"}))
                request = first(collect(ws, 1, want="approval.request"), "approval.request")
                ws.send_text(
                    json.dumps(
                        {
                            "type": "approval.response",
                            "approval_id": request["approval_id"],
                            "approved": False,
                        }
                    )
                )
                collect(ws, 1, want="agent.message")
        assert len(vehicle.get(0x7E0).dtcs) == 3

    def test_forged_approval_id_is_rejected(self, write_settings, vehicle, make_client) -> None:
        """A client inventing an id must not be able to authorise anything."""
        with make_client(write_settings, vehicle, CLEAR_SCRIPT) as client:
            with client.websocket_connect("/ws/diagnostics") as ws:
                ws.send_text(json.dumps({"type": "chat", "text": "clear"}))
                request = first(collect(ws, 1, want="approval.request"), "approval.request")
                ws.send_text(
                    json.dumps(
                        {
                            "type": "approval.response",
                            "approval_id": "totally-made-up",
                            "approved": True,
                        }
                    )
                )
                error = first(collect(ws, 1, want="error"), "error")
                assert error["code"] == "unknown_approval"
                assert len(vehicle.get(0x7E0).dtcs) == 3, "a forged id authorised a write"
                # Close out the still-open real request.
                ws.send_text(
                    json.dumps(
                        {
                            "type": "approval.response",
                            "approval_id": request["approval_id"],
                            "approved": False,
                        }
                    )
                )
                collect(ws, 1, want="agent.message")
        assert len(vehicle.get(0x7E0).dtcs) == 3

    def test_unsolicited_approval_is_rejected(self, write_settings, vehicle, make_client) -> None:
        """No write is in flight, so no approval can be valid."""
        with make_client(write_settings, vehicle, CLEAR_SCRIPT) as client:
            with client.websocket_connect("/ws/diagnostics") as ws:
                ws.send_text(
                    json.dumps(
                        {"type": "approval.response", "approval_id": "anything", "approved": True}
                    )
                )
                error = first(collect(ws, 1, want="error"), "error")
        assert error["code"] == "unknown_approval"
        assert len(vehicle.get(0x7E0).dtcs) == 3

    def test_answering_twice_is_ignored(self, write_settings, vehicle, make_client) -> None:
        with make_client(write_settings, vehicle, CLEAR_SCRIPT) as client:
            with client.websocket_connect("/ws/diagnostics") as ws:
                ws.send_text(json.dumps({"type": "chat", "text": "clear"}))
                request = first(collect(ws, 1, want="approval.request"), "approval.request")
                payload = json.dumps(
                    {
                        "type": "approval.response",
                        "approval_id": request["approval_id"],
                        "approved": False,
                    }
                )
                ws.send_text(payload)
                collect(ws, 1, want="agent.message")
                ws.send_text(payload)  # a second, late "answer"
                error = first(collect(ws, 1, want="error"), "error")
        assert error["code"] == "unknown_approval"
        assert len(vehicle.get(0x7E0).dtcs) == 3

    def test_disconnect_cancels_a_pending_write(self, write_settings, vehicle, make_client) -> None:
        """A pending write must not outlive the human who was being asked."""
        with make_client(write_settings, vehicle, CLEAR_SCRIPT) as client:
            with client.websocket_connect("/ws/diagnostics") as ws:
                ws.send_text(json.dumps({"type": "chat", "text": "clear"}))
                assert first(collect(ws, 1, want="approval.request"), "approval.request")
                # Drop the socket without answering.
        assert len(vehicle.get(0x7E0).dtcs) == 3


class TestStaticFileSafety:
    """The SPA fallback must never serve a file from outside the bundle.

    Joining an untrusted path onto a directory and calling ``is_file()`` on the
    result is an arbitrary file read. On this project the prize is ``.env``,
    which holds the API keys.

    Note on coverage: an HTTP client normalises ``..`` out of a URL before the
    application ever sees it, so driving these through TestClient proves very
    little -- the traversal is only reachable from a client that sends an
    un-normalised path. The load-bearing tests here therefore call
    :func:`_safe_asset` directly, with paths whose depth actually escapes the
    fixture, plus a symlink case that needs no ``..`` at all.
    """

    @pytest.fixture
    def bundle(self, tmp_path, monkeypatch):
        """A fake built frontend, with secrets sitting outside it."""
        import majster_ai.web.app as app_module

        dist = tmp_path / "frontend" / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<!doctype html><title>hud</title>", encoding="utf-8")
        (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
        # dist is two levels below tmp_path: frontend/dist.
        (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-secret", encoding="utf-8")
        (tmp_path / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
        monkeypatch.setattr(app_module, "FRONTEND_DIST", dist)
        return tmp_path

    @pytest.mark.parametrize(
        "path",
        [
            "../../.env",  # -> tmp_path/.env
            "../../secret.txt",  # -> tmp_path/secret.txt
            "assets/../../../.env",  # via a real subdirectory
            "./../../.env",
            "../..//.env",
        ],
    )
    def test_escaping_paths_are_refused(self, bundle, path: str) -> None:
        from majster_ai.web.app import _safe_asset

        # Sanity: the path really does resolve onto the secret, so a failure
        # here means the guard is doing the work rather than the depth being
        # wrong -- the exact mistake that made an earlier version of this test
        # pass against vulnerable code.
        target = (bundle / "frontend" / "dist" / path).resolve()
        assert target.is_file(), f"{path!r} does not reach a real file; test is inert"
        assert _safe_asset(path) is None, f"{path!r} escaped the bundle"

    def test_symlink_out_of_the_bundle_is_refused(self, bundle) -> None:
        """No ``..`` involved, so URL normalisation cannot save us here."""
        from majster_ai.web.app import _safe_asset

        link = bundle / "frontend" / "dist" / "leak.txt"
        try:
            link.symlink_to(bundle / "secret.txt")
        except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
            pytest.skip("symlinks unavailable on this platform")
        assert link.is_file(), "symlink fixture did not resolve"
        assert _safe_asset("leak.txt") is None

    def test_legitimate_assets_resolve(self, bundle) -> None:
        from majster_ai.web.app import _safe_asset

        assert _safe_asset("assets/app.js") is not None
        assert _safe_asset("index.html") is not None

    def test_empty_and_directory_paths_resolve_to_nothing(self, bundle) -> None:
        from majster_ai.web.app import _safe_asset

        assert _safe_asset("") is None
        assert _safe_asset("assets") is None  # a directory, not a file
        assert _safe_asset("nope.js") is None  # simply absent

    def test_real_assets_are_still_served_over_http(
        self, bundle, settings, vehicle, make_client
    ) -> None:
        with make_client(settings, vehicle) as client:
            assert "console.log(1)" in client.get("/assets/app.js").text

    def test_index_is_served(self, bundle, settings, vehicle, make_client) -> None:
        with make_client(settings, vehicle) as client:
            assert "hud" in client.get("/").text

    def test_unknown_route_falls_back_to_the_spa(
        self, bundle, settings, vehicle, make_client
    ) -> None:
        with make_client(settings, vehicle) as client:
            assert "hud" in client.get("/some/client/route").text

    def test_unknown_api_route_is_404(self, bundle, settings, vehicle, make_client) -> None:
        with make_client(settings, vehicle) as client:
            assert client.get("/api/nope").status_code == 404

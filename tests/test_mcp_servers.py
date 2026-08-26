"""The three MCP servers: tool surface, schemas, and end-to-end tool calls.

Exercised through the real FastMCP server objects, so the tool descriptions
the LLM will see are the ones under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from majster_ai.agent.toolkit import run_sync
from majster_ai.config import load_settings
from majster_ai.mcp_servers.car_interface.backends import TransportFactory
from majster_ai.mcp_servers.car_interface.server import build_server as build_car_server
from majster_ai.mcp_servers.car_interface.service import CarInterfaceService
from majster_ai.mcp_servers.rag_workshop.embeddings import HashEmbeddings
from majster_ai.mcp_servers.rag_workshop.server import build_server as build_rag_server
from majster_ai.mcp_servers.rag_workshop.service import RagService
from majster_ai.mcp_servers.rag_workshop.store import InMemoryVectorStore
from majster_ai.mcp_servers.web_search.server import build_server as build_web_server
from majster_ai.mcp_servers.web_search.service import WebSearchService


def call(server, name: str, arguments: dict) -> dict:
    """Invoke a FastMCP tool and return its structured result."""
    result = run_sync(server.call_tool(name, arguments))
    if isinstance(result, tuple):
        return result[1]
    return result


def tool_names(server) -> list[str]:
    return [tool.name for tool in run_sync(server.list_tools())]


def tool_by_name(server, name: str):
    return next(tool for tool in run_sync(server.list_tools()) if tool.name == name)


def flat(text: str | None) -> str:
    """Collapse wrapped prose to one line so assertions are not line-break sensitive."""
    return " ".join((text or "").split())


class TestCarInterfaceServer:
    @pytest.fixture
    def server(self, car):
        return build_car_server(car)

    @pytest.fixture
    def write_server(self, write_car):
        return build_car_server(write_car)

    def test_exposes_the_expected_tools(self, server) -> None:
        assert set(tool_names(server)) == {
            "read_dtc",
            "read_all_dtcs",
            "read_live_data",
            "read_did",
            "scan_modules",
            "list_modules",
            "list_signals",
            "vehicle_info",
            "interface_status",
            "clear_dtc",
        }

    def test_only_one_write_tool_is_exposed(self, server) -> None:
        from majster_ai.agent.hitl import WRITE_TOOLS

        assert {name for name in tool_names(server) if name in WRITE_TOOLS} == {"clear_dtc"}

    def test_read_dtc(self, server) -> None:
        result = call(server, "read_dtc", {"module_id": "ECM"})
        assert result["ok"] is True and result["count"] == 3

    def test_read_live_data(self, server) -> None:
        result = call(server, "read_live_data", {"pid_list": ["RPM", "COOLANT_TEMP"]})
        assert len(result["values"]) == 2

    def test_scan_modules(self, server) -> None:
        assert call(server, "scan_modules", {})["ok"] is True

    def test_clear_is_refused_in_read_only(self, server, ecm) -> None:
        assert call(server, "clear_dtc", {"module_id": "ECM"})["error"] == "safety_violation"
        assert len(ecm.dtcs) == 3

    def test_clear_handshake_over_mcp(self, write_server, ecm) -> None:
        first = call(write_server, "clear_dtc", {"module_id": "ECM"})
        assert first["requires_confirmation"] is True
        assert len(ecm.dtcs) == 3
        second = call(
            write_server,
            "clear_dtc",
            {"module_id": "ECM", "confirmation_token": first["confirmation_token"]},
        )
        assert second["ok"] is True and ecm.dtcs == []

    def test_clear_schema_exposes_the_token(self, server) -> None:
        properties = tool_by_name(server, "clear_dtc").inputSchema["properties"]
        assert set(properties) == {"module_id", "dtc_code", "confirmation_token"}

    def test_clear_description_states_the_handshake(self, server) -> None:
        description = flat(tool_by_name(server, "clear_dtc").description)
        assert "THIS IS A WRITE OPERATION" in description
        assert "will REFUSE" in description
        assert "confirmation_token" in description

    def test_clear_description_warns_about_freeze_frame(self, server) -> None:
        # The model needs to know clearing destroys evidence, not just that it
        # needs a token.
        assert "freeze-frame" in flat(tool_by_name(server, "clear_dtc").description).lower()

    def test_read_tools_are_described_as_safe(self, server) -> None:
        for name in ("read_dtc", "read_live_data", "scan_modules"):
            assert "read-only" in flat(tool_by_name(server, name).description).lower()

    def test_server_instructions_state_the_safety_posture(self, server) -> None:
        instructions = flat(server.instructions)
        assert "Read tools" in instructions and "always safe" in instructions
        assert "clear_dtc is a WRITE" in instructions
        assert "requires explicit human approval" in instructions


class TestRagServer:
    @pytest.fixture
    def server(self, tmp_path: Path, manuals_dir: Path):
        settings = load_settings(
            manuals_dir=manuals_dir, vector_dir=tmp_path / "vs", log_level="CRITICAL"
        )
        embeddings = HashEmbeddings()
        service = RagService(
            settings,
            embeddings=embeddings,
            store=InMemoryVectorStore(None, embeddings.name),
        )
        return build_rag_server(service)

    def test_tools(self, server) -> None:
        assert set(tool_names(server)) == {
            "search_manual",
            "ingest_manuals",
            "manual_index_status",
            "list_manual_sources",
        }

    def test_ingest_then_search(self, server) -> None:
        assert call(server, "ingest_manuals", {})["ok"] is True
        result = call(server, "search_manual", {"query": "swirl flap", "top_k": 2})
        assert result["ok"] is True and result["count"] > 0

    def test_search_before_ingest(self, server) -> None:
        assert call(server, "search_manual", {"query": "x"})["error"] == "index_not_built"

    def test_status(self, server) -> None:
        assert call(server, "manual_index_status", {})["ok"] is True

    def test_description_demands_citations(self, server) -> None:
        assert "citation" in flat(tool_by_name(server, "search_manual").description).lower()

    def test_instructions_forbid_answering_from_general_knowledge(self, server) -> None:
        assert "do not answer from general knowledge" in flat(server.instructions).lower()


class TestWebServer:
    @pytest.fixture
    def server(self):
        return build_web_server(WebSearchService(load_settings(), providers=[]))

    def test_tools(self, server) -> None:
        assert set(tool_names(server)) == {"search_web", "web_search_status"}

    def test_status(self, server) -> None:
        assert call(server, "web_search_status", {})["ok"] is True

    def test_no_providers_is_a_clean_error(self, server) -> None:
        assert call(server, "search_web", {"query": "P0299"})["ok"] is False

    def test_description_warns_about_forum_reliability(self, server) -> None:
        description = flat(tool_by_name(server, "search_web").description)
        assert "experience" in description and "documentation" in description

    def test_instructions_rank_the_evidence_sources(self, server) -> None:
        instructions = flat(server.instructions).lower()
        assert "live vehicle data" in instructions
        assert "workshop manual" in instructions


class TestServerConstruction:
    def test_servers_build_from_process_settings(self, tmp_path: Path) -> None:
        from majster_ai.config import set_settings

        settings = load_settings(
            manuals_dir=tmp_path / "m", vector_dir=tmp_path / "v", log_level="CRITICAL"
        )
        set_settings(settings)
        try:
            assert build_car_server() is not None
            assert build_web_server() is not None
        finally:
            set_settings(None)

    def test_launch_config_names_every_server(self) -> None:
        from majster_ai.agent.toolkit import mcp_server_config

        config = mcp_server_config()
        assert set(config) == {"car_interface", "rag_workshop", "web_search"}
        for entry in config.values():
            assert entry["transport"] == "stdio"
            assert entry["args"][0] == "-m"

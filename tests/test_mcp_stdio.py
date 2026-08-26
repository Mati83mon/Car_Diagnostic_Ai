"""Integration: the MCP servers as real subprocesses over stdio.

Everything else exercises the servers in-process. This file proves the actual
deployment topology works: three separate processes, JSON-RPC over stdio,
reached the way Claude Desktop or any other MCP client would reach them.

It also guards a specific footgun -- a stray ``print()`` anywhere in the
package would corrupt the JSON-RPC stream on stdout and the client would
simply disconnect with an unhelpful error.
"""

from __future__ import annotations

import os
import sys

import pytest

from majster_ai.agent.toolkit import mcp_server_config, run_sync

pytestmark = pytest.mark.integration


def _client_env() -> dict[str, str]:
    """Environment for a spawned server: simulator, quiet, read-only."""
    env = dict(os.environ)
    env.update(
        {
            "MAJSTER_CAN_BACKEND": "virtual",
            "MAJSTER_LOG_LEVEL": "CRITICAL",
            "MAJSTER_UDS_TIMEOUT": "0.2",
            "MAJSTER_UDS_RETRIES": "0",
            "MAJSTER_WRITE_ENABLED": "false",
            "PYTHONPATH": os.pathsep.join(sys.path),
        }
    )
    return env


async def _call_over_stdio(server: str, tool: str, arguments: dict) -> dict:
    """Start one server as a subprocess and call a single tool on it."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    config = mcp_server_config()[server]
    parameters = StdioServerParameters(
        command=config["command"], args=config["args"], env=_client_env()
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            result = await session.call_tool(tool, arguments)
            return {
                "tools": [entry.name for entry in listed.tools],
                "structured": result.structuredContent,
                "is_error": result.isError,
            }


class TestCarInterfaceOverStdio:
    def test_lists_tools_and_reads_dtcs(self) -> None:
        payload = run_sync(_call_over_stdio("car_interface", "read_dtc", {"module_id": "ECM"}))
        assert "read_dtc" in payload["tools"]
        assert "clear_dtc" in payload["tools"]
        assert payload["is_error"] is False
        assert payload["structured"]["count"] == 3

    def test_write_is_refused_across_the_transport(self) -> None:
        # The safety gate lives in the server, so it holds for any MCP client,
        # not just our agent.
        payload = run_sync(_call_over_stdio("car_interface", "clear_dtc", {"module_id": "ECM"}))
        assert payload["structured"]["error"] == "safety_violation"

    def test_stdout_carries_only_json_rpc(self) -> None:
        """A stray print() in the package would corrupt the protocol stream.
        Reaching this assertion at all means the handshake parsed cleanly."""
        payload = run_sync(_call_over_stdio("car_interface", "interface_status", {}))
        assert payload["structured"]["ok"] is True


class TestWebSearchOverStdio:
    def test_status_tool(self) -> None:
        payload = run_sync(_call_over_stdio("web_search", "web_search_status", {}))
        assert payload["structured"]["ok"] is True
        assert "search_web" in payload["tools"]


class TestRagOverStdio:
    def test_status_tool(self) -> None:
        payload = run_sync(_call_over_stdio("rag_workshop", "manual_index_status", {}))
        assert payload["structured"]["ok"] is True
        assert "search_manual" in payload["tools"]

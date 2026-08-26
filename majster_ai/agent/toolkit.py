"""Bridging MCP tools into LangChain/LangGraph.

Two ways to reach the same tools:

:func:`build_local_toolkit`
    Builds the three FastMCP servers **in-process** and wraps their tool
    definitions as LangChain tools. No subprocesses, no stdio, no start-up
    cost. This is the default for the console agent and the whole test-suite.

:func:`build_mcp_toolkit`
    Spawns the three servers as separate processes and connects over stdio
    through ``langchain-mcp-adapters``. This is the real MCP deployment: each
    server isolated, restartable, and reusable by any other MCP client.

Both paths read the tool *definitions* from the same FastMCP servers, so a
tool's description can never drift between "what the test exercises" and "what
the deployed agent sees" -- the docstring in ``server.py`` is the only copy.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from langchain_core.tools import BaseTool, StructuredTool

from majster_ai.config import Settings, get_settings
from majster_ai.errors import MajsterError
from majster_ai.logging_setup import get_logger
from majster_ai.mcp_servers.car_interface.server import build_server as build_car_server
from majster_ai.mcp_servers.car_interface.service import CarInterfaceService
from majster_ai.mcp_servers.rag_workshop.server import build_server as build_rag_server
from majster_ai.mcp_servers.rag_workshop.service import RagService
from majster_ai.mcp_servers.web_search.server import build_server as build_web_server
from majster_ai.mcp_servers.web_search.service import WebSearchService

log = get_logger("agent.toolkit")


def run_sync(coroutine: Any) -> Any:
    """Run a coroutine from synchronous code, even inside a running loop.

    LangChain tools are invoked synchronously by the graph, but FastMCP's
    ``call_tool`` is async. When there is already a loop on this thread (a
    notebook, an async host) ``asyncio.run`` would raise, so the work is handed
    to a worker thread with its own loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


def _unwrap_result(result: Any) -> dict[str, Any]:
    """Normalise a FastMCP tool result into a plain dictionary.

    ``call_tool`` returns ``(content_blocks, structured_content)``. The
    structured half is what we want; the content blocks are the text rendering.
    """
    if isinstance(result, tuple) and len(result) == 2:
        _, structured = result
        if isinstance(structured, dict):
            return structured
        result = _

    if isinstance(result, dict):
        return result

    # A bare content-block sequence: recover the JSON payload from the text.
    if isinstance(result, Sequence):
        for block in result:
            text = getattr(block, "text", None)
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return {"ok": True, "result": text}
            if isinstance(parsed, dict):
                return parsed
            return {"ok": True, "result": parsed}
    return {"ok": True, "result": str(result)}


@dataclass
class Toolkit:
    """The tools available to the agent, plus the services behind them."""

    tools: list[BaseTool] = field(default_factory=list)
    car: CarInterfaceService | None = None
    rag: RagService | None = None
    web: WebSearchService | None = None
    #: Callable used to close anything that needs closing (sessions, buses).
    _closers: list[Callable[[], None]] = field(default_factory=list)

    def names(self) -> list[str]:
        return [tool.name for tool in self.tools]

    def get(self, name: str) -> BaseTool | None:
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None

    def close(self) -> None:
        for closer in self._closers:
            try:
                closer()
            except Exception:  # pragma: no cover - teardown must not raise
                log.debug("Ignoring error during toolkit teardown", exc_info=True)
        self._closers.clear()

    def __enter__(self) -> Toolkit:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _tool_from_mcp(server: Any, name: str, description: str, schema: dict[str, Any]) -> BaseTool:
    """Wrap one FastMCP tool as a LangChain ``StructuredTool``."""

    def call(**kwargs: Any) -> dict[str, Any]:
        # Drop unset optionals: FastMCP validates against the declared schema,
        # and an explicit null for an absent argument is rejected by some.
        arguments = {key: value for key, value in kwargs.items() if value is not None}
        try:
            return _unwrap_result(run_sync(server.call_tool(name, arguments)))
        except MajsterError as exc:
            return exc.to_dict()
        except Exception as exc:
            # A tool must return an error to the model, never raise into the
            # graph: the model can recover from "that failed, here is why",
            # but a traceback ends the session.
            log.exception("Tool %s failed", name)
            return {
                "ok": False,
                "error": "tool_execution_failed",
                "tool": name,
                "message": f"{type(exc).__name__}: {exc}",
            }

    return StructuredTool.from_function(
        func=call,
        name=name,
        description=description,
        args_schema=schema,
        # Errors are already returned as data; never let LangChain swallow them.
        handle_tool_error=False,
    )


def tools_from_server(server: Any) -> list[BaseTool]:
    """Convert every tool on a FastMCP server into a LangChain tool."""
    definitions = run_sync(server.list_tools())
    tools: list[BaseTool] = []
    for definition in definitions:
        schema = dict(definition.inputSchema or {"type": "object", "properties": {}})
        tools.append(
            _tool_from_mcp(
                server,
                definition.name,
                definition.description or definition.name,
                schema,
            )
        )
    return tools


def build_local_toolkit(
    settings: Settings | None = None,
    *,
    car: CarInterfaceService | None = None,
    rag: RagService | None = None,
    web: WebSearchService | None = None,
    include_rag: bool = True,
    include_web: bool = True,
) -> Toolkit:
    """Build every tool in-process, with no subprocesses.

    Services can be injected, which is how tests point the agent at a simulated
    vehicle and a temporary manual index.
    """
    settings = settings or get_settings()
    car_service = car or CarInterfaceService(settings)

    toolkit = Toolkit(car=car_service)
    toolkit.tools.extend(tools_from_server(build_car_server(car_service)))
    toolkit._closers.append(car_service.close)

    if include_rag:
        try:
            rag_service = rag or RagService(settings)
            toolkit.rag = rag_service
            toolkit.tools.extend(tools_from_server(build_rag_server(rag_service)))
        except Exception as exc:
            # A broken manual index must not prevent the agent from reading the
            # car -- that is the part that cannot be replaced by a web search.
            log.warning("RAG tools unavailable (%s) - continuing without them", exc)

    if include_web:
        try:
            web_service = web or WebSearchService(settings)
            toolkit.web = web_service
            toolkit.tools.extend(tools_from_server(build_web_server(web_service)))
        except Exception as exc:
            log.warning("Web-search tools unavailable (%s) - continuing without them", exc)

    log.info("Toolkit ready: %d tool(s) - %s", len(toolkit.tools), ", ".join(toolkit.names()))
    return toolkit


#: Command lines for the three servers when run as separate processes.
MCP_SERVER_MODULES: dict[str, str] = {
    "car_interface": "majster_ai.mcp_servers.car_interface.server",
    "rag_workshop": "majster_ai.mcp_servers.rag_workshop.server",
    "web_search": "majster_ai.mcp_servers.web_search.server",
}


def mcp_server_config(python_executable: str | None = None) -> dict[str, dict[str, Any]]:
    """Server launch configuration for ``langchain-mcp-adapters``.

    Also the shape an external MCP client (Claude Desktop, an IDE) needs to
    launch these servers -- see docs/ARCHITECTURE.md.
    """
    executable = python_executable or sys.executable
    return {
        name: {
            "command": executable,
            "args": ["-m", module],
            "transport": "stdio",
        }
        for name, module in MCP_SERVER_MODULES.items()
    }


async def build_mcp_toolkit_async(
    settings: Settings | None = None, *, servers: Sequence[str] | None = None
) -> Toolkit:
    """Connect to the MCP servers as subprocesses over stdio.

    This is the real deployment topology: three isolated processes, each
    restartable, each usable by any MCP client.

    Raises:
        MajsterError: if ``langchain-mcp-adapters`` is not installed.
    """
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError as exc:
        raise MajsterError(
            "langchain-mcp-adapters is not installed. Install it with "
            "pip install 'car-diagnostic-ai[agent]', or use the in-process "
            "toolkit instead."
        ) from exc

    config = mcp_server_config()
    if servers is not None:
        config = {name: value for name, value in config.items() if name in servers}

    log.info("Starting MCP servers as subprocesses: %s", ", ".join(config))
    client = MultiServerMCPClient(config)
    tools = await client.get_tools()

    toolkit = Toolkit(tools=list(tools))
    closer = getattr(client, "close", None)
    if callable(closer):
        toolkit._closers.append(lambda: run_sync(closer()))
    log.info("MCP toolkit ready: %d tool(s)", len(toolkit.tools))
    return toolkit


def build_mcp_toolkit(
    settings: Settings | None = None, *, servers: Sequence[str] | None = None
) -> Toolkit:
    """Synchronous wrapper around :func:`build_mcp_toolkit_async`."""
    return run_sync(build_mcp_toolkit_async(settings, servers=servers))


__all__ = [
    "Toolkit",
    "build_local_toolkit",
    "build_mcp_toolkit",
    "build_mcp_toolkit_async",
    "mcp_server_config",
    "tools_from_server",
    "run_sync",
    "MCP_SERVER_MODULES",
]

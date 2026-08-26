"""MCP server exposing live web search.

Run it directly (stdio transport)::

    python -m majster_ai.mcp_servers.web_search.server
"""

from __future__ import annotations

import sys
from typing import Any

from majster_ai.config import get_settings
from majster_ai.logging_setup import configure_logging, get_logger
from majster_ai.mcp_servers.web_search.service import WebSearchService

log = get_logger("mcp_servers.web_search.server")

SERVER_NAME = "web_search"
SERVER_INSTRUCTIONS = """\
Live web search, weighted towards Land Rover marque forums.

Use it for what the workshop manual cannot tell you: which failures are common
in practice, whether a symptom pattern is a known weak point, what a repair
actually costs, and how other owners diagnosed the same fault.

Order of authority, highest first:
  1. Live vehicle data (car_interface) -- what this car is doing right now.
  2. The workshop manual (search_manual) -- the manufacturer's procedure.
  3. Web and forum results -- experience and anecdote.

Never let a forum post override a manual procedure or a live measurement, and
always attribute: "a forum thread suggests..." is honest, "the procedure is..."
is not.
"""


def build_server(service: WebSearchService | None = None) -> Any:
    """Construct the FastMCP server.

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

    web = service or WebSearchService()
    mcp = FastMCP(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)

    @mcp.tool()
    def search_web(
        query: str,
        max_results: int = 5,
        include_vehicle_context: bool = True,
        forums_only: bool = False,
    ) -> dict[str, Any]:
        """Search the web for automotive diagnostic information.

        Best used after you have read the fault codes and consulted the
        manual, to find out whether a fault is a known pattern on this engine.

        Args:
            query: What to search for. Include the DTC and the symptom, e.g.
                "P0299 underboost limp mode intermittent".
            max_results: How many results. 5 is usually enough.
            include_vehicle_context: Append "Land Rover Freelander 2 2.2 TD4"
                to the query. Keep this on unless you are deliberately
                researching a shared component across marques -- the 2.2 TD4 is
                a PSA DW12, so Peugeot/Citroen/Ford results are often relevant
                and you may want to turn it off to find them.
            forums_only: Restrict to the configured marque forums.

        Returns:
            Ranked results, each flagged with whether it is a forum. Attribute
            what you take from them: forum posts are experience, not
            documentation, and must never be presented as a manufacturer
            procedure.
        """
        return web.search_web(
            query=query,
            max_results=max_results,
            include_vehicle_context=include_vehicle_context,
            forums_only=forums_only,
        )

    @mcp.tool()
    def web_search_status() -> dict[str, Any]:
        """Report which search providers are configured and usable.

        Check this if searches keep failing: the likely causes are a missing
        TAVILY_API_KEY, an uninstalled client library, or no network.
        """
        return web.status()

    log.info(
        "web_search MCP server ready (providers: %s)",
        ", ".join(p.name for p in web.providers if p.is_available()) or "none available",
    )
    return mcp


def main() -> int:
    """Entry point for ``python -m majster_ai.mcp_servers.web_search.server``."""
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

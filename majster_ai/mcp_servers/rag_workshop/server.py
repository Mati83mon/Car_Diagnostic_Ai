"""MCP server exposing workshop-manual retrieval.

Run it directly (stdio transport)::

    python -m majster_ai.mcp_servers.rag_workshop.server
"""

from __future__ import annotations

import sys
from typing import Any

from majster_ai.config import get_settings
from majster_ai.logging_setup import configure_logging, get_logger
from majster_ai.mcp_servers.rag_workshop.service import RagService

log = get_logger("mcp_servers.rag_workshop.server")

SERVER_NAME = "rag_workshop"
SERVER_INSTRUCTIONS = """\
Local retrieval over the workshop manuals in this installation's manuals
directory. Entirely on-device.

Use search_manual whenever a fault code, component or procedure needs the
manufacturer's own words: torque figures, test conditions, pinouts, removal
sequences, and the meaning of manufacturer-specific DTCs that have no generic
SAE definition.

Every passage comes with a citation (file name and page). Quote it. A mechanic
must be able to check your advice against the actual page before they start
undoing bolts.

If the index is empty, say so and tell the operator to run ingestion -- do not
answer from general knowledge and present it as though it came from the manual.
"""


def build_server(service: RagService | None = None) -> Any:
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

    rag = service or RagService()
    mcp = FastMCP(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)

    @mcp.tool()
    def search_manual(
        query: str, top_k: int = 5, source_filter: str | None = None
    ) -> dict[str, Any]:
        """Search the indexed workshop manuals for a passage.

        This is the authoritative source for anything vehicle-specific:
        procedures, torque specifications, test values, wiring, and the meaning
        of manufacturer-specific fault codes.

        Args:
            query: What to look for. Use the manual's vocabulary -- a DTC
                number ("P0299"), a component ("turbocharger actuator"), or a
                procedure ("swirl flap removal"). Short, specific queries beat
                long conversational ones.
            top_k: How many passages to return. 3-5 is usually right.
            source_filter: Restrict to one file by a substring of its name.

        Returns:
            Ranked passages, each with its text and a citation naming the file
            and page. Quote the citation in your answer. If the results do not
            actually address the question, say so rather than stretching them:
            an empty result is more useful than a confident wrong one.
        """
        return rag.search_manual(query=query, top_k=top_k, source_filter=source_filter)

    @mcp.tool()
    def ingest_manuals(rebuild: bool = False) -> dict[str, Any]:
        """Build or refresh the manual index from the manuals directory.

        Run this after adding PDFs. Safe to re-run: chunk ids are
        deterministic, so existing chunks update instead of duplicating.

        Args:
            rebuild: Drop the index first. Necessary after changing the
                embedding backend, because vectors from different backends are
                not comparable.
        """
        return rag.ingest(rebuild=rebuild)

    @mcp.tool()
    def manual_index_status() -> dict[str, Any]:
        """Report what is indexed and which embedding backend is in use.

        Check this before concluding that the manuals contain nothing on a
        topic -- the index may simply be empty, or built with the lexical
        fallback backend.
        """
        return rag.status()

    @mcp.tool()
    def list_manual_sources() -> dict[str, Any]:
        """List the documents currently present in the index."""
        return rag.list_sources()

    log.info(
        "rag_workshop MCP server ready (%d chunk(s), backend=%s)",
        rag.store.count(),
        rag.embeddings.name,
    )
    return mcp


def main() -> int:
    """Entry point for ``python -m majster_ai.mcp_servers.rag_workshop.server``."""
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

"""Web_Search_MCP -- live web and marque-forum search.

Tavily when a key is configured, DuckDuckGo when it is not, with results from
Land Rover forums weighted up: for a 15-year-old vehicle the useful answer is
usually in a forum thread rather than an article.
"""

from __future__ import annotations

from majster_ai.mcp_servers.web_search.providers import (
    DuckDuckGoProvider,
    SearchProvider,
    TavilyProvider,
    WebResult,
    build_providers,
)
from majster_ai.mcp_servers.web_search.service import WebSearchService

__all__ = [
    "WebSearchService",
    "SearchProvider",
    "TavilyProvider",
    "DuckDuckGoProvider",
    "WebResult",
    "build_providers",
]

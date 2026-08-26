"""Web-search providers: Tavily first, DuckDuckGo as the keyless fallback.

Both are wrapped behind :class:`SearchProvider` so the service can try one and
fall through to the other. Imports are deferred to call time: a missing
optional dependency must degrade the tool, not break the import of the whole
package.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import urlparse

from majster_ai.errors import WebSearchError
from majster_ai.logging_setup import get_logger

log = get_logger("mcp_servers.web_search.providers")


@dataclass(frozen=True, slots=True)
class WebResult:
    """One search hit."""

    title: str
    url: str
    snippet: str
    provider: str
    score: float = 0.0

    @property
    def domain(self) -> str:
        try:
            host = urlparse(self.url).netloc.lower()
        except Exception:  # pragma: no cover - urlparse is very tolerant
            return ""
        return host[4:] if host.startswith("www.") else host

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "domain": self.domain,
            "provider": self.provider,
            "score": round(self.score, 4),
        }


class SearchProvider(abc.ABC):
    """A source of web results."""

    name: str = "unknown"
    #: Whether this provider needs an API key.
    requires_key: bool = False

    @abc.abstractmethod
    def is_available(self) -> bool:
        """True when this provider can actually be used right now."""

    @abc.abstractmethod
    def search(self, query: str, max_results: int, timeout: float) -> list[WebResult]:
        """Run a search.

        Raises:
            WebSearchError: on any provider failure, so the service can fall
                through to the next provider rather than crash.
        """

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "requires_key": self.requires_key,
            "available": self.is_available(),
        }


class TavilyProvider(SearchProvider):
    """Tavily -- better ranking for technical and forum content, needs a key."""

    name = "tavily"
    requires_key = True

    def __init__(self, api_key: str | None, *, search_depth: str = "advanced") -> None:
        self._api_key = (api_key or "").strip()
        self._search_depth = search_depth
        self._client: Any | None = None

    def is_available(self) -> bool:
        if not self._api_key:
            return False
        try:
            import tavily  # noqa: F401
        except ImportError:
            return False
        return True

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from tavily import TavilyClient
        except ImportError as exc:
            raise WebSearchError(
                "tavily-python is not installed. Install it with "
                "pip install 'car-diagnostic-ai[web]', or leave TAVILY_API_KEY "
                "unset to use the DuckDuckGo fallback."
            ) from exc
        self._client = TavilyClient(api_key=self._api_key)
        return self._client

    def search(self, query: str, max_results: int, timeout: float) -> list[WebResult]:
        if not self._api_key:
            raise WebSearchError("TAVILY_API_KEY is not set")
        client = self._get_client()
        try:
            response = client.search(
                query=query,
                max_results=max_results,
                search_depth=self._search_depth,
            )
        except Exception as exc:
            raise WebSearchError(f"Tavily search failed: {exc}", provider=self.name) from exc

        results: list[WebResult] = []
        for item in (response or {}).get("results", []) or []:
            url = str(item.get("url", "")).strip()
            if not url:
                continue
            results.append(
                WebResult(
                    title=str(item.get("title", "")).strip() or url,
                    url=url,
                    snippet=str(item.get("content", "")).strip(),
                    provider=self.name,
                    score=float(item.get("score", 0.0) or 0.0),
                )
            )
        return results


class DuckDuckGoProvider(SearchProvider):
    """DuckDuckGo via ``ddgs`` -- no key, lower quality, rate-limited."""

    name = "duckduckgo"
    requires_key = False

    def __init__(self, region: str = "wt-wt", safesearch: str = "moderate") -> None:
        self._region = region
        self._safesearch = safesearch

    def is_available(self) -> bool:
        try:
            import ddgs  # noqa: F401
        except ImportError:
            try:
                import duckduckgo_search  # noqa: F401
            except ImportError:
                return False
        return True

    @staticmethod
    def _client() -> Any:
        """Import the client, tolerating the ddgs/duckduckgo_search rename."""
        try:
            from ddgs import DDGS
        except ImportError:
            try:
                from duckduckgo_search import DDGS  # type: ignore[no-redef]
            except ImportError as exc:
                raise WebSearchError(
                    "No DuckDuckGo client installed. Install it with "
                    "pip install 'car-diagnostic-ai[web]'"
                ) from exc
        return DDGS

    def search(self, query: str, max_results: int, timeout: float) -> list[WebResult]:
        ddgs_class = self._client()
        try:
            with ddgs_class(timeout=int(timeout)) as client:
                raw = client.text(
                    query,
                    region=self._region,
                    safesearch=self._safesearch,
                    max_results=max_results,
                )
        except TypeError:
            # Older/newer signatures differ in which kwargs they accept; retry
            # with only the ones every version has.
            try:
                with ddgs_class() as client:
                    raw = client.text(query, max_results=max_results)
            except Exception as exc:
                raise WebSearchError(
                    f"DuckDuckGo search failed: {exc}", provider=self.name
                ) from exc
        except Exception as exc:
            raise WebSearchError(
                f"DuckDuckGo search failed: {exc}. This provider is rate-limited; "
                f"set TAVILY_API_KEY for a reliable one.",
                provider=self.name,
            ) from exc

        results: list[WebResult] = []
        for item in raw or []:
            url = str(item.get("href") or item.get("url") or "").strip()
            if not url:
                continue
            results.append(
                WebResult(
                    title=str(item.get("title", "")).strip() or url,
                    url=url,
                    snippet=str(item.get("body") or item.get("snippet") or "").strip(),
                    provider=self.name,
                )
            )
        return results


def build_providers(
    tavily_api_key: str | None, *, prefer: Sequence[str] | None = None
) -> list[SearchProvider]:
    """Build the provider chain, best first.

    Tavily leads when a key is present; DuckDuckGo is always appended so there
    is a keyless path even if Tavily errors or runs out of credit.
    """
    providers: list[SearchProvider] = [
        TavilyProvider(tavily_api_key),
        DuckDuckGoProvider(),
    ]
    if prefer:
        order = {name.lower(): index for index, name in enumerate(prefer)}
        providers.sort(key=lambda provider: order.get(provider.name, len(order)))
    return providers


__all__ = [
    "WebResult",
    "SearchProvider",
    "TavilyProvider",
    "DuckDuckGoProvider",
    "build_providers",
]

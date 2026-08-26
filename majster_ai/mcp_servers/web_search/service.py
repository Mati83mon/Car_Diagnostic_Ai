"""Live web search, tuned for automotive fault-finding.

Two things make this more than a thin wrapper around a search API:

**Provider fall-through.** Tavily when a key is configured, DuckDuckGo when it
is not or when Tavily fails. A diagnostic session in a workshop should not stop
because a credit balance ran out.

**Forum weighting.** For a 15-year-old Freelander, the useful answer is far
more often in a marque forum thread than in a content-farm article. Results
from the configured forum domains are boosted, and the tool output says which
hits are forums so the model can weigh them accordingly.

Provenance is always reported. Forum posts are opinion, not documentation, and
the agent is told to treat them that way.
"""

from __future__ import annotations

from typing import Any, Sequence

from majster_ai.config import Settings, get_settings
from majster_ai.errors import WebSearchError
from majster_ai.logging_setup import get_logger, log_agent_step
from majster_ai.mcp_servers.web_search.providers import SearchProvider, WebResult, build_providers

log = get_logger("mcp_servers.web_search.service")

#: Score bonus applied to a result from a preferred forum domain.
FORUM_BOOST = 0.35

#: Appended to a query when the caller asks for vehicle-scoped search.
VEHICLE_CONTEXT = "Land Rover Freelander 2 2.2 TD4"


class WebSearchService:
    """Search the web with automotive-aware ranking."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        providers: Sequence[SearchProvider] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        key = (
            self.settings.tavily_api_key.get_secret_value()
            if self.settings.tavily_api_key is not None
            else None
        )
        self.providers = list(providers) if providers is not None else build_providers(key)

    @property
    def preferred_domains(self) -> tuple[str, ...]:
        return tuple(d.lower() for d in self.settings.web_preferred_domains)

    def _is_preferred(self, result: WebResult) -> bool:
        domain = result.domain
        return any(domain == d or domain.endswith(f".{d}") for d in self.preferred_domains)

    def _rank(self, results: Sequence[WebResult]) -> list[WebResult]:
        """Boost forum hits, then sort. Stable within equal scores."""
        ranked = [
            WebResult(
                title=result.title,
                url=result.url,
                snippet=result.snippet,
                provider=result.provider,
                score=result.score + (FORUM_BOOST if self._is_preferred(result) else 0.0),
            )
            for result in results
        ]
        # Preserve provider order for ties: a provider's own ranking is
        # information, and re-sorting on a 0.0 score would discard it.
        return sorted(ranked, key=lambda r: r.score, reverse=True)

    @staticmethod
    def _deduplicate(results: Sequence[WebResult]) -> list[WebResult]:
        seen: set[str] = set()
        unique: list[WebResult] = []
        for result in results:
            key = result.url.rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(result)
        return unique

    def search_web(
        self,
        query: str,
        max_results: int | None = None,
        *,
        include_vehicle_context: bool = True,
        forums_only: bool = False,
    ) -> dict[str, Any]:
        """Search the web.

        Args:
            query: What to search for.
            max_results: How many hits. Defaults to the configured value.
            include_vehicle_context: Append the vehicle description to the
                query, so "swirl flap failure" does not return results for a
                different marque.
            forums_only: Keep only results from the configured forum domains.

        Returns:
            Ranked results with provenance, or a structured error naming every
            provider that was tried.
        """
        if not query or not query.strip():
            return {
                "ok": False,
                "error": "empty_query",
                "message": "Provide a search query.",
            }

        limit = max_results if max_results and max_results > 0 else self.settings.web_max_results
        effective_query = (
            f"{query.strip()} {VEHICLE_CONTEXT}"
            if include_vehicle_context and VEHICLE_CONTEXT.lower() not in query.lower()
            else query.strip()
        )

        log_agent_step("web.search", f"Searching the web for: {effective_query!r}")

        attempts: list[dict[str, Any]] = []
        results: list[WebResult] = []
        provider_used: str | None = None

        for provider in self.providers:
            if not provider.is_available():
                attempts.append(
                    {
                        "provider": provider.name,
                        "status": "unavailable",
                        "reason": (
                            "no API key configured"
                            if provider.requires_key
                            else "client library not installed"
                        ),
                    }
                )
                continue
            try:
                # Over-fetch so forum filtering and de-duplication still leave
                # enough results to return.
                found = provider.search(
                    effective_query,
                    limit * 3 if forums_only else limit * 2,
                    self.settings.web_timeout,
                )
            except WebSearchError as exc:
                log.warning("Provider %s failed: %s", provider.name, exc.message)
                attempts.append(
                    {"provider": provider.name, "status": "error", "reason": exc.message}
                )
                continue

            attempts.append({"provider": provider.name, "status": "ok", "results": len(found)})
            if found:
                results = found
                provider_used = provider.name
                break

        if not results:
            return {
                "ok": False,
                "error": "web_search_error",
                "message": (
                    "No web results. Every provider was unavailable or returned "
                    "nothing. Set TAVILY_API_KEY for a reliable provider, or check "
                    "network connectivity."
                ),
                "query": effective_query,
                "attempts": attempts,
            }

        ranked = self._rank(self._deduplicate(results))
        if forums_only:
            ranked = [result for result in ranked if self._is_preferred(result)]
        ranked = ranked[:limit]

        forum_hits = [result for result in ranked if self._is_preferred(result)]
        return {
            "ok": True,
            "query": effective_query,
            "original_query": query,
            "provider": provider_used,
            "count": len(ranked),
            "results": [
                {**result.to_dict(), "is_forum": self._is_preferred(result)} for result in ranked
            ],
            "forum_results": len(forum_hits),
            "attempts": attempts,
            "summary": (
                f"{len(ranked)} result(s) from {provider_used}, "
                f"{len(forum_hits)} from marque forums."
            ),
            "reliability_note": (
                "Web results -- especially forum posts -- are anecdote, not "
                "documentation. Treat them as leads to verify against the "
                "workshop manual and live vehicle data, and say where each claim "
                "came from. Never present a forum post as a manufacturer procedure."
            ),
        }

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "providers": [provider.describe() for provider in self.providers],
            "preferred_domains": list(self.preferred_domains),
            "max_results": self.settings.web_max_results,
            "timeout": self.settings.web_timeout,
            "any_available": any(provider.is_available() for provider in self.providers),
        }


__all__ = ["WebSearchService", "FORUM_BOOST", "VEHICLE_CONTEXT"]

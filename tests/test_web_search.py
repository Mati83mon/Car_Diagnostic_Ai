"""Web search: provider fall-through, forum weighting, and honest provenance."""

from __future__ import annotations

from unittest.mock import MagicMock, Mock

import pytest

from majster_ai.config import load_settings
from majster_ai.errors import WebSearchError
from majster_ai.mcp_servers.web_search.providers import (
    DuckDuckGoProvider,
    SearchProvider,
    TavilyProvider,
    WebResult,
    build_providers,
)
from majster_ai.mcp_servers.web_search.service import VEHICLE_CONTEXT, WebSearchService


class StubProvider(SearchProvider):
    """A provider whose behaviour a test controls exactly."""

    def __init__(
        self,
        name: str,
        results=None,
        *,
        available: bool = True,
        error: str | None = None,
        requires_key: bool = False,
    ) -> None:
        self.name = name
        self.requires_key = requires_key
        self._results = list(results or [])
        self._available = available
        self._error = error
        self.queries: list[str] = []

    def is_available(self) -> bool:
        return self._available

    def search(self, query: str, max_results: int, timeout: float) -> list[WebResult]:
        self.queries.append(query)
        if self._error:
            raise WebSearchError(self._error, provider=self.name)
        return self._results


def result(url: str, score: float = 0.5, title: str = "t") -> WebResult:
    return WebResult(title=title, url=url, snippet="s", provider="stub", score=score)


class TestWebResult:
    @pytest.mark.parametrize(
        ("url", "domain"),
        [
            ("https://www.freel2.com/forum/t1", "freel2.com"),
            ("https://freel2.com/x", "freel2.com"),
            ("http://sub.landyzone.co.uk/y", "sub.landyzone.co.uk"),
            ("not a url", ""),
        ],
    )
    def test_domain_extraction(self, url: str, domain: str) -> None:
        assert result(url).domain == domain


class TestRanking:
    def test_forum_results_are_boosted(self) -> None:
        provider = StubProvider(
            "stub",
            [
                result("https://contentfarm.example/a", 0.9),
                result("https://www.freel2.com/t1", 0.4),
            ],
        )
        service = WebSearchService(load_settings(), providers=[provider])
        results = service.search_web("P0299")["results"]
        assert results[0]["domain"] == "contentfarm.example"
        assert results[1]["is_forum"] is True
        assert results[1]["score"] > 0.4

    def test_forum_beats_a_close_non_forum(self) -> None:
        provider = StubProvider(
            "stub",
            [
                result("https://contentfarm.example/a", 0.6),
                result("https://www.freel2.com/t1", 0.4),
            ],
        )
        results = WebSearchService(load_settings(), providers=[provider]).search_web("x")["results"]
        assert results[0]["is_forum"] is True

    def test_duplicate_urls_removed(self) -> None:
        provider = StubProvider(
            "stub",
            [
                result("https://example.com/a", 0.9),
                result("https://example.com/a/", 0.5),
                result("https://EXAMPLE.com/a", 0.4),
            ],
        )
        assert WebSearchService(load_settings(), providers=[provider]).search_web("x")["count"] == 1

    def test_forums_only_filter(self) -> None:
        provider = StubProvider(
            "stub",
            [
                result("https://contentfarm.example/a", 0.9),
                result("https://www.freel2.com/t1", 0.4),
            ],
        )
        service = WebSearchService(load_settings(), providers=[provider])
        results = service.search_web("x", forums_only=True)["results"]
        assert len(results) == 1 and results[0]["is_forum"]

    def test_max_results_respected(self) -> None:
        provider = StubProvider("stub", [result(f"https://e.com/{i}", 0.5) for i in range(20)])
        assert (
            WebSearchService(load_settings(), providers=[provider]).search_web("x", max_results=3)[
                "count"
            ]
            == 3
        )

    def test_configurable_domains(self) -> None:
        settings = load_settings(web_preferred_domains="mymarque.example")
        provider = StubProvider("stub", [result("https://mymarque.example/t", 0.1)])
        assert (
            WebSearchService(settings, providers=[provider]).search_web("x")["results"][0][
                "is_forum"
            ]
            is True
        )


class TestQueryBuilding:
    def test_vehicle_context_appended(self) -> None:
        provider = StubProvider("stub", [result("https://e.com/a")])
        WebSearchService(load_settings(), providers=[provider]).search_web("swirl flap")
        assert VEHICLE_CONTEXT in provider.queries[0]

    def test_context_not_duplicated(self) -> None:
        provider = StubProvider("stub", [result("https://e.com/a")])
        WebSearchService(load_settings(), providers=[provider]).search_web(
            f"swirl flap {VEHICLE_CONTEXT}"
        )
        assert provider.queries[0].count("Freelander") == 1

    def test_context_can_be_disabled(self) -> None:
        # The 2.2 TD4 is a PSA DW12; cross-marque results are often the useful ones.
        provider = StubProvider("stub", [result("https://e.com/a")])
        WebSearchService(load_settings(), providers=[provider]).search_web(
            "DW12 injector", include_vehicle_context=False
        )
        assert provider.queries[0] == "DW12 injector"

    def test_empty_query(self) -> None:
        assert (
            WebSearchService(load_settings(), providers=[]).search_web("")["error"] == "empty_query"
        )


class TestFallThrough:
    def test_unavailable_provider_is_skipped(self) -> None:
        first = StubProvider("first", available=False, requires_key=True)
        second = StubProvider("second", [result("https://e.com/a")])
        payload = WebSearchService(load_settings(), providers=[first, second]).search_web("x")
        assert payload["provider"] == "second"
        assert payload["attempts"][0]["status"] == "unavailable"

    def test_failing_provider_falls_through(self) -> None:
        first = StubProvider("first", error="rate limited")
        second = StubProvider("second", [result("https://e.com/a")])
        payload = WebSearchService(load_settings(), providers=[first, second]).search_web("x")
        assert payload["ok"] is True and payload["provider"] == "second"

    def test_empty_provider_falls_through(self) -> None:
        first = StubProvider("first", [])
        second = StubProvider("second", [result("https://e.com/a")])
        assert (
            WebSearchService(load_settings(), providers=[first, second]).search_web("x")["provider"]
            == "second"
        )

    def test_all_providers_failing_is_a_clean_error(self) -> None:
        payload = WebSearchService(
            load_settings(),
            providers=[StubProvider("a", error="boom"), StubProvider("b", available=False)],
        ).search_web("x")
        assert payload["ok"] is False
        assert payload["error"] == "web_search_error"
        assert len(payload["attempts"]) == 2
        assert "TAVILY_API_KEY" in payload["message"]


class TestProvenance:
    def test_reliability_note_is_always_present(self) -> None:
        provider = StubProvider("stub", [result("https://e.com/a")])
        payload = WebSearchService(load_settings(), providers=[provider]).search_web("x")
        assert "anecdote" in payload["reliability_note"]
        assert "manufacturer procedure" in payload["reliability_note"]

    def test_forum_flag_on_each_result(self) -> None:
        provider = StubProvider(
            "stub", [result("https://www.freel2.com/t"), result("https://e.com/a")]
        )
        results = WebSearchService(load_settings(), providers=[provider]).search_web("x")["results"]
        assert {entry["is_forum"] for entry in results} == {True, False}


class TestProviders:
    def test_tavily_unavailable_without_a_key(self) -> None:
        assert TavilyProvider(None).is_available() is False
        assert TavilyProvider("   ").is_available() is False

    def test_tavily_search_without_a_key_raises(self) -> None:
        with pytest.raises(WebSearchError, match="TAVILY_API_KEY"):
            TavilyProvider(None).search("x", 5, 10)

    def test_tavily_parses_results(self) -> None:
        client = Mock()
        client.search.return_value = {
            "results": [
                {"url": "https://e.com/a", "title": "T", "content": "C", "score": 0.8},
                {"url": "", "title": "no url"},  # must be skipped
            ]
        }
        provider = TavilyProvider("tvly-key")
        provider._client = client
        results = provider.search("q", 5, 10)
        assert len(results) == 1 and results[0].score == 0.8

    def test_tavily_api_failure_becomes_a_websearcherror(self) -> None:
        client = Mock()
        client.search.side_effect = RuntimeError("402 out of credit")
        provider = TavilyProvider("tvly-key")
        provider._client = client
        with pytest.raises(WebSearchError, match="out of credit"):
            provider.search("q", 5, 10)

    def test_duckduckgo_is_keyless(self) -> None:
        assert DuckDuckGoProvider().requires_key is False

    def test_duckduckgo_parses_results(self, monkeypatch) -> None:
        # MagicMock, not Mock: the provider uses the client as a context manager.
        ddgs_class = MagicMock()
        client = ddgs_class.return_value.__enter__.return_value
        client.text.return_value = [
            {"href": "https://e.com/a", "title": "T", "body": "B"},
            {"url": "https://e.com/b", "title": "T2", "snippet": "S"},
        ]
        provider = DuckDuckGoProvider()
        monkeypatch.setattr(DuckDuckGoProvider, "_client", staticmethod(lambda: ddgs_class))
        results = provider.search("q", 5, 10)
        assert [entry.url for entry in results] == ["https://e.com/a", "https://e.com/b"]

    def test_duckduckgo_error_mentions_rate_limits(self, monkeypatch) -> None:
        ddgs_class = MagicMock()
        ddgs_class.return_value.__enter__.side_effect = RuntimeError("429")
        provider = DuckDuckGoProvider()
        monkeypatch.setattr(DuckDuckGoProvider, "_client", staticmethod(lambda: ddgs_class))
        with pytest.raises(WebSearchError, match="rate-limited"):
            provider.search("q", 5, 10)

    def test_chain_always_includes_a_keyless_provider(self) -> None:
        # A workshop session must not stop because a credit balance ran out.
        names = [provider.name for provider in build_providers(None)]
        assert "duckduckgo" in names

    def test_tavily_leads_when_keyed(self) -> None:
        assert build_providers("tvly-key")[0].name == "tavily"


class TestStatus:
    def test_reports_provider_availability(self) -> None:
        status = WebSearchService(
            load_settings(), providers=[StubProvider("a"), StubProvider("b", available=False)]
        ).status()
        assert status["any_available"] is True
        assert {p["name"]: p["available"] for p in status["providers"]} == {"a": True, "b": False}

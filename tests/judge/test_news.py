"""Tests for sentinel.judge.news — Tavily/Exa news fetcher."""

from __future__ import annotations

import httpx
import pytest
import respx

from sentinel.judge.news import (
    _search_exa,
    _search_tavily,
    clear_cache,
    fetch_news,
    format_headlines,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_news_cache():
    """Clear the news cache before each test."""
    clear_cache()
    yield
    clear_cache()


# ── format_headlines ────────────────────────────────────────────────────────


class TestFormatHeadlines:
    def test_empty_list(self):
        assert format_headlines([]) == "No relevant news found."

    def test_single_headline(self):
        result = format_headlines(["Breaking news: BTC hits $100k"])
        assert result == "1. Breaking news: BTC hits $100k"

    def test_multiple_headlines(self):
        headlines = ["First headline", "Second headline", "Third headline"]
        result = format_headlines(headlines)
        assert "1. First headline" in result
        assert "2. Second headline" in result
        assert "3. Third headline" in result

    def test_max_chars_truncation(self):
        headlines = [f"Headline {i} with some extra text to fill up space" for i in range(20)]
        result = format_headlines(headlines, max_chars=100)
        assert len(result) <= 120  # slight overshoot allowed per line boundary

    def test_numbered_format(self):
        result = format_headlines(["A", "B"])
        assert result.startswith("1.")
        assert "2." in result


# ── _search_tavily (mocked) ─────────────────────────────────────────────────


class TestSearchTavily:
    @respx.mock
    async def test_tavily_returns_headlines(self):
        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {"title": "Bitcoin rally continues"},
                        {"title": "Crypto markets surge"},
                    ]
                },
            )
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("sentinel.judge.news.settings.tavily_api_key", "test-key")
            headlines = await _search_tavily("bitcoin", max_results=5, lookback_days=3)
        assert len(headlines) == 2
        assert "Bitcoin rally continues" in headlines

    @respx.mock
    async def test_tavily_empty_results(self):
        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("sentinel.judge.news.settings.tavily_api_key", "test-key")
            headlines = await _search_tavily("obscure query")
        assert headlines == []

    @respx.mock
    async def test_tavily_filters_empty_titles(self):
        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {"title": "Real headline"},
                        {"title": ""},
                        {"title": "Another real one"},
                    ]
                },
            )
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("sentinel.judge.news.settings.tavily_api_key", "test-key")
            headlines = await _search_tavily("test")
        assert len(headlines) == 2


# ── _search_exa (mocked) ────────────────────────────────────────────────────


class TestSearchExa:
    @respx.mock
    async def test_exa_returns_headlines(self):
        respx.post("https://api.exa.ai/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {"title": "Exa headline 1"},
                        {"title": "Exa headline 2"},
                    ]
                },
            )
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("sentinel.judge.news.settings.exa_api_key", "test-key")
            headlines = await _search_exa("test", max_results=5)
        assert len(headlines) == 2


# ── fetch_news (integration with cache + fallback) ──────────────────────────


class TestFetchNews:
    @respx.mock
    async def test_caches_results(self):
        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(
                200, json={"results": [{"title": "Cached headline"}]}
            )
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("sentinel.judge.news.settings.tavily_api_key", "test-key")
            # First call fetches
            h1 = await fetch_news("test question", "market-001")
            assert len(h1) == 1

            # Second call uses cache (even if mock is changed)
            respx.post("https://api.tavily.com/search").mock(
                return_value=httpx.Response(
                    200, json={"results": [{"title": "New headline"}]}
                )
            )
            h2 = await fetch_news("test question", "market-001")
            assert h2 == h1  # Same cached result

    @respx.mock
    async def test_falls_back_to_exa(self):
        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(500)
        )
        respx.post("https://api.exa.ai/search").mock(
            return_value=httpx.Response(
                200, json={"results": [{"title": "Exa fallback headline"}]}
            )
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("sentinel.judge.news.settings.tavily_api_key", "test-key")
            mp.setattr("sentinel.judge.news.settings.exa_api_key", "test-key")
            headlines = await fetch_news("test", "market-002")
        assert len(headlines) == 1
        assert "Exa fallback" in headlines[0]

    @respx.mock
    async def test_both_fail_returns_empty(self):
        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(500)
        )
        respx.post("https://api.exa.ai/search").mock(
            return_value=httpx.Response(500)
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("sentinel.judge.news.settings.tavily_api_key", "test-key")
            mp.setattr("sentinel.judge.news.settings.exa_api_key", "test-key")
            headlines = await fetch_news("test", "market-003")
        assert headlines == []

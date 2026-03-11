"""News context fetcher (Tavily + Exa fallback).

Fetches recent headlines related to a Polymarket market question so the
LLM can assess whether a flagged trade coincides with breaking news.

Features:
  - Per-market, per-hour cache to avoid duplicate Tavily calls.
  - Automatic fallback to Exa if Tavily fails.
  - Output is a compact string suitable for direct LLM prompt insertion.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from sentinel.config import settings

logger = structlog.get_logger()

# ── In-memory cache ─────────────────────────────────────────────────────────

_cache: dict[str, tuple[float, list[str]]] = {}
_CACHE_TTL_S = 3600  # 1 hour


def _cache_key(market_id: str) -> str:
    return market_id


def _get_cached(market_id: str) -> list[str] | None:
    key = _cache_key(market_id)
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, headlines = entry
    if time.monotonic() - ts > _CACHE_TTL_S:
        del _cache[key]
        return None
    return headlines


def _set_cache(market_id: str, headlines: list[str]) -> None:
    _cache[_cache_key(market_id)] = (time.monotonic(), headlines)


def clear_cache() -> None:
    """Clear the headline cache (useful for testing)."""
    _cache.clear()


# ── Tavily ──────────────────────────────────────────────────────────────────


async def _search_tavily(
    query: str,
    *,
    max_results: int = 5,
    lookback_days: int = 3,
) -> list[str]:
    """Call Tavily search and return a list of headline strings."""
    if not settings.tavily_api_key:
        raise RuntimeError("TAVILY_API_KEY not configured")

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": settings.tavily_api_key,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
                "include_answer": False,
                "days": lookback_days,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    results: list[dict[str, Any]] = data.get("results", [])
    return [r.get("title", "") for r in results if r.get("title")]


# ── Exa fallback ────────────────────────────────────────────────────────────


async def _search_exa(
    query: str,
    *,
    max_results: int = 5,
) -> list[str]:
    """Call Exa search as a fallback for Tavily."""
    if not settings.exa_api_key:
        raise RuntimeError("EXA_API_KEY not configured")

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": settings.exa_api_key},
            json={
                "query": query,
                "num_results": max_results,
                "type": "neural",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    results: list[dict[str, Any]] = data.get("results", [])
    return [r.get("title", "") for r in results if r.get("title")]


# ── Public API ──────────────────────────────────────────────────────────────


async def fetch_news(
    market_question: str,
    market_id: str,
    *,
    max_results: int | None = None,
    lookback_days: int | None = None,
) -> list[str]:
    """Fetch recent headlines for a market question.

    Returns cached results if available. Falls back to Exa if Tavily
    fails.  Returns an empty list (not an exception) when both fail.

    Args:
        market_question: The human-readable market question text.
        market_id: Used as cache key.
        max_results: Override ``settings.news_search_max_results``.
        lookback_days: Override ``settings.news_search_lookback_days``.

    Returns:
        List of headline strings (may be empty).
    """
    cached = _get_cached(market_id)
    if cached is not None:
        logger.debug("News cache hit", market_id=market_id)
        return cached

    mr = max_results or settings.news_search_max_results
    ld = lookback_days or settings.news_search_lookback_days

    # Try Tavily first
    try:
        headlines = await _search_tavily(market_question, max_results=mr, lookback_days=ld)
        _set_cache(market_id, headlines)
        logger.info("News fetched (Tavily)", market_id=market_id, count=len(headlines))
        return headlines
    except Exception as exc:
        logger.warning("Tavily search failed, trying Exa", error=str(exc))

    # Fallback: Exa
    try:
        headlines = await _search_exa(market_question, max_results=mr)
        _set_cache(market_id, headlines)
        logger.info("News fetched (Exa)", market_id=market_id, count=len(headlines))
        return headlines
    except Exception as exc:
        logger.warning("Exa search also failed", error=str(exc))

    # Both failed — cache empty to avoid hammering
    _set_cache(market_id, [])
    return []


def format_headlines(headlines: list[str], *, max_chars: int = 2000) -> str:
    """Format headline list into a numbered string for LLM prompt injection.

    Keeps the output under *max_chars* (roughly ≤ 500 tokens).
    """
    if not headlines:
        return "No relevant news found."

    lines: list[str] = []
    total = 0
    for i, h in enumerate(headlines, 1):
        line = f"{i}. {h}"
        if total + len(line) + 1 > max_chars:
            break
        lines.append(line)
        total += len(line) + 1  # +1 for newline

    return "\n".join(lines)

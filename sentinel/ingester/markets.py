"""Market metadata synchronisation from Polymarket REST API.

Fetches all active markets via the CLOB ``/markets`` endpoint, filters to
configured categories, and upserts into the DuckDB ``markets`` table.
"""

from __future__ import annotations

import ssl
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from sentinel.config import settings

logger = structlog.get_logger()

# Polymarket pagination sentinel
_END_CURSOR = "LTE="
_PAGE_SIZE = 1000  # server default

# Build a lenient SSL context (Polymarket CDN cert can be flaky)
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


async def fetch_all_markets(
    *,
    base_url: str | None = None,
    categories: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Page through ``/markets`` and return active markets.

    Args:
        base_url: Override for the REST URL (tests / dry-run).
        categories: Category allow-list.  ``None`` means accept all.

    Returns:
        List of raw market dicts from the API.
    """
    url = base_url or settings.polymarket_rest_url
    cats = {c.lower() for c in (categories or settings.categories_list)}
    cursor = "MA=="
    markets: list[dict[str, Any]] = []
    page = 0

    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        while cursor and cursor != _END_CURSOR:
            resp = await client.get(
                f"{url}/markets",
                params={"next_cursor": cursor},
            )
            resp.raise_for_status()
            body = resp.json()
            page += 1

            for m in body.get("data", []):
                # Basic active filter
                if not m.get("active") or m.get("closed") or m.get("archived"):
                    continue
                # Category filter — Polymarket uses "tags" list
                raw_tags = m.get("tags") or []
                tags = [t.lower() for t in raw_tags if isinstance(t, str)]
                if cats and not any(t in cats for t in tags):
                    continue
                markets.append(m)

            cursor = body.get("next_cursor")
            # Log progress every 100 pages instead of every page
            if page % 100 == 0:
                logger.info("Market sync progress", page=page, found=len(markets))

    logger.info("Market fetch complete", total=len(markets), pages=page)
    return markets


def _parse_end_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def upsert_markets(conn: Any, markets: list[dict[str, Any]]) -> int:
    """Upsert market metadata into DuckDB.

    Uses DELETE + INSERT because DuckDB doesn't support ``ON CONFLICT UPDATE``
    with all column types elegantly.  This is fine for metadata refreshes.

    Returns the number of rows upserted.
    """
    if not markets:
        return 0

    rows = []
    for m in markets:
        tokens = m.get("tokens") or []
        volume = sum(float(t.get("price", 0)) for t in tokens) if tokens else 0.0
        rows.append((
            str(m["condition_id"]),
            m.get("question", ""),
            m.get("market_slug", m.get("slug", "")),
            ",".join(t for t in (m.get("tags") or []) if isinstance(t, str)),
            _parse_end_date(m.get("end_date_iso")),
            float(m.get("volume", 0) or 0),
            float(m.get("liquidity", 0) or 0),
            True,  # active
            datetime.now(UTC),
        ))

    # Batch DELETE + INSERT inside a transaction
    market_ids = [r[0] for r in rows]
    placeholders = ",".join(["?"] * len(market_ids))
    conn.execute(f"DELETE FROM markets WHERE market_id IN ({placeholders})", market_ids)
    conn.executemany(
        """INSERT INTO markets
           (market_id, question, slug, category, end_date,
            volume_usd, liquidity_usd, active, last_synced)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    logger.info("Markets upserted", count=len(rows))
    return len(rows)


async def fetch_market_by_id(
    condition_id: str,
    *,
    base_url: str | None = None,
) -> dict[str, Any] | None:
    """Fetch a single market by condition_id from the REST API.

    Returns the raw market dict, or ``None`` if the market is not found or
    the request fails.
    """
    url = base_url or settings.polymarket_rest_url
    async with httpx.AsyncClient(verify=False, timeout=15) as client:
        try:
            resp = await client.get(f"{url}/markets/{condition_id}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning(
                "Failed to fetch market by id",
                condition_id=condition_id,
                error=str(exc),
            )
            return None


async def sync_markets(conn: Any, *, base_url: str | None = None) -> int:
    """Full sync: fetch from API → upsert into DuckDB.

    Returns the number of markets upserted.
    """
    markets = await fetch_all_markets(base_url=base_url)
    return upsert_markets(conn, markets)


async def fetch_active_asset_ids(
    *,
    base_url: str | None = None,
    limit: int = 20,
) -> list[str]:
    """Fetch token IDs for the most actively-traded markets.

    Uses the ``/sampling-markets`` endpoint which returns the top markets
    by recent activity.  Extracts both YES and NO token IDs from each market.

    Args:
        base_url: Override for the REST URL.
        limit: Max number of markets to pull (each yields 2 token IDs).

    Returns:
        A flat list of asset_id strings (token IDs).
    """
    active = await fetch_active_markets(base_url=base_url, limit=limit)
    asset_ids: list[str] = []
    for _cid, aids in active:
        asset_ids.extend(aids)
    return asset_ids


async def fetch_active_markets(
    *,
    base_url: str | None = None,
    limit: int = 20,
) -> list[tuple[str, list[str]]]:
    """Fetch actively-traded markets with their condition_ids and token IDs.

    Uses the ``/sampling-markets`` endpoint which returns the top markets
    by recent activity.

    Args:
        base_url: Override for the REST URL.
        limit: Max number of markets to pull.

    Returns:
        A list of ``(condition_id, [asset_id, ...])`` tuples.
    """
    url = base_url or settings.polymarket_rest_url
    result: list[tuple[str, list[str]]] = []

    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.get(f"{url}/sampling-markets", params={"limit": limit})
        resp.raise_for_status()
        body = resp.json()

    # Handle both response formats: array or {data: [...]}
    if isinstance(body, list):
        markets = body
    elif isinstance(body, dict):
        markets = body.get("data", [])
    else:
        logger.warning("Unexpected response format from /sampling-markets", type=type(body).__name__)
        return []

    for market in markets:
        if not isinstance(market, dict):
            continue
        condition_id = market.get("condition_id", "")
        if not condition_id:
            continue
        token_ids: list[str] = []
        for token in market.get("tokens", []):
            tid = token.get("token_id", "")
            if tid:
                token_ids.append(tid)
        if token_ids:
            result.append((condition_id, token_ids))
        # Enforce client-side limit (API may ignore the query param)
        if len(result) >= limit:
            break

    logger.info(
        "Fetched active markets",
        markets=len(result),
        assets=sum(len(aids) for _, aids in result),
    )
    return result


def get_all_condition_ids(conn: Any) -> list[str]:
    """Return all active market condition_ids from the local DB."""
    rows = conn.execute(
        "SELECT market_id FROM markets WHERE active = true ORDER BY market_id"
    ).fetchall()
    return [r[0] for r in rows]
    return result

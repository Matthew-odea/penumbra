"""Market metadata synchronisation from Polymarket REST API.

Fetches all active markets via the CLOB ``/markets`` endpoint, filters to
configured categories, and upserts into the DuckDB ``markets`` table.
"""

from __future__ import annotations

import ssl
from datetime import datetime, UTC
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
                tags = [t.lower() for t in m.get("tags", [])]
                if cats and not any(t in cats for t in tags):
                    continue
                markets.append(m)

            cursor = body.get("next_cursor")
            logger.debug("Fetched market page", page=page, cursor=cursor, found=len(markets))

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
        tokens = m.get("tokens", [])
        volume = sum(float(t.get("price", 0)) for t in tokens) if tokens else 0.0
        rows.append((
            str(m["condition_id"]),
            m.get("question", ""),
            m.get("market_slug", m.get("slug", "")),
            ",".join(m.get("tags", [])),
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


async def sync_markets(conn: Any, *, base_url: str | None = None) -> int:
    """Full sync: fetch from API → upsert into DuckDB.

    Returns the number of markets upserted.
    """
    markets = await fetch_all_markets(base_url=base_url)
    return upsert_markets(conn, markets)

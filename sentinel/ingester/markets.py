"""Market metadata synchronisation from Polymarket REST API.

Fetches ALL active markets via the CLOB ``/markets`` endpoint (no category
filter — attractiveness scoring replaces category-based filtering) and upserts
into the DuckDB ``markets`` table.

Also provides ``get_priority_market_ids()`` which ranks DB markets by the
informed-trading priority formula for use by the hot-tier poller.
"""

from __future__ import annotations

import csv
import os
import ssl
import tempfile
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

# ── Priority scoring SQL ──────────────────────────────────────────────────────
# Weights mirrors the plan:
#   (attractiveness / 100) × time_weight × uncertainty × liquidity_scaling
# epoch() returns unix seconds in DuckDB.

_PRIORITY_SQL = """
SELECT market_id
FROM markets
WHERE active = true
  AND resolved = false
  AND end_date > CURRENT_TIMESTAMP
  AND liquidity_usd >= ?
  AND attractiveness_score IS NOT NULL
  AND attractiveness_score >= ?
ORDER BY
    (attractiveness_score / 100.0)
    * CASE
        WHEN epoch(end_date) - epoch(CURRENT_TIMESTAMP) < 86400    THEN 1.0
        WHEN epoch(end_date) - epoch(CURRENT_TIMESTAMP) < 259200   THEN 0.9
        WHEN epoch(end_date) - epoch(CURRENT_TIMESTAMP) < 604800   THEN 0.8
        WHEN epoch(end_date) - epoch(CURRENT_TIMESTAMP) < 1209600  THEN 0.65
        WHEN epoch(end_date) - epoch(CURRENT_TIMESTAMP) < 2592000  THEN 0.5
        WHEN epoch(end_date) - epoch(CURRENT_TIMESTAMP) < 7776000  THEN 0.35
        WHEN epoch(end_date) - epoch(CURRENT_TIMESTAMP) < 15552000 THEN 0.2
        ELSE 0.1
      END
    * (1.0 - ABS(COALESCE(last_price, 0.5) - 0.5) * 2.0)
    * LEAST(liquidity_usd, 500000.0) / 500000.0
DESC
LIMIT ?
"""

# Fallback when insufficient scored markets exist: top by liquidity
_FALLBACK_SQL = """
SELECT market_id
FROM markets
WHERE active = true
  AND resolved = false
  AND end_date > CURRENT_TIMESTAMP
  AND liquidity_usd >= ?
ORDER BY liquidity_usd DESC
LIMIT ?
"""


async def fetch_all_markets(
    *,
    base_url: str | None = None,
) -> list[dict[str, Any]]:
    """Page through ``/markets`` and return active (non-closed) markets.

    Closed/archived markets are skipped — resolution data for markets we
    already track is handled by ``sync_resolutions()`` instead.

    Args:
        base_url: Override for the REST URL (tests / dry-run).

    Returns:
        List of raw market dicts from the API.
    """
    url = base_url or settings.polymarket_rest_url
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
                if not m.get("active") or m.get("closed") or m.get("archived"):
                    continue
                markets.append(m)

            cursor = body.get("next_cursor")
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


def _extract_yes_price(tokens: list[dict[str, Any]]) -> float | None:
    """Extract the YES token price (current market probability) from tokens array."""
    for token in tokens:
        if isinstance(token, dict) and str(token.get("outcome", "")).lower() == "yes":
            raw = token.get("price")
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    pass
    return None


def _extract_resolved_price(tokens: list[dict[str, Any]]) -> float | None:
    """Extract the resolution price from a closed market's tokens.

    For binary markets the YES token's ``winner`` field is the ground truth.
    Returns 1.0 (YES won), 0.0 (NO won), or None if unresolved / ambiguous.

    The API returns ``"price": 1`` on the winning token, but ``winner`` is
    more reliable because ``price`` can reflect pre-resolution trading.
    """
    for token in tokens:
        if not isinstance(token, dict):
            continue
        outcome = str(token.get("outcome", "")).lower()
        if outcome != "yes":
            continue
        # Prefer the 'winner' flag (explicit boolean from Polymarket)
        winner = token.get("winner")
        if winner is True:
            return 1.0
        if winner is False:
            return 0.0
        # Fallback: on resolved markets the price snaps to 0 or 1
        price = token.get("price")
        if price is not None:
            try:
                p = float(price)
                if p >= 0.95:
                    return 1.0
                if p <= 0.05:
                    return 0.0
            except (TypeError, ValueError):
                pass
    return None


_CSV_COLS = [
    "market_id", "question", "slug", "category", "end_date",
    "volume_usd", "liquidity_usd", "active", "resolved", "resolved_price",
    "last_price", "last_synced", "token_ids",
]

_UPSERT_SQL = """
INSERT INTO markets
    (market_id, question, slug, category, end_date,
     volume_usd, liquidity_usd, active, resolved, resolved_price,
     last_price, last_synced, token_ids)
SELECT
    market_id, question, slug, category,
    NULLIF(end_date, '')::TIMESTAMPTZ,
    volume_usd::DOUBLE, liquidity_usd::DOUBLE,
    active::BOOLEAN, resolved::BOOLEAN,
    NULLIF(resolved_price, '')::DOUBLE,
    NULLIF(last_price, '')::DOUBLE,
    NULLIF(last_synced, '')::TIMESTAMPTZ,
    NULLIF(token_ids, '')
FROM read_csv({path!r}, columns={col_types!r}, header=true, quote='"', escape='"')
ON CONFLICT (market_id) DO UPDATE SET
    question       = excluded.question,
    slug           = excluded.slug,
    category       = excluded.category,
    end_date       = excluded.end_date,
    volume_usd     = excluded.volume_usd,
    liquidity_usd  = excluded.liquidity_usd,
    active         = excluded.active,
    resolved       = excluded.resolved,
    resolved_price = COALESCE(excluded.resolved_price, markets.resolved_price),
    last_price     = excluded.last_price,
    last_synced    = excluded.last_synced,
    token_ids      = excluded.token_ids
"""

# DuckDB column-type hints for the CSV reader (all TEXT so we control casts above)
_COL_TYPES = {c: "TEXT" for c in _CSV_COLS}


def upsert_markets(conn: Any, markets: list[dict[str, Any]]) -> tuple[int, set[str]]:
    """Upsert market metadata into DuckDB.

    Uses a temp CSV file + DuckDB's native C++ CSV reader to load 36k rows as
    a single bulk INSERT … ON CONFLICT DO UPDATE (~200ms) instead of row-by-row
    executemany (~45s).  Attractiveness fields are preserved on conflict (not
    included in the UPDATE SET list).

    Returns (rows_upserted, set_of_upserted_condition_ids).
    """
    if not markets:
        return 0, set()

    now_str = datetime.now(UTC).isoformat()
    rows: list[tuple[str, ...]] = []
    synced_ids: set[str] = set()
    for m in markets:
        tokens = m.get("tokens") or []
        last_price = _extract_yes_price(tokens)
        resolved = bool(m.get("closed", False))
        resolved_price = _extract_resolved_price(tokens) if resolved else None
        active = not resolved and not m.get("archived", False)
        end_dt = _parse_end_date(m.get("end_date_iso"))

        condition_id = str(m["condition_id"])
        synced_ids.add(condition_id)
        token_ids_str = ",".join(
            str(t.get("token_id", ""))
            for t in tokens
            if t.get("token_id")
        )
        rows.append((
            condition_id,
            m.get("question", ""),
            m.get("market_slug", m.get("slug", "")),
            ",".join(t for t in (m.get("tags") or []) if isinstance(t, str)),
            end_dt.isoformat() if end_dt else "",
            str(float(m.get("volume", 0) or 0)),
            str(float(m.get("liquidity", 0) or 0)),
            "true" if active else "false",
            "true" if resolved else "false",
            str(resolved_price) if resolved_price is not None else "",
            str(last_price) if last_price is not None else "",
            now_str,
            token_ids_str,
        ))

    # Write to a temp CSV and use DuckDB's native C++ reader for bulk load.
    # This is ~240× faster than executemany for 36k rows (0.2s vs 45s) because
    # DuckDB's CSV reader bypasses Python's per-row overhead entirely.
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, newline=""
    )
    try:
        writer = csv.writer(tmp)
        writer.writerow(_CSV_COLS)
        writer.writerows(rows)
        tmp.close()
        conn.execute(_UPSERT_SQL.format(path=tmp.name, col_types=_COL_TYPES))
    finally:
        os.unlink(tmp.name)

    logger.info("Markets upserted", count=len(rows))
    return len(rows), synced_ids


async def fetch_market_by_id(
    condition_id: str,
    *,
    base_url: str | None = None,
) -> dict[str, Any] | None:
    """Fetch a single market by condition_id from the REST API."""
    url = base_url or settings.polymarket_rest_url
    async with httpx.AsyncClient(verify=False, timeout=15) as client:
        try:
            resp = await client.get(f"{url}/markets/{condition_id}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data
        except Exception as exc:
            logger.warning(
                "Failed to fetch market by id",
                condition_id=condition_id,
                error=str(exc),
            )
            return None


async def sync_markets(conn: Any, *, base_url: str | None = None) -> int:
    """Full sync: fetch all markets from API → upsert into DuckDB.

    After upserting, marks any previously-active market that was not returned
    by the API as inactive (closed, delisted, or resolved early).  Only runs
    the deactivation pass when at least 50 markets were synced — this guards
    against a partial API response silently deactivating the entire DB.

    Returns the number of markets upserted.
    """
    markets = await fetch_all_markets(base_url=base_url)
    count, synced_ids = upsert_markets(conn, markets)

    if len(synced_ids) > 50:
        synced_list = list(synced_ids)
        placeholders = ",".join("?" * len(synced_list))
        deactivated = conn.execute(
            f"""
            UPDATE markets
            SET active = false
            WHERE active = true
              AND market_id NOT IN ({placeholders})
            """,
            synced_list,
        ).rowcount
        if deactivated:
            logger.info("Deactivated unlisted markets", count=deactivated)

    # Resolve markets that disappeared from the active set
    await sync_resolutions(conn, base_url=base_url)

    return count


async def sync_resolutions(conn: Any, *, base_url: str | None = None) -> int:
    """Fetch resolution data for markets that were deactivated but lack resolved_price.

    Queries the Polymarket API for each unresolved-but-inactive market to check
    if it has closed and extract the outcome.  This is a targeted pass — typically
    only a handful of markets per sync cycle.

    Returns the number of markets resolved.
    """
    rows = conn.execute(
        """
        SELECT m.market_id FROM markets m
        WHERE m.active = false
          AND m.resolved = false
          AND m.resolved_price IS NULL
          AND EXISTS (SELECT 1 FROM signals s WHERE s.market_id = m.market_id)
        LIMIT 50
        """
    ).fetchall()

    if not rows:
        return 0

    resolved_count = 0
    for (market_id,) in rows:
        try:
            m = await fetch_market_by_id(market_id, base_url=base_url)
            if m is None:
                continue
            if not m.get("closed"):
                continue

            tokens = m.get("tokens") or []
            resolved_price = _extract_resolved_price(tokens)
            if resolved_price is not None:
                conn.execute(
                    "UPDATE markets SET resolved = true, resolved_price = ? WHERE market_id = ?",
                    [resolved_price, market_id],
                )
                resolved_count += 1
        except Exception as exc:
            logger.debug("Resolution check failed", market=market_id[:16], error=str(exc))

    if resolved_count:
        logger.info("Markets resolved", count=resolved_count)
    return resolved_count


def get_priority_market_ids(
    conn: Any,
    *,
    limit: int | None = None,
    min_liquidity: float | None = None,
    min_score: int | None = None,
) -> list[str]:
    """Return top-N market condition_ids ranked by insider-trading priority.

    Priority = (attractiveness/100) × time_weight × uncertainty × liquidity_scaling

    Markets without an attractiveness score are excluded.  If fewer than
    ``limit`` scored markets qualify, fills remaining slots from the
    top-liquidity unscored markets so the hot tier is never empty.

    Args:
        conn: DuckDB connection.
        limit: Max markets to return (defaults to settings.hot_market_count).
        min_liquidity: Minimum liquidity_usd (defaults to settings.hot_market_min_liquidity).
        min_score: Minimum attractiveness_score (defaults to settings.hot_market_min_score).

    Returns:
        List of condition_ids in priority order.
    """
    n = limit or settings.hot_market_count
    liq = min_liquidity if min_liquidity is not None else settings.hot_market_min_liquidity
    score_thresh = min_score if min_score is not None else settings.hot_market_min_score

    rows = conn.execute(_PRIORITY_SQL, [liq, score_thresh, n]).fetchall()
    result = [r[0] for r in rows]

    # Fill with liquidity-sorted unscored markets if hot tier is thin
    if len(result) < n:
        needed = n - len(result)
        existing = set(result)
        fallback_rows = conn.execute(_FALLBACK_SQL, [liq, n * 2]).fetchall()
        for r in fallback_rows:
            if r[0] not in existing:
                result.append(r[0])
                if len(result) >= n:
                    break
        if len(result) > len(rows):
            logger.info(
                "Hot tier padded with unscored markets",
                scored=len(rows),
                padded=len(result) - len(rows),
            )

    return result


async def fetch_active_asset_ids(
    *,
    base_url: str | None = None,
    limit: int = 20,
) -> list[str]:
    """Fetch token IDs for the most actively-traded markets.

    Uses the ``/sampling-markets`` endpoint.  Extracts both YES and NO
    token IDs from each market.
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

    Uses the ``/sampling-markets`` endpoint for WS subscription bootstrap only.
    Hot-tier polling now uses ``get_priority_market_ids()`` instead.
    """
    url = base_url or settings.polymarket_rest_url
    result: list[tuple[str, list[str]]] = []

    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.get(f"{url}/sampling-markets", params={"limit": limit})
        resp.raise_for_status()
        body = resp.json()

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
        if len(result) >= limit:
            break

    logger.info(
        "Fetched active markets for WS bootstrap",
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

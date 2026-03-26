"""Price impact calculator.

Measures how much a single trade moved the market price relative to
available liquidity.  Uses DuckDB's ``v_price_impact`` view (if
materialised) or on-the-fly SQL when called per-trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from sentinel.config import settings

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class PriceImpact:
    """Price impact metrics for a single trade."""

    trade_id: str
    market_id: str
    wallet: str
    size_usd: float
    price: float
    price_delta: float
    liquidity_usd: float
    impact_score: float


# ── SQL ─────────────────────────────────────────────────────────────────────

_IMPACT_FOR_TRADE_SQL = """
WITH trade_window AS (
    SELECT
        t.trade_id,
        t.market_id,
        t.wallet,
        t.price,
        t.size_usd,
        t.timestamp,
        LAG(t.price) OVER (PARTITION BY t.market_id ORDER BY t.timestamp) AS prev_price
    FROM trades t
    WHERE t.market_id = ?
      AND t.timestamp >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
)
SELECT
    tw.trade_id,
    tw.market_id,
    tw.wallet,
    tw.size_usd,
    tw.price,
    ABS(tw.price - COALESCE(tw.prev_price, tw.price)) AS price_delta,
    m.liquidity_usd,
    CASE
        WHEN COALESCE(m.liquidity_usd, 0) > 0
        THEN ABS(tw.price - COALESCE(tw.prev_price, tw.price)) / m.liquidity_usd * tw.size_usd
        ELSE ABS(tw.price - COALESCE(tw.prev_price, tw.price)) / ? * tw.size_usd
    END AS impact_score
FROM trade_window tw
JOIN markets m ON tw.market_id = m.market_id
WHERE tw.trade_id = ?
"""

_HIGH_IMPACT_SQL = """
WITH trade_window AS (
    SELECT
        t.trade_id,
        t.market_id,
        t.wallet,
        t.price,
        t.size_usd,
        t.timestamp,
        LAG(t.price) OVER (PARTITION BY t.market_id ORDER BY t.timestamp) AS prev_price
    FROM trades t
    WHERE t.timestamp >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
      AND t.size_usd >= ?
)
SELECT
    tw.trade_id,
    tw.market_id,
    tw.wallet,
    tw.size_usd,
    tw.price,
    ABS(tw.price - COALESCE(tw.prev_price, tw.price)) AS price_delta,
    COALESCE(m.liquidity_usd, 0) AS liquidity_usd,
    CASE
        WHEN COALESCE(m.liquidity_usd, 0) > 0
        THEN ABS(tw.price - COALESCE(tw.prev_price, tw.price)) / m.liquidity_usd * tw.size_usd
        ELSE 0
    END AS impact_score
FROM trade_window tw
LEFT JOIN markets m ON tw.market_id = m.market_id
ORDER BY impact_score DESC
LIMIT ?
"""


def _row_to_impact(row: tuple) -> PriceImpact:
    return PriceImpact(
        trade_id=str(row[0]),
        market_id=str(row[1]),
        wallet=str(row[2]),
        size_usd=float(row[3] or 0),
        price=float(row[4] or 0),
        price_delta=float(row[5] or 0),
        liquidity_usd=float(row[6] or 0),
        impact_score=float(row[7] or 0),
    )


def get_price_impact(conn: Any, market_id: str, trade_id: str) -> PriceImpact | None:
    """Compute the price impact for a specific trade.

    Returns ``None`` if the trade is not found or has no market metadata.
    When ``liquidity_usd`` is 0 (Polymarket API returns null for most markets),
    falls back to ``settings.price_impact_fallback_liquidity_usd`` so the
    impact component is never permanently zeroed out.
    """
    fallback = settings.price_impact_fallback_liquidity_usd
    row = conn.execute(_IMPACT_FOR_TRADE_SQL, [market_id, fallback, trade_id]).fetchone()
    return _row_to_impact(row) if row else None


def get_high_impact_trades(
    conn: Any,
    *,
    min_size_usd: float | None = None,
    limit: int = 50,
) -> list[PriceImpact]:
    """Return the top *limit* trades by price impact score.

    Only trades with ``size_usd >= min_size_usd`` are considered (default:
    ``settings.min_trade_size_usd`` = 500).
    """
    min_size = min_size_usd if min_size_usd is not None else settings.min_trade_size_usd
    rows = conn.execute(_HIGH_IMPACT_SQL, [min_size, limit]).fetchall()
    impacts = [_row_to_impact(r) for r in rows]
    if impacts:
        logger.debug("High-impact trades", count=len(impacts), min_size_usd=min_size)
    return impacts


def compute_impact_score(
    price_delta: float,
    liquidity_usd: float,
    size_usd: float,
) -> float:
    """Pure-Python impact calculation (for use without DuckDB).

    Formula: |ΔP| / L × V
    """
    if liquidity_usd <= 0:
        return 0.0
    return abs(price_delta) / liquidity_usd * size_usd

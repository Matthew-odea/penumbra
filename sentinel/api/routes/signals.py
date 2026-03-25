"""Signal endpoints — enriched feed for the dashboard."""

from __future__ import annotations

import json

from fastapi import APIRouter, Query

from sentinel.api.deps import get_db

router = APIRouter(tags=["signals"])


@router.get("/signals")
async def list_signals(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    min_score: int = Query(0, ge=0, le=100),
    market_id: str | None = Query(None),
    wallet: str | None = Query(None),
) -> list[dict]:
    """Return recent signals joined with reasoning and market metadata.

    Supports filtering by minimum suspicion score, market, or wallet.
    """
    db = get_db()

    where_clauses = ["1=1"]
    params: list = []
    if min_score > 0:
        where_clauses.append("COALESCE(sr.suspicion_score, s.statistical_score) >= ?")
        params.append(min_score)
    if market_id:
        where_clauses.append("s.market_id = ?")
        params.append(market_id)
    if wallet:
        where_clauses.append("s.wallet = ?")
        params.append(wallet)

    where_sql = " AND ".join(where_clauses)

    rows = db.execute(
        f"""
        SELECT
            s.signal_id,
            s.trade_id,
            s.market_id,
            s.wallet,
            s.side,
            s.price,
            s.size_usd,
            s.trade_timestamp,
            s.modified_z_score,
            s.price_impact,
            s.wallet_win_rate,
            s.wallet_total_trades,
            s.is_whitelisted,
            s.funding_anomaly,
            s.funding_age_minutes,
            s.statistical_score,
            s.created_at,
            -- enriched
            s.ofi_score,
            s.hours_to_resolution,
            s.market_concentration,
            -- reasoning
            sr.classification,
            sr.tier1_confidence,
            sr.suspicion_score,
            sr.reasoning,
            sr.key_evidence,
            sr.news_headlines,
            sr.tier1_model,
            sr.tier2_model,
            -- market
            m.question AS market_question,
            m.category,
            m.liquidity_usd AS market_liquidity
        FROM signals s
        LEFT JOIN signal_reasoning sr ON s.signal_id = sr.signal_id
        LEFT JOIN markets m ON s.market_id = m.market_id
        WHERE {where_sql}
        ORDER BY s.created_at DESC
        LIMIT ?
        OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()

    columns = [
        "signal_id", "trade_id", "market_id", "wallet", "side", "price",
        "size_usd", "trade_timestamp", "modified_z_score", "price_impact",
        "wallet_win_rate", "wallet_total_trades", "is_whitelisted",
        "funding_anomaly", "funding_age_minutes", "statistical_score",
        "created_at",
        "ofi_score", "hours_to_resolution", "market_concentration",
        "classification", "tier1_confidence", "suspicion_score",
        "reasoning", "key_evidence", "news_headlines", "tier1_model", "tier2_model",
        "market_question", "category", "market_liquidity",
    ]

    result = []
    for row in rows:
        d = dict(zip(columns, row))
        # Coerce Decimal/datetime to JSON-friendly types
        for k in ("price", "size_usd", "modified_z_score", "price_impact",
                   "wallet_win_rate", "market_liquidity",
                   "ofi_score", "market_concentration"):
            if d[k] is not None:
                d[k] = float(d[k])
        for k in ("trade_timestamp", "created_at"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        # Parse news_headlines from stored JSON string to list
        raw_headlines = d.get("news_headlines")
        if raw_headlines:
            try:
                d["news_headlines"] = json.loads(raw_headlines)
            except (json.JSONDecodeError, TypeError):
                d["news_headlines"] = []
        else:
            d["news_headlines"] = []
        result.append(d)

    return result


@router.get("/signals/stats")
async def signal_stats() -> dict:
    """Aggregate stats for the summary cards."""
    db = get_db()

    row = db.execute("""
        SELECT
            COUNT(*) AS total_signals,
            COUNT(*) FILTER (
                WHERE sr.suspicion_score >= 80
            ) AS high_suspicion,
            COUNT(DISTINCT s.market_id) AS active_markets
        FROM signals s
        LEFT JOIN signal_reasoning sr ON s.signal_id = sr.signal_id
        WHERE s.created_at >= CURRENT_DATE
    """).fetchone()

    return {
        "total_signals_today": row[0],
        "high_suspicion_today": row[1],
        "active_markets": row[2],
    }

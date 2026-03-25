"""Market endpoints — detail + volume history."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from sentinel.api.deps import get_db

router = APIRouter(tags=["markets"])


@router.get("/markets")
async def list_markets(
    limit: int = Query(50, ge=1, le=500),
    active_only: bool = Query(True),
) -> list[dict]:
    """List tracked markets, ordered by recent signal activity."""
    db = get_db()

    where = "WHERE m.active = TRUE" if active_only else ""

    rows = db.execute(
        f"""
        SELECT
            m.market_id,
            m.question,
            m.category,
            m.volume_usd,
            m.liquidity_usd,
            m.active,
            m.end_date,
            COUNT(s.signal_id) AS signal_count,
            MAX(s.created_at) AS last_signal_at
        FROM markets m
        LEFT JOIN signals s ON m.market_id = s.market_id
        {where}
        GROUP BY m.market_id, m.question, m.category,
                 m.volume_usd, m.liquidity_usd, m.active, m.end_date
        ORDER BY last_signal_at DESC NULLS LAST
        LIMIT ?
        """,
        [limit],
    ).fetchall()

    columns = [
        "market_id", "question", "category", "volume_usd", "liquidity_usd",
        "active", "end_date", "signal_count", "last_signal_at",
    ]

    result = []
    for row in rows:
        d = dict(zip(columns, row))
        for k in ("volume_usd", "liquidity_usd"):
            if d[k] is not None:
                d[k] = float(d[k])
        for k in ("end_date", "last_signal_at"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        result.append(d)
    return result


@router.get("/markets/{market_id}")
async def get_market(market_id: str) -> dict:
    """Market detail with recent signals and volume data."""
    db = get_db()

    market_row = db.execute(
        """
        SELECT market_id, question, category, volume_usd, liquidity_usd,
               active, resolved, resolved_price, end_date
        FROM markets WHERE market_id = ?
        """,
        [market_id],
    ).fetchone()

    if not market_row:
        raise HTTPException(status_code=404, detail="Market not found")

    market = dict(zip(
        ["market_id", "question", "category", "volume_usd", "liquidity_usd",
         "active", "resolved", "resolved_price", "end_date"],
        market_row,
    ))
    for k in ("volume_usd", "liquidity_usd", "resolved_price"):
        if market[k] is not None:
            market[k] = float(market[k])
    if market["end_date"] is not None:
        market["end_date"] = market["end_date"].isoformat()

    return market


@router.get("/markets/{market_id}/volume")
async def get_market_volume(
    market_id: str,
    hours: int = Query(24, ge=1, le=168),
) -> list[dict]:
    """Hourly volume data for a market (for charting)."""
    db = get_db()

    rows = db.execute(
        """
        SELECT
            date_trunc('hour', timestamp) AS hour,
            COUNT(*) AS trade_count,
            SUM(size_usd) AS volume_usd,
            COUNT(DISTINCT wallet) AS unique_wallets
        FROM trades
        WHERE market_id = ?
          AND timestamp >= CURRENT_TIMESTAMP - INTERVAL (? || ' hours')
        GROUP BY 1
        ORDER BY 1
        """,
        [market_id, hours],
    ).fetchall()

    return [
        {
            "hour": r[0].isoformat(),
            "trade_count": r[1],
            "volume_usd": float(r[2]) if r[2] else 0,
            "unique_wallets": r[3],
        }
        for r in rows
    ]


@router.get("/markets/{market_id}/anomalies")
async def get_market_anomalies(market_id: str) -> list[dict]:
    """Volume anomaly Z-scores for a market (last 24h, from v_volume_anomalies)."""
    db = get_db()

    rows = db.execute(
        """
        SELECT hour_bucket, volume_usd, trade_count, modified_z_score
        FROM v_volume_anomalies
        WHERE market_id = ?
        ORDER BY hour_bucket
        """,
        [market_id],
    ).fetchall()

    return [
        {
            "hour": r[0].isoformat(),
            "volume_usd": float(r[1]) if r[1] else 0.0,
            "trade_count": r[2],
            "z_score": round(float(r[3]), 2) if r[3] else 0.0,
        }
        for r in rows
    ]


@router.get("/markets/{market_id}/signals")
async def get_market_signals(
    market_id: str,
    limit: int = Query(50, ge=1, le=200),
) -> list[dict]:
    """Signals for a specific market."""
    from sentinel.api.routes.signals import list_signals
    return await list_signals(limit=limit, offset=0, min_score=0, market_id=market_id, wallet=None)

"""Market endpoints — list, detail, watchlist, volume, anomalies, signals."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query

from sentinel.api.deps import get_db, to_iso
from sentinel.config import settings

router = APIRouter(tags=["markets"])


def _hours_to_resolution(end_date: datetime | None) -> int | None:
    if end_date is None:
        return None
    now = datetime.now(UTC)
    # end_date may be naive (no tzinfo) from DuckDB
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)
    delta = end_date - now
    return max(0, int(delta.total_seconds() / 3600))


def _tier(
    attractiveness_score: int | None,
    active: bool,
    resolved: bool,
    liquidity_usd: float | None,
    end_date: datetime | None,
) -> str:
    """Compute display tier: hot / scored / unscored."""
    if attractiveness_score is None:
        return "unscored"
    if end_date is not None and end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    if (
        active
        and not resolved
        and attractiveness_score >= settings.hot_market_min_score
        and (liquidity_usd or 0) >= settings.hot_market_min_liquidity
        and end_date is not None
        and end_date > now
    ):
        return "hot"
    return "scored"


@router.get("/markets")
async def list_markets(
    limit: int = Query(200, ge=1, le=2000),
    active_only: bool = Query(True),
    tier: str | None = Query(None, description="hot | scored | unscored"),
    min_score: int | None = Query(None, ge=0),
    sort: str = Query("signals", description="signals | priority | liquidity | resolution"),
) -> list[dict]:
    """List tracked markets with attractiveness scores and tier information."""
    db = get_db()

    where_parts = []
    params: list = []
    if active_only:
        where_parts.append("m.active = TRUE")
    if min_score is not None:
        where_parts.append("m.attractiveness_score >= ?")
        params.append(min_score)

    where_sql = "WHERE " + " AND ".join(where_parts) if where_parts else ""

    # Order clause
    if sort == "priority":
        order_sql = """
            ORDER BY
                COALESCE(m.attractiveness_score, 0) / 100.0
                * CASE
                    WHEN m.end_date IS NULL THEN 0.1
                    WHEN epoch(m.end_date) - epoch(CURRENT_TIMESTAMP) < 86400    THEN 1.0
                    WHEN epoch(m.end_date) - epoch(CURRENT_TIMESTAMP) < 259200   THEN 0.9
                    WHEN epoch(m.end_date) - epoch(CURRENT_TIMESTAMP) < 604800   THEN 0.8
                    WHEN epoch(m.end_date) - epoch(CURRENT_TIMESTAMP) < 1209600  THEN 0.65
                    WHEN epoch(m.end_date) - epoch(CURRENT_TIMESTAMP) < 2592000  THEN 0.5
                    WHEN epoch(m.end_date) - epoch(CURRENT_TIMESTAMP) < 7776000  THEN 0.35
                    WHEN epoch(m.end_date) - epoch(CURRENT_TIMESTAMP) < 15552000 THEN 0.2
                    ELSE 0.1
                  END
                * (1.0 - ABS(COALESCE(m.last_price, 0.5) - 0.5) * 2.0)
                * LEAST(COALESCE(m.liquidity_usd, 0), 500000.0) / 500000.0
            DESC NULLS LAST
        """
    elif sort == "liquidity":
        order_sql = "ORDER BY m.liquidity_usd DESC NULLS LAST"
    elif sort == "resolution":
        order_sql = "ORDER BY m.end_date ASC NULLS LAST"
    else:  # signals (default)
        order_sql = "ORDER BY last_signal_at DESC NULLS LAST"

    rows = db.execute(
        f"""
        SELECT
            m.market_id,
            m.question,
            m.category,
            m.volume_usd,
            m.liquidity_usd,
            m.active,
            m.resolved,
            m.end_date,
            m.last_price,
            m.attractiveness_score,
            m.attractiveness_reason,
            COUNT(s.signal_id) AS signal_count,
            MAX(s.created_at) AS last_signal_at
        FROM markets m
        LEFT JOIN signals s ON m.market_id = s.market_id
        {where_sql}
        GROUP BY m.market_id, m.question, m.category,
                 m.volume_usd, m.liquidity_usd, m.active, m.resolved, m.end_date,
                 m.last_price, m.attractiveness_score, m.attractiveness_reason
        {order_sql}
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()

    columns = [
        "market_id", "question", "category", "volume_usd", "liquidity_usd",
        "active", "resolved", "end_date", "last_price", "attractiveness_score",
        "attractiveness_reason", "signal_count", "last_signal_at",
    ]

    result = []
    for row in rows:
        d = dict(zip(columns, row))
        for k in ("volume_usd", "liquidity_usd", "last_price"):
            if d[k] is not None:
                d[k] = float(d[k])
        end_dt = d.get("end_date")
        for k in ("end_date", "last_signal_at"):
            if d[k] is not None:
                d[k] = to_iso(d[k])
        d["hours_to_resolution"] = _hours_to_resolution(end_dt)
        d["tier"] = _tier(
            d["attractiveness_score"],
            d["active"],
            d["resolved"],
            d["liquidity_usd"],
            end_dt,
        )
        if tier and d["tier"] != tier:
            continue
        result.append(d)
    return result


@router.get("/watchlist")
async def get_watchlist() -> list[dict]:
    """Current hot-tier markets ranked by insider-trading priority.

    Runs the same priority formula used by the ingester hot-tier refresh.
    Returns full market details plus the computed priority_score for display.
    """
    db = get_db()

    rows = db.execute(
        f"""
        SELECT
            m.market_id,
            m.question,
            m.category,
            m.volume_usd,
            m.liquidity_usd,
            m.active,
            m.resolved,
            m.end_date,
            m.last_price,
            m.attractiveness_score,
            m.attractiveness_reason,
            -- computed priority score for display
            ROUND(
                (m.attractiveness_score / 100.0)
                * CASE
                    WHEN epoch(m.end_date) - epoch(CURRENT_TIMESTAMP) < 86400    THEN 1.0
                    WHEN epoch(m.end_date) - epoch(CURRENT_TIMESTAMP) < 259200   THEN 0.9
                    WHEN epoch(m.end_date) - epoch(CURRENT_TIMESTAMP) < 604800   THEN 0.8
                    WHEN epoch(m.end_date) - epoch(CURRENT_TIMESTAMP) < 1209600  THEN 0.65
                    WHEN epoch(m.end_date) - epoch(CURRENT_TIMESTAMP) < 2592000  THEN 0.5
                    WHEN epoch(m.end_date) - epoch(CURRENT_TIMESTAMP) < 7776000  THEN 0.35
                    WHEN epoch(m.end_date) - epoch(CURRENT_TIMESTAMP) < 15552000 THEN 0.2
                    ELSE 0.1
                  END
                * (1.0 - ABS(COALESCE(m.last_price, 0.5) - 0.5) * 2.0)
                * LEAST(m.liquidity_usd, 500000.0) / 500000.0,
            3) AS priority_score,
            COUNT(s.signal_id) FILTER (
                WHERE s.created_at >= CURRENT_DATE
            ) AS signals_today
        FROM markets m
        LEFT JOIN signals s ON m.market_id = s.market_id
        WHERE m.active = true
          AND m.resolved = false
          AND m.end_date > CURRENT_TIMESTAMP
          AND m.liquidity_usd >= ?
          AND m.attractiveness_score IS NOT NULL
          AND m.attractiveness_score >= ?
        GROUP BY m.market_id, m.question, m.category, m.volume_usd, m.liquidity_usd,
                 m.active, m.resolved, m.end_date, m.last_price,
                 m.attractiveness_score, m.attractiveness_reason
        ORDER BY priority_score DESC NULLS LAST
        LIMIT ?
        """,
        [settings.hot_market_min_liquidity, settings.hot_market_min_score, settings.ws_market_count],
    ).fetchall()

    columns = [
        "market_id", "question", "category", "volume_usd", "liquidity_usd",
        "active", "resolved", "end_date", "last_price",
        "attractiveness_score", "attractiveness_reason",
        "priority_score", "signals_today",
    ]

    result = []
    for row in rows:
        d = dict(zip(columns, row))
        for k in ("volume_usd", "liquidity_usd", "last_price", "priority_score"):
            if d[k] is not None:
                d[k] = float(d[k])
        end_dt = d.get("end_date")
        if end_dt is not None:
            d["end_date"] = to_iso(end_dt)
        d["hours_to_resolution"] = _hours_to_resolution(end_dt)
        result.append(d)
    return result


@router.get("/markets/{market_id}")
async def get_market(market_id: str) -> dict:
    """Market detail with attractiveness score and all metadata."""
    db = get_db()

    market_row = db.execute(
        """
        SELECT market_id, question, category, volume_usd, liquidity_usd,
               active, resolved, resolved_price, end_date,
               last_price, attractiveness_score, attractiveness_reason
        FROM markets WHERE market_id = ?
        """,
        [market_id],
    ).fetchone()

    if not market_row:
        raise HTTPException(status_code=404, detail="Market not found")

    market = dict(zip(
        ["market_id", "question", "category", "volume_usd", "liquidity_usd",
         "active", "resolved", "resolved_price", "end_date",
         "last_price", "attractiveness_score", "attractiveness_reason"],
        market_row,
    ))
    for k in ("volume_usd", "liquidity_usd", "resolved_price", "last_price"):
        if market[k] is not None:
            market[k] = float(market[k])
    end_dt = market.get("end_date")
    if end_dt is not None:
        market["end_date"] = to_iso(end_dt)
    market["hours_to_resolution"] = _hours_to_resolution(end_dt)
    market["tier"] = _tier(
        market["attractiveness_score"],
        market["active"],
        market["resolved"],
        market["liquidity_usd"],
        end_dt,
    )
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
        FROM v_deduped_trades
        WHERE market_id = ?
          AND timestamp >= CURRENT_TIMESTAMP - INTERVAL (? || ' hours')
        GROUP BY 1
        ORDER BY 1
        """,
        [market_id, hours],
    ).fetchall()

    return [
        {
            "hour": to_iso(r[0]),
            "trade_count": r[1],
            "volume_usd": float(r[2]) if r[2] else 0,
            "unique_wallets": r[3],
        }
        for r in rows
    ]


@router.get("/markets/{market_id}/anomalies")
async def get_market_anomalies(market_id: str) -> list[dict]:
    """Volume anomaly Z-scores for a market (last 24h)."""
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
            "hour": to_iso(r[0]),
            "volume_usd": float(r[1]) if r[1] else 0.0,
            "trade_count": r[2],
            "z_score": round(float(r[3]), 2) if r[3] else 0.0,
        }
        for r in rows
    ]


@router.get("/markets/{market_id}/vpin")
async def get_market_vpin(
    market_id: str,
    hours: int = Query(24, ge=1, le=168),
) -> list[dict]:
    """VPIN time series for a market — one point per completed bucket."""
    db = get_db()

    rows = db.execute(
        """
        SELECT
            bucket_end,
            ABS(buy_vol - sell_vol) / NULLIF(bucket_volume, 0) AS imbalance,
            bucket_volume,
            buy_vol,
            sell_vol
        FROM vpin_buckets
        WHERE market_id = ?
          AND bucket_end >= CURRENT_TIMESTAMP - INTERVAL (? || ' hours')
        ORDER BY bucket_idx
        """,
        [market_id, hours],
    ).fetchall()

    return [
        {
            "timestamp": to_iso(r[0]),
            "imbalance": round(float(r[1]), 4) if r[1] is not None else None,
            "bucket_volume": float(r[2]) if r[2] else 0,
            "buy_vol": float(r[3]) if r[3] else 0,
            "sell_vol": float(r[4]) if r[4] else 0,
        }
        for r in rows
    ]


@router.get("/markets/{market_id}/lambda")
async def get_market_lambda(
    market_id: str,
    hours: int = Query(24, ge=1, le=168),
) -> list[dict]:
    """Kyle's Lambda estimates over time for a market."""
    db = get_db()

    rows = db.execute(
        """
        SELECT estimated_at, lambda_value, r_squared, residual_std, n_obs
        FROM market_lambda
        WHERE market_id = ?
          AND estimated_at >= CURRENT_TIMESTAMP - INTERVAL (? || ' hours')
        ORDER BY estimated_at
        """,
        [market_id, hours],
    ).fetchall()

    return [
        {
            "timestamp": to_iso(r[0]),
            "lambda_value": float(r[1]) if r[1] is not None else None,
            "r_squared": float(r[2]) if r[2] is not None else None,
            "residual_std": float(r[3]) if r[3] is not None else None,
            "n_obs": r[4],
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
    return await list_signals(
        limit=limit, offset=0, min_score=0,
        market_id=market_id, wallet=None, hours=None, search=None,
    )

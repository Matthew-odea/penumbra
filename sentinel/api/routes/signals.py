"""Signal endpoints — enriched feed for the dashboard."""

from __future__ import annotations

from fastapi import APIRouter, Query

from sentinel.api.deps import get_db, to_iso

router = APIRouter(tags=["signals"])


def _make_explanation(d: dict) -> str | None:
    """Build a template-based natural language explanation for high-scoring signals."""
    score = d.get("statistical_score") or 0
    if score < 80:
        return None

    parts: list[str] = []

    z = d.get("modified_z_score") or 0.0
    if z > 3.5:
        parts.append(f"volume spike ({z:.1f}x normal)")

    if d.get("funding_anomaly") and d.get("funding_age_minutes") is not None:
        age = d["funding_age_minutes"]
        if age < 60:
            parts.append(f"new wallet (funded {age}min ago)")
        else:
            parts.append(f"new wallet (funded {age // 60}h ago)")

    size_usd = d.get("size_usd") or 0.0
    side = d.get("side") or ""
    parts.append(f"${size_usd:,.0f} {side.lower()}")

    conc = d.get("market_concentration") or 0.0
    if conc >= 0.8:
        parts.append(f"{conc:.0%} concentration on this market")
    elif conc >= 0.5:
        parts.append(f"{conc:.0%} market concentration")

    hours = d.get("hours_to_resolution")
    if hours is not None and hours < 72:
        parts.append(f"{hours}h to resolution")

    if d.get("is_whitelisted"):
        parts.append("known profitable wallet")
    elif (d.get("wallet_win_rate") or 0.0) > 0.65:
        parts.append(f"{d['wallet_win_rate']:.0%} historical win rate")

    ofi = d.get("ofi_score") or 0.0
    if abs(ofi) >= 0.4:
        flow_dir = "buying" if ofi > 0 else "selling"
        if (ofi > 0) != (side.upper() == "BUY"):
            parts.append(f"contrarian trade against {flow_dir} pressure")

    coord = d.get("coordination_wallet_count") or 0
    if coord >= 3:
        parts.append(f"{coord} wallets coordinating")

    if d.get("liquidity_cliff"):
        parts.append("liquidity cliff detected")

    pos = d.get("position_trade_count") or 0
    if pos >= 3:
        parts.append(f"accumulating ({pos} trades same side)")

    return f"High suspicion: {', '.join(parts)}" if parts else None


@router.get("/signals")
async def list_signals(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    min_score: int = Query(0, ge=0, le=100),
    market_id: str | None = Query(None),
    wallet: str | None = Query(None),
    hours: int | None = Query(None, ge=1, le=168, description="Only signals from the last N hours"),
    search: str | None = Query(None, description="Search market question text"),
) -> list[dict]:
    """Return recent signals joined with market metadata.

    Supports filtering by minimum suspicion score, market, or wallet.
    """
    db = get_db()

    where_clauses = ["1=1"]
    params: list = []
    if min_score > 0:
        where_clauses.append("s.statistical_score >= ?")
        params.append(min_score)
    if market_id:
        where_clauses.append("s.market_id = ?")
        params.append(market_id)
    if wallet:
        where_clauses.append("s.wallet = ?")
        params.append(wallet)
    if hours:
        where_clauses.append("s.created_at >= CURRENT_TIMESTAMP - INTERVAL (? || ' hours')")
        params.append(hours)
    if search:
        where_clauses.append("m.question ILIKE ?")
        params.append(f"%{search}%")

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
            s.ofi_score,
            s.hours_to_resolution,
            s.market_concentration,
            s.coordination_wallet_count,
            s.liquidity_cliff,
            s.position_trade_count,
            -- market
            m.question AS market_question,
            m.category,
            m.liquidity_usd AS market_liquidity,
            m.attractiveness_score,
            m.attractiveness_reason
        FROM signals s
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
        "coordination_wallet_count", "liquidity_cliff", "position_trade_count",
        "market_question", "category", "market_liquidity",
        "attractiveness_score", "attractiveness_reason",
    ]

    result = []
    for row in rows:
        d = dict(zip(columns, row, strict=False))
        for k in ("price", "size_usd", "modified_z_score", "price_impact",
                  "wallet_win_rate", "market_liquidity",
                  "ofi_score", "market_concentration"):
            if d[k] is not None:
                d[k] = float(d[k])
        for k in ("trade_timestamp", "created_at"):
            if d[k] is not None:
                d[k] = to_iso(d[k])
        d["explanation"] = _make_explanation(d)
        result.append(d)

    return result


@router.get("/signals/stats")
async def signal_stats() -> dict:
    """Aggregate stats for the summary cards."""
    db = get_db()

    row = db.execute("""
        SELECT
            COUNT(*) AS total_signals,
            COUNT(*) FILTER (WHERE statistical_score >= 80) AS high_suspicion,
            COUNT(DISTINCT market_id) AS active_markets
        FROM signals
        WHERE created_at >= CURRENT_DATE
    """).fetchone()

    return {
        "total_signals_today": row[0] if row else 0,
        "high_suspicion_today": row[1] if row else 0,
        "active_markets": row[2] if row else 0,
    }


@router.get("/signals/{signal_id}")
async def get_signal(signal_id: str) -> dict:
    """Return a single signal with all feature values and market metadata."""
    from fastapi import HTTPException

    db = get_db()
    row = db.execute(
        """
        SELECT
            s.*,
            m.question AS market_question,
            m.category,
            m.liquidity_usd AS market_liquidity,
            m.attractiveness_score,
            m.attractiveness_reason,
            m.resolved,
            m.resolved_price
        FROM signals s
        LEFT JOIN markets m ON s.market_id = m.market_id
        WHERE s.signal_id = ?
        """,
        [signal_id],
    ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Signal not found")

    columns = [desc[0] for desc in db.execute(
        """
        SELECT
            s.*,
            m.question AS market_question,
            m.category,
            m.liquidity_usd AS market_liquidity,
            m.attractiveness_score,
            m.attractiveness_reason,
            m.resolved,
            m.resolved_price
        FROM signals s
        LEFT JOIN markets m ON s.market_id = m.market_id
        LIMIT 0
        """
    ).description]

    d = dict(zip(columns, row, strict=False))
    for k in ("price", "size_usd", "volume_z_score", "modified_z_score",
              "price_impact", "wallet_win_rate", "market_liquidity",
              "ofi_score", "market_concentration", "vpin_percentile",
              "lambda_value", "resolved_price"):
        if k in d and d[k] is not None:
            d[k] = float(d[k])
    for k in ("trade_timestamp", "created_at"):
        if k in d and d[k] is not None:
            d[k] = to_iso(d[k])
    d["explanation"] = _make_explanation(d)
    return d

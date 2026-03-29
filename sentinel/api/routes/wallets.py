"""Wallet endpoints — profiling and trade history."""

from __future__ import annotations

from fastapi import APIRouter, Query

from sentinel.api.deps import get_db

router = APIRouter(tags=["wallets"])


@router.get("/wallets")
async def list_wallets(
    limit: int = Query(50, ge=1, le=200),
) -> list[dict]:
    """Wallet leaderboard ranked by win_rate × resolved_trades (smart-money score)."""
    db = get_db()

    rows = db.execute(
        """
        SELECT
            vwp.wallet,
            vwp.total_resolved_trades,
            vwp.wins,
            vwp.win_rate,
            COUNT(DISTINCT t.trade_id)   AS total_trades,
            COUNT(DISTINCT s.signal_id)  AS signal_count,
            CASE
                WHEN COUNT(DISTINCT t.trade_id) > 0
                THEN COUNT(DISTINCT s.signal_id)::FLOAT / COUNT(DISTINCT t.trade_id)
                ELSE 0.0
            END AS signal_hit_rate
        FROM v_wallet_performance vwp
        LEFT JOIN v_deduped_trades t ON vwp.wallet = t.wallet
        LEFT JOIN signals s ON vwp.wallet = s.wallet
        GROUP BY vwp.wallet, vwp.total_resolved_trades, vwp.wins, vwp.win_rate
        ORDER BY vwp.win_rate * vwp.total_resolved_trades DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()

    columns = [
        "wallet", "resolved_trades", "wins", "win_rate",
        "total_trades", "signal_count", "signal_hit_rate",
    ]
    result = []
    for row in rows:
        d = dict(zip(columns, row))
        for k in ("win_rate", "signal_hit_rate"):
            if d[k] is not None:
                d[k] = float(d[k])
        result.append(d)
    return result


@router.get("/wallets/{address}")
async def get_wallet(address: str) -> dict:
    """Wallet profile: win rate, trade count, category breakdown, signals."""
    db = get_db()

    # Overall performance
    perf_row = db.execute(
        """
        SELECT
            wallet,
            total_resolved_trades,
            wins,
            win_rate
        FROM v_wallet_performance
        WHERE wallet = ?
        """,
        [address],
    ).fetchone()

    # Total trades (including unresolved)
    total_row = db.execute(
        "SELECT COUNT(*) FROM v_deduped_trades WHERE wallet = ?",
        [address],
    ).fetchone()

    # Category breakdown
    cat_rows = db.execute(
        """
        SELECT
            COALESCE(m.category, 'Unknown') AS category,
            COUNT(*) AS trades,
            SUM(t.size_usd) AS volume_usd
        FROM v_deduped_trades t
        LEFT JOIN markets m ON t.market_id = m.market_id
        WHERE t.wallet = ?
        GROUP BY 1
        ORDER BY volume_usd DESC
        """,
        [address],
    ).fetchall()

    # Signal count
    signal_count = db.execute(
        "SELECT COUNT(*) FROM signals WHERE wallet = ?",
        [address],
    ).fetchone()[0]

    return {
        "wallet": address,
        "total_trades": total_row[0] if total_row else 0,
        "resolved_trades": perf_row[1] if perf_row else 0,
        "wins": perf_row[2] if perf_row else 0,
        "win_rate": float(perf_row[3]) if perf_row else None,
        "signal_count": signal_count,
        "categories": [
            {
                "category": r[0],
                "trades": r[1],
                "volume_usd": float(r[2]) if r[2] else 0,
            }
            for r in cat_rows
        ],
    }


@router.get("/wallets/{address}/trades")
async def get_wallet_trades(
    address: str,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict]:
    """Recent trades for a wallet."""
    db = get_db()

    rows = db.execute(
        """
        SELECT
            t.trade_id,
            t.market_id,
            t.side,
            t.price,
            t.size_usd,
            t.timestamp,
            m.question AS market_question,
            m.category,
            m.resolved,
            m.resolved_price
        FROM v_deduped_trades t
        LEFT JOIN markets m ON t.market_id = m.market_id
        WHERE t.wallet = ?
        ORDER BY t.timestamp DESC
        LIMIT ?
        """,
        [address, limit],
    ).fetchall()

    columns = [
        "trade_id", "market_id", "side", "price", "size_usd",
        "timestamp", "market_question", "category", "resolved", "resolved_price",
    ]
    result = []
    for row in rows:
        d = dict(zip(columns, row))
        for k in ("price", "size_usd", "resolved_price"):
            if d[k] is not None:
                d[k] = float(d[k])
        if d["timestamp"] is not None:
            d["timestamp"] = d["timestamp"].isoformat()
        result.append(d)
    return result


@router.get("/wallets/{address}/signals")
async def get_wallet_signals(
    address: str,
    limit: int = Query(50, ge=1, le=200),
) -> list[dict]:
    """Signals for a specific wallet."""
    from sentinel.api.routes.signals import list_signals
    return await list_signals(limit=limit, offset=0, min_score=0, market_id=None, wallet=address)

"""Volume anomaly detection via Modified Z-Scores.

Queries DuckDB's ``v_volume_anomalies`` view and exposes a simple API for
the scanner pipeline to retrieve anomalous market–hour buckets.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from sentinel.config import settings

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class VolumeAnomaly:
    """A single market–hour bucket that exceeds the Z-score threshold."""

    market_id: str
    hour_bucket: datetime
    volume_usd: float
    trade_count: int
    unique_wallets: int
    median_vol: float
    mad_vol: float
    modified_z_score: float


# ── Core queries ────────────────────────────────────────────────────────────


_ANOMALIES_SQL = """
SELECT
    market_id,
    hour_bucket,
    volume_usd,
    trade_count,
    unique_wallets,
    median_vol,
    mad_vol,
    modified_z_score
FROM v_volume_anomalies
WHERE modified_z_score >= ?
ORDER BY modified_z_score DESC
"""

_ANOMALY_FOR_MARKET_SQL = """
SELECT
    market_id,
    hour_bucket,
    volume_usd,
    trade_count,
    unique_wallets,
    median_vol,
    mad_vol,
    modified_z_score
FROM v_volume_anomalies
WHERE market_id = ?
ORDER BY hour_bucket DESC
LIMIT 1
"""

_ZSCORE_FOR_MARKET_SQL = """
SELECT modified_z_score
FROM v_volume_anomalies
WHERE market_id = ?
ORDER BY hour_bucket DESC
LIMIT 1
"""


def _row_to_anomaly(row: tuple) -> VolumeAnomaly:
    """Convert a DuckDB row tuple into a ``VolumeAnomaly``."""
    return VolumeAnomaly(
        market_id=str(row[0]),
        hour_bucket=row[1],
        volume_usd=float(row[2] or 0),
        trade_count=int(row[3] or 0),
        unique_wallets=int(row[4] or 0),
        median_vol=float(row[5] or 0),
        mad_vol=float(row[6] or 0),
        modified_z_score=float(row[7] or 0),
    )


def get_anomalies(
    conn: Any,
    *,
    threshold: float | None = None,
) -> list[VolumeAnomaly]:
    """Return all market–hour buckets whose modified Z-score ≥ *threshold*.

    Defaults to ``settings.zscore_threshold`` (3.5).
    """
    threshold = threshold if threshold is not None else settings.zscore_threshold
    rows = conn.execute(_ANOMALIES_SQL, [threshold]).fetchall()
    anomalies = [_row_to_anomaly(r) for r in rows]
    if anomalies:
        logger.info(
            "Volume anomalies detected",
            count=len(anomalies),
            threshold=threshold,
        )
    return anomalies


def get_zscore_for_market(conn: Any, market_id: str) -> float:
    """Return the latest modified Z-score for a single market.

    Returns 0.0 if the market has no recent trade data.
    """
    row = conn.execute(_ZSCORE_FOR_MARKET_SQL, [market_id]).fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def get_anomaly_for_market(conn: Any, market_id: str) -> VolumeAnomaly | None:
    """Return the latest volume stats for a single market (regardless of threshold)."""
    row = conn.execute(_ANOMALY_FOR_MARKET_SQL, [market_id]).fetchone()
    return _row_to_anomaly(row) if row else None


# ── Order Flow Imbalance ────────────────────────────────────────────────────

_OFI_SQL = """
SELECT
    SUM(CASE WHEN side = 'BUY'  THEN size_usd ELSE 0 END) AS buy_vol,
    SUM(CASE WHEN side = 'SELL' THEN size_usd ELSE 0 END) AS sell_vol,
    SUM(size_usd) AS total_vol,
    COUNT(*) AS trade_count
FROM v_deduped_trades
WHERE market_id = ?
  AND timestamp >= CURRENT_TIMESTAMP - INTERVAL '1 hour'
"""

# Minimum trades in the OFI window for the signal to be meaningful.
# With fewer trades, OFI approaches ±1.0 by construction and would
# always trigger the multiplier.
_OFI_MIN_TRADES = 5

_CONCENTRATION_SQL = """
SELECT
    COUNT(*) FILTER (WHERE market_id = ?) * 1.0 / COUNT(*) AS concentration
FROM (
    SELECT market_id
    FROM v_deduped_trades
    WHERE wallet = ?
    ORDER BY timestamp DESC
    LIMIT 50
) recent
"""

_HOURS_TO_RESOLUTION_SQL = """
SELECT end_date
FROM markets
WHERE market_id = ?
"""


def get_ofi_for_market(conn: Any, market_id: str) -> float:
    """Return order flow imbalance in [-1, 1] for the last hour.

    Positive = net buying pressure; negative = net selling.
    Returns 0.0 (neutral) when fewer than ``_OFI_MIN_TRADES`` trades exist
    in the window, since OFI with 1-2 trades is structurally +-1.0 and
    would always trigger the scorer's OFI multiplier.
    """
    row = conn.execute(_OFI_SQL, [market_id]).fetchone()
    if not row or not row[2] or float(row[2]) == 0:
        return 0.0
    trade_count = int(row[3] or 0)
    if trade_count < _OFI_MIN_TRADES:
        return 0.0
    buy_vol = float(row[0] or 0)
    sell_vol = float(row[1] or 0)
    total_vol = float(row[2])
    return (buy_vol - sell_vol) / total_vol


def get_market_concentration(conn: Any, wallet: str, market_id: str) -> float:
    """Return fraction of wallet's last 50 trades that are on this market.

    High concentration (≥ 0.5) suggests domain-specific informed trading.
    Returns 0.0 if no trade history.
    """
    row = conn.execute(_CONCENTRATION_SQL, [market_id, wallet]).fetchone()
    if not row or row[0] is None:
        return 0.0
    return float(row[0])


_ZSCORE_5M_FOR_MARKET_SQL = """
SELECT modified_z_score
FROM v_volume_anomalies_5m
WHERE market_id = ?
ORDER BY hour_bucket DESC
LIMIT 1
"""


def get_zscore_5m_for_market(conn: Any, market_id: str) -> float:
    """Return the latest 5-minute modified Z-score for a single market.

    Returns 0.0 if the market has no data in the current 5-min window.
    """
    row = conn.execute(_ZSCORE_5M_FOR_MARKET_SQL, [market_id]).fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


_TRADE_SIZE_PERCENTILE_SQL = """
SELECT
    (SELECT COUNT(*) FROM v_deduped_trades
     WHERE market_id = ? AND timestamp >= CURRENT_TIMESTAMP - INTERVAL '7 days'
       AND size_usd <= ?) * 1.0
    /
    NULLIF((SELECT COUNT(*) FROM v_deduped_trades
     WHERE market_id = ? AND timestamp >= CURRENT_TIMESTAMP - INTERVAL '7 days'), 0)
"""


def get_trade_size_percentile(conn: Any, market_id: str, size_usd: float) -> float:
    """Return this trade's size percentile vs the market's 7-day history.

    Returns 0.5 (neutral) if no data exists for the market.
    """
    row = conn.execute(_TRADE_SIZE_PERCENTILE_SQL, [market_id, size_usd, market_id]).fetchone()
    return float(row[0]) if row and row[0] is not None else 0.5


_COORDINATION_SQL = """
SELECT
    COUNT(DISTINCT wallet) AS wallet_count,
    SUM(size_usd) AS collective_volume_usd
FROM v_deduped_trades
WHERE market_id = ?
  AND side = ?
  AND wallet != ''
  AND wallet != ?
  AND timestamp >= CURRENT_TIMESTAMP - INTERVAL '10 minutes'
  AND to_timestamp(FLOOR(epoch(timestamp) / 300) * 300) = (
      SELECT to_timestamp(FLOOR(epoch(timestamp) / 300) * 300)
      FROM v_deduped_trades
      WHERE market_id = ?
        AND side = ?
        AND wallet != ''
        AND timestamp >= CURRENT_TIMESTAMP - INTERVAL '10 minutes'
      GROUP BY 1
      ORDER BY 1 DESC
      LIMIT 1
  )
HAVING COUNT(DISTINCT wallet) >= 3
"""


def get_coordination_signal(
    conn: Any, market_id: str, side: str, exclude_wallet: str = "",
) -> tuple[int, float] | None:
    """Return (wallet_count, collective_volume_usd) for the most recent
    5-min coordination window on this market+side, excluding *exclude_wallet*.

    Excluding the triggering wallet prevents a single whale making multiple
    trades from appearing as "coordination" with itself.

    Returns None if no coordination detected (< 3 distinct wallets).
    """
    row = conn.execute(
        _COORDINATION_SQL, [market_id, side, exclude_wallet, market_id, side],
    ).fetchone()
    if not row:
        return None
    return int(row[0] or 0), float(row[1] or 0)


_LIQUIDITY_CLIFF_SQL = """
WITH recent AS (
    SELECT
        best_ask - best_bid AS spread,
        ts
    FROM book_snapshots
    WHERE market_id = ?
      AND ts >= CURRENT_TIMESTAMP - INTERVAL '10 minutes'
),
spread_stats AS (
    SELECT
        (SELECT spread FROM recent ORDER BY ts DESC LIMIT 1) AS current_spread,
        MIN(spread) AS min_spread
    FROM recent
)
SELECT
    current_spread,
    min_spread,
    CASE
        WHEN min_spread > 0
        THEN (current_spread - min_spread) / min_spread
        ELSE 0
    END AS spread_change_pct
FROM spread_stats
WHERE min_spread IS NOT NULL
"""


def get_liquidity_cliff(conn: Any, market_id: str) -> tuple[bool, float]:
    """Return (is_cliff, spread_change_pct) for the given market.

    A liquidity cliff is defined as the spread widening by >30% in the last
    10 minutes — indicating market makers withdrawing liquidity before an
    informed trade.

    Returns (False, 0.0) if insufficient snapshot data (< 2 snapshots).
    """
    row = conn.execute(_LIQUIDITY_CLIFF_SQL, [market_id]).fetchone()
    if not row:
        return False, 0.0
    spread_change_pct = float(row[2] or 0)
    return spread_change_pct > 0.30, spread_change_pct


def get_hours_to_resolution(conn: Any, market_id: str, trade_timestamp: Any) -> int | None:
    """Return hours between trade_timestamp and market end_date.

    Returns None if end_date is unknown or already passed.
    """
    from datetime import UTC

    row = conn.execute(_HOURS_TO_RESOLUTION_SQL, [market_id]).fetchone()
    if not row or row[0] is None:
        return None
    end_date = row[0]
    # Ensure both are timezone-aware
    if hasattr(end_date, "tzinfo") and end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)
    if hasattr(trade_timestamp, "tzinfo") and trade_timestamp.tzinfo is None:
        trade_timestamp = trade_timestamp.replace(tzinfo=UTC)
    delta_seconds = (end_date - trade_timestamp).total_seconds()
    if delta_seconds <= 0:
        return None  # Market already resolved
    return int(delta_seconds / 3600)


# ── Position Accumulation ─────────────────────────────────────────────────

_POSITION_SQL = """
SELECT trade_count, trades_per_hour
FROM v_wallet_positions
WHERE wallet = ?
  AND market_id = ?
  AND side = ?
"""


def get_position_signal(
    conn: Any, wallet: str, market_id: str, side: str,
) -> tuple[int, float] | None:
    """Return (trade_count, trades_per_hour) if wallet is building a position.

    Returns None if the wallet has fewer than 3 trades on this market+side
    in the last 7 days (the view's HAVING clause filters these out).
    """
    row = conn.execute(_POSITION_SQL, [wallet, market_id, side]).fetchone()
    if not row:
        return None
    return int(row[0] or 0), float(row[1] or 0)

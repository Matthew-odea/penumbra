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
    return float(row[0]) if row else 0.0


def get_anomaly_for_market(conn: Any, market_id: str) -> VolumeAnomaly | None:
    """Return the latest volume stats for a single market (regardless of threshold)."""
    row = conn.execute(_ANOMALY_FOR_MARKET_SQL, [market_id]).fetchone()
    return _row_to_anomaly(row) if row else None

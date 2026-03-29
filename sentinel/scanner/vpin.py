"""VPIN (Volume-Synchronized Probability of Informed Trading).

Easley, Lopez de Prado, O'Hara (2012): "Flow Toxicity and Liquidity
in a High-frequency World."

Computes order flow toxicity using volume-synchronized buckets and
Polymarket's native trade aggressor labels (bypasses BVC criticism
from Andersen & Bondarenko 2014).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from sentinel.config import settings

logger = structlog.get_logger()

_INSERT_BUCKET_SQL = """
INSERT INTO vpin_buckets (market_id, bucket_idx, bucket_end, buy_vol, sell_vol, bucket_volume)
VALUES (?, ?, ?, ?, ?, ?)
"""

_VPIN_SQL = """
SELECT
    AVG(ABS(buy_vol - sell_vol) / NULLIF(bucket_volume, 0)) AS vpin
FROM (
    SELECT buy_vol, sell_vol, bucket_volume
    FROM vpin_buckets
    WHERE market_id = ?
    ORDER BY bucket_idx DESC
    LIMIT ?
)
"""

_BUCKET_COUNT_SQL = """
SELECT COUNT(*) FROM vpin_buckets WHERE market_id = ?
"""

_MAX_BUCKET_IDX_SQL = """
SELECT MAX(bucket_idx) FROM vpin_buckets WHERE market_id = ?
"""

_AVG_DAILY_VOLUME_SQL = """
SELECT SUM(size_usd) / NULLIF(COUNT(DISTINCT date_trunc('day', timestamp)), 0)
FROM v_deduped_trades
WHERE market_id = ?
  AND timestamp >= CURRENT_TIMESTAMP - INTERVAL '7 days'
"""

_VPIN_PERCENTILE_SQL = """
WITH rolling AS (
    SELECT bucket_idx,
        AVG(ABS(buy_vol - sell_vol) / NULLIF(bucket_volume, 0))
            OVER (ORDER BY bucket_idx ROWS BETWEEN ? PRECEDING AND CURRENT ROW) AS vpin
    FROM vpin_buckets
    WHERE market_id = ?
      AND bucket_end >= CURRENT_TIMESTAMP - INTERVAL '7 days'
)
SELECT
    COUNT(*) FILTER (WHERE vpin <= ?) * 1.0 / NULLIF(COUNT(*), 0)
FROM rolling
"""


@dataclass
class _BucketState:
    """In-memory state for a market's current (unfilled) VPIN bucket."""

    buy_vol: float = 0.0
    sell_vol: float = 0.0
    bucket_size: float = 0.0
    bucket_idx: int = 0
    last_refreshed: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))


class VPINTracker:
    """Manages VPIN bucket accumulation and computation per market.

    Usage::

        tracker = VPINTracker(conn)
        tracker.add_trade(market_id, side, size_usd, timestamp)
        vpin = tracker.get_vpin(market_id)            # float or None
        pctile = tracker.get_vpin_percentile(market_id)  # float [0,1] or None
    """

    def __init__(
        self,
        conn: Any,
        *,
        default_bucket_size: float | None = None,
        min_buckets: int | None = None,
        lookback_buckets: int | None = None,
    ) -> None:
        self._conn = conn
        self._default_bucket_size = default_bucket_size or settings.vpin_min_bucket_size
        self._min_buckets = min_buckets or settings.vpin_min_buckets
        self._lookback_buckets = lookback_buckets or settings.vpin_lookback_buckets
        self._buckets: dict[str, _BucketState] = {}

    def _get_or_create_state(self, market_id: str) -> _BucketState:
        """Get or initialize bucket state for a market."""
        if market_id in self._buckets:
            state = self._buckets[market_id]
            # Refresh bucket_size hourly
            now = datetime.now(tz=UTC)
            if (now - state.last_refreshed) > timedelta(hours=1):
                state.bucket_size = self._compute_bucket_size(market_id)
                state.last_refreshed = now
            return state

        # Initialize: recover bucket_idx from DB, compute bucket_size
        bucket_size = self._compute_bucket_size(market_id)
        row = self._conn.execute(_MAX_BUCKET_IDX_SQL, [market_id]).fetchone()
        next_idx = (row[0] + 1) if row and row[0] is not None else 0

        state = _BucketState(
            bucket_size=bucket_size,
            bucket_idx=next_idx,
            last_refreshed=datetime.now(tz=UTC),
        )
        self._buckets[market_id] = state
        return state

    def _compute_bucket_size(self, market_id: str) -> float:
        """Compute bucket size as avg_daily_volume / divisor."""
        try:
            row = self._conn.execute(_AVG_DAILY_VOLUME_SQL, [market_id]).fetchone()
            if row and row[0] is not None:
                avg_daily = float(row[0])
                bucket_size = avg_daily / settings.vpin_bucket_divisor
                return max(bucket_size, settings.vpin_min_bucket_size)
        except Exception:
            pass
        return self._default_bucket_size

    def add_trade(self, market_id: str, side: str, size_usd: float, timestamp: datetime) -> None:
        """Accumulate a trade into the current VPIN bucket for this market.

        If the trade overflows the bucket, the bucket is closed and written
        to DuckDB.  Large trades that span multiple buckets are split.
        When a trade fills multiple buckets, each gets a 1ms-offset timestamp
        so ``bucket_end`` remains monotonically increasing.
        """
        state = self._get_or_create_state(market_id)
        remaining = size_usd
        buckets_closed = 0

        while remaining > 0:
            capacity = state.bucket_size - (state.buy_vol + state.sell_vol)
            fill = min(remaining, capacity)

            if side.upper() == "BUY":
                state.buy_vol += fill
            else:
                state.sell_vol += fill
            remaining -= fill

            # Check if bucket is full
            if (state.buy_vol + state.sell_vol) >= state.bucket_size:
                bucket_ts = timestamp + timedelta(milliseconds=buckets_closed)
                self._close_bucket(market_id, state, bucket_ts)
                buckets_closed += 1

    def _close_bucket(self, market_id: str, state: _BucketState, timestamp: datetime) -> None:
        """Write a completed bucket to DuckDB and reset state."""
        bucket_volume = state.buy_vol + state.sell_vol
        self._conn.execute(
            _INSERT_BUCKET_SQL,
            [market_id, state.bucket_idx, timestamp, state.buy_vol, state.sell_vol, bucket_volume],
        )
        state.bucket_idx += 1
        state.buy_vol = 0.0
        state.sell_vol = 0.0

    def get_vpin(self, market_id: str) -> float | None:
        """Current VPIN for a market (average flow imbalance over recent buckets).

        Returns None if fewer than ``min_buckets`` have completed.
        """
        row = self._conn.execute(_BUCKET_COUNT_SQL, [market_id]).fetchone()
        if not row or row[0] < self._min_buckets:
            return None

        row = self._conn.execute(
            _VPIN_SQL, [market_id, self._lookback_buckets]
        ).fetchone()
        if row and row[0] is not None:
            return float(row[0])
        return None

    def get_vpin_percentile(self, market_id: str) -> float | None:
        """Current VPIN as a percentile [0, 1] vs this market's 7-day history.

        Returns None if insufficient data.
        """
        current_vpin = self.get_vpin(market_id)
        if current_vpin is None:
            return None

        row = self._conn.execute(
            _VPIN_PERCENTILE_SQL,
            [self._lookback_buckets - 1, market_id, current_vpin],
        ).fetchone()
        if row and row[0] is not None:
            return float(row[0])
        return None

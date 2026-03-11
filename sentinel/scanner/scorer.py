"""Composite signal scorer.

Combines volume anomaly, price impact, wallet reputation, and funding
anomaly metrics into a single 0-100 "statistical score" that determines
whether a trade is forwarded to the Judge (Sprint 3).

Weight distribution (configurable):
  - Volume anomaly (Z-score):  0-40 points
  - Price impact:              0-20 points
  - Wallet reputation:         0-20 points
  - Funding anomaly:           0-20 points
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from sentinel.config import settings

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class Signal:
    """A scored signal ready for DuckDB storage and Judge evaluation."""

    signal_id: str
    trade_id: str
    market_id: str
    wallet: str
    side: str
    price: float
    size_usd: float
    trade_timestamp: datetime

    # Individual metrics
    volume_z_score: float
    modified_z_score: float
    price_impact: float
    wallet_win_rate: float | None
    wallet_total_trades: int | None
    is_whitelisted: bool
    funding_anomaly: bool
    funding_age_minutes: int | None

    # Composite
    statistical_score: int
    created_at: datetime

    def as_db_tuple(self) -> tuple:
        """Return a tuple matching the DuckDB ``signals`` INSERT order."""
        return (
            self.signal_id,
            self.trade_id,
            self.market_id,
            self.wallet,
            self.side,
            self.price,
            self.size_usd,
            self.trade_timestamp,
            self.volume_z_score,
            self.modified_z_score,
            self.price_impact,
            self.wallet_win_rate,
            self.wallet_total_trades,
            self.is_whitelisted,
            self.funding_anomaly,
            self.funding_age_minutes,
            self.statistical_score,
            self.created_at,
        )

    def as_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "trade_id": self.trade_id,
            "market_id": self.market_id,
            "wallet": self.wallet[:10] + "...",
            "side": self.side,
            "price": self.price,
            "size_usd": self.size_usd,
            "statistical_score": self.statistical_score,
            "volume_z_score": round(self.volume_z_score, 2),
            "price_impact": round(self.price_impact, 4),
            "wallet_win_rate": round(self.wallet_win_rate, 3) if self.wallet_win_rate else None,
            "is_whitelisted": self.is_whitelisted,
            "funding_anomaly": self.funding_anomaly,
            "funding_age_minutes": self.funding_age_minutes,
        }


# ── Score computation ───────────────────────────────────────────────────────

def compute_statistical_score(
    z_score: float,
    price_impact: float,
    win_rate: float | None,
    is_whitelisted: bool,
    funding_anomaly: bool,
    funding_age_minutes: int | None,
    *,
    zscore_threshold: float | None = None,
) -> int:
    """Weighted composite score (0-100) for passing to the Judge.

    Weight caps are read from ``settings.scorer_weight_*``.
    The threshold defaults to ``settings.zscore_threshold`` (3.5).
    """
    threshold = zscore_threshold if zscore_threshold is not None else settings.zscore_threshold
    w_vol = settings.scorer_weight_volume
    w_imp = settings.scorer_weight_impact
    w_wal = settings.scorer_weight_wallet
    w_fun = settings.scorer_weight_funding

    score = 0

    # Volume anomaly (0-w_vol points)
    if z_score > threshold:
        score += min(w_vol, int((z_score - threshold) * 10))

    # Price impact (0-w_imp points)
    score += min(w_imp, int(price_impact * 1000))

    # Wallet reputation (0-w_wal points)
    if is_whitelisted:
        score += w_wal
    elif win_rate and win_rate > 0.6:
        score += int(win_rate * w_wal)

    # Funding anomaly (0-w_fun points)
    if funding_anomaly:
        if funding_age_minutes is not None and funding_age_minutes < 15:
            score += w_fun
        elif funding_age_minutes is not None and funding_age_minutes < 60:
            score += w_fun // 2

    return min(100, score)


# ── Signal construction ─────────────────────────────────────────────────────


def build_signal(
    *,
    trade_id: str,
    market_id: str,
    wallet: str,
    side: str,
    price: float,
    size_usd: float,
    trade_timestamp: datetime,
    z_score: float = 0.0,
    modified_z_score: float = 0.0,
    price_impact: float = 0.0,
    wallet_win_rate: float | None = None,
    wallet_total_trades: int | None = None,
    is_whitelisted: bool = False,
    funding_anomaly: bool = False,
    funding_age_minutes: int | None = None,
) -> Signal:
    """Construct a scored ``Signal`` from individual metrics."""
    stat_score = compute_statistical_score(
        z_score=modified_z_score,
        price_impact=price_impact,
        win_rate=wallet_win_rate,
        is_whitelisted=is_whitelisted,
        funding_anomaly=funding_anomaly,
        funding_age_minutes=funding_age_minutes,
    )

    return Signal(
        signal_id=str(uuid.uuid4()),
        trade_id=trade_id,
        market_id=market_id,
        wallet=wallet,
        side=side,
        price=price,
        size_usd=size_usd,
        trade_timestamp=trade_timestamp,
        volume_z_score=z_score,
        modified_z_score=modified_z_score,
        price_impact=price_impact,
        wallet_win_rate=wallet_win_rate,
        wallet_total_trades=wallet_total_trades,
        is_whitelisted=is_whitelisted,
        funding_anomaly=funding_anomaly,
        funding_age_minutes=funding_age_minutes,
        statistical_score=stat_score,
        created_at=datetime.now(tz=UTC),
    )


# ── DuckDB persistence ─────────────────────────────────────────────────────

_INSERT_SIGNAL_SQL = """
INSERT OR IGNORE INTO signals (
    signal_id, trade_id, market_id, wallet, side, price, size_usd,
    trade_timestamp, volume_z_score, modified_z_score, price_impact,
    wallet_win_rate, wallet_total_trades, is_whitelisted,
    funding_anomaly, funding_age_minutes, statistical_score, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def write_signal(conn: Any, signal: Signal) -> None:
    """Persist a signal to the DuckDB ``signals`` table."""
    conn.execute(_INSERT_SIGNAL_SQL, list(signal.as_db_tuple()))


def write_signals(conn: Any, signals: list[Signal]) -> None:
    """Batch-persist signals to DuckDB."""
    if not signals:
        return
    conn.executemany(_INSERT_SIGNAL_SQL, [list(s.as_db_tuple()) for s in signals])
    logger.info("Signals written to DuckDB", count=len(signals))

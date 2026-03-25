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

    # Enriched signals (sprint 4) — defaults allow existing callers to omit these
    ofi_score: float | None = None       # Order flow imbalance [-1, 1]; None = no data
    hours_to_resolution: int | None = None  # Hours from trade to market end_date
    market_concentration: float = 0.0    # Fraction of wallet's recent trades on this market

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
            self.ofi_score,
            self.hours_to_resolution,
            self.market_concentration,
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
    side: str = "BUY",
    ofi_score: float | None = None,
    hours_to_resolution: int | None = None,
    market_concentration: float = 0.0,
) -> int:
    """Weighted composite score (0-100) for passing to the Judge.

    Weight caps are read from ``settings.scorer_weight_*``.

    Scoring improvements (sprint 4, grounded in microstructure literature):
    - Volume component is amplified by Order Flow Imbalance when directional
      flow aligns with the trade side (Polymarket anatomy paper, arXiv 2025).
    - Wallet concentration bonus for single-market-focused wallets.
    - Time-to-resolution urgency multiplier (Kyle 1985): trades closer to
      market end_date are scored higher.
    """
    threshold = zscore_threshold if zscore_threshold is not None else settings.zscore_threshold
    w_vol = settings.scorer_weight_volume
    w_imp = settings.scorer_weight_impact
    w_wal = settings.scorer_weight_wallet
    w_fun = settings.scorer_weight_funding

    score = 0

    # ── Volume anomaly × OFI multiplier (0–w_vol points) ─────────────────
    if z_score > threshold:
        raw_vol_score = int((z_score - threshold) * 10)
        if ofi_score is not None:
            abs_ofi = abs(ofi_score)
            if abs_ofi >= 0.4:
                # Directional alignment: does the trade go with the flow?
                flow_is_buy = ofi_score > 0
                trade_is_buy = side.upper() == "BUY"
                if flow_is_buy == trade_is_buy:
                    ofi_mult = 1.5 if abs_ofi >= 0.7 else 1.2
                else:
                    ofi_mult = 1.0  # Against flow — don't penalise, may still be informed
            elif abs_ofi < 0.2:
                # Measured balanced flow: volume spike more likely noise
                ofi_mult = 0.8
            else:
                ofi_mult = 1.0
        else:
            ofi_mult = 1.0  # No OFI data — no adjustment
        score += min(w_vol, int(raw_vol_score * ofi_mult))

    # ── Price impact (0–w_imp points) ────────────────────────────────────
    score += min(w_imp, int(price_impact * 1000))

    # ── Wallet reputation + concentration (0–w_wal points + bonus) ───────
    if is_whitelisted:
        score += w_wal
    elif win_rate and win_rate > 0.6:
        score += int(win_rate * w_wal)

    # Concentration bonus: single-market wallets are a key insider tell
    if market_concentration >= 0.8:
        score += 10
    elif market_concentration >= 0.5:
        score += 5

    # ── Funding anomaly (0–w_fun points) ──────────────────────────────────
    if funding_anomaly:
        if funding_age_minutes is not None and funding_age_minutes < 15:
            score += w_fun
        elif funding_age_minutes is not None and funding_age_minutes < 60:
            score += w_fun // 2

    # ── Time-to-resolution urgency multiplier ────────────────────────────
    # Trades closest to resolution contain the most information (Kyle 1985)
    if hours_to_resolution is not None:
        if hours_to_resolution < 24:
            score = int(score * 1.4)
        elif hours_to_resolution < 72:
            score = int(score * 1.2)

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
    ofi_score: float | None = None,
    hours_to_resolution: int | None = None,
    market_concentration: float = 0.0,
) -> Signal:
    """Construct a scored ``Signal`` from individual metrics."""
    stat_score = compute_statistical_score(
        z_score=modified_z_score,
        price_impact=price_impact,
        win_rate=wallet_win_rate,
        is_whitelisted=is_whitelisted,
        funding_anomaly=funding_anomaly,
        funding_age_minutes=funding_age_minutes,
        side=side,
        ofi_score=ofi_score,
        hours_to_resolution=hours_to_resolution,
        market_concentration=market_concentration,
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
        ofi_score=ofi_score,
        hours_to_resolution=hours_to_resolution,
        market_concentration=market_concentration,
        statistical_score=stat_score,
        created_at=datetime.now(tz=UTC),
    )


# ── DuckDB persistence ─────────────────────────────────────────────────────

_INSERT_SIGNAL_SQL = """
INSERT OR IGNORE INTO signals (
    signal_id, trade_id, market_id, wallet, side, price, size_usd,
    trade_timestamp, volume_z_score, modified_z_score, price_impact,
    wallet_win_rate, wallet_total_trades, is_whitelisted,
    funding_anomaly, funding_age_minutes, statistical_score,
    ofi_score, hours_to_resolution, market_concentration, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

"""Composite signal scorer.

Combines volume anomaly, price impact, wallet reputation, and funding
anomaly metrics into a composite "statistical score" that determines
whether a trade is forwarded to the Judge.

Base components sum to at most 100 points; multipliers (urgency, liquidity
cliff, coordination) can push the score above 100. The uncapped score
preserves relative ranking for the Judge.

Weight distribution (configurable):
  - Volume anomaly (Z-score):  0-40 points
  - Price impact:              0-20 points
  - Wallet reputation:         0-20 points
  - Funding anomaly:           0-20 points
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from sentinel.config import settings

logger = structlog.get_logger()

# Increment when the scoring formula changes materially.
# v1 = original (pre-ad2084b): linear price impact, aligned OFI boost, 60% win-rate cliff
# v2 = post-ad2084b: log price impact, contrarian OFI, smooth win-rate ramp, trade-size Z modulation
SCORING_VERSION = 2


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

    ofi_score: float | None = None       # Order flow imbalance [-1, 1]; None = no data
    hours_to_resolution: int | None = None  # Hours from trade to market end_date
    market_concentration: float = 0.0    # Fraction of wallet's recent trades on this market
    coordination_wallet_count: int = 0   # Distinct wallets in same 5-min window (≥3 = coordinated)
    liquidity_cliff: bool = False        # Spread widened >30% in 10 min before trade
    scoring_version: int = SCORING_VERSION  # Formula version that produced this score
    position_trade_count: int = 0        # Wallet's trade count on this market+side (accumulation)
    vpin_percentile: float | None = None  # VPIN percentile [0, 1] vs 7-day market history
    lambda_value: float | None = None     # Kyle's Lambda coefficient for the market

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
            self.coordination_wallet_count,
            self.liquidity_cliff,
            self.scoring_version,
            self.position_trade_count,
            self.vpin_percentile,
            self.lambda_value,
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
    wallet_total_trades: int | None = None,
    size_usd: float | None = None,
    liquidity_cliff: bool = False,
    coordination_wallet_count: int = 0,
    position_trade_count: int = 0,
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
                # Contrarian trading is the insider signature: an informed
                # seller dumps into buying pressure, an informed buyer
                # accumulates during a sell-off.  Boost contrarian trades;
                # dampen with-flow trades (more likely momentum/herding).
                flow_is_buy = ofi_score > 0
                trade_is_buy = side.upper() == "BUY"
                if flow_is_buy != trade_is_buy:
                    ofi_mult = 1.5 if abs_ofi >= 0.7 else 1.2
                else:
                    ofi_mult = 0.8 if abs_ofi >= 0.7 else 1.0
            elif abs_ofi < 0.2:
                # Measured balanced flow: volume spike more likely noise
                ofi_mult = 0.8
            else:
                ofi_mult = 1.0
        else:
            ofi_mult = 1.0  # No OFI data — no adjustment
        score += min(w_vol, int(raw_vol_score * ofi_mult))

    # ── Price impact (0–w_imp points) ────────────────────────────────────
    # Log scaling maps the empirical range [0.0001, 10] to [0, 20] points.
    # Linear scaling (the old `int(price_impact * 1000)`) produced near-zero
    # scores for typical Polymarket trades where impact is O(0.001).
    if price_impact > 0:
        raw_pts = int((math.log10(price_impact) + 4) * 4)
        score += min(w_imp, max(0, raw_pts))


    # ── Wallet reputation + concentration (0–w_wal points + bonus) ───────
    if is_whitelisted:
        score += w_wal
    elif win_rate is not None and win_rate > 0.5:
        # Smooth ramp: 0 pts at 50%, full w_wal pts at 100%.
        # Replaces the old hard cutoff at 60% which created a 0→12 cliff.
        ramp = (win_rate - 0.5) / 0.5
        score += min(w_wal, round(ramp * w_wal))

    # Concentration bonus: single-market wallets are a key insider tell
    if market_concentration >= 0.8:
        score += 10
    elif market_concentration >= 0.5:
        score += 5

    # ── Funding anomaly — tiered decay over 72h (0–w_fun points) ─────────
    if funding_anomaly:
        if funding_age_minutes is not None:
            age_h = funding_age_minutes / 60.0
            if funding_age_minutes < 15:
                funding_pts = w_fun                        # 20 pts
            elif funding_age_minutes < 60:
                funding_pts = int(w_fun * 0.75)            # 15 pts
            elif age_h < 6:
                funding_pts = int(w_fun * 0.50)            # 10 pts
            elif age_h < 24:
                funding_pts = int(w_fun * 0.25)            # 5 pts
            elif age_h < 72:
                funding_pts = max(1, int(w_fun * 0.10))    # 2 pts
            else:
                funding_pts = 0
            score += funding_pts
        else:
            score += max(1, w_fun // 4)  # age unknown, apply minimum signal

    # ── Zero-history suspicion bonus ──────────────────────────────────────
    # Wallet absent from v_wallet_performance (< wallet_min_trades resolved
    # trades) + large trade is inherently suspicious.  wallet_total_trades is
    # None when the wallet has no resolved-trade history in the view; == 0 can
    # never occur because the view enforces HAVING COUNT(*) >= wallet_min_trades.
    large_threshold = settings.min_trade_size_usd * settings.new_wallet_large_trade_multiplier
    if (
        wallet_total_trades is None
        and size_usd is not None
        and size_usd > large_threshold
    ):
        score += 5

    # ── Position accumulation bonus ────────────────────────────────────
    # Multiple trades same wallet + market + side in a short window
    # indicates deliberate position building, not one-off speculation.
    if position_trade_count >= 5:
        score += 5
    elif position_trade_count >= 3:
        score += 3

    # ── Time-to-resolution urgency multiplier ────────────────────────────
    # Trades closest to resolution contain the most information (Kyle 1985)
    if hours_to_resolution is not None:
        if hours_to_resolution < 24:
            score = int(score * 1.4)
        elif hours_to_resolution < 72:
            score = int(score * 1.2)

    # ── Liquidity cliff multiplier ────────────────────────────────────────
    # Market makers withdrawing liquidity before a trade is a classic insider tell.
    if liquidity_cliff:
        score = int(score * 1.2)

    # ── Coordination multiplier ───────────────────────────────────────────
    # Multiple wallets trading same side in a 5-min window raises suspicion.
    # Dampened if volume Z-score already fired (to reduce double-counting).
    if coordination_wallet_count >= settings.coordination_wallet_count_min:
        coord_mult = 1.15 if z_score > threshold else 1.3
        score = int(score * coord_mult)

    return score


# ── Explanation generation ─────────────────────────────────────────────────


def generate_explanation(signal: Signal, *, threshold: int = 80) -> str | None:
    """Build a template-based natural language explanation for high-scoring signals.

    Returns ``None`` if the signal's score is below *threshold*.
    """
    if signal.statistical_score < threshold:
        return None

    parts: list[str] = []

    if signal.modified_z_score > 3.5:
        parts.append(f"volume spike ({signal.modified_z_score:.1f}x normal)")

    if signal.funding_anomaly and signal.funding_age_minutes is not None:
        if signal.funding_age_minutes < 60:
            parts.append(f"new wallet (funded {signal.funding_age_minutes}min ago)")
        else:
            hours = signal.funding_age_minutes // 60
            parts.append(f"new wallet (funded {hours}h ago)")

    parts.append(f"${signal.size_usd:,.0f} {signal.side.lower()}")

    if signal.market_concentration >= 0.8:
        parts.append(f"{signal.market_concentration:.0%} concentration on this market")
    elif signal.market_concentration >= 0.5:
        parts.append(f"{signal.market_concentration:.0%} market concentration")

    if signal.hours_to_resolution is not None and signal.hours_to_resolution < 72:
        parts.append(f"{signal.hours_to_resolution}h to resolution")

    if signal.is_whitelisted:
        parts.append("known profitable wallet")
    elif signal.wallet_win_rate is not None and signal.wallet_win_rate > 0.65:
        parts.append(f"{signal.wallet_win_rate:.0%} historical win rate")

    if signal.ofi_score is not None and abs(signal.ofi_score) >= 0.4:
        flow_dir = "buying" if signal.ofi_score > 0 else "selling"
        trade_is_buy = signal.side.upper() == "BUY"
        flow_is_buy = signal.ofi_score > 0
        if flow_is_buy != trade_is_buy:
            parts.append(f"contrarian trade against {flow_dir} pressure")

    if signal.coordination_wallet_count >= 3:
        parts.append(f"{signal.coordination_wallet_count} wallets coordinating")

    if signal.liquidity_cliff:
        parts.append("liquidity cliff detected")

    if signal.position_trade_count >= 3:
        parts.append(f"accumulating ({signal.position_trade_count} trades same side)")

    return f"High suspicion: {', '.join(parts)}" if parts else None


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
    coordination_wallet_count: int = 0,
    liquidity_cliff: bool = False,
    position_trade_count: int = 0,
    vpin_percentile: float | None = None,
    lambda_value: float | None = None,
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
        wallet_total_trades=wallet_total_trades,
        size_usd=size_usd,
        liquidity_cliff=liquidity_cliff,
        coordination_wallet_count=coordination_wallet_count,
        position_trade_count=position_trade_count,
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
        coordination_wallet_count=coordination_wallet_count,
        liquidity_cliff=liquidity_cliff,
        scoring_version=SCORING_VERSION,
        position_trade_count=position_trade_count,
        vpin_percentile=vpin_percentile,
        lambda_value=lambda_value,
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
    ofi_score, hours_to_resolution, market_concentration,
    coordination_wallet_count, liquidity_cliff,
    scoring_version, position_trade_count,
    vpin_percentile, lambda_value, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

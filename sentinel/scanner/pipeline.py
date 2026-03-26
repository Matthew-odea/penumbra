"""Scanner pipeline — processes trades from the ingester queue and emits signals.

The scanner is the second stage of the Penumbra pipeline:

    Ingester → **Scanner** → Judge

It consumes batches of ``Trade`` / ``BookEvent`` objects from an
``asyncio.Queue``, runs them through four detection layers:

  1. Volume anomaly (Modified Z-Score)
  2. Price impact (ΔP / L × V)
  3. Wallet profiling (win-rate on resolved markets)
  4. Funding anomaly (Alchemy wallet-age check)

…and emits ``Signal`` objects to the Judge queue for any trade scoring ≥
``signal_min_score`` (default 30).
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from sentinel.config import settings
from sentinel.ingester.models import BookEvent, IngesterEvent, Trade
from sentinel.scanner.funding import check_funding_anomaly
from sentinel.scanner.price_impact import get_price_impact
from sentinel.scanner.scorer import Signal, build_signal, write_signal
from sentinel.scanner.volume import (
    get_coordination_signal,
    get_hours_to_resolution,
    get_liquidity_cliff,
    get_market_concentration,
    get_ofi_for_market,
    get_zscore_5m_for_market,
    get_zscore_for_market,
)
from sentinel.scanner.wallet_profiler import get_wallet_profile

logger = structlog.get_logger()


class Scanner:
    """Async scanner that reads from the ingester queue and emits signals.

    Args:
        conn: Open DuckDB connection.
        scanner_queue: Queue populated by the ingester's ``BatchWriter``.
        judge_queue: Optional queue to push scored signals to.
        dry_run: When ``True``, print signals to stdout instead of persisting.
    """

    def __init__(
        self,
        conn: Any,
        *,
        scanner_queue: asyncio.Queue[list[IngesterEvent]],
        judge_queue: asyncio.Queue[Signal] | None = None,
        dry_run: bool = False,
    ) -> None:
        self._conn = conn
        self._scanner_queue = scanner_queue
        self._judge_queue = judge_queue
        self._dry_run = dry_run
        self._running = True

        # Counters
        self._trades_scanned = 0
        self._book_events_scanned = 0
        self._signals_emitted = 0

    # ── Public API ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main loop — pull batches from the queue and process each event."""
        logger.info("Scanner started", min_score=settings.signal_min_score)
        while self._running:
            try:
                batch = await asyncio.wait_for(
                    self._scanner_queue.get(), timeout=2.0
                )
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                for event in batch:
                    if isinstance(event, Trade):
                        await self._process_trade(event)
                    elif isinstance(event, BookEvent):
                        self._book_events_scanned += 1
                        # BookEvents feed into volume stats via DuckDB but
                        # don't generate signals directly — they're order book
                        # changes, not trade executions.
            finally:
                self._scanner_queue.task_done()

    def stop(self) -> None:
        self._running = False

    @property
    def trades_scanned(self) -> int:
        return self._trades_scanned

    @property
    def book_events_scanned(self) -> int:
        return self._book_events_scanned

    @property
    def signals_emitted(self) -> int:
        return self._signals_emitted

    # ── Per-trade processing ────────────────────────────────────────────

    async def _process_trade(self, trade: Trade) -> None:
        """Run the four detection layers on a single trade."""
        self._trades_scanned += 1

        # Skip tiny trades
        if float(trade.size_usd) < settings.min_trade_size_usd:
            return

        # WS Format 1 trades (last_trade_price) carry no wallet address.
        # Flag this upfront so we can skip Alchemy and avoid awarding the
        # "unknown new wallet" bonus — missing wallet ≠ new wallet.
        _wallet_known = bool(trade.wallet)

        # 1. Volume Z-score (max of hourly and 5-min windows) + OFI
        z_score = 0.0
        ofi_score = 0.0
        try:
            z_hourly = get_zscore_for_market(self._conn, trade.market_id)
            z_5m = get_zscore_5m_for_market(self._conn, trade.market_id)
            z_score = max(z_hourly, z_5m)
        except Exception as exc:
            logger.debug("Z-score lookup failed", market=trade.market_id, error=str(exc))
        try:
            ofi_score = get_ofi_for_market(self._conn, trade.market_id)
        except Exception as exc:
            logger.debug("OFI lookup failed", market=trade.market_id, error=str(exc))

        # 2. Price impact
        price_impact_score = 0.0
        try:
            impact = get_price_impact(self._conn, trade.market_id, trade.trade_id)
            if impact:
                price_impact_score = impact.impact_score
        except Exception as exc:
            logger.debug("Price impact lookup failed", trade=trade.trade_id, error=str(exc))

        # 3. Wallet profiling
        wallet_win_rate: float | None = None
        wallet_total_trades: int | None = None
        is_whitelisted = False
        try:
            profile = get_wallet_profile(self._conn, trade.wallet)
            if profile:
                wallet_win_rate = profile.win_rate
                wallet_total_trades = profile.total_resolved_trades
                is_whitelisted = profile.is_whitelisted
        except Exception as exc:
            logger.debug("Wallet profile lookup failed", wallet=trade.wallet[:10], error=str(exc))

        # Quick pre-check: does this trade pass any filter at all?
        # NOTE: the funding check runs *after* this gate, so we must also let
        # through unknown wallets (wallet_total_trades is None = not yet in
        # v_wallet_performance) with large trades — they may score purely on
        # the funding anomaly component.
        has_volume_signal = z_score > settings.zscore_threshold
        has_impact_signal = price_impact_score > 0.01
        has_wallet_signal = is_whitelisted or (wallet_win_rate is not None and wallet_win_rate > 0.6)
        _large_threshold = settings.min_trade_size_usd * settings.new_wallet_large_trade_multiplier
        has_unknown_wallet = (
            _wallet_known  # "no wallet address" is not the same as "new wallet"
            and wallet_total_trades is None
            and float(trade.size_usd) >= _large_threshold
        )

        if not (has_volume_signal or has_impact_signal or has_wallet_signal or has_unknown_wallet):
            return

        # 4. Funding anomaly (only for trades that pass at least one filter)
        funding_anomaly = False
        funding_age_minutes: int | None = None
        if _wallet_known:
            try:
                funding = await check_funding_anomaly(trade.wallet, trade.timestamp)
                funding_anomaly = funding.is_anomaly
                funding_age_minutes = funding.funding_age_minutes
            except Exception as exc:
                logger.debug("Funding check failed", wallet=trade.wallet[:10], error=str(exc))

        # 5. Market concentration (wallet focus on this market)
        market_concentration = 0.0
        try:
            market_concentration = get_market_concentration(self._conn, trade.wallet, trade.market_id)
        except Exception as exc:
            logger.debug("Concentration lookup failed", wallet=trade.wallet[:10], error=str(exc))

        # 6. Time to resolution
        hours_to_resolution: int | None = None
        try:
            hours_to_resolution = get_hours_to_resolution(self._conn, trade.market_id, trade.timestamp)
        except Exception as exc:
            logger.debug("Hours-to-resolution lookup failed", market=trade.market_id, error=str(exc))

        # 7. Coordination detection (≥3 distinct wallets, same side, last 5 min)
        coordination_wallet_count = 0
        try:
            coord = get_coordination_signal(self._conn, trade.market_id, trade.side)
            if coord is not None:
                coordination_wallet_count = coord[0]
        except Exception as exc:
            logger.debug("Coordination lookup failed", market=trade.market_id, error=str(exc))

        # 8. Liquidity cliff (spread widened >30% in last 10 min)
        liquidity_cliff = False
        try:
            liquidity_cliff, _ = get_liquidity_cliff(self._conn, trade.market_id)
        except Exception as exc:
            logger.debug("Liquidity cliff check failed", market=trade.market_id, error=str(exc))

        # Build and score the signal
        signal = build_signal(
            trade_id=trade.trade_id,
            market_id=trade.market_id,
            wallet=trade.wallet,
            side=trade.side,
            price=float(trade.price),
            size_usd=float(trade.size_usd),
            trade_timestamp=trade.timestamp,
            z_score=z_score,
            modified_z_score=z_score,  # Both fields store the same modified z-score (max of hourly/5m views)
            price_impact=price_impact_score,
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
        )

        if signal.statistical_score < settings.signal_min_score:
            return

        # Emit!
        self._signals_emitted += 1

        logger.info(
            "SIGNAL DETECTED",
            score=signal.statistical_score,
            market=trade.market_id[:12],
            wallet=trade.wallet[:10],
            z_score=round(z_score, 2),
            ofi=round(ofi_score, 2),
            impact=round(price_impact_score, 4),
            win_rate=round(wallet_win_rate, 3) if wallet_win_rate else None,
            concentration=round(market_concentration, 2),
            hours_to_res=hours_to_resolution,
            funding_anomaly=funding_anomaly,
        )

        # Persist to DuckDB
        if not self._dry_run and self._conn is not None:
            try:
                write_signal(self._conn, signal)
            except Exception as exc:
                logger.error("Failed to write signal", signal_id=signal.signal_id, error=str(exc))

        # Forward to Judge queue
        if self._judge_queue is not None:
            await self._judge_queue.put(signal)

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
from sentinel.scanner.price_impact import compute_impact_score, get_price_impact
from sentinel.scanner.scorer import Signal, build_signal, write_signal
from sentinel.scanner.volume import get_zscore_for_market
from sentinel.scanner.wallet_profiler import get_wallet_profile

logger = structlog.get_logger()


class Scanner:
    """Async scanner that reads from the ingester queue and emits signals.

    Args:
        conn: Open DuckDB connection.
        scanner_queue: Queue populated by the ingester's ``BatchWriter``.
        judge_queue: Optional queue to push scored signals to (Sprint 3).
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
                    self._scanner_queue.get(), timeout=5.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            for event in batch:
                if isinstance(event, Trade):
                    await self._process_trade(event)
                elif isinstance(event, BookEvent):
                    self._book_events_scanned += 1
                    # BookEvents feed into volume stats via DuckDB but
                    # don't generate signals directly — they're order book
                    # changes, not trade executions.

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

        # 1. Volume Z-score for this trade's market
        z_score = 0.0
        try:
            z_score = get_zscore_for_market(self._conn, trade.market_id)
        except Exception as exc:
            logger.debug("Z-score lookup failed", market=trade.market_id, error=str(exc))

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
        has_volume_signal = z_score > settings.zscore_threshold
        has_impact_signal = price_impact_score > 0.01
        has_wallet_signal = is_whitelisted or (wallet_win_rate is not None and wallet_win_rate > 0.6)

        if not (has_volume_signal or has_impact_signal or has_wallet_signal):
            return

        # 4. Funding anomaly (only for trades that pass at least one filter)
        funding_anomaly = False
        funding_age_minutes: int | None = None
        try:
            funding = await check_funding_anomaly(trade.wallet, trade.timestamp)
            funding_anomaly = funding.is_anomaly
            funding_age_minutes = funding.funding_age_minutes
        except Exception as exc:
            logger.debug("Funding check failed", wallet=trade.wallet[:10], error=str(exc))

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
            modified_z_score=z_score,  # We use modified Z from the view
            price_impact=price_impact_score,
            wallet_win_rate=wallet_win_rate,
            wallet_total_trades=wallet_total_trades,
            is_whitelisted=is_whitelisted,
            funding_anomaly=funding_anomaly,
            funding_age_minutes=funding_age_minutes,
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
            impact=round(price_impact_score, 4),
            win_rate=round(wallet_win_rate, 3) if wallet_win_rate else None,
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

"""Scanner pipeline — processes trades from the ingester queue and emits signals.

The scanner is the second stage of the Penumbra pipeline:

    Ingester → **Scanner** → DuckDB / API

It consumes batches of ``Trade`` / ``BookEvent`` objects from an
``asyncio.Queue``, runs them through four detection layers:

  1. Volume anomaly (Modified Z-Score)
  2. Price impact (deltaP / L x V)
  3. Wallet profiling (win-rate on resolved markets)
  4. Funding anomaly (Alchemy wallet-age check)

…and persists ``Signal`` objects to DuckDB for any trade scoring ≥
``signal_min_score`` (default 30).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from sentinel.config import settings
from sentinel.ingester.models import IngesterEvent, Trade
from sentinel.scanner.funding import check_funding_anomaly
from sentinel.scanner.kyle_lambda import get_cached_lambda
from sentinel.scanner.price_impact import get_price_impact
from sentinel.scanner.scorer import build_signal, write_signal
from sentinel.scanner.volume import (
    get_coordination_signal,
    get_liquidity_cliff,
    get_market_concentration,
    get_ofi_for_market,
    get_position_signal,
    get_trade_size_percentile,
    get_zscore_5m_for_market,
    get_zscore_for_market,
)
from sentinel.scanner.vpin import VPINTracker
from sentinel.scanner.wallet_profiler import get_resolved_trade_count, get_wallet_profile

logger = structlog.get_logger()


class _MarketStatsCache:
    """Per-market TTL cache for hot-path scanner queries.

    Same market is queried for every trade; within a 30s window the answer
    barely changes. Caching collapses many DB roundtrips into one per market.
    Each store is bulk-cleared at ``max_entries`` to bound memory (matching
    the eviction style of ``Scanner._excluded_cache``).
    """

    def __init__(self, conn: Any, ttl_seconds: float, max_entries: int = 5000) -> None:
        self._conn = conn
        self._ttl = ttl_seconds
        self._max = max_entries
        self._zscore_h: dict[str, tuple[float, float]] = {}
        self._zscore_5m: dict[str, tuple[float, float]] = {}
        self._ofi: dict[str, tuple[float, float]] = {}
        self._cliff: dict[str, tuple[tuple[bool, float], float]] = {}
        self._end_date: dict[str, tuple[Any, float]] = {}

    def _maybe_evict(self, store: dict[str, Any]) -> None:
        if len(store) > self._max:
            store.clear()

    def get_zscore(self, market_id: str) -> float:
        now = time.monotonic()
        cached = self._zscore_h.get(market_id)
        if cached and cached[1] > now:
            return cached[0]
        val = get_zscore_for_market(self._conn, market_id)
        self._zscore_h[market_id] = (val, now + self._ttl)
        self._maybe_evict(self._zscore_h)
        return val

    def get_zscore_5m(self, market_id: str) -> float:
        now = time.monotonic()
        cached = self._zscore_5m.get(market_id)
        if cached and cached[1] > now:
            return cached[0]
        val = get_zscore_5m_for_market(self._conn, market_id)
        self._zscore_5m[market_id] = (val, now + self._ttl)
        self._maybe_evict(self._zscore_5m)
        return val

    def get_ofi(self, market_id: str) -> float:
        now = time.monotonic()
        cached = self._ofi.get(market_id)
        if cached and cached[1] > now:
            return cached[0]
        val = get_ofi_for_market(self._conn, market_id)
        self._ofi[market_id] = (val, now + self._ttl)
        self._maybe_evict(self._ofi)
        return val

    def get_liquidity_cliff(self, market_id: str) -> tuple[bool, float]:
        now = time.monotonic()
        cached = self._cliff.get(market_id)
        if cached and cached[1] > now:
            return cached[0]
        val = get_liquidity_cliff(self._conn, market_id)
        self._cliff[market_id] = (val, now + self._ttl)
        self._maybe_evict(self._cliff)
        return val

    def get_hours_to_resolution(self, market_id: str, trade_timestamp: Any) -> int | None:
        # Cache the market's end_date (rarely changes) and recompute hours from
        # the live trade_timestamp on each call — avoids re-querying markets per trade.
        from datetime import UTC

        now = time.monotonic()
        cached = self._end_date.get(market_id)
        if cached and cached[1] > now:
            end_date = cached[0]
        else:
            row = self._conn.execute(
                "SELECT end_date FROM markets WHERE market_id = ?", [market_id],
            ).fetchone()
            end_date = row[0] if row else None
            self._end_date[market_id] = (end_date, now + self._ttl)
            self._maybe_evict(self._end_date)

        if end_date is None:
            return None
        if hasattr(end_date, "tzinfo") and end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=UTC)
        if hasattr(trade_timestamp, "tzinfo") and trade_timestamp.tzinfo is None:
            trade_timestamp = trade_timestamp.replace(tzinfo=UTC)
        delta_seconds = (end_date - trade_timestamp).total_seconds()
        if delta_seconds <= 0:
            return None
        return int(delta_seconds / 3600)


class Scanner:
    """Async scanner that reads from the ingester queue and emits signals.

    Args:
        conn: Open DuckDB connection.
        scanner_queue: Queue populated by the ingester's ``BatchWriter``.
        dry_run: When ``True``, print signals to stdout instead of persisting.
    """

    def __init__(
        self,
        conn: Any,
        *,
        scanner_queue: asyncio.Queue[list[IngesterEvent]],
        dry_run: bool = False,
    ) -> None:
        self._conn = conn
        self._scanner_queue = scanner_queue
        self._dry_run = dry_run
        self._running = True
        self._plan_b_enabled = settings.enable_plan_b_collection
        self._vpin_tracker = VPINTracker(conn) if self._plan_b_enabled else None
        self._stats_cache = _MarketStatsCache(conn, settings.scanner_cache_ttl_seconds)

        # Counters
        self._trades_scanned = 0
        self._signals_emitted = 0

        # Cache of market_id → is_excluded for settings.excluded_categories.
        # Queried once per unique market then cached; cleared at 5k entries.
        self._excluded_cache: dict[str, bool] = {}

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
            finally:
                self._scanner_queue.task_done()

    def stop(self) -> None:
        self._running = False

    @property
    def trades_scanned(self) -> int:
        return self._trades_scanned

    @property
    def signals_emitted(self) -> int:
        return self._signals_emitted

    # ── Per-trade processing ────────────────────────────────────────────

    def _is_excluded_market(self, market_id: str) -> bool:
        """Return True if the market should be skipped by the scanner.

        Two exclusion signals:
        1. Category string match (for when Gamma API populates the category field).
        2. Attractiveness score below threshold (catches sports/crypto even when
           category is null — which is currently the case for all Gamma markets).
        """
        if market_id not in self._excluded_cache:
            row = self._conn.execute(
                "SELECT category, attractiveness_score FROM markets WHERE market_id = ?",
                [market_id],
            ).fetchone()
            if row:
                cat = (row[0] or "").lower()
                score: int | None = row[1]
                cat_excluded = bool(settings.excluded_categories) and any(
                    exc.lower() in cat for exc in settings.excluded_categories
                )
                score_excluded = (
                    score is not None
                    and score < settings.scanner_min_attractiveness
                )
                self._excluded_cache[market_id] = cat_excluded or score_excluded
            else:
                self._excluded_cache[market_id] = False
            if len(self._excluded_cache) > 5000:
                self._excluded_cache.clear()
        return self._excluded_cache[market_id]

    async def _process_trade(self, trade: Trade) -> None:
        """Run the four detection layers on a single trade."""
        self._trades_scanned += 1

        # Skip markets in excluded categories (e.g. sports, crypto)
        if self._is_excluded_market(trade.market_id):
            return

        # Accumulate into VPIN buckets (ALL trades, including small ones).
        # Only when Plan B Phase 1 collection is enabled — VPIN/Lambda are
        # data-collection-only and don't feed the current scorer.
        if self._vpin_tracker is not None:
            try:
                self._vpin_tracker.add_trade(
                    trade.market_id, trade.side, float(trade.size_usd), trade.timestamp,
                )
            except Exception as exc:
                logger.debug("VPIN accumulation failed", market=trade.market_id, error=str(exc))

        # Skip tiny trades (for scoring, not for VPIN)
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
            z_hourly = self._stats_cache.get_zscore(trade.market_id)
            z_5m = self._stats_cache.get_zscore_5m(trade.market_id)
            z_score = max(z_hourly, z_5m)
        except Exception as exc:
            logger.debug("Z-score lookup failed", market=trade.market_id, error=str(exc))
        # Modulate Z-score by trade size: large trades get a higher share
        # of the market-level Z, small trades get dampened.
        # Maps size percentile [0, 1] to multiplier [0.5, 1.5].
        if z_score > 0:
            try:
                size_pctile = get_trade_size_percentile(
                    self._conn, trade.market_id, float(trade.size_usd),
                )
                z_score *= 0.5 + size_pctile
            except Exception as exc:
                logger.debug("Size percentile lookup failed", market=trade.market_id, error=str(exc))

        try:
            ofi_score = self._stats_cache.get_ofi(trade.market_id)
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
        wallet_total_trades: int | None = None  # None = truly unknown (0 resolved trades)
        is_whitelisted = False
        try:
            profile = get_wallet_profile(self._conn, trade.wallet)
            if profile:
                wallet_win_rate = profile.win_rate
                wallet_total_trades = profile.total_resolved_trades
                is_whitelisted = profile.is_whitelisted
            elif _wallet_known:
                # Profile is None (< 5 resolved trades). Distinguish "truly new"
                # (0 resolved) from "has some history" (1-4 resolved) so the
                # scorer doesn't award the zero-history bonus to wallets that
                # have traded before but just below the profiling threshold.
                raw_count = get_resolved_trade_count(self._conn, trade.wallet)
                if raw_count > 0:
                    wallet_total_trades = raw_count  # non-None → no zero-history bonus
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
            hours_to_resolution = self._stats_cache.get_hours_to_resolution(
                trade.market_id, trade.timestamp,
            )
        except Exception as exc:
            logger.debug("Hours-to-resolution lookup failed", market=trade.market_id, error=str(exc))

        # 7. Coordination detection (≥3 distinct wallets, same side, last 5 min)
        coordination_wallet_count = 0
        try:
            coord = get_coordination_signal(self._conn, trade.market_id, trade.side, trade.wallet)
            if coord is not None:
                coordination_wallet_count = coord[0]
        except Exception as exc:
            logger.debug("Coordination lookup failed", market=trade.market_id, error=str(exc))

        # 8. Liquidity cliff (spread widened >30% in last 10 min)
        liquidity_cliff = False
        try:
            liquidity_cliff, _ = self._stats_cache.get_liquidity_cliff(trade.market_id)
        except Exception as exc:
            logger.debug("Liquidity cliff check failed", market=trade.market_id, error=str(exc))

        # 9. Position accumulation (wallet building a position on this market)
        position_trade_count = 0
        if _wallet_known:
            try:
                pos = get_position_signal(self._conn, trade.wallet, trade.market_id, trade.side)
                if pos is not None:
                    position_trade_count = pos[0]
            except Exception as exc:
                logger.debug("Position lookup failed", wallet=trade.wallet[:10], error=str(exc))

        # 10. VPIN percentile + 11. Kyle's Lambda (Plan B Phase 1 — data
        # collection only, gated behind enable_plan_b_collection)
        vpin_percentile: float | None = None
        lambda_value: float | None = None
        if self._plan_b_enabled and self._vpin_tracker is not None:
            try:
                vpin_percentile = self._vpin_tracker.get_vpin_percentile(trade.market_id)
            except Exception as exc:
                logger.debug("VPIN lookup failed", market=trade.market_id, error=str(exc))
            try:
                lam = get_cached_lambda(self._conn, trade.market_id)
                if lam is not None:
                    lambda_value = lam[0]  # Store lambda coefficient
            except Exception as exc:
                logger.debug("Lambda failed", market=trade.market_id, error=str(exc))

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
            position_trade_count=position_trade_count,
            vpin_percentile=vpin_percentile,
            lambda_value=lambda_value,
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


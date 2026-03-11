"""Judge pipeline orchestrator.

Consumes signals from the judge queue, orchestrates the full reasoning
flow (news → Tier 1 → optional Tier 2 → store → alert), and respects
budget gates at every stage.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from sentinel.config import settings
from sentinel.judge.budget import BudgetManager
from sentinel.judge.classifier import ClassificationResult, classify
from sentinel.judge.news import fetch_news, format_headlines
from sentinel.judge.reasoner import ReasoningResult, reason
from sentinel.judge.store import Alert, build_alert, store_reasoning
from sentinel.scanner.scorer import Signal

logger = structlog.get_logger()


class Judge:
    """Async pipeline that evaluates signals via LLM reasoning.

    Designed to be run as an ``asyncio.Task`` alongside the ingester and
    scanner. Uses a worker pool for parallel LLM processing.

    Args:
        db: Open DuckDB connection.
        judge_queue: Incoming signals from the scanner.
        alert_queue: Outgoing high-suspicion alerts for Sprint 4.
        dry_run: If True, skip Bedrock calls and DB writes.
        max_workers: Number of parallel workers for LLM processing (default: 8).
    """

    def __init__(
        self,
        db: Any,
        *,
        judge_queue: asyncio.Queue[Signal],
        alert_queue: asyncio.Queue[Alert],
        dry_run: bool = False,
        max_workers: int | None = None,
    ) -> None:
        self.db = db
        self._judge_queue = judge_queue
        self._alert_queue = alert_queue
        self._dry_run = dry_run
        self._running = True
        self._budget = BudgetManager(db) if db else None
        self.max_workers = max_workers or settings.judge_max_workers
        self._semaphore = asyncio.Semaphore(self.max_workers)

        # Counters (use locks for thread-safety across workers)
        self._counter_lock = asyncio.Lock()
        self.signals_processed: int = 0
        self.tier1_calls: int = 0
        self.tier2_calls: int = 0
        self.alerts_emitted: int = 0
        self.skipped_budget: int = 0

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        """Spawn worker pool to process signals in parallel."""
        logger.info(
            "Judge pipeline started with worker pool",
            dry_run=self._dry_run,
            workers=self.max_workers,
        )

        workers = [
            asyncio.create_task(self._worker(i), name=f"judge-worker-{i}")
            for i in range(self.max_workers)
        ]

        try:
            await asyncio.gather(*workers)
        except Exception as exc:
            logger.error("Judge worker pool error", error=str(exc))
            raise
        finally:
            logger.info(
                "Judge pipeline stopped",
                processed=self.signals_processed,
                tier1_calls=self.tier1_calls,
                tier2_calls=self.tier2_calls,
                alerts=self.alerts_emitted,
                skipped=self.skipped_budget,
            )

    async def _worker(self, worker_id: int) -> None:
        """Worker task that pulls from queue and processes signals."""
        logger.debug("Judge worker started", worker_id=worker_id)

        while self._running:
            try:
                signal = await asyncio.wait_for(
                    self._judge_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            async with self._semaphore:
                try:
                    await self._process_signal(signal)
                except Exception as exc:
                    logger.error(
                        "Judge error processing signal",
                        signal_id=signal.signal_id,
                        worker_id=worker_id,
                        error=str(exc),
                    )
                finally:
                    self._judge_queue.task_done()

        logger.debug("Judge worker stopped", worker_id=worker_id)

    async def _process_signal(self, signal: Signal) -> None:
        """Full reasoning flow for one signal."""
        # ── Budget gate ─────────────────────────────────────────────────
        if self._budget and not self._budget.can_call("tier1"):
            logger.warning("Tier 1 budget exhausted, skipping signal", signal_id=signal.signal_id)
            async with self._counter_lock:
                self.skipped_budget += 1
            return

        # ── Market context ──────────────────────────────────────────────
        market_question, category, liquidity = self._lookup_market(signal.market_id)

        # ── News fetch (only for high-scoring signals) ──────────────────
        if signal.statistical_score >= settings.news_min_score:
            headlines = await fetch_news(market_question, signal.market_id)
        else:
            headlines = []
        
        headlines_str = format_headlines(headlines)  # Returns "No relevant news found." if empty

        # ── Tier 1 classification ───────────────────────────────────────
        if self._dry_run:
            logger.info(
                "DRY RUN — would classify",
                signal_id=signal.signal_id,
                market=market_question[:60],
            )
            async with self._counter_lock:
                self.signals_processed += 1
            return

        t1_result: ClassificationResult = await classify(
            signal,
            news_headlines=headlines_str,
            market_question=market_question,
            category=category,
            liquidity_usd=liquidity,
        )
        async with self._counter_lock:
            self.tier1_calls += 1
        if self._budget:
            self._budget.record_call("tier1")

        # ── Tier 2 reasoning (optional) ─────────────────────────────────
        t2_result: ReasoningResult | None = None

        if (
            t1_result.confidence >= settings.bedrock_tier2_min_suspicion
            and self._budget
            and self._budget.can_call("tier2")
        ):
            t2_result = await reason(
                signal,
                news_headlines=headlines_str,
                t1_result=t1_result,
                market_question=market_question,
                category=category,
            )
            async with self._counter_lock:
                self.tier2_calls += 1
            if self._budget:
                self._budget.record_call("tier2")

        # ── Store results ───────────────────────────────────────────────
        if self.db:
            store_reasoning(self.db, signal, t1_result, t2_result, headlines)

        # ── Alert emission ──────────────────────────────────────────────
        alert = build_alert(signal, t1_result, t2_result)
        if alert:
            await self._alert_queue.put(alert)
            async with self._counter_lock:
                self.alerts_emitted += 1
            logger.info(
                "Alert emitted",
                signal_id=signal.signal_id,
                score=alert.score,
                classification=alert.classification,
            )

        async with self._counter_lock:
            self.signals_processed += 1

    def _lookup_market(self, market_id: str) -> tuple[str, str, float]:
        """Fetch market metadata from DuckDB. Returns (question, category, liquidity)."""
        if not self.db:
            return ("", "", 0.0)

        try:
            row = self.db.execute(
                "SELECT question, category, liquidity_usd FROM markets WHERE market_id = ?",
                [market_id],
            ).fetchone()
            if row:
                return (row[0] or "", row[1] or "", float(row[2] or 0))
        except Exception as exc:
            logger.warning("Market lookup failed", market_id=market_id, error=str(exc))

        return ("", "", 0.0)

"""DuckDB batch writer for Polymarket trades.

Accumulates trades in memory and flushes when either the batch size or the
time interval threshold is reached.  After a successful write the batch is
optionally forwarded to an ``asyncio.Queue`` (consumed by the Scanner in
Sprint 2).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import structlog

from sentinel.config import settings
from sentinel.ingester.models import Trade

logger = structlog.get_logger()

# SQL for batch insert — duplicate trade_ids are silently skipped.
_INSERT_SQL = """
INSERT OR IGNORE INTO trades
    (trade_id, market_id, asset_id, wallet, side, price, size_usd, timestamp, tx_hash)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class BatchWriter:
    """Buffered DuckDB writer with time-or-count flush policy.

    Args:
        conn: Open DuckDB connection.
        scanner_queue: Optional ``asyncio.Queue`` to push batches for downstream
            processing (Sprint 2).
        batch_size: Number of trades that trigger a flush.
        flush_interval: Seconds between automatic flushes.
        dry_run: When ``True``, print trades as JSON instead of writing to DB.
    """

    def __init__(
        self,
        conn: Any,
        *,
        scanner_queue: asyncio.Queue[list[Trade]] | None = None,
        batch_size: int | None = None,
        flush_interval: float | None = None,
        dry_run: bool = False,
    ) -> None:
        self._conn = conn
        self._queue = scanner_queue
        self._batch_size = batch_size or settings.ingester_batch_size
        self._flush_interval = flush_interval or settings.ingester_flush_interval_seconds
        self._dry_run = dry_run

        self._buffer: list[Trade] = []
        self._last_flush = time.monotonic()
        self._total_written = 0
        self._flush_lock = asyncio.Lock()

    # ── public API ──────────────────────────────────────────────────────

    async def add(self, trade: Trade) -> None:
        """Add a trade to the buffer.  Flushes automatically when thresholds are met."""
        self._buffer.append(trade)

        if self._should_flush():
            await self.flush()

    async def flush(self) -> None:
        """Write the current buffer to DuckDB (or stdout in dry-run mode)."""
        async with self._flush_lock:
            if not self._buffer:
                return

            batch = self._buffer[:]
            self._buffer.clear()
            self._last_flush = time.monotonic()

        if self._dry_run:
            self._print_batch(batch)
        else:
            self._write_batch(batch)

        # Forward to scanner queue
        if self._queue is not None:
            await self._queue.put(batch)

    @property
    def total_written(self) -> int:
        return self._total_written

    @property
    def buffer_size(self) -> int:
        return len(self._buffer)

    # ── background flusher ──────────────────────────────────────────────

    async def run_timer(self) -> None:
        """Periodically flush the buffer on a timer.

        Run this as a background task alongside the listener::

            asyncio.create_task(writer.run_timer())
        """
        while True:
            await asyncio.sleep(self._flush_interval)
            if self._buffer:
                await self.flush()

    # ── internals ───────────────────────────────────────────────────────

    def _should_flush(self) -> bool:
        if len(self._buffer) >= self._batch_size:
            return True
        elapsed = time.monotonic() - self._last_flush
        if elapsed >= self._flush_interval and self._buffer:
            return True
        return False

    def _write_batch(self, batch: list[Trade]) -> None:
        t0 = time.perf_counter()
        rows = [t.as_db_tuple() for t in batch]
        self._conn.executemany(_INSERT_SQL, rows)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self._total_written += len(batch)
        logger.info(
            "Batch written",
            size=len(batch),
            total=self._total_written,
            latency_ms=round(elapsed_ms, 1),
        )

    def _print_batch(self, batch: list[Trade]) -> None:
        for t in batch:
            print(json.dumps(t.as_dict(), indent=2))
        self._total_written += len(batch)
        logger.info("Dry-run batch printed", size=len(batch), total=self._total_written)

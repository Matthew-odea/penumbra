"""Tests for the DuckDB batch writer."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sentinel.db.init import init_schema
from sentinel.ingester.models import Trade
from sentinel.ingester.writer import BatchWriter

# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_trade(
    trade_id: str = "t-1",
    market_id: str = "0xmarket",
    price: str = "0.73",
    size: str = "500.00",
    side: str = "BUY",
) -> Trade:
    return Trade(
        trade_id=trade_id,
        market_id=market_id,
        asset_id="0xtoken",
        wallet="0xwallet",
        side=side,
        price=Decimal(price),
        size_usd=Decimal(size),
        timestamp=datetime(2026, 3, 10, 12, 0, 0, tzinfo=UTC),
        tx_hash="0xhash",
    )


@pytest.fixture()
def db_conn(tmp_path):
    """In-memory DuckDB connection with schema applied."""
    conn = init_schema(tmp_path / "test.duckdb")
    yield conn
    conn.close()


# ── Unit tests ──────────────────────────────────────────────────────────────


class TestBatchWriter:
    @pytest.mark.asyncio
    async def test_single_trade_write(self, db_conn):
        writer = BatchWriter(db_conn, batch_size=1)
        await writer.add(_make_trade())
        # batch_size=1 triggers immediate flush
        count = db_conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert count == 1

    @pytest.mark.asyncio
    async def test_batch_accumulates(self, db_conn):
        writer = BatchWriter(db_conn, batch_size=5)
        for i in range(3):
            await writer.add(_make_trade(trade_id=f"t-{i}"))
        # Not flushed yet (3 < 5)
        assert writer.buffer_size == 3
        count = db_conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert count == 0

    @pytest.mark.asyncio
    async def test_batch_flushes_at_threshold(self, db_conn):
        writer = BatchWriter(db_conn, batch_size=3)
        for i in range(3):
            await writer.add(_make_trade(trade_id=f"t-{i}"))
        count = db_conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert count == 3
        assert writer.total_written == 3

    @pytest.mark.asyncio
    async def test_manual_flush(self, db_conn):
        writer = BatchWriter(db_conn, batch_size=100)
        await writer.add(_make_trade())
        await writer.flush()
        count = db_conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert count == 1

    @pytest.mark.asyncio
    async def test_duplicate_trade_ignored(self, db_conn):
        writer = BatchWriter(db_conn, batch_size=1)
        trade = _make_trade(trade_id="dup-1")
        await writer.add(trade)
        await writer.add(trade)  # duplicate
        count = db_conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert count == 1

    @pytest.mark.asyncio
    async def test_scanner_queue_receives_batch(self, db_conn):
        queue: asyncio.Queue = asyncio.Queue()
        writer = BatchWriter(db_conn, scanner_queue=queue, batch_size=2)
        await writer.add(_make_trade(trade_id="q-1"))
        await writer.add(_make_trade(trade_id="q-2"))
        batch = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert len(batch) == 2

    @pytest.mark.asyncio
    async def test_dry_run_prints_json(self, db_conn, capsys):
        writer = BatchWriter(db_conn, batch_size=1, dry_run=True)
        await writer.add(_make_trade())
        captured = capsys.readouterr()
        # stdout may contain structlog output after the JSON — extract first JSON object
        lines = captured.out.strip().split("\n")
        json_lines = []
        for line in lines:
            if line.startswith("{") or line.startswith(" ") or line.startswith("}"):
                json_lines.append(line)
            else:
                break
        parsed = json.loads("\n".join(json_lines))
        assert parsed["trade_id"] == "t-1"
        assert parsed["type"] == "trade"
        # DB should be empty in dry-run
        count = db_conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert count == 0

    @pytest.mark.asyncio
    async def test_empty_flush_is_noop(self, db_conn):
        writer = BatchWriter(db_conn, batch_size=100)
        await writer.flush()  # should not raise
        assert writer.total_written == 0

    @pytest.mark.asyncio
    async def test_data_integrity(self, db_conn):
        writer = BatchWriter(db_conn, batch_size=1)
        trade = _make_trade(trade_id="integrity", price="0.55", size="1234.56")
        await writer.add(trade)
        row = db_conn.execute(
            "SELECT trade_id, price, size_usd, side FROM trades WHERE trade_id = 'integrity'"
        ).fetchone()
        assert row[0] == "integrity"
        assert float(row[1]) == pytest.approx(0.55)
        assert float(row[2]) == pytest.approx(1234.56)
        assert row[3] == "BUY"

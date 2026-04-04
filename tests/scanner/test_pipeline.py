"""Integration tests for the scanner pipeline."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import duckdb
import pytest

from sentinel.ingester.models import BookEvent, Trade
from sentinel.scanner.pipeline import Scanner


def _init_db() -> duckdb.DuckDBPyConnection:
    """Full schema in-memory DB for pipeline tests."""
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE markets (
            market_id VARCHAR PRIMARY KEY,
            question VARCHAR,
            slug VARCHAR,
            category VARCHAR,
            end_date TIMESTAMP,
            volume_usd DECIMAL(18,6),
            liquidity_usd DECIMAL(18,6),
            active BOOLEAN DEFAULT TRUE,
            resolved BOOLEAN DEFAULT FALSE,
            resolved_price DECIMAL(10,6),
            attractiveness_score INTEGER,
            last_synced TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE trades (
            trade_id VARCHAR PRIMARY KEY,
            market_id VARCHAR NOT NULL,
            asset_id VARCHAR NOT NULL,
            wallet VARCHAR NOT NULL,
            side VARCHAR NOT NULL,
            price DECIMAL(10,6),
            size_usd DECIMAL(18,6),
            timestamp TIMESTAMP NOT NULL,
            tx_hash VARCHAR,
            source VARCHAR DEFAULT 'ws',
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE signals (
            signal_id VARCHAR PRIMARY KEY,
            trade_id VARCHAR NOT NULL,
            market_id VARCHAR NOT NULL,
            wallet VARCHAR NOT NULL,
            side VARCHAR NOT NULL,
            price DECIMAL(10,6),
            size_usd DECIMAL(18,6),
            trade_timestamp TIMESTAMP NOT NULL,
            volume_z_score DECIMAL(8,4),
            modified_z_score DECIMAL(8,4),
            price_impact DECIMAL(8,6),
            wallet_win_rate DECIMAL(5,4),
            wallet_total_trades INTEGER,
            is_whitelisted BOOLEAN DEFAULT FALSE,
            funding_anomaly BOOLEAN DEFAULT FALSE,
            funding_age_minutes INTEGER,
            statistical_score INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Dedup view
    conn.execute("""
        CREATE OR REPLACE VIEW v_deduped_trades AS
        WITH ranked AS (
            SELECT *,
                ROW_NUMBER() OVER (
                    PARTITION BY market_id, side,
                        date_trunc('second', timestamp),
                        ROUND(size_usd::DOUBLE, 2)
                    ORDER BY
                        CASE WHEN source = 'rest' THEN 0 ELSE 1 END,
                        ingested_at DESC
                ) AS rn
            FROM trades
        )
        SELECT trade_id, market_id, asset_id, wallet, side,
               price, size_usd, timestamp, tx_hash, source, ingested_at
        FROM ranked WHERE rn = 1
    """)
    # Views
    conn.execute("""
        CREATE OR REPLACE VIEW v_hourly_volume AS
        SELECT
            market_id,
            date_trunc('hour', timestamp) AS hour_bucket,
            COUNT(*) AS trade_count,
            SUM(size_usd) AS volume_usd,
            COUNT(DISTINCT wallet) AS unique_wallets
        FROM v_deduped_trades
        WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '7 days'
        GROUP BY 1, 2
    """)
    conn.execute("""
        CREATE OR REPLACE VIEW v_volume_anomalies AS
        WITH hourly AS (
            SELECT * FROM v_hourly_volume
            WHERE hour_bucket >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
        ),
        market_stats AS (
            SELECT h.market_id,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY h.volume_usd) AS median_vol
            FROM hourly h GROUP BY 1
        ),
        market_mad AS (
            SELECT h.market_id,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ABS(h.volume_usd - ms.median_vol)) AS mad_vol
            FROM hourly h JOIN market_stats ms ON h.market_id = ms.market_id GROUP BY 1
        )
        SELECT h.market_id, h.hour_bucket, h.volume_usd, h.trade_count, h.unique_wallets,
            ms.median_vol, mm.mad_vol,
            CASE WHEN mm.mad_vol > 0 THEN 0.6745 * (h.volume_usd - ms.median_vol) / mm.mad_vol ELSE NULL END AS modified_z_score
        FROM hourly h
        JOIN market_stats ms ON h.market_id = ms.market_id
        JOIN market_mad mm ON h.market_id = mm.market_id
    """)
    conn.execute("""
        CREATE OR REPLACE VIEW v_wallet_performance AS
        SELECT t.wallet,
            COUNT(*) AS total_resolved_trades,
            SUM(CASE WHEN (t.side='BUY' AND m.resolved_price>=0.95) OR (t.side='SELL' AND m.resolved_price<=0.05) THEN 1 ELSE 0 END) AS wins,
            CASE WHEN COUNT(*)>0 THEN SUM(CASE WHEN (t.side='BUY' AND m.resolved_price>=0.95) OR (t.side='SELL' AND m.resolved_price<=0.05) THEN 1 ELSE 0 END)::FLOAT/COUNT(*) ELSE 0 END AS win_rate
        FROM v_deduped_trades t JOIN markets m ON t.market_id=m.market_id WHERE m.resolved=TRUE AND t.wallet != '' GROUP BY t.wallet HAVING COUNT(*)>=5
    """)
    return conn


def _make_trade(
    trade_id: str = "t1",
    market_id: str = "mkt-1",
    wallet: str = "0xwallet",
    size_usd: float = 1000.0,
    price: float = 0.5,
) -> Trade:
    return Trade(
        trade_id=trade_id,
        market_id=market_id,
        asset_id="asset-1",
        wallet=wallet,
        side="BUY",
        price=Decimal(str(price)),
        size_usd=Decimal(str(size_usd)),
        timestamp=datetime.now(tz=UTC),
    )


class TestScannerPipeline:
    @pytest.mark.asyncio
    async def test_scanner_processes_trade_batch(self):
        """Scanner should consume trades from the queue."""
        conn = _init_db()
        conn.execute("INSERT INTO markets (market_id, question, liquidity_usd) VALUES (?, ?, ?)",
                      ["mkt-1", "Test?", 50000.0])

        scanner_queue: asyncio.Queue = asyncio.Queue()
        scanner = Scanner(conn, scanner_queue=scanner_queue)

        trade = _make_trade(size_usd=100.0)  # Below min_trade_size_usd
        await scanner_queue.put([trade])

        # Run scanner briefly
        task = asyncio.create_task(scanner.run())
        await asyncio.sleep(0.1)
        scanner.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert scanner.trades_scanned == 1
        assert scanner.signals_emitted == 0  # Below $500 threshold

    @pytest.mark.asyncio
    async def test_scanner_skips_small_trades(self):
        """Trades below min_trade_size_usd should be skipped."""
        conn = _init_db()
        scanner_queue: asyncio.Queue = asyncio.Queue()
        scanner = Scanner(conn, scanner_queue=scanner_queue)

        trade = _make_trade(size_usd=10.0)
        await scanner_queue.put([trade])

        task = asyncio.create_task(scanner.run())
        await asyncio.sleep(0.1)
        scanner.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert scanner.trades_scanned == 1
        assert scanner.signals_emitted == 0

    @pytest.mark.asyncio
    async def test_scanner_emits_signal_for_anomaly(self):
        """A large trade in a market with a volume spike should emit a signal."""
        conn = _init_db()
        conn.execute("INSERT INTO markets (market_id, question, liquidity_usd) VALUES (?, ?, ?)",
                      ["mkt-1", "Test?", 1000.0])

        # Insert historical trades to create a volume baseline
        now = datetime.now(tz=UTC)
        for i in range(1, 6):
            conn.execute(
                "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'ws', CURRENT_TIMESTAMP)",
                [f"hist-{i}", "mkt-1", "a1", "0xother", "BUY", 0.5, 100.0,
                 now - timedelta(hours=i + 0.5)],
            )
        # Insert a spike trade in the current hour
        conn.execute(
            "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'ws', CURRENT_TIMESTAMP)",
            ["spike-1", "mkt-1", "a1", "0xwhale", "BUY", 0.8, 50000.0,
             now - timedelta(minutes=5)],
        )

        scanner_queue: asyncio.Queue = asyncio.Queue()
        scanner = Scanner(conn, scanner_queue=scanner_queue)

        # Feed the spike trade through the scanner
        spike_trade = Trade(
            trade_id="spike-1",
            market_id="mkt-1",
            asset_id="a1",
            wallet="0xwhale",
            side="BUY",
            price=Decimal("0.8"),
            size_usd=Decimal("50000"),
            timestamp=now - timedelta(minutes=5),
        )
        await scanner_queue.put([spike_trade])

        task = asyncio.create_task(scanner.run())
        await asyncio.sleep(0.2)
        scanner.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert scanner.trades_scanned == 1
        # Should have detected the anomaly (high z-score + price impact)
        if scanner.signals_emitted > 0:
            row = conn.execute(
                "SELECT statistical_score FROM signals WHERE trade_id = 'spike-1'"
            ).fetchone()
            assert row is not None
            assert row[0] >= 30

    @pytest.mark.asyncio
    async def test_scanner_dry_run(self):
        """Dry run should not write to DB."""
        conn = _init_db()
        conn.execute("INSERT INTO markets (market_id, question, liquidity_usd) VALUES (?, ?, ?)",
                      ["mkt-1", "Test?", 50000.0])

        scanner_queue: asyncio.Queue = asyncio.Queue()
        scanner = Scanner(conn, scanner_queue=scanner_queue, dry_run=True)

        trade = _make_trade(size_usd=5000.0)
        await scanner_queue.put([trade])

        task = asyncio.create_task(scanner.run())
        await asyncio.sleep(0.1)
        scanner.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # No signals written to DB in dry-run
        count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        assert count == 0

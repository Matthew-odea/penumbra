"""Tests for VPIN (Volume-Synchronized Probability of Informed Trading)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from sentinel.scanner.vpin import VPINTracker


@pytest.fixture
def conn() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with required schema."""
    c = duckdb.connect(":memory:")
    c.execute("""
        CREATE TABLE trades (
            trade_id VARCHAR PRIMARY KEY, market_id VARCHAR, asset_id VARCHAR,
            wallet VARCHAR, side VARCHAR, price DECIMAL(10,6), size_usd DECIMAL(18,6),
            timestamp TIMESTAMP, tx_hash VARCHAR, source VARCHAR DEFAULT 'rest',
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("CREATE OR REPLACE VIEW v_deduped_trades AS SELECT * FROM trades")
    c.execute("""
        CREATE TABLE vpin_buckets (
            market_id VARCHAR NOT NULL, bucket_idx INTEGER NOT NULL,
            bucket_end TIMESTAMP NOT NULL, buy_vol DECIMAL(18,6),
            sell_vol DECIMAL(18,6), bucket_volume DECIMAL(18,6),
            PRIMARY KEY (market_id, bucket_idx)
        )
    """)
    return c


class TestVPINTracker:
    def test_no_buckets_returns_none(self, conn: duckdb.DuckDBPyConnection) -> None:
        """VPIN returns None with no data."""
        tracker = VPINTracker(conn, default_bucket_size=100.0)
        assert tracker.get_vpin("m1") is None

    def test_insufficient_buckets_returns_none(self, conn: duckdb.DuckDBPyConnection) -> None:
        """VPIN returns None when fewer than min_buckets have completed."""
        tracker = VPINTracker(conn, default_bucket_size=100.0, min_buckets=5)
        now = datetime.now(tz=UTC)
        # Fill 3 buckets — still below min_buckets=5
        for i in range(3):
            tracker.add_trade("m1", "BUY", 110.0, now + timedelta(seconds=i))
        assert tracker.get_vpin("m1") is None

    def test_bucket_fills_and_writes(self, conn: duckdb.DuckDBPyConnection) -> None:
        """When accumulated volume exceeds bucket_size, a bucket is written."""
        tracker = VPINTracker(conn, default_bucket_size=100.0)
        now = datetime.now(tz=UTC)

        tracker.add_trade("m1", "BUY", 60.0, now)
        # Not yet full (60 < 100)
        rows = conn.execute("SELECT * FROM vpin_buckets WHERE market_id = 'm1'").fetchall()
        assert len(rows) == 0

        tracker.add_trade("m1", "SELL", 50.0, now + timedelta(seconds=1))
        # Now full (110 >= 100)
        rows = conn.execute("SELECT * FROM vpin_buckets WHERE market_id = 'm1'").fetchall()
        assert len(rows) == 1

    def test_bucket_buy_sell_volumes(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Bucket correctly separates buy and sell volumes."""
        tracker = VPINTracker(conn, default_bucket_size=100.0)
        now = datetime.now(tz=UTC)

        tracker.add_trade("m1", "BUY", 70.0, now)
        tracker.add_trade("m1", "SELL", 40.0, now + timedelta(seconds=1))

        row = conn.execute(
            "SELECT buy_vol, sell_vol, bucket_volume FROM vpin_buckets WHERE market_id = 'm1'"
        ).fetchone()
        assert row is not None
        buy, sell, total = float(row[0]), float(row[1]), float(row[2])
        assert buy == pytest.approx(70.0)
        assert sell == pytest.approx(30.0)  # 40 capped to fill bucket: 100 - 70 = 30
        assert total == pytest.approx(100.0)

    def test_overflow_splits_across_buckets(self, conn: duckdb.DuckDBPyConnection) -> None:
        """A trade larger than bucket_size fills multiple buckets."""
        tracker = VPINTracker(conn, default_bucket_size=100.0)
        now = datetime.now(tz=UTC)

        tracker.add_trade("m1", "BUY", 250.0, now)

        rows = conn.execute(
            "SELECT bucket_idx, buy_vol, sell_vol FROM vpin_buckets "
            "WHERE market_id = 'm1' ORDER BY bucket_idx"
        ).fetchall()
        assert len(rows) == 2  # 250 fills 2 full buckets (100+100), 50 left in accumulator

    def test_vpin_computation(self, conn: duckdb.DuckDBPyConnection) -> None:
        """VPIN = mean |buy - sell| / total across buckets."""
        tracker = VPINTracker(conn, default_bucket_size=100.0, min_buckets=2)
        now = datetime.now(tz=UTC)

        # Bucket 0: all buys → imbalance = |100-0|/100 = 1.0
        tracker.add_trade("m1", "BUY", 110.0, now)

        # Bucket 1: balanced → imbalance ≈ 0.0
        tracker.add_trade("m1", "BUY", 45.0, now + timedelta(seconds=1))
        tracker.add_trade("m1", "SELL", 65.0, now + timedelta(seconds=2))

        vpin = tracker.get_vpin("m1")
        assert vpin is not None
        # Bucket 0: 100 buy, 0 sell → 1.0
        # Bucket 1: ~45 buy, ~55 sell → |45-55|/100 = 0.1
        # Average ≈ 0.55
        assert 0.3 < vpin < 0.7

    def test_vpin_percentile_returns_none_insufficient_data(
        self, conn: duckdb.DuckDBPyConnection
    ) -> None:
        """Percentile returns None when VPIN returns None."""
        tracker = VPINTracker(conn, default_bucket_size=100.0)
        assert tracker.get_vpin_percentile("m1") is None

    def test_independent_markets(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Buckets are tracked independently per market."""
        tracker = VPINTracker(conn, default_bucket_size=100.0)
        now = datetime.now(tz=UTC)

        tracker.add_trade("m1", "BUY", 110.0, now)
        tracker.add_trade("m2", "SELL", 50.0, now)

        m1_rows = conn.execute(
            "SELECT COUNT(*) FROM vpin_buckets WHERE market_id = 'm1'"
        ).fetchone()
        m2_rows = conn.execute(
            "SELECT COUNT(*) FROM vpin_buckets WHERE market_id = 'm2'"
        ).fetchone()
        assert m1_rows is not None and m1_rows[0] == 1
        assert m2_rows is not None and m2_rows[0] == 0  # m2 hasn't filled a bucket

    def test_bucket_idx_resumes_from_db(self, conn: duckdb.DuckDBPyConnection) -> None:
        """After restart, bucket_idx resumes from the max in DB."""
        now = datetime.now(tz=UTC)
        # Simulate prior run: bucket 0 and 1 already in DB
        conn.execute(
            "INSERT INTO vpin_buckets VALUES ('m1', 0, ?, 50, 50, 100)", [now]
        )
        conn.execute(
            "INSERT INTO vpin_buckets VALUES ('m1', 1, ?, 60, 40, 100)", [now]
        )

        tracker = VPINTracker(conn, default_bucket_size=100.0)
        tracker.add_trade("m1", "BUY", 110.0, now + timedelta(seconds=1))

        # New bucket should be idx=2
        row = conn.execute(
            "SELECT MAX(bucket_idx) FROM vpin_buckets WHERE market_id = 'm1'"
        ).fetchone()
        assert row is not None and row[0] == 2

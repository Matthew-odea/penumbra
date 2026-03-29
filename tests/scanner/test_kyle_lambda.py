"""Tests for Kyle's Lambda estimation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from sentinel.scanner.kyle_lambda import (
    _lambda_cache,
    estimate_lambda,
    get_cached_lambda,
    store_lambda,
)


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
        CREATE TABLE market_lambda (
            market_id VARCHAR NOT NULL, estimated_at TIMESTAMP NOT NULL,
            lambda_value DECIMAL(12,8), r_squared DECIMAL(8,6),
            residual_std DECIMAL(12,8), n_obs INTEGER,
            PRIMARY KEY (market_id, estimated_at)
        )
    """)
    return c


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """Clear lambda cache between tests."""
    _lambda_cache.clear()


def _insert_correlated_trades(conn: duckdb.DuckDBPyConnection, n_windows: int = 8) -> None:
    """Insert trades with correlated price/volume across 5-min windows.

    Both signed_volume AND delta_price vary across windows, with
    delta_price proportional to signed_volume so REGR_SLOPE > 0.
    """
    now = datetime.now(tz=UTC)
    for i in range(n_windows):
        ts = now - timedelta(minutes=45) + timedelta(minutes=i * 5)
        buy_size = 400.0 + i * 50  # More buying over time
        sell_size = 300.0 - i * 20  # Less selling
        # signed_volume = buy_size - sell_size = 100 + 70*i
        # delta_price proportional to signed_volume
        net_flow = buy_size - sell_size
        start_price = 0.50
        end_price = start_price + net_flow * 0.001  # Positive correlation
        conn.execute(
            "INSERT INTO trades VALUES (?, 'm1', 'a1', 'w1', 'BUY', ?, ?, ?, NULL, 'rest',"
            " CURRENT_TIMESTAMP)",
            [f"t{i}a", start_price, buy_size, ts],
        )
        conn.execute(
            "INSERT INTO trades VALUES (?, 'm1', 'a1', 'w2', 'SELL', ?, ?, ?, NULL, 'rest',"
            " CURRENT_TIMESTAMP)",
            [f"t{i}b", end_price, sell_size, ts + timedelta(seconds=30)],
        )


class TestEstimateLambda:
    def test_returns_none_no_data(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Returns None when no trades exist."""
        assert estimate_lambda(conn, "m1") is None

    def test_returns_none_insufficient_windows(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Returns None when fewer than 6 five-minute windows."""
        _insert_correlated_trades(conn, n_windows=3)
        assert estimate_lambda(conn, "m1") is None

    def test_lambda_positive_with_correlated_data(self, conn: duckdb.DuckDBPyConnection) -> None:
        """When net buying pushes price up, lambda should be positive."""
        _insert_correlated_trades(conn)
        result = estimate_lambda(conn, "m1")
        assert result is not None
        lambda_val, _r_sq, _res_std, _n = result
        assert lambda_val > 0  # Net buying → price up → positive lambda

    def test_returns_tuple_of_four(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Result is (lambda_val, r_squared, residual_std, n_obs)."""
        _insert_correlated_trades(conn)
        result = estimate_lambda(conn, "m1")
        assert result is not None
        assert len(result) == 4
        assert all(isinstance(v, (float, int)) for v in result)
        assert result[3] >= 6  # At least lambda_min_observations windows


class TestStoreLambda:
    def test_store_and_retrieve(self, conn: duckdb.DuckDBPyConnection) -> None:
        """store_lambda persists to market_lambda table."""
        store_lambda(conn, "m1", 0.001, 0.85, 0.0002, 10)
        rows = conn.execute("SELECT * FROM market_lambda WHERE market_id = 'm1'").fetchall()
        assert len(rows) == 1
        assert float(rows[0][2]) == pytest.approx(0.001, abs=1e-6)


class TestGetCachedLambda:
    def test_cache_miss_computes(self, conn: duckdb.DuckDBPyConnection) -> None:
        """On cache miss, estimate_lambda is called and result is stored with n_obs."""
        _insert_correlated_trades(conn)
        result = get_cached_lambda(conn, "m1")
        assert result is not None
        assert len(result) == 4
        rows = conn.execute("SELECT n_obs FROM market_lambda WHERE market_id = 'm1'").fetchall()
        assert len(rows) == 1
        assert rows[0][0] >= 6  # Actual observation count, not 0

    def test_cache_hit_skips_db(self, conn: duckdb.DuckDBPyConnection) -> None:
        """On cache hit, the DB is not queried (result comes from memory)."""
        _insert_correlated_trades(conn)
        r1 = get_cached_lambda(conn, "m1")
        # Delete all trades — if cache works, second call still returns
        conn.execute("DELETE FROM trades")
        r2 = get_cached_lambda(conn, "m1")
        assert r1 == r2

    def test_returns_none_no_data(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Returns None when no trades exist (not cached)."""
        assert get_cached_lambda(conn, "m1") is None

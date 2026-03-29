"""Tests for sentinel.scanner.price_impact — price impact calculation."""

from datetime import UTC, datetime, timedelta

import duckdb

from sentinel.scanner.price_impact import (
    PriceImpact,
    compute_impact_score,
    get_high_impact_trades,
    get_price_impact,
)


def _init_db() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with trades + markets tables."""
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
    return conn


def _seed(conn: duckdb.DuckDBPyConnection) -> None:
    """Insert a market + sequential trades with increasing prices."""
    conn.execute(
        "INSERT INTO markets (market_id, question, liquidity_usd) VALUES (?, ?, ?)",
        ["mkt-1", "Test?", 50000.0],
    )
    now = datetime.now(tz=UTC)
    trades = [
        ("t1", "mkt-1", "a1", "0xwalletA", "BUY", 0.50, 1000.0, now - timedelta(minutes=10)),
        ("t2", "mkt-1", "a1", "0xwalletB", "BUY", 0.55, 2000.0, now - timedelta(minutes=5)),
        ("t3", "mkt-1", "a1", "0xwalletC", "BUY", 0.70, 5000.0, now - timedelta(minutes=1)),
    ]
    conn.executemany(
        "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'ws', CURRENT_TIMESTAMP)",
        trades,
    )


class TestPriceImpact:
    def test_compute_impact_score_basic(self):
        """Pure-Python formula: |ΔP| / L × V."""
        score = compute_impact_score(price_delta=0.05, liquidity_usd=10000.0, size_usd=2000.0)
        assert abs(score - 0.01) < 1e-9

    def test_compute_impact_score_zero_liquidity(self):
        """Zero liquidity should return 0 (not divide-by-zero)."""
        assert compute_impact_score(0.05, 0.0, 2000.0) == 0.0

    def test_compute_impact_score_negative_liquidity(self):
        assert compute_impact_score(0.05, -100.0, 2000.0) == 0.0

    def test_compute_impact_score_zero_delta(self):
        assert compute_impact_score(0.0, 10000.0, 2000.0) == 0.0

    def test_get_price_impact_single_trade(self):
        """The first trade has no predecessor → price_delta = 0."""
        conn = _init_db()
        _seed(conn)
        impact = get_price_impact(conn, "mkt-1", "t1")
        assert impact is not None
        assert impact.price_delta == 0.0  # No previous trade

    def test_get_price_impact_subsequent_trade(self):
        """Trade t2 follows t1 → price_delta = |0.55 - 0.50| = 0.05."""
        conn = _init_db()
        _seed(conn)
        impact = get_price_impact(conn, "mkt-1", "t2")
        assert impact is not None
        assert abs(impact.price_delta - 0.05) < 1e-5

    def test_get_price_impact_large_move(self):
        """Trade t3 has a 0.15 delta (0.70 - 0.55) on a $5000 trade."""
        conn = _init_db()
        _seed(conn)
        impact = get_price_impact(conn, "mkt-1", "t3")
        assert impact is not None
        assert abs(impact.price_delta - 0.15) < 1e-5
        assert impact.impact_score > 0

    def test_get_price_impact_missing_trade(self):
        conn = _init_db()
        _seed(conn)
        assert get_price_impact(conn, "mkt-1", "nonexistent") is None

    def test_get_high_impact_trades(self):
        conn = _init_db()
        _seed(conn)
        results = get_high_impact_trades(conn, min_size_usd=500.0, limit=10)
        assert isinstance(results, list)
        # t1 has 0 impact (first trade), t2 and t3 have non-zero
        nonzero = [r for r in results if r.impact_score > 0]
        assert len(nonzero) >= 1

    def test_get_high_impact_min_size_filter(self):
        """High min_size_usd filters out smaller trades."""
        conn = _init_db()
        _seed(conn)
        results = get_high_impact_trades(conn, min_size_usd=3000.0, limit=10)
        for r in results:
            assert r.size_usd >= 3000.0

    def test_price_impact_dataclass(self):
        pi = PriceImpact(
            trade_id="t1",
            market_id="m1",
            wallet="0x",
            size_usd=1000.0,
            price=0.5,
            price_delta=0.1,
            liquidity_usd=50000.0,
            impact_score=0.002,
        )
        assert pi.impact_score == 0.002

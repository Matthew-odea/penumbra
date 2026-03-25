"""Tests for sentinel.scanner.volume — volume anomaly detection."""

from datetime import UTC, datetime, timedelta

import duckdb

from sentinel.scanner.volume import (
    VolumeAnomaly,
    get_anomalies,
    get_anomaly_for_market,
    get_zscore_for_market,
)

# ── Helpers ─────────────────────────────────────────────────────────────────


def _init_db() -> duckdb.DuckDBPyConnection:
    """Create an in-memory DuckDB with the required schema + views."""
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
    # Views from init.py
    conn.execute("""
        CREATE OR REPLACE VIEW v_hourly_volume AS
        SELECT
            market_id,
            date_trunc('hour', timestamp) AS hour_bucket,
            COUNT(*) AS trade_count,
            SUM(size_usd) AS volume_usd,
            COUNT(DISTINCT wallet) AS unique_wallets
        FROM trades
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
            SELECT
                h.market_id,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY h.volume_usd) AS median_vol
            FROM hourly h
            GROUP BY 1
        ),
        market_mad AS (
            SELECT
                h.market_id,
                PERCENTILE_CONT(0.5) WITHIN GROUP (
                    ORDER BY ABS(h.volume_usd - ms.median_vol)
                ) AS mad_vol
            FROM hourly h
            JOIN market_stats ms ON h.market_id = ms.market_id
            GROUP BY 1
        )
        SELECT
            h.market_id,
            h.hour_bucket,
            h.volume_usd,
            h.trade_count,
            h.unique_wallets,
            ms.median_vol,
            mm.mad_vol,
            CASE
                WHEN mm.mad_vol > 0
                THEN 0.6745 * (h.volume_usd - ms.median_vol) / mm.mad_vol
                ELSE 0
            END AS modified_z_score
        FROM hourly h
        JOIN market_stats ms ON h.market_id = ms.market_id
        JOIN market_mad mm ON h.market_id = mm.market_id
    """)
    return conn


def _insert_trade(
    conn: duckdb.DuckDBPyConnection,
    *,
    trade_id: str,
    market_id: str = "mkt-1",
    wallet: str = "0xwallet",
    size_usd: float = 100.0,
    price: float = 0.5,
    hours_ago: float = 1.0,
) -> None:
    ts = datetime.now(tz=UTC) - timedelta(hours=hours_ago)
    conn.execute(
        "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ws', CURRENT_TIMESTAMP)",
        [trade_id, market_id, "asset-1", wallet, "BUY", price, size_usd, ts, None],
    )


# ── Tests ───────────────────────────────────────────────────────────────────


class TestVolumeAnomaly:
    """Unit tests for volume anomaly Z-score calculations."""

    def test_no_trades_returns_empty(self):
        conn = _init_db()
        result = get_anomalies(conn, threshold=0)
        assert result == []

    def test_single_market_single_hour_zscore_zero(self):
        """One hour bucket → MAD = 0 → Z-score must be 0."""
        conn = _init_db()
        _insert_trade(conn, trade_id="t1", size_usd=1000.0, hours_ago=0.5)
        result = get_anomalies(conn, threshold=0)
        assert len(result) == 1
        assert result[0].modified_z_score == 0.0

    def test_uniform_hours_zscore_zero(self):
        """Multiple hours with identical volume → MAD = 0 → Z-score = 0."""
        conn = _init_db()
        for i in range(5):
            _insert_trade(conn, trade_id=f"t{i}", size_usd=500.0, hours_ago=i + 0.5)
        result = get_anomalies(conn, threshold=0)
        for a in result:
            assert a.modified_z_score == 0.0

    def test_spike_produces_high_zscore(self):
        """One abnormally large hour should produce a high Z-score."""
        conn = _init_db()
        # Create 5 normal hours with varied but similar volumes to get
        # a meaningful MAD.  Multiple trades per hour at different sizes
        # ensure the hour-bucket volumes differ slightly (non-zero MAD).
        for i in range(1, 6):
            base = 100 + i * 10  # 110, 120, 130, 140, 150
            _insert_trade(conn, trade_id=f"tA{i}", size_usd=base, hours_ago=i + 0.3)
            _insert_trade(conn, trade_id=f"tB{i}", size_usd=base, hours_ago=i + 0.6)
        # Spike hour: $50,000 — extreme outlier
        _insert_trade(conn, trade_id="spike1", size_usd=25000.0, hours_ago=0.3)
        _insert_trade(conn, trade_id="spike2", size_usd=25000.0, hours_ago=0.6)
        result = get_anomalies(conn, threshold=0)
        zscores = [a.modified_z_score for a in result]
        assert max(zscores) > 3.5, f"Expected high Z-score, got {zscores}"

    def test_threshold_filters(self):
        """Only return anomalies above the given threshold."""
        conn = _init_db()
        for i in range(1, 6):
            _insert_trade(conn, trade_id=f"t{i}", size_usd=100.0, hours_ago=i + 0.5)
        _insert_trade(conn, trade_id="spike", size_usd=10000.0, hours_ago=0.5)

        all_results = get_anomalies(conn, threshold=0)
        high_only = get_anomalies(conn, threshold=3.5)
        assert len(high_only) <= len(all_results)

    def test_get_zscore_for_market_exists(self):
        conn = _init_db()
        _insert_trade(conn, trade_id="t1", market_id="mkt-x", hours_ago=0.5)
        z = get_zscore_for_market(conn, "mkt-x")
        assert isinstance(z, float)

    def test_get_zscore_for_market_missing(self):
        conn = _init_db()
        z = get_zscore_for_market(conn, "nonexistent")
        assert z == 0.0

    def test_get_anomaly_for_market(self):
        conn = _init_db()
        _insert_trade(conn, trade_id="t1", market_id="mkt-a", hours_ago=0.5)
        anomaly = get_anomaly_for_market(conn, "mkt-a")
        assert anomaly is not None
        assert anomaly.market_id == "mkt-a"

    def test_get_anomaly_for_market_missing(self):
        conn = _init_db()
        anomaly = get_anomaly_for_market(conn, "nope")
        assert anomaly is None

    def test_volume_anomaly_dataclass_fields(self):
        a = VolumeAnomaly(
            market_id="m1",
            hour_bucket=datetime.now(tz=UTC),
            volume_usd=5000.0,
            trade_count=10,
            unique_wallets=3,
            median_vol=1000.0,
            mad_vol=200.0,
            modified_z_score=4.5,
        )
        assert a.market_id == "m1"
        assert a.trade_count == 10
        assert a.modified_z_score == 4.5

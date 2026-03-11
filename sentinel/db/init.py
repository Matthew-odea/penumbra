"""DuckDB schema initialization.

Run directly to create/migrate the schema:
    python -m sentinel.db.init
"""

from pathlib import Path

import duckdb
import structlog

from sentinel.config import settings

logger = structlog.get_logger()

SCHEMA_SQL = """
-- =============================================================================
-- Penumbra — DuckDB Schema
-- Version: 001
-- =============================================================================

-- ─── Core Tables ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS markets (
    market_id       VARCHAR PRIMARY KEY,   -- Polymarket condition_id
    question        VARCHAR,
    slug            VARCHAR,
    category        VARCHAR,               -- Biotech, Politics, Crypto, Science
    end_date        TIMESTAMP,
    volume_usd      DECIMAL(18, 6),
    liquidity_usd   DECIMAL(18, 6),
    active          BOOLEAN DEFAULT TRUE,
    resolved        BOOLEAN DEFAULT FALSE,
    resolved_price  DECIMAL(10, 6),        -- 1.0 if YES, 0.0 if NO, NULL if unresolved
    last_synced     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id        VARCHAR PRIMARY KEY,
    market_id       VARCHAR NOT NULL,
    asset_id        VARCHAR NOT NULL,       -- token_id
    wallet          VARCHAR NOT NULL,       -- taker_address
    side            VARCHAR NOT NULL,       -- BUY or SELL
    price           DECIMAL(10, 6),         -- 0-1 probability
    size_usd        DECIMAL(18, 6),
    timestamp       TIMESTAMP NOT NULL,
    tx_hash         VARCHAR,
    ingested_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Index for scanner queries
CREATE INDEX IF NOT EXISTS idx_trades_market_ts
    ON trades (market_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_wallet
    ON trades (wallet);
CREATE INDEX IF NOT EXISTS idx_trades_timestamp
    ON trades (timestamp);

-- ─── Signal Tables ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS signals (
    signal_id           VARCHAR PRIMARY KEY,
    trade_id            VARCHAR NOT NULL,
    market_id           VARCHAR NOT NULL,
    wallet              VARCHAR NOT NULL,
    side                VARCHAR NOT NULL,
    price               DECIMAL(10, 6),
    size_usd            DECIMAL(18, 6),
    trade_timestamp     TIMESTAMP NOT NULL,
    -- Statistical filter
    volume_z_score      DECIMAL(8, 4),
    modified_z_score    DECIMAL(8, 4),
    price_impact        DECIMAL(8, 6),
    -- Behavioral filter
    wallet_win_rate     DECIMAL(5, 4),
    wallet_total_trades INTEGER,
    is_whitelisted      BOOLEAN DEFAULT FALSE,
    funding_anomaly     BOOLEAN DEFAULT FALSE,
    funding_age_minutes INTEGER,
    -- Composite score (pre-Judge)
    statistical_score   INTEGER,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS signal_reasoning (
    signal_id           VARCHAR PRIMARY KEY,
    trade_id            VARCHAR NOT NULL,
    classification      VARCHAR,            -- INFORMED / NOISE
    tier1_confidence    INTEGER,
    suspicion_score     INTEGER,            -- Final score (1-100)
    reasoning           VARCHAR,
    key_evidence        VARCHAR,
    news_headlines      VARCHAR,            -- JSON array
    tier1_model         VARCHAR,
    tier2_model         VARCHAR,
    tier1_tokens        INTEGER,
    tier2_tokens        INTEGER,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ─── Budget Tracking ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS llm_budget (
    date            DATE NOT NULL,
    tier            VARCHAR NOT NULL,       -- "tier1" or "tier2"
    calls_used      INTEGER DEFAULT 0,
    calls_limit     INTEGER NOT NULL,
    PRIMARY KEY (date, tier)
);

-- ─── Analytical Views ────────────────────────────────────────────────────────

-- Hourly volume per market (for Z-score calculations)
CREATE OR REPLACE VIEW v_hourly_volume AS
SELECT
    market_id,
    date_trunc('hour', timestamp) AS hour_bucket,
    COUNT(*) AS trade_count,
    SUM(size_usd) AS volume_usd,
    COUNT(DISTINCT wallet) AS unique_wallets
FROM trades
WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '7 days'
GROUP BY 1, 2;

-- Modified Z-Score anomaly detection
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
JOIN market_mad mm ON h.market_id = mm.market_id;

-- Wallet performance on resolved markets
CREATE OR REPLACE VIEW v_wallet_performance AS
SELECT
    t.wallet,
    COUNT(*) AS total_resolved_trades,
    SUM(CASE
        WHEN (t.side = 'BUY' AND m.resolved_price >= 0.95) OR
             (t.side = 'SELL' AND m.resolved_price <= 0.05)
        THEN 1 ELSE 0
    END) AS wins,
    CASE
        WHEN COUNT(*) > 0
        THEN SUM(CASE
            WHEN (t.side = 'BUY' AND m.resolved_price >= 0.95) OR
                 (t.side = 'SELL' AND m.resolved_price <= 0.05)
            THEN 1 ELSE 0
        END)::FLOAT / COUNT(*)
        ELSE 0
    END AS win_rate
FROM trades t
JOIN markets m ON t.market_id = m.market_id
WHERE m.resolved = TRUE
GROUP BY t.wallet
HAVING COUNT(*) >= 5;
"""


def init_schema(db_path: Path | None = None) -> duckdb.DuckDBPyConnection:
    """Create or open the DuckDB database and apply the schema.

    Args:
        db_path: Path to the .duckdb file. Defaults to settings.duckdb_path.

    Returns:
        An open DuckDB connection.
    """
    db_path = db_path or settings.duckdb_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Initializing DuckDB schema", path=str(db_path))
    conn = duckdb.connect(str(db_path))
    conn.execute(SCHEMA_SQL)
    logger.info("Schema initialized successfully")

    # Log table counts for verification
    tables = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()
    logger.info("Tables available", tables=[t[0] for t in tables])

    return conn


if __name__ == "__main__":
    conn = init_schema()
    print(f"DuckDB initialized at {settings.duckdb_path}")
    tables = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()
    for t in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {t[0]}").fetchone()[0]
        print(f"  {t[0]}: {count} rows")
    conn.close()

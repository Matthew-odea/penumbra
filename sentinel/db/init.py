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
    source          VARCHAR DEFAULT 'ws',   -- 'ws' or 'rest'
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
    -- Enriched signals (sprint 4)
    ofi_score           DECIMAL(8, 4),   -- Order flow imbalance [-1, 1]
    hours_to_resolution INTEGER,         -- Hours from trade to market end_date
    market_concentration DECIMAL(5, 4),  -- Fraction of wallet's recent 50 trades on this market
    -- Detection improvements (sprint 5)
    coordination_wallet_count INTEGER DEFAULT 0,     -- Distinct wallets in same 5-min window
    liquidity_cliff     BOOLEAN DEFAULT FALSE,        -- Spread widened >30% in 10 min before trade
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
    tier2_used          BOOLEAN DEFAULT FALSE,
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

-- Order flow imbalance per market-hour (last 24h)
-- OFI = (buy_vol - sell_vol) / total_vol  ∈ [-1, 1]
-- Strongly positive = net buying; negative = net selling
CREATE OR REPLACE VIEW v_order_flow_imbalance AS
SELECT
    market_id,
    date_trunc('hour', timestamp) AS hour_bucket,
    SUM(CASE WHEN side = 'BUY'  THEN size_usd ELSE 0 END) AS buy_volume,
    SUM(CASE WHEN side = 'SELL' THEN size_usd ELSE 0 END) AS sell_volume,
    SUM(size_usd) AS total_volume,
    CASE
        WHEN SUM(size_usd) > 0
        THEN (
            SUM(CASE WHEN side = 'BUY'  THEN size_usd ELSE 0 END) -
            SUM(CASE WHEN side = 'SELL' THEN size_usd ELSE 0 END)
        ) / SUM(size_usd)
        ELSE 0
    END AS ofi
FROM trades
WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
GROUP BY 1, 2;

-- 5-minute volume per market (for fine-grained Z-score detection)
-- Uses epoch arithmetic: floor(epoch / 300) * 300 → 5-min bucket boundary
CREATE OR REPLACE VIEW v_5m_volume AS
SELECT
    market_id,
    to_timestamp(FLOOR(epoch(timestamp) / 300) * 300) AS bucket_5m,
    COUNT(*) AS trade_count,
    SUM(size_usd) AS volume_usd,
    COUNT(DISTINCT wallet) AS unique_wallets
FROM trades
WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '2 hours'
GROUP BY 1, 2;

-- Modified Z-Score on 5-minute buckets (mirrors v_volume_anomalies logic)
CREATE OR REPLACE VIEW v_volume_anomalies_5m AS
WITH market_stats AS (
    SELECT
        market_id,
        PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY volume_usd) AS median_vol
    FROM v_5m_volume
    GROUP BY 1
),
market_mad AS (
    SELECT
        v.market_id,
        PERCENTILE_CONT(0.5) WITHIN GROUP (
            ORDER BY ABS(v.volume_usd - ms.median_vol)
        ) AS mad_vol
    FROM v_5m_volume v
    JOIN market_stats ms ON v.market_id = ms.market_id
    GROUP BY 1
)
SELECT
    v.market_id,
    v.bucket_5m AS hour_bucket,
    v.volume_usd,
    v.trade_count,
    v.unique_wallets,
    ms.median_vol,
    mm.mad_vol,
    CASE
        WHEN mm.mad_vol > 0
        THEN 0.6745 * (v.volume_usd - ms.median_vol) / mm.mad_vol
        ELSE 0
    END AS modified_z_score
FROM v_5m_volume v
JOIN market_stats ms ON v.market_id = ms.market_id
JOIN market_mad mm ON v.market_id = mm.market_id;

-- Coordination signals: ≥3 distinct wallets trading same market+side in a 5-min window
CREATE OR REPLACE VIEW v_coordination_signals AS
SELECT
    market_id,
    side,
    to_timestamp(FLOOR(epoch(timestamp) / 300) * 300) AS window_start,
    COUNT(DISTINCT wallet) AS wallet_count,
    SUM(size_usd) AS collective_volume_usd
FROM trades
WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '10 minutes'
GROUP BY 1, 2, 3
HAVING COUNT(DISTINCT wallet) >= 3;

-- Order book snapshots (best bid/ask every ~30s, for liquidity cliff detection)
CREATE TABLE IF NOT EXISTS book_snapshots (
    market_id   VARCHAR NOT NULL,
    asset_id    VARCHAR NOT NULL,
    ts          TIMESTAMP NOT NULL,
    best_bid    DECIMAL(10, 6),
    best_ask    DECIMAL(10, 6),
    PRIMARY KEY (market_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_book_snapshots_market_ts
    ON book_snapshots (market_id, ts);

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

    # ── Migrations (idempotent) ─────────────────────────────────────────
    # v002: add source column to trades (ws vs rest)
    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='trades'"
        ).fetchall()
    }
    if "source" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN source VARCHAR DEFAULT 'ws'")
        logger.info("Migration: added 'source' column to trades table")

    # v003: add tier2_used column to signal_reasoning
    sr_cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='signal_reasoning'"
        ).fetchall()
    }
    if "tier2_used" not in sr_cols:
        conn.execute("ALTER TABLE signal_reasoning ADD COLUMN tier2_used BOOLEAN DEFAULT FALSE")
        logger.info("Migration: added 'tier2_used' column to signal_reasoning table")

    # v004: enriched signal columns (OFI, time-to-resolution, concentration)
    sig_cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='signals'"
        ).fetchall()
    }
    if "ofi_score" not in sig_cols:
        conn.execute("ALTER TABLE signals ADD COLUMN ofi_score DECIMAL(8, 4)")
        logger.info("Migration: added 'ofi_score' column to signals table")
    if "hours_to_resolution" not in sig_cols:
        conn.execute("ALTER TABLE signals ADD COLUMN hours_to_resolution INTEGER")
        logger.info("Migration: added 'hours_to_resolution' column to signals table")
    if "market_concentration" not in sig_cols:
        conn.execute("ALTER TABLE signals ADD COLUMN market_concentration DECIMAL(5, 4)")
        logger.info("Migration: added 'market_concentration' column to signals table")

    # v005: coordination wallet count
    if "coordination_wallet_count" not in sig_cols:
        conn.execute("ALTER TABLE signals ADD COLUMN coordination_wallet_count INTEGER DEFAULT 0")
        logger.info("Migration: added 'coordination_wallet_count' column to signals table")

    # v006: liquidity cliff flag
    if "liquidity_cliff" not in sig_cols:
        conn.execute("ALTER TABLE signals ADD COLUMN liquidity_cliff BOOLEAN DEFAULT FALSE")
        logger.info("Migration: added 'liquidity_cliff' column to signals table")

    # Force WAL checkpoint so all schema changes are flushed to the .duckdb
    # file before this function returns.  Without this, if the process is
    # killed between a migration ALTER TABLE and DuckDB's next automatic
    # checkpoint, the WAL file is left with a partial replay that triggers
    # "Calling DatabaseManager::GetDefaultDatabase with no default database
    # set" on every subsequent startup — causing a crash-loop on every deploy.
    conn.execute("CHECKPOINT")
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

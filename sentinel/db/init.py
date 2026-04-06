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
-- Penumbra — DuckDB Schema  (migrations v002–v009 applied in init_schema())
-- =============================================================================

-- ─── Core Tables ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS markets (
    market_id            VARCHAR PRIMARY KEY,   -- Polymarket condition_id
    question             VARCHAR,
    slug                 VARCHAR,
    category             VARCHAR,               -- Raw tags joined with commas
    end_date             TIMESTAMP,
    volume_usd           DECIMAL(18, 6),
    liquidity_usd        DECIMAL(18, 6),
    active               BOOLEAN DEFAULT TRUE,
    resolved             BOOLEAN DEFAULT FALSE,
    resolved_price       DECIMAL(10, 6),        -- 1.0 if YES, 0.0 if NO, NULL if unresolved
    last_synced          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_price           DECIMAL(10, 6),        -- YES token current price (0-1 probability)
    attractiveness_score INTEGER,               -- LLM score 0-100, NULL until scored
    attractiveness_reason VARCHAR,              -- One-sentence explanation from LLM
    token_ids            VARCHAR                -- Comma-joined YES/NO token_ids for WS subscription
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

-- Deduplicated trades: WS and REST can ingest the same physical trade with
-- different trade_ids (WS uses synthetic IDs, REST uses the real API ID).
-- This view keeps one row per physical trade, preferring the REST version
-- (which has wallet address and tx_hash) over the WS version.
-- Dedup key: (market_id, side, second-truncated timestamp, rounded size).
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
FROM ranked WHERE rn = 1;

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
    -- Scoring metadata
    scoring_version     INTEGER,                      -- Formula version (1=pre-fix, 2=post-fix)
    position_trade_count INTEGER DEFAULT 0,           -- Wallet's trade count on this market+side
    -- Plan B Phase 1: microstructure metrics (data collection)
    vpin_percentile     DECIMAL(5, 4),                -- VPIN percentile [0, 1] vs 7-day history
    lambda_value        DECIMAL(10, 6),               -- Kyle's Lambda coefficient for the market
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
FROM v_deduped_trades
WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '7 days'
GROUP BY 1, 2;

-- Modified Z-Score anomaly detection
-- Baseline (median/MAD) uses the full 7-day window from v_hourly_volume for
-- statistical stability.  Only the most recent 2 hours are returned as
-- detection rows, so the baseline and detection windows are decoupled.
CREATE OR REPLACE VIEW v_volume_anomalies AS
WITH market_stats AS (
    SELECT
        market_id,
        PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY volume_usd) AS median_vol
    FROM v_hourly_volume
    GROUP BY 1
),
market_mad AS (
    SELECT
        h.market_id,
        PERCENTILE_CONT(0.5) WITHIN GROUP (
            ORDER BY ABS(h.volume_usd - ms.median_vol)
        ) AS mad_vol
    FROM v_hourly_volume h
    JOIN market_stats ms ON h.market_id = ms.market_id
    GROUP BY 1
),
recent AS (
    SELECT * FROM v_hourly_volume
    WHERE hour_bucket >= CURRENT_TIMESTAMP - INTERVAL '2 hours'
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
         AND ms.median_vol >= 500  -- suppress dormant markets (median < $500/hr → noisy z-scores)
        THEN 0.6745 * (h.volume_usd - ms.median_vol) / mm.mad_vol
        ELSE NULL
    END AS modified_z_score
FROM recent h
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
FROM v_deduped_trades
WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
GROUP BY 1, 2;

-- 5-minute volume per market (for fine-grained Z-score detection)
-- Uses epoch arithmetic: floor(epoch / 300) * 300 → 5-min bucket boundary
-- 24-hour window gives 288 buckets per market — enough for a stable baseline.
CREATE OR REPLACE VIEW v_5m_volume AS
SELECT
    market_id,
    to_timestamp(FLOOR(epoch(timestamp) / 300) * 300) AS bucket_5m,
    COUNT(*) AS trade_count,
    SUM(size_usd) AS volume_usd,
    COUNT(DISTINCT wallet) AS unique_wallets
FROM v_deduped_trades
WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
GROUP BY 1, 2;

-- Modified Z-Score on 5-minute buckets
-- Baseline uses the full 24-hour window; detection returns only the last 30 min.
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
),
recent AS (
    SELECT * FROM v_5m_volume
    WHERE bucket_5m >= CURRENT_TIMESTAMP - INTERVAL '30 minutes'
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
         AND ms.median_vol >= 50  -- suppress dormant markets (median < $50/5-min → noisy z-scores)
        THEN 0.6745 * (v.volume_usd - ms.median_vol) / mm.mad_vol
        ELSE NULL
    END AS modified_z_score
FROM recent v
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
FROM v_deduped_trades
WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '10 minutes'
  AND wallet != ''
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
FROM v_deduped_trades t
JOIN markets m ON t.market_id = m.market_id
WHERE m.resolved = TRUE
  AND t.wallet != ''
GROUP BY t.wallet
HAVING COUNT(*) >= 5;

-- Signal outcome validation: joins signals to market resolution for accuracy tracking.
-- Each row is one signal on a resolved market with its confusion matrix category.
-- Prediction proxy: statistical_score >= 80 is treated as "INFORMED" (high-suspicion call).
CREATE OR REPLACE VIEW v_signal_outcomes AS
SELECT
    s.signal_id,
    s.market_id,
    s.wallet,
    s.side,
    s.price,
    s.size_usd,
    s.statistical_score,
    s.trade_timestamp,
    s.created_at,
    m.question,
    m.resolved_price,
    CASE
        WHEN (s.side = 'BUY' AND m.resolved_price >= 0.95) OR
             (s.side = 'SELL' AND m.resolved_price <= 0.05)
        THEN TRUE ELSE FALSE
    END AS trade_correct,
    CASE
        WHEN s.statistical_score >= 80 AND (
            (s.side = 'BUY' AND m.resolved_price >= 0.95) OR
            (s.side = 'SELL' AND m.resolved_price <= 0.05)
        ) THEN 'TP'
        WHEN s.statistical_score >= 80 AND NOT (
            (s.side = 'BUY' AND m.resolved_price >= 0.95) OR
            (s.side = 'SELL' AND m.resolved_price <= 0.05)
        ) THEN 'FP'
        WHEN s.statistical_score < 80 AND (
            (s.side = 'BUY' AND m.resolved_price >= 0.95) OR
            (s.side = 'SELL' AND m.resolved_price <= 0.05)
        ) THEN 'FN'
        WHEN s.statistical_score < 80 AND NOT (
            (s.side = 'BUY' AND m.resolved_price >= 0.95) OR
            (s.side = 'SELL' AND m.resolved_price <= 0.05)
        ) THEN 'TN'
    END AS confusion
FROM signals s
JOIN markets m ON s.market_id = m.market_id
WHERE m.resolved = TRUE
  AND m.resolved_price IS NOT NULL;

-- Wallet position accumulation: detects wallets building positions
-- (multiple trades on the same market+side in a rolling 7-day window).
CREATE OR REPLACE VIEW v_wallet_positions AS
SELECT
    wallet,
    market_id,
    side,
    COUNT(*) AS trade_count,
    SUM(size_usd) AS total_volume_usd,
    MIN(timestamp) AS first_trade,
    MAX(timestamp) AS last_trade,
    EXTRACT(EPOCH FROM MAX(timestamp) - MIN(timestamp)) / 3600.0 AS span_hours,
    CASE
        WHEN EXTRACT(EPOCH FROM MAX(timestamp) - MIN(timestamp)) > 0
        THEN COUNT(*) / (EXTRACT(EPOCH FROM MAX(timestamp) - MIN(timestamp)) / 3600.0)
        ELSE NULL
    END AS trades_per_hour
FROM v_deduped_trades
WHERE wallet != ''
  AND timestamp >= CURRENT_TIMESTAMP - INTERVAL '7 days'
GROUP BY wallet, market_id, side
HAVING COUNT(*) >= 3;

-- ─── Plan B Phase 1: VPIN + Kyle's Lambda ─────────────────────────────────

-- VPIN volume-synchronized buckets
CREATE TABLE IF NOT EXISTS vpin_buckets (
    market_id      VARCHAR NOT NULL,
    bucket_idx     INTEGER NOT NULL,
    bucket_end     TIMESTAMP NOT NULL,
    buy_vol        DECIMAL(18, 6),
    sell_vol       DECIMAL(18, 6),
    bucket_volume  DECIMAL(18, 6),  -- buy_vol + sell_vol (denormalized for query speed)
    PRIMARY KEY (market_id, bucket_idx)
);

CREATE INDEX IF NOT EXISTS idx_vpin_market_end
    ON vpin_buckets (market_id, bucket_end);

-- Kyle's Lambda estimates per market
CREATE TABLE IF NOT EXISTS market_lambda (
    market_id    VARCHAR NOT NULL,
    estimated_at TIMESTAMP NOT NULL,
    lambda_value DECIMAL(12, 8),
    r_squared    DECIMAL(8, 6),
    residual_std DECIMAL(12, 8),
    n_obs        INTEGER,
    PRIMARY KEY (market_id, estimated_at)
);
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
    # Note: ALTER TABLE ADD COLUMN with DEFAULT triggers a DuckDB WAL replay
    # bug ("GetDefaultDatabase"). Use nullable column + UPDATE instead.
    if "coordination_wallet_count" not in sig_cols:
        conn.execute("ALTER TABLE signals ADD COLUMN coordination_wallet_count INTEGER")
        conn.execute("UPDATE signals SET coordination_wallet_count = 0 WHERE coordination_wallet_count IS NULL")
        logger.info("Migration: added 'coordination_wallet_count' column to signals table")

    # v006: liquidity cliff flag
    if "liquidity_cliff" not in sig_cols:
        conn.execute("ALTER TABLE signals ADD COLUMN liquidity_cliff BOOLEAN")
        conn.execute("UPDATE signals SET liquidity_cliff = FALSE WHERE liquidity_cliff IS NULL")
        logger.info("Migration: added 'liquidity_cliff' column to signals table")

    # v007-v009: market intelligence columns
    mkt_cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='markets'"
        ).fetchall()
    }
    if "last_price" not in mkt_cols:
        conn.execute("ALTER TABLE markets ADD COLUMN last_price DECIMAL(10, 6)")
        logger.info("Migration: added 'last_price' column to markets table")
    if "attractiveness_score" not in mkt_cols:
        conn.execute("ALTER TABLE markets ADD COLUMN attractiveness_score INTEGER")
        logger.info("Migration: added 'attractiveness_score' column to markets table")
    if "attractiveness_reason" not in mkt_cols:
        conn.execute("ALTER TABLE markets ADD COLUMN attractiveness_reason VARCHAR")
        logger.info("Migration: added 'attractiveness_reason' column to markets table")

    # v010: token_ids for WS subscription refresh
    if "token_ids" not in mkt_cols:
        conn.execute("ALTER TABLE markets ADD COLUMN token_ids VARCHAR")
        logger.info("Migration: added 'token_ids' column to markets table")

    # v011: scoring version tracking — distinguishes pre-fix (v1) from post-fix (v2) scores
    if "scoring_version" not in sig_cols:
        conn.execute("ALTER TABLE signals ADD COLUMN scoring_version INTEGER")
        conn.execute("UPDATE signals SET scoring_version = 1 WHERE scoring_version IS NULL")
        logger.info("Migration: added 'scoring_version' column to signals table")

    # v012: position accumulation tracking
    if "position_trade_count" not in sig_cols:
        conn.execute("ALTER TABLE signals ADD COLUMN position_trade_count INTEGER")
        conn.execute("UPDATE signals SET position_trade_count = 0 WHERE position_trade_count IS NULL")
        logger.info("Migration: added 'position_trade_count' column to signals table")

    # v013: VPIN percentile on signals (Plan B Phase 1)
    if "vpin_percentile" not in sig_cols:
        conn.execute("ALTER TABLE signals ADD COLUMN vpin_percentile DECIMAL(5, 4)")
        logger.info("Migration: added 'vpin_percentile' column to signals table")

    # v014: Lambda residual on signals (Plan B Phase 1)
    if "lambda_value" not in sig_cols:
        conn.execute("ALTER TABLE signals ADD COLUMN lambda_value DECIMAL(10, 6)")
        logger.info("Migration: added 'lambda_value' column to signals table")

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

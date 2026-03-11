-- =============================================================================
-- Supabase Migration: Initial Schema
-- Run via Supabase Dashboard → SQL Editor, or `supabase db push`
-- =============================================================================

-- ─── Wallets ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS wallets (
    address         TEXT PRIMARY KEY,
    label           TEXT,
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    total_trades    INTEGER DEFAULT 0,
    total_volume    NUMERIC(18,6) DEFAULT 0,
    win_count       INTEGER DEFAULT 0,
    loss_count      INTEGER DEFAULT 0,
    win_rate        NUMERIC(5,4) GENERATED ALWAYS AS (
                        CASE WHEN (win_count + loss_count) > 0
                             THEN win_count::NUMERIC / (win_count + loss_count)
                             ELSE 0
                        END
                    ) STORED,
    is_whitelisted  BOOLEAN DEFAULT FALSE,
    tags            TEXT[] DEFAULT '{}',
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wallets_whitelisted
    ON wallets (is_whitelisted) WHERE is_whitelisted = TRUE;
CREATE INDEX IF NOT EXISTS idx_wallets_win_rate
    ON wallets (win_rate DESC) WHERE (win_count + loss_count) >= 10;

-- ─── Signals ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS signals (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trade_id            TEXT NOT NULL,
    market_id           TEXT NOT NULL,
    market_question     TEXT,
    wallet              TEXT NOT NULL REFERENCES wallets(address),
    side                TEXT NOT NULL,
    price               NUMERIC(10,6),
    size_usd            NUMERIC(18,6),
    trade_timestamp     TIMESTAMPTZ NOT NULL,

    -- Statistical filter
    volume_z_score      NUMERIC(8,4),
    price_impact        NUMERIC(8,6),

    -- Behavioral filter
    wallet_win_rate     NUMERIC(5,4),
    is_whitelisted      BOOLEAN DEFAULT FALSE,
    funding_anomaly     BOOLEAN DEFAULT FALSE,
    funding_age_minutes INTEGER,

    -- Intelligence filter
    suspicion_score     INTEGER CHECK (suspicion_score BETWEEN 0 AND 100),
    reasoning           TEXT,
    news_headlines      JSONB,
    bedrock_model       TEXT,

    -- Metadata
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    notified            BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_signals_suspicion ON signals (suspicion_score DESC);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_wallet ON signals (wallet);

-- ─── Row Level Security ──────────────────────────────────────────────────────

ALTER TABLE signals ENABLE ROW LEVEL SECURITY;
ALTER TABLE wallets ENABLE ROW LEVEL SECURITY;

-- Anyone can read signals (dashboard uses anon key)
CREATE POLICY "Signals are publicly readable"
    ON signals FOR SELECT
    USING (true);

-- Only service role can write
CREATE POLICY "Service role manages signals"
    ON signals FOR ALL
    USING (auth.role() = 'service_role');

-- Anyone can read wallets
CREATE POLICY "Wallets are publicly readable"
    ON wallets FOR SELECT
    USING (true);

-- Only service role can write wallets
CREATE POLICY "Service role manages wallets"
    ON wallets FOR ALL
    USING (auth.role() = 'service_role');

-- ─── Realtime ────────────────────────────────────────────────────────────────

-- Enable realtime for signals table (dashboard live feed)
ALTER PUBLICATION supabase_realtime ADD TABLE signals;

-- ─── Helper Functions ────────────────────────────────────────────────────────

-- Auto-update updated_at on wallets
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER wallets_updated_at
    BEFORE UPDATE ON wallets
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();

# Integration: Supabase (Postgres + Auth + Storage)

> Persistent metadata store, wallet whitelist, and API layer for the dashboard.

## Role in the Stack

| Concern | Supabase Feature | Table/Bucket |
|---------|-----------------|--------------|
| Wallet whitelist & win-rate history | Postgres | `wallets`, `wallet_performance` |
| Signal metadata (for dashboard queries) | Postgres | `signals`, `signal_reasoning` |
| Market metadata cache | Postgres | `markets` |
| DuckDB nightly backups | Storage | `backups/` bucket |
| Dashboard auth (optional) | Auth | Built-in |
| Real-time feed to dashboard | Realtime | Postgres changes on `signals` |

## Why Supabase Instead of Just DuckDB?

DuckDB is single-writer, in-process, and local. It's perfect for analytics but:
- The Next.js dashboard can't connect to an in-process DuckDB
- We need a persistent, network-accessible store for the wallet whitelist
- Supabase Realtime lets the dashboard show live signals without polling
- Supabase free tier: 500MB database, 1GB storage, 50K monthly active users

## Schema

### `wallets` — Behavioral Profile

```sql
CREATE TABLE wallets (
    address         TEXT PRIMARY KEY,
    label           TEXT,               -- "Known Whale", "Polymarket MM", etc.
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
    is_whitelisted  BOOLEAN DEFAULT FALSE,   -- Manually flagged "smart money"
    tags            TEXT[] DEFAULT '{}',       -- ["biotech_winner", "election_caller"]
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Index for behavioral filter lookups
CREATE INDEX idx_wallets_whitelisted ON wallets (is_whitelisted) WHERE is_whitelisted = TRUE;
CREATE INDEX idx_wallets_win_rate ON wallets (win_rate DESC) WHERE (win_count + loss_count) >= 10;
```

### `signals` — Flagged Trades

```sql
CREATE TABLE signals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trade_id        TEXT NOT NULL,
    market_id       TEXT NOT NULL,
    market_question TEXT,
    wallet          TEXT NOT NULL REFERENCES wallets(address),
    side            TEXT NOT NULL,
    price           NUMERIC(10,6),
    size_usd        NUMERIC(18,6),
    trade_timestamp TIMESTAMPTZ NOT NULL,
    
    -- Statistical filter outputs
    volume_z_score  NUMERIC(8,4),
    price_impact    NUMERIC(8,6),
    
    -- Behavioral filter outputs
    wallet_win_rate     NUMERIC(5,4),
    is_whitelisted      BOOLEAN DEFAULT FALSE,
    funding_anomaly     BOOLEAN DEFAULT FALSE,
    funding_age_minutes INTEGER,
    
    -- Intelligence filter outputs
    suspicion_score     INTEGER CHECK (suspicion_score BETWEEN 0 AND 100),
    reasoning           TEXT,
    news_headlines      JSONB,
    bedrock_model       TEXT,
    
    -- Metadata
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    notified        BOOLEAN DEFAULT FALSE     -- Alert notification sent?
);

CREATE INDEX idx_signals_suspicion ON signals (suspicion_score DESC);
CREATE INDEX idx_signals_created ON signals (created_at DESC);
CREATE INDEX idx_signals_wallet ON signals (wallet);

-- Enable Realtime for dashboard
ALTER PUBLICATION supabase_realtime ADD TABLE signals;
```

### Row Level Security (RLS)

```sql
-- Public read for dashboard (authenticated via Supabase anon key)
ALTER TABLE signals ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Signals are viewable by authenticated users"
    ON signals FOR SELECT
    USING (auth.role() = 'authenticated' OR auth.role() = 'anon');

-- Only the service role (Python backend) can insert/update
CREATE POLICY "Service role can manage signals"
    ON signals FOR ALL
    USING (auth.role() = 'service_role');
```

## Python Client Setup

```python
from supabase import create_client, Client
from sentinel.config import settings

supabase: Client = create_client(
    supabase_url=settings.supabase_url,
    supabase_key=settings.supabase_service_key,  # Service role for writes
)

# Insert a signal
supabase.table("signals").insert({
    "trade_id": "abc-123",
    "market_id": "0x...",
    "wallet": "0x...",
    # ...
}).execute()

# Query wallet history
wallet = supabase.table("wallets") \
    .select("*") \
    .eq("address", "0xABC...") \
    .single() \
    .execute()
```

## Dashboard Integration

> **Note:** The dashboard (Vite + React) now reads all data from the
> **FastAPI gateway** (`/api/signals`, `/api/markets`, etc.) which queries
> DuckDB directly. Supabase is retained as an optional persistent store
> and for future Realtime subscriptions if needed.

## Backup Strategy

Nightly cron uploads the DuckDB file to Supabase Storage:

```python
import os
from supabase import create_client

supabase = create_client(url, key)
with open("sentinel.duckdb", "rb") as f:
    supabase.storage.from_("backups").upload(
        f"sentinel-{date.today().isoformat()}.duckdb",
        f,
        {"content-type": "application/octet-stream"}
    )
```

## Environment Variables

```
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=eyJ...       # For dashboard (public, read-only)
SUPABASE_SERVICE_KEY=eyJ...    # For Python backend (full access)
```

## Costs (Free Tier)

| Resource | Free Limit | Our Usage Estimate |
|----------|-----------|-------------------|
| Database | 500 MB | ~50 MB/month (signals + wallets) |
| Storage | 1 GB | ~200 MB (7 daily backups × ~30 MB) |
| Realtime | 200 concurrent connections | 1-5 (dashboard users) |
| Edge Functions | 500K invocations | 0 (we use FastAPI) |

# Sprint 2: The Scanner (Statistical Signal Engine)

**Goal:** Detect "Informed Flow" candidates using statistical anomalies (volume Z-scores, price impact) and behavioral signals (wallet profiling, funding anomalies).

**Duration:** ~4-6 hours  
**Depends on:** Sprint 1 (trades + markets in DuckDB)

---

## Architecture

```
Scanner Queue (from Ingester)
         │
         ▼
  [Volume Anomaly Detector]  ── Modified Z-Score per market/hour
         │
         ▼
  [Price Impact Calculator]  ── Impact vs liquidity ratio
         │
         ▼
  [Wallet Profiler]          ── Win rate from Supabase + DuckDB
         │
         ▼
  [Funding Anomaly Checker]  ── Alchemy API for wallet age
         │
         ▼
  [Signal Scorer]            ── Composite score → threshold → emit Signal
         │
         ▼
  Judge Queue (Sprint 3)
```

## Tasks

### 2.1 — Volume Anomaly View (`sentinel/scanner/volume.py`)

DuckDB SQL view that computes Modified Z-Scores per market per hour:

```sql
CREATE OR REPLACE VIEW v_volume_anomalies AS
WITH hourly AS (
    SELECT
        market_id,
        date_trunc('hour', timestamp) AS hour_bucket,
        COUNT(*) AS trade_count,
        SUM(size_usd) AS volume_usd,
        COUNT(DISTINCT wallet) AS unique_wallets
    FROM trades
    WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
    GROUP BY 1, 2
),
rolling_stats AS (
    SELECT
        market_id,
        PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY volume_usd) AS median_vol,
        PERCENTILE_CONT(0.5) WITHIN GROUP (
            ORDER BY ABS(volume_usd - PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY volume_usd))
        ) AS mad_vol
    FROM hourly
    GROUP BY 1
)
SELECT
    h.market_id,
    h.hour_bucket,
    h.volume_usd,
    h.trade_count,
    h.unique_wallets,
    r.median_vol,
    r.mad_vol,
    CASE
        WHEN r.mad_vol > 0
        THEN 0.6745 * (h.volume_usd - r.median_vol) / r.mad_vol
        ELSE 0
    END AS modified_z_score
FROM hourly h
JOIN rolling_stats r ON h.market_id = r.market_id;
```

**Acceptance criteria:**
- [ ] View returns results for all markets with trades in the last 24h
- [ ] Modified Z-Score correctly handles zero-MAD markets (returns 0)
- [ ] Configurable Z-Score threshold (default: 3.5)
- [ ] Exposes both standard and modified Z-scores for comparison

### 2.2 — Price Impact Calculator (`sentinel/scanner/price_impact.py`)

Measures how much a single trade moved the market price relative to liquidity:

$$\text{Price Impact} = \frac{|\Delta P|}{L} \times V$$

Where:
- $\Delta P$ = price change in the minute around the trade
- $L$ = market liquidity (from `markets` table)
- $V$ = trade size in USD

```sql
CREATE OR REPLACE VIEW v_price_impact AS
WITH trade_window AS (
    SELECT
        t.trade_id,
        t.market_id,
        t.wallet,
        t.price,
        t.size_usd,
        t.timestamp,
        LAG(t.price) OVER (PARTITION BY t.market_id ORDER BY t.timestamp) AS prev_price,
        LEAD(t.price) OVER (PARTITION BY t.market_id ORDER BY t.timestamp) AS next_price
    FROM trades t
    WHERE t.timestamp >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
)
SELECT
    tw.trade_id,
    tw.market_id,
    tw.wallet,
    tw.size_usd,
    tw.price,
    ABS(tw.price - COALESCE(tw.prev_price, tw.price)) AS price_delta,
    m.liquidity_usd,
    CASE
        WHEN m.liquidity_usd > 0
        THEN ABS(tw.price - COALESCE(tw.prev_price, tw.price)) / m.liquidity_usd * tw.size_usd
        ELSE 0
    END AS price_impact_score
FROM trade_window tw
JOIN markets m ON tw.market_id = m.market_id;
```

**Acceptance criteria:**
- [ ] Correctly identifies high-impact trades in low-liquidity markets
- [ ] Handles markets with zero liquidity gracefully
- [ ] Configurable minimum trade size to filter noise (default: $500)

### 2.3 — Wallet Win Rate Profiler (`sentinel/scanner/wallet_profiler.py`)

Calculates historical accuracy per wallet using resolved markets:

```sql
-- For DuckDB: compute wallet performance on resolved markets
CREATE OR REPLACE VIEW v_wallet_performance AS
WITH resolved_trades AS (
    SELECT
        t.wallet,
        t.market_id,
        t.side,
        t.price AS entry_price,
        m.category,
        -- Determine if trade was a "win":
        -- BUY at price P wins if market resolved YES (final price = 1.0)
        -- SELL at price P wins if market resolved NO (final price = 0.0)
        CASE
            WHEN t.side = 'BUY' AND m.resolved_price >= 0.95 THEN TRUE
            WHEN t.side = 'SELL' AND m.resolved_price <= 0.05 THEN TRUE
            ELSE FALSE
        END AS is_win
    FROM trades t
    JOIN markets m ON t.market_id = m.market_id
    WHERE m.resolved = TRUE
)
SELECT
    wallet,
    COUNT(*) AS total_resolved_trades,
    SUM(CASE WHEN is_win THEN 1 ELSE 0 END) AS wins,
    SUM(CASE WHEN is_win THEN 1 ELSE 0 END)::FLOAT / COUNT(*) AS win_rate,
    -- Category-specific win rates
    SUM(CASE WHEN is_win AND category = 'Biotech' THEN 1 ELSE 0 END) AS biotech_wins,
    SUM(CASE WHEN category = 'Biotech' THEN 1 ELSE 0 END) AS biotech_trades,
    SUM(CASE WHEN is_win AND category = 'Politics' THEN 1 ELSE 0 END) AS politics_wins,
    SUM(CASE WHEN category = 'Politics' THEN 1 ELSE 0 END) AS politics_trades
FROM resolved_trades
GROUP BY wallet
HAVING COUNT(*) >= 5;  -- Minimum trades for meaningful win rate
```

**Acceptance criteria:**
- [ ] Computes win rate per wallet with category breakdown
- [ ] Minimum trade threshold (default: 5) to avoid noise from low-activity wallets
- [ ] Syncs top performers to Supabase `wallets` table for persistent tracking
- [ ] Supports "Whitelist" flagging for wallets with win rate > 65% and > 20 trades

### 2.4 — Funding Anomaly Checker (`sentinel/scanner/funding.py`)

Calls Alchemy to check wallet funding recency for flagged trades:

**Acceptance criteria:**
- [ ] Only checks wallets involved in trades already flagged by volume or price impact
- [ ] Calls Alchemy Transfers API (not raw RPC)
- [ ] Flags wallets funded < 60 minutes before the trade
- [ ] Caches results for 1 hour to avoid redundant API calls
- [ ] Falls back gracefully if Alchemy is unavailable (logs warning, skips check)

### 2.5 — Composite Signal Scorer (`sentinel/scanner/scorer.py`)

Combines all filters into a single "Statistical Score" (0-100):

```python
def compute_statistical_score(
    z_score: float,
    price_impact: float,
    win_rate: float | None,
    is_whitelisted: bool,
    funding_anomaly: bool,
    funding_age_minutes: int | None,
) -> int:
    """Weighted composite score for passing to the Judge."""
    score = 0
    
    # Volume anomaly (0-40 points)
    if z_score > 3.5:
        score += min(40, int((z_score - 3.5) * 10))
    
    # Price impact (0-20 points)
    score += min(20, int(price_impact * 1000))
    
    # Wallet reputation (0-20 points)
    if is_whitelisted:
        score += 20
    elif win_rate and win_rate > 0.6:
        score += int(win_rate * 20)
    
    # Funding anomaly (0-20 points)
    if funding_anomaly:
        if funding_age_minutes and funding_age_minutes < 15:
            score += 20
        elif funding_age_minutes and funding_age_minutes < 60:
            score += 10
    
    return min(100, score)
```

**Acceptance criteria:**
- [ ] Produces a score 0-100 for every trade that passes any individual filter
- [ ] Trades scoring ≥ 30 are written to DuckDB `signals` table
- [ ] Trades scoring ≥ 30 are pushed to the Judge queue (Sprint 3)
- [ ] Weights are configurable in `sentinel/config.py`
- [ ] Prints `SIGNAL DETECTED: score={score} market={slug} wallet={addr[:8]}` to log

---

## Definition of Done

- [ ] `python -m sentinel.scanner` processes trades from DuckDB and emits signals
- [ ] At least one synthetic "anomaly" in test data triggers "SIGNAL DETECTED"
- [ ] Wallet profiler correctly computes win rates from resolved market data
- [ ] Funding anomaly check works against Alchemy (test with a known wallet)
- [ ] `signals` table in DuckDB contains flagged trades with composite scores

## Testing

| Test | Type | Command |
|------|------|---------|
| Z-score calculation | Unit | `pytest tests/scanner/test_volume.py` |
| Price impact edge cases | Unit | `pytest tests/scanner/test_price_impact.py` |
| Win rate computation | Unit | `pytest tests/scanner/test_wallet_profiler.py` |
| Composite scorer | Unit | `pytest tests/scanner/test_scorer.py` |
| Funding check (Alchemy) | Integration | `pytest tests/scanner/test_funding.py -m integration` |
| Full scanner pipeline | Smoke | `python -m sentinel.scanner --backtest` |

## Estimated Cost

- Alchemy: Free tier (300 CU/s = ~2 calls/sec)
- DuckDB: Local, free
- Supabase: Free tier (wallet sync)

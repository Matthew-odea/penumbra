# Implementation Prompt: Plan B Phase 1 — VPIN + Kyle's Lambda

> Give this prompt to an LLM with code editing tools and full repo access. It is self-contained.

---

## Objective

Add two new market microstructure metrics — **VPIN** (Volume-Synchronized Probability of Informed Trading) and **Kyle's Lambda** — as new scanner modules running **alongside** the existing scorer. This is Phase 1 of Plan B: data collection only, no scoring changes. The existing `compute_statistical_score` continues to drive signal emission unchanged.

**Success criteria:**
1. VPIN is computed per-market as trades arrive, stored in a new `vpin_buckets` table
2. Kyle's Lambda is estimated per-market on a rolling window, stored in a new `market_lambda` table
3. Both metrics are looked up during signal scoring and stored on each new signal (new columns: `vpin_percentile`, `lambda_residual`)
4. The existing scorer is untouched — `statistical_score` is computed identically to today
5. `make check` passes (lint + typecheck + all unit tests)
6. No new external dependencies (VPIN and Lambda use only stdlib + DuckDB SQL)

---

## Background: What These Metrics Are

### VPIN (Easley, Lopez de Prado, O'Hara 2012)

VPIN measures order flow toxicity — the probability that trades are driven by informed actors rather than noise. Unlike time-based metrics, VPIN uses **volume-synchronized** buckets: each bucket fills when a fixed amount of volume has traded, giving more resolution during intense activity (exactly when informed traders are active).

**Formula:**
```
VPIN = (1/N) * SUM_n |V_buy(n) - V_sell(n)| / V_bucket
```

Where:
- `V_bucket` = bucket volume threshold (avg daily volume / 50)
- `V_buy(n)` = buy-initiated volume in bucket n
- `V_sell(n)` = sell-initiated volume in bucket n
- `N` = number of trailing buckets to average (50)

**Key design decision:** Use Polymarket's native `side` field (BUY/SELL) for trade classification. This sidesteps the main academic criticism of VPIN (Andersen & Bondarenko 2014), which targeted the Bulk Volume Classification (BVC) approximation. We have true aggressor labels, so we don't need BVC.

**VPIN ranges from 0.0 to 1.0:**
- 0.0 = perfectly balanced flow (equal buying and selling in every bucket)
- 1.0 = completely one-sided flow (all buys or all sells in every bucket)
- Typical liquid market: 0.2-0.4
- Suspicious activity: >0.6

### Kyle's Lambda (Kyle 1985)

Lambda measures the permanent price impact of order flow — how much each dollar of net buying moves the price. It's the slope coefficient from regressing price changes on signed net volume:

```
delta_P(t) = lambda * SignedVolume(t) + epsilon
```

A high Lambda means the market is pricing in adverse selection (informed trading moves prices). A trade that moves the price more than Lambda predicts is anomalous.

**For our use:** We estimate Lambda per market via OLS on 5-minute intervals over a 1-hour window, then compute each trade's "residual" — how much its actual price impact deviates from what Lambda predicts. High residual = suspicious.

---

## Current Codebase State (Critical Context)

### Trade Flow
```
Polymarket WS/REST → writer.py (batch INSERT) → DuckDB trades table
                                                      ↓
                                                 v_deduped_trades (ROW_NUMBER dedup)
                                                      ↓
                                              Scanner._process_trade()
                                                      ↓
                                              build_signal() → DuckDB signals table
```

### Existing Files You Will Modify

**`sentinel/db/init.py`** (527 lines)
- Contains `SCHEMA_SQL` string with all CREATE TABLE and CREATE VIEW statements
- Contains `init_schema()` with idempotent migrations (v002-v012)
- Current tables: `markets`, `trades`, `signals`, `signal_reasoning`, `llm_budget`, `book_snapshots`
- Current views: `v_deduped_trades`, `v_hourly_volume`, `v_volume_anomalies`, `v_volume_anomalies_5m`, `v_5m_volume`, `v_order_flow_imbalance`, `v_coordination_signals`, `v_wallet_performance`, `v_signal_outcomes`, `v_wallet_positions`
- **You will add:** `vpin_buckets` table, `market_lambda` table, v013 and v014 migrations for new signal columns

**`sentinel/scanner/scorer.py`** (376 lines)
- `Signal` dataclass (25 fields, `as_db_tuple()` returns 25-element tuple)
- `_INSERT_SIGNAL_SQL` has 25 columns and 25 `?` placeholders
- `compute_statistical_score()` — DO NOT MODIFY the scoring logic
- `build_signal()` — add new parameters, pass through to Signal
- **You will add:** `vpin_percentile` and `lambda_residual` fields to Signal, update `as_db_tuple()`, `_INSERT_SIGNAL_SQL`, and `build_signal()`

**`sentinel/scanner/pipeline.py`** (302 lines)
- `Scanner._process_trade()` runs 9 detection layers then calls `build_signal()`
- **You will add:** VPIN lookup and Lambda residual computation as new steps before `build_signal()`

**`sentinel/config.py`** (131 lines)
- Pydantic `BaseSettings` class
- **You will add:** VPIN and Lambda configuration parameters

**`sentinel/ingester/writer.py`** (150 lines)
- `BatchWriter._write_batch()` does `INSERT OR IGNORE INTO trades`
- After writing, forwards trades to scanner_queue
- **You will add:** VPIN bucket accumulation call after trade write

### Existing Files You Will NOT Modify (reference only)

**`sentinel/ingester/models.py`** — `Trade` dataclass has: `trade_id`, `market_id`, `asset_id`, `wallet`, `side` (BUY/SELL), `price` (Decimal 0-1), `size_usd` (Decimal), `timestamp`, `tx_hash`, `source`

**`sentinel/scanner/volume.py`** — Contains SQL queries and helper functions. Reference for style. All SQL uses `?` parameter placeholders and queries `v_deduped_trades`.

### Tests You Will Modify/Create

**`tests/scanner/test_scorer.py`** (272 lines) — Update `test_signal_as_db_tuple` assertion from 25 to 27 columns. Scorer logic tests remain unchanged.

**`tests/scanner/test_pipeline.py`** — May need to mock new VPIN/Lambda lookups.

**`tests/api/conftest.py`** and **`tests/test_integration.py`** — Signal INSERT statements need new columns.

**`tests/test_regression.py`** — Tuple length assertion needs updating.

---

## Implementation Plan

### Step 1: New Module — `sentinel/scanner/vpin.py`

Create a new file that manages VPIN computation per market.

```python
"""VPIN (Volume-Synchronized Probability of Informed Trading).

Easley, Lopez de Prado, O'Hara (2012): "Flow Toxicity and Liquidity
in a High-frequency World."

Computes order flow toxicity using volume-synchronized buckets and
Polymarket's native trade aggressor labels (bypasses BVC criticism
from Andersen & Bondarenko 2014).
"""
```

**Core class: `VPINTracker`**

```python
class VPINTracker:
    """Manages VPIN bucket accumulation and computation per market.

    Usage:
        tracker = VPINTracker(conn)
        tracker.add_trade(market_id, side, size_usd, timestamp)
        vpin = tracker.get_vpin(market_id)  # Returns float or None
        pctile = tracker.get_vpin_percentile(market_id)  # Returns float [0,1] or None
    """
```

**Bucket mechanics:**

1. Each market has an independent bucket stream
2. `bucket_size` = market's average daily volume / 50 (configurable divisor)
   - Compute from DuckDB: `SELECT SUM(size_usd) / NULLIF(COUNT(DISTINCT date_trunc('day', timestamp)), 0) FROM v_deduped_trades WHERE market_id = ? AND timestamp >= CURRENT_TIMESTAMP - INTERVAL '7 days'`
   - Minimum bucket size: $100 (prevent degenerate buckets on low-volume markets)
   - Cache bucket sizes per market, refresh every hour
3. As trades arrive via `add_trade()`:
   - Accumulate `size_usd` into current bucket's buy_vol or sell_vol based on `side`
   - When accumulated volume >= bucket_size, close the bucket:
     - Write to `vpin_buckets` table: (market_id, bucket_idx, bucket_end_ts, buy_vol, sell_vol, bucket_volume)
     - Increment bucket_idx for this market
     - Start a new bucket with any overflow volume
4. VPIN = mean of `|buy_vol - sell_vol| / (buy_vol + sell_vol)` over the last N buckets (default 50)

**In-memory state:**
```python
# Per-market accumulation state (not persisted — rebuilt from DB on restart)
_current_buckets: dict[str, _BucketState]  # market_id → current unfilled bucket

@dataclass
class _BucketState:
    buy_vol: float = 0.0
    sell_vol: float = 0.0
    bucket_size: float = 0.0  # Target volume per bucket
    bucket_idx: int = 0       # Next bucket index
    last_refreshed: datetime  # When bucket_size was last computed
```

**DuckDB write (when bucket completes):**
```sql
INSERT INTO vpin_buckets (market_id, bucket_idx, bucket_end, buy_vol, sell_vol, bucket_volume)
VALUES (?, ?, ?, ?, ?, ?)
```

**VPIN query:**
```sql
SELECT
    AVG(ABS(buy_vol - sell_vol) / NULLIF(bucket_volume, 0)) AS vpin
FROM (
    SELECT buy_vol, sell_vol, buy_vol + sell_vol AS bucket_volume
    FROM vpin_buckets
    WHERE market_id = ?
    ORDER BY bucket_idx DESC
    LIMIT ?  -- N = 50
)
```

**VPIN percentile query (vs 7-day distribution for this market):**
```sql
WITH current AS (
    SELECT AVG(ABS(buy_vol - sell_vol) / NULLIF(buy_vol + sell_vol, 0)) AS vpin
    FROM (
        SELECT buy_vol, sell_vol
        FROM vpin_buckets
        WHERE market_id = ?
        ORDER BY bucket_idx DESC
        LIMIT ?
    )
),
historical AS (
    SELECT bucket_idx,
        AVG(ABS(buy_vol - sell_vol) / NULLIF(buy_vol + sell_vol, 0))
            OVER (ORDER BY bucket_idx ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) AS rolling_vpin
    FROM vpin_buckets
    WHERE market_id = ?
      AND bucket_end >= CURRENT_TIMESTAMP - INTERVAL '7 days'
)
SELECT
    (SELECT COUNT(*) FROM historical WHERE rolling_vpin <= (SELECT vpin FROM current)) * 1.0
    / NULLIF((SELECT COUNT(*) FROM historical), 0) AS percentile
```

Actually, this percentile query is complex. Simplify: just compute VPIN as a raw value and store it on the signal. The percentile can be computed more simply:

```python
def get_vpin_percentile(self, market_id: str) -> float | None:
    """Return current VPIN as a percentile [0, 1] vs this market's 7-day history."""
    current_vpin = self.get_vpin(market_id)
    if current_vpin is None:
        return None

    row = self._conn.execute("""
        WITH rolling AS (
            SELECT bucket_idx,
                AVG(ABS(buy_vol - sell_vol) / NULLIF(buy_vol + sell_vol, 0))
                    OVER (ORDER BY bucket_idx ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) AS vpin
            FROM vpin_buckets
            WHERE market_id = ?
              AND bucket_end >= CURRENT_TIMESTAMP - INTERVAL '7 days'
        )
        SELECT
            COUNT(*) FILTER (WHERE vpin <= ?) * 1.0 / NULLIF(COUNT(*), 0)
        FROM rolling
    """, [market_id, current_vpin]).fetchone()

    return float(row[0]) if row and row[0] is not None else None
```

**Startup recovery:** On init, query `SELECT MAX(bucket_idx) FROM vpin_buckets WHERE market_id = ?` to resume bucket numbering. Don't try to recover partial bucket state — just start a fresh bucket.

**Important edge cases:**
- Market with no trade history: bucket_size defaults to $1000, VPIN returns None
- Market with fewer than 5 completed buckets: VPIN returns None (insufficient data)
- Bucket overflow: if a single trade exceeds bucket_size, split it proportionally across buckets

### Step 2: New Module — `sentinel/scanner/kyle_lambda.py`

```python
"""Kyle's Lambda — permanent price impact coefficient.

Kyle (1985): "Continuous Auctions and Insider Trading."

Estimates how much each dollar of net order flow moves the price,
via OLS regression of price changes on signed volume in 5-minute windows.
"""
```

**Core function: `estimate_lambda()`**

Lambda estimation uses pure SQL — no numpy/scipy needed.

```python
def estimate_lambda(conn, market_id: str) -> tuple[float, float, float] | None:
    """Estimate Kyle's Lambda for a market using 1-hour rolling OLS.

    Returns (lambda, r_squared, residual_std) or None if insufficient data.

    The regression is: delta_price = lambda * signed_volume + epsilon
    where each observation is a 5-minute window.
    """
```

**SQL for OLS regression (pure DuckDB, no external deps):**

```sql
WITH five_min AS (
    SELECT
        to_timestamp(FLOOR(epoch(timestamp) / 300) * 300) AS bucket,
        -- Price change: last price minus first price in bucket
        LAST(price ORDER BY timestamp) - FIRST(price ORDER BY timestamp) AS delta_price,
        -- Signed volume: buy volume minus sell volume
        SUM(CASE WHEN side = 'BUY' THEN size_usd ELSE -size_usd END) AS signed_volume,
        COUNT(*) AS n_trades
    FROM v_deduped_trades
    WHERE market_id = ?
      AND timestamp >= CURRENT_TIMESTAMP - INTERVAL '1 hour'
    GROUP BY 1
    HAVING COUNT(*) >= 2  -- Need at least 2 trades for a meaningful price change
),
ols AS (
    SELECT
        COUNT(*) AS n,
        REGR_SLOPE(delta_price, signed_volume) AS lambda,
        REGR_R2(delta_price, signed_volume) AS r_squared,
        REGR_INTERCEPT(delta_price, signed_volume) AS intercept,
        STDDEV_POP(delta_price - (
            REGR_SLOPE(delta_price, signed_volume) * signed_volume +
            REGR_INTERCEPT(delta_price, signed_volume)
        )) AS residual_std
    FROM five_min
    WHERE signed_volume != 0  -- Exclude zero-flow buckets
)
SELECT lambda, r_squared, residual_std, n
FROM ols
WHERE n >= 6  -- Need at least 6 five-minute buckets (30 min of data)
```

**IMPORTANT:** DuckDB supports `REGR_SLOPE`, `REGR_R2`, `REGR_INTERCEPT` natively. No need for numpy. Verify this works with a test query first.

**Note on `residual_std` calculation:** The nested window function approach above may not work in DuckDB. Alternative: compute lambda and intercept first, then compute residuals in a second query:

```python
def estimate_lambda(conn, market_id: str) -> tuple[float, float, float] | None:
    # Step 1: Get 5-minute bucketed data and OLS coefficients
    row = conn.execute("""
        WITH five_min AS (
            SELECT
                to_timestamp(FLOOR(epoch(timestamp) / 300) * 300) AS bucket,
                LAST(price ORDER BY timestamp) - FIRST(price ORDER BY timestamp) AS delta_price,
                SUM(CASE WHEN side = 'BUY' THEN size_usd ELSE -size_usd END) AS signed_volume
            FROM v_deduped_trades
            WHERE market_id = ?
              AND timestamp >= CURRENT_TIMESTAMP - INTERVAL '1 hour'
            GROUP BY 1
            HAVING COUNT(*) >= 2
        )
        SELECT
            REGR_SLOPE(delta_price, signed_volume) AS lambda,
            REGR_R2(delta_price, signed_volume) AS r_squared,
            COUNT(*) AS n
        FROM five_min
        WHERE signed_volume != 0
    """, [market_id]).fetchone()

    if not row or row[0] is None or row[2] < 6:
        return None

    lambda_val, r_squared, n = float(row[0]), float(row[1] or 0), int(row[2])

    # Step 2: Compute residual std separately
    res_row = conn.execute("""
        WITH five_min AS (
            SELECT
                to_timestamp(FLOOR(epoch(timestamp) / 300) * 300) AS bucket,
                LAST(price ORDER BY timestamp) - FIRST(price ORDER BY timestamp) AS delta_price,
                SUM(CASE WHEN side = 'BUY' THEN size_usd ELSE -size_usd END) AS signed_volume
            FROM v_deduped_trades
            WHERE market_id = ?
              AND timestamp >= CURRENT_TIMESTAMP - INTERVAL '1 hour'
            GROUP BY 1
            HAVING COUNT(*) >= 2
        )
        SELECT STDDEV_POP(delta_price - (? * signed_volume))
        FROM five_min
        WHERE signed_volume != 0
    """, [market_id, lambda_val]).fetchone()

    residual_std = float(res_row[0]) if res_row and res_row[0] is not None else 0.0
    return (lambda_val, r_squared, residual_std)
```

**Persisting Lambda estimates:**

```python
def store_lambda(conn, market_id: str, lambda_val: float, r_squared: float,
                 residual_std: float, n_obs: int) -> None:
    conn.execute("""
        INSERT INTO market_lambda (market_id, estimated_at, lambda, r_squared, residual_std, n_obs)
        VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, ?)
    """, [market_id, lambda_val, r_squared, residual_std, n_obs])
```

**Computing a trade's Lambda residual:**

```python
def get_lambda_residual(conn, market_id: str, actual_price_change: float,
                        signed_volume: float) -> float | None:
    """Compute how much a trade's price impact deviates from Lambda prediction.

    Returns |actual_change - lambda * signed_volume| / residual_std, or None.
    """
    row = conn.execute("""
        SELECT lambda, residual_std
        FROM market_lambda
        WHERE market_id = ?
        ORDER BY estimated_at DESC
        LIMIT 1
    """, [market_id]).fetchone()

    if not row or row[0] is None:
        return None

    lambda_val, residual_std = float(row[0]), float(row[1] or 0)
    if residual_std <= 0:
        return None

    predicted_change = lambda_val * signed_volume
    return abs(actual_price_change - predicted_change) / residual_std
```

**When to estimate Lambda:**
- Option A: In the scanner, before scoring each trade (real-time but expensive)
- Option B: On a periodic timer (every 5 minutes per active market)
- **Recommended: Option A for Phase 1** — compute inline since the SQL is fast (~1ms on DuckDB). We can optimize later if needed.

### Step 3: Schema Changes — `sentinel/db/init.py`

**Add to SCHEMA_SQL** (before the `"""` closing the string, after `v_wallet_positions`):

```sql
-- VPIN volume-synchronized buckets (Plan B Phase 1)
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

-- Kyle's Lambda estimates per market (Plan B Phase 1)
CREATE TABLE IF NOT EXISTS market_lambda (
    market_id    VARCHAR NOT NULL,
    estimated_at TIMESTAMP NOT NULL,
    lambda       DECIMAL(12, 8),
    r_squared    DECIMAL(8, 6),
    residual_std DECIMAL(12, 8),
    n_obs        INTEGER,
    PRIMARY KEY (market_id, estimated_at)
);
```

**Add migrations** (after v012 in `init_schema()`):

```python
# v013: VPIN percentile on signals (Plan B Phase 1)
if "vpin_percentile" not in sig_cols:
    conn.execute("ALTER TABLE signals ADD COLUMN vpin_percentile DECIMAL(5, 4)")
    logger.info("Migration: added 'vpin_percentile' column to signals table")

# v014: Lambda residual on signals (Plan B Phase 1)
if "lambda_residual" not in sig_cols:
    conn.execute("ALTER TABLE signals ADD COLUMN lambda_residual DECIMAL(10, 6)")
    logger.info("Migration: added 'lambda_residual' column to signals table")
```

### Step 4: Signal Dataclass Changes — `sentinel/scanner/scorer.py`

**Add to Signal dataclass** (after `position_trade_count`):

```python
vpin_percentile: float | None = None     # VPIN percentile [0, 1] vs 7-day market history
lambda_residual: float | None = None     # |actual_impact - lambda * volume| / residual_std
```

**Update `as_db_tuple()`** to include the two new fields (after `position_trade_count`, before `created_at`):

```python
self.vpin_percentile,
self.lambda_residual,
```

**Update `_INSERT_SIGNAL_SQL`** — add `vpin_percentile, lambda_residual` to both the column list and the VALUES placeholders (now 27 columns, 27 `?`s).

**Update `build_signal()`** — add keyword parameters:
```python
vpin_percentile: float | None = None,
lambda_residual: float | None = None,
```
And pass them through to the Signal constructor.

**DO NOT modify `compute_statistical_score()`** — these new fields are stored on signals for data collection only. They will be used for scoring in Phase 3.

### Step 5: Pipeline Integration — `sentinel/scanner/pipeline.py`

**Add imports:**
```python
from sentinel.scanner.vpin import VPINTracker
from sentinel.scanner.kyle_lambda import estimate_lambda, get_lambda_residual
```

**In `Scanner.__init__()`:**
```python
self._vpin_tracker = VPINTracker(conn)
```

**In `Scanner._process_trade()`, add VPIN accumulation early** (right after the skip-tiny-trades check):
```python
# Accumulate into VPIN buckets (regardless of whether trade triggers a signal)
try:
    self._vpin_tracker.add_trade(
        trade.market_id, trade.side, float(trade.size_usd), trade.timestamp
    )
except Exception as exc:
    logger.debug("VPIN accumulation failed", market=trade.market_id, error=str(exc))
```

**Before `build_signal()`, look up VPIN and Lambda:**
```python
# 10. VPIN percentile (Plan B Phase 1 — data collection)
vpin_percentile: float | None = None
try:
    vpin_percentile = self._vpin_tracker.get_vpin_percentile(trade.market_id)
except Exception as exc:
    logger.debug("VPIN lookup failed", market=trade.market_id, error=str(exc))

# 11. Kyle's Lambda residual (Plan B Phase 1 — data collection)
lambda_residual: float | None = None
try:
    # Estimate lambda inline (fast: single DuckDB query)
    lam = estimate_lambda(self._conn, trade.market_id)
    if lam is not None:
        lambda_val, r_sq, res_std = lam
        # Store the estimate for future queries
        from sentinel.scanner.kyle_lambda import store_lambda
        store_lambda(self._conn, trade.market_id, lambda_val, r_sq, res_std, 0)
        # Compute this trade's residual
        # We need the actual price change and signed volume for this trade
        signed_vol = float(trade.size_usd) if trade.side == "BUY" else -float(trade.size_usd)
        actual_change = float(trade.price) - _get_prev_price(self._conn, trade.market_id, trade.timestamp)
        if actual_change is not None:
            lambda_residual = get_lambda_residual(
                self._conn, trade.market_id, actual_change, signed_vol
            )
except Exception as exc:
    logger.debug("Lambda computation failed", market=trade.market_id, error=str(exc))
```

**WAIT — the actual_change computation above is wrong.** The trade's `price` is the trade execution price, not the market mid-price before and after. We need a simpler approach:

**Better approach for Lambda residual in pipeline:**

Don't compute per-trade residuals inline. Instead:
1. `estimate_lambda()` already stores the estimate with `residual_std`
2. For the signal, store the **Lambda value itself** as `lambda_residual` (renaming: call the column `lambda_value` or store the raw lambda, not the residual)

Actually, re-reading Plan B, the lambda residual is defined as: "A trade's impact anomaly = |actual_impact - lambda * signed_volume| / std(residuals)". The `actual_impact` comes from the existing `get_price_impact()` which already measures the trade's real price movement.

**Simpler approach:**
```python
# 11. Kyle's Lambda residual
lambda_residual: float | None = None
try:
    lam = estimate_lambda(self._conn, trade.market_id)
    if lam is not None:
        lambda_val, r_sq, res_std = lam
        store_lambda(self._conn, trade.market_id, lambda_val, r_sq, res_std, 0)
        # Residual: how much does this trade's impact deviate from lambda prediction?
        # price_impact_score is already computed above (step 2)
        if price_impact_score > 0 and res_std > 0:
            signed_vol = float(trade.size_usd) if trade.side == "BUY" else -float(trade.size_usd)
            predicted_impact = abs(lambda_val * signed_vol)
            lambda_residual = abs(price_impact_score - predicted_impact) / res_std
except Exception as exc:
    logger.debug("Lambda failed", market=trade.market_id, error=str(exc))
```

**Hmm, this is conflating two different scales.** `price_impact_score` is a 0-20 score, not the raw price change. We need the raw price impact value from the `PriceImpact` dataclass.

**Look at the existing price impact code path in pipeline.py:**
```python
impact = get_price_impact(self._conn, trade.market_id, trade.trade_id)
if impact:
    price_impact_score = impact.impact_score
```

The `impact` object likely has a raw delta_price or similar field. Check `sentinel/scanner/price_impact.py` for the `PriceImpact` dataclass.

**For Phase 1, simplify:** Just store the Lambda estimate on the signal. Don't compute per-trade residuals yet — that requires understanding the raw price impact values which we'll clean up in Phase 2.

```python
# Store just the VPIN percentile and Lambda value on the signal.
# Per-trade residual computation deferred to Phase 2 when we rework scoring.
```

**REVISED APPROACH for Phase 1:**

Store two values on each signal:
1. `vpin_percentile` — current VPIN percentile for this market (how toxic is flow right now?)
2. `lambda_residual` — current Lambda value for this market (how much adverse selection?)

Both are market-level context metrics. Per-trade residuals come in Phase 2.

```python
# 10. VPIN percentile
vpin_percentile: float | None = None
try:
    vpin_percentile = self._vpin_tracker.get_vpin_percentile(trade.market_id)
except Exception as exc:
    logger.debug("VPIN lookup failed", market=trade.market_id, error=str(exc))

# 11. Kyle's Lambda (market-level adverse selection)
lambda_residual: float | None = None
try:
    lam = estimate_lambda(self._conn, trade.market_id)
    if lam is not None:
        lambda_val, r_sq, res_std = lam
        store_lambda(self._conn, trade.market_id, lambda_val, r_sq, res_std, 0)
        lambda_residual = lambda_val  # Store raw lambda; residual computation in Phase 2
except Exception as exc:
    logger.debug("Lambda failed", market=trade.market_id, error=str(exc))
```

Then pass both to `build_signal()`:
```python
signal = build_signal(
    ...,
    vpin_percentile=vpin_percentile,
    lambda_residual=lambda_residual,
)
```

### Step 6: Writer Integration for VPIN — `sentinel/ingester/writer.py`

VPIN needs to see ALL trades, not just those that pass the scanner's pre-check gate. The `VPINTracker.add_trade()` call should happen as close to trade ingestion as possible.

**Option A (recommended):** Add VPIN accumulation in `Scanner._process_trade()` BEFORE the skip-tiny-trades check. This way all scanned trades feed VPIN, and the scanner owns the VPINTracker instance (clean ownership).

**Option B:** Add it in `BatchWriter._write_batch()`. This feeds ALL trades including tiny ones. But it requires passing the VPINTracker through the writer, mixing concerns.

**Go with Option A.** Move the VPIN accumulation to the very top of `_process_trade()`, before the `min_trade_size_usd` check. This ensures VPIN buckets accumulate from all non-tiny trades.

Actually, even better: accumulate ALL trades into VPIN including tiny ones. VPIN is a volume metric — every trade contributes to the bucket. Move the accumulation before the size check:

```python
async def _process_trade(self, trade: Trade) -> None:
    self._trades_scanned += 1

    # Accumulate into VPIN buckets (ALL trades, including small ones)
    try:
        self._vpin_tracker.add_trade(
            trade.market_id, trade.side, float(trade.size_usd), trade.timestamp
        )
    except Exception as exc:
        logger.debug("VPIN accumulation failed", market=trade.market_id, error=str(exc))

    # Skip tiny trades (for scoring, not for VPIN)
    if float(trade.size_usd) < settings.min_trade_size_usd:
        return

    # ... rest of existing code ...
```

### Step 7: Configuration — `sentinel/config.py`

Add after the existing scanner thresholds:

```python
# VPIN parameters (Plan B Phase 1)
vpin_bucket_divisor: int = 50          # avg_daily_volume / divisor = bucket size
vpin_min_bucket_size: float = 100.0    # Minimum bucket size in USD
vpin_lookback_buckets: int = 50        # Number of trailing buckets for VPIN
vpin_min_buckets: int = 5              # Minimum completed buckets before reporting VPIN

# Kyle's Lambda parameters (Plan B Phase 1)
lambda_min_observations: int = 6       # Minimum 5-min windows for OLS (30 min of data)
lambda_window_minutes: int = 60        # Rolling window for Lambda estimation
```

### Step 8: Tests

**New file: `tests/scanner/test_vpin.py`**

```python
"""Tests for VPIN tracker."""
import duckdb
import pytest
from datetime import datetime, UTC, timedelta
from sentinel.scanner.vpin import VPINTracker

@pytest.fixture
def conn():
    """In-memory DuckDB with required schema."""
    c = duckdb.connect(":memory:")
    # Create minimal schema needed for VPIN
    c.execute("""
        CREATE TABLE trades (
            trade_id VARCHAR PRIMARY KEY, market_id VARCHAR, asset_id VARCHAR,
            wallet VARCHAR, side VARCHAR, price DECIMAL(10,6), size_usd DECIMAL(18,6),
            timestamp TIMESTAMP, tx_hash VARCHAR, source VARCHAR DEFAULT 'rest',
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""CREATE OR REPLACE VIEW v_deduped_trades AS SELECT * FROM trades""")
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
    def test_bucket_fills_and_writes(self, conn):
        """When accumulated volume exceeds bucket_size, a bucket is written."""
        tracker = VPINTracker(conn, default_bucket_size=100.0)
        now = datetime.now(tz=UTC)

        # Add trades that should fill one bucket
        tracker.add_trade("m1", "BUY", 60.0, now)
        tracker.add_trade("m1", "SELL", 50.0, now + timedelta(seconds=1))

        # Bucket should have been written (60 + 50 = 110 > 100)
        rows = conn.execute("SELECT * FROM vpin_buckets WHERE market_id = 'm1'").fetchall()
        assert len(rows) == 1
        assert rows[0][3] == 60.0  # buy_vol
        assert rows[0][4] == 50.0  # sell_vol

    def test_vpin_returns_none_insufficient_buckets(self, conn):
        """VPIN returns None when fewer than min_buckets completed."""
        tracker = VPINTracker(conn, default_bucket_size=1000.0)
        assert tracker.get_vpin("m1") is None

    def test_vpin_computation(self, conn):
        """VPIN = mean |buy - sell| / total across buckets."""
        tracker = VPINTracker(conn, default_bucket_size=100.0, min_buckets=2)
        now = datetime.now(tz=UTC)

        # Bucket 1: all buys → |100-0|/100 = 1.0
        tracker.add_trade("m1", "BUY", 110.0, now)

        # Bucket 2: balanced → |50-50|/100 = 0.0
        tracker.add_trade("m1", "BUY", 50.0, now + timedelta(seconds=1))
        tracker.add_trade("m1", "SELL", 60.0, now + timedelta(seconds=2))

        vpin = tracker.get_vpin("m1")
        assert vpin is not None
        assert 0.4 < vpin < 0.6  # Average of 1.0 and ~0.0

    def test_overflow_splits_across_buckets(self, conn):
        """A trade larger than bucket_size fills multiple buckets."""
        tracker = VPINTracker(conn, default_bucket_size=100.0)
        now = datetime.now(tz=UTC)

        tracker.add_trade("m1", "BUY", 250.0, now)

        rows = conn.execute(
            "SELECT * FROM vpin_buckets WHERE market_id = 'm1' ORDER BY bucket_idx"
        ).fetchall()
        assert len(rows) == 2  # 250 fills 2 full buckets, 50 left over
```

**New file: `tests/scanner/test_kyle_lambda.py`**

```python
"""Tests for Kyle's Lambda estimation."""
import duckdb
import pytest
from datetime import datetime, UTC, timedelta
from sentinel.scanner.kyle_lambda import estimate_lambda

@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    c.execute("""
        CREATE TABLE trades (
            trade_id VARCHAR PRIMARY KEY, market_id VARCHAR, asset_id VARCHAR,
            wallet VARCHAR, side VARCHAR, price DECIMAL(10,6), size_usd DECIMAL(18,6),
            timestamp TIMESTAMP, tx_hash VARCHAR, source VARCHAR DEFAULT 'rest',
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""CREATE OR REPLACE VIEW v_deduped_trades AS SELECT * FROM trades""")
    c.execute("""
        CREATE TABLE market_lambda (
            market_id VARCHAR NOT NULL, estimated_at TIMESTAMP NOT NULL,
            lambda DECIMAL(12,8), r_squared DECIMAL(8,6),
            residual_std DECIMAL(12,8), n_obs INTEGER,
            PRIMARY KEY (market_id, estimated_at)
        )
    """)
    return c


class TestEstimateLambda:
    def test_returns_none_insufficient_data(self, conn):
        """Lambda returns None when fewer than 6 five-minute windows."""
        result = estimate_lambda(conn, "m1")
        assert result is None

    def test_lambda_positive_with_correlated_data(self, conn):
        """When buys push price up, lambda should be positive."""
        now = datetime.now(tz=UTC)
        # Insert trades across 8 five-minute windows with correlated price/volume
        for i in range(8):
            ts = now - timedelta(minutes=55) + timedelta(minutes=i * 5)
            price = 0.50 + i * 0.01  # Price goes up
            # Two trades per window: one buy, timestamps slightly apart
            conn.execute(
                "INSERT INTO trades VALUES (?, 'm1', 'a1', 'w1', 'BUY', ?, ?, ?, NULL, 'rest', CURRENT_TIMESTAMP)",
                [f"t{i}a", price, 500.0, ts]
            )
            conn.execute(
                "INSERT INTO trades VALUES (?, 'm1', 'a1', 'w2', 'BUY', ?, ?, ?, NULL, 'rest', CURRENT_TIMESTAMP)",
                [f"t{i}b", price + 0.005, 300.0, ts + timedelta(seconds=30)]
            )

        result = estimate_lambda(conn, "m1")
        assert result is not None
        lambda_val, r_sq, res_std = result
        assert lambda_val > 0  # Buys → price up → positive lambda
```

**Update existing tests (exact locations):**

1. **`tests/scanner/test_scorer.py` line 272:** change `assert len(t) == 25` to `assert len(t) == 27`

2. **`tests/api/conftest.py` line 92-96:** The INSERT has 25 `?` placeholders:
   ```sql
   INSERT INTO signals VALUES
       (?, ?, ?, ?, 'BUY', 0.65, 5000.0, ?,
        3.5, 2.1, 0.03, 0.72, 15, FALSE, FALSE, NULL, ?, ?, ?, ?, 0, FALSE, 2, 0, ?)
   ```
   Add `NULL, NULL` after `0,` (position_trade_count) and before `?)` (created_at):
   ```sql
        ..., 2, 0, NULL, NULL, ?)
   ```

3. **`tests/test_integration.py` line 85-89:** Same pattern — 25 `?` placeholders:
   ```python
   "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
   [..., 2, 0, now],
   ```
   Change to 27 placeholders and add `None, None` before `now`:
   ```python
   "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
   [..., 2, 0, None, None, now],
   ```

4. **`tests/test_regression.py` line 313-315:** Change both the docstring and assertion:
   ```python
   """The tuple must match the INSERT column count (27)."""
   ...
   assert len(sig.as_db_tuple()) == 27
   ```

### Step 9: Lambda Estimation Frequency Optimization

Computing Lambda for every single trade is wasteful — Lambda changes slowly (it's a 1-hour rolling window). Cache the result:

In `kyle_lambda.py`:
```python
_lambda_cache: dict[str, tuple[float, float, float, datetime]] = {}
_CACHE_TTL_SECONDS = 300  # 5 minutes

def get_cached_lambda(conn, market_id: str) -> tuple[float, float, float] | None:
    """Return cached Lambda estimate, recomputing if stale."""
    now = datetime.now(tz=UTC)
    if market_id in _lambda_cache:
        val, r2, std, ts = _lambda_cache[market_id]
        if (now - ts).total_seconds() < _CACHE_TTL_SECONDS:
            return (val, r2, std)

    result = estimate_lambda(conn, market_id)
    if result is not None:
        _lambda_cache[market_id] = (*result, now)
        store_lambda(conn, market_id, *result, 0)
    return result
```

Use `get_cached_lambda()` in the pipeline instead of `estimate_lambda()` directly.

---

## Constraints & Pitfalls

### DuckDB Single-Writer
The entire pipeline runs in one async process with one DuckDB connection. VPIN bucket writes happen synchronously on the same connection — no contention issues. Do NOT create a separate connection or thread.

### No New Dependencies
VPIN and Lambda must be implemented with stdlib + DuckDB SQL only. No numpy, scipy, pandas, or scikit-learn in Phase 1. DuckDB's `REGR_SLOPE`, `REGR_R2`, `REGR_INTERCEPT` handle the OLS regression.

### Scoring Version
The `SCORING_VERSION` constant stays at `2`. VPIN and Lambda don't change the scoring formula — they're stored alongside existing scores for data collection. Bump to `3` only in Phase 3 when the Isolation Forest replaces the additive scorer.

### Column Order Matters
`Signal.as_db_tuple()` must match `_INSERT_SIGNAL_SQL` column order exactly. Both must match the signals table schema. The new columns go after `position_trade_count` and before `created_at`.

### Test Fixture Updates
Every test that INSERTs into the signals table or creates Signal objects must be updated for the new 27-column layout. Search for:
- `as_db_tuple` assertions
- Direct `INSERT INTO signals` in test fixtures
- `Signal(` constructor calls (the new fields have defaults, so these should be fine)

### Mypy Strict Mode
All new code must pass `mypy --strict`. Type annotate everything. Use `float | None` for optional metrics.

### Ruff Linting
Line length 100 chars. Import sorting. No unused imports. Run `ruff check` and `ruff format` before committing.

---

## Verification Checklist

After implementation, verify each of these:

1. **`make lint`** passes with no errors
2. **`make typecheck`** passes with no errors
3. **`make test`** passes with all existing + new tests green
4. **VPIN bucket accumulation:** Insert test trades, verify buckets appear in `vpin_buckets` table
5. **VPIN computation:** With enough buckets, `get_vpin()` returns a float in [0, 1]
6. **VPIN percentile:** Returns None when insufficient history, float [0, 1] otherwise
7. **Lambda estimation:** Returns None with < 6 windows, positive lambda with correlated data
8. **Lambda caching:** Second call within 5 min returns cached value (no DB query)
9. **Signal columns:** New signals have `vpin_percentile` and `lambda_residual` populated (or NULL)
10. **Signal tuple length:** `Signal.as_db_tuple()` returns 27 elements
11. **No scoring changes:** `compute_statistical_score()` output is identical to before
12. **Migration idempotent:** Running `init_schema()` twice doesn't error
13. **Backward compatible:** Old signals with NULL vpin/lambda columns still query correctly

---

## File Checklist

| Action | File | What Changes |
|--------|------|-------------|
| CREATE | `sentinel/scanner/vpin.py` | VPINTracker class |
| CREATE | `sentinel/scanner/kyle_lambda.py` | estimate_lambda, store_lambda, get_cached_lambda |
| CREATE | `tests/scanner/test_vpin.py` | VPINTracker unit tests |
| CREATE | `tests/scanner/test_kyle_lambda.py` | Lambda estimation tests |
| MODIFY | `sentinel/db/init.py` | Add vpin_buckets + market_lambda tables, v013 + v014 migrations |
| MODIFY | `sentinel/scanner/scorer.py` | Add vpin_percentile + lambda_residual to Signal, as_db_tuple, INSERT SQL, build_signal |
| MODIFY | `sentinel/scanner/pipeline.py` | Add VPINTracker init, VPIN accumulation, VPIN/Lambda lookups |
| MODIFY | `sentinel/config.py` | Add VPIN/Lambda config params |
| MODIFY | `tests/scanner/test_scorer.py` | Tuple length 25 → 27 |
| MODIFY | `tests/api/conftest.py` | Add NULL columns to signal INSERTs |
| MODIFY | `tests/test_integration.py` | Add NULL columns to signal INSERTs |
| MODIFY | `tests/test_regression.py` | Tuple length 25 → 27 |

---

## What NOT to Do

- Do NOT modify `compute_statistical_score()` — scoring changes come in Phase 3
- Do NOT add scikit-learn or any ML dependencies — that's Phase 2
- Do NOT remove or rename any existing columns, tables, or views
- Do NOT change the `SCORING_VERSION` constant
- Do NOT add VPIN/Lambda to the pre-check gate in the pipeline (the gate filters trades before scoring — VPIN/Lambda are informational only in Phase 1)
- Do NOT create separate threads or connections for VPIN/Lambda computation
- Do NOT add API endpoints for VPIN/Lambda yet — that's Phase 4

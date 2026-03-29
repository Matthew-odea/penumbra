# Plan B: VPIN + Kyle's Lambda + Isolation Forest

> Replace the ad-hoc additive scorer with empirically grounded market microstructure metrics and a learned anomaly detector.

## Problem

The current `compute_statistical_score` is a hand-tuned point system (40/20/20/20 weights) with multiplicative boosters (1.4x, 1.2x, 1.3x) that stack on every liquid market. Result: 98% of classified signals score 80+. The system doesn't discriminate — it's a noisy volume filter with score inflation.

Root causes:
1. **Volume Z-score** is a market-level bucket metric applied to individual trades, with arbitrary thresholds
2. **Price impact** uses a static formula that doesn't adapt to market conditions
3. **Feature combination** is linear+multiplicative — can't capture that "new wallet + high concentration + contrarian + near resolution" is far more suspicious than the sum of parts
4. **Thresholds are hand-tuned** with no empirical basis

## Solution: Three Replacements

### 1. VPIN replaces Volume Z-Score

**What:** Volume-Synchronized Probability of Informed Trading (Easley, Lopez de Prado, O'Hara 2012). Measures order flow imbalance across volume-synchronized buckets.

**Formula:**
```
VPIN = (1/N) * SUM_n |V_buy(n) - V_sell(n)| / V_bucket
```

**Why it's better than Z-scores:**
- Volume-synchronization gives more resolution during intense activity (when informed traders are active)
- Self-calibrating — no hand-tuned thresholds
- Single metric replaces both volume anomaly AND OFI (they're unified in VPIN)
- Proven in production at major exchanges

**Implementation:**
- Compute average daily volume per market, set `bucket_size = avg_daily / 50`
- As trades arrive, accumulate into current bucket
- When bucket fills, classify using Polymarket's native aggressor flag (`side` field) — NOT BVC
- Compute VPIN over trailing 50 buckets
- Store per-market VPIN time series in DuckDB
- A trade's "VPIN signal" = current VPIN percentile vs market's 7-day VPIN distribution

**Key decision:** Use Polymarket's `side` field directly for buy/sell classification. The main criticism of VPIN (Andersen & Bondarenko 2014) targets the BVC classification method, which we bypass entirely.

**Files:**
- New: `sentinel/scanner/vpin.py` — VPIN computation, bucket management, per-market tracking
- New: DuckDB table `vpin_buckets` (market_id, bucket_start, buy_vol, sell_vol, vpin)
- Modified: `sentinel/scanner/pipeline.py` — replace Z-score + OFI lookup with VPIN query
- Modified: `sentinel/ingester/writer.py` — feed trades into VPIN bucket accumulator

### 2. Kyle's Lambda replaces Price Impact

**What:** Rolling regression of price changes on signed net order flow. Lambda = the slope coefficient, representing how much each dollar of net buying moves the price.

**Formula:**
```
delta_P(t) = lambda * SignedVolume(t) + epsilon
```

Estimated via OLS on rolling 1-hour windows per market.

**Why it's better than static price impact:**
- Empirically estimated from the market's own data, not a formula with fallback constants
- Adapts to changing liquidity conditions
- A trade's anomaly = its actual impact vs expected impact from lambda
- Lambda itself is informative: a spike in lambda means the market is pricing in more adverse selection

**Implementation:**
- Every 5 minutes per market: regress 5-min price changes on 5-min signed volume over the last 1 hour (12 data points)
- Store lambda + R-squared per market per estimation window
- A trade's "impact anomaly" = |actual_impact - lambda * signed_volume| / std(residuals)
- High residual = trade moved price more than expected given current lambda

**Files:**
- New: `sentinel/scanner/kyle_lambda.py` — rolling OLS, lambda estimation, residual computation
- New: DuckDB table or view `market_lambda` (market_id, estimated_at, lambda, r_squared, residual_std)
- Modified: `sentinel/scanner/pipeline.py` — replace `get_price_impact` with lambda residual

### 3. Isolation Forest replaces compute_statistical_score

**What:** Ensemble of random binary trees that isolate anomalous points with fewer splits. Points with short average path lengths are anomalies.

**Why it's better than additive scoring:**
- Learns feature interactions from data (new wallet + high concentration + contrarian = super suspicious, even if each alone is weak)
- No hand-tuned weights — the forest discovers which features and combinations matter
- Anomaly score is continuous [0, 1], naturally calibrated by the data distribution
- Interpretable via SHAP values (explain WHY a trade was flagged)
- Handles the cold start: trains on "normal" trades without needing labeled insider trading examples

**Feature vector per trade (13 features):**
```python
[
    vpin_percentile,           # VPIN: replaces volume Z-score + OFI
    lambda_residual,           # Kyle: replaces price impact
    log_size_usd,              # Trade size (log-scaled)
    wallet_win_rate,           # From v_wallet_performance (or 0.5 if unknown)
    wallet_resolved_trades,    # Raw count (0 = truly new)
    market_concentration,      # Fraction of wallet's recent trades on this market
    funding_age_hours,         # Wallet age (continuous, not binary)
    hours_to_resolution,       # Time to market end_date
    position_trade_count,      # Accumulation: wallet's trade count on this market+side
    is_contrarian,             # 1 if trade opposes majority flow, 0 otherwise
    spread_change_pct,         # Liquidity cliff: spread widening in last 10 min
    n_other_wallets_same_side, # Coordination (excluding self)
    log_market_volume_24h,     # Market activity level (normalizes for liquid vs illiquid)
]
```

**Training:**
- Initial training: use all trades from the last 7 days (or since pipeline start)
- Retrain every 24 hours on a rolling 7-day window
- Store the model as a pickle in the data directory (alongside DuckDB)
- `contamination` parameter: 0.02-0.05 (expect 2-5% of trades to be anomalous)

**Scoring:**
- `model.decision_function(X)` returns a continuous anomaly score
- Map to [0, 100] for backward compatibility with the existing signal/reasoning pipeline
- SHAP values explain which features contributed to the score

**Files:**
- New: `sentinel/scanner/anomaly_model.py` — IsolationForest training, scoring, SHAP explanation
- New: `sentinel/scanner/features.py` — feature extraction pipeline (builds the 13-feature vector from DuckDB queries + VPIN + lambda)
- Modified: `sentinel/scanner/pipeline.py` — replace `build_signal` → `compute_statistical_score` with feature extraction → model scoring
- Modified: `sentinel/scanner/scorer.py` — keep `Signal` dataclass but change how `statistical_score` is computed
- New dependency: `scikit-learn` (IsolationForest), `shap` (explanations, optional)

## Migration Strategy

### Phase 1: Add VPIN + Lambda as features (non-breaking)
- Implement VPIN and Lambda as new scanner modules
- Compute them alongside the existing scorer
- Store both VPIN and Lambda on each signal (new columns)
- The existing `compute_statistical_score` still drives signal emission
- **Goal:** Collect data, validate VPIN/Lambda correlate with outcomes better than Z-score/price_impact

### Phase 2: Train Isolation Forest on enriched features
- Once Phase 1 has 7+ days of data with VPIN + Lambda populated
- Extract feature vectors for all recent trades
- Train IsolationForest, evaluate on held-out data
- Compare anomaly score vs existing `statistical_score` on resolved markets using `v_signal_outcomes`
- **Goal:** Demonstrate the model outperforms the hand-tuned scorer

### Phase 3: Switch scoring to Isolation Forest
- Replace `compute_statistical_score` with model-based scoring
- Keep the 0-100 score range for backward compatibility
- Add SHAP explanations to signal metadata
- Remove the old additive scorer
- **Goal:** Production deployment of the learned model

### Phase 4: Semi-supervised refinement
- Use outcome-based labels from `v_signal_outcomes` (trades that were large + correct + near resolution = "probably informed")
- Train a supervised classifier (XGBoost/LightGBM) on the pseudo-labels
- Use Isolation Forest as fallback for markets without enough history
- **Goal:** Calibrated probability of informed trading

## Schema Changes

```sql
-- VPIN bucket storage
CREATE TABLE IF NOT EXISTS vpin_buckets (
    market_id   VARCHAR NOT NULL,
    bucket_idx  INTEGER NOT NULL,
    bucket_end  TIMESTAMP NOT NULL,
    buy_vol     DECIMAL(18, 6),
    sell_vol    DECIMAL(18, 6),
    vpin        DECIMAL(8, 6),
    PRIMARY KEY (market_id, bucket_idx)
);

-- Kyle's Lambda estimates
CREATE TABLE IF NOT EXISTS market_lambda (
    market_id    VARCHAR NOT NULL,
    estimated_at TIMESTAMP NOT NULL,
    lambda       DECIMAL(12, 8),
    r_squared    DECIMAL(8, 6),
    residual_std DECIMAL(12, 8),
    n_obs        INTEGER,
    PRIMARY KEY (market_id, estimated_at)
);

-- New columns on signals
ALTER TABLE signals ADD COLUMN vpin_percentile DECIMAL(5, 4);
ALTER TABLE signals ADD COLUMN lambda_residual DECIMAL(10, 6);
ALTER TABLE signals ADD COLUMN anomaly_score DECIMAL(5, 4);  -- model output [0, 1]
ALTER TABLE signals ADD COLUMN shap_top_features VARCHAR;     -- JSON: top 3 SHAP contributors
```

## New Dependencies

```
scikit-learn>=1.4
shap>=0.44        # optional, for explanations
```

## Verification

1. **VPIN sanity check:** On a market with known high activity, VPIN should be higher than on a quiet market
2. **Lambda sanity check:** Lambda should be higher on illiquid markets than liquid ones
3. **Model evaluation:** Compare Isolation Forest anomaly scores vs `statistical_score` on `v_signal_outcomes` — the model should have higher precision at the same recall
4. **A/B comparison:** Run both scorers in parallel for 1 week, compare signal quality
5. **SHAP validation:** Top SHAP features for high-scoring signals should make intuitive sense

## Estimated Effort

| Phase | Effort | Can run in parallel with production |
|-------|--------|-------------------------------------|
| Phase 1: VPIN + Lambda | 3-4 days | Yes (additive, non-breaking) |
| Phase 2: Train IF | 1-2 days | Yes (offline training) |
| Phase 3: Switch scorer | 1-2 days | Deployment required |
| Phase 4: Semi-supervised | 2-3 days | Yes (offline, needs resolved data) |

## References

- Easley, Lopez de Prado, O'Hara (2012). "Flow Toxicity and Liquidity in a High-frequency World"
- Kyle (1985). "Continuous Auctions and Insider Trading"
- Andersen & Bondarenko (2014). "Reflecting on the VPIN Dispute" — key criticism we address by using native aggressor flags
- Liu et al. (2012). "Isolation-Based Anomaly Detection"
- Becker (2024). "Microstructure of Wealth Transfer in Prediction Markets"

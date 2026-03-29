# Penumbra Scoring Roadmap

> From hand-tuned heuristics to calibrated informed trading probability.

## Current State

The pipeline extracts good features per trade (volume Z-score, OFI, price impact, wallet reputation, funding age, VPIN, Kyle's Lambda, concentration, coordination, accumulation). The problem is how those features are combined into a score.

**What exists:**
- `compute_statistical_score()` — additive point system (40/20/20/20 weights) with multiplicative boosters (1.4x, 1.2x, 1.3x). Hand-tuned, no empirical basis.
- VPIN + Kyle's Lambda — computing and stored on signals as passthrough columns, not used in scoring.
- `v_signal_outcomes` — view joining signals to resolved markets for accuracy measurement. Currently returns zero rows (see Blocker below).
- 23,616 signals, ~4,000 markets, 0 resolved outcomes.

**Why the current scorer is wrong:**
1. Additive scores assume feature independence. They're not independent — a new wallet + high concentration + contrarian + near resolution is exponentially more suspicious than the sum of parts. Interaction effects are where the signal lives.
2. Fixed global weights don't adapt to market type, liquidity regime, or time horizon.
3. The multiplicative boosters are ad-hoc nonlinearity — a crude attempt to model interactions through a back door.
4. Score has no probabilistic meaning. A score of 60 is uninterpretable.

**What the literature uses:** VPIN outputs P(informed). Kyle's Lambda gives $/$ price impact. PIN models estimate information event probability via MLE. Prop desks use learned models (logistic regression, GBM) that output calibrated probabilities with feature interactions learned from data.

## Blocker: No Outcome Labels

`resolved_price` is never populated by the market sync (`sentinel/ingester/markets.py`). The Polymarket API returns `closed=true` but we don't extract the resolution price. The `v_signal_outcomes` view requires `resolved_price IS NOT NULL`, so it returns zero rows.

**Fix:** During market sync, when `closed=true`, extract the resolution price from the Polymarket API response (the `tokens[].outcome` or final `last_price` field) and write it to `markets.resolved_price`. This unblocks all accuracy measurement and supervised training.

This is the single highest-priority item. Everything below depends on it.

---

## Phase 1: Fix Outcome Pipeline

**Goal:** Populate `resolved_price` so `v_signal_outcomes` produces labeled data.

**Work:**
- Update `sentinel/ingester/markets.py` `_build_csv_rows()` to extract resolution price from the API response for closed markets
- Backfill: one-time script to query the Polymarket API for all markets where `resolved=true AND resolved_price IS NULL` and update them
- Verify `v_signal_outcomes` returns rows after fix
- Verify `/api/metrics/accuracy` and `/api/metrics/accuracy/summary` populate

**Validates:** The outcome labeling is correct — `trade_correct` (BUY on YES outcome, SELL on NO outcome) matches reality.

**Estimated effort:** Half a day.

## Phase 2: Feature Correlation Analysis

**Goal:** Understand which features actually predict outcomes before building a model.

**Requires:** Phase 1 complete + ~200 resolved signals (may take 1-2 weeks of pipeline runtime).

**Work:**
- Script that queries `v_signal_outcomes` joined back to signal component values
- Per-feature correlation with `trade_correct`: Pearson r, point-biserial for binary features
- Per-feature precision at various thresholds (e.g. "if funding_age < 60min, what fraction of those trades were correct?")
- Feature interaction analysis: 2-way cross-tabs for top pairs

**Output:** A table like:
```
Feature                     Correlation   Notes
modified_z_score            r=0.12        weak — volume spikes are noisy
wallet_win_rate             r=0.31        moderate — best single predictor?
market_concentration        r=0.22        weak-moderate
funding_age_minutes         r=-0.18       weak negative (younger = more correct)
vpin_percentile             r=???         need data
lambda_value                r=???         need data
hours_to_resolution         r=-0.15       negative — closer = more predictive
coordination_wallet_count   r=0.03        negligible — mostly false positives
```

This tells you which features to keep, which to drop, and whether you have enough signal for a learned model to outperform a simple threshold on the best 2-3 features.

**Estimated effort:** 1 day for the script, then waiting for data.

## Phase 3: Unsupervised Anomaly Detection (Isolation Forest)

**Goal:** Replace the additive scorer with a model that captures feature interactions, without needing labeled data.

**Requires:** VPIN + Lambda collecting for 7+ days (in progress now).

**Architecture:**
```
features → IsolationForest.decision_function(X) → anomaly_score [0, 1] → threshold → emit
```

**Feature vector (13 dimensions):**
```python
[
    vpin_percentile,           # Flow toxicity (replaces volume Z + OFI)
    lambda_residual,           # Actual impact vs expected (replaces price impact)
    log_size_usd,              # Trade size
    wallet_win_rate,           # Historical accuracy (0.5 if unknown)
    wallet_resolved_trades,    # Sample size (0 = new wallet)
    market_concentration,      # Single-market focus
    funding_age_hours,         # Wallet age (continuous)
    hours_to_resolution,       # Time to market close
    position_trade_count,      # Accumulation pattern
    is_contrarian,             # Trade opposes majority flow
    spread_change_pct,         # Liquidity withdrawal
    coordination_wallet_count, # Multi-wallet same-side activity
    log_market_volume_24h,     # Market activity (normalizer)
]
```

**Implementation:**
- New: `sentinel/scanner/features.py` — extracts the 13-feature vector from pipeline data
- New: `sentinel/scanner/anomaly_model.py` — wraps sklearn IsolationForest. Train on rolling 7-day window, retrain daily.
- Model stored as pickle at `data/anomaly_model.pkl`, loaded at pipeline startup
- `contamination=0.03` (expect ~3% anomalous trades)
- Modified: `sentinel/scanner/pipeline.py` — after feature extraction, call `model.score(features)` instead of `compute_statistical_score()`

**Cold start:** Until the model has 7 days of training data, fall back to a simple rule: emit signals where `vpin_percentile > 0.85 OR (funding_age < 60min AND size_usd > 5000)`. This is crude but better than the current 4-component additive formula for the same reason — it uses the features that are most likely to be predictive.

**Signal table change:**
```sql
ALTER TABLE signals ADD COLUMN anomaly_score DECIMAL(5, 4);  -- [0, 1]
```

Keep `statistical_score` populated (as `int(anomaly_score * 100)`) for backward compatibility with the dashboard and existing queries.

**Dependency:** `scikit-learn>=1.4` (IsolationForest). No GPU needed. Inference is <1ms per trade.

**Estimated effort:** 2-3 days.

## Phase 4: Supervised Model

**Goal:** Train a model on actual outcomes to output calibrated P(informed).

**Requires:** Phase 1 + 500+ resolved signals with outcome labels.

**Architecture:**
```
features → GBM.predict_proba(X) → P(informed) [0, 1] → threshold → emit
```

**Why GBM over logistic regression:** Gradient-boosted trees (XGBoost/LightGBM) automatically learn feature interactions, handle missing values (VPIN/Lambda may be NULL for some signals), and are the standard at quant desks for tabular prediction tasks. Logistic regression is a reasonable baseline but can't capture the interaction effects that are the whole point of moving away from the additive scorer.

**Training pipeline:**
1. Query `v_signal_outcomes` for all resolved signals with features
2. Label: `trade_correct` (binary)
3. Train/test split: time-based (train on older, test on newer) to avoid leakage
4. Train LightGBM with Platt scaling for probability calibration
5. Evaluate: precision, recall, F1, calibration curve (predicted P vs actual fraction correct)
6. Store model as pickle, retrain weekly as new markets resolve

**Shadow mode:** Run the supervised model in parallel with the Isolation Forest for 1 week. Log both scores on each signal. Compare precision@recall curves. Switch when the supervised model dominates.

**Feature importance:** LightGBM gives native feature importance. Drop features with zero importance (likely candidates: coordination_wallet_count, which fires on every liquid market).

**New dependency:** `lightgbm>=4.0` (or `xgboost>=2.0`).

**Estimated effort:** 2-3 days for initial model, then ongoing retraining.

## Phase 5: Wallet Graph Intelligence

**Goal:** Detect coordinated informed trading by resolving wallet identity. A single actor using 10 wallets to split a $50K position is invisible at the trade level.

**Requires:** Phase 3 or 4 running (the per-trade model provides the base; this adds cluster-level features on top).

### 5a: Funding Graph

Crawl Polygon MATIC/USDC transfers via Alchemy `getAssetTransfers` (already integrated for funding checks). Build a directed graph of funder -> funded relationships. Cluster wallets that share a common funding ancestor within 3 hops using connected components.

```sql
CREATE TABLE wallet_graph (
    source_wallet   VARCHAR NOT NULL,
    dest_wallet     VARCHAR NOT NULL,
    transfer_amount DECIMAL(18, 6),
    transfer_time   TIMESTAMP,
    tx_hash         VARCHAR,
    hop_distance    INTEGER,
    PRIMARY KEY (source_wallet, dest_wallet, tx_hash)
);

CREATE TABLE wallet_clusters (
    wallet       VARCHAR PRIMARY KEY,
    cluster_id   VARCHAR NOT NULL,
    cluster_size INTEGER,
    root_funder  VARCHAR,
    last_updated TIMESTAMP
);
```

- New: `sentinel/scanner/wallet_graph.py` — graph builder, Alchemy crawler, clustering
- Background task: crawl funding history for wallets seen in trades (rate-limited)
- Budget: Alchemy free tier (300 CU/s) is sufficient

### 5b: Cluster Position Tracking

Aggregate trades across all wallets in the same cluster to compute the actor's true position:

```sql
CREATE OR REPLACE VIEW v_cluster_positions AS
SELECT
    wc.cluster_id,
    wc.cluster_size,
    t.market_id,
    t.side,
    COUNT(DISTINCT t.wallet) AS wallets_used,
    COUNT(*) AS total_trades,
    SUM(t.size_usd) AS total_volume_usd,
    MIN(t.timestamp) AS first_trade,
    MAX(t.timestamp) AS last_trade
FROM v_deduped_trades t
JOIN wallet_clusters wc ON t.wallet = wc.wallet
WHERE t.timestamp >= CURRENT_TIMESTAMP - INTERVAL '7 days'
GROUP BY wc.cluster_id, wc.cluster_size, t.market_id, t.side
HAVING COUNT(*) >= 3;
```

**6 additional features added to the model's feature vector:**
```python
cluster_size,              # How many wallets this actor controls
cluster_wallets_active,    # How many are active on this market
cluster_total_volume_usd,  # True position size (invisible at wallet level)
cluster_accumulation_rate, # Trades per hour across the cluster
cluster_spread_ratio,      # wallets_active / cluster_size
behavioral_similarity_max, # Max similarity to another wallet on this market
```

### 5c: Behavioral Fingerprinting

Identify wallets likely controlled by the same actor via behavioral similarity (trade size patterns, time-of-day activity, gas price preferences), even without shared funding. Cosine similarity on per-wallet feature vectors, merged into clusters as a secondary signal.

### 5d: Dashboard

- New "Clusters" page: identified clusters, member wallets, positions, timeline
- API: `GET /api/clusters`, `GET /api/clusters/{id}`, `GET /api/wallets/{addr}/cluster`
- Add `cluster_id` to signal feed for grouping

**Estimated effort:** 7-11 days total across 5a-5d.

## Phase 6: Online Calibration + Threshold Optimization

**Goal:** Continuously calibrate the model and optimize the signal emission threshold.

**Requires:** Phase 4 running with accumulating resolved data.

**Work:**
- Rolling precision/recall curve on the last 30 days of resolved signals
- Automated threshold selection: pick the threshold that maximizes F1 (or a user-defined precision target, e.g. "I want 50% precision minimum")
- Calibration monitoring: does predicted P(informed) = 0.4 actually correspond to 40% correct? If not, recalibrate.
- Feature drift detection: alert if feature distributions shift significantly (new market type, liquidity regime change)

**This is the steady state.** Once Phase 6 is running, the system is self-improving: new resolved markets provide training data, the model retrains, thresholds adapt.

---

## Execution Order

```
Phase 1: Fix outcome pipeline               ← START HERE, unblocks everything
Phase 2: Feature correlation analysis        ← needs ~200 resolved signals
Phase 3: Isolation Forest                    ← needs 7d VPIN/Lambda data
Phase 4: Supervised model                    ← needs ~500 resolved signals
Phase 5: Wallet graph intelligence           ← independent, can overlap with 3-4
Phase 6: Online calibration                  ← continuous after Phase 4
```

Phases 3 and 5 can run in parallel. Phase 4 depends on having enough outcome data, which depends on Phase 1 + time.

## Architecture Target

```
                    Polymarket WS + REST
                           │
                     ┌─────▼──────┐
                     │  Ingester   │
                     │  + Market   │
                     │    Sync     │
                     └─────┬──────┘
                           │ trades
                     ┌─────▼──────┐
                     │  Feature    │
                     │  Extractor  │
                     │  (13-19     │
                     │  features)  │
                     └─────┬──────┘
                           │ feature vector
              ┌────────────┼────────────┐
              │            │            │
        ┌─────▼─────┐ ┌───▼────┐ ┌────▼─────┐
        │ Isolation  │ │  GBM   │ │ Cluster  │
        │  Forest    │ │ Model  │ │ Position │
        │ (unsup.)   │ │ (sup.) │ │ Tracker  │
        └─────┬─────┘ └───┬────┘ └────┬─────┘
              │            │           │
              └────────────┼───────────┘
                           │
                     ┌─────▼──────┐
                     │ P(informed)│
                     │ + threshold│
                     │ → Signal   │
                     └─────┬──────┘
                           │
                     ┌─────▼──────┐
                     │  DuckDB    │
                     │  + API     │
                     │  + Dashboard│
                     └────────────┘
```

The Isolation Forest runs from day 1 (unsupervised). The GBM takes over once there's enough labeled data. Cluster features feed into both models as additional dimensions.

## What Gets Deleted

When Phase 3 ships:
- `compute_statistical_score()` — replaced by model inference
- `scorer_weight_*` settings — weights are learned, not configured
- The volume Z-score as a scoring component (VPIN subsumes it)
- The static price impact formula (Kyle's Lambda subsumes it)
- The funding anomaly binary flag (replaced by continuous funding_age_hours feature)

The feature extraction code in `pipeline.py` stays — it feeds the model. The `Signal` dataclass stays — it stores the features and the model's output.

## References

- Easley, Lopez de Prado, O'Hara (2012). "Flow Toxicity and Liquidity in a High-frequency World"
- Kyle (1985). "Continuous Auctions and Insider Trading"
- Andersen & Bondarenko (2014). "Reflecting on the VPIN Dispute"
- Liu et al. (2012). "Isolation-Based Anomaly Detection"
- Easley, Kiefer, O'Hara (1997). "One Day in the Life of a Very Common Stock" (PIN model)
- Becker (2024). "Microstructure of Wealth Transfer in Prediction Markets"
- Nansen, Arkham Intelligence — wallet clustering methodology

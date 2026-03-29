# Plan C: Graph Intelligence + Wallet Clustering

> Detect coordinated informed trading by resolving wallet identity and scoring at the actor level, not the trade level.

## Problem

The current system scores individual trades in isolation. A single actor using 10 wallets to accumulate a $50K position across 100 small trades is invisible — no single trade triggers thresholds. This is the dominant pattern in real-world insider trading on prediction markets, where actors deliberately split positions across wallets and time to avoid detection.

The `coordination_wallet_count` signal (3+ wallets same side in 5 min) is a crude proxy that fires constantly on liquid markets (false positives) and misses slow accumulation across hours (false negatives).

## Prerequisites

This plan builds on Plan B (VPIN + Lambda + Isolation Forest). Plan B provides the per-trade feature pipeline and anomaly model; Plan C adds the identity resolution and cluster-level scoring layer on top.

## Solution: Three Components

### 1. Wallet Funding Graph

**What:** Build a directed graph of MATIC/USDC transfers on Polygon. Wallets that share a common funding source are likely controlled by the same actor.

**Why it works:** On-chain analysis firms (Nansen, Arkham, Chainalysis) use this as their primary identity resolution method. Polymarket wallets are funded via Polygon — every wallet has a traceable funding chain.

**Data source:** Alchemy `alchemy_getAssetTransfers` API (already integrated for funding anomaly checks). Expand to crawl the full funding tree, not just the first transfer.

**Graph structure:**
```
Nodes: wallet addresses
Edges: directed, from funder → funded
       weighted by transfer amount
       annotated with timestamp
```

**Clustering algorithm:** Connected components for basic clustering (wallets sharing any funding ancestor within 3 hops). Optionally Louvain community detection for larger clusters.

**Storage:**
```sql
CREATE TABLE wallet_graph (
    source_wallet   VARCHAR NOT NULL,
    dest_wallet     VARCHAR NOT NULL,
    transfer_amount DECIMAL(18, 6),
    transfer_time   TIMESTAMP,
    tx_hash         VARCHAR,
    hop_distance    INTEGER,  -- 1 = direct, 2 = via intermediary, etc.
    PRIMARY KEY (source_wallet, dest_wallet, tx_hash)
);

CREATE TABLE wallet_clusters (
    wallet      VARCHAR PRIMARY KEY,
    cluster_id  VARCHAR NOT NULL,       -- deterministic hash of root funder
    cluster_size INTEGER,               -- total wallets in cluster
    root_funder VARCHAR,                -- ultimate funding source
    last_updated TIMESTAMP
);
```

**Implementation:**
- New: `sentinel/scanner/wallet_graph.py` — graph builder, Alchemy crawler, clustering
- Background task: crawl funding history for wallets seen in trades (rate-limited, cached)
- Update clusters incrementally as new wallets appear
- Budget: Alchemy free tier (300 CU/s) is sufficient for background crawling

### 2. Cluster-Level Position Tracking

**What:** Aggregate trades across all wallets in the same cluster to compute the actor's true position.

**View:**
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
    MAX(t.timestamp) AS last_trade,
    EXTRACT(EPOCH FROM MAX(t.timestamp) - MIN(t.timestamp)) / 3600.0 AS span_hours
FROM v_deduped_trades t
JOIN wallet_clusters wc ON t.wallet = wc.wallet
WHERE t.timestamp >= CURRENT_TIMESTAMP - INTERVAL '7 days'
  AND t.wallet != ''
GROUP BY wc.cluster_id, wc.cluster_size, t.market_id, t.side
HAVING COUNT(*) >= 3;
```

**Scoring dimensions:**
- **Cluster spread:** `wallets_used / cluster_size` — how many wallets in the cluster are active on this market? High spread = deliberate distribution.
- **Accumulation rate:** `total_trades / span_hours` — how fast is the cluster building its position?
- **Total exposure:** `total_volume_usd` — the cluster's true position size (invisible at individual wallet level).
- **Temporal pattern:** Is the cluster trading in bursts (coordinated execution) or gradually (slow accumulation)?

### 3. Behavioral Fingerprinting

**What:** Identify wallets likely controlled by the same actor via behavioral similarity, even without shared funding.

**Features per wallet (computed weekly):**
- Gas price preferences (Polygon tx gas settings)
- Typical trade sizes (median, variance)
- Time-of-day activity distribution
- Market category preferences
- Response latency to price moves
- Round-number bias (do they trade in $100 increments?)

**Similarity:** Cosine similarity or learned embeddings via a small autoencoder. Wallets with similarity > 0.95 and no shared funding are flagged as "suspected same actor."

**Implementation:**
- New: `sentinel/scanner/wallet_fingerprint.py` — feature extraction, similarity computation
- New: DuckDB table `wallet_fingerprints` (wallet, feature_vector, updated_at)
- Merges into wallet_clusters as a secondary signal (lower confidence than funding graph)

## Architecture

```
                    ┌─────────────────────┐
                    │   Polygon / Alchemy  │
                    │   Funding Transfers   │
                    └──────────┬───────────┘
                               │ background crawl
                               ▼
┌───────────┐    ┌──────────────────────────┐
│  Trades   │───▶│   Wallet Graph Builder    │
│ (DuckDB)  │    │   - funding edges         │
└───────────┘    │   - connected components  │
                 │   - cluster assignment    │
                 └──────────┬───────────────┘
                            │
                            ▼
                 ┌──────────────────────────┐
                 │  Cluster Position Tracker │
                 │   - aggregate by cluster  │
                 │   - true position size    │
                 │   - accumulation pattern  │
                 └──────────┬───────────────┘
                            │
                            ▼
                 ┌──────────────────────────┐
                 │  Enriched Feature Vector  │
                 │   (Plan B features +      │
                 │    cluster features)       │
                 └──────────┬───────────────┘
                            │
                            ▼
                 ┌──────────────────────────┐
                 │  Isolation Forest / GBM   │
                 │   (anomaly scoring)       │
                 └──────────────────────────┘
```

## Additional Feature Vector (extends Plan B's 13 features)

```python
# Plan B features (13) + Plan C features (6) = 19 total
[
    # ... Plan B features ...
    cluster_size,              # How many wallets in this actor's cluster
    cluster_wallets_active,    # How many cluster wallets traded this market
    cluster_total_volume_usd,  # Cluster's true position size on this market
    cluster_accumulation_rate, # Trades per hour across cluster
    cluster_spread_ratio,      # wallets_active / cluster_size
    behavioral_similarity_max, # Max similarity to any other wallet trading same market
]
```

## Migration Strategy

### Phase 1: Funding graph (2-3 days)
- Implement Alchemy funding crawl for wallets seen in trades
- Build wallet_graph table
- Run connected components clustering
- Store results in wallet_clusters
- **Validation:** Check known Polymarket whale wallets — do they cluster correctly?

### Phase 2: Cluster positions (1-2 days)
- Create v_cluster_positions view
- Add cluster features to the feature vector
- Retrain Isolation Forest with the expanded feature set
- **Validation:** Do cluster-level features improve model performance on v_signal_outcomes?

### Phase 3: Behavioral fingerprinting (3-4 days)
- Implement per-wallet feature extraction
- Compute pairwise similarity
- Merge behavioral clusters with funding clusters
- **Validation:** Do behavioral clusters catch wallets that funding analysis misses?

### Phase 4: Dashboard integration (1-2 days)
- New "Clusters" page showing identified wallet clusters
- Cluster detail view: member wallets, positions, timeline
- Add cluster_id to signal feed for grouping

## API Surface

```
GET /api/clusters                           — top clusters by activity
GET /api/clusters/{cluster_id}              — cluster detail + member wallets
GET /api/clusters/{cluster_id}/positions    — cluster's positions across markets
GET /api/wallets/{address}/cluster          — which cluster does this wallet belong to?
```

## Constraints

- **Alchemy rate limits:** 300 CU/s on free tier. Crawling 10K wallets' funding history = ~10K API calls. At 300/s, this takes ~33 seconds. Manageable as a background task.
- **Graph size:** With 50K+ unique wallets, the graph could have 100K+ edges. Connected components is O(V+E) — fast. Louvain is O(V log V) — still fast.
- **Privacy:** Wallet clustering is standard on-chain analysis (Nansen, Arkham do it commercially). No off-chain PII involved.
- **False positives:** Exchange hot wallets and DEX routers fund many unrelated wallets. Need an exclusion list for known infrastructure addresses.

## Estimated Effort

| Phase | Effort | Dependencies |
|-------|--------|-------------|
| Phase 1: Funding graph | 2-3 days | Alchemy API (already integrated) |
| Phase 2: Cluster positions | 1-2 days | Phase 1 |
| Phase 3: Behavioral fingerprinting | 3-4 days | Phase 1 |
| Phase 4: Dashboard | 1-2 days | Phase 2 |
| **Total** | **7-11 days** | Plan B should be complete first |

## References

- Nansen wallet labeling methodology
- Arkham Intelligence de-anonymization approach
- Chainalysis transaction graph analysis
- "Sybil Detection via Subgraph Feature Propagation" (ArXiv 2025)
- "GNN for DeFi User Clustering" (ScienceDirect 2025)
- PolyTrack's USDC funding source tracing methodology

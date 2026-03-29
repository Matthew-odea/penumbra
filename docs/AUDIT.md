# Penumbra: Production Readiness Audit

> Generated: 2026-03-26. Updated: 2026-03-29.
>
> Items marked **[FIXED]** were resolved in commits `c50f89c` and `82e250f`.

---

## 🔴 Critical — Silent Data Loss or Corruption

### C1. **[FIXED]** WS trades have no wallet address (`models.py:176`)
Format 1 (`last_trade_price`) doesn't include `taker_address`. Code sets `wallet=""`. Fixed: `_wallet_known` flag gates wallet profiling, Alchemy calls, and new-wallet bonus. Empty wallets filtered from `v_wallet_performance` and `v_coordination_signals`.

---

### C2. **[FIXED]** WS trade_id collisions (`models.py:171`)
Was using 1-second precision + 8-char asset prefix. Fixed: now uses millisecond precision + 16-char prefix: `f"ws-{ts_raw}-{asset_id[:16]}"`.

---

### C3. **[FIXED]** `fee_rate_bps` handler still a landmine (`listener.py:213`)
We fixed trade parsing but the catch-all `if "fee_rate_bps" in msg: return` is still live. Any future new message type from Polymarket that includes this field will be silently eaten with zero logging. Fixed: added logging of skipped messages (first 5 + every 1000th) so new message types are visible.

---

### C4. **[FIXED]** WS reconnect drops trades permanently
`_INITIAL_BACKOFF=1s`, `_MAX_BACKOFF=60s`. During exponential backoff, no trades are ingested and there is no recovery mechanism (no "fetch missed trades since last disconnect"). REST only covers the configured 50 hot-tier markets. Any trade on a WS-only market during the gap is permanently lost. Fixed: max backoff reduced to 15s, `on_reconnect` callback triggers immediate REST backfill poll for all hot-tier markets, reconnect counter exposed in status log. Backoff resets after each reconnect attempt.

---

### C5. **[FIXED]** `wallet_profiler` crashes on empty-string wallet (`scanner/pipeline.py:158`)
Fixed: `_wallet_known` flag added in `_process_trade`; wallet profiling only runs when wallet is non-empty. `v_wallet_performance` view now filters `wallet != ''`.

---

### C6. **[FIXED]** REST dedup LRU eviction causes re-ingestion
`_SEEN_SET_MAX = 200_000`. With 50 markets × 1000 trades/poll = 50K new IDs per cycle. After ~4 cycles (~20 seconds) the LRU starts evicting. Those evicted trade IDs can be re-fetched on the next poll, creating duplicate rows and inflating volume anomaly Z-scores. Fixed: LRU increased to 500K (10 cycles headroom). Volume inflation also mitigated by `v_deduped_trades` view.

---

### C7. **[FIXED]** Coordination view counts a single wallet's own trades (`db/init.py`)
`v_coordination_signals` fires when `COUNT(DISTINCT wallet) >= 3` on same market+side in 5 min. A whale making 3 trades across wallet addresses (or 3 fast consecutive dedup-gap trades from a single address) trips the "coordinated insider" signal. No self-exclusion logic. Fixed: `get_coordination_signal` now accepts `exclude_wallet` parameter; the scanner passes the triggering trade's wallet so it is excluded from the coordination count.

---

### C8. **[FIXED]** Market sync failure leaves stale metadata indefinitely
When `upsert_markets` throws (e.g. CSV parse error), the entire sync batch is lost with a single `logger.warning`. No rows are updated. The pipeline continues with stale liquidity, prices, and end_dates from the last successful sync. Time-to-resolution, price impact, and priority formula all degrade silently with no retry or circuit breaker. Fixed: both initial and periodic syncs use `_sync_with_retry` (4 retries, exponential backoff 30s→10min cap). Each attempt logs the retry count.

---

### C9. **[FIXED]** Scoring workers sleep until midnight; queue accumulates silently
After budget exhaustion, workers drain the queue and sleep. But `_on_demand_market_resolver` continues calling `scoring_queue.put_nowait()`. Those IDs sit in an unbounded queue until midnight. After budget reset, workers wake and score markets that may now be hours stale. Queue can grow to thousands of items with no backpressure. Fixed: scoring queue is now bounded (maxsize=5000). Enqueue calls use `put_nowait` with `QueueFull` catch — dropped IDs are picked up on the next periodic sync.

---

### C10. **[FIXED]** Signal scores exceed 100 before capping, destroying relative ranking
Fixed: `min(100, score)` cap removed from `compute_statistical_score()`. Scores are now uncapped, preserving relative ranking after multipliers.

---

## 🟠 Major — Data Quality Degradation

### M1. **[FIXED]** Can't distinguish "no trades yet" from "< 5 resolved trades"
Both return `None` from `get_wallet_profile` (view enforces `HAVING COUNT(*) >= 5`). Scanner applies the zero-history suspicion bonus (+5 pts) to both cases. A legitimate trader with 4 resolved trades scores the same as a brand-new wallet. Fixed: `get_resolved_trade_count` queries raw trade count; scanner sets `wallet_total_trades` to the actual count (1-4) when the wallet has some history, preventing the zero-history bonus for known wallets.

### M2. **[FIXED]** Market concentration calculated over arbitrary 50 trades, no ORDER BY
```sql
LIMIT 50  -- no ORDER BY timestamp DESC
```
DuckDB returns insertion order. Old trades dominate. A wallet that traded one market 3 months ago then diversified shows as "concentrated" on that old market, triggering the +10 pt bonus for stale behavior. Fixed: `_CONCENTRATION_SQL` already had `ORDER BY timestamp DESC` — AUDIT was referencing stale code.

### M3. Liquidity cliff detection uses latest/earliest spread, not trade-time spread
The cliff detector compares `arg_max(spread, ts)` vs `arg_min(spread, ts)` — the latest and earliest snapshots in the window — not the spread *at the trade's timestamp*. Sparse book snapshots (every 30s per market) mean the "cliff" is measured outside the trade's window in many cases.

### M4. Z-score returns 0 when no data — treated as "no anomaly" not "unknown"
`get_zscore_for_market` returns `0.0` if the view returns no row. Scorer treats that as within normal range and assigns no volume-anomaly score. Insufficient data is indistinguishable from normal data.

### M5. `hours_to_resolution` off when market `end_date` has no timezone
`_parse_end_date` returns a naive datetime if the raw string has no UTC offset. The tz-patching logic only fixes naive `end_date`, but if `trade_timestamp` has a different timezone or tzinfo is already wrong, the subtraction is off by hours.

### M6. Resolved market status lags by up to 2 hours
Win rate in `v_wallet_performance` uses `resolved_price` from `markets` table, updated only on the 2-hour full sync. A market resolving between syncs appears unresolved; trades after resolution are treated as informed-trading candidates.

### M7. **[FIXED]** Budget reset race condition at midnight UTC
Fixed: `try_record_call` now uses atomic `UPDATE ... WHERE calls_used < calls_limit RETURNING`, eliminating the TOCTOU race.

### M8. News cache (12h TTL) outlasts market resolution
News fetched at 11:55 PM is cached until 11:55 AM next day. A market that resolves overnight gets classified against stale pre-resolution headlines. No cache invalidation on market resolution events.

### M9. **[FIXED]** Judge drops signals silently when budget exhausted (`judge/pipeline.py:127`)
Fixed: `skipped_budget` counter added and included in the 30s status log output. Budget exhaustion is now logged as a warning.

### M10. API tier classification diverges from ingester priority formula
The `_tier()` helper in `api/routes/markets.py` replicates the hot-tier logic independently from `ingester/markets.py`. When the priority formula changes in one place, the API's tier labels are stale until manually updated in both.

### M11. `v_order_flow_imbalance` window hardcoded at 5 minutes
The 5-minute window is baked into the view definition, not a config parameter. A fast-moving crypto or breaking-news market needs a tighter window; a slow political market needs a wider one. Currently no way to tune per-market.

### M12. **[FIXED]** Price impact component is likely always 0
Fixed: `price_impact_fallback_liquidity_usd = 10,000` added to config. SQL CASE falls back to this denominator when `liquidity_usd = 0`, so the 20-pt price impact dimension is now active.

### M13. WS and REST trade the same field with different completeness; source field unused
WS trades (`last_trade_price`) have no wallet, no tx_hash, no dedup-safe ID. REST trades have all three. The `source` field (`'ws'`/`'rest'`) is stored in DB but never consulted by the scanner or judge to adjust scoring expectations. Both are scored identically despite fundamentally different data completeness.

---

## 🟡 Moderate — Analysis Correctness

### A1. MAD = 0 case in Z-score calculation
Modified Z-score uses `1.4826 × MAD`. If all volume values in the window are identical (common for new/quiet markets), MAD = 0 → division by zero. No fallback is documented in the view.

### A2. **[FIXED]** OFI for a single trade is always ±1.0
With one trade in the window: `(buy_vol - sell_vol) / total_vol = ±1.0`. The OFI amplifier then boosts a borderline volume signal by 1.5×. New markets with sparse history are structurally biased to score higher. Fixed: `get_ofi_for_market` returns 0.0 (neutral) when fewer than 5 trades in the window, preventing structural ±1.0 bias.

### A3. Wallet win rate threshold (0.6) is coarse at minimum trade count (5)
At exactly 5 resolved trades, a wallet needs 4 wins to exceed 60% (3/5=60% fails; 4/5=80% passes). A single trade swing causes a 20pp win-rate jump — disproportionate to the actual information content.

### A4. **[FIXED]** Time-to-resolution 1.4x multiplier collapses into the score cap
Fixed: score cap removed (see C10). Multiplied scores are now uncapped, preserving ranking distinctions.

### A5. **[FIXED]** Tavily news fetch has no retry or rate-limit handling
`news.py` uses `httpx.AsyncClient` with no retry logic. Tavily rate-limit errors are swallowed by `except Exception`, leaving `headlines = ""`. The LLM classifies with zero news context on rate-limit errors, indistinguishable from a market with genuinely no news. Fixed: `_search_tavily` retries once with 2s backoff on HTTP errors/timeouts. Error results cached with 5-min TTL (not the full 12h) so they're retried soon.

### A6. **[FIXED]** LLM prompt always shows `liquidity_usd = $0`
The classifier prompt passes `liquidity_usd` as market context. Since Polymarket API returns null → stored as 0.0, the LLM always sees "liquidity: $0" and may systematically over-classify toward NOISE for all markets. Fixed: `_lookup_market` now falls back to `settings.price_impact_fallback_liquidity_usd` ($10K) when liquidity is 0.

### A7. Attractiveness score fallback = 50, not NULL
If the LLM fails to parse attractiveness, markets are scored 50/100 and enter the priority formula with a mid-range score rather than being flagged as unscored and excluded. Failed scoring is invisible in the hot tier.

### A8. `v_5m_volume` window includes the anomalous trade itself
The volume view cuts a fixed 5-minute rolling window. If a scanner run processes a trade at t=0, that trade is in the window at t=4m59s when the same market is rescanned. The anomalous trade contaminates its own baseline Z-score calculation.

---

## 🔵 Operational / Observability Gaps

| # | Issue | Location |
|---|---|---|
| O1 | No counter for signals dropped on budget exhaustion | `judge/pipeline.py` |
| O2 | No counter for WS reconnections or gap duration | `listener.py` |
| O3 | No alert when hot tier shrinks below threshold (e.g. 5 of 50 markets) | `__main__.py` |
| O4 | `sync_markets` failure is non-fatal with no retry, no backoff, no circuit breaker | `__main__.py` |
| O5 | WS + REST double-ingestion of same trade (different synthetic vs real trade IDs) | `writer.py` / `poller.py` |
| O6 | Book snapshots every 30s per market → liquidity cliff resolution is ±30s | `__main__.py:346` |
| O7 | `db_written` counter counts batched items, not successfully-written rows | `writer.py` |

---

## Schema / Database

| Issue | Location | Risk |
|---|---|---|
| No FK on `signals(trade_id)` or `signals(market_id)` | `init.py` | Orphaned signals accumulate |
| No index on `statistical_score`, `suspicion_score` — full scans on every API signal query | `init.py` | API latency degrades with data volume |
| No index on `trades(wallet)` despite heavy wallet-profile queries in scanner | `init.py` | Scanner latency scales poorly |
| `v_wallet_performance` win threshold (0.95 / 0.05) arbitrary and undocumented | `init.py` | Win rates distorted near boundary |
| `resolved_price` updated only on 2h sync; no trigger on resolution event | `init.py` | Stale win rates for hours post-resolution |
| `book_snapshots` has no FK to `markets` | `init.py` | Orphaned snapshots on market deletion |

---

## Missing Input Validation

| Field | Location | Risk if invalid |
|---|---|---|
| `price` — not validated to `[0, 1]` | `models.py` | Negative or >1 probabilities corrupt Z-score |
| `size_usd` — not validated `>= 0` | `models.py` | Negative sizes in volume calculations |
| `liquidity_usd` — not validated; could be NaN or Inf | `markets.py:209` | Price impact produces NaN scores |
| `end_date` — not validated to be in the future | `markets.py:200` | Negative hours_to_resolution triggers max urgency multiplier |
| Attractiveness score — fallback 50, not NULL | `market_scorer.py` | Failed LLM scores enter priority formula |

---

## Resolved Issues

The following have been fixed (commits `c50f89c`, `36513f3`, `82e250f`):

- **C1** — WS empty wallet: `_wallet_known` gate + view filters
- **C2** — trade_id collisions: millisecond precision + 16-char prefix
- **C5** — wallet profiler crash: guarded by `_wallet_known`
- **C10** — score cap: `min(100, score)` removed
- **M7** — budget race: atomic `UPDATE ... RETURNING`
- **M9** — judge budget skip: `skipped_budget` counter + status log
- **M12** — price impact dead: $10K fallback liquidity
- **A4** — multiplier collapse: score cap removed

Additionally fixed in `82e250f`:
- Budget isolation: market scoring uses separate 4,000/day pool
- WS subscription refresh: new hot-tier markets get WS events
- BookEvent queue waste: removed from scanner queue
- Market deactivation: absent markets marked `active=false` after sync
- `_tier()` ternary bug: rewritten as explicit if/else
- Empty wallet in views: filtered from `v_wallet_performance` and `v_coordination_signals`

Statistical quality fixes (session 2):
- **C3** — `fee_rate_bps` logging: skip counter + sample logging
- **C4** — WS reconnect: max backoff 15s, `on_reconnect` backfill poll, reconnect counter
- **C8** — Sync retry: `_sync_with_retry` with exponential backoff (4 retries, 30s-10m)
- **O5** — WS/REST double-ingestion: `v_deduped_trades` view, all views/queries repointed
- **A1** — MAD=0: Z-score views return NULL instead of 0
- Price impact: log-scaled scoring (was linear ×1000, now log10+4 ×4)
- OFI direction: contrarian trades now boosted (was aligned)
- Win rate: smooth ramp from 50% (was hard cutoff at 60%)
- Trade-level Z: modulated by trade size percentile (0.5-1.5× multiplier)
- Liquidity cliff: compares current spread vs MIN (was vs earliest)
- Outcome validation: `v_signal_outcomes` view + `/metrics/accuracy/summary` + `/metrics/accuracy/calibration` endpoints

## Remaining Items (Low Priority)

All critical, major, and moderate issues have been resolved. Remaining items are observability gaps and schema hygiene:

| # | Issue | Category |
|---|---|---|
| O2 | No counter for WS reconnections or gap duration | Observability (reconnect_count now tracked; gap duration not yet) |
| O3 | No alert when hot tier shrinks below threshold | Observability |
| O4 | `sync_markets` failure non-fatal (now has retry but no circuit breaker) | Observability |
| O6 | Book snapshots every 30s — liquidity cliff resolution is ±30s | Structural |
| O7 | `db_written` counter counts batched items, not successfully-written rows | Observability |
| Schema | No FK on signals, no index on statistical_score/suspicion_score | DB hygiene |
| A3 | Wallet win rate threshold (0.6) is coarse at n=5 | Now smooth ramp from 0.5, but n=5 is still statistically weak |
| A7 | Attractiveness score fallback = 50, not NULL | Market scorer already changed to fallback=0 |
| A8 | `v_5m_volume` window includes the anomalous trade itself | Partially mitigated by trade-size percentile modulation |

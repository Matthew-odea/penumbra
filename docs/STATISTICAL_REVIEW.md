# Statistical Merit Review: Penumbra

> Generated: 2026-03-29.
>
> **Perspective:** Quantitative trader evaluating whether this tool produces reliable, actionable signals.
>
> **Verdict: Signals are not tradable in current form.** The system detects *something*, but the signal-to-noise ratio is uncharacterized, the statistical foundations have several fundamental flaws, and there is no feedback loop to measure or improve accuracy.

---

## 1. FATAL: No Calibration or Feedback Loop

The entire system has **never been validated against outcomes**. When a market resolves, nobody checks whether INFORMED signals actually traded on the correct side. There is no precision, no recall, no F1, no ROC curve. The `v_wallet_performance` view tracks win rates, but this is never connected back to the signals table to answer: "Of trades we flagged INFORMED, what fraction were actually correct?"

Without this, you have a detector with unknown false positive and false negative rates.

**What's needed:** A backtest pipeline that joins `signals` + `signal_reasoning` against `markets.resolved_price` to compute: (a) precision of INFORMED classifications, (b) whether high-statistical-score trades actually predict the outcome, (c) calibration of the confidence score. Without this, every threshold in the system (Z > 2.0, score >= 30, win_rate > 0.6) is a guess.

---

## 2. FATAL: The Volume Z-Score Measures the Wrong Thing

The Z-score is a **market-hour metric** applied to **individual trades**. `v_volume_anomalies` computes:

```
Is this hour's total volume anomalous vs. the last 24 hours?
```

Every trade within that hour gets the **same Z-score** regardless of its individual size. A $100 retail trade during a whale hour scores identically to the $50,000 whale trade that caused the anomaly. The scanner (`pipeline.py:125-127`) queries the latest Z-score for the market and assigns it to whichever trade is being processed:

```python
z_hourly = get_zscore_for_market(self._conn, trade.market_id)
z_5m = get_zscore_5m_for_market(self._conn, trade.market_id)
z_score = max(z_hourly, z_5m)
```

This is a **market-level anomaly** being treated as a **trade-level feature**. The informed trade and 50 noise trades that happened to occur in the same bucket all get elevated scores.

---

## 3. FATAL: WS/REST Double-Ingestion Inflates All Volume Statistics

WS trades get synthetic IDs (`ws-{ts_raw}-{asset_id[:16]}`). REST trades get the actual API trade ID. The same trade ingested via both paths creates **two rows** in `trades` with different `trade_id`s. The `source` column stores `'ws'` or `'rest'` but is **never consulted** by any view or query.

This means:
- Volume in `v_hourly_volume` and `v_5m_volume` is inflated by up to 2x for hot-tier markets
- Z-scores are computed on inflated baselines
- OFI calculations double-count trades
- `v_coordination_signals` counts the same wallet twice (once per source)
- Win rates in `v_wallet_performance` are correct (both rows map to the same resolved market) but trade counts are inflated

Every statistical measure downstream is corrupted.

---

## 4. CRITICAL: Anomalous Trade Contaminates Its Own Baseline (AUDIT A8)

The 5-min Z-score window includes the trade being analyzed. If a scanner run processes a trade at t=0, that trade's volume is already in `v_5m_volume` when the Z-score is queried. The trade partially creates its own anomaly signal. This is circular reasoning.

For the hourly window, the same issue exists but is diluted across 60 minutes.

---

## 5. CRITICAL: MAD=0 Produces Z=0, Not "Unknown"

When all volume values in the lookback window are identical (common for new/quiet markets), `MAD = 0` and the view returns `z_score = 0`:

```sql
CASE WHEN mm.mad_vol > 0 THEN 0.6745 * (h.volume_usd - ms.median_vol) / mm.mad_vol
     ELSE 0 END
```

A brand-new market with zero trades for 23 hours and then a single large trade gets `z_score = 0` — the **least suspicious** value. But the first large trade on a new market is exactly the pattern you'd want to catch. "Insufficient data" is treated as "normal," which is the opposite of what you want for anomaly detection.

The `get_zscore_for_market` function (`volume.py:117-118`) compounds this by returning `0.0` when no row exists at all — conflating "no data" with "no anomaly."

---

## 6. CRITICAL: Price Impact Component Is Effectively Dead Weight

The formula is `|ΔP| / L × V` where:
- ΔP = price change vs. previous trade (0-1 scale, typically 0.001-0.01)
- L = liquidity in USD (fallback $10K)
- V = trade size in USD

A typical trade: $200 trade, 0.005 price delta, $10K liquidity = `0.005 / 10000 × 200 = 0.0001`.

The scorer does `min(20, int(price_impact * 1000))` = `min(20, 0)` = **0 points**.

For price impact to score even 1 point, you need `impact >= 0.001`, which requires either:
- A huge price move (>$0.05 on $10K liquidity with $200 size), or
- Extremely low liquidity, or
- Very large trade size

In practice, most trades score 0 on this component. The 20-point weight allocation is wasted.

Additionally, ΔP is measured against the **immediately preceding trade** via `LAG()`. In a coordinated buying push, the first trade gets the full delta; subsequent trades in the same direction get diminishing deltas because each is compared to the already-moved previous price.

---

## 7. MAJOR: Scoring Weights Are Arbitrary, Not Learned

The 40/20/20/20 weight split, the Z-score threshold of 2.0, the win rate cutoff of 0.6, the funding window of 72h, the concentration thresholds of 0.5/0.8 — none of these are derived from data. They're hand-tuned parameters with no evidence they're optimal or even reasonable.

The multipliers compound arbitrarily: time-to-resolution (1.4x) × liquidity cliff (1.2x) × coordination (1.3x) = 2.184x. Whether these should be additive, multiplicative, or something else entirely is an open question the system doesn't attempt to answer.

A simple logistic regression on labeled data (flagged trades vs. actual outcomes) would produce calibrated weights and tell you which features actually matter.

---

## 8. MAJOR: Coordination Detection Is Trivially Triggered

The threshold is ≥3 distinct wallets on the same market+side in a 5-minute window. In any liquid market (which all hot-tier markets are by definition), 3 buyers in 5 minutes is **normal activity**. The coordination signal fires constantly on active markets and never fires on inactive ones.

The multiplier is 1.15x when volume Z-score already fired (to "reduce double-counting") and 1.3x otherwise. But this is backwards: coordination is most meaningful when volume is NOT anomalous (small coordinated trades below volume radar).

---

## 9. MAJOR: OFI Amplifier Has Inverted Logic for Informed Trading

The OFI multiplier boosts when the trade goes **with** the flow (1.2-1.5x) and stays neutral when **against** the flow (1.0x). But contrarian positioning — selling into buying pressure or buying into a panic — is a classic informed pattern. An insider who knows the market will resolve NO would sell into frenzied YES buying. The current logic specifically doesn't boost this most suspicious pattern.

The comment at `scorer.py:163` says: "Against flow — don't penalise, may still be informed." It should arguably be the **highest** boost.

---

## 10. MAJOR: Wallet Win Rate Has No Recency or Category Weighting

`v_wallet_performance` aggregates across **all resolved markets, all time**. A wallet that was profitable on sports markets 6 months ago gets the same win_rate boost when trading political markets today. There's no:
- Time decay (recent performance matters more)
- Category matching (sports win rate ≠ political market skill)
- Market difficulty weighting (50/50 markets vs. 90/10 markets)

The `HAVING COUNT(*) >= 5` filter means any wallet with exactly 5 resolved trades and 4 wins (80% rate) gets a massive score boost, even though n=5 is statistically meaningless. The threshold jump at 60% is discontinuous: 59.9% → 0 points, 60.1% → 12 points.

---

## 11. MAJOR: LLM Classification Adds Questionable Value

The Tier 1 classifier receives the same features the statistical scorer already used (Z-score, OFI, win rate, funding age) plus news headlines. For signals scoring <70, there are **no headlines** — the LLM sees "No relevant news found." and classifies based purely on the statistical features it was given. At that point it's an expensive rebrand of the statistical score.

For signals ≥70 that do get news: the 12-hour cache TTL means news can be stale. The LLM is asked to judge "timing suspiciously early relative to news" but has no knowledge of when the news actually broke vs. when it was cached. And the LLM always sees `liquidity_usd = $0` because Polymarket returns null for most markets (AUDIT A6).

The confidence score (0-100) from the LLM is uncalibrated. There's no evidence that LLM confidence=80 corresponds to 80% likelihood of informed trading.

---

## 12. MAJOR: Funding Anomaly Is Over-Inclusive

The 72-hour threshold (`funding_anomaly_threshold_minutes = 4320`) means **any wallet created within the last 3 days** is flagged. On Polymarket, wallet creation is routine and doesn't require identity — many active traders create new wallets regularly for privacy.

The tiered decay mitigates this somewhat (2 points at 24-72h) but the binary `is_anomaly = True` flag means the funding check runs and the wallet enters the scoring path. Combined with the "zero-history suspicion bonus" (+5 pts for wallets with <5 resolved trades and trades >$500), most normal new users are flagged.

---

## 13. MAJOR: Hot Tier Selection Bias

Only 50 markets get intensive monitoring. These are selected by LLM "attractiveness" scores — markets where insider trading seems plausible to a language model. This creates a fundamental selection bias: insider trading might be more prevalent in markets that don't look insider-tradable to an LLM (obscure markets with less scrutiny).

Additionally, the attractiveness scoring itself uses `liquidity_usd` which is typically 0 (see point about Polymarket API returning null), so the LLM always sees "Liquidity: $0" and its scoring is distorted.

---

## 14. MODERATE: Liquidity Cliff Detection Uses Wrong Comparison

```sql
arg_max(spread, ts) AS current_spread,
arg_min(spread, ts) AS oldest_spread
```

This compares the **latest** spread to the **earliest** spread in the 10-minute window. If the spread spiked at t-5min then recovered by t=now, the latest spread is narrow and the cliff is missed. What you want is `MAX(spread)` vs the spread at trade time, not latest vs earliest.

Also, book snapshots are taken every 30 seconds per market. With a 10-minute window, you have ~20 data points. The cliff detector has ±30 second resolution, which is coarse for catching market maker behavior that often happens in seconds.

---

## 15. MODERATE: No Position-Level Analysis

The system analyzes trades individually. Insider trading typically involves **building positions** — many small trades over time. A wallet making 50 × $100 trades over an hour is far more suspicious than one $5,000 trade, but the system flags the $5,000 trade (larger Z-score contribution) and may not flag any of the $100 trades (each below `min_trade_size_usd` or not moving Z-score individually).

There's no cross-trade wallet behavior analysis (e.g., "this wallet has placed 12 trades in 3 hours, all on YES for the same market").

---

## 16. MODERATE: Z-Score Window Sizes Are Fixed and Wrong for Most Markets

- Hourly Z-score: 24h lookback → ≤24 data points. MAD of 24 observations is a weak estimator.
- 5-min Z-score: 2h lookback → ≤24 data points. Same problem.

The 5-min window is too short for slow markets (political: may have 0-1 trades per 5 minutes) and the hourly window is too coarse for fast markets (crypto: hundreds of trades per hour). There's no adaptive windowing.

---

## Signal Quality Assessment

| Component | Weight | Reliability | Issue |
|---|---|---|---|
| Volume Z-score | 40pts | **Low** | Market-level metric, self-contaminating, MAD=0 = "normal", double-counted from WS+REST |
| Price impact | 20pts | **Near-zero** | Almost always scores 0 due to formula scaling |
| Wallet reputation | 20pts | **Low** | No recency, no category, n=5 is meaningless, discontinuous threshold |
| Funding anomaly | 20pts | **Moderate** | Directionally correct but over-inclusive at 72h |
| OFI multiplier | mult | **Inverted** | Boosts with-flow trades, should boost contrarian |
| Coordination | mult | **Noisy** | Trivially triggered on any liquid market |
| Time-to-resolution | mult | **Moderate** | Directionally correct (Kyle 1985 is real) |
| Liquidity cliff | mult | **Weak** | Wrong comparison (latest vs earliest, not max vs trade-time) |
| LLM classification | final | **Unvalidated** | Uncalibrated confidence, often no news context |

---

## What Would Make This Tradable

1. **Outcome validation**: Join signals to market resolution. Compute precision/recall. This is the single most important thing.
2. **Deduplicate WS/REST trades**: Either filter by `source` in views, or deduplicate at ingestion.
3. **Trade-level Z-score**: Compare individual trade size to the market's recent trade size distribution, not hourly volume to hourly volume.
4. **Learned weights**: Replace arbitrary 40/20/20/20 with logistic regression or gradient-boosted model trained on labeled outcomes.
5. **Fix MAD=0**: Return NULL (unknown) instead of 0 (normal). Treat insufficient data as high uncertainty.
6. **Position-level tracking**: Aggregate per-wallet, per-market, per-day and look for accumulation patterns.
7. **Fix OFI direction**: Contrarian trades in heavy flow should score highest.
8. **Recency-weighted win rate**: Exponentially decay older trades. Match by market category.

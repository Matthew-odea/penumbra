# ADR-003: Bedrock Budget Cap & Parallel LLM Processing

**Status:** Accepted (Updated 2026-03-11)  
**Date:** 2026-03-11  
**Deciders:** Core team

## Context

AWS Bedrock pricing (as of March 2026):
- **Amazon Nova Lite**: ~$0.00006/1K input tokens, ~$0.00024/1K output tokens
- **Amazon Nova Pro**: ~$0.0008/1K input tokens, ~$0.0032/1K output tokens

A typical "Judge" call includes:
- Input: ~800 tokens (5 headlines + trade context + system prompt)
- Output: ~200 tokens (2-sentence reasoning + score)

Per-call cost estimate:
- Nova Lite (classifier): ~$0.0001
- Nova Pro (deep reasoning): ~$0.001

Polymarket can see **10K-200K trades/day** during active periods. After statistical filtering (score ≥30), expect **500-30,000 signals/day**. Sequential processing creates a throughput bottleneck.

## Decision

Implement a **high-throughput parallel LLM pipeline with Nova Lite-only mode**:

### Parallel Worker Pool
- **8 concurrent workers** process signals in parallel
- Each worker: news fetch (0.5-2s) + LLM call (1-3s) = 2-5s
- Throughput: **~13,824 signals/day** (96-240/min)
- Semaphore limits concurrent Bedrock calls to avoid rate limits

### Tier 1: Amazon Nova Lite (Classifier)
- **Budget**: 5,000 calls/day (hard cap)
- **Purpose**: Quick classification — "Informed" vs "Retail Noise" with a confidence score
- **Trigger**: Every high-scoring signal from the statistical filter

### Tier 2: Amazon Nova Pro (Deep Reasoner)
- **Budget**: 0 calls/day (disabled by default for cost optimization)
- **Purpose**: Detailed reasoning for highest-suspicion trades (optional)
- **Trigger**: Only when enabled and confidence ≥ 60

### News Caching
- Extended cache TTL: **12 hours** (up from 1 hour)
- **Score-based fetching**: Only fetch news for signals with statistical_score ≥ 70
- Reduces Tavily API calls dramatically (free tier: ~33/day)
- Per-market caching prevents redundant searches
- Low-scoring signals get LLM analysis without news context

### Market Attractiveness Scoring (Separate Budget)
- Uses Nova Lite to score each market's insider-trading potential (0-100)
- **Budget**: 4,000 calls/day — independent pool from the Judge's tier1
- Runs on first sync (~3,900 markets) and for newly discovered markets
- 3 parallel workers (reduced from 8 to fit t3.micro memory)
- Scores are stored in `markets.attractiveness_score` and never recomputed

### Budget Tracking
- Stored in DuckDB: `llm_budget` table with `date`, `tier`, `calls_used`, `calls_limit`
- Three independent budget pools: `tier1` (judge), `tier2` (judge deep), `market_scoring`
- Atomic check-and-increment: `UPDATE ... WHERE calls_used < calls_limit RETURNING`
- Thread-safe across worker pool via DuckDB's single-writer guarantee
- Exposed on the dashboard as a "Budget Remaining" indicator

## Consequences

### Cost Impact
- **Judge (Nova Lite)**: 5,000 x $0.0001 = **$0.50/day**
- **Market scoring (Nova Lite)**: 4,000 x $0.0001 = **$0.40/day** (mostly first-boot)
- **Total**: ~$0.90/day (~$27/month) at full utilisation
- **No surprise bills**: Hard caps prevent runaway costs

### Throughput
- **Sequential**: 720-1,800 signals/day
- **8 workers**: 13,824 signals/day
- Can handle Polymarket's busiest days without queuing

### Trade-offs
- Multi-worker introduces complexity (thread-safe counters, semaphores)
- News fetching now selective (only for score ≥70) — most signals analyzed without news context
- Nova Pro disabled by default — enable per-use-case

## Configuration

All values configurable in `.env`:
```
BEDROCK_TIER1_MODEL=amazon.nova-lite-v1:0
BEDROCK_TIER1_DAILY_LIMIT=5000
BEDROCK_MARKET_SCORING_DAILY_LIMIT=4000
BEDROCK_TIER2_MODEL=amazon.nova-pro-v1:0
BEDROCK_TIER2_DAILY_LIMIT=0
BEDROCK_TIER2_MIN_SUSPICION=60
JUDGE_MAX_WORKERS=8
NEWS_CACHE_TTL_HOURS=12
NEWS_MIN_SCORE=70
```

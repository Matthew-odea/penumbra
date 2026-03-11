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

### Budget Tracking
- Stored in DuckDB: `llm_budget` table with `date`, `tier`, `calls_used`, `calls_limit`
- Thread-safe counters across worker pool
- Checked before every Bedrock invocation
- Exposed on the dashboard as a "Budget Remaining" indicator

## Consequences

### Cost Impact
- **Nova Lite only**: 5,000 × $0.0001 = **$0.50/day** (~$15/month)
- **With Nova Pro** (30 calls): +$0.03/day
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
BEDROCK_TIER2_MODEL=amazon.nova-pro-v1:0
BEDROCK_TIER2_DAILY_LIMIT=0
BEDROCK_TIER2_MIN_SUSPICION=60
JUDGE_MAX_WORKERS=8
NEWS_CACHE_TTL_HOURS=12
NEWS_MIN_SCORE=70
```

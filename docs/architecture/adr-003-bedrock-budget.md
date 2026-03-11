# ADR-003: Bedrock Budget Cap & Call Queue

**Status:** Accepted  
**Date:** 2026-03-11  
**Deciders:** Core team

## Context

AWS Bedrock pricing (as of March 2026):
- **Llama 3 8B**: ~$0.0003/1K input tokens, ~$0.0006/1K output tokens
- **Claude 3.5 Sonnet**: ~$0.003/1K input tokens, ~$0.015/1K output tokens

A typical "Judge" call includes:
- Input: ~800 tokens (5 headlines + trade context + system prompt)
- Output: ~200 tokens (2-sentence reasoning + score)

Per-call cost estimate:
- Llama 3 8B (classifier): ~$0.0004
- Claude 3.5 Sonnet (deep reasoning): ~$0.005

If the statistical filter is miscalibrated and flags **500 trades/day**:
- All to Llama 3: $0.20/day → fine
- All to Claude 3.5: $2.50/day → acceptable but wasteful
- Uncapped: Risk of runaway costs if a market event triggers mass flagging

## Decision

Implement a **two-tier call budget with priority queue**:

### Tier 1: Llama 3 8B (Classifier)
- **Budget**: 200 calls/day (hard cap)
- **Purpose**: Quick classification — "Informed" vs "Retail Noise" with a confidence score
- **Trigger**: Every trade that passes the Statistical Filter

### Tier 2: Claude 3.5 Sonnet (Deep Reasoner)
- **Budget**: 30 calls/day (hard cap)
- **Purpose**: Detailed 2-sentence explanation for the highest-suspicion trades
- **Trigger**: Only trades where Llama 3 returns suspicion ≥ 60

### Priority Queue
When the daily budget is exhausted:
1. Trades are queued with their statistical score
2. If a new trade's score exceeds the lowest-scored queued trade, it replaces it
3. Queue is flushed at midnight UTC, resetting the budget

### Budget Tracking
- Stored in DuckDB: `llm_budget` table with `date`, `tier`, `calls_used`, `calls_limit`
- Checked before every Bedrock invocation
- Exposed on the dashboard as a "Budget Remaining" indicator

## Consequences

- **Worst-case daily cost**: 200 × $0.0004 + 30 × $0.005 = $0.08 + $0.15 = **$0.23/day** (~$7/month)
- **No surprise bills**: Hard caps prevent runaway costs even during mass-flagging events
- **Trade-off**: Some genuinely interesting trades may not get Claude analysis on busy days. The queue ensures the *most* interesting ones are prioritized.

## Configuration

All values configurable in `.env`:
```
BEDROCK_TIER1_MODEL=meta.llama3-8b-instruct-v1:0
BEDROCK_TIER1_DAILY_LIMIT=200
BEDROCK_TIER2_MODEL=anthropic.claude-3-5-sonnet-20241022-v2:0
BEDROCK_TIER2_DAILY_LIMIT=30
BEDROCK_TIER2_MIN_SUSPICION=60
```

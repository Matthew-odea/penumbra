# ADR-003: Bedrock Budget Cap & Call Queue

**Status:** Accepted (Updated)  
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

If the statistical filter is miscalibrated and flags **500 trades/day**:
- All to Nova Lite: $0.05/day → fine
- All to Nova Pro: $0.50/day → acceptable but wasteful
- Uncapped: Risk of runaway costs if a market event triggers mass flagging

## Decision

Implement a **two-tier call budget with priority queue**:

### Tier 1: Amazon Nova Lite (Classifier)
- **Budget**: 200 calls/day (hard cap)
- **Purpose**: Quick classification — "Informed" vs "Retail Noise" with a confidence score
- **Trigger**: Every trade that passes the Statistical Filter

### Tier 2: Amazon Nova Pro (Deep Reasoner)
- **Budget**: 30 calls/day (hard cap)
- **Purpose**: Detailed 2-sentence explanation for the highest-suspicion trades
- **Trigger**: Only trades where Nova Lite returns suspicion ≥ 60

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

- **Worst-case daily cost**: 200 × $0.0001 + 30 × $0.001 = $0.02 + $0.03 = **$0.05/day** (~$1.50/month)
- **No surprise bills**: Hard caps prevent runaway costs even during mass-flagging events
- **Trade-off**: Some genuinely interesting trades may not get Nova Pro analysis on busy days. The queue ensures the *most* interesting ones are prioritized.

## Configuration

All values configurable in `.env`:
```
BEDROCK_TIER1_MODEL=amazon.nova-lite-v1:0
BEDROCK_TIER1_DAILY_LIMIT=200
BEDROCK_TIER2_MODEL=amazon.nova-pro-v1:0
BEDROCK_TIER2_DAILY_LIMIT=30
BEDROCK_TIER2_MIN_SUSPICION=60
```

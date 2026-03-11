# Sprint 3: The Judge (Agentic Reasoning Layer)

**Goal:** For each flagged signal, use AWS Bedrock + Tavily to produce a human-readable explanation and suspicion score (1-100).

**Duration:** ~4-6 hours  
**Depends on:** Sprint 2 (signals in DuckDB with statistical scores)

---

## Architecture

```
Judge Queue (from Scanner)
         │
         ▼
  [Budget Gate]          ── Check daily call limits
         │
         ▼
  [News Fetcher]         ── Tavily search for market context
         │
         ▼
  [Tier 1: Llama 3]     ── Quick classification + confidence
         │
    confidence ≥ 60?
    ┌─── YES ───┐
    ▼            ▼
  [Tier 2:    [Store Result]
   Claude]       └→ DuckDB + Supabase
    │
    ▼
  [Store Result]
    └→ DuckDB + Supabase
    └→ Alert Queue (if score ≥ 80)
```

## Tasks

### 3.1 — Budget Manager (`sentinel/judge/budget.py`)

Tracks and enforces daily LLM call limits.

```python
@dataclass
class BudgetStatus:
    tier: str           # "tier1" or "tier2"
    calls_used: int
    calls_limit: int
    remaining: int
    is_exhausted: bool

class BudgetManager:
    """Tracks daily Bedrock call budgets in DuckDB."""
    
    def __init__(self, db: duckdb.DuckDBPyConnection):
        self.db = db
        self._ensure_table()
    
    def can_call(self, tier: str) -> bool:
        """Check if we have budget remaining for this tier."""
        ...
    
    def record_call(self, tier: str) -> None:
        """Increment the call counter for today."""
        ...
    
    def get_status(self) -> dict[str, BudgetStatus]:
        """Return current budget status for all tiers."""
        ...
```

**Acceptance criteria:**
- [ ] DuckDB `llm_budget` table tracks calls per day per tier
- [ ] Hard caps: 200/day for Tier 1, 30/day for Tier 2
- [ ] Resets at midnight UTC
- [ ] `can_call()` returns False when exhausted
- [ ] Caps are configurable via environment variables

### 3.2 — News Context Fetcher (`sentinel/judge/news.py`)

Wraps Tavily to fetch and format news for a given market.

**Acceptance criteria:**
- [ ] Fetches top 5 headlines from the last 3 days
- [ ] Formats headlines for LLM prompt consumption (≤ 500 tokens)
- [ ] Caches results per market per hour (avoid repeat Tavily calls)
- [ ] Falls back to Exa if Tavily is unavailable
- [ ] Returns "No relevant news found" gracefully if search yields nothing

### 3.3 — Tier 1 Classifier (`sentinel/judge/classifier.py`)

Calls Llama 3 8B via Bedrock for quick classification.

**Prompt template:** See [Bedrock integration doc](../integrations/aws-bedrock.md#tier-1--classifier-prompt-llama-3)

**Input:**
```python
@dataclass
class ClassificationInput:
    market_question: str
    category: str
    side: str
    price: float
    size_usd: float
    liquidity_usd: float
    z_score: float
    wallet_win_rate: float | None
    wallet_total_trades: int
    funding_age_minutes: int | None
    news_headlines: str       # Formatted from Tavily
```

**Output:**
```python
@dataclass
class ClassificationResult:
    classification: str       # "INFORMED" or "NOISE"
    confidence: int           # 0-100
    one_liner: str            # Brief explanation
    model: str                # Model ID used
    input_tokens: int
    output_tokens: int
```

**Acceptance criteria:**
- [ ] Constructs prompt from `ClassificationInput`
- [ ] Parses Llama 3 JSON response (with fallback for malformed JSON)
- [ ] Respects budget gate (skips if Tier 1 exhausted)
- [ ] Logs classification result with latency
- [ ] Confidence ≥ 60 → promote to Tier 2 queue

### 3.4 — Tier 2 Reasoner (`sentinel/judge/reasoner.py`)

Calls Claude 3.5 Sonnet via Bedrock for deep analysis.

**Prompt template:** See [Bedrock integration doc](../integrations/aws-bedrock.md#tier-2--reasoner-prompt-claude)

**Output:**
```python
@dataclass
class ReasoningResult:
    suspicion_score: int      # 1-100
    reasoning: str            # 2-sentence explanation
    key_evidence: str         # Single most important factor
    model: str
    input_tokens: int
    output_tokens: int
```

**Acceptance criteria:**
- [ ] Only called when Tier 1 confidence ≥ 60 AND Tier 2 budget allows
- [ ] Uses Claude Messages API format via Bedrock
- [ ] Parses structured JSON response
- [ ] Handles Bedrock timeouts (5s timeout → log + use Tier 1 result as fallback)
- [ ] Final suspicion score replaces Tier 1 confidence

### 3.5 — Result Persistence (`sentinel/judge/store.py`)

Writes Judge results back to DuckDB and Supabase.

```sql
-- DuckDB: signal_reasoning table
CREATE TABLE IF NOT EXISTS signal_reasoning (
    signal_id       VARCHAR PRIMARY KEY,
    trade_id        VARCHAR NOT NULL,
    classification  VARCHAR,           -- INFORMED / NOISE
    tier1_confidence INTEGER,
    suspicion_score INTEGER,           -- Final score (Tier 2 if available, else Tier 1)
    reasoning       VARCHAR,
    key_evidence    VARCHAR,
    news_headlines  VARCHAR,           -- JSON array of headlines used
    tier1_model     VARCHAR,
    tier2_model     VARCHAR,
    tier1_tokens    INTEGER,
    tier2_tokens    INTEGER,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Acceptance criteria:**
- [ ] Every classified trade gets a row in `signal_reasoning`
- [ ] Supabase `signals` table updated with `suspicion_score`, `reasoning`, `bedrock_model`
- [ ] If Tier 2 was called, stores both Tier 1 and Tier 2 results
- [ ] Emits signals with score ≥ 80 to the alert queue

### 3.6 — Judge Pipeline Orchestrator (`sentinel/judge/pipeline.py`)

Async coroutine that consumes from the Judge queue and orchestrates the flow.

```python
async def run(
    db: duckdb.DuckDBPyConnection,
    judge_queue: asyncio.Queue[Signal],
    alert_queue: asyncio.Queue[Alert],
):
    budget = BudgetManager(db)
    
    while True:
        signal = await judge_queue.get()
        
        # 1. Fetch news
        headlines = await get_news_cached(signal.market_id, ...)
        
        # 2. Tier 1 classification
        if budget.can_call("tier1"):
            t1_result = await classify(signal, headlines)
            budget.record_call("tier1")
        else:
            logger.warning("Tier 1 budget exhausted, skipping")
            continue
        
        # 3. Tier 2 reasoning (if warranted)
        final_score = t1_result.confidence
        reasoning = t1_result.one_liner
        
        if t1_result.confidence >= 60 and budget.can_call("tier2"):
            t2_result = await reason(signal, headlines, t1_result)
            budget.record_call("tier2")
            final_score = t2_result.suspicion_score
            reasoning = t2_result.reasoning
        
        # 4. Store results
        await store_reasoning(db, signal, t1_result, t2_result, headlines)
        
        # 5. Alert if high suspicion
        if final_score >= settings.alert_min_score:
            await alert_queue.put(Alert(signal=signal, score=final_score, reasoning=reasoning))
```

---

## Definition of Done

- [ ] Each flagged trade in DuckDB has a 2-sentence AI explanation attached
- [ ] Suspicion score (1-100) stored for every classified trade
- [ ] Budget manager prevents exceeding daily limits
- [ ] High-suspicion trades (≥80) are queued for alert delivery
- [ ] `signal_reasoning` table populated with at least 5 test entries
- [ ] `python -m sentinel.judge --replay` processes existing signals from DuckDB

## Testing

| Test | Type | Command |
|------|------|---------|
| Budget manager logic | Unit | `pytest tests/judge/test_budget.py` |
| Prompt construction | Unit | `pytest tests/judge/test_prompts.py` |
| Response parsing | Unit | `pytest tests/judge/test_parsing.py` |
| News fetcher (Tavily) | Integration | `pytest tests/judge/test_news.py -m integration` |
| Llama 3 classification | Integration | `pytest tests/judge/test_classifier.py -m integration` |
| Claude reasoning | Integration | `pytest tests/judge/test_reasoner.py -m integration` |
| Full judge pipeline | Smoke | `python -m sentinel.judge --replay --limit 5` |

## Estimated Cost

- Tavily: Free tier (1,000 calls/month)
- Bedrock: ~$0.23/day worst case (see ADR-003)
- Supabase: Free tier (signal writes)

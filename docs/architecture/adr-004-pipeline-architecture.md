# ADR-004: Pipeline Architecture (Single-Writer Coordination)

**Status:** Accepted  
**Date:** 2026-03-11  
**Deciders:** Core team

## Context

DuckDB supports only **one writer at a time**. Our system has three stages that read and write to the database:

1. **Ingester** — writes raw trades continuously
2. **Scanner** — reads trades, writes signal flags
3. **Judge** — reads signals, writes AI reasoning

Running these as independent processes would cause write contention.

## Decision

Use a **single-process, async pipeline** with three stages coordinated via `asyncio.Queue`:

```
WebSocket → [Ingester] → Queue A → [Scanner] → Queue B → [Judge] → Done
                ↓            ↓           ↓           ↓         ↓
              DuckDB       DuckDB     DuckDB      DuckDB    DuckDB
             (trades)     (trades)   (signals)   (signals) (reasoning)
```

### Design

```python
async def main():
    db = duckdb.connect("sentinel.duckdb")
    
    scan_queue: asyncio.Queue[list[Trade]] = asyncio.Queue()
    judge_queue: asyncio.Queue[list[Signal]] = asyncio.Queue()
    
    await asyncio.gather(
        ingester.run(db, scan_queue),    # writes trades, pushes batches
        scanner.run(db, scan_queue, judge_queue),  # reads+writes signals
        judge.run(db, judge_queue),      # reads+writes reasoning
    )
```

### Write Serialization

Since all three stages share a single `duckdb.Connection` in one process, DuckDB handles serialization internally. Each stage:
1. **Batches writes** (e.g., ingester flushes every 100 trades or 5 seconds)
2. **Uses transactions** to ensure atomicity
3. **Yields control** via `await asyncio.sleep(0)` between batches

### Scaling Later

If throughput demands exceed single-process capacity (unlikely for Polymarket's volume):
- Option A: Use DuckDB's WAL mode + separate read-only connections for Scanner/Judge
- Option B: Move to a proper OLTP+OLAP split (Supabase for writes, DuckDB for reads)

## Consequences

- Simple deployment: one Python process, one `.duckdb` file
- No write contention by design
- All stages share the same event loop, so a slow Bedrock call doesn't block ingestion (it's awaited on its own coroutine)
- Testable: each stage can be tested independently with a mock queue

## References

- [DuckDB concurrency docs](https://duckdb.org/docs/connect/concurrency)
- [asyncio.Queue](https://docs.python.org/3/library/asyncio-queue.html)

# Sprint 1: The Hose (Data Ingestion & Local Storage)

**Goal:** Stream live trades from Polymarket and persist them to a local DuckDB file. Backfill 7 days of history for high-asymmetry markets.

**Duration:** ~4-6 hours  
**Depends on:** Sprint 0 (repo setup, schema, config)

---

## Architecture

```
Polymarket WS ──→ [PolymarketWebsocketsClient]
                         │
                         ▼
                  [Trade Parser] ── validate + normalize
                         │
                         ▼
                  [Batch Buffer] ── accumulate 100 trades or 5s
                         │
                         ▼
                  [DuckDB Writer] ── INSERT INTO trades
                         │
                         ▼
                  [asyncio.Queue] ── push batch to Scanner (Sprint 2)
```

## Tasks

### 1.1 — WebSocket Listener (`sentinel/ingester/listener.py`)

**Input:** Polymarket WebSocket stream  
**Output:** Parsed `Trade` dataclass pushed to batch buffer

```python
@dataclass
class Trade:
    trade_id: str
    market_id: str       # condition_id
    asset_id: str        # token_id
    wallet: str          # taker_address
    side: str            # BUY or SELL
    price: Decimal       # 0-1 probability
    size_usd: Decimal    # trade size in USDC
    timestamp: datetime
    tx_hash: str | None
```

**Acceptance criteria:**
- [ ] Connects to `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- [ ] Subscribes to all active markets (or a configurable subset by category)
- [ ] Parses raw WS message into `Trade` dataclass
- [ ] Handles reconnection on disconnect (exponential backoff, max 5 retries)
- [ ] Logs connection status and trade count to structured logger

### 1.2 — Market Metadata Sync (`sentinel/ingester/markets.py`)

**Input:** Polymarket REST API  
**Output:** DuckDB `markets` table populated

- [ ] Fetch all active markets via `ClobClient.get_markets()` (paginated)
- [ ] Filter to categories of interest: `["Biotech", "Politics", "Crypto", "Science"]`
- [ ] Upsert into DuckDB `markets` table
- [ ] Re-sync every 6 hours (configurable)
- [ ] Store `volume_usd`, `liquidity_usd`, `category`, `end_date`

### 1.3 — DuckDB Batch Writer (`sentinel/ingester/writer.py`)

**Input:** Batches of `Trade` from the buffer  
**Output:** Rows in DuckDB `trades` table

- [ ] Buffer trades in memory (100 trades or 5 seconds, whichever comes first)
- [ ] Batch INSERT using DuckDB's `executemany` or `INSERT INTO ... SELECT * FROM df`
- [ ] Handle duplicate `trade_id` gracefully (INSERT OR IGNORE)
- [ ] Log batch size and write latency
- [ ] Push the batch to the Scanner queue after successful write

### 1.4 — Backfill Script (`scripts/backfill.py`)

**Input:** Market IDs (from config or CLI args)  
**Output:** 7 days of historical trades in DuckDB

- [ ] Accept `--markets` flag (comma-separated condition_ids) or `--category` filter
- [ ] Page through `ClobClient.get_trades()` backwards using cursors
- [ ] Stop when oldest trade is >7 days old
- [ ] Skip trades already in DuckDB (by `trade_id`)
- [ ] Respect rate limits (100 req/min) with adaptive sleep
- [ ] Save cursor position to a local file for resumable backfill
- [ ] Print progress: `[Backfill] Market 0x12... — 3,500 trades loaded (5/7 days)`

### 1.5 — Dry Run Mode

- [ ] `--dry-run` flag on both ingester and backfill
- [ ] Prints parsed trades to stdout as JSON without writing to DuckDB
- [ ] Useful for verifying WS connection and message parsing

---

## Definition of Done

- [ ] `sentinel.duckdb` file contains live trades streaming in real-time
- [ ] `sentinel.duckdb` contains 7 days of backfilled data for at least 3 markets
- [ ] `markets` table has metadata for all active markets
- [ ] Ingester survives a WS disconnect and reconnects automatically
- [ ] `python -m sentinel.ingester --dry-run` prints live trades to stdout
- [ ] `python scripts/backfill.py --category Biotech` completes without error

## Testing

| Test | Type | Command |
|------|------|---------|
| Parse WS message | Unit | `pytest tests/ingester/test_parser.py` |
| Batch writer | Unit | `pytest tests/ingester/test_writer.py` |
| Backfill pagination | Unit | `pytest tests/ingester/test_backfill.py` |
| Live WS connection | Integration | `pytest tests/ingester/test_live.py -m integration` |
| Full pipeline (60s) | Smoke | `python -m sentinel.ingester --timeout 60` |

## Estimated Cost

$0 — All Polymarket API access is free. DuckDB is local.

# Integration: Polymarket CLOB API

> Primary data source for trade ingestion.

## Overview

Polymarket operates a **Central Limit Order Book (CLOB)** on Polygon. The CLOB API provides:
- REST endpoints for market metadata, order books, and historical trades
- WebSocket streams for real-time trade and order book updates

## SDK & Authentication

### Package
```
py_clob_client  # Official Python SDK
```

**PyPI**: `py-clob-client`  
**Source**: https://github.com/Polymarket/py-clob-client

### Authentication Levels

| Level | Access | Requires |
|-------|--------|----------|
| **Public** | Market data, order books, trades | Nothing — no API key |
| **Level 1 (L1)** | + Read own orders/positions | API Key + Secret + Passphrase |
| **Level 2 (L2)** | + Place/cancel orders | L1 + Polygon wallet signature |

**We only need Public access** for Penumbra. No API key required for reading trades.

### Rate Limits (as of March 2026)

| Endpoint | Limit |
|----------|-------|
| REST (public) | 100 req/min per IP |
| WebSocket | 5 connections per IP, 20 subscriptions per connection |

## WebSocket Trade Stream

### Connection

```python
import asyncio
from py_clob_client.clob_websocket import (
    PolymarketWebsocketsClient,
    WebSocketMessage,
)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

async def on_trade(message: WebSocketMessage):
    # message.data contains trade details
    print(message)

async def main():
    ws = PolymarketWebsocketsClient(
        ws_url=WS_URL,
        message_handler=on_trade,
    )
    # Subscribe to all trades on specific markets
    await ws.connect()
    # Or subscribe to a specific market by condition_id
    await ws.subscribe_to_market(condition_id="0x...")
```

### Trade Message Schema

```json
{
  "event_type": "trade",
  "data": {
    "id": "trade-uuid",
    "taker_order_id": "order-uuid",
    "market": "condition_id_hex",
    "asset_id": "token_id_hex",
    "side": "BUY",
    "size": "150.00",
    "price": "0.73",
    "timestamp": "1710000000",
    "transaction_hash": "0x...",
    "maker_address": "0x...",
    "taker_address": "0x..."
  }
}
```

### Key Fields We Extract

| Field | Maps To | Notes |
|-------|---------|-------|
| `market` | `market_id` (condition_id) | Join with market metadata to get slug, category |
| `taker_address` | `wallet` | The "informed" party is typically the **taker** |
| `price` | `price` | 0-1 range representing probability |
| `size` | `size` | In USDC (6 decimals, but SDK returns as string) |
| `timestamp` | `timestamp` | Unix epoch seconds |
| `transaction_hash` | `tx_hash` | For on-chain verification |

## REST API — Market Metadata

### Get All Markets

```python
from py_clob_client.client import ClobClient

client = ClobClient(host="https://clob.polymarket.com")
markets = client.get_markets()  # Returns paginated list
```

### Market Object (Relevant Fields)

```json
{
  "condition_id": "0x...",
  "question": "Will X happen by Y date?",
  "slug": "will-x-happen-by-y-date",
  "category": "Biotech",
  "end_date_iso": "2026-06-01T00:00:00Z",
  "tokens": [
    {"token_id": "0x...", "outcome": "Yes", "price": 0.73},
    {"token_id": "0x...", "outcome": "No", "price": 0.27}
  ],
  "volume": "1500000.00",
  "liquidity": "250000.00",
  "active": true
}
```

### Backfill — Historical Trades

```python
# Get trades for a specific market
trades = client.get_trades(
    market="condition_id_hex",
    limit=500,      # max per page
    after="cursor",  # pagination cursor
)
```

**Limitation**: The REST API doesn't support date-range filters on trades. We page backwards from the most recent trade using cursors until we've covered 7 days.

## Data Model Mapping

### DuckDB `trades` Table

```sql
CREATE TABLE IF NOT EXISTS trades (
    trade_id       VARCHAR PRIMARY KEY,
    market_id      VARCHAR NOT NULL,     -- condition_id
    asset_id       VARCHAR NOT NULL,     -- token_id
    wallet         VARCHAR NOT NULL,     -- taker_address (our focus)
    side           VARCHAR NOT NULL,     -- BUY or SELL
    price          DECIMAL(10, 6),       -- 0-1 probability
    size_usd       DECIMAL(18, 6),       -- trade size in USDC
    timestamp      TIMESTAMP NOT NULL,
    tx_hash        VARCHAR,
    ingested_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### DuckDB `markets` Table

```sql
CREATE TABLE IF NOT EXISTS markets (
    market_id      VARCHAR PRIMARY KEY,  -- condition_id
    question       VARCHAR,
    slug           VARCHAR,
    category       VARCHAR,              -- e.g., "Biotech", "Politics"
    end_date       TIMESTAMP,
    volume_usd     DECIMAL(18, 6),
    liquidity_usd  DECIMAL(18, 6),
    active         BOOLEAN DEFAULT TRUE,
    last_synced    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Polymarket changes WS endpoint | Ingestion breaks | Abstract behind `DataSource` interface; pin SDK version |
| Rate limiting on REST backfill | Slow initial load | Exponential backoff + save cursor for resumable backfill |
| SDK breaks on update | Build fails | Pin `py-clob-client==X.Y.Z` in `pyproject.toml` |
| `taker_address` not always the "informed" party | Behavioral filter noise | Also track `maker_address` for market-maker profiling |

## Testing

- **Unit**: Mock WebSocket messages, verify DuckDB insert logic
- **Integration**: Connect to live WS for 60 seconds, assert ≥1 trade received
- **Smoke**: `python -m sentinel.ingester --dry-run` prints trades to stdout without DB writes

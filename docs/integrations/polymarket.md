# Integration: Polymarket APIs

> Primary data source for trade ingestion and order-book monitoring.
>
> **Last verified**: March 2026

## Overview

Polymarket operates on Polygon via a **Central Limit Order Book (CLOB)**.
Penumbra uses three separate API surfaces:

| API | Base URL | Purpose |
|-----|----------|---------|
| **CLOB REST** | `https://clob.polymarket.com` | Market metadata, active-market discovery |
| **CLOB WebSocket** | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Real-time order-book events (price changes, snapshots) |
| **Data API** | `https://data-api.polymarket.com` | Public trade executions with wallet addresses |

All three are **unauthenticated / public** — no API key required.

---

## 1. CLOB REST API

Base: `https://clob.polymarket.com`

### Authentication

Not required for our use case. Polymarket supports L1/L2 auth for placing
orders, but we only read public data.

### Endpoints We Use

#### `GET /markets` — Paginated Market List

Returns all markets with metadata. Used for initial sync and periodic refresh.

```
GET /markets?next_cursor=<cursor>
```

Response:
```json
{
  "data": [
    {
      "condition_id": "0x...",
      "question": "Will X happen by Y date?",
      "slug": "will-x-happen-by-y-date",
      "category": "Biotech",
      "end_date_iso": "2026-06-01T00:00:00Z",
      "tokens": [
        {"token_id": "115462...", "outcome": "Yes", "price": 0.73},
        {"token_id": "488492...", "outcome": "No", "price": 0.27}
      ],
      "volume": "1500000.00",
      "liquidity": "250000.00",
      "active": true
    }
  ],
  "next_cursor": "MjAw"
}
```

**Pagination**: Cursor-based. Pass `next_cursor` from previous response.
Each page returns ~100 markets. Loop until `next_cursor` is empty/absent.

**Gotcha**: No date-range filters. We page through everything.

#### `GET /sampling-markets` — Active/Trending Markets

Returns the most active markets. Used to select hot-tier polling targets.

```
GET /sampling-markets
```

Response:
```json
{
  "data": [
    {
      "condition_id": "0x...",
      "tokens": [
        {"token_id": "115462...", "outcome": "Yes"},
        {"token_id": "488492...", "outcome": "No"}
      ],
      ...
    }
  ]
}
```

**Gotcha**: The `limit` query parameter is **ignored by the server** —
it always returns ~1000 markets. We enforce the limit client-side
(`markets[:N]`).

### Rate Limits

| Type | Limit |
|------|-------|
| REST | ~100 req/min per IP (empirical) |

---

## 2. CLOB WebSocket

URL: `wss://ws-subscriptions-clob.polymarket.com/ws/market`

### Connection & Subscription

```python
import websockets.asyncio.client as ws_client

async with ws_client.connect(
    "wss://ws-subscriptions-clob.polymarket.com/ws/market",
    additional_headers={"Origin": "https://polymarket.com"},
    ssl=ssl_ctx,
    ping_interval=20,
) as ws:
    await ws.send(json.dumps({
        "auth": {},
        "type": "subscribe",
        "markets": [],           # leave empty
        "assets_ids": ["115462...", "488492..."],  # token_ids
    }))
```

**Key detail**: Subscribe by `assets_ids` (token IDs), not by `condition_id`.
Each market has 2 tokens (Yes/No); subscribe to both for full coverage.

The `Origin: https://polymarket.com` header is required or the connection
is rejected.

### Message Types

The WS delivers **order-book state only** — it does **NOT** deliver trade
executions. We verified this empirically (March 2026, 200+ messages sampled):

| Message Type | `event_type` | Key Fields | Frequency |
|-------------|-------------|------------|-----------|
| **Price Changes** | _(absent)_ | `market`, `price_changes[]` | ~73% |
| **Book Snapshot** | `"book"` | `market`, `asset_id`, `bids[]`, `asks[]`, `last_trade_price` | ~26% |
| **Order Update** | `"last_trade_price"` | `market`, `asset_id`, `price`, `size`, `fee_rate_bps` | ~1% |
| **Trade Execution** | `"trade"` | — | **0% (never observed)** |

#### Price Changes (most common)

```json
{
  "market": "0x...",
  "price_changes": [
    {
      "asset_id": "115462...",
      "price": "0.542",
      "size": "0",
      "side": "BUY",
      "hash": "01845343...",
      "best_bid": "0.540",
      "best_ask": "0.545"
    }
  ]
}
```

- `size: "0"` means the level was cancelled
- Non-zero `size` means a new/updated resting order at that price
- `best_bid` / `best_ask` reflect the book state after this change

#### Book Snapshot

```json
{
  "market": "0x...",
  "asset_id": "115462...",
  "timestamp": "1773262601003",
  "hash": "96892e91...",
  "bids": [{"price": "0.54", "size": "2343"}, ...],
  "asks": [{"price": "0.55", "size": "1200"}, ...],
  "tick_size": "0.01",
  "event_type": "book",
  "last_trade_price": "0.542"
}
```

- Delivered on subscription and periodically
- `last_trade_price` is a scalar — no wallet/size/hash attached

### What the WS Is Good For

Despite not delivering trades, the WS is valuable for:

1. **Order-flow detection** — large orders appearing on the book can signal
   informed activity *before* execution
2. **Price impact measurement** — watching `best_bid`/`best_ask` move in
   real-time
3. **Market activity indicators** — frequency of price_changes correlates
   with market activity

### Rate Limits

| Type | Limit |
|------|-------|
| Connections | 5 per IP |
| Subscriptions | 20 per connection |

---

## 3. Data API — Trade Executions

Base: `https://data-api.polymarket.com`

**This is the only source of actual trade execution data with wallet
addresses.** Discovered March 2026 — the previously assumed
`/live-activity/events/{conditionId}` endpoint on the CLOB API does not
exist (returns 404 for all markets).

### `GET /trades` — Public Trade History

```
GET /trades?condition_id=0x...&limit=50
```

**No authentication required.** Returns trades from all wallets (public
market data, not user-scoped — verified by observing 19 unique wallets
in a 20-trade sample).

#### Response Schema

```json
[
  {
    "proxyWallet": "0x1234...abcd",
    "side": "BUY",
    "asset": "115462087336972547...",
    "conditionId": "0x5a8c5193...",
    "size": 5.43,
    "price": 0.92,
    "timestamp": 1710000000,
    "transactionHash": "0xdeadbeef...",
    "outcome": "Yes",
    "outcomeIndex": 0,
    "title": "Will X happen?",
    "slug": "will-x-happen",
    "name": "trader_username",
    "pseudonym": "Anonymous Alpaca",
    "icon": "https://...",
    "eventSlug": "will-x-happen-event"
  },
  ...
]
```

#### Field Mapping to Our `Trade` Model

| API Field | Trade Field | Type | Notes |
|-----------|------------|------|-------|
| `proxyWallet` | `wallet` | string | Trader's proxy wallet (taker) |
| `conditionId` | `market_id` | string | Market condition ID |
| `asset` | `asset_id` | string | Token ID (numeric string) |
| `side` | `side` | string | `"BUY"` or `"SELL"` |
| `price` | `price` | **number** | 0-1 probability (not a string!) |
| `size` | `size_usd` | **number** | USDC amount (not a string!) |
| `timestamp` | `timestamp` | **integer** | Unix epoch seconds |
| `transactionHash` | `tx_hash` / `trade_id` | string | On-chain tx hash, used as dedup key |
| `outcome` | _(not stored)_ | string | "Yes" or "No" |
| `name` | _(not stored)_ | string | Polymarket username |

**Important type differences from CLOB API**: `price` and `size` are
**numbers** (not strings), and `timestamp` is an **integer** (not a string).

#### Query Parameters

| Param | Type | Required | Notes |
|-------|------|----------|-------|
| `condition_id` | string | No | Filter by market. Omit for all recent trades |
| `limit` | int | No | Max results (default varies) |

#### Endpoints NOT Verified

We have not tested pagination, cursor, or date-range parameters on this
endpoint. For now we poll with `limit=50` per market and rely on dedup
to skip already-seen trades.

### Rate Limits

Not formally documented. We poll 20 hot markets every 30s + 50 cold markets
every 60s without issues (~0.5 req/s average).

---

## Endpoint Discovery Log

Documenting what we tried and what worked, for future reference:

| URL Attempted | Status | Notes |
|--------------|--------|-------|
| `clob.polymarket.com/live-activity/events/{cid}` | **404** | Does not exist. Was assumed from old docs. |
| `polymarket.com/api/live-activity/events/{cid}` | **404** | Frontend API, doesn't proxy this. |
| `gamma-api.polymarket.com/live-activity/events/{cid}` | **404** | Legacy API, shut down. |
| `clob.polymarket.com/trades?condition_id={cid}` | **401** | Exists but requires L1+ auth (user's own trades). |
| **`data-api.polymarket.com/trades?condition_id={cid}`** | **200** | Public market-wide trades. No auth. This is the one. |

---

## Architecture: How Penumbra Uses These APIs

```
┌─────────────────────────────────────────────────────────────────┐
│                      Polymarket APIs                            │
├──────────────┬──────────────────┬───────────────────────────────┤
│  CLOB REST   │   CLOB WebSocket │         Data API              │
│  /markets    │   /ws/market     │  /trades?condition_id=...     │
│  /sampling-  │                  │                               │
│   markets    │                  │                               │
└──────┬───────┴────────┬─────────┴──────────────┬────────────────┘
       │                │                        │
       ▼                ▼                        ▼
  Market Sync      Listener               TradePoller
  (every 6h)    (price_changes      ┌──────────┴──────────┐
       │         → BookEvent)       │                     │
       │                │        Hot Tier           Cold Tier
       │                │      (20 markets         (50 markets
       │                │       every 30s)          every 60s,
       │                │           │               rotating)
       │                │           │                  │
       │                ▼           ▼                  ▼
       │         ┌──────────────────────────────────────┐
       │         │          BatchWriter                 │
       │         │    (100-item batches, 5s flush)      │
       └────────►│              │                       │
                 └──────────────┼───────────────────────┘
                                │
                                ▼
                            DuckDB
                         trades table
```

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| `data-api.polymarket.com` goes down or changes schema | Trade ingestion stops | Monitor for HTTP errors; alert on 0 trades for >5 min |
| Rate limiting on data-api | Missed trades | Exponential backoff in poller; reduce cold batch size |
| `proxyWallet` ≠ real wallet | Wallet profiling is less accurate | Proxy wallets map 1:1 per user; profiling still works |
| `/sampling-markets` stops reflecting actual activity | Hot tier misses active markets | Cold tier rotates through ALL markets as safety net |
| WS connection drops | Miss book events | Auto-reconnect with exponential backoff (up to 60s) |
| Polymarket adds auth to data-api | Ingestion breaks | Fall back to `clob.polymarket.com/trades` with L1 auth |

## Testing

- **Unit**: Mock HTTP responses with flat JSON schema, verify `parse_data_api_trade()` mapping
- **Integration**: `python -m sentinel.ingester --timeout 60` — verify `rest_trades > 0` in status line
- **Smoke**: `python -m sentinel.ingester --dry-run --timeout 30` prints trades as JSON without DB writes

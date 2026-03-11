# Integration: Polygon RPC (On-Chain Wallet Analysis)

> Used by the Behavioral Filter to detect "Funding Anomalies" — wallets funded shortly before making large trades.

## Purpose

When the Statistical Filter flags a large trade, we check:
1. **When was this wallet first funded on Polygon?** (wallet age)
2. **When was the most recent inbound transfer?** (funding recency)
3. **What is the funding source?** (bridge, exchange, another wallet)

A wallet funded <60 minutes before a $10k+ trade is a strong behavioral signal.

## Approach: Alchemy Transfers API (Recommended)

Raw Polygon RPC (`eth_getLogs`) requires scanning potentially millions of blocks. Instead, we use **Alchemy's Transfers API** which indexes all ERC-20 transfers and provides a clean query interface.

### Why Alchemy Over Raw RPC?

| Approach | Speed | Cost | Complexity |
|----------|-------|------|------------|
| Raw RPC (`eth_getLogs`) | Slow (scan blocks) | Free (public RPC) | High (block range math) |
| **Alchemy Transfers API** | Fast (indexed) | Free tier: 300 CU/s | Low (one API call) |
| Dune/Flipside (batch) | Minutes latency | Free tier available | Medium (SQL queries) |

### Setup

```
ALCHEMY_API_KEY=your_key_here
ALCHEMY_POLYGON_URL=https://polygon-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}
```

### Query: Get Recent Inbound Transfers

```python
import httpx
from datetime import datetime, timezone

USDC_POLYGON = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # Native USDC on Polygon
USDCE_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e (bridged)


async def get_wallet_funding_info(wallet: str, alchemy_url: str) -> dict:
    """Get the most recent inbound USDC transfer to a wallet."""
    
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "alchemy_getAssetTransfers",
        "params": [{
            "fromBlock": "0x0",
            "toBlock": "latest",
            "toAddress": wallet,
            "category": ["erc20"],
            "contractAddresses": [USDC_POLYGON, USDCE_POLYGON],
            "order": "desc",       # Most recent first
            "maxCount": "0x5",     # Last 5 transfers
            "withMetadata": True,  # Includes block timestamp
        }]
    }
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(alchemy_url, json=payload)
        data = resp.json()
    
    transfers = data.get("result", {}).get("transfers", [])
    
    if not transfers:
        return {
            "funded": False,
            "funding_age_minutes": None,
            "funding_source": None,
            "first_funding": None,
        }
    
    latest = transfers[0]
    earliest = transfers[-1]
    
    latest_time = datetime.fromisoformat(
        latest["metadata"]["blockTimestamp"].replace("Z", "+00:00")
    )
    earliest_time = datetime.fromisoformat(
        earliest["metadata"]["blockTimestamp"].replace("Z", "+00:00")
    )
    
    now = datetime.now(timezone.utc)
    age_minutes = (now - latest_time).total_seconds() / 60
    
    return {
        "funded": True,
        "funding_age_minutes": int(age_minutes),
        "funding_source": latest["from"],
        "funding_amount": float(latest["value"]),
        "first_funding": earliest_time.isoformat(),
        "wallet_age_days": (now - earliest_time).days,
    }
```

## Interpretation

| Funding Age | Wallet Age | Interpretation |
|-------------|------------|----------------|
| < 60 min | < 1 day | **HIGH SIGNAL**: Fresh wallet, funded just before trade |
| < 60 min | > 30 days | Medium: Existing wallet, new deposit (could be normal top-up) |
| > 24 hours | > 30 days | Low: Normal trading activity |
| No transfers | — | Unknown: Wallet may be funded via native MATIC, not USDC |

## Rate Limits & Costs

### Alchemy Free Tier
- 300 Compute Units/second
- `alchemy_getAssetTransfers` costs **150 CU** per call
- Effective: **2 calls/second**, ~7,200/hour

### Our Usage
- We only call this for **flagged trades** (after Statistical Filter)
- Expected: 20-100 calls/day → well within free tier

## Fallback: Public Polygon RPC

If Alchemy is unavailable, we can fall back to raw `eth_getLogs`:

```python
async def get_usdc_transfers_raw(wallet: str, rpc_url: str, blocks_back: int = 2000):
    """Scan last ~2000 blocks (~1 hour) for USDC transfers to wallet."""
    # USDC Transfer event topic
    transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    # Wallet as topic (padded to 32 bytes)
    wallet_topic = "0x" + wallet[2:].lower().zfill(64)
    
    # Get current block
    block_resp = await rpc_call(rpc_url, "eth_blockNumber", [])
    current_block = int(block_resp, 16)
    
    # Query logs
    logs = await rpc_call(rpc_url, "eth_getLogs", [{
        "fromBlock": hex(current_block - blocks_back),
        "toBlock": "latest",
        "address": [USDC_POLYGON, USDCE_POLYGON],
        "topics": [transfer_topic, None, wallet_topic],
    }])
    
    return logs  # Parse block numbers → timestamps
```

**Note**: This is slower and only covers ~1 hour of history. Use Alchemy as primary.

## Environment Variables

```
POLYGON_RPC_URL=https://polygon-rpc.com          # Public fallback
ALCHEMY_API_KEY=your_key_here                      # Primary
ALCHEMY_POLYGON_URL=https://polygon-mainnet.g.alchemy.com/v2/${ALCHEMY_API_KEY}
FUNDING_ANOMALY_THRESHOLD_MINUTES=60               # Flag if funded within this window
```

## Testing

- **Unit**: Mock Alchemy response JSON, verify parsing logic
- **Integration**: Query a known Polymarket whale wallet, assert reasonable funding data
- **Edge cases**: Wallet with no transfers, wallet funded via MATIC not USDC

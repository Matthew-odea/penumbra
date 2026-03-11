# Sprint 4: The Sentinel (Dashboard & Alerts)

**Goal:** Build a professional dashboard to visualize signals and connect real-time data.

**Duration:** ~6-8 hours  
**Depends on:** Sprint 3 (signals with AI reasoning in Supabase)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Next.js App Router                       │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │  Signal Feed  │  │  Market View │  │  Wallet Profiler │  │
│  │  (real-time)  │  │  (drill-down)│  │   (drill-down)   │  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────────┘  │
│         │                 │                  │               │
│  ┌──────┴─────────────────┴──────────────────┴───────────┐  │
│  │              Supabase Client (Realtime + REST)         │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                 Tremor v3 Components                   │   │
│  │  BarChart · AreaChart · Table · Badge · Card · Metric │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────┐      ┌──────────────────────────┐
│  Supabase        │      │  FastAPI (optional)       │
│  - signals       │      │  - /api/signals           │
│  - wallets       │      │  - /api/markets           │
│  - Realtime      │      │  - /api/budget            │
└──────────────────┘      └──────────────────────────┘
```

## Tasks

### 4.1 — Next.js Project Setup (`dashboard/`)

- [ ] `npx create-next-app@latest dashboard --typescript --tailwind --app --src-dir`
- [ ] Install Tremor v3: `npm install @tremor/react`
- [ ] Install Supabase client: `npm install @supabase/supabase-js`
- [ ] Configure environment variables in `.env.local`
- [ ] Set up basic layout: sidebar nav + main content area

### 4.2 — Signal Feed Page (`dashboard/src/app/page.tsx`)

The main dashboard view — a real-time feed of flagged signals.

**Components:**
- **Summary Cards** (top row):
  - Total signals today
  - High-suspicion count (≥80)
  - Bedrock budget remaining (Tier 1 / Tier 2)
  - Active markets monitored
- **Signal Table** (main area):
  - Columns: Time, Market, Side, Size, Price, Z-Score, Suspicion, Reasoning
  - Color-coded by suspicion level (red ≥80, orange 60-79, yellow 30-59)
  - Clickable rows → drill-down to market or wallet view
  - Real-time updates via Supabase Realtime subscription
- **Volume Chart** (sidebar or bottom):
  - 24h volume bar chart per category (Biotech, Politics, etc.)
  - Overlaid with signal markers

**Acceptance criteria:**
- [ ] Page loads and displays signals from Supabase
- [ ] New signals appear automatically (no refresh needed)
- [ ] Suspicion score badge with color coding
- [ ] Responsive layout (works on desktop and tablet)
- [ ] Loading and empty states handled

### 4.3 — Market Drill-Down Page (`dashboard/src/app/market/[id]/page.tsx`)

Deep dive into a specific market's signals and volume history.

**Components:**
- Market question title + category badge
- Price probability chart (area chart over time)
- Volume timeline (bar chart with anomaly markers)
- Signal history table for this market
- Liquidity indicator

**Acceptance criteria:**
- [ ] Shows all signals for a specific market
- [ ] Volume chart highlights anomaly periods
- [ ] Links back to signal feed

### 4.4 — Wallet Profiler Page (`dashboard/src/app/wallet/[address]/page.tsx`)

Examine a specific wallet's trading history and win rate.

**Components:**
- Wallet address (truncated) + label (if whitelisted)
- Win rate donut chart (overall + per category)
- Trade history table (all trades, not just signals)
- Funding timeline (when was wallet funded)
- Risk indicators

**Acceptance criteria:**
- [ ] Shows wallet performance data from Supabase
- [ ] Win rate displayed per category
- [ ] Trade history sortable by date, size, market
- [ ] Whitelist badge if applicable

### 4.5 — FastAPI Gateway (`sentinel/api/main.py`)

Optional REST API for serving data that isn't directly in Supabase (e.g., DuckDB analytics).

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Penumbra API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/signals")
async def get_signals(limit: int = 50, min_score: int = 0):
    """Get recent signals, optionally filtered by minimum suspicion score."""
    ...

@app.get("/api/markets/{market_id}/volume")
async def get_market_volume(market_id: str, hours: int = 24):
    """Get hourly volume data for a market."""
    ...

@app.get("/api/budget")
async def get_budget_status():
    """Get current Bedrock budget status."""
    ...

@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}
```

**Acceptance criteria:**
- [ ] CORS configured for dashboard origin
- [ ] `/api/signals` returns paginated signal list
- [ ] `/api/budget` returns Tier 1 + Tier 2 usage
- [ ] `/api/health` responds with 200

### 4.6 — Alert Delivery (TBD)

> **TODO:** Decide on an alert delivery mechanism (Telegram, Slack, email, webhook, etc.) and implement here.

**Acceptance criteria:**
- [ ] High-suspicion signals (≥80) delivered within 10 seconds
- [ ] Throttled: max 1 alert per market per 15 minutes
- [ ] `--test` flag sends a test alert to verify setup

---

## Definition of Done

- [ ] Dashboard accessible at `http://localhost:3000`
- [ ] Signal feed shows real-time updates
- [ ] Market and wallet drill-down pages functional
- [ ] Alerts fire for test signals with score ≥ 80
- [ ] FastAPI health endpoint returns 200

## Testing

| Test | Type | Command |
|------|------|---------|
| API endpoints | Unit | `pytest tests/api/test_endpoints.py` |
| Alert formatting | Unit | `pytest tests/alerts/test_alerts.py` |
| Dashboard build | Build | `cd dashboard && npm run build` |
| Full stack (local) | Smoke | `docker compose up` → visit localhost:3000 |

## Estimated Cost

- Next.js: Free (self-hosted or Vercel free tier)
- Supabase: Free tier
- FastAPI: Runs alongside Python engine, no extra cost

## Deployment Options

| Option | Cost | Complexity | Best For |
|--------|------|-----------|----------|
| **Local** | $0 | Low | Development, personal use |
| **$5 VPS** (Hetzner/DigitalOcean) | $5/mo | Medium | Always-on monitoring |
| **Vercel + Railway** | Free/$5 | Low | Dashboard on Vercel, Python on Railway |
| **Docker Compose on VPS** | $5/mo | Medium | Everything in one place |

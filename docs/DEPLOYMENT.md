# Penumbra — Deployment Guide

Single-container deployment: pipeline + API + dashboard all on one cheap VPS.

---

## Prerequisites

| What | Where to get it |
|---|---|
| VPS (2 GB RAM, 1 vCPU, 20 GB disk) | [Hetzner CX22](https://www.hetzner.com/cloud/) (~€4/mo) or [AWS Lightsail](https://aws.amazon.com/lightsail/) ($5/mo) |
| Domain (optional) | Any registrar — point an A record to your VPS IP |
| AWS credentials | IAM user with `bedrock:InvokeModel` permission |
| Polymarket API keys | Run `python scripts/setup_l2.py` locally first |
| Tavily API key | [tavily.com](https://tavily.com) (free tier works) |

---

## Step 1 — Provision the VPS

```bash
# SSH into your fresh Ubuntu 22.04+ server
ssh root@YOUR_SERVER_IP

# Update & install Docker
apt update && apt upgrade -y
curl -fsSL https://get.docker.com | sh
apt install -y docker-compose-plugin

# Create a non-root user (optional but recommended)
adduser penumbra
usermod -aG docker penumbra
su - penumbra
```

## Step 2 — Clone the repo

```bash
git clone https://github.com/YOUR_USER/penumbra.git
cd penumbra
```

## Step 3 — Configure environment

```bash
cp .env.example .env
nano .env
```

Fill in **all** required values. The critical ones:

```dotenv
# Polymarket
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...

# AWS Bedrock
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1

# Tavily
TAVILY_API_KEY=tvly-...

# Production settings (already set in example)
BEDROCK_TIER1_DAILY_LIMIT=5000
BEDROCK_TIER2_DAILY_LIMIT=0
JUDGE_MAX_WORKERS=8
LOG_LEVEL=INFO
```

## Step 4 — Build & start

```bash
docker compose up -d --build
```

This builds a multi-stage image (Node → dashboard, Python → backend) and starts:
- **Ingester** — WebSocket connection to Polymarket
- **Scanner** — Anomaly detection on incoming trades
- **Judge** — 8-worker LLM classification pipeline
- **API + Dashboard** — served on port 8000

Verify it's running:

```bash
# Check container status
docker compose ps

# Check health endpoint
curl http://localhost:8000/api/health

# Follow logs
docker compose logs -f
```

The dashboard is now accessible at `http://YOUR_SERVER_IP:8000`.

## Step 5 — Set up HTTPS (optional but recommended)

Use Caddy as a reverse proxy — it handles TLS certificates automatically.

```bash
# Install Caddy
apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```

Create `/etc/caddy/Caddyfile`:

```
penumbra.yourdomain.com {
    reverse_proxy localhost:8000
}
```

```bash
systemctl restart caddy
```

Done — Caddy auto-provisions a Let's Encrypt cert. Dashboard is at `https://penumbra.yourdomain.com`.

> **Note:** Update `DASHBOARD_ORIGIN` in `.env` to `https://penumbra.yourdomain.com` if using a domain.

## Step 6 — Verify everything works

```bash
# Health check
curl https://penumbra.yourdomain.com/api/health
# → {"status":"ok","db":"connected","uptime_seconds":...}

# Check budget
curl https://penumbra.yourdomain.com/api/budget
# → {"tier1":{"calls_used":0,"calls_limit":5000},...}

# Check metrics
curl https://penumbra.yourdomain.com/api/metrics/overview
# → {"funnel":{...},"classification":{...},...}

# Open dashboard in browser
open https://penumbra.yourdomain.com
```

---

## Operations

### View logs

```bash
docker compose logs -f              # All logs
docker compose logs -f --tail 100   # Last 100 lines
```

### Restart

```bash
docker compose restart
```

### Update to latest code

```bash
git pull
docker compose up -d --build
```

### Backup DuckDB

```bash
# The DB lives in a named Docker volume
docker compose exec sentinel cp /app/data/sentinel.duckdb /app/data/backup.duckdb
docker cp $(docker compose ps -q sentinel):/app/data/backup.duckdb ./backup-$(date +%F).duckdb
```

### Monitor disk / memory

```bash
docker stats                        # Live resource usage
df -h                               # Disk space
```

---

## Architecture (Production)

```
                    ┌──────────────────────────────────────┐
                    │          Single Container             │
                    │                                      │
  :8000 ───────────▶│  FastAPI ──┬── /api/*  (REST API)    │
                    │           └── /*      (Dashboard)    │
                    │                                      │
                    │  Ingester ── Scanner ── Judge (×8)   │
                    │           │                          │
                    │           ▼                          │
                    │       DuckDB  (/app/data/)           │
                    └──────────────────────────────────────┘
                                      │
                           Docker volume (penumbra-data)
```

### Cost estimate

| Item | Monthly |
|---|---|
| Hetzner CX22 (2 vCPU / 4 GB) | €4.35 |
| AWS Bedrock Nova Lite (5k calls/day) | ~$3–8 |
| Domain (optional) | ~$1 |
| **Total** | **~$8–14/mo** |

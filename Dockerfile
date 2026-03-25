# ── Stage 1: Build dashboard ─────────────────────────────────────────────────
FROM node:20-slim AS dashboard-build
WORKDIR /app/dashboard
COPY dashboard/package.json dashboard/package-lock.json* ./
RUN npm ci --ignore-scripts
COPY dashboard/ .
RUN npm run build

# ── Stage 2: Python runtime ─────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir -e .

# Copy source
COPY sentinel/ sentinel/
COPY scripts/ scripts/

# Copy built dashboard
COPY --from=dashboard-build /app/dashboard/dist dashboard/dist

# Create data directory
RUN mkdir -p data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1

CMD ["python", "-m", "sentinel", "--with-api"]

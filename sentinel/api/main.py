"""Penumbra API — FastAPI application."""

from __future__ import annotations

import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from sentinel.api.deps import close_db, get_db
from sentinel.api.routes import budget, health, markets, metrics, signals, wallets
from sentinel.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: warm the DuckDB connection
    get_db()
    yield
    # Shutdown: close it
    close_db()


app = FastAPI(
    title="Penumbra",
    version="0.4.0",
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.dashboard_origin],
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api")
app.include_router(signals.router, prefix="/api")
app.include_router(markets.router, prefix="/api")
app.include_router(wallets.router, prefix="/api")
app.include_router(budget.router, prefix="/api")
app.include_router(metrics.router, prefix="/api")

# ── Serve the built React dashboard (production) ────────────────────────────
_DASHBOARD_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "dashboard" / "dist"

if _DASHBOARD_DIR.is_dir():
    # Serve static assets (JS, CSS, images)
    app.mount("/assets", StaticFiles(directory=_DASHBOARD_DIR / "assets"), name="assets")

    # Catch-all: serve index.html for any non-API route (SPA client-side routing)
    @app.get("/{full_path:path}")
    async def _spa_fallback(full_path: str):
        return FileResponse(_DASHBOARD_DIR / "index.html")

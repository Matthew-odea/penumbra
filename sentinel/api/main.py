"""Penumbra API — FastAPI application."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sentinel.api.deps import close_db, get_db
from sentinel.api.routes import budget, health, markets, signals, wallets
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

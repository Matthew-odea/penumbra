"""Budget status endpoint — Bedrock market scoring usage tracking."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from sentinel.api.deps import get_db
from sentinel.config import settings

router = APIRouter(tags=["budget"])


@router.get("/budget")
async def get_budget() -> dict:
    """Current day's Bedrock budget usage for market attractiveness scoring."""
    db = get_db()
    today = datetime.now(tz=UTC).date().isoformat()

    rows = db.execute(
        "SELECT tier, calls_used, calls_limit FROM llm_budget WHERE date = ?",
        [today],
    ).fetchall()

    tiers = {r[0]: {"calls_used": r[1], "calls_limit": r[2]} for r in rows}

    if "market_scoring" not in tiers:
        tiers["market_scoring"] = {
            "calls_used": 0,
            "calls_limit": settings.bedrock_market_scoring_daily_limit,
        }

    return {
        "date": today,
        "market_scoring": tiers["market_scoring"],
    }

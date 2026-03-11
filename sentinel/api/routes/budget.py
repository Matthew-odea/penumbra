"""Budget status endpoint — Bedrock LLM usage tracking."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter

from sentinel.api.deps import get_db

router = APIRouter(tags=["budget"])


@router.get("/budget")
async def get_budget() -> dict:
    """Current day's Bedrock budget usage for both tiers."""
    db = get_db()
    today = date.today().isoformat()

    rows = db.execute(
        "SELECT tier, calls_used, calls_limit FROM llm_budget WHERE date = ?",
        [today],
    ).fetchall()

    tiers = {}
    for r in rows:
        tiers[r[0]] = {"calls_used": r[1], "calls_limit": r[2]}

    # Fill defaults if no rows yet today
    from sentinel.config import settings

    if "tier1" not in tiers:
        tiers["tier1"] = {"calls_used": 0, "calls_limit": settings.bedrock_tier1_daily_limit}
    if "tier2" not in tiers:
        tiers["tier2"] = {"calls_used": 0, "calls_limit": settings.bedrock_tier2_daily_limit}

    return {
        "date": today,
        "tier1": tiers["tier1"],
        "tier2": tiers["tier2"],
    }

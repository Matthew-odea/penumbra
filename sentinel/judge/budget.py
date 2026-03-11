"""Budget manager for LLM call tracking.

Enforces daily call limits for Bedrock Tier 1 (Llama 3) and Tier 2 (Claude)
to keep costs predictable. Tracks usage in the DuckDB ``llm_budget`` table
and resets at midnight UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import structlog

from sentinel.config import settings

logger = structlog.get_logger()

_TIER_LIMITS: dict[str, int] = {
    "tier1": settings.bedrock_tier1_daily_limit,
    "tier2": settings.bedrock_tier2_daily_limit,
}


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    """Snapshot of a single tier's budget for today."""

    tier: str
    calls_used: int
    calls_limit: int

    @property
    def remaining(self) -> int:
        return max(0, self.calls_limit - self.calls_used)

    @property
    def is_exhausted(self) -> bool:
        return self.calls_used >= self.calls_limit


class BudgetManager:
    """Tracks daily Bedrock call budgets in DuckDB.

    The ``llm_budget`` table (created by ``sentinel.db.init``) has a
    composite PK of ``(date, tier)``.  A new row is lazily inserted on the
    first call each day, and the ``calls_used`` column is incremented via
    ``record_call()``.
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    # ── Public API ──────────────────────────────────────────────────────

    def can_call(self, tier: str) -> bool:
        """Return *True* if budget remains for *tier* today."""
        status = self._status_for(tier)
        return not status.is_exhausted

    def record_call(self, tier: str) -> None:
        """Increment the call counter for *tier* today."""
        today = self._today()
        limit = _TIER_LIMITS.get(tier, 0)

        # Upsert: insert if missing, otherwise increment
        self.db.execute(
            """
            INSERT INTO llm_budget (date, tier, calls_used, calls_limit)
            VALUES (?, ?, 1, ?)
            ON CONFLICT (date, tier) DO UPDATE
            SET calls_used = llm_budget.calls_used + 1
            """,
            [today, tier, limit],
        )
        logger.debug("Budget call recorded", tier=tier, date=str(today))

    def get_status(self) -> dict[str, BudgetStatus]:
        """Return current budget status for all tiers."""
        return {tier: self._status_for(tier) for tier in _TIER_LIMITS}

    # ── Internal helpers ────────────────────────────────────────────────

    @staticmethod
    def _today() -> date:
        return datetime.now(tz=UTC).date()

    def _status_for(self, tier: str) -> BudgetStatus:
        today = self._today()
        limit = _TIER_LIMITS.get(tier, 0)

        row = self.db.execute(
            "SELECT calls_used FROM llm_budget WHERE date = ? AND tier = ?",
            [today, tier],
        ).fetchone()

        used = row[0] if row else 0
        return BudgetStatus(tier=tier, calls_used=used, calls_limit=limit)

"""Shared fixtures for API tests."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import duckdb
import pytest
from httpx import ASGITransport, AsyncClient

from sentinel.api import deps
from sentinel.db.init import SCHEMA_SQL


@pytest.fixture()
def test_db():
    """In-memory DuckDB with schema applied and test data seeded."""
    conn = duckdb.connect(":memory:")
    conn.execute("SET TimeZone = 'UTC'")
    conn.execute(SCHEMA_SQL)
    _seed(conn)
    return conn


def _seed(conn: duckdb.DuckDBPyConnection) -> None:
    """Seed the database with realistic test data."""
    now = datetime.utcnow()

    # Markets
    conn.execute(
        """
        INSERT INTO markets VALUES
            ('mkt-001', 'Will Bitcoin exceed $100k by June?', 'btc-100k',
             'Crypto', ?, 5500000.0, 1200000.0, TRUE, FALSE, NULL, ?),
            ('mkt-002', 'Will the Fed cut rates in Q3?', 'fed-cut-q3',
             'Politics', ?, 8200000.0, 3100000.0, TRUE, FALSE, NULL, ?),
            ('mkt-003', 'Resolved test market', 'resolved-mkt',
             'Science', ?, 200000.0, 50000.0, FALSE, TRUE, 1.0, ?)
        """,
        [
            now + timedelta(days=60), now,
            now + timedelta(days=90), now,
            now - timedelta(days=10), now,
        ],
    )

    # Trades — spread across markets and wallets
    base_time = now - timedelta(hours=6)
    trades = []
    for i in range(20):
        trades.append((
            f"trade-{i:03d}",
            "mkt-001" if i < 10 else "mkt-002",
            f"asset-{i % 3}",
            "0xAlpha" if i % 3 == 0 else ("0xBravo" if i % 3 == 1 else "0xCharlie"),
            "BUY" if i % 2 == 0 else "SELL",
            Decimal("0.65") + Decimal("0.01") * i,
            Decimal("1000") + Decimal("500") * i,
            base_time + timedelta(minutes=i * 15),
            f"0xhash{i:03d}",
            now,
        ))
    conn.executemany(
        "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ws', ?)",
        trades,
    )

    # Also add 5 resolved-market trades for wallet performance view
    for i in range(6):
        conn.execute(
            "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ws', ?)",
            [
                f"trade-res-{i:03d}", "mkt-003", "asset-res",
                "0xAlpha", "BUY", Decimal("0.70"),
                Decimal("2000"), now - timedelta(days=5, hours=i),
                f"0xresolved{i}", now,
            ],
        )

    # Signals — some with high scores
    for i in range(8):
        sid = f"sig-{i:03d}"
        tid = f"trade-{i:03d}"
        mid = "mkt-001" if i < 5 else "mkt-002"
        wallet = "0xAlpha" if i % 2 == 0 else "0xBravo"
        score = 30 + i * 10  # 30, 40, 50, 60, 70, 80, 90, 100

        conn.execute(
            """
            INSERT INTO signals VALUES
                (?, ?, ?, ?, 'BUY', 0.65, 5000.0, ?,
                 3.5, 2.1, 0.03, 0.72, 15, FALSE, FALSE, NULL, ?, ?)
            """,
            [sid, tid, mid, wallet, base_time + timedelta(minutes=i * 15), score, now],
        )

    # Signal reasoning — only for some signals
    for i in range(6):
        sid = f"sig-{i:03d}"
        tid = f"trade-{i:03d}"
        classification = "INFORMED" if i >= 3 else "NOISE"
        suspicion = 30 + i * 15  # 30, 45, 60, 75, 90, 105→capped

        conn.execute(
            """
            INSERT INTO signal_reasoning VALUES
                (?, ?, ?, ?, ?, 'Test reasoning text', 'Evidence item',
                 '["headline 1"]', 'nova-lite', 'nova-pro', 150, 300, ?)
            """,
            [sid, tid, classification, 60 + i * 5, min(suspicion, 100), now],
        )

    # LLM Budget — today's usage
    today = now.date()
    conn.execute(
        "INSERT INTO llm_budget VALUES (?, 'tier1', 42, 200)",
        [today],
    )
    conn.execute(
        "INSERT INTO llm_budget VALUES (?, 'tier2', 7, 30)",
        [today],
    )


@pytest.fixture()
def client(test_db):
    """Async httpx client wired to the FastAPI app with test DB."""
    # Monkey-patch deps to use our test connection
    original_get_db = deps.get_db

    def _test_get_db(db_path=None):
        return test_db

    deps.get_db = _test_get_db
    deps._conn = test_db

    from sentinel.api.main import app

    transport = ASGITransport(app=app)

    yield AsyncClient(transport=transport, base_url="http://test")

    # Restore
    deps.get_db = original_get_db
    deps._conn = None

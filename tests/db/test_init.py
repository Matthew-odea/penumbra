"""Tests for DuckDB schema initialization."""

from pathlib import Path

import duckdb
import pytest

from sentinel.db.init import SCHEMA_SQL, init_schema


class TestSchemaInit:
    """Test that the DuckDB schema initializes correctly."""

    def test_schema_creates_tables(self, tmp_path: Path) -> None:
        """All expected tables are created."""
        db_path = tmp_path / "test.duckdb"
        conn = init_schema(db_path)

        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name"
        ).fetchall()
        table_names = [t[0] for t in tables]

        assert "markets" in table_names
        assert "trades" in table_names
        assert "signals" in table_names
        assert "signal_reasoning" in table_names
        assert "llm_budget" in table_names
        conn.close()

    def test_schema_is_idempotent(self, tmp_path: Path) -> None:
        """Running init_schema twice doesn't error."""
        db_path = tmp_path / "test.duckdb"
        conn1 = init_schema(db_path)
        conn1.close()
        conn2 = init_schema(db_path)  # Should not raise
        conn2.close()

    def test_trades_table_schema(self, tmp_path: Path) -> None:
        """Trades table has the expected columns."""
        db_path = tmp_path / "test.duckdb"
        conn = init_schema(db_path)

        columns = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'trades' ORDER BY ordinal_position"
        ).fetchall()
        col_names = [c[0] for c in columns]

        assert "trade_id" in col_names
        assert "market_id" in col_names
        assert "wallet" in col_names
        assert "price" in col_names
        assert "size_usd" in col_names
        assert "timestamp" in col_names
        conn.close()

    def test_views_are_created(self, tmp_path: Path) -> None:
        """Analytical views are queryable (even if empty)."""
        db_path = tmp_path / "test.duckdb"
        conn = init_schema(db_path)

        # Views should return empty results, not error
        result = conn.execute("SELECT * FROM v_hourly_volume").fetchall()
        assert result == []

        result = conn.execute("SELECT * FROM v_volume_anomalies").fetchall()
        assert result == []

        result = conn.execute("SELECT * FROM v_wallet_performance").fetchall()
        assert result == []
        conn.close()

    def test_can_insert_and_query_trade(self, tmp_path: Path) -> None:
        """Basic insert and query works on the trades table."""
        db_path = tmp_path / "test.duckdb"
        conn = init_schema(db_path)

        conn.execute("""
            INSERT INTO trades (trade_id, market_id, asset_id, wallet, side, price, size_usd, timestamp)
            VALUES ('t1', 'm1', 'a1', '0xABC', 'BUY', 0.73, 1500.00, '2026-03-11 12:00:00')
        """)

        result = conn.execute("SELECT trade_id, wallet, size_usd FROM trades").fetchone()
        assert result[0] == "t1"
        assert result[1] == "0xABC"
        assert float(result[2]) == 1500.00
        conn.close()

"""DuckDB connection dependency for FastAPI routes."""

from __future__ import annotations

from pathlib import Path
from typing import Generator

import duckdb

from sentinel.config import settings
from sentinel.db.init import init_schema


_conn: duckdb.DuckDBPyConnection | None = None


def get_db(db_path: Path | None = None) -> duckdb.DuckDBPyConnection:
    """Return (and lazily initialise) a shared DuckDB connection."""
    global _conn
    if _conn is None:
        _conn = init_schema(db_path or settings.duckdb_path)
    return _conn


def close_db() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None

"""DuckDB connection dependency for FastAPI routes."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb

from sentinel.config import settings
from sentinel.db.init import init_schema


def to_iso(dt: datetime) -> str:
    """Convert a datetime to an ISO 8601 string ending in Z.

    DuckDB returns timezone-aware datetimes whose ``.isoformat()`` ends with
    ``+00:00``.  Naively appending ``Z`` creates ``+00:00Z`` which JavaScript's
    ``new Date()`` rejects as ``Invalid Date``.
    """
    s = dt.isoformat()
    if s.endswith("+00:00"):
        return s[:-6] + "Z"
    return s + "Z" if not s.endswith("Z") else s

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

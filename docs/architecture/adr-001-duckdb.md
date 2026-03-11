# ADR-001: DuckDB as Local OLAP Engine

**Status:** Accepted  
**Date:** 2026-03-11  
**Deciders:** Core team

## Context

We need an analytical database to:
1. Store raw trade data from Polymarket (millions of rows over time)
2. Run windowed aggregations (Z-scores, rolling volumes, win rates)
3. Operate on a $5/mo VPS or a developer laptop with zero external dependencies

### Options Considered

| Option | Pros | Cons |
|--------|------|------|
| **DuckDB** | In-process, zero-ops, columnar, fast OLAP, SQL-native window functions | Single-writer (no concurrent writes from multiple processes), no built-in replication |
| **TimescaleDB** | Time-series native, PostgreSQL ecosystem | Requires a running Postgres server, memory-hungry |
| **ClickHouse** | Extremely fast at scale | Heavy operational burden, overkill for <100M rows |
| **SQLite** | Universal, embedded | Row-oriented, slow on analytical queries, no native window functions |

## Decision

**Use DuckDB** as the primary analytical store for trade data, signal computation, and intermediate results.

## Rationale

- **Zero ops**: DuckDB runs in-process. No server to manage, no ports to open, no auth to configure. A single `.duckdb` file holds everything.
- **Columnar + vectorized**: Analytical queries (GROUP BY market, window functions for Z-scores) run 10-100x faster than SQLite.
- **SQL-native**: All statistical logic (Z-scores, percentiles, MAD) can be expressed as SQL views, making them testable and versionable.
- **Memory-mapped**: Can handle datasets larger than RAM by spilling to disk.

## Consequences

- **Single-writer constraint**: The ingester and scanner cannot write simultaneously from separate processes. We solve this with a pipeline architecture: ingester writes → scanner reads → scanner writes signals to a separate table. If we need concurrency later, we use WAL mode or queue writes through a single coordinator process.
- **No replication**: If the VPS dies, we lose data. Mitigation: nightly backup of the `.duckdb` file to S3/Supabase storage.
- **Schema migrations**: We manage these with versioned SQL files in `sentinel/db/migrations/`, applied in order at startup.

## References

- [DuckDB documentation](https://duckdb.org/docs/)
- [DuckDB Python API](https://duckdb.org/docs/api/python/overview)

"""Sprint 1 — Data ingestion from Polymarket.

Components:
    - ``listener``  — WebSocket trade stream consumer
    - ``markets``   — REST market metadata sync
    - ``writer``    — DuckDB batch writer with time/count flush
    - ``models``    — Trade dataclass and WS/REST parsers

Run with::

    python -m sentinel.ingester              # live mode
    python -m sentinel.ingester --dry-run    # stdout JSON
    python -m sentinel.ingester --timeout 60 # smoke test
"""

from sentinel.ingester.models import Trade, parse_rest_trade, parse_ws_trade

__all__ = ["Trade", "parse_rest_trade", "parse_ws_trade"]

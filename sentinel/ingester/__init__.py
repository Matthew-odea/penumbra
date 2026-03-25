"""Data ingestion from Polymarket.

Components:
    - ``listener``  — WebSocket order-book event consumer
    - ``markets``   — REST market metadata sync + active asset discovery
    - ``writer``    — DuckDB batch writer with time/count flush
    - ``models``    — Trade / BookEvent dataclasses and WS/REST parsers

Run with::

    python -m sentinel.ingester              # live mode (auto-discovers assets)
    python -m sentinel.ingester --dry-run    # stdout JSON
    python -m sentinel.ingester --timeout 60 # smoke test
    python -m sentinel.ingester --assets <token_ids>  # specific assets
"""

from sentinel.ingester.models import (
    BookEvent,
    IngesterEvent,
    Trade,
    parse_price_changes,
    parse_rest_trade,
    parse_ws_trade,
)

__all__ = [
    "BookEvent",
    "IngesterEvent",
    "Trade",
    "parse_price_changes",
    "parse_rest_trade",
    "parse_ws_trade",
]

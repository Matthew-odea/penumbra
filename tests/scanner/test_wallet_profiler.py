"""Tests for sentinel.scanner.wallet_profiler — wallet win-rate computation."""


import duckdb

from sentinel.scanner.wallet_profiler import (
    get_top_wallets,
    get_wallet_profile,
    get_whitelisted_wallets,
    is_whitelisted,
)


def _init_db() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE markets (
            market_id VARCHAR PRIMARY KEY,
            question VARCHAR,
            slug VARCHAR,
            category VARCHAR,
            end_date TIMESTAMP,
            volume_usd DECIMAL(18,6),
            liquidity_usd DECIMAL(18,6),
            active BOOLEAN DEFAULT TRUE,
            resolved BOOLEAN DEFAULT FALSE,
            resolved_price DECIMAL(10,6),
            last_synced TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE trades (
            trade_id VARCHAR PRIMARY KEY,
            market_id VARCHAR NOT NULL,
            asset_id VARCHAR NOT NULL,
            wallet VARCHAR NOT NULL,
            side VARCHAR NOT NULL,
            price DECIMAL(10,6),
            size_usd DECIMAL(18,6),
            timestamp TIMESTAMP NOT NULL,
            tx_hash VARCHAR,
            source VARCHAR DEFAULT 'ws',
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE OR REPLACE VIEW v_wallet_performance AS
        SELECT
            t.wallet,
            COUNT(*) AS total_resolved_trades,
            SUM(CASE
                WHEN (t.side = 'BUY' AND m.resolved_price >= 0.95) OR
                     (t.side = 'SELL' AND m.resolved_price <= 0.05)
                THEN 1 ELSE 0
            END) AS wins,
            CASE
                WHEN COUNT(*) > 0
                THEN SUM(CASE
                    WHEN (t.side = 'BUY' AND m.resolved_price >= 0.95) OR
                         (t.side = 'SELL' AND m.resolved_price <= 0.05)
                    THEN 1 ELSE 0
                END)::FLOAT / COUNT(*)
                ELSE 0
            END AS win_rate
        FROM trades t
        JOIN markets m ON t.market_id = m.market_id
        WHERE m.resolved = TRUE
        GROUP BY t.wallet
        HAVING COUNT(*) >= 5
    """)
    return conn


def _seed_resolved_market(conn: duckdb.DuckDBPyConnection, market_id: str, resolved_price: float):
    """Insert a resolved market."""
    conn.execute(
        "INSERT INTO markets (market_id, question, resolved, resolved_price) VALUES (?, ?, TRUE, ?)",
        [market_id, "Resolved?", resolved_price],
    )


def _seed_trade(conn: duckdb.DuckDBPyConnection, trade_id: str, market_id: str, wallet: str, side: str = "BUY"):
    conn.execute(
        "INSERT INTO trades VALUES (?, ?, 'a1', ?, ?, 0.5, 100.0, CURRENT_TIMESTAMP, NULL, 'ws', CURRENT_TIMESTAMP)",
        [trade_id, market_id, wallet, side],
    )


class TestWalletProfiler:
    def test_no_resolved_trades(self):
        """Wallet with no resolved market trades → None."""
        conn = _init_db()
        assert get_wallet_profile(conn, "0xnobody") is None

    def test_below_min_trades(self):
        """Wallet with < 5 resolved trades → filtered by HAVING clause → None."""
        conn = _init_db()
        _seed_resolved_market(conn, "m1", 1.0)
        for i in range(4):  # Only 4 trades, need 5
            _seed_trade(conn, f"t{i}", "m1", "0xfew", "BUY")
        assert get_wallet_profile(conn, "0xfew") is None

    def test_exact_min_trades(self):
        """Wallet with exactly 5 resolved trades → should appear."""
        conn = _init_db()
        _seed_resolved_market(conn, "m1", 1.0)  # YES resolved
        for i in range(5):
            _seed_trade(conn, f"t{i}", "m1", "0xexact", "BUY")
        profile = get_wallet_profile(conn, "0xexact")
        assert profile is not None
        assert profile.total_resolved_trades == 5

    def test_perfect_win_rate(self):
        """All BUY trades on YES-resolved markets → 100% win rate."""
        conn = _init_db()
        _seed_resolved_market(conn, "m1", 1.0)
        for i in range(10):
            _seed_trade(conn, f"t{i}", "m1", "0xperfect", "BUY")
        profile = get_wallet_profile(conn, "0xperfect")
        assert profile is not None
        assert profile.win_rate == 1.0
        assert profile.wins == 10

    def test_zero_win_rate(self):
        """All BUY trades on NO-resolved markets → 0% win rate."""
        conn = _init_db()
        _seed_resolved_market(conn, "m1", 0.0)  # NO resolved
        for i in range(6):
            _seed_trade(conn, f"t{i}", "m1", "0xloser", "BUY")
        profile = get_wallet_profile(conn, "0xloser")
        assert profile is not None
        assert profile.win_rate == 0.0

    def test_sell_side_wins(self):
        """SELL trades win when market resolves NO (price ≤ 0.05)."""
        conn = _init_db()
        _seed_resolved_market(conn, "m1", 0.0)
        for i in range(7):
            _seed_trade(conn, f"t{i}", "m1", "0xseller", "SELL")
        profile = get_wallet_profile(conn, "0xseller")
        assert profile is not None
        assert profile.win_rate == 1.0

    def test_whitelisted_wallet(self):
        """Wallet with ≥ 65% win rate and ≥ 20 trades → whitelisted."""
        conn = _init_db()
        _seed_resolved_market(conn, "m_yes", 1.0)
        _seed_resolved_market(conn, "m_no", 0.0)
        wallet = "0xwhale"
        # 15 wins (BUY on YES)
        for i in range(15):
            _seed_trade(conn, f"win{i}", "m_yes", wallet, "BUY")
        # 5 losses (BUY on NO)
        for i in range(5):
            _seed_trade(conn, f"loss{i}", "m_no", wallet, "BUY")
        profile = get_wallet_profile(conn, wallet)
        assert profile is not None
        assert profile.win_rate == 0.75
        assert profile.total_resolved_trades == 20
        assert profile.is_whitelisted is True

    def test_not_whitelisted_low_volume(self):
        """High win rate but < 20 trades → not whitelisted."""
        conn = _init_db()
        _seed_resolved_market(conn, "m1", 1.0)
        for i in range(6):
            _seed_trade(conn, f"t{i}", "m1", "0xsmall", "BUY")
        profile = get_wallet_profile(conn, "0xsmall")
        assert profile is not None
        assert profile.win_rate == 1.0
        assert profile.is_whitelisted is False  # Only 6 trades < 20

    def test_get_whitelisted_wallets(self):
        """Aggregate query for wallets meeting whitelist criteria."""
        conn = _init_db()
        _seed_resolved_market(conn, "m_yes", 1.0)
        _seed_resolved_market(conn, "m_no", 0.0)
        # One whitelisted wallet (20 wins out of 25)
        for i in range(20):
            _seed_trade(conn, f"wl_w{i}", "m_yes", "0xwhitelist", "BUY")
        for i in range(5):
            _seed_trade(conn, f"wl_l{i}", "m_no", "0xwhitelist", "BUY")
        # One non-whitelisted wallet (6 trades)
        for i in range(6):
            _seed_trade(conn, f"nwl{i}", "m_yes", "0xnonwl", "BUY")

        wl = get_whitelisted_wallets(conn)
        wallets = [w.wallet for w in wl]
        assert "0xwhitelist" in wallets
        assert "0xnonwl" not in wallets

    def test_is_whitelisted_helper(self):
        conn = _init_db()
        assert is_whitelisted(conn, "0xnobody") is False

    def test_get_top_wallets(self):
        conn = _init_db()
        _seed_resolved_market(conn, "m1", 1.0)
        for i in range(10):
            _seed_trade(conn, f"t{i}", "m1", "0xtop", "BUY")
        tops = get_top_wallets(conn, min_trades=5, limit=10)
        assert len(tops) >= 1
        assert tops[0].wallet == "0xtop"

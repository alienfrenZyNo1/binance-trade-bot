"""Tests for Telegram /profit P&L reporting."""

import importlib.util
import os
import sqlite3
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "telegram_bot.py"


def load_module():
    os.environ.setdefault("TELEGRAM_CHAT_IDS", "0")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
    spec = importlib.util.spec_from_file_location("telegram_bot_profit_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_schema(conn):
    conn.execute("CREATE TABLE deposits (amount REAL)")
    conn.execute(
        """CREATE TABLE trade_history (
            id INTEGER PRIMARY KEY,
            datetime TEXT,
            selling INTEGER,
            state TEXT,
            alt_coin_id TEXT,
            alt_trade_amount REAL,
            crypto_coin_id TEXT,
            crypto_trade_amount REAL
        )"""
    )


def _insert_trade(conn, tid, dt, selling, state, alt_coin, alt_amt, crypto_amt):
    conn.execute(
        """INSERT INTO trade_history
           (id, datetime, selling, state, alt_coin_id, alt_trade_amount,
            crypto_coin_id, crypto_trade_amount)
           VALUES (?, ?, ?, ?, ?, ?, 'USDC', ?)""",
        (tid, dt, selling, state, alt_coin, alt_amt, crypto_amt),
    )


def make_profit_db(path):
    conn = sqlite3.connect(path)
    _create_schema(conn)
    conn.execute("INSERT INTO deposits(amount) VALUES (10.0)")
    _insert_trade(conn, 1, "2026-06-25 10:00:00", 0, "COMPLETE", "JUP", 10.0, 10.0)
    conn.commit()
    conn.close()


def make_profit_db_with_holds(path):
    """DB with two closed coin holds (one win, one loss) and one open position."""
    conn = sqlite3.connect(path)
    _create_schema(conn)
    conn.execute("INSERT INTO deposits(amount) VALUES (10.0)")
    # Hold 1: buy JUP $10 -> sell JUP $12.50  (+25%, win)
    _insert_trade(conn, 1, "2026-06-25 10:00:00", 0, "COMPLETE", "JUP", 10.0, 10.00)
    _insert_trade(conn, 2, "2026-06-25 14:00:00", 1, "COMPLETE", "JUP", 10.0, 12.50)
    # Hold 2: buy SOL $12.50 -> sell SOL $11.25  (-10%, loss)
    _insert_trade(conn, 3, "2026-06-25 14:00:01", 0, "COMPLETE", "SOL", 100.0, 12.50)
    _insert_trade(conn, 4, "2026-06-25 18:00:00", 1, "COMPLETE", "SOL", 100.0, 11.25)
    # Current open position: buy ADA $11.25 (flat at entry)
    _insert_trade(conn, 5, "2026-06-25 18:00:01", 0, "COMPLETE", "ADA", 100.0, 11.25)
    conn.commit()
    conn.close()


def test_cmd_profit_reports_spot_unrealized_pnl_for_open_spot_position(tmp_path, monkeypatch):
    module = load_module()
    db_path = tmp_path / "profit.db"
    make_profit_db(db_path)

    monkeypatch.setattr(module, "DB_PATH", str(db_path))
    monkeypatch.setattr(module, "get_current_coin", lambda: "JUP")
    monkeypatch.setattr(
        module,
        "get_holdings",
        lambda: [{"coin_id": "JUP", "balance": 10.0, "usd_price": 1.25}],
    )
    monkeypatch.setattr(module, "get_futures_balance", lambda: {"balance": 0.0, "available": 0.0, "pnl": 0.0})
    monkeypatch.setattr(module, "get_futures_positions", lambda: [])
    monkeypatch.setattr(module, "get_futures_realized", lambda: None)

    output = module.cmd_profit()

    assert "Unrealized total" in output
    assert "Spot unrealized" in output
    assert "$+2.50" in output
    assert "Open Spot Position" in output
    assert "JUP" in output
    assert "SPOT" in output


def test_cmd_profit_reports_meaningful_hold_returns_not_hop_pnl(tmp_path, monkeypatch):
    """Issue #13: hop round-trip P&L is structurally ~0, so win/loss/efficiency
    counted on hops is meaningless. /profit must instead report per-coin
    hold-period returns (buy price -> sell price)."""
    module = load_module()
    db_path = tmp_path / "profit_holds.db"
    make_profit_db_with_holds(db_path)

    monkeypatch.setattr(module, "DB_PATH", str(db_path))
    monkeypatch.setattr(module, "get_current_coin", lambda: "ADA")
    monkeypatch.setattr(
        module,
        "get_holdings",
        lambda: [{"coin_id": "ADA", "balance": 100.0, "usd_price": 0.1125}],
    )
    monkeypatch.setattr(module, "get_futures_balance", lambda: {"balance": 0.0, "available": 0.0, "pnl": 0.0})
    monkeypatch.setattr(module, "get_futures_positions", lambda: [])
    monkeypatch.setattr(module, "get_futures_realized", lambda: None)

    output = module.cmd_profit()

    # Meaningful hold-return section is present with correct per-hold numbers.
    assert "Closed Coin Holds" in output
    assert "Avg hold return" in output
    assert "Win rate" in output
    assert "50% (1W / 1L)" in output          # one win (JUP) + one loss (SOL)
    assert "+25.0%" in output                  # JUP hold return
    assert "-10.0%" in output                  # SOL hold return
    assert "JUP" in output                      # coin shown in recent-holds table
    assert "SOL" in output
    assert "Realized hold P&amp;L" in output            # & is HTML-escaped
    assert "$+1.25" in output                   # 2.50 - 1.25

    # Misleading hop-derived metrics must be gone.
    assert "Cash efficiency" not in output
    assert "Spot cash delta" not in output
    assert "Wins / losses / flat" not in output
    assert "Hop History" not in output
    assert "Rotation Cash Deltas" not in output


def test_cmd_profit_no_closed_holds_omits_section(tmp_path, monkeypatch):
    """With no closed holds (only an open position), the hold section is omitted
    rather than showing empty/zero stats."""
    module = load_module()
    db_path = tmp_path / "profit_open_only.db"
    make_profit_db(db_path)  # single buy, never sold

    monkeypatch.setattr(module, "DB_PATH", str(db_path))
    monkeypatch.setattr(module, "get_current_coin", lambda: "JUP")
    monkeypatch.setattr(
        module,
        "get_holdings",
        lambda: [{"coin_id": "JUP", "balance": 10.0, "usd_price": 1.25}],
    )
    monkeypatch.setattr(module, "get_futures_balance", lambda: {"balance": 0.0, "available": 0.0, "pnl": 0.0})
    monkeypatch.setattr(module, "get_futures_positions", lambda: [])
    monkeypatch.setattr(module, "get_futures_realized", lambda: None)

    output = module.cmd_profit()

    # Open position still reported; closed-hold section absent.
    assert "Open Spot Position" in output
    assert "Closed Coin Holds" not in output

"""Tests for Telegram /profit unrealized P&L reporting."""

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


def make_profit_db(path):
    conn = sqlite3.connect(path)
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
    conn.execute("INSERT INTO deposits(amount) VALUES (10.0)")
    conn.execute(
        """INSERT INTO trade_history
           (id, datetime, selling, state, alt_coin_id, alt_trade_amount, crypto_coin_id, crypto_trade_amount)
           VALUES (1, '2026-06-25 10:00:00', 0, 'COMPLETE', 'JUP', 10.0, 'USDC', 10.0)"""
    )
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

"""Tests for Telegram HTML formatting helpers."""

import subprocess
import sys

from binance_trade_bot.formatting.telegram_html import (
    format_duration,
    format_table,
    funding_flow,
    html_escape,
    kv_table,
    money,
    pct,
    pnl_emoji,
    pre_table,
    section,
    status_word,
)


def test_html_escape_escapes_telegram_html_text_but_not_quotes():
    assert html_escape('A&B <coin> "x"') == 'A&amp;B &lt;coin&gt; "x"'


def test_format_table_decimal_aligns_numeric_columns():
    rendered = format_table(
        ["COIN", "P&L"],
        [["JUP", "+1.23%"], ["AAVE", "-12.5%"], ["SOL", "0"]],
        aligns=["l", "d"],
    )

    assert rendered.splitlines() == [
        "COIN      P&L",
        "────  ───────",
        "JUP    +1.23%",
        "AAVE  -12.5% ",
        "SOL     0    ",
    ]


def test_pre_table_escapes_wraps_and_can_annotate_pnl_rows():
    rendered = pre_table(
        ["COIN", "P&L"],
        [["JUP", "+1.00%"], ["BAD<", "-2.00%"]],
        aligns=["l", "d"],
        pnl_values=[1.0, -2.0],
    )

    assert rendered.startswith("<pre>")
    assert rendered.endswith("</pre>")
    assert "🟢 JUP" in rendered
    assert "🔴 BAD&lt;" in rendered
    assert "<pre>" == rendered[:5]


def test_kv_table_and_section_are_safe_html_fragments():
    assert section("A&B <x>") == "\n<b>A&amp;B &lt;x&gt;</b>"
    rendered = kv_table([["Coin", "JUP&SOL"], ["Risk", "<low>"]])
    assert rendered.startswith("<pre>")
    assert "JUP&amp;SOL" in rendered
    assert "&lt;low&gt;" in rendered


def test_money_pct_duration_status_and_funding_helpers():
    assert money(12.345) == "$12.35"
    assert money(-1.2, signed=True) == "$-1.20"
    assert money("bad") == "$-"
    assert pct(1.234) == "+1.2%"
    assert pct(-1.234, digits=2) == "-1.23%"
    assert pct("bad") == "-"
    assert pnl_emoji(0) == "🟢"
    assert pnl_emoji(-0.1) == "🔴"
    assert pnl_emoji("bad") == "⚪"
    assert funding_flow(0.001) == "GET"
    assert funding_flow(-0.001) == "PAY"
    assert funding_flow(0) == "FLAT"
    assert funding_flow(None) == "-"
    assert status_word(True) == "OK"
    assert status_word(False) == "ERR"
    assert status_word(warn=True) == "WARN"
    assert status_word() == "INFO"
    assert format_duration(59) == "59s"
    assert format_duration(61) == "1m"
    assert format_duration(3661) == "1.0h"


def test_formatting_module_import_is_lightweight():
    code = """
import sys
from binance_trade_bot.formatting.telegram_html import pre_table
assert callable(pre_table)
assert 'socketio' not in sys.modules
assert 'eventlet' not in sys.modules
assert 'binance' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr

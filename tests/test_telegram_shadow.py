"""Tests for Telegram shadow-mode regime reporting."""

import importlib.util
import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "telegram_bot.py"


def load_module():
    os.environ.setdefault("TELEGRAM_CHAT_IDS", "0")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
    spec = importlib.util.spec_from_file_location("telegram_bot_shadow_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_regime(regime="bear", confidence=0.82):
    return {
        "regime": regime,
        "confidence": confidence,
        "score": -2.75,
        "reasons": [
            "BTC trend down: EMA20<EMA50 & -DI>+DI",
            "breadth risk-off: only 25% above EMA50",
            "futures taker flow bearish: buy/sell ratio 0.82",
            "futures storm risk: OI +9.1%",
        ],
        "metrics": {
            "breadth": {
                "valid_coins": 4,
                "above_ema50_pct": 0.25,
                "advancers_24h_pct": 0.25,
                "median_ret_24h": -2.4,
                "median_vol_24h": 5.2,
            },
            "futures": {
                "valid_symbols": 3,
                "avg_funding_pct": -0.011,
                "median_oi_value_change_pct": 9.1,
                "avg_taker_buy_sell_ratio": 0.82,
            },
        },
    }


def fake_context():
    return {
        "current": "AAVE",
        "current_perf": -1.2,
        "lookback": 18,
        "confirmation_cycles": 3,
        "candidates": [
            {
                "coin": "ENA",
                "perf": -6.5,
                "edge": -5.3,
                "one_h": -1.1,
                "rsi": 38.0,
                "futures": True,
                "blockers": [],
                "status": "SIGNAL",
            },
            {
                "coin": "SOL",
                "perf": 2.0,
                "edge": 3.2,
                "one_h": 0.5,
                "rsi": 64.0,
                "futures": True,
                "blockers": ["EDGE"],
                "status": "EDGE",
            },
        ],
    }


def test_build_shadow_report_is_clearly_non_live_and_escaped():
    module = load_module()

    report = module.build_shadow_report(fake_regime(), fake_context(), live_regime="sideways")

    assert "Shadow" in report
    assert "NO LIVE ORDERS" in report
    assert "BEAR" in report
    assert "82%" in report
    assert "SHORT ENA" in report
    assert "SIDEWAYS" in report
    assert "BTC trend down: EMA20&lt;EMA50 &amp; -DI&gt;+DI" in report
    assert "<pre>" in report and "</pre>" in report
    assert len(report) < 4096


def test_cmd_shadow_is_registered_and_uses_injected_collectors(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "collect_shadow_regime", lambda: fake_regime("bull", 0.74))
    monkeypatch.setattr(module, "get_momentum_context", fake_context)
    monkeypatch.setattr(module, "get_latest_regime", lambda: {"regime": "bear"})

    assert module.COMMANDS["/shadow"] is module.cmd_shadow
    output = module.cmd_shadow()

    assert "Shadow" in output
    assert "BULL" in output
    assert "NO LIVE ORDERS" in output
    assert "Live bot" in output
    assert "BEAR" in output

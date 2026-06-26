"""Tests for research-only OI divergence sweep."""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "research_oi_divergence_filter.py"
HOUR_MS = 3600 * 1000


def load_module():
    spec = importlib.util.spec_from_file_location("oi_divergence_filter_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def candle(i, close, high=None, low=None):
    high = high if high is not None else close * 1.01
    low = low if low is not None else close * 0.99
    return {
        "ts": i * HOUR_MS,
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1.0,
    }


def test_summarize_payload_counts_wins_losses_and_exits():
    module = load_module()
    payload = {
        "records": [
            {"pnl_pct": 3.0, "max_drawdown_pct": 2.0, "trailing_exits": 1},
            {"pnl_pct": -1.0, "max_drawdown_pct": 5.0, "stop_loss_exits": 1},
        ]
    }

    cell = module.summarize_payload(payload, lookback_hours=6, min_oi_change_pct=5.0)

    assert cell.lookback_hours == 6
    assert cell.min_oi_change_pct == 5.0
    assert cell.trades == 2
    assert cell.total_pnl_pct == 2.0
    assert cell.avg_pnl_pct == 1.0
    assert cell.win_rate_pct == 50.0
    assert cell.max_drawdown_pct == 5.0
    assert cell.stop_loss_exits == 1
    assert cell.trailing_exits == 1


def test_run_sweep_prefers_oi_threshold_that_skips_bad_trade():
    module = load_module()
    market = {
        "GOODUSDC": {
            "candles": [
                candle(0, 100, high=101, low=99),
                candle(1, 96, high=97, low=93),
                candle(2, 94, high=95, low=90),
            ],
            "open_interest": [
                {"timestamp": 0, "sumOpenInterestValue": "1000"},
                {"timestamp": HOUR_MS, "sumOpenInterestValue": "1120"},
            ],
            "funding": [],
        },
        "BADUSDC": {
            "candles": [
                candle(0, 100, high=101, low=99),
                candle(1, 96, high=97, low=95),
                candle(2, 120, high=121, low=119),
            ],
            "open_interest": [
                {"timestamp": 0, "sumOpenInterestValue": "1000"},
                {"timestamp": HOUR_MS, "sumOpenInterestValue": "900"},
            ],
            "funding": [],
        },
    }
    cfg = module.BacktestConfig(
        initial_balance=1000,
        leverage=1,
        max_margin_pct=0.5,
        fee_rate=0.0,
        slippage_pct=0.0,
        lookback_hours=1,
        min_oi_change_pct=0.0,
        stop_loss_pct=10.0,
        trailing_activation_pct=3.0,
        trailing_callback_pct=1.0,
    )

    result = module.run_sweep(market, lookbacks=[1], oi_thresholds=[-20, 0], base_config=cfg)

    rows = {(row["lookback_hours"], row["min_oi_change_pct"]): row for row in result["ranked"]}
    assert rows[(1, 0.0)]["trades"] < rows[(1, -20.0)]["trades"]
    assert rows[(1, 0.0)]["total_pnl_pct"] > rows[(1, -20.0)]["total_pnl_pct"]
    assert result["ranked"][0]["min_oi_change_pct"] == 0.0

"""Tests for SIDEWAYS/chop research backtester."""

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "research_sideways_chop_backtester.py"
HOUR_MS = 3600 * 1000


def load_module():
    spec = importlib.util.spec_from_file_location("research_sideways_chop_backtester_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def candle(ts, close, *, open_=None, high=None, low=None):
    open_ = close if open_ is None else open_
    high = max(open_, close) if high is None else high
    low = min(open_, close) if low is None else low
    return {
        "ts": ts,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1.0,
    }


def test_chop_window_accepts_range_and_rejects_trend():
    module = load_module()
    config = module.SidewaysConfig(lookback_hours=6, min_range_pct=2.0, max_range_pct=8.0, max_abs_trend_pct=4.0)
    sideways = [candle(i * HOUR_MS, price) for i, price in enumerate([100, 102, 99, 101, 98, 100])]
    trending = [candle(i * HOUR_MS, price) for i, price in enumerate([100, 103, 106, 109, 112, 115])]

    sideways_diag = module.classify_chop_window(sideways, config)
    trend_diag = module.classify_chop_window(trending, config)

    assert sideways_diag["eligible"] is True
    assert trend_diag["eligible"] is False
    assert trend_diag["trend_pct"] > config.max_abs_trend_pct


def test_mean_reversion_uses_completed_window_and_enters_next_candle():
    module = load_module()
    config = module.SidewaysConfig(
        initial_balance=100.0,
        lookback_hours=4,
        entry_z=0.9,
        exit_z=0.1,
        min_range_pct=2.0,
        max_range_pct=8.0,
        max_abs_trend_pct=5.0,
        max_hold_hours=12,
        fee_rate=0.0,
        slippage_pct=0.0,
    )
    candles = [
        candle(0 * HOUR_MS, 100),
        candle(1 * HOUR_MS, 102),
        candle(2 * HOUR_MS, 100),
        candle(3 * HOUR_MS, 98),  # completed-window oversold signal
        candle(4 * HOUR_MS, 97, open_=97, high=99, low=96),  # entry should happen here, not at signal candle
        candle(5 * HOUR_MS, 100, open_=100, high=101, low=99),
        candle(6 * HOUR_MS, 101, open_=101, high=102, low=100),
    ]

    record = module.simulate_sideways_mean_reversion("TIA", candles, config)

    assert record["trade_count"] >= 1
    first_trade = record["trades_detail"][0]
    assert first_trade["entry_ts"] == 4 * HOUR_MS
    assert first_trade["signal_ts"] == 3 * HOUR_MS
    assert first_trade["entry"] == pytest.approx(97.0)
    assert first_trade["exit_ts"] > first_trade["entry_ts"]


def test_fees_can_make_flat_chop_fail_cash_baseline():
    module = load_module()
    record = module.make_acceptance_record(
        "JUP",
        {
            "final": 99.7,
            "pnl_pct": -0.3,
            "fees": 0.25,
            "max_drawdown_pct": 1.0,
            "trade_count": 1,
            "trades_detail": [],
        },
        module.SidewaysConfig(initial_balance=100.0),
    )

    assert record["regime"] == "sideways"
    assert record["baseline_pnl"] == 0.0
    assert record["vs_baseline_pct"] == pytest.approx(-0.3)
    assert record["fee_pct"] == pytest.approx(0.25)


def test_oos_windows_include_warmup_and_do_not_overlap():
    module = load_module()
    timestamps = [i * HOUR_MS for i in range(12)]

    windows = module.build_oos_windows(timestamps, lookback_hours=3, test_hours=2, count=2)

    assert len(windows) == 2
    assert [window["label"] for window in windows] == ["w1", "w2"]
    assert windows[-1]["test_end"] == timestamps[-1]
    for window in windows:
        assert window["warmup_start"] + 3 * HOUR_MS == window["test_start"]
        assert window["test_start"] <= window["test_end"]
    assert windows[0]["test_end"] < windows[1]["test_start"]


def test_windowed_record_uses_median_oos_and_pass_rate():
    module = load_module()
    config = module.SidewaysConfig(initial_balance=100.0)
    window_results = [
        {"window": {"label": "w1"}, "result": {"pnl_pct": 2.0, "fees": 0.1, "max_drawdown_pct": 1.0, "trade_count": 1}, "baseline_pnl": 0.0, "passed": True},
        {"window": {"label": "w2"}, "result": {"pnl_pct": -1.0, "fees": 0.2, "max_drawdown_pct": 2.0, "trade_count": 1}, "baseline_pnl": 0.0, "passed": False},
        {"window": {"label": "w3"}, "result": {"pnl_pct": 3.0, "fees": 0.1, "max_drawdown_pct": 1.5, "trade_count": 2}, "baseline_pnl": 0.5, "passed": True},
    ]

    record = module.make_windowed_acceptance_record("TIA", window_results, config)

    assert record["oos_pnl"] == pytest.approx(2.0)
    assert record["baseline_pnl"] == pytest.approx(0.0)
    assert record["vs_baseline_pct"] == pytest.approx(2.0)
    assert record["max_drawdown"] == pytest.approx(2.0)
    assert record["trade_count"] == 4
    assert record["robustness"]["window_count"] == 3
    assert record["robustness"]["passing_windows"] == 2
    assert record["robustness"]["pass_rate_pct"] == pytest.approx(66.6667, rel=1e-4)


def test_comparison_baseline_requires_beating_cash_and_current_momentum():
    module = load_module()
    record = module.make_acceptance_record(
        "TIA",
        {
            "final": 101.0,
            "pnl_pct": 1.0,
            "fees": 0.2,
            "max_drawdown_pct": 1.0,
            "trade_count": 2,
            "trades_detail": [],
        },
        module.SidewaysConfig(initial_balance=100.0),
    )

    adjusted = module.apply_comparison_baseline(
        [record],
        {"cash": 0.0, "current_momentum": 2.5},
    )

    assert adjusted[0]["baseline_pnl"] == 2.5
    assert adjusted[0]["baseline_pnl_pct"] == 2.5
    assert adjusted[0]["vs_baseline_pct"] == pytest.approx(-1.5)
    assert adjusted[0]["comparison_baselines"]["current_momentum"] == 2.5


def test_research_output_recommends_cash_when_no_candidate_passes():
    module = load_module()
    config = module.SidewaysConfig(initial_balance=100.0)
    ohlcv = {
        "TIA": [candle(0, 10.0), candle(HOUR_MS, 10.1)],
        "SOL": [candle(0, 100.0), candle(HOUR_MS, 100.1)],
    }
    records = [
        module.make_acceptance_record(
            "TIA",
            {
                "final": 99.7,
                "pnl_pct": -0.3,
                "fees": 0.25,
                "max_drawdown_pct": 1.0,
                "trade_count": 1,
                "trades_detail": [],
            },
            config,
        )
    ]

    output = module.build_sideways_research_output(records, ohlcv, config, days=1)

    assert output["records"] == records
    assert output["leaderboard"]["summary"] == {"total": 1, "passed": 0, "failed": 1}
    assert output["recommendation"]["action"] == "cash_standby"
    assert "No SIDEWAYS candidate" in output["recommendation"]["reason"]
    assert output["manifest"]["symbols"] == ["SOLUSDC", "TIAUSDC"]
    assert len(output["manifest"]["data_hash"]) == 64

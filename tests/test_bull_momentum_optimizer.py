"""Tests for BULL momentum robustness research optimizer."""

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "research_bull_momentum_optimizer.py"
HOUR_MS = 3600 * 1000


def load_module():
    spec = importlib.util.spec_from_file_location("research_bull_momentum_optimizer_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def candle(ts, close):
    return {
        "ts": ts,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1.0,
    }


def window_result(label, oos, baseline, dd, trades=4, fees=1.0, train=3.0):
    return {
        "window": {"label": label, "train_start": 0, "train_end": 1, "test_start": 2, "test_end": 3},
        "train": {"pnl": train, "max_dd": dd / 2, "trades": trades, "fees": fees / 2},
        "oos": {"pnl": oos, "final": 100.0 + oos, "max_dd": dd, "trades": trades, "fees": fees},
        "baseline_pnl": baseline,
        "vs_baseline_pct": oos - baseline,
        "passed": oos > 0 and oos >= baseline and dd <= 35.0 and trades >= 1,
    }


def test_walk_forward_windows_are_chronological_and_non_overlapping():
    module = load_module()
    timestamps = [i * HOUR_MS for i in range(20)]

    windows = module.build_walk_forward_windows(
        timestamps,
        train_hours=3,
        test_hours=2,
        count=3,
    )

    assert len(windows) == 3
    assert [window["label"] for window in windows] == ["w1", "w2", "w3"]
    assert windows == sorted(windows, key=lambda window: window["test_end"])
    assert windows[-1]["test_end"] == timestamps[-1]
    for window in windows:
        assert window["train_start"] <= window["train_end"]
        assert window["train_end"] + HOUR_MS == window["test_start"]
        assert window["test_start"] <= window["test_end"]


def test_robust_record_summarizes_multiple_oos_windows():
    module = load_module()
    params = {"momentum_lookback": 18, "momentum_min_edge": 4.0}
    results = [
        window_result("w1", oos=8.0, baseline=2.0, dd=9.0, trades=3, fees=0.8),
        window_result("w2", oos=6.0, baseline=1.0, dd=11.0, trades=4, fees=0.9),
        window_result("w3", oos=-2.0, baseline=0.0, dd=14.0, trades=2, fees=0.4),
    ]

    record = module.make_robust_acceptance_record(
        1,
        params,
        results,
        initial_balance=100.0,
    )

    assert record["name"] == "bull_momentum_robust_rank_1"
    assert record["strategy"] == "bull_momentum_rotation"
    assert record["regime"] == "bull"
    assert record["params"] == params
    assert record["oos_pnl"] == pytest.approx(6.0)  # median OOS, not lucky best window
    assert record["baseline_pnl"] == pytest.approx(1.0)  # median benchmark
    assert record["max_drawdown"] == pytest.approx(14.0)  # worst window drawdown
    assert record["trade_count"] == 9
    assert record["robustness"]["window_count"] == 3
    assert record["robustness"]["passing_windows"] == 2
    assert record["robustness"]["pass_rate_pct"] == pytest.approx(66.6667, rel=1e-4)
    assert record["robustness"]["worst_oos_pnl"] == pytest.approx(-2.0)


def test_robust_score_prefers_consistency_over_one_lucky_spike():
    module = load_module()
    lucky_spike = [
        window_result("w1", oos=55.0, baseline=2.0, dd=42.0, trades=3, fees=1.0),
        window_result("w2", oos=-14.0, baseline=1.0, dd=18.0, trades=3, fees=1.0),
        window_result("w3", oos=-8.0, baseline=0.0, dd=16.0, trades=3, fees=1.0),
    ]
    steady = [
        window_result("w1", oos=8.0, baseline=2.0, dd=8.0, trades=3, fees=0.7),
        window_result("w2", oos=7.0, baseline=1.0, dd=7.0, trades=3, fees=0.7),
        window_result("w3", oos=6.0, baseline=0.0, dd=6.0, trades=3, fees=0.7),
    ]

    assert module.score_robustness(steady) > module.score_robustness(lucky_spike)


def test_candidate_grid_keeps_regime_filter_on_by_default():
    module = load_module()

    safe_grid = module.candidate_param_grid(allow_no_regime_filter=False)
    exploratory_grid = module.candidate_param_grid(allow_no_regime_filter=True)

    assert safe_grid
    assert all(params["use_regime_filter"] is True for params in safe_grid)
    assert any(params["use_regime_filter"] is False for params in exploratory_grid)


def test_research_output_packages_candidates_manifest_and_leaderboard():
    module = load_module()
    ohlcv = {
        "SOL": [candle(0, 1.0), candle(HOUR_MS, 2.0)],
        "TIA": [candle(0, 10.0), candle(HOUR_MS, 12.0)],
    }
    btc = [candle(0, 100.0), candle(HOUR_MS, 101.0)]
    record = module.make_robust_acceptance_record(
        1,
        {"momentum_lookback": 18},
        [
            window_result("w1", oos=8.0, baseline=2.0, dd=8.0),
            window_result("w2", oos=6.0, baseline=1.0, dd=9.0),
        ],
        initial_balance=100.0,
    )

    output = module.build_bull_momentum_research_output(
        [record],
        ohlcv,
        btc,
        assumptions={"train_days": 90, "test_days": 30, "windows": 2},
    )

    assert output["records"] == [record]
    assert output["candidates"] == [record]
    assert output["leaderboard"]["summary"] == {"total": 1, "passed": 1, "failed": 0}
    assert output["manifest"]["symbols"] == ["BTCUSDC", "SOLUSDC", "TIAUSDC"]
    assert output["manifest"]["assumptions"]["windows"] == 2
    assert len(output["manifest"]["data_hash"]) == 64

"""Tests for Regime v2 research evaluator."""

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "research_regime_v2_evaluator.py"
HOUR_MS = 3600 * 1000


def load_module():
    spec = importlib.util.spec_from_file_location("regime_v2_evaluator_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def candle(ts, close, volume=1.0):
    return {
        "ts": ts,
        "open": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": volume,
    }


def make_series(start=100.0, drift=0.001, n=140, shock_at=None, shock=-0.02):
    rows = []
    price = start
    for i in range(n):
        if shock_at is not None and i >= shock_at:
            price *= 1.0 + shock
        else:
            price *= 1.0 + drift
        rows.append(candle(i * HOUR_MS, price, volume=1.0 + i / 1000.0))
    return rows


def make_dataset():
    data = {}
    for idx, coin in enumerate(["BTC", "ETH", "SOL", "SUI", "AAVE", "LINK"]):
        data[coin] = make_series(100 + idx, drift=0.002 if idx < 3 else 0.0015)
    return data


def test_build_feature_snapshot_contains_strategy_utility_features():
    module = load_module()
    snapshot = module.build_feature_snapshot(
        make_dataset(),
        references=["BTC", "ETH", "SOL"],
        breadth_coins=["SOL", "SUI", "AAVE", "LINK"],
    )

    assert snapshot["valid_breadth_coins"] == 4
    assert snapshot["reference_trend_score"] > 0
    assert snapshot["breadth_above_ema50_pct"] >= 0.75
    assert "basket_vs_btc_24h" in snapshot
    assert "downside_vol_24h" in snapshot


def test_strategy_utility_label_prefers_bull_when_basket_beats_cash_and_btc():
    module = load_module()
    label = module.strategy_utility_label(
        future_basket_ret=5.0,
        future_btc_ret=1.0,
        future_vol=3.0,
        fee_bps=10,
    )

    assert label == module.BULL


def test_strategy_utility_label_marks_stormy_on_large_forward_crash():
    module = load_module()
    label = module.strategy_utility_label(
        future_basket_ret=-9.0,
        future_btc_ret=-7.0,
        future_vol=11.0,
        fee_bps=10,
    )

    assert label == module.STORMY


def test_evaluate_regime_v2_history_emits_manifest_records_and_leaderboard():
    module = load_module()
    data = make_dataset()
    output = module.evaluate_regime_v2_history(
        data,
        references=["BTC", "ETH", "SOL"],
        breadth_coins=["SOL", "SUI", "AAVE", "LINK"],
        step_hours=12,
        warmup_hours=72,
        forward_hours=12,
        confirmation_samples=2,
        min_confidence=0.55,
    )

    assert output["manifest"]["script"] == "research_regime_v2_evaluator.py"
    assert output["records"]
    assert output["leaderboard"]["summary"]["total"] >= 2
    first = output["records"][0]
    assert {"time", "legacy_regime", "v1_regime", "v2_regime", "label", "score"}.issubset(first)
    assert "switching" in output["leaderboard"]["by_metric"]
    assert "relative_performance" in output["leaderboard"]["by_metric"]

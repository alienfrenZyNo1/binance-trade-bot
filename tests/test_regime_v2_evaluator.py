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


def synthetic_training_records():
    rows = []
    for idx in range(8):
        rows.append(
            {
                "label": "bull",
                "features": {
                    "reference_trend_score": 1.0,
                    "breadth_above_ema50_pct": 0.85,
                    "breadth_advancers_24h_pct": 0.9,
                    "basket_ret_24h": 3.0,
                    "basket_ret_4h": 0.8,
                    "basket_vs_btc_24h": 1.5,
                    "median_vol_24h": 3.0,
                    "downside_vol_24h": 0.5,
                    "return_dispersion_24h": 1.0,
                    "futures_oi_change_pct": 1.0,
                    "futures_basis_pct": 0.02,
                    "futures_taker_ratio": 1.1,
                    "futures_funding_pct": 0.0,
                },
            }
        )
        rows.append(
            {
                "label": "bear",
                "features": {
                    "reference_trend_score": -1.0,
                    "breadth_above_ema50_pct": 0.15,
                    "breadth_advancers_24h_pct": 0.1,
                    "basket_ret_24h": -3.5,
                    "basket_ret_4h": -1.0,
                    "basket_vs_btc_24h": -1.2,
                    "median_vol_24h": 4.0,
                    "downside_vol_24h": 2.0,
                    "return_dispersion_24h": 2.0,
                    "futures_oi_change_pct": 2.0,
                    "futures_basis_pct": -0.03,
                    "futures_taker_ratio": 0.9,
                    "futures_funding_pct": 0.0,
                },
            }
        )
    return rows


def test_train_scorecard_weights_improves_training_accuracy_over_bad_weights():
    module = load_module()
    records = synthetic_training_records()
    bad_weights = {"reference_trend_score": -2.0, "breadth_score": -1.0, "momentum_score": -1.0, "relative_strength_score": -1.0}

    bad = module.score_records_with_weights(records, bad_weights)
    tuned = module.train_scorecard_weights(records, min_records=4)

    assert tuned["accuracy_pct"] > bad["accuracy_pct"]
    assert tuned["weights"]["reference_trend_score"] > 0
    assert tuned["weights"]["breadth_score"] > 0


def test_evaluate_regime_v2_history_can_emit_tuned_scorecard_results():
    module = load_module()
    output = module.evaluate_regime_v2_history(
        make_dataset(),
        references=["BTC", "ETH", "SOL"],
        breadth_coins=["SOL", "SUI", "AAVE", "LINK"],
        step_hours=12,
        warmup_hours=72,
        forward_hours=12,
        tune_scorecard=True,
        train_fraction=0.5,
    )

    assert output["manifest"]["assumptions"]["tune_scorecard"] is True
    assert output["tuning"]["enabled"] is True
    assert "weights" in output["tuning"]
    assert "regime_v2_tuned" in output["sequence"]
    assert "regime_v2_tuned" in {row["name"] for row in output["leaderboard"]["by_metric"]["label_accuracy"]}
    assert "v2_tuned_regime" in output["records"][-1]

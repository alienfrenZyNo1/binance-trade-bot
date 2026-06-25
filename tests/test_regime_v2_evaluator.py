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


def test_route_return_models_regime_actions_after_costs():
    module = load_module()

    assert module.route_window_return(module.BULL, future_basket_ret=4.0, future_btc_ret=2.0, fee_bps=10) > 3.8
    assert module.route_window_return(module.SIDEWAYS, future_basket_ret=4.0, future_btc_ret=2.0, fee_bps=10) == 0.0
    assert module.route_window_return(module.STORMY, future_basket_ret=-8.0, future_btc_ret=-6.0, fee_bps=10) == 0.0
    assert module.route_window_return(module.BEAR, future_basket_ret=-4.0, future_btc_ret=-3.0, fee_bps=10) > 0.0


def test_build_route_outcomes_compounds_equity_and_drawdown():
    module = load_module()
    records = [
        {"legacy_regime": module.BULL, "v2_smoothed": module.SIDEWAYS, "future_basket_ret": -5.0, "future_btc_ret": -4.0},
        {"legacy_regime": module.BULL, "v2_smoothed": module.BULL, "future_basket_ret": 3.0, "future_btc_ret": 1.0},
    ]

    outcomes = module.build_route_outcomes(records, fee_bps=10)

    assert outcomes["legacy_sol"]["total_return_pct"] < 0
    assert outcomes["regime_v2"]["total_return_pct"] > outcomes["legacy_sol"]["total_return_pct"]
    assert outcomes["regime_v2"]["max_drawdown_pct"] <= outcomes["legacy_sol"]["max_drawdown_pct"]
    assert outcomes["cash"]["total_return_pct"] == 0.0


def test_evaluate_regime_v2_history_includes_route_outcome_leaderboard():
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

    assert "route_outcomes" in output
    assert "route_outcomes" in output["leaderboard"]["by_metric"]
    assert {"cash", "buy_and_hold_basket", "legacy_sol", "regime_v2"}.issubset(output["route_outcomes"])
    assert "total_return_pct" in output["route_outcomes"]["regime_v2"]


def test_train_route_scorecard_weights_optimizes_route_return_not_label_accuracy():
    module = load_module()
    records = []
    for idx in range(8):
        row = synthetic_training_records()[0].copy()
        row.update({"future_basket_ret": -5.0, "future_btc_ret": -4.0, "v2_smoothed": module.BULL})
        records.append(row)

    default_outcomes = module.build_route_outcomes(
        [{**row, "v2_smoothed": module.classify_v2_scorecard(row["features"])["regime"]} for row in records],
        fee_bps=10,
    )
    tuned = module.train_route_scorecard_weights(records, fee_bps=10, min_records=4)

    assert tuned["enabled"] is True
    assert tuned["route_total_return_pct"] > default_outcomes["regime_v2"]["total_return_pct"]
    assert tuned["weights"]["bull_threshold"] >= module.DEFAULT_SCORE_WEIGHTS["bull_threshold"]


def test_route_failure_diagnostics_identifies_worst_windows():
    module = load_module()
    records = [
        {"time": "t1", "v2_smoothed": module.BULL, "future_basket_ret": -8.0, "future_btc_ret": -6.0, "features": {"breadth_above_ema50_pct": 0.8}, "reasons": ["foo"]},
        {"time": "t2", "v2_smoothed": module.SIDEWAYS, "future_basket_ret": 3.0, "future_btc_ret": 2.0, "features": {"breadth_above_ema50_pct": 0.5}, "reasons": ["bar"]},
    ]

    diagnostics = module.route_failure_diagnostics(records, "v2_smoothed", fee_bps=10, limit=1)

    assert diagnostics["route_key"] == "v2_smoothed"
    assert diagnostics["worst_windows"][0]["time"] == "t1"
    assert diagnostics["worst_windows"][0]["route_return_pct"] < 0


def test_evaluate_regime_v2_history_can_tune_route_objective_and_emit_diagnostics():
    module = load_module()
    output = module.evaluate_regime_v2_history(
        make_dataset(),
        references=["BTC", "ETH", "SOL"],
        breadth_coins=["SOL", "SUI", "AAVE", "LINK"],
        step_hours=12,
        warmup_hours=72,
        forward_hours=12,
        tune_route_objective=True,
        train_fraction=0.5,
    )

    assert output["manifest"]["assumptions"]["tune_route_objective"] is True
    assert output["route_tuning"]["enabled"] is True
    assert "regime_v2_route_tuned" in output["route_outcomes"]
    assert "route_failure_diagnostics" in output
    assert output["route_failure_diagnostics"]["regime_v2"]["worst_windows"]


def base_guardrail_features():
    return {
        "reference_trend_score": 1.0,
        "breadth_above_ema20_pct": 0.8,
        "breadth_above_ema50_pct": 0.8,
        "breadth_advancers_24h_pct": 0.8,
        "basket_ret_24h": 3.0,
        "basket_ret_4h": 1.0,
        "basket_vs_btc_24h": 1.5,
        "basket_vs_eth_24h": 1.0,
        "basket_vs_sol_24h": 0.5,
        "return_dispersion_24h": 1.0,
        "median_vol_24h": 3.0,
        "downside_vol_24h": 0.5,
        "median_volume_change_24h": 1.0,
        "futures_valid_symbols": 0,
        "futures_funding_pct": 0.0,
        "futures_basis_pct": 0.0,
        "futures_oi_change_pct": 0.0,
        "futures_taker_ratio": 1.0,
    }


def test_guardrails_block_false_bull_when_breadth_is_deteriorating_fast():
    module = load_module()
    features = base_guardrail_features()
    features.update(
        {
            "basket_ret_4h": -3.2,
            "breadth_advancers_24h_pct": 0.30,
            "downside_vol_24h": 6.5,
            "return_dispersion_24h": 7.5,
        }
    )

    result = module.classify_v2_scorecard(features)

    assert result["regime"] != module.BULL
    assert any("false-bull" in reason for reason in result["reasons"])


def test_guardrails_block_false_bear_when_rebound_risk_is_high():
    module = load_module()
    features = base_guardrail_features()
    features.update(
        {
            "reference_trend_score": -1.0,
            "breadth_above_ema50_pct": 0.25,
            "breadth_advancers_24h_pct": 0.72,
            "basket_ret_24h": -2.5,
            "basket_ret_4h": 3.4,
            "basket_vs_btc_24h": 1.2,
            "futures_taker_ratio": 1.15,
        }
    )

    result = module.classify_v2_scorecard(features)

    assert result["regime"] != module.BEAR
    assert any("rebound" in reason for reason in result["reasons"])


def test_route_robustness_gates_require_multi_window_positive_returns():
    module = load_module()
    records = []
    for idx, ret in enumerate([2.0, 2.0, -6.0, 2.0, 2.0, 2.0]):
        records.append({"v2_smoothed": module.BULL, "future_basket_ret": ret, "future_btc_ret": ret / 2})

    robustness = module.build_route_robustness_gates(records, "v2_smoothed", fee_bps=10, windows=3, min_window_return_pct=0.0, max_window_drawdown_pct=5.0)

    assert robustness["passed"] is False
    assert robustness["passing_windows"] < robustness["total_windows"]
    assert any(window["total_return_pct"] < 0 for window in robustness["windows"])


def test_evaluate_regime_v2_history_emits_route_robustness_gates():
    module = load_module()
    output = module.evaluate_regime_v2_history(
        make_dataset(),
        references=["BTC", "ETH", "SOL"],
        breadth_coins=["SOL", "SUI", "AAVE", "LINK"],
        step_hours=12,
        warmup_hours=72,
        forward_hours=12,
        tune_route_objective=True,
        train_fraction=0.5,
    )

    assert "route_robustness" in output
    assert "regime_v2" in output["route_robustness"]
    assert "passed" in output["route_robustness"]["regime_v2"]


def test_build_selector_route_uses_only_prior_windows():
    module = load_module()
    records = [
        {"a_regime": module.BULL, "b_regime": module.SIDEWAYS, "future_basket_ret": 2.0, "future_btc_ret": 1.0},
        {"a_regime": module.BULL, "b_regime": module.SIDEWAYS, "future_basket_ret": 2.0, "future_btc_ret": 1.0},
        {"a_regime": module.BULL, "b_regime": module.SIDEWAYS, "future_basket_ret": -8.0, "future_btc_ret": -4.0},
    ]

    selected = module.build_selector_route(
        records,
        route_candidates={"a": "a_regime", "b": "b_regime"},
        fee_bps=10,
        lookback=2,
        min_trailing_objective=-999,
    )

    assert selected[0]["selector_route_key"] == "cash"
    assert selected[1]["selector_route_key"] == "a"
    assert selected[2]["selector_route_key"] == "a"
    assert selected[2]["selector_smoothed"] == module.BULL


def test_selector_can_choose_cash_when_all_recent_routes_are_weak():
    module = load_module()
    records = [
        {"a_regime": module.BULL, "future_basket_ret": -2.0, "future_btc_ret": -1.0},
        {"a_regime": module.BULL, "future_basket_ret": -2.0, "future_btc_ret": -1.0},
    ]

    selected = module.build_selector_route(
        records,
        route_candidates={"a": "a_regime"},
        fee_bps=10,
        lookback=1,
        min_trailing_objective=0.0,
    )

    assert selected[1]["selector_route_key"] == "cash"
    assert selected[1]["selector_smoothed"] == module.SIDEWAYS


def test_evaluate_regime_v2_history_emits_selector_route_artifacts():
    module = load_module()
    output = module.evaluate_regime_v2_history(
        make_dataset(),
        references=["BTC", "ETH", "SOL"],
        breadth_coins=["SOL", "SUI", "AAVE", "LINK"],
        step_hours=12,
        warmup_hours=72,
        forward_hours=12,
        tune_scorecard=True,
        tune_route_objective=True,
        train_fraction=0.5,
        selector_lookback=2,
    )

    assert "selector" in output
    assert output["selector"]["enabled"] is True
    assert "regime_v2_selector" in output["route_outcomes"]
    assert "regime_v2_selector" in output["route_robustness"]
    assert "selector_route_key" in output["records"][-1]


def test_selector_can_cash_out_when_trailing_drawdown_is_too_high():
    module = load_module()
    records = [
        {"a_regime": module.BULL, "future_basket_ret": 20.0, "future_btc_ret": 5.0},
        {"a_regime": module.BULL, "future_basket_ret": -25.0, "future_btc_ret": -10.0},
        {"a_regime": module.BULL, "future_basket_ret": 20.0, "future_btc_ret": 5.0},
        {"a_regime": module.BULL, "future_basket_ret": 1.0, "future_btc_ret": 0.5},
    ]

    selected = module.build_selector_route(
        records,
        route_candidates={"a": "a_regime"},
        fee_bps=10,
        lookback=3,
        min_trailing_objective=-999,
        max_trailing_drawdown_pct=15.0,
    )

    assert selected[3]["selector_route_key"] == "cash"
    assert selected[3]["selector_smoothed"] == module.SIDEWAYS
    assert selected[3]["selector_trailing_drawdown_pct"] > 15.0
    assert "drawdown" in selected[3]["selector_block_reason"]


def test_evaluate_regime_v2_history_records_selector_drawdown_guard_settings():
    module = load_module()
    output = module.evaluate_regime_v2_history(
        make_dataset(),
        references=["BTC", "ETH", "SOL"],
        breadth_coins=["SOL", "SUI", "AAVE", "LINK"],
        step_hours=12,
        warmup_hours=72,
        forward_hours=12,
        tune_scorecard=True,
        tune_route_objective=True,
        train_fraction=0.5,
        selector_lookback=2,
        selector_max_trailing_drawdown_pct=15.0,
    )

    assert output["selector"]["max_trailing_drawdown_pct"] == 15.0
    assert output["manifest"]["assumptions"]["selector_max_trailing_drawdown_pct"] == 15.0
    assert "selector_trailing_drawdown_pct" in output["records"][-1]


def test_selector_equity_stop_forces_cash_after_own_drawdown_breach():
    module = load_module()
    records = [
        {"a_regime": module.BULL, "future_basket_ret": 5.0, "future_btc_ret": 1.0},
        {"a_regime": module.BULL, "future_basket_ret": -20.0, "future_btc_ret": -5.0},
        {"a_regime": module.BULL, "future_basket_ret": 5.0, "future_btc_ret": 1.0},
    ]

    selected = module.build_selector_route(
        records,
        route_candidates={"a": "a_regime"},
        fee_bps=10,
        lookback=1,
        min_trailing_objective=-999,
        selector_equity_stop_drawdown_pct=10.0,
    )

    assert selected[2]["selector_route_key"] == "cash"
    assert selected[2]["selector_smoothed"] == module.SIDEWAYS
    assert selected[2]["selector_equity_drawdown_pct"] > 10.0
    assert "equity drawdown" in selected[2]["selector_block_reason"]

"""Tests for report-only Regime v2 promotion readiness."""

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "regime_promotion_readiness.py"


def load_module():
    spec = importlib.util.spec_from_file_location("regime_promotion_readiness_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def artifact(best_route="regime_v2", best_return=12.0, best_dd=6.0, robust_passed=True):
    return {
        "leaderboard": {
            "by_metric": {
                "route_outcomes": [
                    {"name": best_route, "total_return_pct": best_return, "max_drawdown_pct": best_dd, "win_rate_pct": 40.0},
                    {"name": "cash", "total_return_pct": 0.0, "max_drawdown_pct": 0.0, "win_rate_pct": 0.0},
                ]
            }
        },
        "route_robustness": {
            best_route: {"passed": robust_passed, "passing_windows": 3 if robust_passed else 2, "total_windows": 3},
            "cash": {"passed": False, "passing_windows": 0, "total_windows": 3},
        },
        "sequence": {"regime_v2_smoothed": {"distribution": {"bull": 2, "sideways": 1}}},
    }


def test_summarize_window_prefers_best_non_cash_route_and_robustness():
    module = load_module()
    summary = module.summarize_window("30d", artifact(best_route="regime_v2_tuned", best_return=8.5, robust_passed=True))

    assert summary["window"] == "30d"
    assert summary["best_route"] == "regime_v2_tuned"
    assert summary["best_return_pct"] == 8.5
    assert summary["best_route_robust"] is True


def test_summarize_window_can_require_specific_route_instead_of_best_return():
    module = load_module()
    art = artifact(best_route="regime_v2_route_tuned", best_return=25.0, best_dd=30.0, robust_passed=False)
    art["leaderboard"]["by_metric"]["route_outcomes"].append(
        {"name": "regime_v2_selector", "total_return_pct": 12.0, "max_drawdown_pct": 8.0, "win_rate_pct": 55.0}
    )
    art["route_robustness"]["regime_v2_selector"] = {"passed": True, "passing_windows": 3, "total_windows": 3}

    summary = module.summarize_window("240d", art, required_route="regime_v2_selector")

    assert summary["best_route"] == "regime_v2_selector"
    assert summary["best_return_pct"] == 12.0
    assert summary["best_max_drawdown_pct"] == 8.0
    assert summary["best_route_robust"] is True


def test_evaluate_readiness_can_require_specific_route():
    module = load_module()
    art = artifact(best_route="regime_v2_route_tuned", best_return=25.0, best_dd=30.0, robust_passed=False)
    art["leaderboard"]["by_metric"]["route_outcomes"].append(
        {"name": "regime_v2_selector", "total_return_pct": 12.0, "max_drawdown_pct": 8.0, "win_rate_pct": 55.0}
    )
    art["route_robustness"]["regime_v2_selector"] = {"passed": True, "passing_windows": 3, "total_windows": 3}

    result = module.evaluate_readiness({"240d": art}, required_route="regime_v2_selector")

    assert result["verdict"] == "🟢"
    assert result["windows"][0]["best_route"] == "regime_v2_selector"


def test_evaluate_readiness_is_green_only_when_all_windows_have_robust_positive_routes():
    module = load_module()
    result = module.evaluate_readiness(
        {
            "30d": artifact(best_return=5.0, robust_passed=True),
            "60d": artifact(best_return=10.0, robust_passed=True),
            "90d": artifact(best_return=7.0, robust_passed=True),
        }
    )

    assert result["verdict"] == "🟢"
    assert "Eligible" in result["status"]


def test_evaluate_readiness_yellow_when_profitable_but_not_robust():
    module = load_module()
    result = module.evaluate_readiness(
        {
            "30d": artifact(best_return=5.0, robust_passed=False),
            "60d": artifact(best_return=10.0, robust_passed=True),
            "90d": artifact(best_return=7.0, robust_passed=True),
        }
    )

    assert result["verdict"] == "🟡"
    assert any("robustness" in blocker for blocker in result["blockers"])


def test_evaluate_readiness_blocks_high_drawdown_even_when_robust():
    module = load_module()
    result = module.evaluate_readiness(
        {
            "90d": artifact(best_return=20.0, best_dd=8.0, robust_passed=True),
            "180d": artifact(best_return=30.0, best_dd=21.0, robust_passed=True),
        },
        max_allowed_drawdown_pct=18.0,
    )

    assert result["verdict"] == "🟡"
    assert any("drawdown" in blocker.lower() for blocker in result["blockers"])


def test_run_fresh_artifacts_passes_constrained_selector_args(monkeypatch):
    module = load_module()
    calls = []

    def fake_fetch(coins, *, references, days):
        assert coins == ["SOL"]
        assert references == ["BTC"]
        return {"SOL": [], "BTC": []}

    def fake_eval(data, **kwargs):
        calls.append(kwargs)
        return artifact(best_return=1.0, best_dd=1.0, robust_passed=True)

    monkeypatch.setattr(module._regime_v2._regime, "fetch_market_data", fake_fetch)
    monkeypatch.setattr(module._regime_v2, "evaluate_regime_v2_history", fake_eval)
    args = type(
        "Args",
        (),
        {
            "days": "90",
            "coins": "SOL",
            "references": "BTC",
            "step_hours": 6,
            "forward_hours": 24,
            "min_confidence": 0.60,
            "train_fraction": 0.60,
            "selector_lookback": 3,
            "selector_min_objective": 0.0,
            "selector_max_trailing_drawdown_pct": 15.0,
            "selector_equity_stop_drawdown_pct": 15.0,
            "selector_equity_stop_cooldown_windows": 1,
            "selector_min_trailing_return_pct": -999999.0,
            "selector_min_trailing_win_rate_pct": 50.0,
            "selector_trailing_robust_windows": 3,
            "selector_min_passing_trailing_windows": 3,
            "selector_trailing_window_min_return_pct": 0.0,
            "selector_trailing_window_max_drawdown_pct": 15.0,
        },
    )()

    artifacts = module.run_fresh_artifacts(args)

    assert "90d" in artifacts
    assert calls[0]["selector_lookback"] == 3
    assert calls[0]["selector_max_trailing_drawdown_pct"] == 15.0
    assert calls[0]["selector_min_trailing_win_rate_pct"] == 50.0
    assert calls[0]["selector_min_passing_trailing_windows"] == 3


def test_render_markdown_report_is_report_only_and_scannable():
    module = load_module()
    result = module.evaluate_readiness(
        {
            "30d": artifact(best_return=5.0, robust_passed=False),
            "60d": artifact(best_return=10.0, robust_passed=True),
            "90d": artifact(best_return=7.0, robust_passed=True),
        }
    )
    report = module.render_markdown_report(result)

    assert "REPORT ONLY" in report
    assert "NO LIVE ORDERS" in report
    assert "🟡" in report
    assert "| Window | Best route | Return | Max DD | Robust |" in report

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

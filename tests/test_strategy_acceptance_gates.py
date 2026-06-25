"""Tests for strategy acceptance gates and per-regime leaderboard."""

import importlib.util
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "strategy_acceptance_gates.py"


def load_module():
    spec = importlib.util.spec_from_file_location("strategy_acceptance_gates_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_evaluate_strategy_passes_robust_oos_record():
    module = load_module()
    result = module.evaluate_strategy(
        {
            "name": "bull-momentum-v1",
            "regime": "bull",
            "oos_pnl": 18.0,
            "baseline_pnl": 4.0,
            "max_drawdown": 12.0,
            "trades": 14,
            "fee_pct": 3.0,
            "sharpe": 1.2,
        }
    )

    assert result.passed is True
    assert not result.failures
    assert result.metrics["vs_baseline_pct"] == 14.0


def test_evaluate_strategy_fails_unprofitable_or_fragile_record():
    module = load_module()
    result = module.evaluate_strategy(
        {
            "name": "sideways-overtrade",
            "regime": "sideways",
            "pnl_pct": -2.0,
            "baseline_pnl_pct": 0.0,
            "max_drawdown": 48.0,
            "trade_count": 1,
            "fee_pct": 21.0,
            "sharpe": -0.4,
        }
    )

    assert result.passed is False
    assert any("OOS P&L" in failure for failure in result.failures)
    assert any("max drawdown" in failure for failure in result.failures)
    assert any("trades" in failure for failure in result.failures)


def test_build_leaderboard_groups_by_regime_and_sorts_passes_first():
    module = load_module()
    leaderboard = module.build_leaderboard(
        [
            {
                "name": "bear-short",
                "regime": "bear",
                "oos_pnl": 9.0,
                "baseline_pnl": 0.0,
                "max_drawdown": 10.0,
                "trades": 5,
                "fee_pct": 2.0,
                "sharpe": 0.9,
            },
            {
                "name": "bad-bull",
                "regime": "bull",
                "oos_pnl": -1.0,
                "max_drawdown": 40.0,
                "trades": 2,
                "fee_pct": 4.0,
                "sharpe": -0.1,
            },
        ]
    )

    assert leaderboard["summary"] == {"total": 2, "passed": 1, "failed": 1}
    assert leaderboard["overall"][0]["name"] == "bear-short"
    assert "bear" in leaderboard["by_regime"]
    assert "bull" in leaderboard["by_regime"]


def test_cli_reads_json_and_writes_leaderboard(tmp_path, capsys):
    module = load_module()
    input_path = tmp_path / "records.json"
    output_path = tmp_path / "leaderboard.json"
    input_path.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "name": "candidate",
                        "regime": "bull",
                        "oos_pnl": 5.0,
                        "baseline_pnl": 1.0,
                        "max_drawdown": 8.0,
                        "trades": 4,
                        "fee_pct": 1.0,
                        "sharpe": 0.6,
                    }
                ]
            }
        )
    )

    module.main([str(input_path), "--output", str(output_path)])

    assert output_path.exists()
    payload = json.loads(output_path.read_text())
    assert payload["summary"]["passed"] == 1
    assert "candidate" in capsys.readouterr().out

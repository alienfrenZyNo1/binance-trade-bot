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


def test_build_research_output_adds_manifest_and_gated_leaderboard():
    module = load_module()
    ohlcv = {
        "SOL": [
            {"ts": 0, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 10},
            {"ts": 3600_000, "open": 2, "high": 2, "low": 2, "close": 2, "volume": 12},
        ],
        "TIA": [
            {"ts": 0, "open": 10, "high": 10, "low": 10, "close": 10, "volume": 5},
            {"ts": 3600_000, "open": 11, "high": 11, "low": 11, "close": 11, "volume": 6},
        ],
    }
    records = [
        {
            "name": "momentum-oos",
            "strategy": "momentum_rotation",
            "regime": "bull",
            "oos_pnl": 7.0,
            "baseline_pnl": 1.0,
            "max_drawdown": 4.0,
            "trades": 5,
            "fee_pct": 0.8,
            "sharpe": 0.7,
            "params": {"momentum_lookback": 24},
        }
    ]

    output = module.build_research_output(
        records,
        ohlcv_by_coin=ohlcv,
        interval="1h",
        bridge="USDC",
        assumptions={"fee_rate": 0.00075, "slippage": 0.0005},
    )

    assert output["records"] == records
    assert output["leaderboard"]["summary"] == {"total": 1, "passed": 1, "failed": 0}
    manifest = output["manifest"]
    assert manifest["bridge"] == "USDC"
    assert manifest["interval"] == "1h"
    assert manifest["symbols"] == ["SOLUSDC", "TIAUSDC"]
    assert manifest["date_range"]["start_ts"] == 0
    assert manifest["date_range"]["end_ts"] == 3600_000
    assert manifest["candle_counts"] == {"SOL": 2, "TIA": 2}
    assert manifest["assumptions"] == {"fee_rate": 0.00075, "slippage": 0.0005}
    assert len(manifest["data_hash"]) == 64

"""Research output packaging tests for the broad strategy optimizer."""

import runpy
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "strategy_optimizer.py"
HOUR_MS = 3600 * 1000


def candle(ts, close):
    return {
        "ts": ts,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1.0,
    }


def load_module_without_main():
    return runpy.run_path(str(SCRIPT), run_name="strategy_optimizer_test")


def test_strategy_optimizer_output_includes_manifest_and_gates():
    namespace = load_module_without_main()
    build_output = namespace["build_strategy_research_output"]
    ohlcv = {
        "SOL": [candle(0, 1.0), candle(HOUR_MS, 2.0)],
        "TIA": [candle(0, 10.0), candle(HOUR_MS, 12.0)],
    }
    btc = [candle(0, 100.0), candle(HOUR_MS, 101.0)]
    records = [
        {
            "strategy": "momentum_rotation",
            "regime": "bull",
            "oos_pnl": 6.0,
            "baseline_pnl": 1.0,
            "max_drawdown": 5.0,
            "trade_count": 5,
            "total_fees": 0.5,
            "initial_balance": 62.0,
            "sharpe": 0.8,
            "params": {"momentum_lookback": 24},
        }
    ]

    output = build_output(
        records,
        ohlcv,
        btc,
        months=6,
        strategies=["momentum_rotation"],
        max_combos=3,
    )

    assert output["records"] == records
    assert output["leaderboard"]["summary"] == {"total": 1, "passed": 1, "failed": 0}
    assert output["manifest"]["symbols"] == ["BTCUSDC", "SOLUSDC", "TIAUSDC"]
    assert output["manifest"]["assumptions"]["months"] == 6
    assert output["manifest"]["assumptions"]["max_combos"] == 3
    assert output["manifest"]["assumptions"]["strategies"] == ["momentum_rotation"]
    assert len(output["manifest"]["data_hash"]) == 64

"""Safety regressions for the focused momentum optimizer."""

import runpy
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "optimize_momentum.py"
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


def synthetic_ohlcv():
    timestamps = [0, HOUR_MS, 2 * HOUR_MS, 3 * HOUR_MS]
    ref = [candle(ts, 1.0) for ts in timestamps]
    tia = [candle(ts, price) for ts, price in zip(timestamps, [10.0, 20.0, 40.0, 80.0])]
    return {"SOL": ref, "TIA": tia}, ref, timestamps


def load_module_without_main():
    return runpy.run_path(str(SCRIPT), run_name="optimize_momentum_test")


def test_importing_optimize_momentum_has_no_network_or_file_side_effects(monkeypatch, tmp_path, capsys):
    def fail_network(*args, **kwargs):
        raise AssertionError("optimize_momentum import attempted network access")

    import requests

    monkeypatch.setattr(requests, "get", fail_network)
    monkeypatch.chdir(tmp_path)

    namespace = load_module_without_main()

    assert "run_momrot" in namespace
    assert not (tmp_path / "best_momentum.json").exists()
    captured = capsys.readouterr()
    assert "Fetching" not in captured.out


def test_run_momrot_windowed_backtest_sizes_and_values_inside_window():
    namespace = load_module_without_main()
    run_momrot = namespace["run_momrot"]
    build_price_index = namespace["build_price_index"]
    ohlcv, btc, timestamps = synthetic_ohlcv()
    price_index = build_price_index(ohlcv)

    result = run_momrot(
        {"trailing_stop_pct": 100, "momentum_lookback": 24},
        start_ts=HOUR_MS,
        end_ts=2 * HOUR_MS,
        ohlcv_by_coin=ohlcv,
        btc_data=btc,
        price_index=price_index,
        all_timestamps=timestamps,
        initial_balance=100.0,
        starting_coin="TIA",
    )

    # At the window start TIA is $20, so $100 buys 5 TIA. At the window end
    # TIA is $40, so final value is $200. The old behavior sized at $10 and
    # valued at the dataset end $80, incorrectly returning $800.
    assert result["final"] == 200.0
    assert result["pnl"] == 100.0


def test_run_momrot_full_backtest_still_uses_full_dataset():
    namespace = load_module_without_main()
    run_momrot = namespace["run_momrot"]
    build_price_index = namespace["build_price_index"]
    ohlcv, btc, timestamps = synthetic_ohlcv()
    price_index = build_price_index(ohlcv)

    result = run_momrot(
        {"trailing_stop_pct": 100, "momentum_lookback": 24},
        ohlcv_by_coin=ohlcv,
        btc_data=btc,
        price_index=price_index,
        all_timestamps=timestamps,
        initial_balance=100.0,
        starting_coin="TIA",
    )

    assert result["final"] == 800.0
    assert result["pnl"] == 700.0


def test_run_optimization_output_includes_manifest_records_and_gated_leaderboard(tmp_path):
    namespace = load_module_without_main()
    run_optimization = namespace["run_optimization"]
    ohlcv, btc, _timestamps = synthetic_ohlcv()
    output_path = tmp_path / "best_momentum.json"

    payload = run_optimization(
        ohlcv,
        btc,
        max_combos=1,
        output_path=str(output_path),
        initial_balance=100.0,
    )

    assert output_path.exists()
    saved = __import__("json").loads(output_path.read_text())
    assert saved == payload
    assert payload["manifest"]["bridge"] == "USDC"
    assert payload["manifest"]["symbols"] == ["SOLUSDC", "TIAUSDC"]
    assert len(payload["manifest"]["data_hash"]) == 64
    assert payload["manifest"]["assumptions"]["initial_balance"] == 100.0
    assert payload["records"]
    assert payload["records"][0]["strategy"] == "momentum_rotation"
    assert "params" in payload["records"][0]
    assert payload["leaderboard"]["summary"]["total"] == len(payload["records"])

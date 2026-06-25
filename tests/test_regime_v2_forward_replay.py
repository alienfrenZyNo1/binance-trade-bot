"""Tests for cached Regime v2 forward replay harness."""

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "regime_v2_forward_replay.py"
HOUR_MS = 3600 * 1000


def load_module():
    spec = importlib.util.spec_from_file_location("regime_v2_forward_replay_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def candle(ts, close):
    return {"ts": ts, "open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1.0}


def make_dataset(n=140):
    data = {}
    for idx, coin in enumerate(["BTC", "ETH", "SOL", "SUI", "AAVE", "LINK"]):
        price = 100.0 + idx
        rows = []
        for hour in range(n):
            price *= 1.001 + idx / 10000
            rows.append(candle(hour * HOUR_MS, price))
        data[coin] = rows
    return data


def test_cache_key_is_stable_and_order_insensitive():
    module = load_module()

    key_a = module.cache_key(days=30, coins=["SOL", "BTC"], references=["ETH", "BTC"])
    key_b = module.cache_key(days=30, coins=["btc", "sol"], references=["btc", "eth"])

    assert key_a == key_b
    assert key_a.startswith("regime-v2-history-")


def test_load_or_fetch_market_data_uses_cache_after_first_fetch(tmp_path):
    module = load_module()
    calls = []

    def fetcher(coins, *, references, days):
        calls.append((tuple(coins), tuple(references), days))
        return make_dataset()

    data1, meta1 = module.load_or_fetch_market_data(
        cache_dir=tmp_path,
        days=30,
        coins=["SOL", "SUI", "AAVE", "LINK"],
        references=["BTC", "ETH", "SOL"],
        fetcher=fetcher,
    )
    data2, meta2 = module.load_or_fetch_market_data(
        cache_dir=tmp_path,
        days=30,
        coins=["SOL", "SUI", "AAVE", "LINK"],
        references=["BTC", "ETH", "SOL"],
        fetcher=fetcher,
    )

    assert len(calls) == 1
    assert data1 == data2
    assert meta1["cache_hit"] is False
    assert meta2["cache_hit"] is True


def test_evaluate_settings_grid_reuses_same_dataset_for_many_candidates():
    module = load_module()
    settings = [
        {"name": "fast", "step_hours": 12, "warmup_hours": 72, "forward_hours": 12, "selector_lookback": 4},
        {"name": "slow", "step_hours": 24, "warmup_hours": 72, "forward_hours": 24, "selector_lookback": 8},
    ]

    result = module.evaluate_settings_grid(
        make_dataset(),
        settings,
        references=["BTC", "ETH", "SOL"],
        breadth_coins=["SOL", "SUI", "AAVE", "LINK"],
    )

    assert result["summary"]["total_candidates"] == 2
    assert [row["name"] for row in result["candidates"]] == ["fast", "slow"]
    assert result["leaderboard"]
    assert all("best_route" in row for row in result["leaderboard"])
    assert result["leaderboard"][0]["score"] >= result["leaderboard"][-1]["score"]


def test_build_default_settings_can_batch_multiple_windows():
    module = load_module()
    settings = module.build_default_settings(days=[30, 60], step_hours=[12], selector_lookbacks=[6, 12])

    assert len(settings) == 4
    assert {row["name"] for row in settings} == {
        "30d_step12_sel6",
        "30d_step12_sel12",
        "60d_step12_sel6",
        "60d_step12_sel12",
    }


def test_build_default_settings_can_batch_drawdown_guards():
    module = load_module()
    settings = module.build_default_settings(
        days=[60],
        step_hours=[6],
        selector_lookbacks=[3],
        selector_max_trailing_drawdowns=[0.0, 15.0],
        selector_equity_stop_drawdowns=[0.0, 18.0],
    )

    assert len(settings) == 4
    assert settings[0]["name"] == "60d_step6_sel3"
    assert settings[0]["selector_max_trailing_drawdown_pct"] == 0.0
    assert settings[0]["selector_equity_stop_drawdown_pct"] == 0.0
    assert settings[1]["name"] == "60d_step6_sel3_eqstop18"
    assert settings[1]["selector_equity_stop_drawdown_pct"] == 18.0
    assert settings[2]["name"] == "60d_step6_sel3_dd15"
    assert settings[2]["selector_max_trailing_drawdown_pct"] == 15.0
    assert settings[3]["name"] == "60d_step6_sel3_dd15_eqstop18"
    assert settings[3]["selector_max_trailing_drawdown_pct"] == 15.0
    assert settings[3]["selector_equity_stop_drawdown_pct"] == 18.0

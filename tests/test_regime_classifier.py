"""Tests for the research-only multi-signal regime classifier."""

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "research_regime_classifier.py"
HOUR_MS = 3600 * 1000


def load_module():
    spec = importlib.util.spec_from_file_location("regime_classifier_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_series(start=100.0, drift=0.0, n=96, shock=None, wave=0.0):
    rows = []
    price = start
    for i in range(n):
        if shock and i >= shock[0]:
            price *= 1.0 + shock[1]
        else:
            price *= 1.0 + drift
        if wave:
            price = start * (1 + wave * ((i % 8) - 4) / 4)
        high = price * 1.01
        low = price * 0.99
        if shock and i >= shock[0]:
            high = price * 1.05
            low = price * 0.95
        rows.append(
            {
                "ts": i * HOUR_MS,
                "open": price,
                "high": high,
                "low": low,
                "close": price,
                "volume": 1.0,
            }
        )
    return rows


def dataset(drift, *, shock=None, wave=0.0):
    coins = ["BTC", "ETH", "SOL", "SUI", "XRP", "ADA", "DOGE", "NEAR", "LINK", "AAVE"]
    return {
        coin: make_series(start=100.0 + idx, drift=drift, shock=shock, wave=wave)
        for idx, coin in enumerate(coins)
    }


def test_classifies_bull_when_references_and_breadth_trend_up():
    module = load_module()
    result = module.classify_regime(dataset(0.006), breadth_coins=["SOL", "SUI", "XRP", "ADA", "DOGE", "NEAR", "LINK", "AAVE"])

    assert result.regime == module.BULL
    assert result.confidence >= 0.6
    assert result.score > 0


def test_classifies_bear_when_references_and_breadth_trend_down():
    module = load_module()
    result = module.classify_regime(dataset(-0.001), breadth_coins=["SOL", "SUI", "XRP", "ADA", "DOGE", "NEAR", "LINK", "AAVE"])

    assert result.regime == module.BEAR
    assert result.confidence >= 0.6
    assert result.score < 0


def test_classifies_stormy_on_fast_broad_crash():
    module = load_module()
    result = module.classify_regime(dataset(0.0, shock=(72, -0.02)), breadth_coins=["SOL", "SUI", "XRP", "ADA", "DOGE", "NEAR", "LINK", "AAVE"])

    assert result.regime == module.STORMY
    assert any("storm risk" in reason for reason in result.reasons)


def mixed_dataset():
    coins = ["BTC", "ETH", "SOL", "SUI", "XRP", "ADA", "DOGE", "NEAR", "LINK", "AAVE"]
    data = {}
    for idx, coin in enumerate(coins):
        if coin == "BTC":
            drift = 0.001
        elif coin == "ETH":
            drift = -0.001
        elif coin == "SOL":
            drift = 0.0
        else:
            drift = 0.001 if idx % 2 else -0.001
        data[coin] = make_series(start=100.0 + idx, drift=drift)
    return data


def futures_payload(*, basis=-0.01, funding=0.0003, oi_start=100.0, oi_end=110.0, taker=0.8, lsr=2.1):
    mark = 100.0 * (1.0 + basis)
    return {
        "BTCUSDC": {
            "premium": {
                "markPrice": str(mark),
                "indexPrice": "100.0",
                "lastFundingRate": str(funding),
            },
            "funding": [{"fundingRate": str(funding)}],
            "open_interest_hist": [
                {"sumOpenInterestValue": str(oi_start)},
                {"sumOpenInterestValue": str(oi_end)},
            ],
            "global_long_short": [{"longShortRatio": str(lsr)}],
            "top_long_short": [{"longShortRatio": str(lsr)}],
            "taker_long_short": [{"buySellRatio": str(taker)}],
        }
    }


def test_classifies_sideways_on_mixed_low_conviction_market():
    module = load_module()
    result = module.classify_regime(mixed_dataset(), breadth_coins=["SOL", "SUI", "XRP", "ADA", "DOGE", "NEAR", "LINK", "AAVE"])

    assert result.regime == module.SIDEWAYS


def test_futures_metrics_are_aggregated_and_explained():
    module = load_module()
    result = module.classify_regime(
        mixed_dataset(),
        breadth_coins=["SOL", "SUI", "XRP", "ADA", "DOGE", "NEAR", "LINK", "AAVE"],
        futures_data=futures_payload(),
    )

    futures = result.metrics["futures"]
    assert futures["valid_symbols"] == 1
    assert futures["avg_basis_pct"] < -0.5
    assert abs(futures["median_oi_value_change_pct"] - 10.0) < 1e-9
    assert futures["avg_taker_buy_sell_ratio"] == 0.8
    assert any("futures basis negative" in reason for reason in result.reasons)
    assert any("futures taker flow bearish" in reason for reason in result.reasons)

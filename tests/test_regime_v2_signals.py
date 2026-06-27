"""Tests for ``binance_trade_bot.regime_v2_signals`` (issue #102).

All tests use synthetic data — no Binance API access required. Every detector
accepts its data as parameters, mirroring the candidate/research contract.
"""

import math

import pytest

from binance_trade_bot import regime_v2_signals as rv2


# ──────────────────────────────────────────────────────────────────────────
# Synthetic data helpers
# ──────────────────────────────────────────────────────────────────────────

def uptrend(n=80, start=100.0, drift=0.002):
    """A monotonically rising price series (last close > all EMAs)."""
    prices = []
    p = start
    for _ in range(n):
        p *= 1.0 + drift
        prices.append(p)
    return prices


def downtrend(n=80, start=100.0, drift=-0.002):
    """A monotonically falling price series (last close < all EMAs)."""
    return uptrend(n=n, start=start, drift=drift)


def sideways(n=80, start=100.0, amplitude=0.001):
    """A flat oscillating series that hugs `start` (price ≈ EMA)."""
    prices = []
    for i in range(n):
        prices.append(start * (1.0 + amplitude * math.sin(i / 3.0)))
    return prices


def noisy_shock_series(n=80, base=100.0, shock_frac=0.20, shock_at=70):
    """Calm series then a one-bar shock to spike realized volatility."""
    prices = []
    p = base
    for i in range(n):
        p *= 1.0001  # near-flat drift
        if i == shock_at:
            p *= 1.0 - shock_frac
        prices.append(p)
    return prices


# ──────────────────────────────────────────────────────────────────────────
# 1. Breadth
# ──────────────────────────────────────────────────────────────────────────

class TestBreadthSignal:
    def test_all_coins_uptrend_reads_bull(self):
        coin_closes = {
            "SOL": uptrend(),
            "ETH": uptrend(start=200.0),
            "BTC": uptrend(start=300.0),
            "SUI": uptrend(start=10.0),
        }
        result = rv2.breadth_signal(coin_closes)

        assert result["valid_coins"] == 4
        assert result["pct_above_ema50"] == 1.0
        assert result["pct_above_ema20"] == 1.0
        assert result["signal"] == rv2.BULL
        assert result["score"] == pytest.approx(1.0, abs=1e-9)

    def test_all_coins_downtrend_reads_bear(self):
        coin_closes = {
            "SOL": downtrend(),
            "ETH": downtrend(start=200.0),
            "BTC": downtrend(start=300.0),
        }
        result = rv2.breadth_signal(coin_closes)

        assert result["valid_coins"] == 3
        assert result["pct_above_ema50"] == 0.0
        assert result["pct_above_ema50"] < rv2.BREADTH_BEAR_THRESHOLD
        assert result["signal"] == rv2.BEAR
        assert result["score"] == pytest.approx(-1.0, abs=1e-9)

    def test_mixed_market_is_neutral(self):
        # Half the universe rising, half falling ⇒ breadth ~50% ⇒ neutral.
        coin_closes = {
            "A": uptrend(),
            "B": uptrend(start=50.0),
            "C": downtrend(),
            "D": downtrend(start=50.0),
        }
        result = rv2.breadth_signal(coin_closes)

        assert result["valid_coins"] == 4
        assert rv2.BREADTH_BEAR_THRESHOLD < result["pct_above_ema50"] < rv2.BREADTH_BULL_THRESHOLD
        assert result["signal"] == "neutral"
        assert abs(result["score"]) < 0.35  # firmly in the sideways band

    def test_insufficient_history_coin_is_counted_as_not_above(self):
        # One coin with far too little history must not crash the detector and
        # must be excluded from `valid_coins`.
        coin_closes = {
            "A": uptrend(),
            "B": uptrend(start=50.0),
            "SHORT": [100.0, 101.0, 102.0],  # only 3 bars < EMA period
        }
        result = rv2.breadth_signal(coin_closes)

        assert result["valid_coins"] == 2
        assert result["pct_above_ema50"] == 1.0

    def test_empty_input_is_safe(self):
        result = rv2.breadth_signal({})
        assert result["valid_coins"] == 0
        assert result["score"] == 0.0
        assert result["signal"] == "neutral"


# ──────────────────────────────────────────────────────────────────────────
# 2. BTC confirmation
# ──────────────────────────────────────────────────────────────────────────

class TestBtcConfirmation:
    def test_btc_above_ema_confirms_bull(self):
        result = rv2.btc_confirmation(uptrend(n=80))
        assert result["above_ema"] is True
        assert result["signal"] == rv2.BULL
        assert result["score"] == 1.0
        assert result["ema"] is not None and result["price"] > result["ema"]

    def test_btc_below_ema_confirms_bear(self):
        result = rv2.btc_confirmation(downtrend(n=80))
        assert result["above_ema"] is False
        assert result["signal"] == rv2.BEAR
        assert result["score"] == -1.0

    def test_short_history_is_neutral(self):
        result = rv2.btc_confirmation([100.0, 101.0, 102.0])
        assert result["ema"] is None
        assert result["score"] == 0.0
        assert result["signal"] == "neutral"


# ──────────────────────────────────────────────────────────────────────────
# 3. Volatility regime
# ──────────────────────────────────────────────────────────────────────────

class TestVolatilityRegime:
    def test_calm_series_is_low_or_normal(self):
        result = rv2.volatility_regime(uptrend(n=48, drift=0.0005))
        assert result["realized_vol"] < rv2.VOL_LOW_MAX + 1e-9 or result["regime"] in (rv2.VOL_LOW, rv2.VOL_NORMAL)
        assert result["score"] >= 0.0  # calm is not defensive

    def test_shock_series_is_extreme(self):
        # A 40% one-bar drop produces a very large stdev ⇒ extreme.
        result = rv2.volatility_regime(noisy_shock_series(n=80, shock_frac=0.40, shock_at=70))
        assert result["regime"] == rv2.VOL_EXTREME
        assert result["is_extreme"] is True
        assert result["realized_vol"] >= rv2.VOL_HIGH_MAX
        assert result["score"] == -1.0

    def test_regime_ordering_monotonic_with_vol(self):
        calm = rv2.volatility_regime(uptrend(n=48, drift=0.0005))["realized_vol"]
        wild = rv2.volatility_regime(noisy_shock_series(n=80, shock_frac=0.40, shock_at=70))["realized_vol"]
        assert wild > calm

    def test_insufficient_data_returns_normal_default(self):
        result = rv2.volatility_regime([100.0, 101.0, 102.0], window=24)
        assert result["regime"] == rv2.VOL_NORMAL
        assert result["realized_vol"] == 0.0


# ──────────────────────────────────────────────────────────────────────────
# 4. Funding rate signal
# ──────────────────────────────────────────────────────────────────────────

class TestFundingRateSignal:
    def test_high_positive_funding_is_overheated_bull(self):
        result = rv2.funding_rate_signal([0.002, 0.0025, 0.003])
        assert result["signal"] == "overheated_bull"
        assert result["score"] < 0.0  # defensive / exhaustion read

    def test_negative_funding_is_bear_capitulation(self):
        result = rv2.funding_rate_signal([-0.001, -0.002, -0.0015])
        assert result["signal"] == "bear_capitulation"
        assert result["score"] > 0.0  # contrarian/squeeze-leaning

    def test_neutral_funding_band(self):
        result = rv2.funding_rate_signal([0.0001, 0.0, -0.0001])
        assert result["signal"] == "neutral"
        assert result["score"] == 0.0

    def test_empty_input_is_safe(self):
        result = rv2.funding_rate_signal([])
        assert result["signal"] == "neutral"
        assert result["score"] == 0.0
        assert result["funding_rate"] == 0.0

    def test_uses_median_of_multiple_rates(self):
        # Outlier should not dominate: median of [-0.5, 0.001, 0.002] ≈ 0.001.
        result = rv2.funding_rate_signal([-0.5, 0.001, 0.002])
        assert result["funding_rate"] == pytest.approx(0.001, abs=1e-9)
        assert result["signal"] == "overheated_bull"


# ──────────────────────────────────────────────────────────────────────────
# 5. Composite regime scorer
# ──────────────────────────────────────────────────────────────────────────

class TestCompositeRegime:
    def _bull_inputs(self):
        coin_closes = {
            "SOL": uptrend(),
            "ETH": uptrend(start=200.0),
            "SUI": uptrend(start=10.0),
            "AAVE": uptrend(start=50.0),
        }
        return {
            "coin_closes": coin_closes,
            "btc_closes": uptrend(n=80, start=60000.0),
            "vol_closes": uptrend(n=48, drift=0.0005),
            "funding_rates": [0.0001, 0.0002, 0.0],  # mild/neutral funding
        }

    def _bear_inputs(self):
        coin_closes = {
            "SOL": downtrend(),
            "ETH": downtrend(start=200.0),
            "SUI": downtrend(start=10.0),
            "AAVE": downtrend(start=50.0),
        }
        return {
            "coin_closes": coin_closes,
            "btc_closes": downtrend(n=80, start=60000.0),
            "vol_closes": downtrend(n=48, drift=-0.0005),
            "funding_rates": [-0.001, -0.0015, -0.0012],  # capitulation
        }

    def test_clear_bull_inputs_classify_bull(self):
        result = rv2.composite_regime(**self._bull_inputs())
        assert result["regime"] == rv2.BULL
        assert result["score"] >= rv2.SCORE_BULL_MIN
        # Breadth + BTC should agree.
        assert any("agreement" in r for r in result["reasons"])

    def test_clear_bear_inputs_classify_bear(self):
        result = rv2.composite_regime(**self._bear_inputs())
        assert result["regime"] == rv2.BEAR
        assert result["score"] <= rv2.SCORE_BEAR_MAX
        assert any("agreement" in r for r in result["reasons"])

    def test_stormy_override_on_extreme_vol(self):
        # Even with bull breadth + bull BTC, an extreme vol regime forces STORMY
        # (defense-first design).
        inputs = self._bull_inputs()
        inputs["vol_closes"] = noisy_shock_series(n=80, shock_frac=0.40, shock_at=70)
        result = rv2.composite_regime(**inputs)
        assert result["regime"] == rv2.STORMY
        assert result["components"]["volatility"]["is_extreme"] is True
        assert any("STORMY" in r for r in result["reasons"])

    def test_mixed_inputs_fall_to_sideways(self):
        # Breadth bull but BTC bear ⇒ divergence, score near 0 ⇒ SIDEWAYS.
        coin_closes = {
            "SOL": uptrend(),
            "ETH": uptrend(start=200.0),
            "SUI": uptrend(start=10.0),
            "AAVE": uptrend(start=50.0),
        }
        result = rv2.composite_regime(
            coin_closes=coin_closes,
            btc_closes=downtrend(n=80, start=60000.0),
            vol_closes=sideways(n=48),
            funding_rates=[0.0, 0.0001, -0.0001],
        )
        assert result["regime"] == rv2.SIDEWAYS
        assert rv2.SCORE_BEAR_MAX < result["score"] < rv2.SCORE_BULL_MIN

    def test_missing_inputs_do_not_crash_and_renormalize(self):
        # Only breadth provided: weight renormalization should still resolve.
        result = rv2.composite_regime(
            coin_closes={"SOL": uptrend(), "ETH": uptrend(start=200.0)},
        )
        assert result["regime"] in (rv2.BULL, rv2.SIDEWAYS, rv2.BEAR, rv2.STORMY)
        assert -1.0 <= result["score"] <= 1.0
        # The active weight should have been renormalized to ~1.0.
        assert sum(result["weights"].values()) == pytest.approx(1.0, abs=1e-9)

    def test_all_inputs_missing_is_safe(self):
        result = rv2.composite_regime()
        assert result["regime"] == rv2.SIDEWAYS
        assert result["score"] == 0.0

    def test_custom_weights_override_defaults(self):
        # Skew heavily onto breadth so a bearish universe forces BEAR even with
        # neutral BTC.
        weights = {"breadth": 0.9, "btc": 0.05, "volatility": 0.025, "funding": 0.025}
        result = rv2.composite_regime(
            coin_closes={
                "SOL": downtrend(),
                "ETH": downtrend(start=200.0),
                "SUI": downtrend(start=10.0),
                "AAVE": downtrend(start=50.0),
            },
            btc_closes=sideways(n=80),  # neutral-ish BTC
            vol_closes=sideways(n=48),
            funding_rates=[0.0, 0.0, 0.0],
            weights=weights,
        )
        assert result["regime"] == rv2.BEAR

    def test_score_is_bounded(self):
        for inputs in (self._bull_inputs(), self._bear_inputs()):
            result = rv2.composite_regime(**inputs)
            assert -1.0 <= result["score"] <= 1.0

    def test_components_structure(self):
        result = rv2.composite_regime(**self._bull_inputs())
        for key in ("breadth", "btc", "volatility", "funding"):
            assert key in result["components"]
        assert "reasons" in result and isinstance(result["reasons"], list)
        assert "weights" in result and isinstance(result["weights"], dict)

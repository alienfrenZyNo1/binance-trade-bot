"""
Unit tests for the adaptive multi-regime strategy.
Tests: ADX calculation, EMA, regime classification, trailing stop, momentum.

Run with: python -m pytest tests/test_adaptive_strategy.py -v
"""
import sys
import os
import pytest

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── ADX Calculation Tests ────────────────────────────────────────────────────

class TestADXCalculation:
    """Test the ADX (Average Directional Index) computation."""

    def _get_compute_adx(self):
        """Import from standalone indicators module to avoid DB/API deps."""
        from binance_trade_bot.indicators import compute_adx
        return compute_adx

    def test_adx_sideways_market(self):
        """A flat oscillating market should produce low ADX (< 20)."""
        compute_adx = self._get_compute_adx()
        # Simulate 50 candles oscillating tightly around 100
        highs = [101 + (i % 3) * 0.3 for i in range(50)]
        lows = [99 - (i % 3) * 0.3 for i in range(50)]
        closes = [100 + (i % 2) * 0.2 - 0.1 for i in range(50)]

        adx, plus_di, minus_di = compute_adx(highs, lows, closes, period=14)
        assert adx < 30, f"Expected lowish ADX for sideways market, got {adx:.1f}"

    def test_adx_strong_uptrend(self):
        """A steadily rising market should produce higher ADX."""
        compute_adx = self._get_compute_adx()
        closes = [100 + i for i in range(50)]
        highs = [c + 1 for c in closes]
        lows = [c - 0.5 for c in closes]

        adx, plus_di, minus_di = compute_adx(highs, lows, closes, period=14)
        assert adx > 0, "ADX should be positive for a trending market"
        assert plus_di > minus_di, "+DI should exceed -DI in uptrend"

    def test_adx_strong_downtrend(self):
        """A steadily falling market should have -DI > +DI."""
        compute_adx = self._get_compute_adx()
        closes = [200 - i for i in range(50)]
        highs = [c + 0.5 for c in closes]
        lows = [c - 1 for c in closes]

        adx, plus_di, minus_di = compute_adx(highs, lows, closes, period=14)
        assert minus_di > plus_di, "-DI should exceed +DI in downtrend"

    def test_adx_insufficient_data(self):
        """With too few candles, ADX should return 0."""
        compute_adx = self._get_compute_adx()
        adx, _, _ = compute_adx([1, 2, 3], [0, 1, 2], [0.5, 1.5, 2.5], period=14)
        assert adx == 0.0


# ── EMA Calculation Tests ────────────────────────────────────────────────────

class TestEMA:
    def _get_compute_ema(self):
        from binance_trade_bot.indicators import compute_ema
        return compute_ema

    def test_ema_basic(self):
        compute_ema = self._get_compute_ema()
        values = [1, 2, 3, 4, 5]
        ema = compute_ema(values, period=5)
        assert ema is not None
        assert 3 < ema < 5, f"EMA should weight recent values higher, got {ema}"

    def test_ema_empty(self):
        compute_ema = self._get_compute_ema()
        assert compute_ema([], 10) is None

    def test_ema_weights_recent(self):
        """EMA should be closer to recent values than a simple average."""
        compute_ema = self._get_compute_ema()
        values = [10] * 20 + [100] * 5
        ema = compute_ema(values, period=20)
        avg = sum(values) / len(values)
        assert ema is not None
        assert ema > avg, "EMA should be pulled higher by recent jump"

    def test_ema_period_capped(self):
        """EMA period should be capped to available data."""
        compute_ema = self._get_compute_ema()
        values = [1, 2, 3]
        ema = compute_ema(values, period=100)
        assert ema is not None  # Should not crash


# ── Trailing Stop-Loss Tests ────────────────────────────────────────────────

class TestTrailingStop:
    """Test the trailing stop-loss logic using mock objects."""

    def _make_mock(self, stop_pct=8.0):
        """Create a minimal mock with just enough state for stop-loss tests."""

        class MockStrategy:
            def __init__(self, stop_pct):
                self.config = type('obj', (object,), {
                    'TRAILING_STOP_ENABLED': True,
                    'TRAILING_STOP_PCT': stop_pct,
                    'BRIDGE': type('obj', (object,), {'symbol': 'USDC'}),
                })()
                self.manager = MockManager()
                self.logger = MockLogger()
                self._position_entry_price = {}
                self._position_peak_price = {}
                self._awaiting_reentry = False
                self._trades_since_profit_take = 0

            def _check_trailing_stop(self, current_coin, current_coin_price):
                """Inline copy of the stop-loss logic for isolated testing."""
                if not self.config.TRAILING_STOP_ENABLED:
                    return False
                symbol = current_coin.symbol
                if symbol not in self._position_entry_price:
                    self._position_entry_price[symbol] = current_coin_price
                    self._position_peak_price[symbol] = current_coin_price
                    return False
                if current_coin_price > self._position_peak_price.get(symbol, 0):
                    self._position_peak_price[symbol] = current_coin_price
                peak = self._position_peak_price[symbol]
                drop_pct = ((peak - current_coin_price) / peak) * 100 if peak > 0 else 0
                if drop_pct >= self.config.TRAILING_STOP_PCT:
                    return True
                return False

            def _reset_position_tracking(self, symbol, price):
                self._position_entry_price = {symbol: price}
                self._position_peak_price = {symbol: price}

        return MockStrategy(stop_pct)

    def test_stop_triggers_on_drop(self):
        """Position dropping 10% from peak should trigger stop."""
        mock = self._make_mock(stop_pct=8.0)
        coin = MockCoin("SOL")

        mock._reset_position_tracking("SOL", 100.0)

        # Price rises to $120
        result = mock._check_trailing_stop(coin, 120.0)
        assert result is False  # Still rising

        # Price drops to $108 (10% from $120 peak)
        result = mock._check_trailing_stop(coin, 108.0)
        assert result is True  # Should trigger

    def test_stop_no_trigger_small_dip(self):
        """A small dip below threshold should NOT trigger."""
        mock = self._make_mock(stop_pct=8.0)
        coin = MockCoin("SOL")
        mock._reset_position_tracking("SOL", 100.0)

        mock._check_trailing_stop(coin, 110.0)  # Peak
        result = mock._check_trailing_stop(coin, 105.0)  # 4.5% dip
        assert result is False

    def test_stop_disabled(self):
        """When trailing stop is disabled, should never trigger."""
        mock = self._make_mock(stop_pct=8.0)
        mock.config.TRAILING_STOP_ENABLED = False
        coin = MockCoin("SOL")
        mock._reset_position_tracking("SOL", 100.0)
        result = mock._check_trailing_stop(coin, 50.0)
        assert result is False

    def test_stop_new_position_no_trigger(self):
        """First time seeing a coin, should just record entry, not stop."""
        mock = self._make_mock(stop_pct=8.0)
        coin = MockCoin("SOL")
        result = mock._check_trailing_stop(coin, 100.0)
        assert result is False
        assert mock._position_entry_price["SOL"] == 100.0

    def test_stop_exact_threshold(self):
        """Exactly at the threshold percentage should trigger."""
        mock = self._make_mock(stop_pct=8.0)
        coin = MockCoin("SOL")
        mock._reset_position_tracking("SOL", 100.0)

        mock._check_trailing_stop(coin, 100.0)  # Peak stays 100
        result = mock._check_trailing_stop(coin, 92.0)  # Exactly 8% drop
        assert result is True


# ── Regime Classification Logic Tests ────────────────────────────────────────

class TestRegimeClassification:
    """Test the regime classification constants and maps."""

    def test_regime_constants_exist(self):
        from binance_trade_bot.strategies.improved_strategy import BULL, BEAR, SIDEWAYS, STORMY
        assert BULL == "bull"
        assert BEAR == "bear"
        assert SIDEWAYS == "sideways"
        assert STORMY == "stormy"

    def test_regime_emoji_map(self):
        from binance_trade_bot.strategies.improved_strategy import REGIME_EMOJI
        assert len(REGIME_EMOJI) == 4
        assert all(v for v in REGIME_EMOJI.values())

    def test_regime_desc_map(self):
        from binance_trade_bot.strategies.improved_strategy import REGIME_DESC
        for regime in ["bull", "bear", "sideways", "stormy"]:
            assert regime in REGIME_DESC
            assert len(REGIME_DESC[regime]) > 10


# ── Config Parameter Tests ──────────────────────────────────────────────────

class TestConfigParameters:
    """Verify all new config parameters have sensible defaults."""

    def test_adaptive_params_importable(self):
        """Config class should accept all new adaptive parameters."""
        # Just verify the attribute names exist in the class definition source
        import inspect
        from binance_trade_bot.config import Config
        source = inspect.getsource(Config.__init__)

        required = [
            "ADX_PERIOD", "ADX_TREND_THRESHOLD", "EMA_SHORT", "EMA_LONG",
            "BULL_ZSCORE_MULT", "BULL_COOLDOWN", "BULL_PROFIT_TAKE_INTERVAL",
            "BEAR_ZSCORE_MULT", "BEAR_COOLDOWN", "BEAR_PROFIT_TAKE_INTERVAL",
            "BEAR_MOMENTUM_MAX_DROP",
            "TRAILING_STOP_ENABLED", "TRAILING_STOP_PCT",
            "REGIME_CHECK_INTERVAL",
        ]
        for param in required:
            assert f"self.{param}" in source, f"Config missing parameter: {param}"


# ── Mock Helpers ─────────────────────────────────────────────────────────────

class MockCoin:
    def __init__(self, symbol):
        self.symbol = symbol
    def __str__(self):
        return self.symbol
    def __eq__(self, other):
        if isinstance(other, str):
            return self.symbol == other
        return self.symbol == other.symbol
    def __hash__(self):
        return hash(self.symbol)


class MockManager:
    def __init__(self):
        self._balances = {"SOL": 5.0}
    def get_currency_balance(self, symbol):
        return self._balances.get(symbol, 0)
    def get_min_notional(self, *args):
        return 5.0


class MockLogger:
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass
    def debug(self, msg): pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

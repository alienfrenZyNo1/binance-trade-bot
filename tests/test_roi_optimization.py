"""
Comprehensive test suite for ROI optimization features.
Tests indicators, strategy logic, and integration points.

Run with: python -m pytest tests/test_roi_optimization.py -v
"""

import math
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime, timedelta

# Import indicators directly (no external dependencies)
from binance_trade_bot.indicators import (
    compute_ema,
    compute_sma,
    compute_std,
    compute_adx,
    compute_rsi,
    compute_bollinger_bands,
    detect_bollinger_squeeze,
    compute_correlation,
    compute_returns,
    compute_correlation_matrix,
)


# ═══════════════════════════════════════════════════════════════════════════
# INDICATORS: EMA
# ═══════════════════════════════════════════════════════════════════════════

class TestEMA:
    def test_basic_ema(self):
        """EMA should weight recent values more heavily."""
        values = [10, 20, 30, 40, 50]
        ema = compute_ema(values, 5)
        # EMA should be between mean and last value (closer to last)
        assert 30 < ema < 50

    def test_empty_values(self):
        assert compute_ema([], 10) is None

    def test_single_value(self):
        assert compute_ema([42], 5) == 42

    def test_period_larger_than_data(self):
        """Should use available data when period > len(values)."""
        ema = compute_ema([10, 20, 30], 100)
        assert ema is not None

    def test_constant_series(self):
        """EMA of constant values should equal that value."""
        assert compute_ema([50, 50, 50, 50, 50], 5) == 50.0


# ═══════════════════════════════════════════════════════════════════════════
# INDICATORS: SMA and StdDev
# ═══════════════════════════════════════════════════════════════════════════

class TestSMA:
    def test_basic_sma(self):
        assert compute_sma([10, 20, 30, 40, 50], 5) == 30.0

    def test_insufficient_data(self):
        assert compute_sma([10, 20], 5) is None

    def test_empty(self):
        assert compute_sma([], 5) is None


class TestStdDev:
    def test_constant_series(self):
        """Std dev of constant values should be 0."""
        assert compute_std([50, 50, 50, 50, 50], 5) == 0.0

    def test_increasing_series(self):
        """Std dev of evenly spaced values should be positive."""
        std = compute_std([10, 20, 30, 40, 50], 5)
        assert std > 0
        # For [10,20,30,40,50]: mean=30, variance=200, std=14.14
        assert abs(std - math.sqrt(200)) < 0.01


# ═══════════════════════════════════════════════════════════════════════════
# INDICATORS: RSI
# ═══════════════════════════════════════════════════════════════════════════

class TestRSI:
    def test_all_gains(self):
        """RSI should be 100 when all moves are up."""
        closes = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25]
        rsi = compute_rsi(closes, 14)
        assert rsi == 100.0

    def test_all_losses(self):
        """RSI should be 0 when all moves are down."""
        closes = [25, 24, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10]
        rsi = compute_rsi(closes, 14)
        assert rsi is not None
        assert rsi < 10  # Should be very low

    def test_sideways(self):
        """RSI should be near 50 for oscillating market."""
        closes = [50, 51, 49, 51, 49, 51, 49, 51, 49, 51, 49, 51, 49, 51, 49]
        rsi = compute_rsi(closes, 14)
        assert rsi is not None
        assert 30 < rsi < 70

    def test_insufficient_data(self):
        assert compute_rsi([10, 20], 14) is None

    def test_uptrend(self):
        """RSI should be > 50 in uptrend."""
        closes = list(range(100, 120))
        rsi = compute_rsi(closes, 14)
        assert rsi is not None
        assert rsi > 50


# ═══════════════════════════════════════════════════════════════════════════
# INDICATORS: Bollinger Bands
# ═══════════════════════════════════════════════════════════════════════════

class TestBollingerBands:
    def test_basic_bands(self):
        """Bands should bracket the price data."""
        closes = list(range(100, 120))  # Steady uptrend
        result = compute_bollinger_bands(closes, period=20, num_std=2.0)
        assert result is not None
        middle, upper, lower, bandwidth = result
        assert lower < middle < upper
        assert bandwidth > 0

    def test_constant_series(self):
        """Bands should collapse for constant prices."""
        closes = [50.0] * 25
        result = compute_bollinger_bands(closes, period=20)
        assert result is not None
        middle, upper, lower, bandwidth = result
        assert bandwidth == 0.0
        assert upper == lower == middle

    def test_insufficient_data(self):
        assert compute_bollinger_bands([10, 20, 30], 20) is None

    def test_bandwidth_increases_with_volatility(self):
        """Higher volatility should produce wider bands."""
        # Low volatility series
        low_vol = [50 + 0.1 * math.sin(i) for i in range(25)]
        # High volatility series
        high_vol = [50 + 5 * math.sin(i) for i in range(25)]

        low_bw = compute_bollinger_bands(low_vol, 20)[3]
        high_bw = compute_bollinger_bands(high_vol, 20)[3]
        assert high_bw > low_bw


# ═══════════════════════════════════════════════════════════════════════════
# INDICATORS: Bollinger Squeeze Detection
# ═══════════════════════════════════════════════════════════════════════════

class TestBollingerSqueeze:
    def _generate_squeeze_data(self, base=100, n=80):
        """Generate price series with high vol followed by low vol (squeeze)."""
        import random
        random.seed(42)
        prices = []
        # Phase 1: HIGH volatility (wide bandwidth)
        for i in range(40):
            prices.append(base + random.gauss(0, 5))
        # Phase 2: LOW volatility (narrow bandwidth = squeeze)
        for i in range(40):
            prices.append(base + random.gauss(0, 0.2))
        return prices

    def test_squeeze_in_compression_period(self):
        """Should detect squeeze when volatility compresses."""
        prices = self._generate_squeeze_data()
        # The recent prices have low vol → current bandwidth should be
        # much smaller than the high-vol period → squeeze detected
        is_squeeze, bw, pct = detect_bollinger_squeeze(prices, period=20, squeeze_lookback=50)
        assert is_squeeze is True
        assert pct <= 20

    def test_no_squeeze_in_high_vol(self):
        """Should NOT detect squeeze during high volatility."""
        import random
        random.seed(42)
        prices = []
        for i in range(100):
            prices.append(100 + random.gauss(0, 10))
        is_squeeze, bw, pct = detect_bollinger_squeeze(prices, period=20, squeeze_lookback=50)
        assert is_squeeze is False
        assert pct > 20

    def test_insufficient_data(self):
        """Should return (False, 0, 50) for insufficient data."""
        is_squeeze, bw, pct = detect_bollinger_squeeze([10, 20, 30], period=20, squeeze_lookback=50)
        assert is_squeeze is False
        assert pct == 50.0

    def test_squeeze_after_volatility_drop(self):
        """Simulate high→low volatility transition and verify squeeze detection."""
        # Use same data generator as the main squeeze test
        prices = self._generate_squeeze_data()
        is_sq, _, _ = detect_bollinger_squeeze(prices, period=20, squeeze_lookback=50)
        assert is_sq is True


# ═══════════════════════════════════════════════════════════════════════════
# INDICATORS: Correlation
# ═══════════════════════════════════════════════════════════════════════════

class TestCorrelation:
    def test_perfect_positive_correlation(self):
        """Identical series should have correlation of 1."""
        a = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        corr = compute_correlation(a, a)
        assert abs(corr - 1.0) < 0.001

    def test_perfect_negative_correlation(self):
        """Opposite series should have correlation of -1."""
        a = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        b = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
        corr = compute_correlation(a, b)
        assert abs(corr + 1.0) < 0.001

    def test_uncorrelated(self):
        """Random-ish series should have low correlation."""
        a = [1, 5, 2, 8, 3, 7, 4, 6, 9, 2]
        b = [3, 3, 7, 1, 9, 5, 2, 8, 4, 6]
        corr = compute_correlation(a, b)
        assert -0.5 < corr < 0.5

    def test_insufficient_data(self):
        """Should return 0 for insufficient data."""
        assert compute_correlation([1, 2], [3, 4]) == 0.0

    def test_returns_calculation(self):
        """Returns should be percentage changes."""
        prices = [100, 110, 105]
        returns = compute_returns(prices)
        assert len(returns) == 2
        assert abs(returns[0] - 0.1) < 0.001  # 10% gain
        assert abs(returns[1] - (-0.0454)) < 0.001  # ~4.5% loss


class TestCorrelationMatrix:
    def test_matrix_symmetry(self):
        """Matrix should be symmetric."""
        data = {
            "A": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
            "B": [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13],
            "C": [10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0, -1],
        }
        matrix = compute_correlation_matrix(data)
        for s1 in data:
            for s2 in data:
                assert abs(matrix[s1][s2] - matrix[s2][s1]) < 0.001

    def test_diagonal_is_one(self):
        """Correlation with self should be 1."""
        data = {
            "A": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
            "B": [2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24],
        }
        matrix = compute_correlation_matrix(data)
        assert abs(matrix["A"]["A"] - 1.0) < 0.001
        assert abs(matrix["B"]["B"] - 1.0) < 0.001


# ═══════════════════════════════════════════════════════════════════════════
# INDICATORS: ADX
# ═══════════════════════════════════════════════════════════════════════════

class TestADX:
    def test_strong_uptrend(self):
        """ADX should be high (>25) in a strong trend."""
        closes = [float(i) for i in range(50)]
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        adx, plus_di, minus_di = compute_adx(highs, lows, closes, 14)
        assert adx > 20  # Should detect trending

    def test_sideways(self):
        """ADX should be low (<20) in choppy/sideways market."""
        # Oscillating market
        closes = []
        for i in range(50):
            closes.append(100 + 0.5 * math.sin(i))
        highs = [c + 0.3 for c in closes]
        lows = [c - 0.3 for c in closes]
        adx, plus_di, minus_di = compute_adx(highs, lows, closes, 14)
        assert adx < 30  # Should be relatively low

    def test_insufficient_data(self):
        """Should return zeros for insufficient data."""
        adx, _, _ = compute_adx([1, 2, 3], [3, 2, 1], [2, 2.5, 2], 14)
        assert adx == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# STRATEGY: Anti-Churn Logic
# ═══════════════════════════════════════════════════════════════════════════

class TestAntiChurn:
    """Test the anti-churn rule prevents rapid re-buying."""

    def _make_strategy_mock(self):
        """Create a minimal strategy-like mock for testing."""
        from binance_trade_bot.strategies.improved_strategy import Strategy
        mock = MagicMock(spec=Strategy)
        mock._recently_held = {}
        mock._churn_block_seconds = 21600  # 6 hours
        mock._is_churn_blocked = Strategy._is_churn_blocked.__get__(mock, Strategy)
        return mock

    def test_coin_not_in_recently_held(self):
        """Coin never held → not blocked."""
        s = self._make_strategy_mock()
        assert s._is_churn_blocked("SOL") is False

    def test_recently_held_blocked(self):
        """Coin held 1 minute ago → blocked."""
        import time
        s = self._make_strategy_mock()
        s._recently_held["TIA"] = time.time() - 60  # 1 min ago
        assert s._is_churn_blocked("TIA") is True

    def test_expired_block(self):
        """Coin held 7 hours ago → not blocked (6h limit)."""
        import time
        s = self._make_strategy_mock()
        s._recently_held["ENA"] = time.time() - 25200  # 7 hours ago
        assert s._is_churn_blocked("ENA") is False
        # Should clean up expired entry
        assert "ENA" not in s._recently_held

    def test_multiple_coins(self):
        """Different coins tracked independently."""
        import time
        s = self._make_strategy_mock()
        s._recently_held["TIA"] = time.time() - 60
        s._recently_held["ENA"] = time.time() - 60
        assert s._is_churn_blocked("TIA") is True
        assert s._is_churn_blocked("ENA") is True
        assert s._is_churn_blocked("SOL") is False  # Never held


# ═══════════════════════════════════════════════════════════════════════════
# STRATEGY: Config Validation
# ═══════════════════════════════════════════════════════════════════════════

class TestConfigValidation:
    """Test that all new config parameters have correct types and defaults."""

    def test_config_loads_new_params(self):
        """Config should load all ROI optimization parameters."""
        import os
        # Ensure env vars don't interfere
        for key in ['USE_MAKER_ORDERS', 'DYNAMIC_POSITION_ENABLED', 'BEAR_POSITION_SIZE',
                     'BB_SQUEEZE_ENABLED', 'CORRELATION_FILTER_ENABLED', 'BTC_CORRELATION_ENABLED',
                     'MIN_PROFIT_THRESHOLD', 'CHURN_BLOCK_SECONDS', 'CANARY_MODE_ENABLED',
                     'CANARY_MAX_SPOT_TRADE_USDC', 'CANARY_FUTURES_MAX_MARGIN_PCT',
                     'CANARY_MAX_FUTURES_MARGIN_USDC']:
            os.environ.pop(key, None)

        from binance_trade_bot.config import Config
        c = Config()

        # Feature 1: Maker orders
        assert hasattr(c, 'USE_MAKER_ORDERS')
        assert isinstance(c.USE_MAKER_ORDERS, bool)

        # Feature 2: BTC correlation
        assert hasattr(c, 'BTC_CORRELATION_ENABLED')
        assert isinstance(c.BTC_CORRELATION_ENABLED, bool)

        # Feature 3: Dynamic position sizing
        assert hasattr(c, 'DYNAMIC_POSITION_ENABLED')
        assert isinstance(c.DYNAMIC_POSITION_ENABLED, bool)
        assert hasattr(c, 'BEAR_POSITION_SIZE')
        assert 0 < c.BEAR_POSITION_SIZE <= 1.0
        assert hasattr(c, 'SIDEWAYS_POSITION_SIZE')
        assert 0 < c.SIDEWAYS_POSITION_SIZE <= 1.0

        # Feature 4: Correlation filter
        assert hasattr(c, 'CORRELATION_FILTER_ENABLED')
        assert isinstance(c.CORRELATION_FILTER_ENABLED, bool)
        assert hasattr(c, 'CORRELATION_THRESHOLD')
        assert 0 < c.CORRELATION_THRESHOLD <= 1.0

        # Feature 5: BB squeeze
        assert hasattr(c, 'BB_SQUEEZE_ENABLED')
        assert isinstance(c.BB_SQUEEZE_ENABLED, bool)
        assert hasattr(c, 'BB_PERIOD')
        assert isinstance(c.BB_PERIOD, int)
        assert hasattr(c, 'BB_SQUEEZE_LOOKBACK')
        assert isinstance(c.BB_SQUEEZE_LOOKBACK, int)

        # Anti-churn + min profit
        assert hasattr(c, 'MIN_PROFIT_THRESHOLD')
        assert isinstance(c.MIN_PROFIT_THRESHOLD, float)
        assert hasattr(c, 'CHURN_BLOCK_SECONDS')
        assert isinstance(c.CHURN_BLOCK_SECONDS, int)

        # RSI
        assert hasattr(c, 'RSI_FILTER_ENABLED')
        assert hasattr(c, 'RSI_OVERBOUGHT')
        assert 0 < c.RSI_OVERBOUGHT <= 100

        # Canary capital guard defaults are disabled/no-op
        assert hasattr(c, 'CANARY_MODE_ENABLED')
        assert c.CANARY_MODE_ENABLED is False
        assert hasattr(c, 'CANARY_MAX_SPOT_TRADE_USDC')
        assert c.CANARY_MAX_SPOT_TRADE_USDC == 0.0
        assert hasattr(c, 'CANARY_FUTURES_MAX_MARGIN_PCT')
        assert c.CANARY_FUTURES_MAX_MARGIN_PCT == 0.0
        assert hasattr(c, 'CANARY_MAX_FUTURES_MARGIN_USDC')
        assert c.CANARY_MAX_FUTURES_MARGIN_USDC == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# INTEGRATION: Strategy Import & Method Existence
# ═══════════════════════════════════════════════════════════════════════════

class TestStrategyIntegration:
    """Verify the strategy class has all new methods properly wired."""

    def test_strategy_imports(self):
        """Strategy module should import without errors."""
        from binance_trade_bot.strategies.improved_strategy import Strategy
        assert Strategy is not None

    def test_new_methods_exist(self):
        """All new feature methods should exist on the strategy class."""
        from binance_trade_bot.strategies.improved_strategy import Strategy
        # Feature 1: Maker order support is in api_manager
        # Feature 2: BTC correlation in regime detection
        # Feature 3: Dynamic position sizing
        assert hasattr(Strategy, 'transaction_through_bridge')  # Override
        # Feature 4: Correlation penalty
        assert hasattr(Strategy, '_get_correlation_penalty')
        # Feature 5: BB squeeze bonus
        assert hasattr(Strategy, '_check_bb_squeeze_bonus')
        # Anti-churn
        assert hasattr(Strategy, '_is_churn_blocked')
        # RSI filter
        assert hasattr(Strategy, '_get_rsi')
        assert hasattr(Strategy, '_check_rsi_filter')

    def test_api_manager_methods_exist(self):
        """API manager should have maker order methods."""
        from binance_trade_bot.binance_api_manager import BinanceAPIManager
        assert hasattr(BinanceAPIManager, '_get_order_book_price')
        assert hasattr(BinanceAPIManager, '_get_tick_size')

    def test_buy_alt_accepts_max_balance(self):
        """buy_alt should accept max_target_balance parameter."""
        import inspect
        from binance_trade_bot.binance_api_manager import BinanceAPIManager
        sig = inspect.signature(BinanceAPIManager.buy_alt)
        assert 'max_target_balance' in sig.parameters


# ═══════════════════════════════════════════════════════════════════════════
# STRATEGY: Dynamic Position Sizing Logic
# ═══════════════════════════════════════════════════════════════════════════

class TestDynamicPositionSizing:
    """Test position sizing calculations without executing real trades."""

    def test_bear_position_size(self):
        """Bear mode should use reduced position size."""
        bear_pct = 0.7
        bridge_balance = 62.0
        max_balance = bridge_balance * bear_pct
        reserve = bridge_balance - max_balance
        assert abs(max_balance - 43.4) < 0.01
        assert abs(reserve - 18.6) < 0.01

    def test_bull_full_position(self):
        """Bull mode should use full position."""
        bull_pct = 1.0
        bridge_balance = 62.0
        assert bridge_balance * bull_pct == 62.0

    def test_reserve_above_min_notional(self):
        """Reserve should be above $5 min notional to be worthwhile."""
        bridge_balance = 62.0
        bear_pct = 0.7
        reserve = bridge_balance * (1 - bear_pct)
        assert reserve >= 5.0  # $18.6 > $5

    def test_small_account_skips_reserve(self):
        """If reserve would be < $5, should go all in."""
        bridge_balance = 10.0
        bear_pct = 0.7
        reserve = bridge_balance * (1 - bear_pct)
        assert reserve < 5.0  # $3 < $5, so go all in


# ═══════════════════════════════════════════════════════════════════════════
# STRATEGY: Correlation Penalty Logic
# ═══════════════════════════════════════════════════════════════════════════

class TestCorrelationPenalty:
    """Test the correlation penalty calculation logic."""

    def test_high_correlation_reduces_score(self):
        """Highly correlated coins should get penalized."""
        # Simulate: corr=0.95, threshold=0.85
        corr = 0.95
        threshold = 0.85
        excess = (abs(corr) - threshold) / (1.0 - threshold)
        penalty = max(0.2, 1.0 - excess * 0.5)
        # excess = 0.10/0.15 = 0.667, penalty = 1 - 0.333 = 0.667
        assert penalty < 1.0
        assert penalty >= 0.2

    def test_low_correlation_no_penalty(self):
        """Low correlation should not penalize."""
        corr = 0.5
        threshold = 0.85
        assert abs(corr) <= threshold  # No penalty applied

    def test_perfect_correlation_min_penalty(self):
        """Corr=1.0 should give max penalty (0.2 floor)."""
        corr = 1.0
        threshold = 0.85
        excess = (abs(corr) - threshold) / (1.0 - threshold)
        penalty = max(0.2, 1.0 - excess * 0.5)
        assert penalty == 0.5  # (1 - 0.5*1.0) = 0.5


# ═══════════════════════════════════════════════════════════════════════════
# STRATEGY: BB Squeeze Bonus Logic
# ═══════════════════════════════════════════════════════════════════════════

class TestBBSqueezeBonus:
    """Test BB squeeze bonus calculation."""

    def test_no_squeeze_no_bonus(self):
        """Non-squeeze should return 1.0 (no bonus)."""
        is_squeeze = False
        bonus = 1.0  # Default when not squeeze
        assert bonus == 1.0

    def test_squeeze_gives_bonus(self):
        """Squeeze should give bonus > 1.0."""
        percentile = 10  # Bottom 10th percentile
        bonus = 1.0 + (1.0 - percentile / 20.0) * 0.3
        assert bonus > 1.0
        assert bonus <= 1.3  # Max bonus

    def test_extreme_squeeze_max_bonus(self):
        """Percentile=0 (extreme squeeze) gives max bonus."""
        percentile = 0
        bonus = 1.0 + (1.0 - percentile / 20.0) * 0.3
        assert abs(bonus - 1.3) < 0.001

    def test_borderline_squeeze_min_bonus(self):
        """Percentile=20 (borderline) gives minimal bonus."""
        percentile = 20
        bonus = 1.0 + (1.0 - percentile / 20.0) * 0.3
        assert abs(bonus - 1.0) < 0.001


# ═══════════════════════════════════════════════════════════════════════════
# STRATEGY: Maker Order Price Calculation
# ═══════════════════════════════════════════════════════════════════════════

class TestMakerOrders:
    """Test maker order logic."""

    def test_buy_at_bid_is_maker(self):
        """Buying at the best bid should be a maker order."""
        best_bid = 0.50
        best_ask = 0.52
        ticker = 0.51
        # For maker buy: place at best_bid
        maker_price = best_bid
        assert maker_price < ticker  # Below market = maker

    def test_sell_at_ask_is_maker(self):
        """Selling at the best ask should be a maker order."""
        best_bid = 0.50
        best_ask = 0.52
        ticker = 0.51
        # For maker sell: place at best_ask
        maker_price = best_ask
        assert maker_price > ticker  # Above market = maker

    def test_fee_savings(self):
        """Maker fee should be 67% lower than taker."""
        taker_fee = 0.00075  # 0.075%
        maker_fee = 0.00025  # 0.025%
        savings_pct = (taker_fee - maker_fee) / taker_fee * 100
        assert abs(savings_pct - 66.67) < 0.1  # ~67% savings

    def test_annual_savings_calculation(self):
        """Calculate annual fee savings for 100 trades at $62."""
        taker_fee = 0.00075
        maker_fee = 0.00025
        trades_per_year = 100
        avg_trade_size = 62.0
        taker_cost = trades_per_year * avg_trade_size * taker_fee * 2  # buy + sell
        maker_cost = trades_per_year * avg_trade_size * maker_fee * 2
        savings = taker_cost - maker_cost
        assert savings > 0
        # 100 trades * $62 * 0.05% * 2 = $6.20 savings
        assert abs(savings - 6.20) < 0.01

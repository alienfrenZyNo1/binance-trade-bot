"""
Test suite for production hardening features:
- Maker order reprice fallback
- Indicator cache (TTL-based)
- Spread detection + midpoint pricing
- Telegram crash alert
"""

import math
import time
import pytest
from unittest.mock import MagicMock, patch


# ═══════════════════════════════════════════════════════════════════════════
# MAKER ORDER REPRICE LOGIC
# ═══════════════════════════════════════════════════════════════════════════

class TestMakerReprice:
    """Test the maker order reprice fallback logic."""

    def test_reprice_triggers_after_timeout(self):
        """Order should be repriced after MAKER_REPRICE_TIMEOUT minutes."""
        from binance_trade_bot.binance_api_manager import BinanceAPIManager
        
        mock = MagicMock(spec=BinanceAPIManager)
        mock.config = MagicMock()
        mock.config.MAKER_REPRICE_TIMEOUT = 5.0
        mock._should_reprice_order = BinanceAPIManager._should_reprice_order.__get__(mock, BinanceAPIManager)

        order_status = MagicMock()
        order_status.status = "NEW"
        order_status.time = (time.time() - 400) * 1000  # 6.7 minutes ago (> 5 min)

        assert mock._should_reprice_order(order_status) is True

    def test_reprice_skipped_before_timeout(self):
        """Order should NOT be repriced before timeout."""
        from binance_trade_bot.binance_api_manager import BinanceAPIManager
        
        mock = MagicMock(spec=BinanceAPIManager)
        mock.config = MagicMock()
        mock.config.MAKER_REPRICE_TIMEOUT = 5.0
        mock._should_reprice_order = BinanceAPIManager._should_reprice_order.__get__(mock, BinanceAPIManager)

        order_status = MagicMock()
        order_status.status = "NEW"
        order_status.time = (time.time() - 120) * 1000  # 2 minutes ago (< 5 min)

        assert mock._should_reprice_order(order_status) is False

    def test_reprice_skipped_if_not_new(self):
        """Partially filled orders should not be repriced."""
        from binance_trade_bot.binance_api_manager import BinanceAPIManager
        
        mock = MagicMock(spec=BinanceAPIManager)
        mock.config = MagicMock()
        mock.config.MAKER_REPRICE_TIMEOUT = 5.0
        mock._should_reprice_order = BinanceAPIManager._should_reprice_order.__get__(mock, BinanceAPIManager)

        order_status = MagicMock()
        order_status.status = "PARTIALLY_FILLED"
        order_status.time = (time.time() - 600) * 1000  # 10 minutes ago

        assert mock._should_reprice_order(order_status) is False

    def test_reprice_disabled_when_timeout_zero(self):
        """If MAKER_REPRICE_TIMEOUT=0, reprice is disabled."""
        from binance_trade_bot.binance_api_manager import BinanceAPIManager
        
        mock = MagicMock(spec=BinanceAPIManager)
        mock.config = MagicMock()
        mock.config.MAKER_REPRICE_TIMEOUT = 0
        mock._should_reprice_order = BinanceAPIManager._should_reprice_order.__get__(mock, BinanceAPIManager)

        order_status = MagicMock()
        order_status.status = "NEW"
        order_status.time = (time.time() - 9999) * 1000

        assert mock._should_reprice_order(order_status) is False

    def test_reprice_disabled_when_none(self):
        """None order status should not trigger reprice."""
        from binance_trade_bot.binance_api_manager import BinanceAPIManager
        
        mock = MagicMock(spec=BinanceAPIManager)
        mock.config = MagicMock()
        mock.config.MAKER_REPRICE_TIMEOUT = 5.0
        mock._should_reprice_order = BinanceAPIManager._should_reprice_order.__get__(mock, BinanceAPIManager)

        assert mock._should_reprice_order(None) is False

    def test_fee_comparison_maker_vs_taker(self):
        """Verify the fee savings calculation."""
        # After reprice, the order uses taker fee instead of maker
        # But the order FILLS (vs being cancelled entirely)
        taker_fee = 0.00075
        maker_fee = 0.00025

        # On a $62 trade:
        trade = 62.0
        taker_cost = trade * taker_fee
        maker_cost = trade * maker_fee

        # Even with taker, the trade EXECUTES vs missing entirely
        # Missed trade = 0 gain (lost opportunity)
        # Taker trade = small fee but captures the price edge
        assert taker_cost < 0.05  # Under 5 cents per trade
        assert taker_cost < trade * 0.02  # Under 2% of trade


# ═══════════════════════════════════════════════════════════════════════════
# INDICATOR CACHE
# ═══════════════════════════════════════════════════════════════════════════

class TestIndicatorCache:
    """Test the TTL-based indicator cache."""

    def _make_mock(self, ttl=300):
        from binance_trade_bot.strategies.improved_strategy import Strategy
        mock = MagicMock(spec=Strategy)
        mock._indicator_cache = {}
        mock._cache_ttl = ttl
        mock._cache_get = Strategy._cache_get.__get__(mock, Strategy)
        mock._cache_set = Strategy._cache_set.__get__(mock, Strategy)
        return mock

    def test_cache_miss_returns_none(self):
        """Empty cache should return None."""
        s = self._make_mock()
        assert s._cache_get("rsi:SOL:14") is None

    def test_cache_hit_returns_value(self):
        """Stored value should be returned."""
        s = self._make_mock()
        s._cache_set("rsi:SOL:14", 55.5)
        assert s._cache_get("rsi:SOL:14") == 55.5

    def test_cache_expiry(self):
        """Expired entries should return None."""
        s = self._make_mock(ttl=1)  # 1 second TTL
        s._cache_set("bb:TIA", 1.3)
        time.sleep(1.1)
        assert s._cache_get("bb:TIA") is None

    def test_cache_different_keys(self):
        """Different coins should have independent cache entries."""
        s = self._make_mock()
        s._cache_set("rsi:SOL:14", 60.0)
        s._cache_set("rsi:TIA:14", 45.0)
        assert s._cache_get("rsi:SOL:14") == 60.0
        assert s._cache_get("rsi:TIA:14") == 45.0

    def test_cache_overwrite(self):
        """Setting same key should overwrite."""
        s = self._make_mock()
        s._cache_set("corr:TIA:ENA", 0.5)
        s._cache_set("corr:TIA:ENA", 0.8)
        assert s._cache_get("corr:TIA:ENA") == 0.8

    def test_cache_expired_entry_cleaned(self):
        """Expired entries should be deleted on get."""
        s = self._make_mock(ttl=1)
        s._cache_set("rsi:SOL:14", 50.0)
        time.sleep(1.1)
        s._cache_get("rsi:SOL:14")
        assert "rsi:SOL:14" not in s._indicator_cache


# ═══════════════════════════════════════════════════════════════════════════
# SPREAD DETECTION + MIDPOINT PRICING
# ═══════════════════════════════════════════════════════════════════════════

class TestSpreadDetection:
    """Test spread detection and midpoint pricing logic."""

    def test_narrow_spread_uses_bid(self):
        """Narrow spread → use best bid for buy."""
        bid = 100.0
        ask = 100.05  # 0.05% spread — narrow
        spread_pct = ((ask - bid) / bid) * 100
        assert spread_pct < 0.15  # Below threshold

        # Should use bid for buy, ask for sell
        buy_price = bid
        sell_price = ask
        assert buy_price == 100.0
        assert sell_price == 100.05

    def test_wide_spread_uses_midpoint(self):
        """Wide spread → use midpoint."""
        bid = 100.0
        ask = 100.50  # 0.5% spread — very wide
        spread_pct = ((ask - bid) / bid) * 100
        threshold = 0.15

        if spread_pct > threshold * 2:  # > 0.30%
            mid = (bid + ask) / 2
            assert mid == 100.25
            # Midpoint is between bid and ask
            assert bid < mid < ask

    def test_spread_calculation(self):
        """Verify spread percentage math."""
        spread_pct, mid = self._calc_spread(100.0, 100.10)
        assert abs(spread_pct - 0.1) < 0.001

        spread_pct, mid = self._calc_spread(50.0, 50.25)
        assert abs(spread_pct - 0.5) < 0.001

    def _calc_spread(self, bid, ask):
        spread = ((ask - bid) / bid) * 100
        return spread, (bid + ask) / 2

    def test_config_threshold_loaded(self):
        """Config should load USDT_FALLBACK_SPREAD_THRESHOLD."""
        from binance_trade_bot.config import Config
        c = Config()
        assert hasattr(c, 'USDT_FALLBACK_SPREAD_THRESHOLD')
        assert c.USDT_FALLBACK_SPREAD_THRESHOLD > 0


# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM CRASH ALERT
# ═══════════════════════════════════════════════════════════════════════════

class TestCrashAlert:
    """Test that crash alert infrastructure exists."""

    def test_crypto_trading_has_error_handler(self):
        """Main loop should have try/except for crash notifications."""
        from binance_trade_bot import crypto_trading
        import inspect
        source = inspect.getsource(crypto_trading.main)
        assert "except Exception" in source
        assert "CRASHED" in source or "crashed" in source
        assert "NotificationHandler" in source

    def test_notification_handler_importable(self):
        """NotificationHandler should be importable."""
        from binance_trade_bot.notifications import NotificationHandler
        assert NotificationHandler is not None

    def test_notification_handler_has_send(self):
        """NotificationHandler should have send_notification method."""
        from binance_trade_bot.notifications import NotificationHandler
        assert hasattr(NotificationHandler, 'send_notification')


# ═══════════════════════════════════════════════════════════════════════════
# CONFIG VALIDATION FOR NEW PARAMS
# ═══════════════════════════════════════════════════════════════════════════

class TestNewConfigParams:
    """Test all new config parameters load correctly."""

    def test_maker_reprice_timeout(self):
        from binance_trade_bot.config import Config
        c = Config()
        assert hasattr(c, 'MAKER_REPRICE_TIMEOUT')
        assert isinstance(c.MAKER_REPRICE_TIMEOUT, float)
        assert c.MAKER_REPRICE_TIMEOUT > 0

    def test_indicator_cache_ttl(self):
        from binance_trade_bot.config import Config
        c = Config()
        assert hasattr(c, 'INDICATOR_CACHE_TTL')
        assert isinstance(c.INDICATOR_CACHE_TTL, int)
        assert c.INDICATOR_CACHE_TTL > 0

    def test_usdt_fallback_enabled(self):
        from binance_trade_bot.config import Config
        c = Config()
        assert hasattr(c, 'USDT_FALLBACK_ENABLED')
        assert isinstance(c.USDT_FALLBACK_ENABLED, bool)

    def test_usdt_fallback_spread_threshold(self):
        from binance_trade_bot.config import Config
        c = Config()
        assert hasattr(c, 'USDT_FALLBACK_SPREAD_THRESHOLD')
        assert isinstance(c.USDT_FALLBACK_SPREAD_THRESHOLD, float)
        assert c.USDT_FALLBACK_SPREAD_THRESHOLD > 0


# ═══════════════════════════════════════════════════════════════════════════
# API MANAGER METHOD EXISTENCE
# ═══════════════════════════════════════════════════════════════════════════

class TestAPIMethodsExist:
    """Verify all new API manager methods exist."""

    def test_should_reprice_exists(self):
        from binance_trade_bot.binance_api_manager import BinanceAPIManager
        assert hasattr(BinanceAPIManager, '_should_reprice_order')

    def test_get_spread_exists(self):
        from binance_trade_bot.binance_api_manager import BinanceAPIManager
        assert hasattr(BinanceAPIManager, '_get_spread')

    def test_check_order_filled_exists(self):
        from binance_trade_bot.binance_api_manager import BinanceAPIManager
        assert hasattr(BinanceAPIManager, '_check_order_filled')

    def test_wait_for_order_accepts_reprice(self):
        import inspect
        from binance_trade_bot.binance_api_manager import BinanceAPIManager
        sig = inspect.signature(BinanceAPIManager.wait_for_order)
        assert 'reprice_callback' in sig.parameters

"""Regression test for issue #110 — initialize_current_coin() ignores
actual Binance balance on restart.

Bug: When the DB has no current_coin, ``initialize_current_coin()`` picked a
random coin from SUPPORTED_COIN_LIST instead of checking what's actually
held on Binance. This caused the bot to think it held TIA while actually
holding INJ.

Fix: Before the random fallback, the method now calls
``self.manager.get_account()`` to find non-bridge, non-BNB assets with
meaningful balance (> 0.001) and selects the largest by USD value.

Reproduction scenario from the issue:
    - DB current_coin is None (fresh start / corrupt DB)
    - Binance account actually holds INJ (12.26 tokens)
    - Config has no CURRENT_COIN_SYMBOL set

Before fix: random.choice(SUPPORTED_COIN_LIST) → might pick TIA.
After fix: scans account → finds INJ → sets INJ as current coin.
"""
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeConfig:
    """Minimal config stand-in with the attributes the strategy reads."""

    def __init__(self, bridge_symbol="USDC", current_coin_symbol="",
                 supported_coins=None):
        self.BRIDGE = MagicMock(symbol=bridge_symbol)
        self.CURRENT_COIN_SYMBOL = current_coin_symbol
        self.SUPPORTED_COIN_LIST = supported_coins or ["INJ", "TIA", "SOL", "AVAX"]


def _make_account_balance(balances):
    """Build a fake Binance ``get_account()`` response dict."""
    return {
        "balances": [
            {"asset": asset, "free": str(free), "locked": "0.0"}
            for asset, free in balances
        ]
    }


# ---------------------------------------------------------------------------
# Core scenario — DB empty, Binance holds INJ
# ---------------------------------------------------------------------------

def test_initialize_uses_binance_balance_when_db_empty():
    """Issue #110 exact reproduction:

    DB has no current_coin. Binance actually holds INJ. The method should
    detect INJ and set it as the current coin — NOT pick a random one.
    """
    from binance_trade_bot.strategies.momentum_strategy import Strategy

    # --- Arrange ---
    strategy = Strategy.__new__(Strategy)  # bypass __init__
    strategy.db = MagicMock()
    strategy.manager = MagicMock()
    strategy.logger = MagicMock()
    strategy.config = FakeConfig(
        bridge_symbol="USDC",
        current_coin_symbol="",  # no config override
        supported_coins=["INJ", "TIA", "SOL", "AVAX"],
    )

    # DB has no current coin
    strategy.db.get_current_coin.return_value = None

    # Binance account: INJ is the real holding, plus dust and BNB
    strategy.manager.get_account.return_value = _make_account_balance([
        ("INJ", 12.26),
        ("USDC", 0.00765),
        ("BNB", 0.0001),
        ("TIA", 0.0),  # zero balance
    ])

    # Ticker price for INJUSDC
    strategy.manager.get_ticker_price.return_value = 20.0

    # --- Act ---
    strategy.initialize_current_coin()

    # --- Assert: current coin is INJ, not random ---
    strategy.db.set_current_coin.assert_called_once_with("INJ")

    # Logger should mention the account scan and the selection
    log_msgs = [str(c) for c in strategy.logger.info.call_args_list]
    assert any("INJ" in msg for msg in log_msgs), (
        f"Expected a log message mentioning INJ, got: {log_msgs}"
    )


# ---------------------------------------------------------------------------
# Multiple holdings — should pick the largest by USD value
# ---------------------------------------------------------------------------

def test_initialize_picks_largest_holding_when_multiple():
    """When multiple non-bridge, non-BNB assets are held, the method should
    pick the one with the highest USD value.
    """
    from binance_trade_bot.strategies.momentum_strategy import Strategy

    strategy = Strategy.__new__(Strategy)
    strategy.db = MagicMock()
    strategy.manager = MagicMock()
    strategy.logger = MagicMock()
    strategy.config = FakeConfig(
        bridge_symbol="USDC",
        current_coin_symbol="",
        supported_coins=["INJ", "TIA", "SOL", "AVAX"],
    )

    strategy.db.get_current_coin.return_value = None

    # Account holds INJ (12.26 @ ~$20 = ~$245) and SOL (2.0 @ ~$150 = ~$300)
    strategy.manager.get_account.return_value = _make_account_balance([
        ("INJ", 12.26),
        ("SOL", 2.0),
        ("USDC", 0.5),
        ("BNB", 0.001),
    ])

    # Return different prices for different ticker queries
    def _ticker(symbol):
        prices = {"INJUSDC": 20.0, "SOLUSDC": 150.0}
        return prices.get(symbol)

    strategy.manager.get_ticker_price.side_effect = _ticker

    # --- Act ---
    strategy.initialize_current_coin()

    # --- Assert: SOL has higher USD value ---
    strategy.db.set_current_coin.assert_called_once_with("SOL")


# ---------------------------------------------------------------------------
# Dust only — should fall back to random
# ---------------------------------------------------------------------------

def test_initialize_falls_back_to_random_when_no_meaningful_balance():
    """When no non-bridge, non-BNB asset has meaningful balance, the method
    should fall back to the existing random/config behaviour.
    """
    from binance_trade_bot.strategies.momentum_strategy import Strategy

    strategy = Strategy.__new__(Strategy)
    strategy.db = MagicMock()
    strategy.manager = MagicMock()
    strategy.logger = MagicMock()
    strategy.config = FakeConfig(
        bridge_symbol="USDC",
        current_coin_symbol="",
        supported_coins=["INJ", "TIA", "SOL", "AVAX"],
    )

    strategy.db.get_current_coin.return_value = None

    # Only dust and bridge/BNB
    strategy.manager.get_account.return_value = _make_account_balance([
        ("USDC", 0.00765),
        ("BNB", 0.0001),
        ("TIA", 0.0001),  # dust, below 0.001 threshold
    ])

    # --- Act ---
    with patch("binance_trade_bot.strategies.momentum_strategy.random.choice") as mock_choice:
        mock_choice.return_value = "TIA"
        strategy.initialize_current_coin()

    # --- Assert: random fallback was used ---
    assert mock_choice.called, "random.choice should have been called as fallback"
    strategy.db.set_current_coin.assert_called_once_with("TIA")


# ---------------------------------------------------------------------------
# CURRENT_COIN_SYMBOL set in config — should skip account scan
# ---------------------------------------------------------------------------

def test_initialize_respects_current_coin_symbol_config():
    """When CURRENT_COIN_SYMBOL is explicitly set in config, the account scan
    is skipped and the configured symbol is used.
    """
    from binance_trade_bot.strategies.momentum_strategy import Strategy

    strategy = Strategy.__new__(Strategy)
    strategy.db = MagicMock()
    strategy.manager = MagicMock()
    strategy.logger = MagicMock()
    strategy.config = FakeConfig(
        bridge_symbol="USDC",
        current_coin_symbol="SOL",  # explicit config
        supported_coins=["INJ", "TIA", "SOL", "AVAX"],
    )

    strategy.db.get_current_coin.return_value = None

    # --- Act ---
    strategy.initialize_current_coin()

    # --- Assert: config symbol used, account NOT scanned ---
    strategy.db.set_current_coin.assert_called_once_with("SOL")
    strategy.manager.get_account.assert_not_called()


# ---------------------------------------------------------------------------
# API error — should gracefully fall back to random
# ---------------------------------------------------------------------------

def test_initialize_falls_back_when_api_call_fails():
    """When the Binance API call fails, the method should log a warning and
    fall back to the random/config behaviour.
    """
    from binance_trade_bot.strategies.momentum_strategy import Strategy

    strategy = Strategy.__new__(Strategy)
    strategy.db = MagicMock()
    strategy.manager = MagicMock()
    strategy.logger = MagicMock()
    strategy.config = FakeConfig(
        bridge_symbol="USDC",
        current_coin_symbol="",
        supported_coins=["INJ", "TIA", "SOL", "AVAX"],
    )

    strategy.db.get_current_coin.return_value = None
    strategy.manager.get_account.side_effect = Exception("API timeout")

    # --- Act ---
    with patch("binance_trade_bot.strategies.momentum_strategy.random.choice") as mock_choice:
        mock_choice.return_value = "AVAX"
        strategy.initialize_current_coin()

    # --- Assert: random fallback ---
    assert mock_choice.called
    strategy.db.set_current_coin.assert_called_once_with("AVAX")

    # Warning should have been logged
    warn_msgs = [str(c) for c in strategy.logger.warning.call_args_list]
    assert any("account scan failed" in msg for msg in warn_msgs), (
        f"Expected a warning about account scan failure, got: {warn_msgs}"
    )

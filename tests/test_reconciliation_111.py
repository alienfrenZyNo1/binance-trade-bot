"""Regression test for issue #111 — reconciliation self-heal on small balances.

Bug: When the DB ``current_coin`` doesn't match the actual Binance holding AND
balances are small, the reconciliation code logged "seems OK" and did nothing.
The account scan code existed but was gated behind ``bridge_balance > 1.0`` so
it never ran for small bridge balances.

Fix: A final account-scan fallback was added before the "seems OK" log line.
When the DB coin balance is below ``min_notional`` AND bridge balance is below
threshold, it scans the account for the largest non-bridge, non-BNB holding and
sets ``current_coin`` to match reality.

Reproduction scenario from the issue:
    - DB ``current_coin`` says TIA
    - Actual Binance holding is INJ (12.26 tokens)
    - Bridge (USDC) balance is $0.00765 (below 1.0 threshold)
    - TIA balance is 0.01 (below min_notional)
    - No futures balance

Before fix: function falls through to "seems OK" — DB stays wrong.
After fix: function scans account, finds INJ, sets current_coin to INJ.
"""
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeCoin:
    """Lightweight stand-in for models.Coin."""

    def __init__(self, symbol, enabled=True):
        self.symbol = symbol
        self.enabled = enabled

    def __repr__(self):
        return f"Coin({self.symbol})"


class FakeConfig:
    def __init__(self, bridge_symbol="USDC"):
        self.BRIDGE = MagicMock(symbol=bridge_symbol)


def _make_account_balance(balances):
    """Build a fake Binance ``get_account()`` response dict."""
    return {
        "balances": [
            {"asset": asset, "free": str(free), "locked": "0.0"}
            for asset, free in balances
        ]
    }


# ---------------------------------------------------------------------------
# Structural test — ensures the account-scan fallback exists outside the
# bridge_balance > 1.0 guard (the core of the fix).
# ---------------------------------------------------------------------------

def test_account_scan_fallback_exists_outside_bridge_guard():
    """The _reconcile_position source must contain a second account-scan
    block AFTER the futures check and BEFORE the 'seems OK' line — proving
    the scan runs even when bridge_balance <= 1.0.

    This is the structural guarantee that issue #111 is fixed.
    """
    import inspect

    from binance_trade_bot.crypto_trading import _reconcile_position

    src = inspect.getsource(_reconcile_position)

    # The "seems OK" fallthrough must still exist (it's the last resort).
    assert "seems OK" in src, "the 'seems OK' final log line must still exist"

    # The new fallback scan must be present and reference issue #111.
    assert "issue #111" in src or "#111" in src, (
        "the fallback scan block should reference issue #111 for traceability"
    )

    # The fallback must call get_account() a SECOND time (first is inside the
    # bridge_balance > 1.0 guard). Count occurrences of get_account.
    account_calls = src.count("get_account()")
    assert account_calls >= 2, (
        f"Expected >= 2 get_account() calls in _reconcile_position "
        f"(one inside bridge guard, one in the new fallback), found {account_calls}"
    )

    # The fallback must be positioned AFTER the futures check.
    futures_idx = src.index("futures_account_balance")
    fallback_idx = src.index("Could not scan account for reconciliation fallback")
    assert fallback_idx > futures_idx, (
        "the account-scan fallback must come AFTER the futures check"
    )

    # The fallback must be positioned BEFORE the final "seems OK" LOG line.
    # Use rindex to skip the word appearing in the comment of the fallback
    # block itself and find the actual logger.info(...) call.
    seems_ok_idx = src.rindex("seems OK")
    assert fallback_idx < seems_ok_idx, (
        "the account-scan fallback must come BEFORE the 'seems OK' fallthrough"
    )


# ---------------------------------------------------------------------------
# Behavioural test — reproduces the exact scenario from issue #111.
# ---------------------------------------------------------------------------

def test_reconciliation_self_heals_when_db_wrong_and_balances_small():
    """Issue #111 reproduction:

    DB says TIA, actual holding is INJ (12.26), bridge is $0.00765,
    no futures balance.

    Before the fix, the function falls through to "seems OK" without
    correcting the DB.  After the fix, it scans the account, finds INJ,
    and calls ``db.set_current_coin("INJ")``.
    """
    from binance_trade_bot.crypto_trading import _reconcile_position

    # --- Arrange ---
    tia_coin = FakeCoin("TIA")
    inj_coin = FakeCoin("INJ")

    manager = MagicMock()
    db = MagicMock()
    logger = MagicMock()
    config = FakeConfig(bridge_symbol="USDC")

    # DB says current_coin is TIA
    db.get_current_coin.return_value = tia_coin

    # TIA balance: 0.01 (dust, below min_notional)
    # Bridge (USDC) balance: 0.00765 (below 1.0 threshold)
    def _get_currency_balance(symbol, force=False):
        if symbol == "TIA":
            return 0.01
        if symbol == "USDC":
            return 0.00765
        return 0.0

    manager.get_currency_balance.side_effect = _get_currency_balance

    # Ticker price for TIAUSDC
    manager.get_ticker_price.return_value = 5.0
    # min_notional for TIA/USDC — TIA balance 0.01 * $5 = $0.05, way below
    manager.get_min_notional.return_value = 5.0

    # No futures balance
    manager.binance_client = MagicMock()
    manager.binance_client.futures_account_balance.return_value = []

    # Account scan: the real holding is INJ (12.26 tokens)
    # Also has dust TIA (0.01) and bridge USDC (0.00765) and BNB (0.0001)
    manager.get_account.return_value = _make_account_balance([
        ("TIA", 0.01),
        ("INJ", 12.26),
        ("USDC", 0.00765),
        ("BNB", 0.0001),
    ])

    # db.get_coin returns enabled coins
    db.get_coin.side_effect = lambda c: FakeCoin(c, enabled=True)

    # --- Act ---
    _reconcile_position(manager, db, logger, config)

    # --- Assert: DB was corrected to INJ ---
    db.set_current_coin.assert_called_once_with("INJ")
    # Logger should mention the correction
    correction_logs = [str(c) for c in logger.info.call_args_list]
    assert any("INJ" in msg for msg in correction_logs), (
        f"Expected a log message mentioning INJ (the actual holding), "
        f"got: {correction_logs}"
    )


# ---------------------------------------------------------------------------
# Negative test — when the DB coin IS the actual holding (just small),
# reconciliation should NOT change anything.
# ---------------------------------------------------------------------------

def test_reconciliation_does_not_change_when_db_coin_is_actual_holding():
    """If the DB says INJ and we actually hold INJ (even if below
    min_notional with small bridge), the fallback scan should still find
    INJ and 'set' current_coin to INJ — which is a no-op in practice
    but not harmful.

    More importantly, it should NOT set it to some OTHER coin."""
    from binance_trade_bot.crypto_trading import _reconcile_position

    inj_coin = FakeCoin("INJ")

    manager = MagicMock()
    db = MagicMock()
    logger = MagicMock()
    config = FakeConfig(bridge_symbol="USDC")

    db.get_current_coin.return_value = inj_coin

    def _get_currency_balance(symbol, force=False):
        if symbol == "INJ":
            return 0.5  # small but real
        if symbol == "USDC":
            return 0.01
        return 0.0

    manager.get_currency_balance.side_effect = _get_currency_balance
    manager.get_ticker_price.return_value = 20.0  # INJ ~$20
    manager.get_min_notional.return_value = 5.0   # min notional $5
    # 0.5 INJ * $20 = $10 > $5 min_notional → actually should return early OK

    _reconcile_position(manager, db, logger, config)

    # Should NOT have called set_current_coin because balance is sufficient
    db.set_current_coin.assert_not_called()


# ---------------------------------------------------------------------------
# Edge case — no non-bridge holding at all (legitimate "seems OK")
# ---------------------------------------------------------------------------

def test_reconciliation_seems_ok_when_truly_nothing_held():
    """When there's genuinely nothing held (dust only, no meaningful
    non-bridge asset), the function should fall through to 'seems OK'
    without calling set_current_coin."""
    from binance_trade_bot.crypto_trading import _reconcile_position

    tia_coin = FakeCoin("TIA")

    manager = MagicMock()
    db = MagicMock()
    logger = MagicMock()
    config = FakeConfig(bridge_symbol="USDC")

    db.get_current_coin.return_value = tia_coin

    def _get_currency_balance(symbol, force=False):
        if symbol == "TIA":
            return 0.0001  # dust
        if symbol == "USDC":
            return 0.001   # dust
        return 0.0

    manager.get_currency_balance.side_effect = _get_currency_balance
    manager.get_ticker_price.return_value = 5.0
    manager.get_min_notional.return_value = 5.0
    manager.binance_client = MagicMock()
    manager.binance_client.futures_account_balance.return_value = []

    # Account has only dust / bridge / BNB — no real holding
    manager.get_account.return_value = _make_account_balance([
        ("TIA", 0.0001),
        ("USDC", 0.001),
        ("BNB", 0.0001),
    ])

    db.get_coin.side_effect = lambda c: FakeCoin(c, enabled=True)

    _reconcile_position(manager, db, logger, config)

    # Should NOT call set_current_coin — nothing meaningful to set
    db.set_current_coin.assert_not_called()

"""Tests for canary capital safety rails."""

from types import SimpleNamespace


def config(**overrides):
    defaults = {
        "CANARY_MODE_ENABLED": True,
        "CANARY_MAX_SPOT_TRADE_USDC": 75.0,
        "CANARY_FUTURES_MAX_MARGIN_PCT": 0.15,
        "CANARY_MAX_FUTURES_MARGIN_USDC": 50.0,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_spot_trade_cap_limits_bridge_deployment_when_canary_enabled():
    from binance_trade_bot.canary_capital_guard import cap_spot_trade_balance

    capped = cap_spot_trade_balance(250.0, config())

    assert capped.allowed_balance == 75.0
    assert capped.capped is True
    assert "CANARY" in capped.reason


def test_spot_trade_cap_noop_when_disabled_or_unset():
    from binance_trade_bot.canary_capital_guard import cap_spot_trade_balance

    assert cap_spot_trade_balance(250.0, config(CANARY_MODE_ENABLED=False)).allowed_balance == 250.0
    assert cap_spot_trade_balance(250.0, config(CANARY_MAX_SPOT_TRADE_USDC=0)).allowed_balance == 250.0


def test_futures_margin_cap_combines_pct_and_absolute_cap():
    from binance_trade_bot.canary_capital_guard import cap_futures_margin

    capped = cap_futures_margin(usdc_balance=1000.0, configured_margin_pct=0.50, config=config())

    assert capped.allowed_margin == 50.0
    assert capped.effective_margin_pct == 0.05
    assert capped.capped is True


def test_futures_margin_cap_respects_lower_configured_pct():
    from binance_trade_bot.canary_capital_guard import cap_futures_margin

    capped = cap_futures_margin(usdc_balance=1000.0, configured_margin_pct=0.10, config=config(CANARY_MAX_FUTURES_MARGIN_USDC=0))

    assert capped.allowed_margin == 100.0
    assert capped.effective_margin_pct == 0.10
    assert capped.capped is False


def test_canary_status_summary_is_human_readable():
    from binance_trade_bot.canary_capital_guard import canary_status_summary

    summary = canary_status_summary(config())

    assert "🟡 CANARY MODE" in summary
    assert "spot cap $75.00" in summary
    assert "futures margin cap 15.0%" in summary
    assert "futures absolute cap $50.00" in summary

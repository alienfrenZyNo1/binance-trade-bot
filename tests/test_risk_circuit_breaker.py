"""Tests for portfolio drawdown circuit breaker safety rails."""

from types import SimpleNamespace


def cfg(**overrides):
    base = {
        "PORTFOLIO_CIRCUIT_BREAKER_ENABLED": True,
        "PORTFOLIO_DAILY_MAX_DRAWDOWN_PCT": 5.0,
        "PORTFOLIO_WEEKLY_MAX_DRAWDOWN_PCT": 12.0,
        "PORTFOLIO_CIRCUIT_BREAKER_COOLDOWN_HOURS": 24,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_circuit_breaker_noop_when_disabled():
    from binance_trade_bot.risk_circuit_breaker import evaluate_circuit_breaker

    result = evaluate_circuit_breaker(
        current_equity=90.0,
        daily_start_equity=100.0,
        weekly_start_equity=100.0,
        config=cfg(PORTFOLIO_CIRCUIT_BREAKER_ENABLED=False),
    )

    assert result.block_new_risk is False
    assert result.triggered is False
    assert result.reason == "circuit breaker disabled"


def test_daily_drawdown_blocks_new_risk():
    from binance_trade_bot.risk_circuit_breaker import evaluate_circuit_breaker

    result = evaluate_circuit_breaker(
        current_equity=94.9,
        daily_start_equity=100.0,
        weekly_start_equity=100.0,
        config=cfg(),
    )

    assert result.block_new_risk is True
    assert result.triggered is True
    assert result.scope == "daily"
    assert "daily drawdown" in result.reason


def test_weekly_drawdown_blocks_new_risk_when_daily_is_ok():
    from binance_trade_bot.risk_circuit_breaker import evaluate_circuit_breaker

    result = evaluate_circuit_breaker(
        current_equity=87.0,
        daily_start_equity=88.0,
        weekly_start_equity=100.0,
        config=cfg(),
    )

    assert result.block_new_risk is True
    assert result.scope == "weekly"
    assert "weekly drawdown" in result.reason


def test_zero_or_missing_baseline_fails_open_not_closed():
    from binance_trade_bot.risk_circuit_breaker import evaluate_circuit_breaker

    result = evaluate_circuit_breaker(
        current_equity=50.0,
        daily_start_equity=0.0,
        weekly_start_equity=None,
        config=cfg(),
    )

    assert result.block_new_risk is False
    assert result.triggered is False
    assert "baseline unavailable" in result.reason


def test_cooldown_keeps_blocking_after_trigger():
    from binance_trade_bot.risk_circuit_breaker import is_circuit_breaker_cooling_down

    assert is_circuit_breaker_cooling_down(
        last_triggered_at=1_000.0,
        now=1_000.0 + 23 * 3600,
        config=cfg(PORTFOLIO_CIRCUIT_BREAKER_COOLDOWN_HOURS=24),
    ) is True
    assert is_circuit_breaker_cooling_down(
        last_triggered_at=1_000.0,
        now=1_000.0 + 25 * 3600,
        config=cfg(PORTFOLIO_CIRCUIT_BREAKER_COOLDOWN_HOURS=24),
    ) is False


def test_status_summary_is_scannable():
    from binance_trade_bot.risk_circuit_breaker import circuit_breaker_status_summary, evaluate_circuit_breaker

    ok = evaluate_circuit_breaker(100.0, 100.0, 100.0, cfg())
    blocked = evaluate_circuit_breaker(94.0, 100.0, 100.0, cfg())

    assert circuit_breaker_status_summary(ok).startswith("🟢")
    assert circuit_breaker_status_summary(blocked).startswith("🔴")

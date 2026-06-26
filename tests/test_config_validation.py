"""Tests for non-fatal runtime config validation."""

from types import SimpleNamespace

from binance_trade_bot.config_validation import (
    ConfigValidationIssue,
    log_runtime_config_validation,
    validate_runtime_config,
)


class Logger:
    def __init__(self):
        self.infos = []
        self.warnings = []
        self.errors = []

    def info(self, message, notification=True):
        self.infos.append((message, notification))

    def warning(self, message, notification=True):
        self.warnings.append((message, notification))

    def error(self, message, notification=True):
        self.errors.append((message, notification))


def cfg(**overrides):
    base = {
        "BRIDGE": SimpleNamespace(symbol="USDC"),
        "FUTURES_LEVERAGE": 1,
        "FUTURES_MAX_MARGIN_PCT": 0.5,
        "FUTURES_MARGIN_TYPE": "CROSS",
        "FUTURES_STOP_LOSS_PCT": 15.0,
        "FUTURES_TRAILING_STOP_PCT": 10.0,
        "FUTURES_SERVER_TRAILING_CALLBACK_RATE": 1.0,
        "FUTURES_SERVER_TRAILING_MIN_PROFIT_BUFFER_PCT": 0.5,
        "REGIME_CONFIRMATION_CYCLES": 3,
        "CONFIRMATION_CYCLES": 3,
        "SCOUT_SLEEP_TIME": 5,
        "TRADE_COOLDOWN_SECONDS": 7200,
        "SOCKETIO_UPDATES_ENABLED": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def issue_messages(issues):
    return [issue.message for issue in issues]


def test_valid_runtime_config_has_no_issues():
    assert validate_runtime_config(cfg()) == ()


def test_config_validation_flags_usdt_bridge_as_error():
    issues = validate_runtime_config(cfg(BRIDGE=SimpleNamespace(symbol="USDT")))

    assert issues == (
        ConfigValidationIssue(
            key="BRIDGE.symbol",
            severity="error",
            message="Binance EU setup must use USDC bridge currency, not USDT",
        ),
    )


def test_config_validation_flags_futures_risk_ranges():
    issues = validate_runtime_config(
        cfg(
            FUTURES_LEVERAGE=0,
            FUTURES_MAX_MARGIN_PCT=1.5,
            FUTURES_STOP_LOSS_PCT=0,
            FUTURES_SERVER_TRAILING_CALLBACK_RATE=10.0,
            FUTURES_SERVER_TRAILING_MIN_PROFIT_BUFFER_PCT=-0.1,
        )
    )

    messages = issue_messages(issues)
    assert "FUTURES_LEVERAGE must be at least 1" in messages
    assert "FUTURES_MAX_MARGIN_PCT should be between 0 and 1" in messages
    assert "FUTURES_STOP_LOSS_PCT should be greater than 0 and no more than 50" in messages
    assert "FUTURES_SERVER_TRAILING_CALLBACK_RATE should be between 0.1 and 5.0" in messages
    assert "FUTURES_SERVER_TRAILING_MIN_PROFIT_BUFFER_PCT should be zero or positive" in messages


def test_config_validation_flags_confirmation_and_timing_ranges():
    issues = validate_runtime_config(
        cfg(
            REGIME_CONFIRMATION_CYCLES=0,
            CONFIRMATION_CYCLES=0,
            SCOUT_SLEEP_TIME=0,
            TRADE_COOLDOWN_SECONDS=-1,
        )
    )

    messages = issue_messages(issues)
    assert "REGIME_CONFIRMATION_CYCLES must be at least 1" in messages
    assert "CONFIRMATION_CYCLES must be at least 1" in messages
    assert "SCOUT_SLEEP_TIME must be at least 1 second" in messages
    assert "TRADE_COOLDOWN_SECONDS should not be negative" in messages



def test_config_validation_flags_circuit_breaker_ranges():
    issues = validate_runtime_config(
        cfg(
            PORTFOLIO_CIRCUIT_BREAKER_ENABLED=True,
            PORTFOLIO_DAILY_MAX_DRAWDOWN_PCT=0,
            PORTFOLIO_WEEKLY_MAX_DRAWDOWN_PCT=-1,
            PORTFOLIO_CIRCUIT_BREAKER_COOLDOWN_HOURS=-2,
        )
    )

    messages = issue_messages(issues)
    assert "PORTFOLIO_DAILY_MAX_DRAWDOWN_PCT should be greater than 0 when circuit breaker is enabled" in messages
    assert "PORTFOLIO_WEEKLY_MAX_DRAWDOWN_PCT should be greater than 0 when circuit breaker is enabled" in messages
    assert "PORTFOLIO_CIRCUIT_BREAKER_COOLDOWN_HOURS should not be negative" in messages

def test_socketio_enabled_is_only_a_warning():
    issues = validate_runtime_config(cfg(SOCKETIO_UPDATES_ENABLED=True))

    assert issues == (
        ConfigValidationIssue(
            key="SOCKETIO_UPDATES_ENABLED",
            severity="warning",
            message="Legacy Socket.IO updates are enabled; expect python-socketio/eventlet runtime imports",
        ),
    )


def test_log_runtime_config_validation_is_non_fatal_and_no_notifications():
    logger = Logger()
    issues = log_runtime_config_validation(
        cfg(BRIDGE=SimpleNamespace(symbol="USDT"), FUTURES_MAX_MARGIN_PCT=2.0),
        logger,
    )

    assert len(issues) == 2
    assert logger.errors == []
    assert len(logger.warnings) == 2
    assert all(notification is False for _, notification in logger.warnings)
    assert any("Config validation error" in message for message, _ in logger.warnings)


def test_log_runtime_config_validation_logs_ok_message_without_notification():
    logger = Logger()

    issues = log_runtime_config_validation(cfg(), logger)

    assert issues == ()
    assert logger.warnings == []
    assert logger.infos == [("Config validation OK", False)]

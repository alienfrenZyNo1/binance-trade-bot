"""Non-fatal runtime configuration validation.

The live bot should surface dangerous or surprising config drift early without
turning a refactor into a surprise production abort. This module performs pure
validation and a small logging wrapper; it must not read files, environment
variables, secrets, Binance, databases, or Telegram.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class ConfigValidationIssue:
    """One non-secret runtime configuration validation finding."""

    key: str
    severity: str  # "warning" or "error"; logged non-fatally for now.
    message: str


def _symbol(config: Any) -> str:
    bridge = getattr(config, "BRIDGE", None)
    value = getattr(bridge, "symbol", None) or getattr(config, "BRIDGE_SYMBOL", "")
    return str(value or "").upper()


def _float(config: Any, key: str, default: float = 0.0) -> float:
    try:
        return float(getattr(config, key, default))
    except Exception:
        return default


def _int(config: Any, key: str, default: int = 0) -> int:
    try:
        return int(float(getattr(config, key, default)))
    except Exception:
        return default


def _bool(config: Any, key: str, default: bool = False) -> bool:
    value = getattr(config, key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def validate_runtime_config(config: Any) -> Sequence[ConfigValidationIssue]:
    """Return non-secret config validation issues without side effects."""

    issues: list[ConfigValidationIssue] = []

    if _symbol(config) == "USDT":
        issues.append(
            ConfigValidationIssue(
                key="BRIDGE.symbol",
                severity="error",
                message="Binance EU setup must use USDC bridge currency, not USDT",
            )
        )

    leverage = _int(config, "FUTURES_LEVERAGE", 1)
    if leverage < 1:
        issues.append(
            ConfigValidationIssue(
                key="FUTURES_LEVERAGE",
                severity="error",
                message="FUTURES_LEVERAGE must be at least 1",
            )
        )
    elif leverage > 5:
        issues.append(
            ConfigValidationIssue(
                key="FUTURES_LEVERAGE",
                severity="warning",
                message="FUTURES_LEVERAGE is unusually high for this bot; verify account-level stop risk",
            )
        )

    margin_pct = _float(config, "FUTURES_MAX_MARGIN_PCT", 0.5)
    if margin_pct <= 0 or margin_pct > 1:
        issues.append(
            ConfigValidationIssue(
                key="FUTURES_MAX_MARGIN_PCT",
                severity="error",
                message="FUTURES_MAX_MARGIN_PCT should be between 0 and 1",
            )
        )

    margin_type = str(getattr(config, "FUTURES_MARGIN_TYPE", "CROSS") or "").upper()
    if margin_type not in {"CROSS", "ISOLATED"}:
        issues.append(
            ConfigValidationIssue(
                key="FUTURES_MARGIN_TYPE",
                severity="error",
                message="FUTURES_MARGIN_TYPE must be CROSS or ISOLATED",
            )
        )
    elif margin_type == "ISOLATED":
        issues.append(
            ConfigValidationIssue(
                key="FUTURES_MARGIN_TYPE",
                severity="warning",
                message="ISOLATED futures margin is known to be rejected by this Binance account; CROSS is the safe default",
            )
        )

    stop_pct = _float(config, "FUTURES_STOP_LOSS_PCT", 15.0)
    if stop_pct <= 0 or stop_pct > 50:
        issues.append(
            ConfigValidationIssue(
                key="FUTURES_STOP_LOSS_PCT",
                severity="error",
                message="FUTURES_STOP_LOSS_PCT should be greater than 0 and no more than 50",
            )
        )

    trailing_stop_pct = _float(config, "FUTURES_TRAILING_STOP_PCT", 10.0)
    if trailing_stop_pct < 0 or trailing_stop_pct > 50:
        issues.append(
            ConfigValidationIssue(
                key="FUTURES_TRAILING_STOP_PCT",
                severity="warning",
                message="FUTURES_TRAILING_STOP_PCT is outside the expected 0 to 50 range",
            )
        )

    callback_rate = _float(config, "FUTURES_SERVER_TRAILING_CALLBACK_RATE", 1.0)
    if callback_rate < 0.1 or callback_rate > 5.0:
        issues.append(
            ConfigValidationIssue(
                key="FUTURES_SERVER_TRAILING_CALLBACK_RATE",
                severity="error",
                message="FUTURES_SERVER_TRAILING_CALLBACK_RATE should be between 0.1 and 5.0",
            )
        )

    profit_buffer = _float(config, "FUTURES_SERVER_TRAILING_MIN_PROFIT_BUFFER_PCT", 0.5)
    if profit_buffer < 0:
        issues.append(
            ConfigValidationIssue(
                key="FUTURES_SERVER_TRAILING_MIN_PROFIT_BUFFER_PCT",
                severity="error",
                message="FUTURES_SERVER_TRAILING_MIN_PROFIT_BUFFER_PCT should be zero or positive",
            )
        )

    regime_confirmations = _int(config, "REGIME_CONFIRMATION_CYCLES", 3)
    if regime_confirmations < 1:
        issues.append(
            ConfigValidationIssue(
                key="REGIME_CONFIRMATION_CYCLES",
                severity="error",
                message="REGIME_CONFIRMATION_CYCLES must be at least 1",
            )
        )

    rotation_confirmations = _int(config, "CONFIRMATION_CYCLES", 3)
    if rotation_confirmations < 1:
        issues.append(
            ConfigValidationIssue(
                key="CONFIRMATION_CYCLES",
                severity="error",
                message="CONFIRMATION_CYCLES must be at least 1",
            )
        )

    scout_sleep = _int(config, "SCOUT_SLEEP_TIME", 5)
    if scout_sleep < 1:
        issues.append(
            ConfigValidationIssue(
                key="SCOUT_SLEEP_TIME",
                severity="error",
                message="SCOUT_SLEEP_TIME must be at least 1 second",
            )
        )

    cooldown = _float(config, "TRADE_COOLDOWN_SECONDS", 7200)
    if cooldown < 0:
        issues.append(
            ConfigValidationIssue(
                key="TRADE_COOLDOWN_SECONDS",
                severity="error",
                message="TRADE_COOLDOWN_SECONDS should not be negative",
            )
        )

    if _bool(config, "PORTFOLIO_CIRCUIT_BREAKER_ENABLED", False):
        daily_dd = _float(config, "PORTFOLIO_DAILY_MAX_DRAWDOWN_PCT", 5.0)
        weekly_dd = _float(config, "PORTFOLIO_WEEKLY_MAX_DRAWDOWN_PCT", 12.0)
        cb_cooldown = _float(config, "PORTFOLIO_CIRCUIT_BREAKER_COOLDOWN_HOURS", 24.0)
        if daily_dd <= 0:
            issues.append(
                ConfigValidationIssue(
                    key="PORTFOLIO_DAILY_MAX_DRAWDOWN_PCT",
                    severity="error",
                    message="PORTFOLIO_DAILY_MAX_DRAWDOWN_PCT should be greater than 0 when circuit breaker is enabled",
                )
            )
        if weekly_dd <= 0:
            issues.append(
                ConfigValidationIssue(
                    key="PORTFOLIO_WEEKLY_MAX_DRAWDOWN_PCT",
                    severity="error",
                    message="PORTFOLIO_WEEKLY_MAX_DRAWDOWN_PCT should be greater than 0 when circuit breaker is enabled",
                )
            )
        if cb_cooldown < 0:
            issues.append(
                ConfigValidationIssue(
                    key="PORTFOLIO_CIRCUIT_BREAKER_COOLDOWN_HOURS",
                    severity="error",
                    message="PORTFOLIO_CIRCUIT_BREAKER_COOLDOWN_HOURS should not be negative",
                )
            )

    if _bool(config, "SOCKETIO_UPDATES_ENABLED", False):
        issues.append(
            ConfigValidationIssue(
                key="SOCKETIO_UPDATES_ENABLED",
                severity="warning",
                message="Legacy Socket.IO updates are enabled; expect python-socketio/eventlet runtime imports",
            )
        )

    return tuple(issues)


def log_runtime_config_validation(config: Any, logger: Any) -> Sequence[ConfigValidationIssue]:
    """Log validation issues non-fatally, suppressing Telegram notifications."""

    issues = validate_runtime_config(config)
    if not issues:
        logger.info("Config validation OK", notification=False)
        return issues

    for issue in issues:
        logger.warning(
            f"Config validation {issue.severity}: {issue.key} — {issue.message}",
            notification=False,
        )
    return issues

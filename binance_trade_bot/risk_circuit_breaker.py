"""Portfolio-level circuit breaker helpers.

The functions in this module are deliberately pure: they do not call Binance,
write state, or close positions. Strategy/futures code can use the returned
verdict to block *new* risk while keeping exits and server-side protection alive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CircuitBreakerResult:
    block_new_risk: bool
    triggered: bool
    scope: str
    drawdown_pct: float
    threshold_pct: float
    reason: str


def _enabled(config: Any) -> bool:
    return bool(getattr(config, "PORTFOLIO_CIRCUIT_BREAKER_ENABLED", False))


def _float_attr(config: Any, attr: str, default: float) -> float:
    try:
        return float(getattr(config, attr, default) or default)
    except (TypeError, ValueError):
        return default


def _drawdown_pct(current_equity: float, start_equity: float | None) -> float | None:
    try:
        current = float(current_equity)
        start = float(start_equity) if start_equity is not None else 0.0
    except (TypeError, ValueError):
        return None
    if start <= 0:
        return None
    return max(0.0, (start - current) / start * 100.0)


def evaluate_circuit_breaker(
    current_equity: float,
    daily_start_equity: float | None,
    weekly_start_equity: float | None,
    config: Any,
) -> CircuitBreakerResult:
    """Return whether new entries should be blocked by drawdown limits."""
    if not _enabled(config):
        return CircuitBreakerResult(False, False, "none", 0.0, 0.0, "circuit breaker disabled")

    daily_limit = max(0.0, _float_attr(config, "PORTFOLIO_DAILY_MAX_DRAWDOWN_PCT", 0.0))
    weekly_limit = max(0.0, _float_attr(config, "PORTFOLIO_WEEKLY_MAX_DRAWDOWN_PCT", 0.0))
    daily_dd = _drawdown_pct(current_equity, daily_start_equity)
    weekly_dd = _drawdown_pct(current_equity, weekly_start_equity)

    if daily_dd is None and weekly_dd is None:
        return CircuitBreakerResult(False, False, "none", 0.0, 0.0, "equity baseline unavailable")

    if daily_limit > 0 and daily_dd is not None and daily_dd >= daily_limit:
        return CircuitBreakerResult(
            True,
            True,
            "daily",
            daily_dd,
            daily_limit,
            f"daily drawdown {daily_dd:.2f}% >= {daily_limit:.2f}%",
        )

    if weekly_limit > 0 and weekly_dd is not None and weekly_dd >= weekly_limit:
        return CircuitBreakerResult(
            True,
            True,
            "weekly",
            weekly_dd,
            weekly_limit,
            f"weekly drawdown {weekly_dd:.2f}% >= {weekly_limit:.2f}%",
        )

    worst_scope = "daily"
    worst_dd = daily_dd or 0.0
    worst_limit = daily_limit
    if (weekly_dd or 0.0) > worst_dd:
        worst_scope = "weekly"
        worst_dd = weekly_dd or 0.0
        worst_limit = weekly_limit
    return CircuitBreakerResult(
        False,
        False,
        worst_scope,
        worst_dd,
        worst_limit,
        f"drawdown within limits ({worst_scope}: {worst_dd:.2f}% / {worst_limit:.2f}%)",
    )


def is_circuit_breaker_cooling_down(last_triggered_at: float | None, now: float, config: Any) -> bool:
    """Return True while the post-trigger cooldown is still active."""
    if not _enabled(config) or not last_triggered_at:
        return False
    cooldown_hours = max(0.0, _float_attr(config, "PORTFOLIO_CIRCUIT_BREAKER_COOLDOWN_HOURS", 24.0))
    if cooldown_hours <= 0:
        return False
    return (float(now) - float(last_triggered_at)) < cooldown_hours * 3600.0


def circuit_breaker_status_summary(result: CircuitBreakerResult) -> str:
    """Compact human-readable status for logs/Telegram."""
    if result.block_new_risk:
        return f"🔴 Circuit breaker active: {result.reason}"
    if result.triggered:
        return f"🟡 Circuit breaker triggered: {result.reason}"
    return f"🟢 Circuit breaker OK: {result.reason}"

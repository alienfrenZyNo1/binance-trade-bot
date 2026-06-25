"""Pure canary capital safety rails.

These helpers cap deployment size while allowing the existing strategy logic to
run unchanged. They are deliberately pure so sizing is testable without Binance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SpotTradeCap:
    allowed_balance: float
    original_balance: float
    capped: bool
    reason: str


@dataclass(frozen=True)
class FuturesMarginCap:
    allowed_margin: float
    original_margin: float
    effective_margin_pct: float
    capped: bool
    reason: str


def _canary_enabled(config: Any) -> bool:
    return bool(getattr(config, "CANARY_MODE_ENABLED", False))


def _positive_float(config: Any, attr: str, default: float = 0.0) -> float:
    try:
        value = float(getattr(config, attr, default) or 0.0)
    except (TypeError, ValueError):
        return default
    return max(0.0, value)


def cap_spot_trade_balance(bridge_balance: float, config: Any) -> SpotTradeCap:
    """Return bridge balance allowed for a spot buy under canary mode."""
    bridge_balance = max(0.0, float(bridge_balance or 0.0))
    if not _canary_enabled(config):
        return SpotTradeCap(bridge_balance, bridge_balance, False, "canary disabled")

    max_trade = _positive_float(config, "CANARY_MAX_SPOT_TRADE_USDC", 0.0)
    if max_trade <= 0:
        return SpotTradeCap(bridge_balance, bridge_balance, False, "CANARY spot cap unset")

    allowed = min(bridge_balance, max_trade)
    return SpotTradeCap(
        allowed_balance=allowed,
        original_balance=bridge_balance,
        capped=allowed < bridge_balance,
        reason=f"CANARY spot cap ${max_trade:.2f}",
    )


def cap_futures_margin(usdc_balance: float, configured_margin_pct: float, config: Any) -> FuturesMarginCap:
    """Return futures margin allowed after canary pct/absolute caps."""
    usdc_balance = max(0.0, float(usdc_balance or 0.0))
    configured_margin_pct = max(0.0, float(configured_margin_pct or 0.0))
    original_margin = usdc_balance * configured_margin_pct
    if not _canary_enabled(config):
        return FuturesMarginCap(original_margin, original_margin, configured_margin_pct, False, "canary disabled")

    canary_pct = _positive_float(config, "CANARY_FUTURES_MAX_MARGIN_PCT", 0.0)
    effective_pct = min(configured_margin_pct, canary_pct) if canary_pct > 0 else configured_margin_pct
    allowed = usdc_balance * effective_pct

    absolute_cap = _positive_float(config, "CANARY_MAX_FUTURES_MARGIN_USDC", 0.0)
    if absolute_cap > 0:
        allowed = min(allowed, absolute_cap)

    effective_pct = allowed / usdc_balance if usdc_balance > 0 else 0.0
    return FuturesMarginCap(
        allowed_margin=allowed,
        original_margin=original_margin,
        effective_margin_pct=effective_pct,
        capped=allowed < original_margin,
        reason=(
            f"CANARY futures margin cap {canary_pct*100:.1f}%"
            + (f" / ${absolute_cap:.2f}" if absolute_cap > 0 else "")
        ),
    )


def canary_status_summary(config: Any) -> str:
    """Compact human-readable canary status for logs/Telegram."""
    if not _canary_enabled(config):
        return "⚪ CANARY MODE disabled"
    spot = _positive_float(config, "CANARY_MAX_SPOT_TRADE_USDC", 0.0)
    fut_pct = _positive_float(config, "CANARY_FUTURES_MAX_MARGIN_PCT", 0.0)
    fut_abs = _positive_float(config, "CANARY_MAX_FUTURES_MARGIN_USDC", 0.0)
    return (
        "🟡 CANARY MODE enabled | "
        f"spot cap ${spot:.2f} | "
        f"futures margin cap {fut_pct*100:.1f}% | "
        f"futures absolute cap ${fut_abs:.2f}"
    )

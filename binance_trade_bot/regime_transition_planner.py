"""Pure planning helpers for live market-regime transitions.

The live strategy should keep Binance/API side effects in MomentumStrategy, but
use this module to decide which side effects are needed for a confirmed regime
change. Keeping the decision policy pure makes BEAR entry/exit safety easier to
regression-test without touching Binance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


BEAR = "bear"


@dataclass(frozen=True)
class RegimeTransitionPlan:
    """Side-effect intent for a confirmed regime transition."""

    current_regime: str
    target_regime: str
    requires_spot_exit: bool = False
    requires_futures_transfer: bool = False
    requires_short_open: bool = False
    requires_short_close: bool = False
    requires_futures_exit_check: bool = False
    requires_spot_reentry: bool = False
    set_awaiting_reentry: bool = False
    clear_awaiting_reentry: bool = False

    @property
    def has_actions(self) -> bool:
        return any(
            (
                self.requires_spot_exit,
                self.requires_futures_transfer,
                self.requires_short_open,
                self.requires_short_close,
                self.requires_futures_exit_check,
                self.requires_spot_reentry,
                self.set_awaiting_reentry,
                self.clear_awaiting_reentry,
            )
        )


def _normalize_regime(regime: str) -> str:
    return str(regime or "").strip().lower()


def plan_regime_transition(
    *,
    current_regime: str,
    target_regime: str,
    holding_coin: Optional[str],
    has_spot_position: bool,
    has_futures_position: bool,
    awaiting_reentry: bool,
) -> RegimeTransitionPlan:
    """Return required actions for a confirmed market-regime transition.

    The parameters describe current observable state only. This function must
    stay pure: no Binance calls, DB writes, logging, timestamps, or config reads.
    """

    current = _normalize_regime(current_regime)
    target = _normalize_regime(target_regime)

    if current == target:
        return RegimeTransitionPlan(current_regime=current, target_regime=target)

    entering_bear = target == BEAR and current != BEAR
    leaving_bear = current == BEAR and target != BEAR
    has_named_spot_position = bool(holding_coin) and bool(has_spot_position)

    return RegimeTransitionPlan(
        current_regime=current,
        target_regime=target,
        requires_spot_exit=entering_bear and has_named_spot_position,
        requires_futures_transfer=entering_bear,
        requires_short_open=entering_bear and not has_futures_position,
        requires_short_close=leaving_bear and has_futures_position,
        requires_futures_exit_check=leaving_bear,
        requires_spot_reentry=leaving_bear,
        set_awaiting_reentry=leaving_bear and not awaiting_reentry,
        clear_awaiting_reentry=entering_bear and awaiting_reentry,
    )

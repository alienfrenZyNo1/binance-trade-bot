"""Pure accounting helpers for balance/deposit detection."""

from typing import NamedTuple


class DepositEvaluation(NamedTuple):
    """Result of comparing the current spot bridge balance to the baseline."""

    deposit_amount: float
    new_baseline: float
    suppression_consumed: bool


def evaluate_deposit_delta(
    last_balance: float,
    current_balance: float,
    suppress_once: bool = False,
    min_threshold: float = 1.0,
) -> DepositEvaluation:
    """Evaluate whether a spot bridge-balance increase is an external deposit.

    Internal futures→spot transfers look exactly like spot balance increases to
    the old detector.  `suppress_once=True` consumes one detector cycle: it
    updates the baseline to the current balance but deliberately returns no
    deposit amount.  This prevents BEAR→spot transitions from polluting P&L as
    phantom deposits.
    """
    last = float(last_balance or 0.0)
    current = float(current_balance or 0.0)

    if suppress_once:
        return DepositEvaluation(0.0, current, True)

    if last <= 0:
        return DepositEvaluation(0.0, current, False)

    increase = current - last
    if increase > float(min_threshold):
        return DepositEvaluation(increase, current, False)

    return DepositEvaluation(0.0, current, False)

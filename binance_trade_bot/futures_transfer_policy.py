"""Pure transfer policy helpers for USDC-M futures wallet movements.

These helpers keep Binance transfer quirks out of `FuturesManager` orchestration
so amount rounding/retry decisions can be tested without a Binance client.
"""

from decimal import Decimal, ROUND_DOWN
from typing import Optional

from binance.exceptions import BinanceAPIException


TRANSFER_DUST_BUFFER = Decimal("0.10")
TRANSFER_MIN_AMOUNT = Decimal("1.00")
TRANSFER_STEP = Decimal("0.01")
INSUFFICIENT_BALANCE_CODE = -5013


def safe_transfer_amount(amount: float) -> float:
    """Return a conservative transferable amount, or 0 if too small.

    Binance can reject exact max-withdrawable futures transfers.  We leave a
    small dust buffer and floor to cents for USDC transfers.
    """
    raw = Decimal(str(amount or 0)) - TRANSFER_DUST_BUFFER
    if raw < TRANSFER_MIN_AMOUNT:
        return 0.0
    safe = raw.quantize(TRANSFER_STEP, rounding=ROUND_DOWN)
    return float(safe)


def choose_retry_transfer_amount(previous_attempt: float, refreshed_withdrawable: float) -> float:
    """Choose a smaller retry amount after an insufficient-balance error."""
    return min(
        safe_transfer_amount(refreshed_withdrawable),
        safe_transfer_amount(previous_attempt),
    )


def is_insufficient_balance_error(error: Optional[Exception]) -> bool:
    """Return True only for Binance futures transfer insufficient-balance errors."""
    return (
        isinstance(error, BinanceAPIException)
        and getattr(error, "code", None) == INSUFFICIENT_BALANCE_CODE
    )

"""Pure transfer policy helpers for USDC-M futures wallet movements.

These helpers keep Binance transfer quirks out of `FuturesManager` orchestration
so amount rounding/retry decisions can be tested without a Binance client.
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from typing import Optional, Tuple

from binance.exceptions import BinanceAPIException


TRANSFER_DUST_BUFFER = Decimal("0.10")
TRANSFER_MIN_AMOUNT = Decimal("1.00")
TRANSFER_STEP = Decimal("0.01")
INSUFFICIENT_BALANCE_CODE = -5013


class TransferStatus(str, Enum):
    """Structured outcome for futures wallet transfer attempts."""

    SUCCESS = "success"
    SKIPPED = "skipped"
    RETRYABLE_FAILURE = "retryable_failure"
    FAILED = "failed"


@dataclass(frozen=True)
class TransferAttemptResult:
    """Typed result for one high-level futures wallet transfer request."""

    status: TransferStatus
    requested_amount: float
    attempted_amounts: Tuple[float, ...] = ()
    transferred_amount: float = 0.0
    error_code: Optional[int] = None
    retryable: bool = False
    error_message: Optional[str] = None

    def __bool__(self) -> bool:
        return self.status == TransferStatus.SUCCESS

    @property
    def attempt_count(self) -> int:
        return len(self.attempted_amounts)


def binance_error_code(error: Optional[Exception]) -> Optional[int]:
    """Return the Binance API error code when available."""
    if isinstance(error, BinanceAPIException):
        return getattr(error, "code", None)
    return None


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
    return binance_error_code(error) == INSUFFICIENT_BALANCE_CODE

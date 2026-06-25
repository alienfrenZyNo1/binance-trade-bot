"""Small persistence repositories used behind the Database facade."""

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from .accounting import evaluate_deposit_delta
from .models import BotState, Deposit


MIN_DEPOSIT_THRESHOLD = 1.0


class BotStateRepository:
    """Repository for persistent bot key/value state."""

    def __init__(self, session_factory: Callable[[], Session]):
        self.session_factory = session_factory

    @contextmanager
    def _session(self):
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get(self, key: str, default: Optional[Any] = None):
        with self._session() as session:
            entry = session.query(BotState).filter(BotState.key == key).first()
            if entry:
                return entry.value
            return default

    def set(self, key: str, value: Any) -> None:
        with self._session() as session:
            entry = session.query(BotState).filter(BotState.key == key).first()
            if entry:
                entry.value = str(value)
                entry.updated_at = datetime.utcnow()
            else:
                session.add(BotState(key, str(value)))


class DepositRepository:
    """Repository for deposit detection state and deposit records."""

    def __init__(self, session_factory: Callable[[], Session], state: BotStateRepository, logger):
        self.session_factory = session_factory
        self.state = state
        self.logger = logger

    @contextmanager
    def _session(self):
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def suppress_next_detection(self, reason: str = "internal transfer") -> None:
        self.state.set("suppress_next_usdc_deposit_detection", "True")
        self.state.set("suppress_next_usdc_deposit_reason", reason)

    def detect_and_record(self, current_usdc_balance: float) -> float:
        last_bal_str = self.state.get("last_usdc_balance", "0")
        try:
            last_usdc_balance = float(last_bal_str)
        except (ValueError, TypeError):
            last_usdc_balance = 0.0

        suppress_once = str(
            self.state.get("suppress_next_usdc_deposit_detection", "False")
        ).lower() in ("true", "1", "yes")
        reason = self.state.get("suppress_next_usdc_deposit_reason", "internal transfer")

        evaluation = evaluate_deposit_delta(
            last_balance=last_usdc_balance,
            current_balance=current_usdc_balance,
            suppress_once=suppress_once,
            min_threshold=MIN_DEPOSIT_THRESHOLD,
        )

        self.state.set("last_usdc_balance", str(evaluation.new_baseline))

        if evaluation.suppression_consumed:
            self.state.set("suppress_next_usdc_deposit_detection", "False")
            self.state.set("suppress_next_usdc_deposit_reason", "")
            self.logger.info(
                f"Deposit detector baseline reset after {reason}: "
                f"{evaluation.new_baseline:.2f} USDC"
            )
            return 0.0

        if evaluation.deposit_amount > 0:
            increase = evaluation.deposit_amount
            with self._session() as session:
                session.add(
                    Deposit(
                        amount=increase,
                        currency="USDC",
                        source="auto",
                        note=(
                            "Auto-detected: balance increased from "
                            f"{last_usdc_balance:.2f} to {current_usdc_balance:.2f}"
                        ),
                        datetime=datetime.utcnow(),
                    )
                )
            self.logger.info(
                f"💰 Deposit auto-detected: ${increase:.2f} USDC "
                f"(balance {last_usdc_balance:.2f} → {current_usdc_balance:.2f})"
            )
            return increase

        return 0.0

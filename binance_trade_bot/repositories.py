"""Small persistence repositories used behind the Database facade."""

from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from .accounting import evaluate_deposit_delta
from .models import BotState, Coin, CurrentCoin, Deposit, MarketRegimeLog, Pair, ScoutHistory


MIN_DEPOSIT_THRESHOLD = 1.0


class CoinRepository:
    """Repository for coin, pair, and current-coin persistence."""

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

    def set_coins(self, symbols: list[str]) -> None:
        """Sync configured coins without re-enabling manually disabled coins."""
        with self._session() as session:
            coins = session.query(Coin).all()

            for coin in coins:
                if coin.symbol not in symbols:
                    coin.enabled = False

            for symbol in symbols:
                coin = next((c for c in coins if c.symbol == symbol), None)
                if coin is None:
                    session.add(Coin(symbol))

        with self._session() as session:
            coins = session.query(Coin).filter(Coin.enabled).all()
            for from_coin in coins:
                for to_coin in coins:
                    if from_coin == to_coin:
                        continue
                    pair = (
                        session.query(Pair)
                        .filter(
                            Pair.from_coin_id == from_coin.symbol,
                            Pair.to_coin_id == to_coin.symbol,
                        )
                        .first()
                    )
                    if pair is None:
                        session.add(Pair(from_coin, to_coin))

    def get_coins(self, only_enabled: bool = True) -> list[Coin]:
        with self._session() as session:
            query = session.query(Coin)
            if only_enabled:
                query = query.filter(Coin.enabled)
            coins = query.all()
            session.expunge_all()
            return coins

    def get_coin(self, coin: Coin | str) -> Coin:
        if isinstance(coin, Coin):
            return coin
        with self._session() as session:
            result = session.query(Coin).get(coin)
            session.expunge(result)
            return result

    def set_current_coin(self, coin: Coin | str) -> CurrentCoin:
        coin = self.get_coin(coin)
        with self._session() as session:
            if isinstance(coin, Coin):
                coin = session.merge(coin)
            current_coin = CurrentCoin(coin)
            session.add(current_coin)
            session.flush()
            current_coin.datetime
            current_coin.coin.symbol
            current_coin.coin.enabled
            session.expunge_all()
            return current_coin

    def get_current_coin(self) -> Optional[Coin]:
        with self._session() as session:
            current_coin = session.query(CurrentCoin).order_by(CurrentCoin.datetime.desc()).first()
            if current_coin is None:
                return None
            coin = current_coin.coin
            session.expunge(coin)
            return coin

    def get_pair(self, from_coin: Coin | str, to_coin: Coin | str) -> Pair:
        from_coin = self.get_coin(from_coin)
        to_coin = self.get_coin(to_coin)
        with self._session() as session:
            pair = (
                session.query(Pair)
                .filter(
                    Pair.from_coin_id == from_coin.symbol,
                    Pair.to_coin_id == to_coin.symbol,
                )
                .first()
            )
            if pair is not None:
                # Load relationship attributes before detaching so callers keep
                # the same detached-object behaviour as the Database facade.
                pair.from_coin.symbol
                pair.to_coin.symbol
                session.expunge_all()
            return pair

    def get_pairs_from(self, from_coin: Coin | str, only_enabled: bool = True) -> list[Pair]:
        from_coin = self.get_coin(from_coin)
        with self._session() as session:
            query = session.query(Pair).filter(Pair.from_coin_id == from_coin.symbol)
            if only_enabled:
                query = query.filter(Pair.enabled.is_(True))
            pairs = query.all()
            for pair in pairs:
                pair.from_coin.symbol
                pair.to_coin.symbol
            session.expunge_all()
            return pairs

    def get_pairs(self, only_enabled: bool = True) -> list[Pair]:
        with self._session() as session:
            query = session.query(Pair)
            if only_enabled:
                query = query.filter(Pair.enabled.is_(True))
            pairs = query.all()
            for pair in pairs:
                pair.from_coin.symbol
                pair.to_coin.symbol
            session.expunge_all()
            return pairs


class ScoutHistoryRepository:
    """Repository for scout-ratio history persistence."""

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

    def log_scout(
        self,
        pair: Pair,
        target_ratio: float,
        current_coin_price: float,
        other_coin_price: float,
    ) -> ScoutHistory:
        with self._session() as session:
            pair = session.merge(pair)
            history = ScoutHistory(pair, target_ratio, current_coin_price, other_coin_price)
            session.add(history)
            session.flush()
            history.datetime
            history.pair.from_coin.symbol
            history.pair.from_coin.enabled
            history.pair.to_coin.symbol
            history.pair.to_coin.enabled
            history.current_ratio
            session.expunge_all()
            return history

    def prune_scout_history(self, hours_to_keep: int) -> None:
        time_diff = datetime.now() - timedelta(hours=hours_to_keep)
        with self._session() as session:
            session.query(ScoutHistory).filter(ScoutHistory.datetime < time_diff).delete()


class RegimeRepository:
    """Repository for market-regime history persistence."""

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

    def log(
        self,
        regime: str,
        adx_value: Optional[float] = None,
        avg_volatility: Optional[float] = None,
        btc_correlation: Optional[float] = None,
        ema_short: Optional[float] = None,
        ema_long: Optional[float] = None,
    ) -> None:
        with self._session() as session:
            session.add(
                MarketRegimeLog(
                    regime,
                    adx_value,
                    avg_volatility,
                    btc_correlation,
                    ema_short,
                    ema_long,
                )
            )

    @staticmethod
    def _latest_payload(entry: MarketRegimeLog) -> dict[str, Any]:
        return {
            "regime": entry.regime,
            "adx_value": entry.adx_value,
            "avg_volatility": entry.avg_volatility,
            "btc_correlation": entry.btc_correlation,
            "datetime": entry.datetime.isoformat() if entry.datetime else None,
        }

    @staticmethod
    def _history_payload(entry: MarketRegimeLog) -> dict[str, Any]:
        return {
            "regime": entry.regime,
            "adx": entry.adx_value,
            "vol": entry.avg_volatility,
            "datetime": entry.datetime.isoformat() if entry.datetime else None,
        }

    def get_latest(self) -> Optional[dict[str, Any]]:
        with self._session() as session:
            entry = session.query(MarketRegimeLog).order_by(MarketRegimeLog.datetime.desc()).first()
            if entry is None:
                return None
            return self._latest_payload(entry)

    def get_history(self, hours: int = 24) -> list[dict[str, Any]]:
        time_diff = datetime.now() - timedelta(hours=hours)
        with self._session() as session:
            entries = (
                session.query(MarketRegimeLog)
                .filter(MarketRegimeLog.datetime >= time_diff)
                .order_by(MarketRegimeLog.datetime.desc())
                .all()
            )
            return [self._history_payload(entry) for entry in entries]


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

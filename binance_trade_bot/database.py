import json
import os
import time
from contextlib import contextmanager
from datetime import datetime
from typing import List, Optional, Union

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import Config
from .logger import Logger
from .models import *  # pylint: disable=wildcard-import
from .repositories import (
    BotStateRepository,
    CoinRepository,
    DepositRepository,
    RatioStatsRepository,
    RegimeRepository,
    ScoutHistoryRepository,
    ValueHistoryRepository,
)


class Database:
    def __init__(self, logger: Logger, config: Config, uri="sqlite:///data/crypto_trading.db"):
        self.logger = logger
        self.config = config
        # check_same_thread=False allows the API dashboard to read while the bot writes.
        # WAL mode is set via event listener below for concurrent read/write safety.
        self.engine = create_engine(
            uri,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )
        self.SessionMaker = sessionmaker(bind=self.engine)
        self.coins = CoinRepository(self.SessionMaker)
        self.scout_history = ScoutHistoryRepository(self.SessionMaker)
        self.value_history = ValueHistoryRepository(self.SessionMaker)
        bridge_symbol = getattr(getattr(self.config, "BRIDGE", None), "symbol", "USDC")
        self.ratio_stats = RatioStatsRepository(self.SessionMaker, self.logger, bridge_symbol)
        self.regimes = RegimeRepository(self.SessionMaker)
        self.bot_state = BotStateRepository(self.SessionMaker)
        self.deposits = DepositRepository(self.SessionMaker, self.bot_state, self.logger)
        # Lazily created by socketio_connect(). Importing python-socketio at
        # module import time pulls in eventlet/zmq on this dependency set, which
        # breaks pure helper imports and test collection.
        self.socketio_client = None

        # Enable WAL mode for concurrent read/write safety (API dashboard + bot)
        from sqlalchemy import event

        @event.listens_for(self.engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, conn_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()
            logger.debug("SQLite WAL mode enabled")

    def socketio_connect(self):
        if not getattr(self.config, "SOCKETIO_UPDATES_ENABLED", False):
            return False

        if self.socketio_client is None:
            try:
                from socketio import Client
                self.socketio_client = Client()
            except Exception as e:
                self.logger.debug(f"Socket.IO client unavailable: {e}")
                return False

        if self.socketio_client.connected and self.socketio_client.namespaces:
            return True
        try:
            if not self.socketio_client.connected:
                self.socketio_client.connect("http://api:5123", namespaces=["/backend"])
            while not self.socketio_client.connected or not self.socketio_client.namespaces:
                time.sleep(0.1)
            return True
        except Exception:
            return False

    @contextmanager
    def db_session(self):
        """
        Creates a context with an open SQLAlchemy session.

        Always rolls back on exceptions and always closes the session so a
        failed trade/database operation cannot leak locks or leave a dirty
        transaction open.
        """
        session: Session = self.SessionMaker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def set_coins(self, symbols: List[str]):
        self.coins.set_coins(symbols)

    def get_coins(self, only_enabled=True) -> List[Coin]:
        return self.coins.get_coins(only_enabled=only_enabled)

    def get_coin(self, coin: Union[Coin, str]) -> Coin:
        return self.coins.get_coin(coin)

    def set_current_coin(self, coin: Union[Coin, str]):
        current_coin = self.coins.set_current_coin(coin)
        self.send_update(current_coin)

    def get_current_coin(self) -> Optional[Coin]:
        return self.coins.get_current_coin()

    def get_pair(self, from_coin: Union[Coin, str], to_coin: Union[Coin, str]):
        return self.coins.get_pair(from_coin, to_coin)

    def get_pairs_from(self, from_coin: Union[Coin, str], only_enabled=True) -> List[Pair]:
        return self.coins.get_pairs_from(from_coin, only_enabled=only_enabled)

    def get_pairs(self, only_enabled=True) -> List[Pair]:
        return self.coins.get_pairs(only_enabled=only_enabled)

    def log_scout(
        self,
        pair: Pair,
        target_ratio: float,
        current_coin_price: float,
        other_coin_price: float,
    ):
        scout_history = self.scout_history.log_scout(
            pair,
            target_ratio,
            current_coin_price,
            other_coin_price,
        )
        self.send_update(scout_history)

    def prune_scout_history(self):
        self.scout_history.prune_scout_history(self.config.SCOUT_HISTORY_PRUNE_TIME)

    def prune_value_history(self, reference_time: Optional[datetime] = None):
        self.value_history.prune_value_history(reference_time=reference_time)

    def create_database(self):
        Base.metadata.create_all(self.engine)

    # ── Phase 2/3: Rolling ratio statistics ────────────────────────────────

    def sample_ratios(self, manager):
        """Sample current price ratios for all enabled pairs and store them."""
        self.ratio_stats.sample_ratios(manager)

    def update_pair_stats(self):
        """Compute EMA and std for all enabled pairs from ratio samples."""
        self.ratio_stats.update_pair_stats()

    def prune_ratio_samples(self):
        """Delete ratio samples older than the retention period."""
        self.ratio_stats.prune_ratio_samples(self.config.RATIO_SAMPLE_RETENTION_DAYS)

    def get_pair_stat(self, pair_id):
        """Get cached rolling stats for a pair. Returns (ema, std) or (None, None)."""
        return self.ratio_stats.get_pair_stat(pair_id)

    def get_bot_state(self, key, default=None):
        """Get a persisted strategy state value by key. Returns default if not found."""
        return self.bot_state.get(key, default)

    def set_bot_state(self, key, value):
        """Persist a strategy state value. Creates or updates."""
        self.bot_state.set(key, value)

    def suppress_next_deposit_detection(self, reason="internal transfer"):
        """Suppress one automatic spot-bridge deposit check.

        Use this after internal balance moves (for example futures→spot on BEAR
        exit). The next detector run will seed the baseline to the observed
        spot balance without recording a deposit.
        """
        self.deposits.suppress_next_detection(reason)

    def detect_and_record_deposit(self, current_usdc_balance: float):
        """Detect unexpected USDC balance increases and record real deposits."""
        return self.deposits.detect_and_record(current_usdc_balance)

    def log_market_regime(self, regime, adx_value=None, avg_volatility=None,
                          btc_correlation=None, ema_short=None, ema_long=None):
        """Log a market regime classification."""
        self.regimes.log(regime, adx_value, avg_volatility, btc_correlation, ema_short, ema_long)

    def get_latest_regime(self):
        """Get the most recent regime log entry. Returns dict or None."""
        return self.regimes.get_latest()

    def get_regime_history(self, hours=24):
        """Get regime history for the last N hours."""
        return self.regimes.get_history(hours=hours)

    def start_trade_log(self, from_coin: Coin, to_coin: Coin, selling: bool):
        return TradeLog(self, from_coin, to_coin, selling)

    def send_update(self, model):
        if not self.socketio_connect() or self.socketio_client is None:
            return

        self.socketio_client.emit(
            "update",
            {"table": model.__tablename__, "data": model.info()},
            namespace="/backend",
        )

    def migrate_old_state(self):
        """
        For migrating from old dotfile format to SQL db. This method should be removed in
        the future.
        """
        if os.path.isfile(".current_coin"):
            with open(".current_coin") as f:
                coin = f.read().strip()
                self.logger.info(f".current_coin file found, loading current coin {coin}")
                self.set_current_coin(coin)
            os.rename(".current_coin", ".current_coin.old")
            self.logger.info(f".current_coin renamed to .current_coin.old - You can now delete this file")

        if os.path.isfile(".current_coin_table"):
            with open(".current_coin_table") as f:
                self.logger.info(f".current_coin_table file found, loading into database")
                table: dict = json.load(f)
                session: Session
                with self.db_session() as session:
                    for from_coin, to_coin_dict in table.items():
                        for to_coin, ratio in to_coin_dict.items():
                            if from_coin == to_coin:
                                continue
                            pair = session.merge(self.get_pair(from_coin, to_coin))
                            pair.ratio = ratio
                            session.add(pair)

            os.rename(".current_coin_table", ".current_coin_table.old")
            self.logger.info(".current_coin_table renamed to .current_coin_table.old - " "You can now delete this file")


class TradeLog:
    def __init__(self, db: Database, from_coin: Coin, to_coin: Coin, selling: bool):
        self.db = db
        session: Session
        with self.db.db_session() as session:
            from_coin = session.merge(from_coin)
            to_coin = session.merge(to_coin)
            self.trade = Trade(from_coin, to_coin, selling)
            session.add(self.trade)
            # Flush so that SQLAlchemy fills in the id column
            session.flush()
            self.db.send_update(self.trade)

    def set_ordered(self, alt_starting_balance, crypto_starting_balance, alt_trade_amount):
        session: Session
        with self.db.db_session() as session:
            trade: Trade = session.merge(self.trade)
            trade.alt_starting_balance = alt_starting_balance
            trade.alt_trade_amount = alt_trade_amount
            trade.crypto_starting_balance = crypto_starting_balance
            trade.state = TradeState.ORDERED
            self.db.send_update(trade)

    def set_complete(self, crypto_trade_amount):
        session: Session
        with self.db.db_session() as session:
            trade: Trade = session.merge(self.trade)
            trade.crypto_trade_amount = crypto_trade_amount
            trade.state = TradeState.COMPLETE
            self.db.send_update(trade)

    def set_failed(self, reason=""):
        """Mark a trade as FAILED (e.g. sell succeeded but buy failed)."""
        session: Session
        with self.db.db_session() as session:
            trade: Trade = session.merge(self.trade)
            trade.state = TradeState.FAILED
            self.db.send_update(trade)
            # Log the failure reason
            if reason:
                self.db.logger.warning(f"Trade FAILED: {self.trade.alt_coin_id} -> {self.trade.crypto_coin_id}: {reason}")


if __name__ == "__main__":
    database = Database(Logger(), Config())
    database.create_database()

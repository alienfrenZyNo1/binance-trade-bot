import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import List, Optional, Union

from sqlalchemy import create_engine, func
from sqlalchemy.orm import Session, sessionmaker

from .accounting import evaluate_deposit_delta
from .config import Config
from .logger import Logger
from .models import *  # pylint: disable=wildcard-import


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
        """Sync coin list from config, but NEVER re-enable a coin that was
        manually disabled (e.g. via Telegram /removecoin or DB edit).

        Only coins that are brand-new (not yet in DB) are added as enabled.
        Existing coins keep their current enabled/disabled state unless they
        are no longer in the config list at all, in which case they are disabled.
        """
        session: Session

        with self.db_session() as session:
            coins: List[Coin] = session.query(Coin).all()

            # Coins in DB but not in config → disable
            for coin in coins:
                if coin.symbol not in symbols:
                    coin.enabled = False

            # Coins in config but not yet in DB → add as enabled
            # Coins already in DB → KEEP their existing enabled state
            # (do NOT force re-enable — respects manual /removecoin etc.)
            for symbol in symbols:
                coin = next((c for c in coins if c.symbol == symbol), None)
                if coin is None:
                    session.add(Coin(symbol))

        # For all the combinations of coins in the database, add a pair to the database
        with self.db_session() as session:
            coins: List[Coin] = session.query(Coin).filter(Coin.enabled).all()
            for from_coin in coins:
                for to_coin in coins:
                    if from_coin != to_coin:
                        pair = session.query(Pair).filter(Pair.from_coin == from_coin, Pair.to_coin == to_coin).first()
                        if pair is None:
                            session.add(Pair(from_coin, to_coin))

    def get_coins(self, only_enabled=True) -> List[Coin]:
        session: Session
        with self.db_session() as session:
            if only_enabled:
                coins = session.query(Coin).filter(Coin.enabled).all()
            else:
                coins = session.query(Coin).all()
            session.expunge_all()
            return coins

    def get_coin(self, coin: Union[Coin, str]) -> Coin:
        if isinstance(coin, Coin):
            return coin
        session: Session
        with self.db_session() as session:
            coin = session.query(Coin).get(coin)
            session.expunge(coin)
            return coin

    def set_current_coin(self, coin: Union[Coin, str]):
        coin = self.get_coin(coin)
        session: Session
        with self.db_session() as session:
            if isinstance(coin, Coin):
                coin = session.merge(coin)
            cc = CurrentCoin(coin)
            session.add(cc)
            self.send_update(cc)

    def get_current_coin(self) -> Optional[Coin]:
        session: Session
        with self.db_session() as session:
            current_coin = session.query(CurrentCoin).order_by(CurrentCoin.datetime.desc()).first()
            if current_coin is None:
                return None
            coin = current_coin.coin
            session.expunge(coin)
            return coin

    def get_pair(self, from_coin: Union[Coin, str], to_coin: Union[Coin, str]):
        from_coin = self.get_coin(from_coin)
        to_coin = self.get_coin(to_coin)
        session: Session
        with self.db_session() as session:
            pair: Pair = session.query(Pair).filter(Pair.from_coin == from_coin, Pair.to_coin == to_coin).first()
            session.expunge(pair)
            return pair

    def get_pairs_from(self, from_coin: Union[Coin, str], only_enabled=True) -> List[Pair]:
        from_coin = self.get_coin(from_coin)
        session: Session
        with self.db_session() as session:
            pairs = session.query(Pair).filter(Pair.from_coin == from_coin)
            if only_enabled:
                pairs = pairs.filter(Pair.enabled.is_(True))
            pairs = pairs.all()
            session.expunge_all()
            return pairs

    def get_pairs(self, only_enabled=True) -> List[Pair]:
        session: Session
        with self.db_session() as session:
            pairs = session.query(Pair)
            if only_enabled:
                pairs = pairs.filter(Pair.enabled.is_(True))
            pairs = pairs.all()
            session.expunge_all()
            return pairs

    def log_scout(
        self,
        pair: Pair,
        target_ratio: float,
        current_coin_price: float,
        other_coin_price: float,
    ):
        session: Session
        with self.db_session() as session:
            pair = session.merge(pair)
            sh = ScoutHistory(pair, target_ratio, current_coin_price, other_coin_price)
            session.add(sh)
            self.send_update(sh)

    def prune_scout_history(self):
        time_diff = datetime.now() - timedelta(hours=self.config.SCOUT_HISTORY_PRUNE_TIME)
        session: Session
        with self.db_session() as session:
            session.query(ScoutHistory).filter(ScoutHistory.datetime < time_diff).delete()

    def prune_value_history(self):
        session: Session
        with self.db_session() as session:
            # Sets the first entry for each coin for each hour as 'hourly'
            hourly_entries: List[CoinValue] = (
                session.query(CoinValue).group_by(CoinValue.coin_id, func.strftime("%H", CoinValue.datetime)).all()
            )
            for entry in hourly_entries:
                entry.interval = Interval.HOURLY

            # Sets the first entry for each coin for each day as 'daily'
            daily_entries: List[CoinValue] = (
                session.query(CoinValue).group_by(CoinValue.coin_id, func.date(CoinValue.datetime)).all()
            )
            for entry in daily_entries:
                entry.interval = Interval.DAILY

            # Sets the first entry for each coin for each month as 'weekly'
            # (Sunday is the start of the week)
            weekly_entries: List[CoinValue] = (
                session.query(CoinValue).group_by(CoinValue.coin_id, func.strftime("%Y-%W", CoinValue.datetime)).all()
            )
            for entry in weekly_entries:
                entry.interval = Interval.WEEKLY

            # The last 24 hours worth of minutely entries will be kept, so
            # count(coins) * 1440 entries
            time_diff = datetime.now() - timedelta(hours=24)
            session.query(CoinValue).filter(
                CoinValue.interval == Interval.MINUTELY, CoinValue.datetime < time_diff
            ).delete()

            # The last 28 days worth of hourly entries will be kept, so count(coins) * 672 entries
            time_diff = datetime.now() - timedelta(days=28)
            session.query(CoinValue).filter(
                CoinValue.interval == Interval.HOURLY, CoinValue.datetime < time_diff
            ).delete()

            # The last years worth of daily entries will be kept, so count(coins) * 365 entries
            time_diff = datetime.now() - timedelta(days=365)
            session.query(CoinValue).filter(
                CoinValue.interval == Interval.DAILY, CoinValue.datetime < time_diff
            ).delete()

            # All weekly entries will be kept forever

    def create_database(self):
        Base.metadata.create_all(self.engine)

    # ── Phase 2/3: Rolling ratio statistics ────────────────────────────────

    def sample_ratios(self, manager):
        """Sample current price ratios for all enabled pairs and store them."""
        from .models import Coin as CoinModel

        session: Session
        with self.db_session() as session:
            coins = session.query(CoinModel).filter(CoinModel.enabled.is_(True)).all()
            if len(coins) < 2:
                return

            # Fetch all prices in one API call
            prices = {}
            for coin in coins:
                price = manager.get_ticker_price(coin.symbol + self.config.BRIDGE.symbol)
                if price is not None and price > 0:
                    prices[coin.symbol] = price

            if len(prices) < 2:
                self.logger.info("Not enough prices to sample ratios")
                return

            pair_count = 0
            for from_coin in coins:
                for to_coin in coins:
                    if from_coin.symbol == to_coin.symbol:
                        continue
                    if from_coin.symbol not in prices or to_coin.symbol not in prices:
                        continue

                    pair = session.query(Pair).filter(
                        Pair.from_coin_id == from_coin.symbol,
                        Pair.to_coin_id == to_coin.symbol,
                    ).first()
                    if pair is None:
                        continue

                    ratio = prices[from_coin.symbol] / prices[to_coin.symbol]
                    session.add(RatioSample(pair.id, ratio))
                    pair_count += 1

            self.logger.info(f"Sampled {pair_count} pair ratios")

    def update_pair_stats(self):
        """Compute EMA and std for all enabled pairs from ratio samples."""
        import statistics as stats_mod

        session: Session
        with self.db_session() as session:
            # Get all enabled pairs
            pairs = session.query(Pair).all()
            updated = 0

            for pair in pairs:
                # Get recent samples (up to 1008 = 7 days at 10-min intervals)
                samples = session.query(RatioSample).filter(
                    RatioSample.pair_id == pair.id
                ).order_by(RatioSample.datetime.desc()).limit(1008).all()

                if len(samples) < 5:
                    continue

                ratios = [s.ratio for s in samples]
                ratios.reverse()  # chronological order for EMA

                # Compute EMA (span = 144 = 1 day at 10-min intervals)
                span = min(144, len(ratios))
                alpha = 2.0 / (span + 1)
                ema = ratios[0]
                for r in ratios[1:]:
                    ema = alpha * r + (1 - alpha) * ema

                # Compute standard deviation
                std = stats_mod.pstdev(ratios) if len(ratios) > 1 else 0.0

                # Store/update the stat
                stat = session.query(PairStat).filter(PairStat.pair_id == pair.id).first()
                if stat:
                    stat.ema_ratio = ema
                    stat.std_ratio = std
                    stat.sample_count = len(ratios)
                    stat.last_updated = datetime.utcnow()
                else:
                    session.add(PairStat(pair.id, ema, std, len(ratios)))

                # Also update pair.ratio so existing code + /hop command benefit
                pair.ratio = ema
                updated += 1

            self.logger.info(f"Updated rolling stats for {updated} pairs")

    def prune_ratio_samples(self):
        """Delete ratio samples older than the retention period."""
        retention_days = self.config.RATIO_SAMPLE_RETENTION_DAYS
        time_diff = datetime.now() - timedelta(days=retention_days)
        session: Session
        with self.db_session() as session:
            deleted = session.query(RatioSample).filter(
                RatioSample.datetime < time_diff
            ).delete()
            if deleted:
                self.logger.info(f"Pruned {deleted} old ratio samples")

    def get_pair_stat(self, pair_id):
        """Get cached rolling stats for a pair. Returns (ema, std) or (None, None)."""
        session: Session
        with self.db_session() as session:
            stat = session.query(PairStat).filter(PairStat.pair_id == pair_id).first()
            if stat and stat.sample_count >= 5:
                return stat.ema_ratio, stat.std_ratio
            return None, None

    def get_bot_state(self, key, default=None):
        """Get a persisted strategy state value by key. Returns default if not found."""
        from .models import BotState
        session: Session
        with self.db_session() as session:
            entry = session.query(BotState).filter(BotState.key == key).first()
            if entry:
                return entry.value
            return default

    def set_bot_state(self, key, value):
        """Persist a strategy state value. Creates or updates."""
        from .models import BotState
        session: Session
        with self.db_session() as session:
            entry = session.query(BotState).filter(BotState.key == key).first()
            if entry:
                entry.value = str(value)
                entry.updated_at = datetime.utcnow()
            else:
                session.add(BotState(key, str(value)))

    def suppress_next_deposit_detection(self, reason="internal transfer"):
        """Suppress one automatic spot-bridge deposit check.

        Use this after internal balance moves (for example futures→spot on BEAR
        exit). The next detector run will seed the baseline to the observed
        spot balance without recording a deposit.
        """
        self.set_bot_state("suppress_next_usdc_deposit_detection", "True")
        self.set_bot_state("suppress_next_usdc_deposit_reason", reason)

    def detect_and_record_deposit(self, current_usdc_balance: float):
        """Detect unexpected USDC balance increases (deposits) and record them.

        Compares current USDC balance against the last known balance stored in bot_state.
        If current balance exceeds last known by more than a small tolerance (accounts
        for rounding, tiny airdrops, etc.), the difference is recorded as a deposit.
        Internal transfers can suppress one detector cycle via
        suppress_next_deposit_detection().

        Returns the deposit amount recorded (0 if none).
        """
        MIN_DEPOSIT_THRESHOLD = 1.0  # Ignore increases under $1

        last_bal_str = self.get_bot_state("last_usdc_balance", "0")
        try:
            last_usdc_balance = float(last_bal_str)
        except (ValueError, TypeError):
            last_usdc_balance = 0.0

        suppress_once = str(
            self.get_bot_state("suppress_next_usdc_deposit_detection", "False")
        ).lower() in ("true", "1", "yes")
        reason = self.get_bot_state("suppress_next_usdc_deposit_reason", "internal transfer")

        evaluation = evaluate_deposit_delta(
            last_balance=last_usdc_balance,
            current_balance=current_usdc_balance,
            suppress_once=suppress_once,
            min_threshold=MIN_DEPOSIT_THRESHOLD,
        )

        # Update stored balance for next cycle
        self.set_bot_state("last_usdc_balance", str(evaluation.new_baseline))

        if evaluation.suppression_consumed:
            self.set_bot_state("suppress_next_usdc_deposit_detection", "False")
            self.set_bot_state("suppress_next_usdc_deposit_reason", "")
            self.logger.info(
                f"Deposit detector baseline reset after {reason}: "
                f"{evaluation.new_baseline:.2f} USDC"
            )
            return 0.0

        if evaluation.deposit_amount > 0:
            increase = evaluation.deposit_amount
            # Record the deposit
            session: Session
            with self.db_session() as session:
                dep = Deposit(
                    amount=increase,
                    currency="USDC",
                    source="auto",
                    note=f"Auto-detected: balance increased from {last_usdc_balance:.2f} to {current_usdc_balance:.2f}",
                    datetime=datetime.utcnow(),
                )
                session.add(dep)
            self.logger.info(
                f"💰 Deposit auto-detected: ${increase:.2f} USDC "
                f"(balance {last_usdc_balance:.2f} → {current_usdc_balance:.2f})"
            )
            return increase

        return 0.0

    def log_market_regime(self, regime, adx_value=None, avg_volatility=None,
                          btc_correlation=None, ema_short=None, ema_long=None):
        """Log a market regime classification."""
        from .models import MarketRegimeLog
        session: Session
        with self.db_session() as session:
            entry = MarketRegimeLog(
                regime, adx_value, avg_volatility, btc_correlation, ema_short, ema_long
            )
            session.add(entry)

    def get_latest_regime(self):
        """Get the most recent regime log entry. Returns dict or None."""
        from .models import MarketRegimeLog
        session: Session
        with self.db_session() as session:
            entry = session.query(MarketRegimeLog).order_by(
                MarketRegimeLog.datetime.desc()
            ).first()
            if entry:
                return {
                    "regime": entry.regime,
                    "adx_value": entry.adx_value,
                    "avg_volatility": entry.avg_volatility,
                    "btc_correlation": entry.btc_correlation,
                    "datetime": entry.datetime.isoformat() if entry.datetime else None,
                }
            return None

    def get_regime_history(self, hours=24):
        """Get regime history for the last N hours."""
        from .models import MarketRegimeLog
        time_diff = datetime.now() - timedelta(hours=hours)
        session: Session
        with self.db_session() as session:
            entries = session.query(MarketRegimeLog).filter(
                MarketRegimeLog.datetime >= time_diff
            ).order_by(MarketRegimeLog.datetime.desc()).all()
            return [
                {
                    "regime": e.regime,
                    "adx": e.adx_value,
                    "vol": e.avg_volatility,
                    "datetime": e.datetime.isoformat() if e.datetime else None,
                }
                for e in entries
            ]

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

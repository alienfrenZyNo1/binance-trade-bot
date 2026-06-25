"""Repository seam tests for database state/deposit persistence."""

from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from binance_trade_bot.database import Database
from binance_trade_bot.models import Base, Coin, Deposit, MarketRegimeLog, Pair
from binance_trade_bot.repositories import (
    BotStateRepository,
    CoinRepository,
    DepositRepository,
    RegimeRepository,
)


class FakeLogger:
    def __init__(self):
        self.infos = []
        self.debugs = []

    def info(self, message, *args, **kwargs):
        self.infos.append(str(message))

    def debug(self, message, *args, **kwargs):
        self.debugs.append(str(message))


def make_session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_coin_repository_sync_preserves_manual_disabled_state_and_pairs():
    sessions = make_session_factory()
    repo = CoinRepository(sessions)

    repo.set_coins(["SOL", "AVAX"])
    with sessions() as session:
        session.query(Coin).filter(Coin.symbol == "AVAX").one().enabled = False
        session.commit()

    repo.set_coins(["SOL", "AVAX", "JUP"])

    coins = {coin.symbol: coin.enabled for coin in repo.get_coins(only_enabled=False)}
    assert coins == {"SOL": True, "AVAX": False, "JUP": True}
    assert [coin.symbol for coin in repo.get_coins()] == ["SOL", "JUP"]

    enabled_pairs = {(pair.from_coin.symbol, pair.to_coin.symbol) for pair in repo.get_pairs()}
    assert enabled_pairs == {("SOL", "JUP"), ("JUP", "SOL")}

    all_pairs = {(pair.from_coin.symbol, pair.to_coin.symbol) for pair in repo.get_pairs(only_enabled=False)}
    assert ("SOL", "AVAX") in all_pairs
    assert ("AVAX", "SOL") in all_pairs


def test_coin_repository_tracks_current_coin_and_pair_lookup():
    sessions = make_session_factory()
    repo = CoinRepository(sessions)
    repo.set_coins(["SOL", "JUP"])

    repo.set_current_coin("SOL")
    assert repo.get_current_coin().symbol == "SOL"

    current_coin_event = repo.set_current_coin("SOL")
    assert current_coin_event.info()["coin"] == {"symbol": "SOL", "enabled": True}

    jup = repo.get_coin("JUP")
    repo.set_current_coin(jup)
    assert repo.get_current_coin().symbol == "JUP"

    pair = repo.get_pair("SOL", "JUP")
    assert pair.from_coin.symbol == "SOL"
    assert pair.to_coin.symbol == "JUP"


def test_database_facade_delegates_coin_methods_and_sends_current_coin_update():
    logger = FakeLogger()
    config = SimpleNamespace(SOCKETIO_UPDATES_ENABLED=False)
    db = Database(logger, config, uri="sqlite:///:memory:")
    db.create_database()

    db.set_coins(["SOL", "JUP"])
    db.set_current_coin("SOL")

    assert db.get_current_coin().symbol == "SOL"
    assert db.get_pair("SOL", "JUP").from_coin.symbol == "SOL"
    assert {(p.from_coin.symbol, p.to_coin.symbol) for p in db.get_pairs()} == {
        ("SOL", "JUP"),
        ("JUP", "SOL"),
    }


def test_regime_repository_logs_latest_and_hour_filtered_history():
    sessions = make_session_factory()
    repo = RegimeRepository(sessions)

    repo.log("BULL", adx_value=31.5, avg_volatility=2.25, btc_correlation=0.72)
    repo.log("BEAR", adx_value=42.0, avg_volatility=-6.0, ema_short=88.0, ema_long=91.0)

    latest = repo.get_latest()
    assert latest["regime"] == "BEAR"
    assert latest["adx_value"] == 42.0
    assert latest["avg_volatility"] == -6.0
    assert latest["btc_correlation"] is None
    assert latest["datetime"] is not None

    with sessions() as session:
        older = session.query(MarketRegimeLog).filter(MarketRegimeLog.regime == "BULL").one()
        older.datetime = datetime.utcnow() - timedelta(hours=3)
        session.commit()

    recent_history = repo.get_history(hours=1)
    assert recent_history == [
        {
            "regime": "BEAR",
            "adx": 42.0,
            "vol": -6.0,
            "datetime": recent_history[0]["datetime"],
        }
    ]


def test_database_facade_delegates_regime_methods():
    logger = FakeLogger()
    config = SimpleNamespace(SOCKETIO_UPDATES_ENABLED=False)
    db = Database(logger, config, uri="sqlite:///:memory:")
    db.create_database()

    db.log_market_regime("SIDEWAYS", adx_value=18.0, avg_volatility=0.4, btc_correlation=0.3)

    latest = db.get_latest_regime()
    assert latest["regime"] == "SIDEWAYS"
    assert latest["adx_value"] == 18.0
    assert db.get_regime_history(hours=1)[0]["regime"] == "SIDEWAYS"


def test_bot_state_repository_get_set_and_default():
    sessions = make_session_factory()
    repo = BotStateRepository(sessions)

    assert repo.get("missing", "fallback") == "fallback"

    repo.set("regime", "BEAR")
    assert repo.get("regime") == "BEAR"

    repo.set("regime", "SIDEWAYS")
    assert repo.get("regime") == "SIDEWAYS"


def test_deposit_repository_suppresses_one_internal_transfer_cycle():
    sessions = make_session_factory()
    state_repo = BotStateRepository(sessions)
    repo = DepositRepository(sessions, state_repo, FakeLogger())

    repo.suppress_next_detection("internal futures→spot transfer")
    recorded = repo.detect_and_record(current_usdc_balance=56.25)

    assert recorded == 0.0
    assert state_repo.get("last_usdc_balance") == "56.25"
    assert state_repo.get("suppress_next_usdc_deposit_detection") == "False"
    assert state_repo.get("suppress_next_usdc_deposit_reason") == ""

    with sessions() as session:
        assert session.query(Deposit).count() == 0


def test_deposit_repository_records_external_deposit():
    sessions = make_session_factory()
    state_repo = BotStateRepository(sessions)
    repo = DepositRepository(sessions, state_repo, FakeLogger())
    state_repo.set("last_usdc_balance", "10.0")

    recorded = repo.detect_and_record(current_usdc_balance=66.25)

    assert recorded == 56.25
    assert state_repo.get("last_usdc_balance") == "66.25"

    with sessions() as session:
        deposit = session.query(Deposit).one()
        assert deposit.amount == 56.25
        assert deposit.currency == "USDC"
        assert deposit.source == "auto"
        assert "balance increased from 10.00 to 66.25" in deposit.note

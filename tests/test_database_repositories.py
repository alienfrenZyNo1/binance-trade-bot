"""Repository seam tests for database state/deposit persistence."""

from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from binance_trade_bot.database import Database
from binance_trade_bot.models import (
    Base,
    Coin,
    Deposit,
    MarketRegimeLog,
    Pair,
    PairStat,
    RatioSample,
    ScoutHistory,
)
from binance_trade_bot.repositories import (
    BotStateRepository,
    CoinRepository,
    DepositRepository,
    RegimeRepository,
    RatioStatsRepository,
    ScoutHistoryRepository,
)


class FakeLogger:
    def __init__(self):
        self.infos = []
        self.debugs = []

    def info(self, message, *args, **kwargs):
        self.infos.append(str(message))

    def debug(self, message, *args, **kwargs):
        self.debugs.append(str(message))


class FakeManager:
    def __init__(self, prices):
        self.prices = prices
        self.requested_symbols = []

    def get_ticker_price(self, symbol):
        self.requested_symbols.append(symbol)
        return self.prices.get(symbol)


def make_config(**overrides):
    values = {
        "SOCKETIO_UPDATES_ENABLED": False,
        "SCOUT_HISTORY_PRUNE_TIME": 1,
        "RATIO_SAMPLE_RETENTION_DAYS": 1,
        "BRIDGE": SimpleNamespace(symbol="USDC"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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
    config = make_config()
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


def test_scout_history_repository_logs_detached_event_and_prunes_old_rows():
    sessions = make_session_factory()
    coins = CoinRepository(sessions)
    coins.set_coins(["SOL", "JUP"])
    pair = coins.get_pair("SOL", "JUP")
    repo = ScoutHistoryRepository(sessions)

    event = repo.log_scout(
        pair,
        target_ratio=1.25,
        current_coin_price=100.0,
        other_coin_price=80.0,
    )

    assert event.info()["from_coin"] == {"symbol": "SOL", "enabled": True}
    assert event.info()["to_coin"] == {"symbol": "JUP", "enabled": True}
    assert event.current_ratio == 1.25

    with sessions() as session:
        old = session.query(ScoutHistory).one()
        old.datetime = datetime.utcnow() - timedelta(hours=5)
        session.commit()

    repo.prune_scout_history(hours_to_keep=1)

    with sessions() as session:
        assert session.query(ScoutHistory).count() == 0


def test_database_facade_delegates_scout_history_methods():
    logger = FakeLogger()
    config = make_config(SCOUT_HISTORY_PRUNE_TIME=1)
    db = Database(logger, config, uri="sqlite:///:memory:")
    db.create_database()
    db.set_coins(["SOL", "JUP"])
    pair = db.get_pair("SOL", "JUP")

    db.log_scout(pair, target_ratio=2.0, current_coin_price=6.0, other_coin_price=3.0)

    with db.db_session() as session:
        row = session.query(ScoutHistory).one()
        assert row.target_ratio == 2.0
        row.datetime = datetime.utcnow() - timedelta(hours=2)

    db.prune_scout_history()

    with db.db_session() as session:
        assert session.query(ScoutHistory).count() == 0


def test_ratio_stats_repository_samples_updates_prunes_and_reads_stats():
    sessions = make_session_factory()
    coins = CoinRepository(sessions)
    coins.set_coins(["SOL", "JUP"])
    logger = FakeLogger()
    repo = RatioStatsRepository(sessions, logger, bridge_symbol="USDC")

    manager = FakeManager({"SOLUSDC": 100.0, "JUPUSDC": 50.0})
    sampled = repo.sample_ratios(manager)

    assert sampled == 2
    assert set(manager.requested_symbols) == {"SOLUSDC", "JUPUSDC"}
    with sessions() as session:
        samples = session.query(RatioSample).order_by(RatioSample.pair_id).all()
        assert len(samples) == 2
        assert {sample.ratio for sample in samples} == {2.0, 0.5}

    pair_id = None
    with sessions() as session:
        pair_id = session.query(Pair).filter(Pair.from_coin_id == "SOL", Pair.to_coin_id == "JUP").one().id

    for offset, ratio in enumerate([1.0, 1.1, 1.2, 1.3, 1.4]):
        with sessions() as session:
            sample = RatioSample(pair_id, ratio)
            sample.datetime = datetime.utcnow() - timedelta(minutes=offset)
            session.add(sample)
            session.commit()

    updated = repo.update_pair_stats()

    assert updated >= 1
    ema, std = repo.get_pair_stat(pair_id)
    assert ema is not None
    assert std is not None
    with sessions() as session:
        pair = session.query(Pair).filter(Pair.id == pair_id).one()
        stat = session.query(PairStat).filter(PairStat.pair_id == pair_id).one()
        assert pair.ratio == stat.ema_ratio
        old_sample = session.query(RatioSample).first()
        old_sample.datetime = datetime.utcnow() - timedelta(days=10)
        session.commit()

    deleted = repo.prune_ratio_samples(retention_days=1)

    assert deleted >= 1
    assert any("Sampled 2 pair ratios" in message for message in logger.infos)
    assert any("Updated rolling stats" in message for message in logger.infos)
    assert any("Pruned" in message for message in logger.infos)


def test_database_facade_delegates_ratio_stat_methods():
    logger = FakeLogger()
    config = make_config(RATIO_SAMPLE_RETENTION_DAYS=1, BRIDGE=SimpleNamespace(symbol="USDC"))
    db = Database(logger, config, uri="sqlite:///:memory:")
    db.create_database()
    db.set_coins(["SOL", "JUP"])
    manager = FakeManager({"SOLUSDC": 90.0, "JUPUSDC": 30.0})

    db.sample_ratios(manager)
    with db.db_session() as session:
        pair_id = session.query(Pair).filter(Pair.from_coin_id == "SOL", Pair.to_coin_id == "JUP").one().id
        for ratio in [1.0, 1.05, 1.1, 1.15, 1.2]:
            session.add(RatioSample(pair_id, ratio))

    db.update_pair_stats()

    ema, std = db.get_pair_stat(pair_id)
    assert ema is not None
    assert std is not None

    with db.db_session() as session:
        old_sample = session.query(RatioSample).first()
        old_sample.datetime = datetime.utcnow() - timedelta(days=2)

    db.prune_ratio_samples()
    with db.db_session() as session:
        assert session.query(RatioSample).filter(RatioSample.datetime < datetime.utcnow() - timedelta(days=1)).count() == 0


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
    config = make_config()
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

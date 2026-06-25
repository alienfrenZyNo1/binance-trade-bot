"""Repository seam tests for database state/deposit persistence."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from binance_trade_bot.models import Base, Deposit
from binance_trade_bot.repositories import BotStateRepository, DepositRepository


class FakeLogger:
    def __init__(self):
        self.infos = []

    def info(self, message, *args, **kwargs):
        self.infos.append(str(message))


def make_session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


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

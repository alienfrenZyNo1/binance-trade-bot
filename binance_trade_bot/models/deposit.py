from datetime import datetime

from sqlalchemy import Column, DateTime, Float, String, Text

from .base import Base


class Deposit(Base):
    """Record of capital deposited into the trading account."""
    __tablename__ = "deposits"

    id = Column(Float, primary_key=True, autoincrement=True)
    amount = Column(Float, nullable=False)
    currency = Column(String, default="USDC")
    source = Column(String, default="manual")
    note = Column(Text, default="")
    datetime = Column(DateTime, default=datetime.utcnow)

    def __init__(self, amount, currency="USDC", source="manual", note="", datetime=None):
        self.amount = amount
        self.currency = currency
        self.source = source
        self.note = note
        self.datetime = datetime or datetime.utcnow()

from datetime import datetime

from sqlalchemy import Column, DateTime, String, Text

from .base import Base


class BotState(Base):
    """Persistent key-value store for strategy state that must survive restarts."""
    __tablename__ = "bot_state"

    key = Column(String, primary_key=True)
    value = Column(Text)          # JSON-serialised value
    updated_at = Column(DateTime)

    def __init__(self, key, value):
        self.key = key
        self.value = value
        self.updated_at = datetime.utcnow()

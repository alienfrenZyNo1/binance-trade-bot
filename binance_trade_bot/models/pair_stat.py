from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String

from .base import Base


class PairStat(Base):
    """Rolling statistics for a pair: EMA ratio and standard deviation."""
    __tablename__ = "pair_stats"

    id = Column(Integer, primary_key=True)
    pair_id = Column(String, ForeignKey("pairs.id"), unique=True)
    ema_ratio = Column(Float)       # Exponential moving average of ratio
    std_ratio = Column(Float)       # Standard deviation of ratio
    sample_count = Column(Integer)  # How many samples were used
    last_updated = Column(DateTime)

    def __init__(self, pair_id, ema_ratio, std_ratio, sample_count):
        self.pair_id = pair_id
        self.ema_ratio = ema_ratio
        self.std_ratio = std_ratio
        self.sample_count = sample_count
        self.last_updated = datetime.utcnow()

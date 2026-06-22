from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String

from .base import Base


class RatioSample(Base):
    """Periodic ratio snapshot for computing rolling EMA and std."""
    __tablename__ = "ratio_samples"

    id = Column(Integer, primary_key=True)
    pair_id = Column(String, ForeignKey("pairs.id"))
    ratio = Column(Float)  # from_coin_price / to_coin_price
    datetime = Column(DateTime)

    def __init__(self, pair_id, ratio):
        self.pair_id = pair_id
        self.ratio = ratio
        self.datetime = datetime.utcnow()

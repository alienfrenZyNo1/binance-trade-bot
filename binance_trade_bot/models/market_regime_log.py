from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String

from .base import Base


class MarketRegimeLog(Base):
    """Log of market regime classifications for analysis."""
    __tablename__ = "market_regime_log"

    id = Column(Integer, primary_key=True)
    regime = Column(String)           # bull, bear, sideways, stormy
    adx_value = Column(Float)         # ADX indicator value
    avg_volatility = Column(Float)    # Average 24h % change across coins
    btc_correlation = Column(Float)   # Average correlation with BTC
    ema_short = Column(Float)         # Short EMA value
    ema_long = Column(Float)          # Long EMA value
    datetime = Column(DateTime)

    def __init__(self, regime, adx_value=None, avg_volatility=None,
                 btc_correlation=None, ema_short=None, ema_long=None):
        self.regime = regime
        self.adx_value = adx_value
        self.avg_volatility = avg_volatility
        self.btc_correlation = btc_correlation
        self.ema_short = ema_short
        self.ema_long = ema_long
        self.datetime = datetime.utcnow()

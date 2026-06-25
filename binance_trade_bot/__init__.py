"""binance_trade_bot package exports.

Keep package import lightweight: importing a pure helper submodule such as
`binance_trade_bot.indicators` must not eagerly import database/socketio/eventlet
or start heavy runtime dependencies.  Legacy top-level exports are loaded lazily.
"""

__all__ = ["backtest", "BinanceAPIManager", "run_trader"]


def __getattr__(name):
    if name == "backtest":
        from .backtest import backtest
        return backtest
    if name == "BinanceAPIManager":
        from .binance_api_manager import BinanceAPIManager
        return BinanceAPIManager
    if name == "run_trader":
        from .crypto_trading import main
        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

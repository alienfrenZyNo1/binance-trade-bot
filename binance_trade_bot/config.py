# Config consts
import configparser
import os

from .models import Coin

CFG_FL_NAME = "user.cfg"
USER_CFG_SECTION = "binance_user_config"


class Config:  # pylint: disable=too-few-public-methods,too-many-instance-attributes
    def __init__(self):
        # Init config
        config = configparser.ConfigParser()
        config["DEFAULT"] = {
            "bridge": "USDT",
            "use_margin": "no",
            "scout_multiplier": "5",
            "scout_margin": "0.8",
            "scout_sleep_time": "5",
            "hourToKeepScoutHistory": "1",
            "tld": "com",
            "strategy": "default",
            "sell_timeout": "0",
            "buy_timeout": "0",
            "testnet": False,
        }

        if not os.path.exists(CFG_FL_NAME):
            print("No configuration file (user.cfg) found! See README. Assuming default config...")
            config[USER_CFG_SECTION] = {}
        else:
            config.read(CFG_FL_NAME)

        self.BRIDGE_SYMBOL = os.environ.get("BRIDGE_SYMBOL") or config.get(USER_CFG_SECTION, "bridge")
        self.BRIDGE = Coin(self.BRIDGE_SYMBOL, False)
        _testnet_env = os.environ.get("TESTNET", "").strip().lower()
        if _testnet_env:
            self.TESTNET = _testnet_env in ("true", "1", "yes")
        else:
            self.TESTNET = config.getboolean(USER_CFG_SECTION, "testnet")

        # Prune settings
        self.SCOUT_HISTORY_PRUNE_TIME = float(
            os.environ.get("HOURS_TO_KEEP_SCOUTING_HISTORY") or config.get(USER_CFG_SECTION, "hourToKeepScoutHistory")
        )

        # Get config for scout
        self.SCOUT_MULTIPLIER = float(
            os.environ.get("SCOUT_MULTIPLIER") or config.get(USER_CFG_SECTION, "scout_multiplier")
        )
        self.SCOUT_SLEEP_TIME = int(
            os.environ.get("SCOUT_SLEEP_TIME") or config.get(USER_CFG_SECTION, "scout_sleep_time")
        )

        # Get config for binance
        self.BINANCE_API_KEY = os.environ.get("API_KEY") or config.get(USER_CFG_SECTION, "api_key")
        self.BINANCE_API_SECRET_KEY = os.environ.get("API_SECRET_KEY") or config.get(USER_CFG_SECTION, "api_secret_key")
        self.BINANCE_TLD = os.environ.get("TLD") or config.get(USER_CFG_SECTION, "tld")

        # Optional legacy Flask-SocketIO dashboard updates. Disabled by default
        # in the live trading bot because importing python-socketio pulls in
        # eventlet/zmq on this dependency set, creating noisy runtime warnings
        # even when the API dashboard sidecar is not running.
        self.SOCKETIO_UPDATES_ENABLED = (
            os.environ.get("SOCKETIO_UPDATES_ENABLED")
            or config.get(USER_CFG_SECTION, "socketio_updates_enabled", fallback="no")
        ).lower() in ("yes", "true", "1", "on")

        # Get supported coin list from the environment
        supported_coin_list = [
            coin.strip() for coin in os.environ.get("SUPPORTED_COIN_LIST", "").split() if coin.strip()
        ]
        # Get supported coin list from supported_coin_list file
        if not supported_coin_list and os.path.exists("supported_coin_list"):
            with open("supported_coin_list") as rfh:
                for line in rfh:
                    line = line.strip()
                    if not line or line.startswith("#") or line in supported_coin_list:
                        continue
                    supported_coin_list.append(line)
        self.SUPPORTED_COIN_LIST = supported_coin_list

        self.CURRENT_COIN_SYMBOL = os.environ.get("CURRENT_COIN_SYMBOL") or config.get(USER_CFG_SECTION, "current_coin")

        # Strategy: user.cfg file takes priority over env var (allows switching
        # strategies via persistent config without needing Coolify env changes)
        cfg_strategy = config.get(USER_CFG_SECTION, "strategy", fallback="default")
        self.STRATEGY = cfg_strategy if cfg_strategy and cfg_strategy != "default" else (os.environ.get("STRATEGY") or "default")

        self.SELL_TIMEOUT = os.environ.get("SELL_TIMEOUT") or config.get(USER_CFG_SECTION, "sell_timeout")
        self.BUY_TIMEOUT = os.environ.get("BUY_TIMEOUT") or config.get(USER_CFG_SECTION, "buy_timeout")

        self.USE_MARGIN = os.environ.get("USE_MARGIN") or config.get(USER_CFG_SECTION, "use_margin")
        self.SCOUT_MARGIN = float(os.environ.get("SCOUT_MARGIN") or config.get(USER_CFG_SECTION, "scout_margin"))

        # ── Improved strategy settings ─────────────────────────────────────
        # Phase 2: Rolling ratio baseline
        self.RATIO_SAMPLE_INTERVAL = int(
            os.environ.get("RATIO_SAMPLE_INTERVAL") or config.get(USER_CFG_SECTION, "ratio_sample_interval", fallback="10")
        )
        self.RATIO_SAMPLE_RETENTION_DAYS = int(
            os.environ.get("RATIO_SAMPLE_RETENTION_DAYS") or config.get(USER_CFG_SECTION, "ratio_sample_retention_days", fallback="7")
        )

        # Phase 3: Z-score threshold (trade when z-score exceeds this)
        self.Z_SCORE_THRESHOLD = float(
            os.environ.get("Z_SCORE_THRESHOLD") or config.get(USER_CFG_SECTION, "z_score_threshold", fallback="1.5")
        )

        # Phase 4: Momentum filter
        self.MOMENTUM_FILTER_ENABLED = (
            os.environ.get("MOMENTUM_FILTER_ENABLED") or config.get(USER_CFG_SECTION, "momentum_filter_enabled", fallback="yes")
        ).lower() in ("yes", "true", "1")
        self.MOMENTUM_MAX_DROP_1H = float(
            os.environ.get("MOMENTUM_MAX_DROP_1H") or config.get(USER_CFG_SECTION, "momentum_max_drop_1h", fallback="5.0")
        )

        # Phase 5: Market regime detection
        self.REGIME_CHECK_ENABLED = (
            os.environ.get("REGIME_CHECK_ENABLED") or config.get(USER_CFG_SECTION, "regime_check_enabled", fallback="yes")
        ).lower() in ("yes", "true", "1")
        self.REGIME_HIGH_VOL_THRESHOLD = float(
            os.environ.get("REGIME_HIGH_VOL_THRESHOLD") or config.get(USER_CFG_SECTION, "regime_high_vol_threshold", fallback="8.0")
        )
        self.REGIME_Z_SCORE_MULTIPLIER = float(
            os.environ.get("REGIME_Z_SCORE_MULTIPLIER") or config.get(USER_CFG_SECTION, "regime_z_score_multiplier", fallback="2.0")
        )

        # Phase 6: Trade cooldown + USDC profit-taking
        self.TRADE_COOLDOWN_SECONDS = int(
            os.environ.get("TRADE_COOLDOWN_SECONDS") or config.get(USER_CFG_SECTION, "trade_cooldown_seconds", fallback="300")
        )
        self.PROFIT_TAKING_ENABLED = (
            os.environ.get("PROFIT_TAKING_ENABLED") or config.get(USER_CFG_SECTION, "profit_taking_enabled", fallback="yes")
        ).lower() in ("yes", "true", "1")
        self.PROFIT_TAKING_INTERVAL = int(
            os.environ.get("PROFIT_TAKING_INTERVAL") or config.get(USER_CFG_SECTION, "profit_taking_interval", fallback="15")
        )

        # ── Adaptive multi-regime strategy settings ──────────────────────────
        # Phase A: Regime detection
        self.ADX_PERIOD = int(
            os.environ.get("ADX_PERIOD") or config.get(USER_CFG_SECTION, "adx_period", fallback="14")
        )
        self.ADX_TREND_THRESHOLD = float(
            os.environ.get("ADX_TREND_THRESHOLD") or config.get(USER_CFG_SECTION, "adx_trend_threshold", fallback="25")
        )
        self.EMA_SHORT = int(
            os.environ.get("EMA_SHORT") or config.get(USER_CFG_SECTION, "ema_short", fallback="20")
        )
        self.EMA_LONG = int(
            os.environ.get("EMA_LONG") or config.get(USER_CFG_SECTION, "ema_long", fallback="50")
        )
        self.REGIME_CHECK_INTERVAL = int(
            os.environ.get("REGIME_CHECK_INTERVAL") or config.get(USER_CFG_SECTION, "regime_check_interval", fallback="300")
        )
        self.REGIME_CONFIRMATION_CYCLES = int(
            os.environ.get("REGIME_CONFIRMATION_CYCLES") or config.get(USER_CFG_SECTION, "regime_confirmation_cycles", fallback="3")
        )

        # Phase B: Bull trend mode
        self.BULL_ZSCORE_MULT = float(
            os.environ.get("BULL_ZSCORE_MULT") or config.get(USER_CFG_SECTION, "bull_zscore_mult", fallback="0.67")
        )
        self.BULL_COOLDOWN = int(
            os.environ.get("BULL_COOLDOWN") or config.get(USER_CFG_SECTION, "bull_cooldown", fallback="900")
        )
        self.BULL_PROFIT_TAKE_INTERVAL = int(
            os.environ.get("BULL_PROFIT_TAKE_INTERVAL") or config.get(USER_CFG_SECTION, "bull_profit_take_interval", fallback="30")
        )

        # Phase C: Bear trend mode
        self.BEAR_ZSCORE_MULT = float(
            os.environ.get("BEAR_ZSCORE_MULT") or config.get(USER_CFG_SECTION, "bear_zscore_mult", fallback="2.0")
        )
        self.BEAR_COOLDOWN = int(
            os.environ.get("BEAR_COOLDOWN") or config.get(USER_CFG_SECTION, "bear_cooldown", fallback="1800")
        )
        self.BEAR_PROFIT_TAKE_INTERVAL = int(
            os.environ.get("BEAR_PROFIT_TAKE_INTERVAL") or config.get(USER_CFG_SECTION, "bear_profit_take_interval", fallback="5")
        )
        self.BEAR_MOMENTUM_MAX_DROP = float(
            os.environ.get("BEAR_MOMENTUM_MAX_DROP") or config.get(USER_CFG_SECTION, "bear_momentum_max_drop", fallback="2.0")
        )

        # Futures bear-mode controls
        self.FUTURES_LEVERAGE = int(
            os.environ.get("FUTURES_LEVERAGE") or config.get(USER_CFG_SECTION, "futures_leverage", fallback="1")
        )
        self.FUTURES_MAX_MARGIN_PCT = float(
            os.environ.get("FUTURES_MAX_MARGIN_PCT") or config.get(USER_CFG_SECTION, "futures_max_margin_pct", fallback="0.5")
        )
        self.FUTURES_MARGIN_TYPE = (
            os.environ.get("FUTURES_MARGIN_TYPE") or config.get(USER_CFG_SECTION, "futures_margin_type", fallback="CROSS")
        ).upper()
        self.FUTURES_STOP_LOSS_PCT = float(
            os.environ.get("FUTURES_STOP_LOSS_PCT") or config.get(USER_CFG_SECTION, "futures_stop_loss_pct", fallback="15.0")
        )
        self.FUTURES_TRAILING_STOP_PCT = float(
            os.environ.get("FUTURES_TRAILING_STOP_PCT") or config.get(USER_CFG_SECTION, "futures_trailing_stop_pct", fallback="10.0")
        )
        self.FUTURES_TRAILING_ACTIVATION_PCT = float(
            os.environ.get("FUTURES_TRAILING_ACTIVATION_PCT") or config.get(USER_CFG_SECTION, "futures_trailing_activation_pct", fallback="3.0")
        )
        self.FUTURES_SERVER_TRAILING_ENABLED = (
            os.environ.get("FUTURES_SERVER_TRAILING_ENABLED") or config.get(USER_CFG_SECTION, "futures_server_trailing_enabled", fallback="yes")
        ).lower() in ("yes", "true", "1")
        self.FUTURES_SERVER_TRAILING_CALLBACK_RATE = float(
            os.environ.get("FUTURES_SERVER_TRAILING_CALLBACK_RATE") or config.get(USER_CFG_SECTION, "futures_server_trailing_callback_rate", fallback="1.0")
        )
        self.FUTURES_SERVER_TRAILING_MIN_PROFIT_BUFFER_PCT = float(
            os.environ.get("FUTURES_SERVER_TRAILING_MIN_PROFIT_BUFFER_PCT") or config.get(USER_CFG_SECTION, "futures_server_trailing_min_profit_buffer_pct", fallback="0.5")
        )
        self.FUTURES_MAX_FUNDING_RATE = float(
            os.environ.get("FUTURES_MAX_FUNDING_RATE") or config.get(USER_CFG_SECTION, "futures_max_funding_rate", fallback="0.0001")
        )
        self.FUTURES_FUNDING_EXIT_MULTIPLIER = float(
            os.environ.get("FUTURES_FUNDING_EXIT_MULTIPLIER") or config.get(USER_CFG_SECTION, "futures_funding_exit_multiplier", fallback="3.0")
        )
        self.FUTURES_CHECK_INTERVAL = int(
            os.environ.get("FUTURES_CHECK_INTERVAL") or config.get(USER_CFG_SECTION, "futures_check_interval", fallback="60")
        )

        # Canary capital guard — conservative real-money rollout caps. Disabled by default.
        self.CANARY_MODE_ENABLED = (
            os.environ.get("CANARY_MODE_ENABLED") or config.get(USER_CFG_SECTION, "canary_mode_enabled", fallback="no")
        ).lower() in ("yes", "true", "1", "on")
        self.CANARY_MAX_SPOT_TRADE_USDC = float(
            os.environ.get("CANARY_MAX_SPOT_TRADE_USDC") or config.get(USER_CFG_SECTION, "canary_max_spot_trade_usdc", fallback="0")
        )
        self.CANARY_FUTURES_MAX_MARGIN_PCT = float(
            os.environ.get("CANARY_FUTURES_MAX_MARGIN_PCT") or config.get(USER_CFG_SECTION, "canary_futures_max_margin_pct", fallback="0")
        )
        self.CANARY_MAX_FUTURES_MARGIN_USDC = float(
            os.environ.get("CANARY_MAX_FUTURES_MARGIN_USDC") or config.get(USER_CFG_SECTION, "canary_max_futures_margin_usdc", fallback="0")
        )

        # Portfolio circuit breaker — disabled by default. When enabled, strategy
        # code may block new entries after daily/weekly equity drawdown limits
        # while still allowing exits and server-side protection to manage risk.
        self.PORTFOLIO_CIRCUIT_BREAKER_ENABLED = (
            os.environ.get("PORTFOLIO_CIRCUIT_BREAKER_ENABLED")
            or config.get(USER_CFG_SECTION, "portfolio_circuit_breaker_enabled", fallback="no")
        ).lower() in ("yes", "true", "1", "on")
        self.PORTFOLIO_DAILY_MAX_DRAWDOWN_PCT = float(
            os.environ.get("PORTFOLIO_DAILY_MAX_DRAWDOWN_PCT")
            or config.get(USER_CFG_SECTION, "portfolio_daily_max_drawdown_pct", fallback="5.0")
        )
        self.PORTFOLIO_WEEKLY_MAX_DRAWDOWN_PCT = float(
            os.environ.get("PORTFOLIO_WEEKLY_MAX_DRAWDOWN_PCT")
            or config.get(USER_CFG_SECTION, "portfolio_weekly_max_drawdown_pct", fallback="12.0")
        )
        self.PORTFOLIO_CIRCUIT_BREAKER_COOLDOWN_HOURS = float(
            os.environ.get("PORTFOLIO_CIRCUIT_BREAKER_COOLDOWN_HOURS")
            or config.get(USER_CFG_SECTION, "portfolio_circuit_breaker_cooldown_hours", fallback="24")
        )

        # Phase D: Trailing stop-loss
        self.TRAILING_STOP_ENABLED = (
            os.environ.get("TRAILING_STOP_ENABLED") or config.get(USER_CFG_SECTION, "trailing_stop_enabled", fallback="yes")
        ).lower() in ("yes", "true", "1")
        self.TRAILING_STOP_PCT = float(
            os.environ.get("TRAILING_STOP_PCT") or config.get(USER_CFG_SECTION, "trailing_stop_pct", fallback="8.0")
        )

        # ── ROI Optimization settings ─────────────────────────────────────────
        # Minimum profit threshold: don't trade unless expected gain exceeds this
        # Prevents fee-bleeding from marginal trades
        self.MIN_PROFIT_THRESHOLD = float(
            os.environ.get("MIN_PROFIT_THRESHOLD") or config.get(USER_CFG_SECTION, "min_profit_threshold", fallback="0.01")
        )

        # Anti-churn: don't re-buy a coin held within this many seconds
        self.CHURN_BLOCK_SECONDS = int(
            os.environ.get("CHURN_BLOCK_SECONDS") or config.get(USER_CFG_SECTION, "churn_block_seconds", fallback="14400")
        )

        # Confirmation delay: require N consecutive scout cycles confirming the same rotation
        self.CONFIRMATION_CYCLES = int(
            os.environ.get("CONFIRMATION_CYCLES") or config.get(USER_CFG_SECTION, "confirmation_cycles", fallback="3")
        )

        # RSI filter: skip buying overbought coins
        self.RSI_FILTER_ENABLED = (
            os.environ.get("RSI_FILTER_ENABLED") or config.get(USER_CFG_SECTION, "rsi_filter_enabled", fallback="yes")
        ).lower() in ("yes", "true", "1")
        self.RSI_OVERBOUGHT = float(
            os.environ.get("RSI_OVERBOUGHT") or config.get(USER_CFG_SECTION, "rsi_overbought", fallback="68")
        )

        # ── ROI Maximization features ─────────────────────────────────────────
        # Maker orders: place limit orders at bid/ask for 0.025% fee (vs 0.075% taker)
        self.USE_MAKER_ORDERS = (
            os.environ.get("USE_MAKER_ORDERS") or config.get(USER_CFG_SECTION, "use_maker_orders", fallback="yes")
        ).lower() in ("yes", "true", "1")

        # Dynamic position sizing: in bear mode, only trade a fraction of position
        self.DYNAMIC_POSITION_ENABLED = (
            os.environ.get("DYNAMIC_POSITION_ENABLED") or config.get(USER_CFG_SECTION, "dynamic_position_enabled", fallback="yes")
        ).lower() in ("yes", "true", "1")
        self.BEAR_POSITION_SIZE = float(
            os.environ.get("BEAR_POSITION_SIZE") or config.get(USER_CFG_SECTION, "bear_position_size", fallback="0.7")
        )
        self.SIDEWAYS_POSITION_SIZE = float(
            os.environ.get("SIDEWAYS_POSITION_SIZE") or config.get(USER_CFG_SECTION, "sideways_position_size", fallback="0.9")
        )

        # Bollinger Band squeeze detection
        self.BB_SQUEEZE_ENABLED = (
            os.environ.get("BB_SQUEEZE_ENABLED") or config.get(USER_CFG_SECTION, "bb_squeeze_enabled", fallback="yes")
        ).lower() in ("yes", "true", "1")
        self.BB_PERIOD = int(
            os.environ.get("BB_PERIOD") or config.get(USER_CFG_SECTION, "bb_period", fallback="20")
        )
        self.BB_SQUEEZE_LOOKBACK = int(
            os.environ.get("BB_SQUEEZE_LOOKBACK") or config.get(USER_CFG_SECTION, "bb_squeeze_lookback", fallback="50")
        )

        # Correlation-based coin selection
        self.CORRELATION_FILTER_ENABLED = (
            os.environ.get("CORRELATION_FILTER_ENABLED") or config.get(USER_CFG_SECTION, "correlation_filter_enabled", fallback="yes")
        ).lower() in ("yes", "true", "1")
        self.CORRELATION_THRESHOLD = float(
            os.environ.get("CORRELATION_THRESHOLD") or config.get(USER_CFG_SECTION, "correlation_threshold", fallback="0.85")
        )

        # BTC correlation for regime detection
        self.BTC_CORRELATION_ENABLED = (
            os.environ.get("BTC_CORRELATION_ENABLED") or config.get(USER_CFG_SECTION, "btc_correlation_enabled", fallback="yes")
        ).lower() in ("yes", "true", "1")

        # Maker reprice: reprice to taker after this many minutes if unfilled
        self.MAKER_REPRICE_TIMEOUT = float(
            os.environ.get("MAKER_REPRICE_TIMEOUT") or config.get(USER_CFG_SECTION, "maker_reprice_timeout", fallback="5")
        )

        # Indicator cache TTL (seconds) — cache RSI/correlation/BB results between scout cycles
        self.INDICATOR_CACHE_TTL = int(
            os.environ.get("INDICATOR_CACHE_TTL") or config.get(USER_CFG_SECTION, "indicator_cache_ttl", fallback="300")
        )

        # Spread detection: use midpoint pricing when USDC spread > threshold
        self.SPREAD_DETECTION_ENABLED = (
            os.environ.get("SPREAD_DETECTION_ENABLED") or config.get(USER_CFG_SECTION, "spread_detection_enabled", fallback="yes")
        ).lower() in ("yes", "true", "1")
        self.WIDE_SPREAD_THRESHOLD = float(
            os.environ.get("WIDE_SPREAD_THRESHOLD") or config.get(USER_CFG_SECTION, "wide_spread_threshold", fallback="0.15")
        )

        # ── Momentum Rotation Strategy settings (backtest-optimized) ──────────
        # Lookback window for performance measurement (in hours)
        self.MOMENTUM_LOOKBACK_HOURS = int(
            os.environ.get("MOMENTUM_LOOKBACK_HOURS") or config.get(USER_CFG_SECTION, "momentum_lookback_hours", fallback="18")
        )
        # Minimum outperformance edge required to trigger a trade (in %)
        self.MOMENTUM_MIN_EDGE = float(
            os.environ.get("MOMENTUM_MIN_EDGE") or config.get(USER_CFG_SECTION, "momentum_min_edge", fallback="8.0")
        )

        # Per-regime momentum parameters. Disabled by default until the
        # sensitivity + walk-forward validation gates pass.
        self.PER_REGIME_PARAMS_ENABLED = (
            os.environ.get("PER_REGIME_PARAMS_ENABLED")
            or config.get(USER_CFG_SECTION, "per_regime_params_enabled", fallback="no")
        ).lower() in ("yes", "true", "1", "on")
        self.BULL_MOMENTUM_LOOKBACK_HOURS = int(
            os.environ.get("BULL_MOMENTUM_LOOKBACK_HOURS")
            or config.get(USER_CFG_SECTION, "bull_momentum_lookback_hours", fallback="36")
        )
        self.BULL_MOMENTUM_MIN_EDGE = float(
            os.environ.get("BULL_MOMENTUM_MIN_EDGE")
            or config.get(USER_CFG_SECTION, "bull_momentum_min_edge", fallback="8.0")
        )
        self.SIDEWAYS_MOMENTUM_LOOKBACK_HOURS = int(
            os.environ.get("SIDEWAYS_MOMENTUM_LOOKBACK_HOURS")
            or config.get(USER_CFG_SECTION, "sideways_momentum_lookback_hours", fallback="18")
        )
        self.SIDEWAYS_MOMENTUM_MIN_EDGE = float(
            os.environ.get("SIDEWAYS_MOMENTUM_MIN_EDGE")
            or config.get(USER_CFG_SECTION, "sideways_momentum_min_edge", fallback="8.0")
        )
        self.BEAR_MOMENTUM_LOOKBACK_HOURS = int(
            os.environ.get("BEAR_MOMENTUM_LOOKBACK_HOURS")
            or config.get(USER_CFG_SECTION, "bear_momentum_lookback_hours", fallback="6")
        )
        self.BEAR_MOMENTUM_MIN_EDGE = float(
            os.environ.get("BEAR_MOMENTUM_MIN_EDGE")
            or config.get(USER_CFG_SECTION, "bear_momentum_min_edge", fallback="5.0")
        )
        self.STORMY_MOMENTUM_LOOKBACK_HOURS = int(
            os.environ.get("STORMY_MOMENTUM_LOOKBACK_HOURS")
            or config.get(USER_CFG_SECTION, "stormy_momentum_lookback_hours", fallback="6")
        )
        self.STORMY_MOMENTUM_MIN_EDGE = float(
            os.environ.get("STORMY_MOMENTUM_MIN_EDGE")
            or config.get(USER_CFG_SECTION, "stormy_momentum_min_edge", fallback="10.0")
        )

        # Time-based rotation confirmation. Cycle count alone can confirm a
        # multi-hour signal after only a few seconds when SCOUT_SLEEP_TIME=1.
        self.CONFIRMATION_TIME_ENABLED = (
            os.environ.get("CONFIRMATION_TIME_ENABLED")
            or config.get(USER_CFG_SECTION, "confirmation_time_enabled", fallback="no")
        ).lower() in ("yes", "true", "1", "on")
        self.CONFIRMATION_MIN_SECONDS = int(
            os.environ.get("CONFIRMATION_MIN_SECONDS")
            or config.get(USER_CFG_SECTION, "confirmation_min_seconds", fallback="180")
        )
        self.BULL_CONFIRMATION_MIN_SECONDS = int(
            os.environ.get("BULL_CONFIRMATION_MIN_SECONDS")
            or config.get(USER_CFG_SECTION, "bull_confirmation_min_seconds", fallback="300")
        )
        self.SIDEWAYS_CONFIRMATION_MIN_SECONDS = int(
            os.environ.get("SIDEWAYS_CONFIRMATION_MIN_SECONDS")
            or config.get(USER_CFG_SECTION, "sideways_confirmation_min_seconds", fallback="180")
        )
        self.BEAR_CONFIRMATION_MIN_SECONDS = int(
            os.environ.get("BEAR_CONFIRMATION_MIN_SECONDS")
            or config.get(USER_CFG_SECTION, "bear_confirmation_min_seconds", fallback="60")
        )
        self.STORMY_CONFIRMATION_MIN_SECONDS = int(
            os.environ.get("STORMY_CONFIRMATION_MIN_SECONDS")
            or config.get(USER_CFG_SECTION, "stormy_confirmation_min_seconds", fallback="300")
        )

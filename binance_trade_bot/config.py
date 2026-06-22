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

        self.STRATEGY = os.environ.get("STRATEGY") or config.get(USER_CFG_SECTION, "strategy")

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

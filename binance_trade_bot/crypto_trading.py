#!python3
import time

from .binance_api_manager import BinanceAPIManager
from .config import Config
from .database import Database
from .logger import Logger
from .scheduler import SafeScheduler
from .strategies import get_strategy


def main():
    logger = Logger()
    logger.info("Starting")

    config = Config()
    db = Database(logger, config)
    manager = BinanceAPIManager(config, db, logger, config.TESTNET)
    # check if we can access API feature that require valid config
    try:
        _ = manager.get_account()
    except Exception as e:  # pylint: disable=broad-except
        logger.error("Couldn't access Binance API - API keys may be wrong or lack sufficient permissions")
        logger.error(e)
        return
    strategy = get_strategy(config.STRATEGY)
    if strategy is None:
        logger.error("Invalid strategy name")
        return
    trader = strategy(manager, db, logger, config)
    logger.info(f"Chosen strategy: {config.STRATEGY}")

    logger.info("Creating database schema if it doesn't already exist")
    db.create_database()

    db.set_coins(config.SUPPORTED_COIN_LIST)
    db.migrate_old_state()

    trader.initialize()

    schedule = SafeScheduler(logger)
    schedule.every(config.SCOUT_SLEEP_TIME).seconds.do(trader.scout).tag("scouting")
    schedule.every(config.SCOUT_SLEEP_TIME).seconds.do(trader.bridge_scout).tag("bridge scouting")
    schedule.every(1).minutes.do(trader.update_values).tag("updating value history")
    schedule.every(1).minutes.do(db.prune_scout_history).tag("pruning scout history")
    schedule.every(1).hours.do(db.prune_value_history).tag("pruning value history")

    # Phase 2/3: Rolling ratio statistics
    sample_interval = getattr(config, "RATIO_SAMPLE_INTERVAL", 10)
    schedule.every(sample_interval).minutes.do(db.sample_ratios, manager).tag("sampling ratios")
    schedule.every(sample_interval).minutes.do(db.update_pair_stats).tag("updating pair stats")
    schedule.every(6).hours.do(db.prune_ratio_samples).tag("pruning ratio samples")

    logger.info(
        f"Improved strategy active | "
        f"Z-score threshold: {getattr(config, 'Z_SCORE_THRESHOLD', 1.5)} | "
        f"Cooldown: {getattr(config, 'TRADE_COOLDOWN_SECONDS', 300)}s | "
        f"Ratio sampling: every {sample_interval}min"
    )

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    finally:
        manager.stream_manager.close()
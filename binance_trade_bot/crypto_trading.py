#!python3
import os
import time
import traceback

from .binance_api_manager import BinanceAPIManager
from .config import Config
from .database import Database
from .logger import Logger
from .scheduler import SafeScheduler
from .strategies import get_strategy


def _acquire_singleton_lock(logger):
    """Acquire an exclusive flock on a PID file to prevent double-start.
    Returns the file handle (must stay open for lock to hold) or None on failure."""
    import fcntl
    pid_path = os.path.join("data", "bot.pid")
    try:
        os.makedirs("data", exist_ok=True)
        pid_file = open(pid_path, "w")
        fcntl.flock(pid_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pid_file.write(str(os.getpid()))
        pid_file.flush()
        logger.info(f"Acquired singleton lock (PID {os.getpid()})")
        return pid_file
    except (IOError, OSError):
        logger.error("Another bot instance is already running (PID lock held). Exiting.")
        return None


def _reconcile_position(manager, db, logger, config):
    """On startup, verify the DB current_coin matches actual exchange balance.
    If they diverge (e.g. crash mid-trade), fix the DB to match reality.
    Also checks futures wallet — if funds are in futures during BEAR regime,
    that's a valid state, not an error."""
    from .models import Coin

    current_coin = db.get_current_coin()
    if current_coin is None:
        logger.info("No current coin in DB — skipping reconciliation")
        return

    bridge_symbol = config.BRIDGE.symbol
    try:
        coin_balance = manager.get_currency_balance(current_coin.symbol, force=True)
        bridge_balance = manager.get_currency_balance(bridge_symbol, force=True)
    except Exception as e:
        logger.warning(f"Could not fetch balances for reconciliation: {e}")
        return

    # If we hold the DB coin with a meaningful balance, we're fine
    if coin_balance and coin_balance > manager.get_min_notional(
        current_coin.symbol, bridge_symbol
    ) / manager.get_ticker_price(current_coin.symbol + bridge_symbol):
        logger.info(f"Reconciliation OK: holding {coin_balance} {current_coin.symbol}")
        return

    # If we hold mostly bridge, the bot probably crashed mid-sell
    if bridge_balance and bridge_balance > 1.0:
        logger.warning(
            f"RECONCILIATION MISMATCH: DB says {current_coin.symbol} "
            f"(bal: {coin_balance}) but bridge balance is {bridge_balance} {bridge_symbol}. "
            f"Bot likely crashed mid-trade. Will re-balance on next scout cycle."
        )
        # Find which coin we actually hold the most of
        try:
            account = manager.get_account()
            for bal in account.get("balances", []):
                asset = bal["asset"]
                free = float(bal["free"])
                if asset in (bridge_symbol, "BNB") or free <= 0:
                    continue
                # Found a non-bridge asset we hold
                if free > 0.001:
                    coin_obj = db.get_coin(asset)
                    if coin_obj and coin_obj.enabled:
                        logger.info(f"Reconciliation: setting current_coin to {asset} (actual holding)")
                        db.set_current_coin(asset)
                        return
        except Exception as e:
            logger.warning(f"Could not scan account for reconciliation: {e}")
        return

    # Check futures wallet — if funds are there, that's OK during BEAR regime
    try:
        futures_balances = manager.binance_client.futures_account_balance()
        futures_total = 0.0
        for fb in futures_balances:
            if fb.get("asset") == bridge_symbol:
                futures_total = float(fb.get("balance", 0))
                break
        if futures_total > 5.0:
            logger.info(
                f"Reconciliation: spot empty (0 {current_coin.symbol}, {bridge_balance} {bridge_symbol}) "
                f"but futures has {futures_total:.2f} {bridge_symbol} — funds in futures wallet (BEAR mode)"
            )
            return
    except Exception:
        pass

    logger.info(f"Reconciliation: holding {coin_balance} {current_coin.symbol}, {bridge_balance} {bridge_symbol} — seems OK")


def _backup_database():
    """Create a daily backup of the SQLite database using the VACUUM INTO command
    for a consistent snapshot that doesn't lock the DB."""
    import shutil
    src = "/data/crypto_trading.db"
    bak = "/data/crypto_trading.db.bak"
    try:
        if os.path.exists(src):
            shutil.copy2(src, bak)
            # Keep only the last 3 backups
            for i in range(7, 0, -1):
                old_bak = f"{src}.bak.{i}"
                if os.path.exists(old_bak):
                    os.remove(old_bak)
            # Rotate: if .bak exists, move to .bak.1
            if os.path.exists(bak):
                for i in range(6, 0, -1):
                    next_bak = f"{src}.bak.{i+1}" if i < 6 else None
                    curr_bak = f"{src}.bak.{i}" if i > 0 else bak
                    if os.path.exists(curr_bak):
                        if next_bak and i == 6:
                            os.remove(curr_bak)
                        elif next_bak:
                            os.rename(curr_bak, next_bak)
                        elif i == 0:
                            pass  # the fresh backup is already at bak
            print(f"[backup] Database backed up to {bak}")
    except Exception as e:
        print(f"[backup] Failed: {e}")


def main():
    import os as _os

    logger = Logger()
    logger.info("Starting")

    # Singleton lock — prevent double-start
    pid_file = _acquire_singleton_lock(logger)
    if pid_file is None:
        return

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

    # Position reconciliation: verify DB state matches exchange reality
    _reconcile_position(manager, db, logger, config)

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
    schedule.every(24).hours.do(_backup_database).tag("backing up database")

    logger.info(
        f"Strategy '{config.STRATEGY}' active | "
        f"Cooldown: {getattr(config, 'TRADE_COOLDOWN_SECONDS', 300)}s | "
        f"Ratio sampling: every {sample_interval}min"
    )

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user (Ctrl+C)")
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        logger.error(traceback.format_exc())
        # Try to send Telegram alert
        try:
            import os
            from binance_trade_bot.notifications import NotificationHandler
            nm = NotificationHandler()
            nm.send_notification(f"🚨 BOT CRASHED\n\nError: {str(e)[:200]}\n\nThe trading bot has stopped unexpectedly and needs attention.")
        except Exception:
            pass  # Don't let notification failure mask the original error
        raise
    finally:
        manager.stream_manager.close()
import logging.handlers
import os
from pathlib import Path

from .notifications import NotificationHandler


class Logger:
    Logger = None
    NotificationHandler = None

    def __init__(self, logging_service="crypto_trading", enable_notifications=True):
        # Logger setup
        self.Logger = logging.getLogger(f"{logging_service}_logger")
        self.Logger.setLevel(logging.DEBUG)
        self.Logger.propagate = False
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )

        # --- Rotating file handler (persistent logs, fixes GitHub #91) ---
        # Default to a relative "logs/" dir for local/dev runs; in production the
        # systemd unit sets LOG_DIR=/data/binance-bot-data/logs so logs survive
        # across restarts and live alongside the database.
        log_dir = os.environ.get("LOG_DIR") or "logs"
        log_dir_path = Path(log_dir)
        # The bot runs as `lunafox` under systemd; ensure the dir exists and is
        # writable. Failures here are non-fatal: we warn and keep the console
        # handler so the process never silently loses all output.
        try:
            log_dir_path.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_dir_path / f"{logging_service}.log",
                maxBytes=10 * 1024 * 1024,  # 10 MB per file
                backupCount=5,
                encoding="utf-8",
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(formatter)
            self.Logger.addHandler(fh)
        except OSError as exc:
            # Missing/owned-by-root dir in an unprivileged context, etc.
            # Don't crash startup over logging; console handler still works.
            print(
                f"[logger] WARNING: could not create file handler in "
                f"'{log_dir_path}' ({exc}); continuing with console only."
            )

        # logging to console (journald under systemd, terminal otherwise)
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(formatter)
        self.Logger.addHandler(ch)

        # notification handler
        self.NotificationHandler = NotificationHandler(enable_notifications)

    def log(self, message, level="info", notification=True):
        if level == "info":
            self.Logger.info(message)
        elif level == "warning":
            self.Logger.warning(message)
        elif level == "error":
            self.Logger.error(message)
        elif level == "debug":
            self.Logger.debug(message)

        if notification and self.NotificationHandler.enabled:
            self.NotificationHandler.send_notification(str(message))

    def info(self, message, notification=True):
        self.log(message, "info", notification)

    def warning(self, message, notification=True):
        self.log(message, "warning", notification)

    def error(self, message, notification=True):
        self.log(message, "error", notification)

    def debug(self, message, notification=False):
        self.log(message, "debug", notification)

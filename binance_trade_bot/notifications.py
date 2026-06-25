import logging
import os
import queue
import threading
import time
from collections import deque
from os import path

import apprise

APPRISE_CONFIG_PATHS = [
    "config/apprise.yml",
    "data/apprise.yml",
]

log = logging.getLogger(__name__)


class NotificationHandler:
    def __init__(self, enabled=True):
        self.enabled = False
        self.apobj = apprise.Apprise()
        self._recent_messages = {}
        self._sent_timestamps = deque()
        self._last_rate_limit_log = 0.0
        self.dedup_seconds = float(os.environ.get("NOTIFICATION_DEDUP_SECONDS", "300"))
        self.max_per_minute = int(os.environ.get("NOTIFICATION_MAX_PER_MINUTE", "12"))

        # Try apprise.yml config file(s) first
        if enabled:
            for cfg_path in APPRISE_CONFIG_PATHS:
                if path.exists(cfg_path):
                    config = apprise.AppriseConfig()
                    config.add(cfg_path)
                    self.apobj.add(config)
                    self.enabled = True
                    break
        # Fall back to APPRISE_URLS env var (supports multiple, space-separated)
        if not self.enabled and enabled and os.environ.get("APPRISE_URLS"):
            for url in os.environ["APPRISE_URLS"].split():
                self.apobj.add(url)
            self.enabled = True

        if self.enabled:
            self.queue = queue.Queue()
            self.start_worker()

    def start_worker(self):
        threading.Thread(target=self.process_queue, daemon=True).start()

    def process_queue(self):
        while True:
            message, attachments = self.queue.get()
            try:
                if attachments:
                    result = self.apobj.notify(body=message, attach=attachments)
                else:
                    result = self.apobj.notify(body=message)
                if not result:
                    log.warning("Notification delivery returned False")
            except Exception as e:
                # Never let a single Apprise/Telegram error kill the daemon
                # worker thread; otherwise future alerts silently stop.
                log.exception("Notification delivery failed: %s", e)
            finally:
                self.queue.task_done()

    def send_notification(self, message, attachments=None):
        if not self.enabled:
            return

        now = time.time()
        message = str(message)

        # Drop exact duplicate notifications for a short window. This prevents
        # routine scout/filter logs from turning into Telegram floods while
        # still letting the first occurrence through for visibility.
        last_seen = self._recent_messages.get(message)
        if last_seen is not None and now - last_seen < self.dedup_seconds:
            return
        self._recent_messages[message] = now

        # Keep the dedupe map bounded.
        cutoff = now - self.dedup_seconds
        for old_message, seen_at in list(self._recent_messages.items()):
            if seen_at < cutoff:
                del self._recent_messages[old_message]

        # Global rate cap as a belt-and-suspenders guard against future unique
        # message loops. Trade/error bursts still get through, but floods stop.
        while self._sent_timestamps and now - self._sent_timestamps[0] >= 60:
            self._sent_timestamps.popleft()
        if self.max_per_minute > 0 and len(self._sent_timestamps) >= self.max_per_minute:
            if now - self._last_rate_limit_log >= 60:
                log.warning("Notification rate limit reached; suppressing Telegram flood")
                self._last_rate_limit_log = now
            return

        self._sent_timestamps.append(now)
        self.queue.put((message, attachments or []))

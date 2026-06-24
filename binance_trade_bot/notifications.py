import os
import queue
import threading
from os import path

import apprise

APPRISE_CONFIG_PATHS = [
    "config/apprise.yml",
    "data/apprise.yml",
]


class NotificationHandler:
    def __init__(self, enabled=True):
        self.enabled = False
        self.apobj = apprise.Apprise()

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

            if attachments:
                self.apobj.notify(body=message, attach=attachments)
            else:
                self.apobj.notify(body=message)
            self.queue.task_done()

    def send_notification(self, message, attachments=None):
        if self.enabled:
            self.queue.put((message, attachments or []))

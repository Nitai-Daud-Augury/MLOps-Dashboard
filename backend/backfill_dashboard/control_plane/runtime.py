from __future__ import annotations

import threading
import logging
import random


LOGGER = logging.getLogger(__name__)


class ControlPlaneRuntime:
    def __init__(self, synchronizer, dispatcher=None, sync_seconds: int = 600, dispatch_seconds: int = 3) -> None:
        self.synchronizer, self.dispatcher = synchronizer, dispatcher
        self.sync_seconds, self.dispatch_seconds = sync_seconds, dispatch_seconds
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        if self.threads:
            return
        self.threads.append(threading.Thread(target=self._sync_loop, name="inventory-sync", daemon=True))
        if self.dispatcher:
            self.threads.append(threading.Thread(target=self._dispatch_loop, name="campaign-dispatch", daemon=True))
        for thread in self.threads:
            thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=5)
        self.threads.clear()

    def _sync_loop(self) -> None:
        while not self.stop_event.is_set():
            self.synchronizer.sync()
            self.stop_event.wait(self.sync_seconds)

    def _dispatch_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.dispatcher.tick()
                self.dispatcher.reconcile()
            except Exception:
                LOGGER.exception("campaign dispatcher iteration failed")
            self.stop_event.wait(self.dispatch_seconds * random.uniform(.85, 1.15))

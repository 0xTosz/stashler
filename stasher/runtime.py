"""Background worker + shared status for the UI.

Owns the capture threads (live websocket + on-demand backfill) and a thread-safe
status snapshot the Flask UI polls. Both capture paths share one :class:`Pipeline`
and one :class:`RateLimiter`, so a manual backfill triggered from the UI can never
double-spend the rate budget against an active live session.
"""

from __future__ import annotations

import threading
import time

from .backfill import run_backfill
from .client import TradeClient
from .config import Config
from .live import run_live
from .pipeline import Pipeline
from .ratelimit import RateLimiter
from .store import Store


class Worker:
    def __init__(self, config: Config, store: Store, limiter: RateLimiter, client: TradeClient):
        self.config = config
        self.store = store
        self.limiter = limiter
        self.client = client
        self.pipeline = Pipeline(client, store, on_stored=self._on_stored)

        self._lock = threading.Lock()
        self._session_new = 0
        self._live_state = "idle"  # idle | live | reconnecting
        self._live_connected = False
        self._last_error: str | None = None
        self._last_push: int | None = None
        self._backfill_running = False
        self._backfill_paused = False
        self._backfill_label: str | None = None
        self._backfill_partitions = 0

        self._stop = threading.Event()
        self._live_stop = threading.Event()
        self._bf_stop = threading.Event()
        self._live_thread: threading.Thread | None = None
        self._bf_thread: threading.Thread | None = None

    # --- live -----------------------------------------------------------

    def start_live(self) -> bool:
        with self._lock:
            if self._live_thread and self._live_thread.is_alive():
                return False
            self._live_stop.clear()
            self._live_thread = threading.Thread(
                target=self._live_run, name="stasher-live", daemon=True
            )
            self._live_thread.start()
            return True

    def stop_live(self) -> None:
        self._live_stop.set()
        thread = self._live_thread
        if thread:
            thread.join(timeout=10)

    def _live_run(self) -> None:
        run_live(self.client, self.pipeline, self._live_stop, status=self._live_status)

    def _live_status(self, **kw) -> None:
        with self._lock:
            if "mode" in kw:
                self._live_state = kw["mode"]
            if "live_connected" in kw:
                self._live_connected = kw["live_connected"]
            if kw.get("last_error"):
                self._last_error = kw["last_error"]
            if "last_push" in kw:
                self._last_push = kw["last_push"]

    # --- backfill -------------------------------------------------------

    def start_backfill(self) -> bool:
        with self._lock:
            if self._bf_thread and self._bf_thread.is_alive():
                return False
            self._bf_stop.clear()
            self._backfill_running = True
            self._backfill_paused = False
            self._backfill_label = "starting"
            self._backfill_partitions = 0
            self._bf_thread = threading.Thread(
                target=self._bf_run, name="stasher-backfill", daemon=True
            )
            self._bf_thread.start()
            return True

    def stop_backfill(self) -> None:
        self._bf_stop.set()
        with self._lock:
            self._backfill_paused = False

    def pause_backfill(self) -> bool:
        with self._lock:
            if not self._backfill_running:
                return False
            self._backfill_paused = True
            return True

    def resume_backfill(self) -> bool:
        with self._lock:
            if not self._backfill_running:
                return False
            self._backfill_paused = False
            return True

    def backfill_running(self) -> bool:
        with self._lock:
            return self._backfill_running

    def _bf_gate(self) -> bool:
        """should_stop callback that also blocks (parks) while paused, so backfill
        resumes from exactly where it stopped instead of re-enumerating."""
        while True:
            if self._stop.is_set() or self._bf_stop.is_set():
                return True
            with self._lock:
                paused = self._backfill_paused
            if not paused:
                return False
            time.sleep(0.2)

    def _bf_run(self) -> None:
        try:
            run_backfill(
                self.client,
                self.store,
                self.pipeline,
                progress=self._bf_progress,
                should_stop=self._bf_gate,
            )
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._last_error = str(exc)[:160]
        finally:
            with self._lock:
                self._backfill_running = False
                self._backfill_label = None

    def _bf_progress(self, label: str, partitions: int, new: int) -> None:
        with self._lock:
            self._backfill_label = label
            self._backfill_partitions = partitions

    # --- shared ---------------------------------------------------------

    def _on_stored(self, n: int) -> None:
        with self._lock:
            self._session_new += n

    def set_rate_mode(self, mode: str) -> str:
        applied = self.limiter.set_mode(mode)
        self.store.set_setting("rate_mode", applied)
        return applied

    def stop(self) -> None:
        self._stop.set()
        self._bf_stop.set()
        self.stop_live()

    def status(self) -> dict:
        with self._lock:
            if self._backfill_running and self._backfill_paused:
                mode = "backfill: paused"
            elif self._backfill_running:
                mode = f"backfill: {self._backfill_label}"
            elif self._live_connected:
                mode = "live"
            elif self._live_state == "reconnecting":
                mode = "reconnecting"
            else:
                mode = "idle"
            base = {
                "mode": mode,
                "live_connected": self._live_connected,
                "backfill_running": self._backfill_running,
                "backfill_paused": self._backfill_paused,
                "backfill_label": self._backfill_label,
                "backfill_partitions": self._backfill_partitions,
                "session_new": self._session_new,
                "last_error": self._last_error,
                "last_push": self._last_push,
            }
        base["items_total"] = self.store.count_items()
        base["rate_mode"] = self.limiter.mode
        base["rate_limits"] = self.limiter.snapshot()
        base["recent_queries"] = [dict(r) for r in self.store.recent_queries(40)]
        return base

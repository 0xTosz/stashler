"""Background worker + shared status for the UI.

Owns the capture threads (live websocket + on-demand backfill) and a thread-safe
status snapshot the Flask UI polls. Both capture paths share one :class:`Pipeline`
and one :class:`RateLimiter`, so a manual backfill triggered from the UI can never
double-spend the rate budget against an active live session.
"""

from __future__ import annotations

import threading
import time

from .backfill import poll_indicates_overflow, run_backfill, run_light_poll
from .client import TradeClient
from .config import Config
from .live import probe_live, run_live
from .pipeline import Pipeline
from .ratelimit import RateLimiter
from .store import Store, utc_now_iso


class Worker:
    def __init__(
        self,
        config: Config,
        store: Store,
        limiter: RateLimiter,
        client: TradeClient,
        evaluator=None,
    ):
        self.config = config
        self.store = store
        self.limiter = limiter
        self.client = client
        self.pipeline = Pipeline(
            client, store, on_stored=self._on_stored, evaluator=evaluator
        )

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

        # Auto-refresh loop: periodic light poll + occasional full backfill.
        self._auto_stop = threading.Event()
        self._auto_thread: threading.Thread | None = None
        self._auto_last_poll: str | None = None
        self._auto_last_full: float = 0.0
        self._auto_last_result: str | None = None

    # --- live (websocket) -----------------------------------------------
    # NOT WIRED INTO THE UI. PoE2 delivers live-search notifications as an encrypted
    # JWT payload (the {"result": ...} frame's `d` field; the official web client
    # decrypts it client-side), so a third-party tool can't read the new-item ids from
    # the socket. These methods connect and authenticate fine but can't capture, so the
    # UI uses Auto-refresh (light poll + full backfill) instead. Kept for reference /
    # the CLI `live` command and in case the protocol opens up again.

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

    def test_live(self) -> dict:
        """One-shot diagnostic: probe the live socket and report the outcome."""
        with self._lock:
            running = bool(self._live_thread and self._live_thread.is_alive())
        if running:
            return {
                "ok": False,
                "stage": "open",
                "detail": "Stop live capture first — a second live search on the "
                "same session is itself rejected (1008).",
            }
        return probe_live(self.client)

    # --- auto-refresh (light poll + periodic full backfill) -------------

    def start_auto(self) -> bool:
        with self._lock:
            if self._auto_thread and self._auto_thread.is_alive():
                return False
            self._auto_stop.clear()
            self._auto_thread = threading.Thread(
                target=self._auto_run, name="stasher-auto", daemon=True
            )
            self._auto_thread.start()
        self.store.set_setting("auto_mode", "on")
        return True

    def stop_auto(self) -> None:
        """Request a graceful stop. Returns promptly; a mid-backfill loop keeps winding
        down in the background and is reported as the ``stopping`` state until it exits."""
        self.store.set_setting("auto_mode", "off")
        self._auto_stop.set()
        thread = self._auto_thread
        if thread:
            thread.join(timeout=2)  # the idle between-poll case exits in well under 1s

    def auto_running(self) -> bool:
        with self._lock:
            return bool(self._auto_thread and self._auto_thread.is_alive())

    def auto_state(self) -> str:
        """``off`` | ``on`` | ``stopping`` -- drives the single UI toggle."""
        with self._lock:
            alive = bool(self._auto_thread and self._auto_thread.is_alive())
        if not alive:
            return "off"
        return "stopping" if self._auto_stop.is_set() else "on"

    def _auto_run(self) -> None:
        poll_iv = max(30.0, self.config.auto_poll_interval)
        # Optional timed full-backfill safety net (0 disables it). We always lead with a
        # light poll -- the archive is normally already seeded, and a cold start is seeded
        # by the poll itself (overflow detection pulls a full backfill when needed).
        full_iv = self.config.auto_full_interval
        self._auto_last_full = time.monotonic()  # don't fire the timer on the first tick
        while not self._auto_gate():
            try:
                timed = full_iv > 0 and (time.monotonic() - self._auto_last_full) >= full_iv
                if timed:
                    summary = self.backfill_blocking()
                    self._auto_last_full = time.monotonic()
                    result = f"full backfill: {summary['new']} new"
                else:
                    res = run_light_poll(self.client, self.store, self.pipeline)
                    result = f"poll: {res['new']} new of {res['listed']} listed"
                    if poll_indicates_overflow(res) and not self._auto_gate():
                        # The whole newest page was new and more listings exist beyond it
                        # -> re-sync now so nothing that fell past page 1 is missed (this
                        # also seeds a fresh archive that has more than one page of items).
                        summary = self.backfill_blocking()
                        self._auto_last_full = time.monotonic()
                        result += f" · overflow → full backfill: {summary['new']} new"
                with self._lock:
                    self._auto_last_poll = utc_now_iso()
                    self._auto_last_result = result
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                with self._lock:
                    self._last_error = str(exc)[:160]
                    self._auto_last_result = f"error: {str(exc)[:80]}"
            # Sleep the poll interval in short slices so stop is responsive.
            waited = 0.0
            while waited < poll_iv and not self._auto_gate():
                time.sleep(0.5)
                waited += 0.5

    def force_resync(self) -> bool:
        """Drop the archived items + evaluations and re-fetch everything from scratch.

        Clears the dedup set so cleared hashes are fetched again, then runs a full
        backfill in the background (records repopulate + re-evaluate as it goes).
        Returns False if a backfill is already running. Settings are kept.
        """
        with self._lock:
            if self._bf_thread and self._bf_thread.is_alive():
                return False
        self.store.clear_archive()
        self.pipeline.reset()
        return self.start_backfill()

    def backfill_blocking(self) -> dict:
        """Run a full backfill synchronously (used by the auto loop)."""
        return run_backfill(self.client, self.store, self.pipeline, should_stop=self._auto_gate)

    def _auto_gate(self) -> bool:
        return self._stop.is_set() or self._auto_stop.is_set()

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
        self._auto_stop.set()
        self.stop_live()

    def status(self) -> dict:
        with self._lock:
            auto_alive = bool(self._auto_thread and self._auto_thread.is_alive())
            auto_stopping = auto_alive and self._auto_stop.is_set()
            if self._backfill_running and self._backfill_paused:
                mode = "backfill: paused"
            elif self._backfill_running:
                mode = f"backfill: {self._backfill_label}"
            elif self._live_connected:
                mode = "live"
            elif self._live_state == "reconnecting":
                mode = "reconnecting"
            elif auto_stopping:
                mode = "auto: stopping"
            elif auto_alive:
                mode = "auto-refresh"
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
                "auto_last_poll": self._auto_last_poll,
                "auto_last_result": self._auto_last_result,
            }
        base["auto_running"] = auto_alive
        base["auto_state"] = "stopping" if auto_stopping else ("on" if auto_alive else "off")
        creds = self.client.creds()
        base["setup_ok"] = bool(creds.account and creds.poesessid)
        base["items_total"] = self.store.count_items()
        base["queue_unseen"] = self.store.count_unseen()
        base["rate_mode"] = self.limiter.mode
        base["rate_limits"] = self.limiter.snapshot()
        base["recent_queries"] = [dict(r) for r in self.store.recent_queries(40)]
        return base

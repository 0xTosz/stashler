"""stasher -- append-only archive of your own PoE2 trade listings.

Public entry point is :class:`Stasher`. Built to be embedded in a larger Python tool:
construct it, call :meth:`backfill` / :meth:`run_live`, and read the SQLite file at
:attr:`db_path` directly.
"""

from __future__ import annotations

import threading
from typing import Callable

from .backfill import run_backfill
from .client import TradeClient
from .config import Config, __version__
from .evaluate import Evaluator
from .live import run_live
from .pipeline import Pipeline
from .ratelimit import RateLimiter
from .runtime import Worker
from .store import Store

__all__ = ["Stasher", "Config", "Worker", "__version__"]


class Stasher:
    def __init__(self, config: Config):
        self.config = config
        self.store = Store(config.db_path)
        self._seed_settings()
        self.limiter = RateLimiter(
            self.store,
            config.rate_limit_margin,
            config.fallback_buckets,
            mode=self.store.get_setting("rate_mode", config.rate_mode) or "full",
            restrictive_fraction=config.restrictive_fraction,
        )
        self.client = TradeClient(config, self.store, self.limiter)
        self.evaluator = Evaluator(self.store, config.rules_path)
        self.pipeline = Pipeline(self.client, self.store, evaluator=self.evaluator)
        self._worker: Worker | None = None

    @classmethod
    def from_config(cls, path: str | None = None, **overrides) -> "Stasher":
        return cls(Config.load(path, **overrides))

    @property
    def db_path(self) -> str:
        return self.store.db_path

    def _seed_settings(self) -> None:
        seeds = {
            "account_name": self.config.account_name,
            "poesessid": self.config.poesessid,
            "league": self.config.league,
            "rate_mode": self.config.rate_mode,
        }
        for key, value in seeds.items():
            if value and self.store.get_setting(key) is None:
                self.store.set_setting(key, value)

    # --- capture --------------------------------------------------------

    def backfill(
        self,
        progress: Callable[[str, int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict:
        return run_backfill(self.client, self.store, self.pipeline, progress, should_stop)

    def run_live(
        self,
        stop_event: threading.Event | None = None,
        status: Callable[..., None] | None = None,
    ) -> None:
        run_live(self.client, self.pipeline, stop_event or threading.Event(), status)

    def run(self, stop_event: threading.Event | None = None) -> None:
        self.backfill()
        self.run_live(stop_event)

    # --- evaluation -----------------------------------------------------

    def reevaluate_all(
        self,
        progress: Callable[[int, int], None] | None = None,
        force: bool = False,
    ) -> dict:
        """Re-run the rule checkers over stored items. Returns a summary dict."""
        return self.evaluator.reevaluate_all(progress, force)

    def worker(self) -> Worker:
        if self._worker is None:
            self._worker = Worker(
                self.config, self.store, self.limiter, self.client, self.evaluator
            )
        return self._worker

    # --- lifecycle ------------------------------------------------------

    def close(self) -> None:
        if self._worker is not None:
            self._worker.stop()
        self.client.close()
        self.store.close()

    def __enter__(self) -> "Stasher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

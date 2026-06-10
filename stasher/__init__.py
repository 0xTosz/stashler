"""stasher -- append-only archive of your own PoE2 trade listings.

Public entry point is :class:`Stasher`. Built to be embedded in a larger Python tool:
construct it, call :meth:`backfill` / :meth:`run_live`, and read the SQLite file at
:attr:`db_path` directly.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from .backfill import run_backfill
from .client import TradeClient
from .config import Config, __version__
from .evaluate import Evaluator
from .evaluate.rules import install_archetype_set, seed_user_rules
from .live import run_live
from .pipeline import Pipeline
from .ratelimit import RateLimiter
from .runtime import Worker
from .store import Store

__all__ = ["Stasher", "Config", "Worker", "__version__"]


class Stasher:
    def __init__(self, config: Config):
        self.config = config
        # Make sure the storage directory exists before opening the DB (it may be a
        # fresh per-user data dir on first run, esp. for the packaged app).
        Path(config.db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
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
        # Seed starter rules + example filter into the writable location on first run
        # (so the packaged app ships with a working item filter, not an empty editor).
        seed_user_rules(config.rules_path, config.data_dir)
        # Install/refresh the packaged graded archetype set (fresh install, or replace an
        # outdated set — backing the old one up). Before the Evaluator so it loads it.
        set_change = install_archetype_set(config.rules_path, config.data_dir, self.store)
        self.evaluator = Evaluator(self.store, config.rules_path, config.data_dir)
        self.pipeline = Pipeline(self.client, self.store, evaluator=self.evaluator)
        self._worker: Worker | None = None
        self._pricing = None
        # A set upgrade changes the rules hash, so the stored archive is now stale. Refresh it in
        # place so new grades (e.g. jewels) show on open without a manual Re-evaluate. force=False
        # only touches items whose evaluation predates the new set; a fresh install has none.
        if set_change:
            try:
                self.evaluator.reevaluate_all()
            except Exception:  # never let a refresh failure block startup
                pass

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

    def pricing(self):
        """On-demand price-appraisal service (lazy). Shares the one TradeClient/RateLimiter,
        so price checks spend the same rate budget as capture — never a second path. Idle
        until an item is enqueued via the UI; refuses live searches until the Phase-0 stat
        data is harvested (see stasher.pricing.appraise.data_ready)."""
        if self._pricing is None:
            from .pricing.appraise import PricingService
            from .pricing.pricer import TradeClientSource

            self._pricing = PricingService(
                self.store,
                TradeClientSource(self.client),
                league_getter=lambda: self.store.get_setting("league", self.config.league)
                or self.config.league,
            )
        return self._pricing

    # --- lifecycle ------------------------------------------------------

    def close(self) -> None:
        if self._worker is not None:
            self._worker.stop()
        if self._pricing is not None:
            self._pricing.stop()
        self.client.close()
        self.store.close()

    def __enter__(self) -> "Stasher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

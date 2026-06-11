"""The fetch pipeline shared by backfill and live capture.

Takes item hashes, drops the ones already archived (or already in flight), fetches the
rest in batches of 10, and append-only inserts them. Known hashes cost nothing, which
is what makes the whole archive cheap to keep up to date.
"""

from __future__ import annotations

import threading
from typing import Callable, Iterable

from .client import FETCH_BATCH, TradeAPIError, TradeClient
from .store import Store, record_from_fetch_entry


class Pipeline:
    def __init__(
        self,
        client: TradeClient,
        store: Store,
        on_stored: Callable[[int], None] | None = None,
        evaluator=None,
    ):
        self.client = client
        self.store = store
        self.on_stored = on_stored
        self.evaluator = evaluator
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def submit_hashes(self, hashes: Iterable[str], query_id: str) -> int:
        """Fetch + store any not-yet-archived hashes. Returns count newly stored."""
        new = self._claim_new(hashes)
        if not new:
            return 0
        league = self.client.creds().league
        stored = 0
        for batch in _chunks(new, FETCH_BATCH):
            try:
                entries = self.client.fetch_batch(batch, query_id)
            except TradeAPIError:
                # Let these be retried on a future run/message.
                self._release(batch)
                continue
            for entry in entries:
                rec = record_from_fetch_entry(entry, league)
                if rec.hash and self.store.insert_item(rec):
                    stored += 1
                    if self.evaluator is not None:
                        # Score new captures so they reach the review queue immediately
                        # (the evaluator's on_evaluated hook fires there, e.g. auto price
                        # checks). Never let an evaluation hiccup drop a stored item.
                        try:
                            self.evaluator.evaluate_entry(entry)
                        except Exception:  # noqa: BLE001
                            pass
        if stored and self.on_stored:
            self.on_stored(stored)
        return stored

    # --- dedupe bookkeeping --------------------------------------------

    def _claim_new(self, hashes: Iterable[str]) -> list[str]:
        out: list[str] = []
        with self._lock:
            for h in hashes:
                if not h or h in self._seen:
                    continue
                self._seen.add(h)
                if self.store.has_hash(h):
                    continue
                out.append(h)
        return out

    def _release(self, hashes: Iterable[str]) -> None:
        with self._lock:
            for h in hashes:
                self._seen.discard(h)

    def reset(self) -> None:
        """Forget the in-memory dedup set so cleared hashes get re-fetched (force resync)."""
        with self._lock:
            self._seen.clear()


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]

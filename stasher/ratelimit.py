"""Rate limiting for the PoE trade API.

The trade API publishes its limits in ``X-Rate-Limit-*`` response headers and will
lock you out for 15-30 minutes if you blow the longest window, so this limiter is
deliberately conservative:

* It throttles using *our own* recorded request timestamps (a sliding window per
  policy), persisted to the DB so a freshly started process doesn't burst.
* It learns the real bucket definitions from the response headers (they "can change
  at any time"), and honours the server's restriction state and ``Retry-After``.
* A configurable margin keeps a request or two of headroom under every bucket.

Two policies are tracked independently: ``search`` and ``fetch``.
"""

from __future__ import annotations

import math
import threading
import time
from collections import defaultdict
from dataclasses import dataclass

from .store import Store

Bucket = tuple[int, int]  # (max_hits, period_seconds)


@dataclass
class BucketStatus:
    max_hits: int
    limit_eff: int
    period: int
    used: int
    remaining: int
    reset_in: float
    restricted: bool


class RateLimiter:
    def __init__(
        self,
        store: Store,
        margin: int,
        fallback: dict[str, list[Bucket]],
        mode: str = "full",
        restrictive_fraction: float = 0.5,
    ):
        self.store = store
        self.margin = max(0, margin)
        self.mode = "restrictive" if mode == "restrictive" else "full"
        self.restrictive_fraction = min(0.9, max(0.0, restrictive_fraction))
        self._buckets: dict[str, list[Bucket]] = {k: list(v) for k, v in fallback.items()}
        self._restricted_until: dict[str, float] = defaultdict(float)
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._pruned_at = 0.0

    def set_mode(self, mode: str) -> str:
        self.mode = "restrictive" if mode == "restrictive" else "full"
        return self.mode

    def _limit_eff(self, max_hits: int) -> int:
        """Effective ceiling the tool will use for a bucket, given the mode."""
        if self.mode == "restrictive":
            reserve = max(self.margin, math.ceil(max_hits * self.restrictive_fraction))
        else:
            reserve = self.margin
        return max(1, max_hits - reserve)

    # --- public API -----------------------------------------------------

    def before(self, policy: str) -> None:
        """Block until it is safe to issue one request on ``policy``."""
        with self._locks[policy]:
            while True:
                wait = self._wait_needed(policy)
                if wait <= 0:
                    break
                time.sleep(min(wait, 60.0))
            self.store.record_rate_event(policy, time.time())
            self._maybe_prune()

    def after(self, policy: str, status_code: int, headers) -> None:
        """Update bucket definitions and restriction state from a response."""
        rules = _split(headers.get("X-Rate-Limit-Rules"))
        learned: list[Bucket] = []
        restricted_for = 0.0
        for rule in rules:
            for mx, period, _restrict in _parse_triplets(headers.get(f"X-Rate-Limit-{rule}")):
                learned.append((mx, period))
            for _cur, _period, restrict in _parse_triplets(
                headers.get(f"X-Rate-Limit-{rule}-State")
            ):
                restricted_for = max(restricted_for, restrict)
        if learned:
            self._buckets[policy] = _dedupe_buckets(learned)
        if restricted_for > 0:
            self._restricted_until[policy] = time.time() + restricted_for

    def note_429(self, policy: str, attempt: int, headers) -> float:
        """Record a 429 and return how long the caller should consider waiting.

        ``before`` will also enforce the resulting restriction window, so callers can
        simply retry after this; the returned value is informative."""
        retry_after = _to_float(headers.get("Retry-After"))
        backoff = min(2.0 ** attempt, 60.0)
        delay = max(retry_after or 0.0, backoff)
        self._restricted_until[policy] = max(
            self._restricted_until[policy], time.time() + delay
        )
        return delay

    def snapshot(self) -> dict[str, list[dict]]:
        """Per-policy bucket usage for the UI status bar."""
        out: dict[str, list[dict]] = {}
        now = time.time()
        for policy, buckets in self._buckets.items():
            statuses: list[dict] = []
            restricted_global = self._restricted_until[policy] > now
            for mx, period in sorted(buckets, key=lambda b: b[1]):
                events = self.store.rate_events_since(policy, now - period)
                used = len(events)
                reset_in = (events[0] + period - now) if events else 0.0
                statuses.append(
                    BucketStatus(
                        max_hits=mx,
                        limit_eff=self._limit_eff(mx),
                        period=period,
                        used=used,
                        remaining=max(0, mx - used),
                        reset_in=max(0.0, reset_in),
                        restricted=restricted_global,
                    ).__dict__
                )
            out[policy] = statuses
        return out

    # --- internals ------------------------------------------------------

    def _wait_needed(self, policy: str) -> float:
        now = time.time()
        wait = self._restricted_until[policy] - now
        buckets = self._buckets.get(policy, [])
        if not buckets:
            return max(0.0, wait)
        max_period = max(period for _, period in buckets)
        events = self.store.rate_events_since(policy, now - max_period)
        for mx, period in buckets:
            limit_eff = self._limit_eff(mx)
            in_window = [ts for ts in events if ts >= now - period]
            count = len(in_window)
            if count >= limit_eff:
                # Wait until enough of the oldest events age out of this window.
                k = count - (limit_eff - 1)  # number that must expire
                expire_at = in_window[k - 1] + period
                wait = max(wait, expire_at - now)
        return max(0.0, wait)

    def _maybe_prune(self) -> None:
        now = time.time()
        if now - self._pruned_at < 60:
            return
        self._pruned_at = now
        longest = max((p for bs in self._buckets.values() for _, p in bs), default=3600)
        self.store.prune_rate_events(now - longest - 60)


# --- header parsing -----------------------------------------------------


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in value.split(",")] if value else []


def _parse_triplets(value: str | None) -> list[tuple[int, int, float]]:
    """Parse ``a:b:c,d:e:f`` rate-limit header values into (a, b, c) tuples."""
    out: list[tuple[int, int, float]] = []
    for part in _split(value):
        bits = part.split(":")
        if len(bits) >= 3:
            try:
                out.append((int(bits[0]), int(bits[1]), float(bits[2])))
            except ValueError:
                continue
    return out


def _dedupe_buckets(buckets: list[Bucket]) -> list[Bucket]:
    """Keep the most restrictive max_hits for each period."""
    best: dict[int, int] = {}
    for mx, period in buckets:
        if period not in best or mx < best[period]:
            best[period] = mx
    return sorted(((mx, p) for p, mx in best.items()), key=lambda b: b[1])


def _to_float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None

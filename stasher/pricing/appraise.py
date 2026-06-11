"""On-demand appraisal service: cache lookup + a background pricing queue.

The consumer-facing layer over the pricing core. A :class:`PricingService` owns a single
worker thread that drains a queue of item hashes, prices each through the shared
:class:`~stasher.pricing.PriceSource` (so it interleaves with capture on one rate budget),
and writes the result to the :class:`~stasher.store.Store` price cache. The Flask UI calls
:meth:`lookup` (synchronous, cache-only) and :meth:`request` (enqueue a fresh check), and
polls :meth:`status`.

**Safety interlock.** :func:`data_ready` gates live pricing: while the vendored stat tables
still carry Phase-0 ``_example`` placeholders (pseudo ids, empty-slot ids, the provisional
defence filter group), a fresh check would issue searches with unverified filters — so the
service refuses to enqueue and reports why. Real *explicit* ids come from the item itself and
are fine; only the harvested aggregates are pending (``PRICING_MODULE_PLAN.md`` §11).
"""

from __future__ import annotations

import queue
import threading
from datetime import datetime, timezone

from . import plan as _plan
from . import pricer as _pricer
from . import pseudo as _pseudo
from . import query as _query

DEFAULT_TTL_HOURS = 14 * 24.0  # 14 days — the trade market moves slowly for generic mod combos


def data_ready(store=None) -> tuple[bool, str]:
    """Whether it is safe to issue live price searches. Returns ``(ready, reason)``.

    A user can force-enable once they've harvested real ids by setting
    ``settings.pricing_force_ready = "1"`` (escape hatch for testing the wiring)."""
    if store is not None and (store.get_setting("pricing_force_ready") or "") == "1":
        return True, "forced"
    rules = _pseudo._rules()
    placeholder = any(a.get("_example") for a in rules.get("aggregates") or []) or bool(
        (rules.get("empty_slots") or {}).get("_example")
    )
    if placeholder:
        return False, ("Pricing data not yet harvested (Phase 0): pseudo/aggregate stat ids are "
                       "placeholders. See PRICING_MODULE_PLAN.md §11.")
    return True, "ready"


def _age_hours(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


# Automatic price checks: enqueue new captures whose current/crafting score crosses a
# threshold. OFF by default (the header button next to Auto-refresh toggles it); the two
# thresholds are configured separately on the Settings page (0 disables that basis).
AUTO_PRICE_ENABLED_KEY = "auto_price_enabled"
AUTO_PRICE_MIN_NOW_KEY = "auto_price_min_now"
AUTO_PRICE_MIN_CRAFT_KEY = "auto_price_min_craft"
DEFAULT_AUTO_PRICE_MIN = 0.75


def auto_price_config(store) -> dict:
    """{'enabled', 'min_now', 'min_craft'} from settings (clamped 0..1; defaults off/0.75)."""
    def f(key: str) -> float:
        try:
            return max(0.0, min(1.0, float(store.get_setting(key, str(DEFAULT_AUTO_PRICE_MIN))
                                           or DEFAULT_AUTO_PRICE_MIN)))
        except (TypeError, ValueError):
            return DEFAULT_AUTO_PRICE_MIN
    return {"enabled": store.get_setting(AUTO_PRICE_ENABLED_KEY, "0") == "1",
            "min_now": f(AUTO_PRICE_MIN_NOW_KEY),
            "min_craft": f(AUTO_PRICE_MIN_CRAFT_KEY)}


class PricingService:
    def __init__(self, store, source, league_getter, *, grade=None, hint_getter=None):
        self.store = store
        self.source = source
        self._league = league_getter
        self._grade = grade
        # Optional evaluator verdict for an item ({"driver": "now"|"craft", ...}) — decides
        # the rare finished-vs-potential plan strategy. Never lets a hint failure block a
        # price check.
        self._hint_getter = hint_getter

        self._q: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self._queued: set[str] = set()
        self._lock = threading.Lock()
        self._in_progress: str | None = None
        self._last: dict | None = None
        self._last_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _hint(self, item: dict) -> dict | None:
        if self._hint_getter is None:
            return None
        try:
            return self._hint_getter(item)
        except Exception:  # noqa: BLE001 — hints are best-effort
            return None

    # --- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="stasher-pricing", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def ttl_hours(self) -> float:
        try:
            return max(0.0, float(self.store.get_setting("price_cache_ttl_hours",
                                                         str(DEFAULT_TTL_HOURS))))
        except (TypeError, ValueError):
            return DEFAULT_TTL_HOURS

    # --- cache lookup (synchronous) -------------------------------------

    def lookup(self, item: dict) -> dict:
        """Cache-only result for an item (no network): exact hit (fresh/stale by TTL), else a
        fuzzy 'similar' hit, else a miss. Always returns the computed ``plan_sig``."""
        plan = _plan.build_for_item(item, grade=self._grade, eval_hint=self._hint(item))
        league = self._league()
        sig = _query.plan_sig(plan, league)
        exact = self.store.get_cached_price(sig, league)
        if exact:
            age = _age_hours(exact.get("sampled_at"))
            fresh = age is not None and age <= self.ttl_hours()
            return {"status": "fresh" if fresh else "stale", "estimate": exact["estimate"],
                    "age_hours": age, "plan_sig": sig}
        similar = self.store.find_similar_price(
            strategy=plan.strategy, base=plan.base, league=league,
            filters=_query.normalized_filters(plan), max_age_hours=self.ttl_hours())
        if similar:
            return {"status": "similar", "estimate": similar["estimate"],
                    "age_hours": _age_hours(similar.get("sampled_at")), "plan_sig": sig}
        return {"status": "miss", "plan_sig": sig}

    # --- automatic price checks (threshold-driven, off by default) -------

    def maybe_auto_request(self, item_hash: str, item: dict, evaluation, *,
                           require_unpriced: bool = False) -> bool:
        """Enqueue a price check for an evaluated item when auto-pricing is on and its
        current (as-is) or crafting score crosses the configured threshold (a 0 threshold
        disables that basis). Cache discipline — automation must never spend budget on
        what's already known: a FRESH cached estimate always skips, and with
        ``require_unpriced`` (the bulk re-evaluation path) anything short of a complete
        cache miss skips (stale/similar data still counts as "has price data"). Returns
        True when a check was actually queued."""
        cfg = auto_price_config(self.store)
        if not cfg["enabled"]:
            return False
        now = getattr(evaluation, "score_now", None)
        craft = getattr(evaluation, "score_potential", None)
        hit = ((cfg["min_now"] > 0 and now is not None and now >= cfg["min_now"])
               or (cfg["min_craft"] > 0 and craft is not None and craft >= cfg["min_craft"]))
        if not hit:
            return False
        try:
            status = self.lookup(item).get("status")
            if status == "fresh" or (require_unpriced and status != "miss"):
                return False
        except Exception:  # noqa: BLE001 — a lookup hiccup shouldn't block the check
            pass
        return bool(self.request(item_hash, item).get("queued"))

    # --- enqueue a fresh check ------------------------------------------

    def request(self, item_hash: str, item: dict) -> dict:
        """Enqueue a fresh price check (deduped). Refuses when :func:`data_ready` is False."""
        ready, reason = data_ready(self.store)
        if not ready:
            return {"ok": False, "reason": reason}
        with self._lock:
            if item_hash in self._queued or item_hash == self._in_progress:
                return {"ok": True, "queued": True, "deduped": True}
            self._queued.add(item_hash)
        self._q.put((item_hash, item))
        self.start()
        return {"ok": True, "queued": True}

    def status(self) -> dict:
        with self._lock:
            return {
                "queued": len(self._queued),
                "queued_hashes": sorted(self._queued),  # lets the UI flag the specific card(s)
                "in_progress": self._in_progress,
                "last": dict(self._last) if self._last else None,
                "last_error": self._last_error,
            }

    # --- worker loop ----------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item_hash, item = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            with self._lock:
                self._in_progress = item_hash
                self._queued.discard(item_hash)
            try:
                self._price_one(item_hash, item)
            except Exception as exc:  # noqa: BLE001 — keep the loop alive
                with self._lock:
                    self._last_error = str(exc)[:160]
            finally:
                with self._lock:
                    self._in_progress = None
                self._q.task_done()

    def _price_one(self, item_hash: str, item: dict) -> None:
        plan = _plan.build_for_item(item, grade=self._grade, eval_hint=self._hint(item))
        league = self._league()
        est = _pricer.estimate(plan, self.source, league=league)
        self.store.cache_price(
            est.plan_sig, strategy=plan.strategy, rarity=plan.rarity, base=plan.base,
            league=league, filters=_query.normalized_filters(plan), estimate=est.to_dict())
        self.store.set_item_price(item_hash, est.plan_sig)
        with self._lock:
            self._last = {"item_hash": item_hash, "value": est.value, "currency": est.currency,
                          "confidence": est.confidence, "is_floor": est.is_floor,
                          "at": est.sampled_at}

"""Backfill: enumerate currently-listed items via partitioned trade searches.

A single search returns at most ~100 result hashes with no offset pagination, so to
capture an account with thousands of listings we partition: first by item category,
then recursively bisect by item level, and finally split by rarity. Each leaf search
returns <= the cap (or we log it as incomplete and take what we can).
"""

from __future__ import annotations

import json
from typing import Callable

from .categories import category_filter, fetch_categories
from .client import TradeAPIError, TradeClient
from .pipeline import Pipeline
from .store import Store, utc_now_iso

RESULT_CAP = 100  # usable results from one search
ILVL_MIN, ILVL_MAX = 0, 100
RARITIES = ["normal", "magic", "rare", "unique"]
NEWEST_SORT = {"indexed": "desc"}  # surface freshly-listed items first
POLL_ANCHOR_KEY = "poll_anchor_hashes"
POLL_ANCHORS = 10  # newest hashes remembered between polls to anchor the page boundary

ProgressFn = Callable[[str, int, int], None]  # (label, partitions_done, items_new)
StopFn = Callable[[], bool]


def run_light_poll(client: TradeClient, store: Store, pipeline: Pipeline) -> dict:
    """One cheap account search (newest-first) -> fetch only the not-yet-archived hashes.

    The recurring poll for near-live capture: a single search call plus fetches for
    genuinely new listings. Catches everything when your active listings fit in one page
    (<= ~100); the periodic full backfill covers the rare overflow. Falls back to the
    default sort if the server rejects the newest-first sort key.
    """
    try:
        res = client.search(target="poll (newest)", sort=NEWEST_SORT)
    except TradeAPIError:
        res = client.search(target="poll")
    hashes = res["result"]
    stored = pipeline.submit_hashes(hashes, res["id"])
    # Carry the previous poll's newest hashes as anchors, then remember this poll's for
    # the next one (see poll_indicates_overflow).
    prev_anchors = json.loads(store.get_meta(POLL_ANCHOR_KEY) or "[]")
    store.set_meta(POLL_ANCHOR_KEY, json.dumps(hashes[:POLL_ANCHORS]))
    store.set_meta("last_poll_at", utc_now_iso())
    return {
        "new": stored,
        "listed": len(hashes),
        "total": res["total"],
        "hashes": hashes,
        "prev_anchors": prev_anchors,
    }


def poll_indicates_overflow(res: dict) -> bool:
    """True if a light poll may have missed newly-listed items.

    With newest-first sorting, a poll has provably caught up only if its page still
    reaches back into the region we saw last time -- i.e. at least one *anchor* (one of
    the previous poll's newest hashes) is still on the page. Every genuinely-new listing
    is newer than those anchors, so if an anchor is present, the new items are on the
    page too. If *all* anchors have been pushed off (a bulk re-index -- e.g. dragging a
    full quad tab into a price tab re-indexes 100+ known items above page 1 -- or a flood
    of new listings), or there is no prior anchor at all (cold start), we can't rule out
    missed listings, so a full re-sync is warranted. A page that holds the whole account
    (``total <= listed``) is always complete.
    """
    listed = res["listed"]
    if listed == 0 or res["total"] <= listed:
        return False
    prev_anchors = res.get("prev_anchors") or []
    if not prev_anchors:
        return True
    current = set(res.get("hashes") or [])
    return not any(a in current for a in prev_anchors)


def run_backfill(
    client: TradeClient,
    store: Store,
    pipeline: Pipeline,
    progress: ProgressFn | None = None,
    should_stop: StopFn | None = None,
) -> dict:
    summary = {"new": 0, "partitions": 0, "categories": 0, "incomplete": 0}
    categories = fetch_categories(client.config.base_url, client.config.realm, client._headers())
    summary["categories"] = len(categories)

    for index, category in enumerate(categories):
        if should_stop and should_stop():
            break
        label = f"{category} ({index + 1}/{len(categories)})"
        if progress:
            progress(label, summary["partitions"], summary["new"])
        try:
            _enumerate(client, pipeline, category, summary, progress, should_stop)
        except TradeAPIError as exc:
            store.log_query("search", category, "error", None, str(exc)[:160])
            continue

    store.set_meta("last_backfill_at", utc_now_iso())
    return summary


def _enumerate(
    client: TradeClient,
    pipeline: Pipeline,
    category: str,
    summary: dict,
    progress: ProgressFn | None,
    should_stop: StopFn | None,
    ilvl_lo: int = ILVL_MIN,
    ilvl_hi: int = ILVL_MAX,
    rarity: str | None = None,
) -> None:
    if should_stop and should_stop():
        return

    res = client.search(
        _compose(category, ilvl_lo, ilvl_hi, rarity),
        target=_label(category, ilvl_lo, ilvl_hi, rarity),
    )
    summary["partitions"] += 1
    total = res["total"]
    result = res["result"]
    query_id = res["id"]

    if total <= RESULT_CAP:
        summary["new"] += pipeline.submit_hashes(result, query_id)
        if progress:
            progress(_label(category, ilvl_lo, ilvl_hi, rarity),
                     summary["partitions"], summary["new"])
        return

    # Too many results: subdivide. Bisect item level first.
    if ilvl_hi > ilvl_lo:
        mid = (ilvl_lo + ilvl_hi) // 2
        _enumerate(client, pipeline, category, summary, progress, should_stop,
                   ilvl_lo, mid, rarity)
        _enumerate(client, pipeline, category, summary, progress, should_stop,
                   mid + 1, ilvl_hi, rarity)
        return

    # Single ilvl and still over cap: split by rarity.
    if rarity is None:
        for r in RARITIES:
            _enumerate(client, pipeline, category, summary, progress, should_stop,
                       ilvl_lo, ilvl_hi, r)
        return

    # Cannot subdivide further; take the first page and flag it.
    summary["incomplete"] += 1
    summary["new"] += pipeline.submit_hashes(result, query_id)


def _compose(category: str, ilvl_lo: int, ilvl_hi: int, rarity: str | None) -> dict:
    type_filters = category_filter(category)["type_filters"]
    if rarity is not None:
        type_filters["filters"]["rarity"] = {"option": rarity}
    filters: dict = {"type_filters": type_filters}
    if (ilvl_lo, ilvl_hi) != (ILVL_MIN, ILVL_MAX):
        filters["misc_filters"] = {
            "filters": {"ilvl": {"min": ilvl_lo, "max": ilvl_hi}}
        }
    return filters


def _label(category: str, ilvl_lo: int, ilvl_hi: int, rarity: str | None) -> str:
    parts = [category]
    if (ilvl_lo, ilvl_hi) != (ILVL_MIN, ILVL_MAX):
        parts.append(f"ilvl {ilvl_lo}-{ilvl_hi}")
    if rarity:
        parts.append(rarity)
    return " ".join(parts)

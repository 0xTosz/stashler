"""Turn a :class:`~stasher.pricing.FilterPlan` into trade2 search bodies — pure, no network.

Two things live here:

* :func:`compose` routes each :class:`StatFilter` to its trade2 filter group (``stats`` /
  ``weapon_filters`` / ``armour_filters``) and assembles the ``extra_filters`` fragment that
  :meth:`stasher.client.TradeClient.search` (market mode) merges into the query body.
* :func:`ladder` yields the budgeted relaxation rungs (≤3 + a terminal floor): full rolls →
  tier floors → drop the droppable mods → drop the base anchor (floor estimate). The runner
  in :mod:`stasher.pricing.pricer` stops at the first rung with enough matches.

Also :func:`plan_sig` — a *deterministic* cache key derived from the item's intended plan
(its tier-floor buckets), not the rung that happened to win, so re-pricing is a clean hit.
"""

from __future__ import annotations

import hashlib

from . import (
    TARGET_STATS,
    FilterPlan,
    StatFilter,
)

# Ladder rung labels (also surfaced in PriceEstimate.strategy).
RUNG_FULL = "full"
RUNG_RELAXED = "relaxed"
RUNG_DROPPED = "dropped"
RUNG_FLOOR = "floor"


def _route(extra: dict, sf: StatFilter, value: float) -> None:
    """Place one filter (at ``value``) into the right group of an ``extra_filters`` dict."""
    if sf.target == TARGET_STATS:
        if not sf.id:
            return
        group = extra.setdefault("stats", [{"type": "and", "filters": []}])
        group[0]["filters"].append(
            {"id": sf.id, "value": {"min": _num(value)}, "disabled": False}
        )
        return
    # Aggregate groups, e.g. "weapon_filters.pdps" / "armour_filters.es".
    if "." in sf.target:
        group_name, field = sf.target.split(".", 1)
        grp = extra.setdefault(group_name, {"filters": {}})
        grp["filters"][field] = {"min": _num(value)}


def _num(value: float):
    """Trade values are cleaner as ints when whole (matches what the site sends)."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def compose(plan: FilterPlan, filters: list[StatFilter]) -> dict:
    """Build the ``extra_filters`` fragment (type_filters + the routed filter groups) for a
    given subset of filters, each already at the min it should search."""
    extra: dict = {}
    if plan.type_filters:
        extra["type_filters"] = dict(plan.type_filters)
    for sf in filters:
        _route(extra, sf, sf.min)
    return extra


def body(plan: FilterPlan, filters: list[StatFilter], *, drop_base: bool = False) -> dict:
    """The full market-search payload fragment: ``compose`` output plus the exact base ``type``
    when base-anchored (and not dropped). The exact base is carried as the reserved key
    ``_type`` (the search adapter lifts it to the body's top-level ``query.type``); the account
    filter and ``status`` are added by :meth:`TradeClient.search` in market mode, never here.
    ``drop_base`` (the terminal floor rung) omits the base so only the category/rarity gate +
    surviving filters remain."""
    out = compose(plan, filters)
    if plan.base and not drop_base:
        out["_type"] = plan.base
    return out


def ladder(plan: FilterPlan) -> list[tuple[str, dict]]:
    """The ordered (label, body-fragment) rungs to try, most-specific first.

    * ``full``    — every filter at its rolled min.
    * ``relaxed`` — every filter at its tier floor (``relax_floor``).
    * ``dropped`` — non-droppable filters only, at their tier floor.
    * ``floor``   — base anchor dropped too; the broadest comparable (a *floor* estimate).
    """
    full = list(plan.filters)
    relaxed = [sf.with_min(sf.relax_floor) for sf in plan.filters]
    kept = [sf.with_min(sf.relax_floor) for sf in plan.filters if not sf.droppable]

    rungs: list[tuple[str, dict]] = [
        (RUNG_FULL, body(plan, full)),
        (RUNG_RELAXED, body(plan, relaxed)),
    ]
    # Only add a distinct "dropped" rung if it actually removes something.
    if kept and len(kept) != len(plan.filters):
        rungs.append((RUNG_DROPPED, body(plan, kept)))
    # Terminal floor: drop the base anchor; keep whatever non-droppable filters remain (or, if
    # all were droppable, keep none — pure category/rarity breadth).
    rungs.append((RUNG_FLOOR, body(plan, kept, drop_base=True)))
    return rungs


def plan_sig(plan: FilterPlan, league: str | None = None) -> str:
    """Deterministic cache signature: strategy + base + each filter's (target, id, tier floor).

    Keys on ``relax_floor`` (the tier floor), not the exact roll, so two items in the same
    tier band collide onto one cache entry — and the key never shifts when the market thins
    (it's the *intended* plan, not the winning rung)."""
    parts = [plan.strategy, plan.base or "", league or ""]
    units = sorted(
        f"{sf.target}|{sf.id or ''}|{_num(sf.relax_floor)}" for sf in plan.filters
    )
    parts.extend(units)
    digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def normalized_filters(plan: FilterPlan) -> list[dict]:
    """A JSON-able, order-independent view of the plan's filters for similarity matching in the
    cache (see Store.find_similar_price): each filter's target/id and its tier floor."""
    return sorted(
        ({"target": sf.target, "id": sf.id, "floor": _num(sf.relax_floor)} for sf in plan.filters),
        key=lambda d: (d["target"], d["id"] or ""),
    )

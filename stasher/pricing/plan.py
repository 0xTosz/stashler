"""Turn a captured item into a :class:`~stasher.pricing.FilterPlan`.

This is where the rarity/finish **strategy matrix** (``PRICING_MODULE_PLAN.md`` §4.1) and the
filter ranking live. It reads the item via :mod:`stasher.evaluate.itemdata`, folds fungible
stats into pseudos (:mod:`stasher.pricing.pseudo`), and picks headline aggregates
(:mod:`stasher.pricing.aggregates`).

NOTE — eval integration is intentionally light here (Phase-1 scaffold): a ``grade`` callback
supplies per-stat desirability (0..1) and the "finished vs. potential" / "strong base"
decisions use coarse heuristics. The plan is to replace these with the evaluator's real mod
grading, tier bands (for ``relax_floor``), free-slot/headroom and defence-gate signals — all
of which already exist in :mod:`stasher.evaluate`. The marked TODOs are those seams.
"""

from __future__ import annotations

from typing import Callable

from . import (
    GROUP_AGGREGATE,
    GROUP_EXPLICIT,
    GROUP_PSEUDO,
    STRATEGY_MAGIC_BASE,
    STRATEGY_RARE_FINISHED,
    STRATEGY_RARE_POTENTIAL,
    STRATEGY_UNIQUE,
    TARGET_STATS,
    FilterPlan,
    StatFilter,
)
from ..evaluate import itemdata
from . import aggregates, pseudo

DISCLAIMER = "Price reflects the base item; runes, sockets and corruption are not valued."

# Coarse, replace-with-eval thresholds.
_RELAX_FRACTION = 0.8     # fallback when an affix carries no tier range (relax_floor from extended).
_FINISHED_AFFIXES = 5     # TODO(eval): use free_slots/headroom, not a rendered-affix count.
_MAX_FILTERS = 4
_MAGIC_MAX_AFFIXES = 2    # a magic item has at most 1 prefix + 1 suffix


Grader = Callable[[str], float]  # stat id -> desirability 0..1


def _rarity_option(item: dict) -> str | None:
    r = (itemdata.rarity(item) or "").lower()
    return r if r in ("normal", "magic", "rare", "unique") else None


def _open_affix_filter(item: dict, max_affixes: int) -> tuple[list[StatFilter], int]:
    """A droppable "comparable must have >= this many open affix slots" filter, sized to the
    item's *own* open count, + that count. Empty when the item is full or the pseudo is
    unharvested. Prices the crafting headroom of the open slot(s) — without it, the search
    matches items whose slots are already filled (with junk), which are cheaper and drag the
    cheapest-N price down."""
    _, affix_count = itemdata.explicit_affix_mods(item)
    open_slots = max(0, max_affixes - affix_count)
    aff = pseudo.empty_slot_ids().get("affix")
    if open_slots >= 1 and aff:
        return [StatFilter(TARGET_STATS, min=open_slots, relax_floor=1, droppable=True,
                          id=aff, group=GROUP_PSEUDO)], open_slots
    return [], 0


def _relax(value: float) -> float:
    floor = value * _RELAX_FRACTION
    return float(int(floor)) if floor >= 1 else round(floor, 2)


def _explicit_filters(item: dict, grade: Grader | None, *, exclude: set[str]) -> list[StatFilter]:
    """One stat filter per explicit stat id (value = de-merged total), ranked by desirability
    (``grade``) then magnitude, excluding ids already consumed by a pseudo. ``relax_floor`` is
    the affix's real tier minimum (from ``extended``), falling back to a fraction of the roll."""
    totals = pseudo.item_stat_totals(item)
    floors = pseudo.item_stat_floors(item)
    ranked = sorted(
        ((sid, val) for sid, val in totals.items() if sid not in exclude),
        key=lambda kv: ((grade(kv[0]) if grade else 0.0), kv[1]),
        reverse=True,
    )
    out: list[StatFilter] = []
    for sid, val in ranked:
        out.append(StatFilter(TARGET_STATS, min=round(val, 2),
                              relax_floor=floors.get(sid) or _relax(val),
                              droppable=True, id=sid, group=GROUP_EXPLICIT))
    return out


def _pseudo_filters(item: dict) -> tuple[list[StatFilter], set[str]]:
    """Pseudo aggregate filters + the set of real stat ids they consume (to dedupe)."""
    out: list[StatFilter] = []
    consumed: set[str] = set()
    for ps in pseudo.pseudos_for(item):
        out.append(StatFilter(TARGET_STATS, min=ps.value, relax_floor=ps.floor or _relax(ps.value),
                              droppable=True, id=ps.pseudo_id, group=GROUP_PSEUDO))
        consumed.update(ps.components)
    return out, consumed


def _aggregate_filters(item: dict, *, droppable: bool) -> list[StatFilter]:
    """Headline aggregate filters (weapon dps / defence totals) for the item's relevant type."""
    out: list[StatFilter] = []
    headline = aggregates.headline_for(item)
    # Prefer the most informative single aggregate per family: dps for weapons, the largest
    # defence total otherwise. (TODO(eval): pick by the item's actual build role.)
    if aggregates.is_weapon(item):
        for key in ("dps", "pdps", "edps"):
            if key in headline:
                target, value = headline[key]
                out.append(StatFilter(target, min=value, relax_floor=_relax(value),
                                      droppable=droppable, group=GROUP_AGGREGATE))
                break
    else:
        for key in ("energy_shield", "armour", "evasion"):
            if key in headline:
                target, value = headline[key]
                out.append(StatFilter(target, min=value, relax_floor=_relax(value),
                                      droppable=droppable, group=GROUP_AGGREGATE))
                break
    return out


def _open_slot_filters(item: dict) -> list[StatFilter]:
    """Require the comparable to also have crafting headroom: at least one **total** open affix
    slot. Droppable, so the ladder can relax it rather than zero out.

    Uses the total-open-affix pseudo (not a prefix AND suffix pair, which would force *both* open
    even when the item has only one — over-restrictive). TODO(eval): require the item's actual
    open count once prefix/suffix classification is wired in."""
    aff = pseudo.empty_slot_ids().get("affix")
    if not aff:
        return []
    return [StatFilter(TARGET_STATS, min=1, relax_floor=1, droppable=True,
                      id=aff, group=GROUP_PSEUDO)]


def build_for_item(
    item: dict,
    *,
    grade: Grader | None = None,
    max_filters: int = _MAX_FILTERS,
) -> FilterPlan:
    """Build the search plan for one ``/fetch`` item dict (the ``item`` sub-object)."""
    rarity = _rarity_option(item)
    base = itemdata.base_type(item) or None
    type_filters = {"filters": {}}
    if rarity:
        type_filters["filters"]["rarity"] = {"option": rarity}
    notes = [DISCLAIMER]

    # --- unique: name-anchored (reserved; minimal plan for now) ---------------------
    if rarity == "unique":
        notes.append("Unique pricing is preliminary (name-anchored).")
        return FilterPlan(STRATEGY_UNIQUE, type_filters=type_filters,
                          filters=[], notes=notes, rarity=rarity, base=base)

    pseudo_fs, consumed = _pseudo_filters(item)
    explicit_fs = _explicit_filters(item, grade, exclude=consumed)

    # --- magic: base-anchored (+ crafting headroom for an open slot) ----------------
    if rarity == "magic":
        filters = (pseudo_fs + explicit_fs)[:max_filters]
        open_fs, open_n = _open_affix_filter(item, _MAGIC_MAX_AFFIXES)
        if open_fs:
            # Value the open slot: compare only against equally-craftable magics (an open slot),
            # not ones whose second slot is already used up. Otherwise 1-affix magics underprice.
            filters = filters + open_fs
            notes.append(f"Values {open_n} open affix slot{'s' if open_n != 1 else ''} "
                         "(crafting potential).")
        return FilterPlan(STRATEGY_MAGIC_BASE, type_filters=type_filters,
                          filters=filters, notes=notes, rarity=rarity, base=base)

    # --- rare: finished (aggregate-anchored) vs potential (base + open slots) -------
    _affixes = pseudo.item_stat_totals(item)
    affix_count = len(_affixes)  # TODO(eval): use real affix/free-slot counts.
    finished = affix_count >= _FINISHED_AFFIXES
    strong_base = bool(aggregates.headline_for(item))  # TODO(eval): use the defence gate.

    if finished or not strong_base:
        # Finished: anchor on the headline aggregate (base-agnostic → no exact base).
        agg = _aggregate_filters(item, droppable=False)
        filters = (agg + pseudo_fs + explicit_fs)[: max_filters + len(agg)]
        return FilterPlan(STRATEGY_RARE_FINISHED, type_filters=type_filters,
                          filters=filters, notes=notes, rarity=rarity, base=None)

    # Potential: keep the (strong) base, price the present mods + crafting headroom.
    notes.append("Includes crafting potential (open affix slots).")
    filters = (_aggregate_filters(item, droppable=False) + pseudo_fs
               + explicit_fs[: max_filters] + _open_slot_filters(item))
    return FilterPlan(STRATEGY_RARE_POTENTIAL, type_filters=type_filters,
                      filters=filters, notes=notes, rarity=rarity, base=base)

"""Pseudo-mod aggregation: turn an item's real stats into liquid ``pseudo.*`` filters.

Trade2 exposes aggregate ``pseudo`` stats (total elemental resistance, total attributes, …)
that sum several real stats and so match far more listings than ANDing the components. The
item already carries the real, fully-prefixed stat ids in its ``extended`` block (see
``PRICING_MODULE_PLAN.md`` §2), so we just sum the present components and emit the pseudo.

The composition table (``data/pseudo_rules.json``) is **seed-only** until the Phase-0 harvest
replaces the placeholder ids with the real ``/data/stats`` ones (§11). The logic here is final.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_RULES_FILE = Path(__file__).resolve().parent / "data" / "pseudo_rules.json"


@lru_cache(maxsize=1)
def _rules() -> dict:
    try:
        return json.loads(_RULES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"aggregates": [], "empty_slots": {}}


@dataclass(frozen=True)
class Pseudo:
    pseudo_id: str
    value: float
    floor: float                 # the same sum using each component's tier minimum (relax target)
    components: tuple[str, ...]  # the real stat ids consumed (so the plan can avoid double-counting)


def _stat_agg(item: dict, pick) -> dict[str, float]:
    """Sum ``pick(mag)`` for each explicit stat id across the item's affixes (from
    ``extended.mods.explicit[].magnitudes[]``). {} without structured mod data."""
    emods = ((item.get("extended") or {}).get("mods") or {}).get("explicit") or []
    out: dict[str, float] = {}
    for mod in emods:
        for mag in mod.get("magnitudes") or []:
            h = mag.get("hash")
            if not h:
                continue
            try:
                out[h] = out.get(h, 0.0) + pick(mag)
            except (TypeError, ValueError, KeyError):
                continue
    return out


def item_stat_totals(item: dict) -> dict[str, float]:
    """Each explicit stat id's value = sum over affixes of its range **midpoint** (the de-merged
    per-affix share — see :func:`stasher.evaluate.itemdata.explicit_affix_mods`)."""
    return _stat_agg(item, lambda m: (float(m["min"]) + float(m["max"])) / 2.0)


def item_stat_floors(item: dict) -> dict[str, float]:
    """Each explicit stat id's **tier floor** = sum over affixes of its magnitude minimum (the
    lowest roll of the rolled tier). Used as the ladder's ``relax_floor`` — searching down to
    the tier floor keeps same-tier comparables instead of dropping the mod."""
    return _stat_agg(item, lambda m: float(m["min"]))


def pseudos_for(item: dict) -> list[Pseudo]:
    """The pseudo aggregates an item qualifies for (≥ ``min_components`` of a rule present).

    Each component carries a value ``mult`` (e.g. a single "+x% to all Elemental Resistances"
    contributes ``3·x`` to total elemental resistance), matching EE2's PSEUDO_RULES."""
    totals = item_stat_totals(item)
    floors = item_stat_floors(item)
    out: list[Pseudo] = []
    for rule in _rules().get("aggregates") or []:
        present = [(c["id"], c.get("mult", 1)) for c in (rule.get("components") or [])
                   if c.get("id") in totals]
        if len(present) < int(rule.get("min_components", 1)):
            continue
        summed = sum(totals[cid] * mult for cid, mult in present)
        if summed <= 0:
            continue
        floor = sum(floors.get(cid, 0.0) * mult for cid, mult in present)
        out.append(Pseudo(rule["pseudo_id"], round(summed, 2), round(floor, 2),
                          tuple(cid for cid, _ in present)))
    return out


def empty_slot_ids() -> dict[str, str]:
    """Open-slot pseudo ids: ``{"prefix", "suffix", "affix"}`` (``affix`` = total open slots),
    each present only when harvested. Used by the rare_potential strategy to price headroom."""
    es = _rules().get("empty_slots") or {}
    return {k: es[k] for k in ("prefix", "suffix", "affix") if es.get(k)}

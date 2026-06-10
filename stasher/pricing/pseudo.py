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
    components: tuple[str, ...]  # the real stat ids consumed (so the plan can avoid double-counting)


def item_stat_totals(item: dict) -> dict[str, float]:
    """Sum each explicit stat id's magnitude across the item's affixes.

    Reads ``extended.mods.explicit[].magnitudes[]`` ({hash, min, max}); a stat's value is the
    sum over affixes of its range midpoint (the de-merged per-affix share — see
    :func:`stasher.evaluate.itemdata.explicit_affix_mods`). Falls back to {} without
    structured mod data."""
    ext = item.get("extended") or {}
    emods = (ext.get("mods") or {}).get("explicit") or []
    totals: dict[str, float] = {}
    for mod in emods:
        for mag in mod.get("magnitudes") or []:
            h = mag.get("hash")
            if not h:
                continue
            try:
                mid = (float(mag["min"]) + float(mag["max"])) / 2.0
            except (TypeError, ValueError, KeyError):
                continue
            totals[h] = totals.get(h, 0.0) + mid
    return totals


def pseudos_for(item: dict) -> list[Pseudo]:
    """The pseudo aggregates an item qualifies for (≥ ``min_components`` of a rule present)."""
    totals = item_stat_totals(item)
    out: list[Pseudo] = []
    for rule in _rules().get("aggregates") or []:
        comps = [c for c in rule.get("components") or [] if c in totals]
        if len(comps) < int(rule.get("min_components", 1)):
            continue
        summed = sum(totals[c] for c in comps)
        if summed <= 0:
            continue
        out.append(Pseudo(rule["pseudo_id"], round(summed, 2), tuple(comps)))
    return out


def empty_slot_ids() -> dict[str, str]:
    """``{"prefix": <pseudo id>, "suffix": <pseudo id>}`` for the open-slot filters used by the
    rare_potential strategy (empty entries when not yet harvested)."""
    es = _rules().get("empty_slots") or {}
    return {k: es[k] for k in ("prefix", "suffix") if es.get(k)}

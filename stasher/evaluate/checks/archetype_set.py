"""Archetype-set checker: score items against a *mined* ``ArchetypeSet`` (data-driven).

Loads a league archetype YAML produced by ``archetype_miner`` (graded, base-aware
archetypes) and flags an item with its best-matching archetype + the computed tier/value.
Toggleable like the loot filter via ``[archetype_set] enabled`` in the rules file.

This is distinct from the hand-authored ``archetype`` checker (``checks/archetype.py``); both
can be enabled independently. Item affixes are normalized to the same ``mod_key`` the miner
used (see ``affix_norm``) so keys line up with the YAML.
"""

from __future__ import annotations

from pathlib import Path

from . import base as _base
from ..affix_norm import clean_mod_text, defence_types, mod_key, mod_magnitude
from ..archetype_model import ArchetypeSet
from ..itemdata import base_type, item_class, rarity

CheckResult = _base.CheckResult

# How many matched archetypes to surface as separate queue flag reasons (full matches first,
# then partials above the set's partial threshold), ranked by value.
TOP_K_REASONS = 3


def model_item(item: dict) -> dict:
    """Project a GGG ``/fetch`` item into the model's input: class/rarity/base, the base's defence
    types (segment gate), explicit affix count (open-slot/craft reasoning), and a
    ``{mod_key: magnitude}`` map (same normalization as the miner). Every affix is its own unit;
    fungible elemental resistances are grouped by the set's ``ele_res`` family at match time."""
    explicit = item.get("explicitMods") or []
    mods: dict[str, float | None] = {}
    for raw in explicit:
        clean = clean_mod_text(raw)
        if clean:
            mods.setdefault(mod_key(clean), mod_magnitude(clean))
    return {"class": item_class(item), "rarity": rarity(item), "base": base_type(item),
            "defence": defence_types(item), "mod_count": len(explicit), "mods": mods}


class ArchetypeSetChecker:
    name = "archetype_set"

    def __init__(self, aset: ArchetypeSet):
        self.aset = aset
        self._name_by_id = {a.id: a.name for a in aset.archetypes}

    def check(self, item: dict) -> list[CheckResult]:
        matches = self.aset.match(model_item(item))
        if not matches:
            return []
        # An item is the full "ideal" of some rules and a partial subset of others — surface the
        # top few as separate flag reasons (full matches first), each tagged with coverage. The
        # item's stored score is the best match's value (the first result).
        out: list[CheckResult] = []
        for m in matches[:TOP_K_REASONS]:
            name = self._name_by_id.get(m["archetype"], m["archetype"])
            cov = "full" if m["full"] else f"{m['satisfied']}/{m['required']}"
            out.append(CheckResult(
                f"archetype_set:{m['archetype']}",
                f"{name} · {m['tier']} ({m['value']:.2f}) · {cov}",
                score=m["value"],
            ))
        return out

    def explain(self, item: dict) -> dict:
        """Rich, debuggable breakdown of the score for the detail view: every matched
        archetype with its value/tier, the intrinsic archetype value + components, the base
        grade applied, and per-affix grading (rolled magnitude → tier band, weight)."""
        mi = model_item(item)
        mods = mi["mods"]
        fams = self.aset.mod_families
        matches: list[dict] = []
        for a in self.aset.archetypes:
            scored = a.score(mi, fams)
            if scored is None:
                continue
            reqs = []
            for r in a.requires:
                bands = [{"tier": b.tier, "min": b.min} for b in r.bands]
                if r.family:
                    members = fams[r.family].members if r.family in fams else []
                    present = {m: mods[m] for m in members if m in mods}
                    mag = max((v for v in present.values() if v is not None), default=None)
                    reqs.append({"phrase": r.phrase, "pool": r.family, "min_count": r.min_count,
                                 "present": len(present), "magnitude": mag, "weight": r.weight,
                                 "hybrid": r.hybrid, "tier_value": r.tier_value(mag), "bands": bands})
                else:
                    mag = mods.get(r.key)
                    reqs.append({"phrase": r.phrase, "key": r.key, "magnitude": mag,
                                 "weight": r.weight, "hybrid": r.hybrid,
                                 "tier_value": r.tier_value(mag) if r.key in mods else 0.0,
                                 "bands": bands})
            matches.append({
                **scored,
                "name": self._name_by_id.get(a.id, a.id),
                "archetype_score": round(a.value.score, 3),
                "components": a.value.components,
                "bases_mode": a.bases.mode,
                "requires": reqs,
            })
        matches.sort(key=lambda m: m["value"], reverse=True)
        return {"score": matches[0]["value"] if matches else None,
                "matches": matches, "item": mi}


def build_from_file(path: Path) -> ArchetypeSetChecker:
    """Build the checker from a mined ArchetypeSet YAML."""
    return ArchetypeSetChecker(ArchetypeSet.load(path))

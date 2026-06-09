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
from ..archetype_model import ArchetypeSet, value_to_tier
from ..itemdata import base_type, item_class, rarity

CheckResult = _base.CheckResult

# How many matched archetypes to surface as per-rule contribution reasons under the headline
# (best first), ranked by contribution.
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
        scored = self.aset.score_item(model_item(item))
        matches = scored["matches"]
        if not matches:
            return []
        # The item's stored score is the aggregate **overall** (best rule + a capped breadth
        # bonus from extra distinct matches). The headline carries that score; the top few rules
        # follow as score-less breakdown reasons (each tagged with its contribution + coverage).
        overall = scored["overall"]
        n = len(matches)
        headline = f"Archetype {value_to_tier(overall)} ({overall:.2f})"
        if n > 1:
            headline += f" · {n} matches"
        out: list[CheckResult] = [CheckResult("archetype_set", headline, score=overall)]
        for m in matches[:TOP_K_REASONS]:
            name = self._name_by_id.get(m["archetype"], m["archetype"])
            cov = "full" if m["full"] else f"{m['satisfied']}/{m['required']}"
            note = "" if (m["full"] or m["reachable"]) else " · no open slot"
            out.append(CheckResult(
                f"archetype_set:{m['archetype']}",
                f"{name} · {m['tier']} ({m['contribution']:.2f}) · {cov}{note}",
                score=None,
            ))
        return out

    def explain(self, item: dict) -> dict:
        """Rich, debuggable breakdown for the detail view: the aggregate **overall** (with its
        peak + breadth split) and every matched archetype with its contribution/quality/
        completion, coverage + reachability, the intrinsic archetype value + components, the
        base grade applied, and per-affix grading (rolled magnitude → tier band, weight)."""
        mi = model_item(item)
        mods = mi["mods"]
        fams = self.aset.mod_families
        tw = self.aset.scoring.tier_weights
        by_id = {a.id: a for a in self.aset.archetypes}
        scored = self.aset.score_item(mi)
        matches: list[dict] = []
        for m in scored["matches"]:
            a = by_id.get(m["archetype"])
            reqs = []
            for r in (a.requires if a else []):
                bands = [{"tier": b.tier, "min": b.min} for b in r.bands]
                if r.family:
                    members = fams[r.family].members if r.family in fams else []
                    present = {mm: mods[mm] for mm in members if mm in mods}
                    mag = max((v for v in present.values() if v is not None), default=None)
                    reqs.append({"phrase": r.phrase, "pool": r.family, "min_count": r.min_count,
                                 "present": len(present), "magnitude": mag, "weight": r.weight,
                                 "hybrid": r.hybrid, "tier_value": r.tier_value(mag, tw), "bands": bands})
                else:
                    mag = mods.get(r.key)
                    reqs.append({"phrase": r.phrase, "key": r.key, "magnitude": mag,
                                 "weight": r.weight, "hybrid": r.hybrid,
                                 "tier_value": r.tier_value(mag, tw) if r.key in mods else 0.0,
                                 "bands": bands})
            matches.append({
                **m,
                "name": self._name_by_id.get(m["archetype"], m["archetype"]),
                "archetype_score": round(a.value.score, 3) if a else None,
                "components": a.value.components if a else {},
                "bases_mode": a.bases.mode if a else None,
                "requires": reqs,
            })
        return {"score": scored["overall"], "overall": scored["overall"],
                "tier": value_to_tier(scored["overall"]),
                "peak": scored["peak"], "breadth": scored["breadth"],
                "matches": matches, "targets": self.aset.upgrade_targets(mi), "item": mi}


def build_from_file(path: Path) -> ArchetypeSetChecker:
    """Build the checker from a mined ArchetypeSet YAML."""
    return ArchetypeSetChecker(ArchetypeSet.load(path))

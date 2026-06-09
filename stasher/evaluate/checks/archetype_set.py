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
from ..affix_norm import defence_types
from ..archetype_model import ArchetypeSet, rarity_amp, value_to_tier
from ..itemdata import base_type, explicit_affix_mods, item_class, rarity

CheckResult = _base.CheckResult

# How many matched archetypes to surface as per-rule contribution reasons under the headline
# (best first), ranked by contribution.
TOP_K_REASONS = 3


def model_item(item: dict) -> dict:
    """Project a GGG ``/fetch`` item into the model's input: class/rarity/base, the base's defence
    types (segment gate), explicit affix count (open-slot/craft reasoning), and a
    ``{mod_key: magnitude}`` map (same normalization as the miner). Every affix is its own unit;
    fungible elemental resistances are grouped by the set's ``ele_res`` family at match time.

    Affixes are read per *underlying* affix via :func:`explicit_affix_mods` (de-merging the trade
    site's lossy rendering -- summed lines, split hybrids) so count and magnitudes match what the
    miner mined, not the rendered text."""
    mods, affix_count = explicit_affix_mods(item)
    return {"class": item_class(item), "rarity": rarity(item), "base": base_type(item),
            "defence": defence_types(item), "mod_count": affix_count, "mods": mods}


class ArchetypeSetChecker:
    name = "archetype_set"

    def __init__(self, aset: ArchetypeSet):
        self.aset = aset
        self._name_by_id = {a.id: a.name for a in aset.archetypes}

    def check(self, item: dict) -> list[CheckResult]:
        scored = self.aset.score_item(model_item(item))
        matches = scored["matches"]
        overall = scored["overall"]
        # No archetype match AND no rarity floor → nothing to surface. A floor-only item (a brick
        # carrying a rare, desirable mod) still flags, headlined as a rare-mod find.
        if not matches and overall <= 0:
            return []
        # The item's stored score is the aggregate **overall** (best rule + a capped breadth bonus,
        # or a rare-mod value floor). The headline carries that score; the top few rules follow as
        # score-less breakdown reasons (each tagged with its contribution + coverage).
        n = len(matches)
        headline = f"Archetype {value_to_tier(overall)} ({overall:.2f})"
        if n > 1:
            headline += f" · {n} matches"
        elif not matches:
            headline += " · rare mod"
        # The headline carries the *true* match total (n); only TOP_K per-rule reasons follow, so
        # the count must come from here, not from the surfaced reasons.
        out: list[CheckResult] = [CheckResult("archetype_set", headline, score=overall, count=n)]
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
        # Rarity-floor itemization (debug): the spawn-weight math behind the value floor — only the
        # best **synergy group** (mods co-occurring in one mined archetype) sums, so the popup shows
        # which build won and which trophies actually counted (vs. surfaced-but-uncounted ones).
        sc = self.aset.scoring
        cat = self.aset.mods.get(mi.get("class") or "", {})
        trophies = self.aset.rarity_trophies(mi)
        group_sum, group_arch, group_keys = self.aset.best_synergy_group(mi, trophies)
        gset = set(group_keys)
        rarity_rows = []
        for k, t in trophies.items():
            info = cat.get(k)
            rarity_rows.append({"key": k, "magnitude": mods.get(k), "freq": round(info.mod_frequency(), 4),
                                "amp": round(rarity_amp(info.mod_frequency(), sc), 2),
                                "desirability": round(info.desirability, 2),
                                "tier_quality": round(info.tier_quality(mods.get(k)), 2),
                                "trophy": round(t, 3), "counted": k in gset})
        rarity_rows.sort(key=lambda r: (r["counted"], r["trophy"]), reverse=True)
        return {"score": scored["overall"], "overall": scored["overall"],
                "tier": value_to_tier(scored["overall"]),
                "peak": scored["peak"], "breadth": scored["breadth"],
                "floor": scored.get("floor", 0.0),
                "scoring": sc.to_dict(),
                "rarity": {"rows": rarity_rows, "total": round(group_sum, 3),
                           "build": self._name_by_id.get(group_arch, group_arch)},
                "matches": matches, "targets": self.aset.upgrade_targets(mi), "item": mi}


def build_from_file(path: Path) -> ArchetypeSetChecker:
    """Build the checker from a mined ArchetypeSet YAML."""
    return ArchetypeSetChecker(ArchetypeSet.load(path))

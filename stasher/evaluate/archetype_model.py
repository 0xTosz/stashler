"""The archetype data model — VENDORED from ``archetype_miner/model.py`` (keep byte-identical
bodies). The miner produces these ``ArchetypeSet`` YAML files; Stashler consumes them here at
runtime. It's a vendored copy (not a cross-package import) because the packaged exe only
bundles ``stasher`` and the miner may spin out. The **YAML is the contract**.

Pure-Python (only ``yaml``): dataclasses + YAML (de)serialization + matching/scoring
(``Archetype.matches/score``, ``ArchetypeSet.match``), tunable via the ``Scoring`` block.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Letter base grades → a *mild* 0..1 multiplier. Base quality nudges value rather than
# dominating it: the big armour-vs-evasion distinction is handled by the defence *gate*, so
# within-family base quality (and unknown/exotic bases) only shifts the score a little.
GRADE_FACTOR = {"S": 1.0, "A": 0.95, "B": 0.90, "C": 0.85, "D": 0.80}
UNKNOWN_BASE = 0.85  # base not graded (incl. under-sampled exotic bases) — lenient, not punishing
MAX_AFFIXES = {"Magic": 2, "Rare": 6}  # explicit-affix ceiling, for open-slot (craft) reasoning
MAX_PREFIX = MAX_SUFFIX = 3  # a rare holds ≤3 prefixes + ≤3 suffixes (the craft target)
TIER_CUTS = [("S", 0.80), ("A", 0.62), ("B", 0.45), ("C", 0.28), ("D", 0.0)]

# How strongly the rolled tier drives value, and how steeply tier maps to quality. Tunable per
# set (Rules page). The tier curve is deliberately steep (a T1 roll ≫ T2/T3) and the roll
# influence is high — 3× T1 should beat 4× T2.
DEFAULT_ROLL_INFLUENCE = 0.8
DEFAULT_MAGIC_ROLL_INFLUENCE = 0.95   # magic has ≤2 affixes → only the best are worth it
DEFAULT_TIER_WEIGHTS = {"T1": 1.0, "T2": 0.5, "T3": 0.2, "below": 0.05}

# Crafting-aware scoring. A rule is the full "ideal" item; a per-rule **contribution** =
# quality × completion, where completion = coverage + (1-coverage)·craft_credit for a *reachable*
# partial (missing affixes fit open prefix/suffix slots) — i.e. an item you can finish into the
# rule scores closer to full; a non-reachable partial keeps just its realized coverage.
DEFAULT_PARTIAL_THRESHOLD = 0.6   # min coverage to flag a partial (3/4, 4/5, 4/6); below → no flag
DEFAULT_CRAFT_CREDIT = 0.4        # value of a reachable-but-missing affix vs actually having it
DEFAULT_BREADTH_CAP = 0.3         # max boost a base gets from matching many *distinct* rules
HYBRID_PREMIUM = 1.15             # weight multiplier for a hybrid (one-slot, two-stat) requirement
# Magic items are evaluated against the same rare templates as **craft bases**: they can satisfy
# only ≤2 of a 4+ unit rule but have open slots to craft the rest. They count only if every kept
# mod is high-tier (≥ T2) — a magic base is worth crafting on only when its rolls are good.
MAGIC_MIN_TV = DEFAULT_TIER_WEIGHTS["T2"]


def value_to_tier(score: float) -> str:
    for tier, cut in TIER_CUTS:
        if score >= cut:
            return tier
    return "D"


@dataclass
class TierBand:
    tier: str
    min: float

    def to_dict(self) -> dict:
        return {"tier": self.tier, "min": self.min}


@dataclass
class ModReq:
    """One requirement in an archetype: a specific ``mod`` (key) or an ``any_of`` family pool.

    ``mag`` stats + ``bands`` describe the magnitude distribution among matching items;
    ``weight`` is the significance (consistently high-rolled mods weigh more in scoring)."""

    phrase: str
    key: str | None = None          # specific mod key; None when this is a family pool
    family: str | None = None       # mod-family id (fungible pool, e.g. ele_res) — vs key
    min_count: int = 1              # pool: how many members (affix slots) must be present
    hybrid: bool = False            # one slot, two stats (slot-efficient) → premium
    weight: float = 1.0
    p50: float | None = None
    p90: float | None = None
    max: float | None = None
    bands: list[TierBand] = field(default_factory=list)

    def units(self) -> int:
        """Affix slots this requirement represents — a family pool needs ``min_count`` slots."""
        return self.min_count if self.family else 1

    def to_dict(self) -> dict:
        mag = {"weight": round(self.weight, 3)}
        for k in ("p50", "p90", "max"):
            v = getattr(self, k)
            if v is not None:
                mag[k] = round(v, 2)
        if self.bands:
            mag["bands"] = [b.to_dict() for b in self.bands]
        d: dict = {"phrase": self.phrase, "mag": mag}
        if self.family:
            d["any_of"] = self.family
            if self.min_count != 1:
                d["min"] = self.min_count
        else:
            d["mod"] = self.key
        if self.hybrid:
            d["hybrid"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ModReq":
        mag = d.get("mag") or {}
        bands = [TierBand(b["tier"], float(b["min"])) for b in mag.get("bands", [])]
        return cls(
            phrase=d.get("phrase", ""),
            key=d.get("mod"),
            family=d.get("any_of"),
            min_count=int(d.get("min", 1)),
            hybrid=bool(d.get("hybrid", False)),
            weight=float(mag.get("weight", 1.0)),
            p50=mag.get("p50"), p90=mag.get("p90"), max=mag.get("max"),
            bands=bands,
        )

    def tier_value(self, magnitude: float | None,
                   tier_weights: dict[str, float] | None = None) -> float:
        """Map a rolled magnitude to a 0..1 quality via the (tunable) per-tier weights.

        The band whose ``min`` the roll clears gives its tier weight (T1 ≫ T2/T3 by default);
        below the lowest band → the ``below`` weight. No bands or no magnitude (a flag mod) → 1.0."""
        tw = tier_weights or DEFAULT_TIER_WEIGHTS
        if not self.bands or magnitude is None:
            return 1.0
        for b in sorted(self.bands, key=lambda x: x.min, reverse=True):
            if magnitude >= b.min:
                return tw.get(b.tier, 1.0)
        return tw.get("below", 0.05)


@dataclass
class BaseGrading:
    """How an archetype treats the item's base.

    ``defence`` (e.g. ``["armour"]``) is a **gate** for defence-bearing classes: the item's
    own defence types must match exactly, so an armour archetype never fires on an evasion
    base. ``grades`` then nudges value by the specific base's quality (a mild multiplier).
    ``defence`` empty = no gate (defenceless classes like rings/amulets, or universal
    archetypes): base is a pure soft multiplier and unknown/exotic bases stay lenient.
    """

    mode: str = "baseless"                      # "graded" | "baseless"
    grades: dict[str, str] = field(default_factory=dict)
    family: str | None = None
    defence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        if self.mode == "baseless":
            return {"mode": "baseless"}
        d: dict = {"mode": "graded", "grades": dict(self.grades)}
        if self.family:
            d["family"] = self.family
        if self.defence:
            d["defence"] = list(self.defence)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "BaseGrading":
        return cls(mode=d.get("mode", "baseless"),
                   grades={str(k): str(v) for k, v in (d.get("grades") or {}).items()},
                   family=d.get("family"),
                   defence=list(d.get("defence") or []))

    def factor_for(self, base: str | None) -> float:
        if self.mode == "baseless":
            return 1.0
        if base and base in self.grades:
            return GRADE_FACTOR.get(self.grades[base], UNKNOWN_BASE)
        return UNKNOWN_BASE


@dataclass
class ArchetypeValue:
    score: float = 0.0
    tier: str = "D"
    components: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"score": round(self.score, 3), "tier": self.tier,
                "components": {k: round(v, 3) for k, v in self.components.items()}}

    @classmethod
    def from_dict(cls, d: dict) -> "ArchetypeValue":
        return cls(score=float(d.get("score", 0.0)), tier=d.get("tier", "D"),
                   components=dict(d.get("components") or {}))


@dataclass
class Scoring:
    """Tunable scoring knobs, stored per set and editable on the Rules page."""

    roll_influence: float = DEFAULT_ROLL_INFLUENCE
    magic_roll_influence: float = DEFAULT_MAGIC_ROLL_INFLUENCE
    tier_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TIER_WEIGHTS))
    partial_threshold: float = DEFAULT_PARTIAL_THRESHOLD
    craft_credit: float = DEFAULT_CRAFT_CREDIT
    breadth_cap: float = DEFAULT_BREADTH_CAP

    def to_dict(self) -> dict:
        return {"roll_influence": round(self.roll_influence, 3),
                "magic_roll_influence": round(self.magic_roll_influence, 3),
                "tier_weights": dict(self.tier_weights),
                "partial_threshold": round(self.partial_threshold, 3),
                "craft_credit": round(self.craft_credit, 3),
                "breadth_cap": round(self.breadth_cap, 3)}

    @classmethod
    def from_dict(cls, d: dict) -> "Scoring":
        d = d or {}
        tw = {str(k): float(v) for k, v in (d.get("tier_weights") or {}).items()}
        return cls(roll_influence=float(d.get("roll_influence", DEFAULT_ROLL_INFLUENCE)),
                   magic_roll_influence=float(d.get("magic_roll_influence", DEFAULT_MAGIC_ROLL_INFLUENCE)),
                   tier_weights=tw or dict(DEFAULT_TIER_WEIGHTS),
                   partial_threshold=float(d.get("partial_threshold", DEFAULT_PARTIAL_THRESHOLD)),
                   craft_credit=float(d.get("craft_credit", DEFAULT_CRAFT_CREDIT)),
                   breadth_cap=float(d.get("breadth_cap", DEFAULT_BREADTH_CAP)))


@dataclass
class PriceInfo:
    """A read-only price estimate produced by the (deferred) pricing module. Distilled from
    trade listings; ``inherited`` marks a value carried down from a parent set as a fallback."""

    value: float
    currency: str = "chaos"
    n_samples: int = 0
    confidence: float = 0.0
    sampled_at: str | None = None
    run_id: int | None = None
    inherited: bool = False

    def to_dict(self) -> dict:
        d: dict = {"value": round(self.value, 3), "currency": self.currency,
                   "n_samples": self.n_samples, "confidence": round(self.confidence, 3)}
        if self.sampled_at:
            d["sampled_at"] = self.sampled_at
        if self.run_id is not None:
            d["run_id"] = self.run_id
        if self.inherited:
            d["inherited"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PriceInfo":
        return cls(value=float(d.get("value", 0.0)), currency=d.get("currency", "chaos"),
                   n_samples=int(d.get("n_samples", 0)), confidence=float(d.get("confidence", 0.0)),
                   sampled_at=d.get("sampled_at"), run_id=d.get("run_id"),
                   inherited=bool(d.get("inherited", False)))


@dataclass
class Lineage:
    """Where an archetype came from in the price-feedback loop, for the cross-run state machine
    (see ``pricing.resolve_action``). ``parent_price`` is a *sticky* reference inherited when a
    set is enlarged/split — a fallback price for a child that never gets a definite one of its
    own; it is never overwritten by the child's own pricing."""

    run_id: int | None = None
    origin: str = "mined"            # mined | enlarged | split
    parent_id: str | None = None
    op_run_id: int | None = None     # run in which the op that produced this set happened
    status: str = "active"           # active | frozen | paused | retired
    parent_price: PriceInfo | None = None

    def is_default(self) -> bool:
        return (self.run_id is None and self.origin == "mined" and self.parent_id is None
                and self.op_run_id is None and self.status == "active"
                and self.parent_price is None)

    def to_dict(self) -> dict:
        d: dict = {"origin": self.origin, "status": self.status}
        if self.run_id is not None:
            d["run_id"] = self.run_id
        if self.parent_id:
            d["parent_id"] = self.parent_id
        if self.op_run_id is not None:
            d["op_run_id"] = self.op_run_id
        if self.parent_price is not None:
            d["parent_price"] = self.parent_price.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Lineage":
        pp = d.get("parent_price")
        return cls(run_id=d.get("run_id"), origin=d.get("origin", "mined"),
                   parent_id=d.get("parent_id"), op_run_id=d.get("op_run_id"),
                   status=d.get("status", "active"),
                   parent_price=PriceInfo.from_dict(pp) if pp else None)


@dataclass
class Archetype:
    id: str
    name: str
    requires: list[ModReq]
    item_class: str | None = None
    rarity: list[str] = field(default_factory=lambda: ["Rare"])
    bases: BaseGrading = field(default_factory=BaseGrading)
    value: ArchetypeValue = field(default_factory=ArchetypeValue)
    subset_of: list[str] = field(default_factory=list)
    superset_of: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    enabled: bool = True
    price: PriceInfo | None = None
    lineage: Lineage = field(default_factory=Lineage)

    def mod_keys(self) -> frozenset[str]:
        """The specific mod keys this archetype is built on (pools/aggregates excluded)."""
        return frozenset(r.key for r in self.requires if r.key)

    def signature(self) -> tuple[str, ...]:
        """Canonical identity of this archetype's requirement set — one token per requirement
        (family or mod key). Used for dedup and the pricing loop's ``seen`` history."""
        return tuple(sorted(r.family or r.key or r.phrase for r in self.requires))

    def to_dict(self) -> dict:
        d = {
            "id": self.id, "name": self.name, "item_class": self.item_class,
            "rarity": list(self.rarity),
            "requires": [r.to_dict() for r in self.requires],
            "bases": self.bases.to_dict(),
            "value": self.value.to_dict(),
            "relations": {"subset_of": self.subset_of, "superset_of": self.superset_of},
            "provenance": self.provenance,
        }
        if not self.enabled:
            d["enabled"] = False
        if self.price is not None:
            d["price"] = self.price.to_dict()
        if not self.lineage.is_default():
            d["lineage"] = self.lineage.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Archetype":
        rel = d.get("relations") or {}
        return cls(
            id=d["id"], name=d.get("name", d["id"]),
            requires=[ModReq.from_dict(r) for r in d.get("requires", [])],
            item_class=d.get("item_class"),
            rarity=list(d.get("rarity") or ["Rare"]),
            bases=BaseGrading.from_dict(d.get("bases") or {}),
            value=ArchetypeValue.from_dict(d.get("value") or {}),
            subset_of=list(rel.get("subset_of") or []),
            superset_of=list(rel.get("superset_of") or []),
            provenance=dict(d.get("provenance") or {}),
            enabled=bool(d.get("enabled", True)),
            price=PriceInfo.from_dict(d["price"]) if d.get("price") else None,
            lineage=Lineage.from_dict(d.get("lineage") or {}),
        )

    # --- matching / scoring (operates on an item dict) ------------------

    def _req_state(self, req: ModReq, mods: dict[str, float | None],
                   families: dict[str, "ModFamily"], tier_weights=None) -> tuple[bool, float]:
        """(satisfied, tier_value) for one requirement against an item's ``mods`` map. A family
        pool (e.g. fungible elemental res) needs ``min_count`` distinct members present; its tier
        is the mean of the best ``min_count`` members (so spreading thin tiers low)."""
        if req.family:
            members = families[req.family].members if req.family in families else []
            present = sorted((req.tier_value(mods[m], tier_weights) for m in members if m in mods),
                             reverse=True)
            if len(present) < req.min_count:
                return False, 0.0
            top = present[:req.min_count]
            return True, sum(top) / len(top)
        if req.key in mods:
            return True, req.tier_value(mods[req.key], tier_weights)
        return False, 0.0

    def _gates_ok(self, item: dict) -> bool:
        """Class / rarity / base-defence gates (independent of which mods are present)."""
        if self.item_class and item.get("class") and item["class"] != self.item_class:
            return False
        if self.rarity and item.get("rarity") and item["rarity"] not in self.rarity:
            return False
        if self.bases.defence and tuple(item.get("defence") or ()) != tuple(self.bases.defence):
            return False
        return True

    def matches(self, item: dict, families: dict[str, "ModFamily"] | None = None) -> bool:
        """Full match: gates pass and every required mod/pool is present."""
        fams = families or {}
        if not self._gates_ok(item):
            return False
        mods = item.get("mods") or {}
        return all(self._req_state(r, mods, fams)[0] for r in self.requires)

    def unit_count(self) -> int:
        """Affix slots this archetype requires (a family pool counts ``min_count`` slots)."""
        return sum(r.units() for r in self.requires)

    def _craft_units(self, mods, families, tier_weights) -> int:
        """High-tier (≥ T2) affix slots the item already fills toward this archetype — used for
        magic **craft-base** admission. A *partially*-filled fungible family still counts its
        present high rolls (capped at the requirement's ``min_count``): e.g. life + one high res
        fills 2 slots of a `life + 2-res + …` rule even though the res family isn't complete."""
        total = 0
        for r in self.requires:
            if r.family:
                members = families[r.family].members if r.family in families else []
                hi = sum(1 for m in members
                         if m in mods and r.tier_value(mods[m], tier_weights) >= MAGIC_MIN_TV)
                total += min(hi, r.min_count)
            elif r.key in mods and r.tier_value(mods[r.key], tier_weights) >= MAGIC_MIN_TV:
                total += 1
        return total

    def _slot_demand(self, mods, families, affix_slots) -> tuple[int, int, list[dict]]:
        """Open prefix/suffix counts on the item + the list of unsatisfied requirements with the
        slot ``kind`` each needs. Shared by ``_slot_reachable`` (a boolean gate) and
        ``upgrade_target`` (which surfaces the missing affixes). ``kind`` is ``prefix``/``suffix``/
        ``both`` (from ``affix_slots``) or ``None`` when the mod is unclassified. Each missing slot
        of a partially-filled family becomes its own ``missing`` entry."""
        used_p = sum(1 for k in mods if affix_slots.get(k) == "prefix")
        used_s = sum(1 for k in mods if affix_slots.get(k) == "suffix")
        missing: list[dict] = []
        for r in self.requires:
            if r.family:
                members = families[r.family].members if r.family in families else []
                gap = max(0, r.min_count - sum(1 for m in members if m in mods))
                kind = next((affix_slots[m] for m in members if m in affix_slots), None)
                for _ in range(gap):
                    missing.append({"phrase": r.phrase, "family": r.family, "kind": kind})
            elif r.key not in mods:
                missing.append({"phrase": r.phrase, "key": r.key, "kind": affix_slots.get(r.key)})
        return MAX_PREFIX - used_p, MAX_SUFFIX - used_s, missing

    @staticmethod
    def _missing_fits(open_p: int, open_s: int, missing: list[dict]) -> bool:
        """Whether every missing requirement fits an open slot of its own type — fixed prefix/
        suffix needs must fit their pool; flexible ``both`` needs take the leftover of either pool.
        Unclassified (``kind`` None) needs never force a false negative (conservative)."""
        need_p = sum(1 for m in missing if m["kind"] == "prefix")
        need_s = sum(1 for m in missing if m["kind"] == "suffix")
        need_either = sum(1 for m in missing if m["kind"] == "both")
        return (need_p <= open_p and need_s <= open_s
                and need_either <= (open_p - need_p) + (open_s - need_s))

    def _slot_reachable(self, mods, families, affix_slots) -> bool:
        """Whether the missing requirements can be **crafted into open slots of the right type** —
        a rare holds ≤3 prefixes + ≤3 suffixes, so a missing prefix needs an open *prefix* slot.
        Conservative: only known-type present mods count as used and only known-type missing
        requirements count as needed, so an unclassified mod never forces a false discard. Empty
        ``affix_slots`` (older sets) → always reachable."""
        if not affix_slots:
            return True
        open_p, open_s, missing = self._slot_demand(mods, families, affix_slots)
        return self._missing_fits(open_p, open_s, missing)

    def score(self, item: dict, families: dict[str, "ModFamily"] | None = None,
              scoring: "Scoring | None" = None,
              affix_slots: dict[str, str] | None = None) -> dict | None:
        """One rule's **contribution** for an item, or None if it doesn't meaningfully match.

        ``contribution = quality × completion``:
        * **quality** = the rule's base value × a tier-weighted roll blend × base grade — the
          worth of the affixes the item actually has (tiers weigh heavily; magic leans harder).
        * **completion** = ``coverage + (1-coverage)·craft_credit`` when the missing affixes are
          **reachable** (fit open prefix/suffix slots) — so a finishable partial scores near a
          full match; a non-reachable partial keeps just its realized ``coverage`` (still good,
          no upgrade credit). A full match → completion 1.
        Below the partial floor (and magic needing ≥2 high-tier kept affixes) → no match."""
        sc = scoring or Scoring()
        fams = families or {}
        if not self._gates_ok(item):
            return None
        mods = item.get("mods") or {}
        n = len(self.requires)
        if n == 0:
            return None
        sat = sat_units = 0
        wsum = tvsum = 0.0
        per_mod = []
        for r in self.requires:
            ok, tv = self._req_state(r, mods, fams, sc.tier_weights)
            w = r.weight * (HYBRID_PREMIUM if r.hybrid else 1.0)
            wsum += w
            if ok:
                sat += 1
                sat_units += r.units()
                tvsum += w * tv
            per_mod.append({"mod": r.key or r.family, "satisfied": ok,
                            "hybrid": r.hybrid, "tier_value": round(tv, 3)})
        full = sat == n
        is_magic = item.get("rarity") == "Magic"
        craft_max = MAX_AFFIXES["Rare"]      # the finished rare you craft toward
        free = max(0, craft_max - int(item.get("affix_count", item.get("mod_count", sat_units))))
        if not full:
            if is_magic:
                # a craft base needs ≥2 high-tier (≥T2) kept affixes (only the best 2 are worth it)
                if self._craft_units(mods, fams, sc.tier_weights) < 2:
                    return None
            elif sat < max(2, math.ceil(sc.partial_threshold * n)):
                return None
        coverage = sat / n
        # reachability gates the *upgrade* (completion) credit, not the match — a 5/6 with no open
        # slot for the missing affix is still a good item, it just can't be finished into the rule.
        reachable = full or self._slot_reachable(mods, fams, affix_slots)
        completion = coverage + (1.0 - coverage) * (sc.craft_credit if reachable else 0.0)
        mod_score = (tvsum / wsum) if (wsum and sat) else 0.0
        roll = sc.magic_roll_influence if is_magic else sc.roll_influence
        base_factor = self.bases.factor_for(item.get("base"))
        quality = self.value.score * ((1 - roll) + roll * mod_score) * base_factor
        contribution = max(0.0, min(1.0, quality * completion))
        return {
            "archetype": self.id, "value": round(contribution, 3),
            "contribution": round(contribution, 3), "quality": round(quality, 3),
            "completion": round(completion, 3), "tier": value_to_tier(contribution),
            "coverage": round(coverage, 3), "satisfied": sat, "required": n, "full": full,
            "reachable": reachable, "free_slots": free,
            "mod_score": round(mod_score, 3), "base_factor": round(base_factor, 3),
            "base_grade": self.bases.grades.get(item.get("base", ""), None),
            "mods": per_mod,
        }

    def upgrade_target(self, item: dict, families: dict[str, "ModFamily"] | None = None,
                       affix_slots: dict[str, str] | None = None) -> dict | None:
        """A *craftable* target: a rule this item isn't a full match for yet but **could be**, by
        crafting its missing affixes into open slots. Unlike ``score`` (which gates on the partial
        floor and grades current rolls), this is a forward-looking "what it can become" — surfaced
        in the popup's upgrade-paths tab. Returns ``None`` unless gates pass, the item already
        satisfies ≥1 requirement, isn't full, and **every** missing affix fits an open slot of its
        type. The payload names the missing affixes + open prefix/suffix counts and the rule's
        finished value/tier (the worth of completing it)."""
        fams = families or {}
        if not self._gates_ok(item):
            return None
        mods = item.get("mods") or {}
        n = len(self.requires)
        if n == 0:
            return None
        sat = sum(1 for r in self.requires if self._req_state(r, mods, fams)[0])
        if sat == 0 or sat == n:                       # nothing to anchor on / already full
            return None
        open_p, open_s, missing = self._slot_demand(mods, fams, affix_slots or {})
        if not self._missing_fits(open_p, open_s, missing):
            return None
        return {
            "archetype": self.id, "name": self.name, "units": n,
            "satisfied": sat, "required": n,
            "missing": missing, "open_prefix": open_p, "open_suffix": open_s,
            "value": round(self.value.score, 3), "tier": self.value.tier,
        }


@dataclass
class ModFamily:
    id: str
    name: str
    members: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "members": self.members}


@dataclass
class BaseFamily:
    id: str
    name: str
    item_class: str | None = None
    defence: list[str] = field(default_factory=list)
    members: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "item_class": self.item_class,
                "defence": self.defence, "members": self.members}


@dataclass
class ArchetypeSet:
    meta: dict = field(default_factory=dict)
    scoring: Scoring = field(default_factory=Scoring)
    mod_families: dict[str, ModFamily] = field(default_factory=dict)
    base_families: dict[str, BaseFamily] = field(default_factory=dict)
    affix_slots: dict[str, str] = field(default_factory=dict)  # mod_key -> prefix|suffix
    archetypes: list[Archetype] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "meta": self.meta,
            "scoring": self.scoring.to_dict(),
            "mod_families": {k: v.to_dict() for k, v in self.mod_families.items()},
            "base_families": {k: v.to_dict() for k, v in self.base_families.items()},
            "affix_slots": dict(self.affix_slots),
            "archetypes": [a.to_dict() for a in self.archetypes],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ArchetypeSet":
        return cls(
            meta=dict(d.get("meta") or {}),
            scoring=Scoring.from_dict(d.get("scoring") or {}),
            mod_families={k: ModFamily(k, v.get("name", k), list(v.get("members") or []))
                          for k, v in (d.get("mod_families") or {}).items()},
            base_families={k: BaseFamily(k, v.get("name", k), v.get("item_class"),
                                         list(v.get("defence") or []), list(v.get("members") or []))
                           for k, v in (d.get("base_families") or {}).items()},
            affix_slots={str(k): str(v) for k, v in (d.get("affix_slots") or {}).items()},
            archetypes=[Archetype.from_dict(a) for a in d.get("archetypes") or []],
        )

    def save(self, path: str | Path, *, header: str = "") -> None:
        text = yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True, width=100)
        Path(path).write_text(header + text, encoding="utf-8", newline="\n")

    @classmethod
    def load(cls, path: str | Path) -> "ArchetypeSet":
        return cls.from_dict(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})

    @classmethod
    def loads(cls, text: str) -> "ArchetypeSet":
        return cls.from_dict(yaml.safe_load(text) or {})

    def match(self, item: dict) -> list[dict]:
        """Enabled archetypes the item matches, each scored (with this set's ``scoring``) and
        ranked by contribution (desc). This is the **per-rule** list; ``score_item`` aggregates
        it into a single headline score."""
        out = []
        for a in self.archetypes:
            if not a.enabled:
                continue
            s = a.score(item, self.mod_families, self.scoring, self.affix_slots)
            if s is not None:
                out.append(s)
        out.sort(key=lambda s: s["value"], reverse=True)
        return out

    def score_item(self, item: dict) -> dict:
        """Aggregate every matching rule into one headline score + the per-rule breakdown.

        ``overall`` is **peak-dominated**: the single best rule's contribution is the floor (an
        exact match to a complex 5/6-unit pattern is top-tier on its own), and *additional
        distinct* matches add a capped ``breadth`` bonus on top — so a universal base matching
        many rules outranks one with a single upgrade path, but breadth can never overtake a
        strong peak. ``overall = peak + (1-peak)·breadth``, breadth ∈ [0, ``breadth_cap``], with
        each extra rule weighted by its **novelty** (the share of its requirement units not
        already covered by a higher-ranked match) so near-duplicate rules don't inflate it.

        Returns ``{"overall", "peak", "breadth", "matches"}`` (``matches`` is the ranked
        per-rule list from ``match``); an item that matches nothing scores ``overall`` 0."""
        by_id = {a.id: a for a in self.archetypes}
        matches = self.match(item)
        if not matches:
            return {"overall": 0.0, "peak": 0.0, "breadth": 0.0, "matches": []}
        peak = matches[0]["contribution"]
        seen: set[str] = set()
        top = by_id.get(matches[0]["archetype"])
        if top is not None:
            seen.update(top.signature())
        breadth = 0.0
        for m in matches[1:]:
            a = by_id.get(m["archetype"])
            units = set(a.signature()) if a is not None else set()
            novelty = (len(units - seen) / len(units)) if units else 0.0
            breadth += novelty * m["contribution"]
            seen |= units
        breadth = min(self.scoring.breadth_cap, breadth)
        overall = peak + (1.0 - peak) * breadth
        return {"overall": round(overall, 3), "peak": round(peak, 3),
                "breadth": round(breadth, 3), "matches": matches}

    def upgrade_targets(self, item: dict) -> list[dict]:
        """Enabled rules this item could be **crafted into** (every missing affix fits an open
        slot of its type) but isn't a full match for yet — the popup's *upgrade paths*. Ranked by
        finished ``value`` then unit count, so the richest 5/6–6/6 targets lead."""
        out = []
        for a in self.archetypes:
            if not a.enabled:
                continue
            t = a.upgrade_target(item, self.mod_families, self.affix_slots)
            if t is not None:
                out.append(t)
        out.sort(key=lambda t: (t["value"], t["units"]), reverse=True)
        return out

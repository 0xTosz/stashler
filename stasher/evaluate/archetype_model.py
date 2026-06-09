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
TIER_CUTS = [("S", 0.75), ("A", 0.55), ("B", 0.40), ("C", 0.25), ("D", 0.0)]

# How steeply tier maps to quality, and how much *craftable upside* to credit. Tunable per set
# (Rules page). The tier curve is deliberately steep (a T1 roll ≫ T2/T3). ``roll_influence`` is
# the weight of the open-slot craft bonus: a finished item is scored on its rolls alone (tiers
# fully drive), and only open slots on a base whose kept rolls are good earn a lift toward the
# craftable ceiling — so a bricked full item can't hide behind its archetype value.
DEFAULT_ROLL_INFLUENCE = 0.8
DEFAULT_TIER_WEIGHTS = {"T1": 1.0, "T2": 0.5, "T3": 0.2, "below": 0.05}
DEFAULT_CRAFT_TARGET = 6              # affix slots a finished rare crafts toward (open-slot headroom)

# Crafting-aware scoring. A rule is the full "ideal" item; a per-rule **contribution** =
# quality × completion, where completion = coverage + (1-coverage)·craft_credit for a *reachable*
# partial (missing affixes fit open prefix/suffix slots) — i.e. an item you can finish into the
# rule scores closer to full; a non-reachable partial keeps just its realized coverage.
DEFAULT_PARTIAL_THRESHOLD = 0.6   # min coverage to flag a partial (3/4, 4/5, 4/6); below → no flag
DEFAULT_CRAFT_CREDIT = 0.4        # value of a reachable-but-missing affix vs actually having it
DEFAULT_MAGIC_COMPLETION = 0.95   # a magic craft base's completion (open slots = upside); <1 so a
                                  # finished rare still edges an equivalent 2-affix base
DEFAULT_MAGIC_SOLO = 0.55         # lock-in factor for a magic with a single T1 affix (vs a 2-affix
                                  # base): the empty slot is cheap to augment but the 2nd good roll
                                  # still isn't guaranteed, so a solo premium base lands a "potential"
                                  # tier (B/C) below a finished item or a genuine two-high-tier base
DEFAULT_BREADTH_CAP = 0.3         # max boost a base gets from matching many *distinct* rules
HYBRID_PREMIUM = 1.15             # weight multiplier for a hybrid (one-slot, two-stat) requirement

# Mod-rarity premium. Rarity = a mod's **presence frequency** = sum of its tier spawn weights /
# the pool (total spawn weight of its prefix/suffix track) = the chance an affix of that type *is*
# this mod, any tier. Common mods are frequent (life ≈0.12, fire-res ≈0.08), chase mods scarce
# (spirit ≈0.03, +proj-levels ≈0.009). This is **roll-independent** (the roll is scored separately
# by ``tier_quality``), so a high roll of a *common* mod earns no rarity premium. A *rare* **and**
# desirable mod massively boosts an item — a bricked spirit/+proj amulet is still a chase item. The
# item gets a rarity **floor**: the premium (amp−1) of its rare-desirable mods saturates toward 1,
# and the score is ``max(archetype_score, floor)``.
DEFAULT_RARITY_REF = 0.1          # reference "common-mod" presence frequency (mods at/above → amp 1)
DEFAULT_RARITY_GAMMA = 0.7        # amplification exponent: amp = (ref/freq) ** gamma
DEFAULT_RARITY_CAP = 7.0          # max rarity multiplier for an ultra-rare mod
DEFAULT_RARITY_FLOOR_SCALE = 2.8  # saturation scale: smaller ⇒ a rare mod alone reaches a high floor


def rarity_amp(freq: float, sc: "Scoring") -> float:
    """Amplifying rarity multiplier for a mod of presence ``freq`` (Σtier weights / pool): ~1× at/
    above ``rarity_ref`` (common), growing to ``rarity_cap`` for an ultra-rare mod. ``freq`` 0 → 1×."""
    if freq <= 0:
        return 1.0
    return max(1.0, min(sc.rarity_cap, (sc.rarity_ref / freq) ** sc.rarity_gamma))
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
    tier_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TIER_WEIGHTS))
    partial_threshold: float = DEFAULT_PARTIAL_THRESHOLD
    craft_credit: float = DEFAULT_CRAFT_CREDIT
    craft_target: int = DEFAULT_CRAFT_TARGET
    magic_completion: float = DEFAULT_MAGIC_COMPLETION
    magic_solo: float = DEFAULT_MAGIC_SOLO
    breadth_cap: float = DEFAULT_BREADTH_CAP
    rarity_ref: float = DEFAULT_RARITY_REF
    rarity_gamma: float = DEFAULT_RARITY_GAMMA
    rarity_cap: float = DEFAULT_RARITY_CAP
    rarity_floor_scale: float = DEFAULT_RARITY_FLOOR_SCALE

    def to_dict(self) -> dict:
        return {"roll_influence": round(self.roll_influence, 3),
                "tier_weights": dict(self.tier_weights),
                "partial_threshold": round(self.partial_threshold, 3),
                "craft_credit": round(self.craft_credit, 3),
                "craft_target": int(self.craft_target),
                "magic_completion": round(self.magic_completion, 3),
                "magic_solo": round(self.magic_solo, 3),
                "breadth_cap": round(self.breadth_cap, 3),
                "rarity_ref": round(self.rarity_ref, 5),
                "rarity_gamma": round(self.rarity_gamma, 3),
                "rarity_cap": round(self.rarity_cap, 3),
                "rarity_floor_scale": round(self.rarity_floor_scale, 3)}

    @classmethod
    def from_dict(cls, d: dict) -> "Scoring":
        d = d or {}
        tw = {str(k): float(v) for k, v in (d.get("tier_weights") or {}).items()}
        return cls(roll_influence=float(d.get("roll_influence", DEFAULT_ROLL_INFLUENCE)),
                   tier_weights=tw or dict(DEFAULT_TIER_WEIGHTS),
                   partial_threshold=float(d.get("partial_threshold", DEFAULT_PARTIAL_THRESHOLD)),
                   craft_credit=float(d.get("craft_credit", DEFAULT_CRAFT_CREDIT)),
                   craft_target=int(d.get("craft_target", DEFAULT_CRAFT_TARGET)),
                   magic_completion=float(d.get("magic_completion", DEFAULT_MAGIC_COMPLETION)),
                   magic_solo=float(d.get("magic_solo", DEFAULT_MAGIC_SOLO)),
                   breadth_cap=float(d.get("breadth_cap", DEFAULT_BREADTH_CAP)),
                   rarity_ref=float(d.get("rarity_ref", DEFAULT_RARITY_REF)),
                   rarity_gamma=float(d.get("rarity_gamma", DEFAULT_RARITY_GAMMA)),
                   rarity_cap=float(d.get("rarity_cap", DEFAULT_RARITY_CAP)),
                   rarity_floor_scale=float(d.get("rarity_floor_scale", DEFAULT_RARITY_FLOOR_SCALE)))


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

    def _craft_units(self, mods, families, tier_weights, threshold: float = MAGIC_MIN_TV) -> int:
        """Affix slots the item fills toward this archetype at tier value ``>= threshold`` — used for
        magic **craft-base** admission (default ``MAGIC_MIN_TV`` = the ≥T2 count; pass the T1 weight
        for the count of *perfect* kept affixes). A *partially*-filled fungible family still counts
        its present high rolls (capped at ``min_count``): e.g. life + one high res fills 2 slots of a
        `life + 2-res + …` rule even though the res family isn't complete."""
        total = 0
        for r in self.requires:
            if r.family:
                members = families[r.family].members if r.family in families else []
                hi = sum(1 for m in members
                         if m in mods and r.tier_value(mods[m], tier_weights) >= threshold)
                total += min(hi, r.min_count)
            elif r.key in mods and r.tier_value(mods[r.key], tier_weights) >= threshold:
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
        * **quality** = the rule's base value × a roll/craft blend × base grade. A finished item
          (no open slots) is scored on its rolled tiers alone; open slots add a craft bonus
          (weighted by ``roll_influence``) only when the kept rolls are good — so a full brick
          can't coast on its archetype value, and crafting onto trash tiers isn't rewarded.
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
        wsum = sat_wsum = tvsum = 0.0
        per_mod = []
        for r in self.requires:
            ok, tv = self._req_state(r, mods, fams, sc.tier_weights)
            w = r.weight * (HYBRID_PREMIUM if r.hybrid else 1.0)
            wsum += w
            if ok:
                sat += 1
                sat_units += r.units()
                sat_wsum += w
                tvsum += w * tv
            per_mod.append({"mod": r.key or r.family, "satisfied": ok,
                            "hybrid": r.hybrid, "tier_value": round(tv, 3)})
        full = sat == n
        is_magic = item.get("rarity") == "Magic"
        craft_max = MAX_AFFIXES["Rare"]      # the finished rare you craft toward
        free = max(0, craft_max - int(item.get("affix_count", item.get("mod_count", sat_units))))
        # Magic craft-base admission: two decent (≥T2) kept affixes, OR a single **T1** affix — one
        # perfect mod is a worthwhile base since augmenting the empty slot in-game is virtually free
        # (the locked premium roll is the hard part). ``hi`` (≥T2 count) also sets the lock-in below.
        hi = self._craft_units(mods, fams, sc.tier_weights) if is_magic else 0
        if not full:
            if is_magic:
                if hi < 2 and self._craft_units(mods, fams, sc.tier_weights,
                                                sc.tier_weights.get("T1", 1.0)) < 1:
                    return None
            elif sat < max(2, math.ceil(sc.partial_threshold * n)):
                return None
        coverage = sat / n
        # reachability gates the *upgrade* (completion) credit, not the match — a 5/6 with no open
        # slot for the missing affix is still a good item, it just can't be finished into the rule.
        reachable = full or self._slot_reachable(mods, fams, affix_slots)
        # ``mod_score`` blends tier with coverage (unsatisfied reqs weigh in ``wsum`` but not
        # ``tvsum``) — the right signal for a *rare's* roll/craft blend. ``kept_quality`` is the
        # pure tier of the affixes actually present (coverage-independent) — the right signal for a
        # *magic craft base* (judged on its 2 kept rolls) and for gating breadth.
        mod_score = (tvsum / wsum) if (wsum and sat) else 0.0
        kept_quality = (tvsum / sat_wsum) if sat_wsum else 0.0
        base_factor = self.bases.factor_for(item.get("base"))
        lockin = 1.0
        if is_magic:
            # A magic is a *craft base*: its kept affixes are fully realized and the open slots are
            # pure upside (not a coverage deficiency), so completion rides high and value rides on
            # the tier of those affixes. Versatility (breadth) separates a premium base — gated by
            # ``kept_quality`` in ``score_item``, so it only pays off on good rolls. A base with just
            # one locked high-tier affix (``hi`` < 2) is worth less than a two-affix one (the second
            # good mod still has to be *hit*, even if cheap to add) → ``magic_solo`` lock-in factor.
            lockin = 1.0 if hi >= 2 else sc.magic_solo
            completion = sc.magic_completion * lockin
            quality = self.value.score * kept_quality * base_factor
        else:
            completion = coverage + (1.0 - coverage) * (sc.craft_credit if reachable else 0.0)
            # Value is roll-driven: a *finished* item (no open slots) is scored on its tiers alone,
            # so a bricked full item can't coast on its archetype value. Open slots add a craft
            # bonus, but only on a base whose kept rolls are worth building on (``potential`` is
            # gated by ``mod_score``) — crafting onto trash tiers is not rewarded. ``headroom`` 0
            # (full) ⇒ ``blend = mod_score``; an empty base with good rolls lifts toward the ceiling.
            headroom = min(1.0, max(0, free) / sc.craft_target) if sc.craft_target else 0.0
            potential = mod_score * headroom
            blend = mod_score + (1.0 - mod_score) * sc.roll_influence * potential
            quality = self.value.score * blend * base_factor
        contribution = max(0.0, min(1.0, quality * completion))
        return {
            "archetype": self.id, "value": round(contribution, 3),
            "contribution": round(contribution, 3), "quality": round(quality, 3),
            "completion": round(completion, 3), "tier": value_to_tier(contribution),
            "coverage": round(coverage, 3), "satisfied": sat, "required": n, "full": full,
            "reachable": reachable, "free_slots": free,
            "mod_score": round(mod_score, 3), "kept_quality": round(kept_quality, 3),
            "lockin": round(lockin, 3), "base_factor": round(base_factor, 3),
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
class ModInfo:
    """Spawn-weight + desirability for one mod on one class (the rarity-premium input).

    ``tiers`` is ``[(min_magnitude, spawn_weight)]`` sorted ascending — the game's roll tiers from
    poe2db (rarer top tiers carry lower weight). ``pool`` is the total spawn weight of this mod's
    prefix/suffix track on the base, so ``frequency`` = a tier's weight / pool is "how often a roll
    of that affix type is *this* mod-tier". ``desirability`` is how wanted the mod is (max value of
    the class's archetypes that include it; 0 = in none → no trophy). Built by the miner; consumed
    by :meth:`ArchetypeSet.rarity_floor`."""

    gen: str = ""                                   # prefix | suffix | ""
    pool: float = 0.0
    desirability: float = 0.0
    tiers: list[tuple[float, float]] = field(default_factory=list)  # (min, weight) asc by min

    def _tier_idx(self, magnitude: float | None) -> int:
        if not self.tiers:
            return -1
        if magnitude is None:                       # flag mod: treat as the top (rarest) tier
            return len(self.tiers) - 1
        idx = -1
        for i, (mn, _w) in enumerate(self.tiers):
            if magnitude >= mn:
                idx = i
            else:
                break
        return idx

    def weight_for(self, magnitude: float | None) -> float:
        """Spawn weight of the tier this roll falls in (below the lowest tier → the most common)."""
        i = self._tier_idx(magnitude)
        if i < 0:
            return self.tiers[0][1] if self.tiers else 0.0
        return self.tiers[i][1]

    def tier_quality(self, magnitude: float | None) -> float:
        """0..1 roll quality on the mod's *own* tier ladder (top tier → 1.0). Self-contained, so
        the rarity floor needs no archetype bands."""
        if not self.tiers:
            return 1.0
        return (max(0, self._tier_idx(magnitude)) + 1) / len(self.tiers)

    def frequency(self, magnitude: float | None) -> float:
        """Per-tier roll frequency (the rolled tier's weight / pool) — the odds of *this exact tier*."""
        return (self.weight_for(magnitude) / self.pool) if self.pool > 0 else 0.0

    def mod_frequency(self) -> float:
        """Presence frequency = Σ tier weights / pool = the chance an affix of this type is this mod
        (any tier). The roll-independent rarity signal for the rarity premium."""
        total = sum(w for _m, w in self.tiers)
        return (total / self.pool) if self.pool > 0 else 0.0

    def top_weight(self) -> float:
        """Weight of the rarest (best) tier — the craft target's odds."""
        return min((w for _m, w in self.tiers), default=0.0)

    def to_dict(self) -> dict:
        d: dict = {"desirability": round(self.desirability, 3)}
        if self.gen:
            d["gen"] = self.gen
        if self.pool:
            d["pool"] = round(self.pool, 1)
        if self.tiers:
            d["tiers"] = [{"min": m, "weight": w} for m, w in self.tiers]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ModInfo":
        tiers = sorted((float(t["min"]), float(t["weight"])) for t in d.get("tiers") or [])
        return cls(gen=d.get("gen", ""), pool=float(d.get("pool", 0.0)),
                   desirability=float(d.get("desirability", 0.0)), tiers=tiers)


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
    # Per-class mod spawn-weight + desirability catalog (the rarity-premium input). class -> mod_key
    # -> ModInfo. Built by the miner from poe2db weights; empty in older sets (floor then inert).
    mods: dict[str, dict[str, ModInfo]] = field(default_factory=dict)
    archetypes: list[Archetype] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "meta": self.meta,
            "scoring": self.scoring.to_dict(),
            "mod_families": {k: v.to_dict() for k, v in self.mod_families.items()},
            "base_families": {k: v.to_dict() for k, v in self.base_families.items()},
            "affix_slots": dict(self.affix_slots),
            "mods": {cls: {k: mi.to_dict() for k, mi in cat.items()}
                     for cls, cat in self.mods.items()},
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
            mods={str(cls): {str(k): ModInfo.from_dict(mi) for k, mi in (cat or {}).items()}
                  for cls, cat in (d.get("mods") or {}).items()},
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

    def rarity_trophies(self, item: dict) -> dict[str, float]:
        """``{mod_key: trophy}`` for the item's rare *and* desirable kept mods — the rarity-floor
        inputs. trophy = ``desirability × (rarity_amp − 1) × tier_quality``: only the premium above
        baseline counts (a common mod, amp≈1, scores ~0, so a high roll of a common mod earns
        nothing — that's already in the archetype score), and a low roll of a rare mod contributes
        little. Empty when no weight/desirability data (older sets → floor inert)."""
        cat = self.mods.get(item.get("class") or "")
        if not cat:
            return {}
        out: dict[str, float] = {}
        for key, mag in (item.get("mods") or {}).items():
            info = cat.get(key)
            if info is None or info.desirability <= 0 or info.pool <= 0 or not info.tiers:
                continue
            premium = max(0.0, rarity_amp(info.mod_frequency(), self.scoring) - 1.0)
            t = info.desirability * premium * info.tier_quality(mag)
            if t > 0:
                out[key] = t
        return out

    def best_synergy_group(self, item: dict,
                           trophies: dict[str, float] | None = None) -> tuple[float, str | None, list[str]]:
        """``(summed_trophies, archetype_id|None, keys)`` for the item's strongest **coherent** set
        of rare mods: the most valuable group of trophy mods that **co-occur in a single mined
        archetype** (the data's synergy signal), or a lone mod on its own. So two rare mods that no
        build uses together (e.g. attack damage + cast speed on a ring) are graded as the better of
        the two *standalone* — never summed into an unsupported attack-and-caster combo."""
        trophies = self.rarity_trophies(item) if trophies is None else trophies
        if not trophies:
            return 0.0, None, []
        best = max(((t, None, [k]) for k, t in trophies.items()), key=lambda x: x[0])  # lone mod
        cls = item.get("class") or ""
        fams = self.mod_families
        for a in self.archetypes:
            if a.item_class and a.item_class != cls:
                continue
            keys: set[str] = set()
            for r in a.requires:
                if r.key:
                    keys.add(r.key)
                elif r.family and r.family in fams:
                    keys.update(fams[r.family].members)
            grp = [k for k in trophies if k in keys]
            s = sum(trophies[k] for k in grp)
            if s > best[0]:
                best = (s, a.id, grp)
        return best

    def rarity_floor(self, item: dict) -> float:
        """A value **floor** from the item's rare *and* desirable mods, independent of archetype
        completion — so a bricked item carrying a chase mod (low spawn weight, wanted) is still
        scored high (e.g. a spirit + top ``+projectile levels`` amulet buried in trash). Only mods
        that **synergize** (co-occur in a mined archetype) sum — see :meth:`best_synergy_group` — so
        non-synergistic rare mods are graded as the best standalone build, not a phantom combo. The
        winning group's summed trophies saturate toward 1 via ``rarity_floor_scale``."""
        best, _arch, _keys = self.best_synergy_group(item)
        if best <= 0:
            return 0.0
        scale = self.scoring.rarity_floor_scale or 1.0
        return 1.0 - math.exp(-best / scale)

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
        strong peak. ``overall = peak + (1-peak)·breadth·kept_quality``: breadth ∈ [0,
        ``breadth_cap``], with each extra rule weighted by its **novelty** (the share of its
        requirement units not already covered by a higher-ranked match) so near-duplicate rules
        don't inflate it, and the whole bonus is **scaled by the peak match's ``kept_quality``**
        (the pure tier of its present affixes) so versatility amplifies a base whose rolls are
        genuinely good rather than rescuing a weak one — and for a **magic** base, where peak is
        structurally capped by its 2 affixes, breadth (gated by those affixes' tier) is what
        promotes a premium 2×T1 base toward the top.

        A rare-mod **floor** (``rarity_floor``) is taken on top: ``overall = max(peak+breadth,
        floor)`` — so a bricked item carrying a chase mod still ranks high even when archetype
        completion is poor (or nothing matches at all).

        Returns ``{"overall", "peak", "breadth", "floor", "matches"}``; an item that matches nothing
        still scores its rarity ``floor`` (``overall`` 0 only when it has no rare desirable mod)."""
        by_id = {a.id: a for a in self.archetypes}
        matches = self.match(item)
        floor = self.rarity_floor(item)
        if not matches:
            return {"overall": round(floor, 3), "peak": 0.0, "breadth": 0.0,
                    "floor": round(floor, 3), "matches": []}
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
        # Versatility only counts on a genuinely good, locked-in base: gate by the peak's pure tier
        # (``kept_quality``) and its ``lockin`` — so a solo single-affix magic (one common mod "fits
        # everywhere") doesn't ride breadth up into finished-item tiers.
        gate = matches[0].get("kept_quality", peak) * matches[0].get("lockin", 1.0)
        overall = max(peak + (1.0 - peak) * breadth * gate, floor)
        return {"overall": round(overall, 3), "peak": round(peak, 3),
                "breadth": round(breadth, 3), "floor": round(floor, 3), "matches": matches}

    def upgrade_targets(self, item: dict) -> list[dict]:
        """Enabled rules this item could be **crafted into** (every missing affix fits an open
        slot of its type) but isn't a full match for yet — the popup's *upgrade paths*. Ranked by
        finished ``value`` then unit count, so the richest 5/6–6/6 targets lead.

        Each missing affix is annotated with its spawn odds (``chance`` = presence frequency, and
        ``rarity`` = the amp it would earn) so a path that hinges on a rare mod reads as a long
        shot — the empty slot is cheap to roll, hitting the *right* rare mod is not."""
        cat = self.mods.get(item.get("class") or "", {})
        out = []
        for a in self.archetypes:
            if not a.enabled:
                continue
            t = a.upgrade_target(item, self.mod_families, self.affix_slots)
            if t is None:
                continue
            for m in t.get("missing") or []:
                info = cat.get(m.get("key"))
                if info is not None and info.pool > 0 and info.tiers:
                    m["chance"] = round(info.mod_frequency(), 4)
                    m["rarity"] = round(rarity_amp(info.mod_frequency(), self.scoring), 2)
            out.append(t)
        out.sort(key=lambda t: (t["value"], t["units"]), reverse=True)
        return out

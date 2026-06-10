"""On-demand trade2 price estimation for captured items.

A **universal** core: a producer turns an item (or, later, an archetype) into a
:class:`FilterPlan` — an ordered, group-targeted set of trade2 search filters with
relaxation floors — and the core executes a *budgeted relaxation ladder* against the live
trade API (always through Stashler's shared rate limiter), distilling the cheapest
instant-buyout listings into a :class:`PriceEstimate`.

See ``PRICING_MODULE_PLAN.md`` for the full design. This package is split so the pure,
offline-testable parts (plan→body, price math, the ladder control flow against a
:class:`PriceSource`) carry no network or UI concerns:

* :mod:`stasher.pricing.query`      — :class:`FilterPlan` → trade2 body + the ladder rungs.
* :mod:`stasher.pricing.price`      — listings → :class:`PriceEstimate` (cheapest-N median).
* :mod:`stasher.pricing.aggregates` — headline aggregates (weapon dps / defence totals).
* :mod:`stasher.pricing.pseudo`     — pseudo-mod aggregation (real stats → ``pseudo.*``).
* :mod:`stasher.pricing.plan`       — item → :class:`FilterPlan` (rarity branch, ranking).
* :mod:`stasher.pricing.pricer`     — the ladder runner: ``estimate(plan, source)``.

**Rate discipline:** every live call goes through the shared limiter; in development prefer
the checked-in ``data/`` fixtures over live requests. Never run pricing concurrently with
Auto-refresh capture or the miner (one IP budget).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "StatFilter",
    "FilterPlan",
    "PriceEstimate",
    "SearchResult",
    "PriceSource",
    # strategy vocabulary
    "STRATEGY_MAGIC_BASE",
    "STRATEGY_RARE_FINISHED",
    "STRATEGY_RARE_POTENTIAL",
    "STRATEGY_UNIQUE",
]

# Strategy vocabulary (FilterPlan.strategy). Kept here so producers/consumers agree.
STRATEGY_MAGIC_BASE = "magic_base"        # base-anchored: base type dominates a 2-mod item
STRATEGY_RARE_FINISHED = "rare_finished"  # aggregate-anchored, base-agnostic
STRATEGY_RARE_POTENTIAL = "rare_potential"  # present mods + open-slot pseudos (crafting value)
STRATEGY_UNIQUE = "unique"                # name+base (reserved; not built yet)

# StatFilter.group
GROUP_EXPLICIT = "explicit"
GROUP_PSEUDO = "pseudo"
GROUP_AGGREGATE = "aggregate"

# StatFilter.target prefixes — the trade2 filter group an entry routes into.
TARGET_STATS = "stats"                    # the `stats` group (id is a stat/pseudo id)
TARGET_WEAPON = "weapon_filters"          # e.g. "weapon_filters.pdps"
TARGET_ARMOUR = "armour_filters"          # e.g. "armour_filters.es"


@dataclass
class StatFilter:
    """One trade2 search filter, with the floor the ladder may relax it down to.

    ``target`` routes the entry to its trade2 filter group: ``"stats"`` (then ``id`` is a
    stat/pseudo id, e.g. ``explicit.stat_3299347043`` or ``pseudo.pseudo_total_...``), or a
    ``group.field`` aggregate like ``"weapon_filters.pdps"`` / ``"armour_filters.es"``
    (then ``id`` is None). ``min`` is the item's rolled value; ``relax_floor`` is the lower
    bound the ladder relaxes to (its tier floor); ``droppable`` whether it may be removed
    entirely as the last rung.
    """

    target: str
    min: float
    relax_floor: float
    droppable: bool = True
    id: str | None = None
    group: str = GROUP_EXPLICIT

    def with_min(self, value: float) -> "StatFilter":
        return StatFilter(self.target, value, self.relax_floor, self.droppable, self.id, self.group)


@dataclass
class FilterPlan:
    """The universal input to the pricing core — what to search for, in priority order.

    Produced by :mod:`stasher.pricing.plan` for an item (later, by the miner for an
    archetype). ``filters`` is ordered most-price-defining first and may target multiple
    trade2 groups; the executor in :mod:`stasher.pricing.query` routes each by ``target``.
    """

    strategy: str
    type_filters: dict = field(default_factory=dict)  # the `type_filters.filters` contents
    filters: list[StatFilter] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rarity: str | None = None
    base: str | None = None  # exact base type when base-anchored (else None)


@dataclass(frozen=True)
class PriceEstimate:
    """A distilled market price for one plan. ``is_floor`` marks a lower bound (the ladder
    found too few comparables even fully relaxed → "≥ value, rarer than listings")."""

    value: float
    currency: str
    low: float
    high: float
    n_samples: int
    total_matches: int
    confidence: float
    strategy: str
    plan_sig: str
    sampled_at: str
    is_floor: bool = False
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["notes"] = list(self.notes)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PriceEstimate":
        d = dict(d)
        d["notes"] = tuple(d.get("notes") or ())
        return cls(**d)


@dataclass(frozen=True)
class SearchResult:
    """What a :class:`PriceSource` search returns: a query id, total match count, and the
    (price-ascending) result hashes."""

    id: str
    total: int
    hashes: list[str]


@runtime_checkable
class PriceSource(Protocol):
    """The trade-call seam the ladder runs against — abstracted so :mod:`pricer` is testable
    offline with a fake. The concrete adapter wraps :class:`stasher.client.TradeClient` in
    **market mode** (no seller-account filter, ``status="securable"``)."""

    def search(self, extra_filters: dict) -> SearchResult:
        """Run one instant-buyout, account-omitted search (sorted price-ascending)."""
        ...

    def fetch(self, hashes: list[str], query_id: str) -> list[dict]:
        """Fetch full ``{listing, item}`` entries for up to 10 hashes."""
        ...

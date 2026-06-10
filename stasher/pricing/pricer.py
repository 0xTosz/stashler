"""The ladder runner — execute a :class:`~stasher.pricing.FilterPlan` against a
:class:`~stasher.pricing.PriceSource` and distil a :class:`~stasher.pricing.PriceEstimate`.

Walks the budgeted relaxation rungs (:func:`stasher.pricing.query.ladder`), stops at the
first rung with ``>= enough`` matches (or lands on the terminal *floor* rung as a lower
bound), fetches the cheapest N of that rung, and summarizes (:mod:`stasher.pricing.price`).

The trade-call seam is :class:`PriceSource`; :class:`TradeClientSource` adapts Stashler's
:class:`~stasher.client.TradeClient` in **market mode**. Pure ladder logic is fully testable
with a fake source — no network.
"""

from __future__ import annotations

from . import (
    FilterPlan,
    PriceEstimate,
    PriceSource,
    SearchResult,
)
from . import price as _price
from . import query as _query
from ..store import utc_now_iso

DEFAULT_CHEAPEST_N = 10   # also the /fetch hard cap
DEFAULT_ENOUGH = 8        # match count that counts as "enough to price"


def estimate(
    plan: FilterPlan,
    source: PriceSource,
    *,
    league: str | None = None,
    cheapest_n: int = DEFAULT_CHEAPEST_N,
    enough: int = DEFAULT_ENOUGH,
    rates: dict | None = None,
) -> PriceEstimate:
    """Price one plan. Issues at most one search per rung (≤4 total incl. the floor) and one
    fetch, all through ``source`` (the shared rate limiter lives behind it)."""
    sig = _query.plan_sig(plan, league)
    rungs = _query.ladder(plan)
    notes = list(plan.notes)

    chosen_label = _query.RUNG_FLOOR
    chosen: SearchResult | None = None
    relaxed_steps = 0
    for i, (label, frag) in enumerate(rungs):
        res = source.search(frag)
        chosen_label, chosen, relaxed_steps = label, res, i
        if label != _query.RUNG_FLOOR and res.total >= enough:
            break  # this rung has enough comparables — use it

    is_floor = chosen_label == _query.RUNG_FLOOR and (chosen is None or chosen.total < enough)
    if is_floor:
        notes.append("≥ this price — rarer/better than current listings.")

    rates = rates or _price.load_rates()
    stats = None
    if chosen and chosen.hashes:
        entries = source.fetch(chosen.hashes[:cheapest_n], chosen.id)
        stats = _price.summarize(_price.listings_from_entries(entries), rates,
                                 cheapest_n=cheapest_n)

    if stats is None:
        # Nothing usable to price — emit a zero-confidence floor so the UI can say "too thin".
        notes.append("No usable instant-buyout listings to price.")
        return PriceEstimate(
            value=0.0, currency=_price.base_unit(), low=0.0, high=0.0,
            n_samples=0, total_matches=(chosen.total if chosen else 0),
            confidence=0.0, strategy=f"{plan.strategy}:{chosen_label}",
            plan_sig=sig, sampled_at=utc_now_iso(), is_floor=True, notes=tuple(notes),
        )

    mapped_fraction = _mapped_fraction(plan)
    confidence = _price.compute_confidence(
        stats, cheapest_n=cheapest_n, mapped_fraction=mapped_fraction,
        relaxed_steps=relaxed_steps, is_floor=is_floor,
        rates_stale=False,  # TODO: compare rates_sampled_at() against a freshness threshold
    )
    return PriceEstimate(
        value=stats.value, currency=stats.currency, low=stats.low, high=stats.high,
        n_samples=stats.n_samples, total_matches=chosen.total, confidence=confidence,
        strategy=f"{plan.strategy}:{chosen_label}", plan_sig=sig,
        sampled_at=utc_now_iso(), is_floor=is_floor, notes=tuple(notes),
    )


def _mapped_fraction(plan: FilterPlan) -> float:
    """Fraction of filters that carry a usable id/aggregate target (a `stats` filter needs an
    id; aggregate targets are always concrete). 1.0 when the plan has no filters."""
    if not plan.filters:
        return 1.0
    mapped = sum(1 for sf in plan.filters if sf.target != "stats" or sf.id)
    return mapped / len(plan.filters)


class TradeClientSource:
    """Adapts :class:`stasher.client.TradeClient` to :class:`PriceSource` in market mode:
    account filter omitted, ``status="securable"`` (instant buyout), price-ascending. Lifts
    the reserved ``_type``/``_name`` body keys to the query's exact base ``type`` / unique
    ``name``."""

    def __init__(self, client, *, sort: dict | None = None):
        self.client = client
        self.sort = sort or {"price": "asc"}

    def search(self, extra_filters: dict) -> SearchResult:
        frag = dict(extra_filters)
        type_name = frag.pop("_type", None)
        item_name = frag.pop("_name", None)
        res = self.client.search(
            frag, target="price", sort=self.sort, market=True,
            type_name=type_name, item_name=item_name,
        )
        return SearchResult(id=res["id"], total=res["total"], hashes=res["result"])

    def fetch(self, hashes: list[str], query_id: str) -> list[dict]:
        return self.client.fetch_batch(hashes[:10], query_id)

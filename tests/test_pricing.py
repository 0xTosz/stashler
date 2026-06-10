"""Offline tests for the pricing module (no network).

Covers the pure pieces: price math (modal-currency median, trim, band, confidence), the
query executor + relaxation ladder + deterministic plan signature, the TradeClient market
mode, the Store price cache (exact + fuzzy lookup), the ladder runner against a fake
PriceSource, and the headline-aggregate DPS math.
"""

import tempfile

import pytest

from stasher.client import _build_query
from stasher.pricing import (
    GROUP_EXPLICIT,
    STRATEGY_RARE_FINISHED,
    FilterPlan,
    SearchResult,
    StatFilter,
    TARGET_STATS,
)
from stasher.pricing import aggregates, price, pricer, query
from stasher.store import Store


# --- price math --------------------------------------------------------

def test_modal_currency_median_ignores_high_fishing():
    # Cheapest cluster is 5 exalt; one seller fishes at 999 — median must not move.
    listings = [(5, "exalted"), (5, "exalted"), (6, "exalted"), (5, "exalted"),
                (999, "exalted")]
    stats = price.summarize(listings, {"exalted": 1.0}, cheapest_n=10, trim_low_frac=0.0)
    assert stats.currency == "exalted"
    assert 5 <= stats.value <= 6


def test_unknown_currency_dropped():
    listings = [(5, "exalted"), (3, "mysterycoin"), (6, "exalted")]
    stats = price.summarize(listings, {"exalted": 1.0})
    assert stats.dropped == 1
    assert stats.n_samples == 2


def test_modal_currency_converts_only_outliers():
    # Most listings in exalted; one in divine (=200ex). Modal is exalted; the divine one is
    # converted in for ordering but the reported currency stays exalted.
    rates = {"exalted": 1.0, "divine": 200.0}
    listings = [(10, "exalted"), (12, "exalted"), (11, "exalted"), (1, "divine")]
    stats = price.summarize(listings, rates, trim_low_frac=0.0)
    assert stats.currency == "exalted"
    assert 10 <= stats.value <= 12  # the 200ex divine sits at the top, doesn't drag the median


def test_low_trim_defuses_underlisting():
    listings = [(1, "exalted")] + [(10, "exalted")] * 9  # one manipulative lowball
    untrimmed = price.summarize(listings, {"exalted": 1.0}, trim_low_frac=0.0)
    trimmed = price.summarize(listings, {"exalted": 1.0}, trim_low_frac=0.1)
    assert trimmed.value >= untrimmed.value == 10  # median already 10 here; trim never lowers it


def test_confidence_drops_with_relaxation_and_floor():
    stats = price.PriceStats(value=10, currency="exalted", low=9, high=11, spread=0.2,
                             n_samples=10, dropped=0)
    full = price.compute_confidence(stats, cheapest_n=10, mapped_fraction=1.0,
                                    relaxed_steps=0, is_floor=False, rates_stale=False)
    relaxed = price.compute_confidence(stats, cheapest_n=10, mapped_fraction=1.0,
                                       relaxed_steps=2, is_floor=False, rates_stale=False)
    floor = price.compute_confidence(stats, cheapest_n=10, mapped_fraction=1.0,
                                     relaxed_steps=0, is_floor=True, rates_stale=False)
    assert full > relaxed > 0
    assert floor < full


# --- query executor + ladder + signature -------------------------------

def _plan():
    return FilterPlan(
        STRATEGY_RARE_FINISHED,
        type_filters={"filters": {"rarity": {"option": "rare"}}},
        filters=[
            StatFilter("equipment_filters.es", min=400, relax_floor=300, droppable=False,
                       group="aggregate"),
            StatFilter(TARGET_STATS, min=80, relax_floor=60, droppable=True,
                       id="explicit.stat_life", group=GROUP_EXPLICIT),
        ],
        rarity="rare", base="Vaal Regalia",
    )


def test_compose_routes_groups_and_omits_account():
    plan = _plan()
    body = query.body(plan, plan.filters)
    assert body["type_filters"]["filters"]["rarity"]["option"] == "rare"
    assert body["equipment_filters"]["filters"]["es"]["min"] == 400
    assert body["stats"][0]["filters"][0]["id"] == "explicit.stat_life"
    assert body["_type"] == "Vaal Regalia"
    # The whole body fragment must never carry a trade_filters/account block.
    assert "trade_filters" not in body


def test_ladder_relaxes_then_drops_then_floor():
    plan = _plan()
    rungs = query.ladder(plan)
    labels = [lbl for lbl, _ in rungs]
    assert labels[0] == query.RUNG_FULL and labels[-1] == query.RUNG_FLOOR
    full = dict(rungs[0][1])
    relaxed = dict(rungs[1][1])
    assert full["stats"][0]["filters"][0]["value"]["min"] == 80
    assert relaxed["stats"][0]["filters"][0]["value"]["min"] == 60  # relaxed to floor
    # The dropped rung keeps the non-droppable aggregate, removes the droppable life stat.
    dropped = next(b for lbl, b in rungs if lbl == query.RUNG_DROPPED)
    assert "stats" not in dropped and dropped["equipment_filters"]["filters"]["es"]["min"] == 300
    # The floor rung drops the exact base.
    floor = next(b for lbl, b in rungs if lbl == query.RUNG_FLOOR)
    assert "_type" not in floor


def test_plan_sig_is_deterministic_and_floor_keyed():
    plan = _plan()
    # Re-pricing the same item (different rolled mins, same tier floors) keys identically.
    plan2 = FilterPlan(
        plan.strategy, type_filters=plan.type_filters,
        filters=[plan.filters[0].with_min(999), plan.filters[1].with_min(120)],
        rarity="rare", base="Vaal Regalia",
    )
    assert query.plan_sig(plan, "Std") == query.plan_sig(plan2, "Std")
    assert query.plan_sig(plan, "Std") != query.plan_sig(plan, "Hardcore")


# --- TradeClient market mode -------------------------------------------

def test_build_query_market_mode_omits_account():
    body = _build_query("Me#1", {"type_filters": {"filters": {"rarity": {"option": "rare"}}}},
                        status="securable", market=True, type_name="Vaal Regalia")
    assert "trade_filters" not in body["query"]["filters"]
    assert body["query"]["status"]["option"] == "securable"
    assert body["query"]["type"] == "Vaal Regalia"


def test_build_query_account_mode_still_has_account():
    body = _build_query("Me#1", None, status="online")
    assert body["query"]["filters"]["trade_filters"]["filters"]["account"]["input"] == "Me#1"


def test_stats_group_is_top_level_not_under_filters():
    # trade2 rejects a `stats` key inside query.filters ("Unknown filter group: stats");
    # it must sit at query.stats. A composed plan body must end up shaped that way.
    plan = _plan()
    extra = query.body(plan, plan.filters)   # carries both `equipment_filters` and `stats`
    extra.pop("_type", None)
    body = _build_query("", extra, status="securable", market=True)
    assert "stats" in body["query"] and "stats" not in body["query"]["filters"]
    assert body["query"]["stats"][0]["filters"][0]["id"] == "explicit.stat_life"
    # equipment_filters stays inside query.filters (it IS a filter group).
    assert "equipment_filters" in body["query"]["filters"]


# --- Store price cache (exact + fuzzy) ---------------------------------

def _store():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Store(tmp.name)


def test_price_cache_exact_and_item_pointer():
    store = _store()
    filters = [{"target": "stats", "id": "explicit.stat_life", "floor": 60}]
    store.cache_price("sig1", strategy="rare_finished", rarity="rare", base=None,
                      league="Std", filters=filters, estimate={"value": 5, "currency": "exalted"})
    store.set_item_price("itemhashA", "sig1")
    assert store.get_cached_price("sig1", "Std")["estimate"]["value"] == 5
    assert store.get_cached_price("sig1", "Other") is None
    assert store.get_item_price("itemhashA")["estimate"]["value"] == 5
    store.close()


def test_price_cache_fuzzy_within_tier_band_and_mod_diff():
    store = _store()
    filters = [{"target": "stats", "id": "explicit.stat_life", "floor": 60},
               {"target": "stats", "id": "explicit.stat_es", "floor": 100}]
    store.cache_price("sig1", strategy="rare_finished", rarity="rare", base=None,
                      league="Std", filters=filters, estimate={"value": 5})
    # Same set, floors slightly different (same tier band) -> match.
    near = [{"target": "stats", "id": "explicit.stat_life", "floor": 62},
            {"target": "stats", "id": "explicit.stat_es", "floor": 105}]
    assert store.find_similar_price(strategy="rare_finished", base=None, league="Std",
                                    filters=near) is not None
    # One extra mod (within max_mod_diff=1) -> still match.
    plus_one = near + [{"target": "stats", "id": "explicit.stat_res", "floor": 30}]
    assert store.find_similar_price(strategy="rare_finished", base=None, league="Std",
                                    filters=plus_one) is not None
    # Two extra mods -> no match.
    plus_two = plus_one + [{"target": "stats", "id": "explicit.stat_mana", "floor": 40}]
    assert store.find_similar_price(strategy="rare_finished", base=None, league="Std",
                                    filters=plus_two) is None
    # Different league -> no match.
    assert store.find_similar_price(strategy="rare_finished", base=None, league="HC",
                                    filters=near) is None
    assert store.clear_price_cache() == 1
    store.close()


# --- ladder runner against a fake source -------------------------------

class FakeSource:
    """Returns a scripted total per rung index and canned listings on fetch."""

    def __init__(self, totals, listings):
        self.totals = totals
        self.listings = listings
        self.searches = 0

    def search(self, extra_filters):
        total = self.totals[min(self.searches, len(self.totals) - 1)]
        self.searches += 1
        hashes = [f"h{i}" for i in range(min(total, 10))]
        return SearchResult(id="q", total=total, hashes=hashes)

    def fetch(self, hashes, query_id):
        return [{"listing": {"price": {"amount": a, "currency": c}}}
                for a, c in self.listings[: len(hashes)]]


def test_runner_stops_at_first_enough_rung():
    plan = _plan()
    src = FakeSource(totals=[2, 50], listings=[(5, "exalted")] * 10)
    est = pricer.estimate(plan, src, league="Std", enough=8)
    assert src.searches == 2          # full (2 < 8) -> relaxed (50 >= 8) -> stop
    assert not est.is_floor
    assert est.value == 5
    assert est.strategy.endswith(query.RUNG_RELAXED)


def test_runner_falls_through_to_floor():
    plan = _plan()
    src = FakeSource(totals=[0, 1, 1, 2], listings=[(7, "exalted")] * 2)
    est = pricer.estimate(plan, src, league="Std", enough=8)
    assert est.is_floor                # never hit `enough` -> floor estimate
    assert est.strategy.endswith(query.RUNG_FLOOR)
    assert any("rarer" in n for n in est.notes)


def test_runner_floor_when_no_listings():
    plan = _plan()
    src = FakeSource(totals=[0, 0, 0, 0], listings=[])
    est = pricer.estimate(plan, src, league="Std")
    assert est.is_floor and est.confidence == 0.0 and est.n_samples == 0


# --- headline aggregate DPS math ---------------------------------------

def test_weapon_dps_from_properties():
    item = {
        "properties": [
            {"name": "Physical Damage", "values": [["100-200", 1]]},
            {"name": "Elemental Damage", "values": [["10-30", 4], ["5-15", 5]]},
            {"name": "Attacks per Second", "values": [["2.0", 0]]},
        ]
    }
    dps = aggregates.weapon_dps(item)
    assert dps["pdps"] == pytest.approx(300.0)   # avg(150) * 2.0
    assert dps["edps"] == pytest.approx(60.0)    # (avg20 + avg10) * 2.0
    assert dps["dps"] == pytest.approx(360.0)


def test_defence_totals_from_properties():
    item = {"properties": [{"name": "Energy Shield", "values": [["520", 0]]}]}
    assert aggregates.defence_totals(item) == {"energy_shield": 520.0}


# --- pseudo aggregation on the real harvested ids ----------------------

def _ext(*mags):
    return {"extended": {"mods": {"explicit": [{"magnitudes": list(mags)}]}}}


def test_pseudo_total_ele_res_sums_components():
    from stasher.pricing import pseudo
    # Real trade ids from the harvested table: fire (mult 1) + cold (mult 1).
    item = _ext({"hash": "explicit.stat_3372524247", "min": "28", "max": "32"},  # fire
                {"hash": "explicit.stat_4220027924", "min": "23", "max": "27"})  # cold
    ps = {p.pseudo_id: p for p in pseudo.pseudos_for(item)}
    ele = ps["pseudo.pseudo_total_elemental_resistance"]
    assert ele.value == 55  # 30 + 25
    assert set(ele.components) == {"explicit.stat_3372524247", "explicit.stat_4220027924"}


def test_pseudo_all_ele_res_uses_multiplier():
    from stasher.pricing import pseudo
    # "+x% to all Elemental Resistances" has multiplier 3 toward total elemental resistance.
    item = _ext({"hash": "explicit.stat_2901986750", "min": "14", "max": "16"})  # all-ele, ~15
    ele = next(p for p in pseudo.pseudos_for(item)
               if p.pseudo_id == "pseudo.pseudo_total_elemental_resistance")
    assert ele.value == 45  # 15 * 3


# --- appraisal service + data interlock --------------------------------

def _magic_ring():
    return {
        "frameType": 1, "baseType": "Iron Ring", "typeLine": "Iron Ring",
        "extended": {"mods": {"explicit": [
            {"magnitudes": [{"hash": "explicit.stat_life", "min": "20", "max": "30"}]},
        ]}},
    }


def test_data_ready_interlock(monkeypatch):
    from stasher.pricing import appraise
    store = _store()
    # Shipped data is real (harvested) -> ready. Simulate unharvested placeholders to prove the
    # interlock blocks, and the force-ready escape hatch overrides it.
    assert appraise.data_ready(store)[0]
    monkeypatch.setattr(appraise._pseudo, "_rules",
                        lambda: {"aggregates": [{"_example": True}], "empty_slots": {}})
    ready, reason = appraise.data_ready(store)
    assert not ready and "Phase 0" in reason
    store.set_setting("pricing_force_ready", "1")
    assert appraise.data_ready(store)[0]  # escape hatch overrides the placeholder check
    store.close()


def test_service_request_refused_when_unharvested(monkeypatch):
    from stasher.pricing import appraise
    monkeypatch.setattr(appraise._pseudo, "_rules",
                        lambda: {"aggregates": [{"_example": True}], "empty_slots": {}})
    store = _store()
    svc = appraise.PricingService(store, FakeSource([50], [(5, "exalted")] * 10), lambda: "Std")
    res = svc.request("h1", _magic_ring())
    assert res["ok"] is False and "Phase 0" in res["reason"]
    store.close()


def test_service_prices_and_caches():
    from stasher.pricing.appraise import PricingService
    store = _store()
    svc = PricingService(store, FakeSource([50], [(5, "exalted")] * 10), lambda: "Std")
    item = _magic_ring()
    assert svc.lookup(item)["status"] == "miss"
    svc._price_one("h1", item)            # synchronous price (no thread)
    res = svc.lookup(item)
    assert res["status"] == "fresh" and res["estimate"]["value"] == 5
    assert store.get_item_price("h1")["estimate"]["currency"] == "exalted"
    store.close()


def test_service_dedupes_in_flight():
    from stasher.pricing.appraise import PricingService
    store = _store()
    store.set_setting("pricing_force_ready", "1")
    svc = PricingService(store, FakeSource([50], [(5, "exalted")] * 10), lambda: "Std")
    svc._in_progress = "h1"  # simulate an item already being priced
    res = svc.request("h1", _magic_ring())
    assert res.get("queued") and res.get("deduped")  # not re-enqueued; worker not started
    assert svc._thread is None
    store.close()

# Pricing Module — Design Plan (Stashler)

> Status: **design only** (for later implementation). This is the *Stashler-side*
> pricing module: a **universal** trade2 price-estimation core, whose **first consumer
> is on-demand user-facing item appraisal** ("what is this captured item worth right
> now?"). The module is deliberately shaped so the deferred **archetype** pricing loop
> in `archetype_miner/PRICING_MODULE_PLAN.md` can later plug into the same core via the
> same `FilterPlan` contract (§4) — but that loop is **out of scope here**.
>
> Reference implementation for query quirks: **Exiled-Exchange-2**
> (https://github.com/kvan7/Exiled-Exchange-2) — the PoE2 fork of Awakened PoE Trade. We
> borrow its *ideas* (pseudo-mod aggregation, headline-aggregate math for DPS/defences,
> currency handling), not its translation layer (see §2).

> ⚠️ **Rate limits, always.** Every interaction with the live pathofexile.com PoE2 trade
> API — in production **and during development/testing/data-harvesting** — must go through
> Stashler's shared [RateLimiter](stasher/ratelimit.py) and stay conservative. A blown
> long-window bucket is a **15–30 minute IP lockout**. While building this module: never
> hand-loop searches against the live API, never run the appraiser concurrently with
> Auto-refresh or the miner, prefer the checked-in `/data/*` fixtures (§11) over live
> calls, and when a live call is unavoidable, make **one** at a time, in restrictive rate
> mode, and watch the Log. See §3.5 and §10.

---

## 0. Implementation status (2026-06-11)

**Built & live-capable** on branch `feat/pricing-module` (`stasher/pricing/`): the universal
core (`FilterPlan`→body→`PriceEstimate`), the budgeted relaxation ladder with the floor rung,
modal-currency price math, TradeClient **market mode** (`securable`, no account filter), the
`price_cache`/`price_item` store with exact + fuzzy lookup, the `PricingService` queue, the
Flask routes, and the UI (detail-card price box, status indicator, Settings clear/TTL). ~30
offline tests; full suite green.

**Phase-0 harvest done** via `tools/harvest_ee2_pricing.py` (from Exiled-Exchange-2's bundled
data on GitHub — no PoE API calls): real trade2 ids for the elemental/chaos resistance and
all-attributes pseudos (multiplier-weighted) and the empty prefix/suffix pseudos. The
`data_ready()` interlock now passes, so a user "Price check" issues real (rate-limited,
instant-buyout) searches.

**Correction to this doc:** PoE2 trade puts weapon DPS *and* defence totals in **one
`equipment_filters` group** (`ar/es/ev/dps/pdps/edps/aps/crit/block/...`) — not the
`weapon_filters`/`armour_filters` named below (which reflect PoE1). The code uses
`equipment_filters`; treat the older names in §3.3/§4 as illustrative.

**Still open (deferred, not API-blocked):** replace `plan.py`'s heuristics (filter *ranking*,
the finished-vs-potential decision, "strong base") with the evaluator's real mod grading /
free-slot / defence-gate signals — best calibrated against live prices. The relaxation floor
already uses the real per-affix tier minimum from `extended`.

---

## 1. Scope & goals

**Goal:** given a captured item, estimate its current market value with reasonable
accuracy, on demand, without ever tripping GGG's rate limits.

**In scope (this plan):**
- A universal core: `FilterPlan → trade2 search body → cheapest-N price → PriceEstimate`.
- A plan builder that turns an item into a `FilterPlan`, reusing Stashler's existing mod
  grading (desirability/rarity, tier bands, free-slot/headroom, defence gates).
- The **appraisal consumer**: on-demand queue, background processing surfaced in the UI,
  and a structured cache with a long TTL + fuzzy lookup + manual invalidation.

**Out of scope (designed-for, not built):**
- The archetype refinement loop (`specialize`/`generalize`, `resolve_action`). It will
  reuse the core by emitting a `FilterPlan` from an archetype (§9).
- **Uniques** — the `strategy` field reserves a slot for them (name+base path), but they
  are **not built now** (low priority).

**Hard constraints:**
- **One shared rate budget** (see the banner above + §3.5). Pricing goes through the
  *same* [RateLimiter](stasher/ratelimit.py)/[TradeClient](stasher/client.py) instances
  the capture worker uses (CLAUDE.md "Gotchas": never add a second budget-spending path).
- **Instant-buyout only** — pricing searches force `status="securable"` (§3.5); this
  removes offline/unbuyable/price-fixed noise and makes the cheapest-N median reliable.
- **Background + visible** — appraisal is queued; the UI shows pricing is in progress.
- **Cache aggressively** — long TTL, fuzzy lookup, manual invalidation (§6).

---

## 2. The key insight: our items already carry trade2 stat ids

EE2 works from the **in-game clipboard text**, so most of its complexity is a translation
layer (rendered stat line → stat database → numeric trade2 id). **Stashler does not need
this.** Every captured listing is the full `/fetch` entry including the `extended` block,
which already stores the **fully-prefixed** trade2 stat id and per-affix ranges:

- `extended.hashes.explicit[i] = ["explicit.stat_3299347043", [mod_idx, …]]`
- `extended.mods.explicit[k].magnitudes[] = {hash: "explicit.stat_…", min, max}`

Verified against the fixtures in [tests/test_checks.py:55-66](tests/test_checks.py#L55-L66):
the hash is `explicit.stat_<id>` — **exactly** the id a trade2 `stats` filter takes (no
name→id translation, no prefix composition). The de-merge helpers already exist:
[itemdata.explicit_affix_mods](stasher/evaluate/itemdata.py#L331),
[itemdata.mods_for_lines](stasher/evaluate/itemdata.py#L210),
[itemdata.explicit_roll_percents](stasher/evaluate/itemdata.py#L386). (See the
*affix-representation* project memory for why `extended` is lossless and rendered text is
not.)

**What we still need from EE2** (vendored as data, not code):

| EE2 capability | Need? |
| --- | --- |
| Clipboard text → stat-id translation | **No** — `extended` already has ids |
| **Pseudo-mod aggregation** (which real stats sum into a `pseudo.*`) | **Yes** (§5) |
| **Headline-aggregate math** — weapon dps/pdps/edps, defence totals from base+%+quality | **Yes** (§3.3) |
| **Currency normalization** | **Yes** (small table, §5b) |
| Per-type catalog of which aggregates/pseudos exist | **Yes** — Phase-0 harvest (§11) |

---

## 3. Architecture

New package `stasher/pricing/`:

```
stasher/pricing/
  __init__.py        facade: PriceEstimate, PriceSource, FilterPlan, build_pricer()
  plan.py            item -> FilterPlan (rarity branch, ranking, thresholds) — uses eval data
  query.py           FilterPlan -> trade2 search body; the budgeted relaxation ladder
  pseudo.py          pseudo-mod aggregation (item's real stats -> pseudo filters)
  aggregates.py      headline aggregates: weapon dps/pdps/edps + defence totals (EE2 math)
  price.py           listings -> PriceEstimate (cheapest-N median, modal-currency)
  appraise.py        the on-demand consumer: queue worker + (fuzzy) cache orchestration
  data/
    pseudo_rules.json    aggregation table (EE2 + /data/stats), curated
    aggregate_map.json   per-item-type headline filters (EE2 + /data/filters)
    currency_rates.json  base-unit rates (seeded; refreshable)
    stats_fixture.json   checked-in /data/stats + /data/filters snapshot for tests
```

Reuse, do not reimplement:

| Need | Reuse | Symbol |
| --- | --- | --- |
| Search (≤100 hashes) | [TradeClient.search](stasher/client.py#L75) | + **market mode** (§3.5) |
| Fetch (≤10 ids, full listing) | [TradeClient.fetch_batch](stasher/client.py#L114) | as-is |
| Rate limiting / 429 / bucket learning | [RateLimiter](stasher/ratelimit.py#L40) | shared instance |
| Type/category filter | [categories.category_filter](stasher/categories.py#L26) | as-is |
| Mod grading (desirability/rarity, tiers, free slots, defence gate) | `evaluate` (archetype set) | read-only, for `plan.py` |
| Item class / ids / affixes / roll% | [itemdata](stasher/evaluate/itemdata.py) | as-is |
| Settings, meta, query log | [Store](stasher/store.py) | + cache tables (§6) |
| Background thread + UI-polled status | [Worker](stasher/runtime.py) pattern | new `PricingWorker` |
| Long-TTL cache precedent | leagues cache ([app.py:192](stasher/ui/app.py#L192)) | structured + fuzzy (§6) |

### 3.1 `PriceEstimate`

```python
@dataclass(frozen=True)
class PriceEstimate:
    value: float            # base unit (or modal currency, see §5b)
    currency: str
    low: float; high: float # p25/p75 of cheapest-N (the band shown to the user)
    is_floor: bool          # True => "≥ value, likely higher" (terminal rung, §3.4)
    n_samples: int          # usable same-currency listings
    total_matches: int      # search `total`
    confidence: float       # 0..1 (§5c)
    strategy: str           # FilterPlan.strategy + how far the ladder relaxed
    plan_sig: str           # deterministic signature of the item's plan (cache key, §6)
    notes: list[str]        # disclaimers (ignores runes/sockets/corruption, floor, etc.)
    sampled_at: str
```

### 3.2 Stat selection — driven by eval, not by pricing

Pricing does **not** reach into the archetype model. The **plan builder** (`plan.py`)
consumes the grading the evaluator already produces and emits an **ordered** filter list.
Per-stat inputs, all local (no API call):
- **stat id + value** from `extended` (§2);
- **desirability / spawn-weight rarity** per `mod_key` from the loaded `ArchetypeSet.mods`
  catalog (`ModInfo{gen, pool, desirability, tiers}` — see *archetype-miner* memory); a
  static fallback table when no set is loaded;
- **tier band / roll%** ([explicit_roll_percents](stasher/evaluate/itemdata.py#L386)) → the
  `relax_floor` each filter can relax down to (its tier floor, not its exact roll);
- **free affix slots / headroom** and the **defence gate**, already computed by eval, to
  decide the `strategy` (finished vs. potential; strong base or not).

### 3.3 Headline aggregates (`aggregates.py`)

Every item type has a few aggregate stats the market actually prices on. The plan builder
computes the item's **own** aggregate locally from its `properties` and emits it as a
group-targeted filter with a `min`:

| Item type | Headline aggregate(s) | Trade group |
| --- | --- | --- |
| Armour bases | total armour / energy shield / evasion (+ hybrids) | defence filters / pseudo totals |
| **Weapons** | **dps, pdps (physical), edps (elemental)** | `weapon_filters` |
| Caster wpn / foci | +levels, spirit | `stats` / pseudo |

DPS = avg damage × APS × (1 + quality); defence total = base × (1 + %inc) × (1 + quality)
— the EE2 math, computed from the item's `properties`. The exact group/field names
(`weapon_filters.pdps`, the defence group) are confirmed to exist (DPS and total-defence
searches are live trade features) and pinned in Phase 0 (§11).

### 3.4 Search strategy — the budgeted relaxation ladder

Instant-buyout's pool is smaller, so "all filters at the rolled min" usually returns too
few results. Relax **thresholds before dropping mods** (higher accuracy), and **bound the
calls hard — ≤3 searches per item**:

```
plan = ordered, group-targeted FilterPlan (most price-defining first)
rung 1: all filters at rolled min                       -> search
rung 2: each filter.min relaxed to its relax_floor      -> search   (tier floors)
rung 3: drop the weakest filter.droppable mods          -> search
stop at the first rung with res.total >= ENOUGH (e.g. 8); estimate from its cheapest-N.
TERMINAL: if rung 3 still < ENOUGH -> drop the base/anchor and take the broadest
          comparable as a FLOOR: PriceEstimate(is_floor=True, "≥ X, rarer than listings").
```

- Each rung is one `search` against the shared limiter; never exceed 3 + the terminal.
- Pseudos (ele-res, defence totals, empty-slot) and weapon aggregates count as strong
  filters and relax last — they stay liquid where exact lines don't.
- The terminal **floor** rung is the *most informative* case for chase items, not a
  failure: thin/empty results mean "rarer/better than anything currently listed."

**Alternative cheap mode** (one search, fixed top-N) and **deep mode** (a 2nd combination
probe for high-value items) are toggles layered on the same ladder; default is the ladder
above.

### 3.5 Rate discipline & market mode

- **Instant-buyout only:** force `status="securable"` on every pricing search regardless
  of the app's `status` setting.
- **Market mode (must-fix in TradeClient):** [_build_query](stasher/client.py#L183)
  **always injects the seller-account filter** — correct for archiving your listings,
  **wrong for market pricing**. Add `market: bool=False` (or `account: str|None`) to
  `search`/`_build_query` that **omits** the account filter; a test asserts a market body
  has no `trade_filters.account`.
- **Shared budget, serial, paced:** all pricing goes through the live `RateLimiter`;
  one request in flight; the appraiser runs in restrictive headroom. **During development**
  prefer fixtures; any live probe is one-at-a-time and watched (banner + §10).

---

## 4. The `FilterPlan` contract (the universal seam)

The input to the pricing core. Anything that can produce one can drive pricing — the item
plan builder now, the miner's archetype loop later (§9).

```python
@dataclass
class StatFilter:
    target: str        # "stats" | "weapon_filters.pdps" | "armour_filters.es" | ...
    id: str | None     # stat id when target == "stats" (incl. pseudos); else None
    min: float
    relax_floor: float # the tier-floor min the ladder may relax down to
    droppable: bool    # may the ladder drop this mod entirely?
    group: str         # "explicit" | "pseudo" | "aggregate"

@dataclass
class FilterPlan:
    strategy: str            # "magic_base" | "rare_finished" | "rare_potential" | "unique"
    type_filters: dict       # class/category; + exact base (name/type) when base-anchored
    filters: list[StatFilter]# ORDERED by price-relevance, most-defining first
    notes: list[str]         # UI disclaimers
```

`filters` carries entries for **multiple trade groups** (`stats`, `weapon_filters`,
defence filters) — the executor in `query.py` routes each by `target`. This is what makes
headline aggregates first-class rather than bolted on.

### 4.1 Strategy matrix (set by `plan.py` from eval data)

| Item | `strategy` | Anchor | Rationale |
| --- | --- | --- | --- |
| **Magic** | `magic_base` | exact **base type** + its 1–2 mods | base dominates a 2-mod item. **If even the base-dropped rung is empty → floor estimate "≥ X, base is exceptional"** (some magic mod/base combos are rarer than anything listed). |
| **Finished rare** (no/low open slots) | `rare_finished` | **headline aggregate** (defence total / weapon dps), **base-agnostic** + top mods | great rolls on a mid base match top base / mid rolls at the same total. |
| **Rare, open slots, strong base** | `rare_potential` | base/aggregate floor + present good mods + **`pseudo_number_of_empty_prefix/suffix ≥ N`** | prices **crafting potential** by comparing against *other craftable strong bases*, **never** by fabricating the missing mods. Lower confidence, labeled "base + potential". |
| **Unique** | `unique` (future) | name + base | stub now; slots in via the same field later. |

"Strong base" reuses eval's existing defence gate, so there is one definition of strong,
shared with grading.

---

## 5. Pseudo mods, currency, confidence

### 5a. Pseudo mods (`pseudo.py`)
Trade2 `pseudo.*` stats aggregate several real stats and stay far more liquid than ANDing
components. We:
1. **Vendor** `data/pseudo_rules.json`: `pseudo_id → [contributing stat ids, weight]`,
   sourced from EE2 and cross-checked against live `/api/trade2/data/stats` (the pseudo
   *ids* come from the endpoint; the **composition** is the curated part EE2 supplies).
2. At plan time, sum the item's present component magnitudes → emit
   `{id: pseudo_id, value:{min: floor}}`. Prefer a pseudo over its components when it
   covers ≥2 of them — especially **elemental resistance** (fire/cold/light fungible, the
   same `ele_res` family the archetype model already encodes). **Chaos res stays
   separate.**
3. The **empty-slot pseudos** (`pseudo_number_of_empty_prefix_mods` /
   `..._empty_suffix_mods`) are how `rare_potential` expresses open slots.
4. Pseudo ids not present in the live `/data/stats` snapshot are dropped (lower
   confidence), never faked.

### 5b. Price extraction & normalization (`price.py`)
1. **Sort + truncate:** `sort price asc`; one `fetch_batch` for the cheapest N (default
   10) hashes only. No pagination (trade2 has no offset).
2. **Modal-currency median:** compute the median in the **modal currency** of the
   cheapest-N (the currency most listings use) and convert only the outliers via
   `currency_rates.json` — this minimizes exposure to a stale divine:exalt ratio. Unknown
   currencies dropped (lower confidence); rates carry `sampled_at` and mark estimates
   low-confidence when stale.
3. **Robust central value + band:** `value = median(cheapest-N)`; against systematic
   under-listing use a **low-quantile / small trim fraction**, not just trim-one;
   `low,high = p25,p75` shown as the range.

### 5c. Confidence (0..1)
High when ≥N usable same-currency securable listings, all chosen filters mapped to real
ids, tight band, and the ladder didn't relax far. Low (with a surfaced reason — "only 3
listings", "widened to 2 mods", "stale rates", "base + potential") when few listings,
dropped/unmapped filters, wide band, heavy relaxation, or `is_floor`.

---

## 6. Caching — structured + fuzzy (`appraise.py` + Store)

The market is slow-moving, so days-old prices are fine for generic mod combinations, and
**exact-key caching is the wrong model** (the post-widening body depends on live volume →
keys fragment and prices visibly jump). Instead: **store structured filters and do
nearest-neighbour lookup.**

New tables (additive migration, same style as [Store._migrate](stasher/store.py#L130)):

```sql
CREATE TABLE IF NOT EXISTS price_cache (
    plan_sig    TEXT PRIMARY KEY,   -- deterministic sig of the item's FilterPlan (pre-widening)
    strategy    TEXT NOT NULL,
    rarity      TEXT,
    base        TEXT,
    filters     TEXT NOT NULL,      -- normalized [{target,id,min}] for similarity matching
    estimate    TEXT NOT NULL,      -- PriceEstimate JSON
    sampled_at  TEXT NOT NULL,
    league      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_item (   -- last estimate per captured item, for the UI
    item_hash   TEXT PRIMARY KEY,
    plan_sig    TEXT NOT NULL,
    priced_at   TEXT NOT NULL
);
```

- **Key (`plan_sig`) is deterministic** — derived from the item's *intended* plan
  (sorted target+id+bucketed-min + strategy + base + league), **not** the rung that
  happened to win. Re-pricing the same item is a clean hit; the price doesn't jump because
  the market got thinner.
- **Fuzzy lookup on request:** find a cached row with the **same filter set (or ±1 mod)
  and each `min` within the same tier band**, same strategy/base/league. If found, **show
  it first**, labeled *"based on a similar check N days ago,"* with its age, and only offer
  a **"fresh check"** button (which enqueues a real search) — don't auto-spend the rate
  budget. Linear scan is fine (the cache is small, per-user).
- **TTL** (`price_cache_ttl_hours`, default ~24, in Settings) governs when a row is
  considered stale enough to suggest a refresh; the stale value still shows (greyed) until
  fresh lands.
- **Manual invalidation** from Settings: "Clear price cache" → `Store.clear_price_cache()`
  (truncate both tables), mirroring [`/feedback/clear`](stasher/ui/app.py#L537). A per-item
  "re-price" deletes just that `plan_sig`.
- **Per-league scope:** cache + rates are league-keyed; segregate on league change.

---

## 7. Background queue + UI (`PricingWorker`)

A daemon thread modeled on [Worker](stasher/runtime.py) draining a `queue.Queue` of item
hashes, holding a thread-safe status dict the Flask UI polls. **It calls the shared
`TradeClient`/`RateLimiter`**, so its `before("search")`/`before("fetch")` interleave with
capture — one IP budget, no second spender.

Flow:
1. "Price check" on an item card → `POST /api/price/<item_hash>` → fuzzy cache lookup
   (§6); on a similar hit, return it + offer fresh; otherwise enqueue, return `{queued}`.
2. Worker: build `FilterPlan` (`plan.py`) → run the ladder (`query.py`, ≤3+terminal) →
   cheapest-N fetch → `PriceEstimate` (`price.py`) → write cache + `price_item`. Each
   request logged to `query_log` (`kind="price"`) so it appears in the Log feed.
3. **Status surfaced** by extending [Worker.status](stasher/runtime.py#L300) with
   `pricing: {queued, in_progress, last_priced}` so the global bar shows "pricing 1 item…"
   — the user always knows work is happening.
4. UI renders value + band + confidence + reason + `is_floor`/disclaimer notes inline, with
   a re-price affordance and a spinner while queued.

FIFO, **one in flight** (the rate budget is the bottleneck). Coalesce duplicate enqueues.
**Keep v1 single-item / small-batch** — a future "price all" must impose its own batch
budget + ETA (50 items × up to 3 searches can approach the long-window lockout), not just
the per-item cap.

---

## 8. Disclaimers (runes / sockets / corruption)

Per design, v1 **ignores** rune/socketed stats and **does not** filter on corruption —
with a clear UI disclaimer, and a toggle to add them later:
- Exclude `rune.*` and socketed-derived stats from the plan (they're added, removable
  value; searching on them finds only items with that rune).
- Don't pin `corrupted`; price the base item. Disclaimer copy must be honest: *"price
  reflects the base item; corruption, runes, and sockets are not valued"* — ignoring
  corruption can over-price a bricked corrupt and under-price a well-corrupted one.
- Adding a `corrupted` `misc_filter` (and rune valuation) later is a small, additive
  follow-up behind a toggle.

---

## 9. Universal seam — the miner loop later

The pricing core (`query`/`price`/`pseudo`/`aggregates`) is free of appraisal/queue
concerns and takes a `FilterPlan` + a `PriceSource`. Two callers of the same core:
- **Appraisal (built here):** item → `plan.build_for_item(item)` → core → cached estimate.
- **Archetype loop (later):** archetype → `plan.build_for_archetype(arch)` (its `requires`
  floors → the same group-targeted `FilterPlan`, reusing pseudo + aggregate machinery) →
  core → its `feedback`/`resolve_action` machine. The `STAT_ID` table that
  `archetype_miner/PRICING_MODULE_PLAN.md` calls net-new is **subsumed** by our
  `extended`-derived ids + `pseudo_rules.json`.

**Cross-process rate coordination caveat.** The limiter's in-memory restriction state
(`_restricted_until`, locks) is **per-process**; the app shares it in-process, but the
miner is a separate process and only coordinates via the persisted `rate_events` table (no
shared restriction window). So app + miner pricing *concurrently* could burst past a
bucket. **Mitigation:** the miner's `price` CLI **detects a running Stashler instance**
(a pidfile/lockfile in the data dir, or probing the known UI port) and **aborts with a
message** ("Stashler is running — close it before pricing; shared rate budget") rather than
co-running. (Optionally also persist the restriction window for weak cross-process backoff.)

---

## 10. Caveats & risks

1. **Rate lockouts (top risk, dev included).** A blown long-window bucket = 15–30 min
   lockout. Shared limiter + restrictive mode + the ≤3-search cap + fixtures-over-live in
   development keep us safe; never add a parallel pricer; never co-run with the miner.
2. **Account-scope leakage** — market searches must omit the account filter (§3.5); assert
   in a test or you price only your own listing (always n=1).
3. **Ask ≠ sold** — even securable asks are asks; cheapest-N median is a proxy. Label it
   ("est. instant-buyout, cheapest listings").
4. **Pseudo/aggregate/stat-id drift** — `/data/stats` & `/data/filters` "can change at any
   time"; pin fixtures, refresh the vendored tables on release, drop unmapped ids with
   lower confidence.
5. **Currency volatility** — stale rates skew the outlier conversion; modal-currency median
   limits exposure; store `sampled_at`, lower confidence when stale.
6. **Thin/empty results are signal, not error** — handled as the floor rung (§3.4).
7. **Corruption/runes ignored** — disclaimed (§8).

---

## 11. Open data tasks (Phase 0 — confirm exact ids, respectfully)

Harvest from EE2 + a **single, rate-limited** pull of each static endpoint (or borrow EE2's
checked-in copies — preferred, no live call), then pin as fixtures:
- `pseudo_rules.json` — pseudo ids from `/api/trade2/data/stats`; **composition** from EE2.
- `aggregate_map.json` — per-item-type headline filters: confirm the **`weapon_filters`**
  field names (`dps`/`pdps`/`edps`) and the **defence-total** group/ids from
  `/api/trade2/data/filters` + EE2.
- The **empty-slot pseudo ids** (`pseudo_number_of_empty_prefix_mods` /
  `..._empty_suffix_mods`) — confirm exact ids from `/data/stats`.
- `currency_rates.json` — seed base-unit rates (`exalted` base) with `sampled_at`.
- `stats_fixture.json` — snapshot of `/data/stats` + `/data/filters` for offline tests.

These endpoints are static data (like [categories.fetch_categories](stasher/categories.py#L31)
using `/data/filters`); still fetch **at most once**, cached, never in a loop.

---

## 12. Phased build order + verification

> Every phase that can be verified offline **must** be — live calls are a last resort and
> always rate-limited (see banner). Phases 0–4 are fully offline against fixtures.

**Phase 0 — data artifacts (offline tests).** The §11 tables/fixtures. *Verify:* fixtures
load; every pseudo's components + every aggregate field exist in the `/data` snapshot.

**Phase 1 — plan builder (offline).** `plan.py`: item → `FilterPlan` with the §4.1 strategy
branch. *Verify:* a magic item → `magic_base` (base-anchored); a finished defensive rare →
`rare_finished` (aggregate-anchored, base-agnostic); a strong open-slot rare →
`rare_potential` with empty-slot pseudos and missing mods **absent**; 3 resistances collapse
to one ele-res pseudo; a weapon emits a `weapon_filters.pdps` aggregate; unmapped stat lowers
confidence.

**Phase 2 — query executor + ladder (offline).** `query.py`: `FilterPlan` → body, routing
each `StatFilter` by `target`; the ≤3-rung ladder + terminal floor as a pure planner (emit
the *sequence* of bodies, don't call out). *Verify:* body has correct `type_filters`,
group-routed filters (`stats`/`weapon_filters`/defence), `status="securable"`, **no** account
filter; relaxation walks rolled-min → tier-floor → drop; empty result yields `is_floor`.

**Phase 3 — price math (offline).** `price.py`. *Verify:* high-fishing outliers don't move
the modal-currency median; unknown currency dropped; low-quantile defuses under-listing;
few/relaxed/floor → low confidence; band boundaries covered.

**Phase 4 — cache + Store (offline).** `price_cache`/`price_item` + `_migrate` + fuzzy
lookup + `clear_price_cache`. *Verify:* deterministic `plan_sig`; nearest-neighbour matches
within tier-band tolerance; "similar, N days ago" path doesn't enqueue; manual clear empties
both; migration additive on an existing DB.

**Phase 5 — TradeClient market mode (tiny).** Market flag + `status="securable"`. *Verify:*
market body has no `trade_filters.account`; capture path unchanged.

**Phase 6 — worker + UI + live (rate-limited, one item).** `PricingWorker`, `/api/price`,
status surfacing, Settings clear/TTL, the card affordance, miner-abort lockfile. *Verify
(single real item, restrictive mode, watch the Log):* end-to-end price; status bar shows
in-progress; a second price is a cache/fuzzy hit (no new search events); "Clear price cache"
forces a re-query; `query_log` shows the `price` calls; the limiter never exceeds the shared
buckets.

**Phase 7 — full suite + smoke.** `python -m pytest` green; price a handful of items of
different classes (serially, paced); confirm no bucket breach.

---

## Critical files
- [stasher/client.py](stasher/client.py) — `search`/`fetch_batch`/`_build_query`; **add market mode + securable** (§3.5).
- [stasher/ratelimit.py](stasher/ratelimit.py) — the shared GGG-aware limiter (reuse the live instance; respect always).
- [stasher/evaluate/itemdata.py](stasher/evaluate/itemdata.py) — `extended` stat ids/ranges, roll%, class, properties.
- [stasher/evaluate](stasher/evaluate) — mod grading (desirability/rarity, tiers, free slots, defence gate) consumed by `plan.py`.
- [stasher/categories.py](stasher/categories.py) — `category_filter` + the `/data/*` discovery pattern (mirror for `/data/stats`, once).
- [stasher/store.py](stasher/store.py) — add `price_cache`/`price_item` + accessors + `clear_price_cache` (§6).
- [stasher/runtime.py](stasher/runtime.py) — `Worker` pattern → `PricingWorker`; extend `status()`.
- [stasher/ui/app.py](stasher/ui/app.py) — `/api/price`, status payload, Settings clear/TTL (mirror `/feedback/clear`, leagues cache).
- `archetype_miner/PRICING_MODULE_PLAN.md` (sibling repo) — the future second consumer; keep the §4/§9 `FilterPlan` seam compatible.
```

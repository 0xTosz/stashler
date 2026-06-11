# Evaluation Engine Improvements — Implementation Plan

> Status: **EXECUTED + SHIPPED** (2026-06-12, set marker `0.2.0b9-craftability`). Corpus
> results: overall ρ 0.014→**0.251**, Magic −0.043→**+0.135**, Jewels +0.144→**+0.451**,
> Rare preserved (+0.247); tier-band medians now economically ordered (A 10 ex / B 5 /
> C–D 1.5). Knobs corpus-fit: `magic_completion 0.95→0.85`, `magic_solo 0.55→0.35` (flat
> plateau 0.75–0.85 × 0.30–0.40; mid chosen against overfitting). Deviation from the
> Phase-3 spec: `overall` keeps the proven blended aggregation; now/potential/driver are
> surfaced alongside it (rank-preserving) rather than `overall = max(now, potential)`.
> Known residual: high-confidence expensive MAGIC items with a chase mod KEPT (the value
> is the kept mod, not the craft) can under-score — next corpus iteration's target.
>
> Original plan below. Grounded in the first full price-sampling cycle and
> the 353-check in-app validation study — see `archetype_miner/RESEARCH_LOG.md` (sibling
> repo) for the artifacts and conclusions this plan cites. Scope: Designs 1+2
> (probabilistic completion + now/potential split), the price-check liquidity fix, and
> the magic-overvaluation remediation. Out of scope (sequenced after): closed-itemset
> re-mine, T1-bucket tier-curve calibration, the feedback loop.

**The evidence driving each piece** (RESEARCH_LOG §7):
- Magic ρ = **−0.21** vs market (n=193): flat `magic_completion=0.95` pays the craft-base
  premium regardless of *which* mods remain to hit; the market pays it only for chase mods.
- "Expensive junk" appraisals all rest on 9–30 total matches: `compute_confidence` never
  sees `total_matches`, so a 10-of-10 fetch scores a full sample factor.
- Rare ρ = +0.19 and the price-blended anchors behave (poison robe → C, Stonefist → S):
  the *anchor* layer is sound; the gaps are craft-potential math and appraisal confidence.

---

## Phase 0 — Validation harness (build FIRST, measure everything against it)

New `tools/validate_eval.py` (this repo; offline, no API):
1. Join `price_item` × `price_cache` × `items` in the stasher DB → the 353-check corpus
   (filter: non-floor, `confidence ≥ 0.5`, known currency → ~282 usable).
2. Re-evaluate each item's archetype score **in-process** against an arbitrary set YAML +
   `Scoring` overrides (no DB writes): `model_item()` → `ArchetypeSet.score_item()`.
3. Report: Spearman ρ (overall / Magic / Rare / Jewels), median + p90 appraised price per
   predicted tier band, and the top-N disagreement lists.
4. `--baseline` writes a JSON snapshot; subsequent runs print deltas.

*Verify:* reproduces the study numbers (ρ −0.08 / −0.21 / +0.19) against the shipped set.
Every later phase must move Magic ρ toward positive without degrading Rare ρ.
**Lesson learned encoded:** never tune knobs by feel again — the corpus is the judge
(and it grows every time the user price-checks an item).

## Phase 1 — Price-check liquidity fix (smallest, ship first)

`stasher/pricing/price.py::compute_confidence` gains `total_matches`:
- `liquidity = min(1.0, total_matches / LIQUID_TOTAL)` with `LIQUID_TOTAL = 48` (6× the
  ladder's `ENOUGH`): 10 matches → 0.21, 30 → 0.63, 48+ → 1.0.
- Re-weight: `conf = 0.30·sample + 0.20·liquidity + 0.25·mapped + 0.25·(1−spread_penalty)`
  (relax/floor/dropped/stale modifiers unchanged).
- `pricer.estimate` passes `chosen.total`; the UI price box already shows the band — add
  "N of M listings" to the confidence reason string.
*Verify:* the 8 forensics cases (Grim Snare 23 matches, Vengeance Coil 9, …) fall below
the 0.5 usability line; a 50+-match commodity estimate is unchanged within ±0.05. Existing
cache entries keep old confidences (TTL handles); note in the Settings clear-cache copy.
*Also applies to the sampler:* compile's blend weight `w` inherits the
honest confidence on the next sweep — thin chase anchors shrink toward meta
appropriately. No compile code change needed.

## Phase 2 — Design 1: probabilistic completion (`archetype_miner/model.py` → re-vendor)

One new concept: **craftability** `G(item, rule) ∈ [0,1]` — how plausibly the missing
requirements can actually be hit.

1. Per missing requirement `r`: `p(r)` = its mod's **presence frequency** from the set's
   `ModInfo` catalog (`mod_frequency()`, the same roll-independent signal as the rarity
   floor); family pools use the summed member frequency (capped 1). No catalog entry →
   `p = None`.
2. Per-missing credit factor `g(r) = min(1, p / rarity_ref)` (reuse the existing
   `rarity_ref` knob ≈ "common-mod" frequency): a life/res slot ≈ 1.0, spirit ≈ 0.3,
   +proj-levels ≈ 0.09. `p None` → `g = 1.0` (graceful: old sets / uncataloged mods keep
   today's behavior).
3. `G` = geometric mean of the `g(r)` over missing requirements (scale-stable across
   1-missing vs 3-missing partials).
4. **Rare path** (`Archetype.score`): `completion = coverage + (1−coverage)·craft_credit·G`
   (reachability still gates, as today), and the open-slot bonus becomes
   `potential = mod_score · headroom · G_best` — crafting toward a rule that needs chase
   mods no longer earns the same lift as one needing life+res.
5. **Magic path — this is the point-3 remediation:** replace the flat completion with a
   realized-plus-probable blend:
   `completion = lockin · (realized + (magic_completion − realized) · G)` where
   `realized = sat_units / affix_ceiling`. A 2-mod base needing life+2res completes ≈0.9
   (unchanged); one needing chase mods completes ≈0.4 — exactly the differentiation the
   market showed (Potency 35 ex vs Rapidity 1 ex at near-equal old scores).
6. **Jewels must join the frequency catalog** (currently `_NO_FLOOR_CLASSES` excludes them
   → `G` would never bite where the misses concentrate): `tools/mod_catalog.py` emits
   Jewels `ModInfo` with **`desirability = 0`** — the rarity floor stays inert (trophy
   needs desirability > 0) while `mod_frequency()` becomes available. Re-inject into the
   shipped set (`python -m tools.mod_catalog <set>`); no re-mine needed.
7. `upgrade_targets` already annotates per-missing `chance`; add the aggregate `G` to each
   target's payload (UI shows "completion odds").
*Tests:* miner-side unit tests for `G` (geometric mean, family pools, None fallback) and
both path formulas; stasher tests re-run after `python -m tools.vendor`. *Verify:* Phase-0
harness — expect the Magic segment to move most; spot-check Rapidity vs Potency ordering.

## Phase 3 — Design 2: `score_now` / `score_potential` split (model + checker + UI)

1. `Archetype.score()` returns two contributions alongside the blended one:
   - `now`: zero craft terms — `headroom = 0`, `craft_credit = 0`; magic graded at
     `lockin · realized` completion (what the item is worth if never touched).
   - `potential`: the rule's finished `value.score × (coverage + (1−coverage)·G)` for
     reachable rules (risk-discounted destination value), else = `now`.
2. `ArchetypeSet.score_item()` aggregates both (same peak+breadth machinery) →
   `{"overall", "now", "potential", …}` with `overall = max(now, potential)` and a
   `driver: "now"|"craft"` flag. Rarity floor applies to both (a chase mod is value
   either way).
3. Checker (`stasher/evaluate/checks/archetype_set.py`): headline carries `overall` as
   today (stored score unchanged in schema); the headline reason gains a `⚒ craft` marker
   when `driver == "craft"`; `explain()` exposes both numbers + per-rule `G` for the
   detail card's score-math block.
4. UI: queue tier badge gets the ⚒ affordance (template-only change); detail card shows
   "as-is X / craft potential Y (odds Z)".
5. **Pricing seam payoff:** `stasher/pricing/plan.py`'s `TODO(eval)` heuristics consume
   the evaluator's verdict — `driver == "craft"` + free slots → `rare_potential`,
   else `rare_finished`; "strong base" = peak rule's `kept_quality` over a threshold.
*Verify:* harness unchanged-or-better (the split shouldn't move ranks — `overall` math is
preserved when `G=1`); UI smoke: a good 4/6 base shows ⚒, a finished 6-mod rare doesn't.

## Phase 4 — Magic knob recalibration (after Phases 1–3 land)

With `G` live, re-fit the remaining magic knobs against the corpus: sweep
`magic_completion ∈ [0.6..0.95]`, `magic_solo ∈ [0.3..0.6]` in `validate_eval`, pick the
pair maximizing Magic ρ subject to Rare ρ not degrading; update the **shipped set's
scoring block** (knobs live per-set, Rules page) + the `Scoring` dataclass defaults +
bump `ARCHETYPE_SET_VERSION`. Document the chosen values + the ρ deltas in RESEARCH_LOG.
*Lesson learned encoded:* the 0.95/0.55 defaults were meta-reasoned; every future knob
default cites a corpus measurement.

## Phase 5 — Ship + close the loop

Both suites green; re-vendor checked (`tools/vendor.py` byte-identical guard); set
version bump → app auto-installs + re-evaluates; user A/B spot-check against fresh price
checks (which also grow the corpus). Update `RESEARCH_LOG.md` §7 with the post-change ρ
table and the project memory's roadmap entry.

---

## Deliberately deferred (next in line after this plan)
1. **Closed-itemset mining** (RESEARCH_LOG §8) — root fix for filler-dominated archetypes;
   needs a re-mine + `--prune-stale` + re-blend cycle, so batch it with the next league
   or set refresh.
2. **T1-bucket sampling pass** on the 137 wide-spread deep-market units → calibrate
   `tier_weights`/`roll_influence` against the floor→T1 price multiple (the first
   regression of Design 4 proper).
3. **Magic-market sampling** (anchors are rare-listing prices; a magic-jewel bucket would
   anchor craft bases directly) — only if Phase 2+4 leave Magic ρ unsatisfying.

## Critical files
- `archetype_miner/archetype_miner/model.py` — Design 1+2 math (re-vendor after!).
- [stasher/evaluate/archetype_model.py](stasher/evaluate/archetype_model.py) — vendored copy (`python -m tools.vendor`).
- [stasher/pricing/price.py](stasher/pricing/price.py) — `compute_confidence` liquidity term.
- [stasher/pricing/pricer.py](stasher/pricing/pricer.py) — pass `chosen.total` through.
- [stasher/evaluate/checks/archetype_set.py](stasher/evaluate/checks/archetype_set.py) — now/potential surfacing + ⚒.
- [stasher/pricing/plan.py](stasher/pricing/plan.py) — strategy matrix consumes the evaluator verdict.
- `archetype_miner/tools/mod_catalog.py` — Jewels frequency emission (desirability 0).
- `tools/validate_eval.py` (new, this repo) — the corpus harness; Phase 0.

# Identified-items filter: extraction notes

**Source:** NeverSink's PoE2 filter, strictness **4 (Very Strict)**, `v0.10.1a.2026.158.5`
(`FilterBlade/FilterBlade_4_Very Strict.filter`).
**Output:** `identified.filter` (load it in Settings, or copy over `stasher.filter`).

## Why Very Strict

Stashler only flags from data it can see locally, and it runs over items you've
*already listed* (so already identified). For identified gear the base sets the
ceiling, so the useful thing to extract is FilterBlade's **per-class base tiering**.
Across strictness levels the rare-gear tiers prune like this:

| Strictness | Rare gear tiers kept |
|---|---|
| 3 Strict | T1, T2, T3 + T4/T5 catch-all by class |
| **4 Very Strict** | **T1 + T2 (the best bases per class), ilvl 82+** |
| 5 Uber Strict | T1 only |
| 6 Uber Plus | none (relies on mod tiers instead) |

Level 4 is the sweet spot: it is exactly "best + strong base per class" without the
weak-base noise of T3-T5 or the over-pruning of levels 5-6. T1 jewellery + T2
jewellery and T1_top + T2_top gear were lifted verbatim.

## What was extracted vs. dropped

- **Kept:** `[1300]` rare jewellery (rings/amulets/belts) T1+T2, `[1400]` rare gear
  T1_top + T2_top. Conditions reduced to the ones Stashler supports:
  `BaseType`, `Rarity`, `ItemLevel`.
- **Dropped:** crafting/chancing bases (`[0300]`/`[0301]`, normal/magic unidentified
  canvases — not "identified gear"), uniques (`[2800]`, value is per *unique name*,
  which the filter can't read; Stashler's `[[unique_roll]]` rule handles these),
  and all currency/map/gem/socketable sections (not gear).

---

## Fields used in the source — support status

Stashler's filter parser (`stasher/evaluate/checks/item_filter.py`) now supports
`BaseType`, `Class`, `Rarity`, `ItemLevel`/`ilvl`, `Quality`, `Sockets`,
`HasExplicitMod`, `Corrupted`, `Mirrored`, `Identified`. Items 1–3 below were the
gaps for *identified* items and have since been implemented (see "Implemented" note
under each). Item 4 remains future work.

### 1. `HasExplicitMod` — highest value ✅ IMPLEMENTED
Section `[[0400]] IDENTIFIED MODS` is the part of FilterBlade aimed squarely at
identified items: it flags by **named high-value affix**, per class. Examples pulled
from the source:

| Class | Flagged mods (`HasExplicitMod >=1 ...`) |
|---|---|
| Boots | `Hellion's` |
| Amulets | `Countess'`, `of the Sharpshooter` |
| Staves, Wands | `Runic`, `of the Wizard`, `of Inferno`, `of Frostbite`, `of Thunder`, `of Armageddon`, `of Grief` |
| Sceptres | `King's`, `of the Slavedriver`, `Empowering` |
| Bows, Crossbows | `of Many`, `Merciless`, `of the Sniper` |
| Spears | `Merciless`, `Flaring`, `Amazon's`, `of the Sniper` |
| Quarterstaves, Talismans, Two Hand Maces | `Merciless`, `of War` |

> **Implemented:** `HasExplicitMod [<count-op>] "name" ...` reads affix names from
> `extended.mods.explicit[].name` (and falls back to rendered stat text), so the
> whole `[[0400]]` section is now ported into `identified.filter` 1:1. The optional
> leading count token (`>=1` default, also `>=2`, `==1`, …) gates how many listed
> mods must match. `itemdata.explicit_mod_names()` exposes the names.

### 2. `Sockets` — was parsed but stubbed ✅ IMPLEMENTED
Previously accepted by the parser but `_match_condition` hard-coded the value to `0`,
so any `Sockets >= N` never matched. Now wired to `itemdata.socket_count()`
(`len(item["sockets"])`), so FilterBlade's exceptional-base rules (`[0300]`,
`Sockets >= 2/3`) work.

### 3. Item-state flags: `Corrupted`, `Mirrored`, `Identified` ✅ IMPLEMENTED
Boolean state conditions (`Corrupted False`, `Identified True`, …) now resolve via
`itemdata.is_corrupted/is_mirrored/is_identified` (`corrupted` / `duplicated` /
`identified` in the listing JSON; `Identified` defaults to True when absent, since
Stashler's items are listed). `TwiceCorrupted` is **not** implemented — the API has no
direct flag for it (it needs counting corruption implicits), so it still warns-and-skips.

### 4. Minor / lower priority (still unsupported)
- `BaseArmour` / `BaseEvasion` / `BaseEnergyShield` / `BaseDefencePercentile` — defence
  rolls; would let Stashler grade armour bases by their actual defence roll, not just
  base name. Useful but needs per-base data tables.
- `AreaLevel`, `WaystoneTier`, `StackSize`, `GemLevel`, `UnidentifiedItemTier` — drop-time
  / non-gear context; not relevant to scoring identified listings, safe to keep ignoring.

> Note: unsupported conditions don't break the filter — the parser emits a warning and
> ignores the line. So you can paste richer FilterBlade rules in as-is; only the
> supported conditions take effect.

---

## Implementation summary (this change)

- `stasher/evaluate/itemdata.py`: added `is_corrupted`, `is_mirrored`, `is_identified`,
  `socket_count`, `explicit_mod_names`.
- `stasher/evaluate/checks/item_filter.py`: registered `hasexplicitmod` + the boolean
  flags in `_SUPPORTED`; `Sockets` now uses the real count; added `_bool_match` and
  `_has_explicit_mod` (with a glued/ spaced count-operator parser).
- `tests/test_checks.py`: added coverage for sockets, state flags, and
  `HasExplicitMod` (by name, by text, and the count gate). Full suite green (49 tests).
- `identified.filter`: ported the `[[0400]] IDENTIFIED MODS` per-class affix rules
  using the new `HasExplicitMod` support.

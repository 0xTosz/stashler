"""Headline aggregate stats the market prices on — computed from the item's own properties.

For a *finished* item these aggregates, not the individual mods, anchor the search (a mid
base with great rolls competes with a top base with mid rolls at the same total):

* weapons  → ``dps`` / ``pdps`` (physical) / ``edps`` (elemental), from damage × APS;
* armours  → total armour / energy-shield / evasion (the displayed totals already fold in
  base × %increase × quality).

The trade2 filter group/field names live in ``WEAPON_TARGET`` / ``DEFENCE_TARGET``. The
weapon fields (``dps``/``pdps``/``edps`` under ``weapon_filters``) are confirmed live trade
features; the defence group name must be **confirmed against /api/trade2/data/filters in the
Phase-0 harvest** (see ``PRICING_MODULE_PLAN.md`` §11) — it is isolated here so that's a
one-line change.
"""

from __future__ import annotations

import re

from ..evaluate import itemdata

_RANGE_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")

# PoE2 trade puts both weapon DPS and defence totals in ONE `equipment_filters` group
# (fields: ar/es/ev/dps/pdps/edps/aps/crit/block/...). Confirmed from EE2's
# trade/pathofexile-trade.ts (the Phase-0 harvest).
WEAPON_TARGET = {"dps": "equipment_filters.dps", "pdps": "equipment_filters.pdps",
                 "edps": "equipment_filters.edps"}
DEFENCE_TARGET = {"armour": "equipment_filters.ar", "energy_shield": "equipment_filters.es",
                  "evasion": "equipment_filters.ev"}


def _avg(text: str) -> float:
    """Average of a ``"10-20"`` damage range (or a bare number)."""
    m = _RANGE_RE.search(text or "")
    if m:
        return (float(m.group(1)) + float(m.group(2))) / 2.0
    nums = _NUM_RE.findall(text or "")
    return float(nums[0]) if nums else 0.0


def _prop(item: dict, name_lower: str) -> list:
    for prop in item.get("properties") or []:
        if str(prop.get("name", "")).lower() == name_lower:
            return prop.get("values") or []
    return []


def _first_val(values: list) -> str:
    return str(values[0][0]) if values and values[0] else ""


def weapon_dps(item: dict) -> dict[str, float]:
    """``{dps, pdps, edps}`` for a weapon, or ``{}`` if it has no attack-rate/damage.

    Physical damage already includes quality (the listing renders the post-quality range).
    """
    aps_vals = _prop(item, "attacks per second")
    if not aps_vals:
        return {}
    try:
        aps = float(_NUM_RE.findall(_first_val(aps_vals))[0])
    except (IndexError, ValueError):
        return {}
    if aps <= 0:
        return {}

    pdps = _avg(_first_val(_prop(item, "physical damage"))) * aps
    # Elemental damage can carry several ranges (fire/cold/lightning); sum their averages.
    edps = 0.0
    for val in _prop(item, "elemental damage"):
        if val:
            edps += _avg(str(val[0]))
    edps *= aps
    chaos = _avg(_first_val(_prop(item, "chaos damage"))) * aps
    dps = pdps + edps + chaos
    out = {}
    if dps > 0:
        out["dps"] = round(dps, 1)
    if pdps > 0:
        out["pdps"] = round(pdps, 1)
    if edps > 0:
        out["edps"] = round(edps, 1)
    return out


def defence_totals(item: dict) -> dict[str, float]:
    """``{armour, energy_shield, evasion}`` displayed totals present on the item."""
    out: dict[str, float] = {}
    for key, prop_name in (
        ("armour", "armour"),
        ("energy_shield", "energy shield"),
        ("evasion", "evasion rating"),
    ):
        vals = _prop(item, prop_name)
        if vals:
            try:
                total = float(_NUM_RE.findall(_first_val(vals))[0])
            except (IndexError, ValueError):
                continue
            if total > 0:
                out[key] = total
    return out


def headline_for(item: dict) -> dict[str, tuple[str, float]]:
    """All headline aggregates for an item as ``{key: (trade_target, value)}``.

    Combines weapon DPS and armour totals; the plan builder picks which to anchor on by
    item class. Empty for items with neither (jewellery, jewels, flasks)."""
    out: dict[str, tuple[str, float]] = {}
    for key, value in weapon_dps(item).items():
        out[key] = (WEAPON_TARGET[key], value)
    for key, value in defence_totals(item).items():
        out[key] = (DEFENCE_TARGET[key], value)
    return out


def is_weapon(item: dict) -> bool:
    cls = (itemdata.item_class(item) or "").lower()
    return bool(weapon_dps(item)) or "weapon" in cls or any(
        w in cls for w in ("bow", "wand", "staff", "sceptre", "mace", "spear",
                           "crossbow", "quarterstaff", "sword", "axe", "dagger", "claw")
    )

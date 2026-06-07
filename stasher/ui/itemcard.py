"""Build a trade-tooltip-style view model from a stored item, for the queue UI.

Pulls the readable pieces out of the raw fetch JSON -- class line, properties,
requirements, implicit/explicit mods with their P#/S# tier tags, and weapon DPS -- so
the template can render something close to the in-game / trade-site tooltip.
"""

from __future__ import annotations

from ..evaluate.itemdata import (
    base_type,
    clean_mod_text,
    explicit_display,
    ilvl,
    name as item_name,
    rarity,
)


def _prop_value(values) -> tuple[str, bool]:
    """Join a property's value tokens; flag augmented (blue) values (type == 1)."""
    parts = [str(v[0]) for v in values if v]
    aug = any(len(v) > 1 and v[1] == 1 for v in values)
    return ", ".join(parts), aug


def _properties(item: dict) -> tuple[str | None, list[dict]]:
    """Return (class_line, [{label, value, aug}]). The class line is the lone
    property with no values (e.g. ``[Crossbow]``)."""
    klass = None
    props: list[dict] = []
    for p in item.get("properties") or []:
        label = clean_mod_text(str(p.get("name", "")))
        values = p.get("values") or []
        if not values:
            if klass is None:
                klass = label
            continue
        value, aug = _prop_value(values)
        props.append({"label": label, "value": value, "aug": aug})
    return klass, props


def _requirements(item: dict) -> str | None:
    parts: list[str] = []
    for r in item.get("requirements") or []:
        label = clean_mod_text(str(r.get("name", "")))
        values = r.get("values") or []
        val = values[0][0] if values and values[0] else ""
        if not val:
            continue
        # "Level 59" reads naturally; attributes read "58 Str".
        parts.append(f"{label} {val}" if label.lower() == "level" else f"{val} {label}")
    return ", ".join(parts) if parts else None


def build_card(item: dict) -> dict:
    klass, props = _properties(item)
    ext = item.get("extended") or {}
    dps = None
    if ext.get("dps"):
        dps = {
            "dps": ext.get("dps"),
            "pdps": ext.get("pdps"),
            "edps": ext.get("edps"),
        }
    return {
        "rarity": rarity(item) or "Normal",
        "name": item_name(item),
        "base": base_type(item),
        "klass": klass,
        "icon": item.get("icon"),
        "ilvl": ilvl(item),
        "props": props,
        "reqs": _requirements(item),
        "implicits": [clean_mod_text(m) for m in (item.get("implicitMods") or [])],
        "explicits": explicit_display(item),
        "dps": dps,
    }

"""Affix stat-text normalization — kept byte-identical to the miner's ``archetype_miner``
``normalize`` so the ``mod_key`` derived from a trade item matches the keys in a mined
``ArchetypeSet`` (the YAML contract). Don't "improve" the regexes here in isolation.
"""

from __future__ import annotations

import re

_MARKUP_PIPE = re.compile(r"\[([^\]|]+)\|([^\]]+)\]")
_MARKUP_PLAIN = re.compile(r"\[([^\]]+)\]")
_NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?")
_DEFENCE = {"armour": "armour", "evasion rating": "evasion", "evasion": "evasion",
            "energy shield": "energy_shield", "energyshield": "energy_shield"}


def clean_mod_text(text: str) -> str:
    """``[a|b]`` -> ``b``, ``[a]`` -> ``a``; whitespace collapsed."""
    text = _MARKUP_PIPE.sub(r"\2", text)
    text = _MARKUP_PLAIN.sub(r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def mod_key(clean: str) -> str:
    """Roll-independent grouping key: numbers collapsed to ``#``, lower-cased."""
    return _NUMBER.sub("#", clean).lower().strip()


def mod_magnitude(clean: str) -> float | None:
    """Representative rolled magnitude: mean of the line's numbers (ranges average), or None."""
    nums = [float(x) for x in _NUMBER.findall(clean)]
    return sum(nums) / len(nums) if nums else None


def defence_types(item: dict) -> tuple[str, ...]:
    """Which defence stats (armour/evasion/energy_shield) the item's base exposes — used only for
    the archetype defence-segment gate (presence, not magnitude). Mirrors the miner."""
    out: set[str] = set()
    for prop in item.get("properties") or []:
        canon = _DEFENCE.get(clean_mod_text(str(prop.get("name", ""))).strip().lower())
        if not canon:
            continue
        vals = prop.get("values") or []
        if vals and vals[0] and _NUMBER.search(str(vals[0][0])):
            out.add(canon)
    return tuple(sorted(out))

"""Helpers for reading fields out of a GGG ``/fetch`` item dict.

The stored ``raw_json`` is the full fetch entry ``{id, listing, item}``; checkers
operate on the ``item`` sub-dict. Mod text from the API carries markup like
``[Physical] Damage`` or ``[Critical|Critical Hit] Chance`` -- :func:`clean_mod_text`
renders it the way it reads in-game so rule authors can write natural patterns.
"""

from __future__ import annotations

import re
from typing import Any

_FRAME_RARITY = {
    0: "Normal", 1: "Magic", 2: "Rare", 3: "Unique", 4: "Gem",
    5: "Currency", 6: "Divination Card", 8: "Prophecy", 9: "Relic",
}

# Rarity ordering for filter comparisons (Normal < Magic < Rare < Unique).
RARITY_ORDER = {"Normal": 0, "Magic": 1, "Rare": 2, "Unique": 3}

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_MARKUP_PIPE_RE = re.compile(r"\[([^\]|]+)\|([^\]]+)\]")
_MARKUP_PLAIN_RE = re.compile(r"\[([^\]]+)\]")


def rarity(item: dict) -> str | None:
    ft = item.get("frameType")
    if isinstance(ft, int):
        return _FRAME_RARITY.get(ft)
    return item.get("frameTypeId")


def name(item: dict) -> str | None:
    """The item's unique/rare name (empty for normal/magic)."""
    return item.get("name") or None


def base_type(item: dict) -> str:
    return item.get("baseType") or item.get("typeLine") or ""


def type_line(item: dict) -> str:
    return item.get("typeLine") or item.get("baseType") or ""


def ilvl(item: dict) -> int | None:
    val = item.get("ilvl")
    return val if isinstance(val, int) else None


def clean_mod_text(text: str) -> str:
    """Turn API mod markup into plain readable text.

    ``[a|b]`` -> ``b`` (the in-game display token), ``[a]`` -> ``a``.
    """
    text = _MARKUP_PIPE_RE.sub(r"\2", text)
    text = _MARKUP_PLAIN_RE.sub(r"\1", text)
    return text


def affix_texts(item: dict) -> list[str]:
    """Cleaned explicit + implicit mod lines (each as one string)."""
    out: list[str] = []
    for key in ("explicitMods", "implicitMods"):
        for line in item.get(key) or []:
            out.append(clean_mod_text(line))
    return out


def numbers(text: str) -> list[float]:
    return [float(m) for m in _NUMBER_RE.findall(text)]


def quality(item: dict) -> int:
    """Quality percent from the properties block, 0 if absent."""
    for prop in item.get("properties") or []:
        if str(prop.get("name", "")).lower().startswith("quality"):
            vals = prop.get("values") or []
            if vals and vals[0]:
                nums = numbers(str(vals[0][0]))
                if nums:
                    return int(nums[0])
    return 0


def mods_for_lines(item: dict, kind: str) -> list[dict | None]:
    """The ``extended.mods`` entry behind each displayed mod line, aligned by index.

    ``extended.hashes.<kind>[i]`` corresponds to ``<kind>Mods[i]``; its second element
    indexes into ``extended.mods.<kind>`` (where tier + magnitudes live). Returns one
    mod dict (or None) per displayed line.
    """
    ext = item.get("extended") or {}
    mods = (ext.get("mods") or {}).get(kind) or []
    hashes = (ext.get("hashes") or {}).get(kind) or []
    out: list[dict | None] = []
    for entry in hashes:
        mod = None
        if isinstance(entry, (list, tuple)) and len(entry) == 2 and entry[1]:
            idx = entry[1][0]
            if isinstance(idx, int) and 0 <= idx < len(mods):
                mod = mods[idx]
        out.append(mod)
    return out


def explicit_display(item: dict) -> list[dict]:
    """Each explicit line as ``{tier, text}`` (text cleaned of markup)."""
    texts = item.get("explicitMods") or []
    per_line = mods_for_lines(item, "explicit")
    out = []
    for i, text in enumerate(texts):
        mod = per_line[i] if i < len(per_line) else None
        out.append({"tier": (mod or {}).get("tier") or "", "text": clean_mod_text(text)})
    return out


def explicit_roll_percents(item: dict) -> list[float]:
    """Roll quality (0..1) for each rollable explicit magnitude.

    Pairs each mod's per-tier ``magnitudes`` (min/max range) with the realized values
    parsed out of its displayed line. Magnitudes whose range is a single point
    (min == max) carry no roll and are skipped.
    """
    texts = item.get("explicitMods") or []
    per_line = mods_for_lines(item, "explicit")
    percents: list[float] = []
    for i, text in enumerate(texts):
        mod = per_line[i] if i < len(per_line) else None
        if not mod:
            continue
        nums = numbers(text)
        for j, mag in enumerate(mod.get("magnitudes") or []):
            try:
                lo, hi = float(mag.get("min")), float(mag.get("max"))
            except (TypeError, ValueError):
                continue
            if hi <= lo or j >= len(nums):
                continue
            percents.append(max(0.0, min(1.0, (nums[j] - lo) / (hi - lo))))
    return percents

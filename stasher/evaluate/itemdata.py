"""Helpers for reading fields out of a GGG ``/fetch`` item dict.

The stored ``raw_json`` is the full fetch entry ``{id, listing, item}``; checkers
operate on the ``item`` sub-dict. Mod text from the API carries markup like
``[Physical] Damage`` or ``[Critical|Critical Hit] Chance`` -- :func:`clean_mod_text`
renders it the way it reads in-game so rule authors can write natural patterns.
"""

from __future__ import annotations

import base64
import re
from functools import lru_cache
from typing import Any

from . import affix_norm as _affix_norm

_FRAME_RARITY = {
    0: "Normal", 1: "Magic", 2: "Rare", 3: "Unique", 4: "Gem",
    5: "Currency", 6: "Divination Card", 8: "Prophecy", 9: "Relic",
}

# Rarity ordering for filter comparisons (Normal < Magic < Rare < Unique).
RARITY_ORDER = {"Normal": 0, "Magic": 1, "Rare": 2, "Unique": 3}

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_MARKUP_PIPE_RE = re.compile(r"\[([^\]|]+)\|([^\]]+)\]")
_MARKUP_PLAIN_RE = re.compile(r"\[([^\]]+)\]")

# The trade /fetch item JSON carries no item class, but the `icon` URL is a base64
# token wrapping the art path, e.g. ".../2DItems/Weapons/TwoHandWeapons/Bows/Bow1".
# A distinctive folder in that path identifies the class, which we map to the trade
# class names that loot filters use in `Class ==`. Verified against the live archive;
# folders not seen there are best-effort. See item_class() / _class_from_icon().
_ICON_TOKEN_RE = re.compile(r"/image/([A-Za-z0-9_-]+)")
_ICON_CLASS_FOLDERS = {
    # weapons
    "Wands": "Wands",
    "Scepters": "Sceptres",
    "OneHandMaces": "One Hand Maces",
    "OneHandSpears": "Spears",
    "Spears": "Spears",
    "Crossbows": "Crossbows",
    "Bows": "Bows",
    "Staves": "Staves",
    "WarStaves": "Quarterstaves",
    "TwoHandMaces": "Two Hand Maces",
    # off-hand (the art folder "Shields" also holds bucklers/targes, which PoE2 trade
    # and the NeverSink filters group under "Shields" too)
    "Foci": "Foci",
    "Talismans": "Talismans",
    "Shields": "Shields",
    "Quivers": "Quivers",
    # armour
    "BodyArmours": "Body Armours",
    "Helmets": "Helmets",
    "Gloves": "Gloves",
    "Boots": "Boots",
    # jewellery / other gear
    "Rings": "Rings",
    "Amulets": "Amulets",
    "Belts": "Belts",
    "Jewels": "Jewels",
    "Charms": "Charms",
}


@lru_cache(maxsize=8192)
def _class_from_icon(icon: str) -> str | None:
    """Decode the class out of an item's icon art-path, or None if undeterminable."""
    m = _ICON_TOKEN_RE.search(icon or "")
    if not m:
        return None
    token = m.group(1) + "=" * (-len(m.group(1)) % 4)
    try:
        raw = base64.urlsafe_b64decode(token).decode("utf-8", "replace")
    except (ValueError, UnicodeError):
        return None
    for segment in raw.split("/"):
        cls = _ICON_CLASS_FOLDERS.get(segment)
        if cls:
            return cls
    return None


def item_class(item: dict) -> str | None:
    """The item's class (e.g. ``Bows``, ``Body Armours``).

    Prefers an explicit class if the API ever provides one (``extended.baseClass`` /
    ``class``); otherwise derives it from the icon art-path, since trade ``/fetch``
    data omits the class. Returns None when it can't be determined."""
    ext = item.get("extended") or {}
    cls = ext.get("baseClass") or item.get("class")
    if cls:
        return cls
    return _class_from_icon(item.get("icon") or "")


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


# Metacharacters of the PoE2 stash search engine (Google RE2), escaped literally.
_RE2_SPECIAL = re.compile(r"([\\.^$|?*+()\[\]{}])")


def stash_regex(item: dict, max_len: int = 250) -> str:
    """A stash-search regex (PoE2 Ctrl-F, RE2) that highlights this item in its tab.

    Anchors on the most distinctive *single line* of the item's text: a rare/unique's
    generated name (e.g. ``Dusk Grasp``), else a magic item's full type line (e.g.
    ``Minister's Omen Sceptre of the Prodigy``), else the base type. These are nearly
    unique per tab, so false positives are rare; identical items (e.g. two copies of the
    same unique) can only be told apart by stash position. RE2 has no lookahead, so we
    don't AND fragments across lines. Capped at ``max_len`` (the search box limit)."""
    primary = (name(item) or type_line(item) or base_type(item) or "").strip()
    if not primary:
        return ""
    return _RE2_SPECIAL.sub(r"\\\1", primary)[:max_len]


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


def is_corrupted(item: dict) -> bool:
    return bool(item.get("corrupted"))


def is_mirrored(item: dict) -> bool:
    """GGG marks mirrored copies with ``duplicated``."""
    return bool(item.get("duplicated"))


def is_identified(item: dict) -> bool:
    """Identified state; absent is treated as identified (Stashler's items are listed)."""
    return bool(item.get("identified", True))


def socket_count(item: dict) -> int:
    """Number of socket slots (PoE2 rune sockets), 0 if none."""
    socks = item.get("sockets")
    return len(socks) if isinstance(socks, list) else 0


def explicit_mod_names(item: dict) -> list[str]:
    """Affix names behind the explicit mods (e.g. ``Hellion's``, ``of the Sharpshooter``).

    These come from ``extended.mods.explicit[].name`` -- the prefix/suffix names that
    FilterBlade's ``HasExplicitMod`` keys off, which never appear in the rendered stat
    text. Empty when the listing carries no ``extended`` mod data.
    """
    ext = item.get("extended") or {}
    mods = (ext.get("mods") or {}).get("explicit") or []
    return [n for m in mods if (n := (m or {}).get("name"))]


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


def _estimated_line_text(text: str, mod: dict, stat_hash: str | None) -> str:
    """A summed display line re-stated as *one* affix's share: the rendered number(s) replaced by
    this affix's range midpoint(s) for ``stat_hash`` and prefixed ``≈`` (it's an estimate, since the
    trade site fused two affixes' same-stat rolls into one number). Falls back to the rendered text
    when no usable magnitude range is present."""
    clean = clean_mod_text(text)
    mids: list[float] = []
    for mag in mod.get("magnitudes") or []:
        if mag.get("hash") != stat_hash:
            continue
        try:
            mids.append((float(mag["min"]) + float(mag["max"])) / 2.0)
        except (TypeError, ValueError, KeyError):
            continue
    if not mids:
        return clean
    vals = iter(mids)

    def _repl(m: "re.Match") -> str:
        try:
            return str(int(round(next(vals))))
        except StopIteration:
            return m.group(0)

    return "≈ " + _NUMBER_RE.sub(_repl, clean, count=len(mids))


def explicit_display(item: dict) -> list[dict]:
    """Each explicit **affix** as ``{tier, hybrid, lines}`` (text lines cleaned of markup).

    Built from the real affixes (``extended.mods.explicit``), not the rendered lines, because the
    trade site renders affixes lossily: a **hybrid** (one affix, two stats) is split across two
    lines, and two affixes rolling the same stat are **summed** into one line. Both are undone here:
    each entry is one affix, with all of its stat lines grouped under its single tier — so a hybrid
    shows as one mod (``hybrid`` True), and a stat that was summed away from its hybrid partner is
    still shown with it (re-stated as that affix's midpoint share, ``_estimated_line_text``, so a
    half of a hybrid never floats off as a phantom standalone mod). Entries are ordered by first
    appearance. Falls back to one entry per rendered line when no aligned structured data is present.
    """
    texts = item.get("explicitMods") or []
    ext = item.get("extended") or {}
    emods = (ext.get("mods") or {}).get("explicit") or []
    hashes = (ext.get("hashes") or {}).get("explicit") or []
    if not emods or len(hashes) != len(texts):
        return [{"tier": "", "hybrid": False, "lines": [clean_mod_text(t)]} for t in texts]

    # stat hash -> (its display-line index, the affix indices that contribute to that line). A stat
    # appears on exactly one line (the site sums same-stat affixes into it); a hybrid's two stats
    # are two different hashes on two lines.
    line_of: dict[str, int] = {}
    contributors: dict[str, list[int]] = {}
    for i, entry in enumerate(hashes):
        if not (isinstance(entry, (list, tuple)) and entry):
            continue
        h = entry[0]
        idxs = (entry[1] if len(entry) == 2 and isinstance(entry[1], list) else [])
        line_of.setdefault(h, i)
        contributors[h] = [x for x in idxs if isinstance(x, int)]

    entries: list[tuple[int, dict]] = []
    for mod in emods:
        seen_h: list[str] = []
        for mag in mod.get("magnitudes") or []:
            h = mag.get("hash")
            if h in line_of and h not in seen_h:
                seen_h.append(h)
        if not seen_h:
            continue
        lines = []
        for h in seen_h:
            li = line_of[h]
            if len(contributors.get(h, [])) > 1:           # summed with another affix → estimate
                lines.append(_estimated_line_text(texts[li], mod, h))
            else:
                lines.append(clean_mod_text(texts[li]))
        entries.append((min(line_of[h] for h in seen_h),
                        {"tier": mod.get("tier") or "", "hybrid": len(seen_h) > 1, "lines": lines}))
    entries.sort(key=lambda e: e[0])
    return [e for _, e in entries]


def _hash_midpoint(mod: dict | None, stat_hash: str | None) -> float | None:
    """Mean of the (min+max)/2 ranges a single affix contributes to one stat hash, or None.

    Used to estimate a merged affix's own share of a summed display line: when the trade site
    collapses two affixes that roll the same stat into one line, the rendered number is their
    *sum* and no per-affix split is recoverable from the text -- but each affix's own roll range
    bounds it, so the range midpoint is a stable, never-inflated per-affix magnitude estimate.
    """
    vals: list[float] = []
    for mag in (mod or {}).get("magnitudes") or []:
        if mag.get("hash") != stat_hash:
            continue
        try:
            vals.append((float(mag["min"]) + float(mag["max"])) / 2.0)
        except (TypeError, ValueError, KeyError):
            continue
    return sum(vals) / len(vals) if vals else None


def explicit_affix_mods(item: dict) -> tuple[dict[str, float | None], int]:
    """De-merged ``({mod_key: magnitude}, affix_count)`` for the explicit affixes.

    The trade site *renders* affixes lossily: two prefixes that roll the same stat are summed
    into one display line, and a hybrid (one affix, two stats) is split across two lines -- so
    counting/keying off ``explicitMods`` text both miscounts slots and inflates magnitudes. This
    reads ``extended.mods``/``extended.hashes`` (one entry per *underlying* affix + the per-line
    contributing-mod indices) to recover the per-affix picture the miner mines from poe.ninja:

    * **affix_count** = number of real affixes (``extended.mods.explicit``), not display lines.
    * a line with a **single** contributing affix keeps its real rolled magnitude (the text).
    * a **merged** line (≥2 contributing affixes) contributes each affix under the shared key at
      its own range-midpoint estimate (the summed text is discarded), so the key's magnitude
      reflects a single real roll rather than the inflated sum.

    Keys collide to the strongest magnitude (a requirement is satisfied by the best matching
    affix). Falls back to plain rendered-text parsing when the listing carries no structured mod
    data (unidentified / modless items) or the line/affix arrays don't align.
    """
    norm = _affix_norm
    explicit = item.get("explicitMods") or []
    ext = item.get("extended") or {}
    emods = (ext.get("mods") or {}).get("explicit") or []
    hashes = (ext.get("hashes") or {}).get("explicit") or []

    mods: dict[str, float | None] = {}

    def _put(key: str, mag: float | None) -> None:
        if not key:
            return
        if key not in mods or (mag is not None and (mods[key] is None or mag > mods[key])):
            mods[key] = mag

    if not emods or len(hashes) != len(explicit):
        for raw in explicit:
            clean = norm.clean_mod_text(raw)
            if clean:
                _put(norm.mod_key(clean), norm.mod_magnitude(clean))
        return mods, len(explicit)

    for line, entry in zip(explicit, hashes):
        clean = norm.clean_mod_text(line)
        key = norm.mod_key(clean)
        stat = entry[0] if isinstance(entry, (list, tuple)) and entry else None
        idxs = (entry[1] if isinstance(entry, (list, tuple)) and len(entry) == 2
                and isinstance(entry[1], list) else [])
        if len(idxs) <= 1:
            _put(key, norm.mod_magnitude(clean))          # one affix owns the line: real roll
        else:
            for idx in idxs:                              # merged: each affix's own bounded share
                mod = emods[idx] if isinstance(idx, int) and 0 <= idx < len(emods) else None
                _put(key, _hash_midpoint(mod, stat))
    return mods, len(emods)


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

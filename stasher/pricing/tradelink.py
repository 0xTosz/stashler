"""Build an official-trade-site URL that opens a prefilled search for an item.

A QoL "verify it yourself" link: the most **restrictive** sensible search — the exact base
type + every explicit affix at a slightly-relaxed floor — so the user lands on a tight result
set and can *ease* filters on the site (far easier than re-entering mods by hand).

Uses the trade site's ``?q=<url-encoded JSON>`` state param, so it costs **no API request**
(unlike a price check). Reuses :func:`stasher.client._build_query` for the exact body shape
(incl. the stats-group placement) and the item's real ``extended`` stat ids + tier floors.
"""

from __future__ import annotations

import json
from urllib.parse import quote

from ..client import _build_query
from ..evaluate import itemdata
from . import pseudo


def _min_of(value: float) -> int | float:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _stat_filters(item: dict) -> list[dict]:
    """Stat filters for the item's explicit affixes at slightly-relaxed floors, using the real
    ``extended`` stat ids — with **fungible groups collapsed into pseudo totals** wherever a
    pseudo covers ≥2 of the item's affixes (most prominently elemental resistances: fire +
    cold + lightning become one ``pseudo_total_elemental_resistance ≥ sum`` filter, which is
    far more robust on the site — listings satisfy it with ANY element mix instead of having
    to match the exact lines). Singly-covered pseudos stay as their exact affix (tighter and
    just as liquid). The consumed components are deduped out so nothing is double-required."""
    totals = pseudo.item_stat_totals(item)
    floors = pseudo.item_stat_floors(item)
    out: list[dict] = []
    consumed: set[str] = set()
    for ps in pseudo.pseudos_for(item):
        if len([c for c in ps.components if c in totals]) < 2:
            continue   # one underlying affix: the exact line is tighter and just as liquid
        m = ps.floor or round(ps.value * 0.9, 2)
        out.append({"id": ps.pseudo_id, "value": {"min": _min_of(m)}, "disabled": False})
        consumed.update(ps.components)
    for sid, val in totals.items():
        if sid in consumed:
            continue
        m = floors.get(sid) or round(val * 0.9, 2)
        out.append({"id": sid, "value": {"min": _min_of(m)}, "disabled": False})
    return out


# Max affixes per prefix/suffix group, by rarity (rare = 3 prefix + 3 suffix, magic = 1 + 1).
_MAX_AFFIX = {"rare": 3, "magic": 1}


def _empty_affix_slots(item: dict, rarity: str) -> tuple[int, int]:
    """``(empty_prefix, empty_suffix)`` = the rarity cap minus the item's prefix/suffix counts.

    Each ``extended.mods.explicit`` entry's ``tier`` encodes the kind in trade2 data — ``P*`` is
    a prefix, ``S*`` a suffix (e.g. ``P5`` / ``S1``). Empty slots are craftable headroom a buyer
    values, so a 3-prefix / 1-suffix rare has 2 empty suffix slots."""
    cap = _MAX_AFFIX.get(rarity, 0)
    if not cap:
        return 0, 0
    emods = ((item.get("extended") or {}).get("mods") or {}).get("explicit") or []
    prefixes = sum(1 for m in emods if str(m.get("tier", "")).startswith("P"))
    suffixes = sum(1 for m in emods if str(m.get("tier", "")).startswith("S"))
    return max(0, cap - prefixes), max(0, cap - suffixes)


def build_trade_url(item: dict, *, league: str, base_url: str, realm: str,
                    status: str = "any") -> str | None:
    """A ``pathofexile.com/trade2`` search URL prefilled with this item's base + affixes (+ any
    empty prefix/suffix slots), or None if there's nothing to anchor on. Account-free."""
    rarity = (itemdata.rarity(item) or "").lower()
    base = itemdata.base_type(item) or None
    stat_filters = _stat_filters(item)

    # Empty affix slots are craftable headroom — require the comparable to have at least as many
    # open prefix/suffix slots (pseudo stats). E.g. a 3-prefix/1-suffix rare adds "empty suffix
    # >= 2". The user can ease these on the site.
    ids = pseudo.empty_slot_ids()
    empty_prefix, empty_suffix = _empty_affix_slots(item, rarity)
    if empty_prefix and ids.get("prefix"):
        stat_filters.append({"id": ids["prefix"], "value": {"min": empty_prefix}, "disabled": False})
    if empty_suffix and ids.get("suffix"):
        stat_filters.append({"id": ids["suffix"], "value": {"min": empty_suffix}, "disabled": False})

    if not base and not stat_filters:
        return None
    extra: dict = {}
    if rarity in ("normal", "magic", "rare", "unique"):
        extra["type_filters"] = {"filters": {"rarity": {"option": rarity}}}
    if stat_filters:
        extra["stats"] = [{"type": "and", "filters": stat_filters}]
    # A rare's generated name isn't a searchable filter; only anchor a unique by name.
    name = itemdata.name(item) if rarity == "unique" else None

    body = _build_query("", extra, status=status, market=True, type_name=base, item_name=name)
    q = quote(json.dumps(body, separators=(",", ":")))
    return f"{base_url}/trade2/search/{realm}/{quote(league, safe='')}?q={q}"

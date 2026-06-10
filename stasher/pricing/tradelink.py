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


def _stat_filters(item: dict) -> list[dict]:
    """One stat filter per explicit affix at its tier floor (a slightly-relaxed min), using the
    real ``extended`` stat ids."""
    totals = pseudo.item_stat_totals(item)
    floors = pseudo.item_stat_floors(item)
    out: list[dict] = []
    for sid, val in totals.items():
        m = floors.get(sid) or round(val * 0.9, 2)
        if isinstance(m, float) and m.is_integer():
            m = int(m)
        out.append({"id": sid, "value": {"min": m}, "disabled": False})
    return out


def build_trade_url(item: dict, *, league: str, base_url: str, realm: str,
                    status: str = "any") -> str | None:
    """A ``pathofexile.com/trade2`` search URL prefilled with this item's base + affixes, or
    None if there's nothing to anchor on. The search is account-free (market-wide)."""
    rarity = (itemdata.rarity(item) or "").lower()
    base = itemdata.base_type(item) or None
    stat_filters = _stat_filters(item)
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

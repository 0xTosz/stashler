"""Trade item categories used to partition the backfill search.

The authoritative list comes from the live ``/api/trade2/data/filters`` endpoint
(``type_filters`` -> ``category`` options). We fetch it dynamically; the static list
below is only a best-effort fallback if that request fails. Unknown/invalid category
ids simply produce a failed search which the backfill logs and skips.
"""

from __future__ import annotations

import httpx

# Broad top-level fallback categories (best-effort for PoE2).
FALLBACK_CATEGORIES: list[str] = [
    "weapon",
    "armour",
    "accessory",
    "jewel",
    "flask",
    "gem",
    "currency",
    "map",
]


def category_filter(category_id: str) -> dict:
    """Build the type_filters fragment selecting one category."""
    return {"type_filters": {"filters": {"category": {"option": category_id}}}}


def fetch_categories(base_url: str, realm: str, headers: dict[str, str]) -> list[str]:
    """Return category option ids from the live filters endpoint, or the fallback."""
    url = f"{base_url}/api/trade2/data/filters"
    try:
        resp = httpx.get(url, headers=headers, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return list(FALLBACK_CATEGORIES)

    ids = _extract_category_ids(data)
    return ids or list(FALLBACK_CATEGORIES)


def _extract_category_ids(data: dict) -> list[str]:
    for group in data.get("result", []):
        if group.get("id") != "type_filters":
            continue
        for filt in group.get("filters", []):
            if filt.get("id") != "category":
                continue
            options = (filt.get("option") or {}).get("options", [])
            return [opt["id"] for opt in options if opt.get("id")]
    return []

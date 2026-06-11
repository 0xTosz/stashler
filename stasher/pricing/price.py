"""Distil trade listings into a robust central price — pure, offline-testable.

Listed (instant-buyout) asks are still noisy and a few are mispriced/under-listed to
manipulate, so we never use the mean. The approach (see ``PRICING_MODULE_PLAN.md`` §5b):

1. drop listings in a currency we can't normalize;
2. order by normalized value, keep the cheapest N;
3. compute the median in the **modal currency** of those N (convert only the outliers),
   so a same-currency batch needs no rate at all and rate-staleness barely bites;
4. trim a small low fraction first to defuse systematic under-listing;
5. report the median plus a p25/p75 band.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent / "data"
_RATES_FILE = _DATA_DIR / "currency_rates.json"


@lru_cache(maxsize=1)
def _load_rates_file() -> dict:
    try:
        return json.loads(_RATES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"base": "exalted", "rates": {"exalted": 1.0}}


def load_rates() -> dict[str, float]:
    """Currency → base-unit multipliers from the vendored table."""
    return dict(_load_rates_file().get("rates") or {"exalted": 1.0})


def base_unit() -> str:
    return _load_rates_file().get("base", "exalted")


def rates_sampled_at() -> str | None:
    return _load_rates_file().get("sampled_at")


@dataclass(frozen=True)
class PriceStats:
    """The numeric core of an estimate (assembled into a PriceEstimate by the pricer)."""

    value: float
    currency: str
    low: float
    high: float
    spread: float       # (p75 - p25) / median, 0 when median is 0
    n_samples: int      # usable listings after currency filtering
    dropped: int        # listings dropped for unknown currency


def listings_from_entries(entries: list[dict]) -> list[tuple[float, str]]:
    """Extract ``(amount, currency)`` from fetched ``{listing: {price}}`` entries.

    Skips entries without a numeric buyout price (e.g. "make an offer" listings)."""
    out: list[tuple[float, str]] = []
    for entry in entries or []:
        price = ((entry or {}).get("listing") or {}).get("price") or {}
        amount = price.get("amount")
        currency = price.get("currency")
        if isinstance(amount, (int, float)) and amount > 0 and currency:
            out.append((float(amount), str(currency)))
    return out


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile (q in 0..1) over an ascending list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 >= len(sorted_vals):
        return sorted_vals[-1]
    return sorted_vals[lo] + frac * (sorted_vals[lo + 1] - sorted_vals[lo])


def summarize(
    listings: list[tuple[float, str]],
    rates: dict[str, float] | None = None,
    *,
    cheapest_n: int = 10,
    trim_low_frac: float = 0.1,
    min_for_trim: int = 5,
) -> PriceStats | None:
    """Robust central price from raw ``(amount, currency)`` listings, or None if nothing is
    usable. ``value``/``low``/``high`` are expressed in the **modal currency** of the cheapest
    N (the currency most of them use); only differently-priced listings are rate-converted."""
    rates = rates or load_rates()

    # Normalize to base only to *order* and to pick the cheapest N; drop unknown currencies.
    norm: list[tuple[float, float, str]] = []  # (base_value, amount, currency)
    dropped = 0
    for amount, currency in listings:
        rate = rates.get(currency)
        if rate is None:
            dropped += 1
            continue
        norm.append((amount * rate, amount, currency))
    if not norm:
        return None

    norm.sort(key=lambda t: t[0])
    cheapest = norm[: max(1, cheapest_n)]

    # Modal currency among the cheapest N: convert only the outliers into it.
    counts: dict[str, int] = {}
    for _b, _a, cur in cheapest:
        counts[cur] = counts.get(cur, 0) + 1
    modal = max(counts, key=lambda c: (counts[c], -list(rates).index(c) if c in rates else 0))
    modal_rate = rates.get(modal, 1.0)

    vals: list[float] = []
    for base_value, amount, currency in cheapest:
        vals.append(amount if currency == modal else base_value / modal_rate)
    vals.sort()

    # Trim a small low fraction (systematic under-listing) before the median.
    trimmed = vals
    if len(vals) >= min_for_trim and trim_low_frac > 0:
        k = int(len(vals) * trim_low_frac)
        if k:
            trimmed = vals[k:]

    median = _percentile(trimmed, 0.5)
    low = _percentile(vals, 0.25)
    high = _percentile(vals, 0.75)
    spread = (high - low) / median if median > 0 else 0.0
    return PriceStats(
        value=median,
        currency=modal,
        low=low,
        high=high,
        spread=spread,
        n_samples=len(cheapest),
        dropped=dropped,
    )


# Total search matches at/above which a market counts as fully liquid. 6x the ladder's
# "enough" (8): a 10-of-10-match search fetches a full cheapest-N, but those 10 asks are
# the WHOLE market — illiquid niche rares are priced by other people's hopeful unsold
# listings (validated against 353 in-app checks: every "expensive junk" appraisal rested
# on 9-30 total matches; see archetype_miner/RESEARCH_LOG.md §7).
LIQUID_TOTAL = 48


def compute_confidence(
    stats: PriceStats,
    *,
    cheapest_n: int,
    mapped_fraction: float,
    relaxed_steps: int,
    is_floor: bool,
    rates_stale: bool,
    total_matches: int | None = None,
) -> float:
    """0..1 confidence. High when we have a full cheapest-N of same-currency listings out
    of a LIQUID market (``total_matches``), every requested filter mapped to a real id, a
    tight band, and the ladder didn't relax; low when samples or the market are thin,
    filters were dropped/unmapped, the band is wide, we relaxed far, the estimate is a
    floor, or the currency rates are stale. ``total_matches`` None (older callers) skips
    the liquidity term by treating the market as liquid."""
    sample = min(1.0, stats.n_samples / max(1, cheapest_n))
    liquidity = 1.0 if total_matches is None else min(1.0, total_matches / LIQUID_TOTAL)
    spread_penalty = min(1.0, stats.spread / 1.5)          # spread of ~1.5 IQR/median → full penalty
    relax_penalty = min(1.0, relaxed_steps * 0.25)         # each rung relaxed costs 0.25
    dropped_penalty = 0.2 if stats.dropped else 0.0
    conf = (
        0.30 * sample
        + 0.20 * liquidity
        + 0.25 * max(0.0, min(1.0, mapped_fraction))
        + 0.25 * (1.0 - spread_penalty)
    )
    conf *= (1.0 - relax_penalty)
    conf -= dropped_penalty
    if is_floor:
        conf *= 0.5
    if rates_stale:
        conf *= 0.85
    return max(0.0, min(1.0, conf))

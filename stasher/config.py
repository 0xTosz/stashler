"""Configuration for stasher.

`Config` holds static, non-secret options (URLs, paths, User-Agent, margins).
The dynamic credentials the UI manages -- account name, POESESSID and league --
live in the database `settings` table and are read at request time, so the UI and
the library share a single source of truth. Values provided here (or via env vars)
are used as seeds/fallbacks when the settings table has no value yet.

Resolution order for credentials at runtime: DB settings > Config seed > env var.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "0.1.0"


@dataclass
class Config:
    # Credential seeds (authoritative copy lives in the DB settings table).
    account_name: str = ""  # full seller name incl. #discriminator, e.g. "Name#1234"
    poesessid: str = ""
    league: str = "Runes of Aldur"  # current PoE2 league

    # Listing "status" filter. The trade site defaults to "securable" (Instant Buyout);
    # we want "any" so offline / non-buyout listings are captured too.
    status: str = "any"

    # Endpoint / environment.
    realm: str = "poe2"
    base_url: str = "https://www.pathofexile.com"

    # Local storage.
    db_path: str = "stasher.db"

    # Etiquette + tuning.
    contact: str = "you@example.com"
    rate_limit_margin: int = 1
    request_timeout: float = 30.0

    # Rate-limit mode: "full" uses the normal margin; "restrictive" additionally
    # reserves `restrictive_fraction` of every bucket so you can browse the trade site
    # in a browser (same per-IP limits) without the tool tripping them. The current
    # mode is persisted in the DB and changeable from the UI.
    rate_mode: str = "full"
    restrictive_fraction: float = 0.5

    # Conservative fallback rate-limit buckets (max_hits, period_seconds) per policy,
    # used until the live X-Rate-Limit headers tell us the real ones. Sourced from
    # GGG's published trade limits.
    fallback_buckets: dict[str, list[tuple[int, int]]] = field(
        default_factory=lambda: {
            "search": [(5, 12), (15, 62), (30, 302)],
            "fetch": [(12, 6), (16, 14)],
        }
    )

    @property
    def user_agent(self) -> str:
        return f"stasher/{__version__} (+https://github.com/; contact: {self.contact})"

    # --- loading ---------------------------------------------------------

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None, **overrides) -> "Config":
        """Build a Config from (in increasing precedence): defaults, a TOML file,
        ``STASHER_*`` environment variables, then explicit keyword overrides."""
        data: dict = {}

        toml_path = _resolve_config_path(path)
        if toml_path and toml_path.exists():
            with open(toml_path, "rb") as fh:
                data.update(tomllib.load(fh))

        env_map = {
            "account_name": "STASHER_ACCOUNT",
            "poesessid": "STASHER_POESESSID",
            "league": "STASHER_LEAGUE",
            "db_path": "STASHER_DB",
            "contact": "STASHER_CONTACT",
        }
        for field_name, env_name in env_map.items():
            if os.environ.get(env_name):
                data[field_name] = os.environ[env_name]

        data.update({k: v for k, v in overrides.items() if v is not None})

        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


def _resolve_config_path(path: str | os.PathLike[str] | None) -> Path | None:
    if path is not None:
        return Path(path)
    for candidate in ("config.local.toml", "config.toml"):
        p = Path(candidate)
        if p.exists():
            return p
    return None

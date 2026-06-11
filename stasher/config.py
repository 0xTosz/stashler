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
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "0.2.0b5"

# Uncommon default port for the local UI (avoids the busy 3000/5000/8000/8080 range).
DEFAULT_UI_PORT = 7137

# Valid values for the trade query's listing-type filter (query.status.option), with
# UI labels. "online"/"onlineleague" return in-person listings (whisper-to-buy);
# "securable" is the instant-buyout/merchant-tab subset; "any" is everything (the two
# combined, plus offline). Confirmed against the live trade2 API.
TRADE_STATUS_OPTIONS: list[tuple[str, str]] = [
    ("online", "In-person — online only"),
    ("onlineleague", "In-person — online, this league only"),
    ("any", "Any — in-person + instant-buyout, incl. offline"),
    ("securable", "Instant buyout only (merchant tabs)"),
]
TRADE_STATUS_VALUES = frozenset(v for v, _ in TRADE_STATUS_OPTIONS)


def user_data_dir() -> Path:
    """Per-user, writable directory for the DB, rules, and filter -- so a packaged app
    never tries to write next to its (possibly read-only) executable."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home()
        return Path(base) / "Stashler"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Stashler"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "Stashler"


@dataclass
class Config:
    # Credential seeds (authoritative copy lives in the DB settings table).
    account_name: str = ""  # full seller name incl. #discriminator, e.g. "Name#1234"
    poesessid: str = ""
    league: str = "Runes of Aldur"  # current PoE2 league

    # Listing "status" filter (see TRADE_STATUS_OPTIONS). Default "online" = in-person
    # listings only (whisper-to-buy), excluding "securable" instant-buyout/merchant tabs.
    # Overridable per-install via the Settings dropdown (stored in the settings table).
    status: str = "online"

    # Endpoint / environment.
    realm: str = "poe2"
    base_url: str = "https://www.pathofexile.com"

    # Local storage. Empty -> resolved in __post_init__ to a per-user data directory
    # (see user_data_dir); the DB, rules.toml, and filter live there.
    data_dir: str = ""
    db_path: str = ""

    # Item-evaluation rules file. None -> auto-resolve (cwd rules.toml > data_dir
    # rules.toml > packaged default); see stasher.evaluate.rules.resolve_rules_path.
    rules_path: str | None = None

    # Etiquette + tuning.
    contact: str = "you@example.com"
    rate_limit_margin: int = 1
    request_timeout: float = 30.0

    # Live websocket keepalive. Browsers don't send client pings (the server pings them);
    # PoE's live endpoint appears to close client-initiated pings with 1008, so we default
    # to None (disabled) and rely on the server's pings + our automatic pongs. Set a
    # number of seconds here only if you need the client to ping.
    live_ping_interval: float | None = None
    live_ping_timeout: float = 15.0

    # When true, log every raw frame received on the live socket to the query feed
    # (verbose; for diagnosing why pushes aren't arriving). Toggle via STASHER_LIVE_DEBUG.
    live_debug: bool = False

    # Auto-refresh loop (the supported near-live alternative to the encrypted live feed):
    # a cheap newest-first light poll every `auto_poll_interval` seconds. A full adaptive
    # backfill runs once to seed, then again only when a poll signals it may have
    # overflowed (whole newest page was new + more listings exist). `auto_full_interval`
    # is an optional timed safety net: 0 disables it; a positive value forces a periodic
    # full backfill that often anyway.
    auto_poll_interval: float = 180.0
    auto_full_interval: float = 0.0

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

    def __post_init__(self) -> None:
        # Resolve storage to a per-user data dir unless explicitly overridden, so the DB
        # and rules don't depend on the working directory (critical for a packaged exe).
        if not self.data_dir:
            self.data_dir = str(user_data_dir())
        if not self.db_path:
            self.db_path = str(Path(self.data_dir) / "stasher.db")

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
            "data_dir": "STASHER_DATA",
            "contact": "STASHER_CONTACT",
            "rules_path": "STASHER_RULES",
        }
        for field_name, env_name in env_map.items():
            if os.environ.get(env_name):
                data[field_name] = os.environ[env_name]

        if os.environ.get("STASHER_LIVE_DEBUG"):
            data["live_debug"] = os.environ["STASHER_LIVE_DEBUG"].lower() not in (
                "0", "false", "no", "",
            )

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

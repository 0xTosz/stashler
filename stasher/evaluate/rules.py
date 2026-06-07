"""Load the editable ``rules.toml`` and build the checker chain from it.

Resolution order (first that exists wins):
  1. an explicit path argument / ``STASHER_RULES`` env / config ``rules_path``
  2. ``./rules.local.toml``        (gitignored personal overrides)
  3. ``./rules.toml``              (project starter, user-editable)
  4. the packaged ``default_rules.toml`` (always present, so loading never fails)

``rules_hash`` is a digest of the rules file *and* every referenced item-filter file,
so editing either invalidates stored evaluations and triggers a re-check.
"""

from __future__ import annotations

import hashlib
import os
import tomllib
from pathlib import Path

from .checks import item_filter as _item_filter
from .checks import regex_check as _regex
from .checks import unique_roll as _unique
from .checks.base import Checker

_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_RULES = _PACKAGE_DIR / "default_rules.toml"

# A single app-managed loot-filter file lives next to the rules file. rules.toml only
# carries `[item_filter] enabled = true/false`; the file's contents are edited/uploaded
# from the UI, so no path is exposed to the user.
FILTER_FILENAME = "stasher.filter"


def resolve_rules_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path:
        return Path(path)
    if os.environ.get("STASHER_RULES"):
        return Path(os.environ["STASHER_RULES"])
    for candidate in ("rules.local.toml", "rules.toml"):
        p = Path(candidate)
        if p.exists():
            return p
    return _DEFAULT_RULES


def parse_rules_text(text: str) -> dict:
    """Parse rules TOML, raising ValueError with a friendly message on syntax errors."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"TOML syntax error: {exc}") from exc


def filter_path(base_dir: Path) -> Path:
    """The single app-managed filter file, beside the rules file."""
    return base_dir / FILTER_FILENAME


def item_filter_enabled(data: dict) -> bool:
    """Whether the item-filter checker is on. New form: ``[item_filter] enabled = true``;
    a legacy ``[[item_filter]]`` array counts as enabled if non-empty."""
    itf = data.get("item_filter")
    if isinstance(itf, dict):
        return bool(itf.get("enabled"))
    if isinstance(itf, list):
        return bool(itf)
    return False


def build_checkers(data: dict, base_dir: Path) -> list[Checker]:
    """Build the checker chain from parsed rules. Raises ValueError on a bad rule."""
    checkers: list[Checker] = []
    if data.get("regex"):
        checkers.append(_regex.build(data["regex"]))
    if data.get("unique_roll"):
        checkers.append(_unique.build(data["unique_roll"]))
    if item_filter_enabled(data):
        fp = filter_path(base_dir)
        if fp.exists() and fp.read_text(encoding="utf-8").strip():
            checkers.append(_item_filter.build_from_file(fp))
    return checkers


def hash_rules(rules_bytes: bytes, data: dict, base_dir: Path) -> str:
    hasher = hashlib.sha256()
    hasher.update(rules_bytes)
    fp = filter_path(base_dir)
    if fp.exists():
        hasher.update(fp.read_bytes())
    return hasher.hexdigest()[:16]


def load_checkers(
    path: str | os.PathLike[str] | None = None,
) -> tuple[list[Checker], str]:
    """Return ``(checkers, rules_hash)`` for the resolved rules file."""
    rules_path = resolve_rules_path(path)
    if not rules_path.exists():
        raise FileNotFoundError(f"rules file not found: {rules_path}")
    raw_bytes = rules_path.read_bytes()
    data = parse_rules_text(raw_bytes.decode("utf-8"))
    base_dir = rules_path.parent
    checkers = build_checkers(data, base_dir)
    return checkers, hash_rules(raw_bytes, data, base_dir)

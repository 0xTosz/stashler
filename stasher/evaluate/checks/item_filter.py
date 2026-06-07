"""Item-filter checker: match items against a minimal PoE loot-filter file.

Supports a practical subset of the loot-filter language -- enough to select bases:

    Show  # Good crossbow bases
        BaseType "Twin Crossbow" "Cannonade Crossbow"
        Rarity Rare
        ItemLevel >= 80

    Hide
        Rarity Normal

Supported conditions: ``BaseType``, ``Class``, ``Rarity``, ``ItemLevel``,
``Quality``, ``Sockets``, ``HasExplicitMod``, ``Corrupted``, ``Mirrored``,
``Identified``. Operators: ``= == != > < >= <=`` (numeric default ``==``;
``BaseType``/``Class`` default to substring match, ``==`` forces exact). Blocks are
evaluated top-to-bottom; the first block whose conditions all match wins -- a ``Show``
flags the item, a ``Hide`` suppresses it.

``HasExplicitMod`` matches FilterBlade's affix-name filtering -- e.g.
``HasExplicitMod >=1 "Hellion's" "of the Sharpshooter"`` fires when the item carries
at least one explicit mod whose name (or rendered text) contains one of those strings.
The optional leading count token (``>=1``, ``>=2``, ``==1`` ...) defaults to ``>=1``.
``Corrupted``/``Mirrored``/``Identified`` take ``True``/``False``.

Unsupported conditions are ignored with a parse warning (loot filters also match on
fields Stashler doesn't model, e.g. ``BaseArmour``; the regex checker covers stat
text). ``Class`` only matches when the item exposes a class; otherwise that condition
is treated as unmet.
"""

from __future__ import annotations

import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import base as _base
from ..itemdata import (
    RARITY_ORDER,
    affix_texts,
    base_type,
    explicit_mod_names,
    ilvl,
    is_corrupted,
    is_identified,
    is_mirrored,
    quality,
    rarity,
    socket_count,
    type_line,
)

CheckResult = _base.CheckResult

_OPERATORS = {"=", "==", "!=", ">", "<", ">=", "<="}
_NUMERIC = {"itemlevel", "ilvl", "quality", "sockets"}
_BOOLEAN = {"corrupted", "mirrored", "identified"}
_SUPPORTED = {"basetype", "class", "rarity", "hasexplicitmod"} | _NUMERIC | _BOOLEAN

# A glued count spec such as ">=1", "==2", or a bare "1" (HasExplicitMod).
_COUNT_RE = re.compile(r"^(>=|<=|==|!=|>|<|=)?(\d+)$")


@dataclass
class Condition:
    keyword: str
    op: str | None
    values: list[str]


@dataclass
class Block:
    action: str  # "Show" | "Hide"
    label: str
    conditions: list[Condition]


def parse_filter(text: str) -> tuple[list[Block], list[str]]:
    blocks: list[Block] = []
    warnings: list[str] = []
    current: Block | None = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].rstrip()
        comment = raw.split("#", 1)[1].strip() if "#" in raw else ""
        if not line.strip():
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            warnings.append(f"line {lineno}: unbalanced quotes, skipped")
            continue
        if not tokens:
            continue
        head = tokens[0]
        if head in ("Show", "Hide"):
            current = Block(action=head, label=comment, conditions=[])
            blocks.append(current)
            continue
        if current is None:
            warnings.append(f"line {lineno}: condition before any Show/Hide, skipped")
            continue
        keyword = head
        rest = tokens[1:]
        op: str | None = None
        if rest and rest[0] in _OPERATORS:
            op, rest = rest[0], rest[1:]
        if keyword.lower() not in _SUPPORTED:
            warnings.append(f"line {lineno}: unsupported condition {keyword!r}, ignored")
        current.conditions.append(Condition(keyword=keyword, op=op, values=rest))
    return blocks, warnings


def _cmp(op: str | None, a, b) -> bool:
    if op in (None, "=", "=="):
        return a == b
    if op == "!=":
        return a != b
    if op == ">":
        return a > b
    if op == "<":
        return a < b
    if op == ">=":
        return a >= b
    if op == "<=":
        return a <= b
    return False


def _num_match(cond: Condition, value: int | None) -> bool:
    if value is None or not cond.values:
        return False
    try:
        target = float(cond.values[0])
    except ValueError:
        return False
    return _cmp(cond.op, float(value), target)


def _text_match(cond: Condition, texts: list[str]) -> bool:
    exact = cond.op == "=="
    lowered = [t.lower() for t in texts if t]
    for value in cond.values:
        v = value.lower()
        for t in lowered:
            if (v == t) if exact else (v in t):
                return True
    return False


def _rarity_match(cond: Condition, item: dict) -> bool:
    r = rarity(item)
    if r is None:
        return False
    if cond.op in (">", "<", ">=", "<=") and r in RARITY_ORDER:
        target = cond.values[0] if cond.values else ""
        if target not in RARITY_ORDER:
            return False
        return _cmp(cond.op, RARITY_ORDER[r], RARITY_ORDER[target])
    return any(r.lower() == v.lower() for v in cond.values)


def _item_class(item: dict) -> str | None:
    ext = item.get("extended") or {}
    cls = ext.get("baseClass") or item.get("class")
    return cls or None


def _bool_match(cond: Condition, value: bool) -> bool:
    """Compare an item flag against a ``True``/``False`` value (default ``True``)."""
    want = cond.values[0].lower() if cond.values else "true"
    if want not in ("true", "false"):
        return False
    return value is (want == "true")


def _has_explicit_mod(cond: Condition, item: dict) -> bool:
    """FilterBlade ``HasExplicitMod``: count explicit mods matching any given name.

    The optional first value is a count spec (``>=1`` default). Remaining values are
    matched as case-insensitive substrings against each explicit mod's name and its
    rendered text, so both ``"Hellion's"`` (affix name) and a stat fragment work.
    """
    values = list(cond.values)
    op = cond.op if cond.op in _OPERATORS else ">="
    count = 1
    if values:
        m = _COUNT_RE.match(values[0])
        if m:
            op = m.group(1) or op  # glued operator (">=2") wins over a spaced one
            count = int(m.group(2))
            values = values[1:]
    if not values:
        return False
    haystack = [t.lower() for t in (*explicit_mod_names(item), *affix_texts(item)) if t]
    needles = [v.lower() for v in values]
    matched = sum(1 for t in haystack if any(n in t for n in needles))
    return _cmp(op, matched, count)


def _match_condition(cond: Condition, item: dict) -> bool:
    kw = cond.keyword.lower()
    if kw == "basetype":
        return _text_match(cond, [base_type(item), type_line(item)])
    if kw == "class":
        cls = _item_class(item)
        return _text_match(cond, [cls]) if cls else False
    if kw == "rarity":
        return _rarity_match(cond, item)
    if kw in ("itemlevel", "ilvl"):
        return _num_match(cond, ilvl(item))
    if kw == "quality":
        return _num_match(cond, quality(item))
    if kw == "sockets":
        return _num_match(cond, socket_count(item))
    if kw == "hasexplicitmod":
        return _has_explicit_mod(cond, item)
    if kw == "corrupted":
        return _bool_match(cond, is_corrupted(item))
    if kw == "mirrored":
        return _bool_match(cond, is_mirrored(item))
    if kw == "identified":
        return _bool_match(cond, is_identified(item))
    return True  # unsupported keyword: no effect (already warned at parse time)


@dataclass
class ItemFilterRule:
    name: str
    blocks: list[Block] = field(default_factory=list)


class ItemFilterChecker:
    name = "item_filter"

    def __init__(self, rules: list[ItemFilterRule]):
        self.rules = rules

    def check(self, item: dict) -> list[CheckResult]:
        results: list[CheckResult] = []
        for rule in self.rules:
            for block in rule.blocks:
                if all(_match_condition(c, item) for c in block.conditions):
                    if block.action == "Show":
                        label = block.label or "Show"
                        results.append(
                            CheckResult(rule.name, f"Filter matched: {label}")
                        )
                    break  # first matching block decides this rule
        return results


def build_from_file(path: Path) -> ItemFilterChecker:
    """Build the single item-filter checker from the app-managed filter file."""
    blocks, warnings = parse_filter(path.read_text(encoding="utf-8"))
    for w in warnings:
        print(f"stasher: rules: filter: {w}", file=sys.stderr)
    return ItemFilterChecker([ItemFilterRule(name="filter", blocks=blocks)])

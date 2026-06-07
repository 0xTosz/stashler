"""Item-filter checker: match items against a minimal PoE loot-filter file.

Supports a practical subset of the loot-filter language -- enough to select bases:

    Show  # Good crossbow bases
        BaseType "Twin Crossbow" "Cannonade Crossbow"
        Rarity Rare
        ItemLevel >= 80

    Hide
        Rarity Normal

Supported conditions: ``BaseType``, ``Class``, ``Rarity``, ``ItemLevel``,
``Quality``, ``Sockets``. Operators: ``= == != > < >= <=`` (numeric default ``==``;
``BaseType``/``Class`` default to substring match, ``==`` forces exact). Blocks are
evaluated top-to-bottom; the first block whose conditions all match wins -- a ``Show``
flags the item, a ``Hide`` suppresses it. Unsupported conditions are ignored with a
parse warning (loot filters also match on drop-time fields, e.g. explicit mods, which
the regex checker handles instead). ``Class`` only matches when the item exposes a
class; otherwise that condition is treated as unmet.
"""

from __future__ import annotations

import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import base as _base
from ..itemdata import RARITY_ORDER, base_type, ilvl, quality, rarity, type_line

CheckResult = _base.CheckResult

_OPERATORS = {"=", "==", "!=", ">", "<", ">=", "<="}
_NUMERIC = {"itemlevel", "ilvl", "quality", "sockets"}
_SUPPORTED = {"basetype", "class", "rarity"} | _NUMERIC


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
        return _num_match(cond, 0)
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

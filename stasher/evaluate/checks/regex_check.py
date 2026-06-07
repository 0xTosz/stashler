"""Regex checker: flag items whose name, base, or affixes match a user pattern.

Each rule is ``{name, pattern, targets, ignore_case}``. ``targets`` chooses which
text to search:

* ``affixes`` -- each explicit/implicit mod line (cleaned of API markup)
* ``name``    -- the unique/rare item name
* ``base``    -- the base type / type line

Patterns are matched against in-game-style text (e.g. ``Fire Resistance``,
``Adds # Physical Damage``), so authors don't deal with ``[a|b]`` markup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import base as _base
from ..itemdata import affix_texts, base_type, name as item_name, type_line

CheckResult = _base.CheckResult

_VALID_TARGETS = {"affixes", "name", "base"}


@dataclass
class RegexRule:
    name: str
    pattern: str
    targets: tuple[str, ...] = ("affixes",)
    ignore_case: bool = True
    _compiled: re.Pattern = field(init=False, repr=False)

    def __post_init__(self) -> None:
        bad = set(self.targets) - _VALID_TARGETS
        if bad:
            raise ValueError(
                f"regex rule {self.name!r}: unknown targets {sorted(bad)}; "
                f"valid: {sorted(_VALID_TARGETS)}"
            )
        flags = re.IGNORECASE if self.ignore_case else 0
        try:
            self._compiled = re.compile(self.pattern, flags)
        except re.error as exc:
            raise ValueError(f"regex rule {self.name!r}: bad pattern: {exc}") from exc


class RegexChecker:
    name = "regex"

    def __init__(self, rules: list[RegexRule]):
        self.rules = rules

    def check(self, item: dict) -> list[CheckResult]:
        results: list[CheckResult] = []
        for rule in self.rules:
            hit = self._first_match(rule, item)
            if hit is not None:
                results.append(
                    CheckResult(rule.name, f"Rule '{rule.name}': matched {hit!r}")
                )
        return results

    def _first_match(self, rule: RegexRule, item: dict) -> str | None:
        for text in self._candidates(rule.targets, item):
            m = rule._compiled.search(text)
            if m:
                return m.group(0)
        return None

    @staticmethod
    def _candidates(targets, item: dict):
        for target in targets:
            if target == "affixes":
                yield from affix_texts(item)
            elif target == "name":
                nm = item_name(item)
                if nm:
                    yield nm
            elif target == "base":
                yield base_type(item)
                tl = type_line(item)
                if tl:
                    yield tl


def build(raw_rules: list[dict]) -> RegexChecker:
    rules = [
        RegexRule(
            name=r["name"],
            pattern=r["pattern"],
            targets=tuple(r.get("targets") or ("affixes",)),
            ignore_case=bool(r.get("ignore_case", True)),
        )
        for r in raw_rules
    ]
    return RegexChecker(rules)

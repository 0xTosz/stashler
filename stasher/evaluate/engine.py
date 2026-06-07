"""Run an item through every checker and collect the verdict."""

from __future__ import annotations

from dataclasses import dataclass, field

from .checks.base import Checker


@dataclass
class Evaluation:
    flagged: bool
    reasons: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)  # firing rule names


def evaluate_item(item: dict, checkers: list[Checker]) -> Evaluation:
    """Flag the item if any checker fires; gather all explanations and rule names."""
    reasons: list[str] = []
    rules: list[str] = []
    for checker in checkers:
        for result in checker.check(item):
            reasons.append(result.explanation)
            rules.append(result.rule_name)
    return Evaluation(flagged=bool(reasons), reasons=reasons, rules=rules)

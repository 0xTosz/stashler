"""Run an item through every checker and collect the verdict."""

from __future__ import annotations

from dataclasses import dataclass, field

from .checks.base import Checker


@dataclass
class Evaluation:
    flagged: bool
    reasons: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)  # firing rule names
    score: float | None = None  # max graded value across checkers (archetype_set), else None


def evaluate_item(item: dict, checkers: list[Checker]) -> Evaluation:
    """Flag the item if any checker fires; gather all explanations, rule names, and the
    overall graded score (max of any checker's per-result score)."""
    reasons: list[str] = []
    rules: list[str] = []
    scores: list[float] = []
    for checker in checkers:
        for result in checker.check(item):
            reasons.append(result.explanation)
            rules.append(result.rule_name)
            if result.score is not None:
                scores.append(result.score)
    return Evaluation(flagged=bool(reasons), reasons=reasons, rules=rules,
                      score=max(scores) if scores else None)

"""Checker protocol + result type shared by every checker module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class CheckResult:
    """One reason an item was flagged.

    ``rule_name`` identifies which user rule fired (for per-rule breakdowns);
    ``explanation`` is the human-readable line shown in the review queue. ``score`` is an
    optional 0..1 graded value (the archetype_set checker sets it; binary checkers leave it
    None) — the item's overall score is the max across results.
    """

    rule_name: str
    explanation: str
    score: float | None = None


@runtime_checkable
class Checker(Protocol):
    name: str

    def check(self, item: dict) -> list[CheckResult]:
        """Return one CheckResult per rule that matched ``item`` (empty if none)."""
        ...

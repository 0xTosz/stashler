"""Unique high-roll checker: flag uniques whose explicit rolls sit near the top.

For each rollable explicit magnitude we compute ``(actual - min) / (max - min)``
(see :func:`stasher.evaluate.itemdata.explicit_roll_percents`) and aggregate:

* ``avg`` -- mean roll across all rollable mods (default)
* ``max`` -- the single best-rolled mod
* ``all`` -- the worst mod must clear the bar (every roll is high)

A rule fires when the aggregate >= ``min_percent``. Only uniques are considered;
mods with a fixed value (min == max) contribute nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import base as _base
from ..itemdata import explicit_roll_percents, name as item_name, rarity

CheckResult = _base.CheckResult

_VALID_AGG = {"avg", "max", "all"}


@dataclass
class UniqueRollRule:
    name: str
    min_percent: float = 90.0
    aggregate: str = "avg"

    def __post_init__(self) -> None:
        if self.aggregate not in _VALID_AGG:
            raise ValueError(
                f"unique_roll rule {self.name!r}: aggregate must be one of "
                f"{sorted(_VALID_AGG)}, got {self.aggregate!r}"
            )


class UniqueHighRollChecker:
    name = "unique_roll"

    def __init__(self, rules: list[UniqueRollRule]):
        self.rules = rules

    def check(self, item: dict) -> list[CheckResult]:
        if rarity(item) != "Unique":
            return []
        percents = explicit_roll_percents(item)
        if not percents:
            return []

        agg_value = {
            "avg": sum(percents) / len(percents),
            "max": max(percents),
            "all": min(percents),
        }
        results: list[CheckResult] = []
        for rule in self.rules:
            value = agg_value[rule.aggregate] * 100.0
            if value >= rule.min_percent:
                nm = item_name(item) or "unique"
                results.append(
                    CheckResult(
                        rule.name,
                        f"Rule '{rule.name}': high-roll {nm} "
                        f"({value:.0f}% {rule.aggregate})",
                    )
                )
        return results


def build(raw_rules: list[dict]) -> UniqueHighRollChecker:
    rules = [
        UniqueRollRule(
            name=r["name"],
            min_percent=float(r.get("min_percent", 90.0)),
            aggregate=str(r.get("aggregate", "avg")),
        )
        for r in raw_rules
    ]
    return UniqueHighRollChecker(rules)

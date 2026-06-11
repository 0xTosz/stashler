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
    # The archetype model's now/potential split for the graded result: what the item is
    # worth untouched vs the risk-discounted value of finishing it (queue display/filters).
    score_now: float | None = None
    score_potential: float | None = None
    driver: str | None = None   # "now" | "craft" — which side dominates
    # Structured per-result attribution: {checker, rule, explanation, score}. Drives the
    # per-checker chips/filters/sorts in the queue; reasons/rules/score are derived from it.
    results: list[dict] = field(default_factory=list)


def evaluate_item(item: dict, checkers: list[Checker]) -> Evaluation:
    """Flag the item if any checker fires; gather per-result attribution (which checker/rule),
    the human explanations, and the overall graded score (max of any checker's per-result score)."""
    results: list[dict] = []
    scores: list[float] = []
    split: dict | None = None
    for checker in checkers:
        cname = getattr(checker, "name", "")
        for result in checker.check(item):
            entry = {"checker": cname, "rule": result.rule_name,
                     "explanation": result.explanation, "score": result.score,
                     "count": result.count}
            if result.extra:
                entry["extra"] = result.extra
                if split is None and "now" in result.extra:
                    split = result.extra
            results.append(entry)
            if result.score is not None:
                scores.append(result.score)
    return Evaluation(
        flagged=bool(results),
        reasons=[r["explanation"] for r in results],
        rules=[r["rule"] for r in results],
        score=max(scores) if scores else None,
        score_now=(split or {}).get("now"),
        score_potential=(split or {}).get("potential"),
        driver=(split or {}).get("driver"),
        results=results,
    )

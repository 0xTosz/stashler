"""Local, rule-based item evaluation.

Every captured item is run through a chain of independent *checkers* (see
:mod:`stasher.evaluate.checks`). If any checker fires, the item is flagged and shown
in the UI review queue with the firing checker's human-readable explanation. All
checks are 100% local -- they read only the archived ``raw_json`` -- and are driven by
an editable ``rules.toml`` so users (or Claude) can add/tweak rules without code edits.
"""

from __future__ import annotations

from .engine import Evaluation, evaluate_item
from .evaluator import Evaluator
from .rules import load_checkers

__all__ = ["Evaluation", "evaluate_item", "Evaluator", "load_checkers"]

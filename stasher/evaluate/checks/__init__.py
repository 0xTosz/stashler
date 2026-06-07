"""Built-in checker modules.

Each checker takes an item dict and returns a (possibly empty) list of
:class:`CheckResult`. A non-empty result means "flag this item, here's why". Add a
new checker by implementing the :class:`Checker` protocol and wiring it up in
:mod:`stasher.evaluate.rules`.
"""

from __future__ import annotations

from .base import CheckResult, Checker
from .item_filter import ItemFilterChecker
from .regex_check import RegexChecker
from .unique_roll import UniqueHighRollChecker

__all__ = [
    "CheckResult",
    "Checker",
    "RegexChecker",
    "ItemFilterChecker",
    "UniqueHighRollChecker",
]

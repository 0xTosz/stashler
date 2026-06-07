"""Glue between the checker chain and the store.

Builds the checkers once from the rules file, then evaluates items and persists the
verdict. Used in two places: the capture pipeline (one new item at a time) and the
``stasher evaluate`` batch command (re-check everything when rules change).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Callable

from .engine import Evaluation, evaluate_item
from .rules import (
    _DEFAULT_RULES,
    build_checkers,
    filter_path,
    item_filter_enabled,
    load_checkers,
    parse_rules_text,
    resolve_rules_path,
)


class Evaluator:
    def __init__(self, store, rules_path: str | None = None, data_dir: str | None = None):
        self.store = store
        self._configured_path = rules_path
        self._data_dir = data_dir
        self.rules_path = resolve_rules_path(rules_path, data_dir)
        self.checkers, self.rules_hash = load_checkers(rules_path, data_dir)

    # --- rule editing (UI) ---------------------------------------------

    def reload(self) -> None:
        """Re-resolve and rebuild the checkers (call after editing the rules)."""
        self.rules_path = resolve_rules_path(self._configured_path, self._data_dir)
        self.checkers, self.rules_hash = load_checkers(self._configured_path, self._data_dir)

    def edit_path(self) -> Path:
        """Where edits are written (the resolved writable rules file)."""
        return self.rules_path

    def rules_text(self) -> str:
        """Current rules TOML to show in the editor (writable file, or packaged default
        when it doesn't exist yet)."""
        target = self.edit_path()
        src = target if target.exists() else _DEFAULT_RULES
        return src.read_text(encoding="utf-8")

    def filter_view(self) -> tuple[bool, str]:
        """(enabled, contents) for the single app-managed filter file."""
        data = parse_rules_text(self.rules_text())
        fp = filter_path(self.edit_path().parent)
        text = fp.read_text(encoding="utf-8") if fp.exists() else ""
        return item_filter_enabled(data), text

    def save_rules(self, rules_text: str, filter_text: str) -> None:
        """Validate then persist the rules + the single filter file, and reload.

        Raises ValueError (without writing the rules file) if anything is invalid, so a
        broken edit can never silently disable evaluation. The filter contents are always
        written (so edits persist); whether they apply is governed by ``[item_filter]
        enabled`` in the rules file.
        """
        data = parse_rules_text(rules_text)
        base = self.edit_path().parent
        # Validate the disk-independent checkers (regex, unique_roll) up front, before
        # writing anything -- a bad pattern or aggregate raises here.
        build_checkers({k: v for k, v in data.items() if k != "item_filter"}, base)

        base.mkdir(parents=True, exist_ok=True)
        filter_path(base).write_text(filter_text, encoding="utf-8")

        target = self.edit_path()
        target.write_text(rules_text, encoding="utf-8")
        self.reload()  # full rebuild (incl. item_filter); surfaces any remaining error

    def evaluate_entry(self, entry: dict) -> Evaluation:
        """Evaluate one full fetch entry ``{id, listing, item}`` and store the result."""
        item = entry.get("item") or {}
        item_hash = entry.get("id") or item.get("id")
        ev = evaluate_item(item, self.checkers)
        if item_hash:
            self.store.upsert_evaluation(item_hash, ev, self.rules_hash)
        return ev

    def reevaluate_all(
        self,
        progress: Callable[[int, int], None] | None = None,
        force: bool = False,
    ) -> dict:
        """(Re)check stored items. Returns a summary with a per-rule breakdown.

        ``force`` re-checks everything; otherwise only items whose stored evaluation
        is missing or was produced by a different rules version.
        """
        evaluated = 0
        flagged = 0
        by_rule: Counter[str] = Counter()
        for item_hash, raw_json in self.store.items_to_evaluate(self.rules_hash, force):
            try:
                entry = json.loads(raw_json)
            except (ValueError, TypeError):
                continue
            item = entry.get("item") or {}
            ev = evaluate_item(item, self.checkers)
            self.store.upsert_evaluation(item_hash, ev, self.rules_hash)
            evaluated += 1
            if ev.flagged:
                flagged += 1
                by_rule.update(ev.rules)
            if progress and evaluated % 100 == 0:
                progress(evaluated, flagged)
        if progress:
            progress(evaluated, flagged)
        return {"evaluated": evaluated, "flagged": flagged, "by_rule": dict(by_rule)}

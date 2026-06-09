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

from .archetype_model import ArchetypeSet
from .engine import Evaluation, evaluate_item
from .rules import (
    _DEFAULT_RULES,
    archetype_set_default_path,
    archetype_set_enabled,
    archetype_set_path,
    build_checkers,
    filter_path,
    item_filter_enabled,
    load_checkers,
    normalize_newlines,
    parse_rules_text,
    resolve_rules_path,
    set_section_flag,
)

# rules sections whose checker reads a separate on-disk file; excluded from the pre-write
# validation build (the files may not be written yet).
_DISK_SECTIONS = ("item_filter", "archetype_set")


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
        """Validate then persist the rules TOML + the loot filter, and reload.

        Raises ValueError (without writing the rules file) if anything is invalid, so a
        broken edit can never silently disable evaluation."""
        rules_text = normalize_newlines(rules_text)
        filter_text = normalize_newlines(filter_text)
        data = parse_rules_text(rules_text)
        base = self.edit_path().parent
        # Validate the disk-independent checkers (regex, unique_roll) up front, before
        # writing anything -- a bad pattern or aggregate raises here.
        build_checkers({k: v for k, v in data.items() if k not in _DISK_SECTIONS}, base)
        base.mkdir(parents=True, exist_ok=True)
        # newline="" keeps the normalized LF as-is; without it, Windows text mode would
        # re-expand each \n to \r\n (and double an existing CR to \r\r\n).
        filter_path(base).write_text(filter_text, encoding="utf-8", newline="")
        self.edit_path().write_text(rules_text, encoding="utf-8", newline="")
        self.reload()

    # --- archetype set: enable toggle (Settings) + editor (Rules page) -----

    def archetype_set_is_enabled(self) -> bool:
        return archetype_set_enabled(parse_rules_text(self.rules_text()))

    def set_archetype_set_enabled(self, on: bool) -> None:
        """Flip ``[archetype_set] enabled`` in the rules file (the Settings checkbox)."""
        text = set_section_flag(self.rules_text(), "archetype_set", "enabled", on)
        base = self.edit_path().parent
        base.mkdir(parents=True, exist_ok=True)
        self.edit_path().write_text(text, encoding="utf-8", newline="")
        self.reload()

    def archetype_set(self) -> ArchetypeSet | None:
        """Load the working ArchetypeSet (the editable copy), or None if none uploaded."""
        ap = archetype_set_path(self.edit_path().parent)
        if ap.exists() and ap.read_text(encoding="utf-8").strip():
            return ArchetypeSet.load(ap)
        return None

    def save_archetype_set(self, aset: ArchetypeSet) -> None:
        """Persist an edited ArchetypeSet (from the Rules card editor) and reload."""
        base = self.edit_path().parent
        base.mkdir(parents=True, exist_ok=True)
        aset.save(archetype_set_path(base))
        self.reload()

    def upload_archetype_set(self, text: str) -> None:
        """Install a freshly mined set: validate, then write the working copy *and* a pristine
        ``.default`` copy (for Restore defaults). Raises ValueError on invalid YAML."""
        try:
            ArchetypeSet.loads(text)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"archetype set: invalid YAML: {exc}") from exc
        base = self.edit_path().parent
        base.mkdir(parents=True, exist_ok=True)
        text = normalize_newlines(text)
        archetype_set_path(base).write_text(text, encoding="utf-8", newline="")
        archetype_set_default_path(base).write_text(text, encoding="utf-8", newline="")
        self.reload()

    def restore_archetype_set_defaults(self) -> bool:
        """Revert the working set to the pristine ``.default`` copy. False if none exists."""
        base = self.edit_path().parent
        dp = archetype_set_default_path(base)
        if not (dp.exists() and dp.read_text(encoding="utf-8").strip()):
            return False
        archetype_set_path(base).write_text(dp.read_text(encoding="utf-8"),
                                            encoding="utf-8", newline="")
        self.reload()
        return True

    def explain_score(self, item: dict) -> dict | None:
        """The archetype_set score breakdown for one item (for the detail view), or None if
        that checker isn't active. Recomputed live against the current set."""
        for checker in self.checkers:
            if getattr(checker, "name", "") == "archetype_set" and hasattr(checker, "explain"):
                return checker.explain(item)
        return None

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

"""SQLite persistence for stasher.

One file, opened with ``check_same_thread=False`` and guarded by a lock so the
capture worker and the Flask UI (possibly different threads/processes) can share it.
WAL mode keeps concurrent reads/writes happy. All writes go through :class:`Store`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

QUERY_LOG_KEEP = 500  # rows retained in query_log for the UI feed


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ItemRecord:
    hash: str
    account: str
    listed_at: str | None
    price_amount: float | None
    price_currency: str | None
    price_type: str | None
    item_name: str | None
    type_line: str | None
    rarity: str | None
    whisper: str | None
    league: str | None
    raw_json: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    hash           TEXT PRIMARY KEY,
    account        TEXT,
    listed_at      TEXT,
    fetched_at     TEXT NOT NULL,
    price_amount   REAL,
    price_currency TEXT,
    price_type     TEXT,
    item_name      TEXT,
    type_line      TEXT,
    rarity         TEXT,
    whisper        TEXT,
    league         TEXT,
    raw_json       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_fetched ON items(fetched_at);
CREATE INDEX IF NOT EXISTS idx_items_name    ON items(item_name);
CREATE INDEX IF NOT EXISTS idx_items_rarity  ON items(rarity);

CREATE TABLE IF NOT EXISTS rate_events (
    policy TEXT NOT NULL,
    ts     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_policy_ts ON rate_events(policy, ts);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS query_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    target    TEXT,
    status    TEXT NOT NULL,
    http_code INTEGER,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_query_log_id ON query_log(id);

-- Derived, recomputable verdict from the local rule engine (see stasher.evaluate).
-- Flagged items surface in the UI review queue; `seen` sinks reviewed items.
CREATE TABLE IF NOT EXISTS evaluations (
    hash         TEXT PRIMARY KEY,
    flagged      INTEGER NOT NULL,
    seen         INTEGER NOT NULL DEFAULT 0,
    seen_at      TEXT,
    reasons      TEXT,
    rules_hash   TEXT,
    evaluated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_flagged_seen ON evaluations(flagged, seen);
"""


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- items ----------------------------------------------------------

    def has_hash(self, item_hash: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM items WHERE hash = ? LIMIT 1", (item_hash,)
            ).fetchone()
        return row is not None

    def insert_item(self, rec: ItemRecord) -> bool:
        """Append-only insert. Returns True if this hash was new."""
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO items
                    (hash, account, listed_at, fetched_at, price_amount,
                     price_currency, price_type, item_name, type_line,
                     rarity, whisper, league, raw_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    rec.hash, rec.account, rec.listed_at, utc_now_iso(),
                    rec.price_amount, rec.price_currency, rec.price_type,
                    rec.item_name, rec.type_line, rec.rarity, rec.whisper,
                    rec.league, rec.raw_json,
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def count_items(self, text: str | None = None, rarity: str | None = None) -> int:
        sql, params = self._items_where("SELECT COUNT(*) AS n FROM items", text, rarity)
        with self._lock:
            return int(self._conn.execute(sql, params).fetchone()["n"])

    def iter_items(
        self,
        text: str | None = None,
        rarity: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        sql, params = self._items_where(
            "SELECT hash, account, listed_at, fetched_at, price_amount, "
            "price_currency, price_type, item_name, type_line, rarity, whisper "
            "FROM items",
            text,
            rarity,
        )
        sql += " ORDER BY fetched_at DESC LIMIT ? OFFSET ?"
        params = [*params, limit, offset]
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def all_records(self) -> list[sqlite3.Row]:
        """Every item joined with its evaluation, for the in-memory records table."""
        with self._lock:
            return self._conn.execute(
                "SELECT i.hash, i.item_name, i.type_line, i.rarity, i.price_amount, "
                "i.price_currency, i.listed_at, i.fetched_at, e.flagged, e.reasons "
                "FROM items i LEFT JOIN evaluations e ON e.hash = i.hash "
                "ORDER BY i.fetched_at DESC"
            ).fetchall()

    def get_record(self, item_hash: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT i.raw_json, e.reasons, e.flagged "
                "FROM items i LEFT JOIN evaluations e ON e.hash = i.hash "
                "WHERE i.hash = ?",
                (item_hash,),
            ).fetchone()

    @staticmethod
    def _items_where(base: str, text: str | None, rarity: str | None):
        clauses: list[str] = []
        params: list[Any] = []
        if text:
            clauses.append("(item_name LIKE ? OR type_line LIKE ?)")
            params += [f"%{text}%", f"%{text}%"]
        if rarity:
            clauses.append("rarity = ?")
            params.append(rarity)
        if clauses:
            base += " WHERE " + " AND ".join(clauses)
        return base, params

    # --- evaluations / review queue ------------------------------------

    def upsert_evaluation(self, item_hash: str, evaluation, rules_hash: str) -> None:
        """Store a verdict. Preserves the `seen` flag across re-evaluation."""
        reasons = json.dumps(list(evaluation.reasons), separators=(",", ":"))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO evaluations
                    (hash, flagged, seen, seen_at, reasons, rules_hash, evaluated_at)
                VALUES (?, ?, 0, NULL, ?, ?, ?)
                ON CONFLICT(hash) DO UPDATE SET
                    flagged      = excluded.flagged,
                    reasons      = excluded.reasons,
                    rules_hash   = excluded.rules_hash,
                    evaluated_at = excluded.evaluated_at
                """,
                (
                    item_hash,
                    1 if evaluation.flagged else 0,
                    reasons,
                    rules_hash,
                    utc_now_iso(),
                ),
            )
            self._conn.commit()

    def items_to_evaluate(self, rules_hash: str, force: bool = False) -> list[tuple[str, str]]:
        """(hash, raw_json) for items lacking a current evaluation (or all, if force)."""
        with self._lock:
            if force:
                rows = self._conn.execute(
                    "SELECT hash, raw_json FROM items"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT i.hash, i.raw_json
                    FROM items i
                    LEFT JOIN evaluations e ON e.hash = i.hash
                    WHERE e.hash IS NULL OR e.rules_hash IS NULL OR e.rules_hash != ?
                    """,
                    (rules_hash,),
                ).fetchall()
        return [(r["hash"], r["raw_json"]) for r in rows]

    def queue_items(
        self, show_all: bool = False, limit: int = 100, offset: int = 0
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT i.hash, i.item_name, i.type_line, i.rarity, i.price_amount, "
            "i.price_currency, i.price_type, i.whisper, i.listed_at, i.fetched_at, "
            "i.raw_json, e.reasons, e.flagged, e.seen, e.seen_at, e.evaluated_at "
            "FROM evaluations e JOIN items i ON i.hash = e.hash"
        )
        if not show_all:
            sql += " WHERE e.flagged = 1"
        sql += " ORDER BY e.seen ASC, e.evaluated_at DESC, i.fetched_at DESC LIMIT ? OFFSET ?"
        with self._lock:
            return self._conn.execute(sql, (limit, offset)).fetchall()

    def count_queue(self, show_all: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM evaluations"
        if not show_all:
            sql += " WHERE flagged = 1"
        with self._lock:
            return int(self._conn.execute(sql).fetchone()["n"])

    def count_unseen(self) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) AS n FROM evaluations WHERE flagged = 1 AND seen = 0"
                ).fetchone()["n"]
            )

    def mark_seen(self, item_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE evaluations SET seen = 1, seen_at = ? WHERE hash = ?",
                (utc_now_iso(), item_hash),
            )
            self._conn.commit()

    def mark_all_seen(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE evaluations SET seen = 1, seen_at = ? "
                "WHERE flagged = 1 AND seen = 0",
                (utc_now_iso(),),
            )
            self._conn.commit()
            return cur.rowcount

    # --- maintenance ----------------------------------------------------

    def clear_archive(self) -> int:
        """Drop archived items + their evaluations and reset sync markers, for a force
        resync. Keeps settings (credentials) and the query log / rate history. Returns
        the number of items removed."""
        with self._lock:
            n = int(self._conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"])
            self._conn.execute("DELETE FROM items")
            self._conn.execute("DELETE FROM evaluations")
            self._conn.execute(
                "DELETE FROM meta WHERE key IN ('last_backfill_at', 'last_poll_at')"
            )
            self._conn.commit()
        return n

    # --- settings & meta ------------------------------------------------

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    # --- rate events ----------------------------------------------------

    def record_rate_event(self, policy: str, ts: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO rate_events(policy, ts) VALUES (?, ?)", (policy, ts)
            )
            self._conn.commit()

    def rate_events_since(self, policy: str, since: float) -> list[float]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts FROM rate_events WHERE policy = ? AND ts >= ? ORDER BY ts",
                (policy, since),
            ).fetchall()
        return [r["ts"] for r in rows]

    def prune_rate_events(self, before: float) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM rate_events WHERE ts < ?", (before,))
            self._conn.commit()

    # --- query log ------------------------------------------------------

    def log_query(
        self,
        kind: str,
        target: str | None,
        status: str,
        http_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO query_log(ts, kind, target, status, http_code, detail) "
                "VALUES (?,?,?,?,?,?)",
                (utc_now_iso(), kind, target, status, http_code, detail),
            )
            # Keep the log bounded for the UI feed.
            self._conn.execute(
                "DELETE FROM query_log WHERE id <= "
                "(SELECT MAX(id) FROM query_log) - ?",
                (QUERY_LOG_KEEP,),
            )
            self._conn.commit()

    def recent_queries(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT ts, kind, target, status, http_code, detail "
                "FROM query_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()


def record_from_fetch_entry(entry: dict, league: str | None) -> ItemRecord:
    """Build an ItemRecord from one element of a /fetch result array."""
    listing = entry.get("listing", {}) or {}
    item = entry.get("item", {}) or {}
    price = listing.get("price") or {}
    account = (listing.get("account") or {}).get("name")
    return ItemRecord(
        hash=entry.get("id", ""),
        account=account or "",
        listed_at=listing.get("indexed"),
        price_amount=price.get("amount"),
        price_currency=price.get("currency"),
        price_type=price.get("type"),
        item_name=item.get("name") or None,
        type_line=item.get("typeLine") or item.get("baseType") or None,
        rarity=_rarity_name(item.get("frameType")),
        whisper=listing.get("whisper"),
        league=item.get("league") or league,
        raw_json=json.dumps(entry, separators=(",", ":")),
    )


_FRAME_RARITY = {
    0: "Normal", 1: "Magic", 2: "Rare", 3: "Unique", 4: "Gem",
    5: "Currency", 6: "Divination Card", 8: "Prophecy", 9: "Relic",
}


def _rarity_name(frame_type: Any) -> str | None:
    if isinstance(frame_type, int):
        return _FRAME_RARITY.get(frame_type, str(frame_type))
    return None

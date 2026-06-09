"""Minimal Flask UI: browse records, review queue, settings, live status bar."""

from __future__ import annotations

import json
import math
import os
import sys
import time

from flask import Flask, jsonify, redirect, render_template, request, url_for

from ..config import TRADE_STATUS_OPTIONS, TRADE_STATUS_VALUES
from ..evaluate.archetype_model import value_to_tier
from ..evaluate.itemdata import stash_regex
from .itemcard import build_card

PAGE_SIZE = 50


def _apply_archetype_edits(aset, form) -> None:
    """Apply the Rules card-editor form onto a loaded ArchetypeSet (in place)."""
    def fnum(key):
        v = form.get(key)
        try:
            return float(v) if v not in (None, "") else None
        except ValueError:
            return None

    ri = fnum("scoring.roll_influence")
    if ri is not None:
        aset.scoring.roll_influence = max(0.0, min(1.0, ri))
    mri = fnum("scoring.magic_roll_influence")
    if mri is not None:
        aset.scoring.magic_roll_influence = max(0.0, min(1.0, mri))
    for t in ("T1", "T2", "T3", "below"):
        v = fnum(f"scoring.tier.{t}")
        if v is not None:
            aset.scoring.tier_weights[t] = max(0.0, min(1.0, v))
    pt = fnum("scoring.partial_threshold")
    if pt is not None:
        aset.scoring.partial_threshold = max(0.0, min(1.0, pt))
    cc = fnum("scoring.craft_credit")
    if cc is not None:
        aset.scoring.craft_credit = max(0.0, min(1.0, cc))
    bc = fnum("scoring.breadth_cap")
    if bc is not None:
        aset.scoring.breadth_cap = max(0.0, min(1.0, bc))

    for a in aset.archetypes:
        a.enabled = form.get(f"arch.{a.id}.enabled") == "on"
        sc = fnum(f"arch.{a.id}.score")
        if sc is not None:
            a.value.score = max(0.0, min(1.0, sc))
            a.value.tier = value_to_tier(a.value.score)
        for i, r in enumerate(a.requires):
            w = fnum(f"arch.{a.id}.w.{i}")
            if w is not None:
                r.weight = max(0.0, min(1.0, w))
        if a.bases.mode == "graded":
            for bi, base in enumerate(list(a.bases.grades)):
                g = form.get(f"arch.{a.id}.base.{bi}")
                if g in ("S", "A", "B", "C", "D"):
                    a.bases.grades[base] = g
                elif g == "":
                    a.bases.grades.pop(base, None)


def _ui_dir() -> str:
    """The stasher/ui directory holding templates/ and static/. Resolves both in dev and
    in a PyInstaller bundle (where data is extracted under sys._MEIPASS)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return os.path.join(meipass, "stasher", "ui")
    return os.path.dirname(os.path.abspath(__file__))


def create_app(stasher) -> Flask:
    ui = _ui_dir()
    app = Flask(
        __name__,
        template_folder=os.path.join(ui, "templates"),
        static_folder=os.path.join(ui, "static"),
    )
    # Archetype-set / filter uploads post the file contents as a form field. Werkzeug 3.1 caps
    # form-field memory at 500 KB by default; a full mined archetype set is ~1 MB, so raise the
    # limits (these are local, single-user requests).
    app.config["MAX_FORM_MEMORY_SIZE"] = 32 * 1024 * 1024
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
    store = stasher.store
    worker = stasher.worker()
    # Resume the auto-refresh loop if it was on last session.
    if store.get_setting("auto_mode", "off") == "on":
        worker.start_auto()

    _LEAGUES_TTL = 12 * 3600

    def leagues_cached() -> list[str]:
        raw = store.get_meta("leagues_cache")
        try:
            return json.loads(raw) if raw else []
        except (ValueError, TypeError):
            return []

    def leagues_fetch(force: bool = False) -> list[str]:
        cached = leagues_cached()
        ts = float(store.get_meta("leagues_cached_at") or 0)
        if cached and not force and (time.time() - ts) < _LEAGUES_TTL:
            return cached
        fresh = stasher.client.fetch_leagues()
        if fresh:
            store.set_meta("leagues_cache", json.dumps(fresh))
            store.set_meta("leagues_cached_at", str(time.time()))
            return fresh
        return cached  # stale/empty fallback when the trade site is unreachable

    def _setup_ok() -> bool:
        creds = stasher.client.creds()
        return bool(creds.account and creds.poesessid)

    @app.route("/")
    def records():
        # Shell only; the table is populated client-side from /api/records (the whole
        # dataset is assumed to fit in memory, <10k rows).
        return render_template("records.html", setup_ok=_setup_ok())

    @app.route("/api/records")
    def api_records():
        out = []
        for r in store.all_records():
            try:
                reasons = json.loads(r["reasons"]) if r["reasons"] else []
            except (ValueError, TypeError):
                reasons = []
            price = (
                f"{r['price_amount']:g} {r['price_currency']}"
                if r["price_amount"] else ""
            )
            out.append({
                "hash": r["hash"],
                "name": r["item_name"] or "",
                "type": r["type_line"] or "",
                "rarity": r["rarity"] or "",
                "price": price,
                "flagged": bool(r["flagged"]),
                "reasons": reasons,
                "score": r["score"],
                "listed": r["listed_at"] or "",
                "fetched": r["fetched_at"] or "",
            })
        return jsonify(out)

    @app.route("/records/<item_hash>/card")
    def record_card(item_hash):
        row = store.get_record(item_hash)
        if not row:
            return "not found", 404
        try:
            entry = json.loads(row["raw_json"])
        except (ValueError, TypeError):
            return "bad record", 500
        try:
            reasons = json.loads(row["reasons"]) if row["reasons"] else []
        except (ValueError, TypeError):
            reasons = []
        item = entry.get("item") or {}
        card = build_card(item)
        listing = entry.get("listing") or {}
        return render_template(
            "record_detail.html", c=card, reasons=reasons,
            whisper=listing.get("whisper"),
            score_breakdown=stasher.evaluator.explain_score(item),
        )

    @app.route("/records/<item_hash>/rules")
    def record_rules(item_hash):
        """The matched-rules popup fragment: the live archetype breakdown (overall = peak +
        breadth, per-rule contributions + calculation) and the craftable upgrade-path targets."""
        row = store.get_record(item_hash)
        if not row:
            return "not found", 404
        try:
            entry = json.loads(row["raw_json"])
        except (ValueError, TypeError):
            return "bad record", 500
        item = entry.get("item") or {}
        bd = stasher.evaluator.explain_score(item)
        if bd is None:
            return "<p class='muted'>The archetype_set checker is not active.</p>"
        return render_template("_rules_popup.html", b=bd, hash=item_hash)

    @app.route("/api/rules/<arch_id>/enabled", methods=["POST"])
    def api_rule_enabled(arch_id):
        on = bool((request.get_json(silent=True) or {}).get("enabled", True))
        if not stasher.evaluator.set_archetype_enabled(arch_id, on):
            return jsonify({"ok": False, "error": "no set loaded or unknown rule"}), 404
        return jsonify({"ok": True, "enabled": on})

    @app.route("/queue")
    def queue():
        show_all = request.args.get("all") == "1"
        sort = request.args.get("sort")
        sort = sort if sort in ("matches", "score") else "recent"
        page = max(1, request.args.get("page", 1, type=int))
        total = store.count_queue(show_all)
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(page, pages)
        rows = store.queue_items(
            show_all, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE, sort=sort
        )
        items = []
        for r in rows:
            d = dict(r)
            d["score"] = r["score"]
            d["score_tier"] = value_to_tier(r["score"]) if r["score"] is not None else None
            try:
                d["reasons_list"] = json.loads(r["reasons"]) if r["reasons"] else []
            except (ValueError, TypeError):
                d["reasons_list"] = []
            try:
                entry = json.loads(r["raw_json"])
                item = entry.get("item") or {}
                d["card"] = build_card(item)
                d["stash_tab"] = ((entry.get("listing") or {}).get("stash") or {}).get("name")
                d["stash_regex"] = stash_regex(item)
            except (ValueError, TypeError):
                d["card"] = None
                d["stash_tab"] = None
                d["stash_regex"] = ""
            items.append(d)
        return render_template(
            "queue.html",
            items=items,
            total=total,
            unseen=store.count_unseen(),
            page=page,
            pages=pages,
            show_all=show_all,
            sort=sort,
            setup_ok=_setup_ok(),
        )

    @app.route("/api/queue/seen/<item_hash>", methods=["POST"])
    def api_queue_seen(item_hash):
        store.mark_seen(item_hash)
        return jsonify({"seen": True})

    @app.route("/api/queue/seen_all", methods=["POST"])
    def api_queue_seen_all():
        return jsonify({"marked": store.mark_all_seen()})

    def _render_settings(**extra):
        evaluator = stasher.evaluator
        rules_text = extra.pop("rules_text_override", None)
        filter_text = extra.pop("filter_text_override", None)
        filter_enabled, filter_disk = evaluator.filter_view()
        ctx = dict(
            account_name=store.get_setting("account_name", "") or "",
            league=store.get_setting("league", stasher.config.league) or "",
            leagues=leagues_cached(),  # instant; the page refreshes it via /api/leagues
            has_poesessid=bool(store.get_setting("poesessid", "")),
            status=store.get_setting("status", stasher.config.status) or stasher.config.status,
            status_options=TRADE_STATUS_OPTIONS,
            saved=request.args.get("saved"),
            items_total=store.count_items(),
            rules_path=str(evaluator.edit_path()),
            rules_text=rules_text if rules_text is not None else evaluator.rules_text(),
            filter_enabled=filter_enabled,
            filter_text=filter_text if filter_text is not None else filter_disk,
            archetype_set_enabled=evaluator.archetype_set_is_enabled(),
            rules_error=None,
            rules_message=None,
        )
        ctx.update(extra)
        return render_template("settings.html", **ctx)

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        if request.method == "POST":
            for key in ("account_name", "league"):
                store.set_setting(key, request.form.get(key, "").strip())
            status = request.form.get("status", "").strip()
            if status in TRADE_STATUS_VALUES:  # ignore anything not a known option
                store.set_setting("status", status)
            poesessid = request.form.get("poesessid", "").strip()
            if poesessid:  # don't wipe an existing session on an empty submit
                store.set_setting("poesessid", poesessid)
            return redirect(url_for("settings", saved=1))
        return _render_settings()

    @app.route("/settings/rules", methods=["POST"])
    def settings_rules():
        rules_text = request.form.get("rules_toml", "")
        filter_text = request.form.get("filter_text", "")
        try:
            stasher.evaluator.save_rules(rules_text, filter_text)
        except ValueError as exc:
            return _render_settings(
                rules_error=str(exc),
                rules_text_override=rules_text,
                filter_text_override=filter_text,
            )
        # Always re-evaluate the archive so the queue reflects the saved rules (cheap,
        # local). Otherwise existing items keep stale verdicts and the queue looks wrong.
        summary = stasher.reevaluate_all(force=True)
        message = (
            f"Saved · re-evaluated {summary['evaluated']} items, "
            f"{summary['flagged']} flagged."
        )
        return _render_settings(rules_message=message)

    @app.route("/settings/archetype_set_enabled", methods=["POST"])
    def settings_archetype_set_enabled():
        on = request.form.get("enabled") == "on"
        stasher.evaluator.set_archetype_set_enabled(on)
        stasher.reevaluate_all(force=True)
        return redirect(url_for("settings", saved=1))

    def _render_rules(**extra):
        ev = stasher.evaluator
        ctx = dict(aset=ev.archetype_set(), enabled=ev.archetype_set_is_enabled(),
                   rules_error=None, rules_message=None)
        ctx.update(extra)
        return render_template("rules.html", **ctx)

    def _reeval_msg() -> str:
        summary = stasher.reevaluate_all(force=True)
        return f"Saved · re-evaluated {summary['evaluated']} items, {summary['flagged']} flagged."

    @app.route("/rules")
    def rules():
        return _render_rules()

    @app.route("/rules/save", methods=["POST"])
    def rules_save():
        aset = stasher.evaluator.archetype_set()
        if aset is None:
            return _render_rules(rules_error="No archetype set loaded — upload one first.")
        _apply_archetype_edits(aset, request.form)
        stasher.evaluator.save_archetype_set(aset)
        return _render_rules(rules_message=_reeval_msg())

    @app.route("/rules/reevaluate", methods=["POST"])
    def rules_reevaluate():
        """Re-score every stored item against the current set — applies popup rule toggles (and
        any saved edits) to the queue without re-uploading a set."""
        return _render_rules(rules_message=_reeval_msg())

    @app.route("/rules/upload", methods=["POST"])
    def rules_upload():
        text = request.form.get("archetype_set_text", "")
        if not text.strip():
            return _render_rules(rules_error="Nothing to upload.")
        try:
            stasher.evaluator.upload_archetype_set(text)
        except ValueError as exc:
            return _render_rules(rules_error=str(exc))
        return _render_rules(rules_message="Uploaded · " + _reeval_msg())

    @app.route("/rules/restore", methods=["POST"])
    def rules_restore():
        if not stasher.evaluator.restore_archetype_set_defaults():
            return _render_rules(rules_error="No default copy to restore (upload a set first).")
        return _render_rules(rules_message="Restored defaults · " + _reeval_msg())

    @app.route("/log")
    def log():
        rows = list(reversed(store.recent_queries(300)))  # chronological
        lines = []
        for r in rows:
            head = f"{r['ts']}  {r['kind']:<6} {(r['status'] or ''):<12} {r['http_code'] or '':<4}"
            tail = f"  {r['target'] or ''}"
            if r["detail"]:
                tail += f"  | {r['detail']}"
            lines.append(head + tail)
        return render_template("log.html", text="\n".join(lines), count=len(rows))

    @app.route("/api/leagues")
    def api_leagues():
        return jsonify(leagues_fetch(force=request.args.get("refresh") == "1"))

    @app.route("/api/test-connection", methods=["POST"])
    def api_test_connection():
        creds = stasher.client.creds()
        if not (creds.account and creds.poesessid):
            return jsonify({"ok": False, "detail": "Set account name and POESESSID, then Save first."})
        try:
            res = stasher.client.search(target="test")
            return jsonify({"ok": True, "total": res.get("total", 0)})
        except Exception as exc:  # noqa: BLE001 - report any failure to the UI
            return jsonify({"ok": False, "detail": str(exc)[:180]})

    @app.route("/api/status")
    def api_status():
        return jsonify(worker.status())

    # Manual backfill routes removed from the UI: Auto-refresh now manages capture
    # (it decides light poll vs. full backfill itself). The Worker.start_backfill /
    # pause / resume / stop_backfill methods are kept for reference / library use.

    @app.route("/api/auto/start", methods=["POST"])
    def api_auto_start():
        creds = stasher.client.creds()
        if not (creds.account and creds.poesessid):
            return jsonify({"state": worker.auto_state(), "error": "setup required"}), 400
        worker.start_auto()
        return jsonify({"state": worker.auto_state()})

    @app.route("/api/auto/stop", methods=["POST"])
    def api_auto_stop():
        worker.stop_auto()
        return jsonify({"state": worker.auto_state()})

    @app.route("/api/rate_mode/<mode>", methods=["POST"])
    def api_rate_mode(mode):
        return jsonify({"mode": worker.set_rate_mode(mode)})

    @app.route("/api/resync", methods=["POST"])
    def api_resync():
        # Destructive: drops archived items + evaluations, then full re-fetch.
        return jsonify({"started": worker.force_resync()})

    # Live-websocket routes (start/stop/test) intentionally removed from the UI: PoE2
    # encrypts the live payload so it can't be read for capture. The backend methods
    # (Worker.start_live / stop_live / test_live, stasher/live.py) are kept for reference
    # and CLI use. Use Auto-refresh (/api/auto/*, above) for near-live capture instead.

    return app

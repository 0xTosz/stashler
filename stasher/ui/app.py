"""Minimal Flask UI: browse records, review queue, settings, live status bar."""

from __future__ import annotations

import json
import math
import os
import sys
import time

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for

from ..config import TRADE_STATUS_OPTIONS, TRADE_STATUS_VALUES
from ..evaluate.archetype_model import value_to_tier
from ..evaluate.itemdata import clean_mod_text, stash_regex
from ..pricing.appraise import data_ready as pricing_data_ready
from .itemcard import build_card

PAGE_SIZE = 50

# Per-checker presentation: label + chip color. One chip per checker fires in the queue, so the
# user can tell at a glance which checker flagged an item (and filter/sort by it). Keys match each
# checker's ``name`` (see stasher/evaluate/checks/*). ``order`` controls chip ordering.
CHECKERS: dict[str, dict] = {
    "archetype_set": {"label": "Ruleset", "color": "#8ab6ff"},
    "item_filter":   {"label": "Filter",  "color": "#9be3a3"},
    "regex":         {"label": "Regex",   "color": "#cdb46a"},
    "unique_roll":   {"label": "Unique",  "color": "#c79be3"},
}

# Tier accent colors (match the .tier-* text colors in base.html) — the Ruleset chip is tinted by
# the item's tier so its quality reads at a glance.
TIER_COLORS = {"S": "#ffcf6b", "A": "#9be3a3", "B": "#8ab6ff", "C": "#aeb6c0", "D": "#8b929c"}


def _checker_chips(results: list[dict], score: float | None) -> list[dict]:
    """Collapse a stored ``results`` list into at most one chip per checker (queue + detail card).

    The ``archetype_set`` chip carries the overall tier+score and the matched-rule count; other
    checkers show their label + a fired-rule count, with the joined explanations as a tooltip."""
    by_checker: dict[str, list[dict]] = {}
    for r in results or []:
        by_checker.setdefault(r.get("checker", ""), []).append(r)
    chips = []
    for name in CHECKERS:
        hits = by_checker.get(name)
        if not hits:
            continue
        meta = CHECKERS[name]
        chip = {"checker": name, "label": meta["label"], "color": meta["color"],
                "title": " · ".join(h.get("explanation", "") for h in hits)}
        if name == "archetype_set":
            # True total comes from the headline's `count` (only a few per-rule reasons are stored).
            headline = next((h for h in hits if h.get("rule") == "archetype_set"), None)
            ruleset = (headline or {}).get("count")
            if ruleset is None:
                ruleset = sum(1 for h in hits if str(h.get("rule", "")).startswith("archetype_set:"))
            chip["count"] = ruleset
            chip["text"] = f"{ruleset} rule" + ("" if ruleset == 1 else "s")
            if score is not None:
                chip["tier"] = value_to_tier(score)
                chip["score"] = score
                # Chip keeps its checker color (identity); only the score segment is tier-tinted.
                chip["tier_color"] = TIER_COLORS.get(chip["tier"], meta["color"])
        else:
            chip["count"] = len(hits)
            chip["text"] = f"{len(hits)} match" + ("" if len(hits) == 1 else "es")
        chips.append(chip)
    return chips


def _format_feedback(records) -> str:
    """TEMP: render stored scoring-feedback rows into one analyzable text blob. Each record carries
    a short ref (item hash prefix), the item summary, the user's note, its mod lines, and the full
    raw fetch JSON so nothing is lost."""
    blocks = []
    for r in records:
        ref = (r["item_hash"] or "")[:8]
        name, base, rarity = r["item_name"] or "", r["type_line"] or "", r["rarity"] or "?"
        score = f"{r['score']:.3f}" if r["score"] is not None else "—"
        title = (f"{name} ({base})" if name and base else (name or base or "(unnamed)"))
        lines = [f"### {ref}  {title}  [{rarity} · score {score}]", f"note: {r['note']}"]
        try:
            item = (json.loads(r["raw_json"]) or {}).get("item") or {}
            mods = [clean_mod_text(m) for m in (item.get("explicitMods") or [])]
            if mods:
                lines.append("mods: " + " | ".join(mods))
        except (ValueError, TypeError):
            pass
        lines.append(f"hash: {r['item_hash']}")
        lines.append(f"raw: {r['raw_json'] or ''}")
        blocks.append("\n".join(lines))
    header = (f"# Stashler scoring feedback — {len(records)} record(s)\n"
              "# per record: ### <ref> <item> [<rarity> · score] / note / mods / hash / raw(json)\n\n")
    return header + "\n\n".join(blocks) + ("\n" if blocks else "(no feedback recorded)\n")


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
    ct = fnum("scoring.craft_target")
    if ct is not None:
        aset.scoring.craft_target = max(1, min(6, int(ct)))
    mc = fnum("scoring.magic_completion")
    if mc is not None:
        aset.scoring.magic_completion = max(0.0, min(1.0, mc))
    ms = fnum("scoring.magic_solo")
    if ms is not None:
        aset.scoring.magic_solo = max(0.0, min(1.0, ms))
    bc = fnum("scoring.breadth_cap")
    if bc is not None:
        aset.scoring.breadth_cap = max(0.0, min(1.0, bc))
    rr = fnum("scoring.rarity_ref")
    if rr is not None:
        aset.scoring.rarity_ref = max(0.001, min(1.0, rr))
    rg = fnum("scoring.rarity_gamma")
    if rg is not None:
        aset.scoring.rarity_gamma = max(0.0, min(3.0, rg))
    rcap = fnum("scoring.rarity_cap")
    if rcap is not None:
        aset.scoring.rarity_cap = max(1.0, min(20.0, rcap))
    rfs = fnum("scoring.rarity_floor_scale")
    if rfs is not None:
        aset.scoring.rarity_floor_scale = max(0.1, min(20.0, rfs))

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
        try:
            results = json.loads(row["results"]) if row["results"] else []
        except (ValueError, TypeError):
            results = []
        item = entry.get("item") or {}
        card = build_card(item)
        # Non-ruleset reasons (filter/regex/unique) are surfaced as plain lines; the archetype_set
        # detail comes from explain_score's structured breakdown (tabs).
        other_reasons = [r.get("explanation", "") for r in results
                         if r.get("checker") != "archetype_set"]
        return render_template(
            "_detail_card.html", c=card, reasons=reasons,
            chips=_checker_chips(results, row["score"]),
            other_reasons=other_reasons,
            score_breakdown=stasher.evaluator.explain_score(item),
            hash=item_hash,
        )

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
        sort = sort if sort in ("matches", "score", "checkers", "ruleset") else "recent"
        page = max(1, request.args.get("page", 1, type=int))
        rarities = [r for r in request.args.getlist("rarity") if r]
        checkers = [c for c in request.args.getlist("checker") if c in CHECKERS]
        cutoff_magic, cutoff_rare = store._score_cutoffs()
        total = store.count_queue(show_all, rarities=rarities, checkers=checkers)
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(page, pages)
        rows = store.queue_items(
            show_all, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE, sort=sort,
            rarities=rarities, checkers=checkers,
        )
        notes = store.feedback_notes()  # TEMP: local scoring-feedback notes, keyed by item hash
        items = []
        for r in rows:
            d = dict(r)
            d["score"] = r["score"]
            d["score_tier"] = value_to_tier(r["score"]) if r["score"] is not None else None
            d["feedback"] = notes.get(r["hash"], "")
            try:
                results = json.loads(r["results"]) if r["results"] else []
            except (ValueError, TypeError):
                results = []
            d["chips"] = _checker_chips(results, r["score"])
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
            checkers_meta=CHECKERS,
            rarities_present=store.queue_rarities(show_all),
            active_rarities=rarities,
            active_checkers=checkers,
            cutoff_magic=cutoff_magic,
            cutoff_rare=cutoff_rare,
            archive_stale=store.has_stale_evaluations(stasher.evaluator.rules_hash),
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
            cutoff_magic=store._score_cutoffs()[0],
            cutoff_rare=store._score_cutoffs()[1],
            price_cache_ttl=store.get_setting("price_cache_ttl_hours", "24") or "24",
            price_cache_count=store.count_price_cache(),
            pricing_ready=pricing_data_ready(store)[0],
            pricing_reason=pricing_data_ready(store)[1],
            rules_error=None,
            rules_message=None,
        )
        ctx.update(extra)
        return render_template("settings.html", **ctx)

    @app.route("/settings/queue_cutoff", methods=["POST"])
    def settings_queue_cutoff():
        """Persist the per-rarity ruleset score cutoff (editable from the queue *and* settings).
        Redirects back to wherever it was submitted (queue keeps its filters via ``next``)."""
        for field, key in (("magic", "queue_score_cutoff_magic"), ("rare", "queue_score_cutoff_rare")):
            try:
                v = max(0.0, min(1.0, float(request.form.get(field, "") or 0)))
            except (ValueError, TypeError):
                v = 0.0
            store.set_setting(key, f"{v:.2f}")
        return redirect(request.form.get("next") or request.referrer or url_for("queue"))

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
                   archive_stale=store.has_stale_evaluations(ev.rules_hash),
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
        # Saved notes (newest first) for the inline edit/remove list. Rebuild the full item card
        # from the snapshot so the user sees the same properties/mods as on the queue.
        notes = []
        for r in reversed(store.feedback_records()):
            try:
                item = (json.loads(r["raw_json"]) if r["raw_json"] else {}).get("item") or {}
                card = build_card(item)
            except (ValueError, TypeError):
                card = None
            notes.append({
                "hash": r["item_hash"], "note": r["note"],
                "name": r["item_name"], "type_line": r["type_line"],
                "rarity": r["rarity"] or "Normal", "score": r["score"],
                "tier": value_to_tier(r["score"]) if r["score"] is not None else None,
                "created_at": r["created_at"], "card": card,
            })
        return render_template("log.html", text="\n".join(lines), count=len(rows),
                               feedback_count=store.count_feedback(), notes=notes)

    # --- scoring feedback (TEMPORARY) -----------------------------------
    @app.route("/api/feedback", methods=["POST"])
    def api_feedback():
        data = request.get_json(silent=True) or {}
        item_hash = (data.get("hash") or "").strip()
        if not item_hash:
            return jsonify({"ok": False, "error": "missing hash"}), 400
        stored = store.set_feedback(item_hash, data.get("note") or "")
        return jsonify({"ok": True, "stored": stored, "count": store.count_feedback()})

    @app.route("/api/feedback/edit", methods=["POST"])
    def api_feedback_edit():
        """Edit/remove a saved note in place from the log-page list (keyed by hash; blank = remove)."""
        data = request.get_json(silent=True) or {}
        item_hash = (data.get("hash") or "").strip()
        if not item_hash:
            return jsonify({"ok": False, "error": "missing hash"}), 400
        stored = store.edit_feedback(item_hash, data.get("note") or "")
        return jsonify({"ok": True, "stored": stored, "count": store.count_feedback()})

    @app.route("/feedback/export")
    def feedback_export():
        text = _format_feedback(store.feedback_records())
        fname = f"stashler-feedback-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        return Response(text, mimetype="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    @app.route("/feedback/clear", methods=["POST"])
    def feedback_clear():
        store.clear_feedback()
        return redirect(url_for("log"))

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
        base = worker.status()
        base["pricing"] = stasher.pricing().status()
        return jsonify(base)

    # --- price checking -------------------------------------------------

    def _price_item(item_hash):
        """(item dict, error response) for a hash — None item means the tuple's 2nd is the
        ready-to-return error tuple."""
        row = store.get_record(item_hash)
        if not row:
            return None, (jsonify({"ok": False, "error": "unknown item"}), 404)
        try:
            item = (json.loads(row["raw_json"]) or {}).get("item") or {}
        except (ValueError, TypeError):
            return None, (jsonify({"ok": False, "error": "bad record"}), 500)
        return item, None

    @app.route("/api/price/<item_hash>")
    def api_price_lookup(item_hash):
        """Cache-only lookup (no network): the last/similar known estimate, plus whether a fresh
        check is currently allowed (the Phase-0 data interlock)."""
        item, err = _price_item(item_hash)
        if err:
            return err
        svc = stasher.pricing()
        res = svc.lookup(item)
        ready, reason = pricing_data_ready(store)
        res.update(ok=True, can_refresh=ready, refresh_reason=reason, pricing=svc.status())
        return jsonify(res)

    @app.route("/api/price/<item_hash>", methods=["POST"])
    def api_price_request(item_hash):
        """Enqueue a fresh price check (background, rate-limited). Refused while the Phase-0
        stat data is unharvested."""
        item, err = _price_item(item_hash)
        if err:
            return err
        return jsonify(stasher.pricing().request(item_hash, item))

    @app.route("/settings/price_cache/clear", methods=["POST"])
    def settings_clear_price_cache():
        n = store.clear_price_cache()
        return redirect(url_for("settings", saved=f"Cleared {n} cached price(s)"))

    @app.route("/settings/price_ttl", methods=["POST"])
    def settings_price_ttl():
        try:
            v = max(0.0, float(request.form.get("price_cache_ttl_hours", "") or 24))
        except (ValueError, TypeError):
            v = 24.0
        store.set_setting("price_cache_ttl_hours", str(v))
        return redirect(request.form.get("next") or url_for("settings"))

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

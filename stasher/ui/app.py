"""Minimal Flask UI: browse records, settings, live status bar, manual backfill."""

from __future__ import annotations

import math

from flask import Flask, jsonify, redirect, render_template, request, url_for

PAGE_SIZE = 50


def create_app(stasher) -> Flask:
    app = Flask(__name__)
    store = stasher.store
    worker = stasher.worker()

    @app.route("/")
    def records():
        q = request.args.get("q", "").strip() or None
        rarity = request.args.get("rarity", "").strip() or None
        page = max(1, request.args.get("page", 1, type=int))
        total = store.count_items(q, rarity)
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(page, pages)
        rows = store.iter_items(q, rarity, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
        return render_template(
            "records.html",
            rows=rows,
            total=total,
            page=page,
            pages=pages,
            q=q or "",
            rarity=rarity or "",
        )

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        if request.method == "POST":
            for key in ("account_name", "league"):
                store.set_setting(key, request.form.get(key, "").strip())
            poesessid = request.form.get("poesessid", "").strip()
            if poesessid:  # don't wipe an existing session on an empty submit
                store.set_setting("poesessid", poesessid)
            return redirect(url_for("settings", saved=1))
        return render_template(
            "settings.html",
            account_name=store.get_setting("account_name", "") or "",
            league=store.get_setting("league", stasher.config.league) or "",
            has_poesessid=bool(store.get_setting("poesessid", "")),
            saved=request.args.get("saved"),
        )

    @app.route("/api/status")
    def api_status():
        return jsonify(worker.status())

    @app.route("/api/backfill", methods=["POST"])
    def api_backfill():
        started = worker.start_backfill()
        return jsonify({"started": started, "running": worker.backfill_running()})

    @app.route("/api/backfill/pause", methods=["POST"])
    def api_backfill_pause():
        return jsonify({"paused": worker.pause_backfill()})

    @app.route("/api/backfill/resume", methods=["POST"])
    def api_backfill_resume():
        return jsonify({"resumed": worker.resume_backfill()})

    @app.route("/api/backfill/stop", methods=["POST"])
    def api_backfill_stop():
        worker.stop_backfill()
        return jsonify({"stopped": True})

    @app.route("/api/rate_mode/<mode>", methods=["POST"])
    def api_rate_mode(mode):
        return jsonify({"mode": worker.set_rate_mode(mode)})

    @app.route("/api/live/start", methods=["POST"])
    def api_live_start():
        return jsonify({"started": worker.start_live()})

    @app.route("/api/live/stop", methods=["POST"])
    def api_live_stop():
        worker.stop_live()
        return jsonify({"stopped": True})

    return app

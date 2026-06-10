"""Offline smoke test for the pricing UI wiring: the Flask app builds, the Settings page
renders the pricing card, and the /api/price + /api/status routes work end-to-end against a
fresh Stasher (no network — the data interlock keeps a fresh check from issuing searches)."""

import json
import tempfile

from stasher import Stasher
from stasher.store import ItemRecord
from stasher.ui.app import create_app


def _app():
    tmp = tempfile.mkdtemp()
    stasher = Stasher.from_config(data_dir=tmp)
    app = create_app(stasher)
    app.config.update(TESTING=True)
    return stasher, app


def _insert_item(stasher, item_hash="abc"):
    item = {"frameType": 1, "baseType": "Iron Ring", "typeLine": "Iron Ring",
            "extended": {"mods": {"explicit": [
                {"magnitudes": [{"hash": "explicit.stat_life", "min": "20", "max": "30"}]}]}}}
    entry = {"id": item_hash, "listing": {}, "item": item}
    stasher.store.insert_item(ItemRecord(
        hash=item_hash, account="me", listed_at=None, price_amount=None, price_currency=None,
        price_type=None, item_name=None, type_line="Iron Ring", rarity="Magic",
        whisper=None, league="Std", raw_json=json.dumps(entry)))


def test_settings_page_renders_pricing_card():
    stasher, app = _app()
    try:
        body = app.test_client().get("/settings").get_data(as_text=True)
        assert "Pricing" in body and "Clear price cache" in body
    finally:
        stasher.close()


def test_status_includes_pricing():
    stasher, app = _app()
    try:
        data = app.test_client().get("/api/status").get_json()
        assert "pricing" in data and "queued" in data["pricing"]
    finally:
        stasher.close()


def test_price_lookup_and_refusal():
    stasher, app = _app()
    try:
        _insert_item(stasher)
        client = app.test_client()
        look = client.get("/api/price/abc").get_json()
        assert look["ok"] and look["status"] == "miss"
        assert look["can_refresh"] is False  # Phase-0 interlock blocks live pricing
        # POST a fresh check -> refused (no network issued).
        post = client.post("/api/price/abc").get_json()
        assert post["ok"] is False and "Phase 0" in post["reason"]
        # Unknown item -> 404.
        assert client.get("/api/price/nope").status_code == 404
    finally:
        stasher.close()


def test_clear_price_cache_route():
    stasher, app = _app()
    try:
        stasher.store.cache_price("s", strategy="magic_base", rarity="Magic", base="Iron Ring",
                                  league="Std", filters=[], estimate={"value": 1})
        assert stasher.store.count_price_cache() == 1
        client = app.test_client()
        client.post("/settings/price_cache/clear")
        assert stasher.store.count_price_cache() == 0
    finally:
        stasher.close()

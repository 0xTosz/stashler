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
    entry = {"id": item_hash, "listing": {"stash": {"name": "Quad Tab 3"}}, "item": item}
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


def test_price_lookup_and_refusal(monkeypatch):
    # Force the unharvested state so the POST path is exercised WITHOUT issuing a live search.
    from stasher.pricing import appraise
    monkeypatch.setattr(appraise._pseudo, "_rules",
                        lambda: {"aggregates": [{"_example": True}], "empty_slots": {}})
    stasher, app = _app()
    try:
        _insert_item(stasher)
        client = app.test_client()
        look = client.get("/api/price/abc").get_json()
        assert look["ok"] and look["status"] == "miss"
        assert look["can_refresh"] is False  # interlock blocks live pricing
        post = client.post("/api/price/abc").get_json()
        assert post["ok"] is False and "Phase 0" in post["reason"]
        assert client.get("/api/price/nope").status_code == 404  # unknown item
    finally:
        stasher.close()


def test_price_data_ready_after_harvest():
    # With the shipped (harvested) data, a lookup reports the feature is ready to price.
    stasher, app = _app()
    try:
        _insert_item(stasher)
        look = app.test_client().get("/api/price/abc").get_json()
        assert look["ok"] and look["can_refresh"] is True
    finally:
        stasher.close()


def test_detail_card_shows_blocks_stash_price_and_trade_link():
    stasher, app = _app()
    try:
        _insert_item(stasher)
        body = app.test_client().get("/records/abc/card").get_data(as_text=True)
        # details block: stash tab + a (state-driven) price line + trade link
        assert "Stash" in body and "Quad Tab 3" in body
        assert 'class="price-line"' in body and "not checked" in body  # unpriced item
        assert "Open on trade site" in body and "/trade2/search/poe2/" in body
    finally:
        stasher.close()


def test_detail_card_renders_cached_price():
    stasher, app = _app()
    try:
        _insert_item(stasher)
        sig = "sigX"
        stasher.store.cache_price(sig, strategy="magic_base", rarity="Magic", base="Iron Ring",
                                  league="Std", filters=[],
                                  estimate={"value": 12, "currency": "divine", "confidence": 0.64,
                                            "is_floor": False, "n_samples": 8})
        stasher.store.set_item_price("abc", sig)
        body = app.test_client().get("/records/abc/card").get_data(as_text=True)
        assert "12 divine" in body and "64%" in body
    finally:
        stasher.close()


def test_queue_card_renders_panel_blocks():
    from types import SimpleNamespace
    stasher, app = _app()
    try:
        _insert_item(stasher)
        stasher.store.upsert_evaluation(
            "abc", SimpleNamespace(flagged=True, reasons=["hit"], results=[], score=0.5), "rh")
        body = app.test_client().get("/queue").get_data(as_text=True)
        # the shared details block (price line) + the collapsible note + actions all render
        assert 'class="price-line"' in body and "Quad Tab 3" in body
        assert "note-toggle" in body and "Mark seen" in body
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

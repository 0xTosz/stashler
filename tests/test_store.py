import json

from stasher.store import ItemRecord, Store, record_from_fetch_entry


def make_record(h="abc", name="Test Item"):
    return ItemRecord(
        hash=h, account="me", listed_at="2026-01-01T00:00:00Z",
        price_amount=5.0, price_currency="exalted", price_type="price",
        item_name=name, type_line="Sword", rarity="Rare", whisper="@me hi",
        league="Standard", raw_json="{}",
    )


def test_insert_is_append_only(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    assert store.insert_item(make_record()) is True
    # Same hash, different data -> ignored, original kept.
    assert store.insert_item(make_record(name="Changed")) is False
    assert store.count_items() == 1
    rows = store.iter_items()
    assert rows[0]["item_name"] == "Test Item"
    assert store.has_hash("abc") is True
    assert store.has_hash("nope") is False
    store.close()


def test_filters_and_settings(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.insert_item(make_record(h="a", name="Gold Ring"))
    store.insert_item(make_record(h="b", name="Iron Ring"))
    assert store.count_items(text="Gold") == 1
    assert store.count_items(rarity="Rare") == 2
    assert store.count_items(rarity="Unique") == 0

    store.set_setting("account_name", "Zordon")
    assert store.get_setting("account_name") == "Zordon"
    store.set_setting("account_name", "Other")
    assert store.get_setting("account_name") == "Other"
    assert store.get_setting("missing", "dflt") == "dflt"
    store.close()


def test_query_log(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.log_query("search", "weapon", "ok", 200)
    store.log_query("fetch", "10 ids", "rate_limited", 429, "retry")
    rows = store.recent_queries(10)
    assert rows[0]["kind"] == "fetch"  # most recent first
    assert rows[0]["status"] == "rate_limited"
    assert rows[1]["kind"] == "search"
    store.close()


def test_clear_archive_keeps_settings(tmp_path):
    import types

    store = Store(str(tmp_path / "t.db"))
    store.set_setting("account_name", "Me#1")
    store.set_meta("last_poll_at", "2026-01-01T00:00:00+00:00")
    store.insert_item(make_record(h="h1"))
    store.upsert_evaluation("h1", types.SimpleNamespace(flagged=True, reasons=["r"]), "rh")
    assert store.count_items() == 1 and store.count_queue(show_all=True) == 1

    removed = store.clear_archive()
    assert removed == 1
    assert store.count_items() == 0
    assert store.count_queue(show_all=True) == 0       # evaluations gone too
    assert store.get_setting("account_name") == "Me#1"  # settings kept
    assert store.get_meta("last_poll_at") is None        # sync marker reset
    store.close()


def test_queue_sort_by_match_count(tmp_path):
    import types

    store = Store(str(tmp_path / "t.db"))

    def add(h, n_reasons):
        store.insert_item(make_record(h=h, name=h))
        store.upsert_evaluation(
            h, types.SimpleNamespace(flagged=True, reasons=["r"] * n_reasons), "rh"
        )

    add("a", 1)
    add("b", 3)
    add("c", 2)
    by_recent = [r["hash"] for r in store.queue_items(sort="recent")]
    by_matches = [r["hash"] for r in store.queue_items(sort="matches")]
    assert by_matches == ["b", "c", "a"]   # most matches first
    assert set(by_recent) == {"a", "b", "c"}
    store.close()


def test_pipeline_reset_clears_seen():
    from stasher.pipeline import Pipeline

    p = Pipeline(client=None, store=None)
    p._seen.update(["a", "b"])
    p.reset()
    assert p._seen == set()


def test_record_from_fetch_entry():
    entry = {
        "id": "hash1",
        "listing": {
            "indexed": "2026-06-01T12:00:00Z",
            "account": {"name": "Seller"},
            "price": {"type": "price", "amount": 3, "currency": "divine"},
            "whisper": "@Seller hi",
        },
        "item": {"name": "Foo", "typeLine": "Bar Sword", "frameType": 2, "league": "Standard"},
    }
    rec = record_from_fetch_entry(entry, "Standard")
    assert rec.hash == "hash1"
    assert rec.account == "Seller"
    assert rec.price_amount == 3
    assert rec.rarity == "Rare"
    assert rec.item_name == "Foo"
    assert json.loads(rec.raw_json)["id"] == "hash1"

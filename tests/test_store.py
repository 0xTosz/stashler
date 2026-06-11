import json

from stasher.store import ItemRecord, Store, record_from_fetch_entry


def make_record(h="abc", name="Test Item", rarity="Rare"):
    return ItemRecord(
        hash=h, account="me", listed_at="2026-01-01T00:00:00Z",
        price_amount=5.0, price_currency="exalted", price_type="price",
        item_name=name, type_line="Sword", rarity=rarity, whisper="@me hi",
        league="Standard", raw_json="{}",
    )


def test_feedback_upsert_snapshot_and_clear(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.insert_item(make_record(h="h1", name="Dragon Edge", rarity="Rare"))
    # A note is stored once per item and snapshots the item's name/rarity.
    assert store.set_feedback("h1", "  overrated  ") is True
    assert store.count_feedback() == 1
    assert store.feedback_notes() == {"h1": "overrated"}          # trimmed, keyed by hash
    rec = store.feedback_records()[0]
    assert rec["item_name"] == "Dragon Edge" and rec["rarity"] == "Rare"
    # Re-saving the same item upserts (no dupe); a blank note clears it.
    assert store.set_feedback("h1", "actually fine") is True
    assert store.count_feedback() == 1 and store.feedback_notes()["h1"] == "actually fine"
    assert store.set_feedback("h1", "   ") is False and store.count_feedback() == 0
    # Unknown item → not stored; clear_feedback wipes everything.
    assert store.set_feedback("ghost", "x") is False
    store.set_feedback("h1", "note again")
    assert store.clear_feedback() == 1 and store.count_feedback() == 0
    store.close()


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


def test_queue_dual_score_sorts_and_min_filter(tmp_path):
    import types

    store = Store(str(tmp_path / "t.db"))

    def add(h, overall, now, craft):
        store.insert_item(make_record(h=h, name=h))
        store.upsert_evaluation(
            h, types.SimpleNamespace(flagged=True, reasons=["r"], score=overall,
                                     score_now=now, score_potential=craft,
                                     driver="craft" if craft > now else "now"), "rh")

    add("finished", 0.7, 0.70, 0.70)
    add("craftbase", 0.6, 0.30, 0.85)
    add("junk", 0.2, 0.20, 0.25)

    assert [r["hash"] for r in store.queue_items(sort="score")][:2] == ["finished", "craftbase"]
    assert [r["hash"] for r in store.queue_items(sort="now")][:2] == ["finished", "craftbase"]
    assert [r["hash"] for r in store.queue_items(sort="craft")][:2] == ["craftbase", "finished"]

    # min-score filter on each basis (the craft basis surfaces the craft base alone)
    by_craft = store.queue_items(score_by="craft", score_min=0.8)
    assert [r["hash"] for r in by_craft] == ["craftbase"]
    by_now = store.queue_items(score_by="now", score_min=0.5)
    assert [r["hash"] for r in by_now] == ["finished"]
    assert store.count_queue(score_by="overall", score_min=0.5) == 2
    # the split round-trips for the UI
    row = next(r for r in store.queue_items() if r["hash"] == "craftbase")
    assert (row["score_now"], row["score_potential"], row["score_driver"]) == (0.3, 0.85, "craft")
    store.close()


def test_queue_sort_by_cached_price(tmp_path):
    import types

    store = Store(str(tmp_path / "t.db"))

    def add(h, price=None, currency="exalted"):
        store.insert_item(make_record(h=h, name=h))
        store.upsert_evaluation(h, types.SimpleNamespace(flagged=True, reasons=["r"]), "rh")
        if price is not None:
            store.cache_price(f"sig-{h}", strategy="rare_finished", rarity="Rare", base=None,
                              league="Std", filters=[],
                              estimate={"value": price, "currency": currency,
                                        "is_floor": False, "n_samples": 8})
            store.set_item_price(h, f"sig-{h}")

    add("cheap", price=3)
    add("divine", price=2, currency="divine")     # 2 div ≫ 3 ex after normalization
    add("unpriced")
    add("alien", price=50, currency="mirror_shard")   # unknown currency -> sorts with unpriced

    order = [r["hash"] for r in store.queue_items(sort="price")]
    assert order[:2] == ["divine", "cheap"]           # base-unit normalized, not raw numbers
    assert set(order[2:]) == {"unpriced", "alien"}    # no usable price -> bottom
    store.close()


def test_queue_per_checker_filters_and_sorts(tmp_path):
    import types

    store = Store(str(tmp_path / "t.db"))

    def R(checker, rule="x", expl="e", score=None):
        return {"checker": checker, "rule": rule, "explanation": expl, "score": score}

    def add(h, rarity, results, score=None):
        store.insert_item(make_record(h=h, name=h, rarity=rarity))
        ev = types.SimpleNamespace(flagged=True, score=score, results=results,
                                   reasons=[r["explanation"] for r in results])
        store.upsert_evaluation(h, ev, "rh")

    # a: Rare, flagged by ruleset (2 mined rules) + filter; b: Magic, filter only; c: Rare, unique
    add("a", "Rare", [R("archetype_set", "archetype_set", "ov", 0.8),
                      R("archetype_set", "archetype_set:1"), R("archetype_set", "archetype_set:2"),
                      R("item_filter", "filter")], score=0.8)
    add("b", "Magic", [R("item_filter", "filter")])
    add("c", "Rare", [R("unique_roll", "u")])

    hsah = lambda rows: {r["hash"] for r in rows}
    # rarity filter
    assert hsah(store.queue_items(rarities=["Magic"])) == {"b"}
    assert hsah(store.queue_items(rarities=["Rare"])) == {"a", "c"}
    # checker filter (OR semantics)
    assert hsah(store.queue_items(checkers=["item_filter"])) == {"a", "b"}
    assert hsah(store.queue_items(checkers=["unique_roll", "archetype_set"])) == {"a", "c"}
    # combine rarity + checker
    assert hsah(store.queue_items(rarities=["Rare"], checkers=["item_filter"])) == {"a"}
    # new sorts: most checkers / most ruleset matches both put 'a' first
    assert [r["hash"] for r in store.queue_items(sort="checkers")][0] == "a"
    assert [r["hash"] for r in store.queue_items(sort="ruleset")][0] == "a"
    # counts respect filters
    assert store.count_queue(rarities=["Magic"]) == 1
    assert store.count_queue(checkers=["item_filter"]) == 2
    # denormalized columns persisted
    row = next(r for r in store.queue_items() if r["hash"] == "a")
    assert row["checker_count"] == 2 and row["ruleset_matches"] == 2
    assert hsah(store.queue_items(rarities=["Normal"])) == set()   # rarity not present → empty
    store.queue_rarities()  # smoke
    store.close()


def test_ruleset_matches_uses_headline_count_not_surfaced_reasons(tmp_path):
    """The archetype_set checker surfaces only a few per-rule reasons but the headline carries the
    true total — ruleset_matches must reflect the total (e.g. 12), not the surfaced count (3)."""
    import types

    store = Store(str(tmp_path / "t.db"))
    store.insert_item(make_record(h="z", name="z", rarity="Rare"))
    results = [{"checker": "archetype_set", "rule": "archetype_set", "explanation": "ov",
                "score": 0.9, "count": 12}]
    results += [{"checker": "archetype_set", "rule": f"archetype_set:{i}", "explanation": "r",
                 "score": None, "count": None} for i in range(3)]   # only 3 surfaced
    ev = types.SimpleNamespace(flagged=True, score=0.9, results=results,
                               reasons=[r["explanation"] for r in results])
    store.upsert_evaluation("z", ev, "rh")
    row = next(r for r in store.queue_items() if r["hash"] == "z")
    assert row["ruleset_matches"] == 12      # headline total, not the 3 surfaced reasons
    store.close()


def test_has_stale_evaluations(tmp_path):
    import types

    store = Store(str(tmp_path / "t.db"))
    store.insert_item(make_record(h="a"))
    store.upsert_evaluation("a", types.SimpleNamespace(
        flagged=True, reasons=["r"], score=None, results=[]), "hash1")
    assert store.has_stale_evaluations("hash2") is True    # different rules version → stale
    assert store.has_stale_evaluations("hash1") is False   # current → fresh
    store.close()


def test_queue_score_cutoff_hides_ruleset_only_low_scores(tmp_path):
    import types

    store = Store(str(tmp_path / "t.db"))

    def add(h, rarity, results, score):
        store.insert_item(make_record(h=h, name=h, rarity=rarity))
        store.upsert_evaluation(h, types.SimpleNamespace(
            flagged=True, score=score, results=results,
            reasons=[x["explanation"] for x in results]), "rh")

    def R(checker, rule, expl="e"):
        return {"checker": checker, "rule": rule, "explanation": expl, "score": None}

    add("rare_lo", "Rare", [R("archetype_set", "archetype_set")], 0.40)
    add("rare_hi", "Rare", [R("archetype_set", "archetype_set")], 0.70)
    add("magic_lo", "Magic", [R("archetype_set", "archetype_set")], 0.40)
    add("rare_lo_filter", "Rare",
        [R("archetype_set", "archetype_set"), R("item_filter", "filter")], 0.40)  # filter keeps it
    add("uniq", "Unique", [R("unique_roll", "u")], None)                          # other checker

    assert store.count_queue() == 5                       # cutoff off (default 0) → all show
    store.set_setting("queue_score_cutoff_rare", "0.6")
    store.set_setting("queue_score_cutoff_magic", "0.5")

    shown = {r["hash"] for r in store.queue_items()}
    assert shown == {"rare_hi", "rare_lo_filter", "uniq"}  # low ruleset-only magic/rare hidden
    assert store.count_queue() == 3
    assert store.count_unseen() == 3                       # nav badge respects the cutoff
    assert store.count_queue(show_all=True) == 5           # "show all evaluated" bypasses it
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

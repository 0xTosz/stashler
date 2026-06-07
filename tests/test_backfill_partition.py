from types import SimpleNamespace

from stasher.backfill import RESULT_CAP, _enumerate
from stasher.pipeline import Pipeline
from stasher.store import Store


class FakeClient:
    """Pretends to be a TradeClient backed by an in-memory item 'world'."""

    def __init__(self, items):
        self.items = items
        self.config = SimpleNamespace(base_url="http://x", realm="poe2")
        self.leaf_totals = []  # totals of searches whose results we actually submitted

    def creds(self):
        return SimpleNamespace(league="Standard")

    def search(self, extra_filters=None, target=""):
        tf = extra_filters["type_filters"]["filters"]
        cat = tf["category"]["option"]
        rarity = tf.get("rarity", {}).get("option")
        ilvl = (extra_filters.get("misc_filters") or {}).get("filters", {}).get("ilvl", {})
        lo, hi = ilvl.get("min", 0), ilvl.get("max", 100)
        matched = [
            it for it in self.items
            if it["category"] == cat
            and lo <= it["ilvl"] <= hi
            and (rarity is None or it["rarity"] == rarity)
        ]
        hashes = [it["hash"] for it in matched][:RESULT_CAP]
        return {"id": "q", "result": hashes, "total": len(matched)}

    def fetch_batch(self, hashes, query_id):
        return [
            {"id": h, "listing": {"account": {"name": "me"}, "price": {}},
             "item": {"name": "i", "frameType": 0}}
            for h in hashes
        ]


def run(items, tmp_path):
    store = Store(str(tmp_path / "bf.db"))
    client = FakeClient(items)
    pipeline = Pipeline(client, store)
    summary = {"new": 0, "partitions": 0, "incomplete": 0}
    _enumerate(client, pipeline, "weapon", summary, None, None)
    return store, summary


def test_small_category_single_search(tmp_path):
    items = [{"hash": f"w{i}", "category": "weapon", "ilvl": 50, "rarity": "rare"}
             for i in range(30)]
    store, summary = run(items, tmp_path)
    assert store.count_items() == 30
    assert summary["partitions"] == 1  # no subdivision needed
    assert summary["incomplete"] == 0


def test_large_category_partitions_until_under_cap(tmp_path):
    items = [
        {"hash": f"w{i}", "category": "weapon",
         "ilvl": (i % 100) + 1,
         "rarity": ["normal", "magic", "rare", "unique"][i % 4]}
        for i in range(250)
    ]
    store, summary = run(items, tmp_path)
    assert store.count_items() == 250          # everything captured
    assert summary["new"] == 250
    assert summary["incomplete"] == 0          # ilvl bisection sufficed
    assert summary["partitions"] > 1           # it actually had to subdivide

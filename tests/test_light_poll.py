"""Tests for the newest-first search sort and the light-poll refresh."""

from stasher.backfill import NEWEST_SORT, poll_indicates_overflow, run_light_poll
from stasher.client import TradeAPIError, _build_query, parse_leagues
from stasher.store import Store


def test_parse_leagues_filters_realm_and_dedups():
    data = {"result": [
        {"id": "Standard", "realm": "poe2", "text": "Standard"},
        {"id": "Rise of the Abyssal", "realm": "poe2"},
        {"id": "PoE1League", "realm": "poe1"},     # wrong realm -> dropped
        {"text": "TextOnly"},                       # no id -> use text, no realm -> kept
        {"id": "Standard"},                          # duplicate -> dropped
    ]}
    assert parse_leagues(data, "poe2") == ["Standard", "Rise of the Abyssal", "TextOnly"]
    assert parse_leagues({}, "poe2") == []
    assert parse_leagues({"result": None}) == []


def test_poll_overflow_detection():
    # Whole account fits on the page (total <= listed) -> complete, no overflow.
    assert poll_indicates_overflow({"new": 3, "listed": 50, "total": 50}) is False
    assert poll_indicates_overflow({"new": 50, "listed": 50, "total": 50}) is False
    # Off-page items exist and no prior anchor (cold start / all-new page) -> overflow.
    assert poll_indicates_overflow({"new": 100, "listed": 100, "total": 150}) is True
    # Empty page -> nothing to miss.
    assert poll_indicates_overflow({"new": 0, "listed": 0, "total": 0}) is False


def test_poll_overflow_anchor_logic():
    page = [f"h{i}" for i in range(100)]  # newest-first page of 100
    # An anchor from last poll is still on the page -> boundary reached -> no overflow.
    res = {"listed": 100, "total": 500, "hashes": page,
           "prev_anchors": ["h5", "hX", "hY"]}  # h5 still present
    assert poll_indicates_overflow(res) is False
    # All anchors pushed off the page (bulk re-index floated 100+ known items up) ->
    # genuinely-new items may have slipped past page 1 -> overflow.
    res_bulk = {"listed": 100, "total": 500, "hashes": page,
                "prev_anchors": ["old1", "old2", "old3"]}
    assert poll_indicates_overflow(res_bulk) is True
    # ...but if the whole account still fits on the page, it's complete regardless.
    assert poll_indicates_overflow({"listed": 100, "total": 100,
                                    "hashes": page, "prev_anchors": ["old1"]}) is False


def test_run_light_poll_tracks_anchors(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "t.db"))

    class FakeClient:
        def __init__(self, hashes):
            self.hashes = hashes

        def search(self, target="account", sort=None):
            return {"id": "q", "result": self.hashes, "total": 500}

    pipe = _FakePipeline()
    # First poll: no prior anchors -> overflow (seed), and it records its top hashes.
    res1 = run_light_poll(FakeClient([f"a{i}" for i in range(100)]), store, pipe)
    assert res1["prev_anchors"] == []
    assert poll_indicates_overflow(res1) is True

    # Second poll, page still overlaps -> anchors carry over, no overflow.
    res2 = run_light_poll(FakeClient([f"a{i}" for i in range(100)]), store, pipe)
    assert res2["prev_anchors"] == [f"a{i}" for i in range(10)]
    assert poll_indicates_overflow(res2) is False

    # Third poll, page fully turned over (bulk re-index) -> overflow.
    res3 = run_light_poll(FakeClient([f"b{i}" for i in range(100)]), store, pipe)
    assert poll_indicates_overflow(res3) is True


def test_build_query_sort_override():
    assert _build_query("Me#1", None, "any", {"indexed": "desc"})["sort"] == {"indexed": "desc"}
    assert _build_query("Me#1", None, "any")["sort"] == {"price": "asc"}  # default


def test_build_query_sets_status_option():
    q = _build_query("Me#1", None, "online")
    assert q["query"]["status"] == {"option": "online"}


def test_search_uses_stored_status_then_config_default(tmp_path, monkeypatch):
    from stasher.client import TradeClient
    from stasher.config import Config

    store = Store(str(tmp_path / "t.db"))
    store.set_setting("account_name", "Me#1")
    client = TradeClient(Config(), store, limiter=None)  # limiter unused (we stub _request)
    captured = {}

    def fake_request(policy, method, url, target, json=None):
        captured["body"] = json
        return {"id": "x", "result": [], "total": 0}

    monkeypatch.setattr(client, "_request", fake_request)

    # No stored setting -> config default ("online", in-person).
    client.search()
    assert captured["body"]["query"]["status"]["option"] == "online"

    # Stored setting wins.
    store.set_setting("status", "securable")
    client.search()
    assert captured["body"]["query"]["status"]["option"] == "securable"


class _FakePipeline:
    def __init__(self):
        self.calls = []

    def submit_hashes(self, hashes, query_id):
        self.calls.append((list(hashes), query_id))
        return len(list(hashes))


def test_run_light_poll_uses_newest_and_dedups(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    seen = {}

    class FakeClient:
        def search(self, target="account", sort=None):
            seen["sort"] = sort
            return {"id": "q1", "result": ["h1", "h2"], "total": 2}

    pipe = _FakePipeline()
    res = run_light_poll(FakeClient(), store, pipe)
    assert seen["sort"] == NEWEST_SORT
    assert pipe.calls == [(["h1", "h2"], "q1")]
    assert (res["new"], res["listed"], res["total"]) == (2, 2, 2)
    assert res["hashes"] == ["h1", "h2"]
    assert store.get_meta("last_poll_at")
    store.close()


def test_run_light_poll_falls_back_when_sort_rejected(tmp_path):
    store = Store(str(tmp_path / "t.db"))

    class FakeClient:
        def search(self, target="account", sort=None):
            if sort is not None:
                raise TradeAPIError("400: invalid sort")
            return {"id": "q", "result": ["h1"], "total": 1}

    res = run_light_poll(FakeClient(), store, _FakePipeline())
    assert res["new"] == 1  # fell back to the default-sort search
    store.close()


def test_worker_auto_toggle(tmp_path, monkeypatch):
    import stasher.runtime as rt
    from stasher import Stasher

    s = Stasher.from_config(db_path=str(tmp_path / "t.db"))
    w = s.worker()
    # Keep the loop offline: stub the full backfill and the light poll.
    monkeypatch.setattr(w, "backfill_blocking", lambda: {"new": 0})
    monkeypatch.setattr(rt, "run_light_poll", lambda *a, **k: {"new": 0, "listed": 0, "total": 0})

    assert w.auto_state() == "off"
    assert w.start_auto() is True
    assert w.auto_running() is True
    assert w.auto_state() == "on"
    assert s.store.get_setting("auto_mode") == "on"
    assert w.start_auto() is False  # already running

    w.stop_auto()
    assert w.auto_running() is False
    assert w.auto_state() == "off"
    assert s.store.get_setting("auto_mode") == "off"
    assert w.status()["auto_state"] == "off"
    s.close()

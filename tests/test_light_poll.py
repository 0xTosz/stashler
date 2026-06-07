"""Tests for the newest-first search sort and the light-poll refresh."""

from stasher.backfill import NEWEST_SORT, poll_indicates_overflow, run_light_poll
from stasher.client import TradeAPIError, _build_query
from stasher.store import Store


def test_poll_overflow_detection():
    # Boundary visible on the page (some item already known) -> caught up, no overflow.
    assert poll_indicates_overflow({"new": 3, "listed": 50, "total": 50}) is False
    # Whole page is one full set; nothing beyond it -> complete, no overflow.
    assert poll_indicates_overflow({"new": 50, "listed": 50, "total": 50}) is False
    # Whole newest page was new AND more listings exist beyond it -> overflow.
    assert poll_indicates_overflow({"new": 100, "listed": 100, "total": 150}) is True
    # Empty page -> nothing to miss.
    assert poll_indicates_overflow({"new": 0, "listed": 0, "total": 0}) is False


def test_build_query_sort_override():
    assert _build_query("Me#1", None, "any", {"indexed": "desc"})["sort"] == {"indexed": "desc"}
    assert _build_query("Me#1", None, "any")["sort"] == {"price": "asc"}  # default


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
    assert res == {"new": 2, "listed": 2, "total": 2}
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

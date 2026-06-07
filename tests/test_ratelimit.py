import time

from stasher.ratelimit import (
    RateLimiter,
    _dedupe_buckets,
    _parse_triplets,
)
from stasher.store import Store


def make_limiter(tmp_path, buckets, margin=0):
    store = Store(str(tmp_path / "rl.db"))
    return RateLimiter(store, margin=margin, fallback=buckets), store


def test_parse_triplets():
    assert _parse_triplets("5:12:60,15:62:120") == [(5, 12, 60.0), (15, 62, 120.0)]
    assert _parse_triplets("") == []
    assert _parse_triplets(None) == []
    assert _parse_triplets("garbage") == []


def test_dedupe_buckets_keeps_most_restrictive():
    assert _dedupe_buckets([(10, 60), (5, 60), (3, 10)]) == [(3, 10), (5, 60)]


def test_wait_needed_blocks_when_window_full(tmp_path):
    rl, store = make_limiter(tmp_path, {"x": [(3, 100)]}, margin=0)
    now = time.time()
    for _ in range(3):
        store.record_rate_event("x", now)
    wait = rl._wait_needed("x")
    assert 90 < wait <= 100  # must wait ~one period for the oldest to age out
    store.close()


def test_wait_needed_clear_under_limit(tmp_path):
    rl, store = make_limiter(tmp_path, {"x": [(3, 100)]}, margin=0)
    now = time.time()
    store.record_rate_event("x", now)
    store.record_rate_event("x", now)
    assert rl._wait_needed("x") == 0.0
    store.close()


def test_margin_reserves_headroom(tmp_path):
    rl, store = make_limiter(tmp_path, {"x": [(3, 100)]}, margin=1)
    now = time.time()
    store.record_rate_event("x", now)
    store.record_rate_event("x", now)  # 2 used, limit_eff = 3 - 1 = 2 -> blocked
    assert rl._wait_needed("x") > 0
    store.close()


def test_after_learns_buckets_and_restriction(tmp_path):
    rl, store = make_limiter(tmp_path, {"search": [(5, 12)]}, margin=0)
    headers = {
        "X-Rate-Limit-Rules": "Ip",
        "X-Rate-Limit-Ip": "8:10:60,15:60:120",
        "X-Rate-Limit-Ip-State": "1:10:0,1:60:0",
    }
    rl.after("search", 200, headers)
    assert rl._buckets["search"] == [(8, 10), (15, 60)]

    rl.after("search", 200, {
        "X-Rate-Limit-Rules": "Ip",
        "X-Rate-Limit-Ip": "8:10:60",
        "X-Rate-Limit-Ip-State": "8:10:45",
    })
    assert rl._restricted_until["search"] > time.time() + 40
    store.close()


def test_restrictive_mode_reserves_more_headroom(tmp_path):
    rl, store = make_limiter(tmp_path, {"search": [(5, 12)]}, margin=1)
    assert rl._limit_eff(5) == 4            # full: 5 - margin(1)
    assert rl.set_mode("restrictive") == "restrictive"
    assert rl._limit_eff(5) == 2            # restrictive: 5 - max(1, ceil(5*0.5)=3)
    assert rl.set_mode("anything-else") == "full"  # only "restrictive" enables it
    assert rl._limit_eff(5) == 4
    store.close()


def test_snapshot_shape(tmp_path):
    rl, store = make_limiter(tmp_path, {"fetch": [(12, 6), (16, 14)]}, margin=0)
    store.record_rate_event("fetch", time.time())
    snap = rl.snapshot()
    assert set(snap.keys()) == {"fetch"}
    first = snap["fetch"][0]
    assert {"max_hits", "period", "used", "remaining", "reset_in", "restricted"} <= set(first)
    assert first["used"] == 1
    store.close()


def test_before_records_event_and_is_fast_when_clear(tmp_path):
    rl, store = make_limiter(tmp_path, {"x": [(5, 100)]}, margin=0)
    start = time.time()
    rl.before("x")
    assert time.time() - start < 1.0
    assert len(store.rate_events_since("x", 0)) == 1
    store.close()

"""Tests for the price-check validation harness (tools/validate_eval.py) — offline."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.validate_eval import (
    CorpusItem,
    _midranks,
    apply_knobs,
    disagreements,
    load_corpus,
    metrics,
    spearman,
)


def test_midranks_share_ties():
    assert _midranks([1.0, 1.0, 2.0]) == [0.5, 0.5, 2.0]
    assert _midranks([3.0, 1.0, 2.0]) == [2.0, 0.0, 1.0]


def test_spearman_perfect_and_inverse_and_ties():
    xs = [1, 2, 3, 4, 5]
    assert spearman(xs, [10, 20, 30, 40, 50]) == pytest.approx(1.0)
    assert spearman(xs, [50, 40, 30, 20, 10]) == pytest.approx(-1.0)
    assert spearman([1, 2, 3, 4], [1, 2, 3, 4]) is None          # n < 5
    # a constant series has zero rank variance -> undefined, not a crash
    assert spearman(xs, [7, 7, 7, 7, 7]) is None


def _ci(h, score_target_ex, rarity="Rare", cls="Rings", conf=0.9):
    return CorpusItem(hash=h, name=h, rarity=rarity, cls=cls, item={},
                      ex=score_target_ex, conf=conf)


def test_metrics_segments_and_bands():
    corpus = [_ci(f"r{i}", ex) for i, ex in enumerate([1, 1, 2, 50, 200])]
    corpus += [_ci(f"m{i}", ex, rarity="Magic", cls="Jewels")
               for i, ex in enumerate([1, 1, 1, 5, 30])]
    # scores aligned with price for rares, anti-aligned for magic
    scores = {f"r{i}": s for i, s in enumerate([0.1, 0.2, 0.3, 0.6, 0.8])}
    scores |= {f"m{i}": s for i, s in enumerate([0.8, 0.7, 0.6, 0.3, 0.1])}
    m = metrics(corpus, scores)
    assert m["n"] == 10
    # tied 1-ex prices share a midrank while scores differ -> just under 1.0
    assert m["rho"]["Rare"]["rho"] > 0.95
    assert m["rho"]["Magic"]["rho"] < 0
    assert m["rho"]["Jewels"]["n"] == 5
    assert m["bands"]["S"]["n"] == 2            # 0.8 scores -> S band
    assert m["bands"]["S"]["median_ex"] in (1.0, 200.0)


def test_disagreements_pick_both_tails():
    corpus = [_ci("good", 200), _ci("trap", 1), _ci("sleeper", 100)]
    scores = {"good": 0.9, "trap": 0.85, "sleeper": 0.2}
    d = disagreements(corpus, scores)
    assert [e["name"] for e in d["high_score_cheap"]] == ["trap"]
    assert [e["name"] for e in d["low_score_expensive"]] == ["sleeper"]


def test_apply_knobs_validates_names():
    from stasher.evaluate.archetype_model import ArchetypeSet
    aset = ArchetypeSet()
    apply_knobs(aset, {"magic_completion": 0.75})
    assert aset.scoring.magic_completion == 0.75
    with pytest.raises(SystemExit, match="unknown scoring knob"):
        apply_knobs(aset, {"magic_compleshun": 0.75})


def test_load_corpus_filters_unusable(tmp_path):
    db = tmp_path / "s.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE items (hash TEXT PRIMARY KEY, item_name TEXT, type_line TEXT,
                            rarity TEXT, raw_json TEXT);
        CREATE TABLE price_cache (plan_sig TEXT PRIMARY KEY, estimate TEXT);
        CREATE TABLE price_item (item_hash TEXT PRIMARY KEY, plan_sig TEXT);
    """)
    def add(h, est):
        conn.execute("INSERT INTO items VALUES (?,?,?,?,?)",
                     (h, h, h, "Rare", json.dumps({"item": {"frameType": 2}})))
        conn.execute("INSERT INTO price_cache VALUES (?,?)", (h, json.dumps(est)))
        conn.execute("INSERT INTO price_item VALUES (?,?)", (h, h))
    add("ok", {"value": 5, "currency": "exalted", "confidence": 0.9, "is_floor": False})
    add("divine", {"value": 2, "currency": "divine", "confidence": 0.9, "is_floor": False})
    add("floor", {"value": 5, "currency": "exalted", "confidence": 0.9, "is_floor": True})
    add("lowconf", {"value": 5, "currency": "exalted", "confidence": 0.3, "is_floor": False})
    add("alien", {"value": 5, "currency": "mirror_shard", "confidence": 0.9, "is_floor": False})
    conn.commit(); conn.close()
    corpus = load_corpus(db)
    by = {c.hash: c for c in corpus}
    assert set(by) == {"ok", "divine"}
    assert by["ok"].ex == 5.0
    assert by["divine"].ex > 100          # converted through the rate table

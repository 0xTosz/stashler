#!/usr/bin/env python
"""Validate evaluation scores against the in-app price-check corpus (offline, no API).

The corpus is every item the user has manually price-checked: `price_item` x
`price_cache` x `items` in the stasher DB (it grows with every appraisal). Each item is
re-scored IN-PROCESS against an arbitrary archetype set + Scoring-knob overrides, then
compared to its appraised market value — so model changes and knob sweeps are judged by
the market instead of by feel (EVAL_IMPROVEMENT_PLAN.md Phase 0; baseline numbers in
archetype_miner/RESEARCH_LOG.md §7).

    python tools/validate_eval.py                       # score the shipped/installed set
    python tools/validate_eval.py --save-baseline       # snapshot for later deltas
    python tools/validate_eval.py --knob magic_completion=0.75 --knob magic_solo=0.45

Metrics: tie-aware Spearman rho of score vs log(price) (overall / Magic / Rare / Jewels),
median + p90 appraised price per predicted tier band, and the top disagreement lists.
Corpus filter: non-floor estimates, confidence >= MIN_CONF, known currency. NOTE: most of
the archive is random drops saturating at the ~1 ex price floor, so overall rho is
structurally depressed — watch the segments and the band medians, not just the headline.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from stasher.evaluate.archetype_model import ArchetypeSet, Scoring, value_to_tier
from stasher.evaluate.checks.archetype_set import model_item
from stasher.evaluate.itemdata import item_class
from stasher.evaluate.rules import ARCHETYPE_SET_FILENAME
from stasher.pricing.price import load_rates

MIN_CONF = 0.5
TIER_ORDER = "SABCD"


@dataclass
class CorpusItem:
    hash: str
    name: str
    rarity: str | None
    cls: str | None
    item: dict           # the /fetch item sub-dict
    ex: float            # appraised value, base units (exalted)
    conf: float          # the appraisal's own confidence


def load_corpus(db_path: str | Path, min_conf: float = MIN_CONF) -> list[CorpusItem]:
    """The price-checked items with usable appraisals (non-floor, confident, known
    currency), each carrying its raw item dict for in-process re-scoring."""
    rates = load_rates()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT pi.item_hash, pc.estimate, i.item_name, i.type_line, i.rarity, "
            "i.raw_json FROM price_item pi "
            "JOIN price_cache pc ON pc.plan_sig = pi.plan_sig "
            "JOIN items i ON i.hash = pi.item_hash").fetchall()
    finally:
        conn.close()
    out: list[CorpusItem] = []
    for r in rows:
        est = json.loads(r["estimate"])
        rate = rates.get(est.get("currency"))
        conf = float(est.get("confidence") or 0.0)
        value = float(est.get("value") or 0.0)
        if est.get("is_floor") or rate is None or conf < min_conf or value <= 0:
            continue
        item = json.loads(r["raw_json"]).get("item") or {}
        out.append(CorpusItem(
            hash=r["item_hash"], name=(r["item_name"] or r["type_line"] or "?"),
            rarity=r["rarity"], cls=item_class(item), item=item,
            ex=value * rate, conf=conf))
    return out


def rescore(corpus: list[CorpusItem], aset: ArchetypeSet) -> dict[str, float]:
    """``item hash -> overall archetype score`` under the given set (+ its scoring)."""
    return {c.hash: aset.score_item(model_item(c.item))["overall"] for c in corpus}


def _midranks(values: list[float]) -> list[float]:
    """Average ranks with ties shared (proper Spearman input — appraised prices pile up
    at the 1-ex floor, so tie handling materially changes rho)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 5:
        return None
    rx, ry = _midranks(xs), _midranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return cov / (vx * vy) if vx and vy else None


def metrics(corpus: list[CorpusItem], scores: dict[str, float]) -> dict:
    """The full report dict (JSON-able; doubles as the baseline snapshot)."""
    def seg_rho(pred):
        pairs = [(scores[c.hash], math.log(c.ex)) for c in corpus if pred(c)]
        rho = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        return {"n": len(pairs), "rho": round(rho, 3) if rho is not None else None}

    bands: dict[str, list[float]] = {}
    for c in corpus:
        bands.setdefault(value_to_tier(scores[c.hash]), []).append(c.ex)
    band_stats = {}
    for t in TIER_ORDER:
        v = sorted(bands.get(t, []))
        if v:
            band_stats[t] = {"n": len(v), "median_ex": round(v[len(v) // 2], 1),
                             "p90_ex": round(v[int(len(v) * 0.9)], 1)}
    return {
        "n": len(corpus),
        "rho": {
            "overall": seg_rho(lambda c: True),
            "Magic": seg_rho(lambda c: c.rarity == "Magic"),
            "Rare": seg_rho(lambda c: c.rarity == "Rare"),
            "Jewels": seg_rho(lambda c: c.cls == "Jewels"),
        },
        "bands": band_stats,
    }


def disagreements(corpus: list[CorpusItem], scores: dict[str, float],
                  top: int = 10) -> dict[str, list[dict]]:
    def fmt(c):
        return {"name": c.name[:40], "rarity": c.rarity, "class": c.cls,
                "score": round(scores[c.hash], 3), "ex": round(c.ex, 1),
                "appraisal_conf": round(c.conf, 2)}
    hi_cheap = sorted((c for c in corpus if scores[c.hash] >= 0.7 and c.ex <= 2),
                      key=lambda c: -scores[c.hash])[:top]
    lo_rich = sorted((c for c in corpus if scores[c.hash] <= 0.4 and c.ex >= 20),
                     key=lambda c: -c.ex)[:top]
    return {"high_score_cheap": [fmt(c) for c in hi_cheap],
            "low_score_expensive": [fmt(c) for c in lo_rich]}


def apply_knobs(aset: ArchetypeSet, knobs: dict[str, float]) -> None:
    base = aset.scoring.to_dict()
    for k in knobs:
        if k not in base:
            raise SystemExit(f"unknown scoring knob: {k} (have: {', '.join(sorted(base))})")
    base.update(knobs)
    aset.scoring = Scoring.from_dict(base)


def _print_report(m: dict, baseline: dict | None) -> None:
    def delta(cur, prev):
        if prev is None or cur is None:
            return ""
        d = cur - prev
        return f"  ({'+' if d >= 0 else ''}{d:.3f} vs baseline)"

    print(f"corpus: {m['n']} usable price-checked items")
    for seg, st in m["rho"].items():
        prev = (baseline or {}).get("rho", {}).get(seg, {}).get("rho")
        rho = st["rho"]
        print(f"  rho {seg:<8} n={st['n']:<4} "
              f"{rho if rho is not None else 'n/a'}{delta(rho, prev)}")
    print("median / p90 appraised ex by predicted tier:")
    for t in TIER_ORDER:
        st = m["bands"].get(t)
        if st:
            print(f"  {t}: n={st['n']:<4} median={st['median_ex']:>8}  p90={st['p90_ex']:>9}")


def main(argv: list[str] | None = None) -> int:
    data_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "Stashler"
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=str(data_dir / "stasher.db"))
    p.add_argument("--set", default=str(data_dir / ARCHETYPE_SET_FILENAME),
                   help="Archetype set YAML to score with (default: the installed set)")
    p.add_argument("--knob", action="append", default=[], metavar="NAME=VALUE",
                   help="Scoring override, repeatable (e.g. magic_completion=0.75)")
    p.add_argument("--baseline", default=str(data_dir / "eval_baseline.json"))
    p.add_argument("--save-baseline", action="store_true",
                   help="Write this run's metrics as the new baseline")
    p.add_argument("--top", type=int, default=10, help="Disagreement list length (0 = off)")
    args = p.parse_args(argv)

    corpus = load_corpus(args.db)
    if not corpus:
        print("no usable price checks in the DB — appraise some items first", file=sys.stderr)
        return 1
    aset = ArchetypeSet.load(args.set)
    knobs = {}
    for kv in args.knob:
        k, _, v = kv.partition("=")
        knobs[k.strip()] = float(v)
    if knobs:
        apply_knobs(aset, knobs)
        print(f"knob overrides: {knobs}")
    scores = rescore(corpus, aset)
    m = metrics(corpus, scores)

    baseline = None
    bp = Path(args.baseline)
    if bp.exists() and not args.save_baseline:
        baseline = json.loads(bp.read_text(encoding="utf-8"))
    _print_report(m, baseline)

    if args.top:
        d = disagreements(corpus, scores, args.top)
        print("\nhigh score but cheap (<=2 ex):")
        for e in d["high_score_cheap"]:
            print(f"  {e['score']:.2f} {e['ex']:>7}ex {e['rarity']:<6} "
                  f"{(e['class'] or '?'):<13} {e['name']}")
        print("low score but expensive (>=20 ex):")
        for e in d["low_score_expensive"]:
            print(f"  {e['score']:.2f} {e['ex']:>7}ex conf={e['appraisal_conf']} "
                  f"{e['rarity']:<6} {(e['class'] or '?'):<13} {e['name']}")

    if args.save_baseline:
        bp.write_text(json.dumps(m, indent=1), encoding="utf-8")
        print(f"\nbaseline saved -> {bp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

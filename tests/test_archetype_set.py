"""Tests for the data-driven archetype_set checker + its rules wiring (toggle like item_filter)."""

import pytest

from stasher.evaluate import evaluate_item
from stasher.evaluate.affix_norm import mod_key
from stasher.evaluate.archetype_model import ArchetypeSet
from stasher.evaluate.checks.archetype_set import ArchetypeSetChecker, model_item
from stasher.evaluate.evaluator import Evaluator
from stasher.evaluate.rules import archetype_set_default_path, archetype_set_path, load_checkers
from stasher.store import Store

MS_KEY = mod_key("35% increased Movement Speed")
LIFE_KEY = mod_key("+95 to maximum Life")

ASET_YAML = f"""
meta: {{league: test}}
mod_families: {{}}
base_families: {{}}
archetypes:
  - id: boots-msl
    name: Movement + Life
    item_class: Boots
    rarity: [Rare]
    requires:
      - phrase: increased Movement Speed
        mod: "{MS_KEY}"
        mag: {{weight: 0.9, bands: [{{tier: T1, min: 33}}, {{tier: T2, min: 28}}, {{tier: T3, min: 20}}]}}
      - phrase: to maximum Life
        mod: "{LIFE_KEY}"
        mag: {{weight: 0.7, bands: [{{tier: T1, min: 100}}, {{tier: T2, min: 70}}]}}
    bases: {{mode: graded, grades: {{"Stripped Boots": "A", "Goathide Boots": "C"}}}}
    value: {{score: 0.8, tier: S}}
    relations: {{subset_of: [], superset_of: []}}
"""


def boots_item(base="Stripped Boots", ms="35% increased Movement Speed", life="+95 to maximum Life"):
    return {"frameType": 2, "extended": {"baseClass": "Boots"},
            "baseType": base, "typeLine": base, "explicitMods": [ms, life]}


def test_model_item_keys_align_with_miner_normalization():
    mi = model_item(boots_item())
    assert mi["class"] == "Boots" and mi["rarity"] == "Rare" and mi["base"] == "Stripped Boots"
    assert mi["mods"][MS_KEY] == 35.0 and mi["mods"][LIFE_KEY] == 95.0


def test_model_item_is_per_affix_no_aggregation():
    item = {"frameType": 2, "extended": {"baseClass": "Body Armours"},
            "baseType": "Sacred Plate", "typeLine": "Sacred Plate",
            "properties": [{"name": "Energy Shield", "values": [["420", 1]]}],
            "explicitMods": ["+95 to maximum Life", "+40% to Fire Resistance"]}
    mi = model_item(item)
    assert "stats" not in mi                       # no stat aggregation any more
    assert mi["defence"] == ("energy_shield",)     # presence only (segment gate)
    assert mi["mods"][mod_key("+40% to Fire Resistance")] == 40.0


_FUNGIBLE_YAML = """
meta: {league: test}
mod_families:
  ele_res:
    name: Elemental Resistance
    members: ["#% to fire resistance", "#% to cold resistance", "#% to lightning resistance"]
archetypes:
  - id: ring-res-life
    name: double Res + Life
    item_class: Rings
    rarity: [Rare]
    requires:
      - phrase: Elemental Resistance
        any_of: ele_res
        min: 2
        mag: {weight: 0.6, bands: [{tier: T1, min: 38}, {tier: T2, min: 28}]}
      - phrase: to maximum Life
        mod: "# to maximum life"
        mag: {weight: 0.7, bands: [{tier: T1, min: 90}]}
    bases: {mode: baseless}
    value: {score: 0.7, tier: A}
"""


def _ring(*res_and_life):
    return {"frameType": 2, "extended": {"baseClass": "Rings"}, "baseType": "Gold Ring",
            "explicitMods": list(res_and_life)}


def test_elemental_res_family_matches_any_elements():
    chk = ArchetypeSetChecker(ArchetypeSet.loads(_FUNGIBLE_YAML))
    fc = _ring("+40% to Fire Resistance", "+38% to Cold Resistance", "+95 to maximum Life")
    cl = _ring("+40% to Cold Resistance", "+38% to Lightning Resistance", "+95 to maximum Life")
    assert chk.check(fc) and chk.check(cl)                      # both match the one fungible rule
    assert chk.check(fc)[0].rule_name == chk.check(cl)[0].rule_name


def test_checker_surfaces_multiple_reasons_with_coverage():
    chk = ArchetypeSetChecker(ArchetypeSet.loads(ASET_YAML))
    res = chk.check(boots_item())
    assert res and "·" in res[0].explanation
    # the coverage tag is present (full match here)
    assert res[0].explanation.rstrip().endswith("full")


def test_checker_flags_and_grades():
    chk = ArchetypeSetChecker(ArchetypeSet.loads(ASET_YAML))
    res = chk.check(boots_item())
    assert len(res) == 1
    assert res[0].rule_name == "archetype_set:boots-msl"
    assert res[0].explanation.startswith("Movement + Life · ")

    # a worse base scores lower than the A base (graded), same mods
    import re
    val = lambda r: float(re.search(r"\(([\d.]+)\)", r[0].explanation).group(1))
    assert val(chk.check(boots_item(base="Stripped Boots"))) > \
        val(chk.check(boots_item(base="Goathide Boots")))


def test_checker_no_match_when_a_required_mod_missing():
    chk = ArchetypeSetChecker(ArchetypeSet.loads(ASET_YAML))
    only_ms = {"frameType": 2, "extended": {"baseClass": "Boots"}, "baseType": "Stripped Boots",
               "explicitMods": ["35% increased Movement Speed"]}
    assert chk.check(only_ms) == []


_RULES = "[archetype_set]\nenabled = true\n"


def test_rules_wiring_enables_checker_and_affects_hash(tmp_path):
    (tmp_path / "rules.toml").write_text(_RULES, encoding="utf-8")
    archetype_set_path(tmp_path).write_text(ASET_YAML, encoding="utf-8")
    checkers, h1 = load_checkers(data_dir=str(tmp_path), path=str(tmp_path / "rules.toml"))
    assert "archetype_set" in [c.name for c in checkers]
    assert evaluate_item(boots_item(), checkers).flagged

    # editing the set file changes the rules hash (forces re-eval)
    archetype_set_path(tmp_path).write_text(ASET_YAML + "\n# tweak\n", encoding="utf-8")
    _checkers, h2 = load_checkers(data_dir=str(tmp_path), path=str(tmp_path / "rules.toml"))
    assert h1 != h2

    # disabling drops the checker
    (tmp_path / "rules.toml").write_text("[archetype_set]\nenabled = false\n", encoding="utf-8")
    checkers3, _ = load_checkers(data_dir=str(tmp_path), path=str(tmp_path / "rules.toml"))
    assert "archetype_set" not in [c.name for c in checkers3]


def test_upload_save_restore_and_enable(tmp_path):
    (tmp_path / "rules.toml").write_text(_RULES, encoding="utf-8")
    store = Store(str(tmp_path / "t.db"))
    ev = Evaluator(store, rules_path=str(tmp_path / "rules.toml"))

    # upload installs working + pristine default copies, and activates the checker
    ev.upload_archetype_set(ASET_YAML)
    assert archetype_set_path(tmp_path).exists() and archetype_set_default_path(tmp_path).exists()
    assert "archetype_set" in [c.name for c in ev.checkers]
    assert ev.archetype_set().archetypes[0].value.score == 0.8

    # edit via the model → save → persists
    aset = ev.archetype_set()
    aset.archetypes[0].value.score = 0.123
    aset.archetypes[0].enabled = False
    ev.save_archetype_set(aset)
    reloaded = ev.archetype_set()
    assert reloaded.archetypes[0].value.score == 0.123 and reloaded.archetypes[0].enabled is False

    # restore reverts the working copy to the pristine default
    assert ev.restore_archetype_set_defaults() is True
    assert ev.archetype_set().archetypes[0].value.score == 0.8

    with pytest.raises(ValueError):
        ev.upload_archetype_set("foo: [unclosed")

    # enable toggle drops / re-adds the checker
    ev.set_archetype_set_enabled(False)
    assert ev.archetype_set_is_enabled() is False
    assert "archetype_set" not in [c.name for c in ev.checkers]
    ev.set_archetype_set_enabled(True)
    assert ev.archetype_set_is_enabled() is True
    store.close()


def test_evaluate_item_carries_score():
    chk = ArchetypeSetChecker(ArchetypeSet.loads(ASET_YAML))
    ev = evaluate_item(boots_item(), [chk])
    assert ev.flagged and ev.score is not None and 0 < ev.score <= 1
    no = evaluate_item({"frameType": 2, "extended": {"baseClass": "Gloves"},
                        "baseType": "X", "explicitMods": []}, [chk])
    assert no.score is None


def test_explain_breakdown_has_per_affix_grading():
    chk = ArchetypeSetChecker(ArchetypeSet.loads(ASET_YAML))
    b = chk.explain(boots_item())
    assert b["score"] is not None and b["matches"]
    m = b["matches"][0]
    assert m["name"] == "Movement + Life" and "base_factor" in m and "archetype_score" in m
    ms = next(r for r in m["requires"] if r.get("key") == MS_KEY)
    assert ms["magnitude"] == 35.0 and ms["weight"] == 0.9 and ms["tier_value"] == 1.0
    assert b["item"]["mods"][MS_KEY] == 35.0   # debug payload present


def test_store_score_roundtrip_and_sort(tmp_path):
    from stasher.evaluate.engine import Evaluation as Ev
    from stasher.store import ItemRecord, Store

    store = Store(str(tmp_path / "t.db"))
    for h, sc in [("h-lo", 0.30), ("h-hi", 0.80)]:
        store.insert_item(ItemRecord(hash=h, account="", listed_at=None, price_amount=None,
                                     price_currency=None, price_type=None, item_name=None,
                                     type_line="x", rarity="Rare", whisper=None, league=None,
                                     raw_json="{}"))
        store.upsert_evaluation(h, Ev(flagged=True, reasons=["r"], score=sc), "rh")
    rows = store.queue_items(show_all=False, sort="score")
    assert [r["hash"] for r in rows] == ["h-hi", "h-lo"]
    assert rows[0]["score"] == 0.80
    store.close()

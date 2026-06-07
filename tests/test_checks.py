"""Tests for the local item-evaluation checker chain."""

import base64
import json

from stasher.evaluate import evaluate_item, load_checkers
from stasher.evaluate.checks import item_filter as flt
from stasher.evaluate.checks import regex_check, unique_roll
from stasher.evaluate.evaluator import Evaluator
from stasher.evaluate.itemdata import (
    clean_mod_text,
    explicit_roll_percents,
    item_class,
    stash_regex,
)
from stasher.store import Store


def _icon(art_path: str) -> str:
    """Build an icon URL whose base64 token wraps the given art-path, like GGG's."""
    payload = f'[25,14,{{"f":"{art_path}","w":2,"h":4,"scale":1,"realm":"poe2"}}]'
    token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"https://web.poecdn.com/gen/image/{token}/abc/x.png"


# --- fixtures -----------------------------------------------------------

def rare_crossbow(ilvl=64, mods=None):
    return {
        "frameType": 2,
        "name": "Agony Core",
        "typeLine": "Cannonade Crossbow",
        "baseType": "Cannonade Crossbow",
        "ilvl": ilvl,
        "explicitMods": mods if mods is not None else [
            "53% increased [Physical] Damage",
            "Adds 8 to 11 [Physical|Physical] Damage",
        ],
    }


def unique_maul(phys_line, aps_line):
    """A unique whose two rollable mods use the magnitude ranges below."""
    return {
        "frameType": 3,
        "name": "Trephina",
        "typeLine": "Forge Maul",
        "baseType": "Forge Maul",
        "ilvl": 79,
        "explicitMods": [phys_line, aps_line],
        "extended": {
            "mods": {
                "explicit": [
                    {"magnitudes": [
                        {"hash": "explicit.stat_A", "min": "12", "max": "15"},
                        {"hash": "explicit.stat_A", "min": "22", "max": "25"},
                    ]},
                    {"magnitudes": [
                        {"hash": "explicit.stat_B", "min": "10", "max": "15"},
                    ]},
                ]
            },
            "hashes": {"explicit": [
                ["explicit.stat_A", [0]],
                ["explicit.stat_B", [1]],
            ]},
        },
    }


# --- itemdata helpers ---------------------------------------------------

def test_clean_mod_text_strips_markup():
    assert clean_mod_text("53% increased [Physical] Damage") == "53% increased Physical Damage"
    assert clean_mod_text("[Critical|Critical Hit] Chance") == "Critical Hit Chance"
    assert clean_mod_text("Adds 8 to 11 [Physical|Physical] Damage") == "Adds 8 to 11 Physical Damage"


def test_explicit_roll_percents_pairs_values_to_ranges():
    # Adds 15 to 25 -> A maxes both (1.0, 1.0); 15% APS -> B maxes (1.0).
    pct = explicit_roll_percents(unique_maul("Adds 15 to 25 Physical Damage", "15% increased Attack Speed"))
    assert pct == [1.0, 1.0, 1.0]
    # Low rolls -> all at the floor.
    pct_low = explicit_roll_percents(unique_maul("Adds 12 to 22 Physical Damage", "10% increased Attack Speed"))
    assert pct_low == [0.0, 0.0, 0.0]


# --- regex checker ------------------------------------------------------

def test_regex_matches_affix_and_explains():
    checker = regex_check.build([
        {"name": "Big life", "pattern": r"\+\d{2,} to maximum Life", "targets": ["affixes"]}
    ])
    hit = checker.check(rare_crossbow(mods=["+85 to maximum Life", "+12 to Strength"]))
    assert len(hit) == 1
    assert hit[0].rule_name == "Big life"
    assert "+85 to maximum Life" in hit[0].explanation

    miss = checker.check(rare_crossbow(mods=["+12 to Strength"]))
    assert miss == []


def test_regex_matches_against_cleaned_markup():
    checker = regex_check.build([
        {"name": "Phys dmg", "pattern": "increased Physical Damage", "targets": ["affixes"]}
    ])
    assert checker.check(rare_crossbow()) != []


def test_regex_target_base_and_name():
    base_rule = regex_check.build([{"name": "Crossbow", "pattern": "Crossbow", "targets": ["base"]}])
    assert base_rule.check(rare_crossbow()) != []
    name_rule = regex_check.build([{"name": "Agony", "pattern": "Agony", "targets": ["name"]}])
    assert name_rule.check(rare_crossbow()) != []


# --- unique roll checker ------------------------------------------------

def test_unique_high_roll_fires_only_when_high():
    checker = unique_roll.build([{"name": "Perfect", "min_percent": 90, "aggregate": "avg"}])
    assert checker.check(unique_maul("Adds 15 to 25 Physical Damage", "15% increased Attack Speed")) != []
    assert checker.check(unique_maul("Adds 12 to 22 Physical Damage", "10% increased Attack Speed")) == []


def test_unique_roll_ignores_non_uniques():
    checker = unique_roll.build([{"name": "Perfect", "min_percent": 1, "aggregate": "avg"}])
    assert checker.check(rare_crossbow()) == []


# --- item filter checker ------------------------------------------------

def _write_filter(tmp_path, text):
    p = tmp_path / "stasher.filter"
    p.write_text(text, encoding="utf-8")
    return p


def test_item_filter_basetype_and_ilvl(tmp_path):
    path = _write_filter(tmp_path, (
        "Show  # good crossbow\n"
        '    BaseType "Cannonade Crossbow"\n'
        "    ItemLevel >= 80\n"
    ))
    checker = flt.build_from_file(path)
    assert checker.check(rare_crossbow(ilvl=82)) != []
    assert checker.check(rare_crossbow(ilvl=70)) == []  # ilvl gate fails


def test_item_filter_hide_blocks_first(tmp_path):
    path = _write_filter(tmp_path, (
        "Hide\n"
        "    ItemLevel < 80\n"
        "Show\n"
        '    BaseType "Cannonade Crossbow"\n'
    ))
    checker = flt.build_from_file(path)
    # ilvl 70 hits the Hide block first -> not flagged, even though Show would match.
    assert checker.check(rare_crossbow(ilvl=70)) == []
    assert checker.check(rare_crossbow(ilvl=82)) != []


def test_item_filter_rarity_ordinal(tmp_path):
    path = _write_filter(tmp_path, "Show\n    Rarity <= Magic\n")
    checker = flt.build_from_file(path)
    magic = dict(rare_crossbow(), frameType=1)
    assert checker.check(magic) != []
    assert checker.check(rare_crossbow()) == []  # Rare > Magic


def test_item_filter_sockets(tmp_path):
    path = _write_filter(tmp_path, "Show\n    Sockets >= 2\n")
    checker = flt.build_from_file(path)
    two = dict(rare_crossbow(), sockets=[{"type": "rune"}, {"type": "rune"}])
    one = dict(rare_crossbow(), sockets=[{"type": "rune"}])
    assert checker.check(two) != []
    assert checker.check(one) == []  # only one socket
    assert checker.check(rare_crossbow()) == []  # no sockets field -> 0


def test_item_filter_state_flags(tmp_path):
    path = _write_filter(tmp_path, "Show\n    Corrupted True\n    Mirrored False\n")
    checker = flt.build_from_file(path)
    corrupt = dict(rare_crossbow(), corrupted=True)
    assert checker.check(corrupt) != []
    assert checker.check(rare_crossbow()) == []  # not corrupted
    assert checker.check(dict(corrupt, duplicated=True)) == []  # mirrored excluded


def test_item_filter_has_explicit_mod_by_name(tmp_path):
    # Affix names live in extended.mods.explicit[].name (never in rendered text).
    item = dict(rare_crossbow(), extended={"mods": {"explicit": [
        {"name": "Hellion's"}, {"name": "of the Sharpshooter"},
    ]}})
    path = _write_filter(tmp_path, 'Show\n    HasExplicitMod >=1 "Hellion\'s" "Countess\'"\n')
    assert flt.build_from_file(path).check(item) != []
    miss = _write_filter(tmp_path, 'Show\n    HasExplicitMod >=1 "Countess\'"\n')
    assert flt.build_from_file(miss).check(item) == []
    # Count gate: needs two matching mods.
    two = _write_filter(tmp_path, 'Show\n    HasExplicitMod >=2 "Hellion\'s" "of the Sharpshooter"\n')
    assert flt.build_from_file(two).check(item) != []
    three = _write_filter(tmp_path, 'Show\n    HasExplicitMod >=3 "Hellion\'s" "of the Sharpshooter"\n')
    assert flt.build_from_file(three).check(item) == []


def test_item_filter_has_explicit_mod_by_text(tmp_path):
    # Falls back to rendered stat text when matching a fragment, not an affix name.
    path = _write_filter(tmp_path, 'Show\n    HasExplicitMod "increased Physical Damage"\n')
    assert flt.build_from_file(path).check(rare_crossbow()) != []


def test_item_class_derived_from_icon():
    # Trade /fetch data has no class; it's decoded from the icon art-path.
    cases = {
        "2DItems/Weapons/TwoHandWeapons/Bows/Basetypes/Bow08": "Bows",
        "2DItems/Weapons/OneHandWeapons/Wands/Basetypes/Wand03": "Wands",
        "2DItems/Weapons/TwoHandWeapons/WarStaves/Warstaff02": "Quarterstaves",
        "2DItems/Weapons/OneHandWeapons/Scepters/Basetypes/Sceptre04": "Sceptres",
        "2DItems/Armours/BodyArmours/Basetypes/BodyStr1": "Body Armours",
        "2DItems/Rings/Basetypes/PrismaticRing": "Rings",
        "2DItems/Offhand/Shields/Basetypes/Shield1": "Shields",
    }
    for art, expected in cases.items():
        assert item_class({"icon": _icon(art)}) == expected
    # An explicit class (if the API ever provides one) wins over the icon.
    assert item_class({"class": "Foci", "icon": _icon("2DItems/Rings/x")}) == "Foci"
    # Undeterminable -> None (e.g. currency art, or no icon).
    assert item_class({"icon": _icon("2DItems/Currency/Whatever")}) is None
    assert item_class({}) is None


def test_stash_regex_prefers_name_then_typeline_then_base():
    # Rare/unique: the generated name (most distinctive single line).
    assert stash_regex(rare_crossbow()) == "Agony Core"
    # Magic (no name): the full magic type line.
    magic = {"frameType": 1, "name": "", "typeLine": "Minister's Omen Sceptre of the Prodigy",
             "baseType": "Omen Sceptre"}
    assert stash_regex(magic) == "Minister's Omen Sceptre of the Prodigy"
    # Normal (no name, type line == base): the base type.
    assert stash_regex({"frameType": 0, "baseType": "Omen Sceptre"}) == "Omen Sceptre"
    assert stash_regex({}) == ""


def test_stash_regex_escapes_re2_metacharacters_and_caps_length():
    # Metacharacters in the anchor text are escaped so the search is a literal match.
    out = stash_regex({"name": "Doom (of the Abyss) +1"})
    assert out == r"Doom \(of the Abyss\) \+1"
    # Never exceeds the search-box limit.
    assert len(stash_regex({"name": "x" * 400})) == 250


def test_item_filter_class_matches_via_icon(tmp_path):
    # A Class-gated block must match an item whose only class signal is the icon.
    bow = dict(
        rare_crossbow(),
        baseType="Warmonger Bow",
        typeLine="Warmonger Bow",
        icon=_icon("2DItems/Weapons/TwoHandWeapons/Bows/Basetypes/Bow08"),
    )
    path = _write_filter(tmp_path, 'Show\n    Class == "Bows"\n')
    assert flt.build_from_file(path).check(bow) != []
    # And a wrong class does not match.
    miss = _write_filter(tmp_path, 'Show\n    Class == "Wands"\n')
    assert flt.build_from_file(miss).check(bow) == []


# --- engine + persistence (with a self-contained rules file) ------------

_TEST_RULES = (
    '[[regex]]\n'
    'name = "Any life"\n'
    'pattern = "\\\\+\\\\d+ to maximum Life"\n'
    'targets = ["affixes"]\n'
)


def _rules_file(tmp_path):
    p = tmp_path / "rules.toml"
    p.write_text(_TEST_RULES, encoding="utf-8")
    return str(p)


def test_packaged_default_rules_load():
    # The packaged fallback must always load and build checkers.
    from stasher.evaluate.rules import _DEFAULT_RULES
    checkers, rules_hash = load_checkers(str(_DEFAULT_RULES))
    assert checkers
    assert len(rules_hash) == 16


def test_evaluate_item_aggregates_reasons(tmp_path):
    checkers, _ = load_checkers(_rules_file(tmp_path))
    ev = evaluate_item(rare_crossbow(mods=["+85 to maximum Life"]), checkers)
    assert ev.flagged
    assert ev.reasons and any("Life" in r for r in ev.reasons)

    junk = evaluate_item(rare_crossbow(mods=["+1 to Strength"]), checkers)
    assert not junk.flagged


def test_evaluator_persists_and_queue_roundtrip(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    from stasher.store import ItemRecord

    good = {"id": "h1", "listing": {}, "item": rare_crossbow(mods=["+85 to maximum Life"])}
    bad = {"id": "h2", "listing": {}, "item": rare_crossbow(mods=["+1 to Strength"])}
    for entry in (good, bad):
        store.insert_item(ItemRecord(
            hash=entry["id"], account="", listed_at=None, price_amount=None,
            price_currency=None, price_type=None, item_name=None, type_line="x",
            rarity="Rare", whisper=None, league=None, raw_json=json.dumps(entry),
        ))

    ev = Evaluator(store, rules_path=_rules_file(tmp_path))
    summary = ev.reevaluate_all(force=True)
    assert summary["evaluated"] == 2
    assert summary["flagged"] == 1

    queue = store.queue_items()
    assert len(queue) == 1
    assert queue[0]["hash"] == "h1"
    assert store.count_unseen() == 1

    store.mark_all_seen()
    assert store.count_unseen() == 0
    store.close()


# --- rule editing (Settings UI) -----------------------------------------

def test_save_rules_validates_and_reloads(tmp_path):
    rules = tmp_path / "rules.toml"
    rules.write_text(_TEST_RULES, encoding="utf-8")
    store = Store(str(tmp_path / "t.db"))
    ev = Evaluator(store, rules_path=str(rules))

    new = (
        '[[regex]]\nname = "MS"\npattern = "increased Movement Speed"\ntargets = ["affixes"]\n'
        '[item_filter]\nenabled = true\n'
    )
    ev.save_rules(new, "Show\n    Rarity Magic\n    ItemLevel >= 84\n")

    assert "Movement Speed" in rules.read_text(encoding="utf-8")
    assert (tmp_path / "stasher.filter").exists()  # single app-managed filter file
    # The reloaded checkers reflect the new rule.
    item = rare_crossbow(mods=["25% increased Movement Speed"])
    assert evaluate_item(item, ev.checkers).flagged


def test_save_rules_handles_crlf_without_corrupting_file(tmp_path):
    # Browsers submit textareas as CRLF; the file must save+reload cleanly and not
    # accumulate \r\r\n on disk (the cause of "invalid character '\r'" on Windows).
    rules = tmp_path / "rules.toml"
    rules.write_text(_TEST_RULES, encoding="utf-8")
    store = Store(str(tmp_path / "t.db"))
    ev = Evaluator(store, rules_path=str(rules))

    crlf_rules = "[[regex]]\r\nname = \"MS\"\r\npattern = \"x\"\r\ntargets = [\"affixes\"]\r\n"
    crlf_filter = "Show\r\n    Rarity Magic\r\n    ItemLevel >= 84\r\n"
    ev.save_rules(crlf_rules, crlf_filter)  # must not raise

    raw = rules.read_bytes()
    assert b"\r" not in raw  # normalized to LF on disk
    assert b"\r" not in (tmp_path / "stasher.filter").read_bytes()
    # A second save (re-submitting the now-saved text as CRLF again) still works.
    ev.save_rules(crlf_rules, crlf_filter)
    assert b"\r" not in rules.read_bytes()


def test_parse_rules_text_tolerates_doubled_cr():
    from stasher.evaluate.rules import parse_rules_text
    # A file already corrupted with \r\r\n must still parse (self-heal on next save).
    assert parse_rules_text("# c\r\r\n[item_filter]\r\r\nenabled = true\r\r\n") == {
        "item_filter": {"enabled": True}
    }


def test_seed_user_rules_creates_starter_rules_and_filter(tmp_path):
    from stasher.evaluate.rules import seed_user_rules

    target = tmp_path / "rules.toml"
    seed_user_rules(path=str(target), data_dir=str(tmp_path))
    assert target.exists()                       # starter rules copied
    assert (tmp_path / "stasher.filter").exists()  # example filter copied
    assert "item_filter" in target.read_text(encoding="utf-8")

    # Idempotent: a second run must not clobber edits.
    target.write_text("[[regex]]\nname = 'mine'\npattern = 'x'\n", encoding="utf-8")
    seed_user_rules(path=str(target), data_dir=str(tmp_path))
    assert "mine" in target.read_text(encoding="utf-8")


def test_save_rules_rejects_bad_pattern_without_writing(tmp_path):
    import pytest

    rules = tmp_path / "rules.toml"
    rules.write_text(_TEST_RULES, encoding="utf-8")
    store = Store(str(tmp_path / "t.db"))
    ev = Evaluator(store, rules_path=str(rules))
    before = rules.read_text(encoding="utf-8")

    with pytest.raises(ValueError):
        ev.save_rules('[[regex]]\nname = "bad"\npattern = "("\ntargets = ["affixes"]\n', "")
    assert rules.read_text(encoding="utf-8") == before  # unchanged

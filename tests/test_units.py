"""Display-unit registry + per-person preference store (issue #136).

Unit-level only: the two read paths that honour a preference are exercised in
``test_render.py`` (summary) and ``test_query.py`` (trends), and the CLI verbs in
``test_persons.py``.
"""

import shutil
from pathlib import Path

import pytest

from pemr import db, dedup, persons, units

DICT_PATH = Path(__file__).resolve().parent.parent / "data" / "dictionary.example.toml"
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


@pytest.fixture()
def conn(tmp_path):
    conn = db.connect(tmp_path / "u.db")
    db.migrate(conn)
    persons.add_person(conn, "jane-doe", "Jane Doe")
    yield conn
    conn.close()


@pytest.fixture()
def dictionary():
    return dedup.load_dictionary(DICT_PATH)


# --- registry: alias resolution ----------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("lb", "lb"), ("lbs", "lb"), ("LB", "lb"), ("  Pounds ", "lb"),
    ("kg", "kg"), ("KGs", "kg"),
    ("F", "degF"), ("degF", "degF"), ("deg f", "degF"), ("°F", "degF"),
    ("C", "degC"), ("°c", "degC"),
    ("inches", "in"), ('"', "in"), ("feet", "ft"),
    ("mm[Hg]", "mmHg"), ("mmhg", "mmHg"),
    ("breaths/min", "/min"), ("bpm", "/min"),
    ("percent", "%"),
])
def test_canonical_unit_resolves_corpus_spellings(text, expected):
    assert units.canonical_unit(text) == expected


@pytest.mark.parametrize("text", ["", "   ", None, "widgets", "mg/dL"])
def test_canonical_unit_is_none_for_unknown_or_empty(text):
    """`None` is the caller's cue to leave the stored value alone. `mg/dL` is in the
    corpus and deliberately *not* in the registry: it has no second spelling anyone
    wants to convert to, and guessing at a scale is the one failure this cannot have."""
    assert units.canonical_unit(text) is None


def test_canonical_ids_are_ascii_and_self_resolving():
    for unit_id in units.known_units():
        assert unit_id.isascii()                       # cp437 console contract
        assert units.canonical_unit(unit_id) == unit_id


# --- registry: conversion -----------------------------------------------------

def test_convert_mass_and_length():
    assert round(units.convert(77.6, "kg", "lb"), 2) == 171.08
    assert round(units.convert(171.08, "lbs", "kg"), 1) == 77.6   # round trip
    assert round(units.convert(65, "in", "cm"), 2) == 165.1


def test_convert_temperature_is_affine_both_ways():
    """A factor-only converter silently corrupts every degC/degF display, so both
    directions are pinned."""
    assert round(units.convert(36.6, "C", "degF"), 2) == 97.88
    assert round(units.convert(98.6, "F", "degC"), 2) == 37.0
    assert round(units.convert(0, "degC", "degF"), 2) == 32.0


def test_convert_same_unit_returns_the_input_untouched():
    value = 77.6
    assert units.convert(value, "kgs", "kg") is value


@pytest.mark.parametrize("value,from_text,to_unit", [
    (70.0, "kg", "cm"),          # cross-dimension
    (70.0, "widgets", "kg"),     # unknown source
    (70.0, "kg", "widgets"),     # unknown target
    (70.0, "kg", None),          # no preference
    (70.0, None, "kg"),          # unit-less row
])
def test_convert_refuses_rather_than_guessing(value, from_text, to_unit):
    assert units.convert(value, from_text, to_unit) is None


def test_round_display_is_two_places():
    assert units.round_display(171.00567) == 171.01
    assert units.round_display(5) == 5.0


# --- registry: display resolution --------------------------------------------

def test_display_converts_and_reports_its_source():
    d = units.display(77.6, "kg", "lb")
    assert d.converted is True
    assert d.value == 171.08 and d.unit == "lb"
    assert d.source_value == 77.6 and d.source_unit == "kg"


@pytest.mark.parametrize("value,unit_text,target", [
    (77.6, "kg", None),          # no preference for this key
    (77.6, "widgets", "lb"),     # unknown stored unit
    (77.6, "kg", "cm"),          # cross-dimension preference
    (None, "kg", "lb"),          # value_text-only row
    ("trace", "kg", "lb"),       # non-numeric value
    (170.0, "lbs", "lb"),        # already in the canonical unit: a relabel is #129's job
])
def test_display_passes_the_stored_value_through_untouched(value, unit_text, target):
    d = units.display(value, unit_text, target)
    assert d.converted is False
    assert d.value == value and d.unit == unit_text


def test_in_target_distinguishes_already_canonical_from_unconvertible():
    assert units.in_target("lbs", "lb") is True
    assert units.in_target("kg", "lb") is False
    assert units.in_target("kg", None) is False


# --- preference store ---------------------------------------------------------

def test_set_pref_stores_the_key_token_and_canonical_unit(conn, dictionary):
    pref = units.set_pref(conn, "jane-doe", "A1c", "lbs", dictionary=dictionary)
    assert pref["key"] == "hba1c"        # dictionary-normalised, like every other key
    assert pref["unit"] == "lb"          # spelling canonicalised on the way in
    person_id = persons.get_person(conn, "jane-doe").person_id
    assert units.load_prefs(conn, person_id) == {"hba1c": "lb"}


def test_set_pref_upserts_rather_than_duplicating(conn, dictionary):
    units.set_pref(conn, "jane-doe", "weight", "kg", dictionary=dictionary)
    units.set_pref(conn, "jane-doe", "weight", "lb", dictionary=dictionary)
    assert units.list_prefs(conn, "jane-doe") == [
        {"key": "weight", "unit": "lb",
         "set_at": units.list_prefs(conn, "jane-doe")[0]["set_at"]}
    ]


def test_set_pref_rejects_an_unknown_unit_and_writes_nothing(conn, dictionary):
    with pytest.raises(units.UnknownUnitError, match="known units"):
        units.set_pref(conn, "jane-doe", "weight", "widgets", dictionary=dictionary)
    assert units.list_prefs(conn, "jane-doe") == []


def test_set_pref_rejects_an_empty_key(conn, dictionary):
    with pytest.raises(ValueError):
        units.set_pref(conn, "jane-doe", "   ", "lb", dictionary=dictionary)
    assert units.list_prefs(conn, "jane-doe") == []


def test_set_pref_unknown_slug_raises(conn, dictionary):
    with pytest.raises(persons.PersonNotFoundError):
        units.set_pref(conn, "nobody", "weight", "lb", dictionary=dictionary)


def test_clear_pref_reports_whether_anything_was_there(conn, dictionary):
    assert units.clear_pref(conn, "jane-doe", "weight", dictionary=dictionary) is False
    units.set_pref(conn, "jane-doe", "weight", "lb", dictionary=dictionary)
    assert units.clear_pref(conn, "jane-doe", "weight", dictionary=dictionary) is True
    assert units.load_prefs(
        conn, persons.get_person(conn, "jane-doe").person_id
    ) == {}


def test_list_prefs_is_ordered_by_key_and_person_scoped(conn, dictionary):
    persons.add_person(conn, "john-doe", "John Doe")
    for key, unit in (("weight", "lb"), ("temperature", "degF"), ("height", "cm")):
        units.set_pref(conn, "jane-doe", key, unit, dictionary=dictionary)
    units.set_pref(conn, "john-doe", "weight", "kg", dictionary=dictionary)
    assert [r["key"] for r in units.list_prefs(conn, "jane-doe")] == [
        "height", "temperature", "weight",
    ]
    assert units.list_prefs(conn, "john-doe") == [
        {"key": "weight", "unit": "kg",
         "set_at": units.list_prefs(conn, "john-doe")[0]["set_at"]}
    ]


def test_pre_013_database_reads_as_no_preferences(tmp_path):
    """A restored snapshot predating migration 013 must report "no preferences" rather
    than raise `no such table` (the `curation.has_table` guard)."""
    older = tmp_path / "migrations"
    older.mkdir()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if not path.name.startswith("013"):
            shutil.copy(path, older / path.name)
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn, older)
        person = persons.add_person(conn, "jane-doe", "Jane Doe")
        assert units.has_pref_table(conn) is False
        assert units.load_prefs(conn, person.person_id) == {}
        assert units.list_prefs(conn, "jane-doe") == []
        assert units.clear_pref(conn, "jane-doe", "weight") is False
    finally:
        conn.close()


def test_a_preference_never_blocks_a_childless_person_remove(conn, dictionary):
    """A display preference is not medical history, so `person_unit_pref` is
    deliberately absent from `persons._CHILD_TABLES` and cascades away instead."""
    persons.add_person(conn, "typo", "Typo Person")
    units.set_pref(conn, "typo", "weight", "lb", dictionary=dictionary)
    persons.remove_person(conn, "typo")
    assert persons.get_person(conn, "typo") is None
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM person_unit_pref"
    ).fetchone()["n"] == 0

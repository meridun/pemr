"""Layer-2 dedup: norm()/dictionary, dedup_key determinism, schema validation,
new/duplicate/conflict split via commit_extraction."""

import pytest

from pemr import db, dedup, persons

DICT_PATH = __import__("pathlib").Path(__file__).resolve().parent.parent \
    / "data" / "dictionary.example.toml"


@pytest.fixture()
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    persons.add_person(conn, "jane-doe", "Jane Doe")
    yield conn
    conn.close()


def _make_document(conn, person_slug="jane-doe"):
    pid = conn.execute(
        "SELECT person_id FROM person WHERE slug=?", (person_slug,)
    ).fetchone()["person_id"]
    cur = conn.execute(
        "INSERT INTO document (sha256, person_id, source_path, ingested_at) "
        "VALUES (?, ?, ?, ?)",
        (f"sha-{pid}-{conn.total_changes}", pid, "aa/deadbeef.pdf", "2026-01-01T00:00:00"),
    )
    conn.commit()
    return cur.lastrowid


# --- norm() + dictionary ------------------------------------------------------

def test_norm_lowercases_trims_collapses():
    assert dedup.norm("  Hemoglobin   A1c ") == "hemoglobin a1c"


def test_norm_none_is_empty_string():
    assert dedup.norm(None) == ""


def test_norm_maps_synonyms_via_dictionary():
    d = dedup.load_dictionary(DICT_PATH)
    assert d  # starter dictionary loaded
    for spelling in ("A1c", "HbA1c", "Hemoglobin A1c", "  glycated hemoglobin  "):
        assert dedup.norm(spelling, d) == "hba1c"


def test_load_missing_dictionary_is_empty(tmp_path):
    assert dedup.load_dictionary(tmp_path / "nope.toml") == {}
    assert dedup.load_dictionary(None) == {}


# --- dedup_key determinism ----------------------------------------------------

def test_dedup_key_is_stable_across_formatting():
    d = dedup.load_dictionary(DICT_PATH)
    a = dedup.dedup_key("lab_result",
                        {"test_name": "HbA1c", "collected_at": "2026-01-02", "value_num": 5.7},
                        person_id=1, dictionary=d)
    b = dedup.dedup_key("lab_result",
                        {"test_name": "  a1c ", "collected_at": "2026-01-02T09:30", "value_num": 5.7},
                        person_id=1, dictionary=d)
    assert a == b  # same fact, different spelling/whitespace/time-of-day -> one key


def test_dedup_key_differs_by_person():
    row = {"test_name": "hba1c", "collected_at": "2026-01-02", "value_num": 5.7}
    assert dedup.dedup_key("lab_result", row, 1) != dedup.dedup_key("lab_result", row, 2)


def test_dedup_key_differs_by_date():
    a = dedup.dedup_key("lab_result",
                        {"test_name": "hba1c", "collected_at": "2026-01-02", "value_num": 5.7}, 1)
    b = dedup.dedup_key("lab_result",
                        {"test_name": "hba1c", "collected_at": "2026-06-02", "value_num": 5.7}, 1)
    assert a != b


# --- validation ---------------------------------------------------------------

def test_validate_rejects_unknown_type():
    with pytest.raises(dedup.ValidationError, match="unknown record type"):
        dedup.validate_row("wibble", {"x": 1})


def test_validate_rejects_missing_required():
    with pytest.raises(dedup.ValidationError, match="missing required field"):
        dedup.validate_row("lab_result", {"test_name": "hba1c"})  # no collected_at


def test_validate_rejects_unknown_field():
    with pytest.raises(dedup.ValidationError, match="unknown field"):
        dedup.validate_row(
            "lab_result",
            {"test_name": "hba1c", "collected_at": "2026-01-02", "bogus": 1},
        )


def test_validate_rejects_wrong_type():
    with pytest.raises(dedup.ValidationError, match="value_num"):
        dedup.validate_row(
            "lab_result",
            {"test_name": "hba1c", "collected_at": "2026-01-02", "value_num": "high"},
        )


# --- commit_extraction: new / duplicate / conflict ----------------------------

def _lab(value):
    return {"test_name": "HbA1c", "collected_at": "2026-01-02", "value_num": value,
            "unit": "%"}


def test_commit_inserts_new_rows(conn):
    doc = _make_document(conn)
    d = dedup.load_dictionary(DICT_PATH)
    summary = dedup.commit_extraction(conn, doc, {"lab_result": [_lab(5.7)]}, d)
    assert summary.counts == {"new": 1, "duplicate": 0, "conflict": 0}
    row = conn.execute("SELECT * FROM lab_result").fetchone()
    assert row["test_name"] == "HbA1c" and row["value_num"] == 5.7


def test_same_fact_from_two_documents_collapses(conn):
    d = dedup.load_dictionary(DICT_PATH)
    doc1 = _make_document(conn)
    doc2 = _make_document(conn)
    # doc1 says "HbA1c", doc2 says "A1c" — same person/date/value -> one row
    dedup.commit_extraction(conn, doc1, {"lab_result": [_lab(5.7)]}, d)
    summary = dedup.commit_extraction(
        conn, doc2,
        {"lab_result": [{"test_name": "A1c", "collected_at": "2026-01-02",
                         "value_num": 5.7, "unit": "%"}]},
        d,
    )
    assert summary.counts["duplicate"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 1


def test_differing_value_stages_conflict(conn):
    d = dedup.load_dictionary(DICT_PATH)
    doc1 = _make_document(conn)
    doc2 = _make_document(conn)
    dedup.commit_extraction(conn, doc1, {"lab_result": [_lab(5.7)]}, d)
    # same key bucket (round(5.7)==round(6.2)==6) but a corrected value -> conflict
    summary = dedup.commit_extraction(conn, doc2, {"lab_result": [_lab(6.2)]}, d)
    assert summary.counts == {"new": 0, "duplicate": 0, "conflict": 1}
    # original row untouched, no second lab row inserted
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 1
    assert conn.execute("SELECT value_num FROM lab_result").fetchone()["value_num"] == 5.7
    assert conn.execute("SELECT COUNT(*) AS n FROM conflict WHERE status='open'"
                        ).fetchone()["n"] == 1


def test_commit_unknown_type_rolls_back(conn):
    doc = _make_document(conn)
    with pytest.raises(dedup.ValidationError):
        dedup.commit_extraction(
            conn, doc, {"lab_result": [_lab(5.7)], "wibble": [{"x": 1}]}
        )
    # atomic: the valid lab_result must NOT have landed
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 0


def test_commit_bad_row_rolls_back_whole_batch(conn):
    doc = _make_document(conn)
    with pytest.raises(dedup.ValidationError):
        dedup.commit_extraction(
            conn, doc,
            {"lab_result": [_lab(5.7), {"test_name": "ldl"}]},  # 2nd row: no collected_at
        )
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 0


def test_commit_unknown_document_raises(conn):
    with pytest.raises(dedup.ValidationError, match="no document"):
        dedup.commit_extraction(conn, 999, {"lab_result": [_lab(5.7)]})


def test_observation_and_medication_commit(conn):
    doc = _make_document(conn)
    summary = dedup.commit_extraction(conn, doc, {
        "medication": [{"name": "Metformin", "dose": "500mg", "started_on": "2025-01-01"}],
        "observation": [{"obs_type": "blood_pressure", "observed_at": "2026-01-02",
                         "key": "systolic", "value_num": 120}],
    })
    assert summary.counts["new"] == 2

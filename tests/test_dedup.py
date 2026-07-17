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


def test_norm_treats_underscores_as_spaces():
    assert dedup.norm("blood_pressure") == "blood pressure"
    d = dedup.load_dictionary(DICT_PATH)
    assert dedup.norm("blood_pressure", d) == dedup.norm("Blood Pressure", d) \
        == "blood_pressure"


def test_vitals_synonyms_map_to_canonical():
    d = dedup.load_dictionary(DICT_PATH)
    for spelling in ("Pulse", "pulse rate", "Heart Rate"):
        assert dedup.norm(spelling, d) == "pulse"
    for spelling in ("BP", "Blood Pressure", "blood_pressure"):
        assert dedup.norm(spelling, d) == "blood_pressure"


def test_norm_strips_parenthetical_qualifiers():
    d = dedup.load_dictionary(DICT_PATH)
    # `(HGB)` / `(HCT)` / `(SPEP)` are method/label tags, not different analytes.
    assert dedup.norm("Hemoglobin (HGB)", d) == "hemoglobin"
    assert dedup.norm("Hematocrit (HCT)", d) == "hematocrit"
    assert dedup.norm("M-Spike (SPEP)", d) == "m_spike"
    # Stripping happens even without a dictionary (pure normalization step).
    assert dedup.norm("Creatinine (calculated)") == "creatinine"


# Real-corpus naming variants (issue #11): report-side spelling <-> CSV-side spelling
# for the same clinical fact. Every pair must collapse to a single canonical token or
# the same analyte splits into parallel rows/series downstream.
_CORPUS_VARIANTS = [
    ("GLUC", "Glucose"),
    ("NA", "Sodium"),
    ("K", "Potassium"),
    ("CREA", "Creatinine"),
    ("Hemoglobin (HGB)", "HGB"),
    ("Hematocrit (HCT)", "HCT"),
    ("Platelet count", "PLT"),
    ("Free Kappa light chain", "Kappa"),
    ("Free Lambda light chain", "Lambda"),
    ("Kappa/Lambda ratio", "K/L Ratio"),
    ("M-Spike (SPEP)", "M-Spike"),
    ("Immunoglobulin G, Qn, Serum", "Immunoglobulin G"),
    ("VIT B12", "Vitamin B12"),
    ("B2 Microglobulin", "Beta-2 Microglobulin"),
]


def test_corpus_naming_variants_share_canonical_token():
    d = dedup.load_dictionary(DICT_PATH)
    for report, csv in _CORPUS_VARIANTS:
        assert dedup.norm(report, d) == dedup.norm(csv, d), (report, csv)


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


def test_observation_key_variants_share_dedup_key():
    # Two scans of the same vital: one extraction pass says "blood_pressure",
    # the other "Blood Pressure" — must collapse even without a dictionary.
    row_a = {"obs_type": "vital", "key": "blood_pressure", "value_text": "155/90",
             "observed_at": "2024-06-20"}
    row_b = {"obs_type": "vital", "key": "Blood Pressure", "value_text": "155/90",
             "observed_at": "2024-06-20"}
    assert dedup.dedup_key("observation", row_a, 1) == dedup.dedup_key("observation", row_b, 1)


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


@pytest.mark.parametrize("bad", ["06/15/2026", "not-a-date", "2026-13-01",
                                  "2026-01-32", "20260102", "2026/01/02", "Jan 2 2026"])
def test_validate_rejects_non_iso_date(bad):
    # Non-ISO date strings must be rejected by name+value (issue #19): they sort
    # lexically ahead of real ISO dates and corrupt the timeline.
    with pytest.raises(dedup.ValidationError, match=r"collected_at.*ISO date"):
        dedup.validate_row(
            "lab_result",
            {"test_name": "Sodium", "value_num": 140, "collected_at": bad},
        )


@pytest.mark.parametrize("good", ["2026-01-02", "2026-01-02T09:30",
                                   "2026-01-02T09:30:00", "2026-01-02 09:30:00",
                                   "2026-01-02T09:30:00+00:00", "2026-01-02T09:30:00Z"])
def test_validate_accepts_iso_dates_and_timestamps(good):
    # Full ISO timestamps are accepted alongside bare YYYY-MM-DD.
    dedup.validate_row(
        "lab_result",
        {"test_name": "Sodium", "value_num": 140, "collected_at": good},
    )


def test_validate_rejects_non_iso_date_across_record_types():
    # Every date-typed field, not just lab_result.collected_at, is guarded.
    with pytest.raises(dedup.ValidationError, match=r"started_on.*ISO date"):
        dedup.validate_row("medication", {"name": "Metformin", "started_on": "06/15/2026"})
    with pytest.raises(dedup.ValidationError, match=r"ended_on.*ISO date"):
        dedup.validate_row("medication", {"name": "Metformin", "ended_on": "bad"})
    with pytest.raises(dedup.ValidationError, match=r"performed_on.*ISO date"):
        dedup.validate_row("procedure", {"name": "MRI", "performed_on": "13/01/2026"})
    with pytest.raises(dedup.ValidationError, match=r"scheduled_for.*ISO date"):
        dedup.validate_row("appointment", {"scheduled_for": "next tuesday"})
    with pytest.raises(dedup.ValidationError, match=r"observed_at.*ISO date"):
        dedup.validate_row("observation", {"obs_type": "vital", "observed_at": "2026/01/02"})


def test_commit_bad_date_rolls_back_whole_batch(conn):
    # End-to-end: a non-ISO date in a committed batch rolls the whole thing back.
    doc = _make_document(conn)
    with pytest.raises(dedup.ValidationError, match="ISO date"):
        dedup.commit_extraction(
            conn, doc,
            {"lab_result": [_lab(5.7),
                            {"test_name": "Sodium", "value_num": 140,
                             "collected_at": "06/15/2026"}]},
        )
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 0


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


def test_unit_casing_difference_is_duplicate_not_conflict(conn):
    # `MG/DL` (report) vs `mg/dL` (CSV) is the same unit — must not stage a conflict.
    d = dedup.load_dictionary(DICT_PATH)
    doc1 = _make_document(conn)
    doc2 = _make_document(conn)
    r1 = {"test_name": "Glucose", "collected_at": "2026-02-01", "value_num": 95,
          "unit": "MG/DL"}
    r2 = {"test_name": "GLUC", "collected_at": "2026-02-01", "value_num": 95,
          "unit": "mg/dL"}
    dedup.commit_extraction(conn, doc1, {"lab_result": [r1]}, d)
    summary = dedup.commit_extraction(conn, doc2, {"lab_result": [r2]}, d)
    assert summary.counts == {"new": 0, "duplicate": 1, "conflict": 0}
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 1


def test_real_corpus_overlap_dedups_not_splits(conn):
    """Acceptance (issue #11): a report document and the historical CSV committed as a
    second document must recognize every overlapping fact — none slip through as new,
    unit casing doesn't fabricate conflicts, one stored row per analyte so `trends`
    can't split a series and `render summary` can't double-count."""
    d = dedup.load_dictionary(DICT_PATH)
    doc_report = _make_document(conn)
    doc_csv = _make_document(conn)
    date = "2026-01-05"
    # Same person, same date, same value per analyte — only the spelling and unit
    # casing differ between the two documents.
    report_rows = [
        {"test_name": report, "collected_at": date, "value_num": float(i + 1),
         "unit": "MG/DL"}
        for i, (report, _csv) in enumerate(_CORPUS_VARIANTS)
    ]
    csv_rows = [
        {"test_name": csv, "collected_at": date, "value_num": float(i + 1),
         "unit": "mg/dL"}
        for i, (_report, csv) in enumerate(_CORPUS_VARIANTS)
    ]
    s1 = dedup.commit_extraction(conn, doc_report, {"lab_result": report_rows}, d)
    assert s1.counts == {"new": len(_CORPUS_VARIANTS), "duplicate": 0, "conflict": 0}
    s2 = dedup.commit_extraction(conn, doc_csv, {"lab_result": csv_rows}, d)
    assert s2.counts == {"new": 0, "duplicate": len(_CORPUS_VARIANTS), "conflict": 0}
    # One row per analyte, not two — the split-series / double-count damage is gone.
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] \
        == len(_CORPUS_VARIANTS)


def test_corrected_value_still_conflicts_after_normalization(conn):
    # Normalization must not swallow a genuine value change into a silent duplicate.
    d = dedup.load_dictionary(DICT_PATH)
    doc1 = _make_document(conn)
    doc2 = _make_document(conn)
    r1 = {"test_name": "HGB", "collected_at": "2026-03-01", "value_num": 13.1,
          "unit": "g/dL"}
    r2 = {"test_name": "Hemoglobin (HGB)", "collected_at": "2026-03-01",
          "value_num": 13.4, "unit": "G/DL"}  # same round-bucket, corrected value
    dedup.commit_extraction(conn, doc1, {"lab_result": [r1]}, d)
    summary = dedup.commit_extraction(conn, doc2, {"lab_result": [r2]}, d)
    assert summary.counts == {"new": 0, "duplicate": 0, "conflict": 1}


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

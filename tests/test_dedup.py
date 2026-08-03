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


def test_esr_synonyms_map_to_canonical():
    # Issue #41: an ESR result labeled "SED RATE BY MODIFIED WESTERGREN" on a Quest
    # report must share the `esr` identity with every other ESR spelling.
    d = dedup.load_dictionary(DICT_PATH)
    for spelling in ("ESR", "Sed Rate", "SED RATE BY MODIFIED WESTERGREN",
                     "Erythrocyte Sedimentation Rate"):
        assert dedup.norm(spelling, d) == "esr", spelling


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


# --- key_token(): qualifier-aware identity (issue #71) ------------------------

def test_key_token_keeps_a_meaningful_qualifier():
    # The bug: an SPEP albumin fraction and a CMP albumin off one draw are different
    # assays. They stay one analyte *family* under norm() but must not share a key.
    d = dedup.load_dictionary(DICT_PATH)
    assert dedup.norm("Albumin (SPEP)", d) == dedup.norm("Albumin", d) == "albumin"
    assert dedup.key_token("Albumin (SPEP)", d) == "albumin (spep)"
    assert dedup.key_token("Albumin", d) == "albumin"


def test_key_token_drops_a_redundant_alias_qualifier():
    # Rule 2: the parenthetical maps to the same canonical token as the stem, so it is
    # just another spelling of it -- no dictionary entry of its own required.
    d = dedup.load_dictionary(DICT_PATH)
    assert dedup.key_token("Hemoglobin (HGB)", d) == dedup.key_token("HGB", d) == "hemoglobin"
    assert dedup.key_token("Hematocrit (HCT)", d) == "hematocrit"
    assert dedup.key_token("Platelet Count (PLT)", d) == "platelets"


def test_key_token_honors_a_declared_full_label():
    # Rule 1: a full parenthesized key in [synonyms] is the human declaring that
    # parenthetical noise for this analyte.
    d = dedup.load_dictionary(DICT_PATH)
    assert dedup.key_token("M-Spike (SPEP)", d) == dedup.key_token("M-Spike", d) == "m_spike"
    assert dedup.key_token("Sed Rate (Modified Westergren)", d) \
        == dedup.key_token("Sed Rate", d) == "esr"
    assert dedup.key_token("Creatinine (calculated)", d) == "creatinine"


def test_key_token_maps_the_qualifier_through_the_dictionary():
    # Two spellings of one assay tag agree, so they do not fork the series.
    d = {"albumin": "albumin", "serum protein electrophoresis": "spep"}
    assert dedup.key_token("Albumin (Serum Protein Electrophoresis)", d) \
        == dedup.key_token("Albumin (SPEP)", {**d, "spep": "spep"}) == "albumin (spep)"


def test_key_token_without_dictionary_oversplits_but_norm_does_not():
    # No dictionary: "(calculated)" cannot be proven noise, so the key splits (the
    # accepted, visible, non-lossy failure). norm() is unchanged -- one family.
    assert dedup.norm("Creatinine (calculated)") == dedup.norm("Creatinine") == "creatinine"
    assert dedup.key_token("Creatinine (calculated)") == "creatinine (calculated)"
    bare = {"test_name": "Creatinine", "collected_at": "2026-01-02", "value_num": 1.1}
    calc = {**bare, "test_name": "Creatinine (calculated)"}
    assert dedup.dedup_key("lab_result", bare, 1) != dedup.dedup_key("lab_result", calc, 1)


def test_identity_of_none_and_bare_qualifier():
    assert dedup.identity(None) == ("", "")
    assert dedup.key_token(None) == ""
    # Degenerate name that is nothing but a qualifier: norm() keeps its old ""
    # contract, key_token falls back to the qualifier rather than emitting " (spep)".
    assert dedup.norm("(SPEP)") == ""
    assert dedup.key_token("(SPEP)") == "spep"


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


def test_corpus_naming_variants_share_dedup_key():
    # Stronger than the norm()-level check above: a shared canonical token no longer
    # implies a shared key now that qualifiers are part of the identity (issue #71), and
    # a shared *key* is what actually makes the two spellings dedup.
    d = dedup.load_dictionary(DICT_PATH)
    for report, csv in _CORPUS_VARIANTS:
        assert dedup.key_token(report, d) == dedup.key_token(csv, d), (report, csv)
        a = dedup.dedup_key(
            "lab_result", {"test_name": report, "collected_at": "2026-01-02"}, 1, d)
        b = dedup.dedup_key(
            "lab_result", {"test_name": csv, "collected_at": "2026-01-02"}, 1, d)
        assert a == b, (report, csv)


# --- dedup_key determinism ----------------------------------------------------

def test_dedup_key_is_stable_across_formatting():
    d = dedup.load_dictionary(DICT_PATH)
    a = dedup.dedup_key("lab_result",
                        {"test_name": "HbA1c", "collected_at": "2026-01-02T09:30", "value_num": 5.7},
                        person_id=1, dictionary=d)
    b = dedup.dedup_key("lab_result",
                        {"test_name": "  a1c ", "collected_at": "2026-01-02 09:30", "value_num": 6.2},
                        person_id=1, dictionary=d)
    # same draw (person/analyte/timestamp), different spelling/whitespace, T-vs-space
    # separator, AND a differing value -> one key (value is no longer an identity field)
    assert a == b


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


def test_partial_and_full_date_of_same_event_are_distinct_keys():
    # Issue #42 Q2: the dedup layer never guesses a full date refines a partial one —
    # a YYYY-MM procedure and a later fully-dated copy get distinct keys (two rows).
    partial = dedup.dedup_key("procedure",
                              {"name": "Appendectomy", "performed_on": "2019-03"}, 1)
    full = dedup.dedup_key("procedure",
                           {"name": "Appendectomy", "performed_on": "2019-03-15"}, 1)
    year = dedup.dedup_key("procedure",
                           {"name": "Appendectomy", "performed_on": "2019"}, 1)
    assert partial != full != year and partial != year


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


@pytest.mark.parametrize("good", ["2026", "2026-01", "2026-12", "1999", "2019-03"])
def test_validate_accepts_partial_dates(good):
    # Issue #42: month (YYYY-MM) and year (YYYY) precision prefixes are first-class.
    dedup.validate_row("procedure", {"name": "Appendectomy", "performed_on": good})


@pytest.mark.parametrize("bad", ["2026-13", "2026-00", "2026-3", "2026-1", "0000",
                                  "202X", "2026-", "2026-03-", "2026-03T09:00",
                                  "2026T09:00", "20260", "999"])
def test_validate_rejects_partial_date_junk(bad):
    # Near-miss partial forms are still rejected: bad month, single-digit or missing
    # month, implausible year, and any time component on a non-full-date precision.
    with pytest.raises(dedup.ValidationError, match=r"performed_on.*ISO date"):
        dedup.validate_row("procedure", {"name": "MRI", "performed_on": bad})


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
    # same person/analyte/date (the dedup key) but a corrected value -> conflict.
    # The value is no longer in the key, so this fires regardless of magnitude.
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


def test_distinct_assays_of_one_analyte_both_land_as_new(conn):
    """Acceptance (issue #71): a CMP `Albumin` and an SPEP `Albumin (SPEP)` off ONE draw
    are two facts. Before the fix the parenthetical was stripped, both derived one key,
    and the second was misfiled as a conflict whose value survived only in a free-text
    resolution note."""
    d = dedup.load_dictionary(DICT_PATH)
    doc = _make_document(conn)
    rows = [
        {"test_name": "Albumin", "collected_at": "2026-01-05", "value_num": 4.1,
         "unit": "g/dL"},
        {"test_name": "Albumin (SPEP)", "collected_at": "2026-01-05", "value_num": 3.8,
         "unit": "g/dL"},
    ]
    # Distinct keys, so this is not even an intra-payload collision any more.
    keys = {dedup.dedup_key("lab_result", r, 1, d) for r in rows}
    assert len(keys) == 2
    summary = dedup.commit_extraction(conn, doc, {"lab_result": rows}, d)
    assert summary.counts == {"new": 2, "duplicate": 0, "conflict": 0}
    stored = {r["test_name"]: r["value_num"] for r in
              conn.execute("SELECT test_name, value_num FROM lab_result")}
    assert stored == {"Albumin": 4.1, "Albumin (SPEP)": 3.8}


def test_qualified_observation_keys_are_distinct_rows(conn):
    # Same split for observation.key: two BP readings taken in two postures at one visit.
    doc = _make_document(conn)
    rows = [
        {"obs_type": "vital", "key": "Blood Pressure (sitting)",
         "observed_at": "2026-01-05", "value_text": "128/80"},
        {"obs_type": "vital", "key": "Blood Pressure (standing)",
         "observed_at": "2026-01-05", "value_text": "112/70"},
    ]
    summary = dedup.commit_extraction(conn, doc, {"observation": rows})
    assert summary.counts == {"new": 2, "duplicate": 0, "conflict": 0}


def test_corrected_value_still_conflicts_after_normalization(conn):
    # Normalization must not swallow a genuine value change into a silent duplicate.
    d = dedup.load_dictionary(DICT_PATH)
    doc1 = _make_document(conn)
    doc2 = _make_document(conn)
    r1 = {"test_name": "HGB", "collected_at": "2026-03-01", "value_num": 13.1,
          "unit": "g/dL"}
    r2 = {"test_name": "Hemoglobin (HGB)", "collected_at": "2026-03-01",
          "value_num": 13.4, "unit": "G/DL"}  # same draw, corrected value
    dedup.commit_extraction(conn, doc1, {"lab_result": [r1]}, d)
    summary = dedup.commit_extraction(conn, doc2, {"lab_result": [r2]}, d)
    assert summary.counts == {"new": 0, "duplicate": 0, "conflict": 1}


def test_far_apart_correction_conflicts_not_duplicates(conn):
    # Issue #20 headline: a large-magnitude correction (92 -> 130, same draw) used to
    # slip through as a silent new row because value fed the key via round(). With the
    # value out of the key it now collides on person/analyte/timestamp -> conflict.
    d = dedup.load_dictionary(DICT_PATH)
    doc1 = _make_document(conn)
    doc2 = _make_document(conn)
    r1 = {"test_name": "Glucose", "collected_at": "2026-04-01T08:00", "value_num": 92,
          "unit": "mg/dL"}
    r2 = {"test_name": "GLUC", "collected_at": "2026-04-01T08:00", "value_num": 130,
          "unit": "mg/dL"}  # OCR re-read of the *same* draw, wildly different value
    dedup.commit_extraction(conn, doc1, {"lab_result": [r1]}, d)
    summary = dedup.commit_extraction(conn, doc2, {"lab_result": [r2]}, d)
    assert summary.counts == {"new": 0, "duplicate": 0, "conflict": 1}
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 1


def test_serial_same_day_draws_are_distinct_rows(conn):
    # Two genuine draws on the same day at different times (GTT / peri-op / inpatient
    # q6h) carry distinct timestamps -> distinct keys -> two rows, no data loss.
    d = dedup.load_dictionary(DICT_PATH)
    doc = _make_document(conn)
    rows = [
        {"test_name": "Glucose", "collected_at": "2026-04-01T08:00", "value_num": 92,
         "unit": "mg/dL"},
        {"test_name": "Glucose", "collected_at": "2026-04-01T14:00", "value_num": 130,
         "unit": "mg/dL"},
    ]
    summary = dedup.commit_extraction(conn, doc, {"lab_result": rows}, d)
    assert summary.counts == {"new": 2, "duplicate": 0, "conflict": 0}
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 2


def test_observation_correction_conflicts_not_duplicates(conn):
    # Same latent defect for observation: value used to be *in* the key, so a corrected
    # reading silently duplicated. With value out of the key it now conflicts.
    d = dedup.load_dictionary(DICT_PATH)
    doc1 = _make_document(conn)
    doc2 = _make_document(conn)
    o1 = {"obs_type": "vital", "key": "systolic", "observed_at": "2026-05-01T09:00",
          "value_num": 120}
    o2 = {"obs_type": "vital", "key": "systolic", "observed_at": "2026-05-01T09:00",
          "value_num": 155}  # re-read of the same measurement, corrected value
    dedup.commit_extraction(conn, doc1, {"observation": [o1]}, d)
    summary = dedup.commit_extraction(conn, doc2, {"observation": [o2]}, d)
    assert summary.counts == {"new": 0, "duplicate": 0, "conflict": 1}
    assert conn.execute("SELECT COUNT(*) AS n FROM observation").fetchone()["n"] == 1


def test_serial_same_day_observations_are_distinct_rows(conn):
    # Two same-day vitals at different times stay as two rows.
    d = dedup.load_dictionary(DICT_PATH)
    doc = _make_document(conn)
    rows = [
        {"obs_type": "vital", "key": "systolic", "observed_at": "2026-05-01T09:00",
         "value_num": 120},
        {"obs_type": "vital", "key": "systolic", "observed_at": "2026-05-01T17:00",
         "value_num": 138},
    ]
    summary = dedup.commit_extraction(conn, doc, {"observation": rows}, d)
    assert summary.counts == {"new": 2, "duplicate": 0, "conflict": 0}
    assert conn.execute("SELECT COUNT(*) AS n FROM observation").fetchone()["n"] == 2


# --- intra-payload collisions (issue #58) -------------------------------------

def test_intra_payload_collision_with_differing_values_is_rejected(conn):
    """Two rows of ONE submission deriving one key and disagreeing is an extraction
    error, not a conflict: the 'existing' side would have been inserted milliseconds
    earlier in the same batch, so there is nothing independent to adjudicate."""
    doc = _make_document(conn)
    with pytest.raises(dedup.ValidationError) as exc:
        dedup.commit_extraction(conn, doc, {"lab_result": [
            {"test_name": "Glucose", "collected_at": "2024-04-01", "value_num": 95,
             "value_text": "fasting draw"},
            {"test_name": "Glucose", "collected_at": "2024-04-01", "value_num": 148,
             "value_text": "2-hour post-prandial draw"},
        ]})
    message = str(exc.value)
    assert "rows 0 and 1" in message
    assert "person 1 | glucose | 2024-04-01" in message   # the identity, not a hash
    assert "--keep both" in message                       # names the recovery path
    assert message.isascii()                              # cp1252 console (issue #23)
    # Atomic: nothing of the batch landed.
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 0


def test_intra_payload_identical_rows_still_report_duplicate(conn):
    """An agent listing one fact twice is benign, not an error."""
    doc = _make_document(conn)
    row = {"test_name": "Glucose", "collected_at": "2024-04-01", "value_num": 95}
    summary = dedup.commit_extraction(conn, doc, {"lab_result": [dict(row), dict(row)]})
    assert summary.counts == {"new": 1, "duplicate": 1, "conflict": 0}


def test_intra_payload_collision_checked_per_record_type(conn):
    """Distinct types never collide with each other, and a same-key observation pair is
    caught the same way a lab pair is."""
    doc = _make_document(conn)
    with pytest.raises(dedup.ValidationError, match="observation: rows 0 and 1"):
        dedup.commit_extraction(conn, doc, {"observation": [
            {"obs_type": "vital", "key": "systolic", "observed_at": "2024-04-01",
             "value_num": 120},
            {"obs_type": "vital", "key": "systolic", "observed_at": "2024-04-01",
             "value_num": 138},
        ]})


# --- occurrence numbering -----------------------------------------------------

def test_occurrence_zero_key_is_the_base_byte_for_byte(conn):
    """Migration 005 is a pure column copy only because occurrence 0 reproduces the
    pre-005 key exactly - anything else would invalidate every stored key."""
    row = {"test_name": "hba1c", "collected_at": "2026-01-02"}
    base = dedup.dedup_key("lab_result", row, 1)
    assert dedup.dedup_key("lab_result", row, 1, None, 0) == base
    assert dedup.occurrence_key(base, 0) == base
    assert dedup.occurrence_key(base, 1) != base
    assert dedup.dedup_key("lab_result", row, 1, None, 1) == dedup.occurrence_key(base, 1)


def test_migration_005_preserves_pre_existing_keys(tmp_path):
    """The upgrade path: a database written before the occurrence columns existed must
    come through 005 with every stored key byte-identical, and must keep deduping."""
    import shutil

    staged = tmp_path / "migrations"
    staged.mkdir()
    all_migrations = sorted(db.DEFAULT_MIGRATIONS_DIR.glob("*.sql"))
    pre_005 = [p for p in all_migrations if not p.name.startswith("005_")]
    for path in pre_005:
        shutil.copy(path, staged / path.name)

    conn = db.connect(tmp_path / "legacy.db")
    try:
        db.migrate(conn, staged)
        persons.add_person(conn, "jane-doe", "Jane Doe")
        pid = conn.execute("SELECT person_id FROM person").fetchone()["person_id"]
        row = {"test_name": "hba1c", "collected_at": "2026-01-02", "value_num": 5.7}
        legacy_key = dedup.dedup_key("lab_result", row, pid)
        conn.execute(
            "INSERT INTO lab_result (person_id, test_name, collected_at, value_num, "
            "dedup_key) VALUES (?, ?, ?, ?, ?)",
            (pid, row["test_name"], row["collected_at"], row["value_num"], legacy_key),
        )
        conn.commit()

        for path in all_migrations:
            shutil.copy(path, staged / path.name)
        assert db.migrate(conn, staged) == ["005_dedup_occurrence.sql"]

        stored = conn.execute("SELECT * FROM lab_result").fetchone()
        assert stored["dedup_key"] == legacy_key      # no key churn
        assert stored["dedup_base"] == legacy_key     # backfilled from the key
        assert stored["dedup_occurrence"] == 0

        # And the pre-005 row still dedups against a fresh commit of the same fact.
        doc = _make_document(conn)
        summary = dedup.commit_extraction(conn, doc, {"lab_result": [row]})
        assert summary.counts == {"new": 0, "duplicate": 1, "conflict": 0}
    finally:
        conn.close()


def test_commit_stores_dedup_base_for_every_row(conn):
    """The family lookup keys off dedup_base, so no write path may leave it null."""
    doc = _make_document(conn)
    dedup.commit_extraction(conn, doc, {"lab_result": [_lab(5.7)]})
    row = conn.execute("SELECT * FROM lab_result").fetchone()
    assert row["dedup_base"] == row["dedup_key"]
    assert row["dedup_occurrence"] == 0


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


# --- rekey (dictionary-edit maintenance) --------------------------------------

def _rekey_dict(**extra):
    """Starter dictionary plus the synonyms a maintenance run is meant to pick up.

    The added spellings are deliberately ones the shipped dictionary does NOT carry, so
    the test stays green as `data/dictionary.example.toml` grows."""
    d = dedup.load_dictionary(DICT_PATH)
    d.update(extra)
    return d


def test_rekey_dry_run_reports_without_writing(conn):
    doc = _make_document(conn)
    # Committed with the shipped dictionary, where "cl" has no synonym.
    dedup.commit_extraction(conn, doc, {"lab_result": [
        {"test_name": "ZZT", "collected_at": "2026-01-02", "value_num": 108},
        {"test_name": "HbA1c", "collected_at": "2026-01-02", "value_num": 5.7},
    ]}, dedup.load_dictionary(DICT_PATH))
    before = {r["test_name"]: r["dedup_key"]
              for r in conn.execute("SELECT test_name, dedup_key FROM lab_result")}

    report = dedup.rekey(conn, _rekey_dict(zzt="zonulin_test"))

    assert report.applied is False
    assert report.scanned["lab_result"] == 2
    assert [(c.record_type, c.label) for c in report.changes] == [("lab_result", "ZZT")]
    after = {r["test_name"]: r["dedup_key"]
             for r in conn.execute("SELECT test_name, dedup_key FROM lab_result")}
    assert after == before  # dry run touched nothing


def test_rekey_apply_restores_dedup_for_a_renamed_analyte(conn):
    d_old = dedup.load_dictionary(DICT_PATH)
    d_new = _rekey_dict(zzt="zonulin_test")
    doc = _make_document(conn)
    row = {"test_name": "ZZT", "collected_at": "2026-01-02", "value_num": 108}
    dedup.commit_extraction(conn, doc, {"lab_result": [row]}, d_old)

    # Without a rekey the same fact, committed under the new dictionary, would not
    # match the stored key and would land as a *second* row.
    report = dedup.rekey(conn, d_new, apply=True)
    assert report.applied is True and len(report.changes) == 1
    change = report.changes[0]
    assert change.old_key != change.new_key

    doc2 = _make_document(conn)
    summary = dedup.commit_extraction(conn, doc2, {"lab_result": [row]}, d_new)
    assert summary.counts == {"new": 0, "duplicate": 1, "conflict": 0}
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 1


def test_rekey_is_idempotent(conn):
    doc = _make_document(conn)
    dedup.commit_extraction(conn, doc, {"lab_result": [
        {"test_name": "ZZT", "collected_at": "2026-01-02", "value_num": 108},
    ]}, dedup.load_dictionary(DICT_PATH))
    d_new = _rekey_dict(zzt="zonulin_test")
    assert len(dedup.rekey(conn, d_new, apply=True).changes) == 1
    assert dedup.rekey(conn, d_new, apply=True).changes == []


def test_rekey_refuses_a_dictionary_that_fuses_two_facts(conn):
    """Two methods for one analyte off one draw (CMP ALB vs SPEP Albumin) must not be
    merged by a synonym: the run aborts whole, leaving every stored key untouched."""
    doc = _make_document(conn)
    dedup.commit_extraction(conn, doc, {"lab_result": [
        {"test_name": "ALB", "collected_at": "2026-01-02", "value_num": 4.2},
        {"test_name": "Albumin", "collected_at": "2026-01-02", "value_num": 3.6},
    ]}, dedup.load_dictionary(DICT_PATH))
    before = {r["lab_result_id"]: r["dedup_key"]
              for r in conn.execute("SELECT lab_result_id, dedup_key FROM lab_result")}

    with pytest.raises(dedup.RekeyCollisionError, match="same dedup_key"):
        dedup.rekey(conn, _rekey_dict(alb="albumin"), apply=True)

    after = {r["lab_result_id"]: r["dedup_key"]
             for r in conn.execute("SELECT lab_result_id, dedup_key FROM lab_result")}
    assert after == before


def test_rekey_survives_two_rows_swapping_keys(conn):
    """UNIQUE(dedup_key) is enforced per statement, so a swap must not trip the index
    mid-write: A takes B's old key while B takes A's."""
    doc = _make_document(conn)
    dedup.commit_extraction(conn, doc, {"lab_result": [
        {"test_name": "alpha", "collected_at": "2026-01-02", "value_num": 1},
        {"test_name": "beta", "collected_at": "2026-01-02", "value_num": 2},
    ]}, None)

    report = dedup.rekey(conn, {"alpha": "beta", "beta": "alpha"}, apply=True)
    assert len(report.changes) == 2
    keys = {r["test_name"]: r["dedup_key"]
            for r in conn.execute("SELECT test_name, dedup_key FROM lab_result")}
    pid = conn.execute("SELECT person_id FROM person").fetchone()["person_id"]
    assert keys["alpha"] == dedup.dedup_key(
        "lab_result", {"test_name": "beta", "collected_at": "2026-01-02"}, pid
    )
    assert len(set(keys.values())) == 2


def test_rekey_covers_every_record_type(conn):
    doc = _make_document(conn)
    dedup.commit_extraction(conn, doc, {
        "lab_result": [_lab(5.7)],
        "medication": [{"name": "Metformin", "dose": "500mg"}],
        "procedure": [{"name": "Colonoscopy", "performed_on": "2025-06-01"}],
        "appointment": [{"scheduled_for": "2026-03-01", "provider": "Dr. Smith"}],
        "observation": [{"obs_type": "vital", "key": "bp", "observed_at": "2026-01-02"}],
    })
    report = dedup.rekey(conn, None)
    assert report.scanned == {t: 1 for t in dedup.KNOWN_TYPES}
    assert report.changes == []  # same dictionary (none) -> keys already current


def test_rekey_is_a_no_op_over_a_keep_both_family(conn):
    """The whole reason occurrence lives in a column: `rekey` re-derives keys from
    payload, so siblings must recompute to their own keys rather than collide."""
    doc = _make_document(conn)
    d = dedup.load_dictionary(DICT_PATH)
    dedup.commit_extraction(conn, doc, {"lab_result": [
        {"test_name": "Glucose", "collected_at": "2024-04-01", "value_num": 95}]}, d)
    dedup.commit_extraction(conn, _make_document(conn), {"lab_result": [
        {"test_name": "Glucose", "collected_at": "2024-04-01", "value_num": 148}]}, d)
    conflict_id = dedup.list_conflicts(conn)[0]["conflict_id"]
    dedup.resolve_conflict(conn, conflict_id, keep="both")
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 2

    report = dedup.rekey(conn, d, apply=True)
    assert report.changes == []            # no RekeyCollisionError, no key churn
    keys = [r["dedup_key"] for r in conn.execute("SELECT * FROM lab_result")]
    assert len(set(keys)) == 2


def test_rekey_over_unqualified_names_reports_no_changes(conn):
    """Acceptance (issue #71): the qualifier is FOLDED INTO the existing key part, not
    appended as a fourth one. A fourth part would turn `"a|b|c"` into `"a|b||c"` and move
    every stored key in the database; folding moves only the rows a qualifier splits.

    Proven against the pre-fix formula spelled out longhand, so this fails loudly if the
    key layout is ever changed rather than extended."""
    import hashlib

    d = dedup.load_dictionary(DICT_PATH)
    pid = conn.execute("SELECT person_id FROM person").fetchone()["person_id"]
    doc = _make_document(conn)
    rows = [
        {"test_name": "HbA1c", "collected_at": "2026-01-02T09:30", "value_num": 5.7},
        {"test_name": "GLUC", "collected_at": "2026-01-02", "value_num": 95},
        {"test_name": "Hemoglobin (HGB)", "collected_at": "2026-01-02", "value_num": 13.1},
    ]
    dedup.commit_extraction(conn, doc, {"lab_result": rows}, d)

    for row in rows:
        legacy = "|".join([
            str(pid),
            dedup.norm(row["test_name"], d),                    # pre-fix: norm(), not key_token()
            row["collected_at"].replace("T", " "),
        ])
        assert dedup.dedup_key("lab_result", row, pid, d) \
            == hashlib.sha256(legacy.encode("utf-8")).hexdigest(), row["test_name"]

    assert dedup.rekey(conn, d).changes == []


def test_rekey_migrates_a_row_stored_under_a_stripped_key(conn):
    """The issue-#71 migration: a row committed *before* the fix carries the key the old
    stripping formula derived, so `rekey` is what moves it onto its qualified key. Safe by
    construction -- a qualifier only adds precision, so no two rows can fuse."""
    import hashlib

    d = dedup.load_dictionary(DICT_PATH)
    pid = conn.execute("SELECT person_id FROM person").fetchone()["person_id"]
    legacy_key = hashlib.sha256(
        f"{pid}|albumin|2026-01-05".encode("utf-8")   # pre-fix: "(SPEP)" stripped away
    ).hexdigest()
    conn.execute(
        "INSERT INTO lab_result (person_id, test_name, collected_at, value_num, "
        "dedup_key, dedup_base, dedup_occurrence) VALUES (?, ?, ?, ?, ?, ?, 0)",
        (pid, "Albumin (SPEP)", "2026-01-05", 3.6, legacy_key, legacy_key),
    )
    conn.commit()

    report = dedup.rekey(conn, d)
    assert [(c.label, c.old_key) for c in report.changes] \
        == [("Albumin (SPEP)", legacy_key)]

    dedup.rekey(conn, d, apply=True)
    row = conn.execute("SELECT * FROM lab_result").fetchone()
    assert row["dedup_key"] == row["dedup_base"] != legacy_key
    assert row["value_num"] == 3.6 and row["test_name"] == "Albumin (SPEP)"
    # The vacated key is now free for the CMP albumin that always belonged there.
    doc = _make_document(conn)
    summary = dedup.commit_extraction(conn, doc, {"lab_result": [
        {"test_name": "Albumin", "collected_at": "2026-01-05", "value_num": 4.2}]}, d)
    assert summary.counts == {"new": 1, "duplicate": 0, "conflict": 0}
    assert conn.execute(
        "SELECT dedup_key FROM lab_result WHERE test_name = 'Albumin'"
    ).fetchone()["dedup_key"] == legacy_key


def test_rekey_moves_dedup_base_with_the_key(conn):
    """A dictionary edit must not leave dedup_base pointing at the old family, or the
    commit-time family lookup would miss."""
    doc = _make_document(conn)
    dedup.commit_extraction(conn, doc, {"lab_result": [
        {"test_name": "ZZT", "collected_at": "2026-01-02", "value_num": 108}]},
        dedup.load_dictionary(DICT_PATH))
    dedup.rekey(conn, _rekey_dict(zzt="zonulin_test"), apply=True)
    row = conn.execute("SELECT * FROM lab_result").fetchone()
    assert row["dedup_base"] == row["dedup_key"]   # occurrence 0: base IS the key

"""Phase 3 query layer: structured queries, timeline merge, FTS find + trigger sync,
FTS backfill on migrate, trends math, dictionary normalization, and person isolation."""

import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from pemr import db, dedup, persons, query

DICT_PATH = Path(__file__).resolve().parent.parent / "data" / "dictionary.example.toml"


def _doc(conn, slug, ocr=None, doc_date="2026-01-01"):
    pid = conn.execute(
        "SELECT person_id FROM person WHERE slug=?", (slug,)
    ).fetchone()["person_id"]
    cur = conn.execute(
        "INSERT INTO document (sha256, person_id, doc_date, source_path, ocr_text, "
        "ingested_at) VALUES (?, ?, ?, ?, ?, ?)",
        (f"sha-{slug}-{conn.total_changes}", pid, doc_date, "aa/x.pdf", ocr,
         "2026-01-01T00:00:00"),
    )
    conn.commit()
    return cur.lastrowid


@pytest.fixture()
def seeded(tmp_path):
    """Migrated DB with two people and a spread of records for jane, plus one
    same-analyte lab for john to prove person isolation."""
    conn = db.connect(tmp_path / "q.db")
    db.migrate(conn)
    persons.add_person(conn, "jane-doe", "Jane Doe")
    persons.add_person(conn, "john-doe", "John Doe")
    d = dedup.load_dictionary(DICT_PATH)

    doc1 = _doc(conn, "jane-doe", ocr="Fasting glucose and total cholesterol panel")
    dedup.commit_extraction(conn, doc1, {
        "lab_result": [
            {"test_name": "HbA1c", "collected_at": "2024-01-01", "value_num": 5.5, "unit": "%"},
            {"test_name": "Hemoglobin A1c", "collected_at": "2025-01-01", "value_num": 6.0, "unit": "%"},
            {"test_name": "A1c", "collected_at": "2026-01-01", "value_num": 6.5, "unit": "%"},
            {"test_name": "LDL", "collected_at": "2025-01-01", "value_num": 130, "unit": "mg/dL", "flag": "H"},
        ],
        "medication": [
            {"name": "Metformin", "dose": "500mg", "started_on": "2024-02-01"},
            {"name": "Lisinopril", "dose": "10mg", "started_on": "2023-01-01",
             "ended_on": "2024-06-01", "status": "discontinued"},
        ],
        "procedure": [
            {"name": "Colonoscopy", "performed_on": "2025-03-15", "outcome": "normal"},
        ],
        "appointment": [
            {"scheduled_for": "2026-02-01", "provider": "Dr. Smith",
             "specialty": "Endocrinology", "reason": "diabetes follow-up"},
        ],
        "observation": [
            {"obs_type": "blood_pressure", "observed_at": "2025-07-01",
             "key": "systolic", "value_num": 128, "unit": "mmHg"},
        ],
        "condition": [
            {"name": "Anemia", "status": "resolved", "onset_on": "2022-05-01",
             "resolved_on": "2023-08-01"},
            {"name": "Breast Cancer", "status": "family-history", "relation": "mother",
             "onset_on": "2001-01-01"},
        ],
        "allergy": [
            {"substance": "Penicillin", "reaction": "rash", "noted_on": "2010-01-01"},
        ],
    }, d)

    doc2 = _doc(conn, "john-doe", ocr="unrelated note")
    dedup.commit_extraction(conn, doc2, {
        "lab_result": [
            {"test_name": "HbA1c", "collected_at": "2026-01-01", "value_num": 9.0, "unit": "%"},
        ],
    }, d)
    yield conn
    conn.close()


# --- structured: labs ---------------------------------------------------------

def test_query_labs_all(seeded):
    rows = query.query_labs(seeded, "jane-doe")
    assert len(rows) == 4
    # oldest first
    assert [r["collected_at"] for r in rows] == sorted(r["collected_at"] for r in rows)


def test_query_labs_test_filter_is_dictionary_normalized(seeded):
    d = dedup.load_dictionary(DICT_PATH)
    rows = query.query_labs(seeded, "jane-doe", test="A1c", dictionary=d)
    # three hba1c spellings collapse under the dictionary; LDL excluded
    assert len(rows) == 3
    assert {r["test_name"] for r in rows} == {"HbA1c", "Hemoglobin A1c", "A1c"}


def test_query_labs_since_edge_is_inclusive(seeded):
    rows = query.query_labs(seeded, "jane-doe", since="2025-01-01")
    assert all(r["collected_at"] >= "2025-01-01" for r in rows)
    assert any(r["collected_at"] == "2025-01-01" for r in rows)  # boundary kept
    assert not any(r["collected_at"] == "2024-01-01" for r in rows)


def test_query_labs_isolated_per_person(seeded):
    assert all(r["value_num"] != 9.0 for r in query.query_labs(seeded, "jane-doe"))
    john = query.query_labs(seeded, "john-doe")
    assert len(john) == 1 and john[0]["value_num"] == 9.0


# --- structured: meds ---------------------------------------------------------

def test_query_meds_all_vs_active(seeded):
    assert len(query.query_meds(seeded, "jane-doe")) == 2
    active = query.query_meds(seeded, "jane-doe", active=True)
    assert [m["name"] for m in active] == ["Metformin"]  # Lisinopril is ended


NOW = datetime(2026, 8, 2, 9, 30)  # fixed "today" so the currency tests never age out


@pytest.mark.parametrize("row, expected", [
    ({"status": None, "ended_on": None}, True),          # nothing set -> current
    ({"status": "", "ended_on": None}, True),            # blank status -> current
    ({"status": "active", "ended_on": None}, True),      # explicitly active
    ({"status": "prn", "ended_on": None}, True),         # prn is not terminal
    ({"status": "completed", "ended_on": None}, False),  # issue #21: terminal, no end date
    ({"status": "Stopped", "ended_on": None}, False),    # case-insensitive
    ({"status": " discontinued ", "ended_on": None}, False),  # trimmed
    ({"status": None, "ended_on": "2024-06-01"}, False), # explicit end date
    # issue #57: a past end date ends the course whatever the status label says.
    ({"status": "active", "ended_on": "2024-01-10"}, False),
    ({"status": "Active", "ended_on": "2026-08-01"}, False),   # ended yesterday
    ({"status": "active", "ended_on": "2026-08-02"}, True),    # ends today -> still on it
    ({"status": "active", "ended_on": "2026-09-04"}, True),    # prior auth through a future date
    ({"status": "active", "ended_on": "2026-08-02T00:00:00"}, True),  # timestamp form
    # Partial precision widens to the END of the period it names, so a course only
    # expires once every day it could have covered is past.
    ({"status": "active", "ended_on": "2024"}, False),         # 2024-12-31 < today
    ({"status": "active", "ended_on": "2026"}, True),          # 2026-12-31 >= today
    ({"status": "active", "ended_on": "2026-07"}, False),      # 2026-07-31 < today
    ({"status": "active", "ended_on": "2026-08"}, True),       # 2026-08-31 >= today
    ({"status": "active", "ended_on": "2026-02"}, False),      # leap-month end, still past
    ({"status": "active", "ended_on": "not-a-date"}, True),    # unparseable -> active wins
    ({"status": None, "ended_on": "not-a-date"}, False),       # ...but only for 'active'
    # Unchanged: a bare end date still ends the course; only status='active' overrides
    # a future one.
    ({"status": None, "ended_on": "2026-09-04"}, False),
    ({"status": "completed", "ended_on": "2026-09-04"}, False),
])
def test_med_is_current(row, expected):
    assert query.med_is_current(row, now=NOW) is expected


def test_med_is_current_defaults_to_the_real_clock():
    """Without an injected ``now`` the currency test uses today (the CLI/MCP path)."""
    past = (datetime.now() - timedelta(days=1)).date().isoformat()
    future = (datetime.now() + timedelta(days=365)).date().isoformat()
    assert query.med_is_current({"status": "active", "ended_on": past}) is False
    assert query.med_is_current({"status": "active", "ended_on": future}) is True


def test_query_meds_active_excludes_terminal_status_without_end(seeded):
    """A med with a terminal status but no ended_on is not 'active' (issue #21)."""
    doc = _doc(seeded, "jane-doe")
    dedup.commit_extraction(seeded, doc, {
        "medication": [
            {"name": "Amoxicillin", "dose": "250mg", "frequency": "BID",
             "started_on": "2026-05-20", "status": "completed"},
        ],
    }, dedup.load_dictionary(DICT_PATH))
    names_all = {m["name"] for m in query.query_meds(seeded, "jane-doe")}
    assert "Amoxicillin" in names_all  # still listed by the unfiltered query
    names_active = {m["name"] for m in query.query_meds(seeded, "jane-doe", active=True)}
    assert "Amoxicillin" not in names_active


def test_query_meds_active_excludes_expired_course_labelled_active(seeded):
    """A finished course transcribed with status='active' is not current (issue #57);
    a still-open one with a future end date is."""
    doc = _doc(seeded, "jane-doe")
    dedup.commit_extraction(seeded, doc, {
        "medication": [
            {"name": "Amoxicillin", "dose": "500mg", "frequency": "TID",
             "started_on": "2024-01-01", "ended_on": "2024-01-10", "status": "active"},
            {"name": "Skyrizi", "dose": "150mg", "frequency": "q8w",
             "started_on": "2025-09-04", "ended_on": "2026-09-04", "status": "active"},
        ],
    }, dedup.load_dictionary(DICT_PATH))
    names_all = {m["name"] for m in query.query_meds(seeded, "jane-doe")}
    assert {"Amoxicillin", "Skyrizi"} <= names_all  # both still listed unfiltered
    active = {m["name"] for m in query.query_meds(seeded, "jane-doe", active=True, now=NOW)}
    assert "Amoxicillin" not in active
    assert "Skyrizi" in active


# --- structured: timeline -----------------------------------------------------

def test_timeline_merge_ordering_and_since(seeded):
    events = query.query_timeline(seeded, "jane-doe")
    dates = [e["date"] for e in events]
    assert dates == sorted(dates)
    types = {e["type"] for e in events}
    assert {"lab", "med-start", "med-stop", "procedure", "appointment", "observation"} <= types
    # med start + stop both present for the ended med
    assert sum(1 for e in events if e["type"] == "med-stop") == 1
    since = query.query_timeline(seeded, "jane-doe", since="2026-01-01")
    assert all(e["date"] >= "2026-01-01" for e in since)
    assert len(since) < len(events)


def test_timeline_includes_conditions_and_allergies(seeded):
    """Conditions contribute up to two events (onset + resolution) and allergies one,
    so the journal keeps them after they left `observation` (issue #63)."""
    events = query.query_timeline(seeded, "jane-doe")
    by_type = {e["type"]: e for e in events}
    assert by_type["condition"]["date"] == "2022-05-01"
    assert by_type["condition"]["summary"] == "Anemia (resolved)"
    assert by_type["condition-resolved"]["date"] == "2023-08-01"
    assert by_type["condition-resolved"]["summary"] == "resolved Anemia"
    assert by_type["allergy"]["summary"] == "Penicillin allergy"


def test_timeline_excludes_family_history(seeded):
    """A relative's onset date is not an event in this patient's chronology."""
    events = query.query_timeline(seeded, "jane-doe")
    assert not any("Breast Cancer" in e["summary"] for e in events)
    assert not any(e["date"] == "2001-01-01" for e in events)


def test_timeline_surfaces_functional_observations(seeded):
    """Issue #132: a caregiver-observed functional fact needs no new rendering — it rides
    the generic observation loop, dated, with its `obs_type key` detail and its value."""
    doc = _doc(seeded, "jane-doe", ocr="ledger transcription")
    dedup.commit_extraction(seeded, doc, {"observation": [
        {"obs_type": "functional", "key": "financial_self_management",
         "observed_at": "2025-07-20",
         "value_text": "running balance column stops mid-page"},
    ]})
    events = query.query_timeline(seeded, "jane-doe")
    hit = [e for e in events if "functional" in e["summary"]]
    assert len(hit) == 1
    assert hit[0]["date"] == "2025-07-20"
    assert hit[0]["type"] == "observation"
    assert "financial_self_management" in hit[0]["summary"]
    assert "running balance column stops mid-page" in hit[0]["summary"]


def test_timeline_carries_provenance(seeded):
    events = query.query_timeline(seeded, "jane-doe")
    assert all("document_id" in e for e in events)
    assert any(e["document_id"] is not None for e in events)


def test_timeline_stamps_attestation_only_on_attested_events(seeded):
    """Issue #110: provenance travels with the event, but only when the row carries it -
    a document-sourced record's event shape is unchanged, key for key."""
    from pemr import attestations

    before = query.query_timeline(seeded, "jane-doe")
    assert all(
        "attested_by" not in e and "attested_on" not in e for e in before
    )

    attestations.assert_record(
        seeded, "medication", "jane-doe",
        {"name": "Amlodipine", "started_on": "2026-03-01"},
        attributed_to="Aunt Ada", attested_on="2026-03-02", apply=True,
    )
    after = query.query_timeline(seeded, "jane-doe")
    attested = [e for e in after if "attested_by" in e]
    assert [(e["attested_by"], e["attested_on"], e["document_id"]) for e in attested] == [
        ("Aunt Ada", "2026-03-02", None)
    ]
    # Every pre-existing event is untouched.
    assert [e for e in after if "attested_by" not in e] == before


def test_timeline_with_identity_stamps_family_and_row_keys(seeded):
    """Issues #109/#114: `render_journal` needs each event's family identity to apply
    the curation overlay and its **row** id to apply a row-scoped verdict; an event
    otherwise carries neither."""
    plain = query.query_timeline(seeded, "jane-doe")
    tagged = query.query_timeline(seeded, "jane-doe", with_identity=True)

    assert len(tagged) == len(plain)
    assert all({"record_type", "dedup_base", "record_id"} <= set(e) for e in tagged)
    assert all(e["dedup_base"] for e in tagged)
    assert all(isinstance(e["record_id"], int) and e["record_id"] > 0 for e in tagged)
    assert {e["record_type"] for e in tagged} <= set(dedup.KNOWN_TYPES)
    # Same events, same order, same values - identity is purely additive.
    identity = ("record_type", "dedup_base", "record_id")
    assert [
        {k: v for k, v in e.items() if k not in identity}
        for e in tagged
    ] == plain


def test_timeline_default_shape_is_unchanged(seeded):
    """Default-off is load-bearing: `dedup_base` is an INTERNAL_COLUMN, deliberately
    absent from the CLI `--json` and MCP payloads."""
    for e in query.query_timeline(seeded, "jane-doe"):
        assert set(e) == {"date", "type", "summary", "document_id"}


def test_timeline_summaries_are_ascii_safe(seeded):
    """§4 cp1252/cp437 console lesson: human-table summaries must be ASCII-only, or
    they crash on a non-UTF-8 Windows console (regression for the em-dash bounce —
    procedure ' - outcome' and 'provider - reason' joins used a U+2014 em-dash)."""
    events = query.query_timeline(seeded, "jane-doe")
    proc = next(e for e in events if e["type"] == "procedure")
    appt = next(e for e in events if e["type"] == "appointment")
    assert " - normal" in proc["summary"]          # ascii hyphen, not em-dash
    assert "Dr. Smith Endocrinology - diabetes follow-up" == appt["summary"]
    for e in events:
        assert e["summary"].isascii(), f"non-ASCII summary would crash cp437: {e['summary']!r}"
        e["summary"].encode("cp437")  # raises UnicodeEncodeError if not console-safe


# --- full-text search ---------------------------------------------------------

def test_find_matches_ocr_and_records(seeded):
    hits = query.find(seeded, "jane-doe", "cholesterol")
    tables = {h["source_table"] for h in hits}
    assert "document" in tables  # ocr_text hit
    assert all("snippet" in h and "document_id" in h for h in hits)


def test_find_matches_record_field(seeded):
    hits = query.find(seeded, "jane-doe", "metformin")
    assert any(h["source_table"] == "medication" for h in hits)


def test_find_is_person_scoped(seeded):
    # john's HbA1c/ocr must never surface under jane
    assert query.find(seeded, "jane-doe", "unrelated") == []
    assert query.find(seeded, "john-doe", "unrelated")


def test_find_carries_person_slug(seeded):
    # every hit names its owning person, scoped or household-wide
    for h in query.find(seeded, "jane-doe", "cholesterol"):
        assert h["person"] == "jane-doe"


def test_find_household_wide_when_slug_none(seeded):
    # slug=None searches all people; jane can't see john's 'unrelated', the
    # household can, and the hit is attributed to john
    assert query.find(seeded, "jane-doe", "unrelated") == []
    hits = query.find(seeded, None, "unrelated")
    assert hits and all(h["person"] == "john-doe" for h in hits)


def test_find_ignores_fts_operators_safely(seeded):
    # punctuation / bare operators must not raise or change meaning
    assert isinstance(query.find(seeded, "jane-doe", 'cholesterol AND ("'), list)
    assert query.find(seeded, "jane-doe", "!!!") == []


def test_find_trigger_syncs_on_insert(seeded):
    # a brand-new record (committed after migrate) is searchable via the trigger
    hits = query.find(seeded, "jane-doe", "colonoscopy")
    assert any(h["source_table"] == "procedure" for h in hits)


def test_find_trigger_syncs_on_update(seeded):
    assert query.find(seeded, "jane-doe", "cholesterol")
    doc_id = seeded.execute(
        "SELECT document_id FROM document WHERE ocr_text LIKE '%cholesterol%'"
    ).fetchone()["document_id"]
    with seeded:
        seeded.execute(
            "UPDATE document SET ocr_text='thyroid panel' WHERE document_id=?", (doc_id,)
        )
    assert not any(
        h["source_table"] == "document" for h in query.find(seeded, "jane-doe", "cholesterol")
    )
    assert any(
        h["source_table"] == "document" for h in query.find(seeded, "jane-doe", "thyroid")
    )


def test_fts_backfill_on_migrate(tmp_path):
    """A DB migrated before 003 gets its existing rows indexed by 003's backfill —
    no re-ingest required."""
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    real = db.DEFAULT_MIGRATIONS_DIR
    for name in ("001_init.sql", "002_conflict.sql"):
        shutil.copy(real / name, mdir / name)
    conn = db.connect(tmp_path / "b.db")
    db.migrate(conn, mdir)
    persons.add_person(conn, "jane-doe", "Jane Doe")
    _doc(conn, "jane-doe", ocr="pre-existing cholesterol note")  # inserted pre-003

    shutil.copy(real / "003_fts.sql", mdir / "003_fts.sql")
    assert db.migrate(conn, mdir) == ["003_fts.sql"]
    hits = query.find(conn, "jane-doe", "cholesterol")
    assert any(h["source_table"] == "document" for h in hits)
    conn.close()


# --- trends -------------------------------------------------------------------

def test_trends_math_and_slope(seeded):
    d = dedup.load_dictionary(DICT_PATH)
    t = query.trends(seeded, "jane-doe", "hba1c", dictionary=d)
    assert t["count"] == 3
    assert t["min"] == 5.5 and t["max"] == 6.5
    assert t["latest"] == 6.5 and t["latest_at"] == "2026-01-01"
    assert t["unit"] == "%"
    assert t["slope_per_day"] is not None and t["slope_per_day"] > 0  # rising


def test_trends_dictionary_normalization(seeded):
    d = dedup.load_dictionary(DICT_PATH)
    # querying with a synonym still finds the canonical analyte's points
    assert query.trends(seeded, "jane-doe", "Hemoglobin A1c", dictionary=d)["count"] == 3


def test_trends_single_point_degrades_slope(seeded):
    d = dedup.load_dictionary(DICT_PATH)
    t = query.trends(seeded, "jane-doe", "ldl", dictionary=d)
    assert t["count"] == 1
    assert t["slope_per_day"] is None  # <2 points -> graceful degrade


def test_trends_unknown_analyte_is_empty(seeded):
    t = query.trends(seeded, "jane-doe", "nonesuch")
    assert t["count"] == 0 and t["slope_per_day"] is None


# --- assay split: labs lists the family, trends charts one assay (issue #71) ---

@pytest.fixture()
def albumin_assays(seeded):
    """Two albumin assays off two draws: the CMP's bare `Albumin` and the SPEP
    electrophoresis fraction `Albumin (SPEP)`."""
    d = dedup.load_dictionary(DICT_PATH)
    doc = _doc(seeded, "jane-doe", ocr="CMP + SPEP")
    dedup.commit_extraction(seeded, doc, {"lab_result": [
        {"test_name": "Albumin", "collected_at": "2025-01-01", "value_num": 4.2,
         "unit": "g/dL"},
        {"test_name": "Albumin (SPEP)", "collected_at": "2025-01-01", "value_num": 3.6,
         "unit": "g/dL"},
        {"test_name": "Albumin", "collected_at": "2026-01-01", "value_num": 4.0,
         "unit": "g/dL"},
    ]}, d)
    return seeded


def test_labs_lists_the_whole_analyte_family(albumin_assays):
    # A listing is coarse on purpose: `--test albumin` shows every albumin filed.
    d = dedup.load_dictionary(DICT_PATH)
    rows = query.query_labs(albumin_assays, "jane-doe", test="albumin", dictionary=d)
    assert sorted(r["test_name"] for r in rows) == \
        ["Albumin", "Albumin", "Albumin (SPEP)"]


def test_trends_charts_one_assay_and_discloses_the_others(albumin_assays):
    # A series that interleaved the CMP and SPEP numbers would be a wrong chart, so
    # trends splits them -- but the excluded rows are reported, never silently dropped.
    d = dedup.load_dictionary(DICT_PATH)
    t = query.trends(albumin_assays, "jane-doe", "albumin", dictionary=d)
    assert t["test"] == "albumin"
    assert t["count"] == 2 and t["min"] == 4.0 and t["max"] == 4.2
    assert t["other_assays"] == ["albumin (spep)"] and t["other_assay_count"] == 1

    spep = query.trends(albumin_assays, "jane-doe", "Albumin (SPEP)", dictionary=d)
    assert spep["test"] == "albumin (spep)"
    assert spep["count"] == 1 and spep["latest"] == 3.6
    assert spep["other_assays"] == ["albumin"] and spep["other_assay_count"] == 2


def test_trends_discloses_other_assays_even_with_no_matching_points(albumin_assays):
    # The empty-series path must still name the assay that does have data, or the user
    # sees "no numeric results" for an analyte that is plainly in the record.
    d = dedup.load_dictionary(DICT_PATH)
    t = query.trends(albumin_assays, "jane-doe", "Albumin (Nephelometry)", dictionary=d)
    assert t["count"] == 0
    assert t["other_assays"] == ["albumin", "albumin (spep)"]
    assert t["other_assay_count"] == 3


def test_trends_unrelated_analytes_are_not_reported_as_other_assays(albumin_assays):
    d = dedup.load_dictionary(DICT_PATH)
    t = query.trends(albumin_assays, "jane-doe", "hba1c", dictionary=d)
    assert t["count"] == 3 and t["other_assays"] == [] and t["other_assay_count"] == 0


def test_trends_disclosed_token_round_trips_over_an_underscore_canonical(seeded):
    """The disclosure is only worth anything if its token can be fed straight back in.
    A canonical value may contain underscores (`vitamin_d_25oh`) that `_collapse` turns
    back into spaces on the way in, so a strict comparison made the printed token a
    dead end -- zero rows *and* zero disclosure. `albumin` (the fixture above) has no
    underscore, which is why the suite could not see this."""
    d = dedup.load_dictionary(DICT_PATH)
    doc = _doc(seeded, "jane-doe", ocr="vitamin D panel")
    dedup.commit_extraction(seeded, doc, {"lab_result": [
        {"test_name": "Vitamin D", "collected_at": "2025-01-01", "value_num": 31},
        {"test_name": "Vitamin D", "collected_at": "2026-01-01", "value_num": 28},
        {"test_name": "Vitamin D (25-OH)", "collected_at": "2025-01-01", "value_num": 44},
    ]}, d)

    t = query.trends(seeded, "jane-doe", "vitamin d", dictionary=d)
    assert t["count"] == 2 and t["other_assay_count"] == 1
    token = t["other_assays"][0]
    assert "_" in token                       # the spelling that used to be a dead end

    back = query.trends(seeded, "jane-doe", token, dictionary=d)
    assert back["count"] == 1 and back["latest"] == 44
    assert back["test"] == token              # echoed as disclosed, not as re-derived
    assert back["other_assays"] == [t["test"]] and back["other_assay_count"] == 2


def _insert_lab(conn, slug, **cols):
    """Insert a lab_result row directly (bypassing dedup) to simulate the #20
    same-timestamp duplicate-row state. Returns the new lab_result_id."""
    pid = conn.execute(
        "SELECT person_id FROM person WHERE slug=?", (slug,)
    ).fetchone()["person_id"]
    cols.setdefault("dedup_key", f"k-{uuid.uuid4()}")
    keys = ["person_id", *cols]
    vals = [pid, *cols.values()]
    placeholders = ", ".join("?" for _ in keys)
    cur = conn.execute(
        f"INSERT INTO lab_result ({', '.join(keys)}) VALUES ({placeholders})",
        vals,
    )
    conn.commit()
    return cur.lastrowid


def test_trends_same_timestamp_tie_is_deterministic(seeded):
    d = dedup.load_dictionary(DICT_PATH)
    # Two rows with the *same* collected_at but different values (the #20 symptom).
    # Latest must be the greater lab_result_id (most-recently-ingested), and the
    # tie must be disclosed via latest_tie.
    _insert_lab(seeded, "jane-doe", test_name="Glucose",
                value_num=5.0, unit="mmol/L", collected_at="2026-07-17T09:00:00")
    later_id = _insert_lab(seeded, "jane-doe", test_name="Glucose",
                           value_num=5.2, unit="mmol/L",
                           collected_at="2026-07-17T09:00:00")
    t = query.trends(seeded, "jane-doe", "glucose", dictionary=d)
    assert t["count"] == 2
    assert t["latest"] == 5.2  # higher lab_result_id wins the tie
    assert t["latest_at"] == "2026-07-17T09:00:00"
    assert t["latest_tie"] == 2
    assert later_id  # sanity: the later insert got a greater PK
    assert t["slope_per_day"] is None  # one distinct date


def test_trends_distinct_intraday_times_are_not_a_tie(seeded):
    d = dedup.load_dictionary(DICT_PATH)
    # Genuinely ordered intraday times must NOT trip the tie note.
    _insert_lab(seeded, "jane-doe", test_name="Glucose",
                value_num=5.0, unit="mmol/L", collected_at="2026-07-17T09:00:00")
    _insert_lab(seeded, "jane-doe", test_name="Glucose",
                value_num=5.4, unit="mmol/L", collected_at="2026-07-17T14:00:00")
    t = query.trends(seeded, "jane-doe", "glucose", dictionary=d)
    assert t["latest"] == 5.4 and t["latest_tie"] == 1


def test_query_labs_same_date_siblings_order_by_row_id(seeded):
    """`--keep both` (#58) admits a second draw under the same date, so
    `collected_at, test_name` no longer totally orders lab rows. Row id breaks the
    tie: the later-admitted sibling reads as the later point."""
    first = _insert_lab(seeded, "jane-doe", test_name="Glucose", value_num=95.0,
                        unit="mg/dL", collected_at="2024-04-01")
    second = _insert_lab(seeded, "jane-doe", test_name="Glucose", value_num=148.0,
                         unit="mg/dL", collected_at="2024-04-01")
    same_day = [
        r for r in query.query_labs(seeded, "jane-doe")
        if r["collected_at"] == "2024-04-01"
    ]
    assert [r["lab_result_id"] for r in same_day] == [first, second]
    assert [r["value_num"] for r in same_day] == [95.0, 148.0]


# --- error surfaces -----------------------------------------------------------

def test_unknown_person_raises(seeded):
    with pytest.raises(query.PersonNotFoundError):
        query.query_labs(seeded, "nobody")
    with pytest.raises(query.PersonNotFoundError):
        query.trends(seeded, "nobody", "hba1c")
    with pytest.raises(query.PersonNotFoundError):
        query.find(seeded, "nobody", "cholesterol")

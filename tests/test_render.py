"""Phase 4 render layer: master summary, appointment brief, journal.

Pure, read-only Markdown renders over seeded fixtures -- section presence/empty-state,
abnormal-lab selection, latest-vitals pick, brief scoping, journal ordering + provenance
footnotes, ASCII output (cp1252/cp437 console lesson), and the read-only guarantee
(no row-count change after any render)."""

from pathlib import Path

import pytest

from pemr import db, dedup, persons, query, render

DICT_PATH = Path(__file__).resolve().parent.parent / "data" / "dictionary.example.toml"

_TABLES = (
    "person", "document", "lab_result", "medication", "procedure",
    "appointment", "observation", "conflict",
)


def _doc(conn, slug, ocr=None, doc_date="2026-01-01", category=None, provider=None):
    pid = conn.execute(
        "SELECT person_id FROM person WHERE slug=?", (slug,)
    ).fetchone()["person_id"]
    cur = conn.execute(
        "INSERT INTO document (sha256, person_id, doc_date, category, provider, "
        "source_path, ocr_text, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (f"sha-{slug}-{conn.total_changes}", pid, doc_date, category, provider,
         "aa/x.pdf", ocr, "2026-01-01T00:00:00"),
    )
    conn.commit()
    return cur.lastrowid


def _row_counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in _TABLES}


@pytest.fixture()
def seeded(tmp_path):
    """Migrated file DB with jane (a full spread) + john (isolation), returning the path."""
    conn = db.connect(tmp_path / "r.db")
    db.migrate(conn)
    persons.add_person(conn, "jane-doe", "Jane Doe", dob="1980-01-01")
    persons.add_person(conn, "john-doe", "John Doe")
    d = dedup.load_dictionary(DICT_PATH)

    doc = _doc(conn, "jane-doe", ocr="lab panel", category="labs", provider="Quest")
    dedup.commit_extraction(conn, doc, {
        "lab_result": [
            {"test_name": "HbA1c", "collected_at": "2024-01-01", "value_num": 5.5,
             "unit": "%", "ref_low": 4.0, "ref_high": 6.0},                       # normal
            {"test_name": "LDL", "collected_at": "2025-01-01", "value_num": 130,
             "unit": "mg/dL", "flag": "H"},                                       # abnormal (flag)
            {"test_name": "Glucose, fasting", "collected_at": "2026-01-01",
             "value_num": 200, "unit": "mg/dL", "ref_high": 100},                 # abnormal (ref)
            {"test_name": "TSH", "collected_at": "2023-01-01", "value_num": 2.0,
             "unit": "mIU/L"},                                                    # normal (no ref/flag)
        ],
        "medication": [
            {"name": "Metformin", "dose": "500mg", "frequency": "BID",
             "started_on": "2024-02-01"},                                         # active
            {"name": "Lisinopril", "dose": "10mg", "started_on": "2023-01-01",
             "ended_on": "2024-06-01", "status": "discontinued"},                 # ended
        ],
        "procedure": [
            {"name": "Colonoscopy", "performed_on": "2025-03-15", "outcome": "normal"},
        ],
        "appointment": [
            {"scheduled_for": "2099-02-01", "provider": "Dr. Smith",
             "specialty": "Endocrinology", "reason": "diabetes follow-up"},       # upcoming/open
            {"scheduled_for": "2020-01-01", "provider": "Dr. Past",
             "specialty": "GP", "reason": "physical", "summary": "all clear"},    # closed
            {"scheduled_for": "2021-01-01", "provider": "Dr. Open",
             "specialty": "Cardiology", "reason": "murmur"},                      # past-but-open
        ],
        "observation": [
            {"obs_type": "vital", "key": "blood_pressure", "observed_at": "2025-07-01",
             "value_num": 128, "unit": "mmHg"},
            {"obs_type": "vital", "key": "blood_pressure", "observed_at": "2026-01-01",
             "value_num": 118, "unit": "mmHg"},                                   # latest wins
            {"obs_type": "vital", "key": "weight", "observed_at": "2026-01-01",
             "value_num": 80, "unit": "kg"},
            {"obs_type": "condition", "key": "Type 2 Diabetes", "observed_at": "2024-01-01"},
            {"obs_type": "allergy", "key": "Penicillin", "value_text": "rash",
             "observed_at": "2010-01-01"},
            {"obs_type": "order", "key": "cervical collar", "value_text": "Dr. Smith, ortho",
             "observed_at": "2026-02-01"},                                        # DME order
            {"obs_type": "order", "key": "outpatient physical therapy"},          # bare item, no detail
        ],
    }, d)

    docj = _doc(conn, "john-doe")
    dedup.commit_extraction(conn, docj, {
        "lab_result": [
            {"test_name": "LDL", "collected_at": "2026-01-01", "value_num": 999,
             "unit": "mg/dL", "flag": "H"},
        ],
    }, d)
    yield conn
    conn.close()


def _upcoming_appt_id(conn):
    return conn.execute(
        "SELECT appointment_id FROM appointment WHERE provider='Dr. Smith'"
    ).fetchone()["appointment_id"]


# --- master summary -----------------------------------------------------------

def test_summary_header_is_self_identifying(seeded):
    md = render.render_summary(seeded, "jane-doe", dictionary=dedup.load_dictionary(DICT_PATH))
    assert "# Master Summary: Jane Doe" in md
    assert "DOB: 1980-01-01" in md
    assert "Generated:" in md
    # source row counts so a stale export identifies itself
    assert "labs=4" in md and "medications=2" in md and "observations=7" in md


def test_summary_active_meds_only(seeded):
    md = render.render_summary(seeded, "jane-doe")
    assert "Metformin 500mg BID (since 2024-02-01)" in md
    assert "Lisinopril" not in md  # ended med excluded from the active list


def test_summary_conditions_and_allergies(seeded):
    md = render.render_summary(seeded, "jane-doe")
    assert "## Conditions" in md and "Type 2 Diabetes" in md
    assert "## Allergies" in md and "Penicillin - rash" in md


def test_summary_orders_and_referrals(seeded):
    """Non-medication `order` observations render under a dedicated section: item name
    with optional prescriber/instructions detail, placed after Allergies before Vitals."""
    md = render.render_summary(seeded, "jane-doe")
    section = md.split("## Orders & Referrals")[1].split("##")[0]
    assert "cervical collar - Dr. Smith, ortho" in section   # key - value_text
    assert "outpatient physical therapy" in section          # bare item, no trailing detail
    assert "outpatient physical therapy - " not in section   # no dangling separator
    # placement: after Allergies, before Latest Vitals (clinical-status block stays together)
    assert md.index("## Allergies") < md.index("## Orders & Referrals") < md.index("## Latest Vitals")


def test_summary_latest_vitals_pick(seeded):
    md = render.render_summary(seeded, "jane-doe", dictionary=dedup.load_dictionary(DICT_PATH))
    assert "blood_pressure: 118.0 mmHg" in md   # most recent per key
    assert "128.0 mmHg" not in md               # older reading dropped
    assert "weight: 80.0 kg" in md


def test_summary_abnormal_lab_selection(seeded):
    md = render.render_summary(seeded, "jane-doe")
    section = md.split("## Recent Abnormal Labs")[1].split("##")[0]
    assert "LDL" in section          # flagged abnormal
    assert "Glucose" in section      # outside ref_high
    assert "HbA1c" not in section    # within ref range
    assert "TSH" not in section      # no ref, no flag
    # newest first: Glucose (2026) precedes LDL (2025)
    assert section.index("Glucose") < section.index("LDL")


def test_summary_same_date_lab_siblings_order_by_row_id(seeded):
    """A `--keep both` sibling (#58) shares its date with the row it was admitted
    beside, so newest-first ordering needs a row-id tiebreak to stay deterministic:
    the later-admitted row reads as the later point."""
    d = dedup.load_dictionary(DICT_PATH)
    dedup.commit_extraction(seeded, _doc(seeded, "jane-doe"), {"lab_result": [
        {"test_name": "Glucose, fasting", "collected_at": "2026-01-01",
         "value_num": 320, "unit": "mg/dL", "ref_high": 100},
    ]}, d)
    conflict_id = dedup.list_conflicts(seeded)[0]["conflict_id"]
    admitted = dedup.resolve_conflict(seeded, conflict_id, keep="both")
    assert admitted.occurrence == 1

    md = render.render_summary(seeded, "jane-doe", dictionary=d)
    section = md.split("## Recent Abnormal Labs")[1].split("##")[0]
    assert section.index("320.0") < section.index("200.0")


def test_summary_upcoming_and_open_appointments(seeded):
    md = render.render_summary(seeded, "jane-doe")
    section = md.split("## Upcoming / Open Appointments")[1]
    assert "Dr. Smith" in section    # upcoming
    assert "Dr. Open" in section     # past but no summary -> open
    assert "Dr. Past" not in section  # past + documented -> closed


def test_summary_empty_sections_are_explicit(seeded):
    """A person with no records still renders every section, each with an explicit
    empty-state note (an absent section must not read as an overlooked one)."""
    md = render.render_summary(seeded, "john-doe")
    assert "## Active Medications" in md and "_none recorded_" in md
    assert "## Conditions" in md
    assert "## Orders & Referrals" in md
    order_section = md.split("## Orders & Referrals")[1].split("##")[0]
    assert "_none recorded_" in order_section  # no orders -> explicit empty state
    assert "## Recent Abnormal Labs" in md  # john has an abnormal LDL, so it appears
    assert "999" in md


# --- appointment brief --------------------------------------------------------

def test_brief_scopes_to_appointment_and_person(seeded):
    aid = _upcoming_appt_id(seeded)
    md = render.render_brief(seeded, aid)
    assert "# Appointment Brief: Jane Doe" in md
    assert "Provider: Dr. Smith" in md
    assert "Specialty: Endocrinology" in md
    assert "Reason: diabetes follow-up" in md
    assert "Metformin" in md         # jane's current med
    assert "999" not in md           # john's lab must never leak in


def test_brief_flags_abnormal_labs_with_marker_and_ref(seeded):
    """Recent Labs in the brief must not hide an abnormal value: each abnormal lab gets
    a marker + its reference interval, mirroring render summary; normal labs stay bare."""
    md = render.render_brief(seeded, _upcoming_appt_id(seeded))
    section = md.split("## Recent Labs")[1].split("##")[0]
    lines = {ln.split("  ")[1]: ln for ln in section.splitlines() if ln.startswith("- ")}
    # Glucose is abnormal by reference interval alone (no flag) -> marker + ref shown.
    assert "[!]" in lines["Glucose, fasting"]
    assert "(ref <= 100.0)" in lines["Glucose, fasting"]
    # LDL is flag-abnormal; still marked even though it carries no reference bounds.
    assert "[!]" in lines["LDL"]
    # Normal labs carry neither marker nor ref note.
    assert "[!]" not in lines["HbA1c"]
    assert "[!]" not in lines["TSH"]


def test_brief_recent_labs_keep_abnormals_within_the_limit(seeded):
    """A single draw can carry a 50+ analyte panel; `LIMIT N` over an alphabetical order
    used to return the first N names of that draw and drop every abnormal in it. Newest
    draw still comes first, but abnormal beats normal inside a draw."""
    doc = _doc(seeded, "jane-doe", ocr="big panel", category="labs")
    # One draw, newer than every seeded lab: 8 normals sorting before the abnormal by name.
    rows = [
        {"test_name": f"Analyte {c}", "collected_at": "2026-06-01T09:00",
         "value_num": 1.0, "ref_low": 0.0, "ref_high": 2.0}
        for c in "ABCDEFGH"
    ]
    rows.append({"test_name": "Zinc", "collected_at": "2026-06-01T09:00",
                 "value_num": 99.0, "ref_low": 0.0, "ref_high": 2.0})
    dedup.commit_extraction(seeded, doc, {"lab_result": rows})

    section = render.render_brief(
        seeded, _upcoming_appt_id(seeded), recent_labs=3
    ).split("## Recent Labs")[1].split("##")[0]
    names = [ln.split("  ")[1] for ln in section.splitlines() if ln.startswith("- ")]
    assert names[0] == "Zinc"                     # the only abnormal in the newest draw
    assert "[!]" in section
    assert names[1:] == ["Analyte A", "Analyte B"]  # then alphabetical within that draw
    # Recency is still the primary axis: nothing from an older draw displaces the newest.
    assert "Glucose, fasting" not in section


def test_brief_has_interaction_placeholder(seeded):
    md = render.render_brief(seeded, _upcoming_appt_id(seeded))
    assert "## Medication Interaction Review" in md
    assert "phase 5" in md and "pharmacist" in md


def test_brief_open_conflict_warning(seeded):
    # stage an open conflict for jane, then confirm the brief warns about it
    seeded.execute(
        "INSERT INTO conflict (record_type, dedup_key, person_id, existing_json, "
        "incoming_json, status, detected_at) VALUES "
        "('lab_result', 'k', (SELECT person_id FROM person WHERE slug='jane-doe'), "
        "'{}', '{}', 'open', '2026-01-01')"
    )
    seeded.commit()
    md = render.render_brief(seeded, _upcoming_appt_id(seeded))
    section = md.split("## Open Conflicts")[1].split("##")[0]
    assert "review-conflicts" in section


def test_brief_unknown_appointment_raises(seeded):
    with pytest.raises(render.AppointmentNotFoundError):
        render.render_brief(seeded, 99999)


# --- journal ------------------------------------------------------------------

def test_journal_grouped_and_ordered_with_footnotes(seeded):
    md = render.render_journal(seeded, "jane-doe")
    assert "# Journal: Jane Doe" in md
    # date headers appear in ascending order
    dates = [ln[3:] for ln in md.splitlines() if ln.startswith("## ")]
    assert dates == sorted(dates)
    # provenance footnote marker + definition both present (labs carry a document_id)
    assert "[^1]" in md
    assert "[^1]:" in md
    assert "document #" in md


def test_journal_empty_is_friendly(seeded):
    persons.add_person(seeded, "ghost-town", "Ghost Town")
    md = render.render_journal(seeded, "ghost-town")
    assert "# Journal: Ghost Town" in md
    assert "No dated events" in md


# --- cross-cutting: ASCII + read-only + errors --------------------------------

def test_all_renders_are_ascii_safe(seeded):
    d = dedup.load_dictionary(DICT_PATH)
    outs = [
        render.render_summary(seeded, "jane-doe", dictionary=d),
        render.render_brief(seeded, _upcoming_appt_id(seeded), dictionary=d),
        render.render_journal(seeded, "jane-doe"),
    ]
    for md in outs:
        assert md.isascii(), f"non-ASCII would crash a cp437 console: {md!r}"
        md.encode("cp437")  # raises UnicodeEncodeError if not console-safe


def test_renders_are_read_only(seeded):
    d = dedup.load_dictionary(DICT_PATH)
    before = _row_counts(seeded)
    render.render_summary(seeded, "jane-doe", dictionary=d)
    render.render_brief(seeded, _upcoming_appt_id(seeded), dictionary=d)
    render.render_journal(seeded, "jane-doe")
    assert _row_counts(seeded) == before  # no writes to any table


def test_unknown_person_raises(seeded):
    with pytest.raises(query.PersonNotFoundError):
        render.render_summary(seeded, "nobody")
    with pytest.raises(query.PersonNotFoundError):
        render.render_journal(seeded, "nobody")

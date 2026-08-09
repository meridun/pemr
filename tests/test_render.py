"""Phase 4 render layer: master summary, appointment brief, journal.

Pure, read-only Markdown renders over seeded fixtures -- section presence/empty-state,
abnormal-lab selection, latest-vitals pick, brief scoping, journal ordering + provenance
footnotes, ASCII output (cp1252/cp437 console lesson), and the read-only guarantee
(no row-count change after any render)."""

from datetime import datetime
from pathlib import Path

import pytest

from pemr import curation, db, dedup, persons, query, render

DICT_PATH = Path(__file__).resolve().parent.parent / "data" / "dictionary.example.toml"

_TABLES = (
    "person", "document", "lab_result", "medication", "procedure",
    "appointment", "observation", "condition", "allergy", "conflict",
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


def _stage_conflict(conn, slug, *, record_type="lab_result", status="open"):
    """Stage one conflict row for `slug` (the shape `dedup` writes on a key collision)."""
    cur = conn.execute(
        "INSERT INTO conflict (record_type, dedup_key, person_id, existing_json, "
        "incoming_json, status, resolution, detected_at, resolved_at) VALUES "
        "(?, 'k', (SELECT person_id FROM person WHERE slug = ?), '{}', '{}', ?, ?, "
        "'2026-01-01', ?)",
        (record_type, slug, status,
         "keep-incoming" if status == "resolved" else None,
         "2026-01-02" if status == "resolved" else None),
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
            {"obs_type": "order", "key": "cervical collar", "value_text": "Dr. Smith, ortho",
             "observed_at": "2026-02-01"},                                        # DME order
            {"obs_type": "order", "key": "outpatient physical therapy"},          # bare item, no detail
        ],
        "condition": [
            {"name": "Type 2 Diabetes", "status": "active", "onset_on": "2024-01-01",
             "note": "diet-controlled"},
            {"name": "Appendicitis", "status": "resolved", "onset_on": "2015-03-01",
             "resolved_on": "2015-03-08"},
            {"name": "Chickenpox", "status": "history"},
            {"name": "Type 2 Diabetes", "status": "family-history",
             "relation": "mother"},        # same disease as the active row: distinct subject
        ],
        "allergy": [
            {"substance": "Penicillin", "reaction": "rash", "noted_on": "2010-01-01"},
            {"substance": "Sulfa", "reaction": "anaphylaxis", "criticality": "high"},
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
    assert "labs=4" in md and "medications=2" in md and "observations=5" in md
    assert "conditions=4" in md and "allergies=2" in md


def test_summary_active_meds_only(seeded):
    md = render.render_summary(seeded, "jane-doe")
    assert "Metformin 500mg BID (since 2024-02-01)" in md
    assert "Lisinopril" not in md  # ended med excluded from the active list


def _seed_expired_active_course(conn):
    """A finished 2024 antibiotic course transcribed with status='active' (issue #57)."""
    dedup.commit_extraction(conn, _doc(conn, "jane-doe"), {
        "medication": [
            {"name": "Amoxicillin", "dose": "500mg", "frequency": "TID",
             "started_on": "2024-01-01", "ended_on": "2024-01-10", "status": "active"},
        ],
    }, dedup.load_dictionary(DICT_PATH))


def test_summary_excludes_expired_course_labelled_active(seeded):
    """Issue #57: a stale 'active' label must not keep a 2024 course in the summary --
    and `now` has to reach the currency test, not just the generated-at stamp."""
    _seed_expired_active_course(seeded)
    md = render.render_summary(seeded, "jane-doe", now=datetime(2026, 8, 2, 9, 30))
    assert "Amoxicillin" not in md
    assert "Metformin" in md  # control: genuinely current med still listed


def test_brief_excludes_expired_course_labelled_active(seeded):
    """Same for the brief -- the document actually handed to a clinician (issue #57)."""
    _seed_expired_active_course(seeded)
    md = render.render_brief(
        seeded, _upcoming_appt_id(seeded), now=datetime(2026, 8, 2, 9, 30)
    )
    assert "Amoxicillin" not in md
    assert "Metformin" in md


def test_summary_splits_conditions_by_status(seeded):
    """The single Conditions section became three, one per `condition.status` bucket
    (issue #63) -- an active problem, a past one and a relative's must never read alike."""
    md = render.render_summary(seeded, "jane-doe")
    active = md.split("## Active Problems")[1].split("\n## ")[0]
    past = md.split("## Past Medical History")[1].split("\n## ")[0]
    family = md.split("## Family History")[1].split("\n## ")[0]

    assert "- Type 2 Diabetes  (since 2024-01-01) - diet-controlled" in active
    assert "Appendicitis" not in active and "mother" not in active
    assert "- Appendicitis  (since 2015-03-01)  (resolved 2015-03-08)" in past
    assert "- Chickenpox" in past                 # history: no dates given
    assert "- mother: Type 2 Diabetes" in family  # the relative's, never the patient's


def test_summary_allergies_are_typed_rows_high_criticality_first(seeded):
    md = render.render_summary(seeded, "jane-doe")
    section = md.split("## Allergies")[1].split("\n## ")[0]
    assert "- Sulfa [HIGH] - anaphylaxis" in section
    assert "- Penicillin - rash  (noted 2010-01-01)" in section
    # criticality='high' outranks alphabetical order: it must survive a skim
    assert section.index("Sulfa") < section.index("Penicillin")


def test_summary_orders_and_referrals(seeded):
    """Non-medication `order` observations render under a dedicated section: item name
    with optional prescriber/instructions detail, placed after Allergies before Vitals."""
    md = render.render_summary(seeded, "jane-doe")
    section = md.split("## Orders & Referrals")[1].split("\n## ")[0]
    assert "- cervical collar - Dr. Smith, ortho  (ordered 2026-02-01)" in section
    assert "outpatient physical therapy" in section          # bare item, no detail, undated
    assert "outpatient physical therapy - " not in section   # no dangling separator
    assert "earlier" not in section                          # no group note on singletons
    # placement: after Allergies, before Latest Vitals (clinical-status block stays together)
    assert md.index("## Allergies") < md.index("## Orders & Referrals") < md.index("## Latest Vitals")


def _seed_orders(conn, rows):
    """Extra `obs_type='order'` observations on jane, committed as their own document
    (the shape a second document restating an order arrives in)."""
    dedup.commit_extraction(conn, _doc(conn, "jane-doe"), {
        "observation": [dict(r, obs_type="order") for r in rows],
    }, dedup.load_dictionary(DICT_PATH))


def _orders_section(conn):
    md = render.render_summary(conn, "jane-doe")
    return md.split("## Orders & Referrals")[1].split("\n## ")[0]


def test_summary_orders_group_repeated_item_and_disclose_the_collapse(seeded):
    """Issue #93: one order restated across N documents rendered as N identical bullets.
    It now folds to one bullet carrying the latest detail, and the collapse is *disclosed*
    -- the count plus the span -- so a reader can never mistake it for a single order."""
    _seed_orders(seeded, [
        {"key": "cervical collar", "value_text": "Dr. Jones, ortho",
         "observed_at": "2026-06-14"},
        {"key": "cervical collar", "value_text": "Dr. Smith, ortho",
         "observed_at": "2026-01-05"},
    ])
    section = _orders_section(seeded)
    assert section.count("cervical collar") == 1        # three stored rows, one bullet
    assert ("- cervical collar - Dr. Jones, ortho  "
            "(ordered 2026-06-14; +2 earlier, first 2026-01-05)") in section
    assert "outpatient physical therapy" in section           # distinct key stays distinct
    # storage is untouched: the collapse is render-only and reversible
    assert seeded.execute(
        "SELECT COUNT(*) AS n FROM observation WHERE obs_type='order'"
    ).fetchone()["n"] == 4


def test_summary_orders_qualifier_keeps_its_own_bullet(seeded):
    """Grouping is on `key_token`, so #71's qualifier-awareness carries over: aquatic PT
    is a different order from plain outpatient PT, not a restatement of it."""
    _seed_orders(seeded, [
        {"key": "outpatient physical therapy (aquatic)", "observed_at": "2026-03-01"},
    ])
    section = _orders_section(seeded)
    assert "- outpatient physical therapy (aquatic)  (ordered 2026-03-01)" in section
    assert "- outpatient physical therapy\n" in section   # bare row untouched, ungrouped


def test_summary_orders_keyless_rows_are_never_bucketed_together(seeded):
    """An empty group token means "no identity", not "one shared identity": bucketing
    every keyless row together would fabricate a merge. `value_text` is the fallback
    identity, matching the display fallback."""
    _seed_orders(seeded, [
        {"value_text": "Dr. Ray, cardiology consult", "observed_at": "2026-04-01"},
        {"observed_at": "2026-04-02"},          # neither item name nor detail
        {"observed_at": "2026-04-03"},          # ... and another: two bullets, not one
    ])
    section = _orders_section(seeded)
    assert "- Dr. Ray, cardiology consult  (ordered 2026-04-01)" in section
    assert section.count("(unspecified)") == 2
    assert "earlier" not in section


def test_summary_orders_undated_earlier_row_keeps_the_count(seeded):
    """A group whose earlier rows carry no date drops the `first <date>` clause but keeps
    the disclosure -- the reader still learns the order was restated."""
    _seed_orders(seeded, [
        {"key": "outpatient physical therapy", "value_text": "8 sessions",
         "observed_at": "2026-05-01"},
    ])
    section = _orders_section(seeded)
    assert ("- outpatient physical therapy - 8 sessions  "
            "(ordered 2026-05-01; +1 earlier)") in section
    assert "first" not in section


def test_summary_orders_newest_first_undated_last(seeded):
    """Orders are actionable events, so the section leads with the most recent (the other
    event sections' ordering), undated last then alphabetical."""
    _seed_orders(seeded, [{"key": "sleep study", "observed_at": "2026-07-01"}])
    section = _orders_section(seeded)
    assert (section.index("sleep study")
            < section.index("cervical collar")
            < section.index("outpatient physical therapy"))


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
    section = md.split("## Upcoming / Open Appointments")[1].split("\n## ")[0]
    assert "Dr. Smith" in section    # upcoming
    assert "Dr. Open" in section     # past but no summary -> open
    assert "Dr. Past" not in section  # past + documented -> closed


def test_summary_open_conflicts_section(seeded):
    """The summary is the doc read between appointments, so a staged correction must be
    visible there and not only in `review-conflicts` / a per-appointment brief: without
    it the summary prints the stale value with no hint a correction is pending (#59)."""
    cid = _stage_conflict(seeded, "jane-doe")
    md = render.render_summary(seeded, "jane-doe")
    section = md.split("## Open Conflicts")[1].split("\n## ")[0]
    assert f"conflict #{cid} (lab_result)" in section
    assert "`pemr review-conflicts`" in section       # tells the reader how to clear it


def test_summary_open_conflicts_empty_state_and_scoping(seeded):
    """Empty state is explicit (`_none_`, matching the brief), resolved conflicts drop
    out of the section, and another person's conflict never leaks in."""
    md = render.render_summary(seeded, "jane-doe")
    assert "_none_" in md.split("## Open Conflicts")[1].split("\n## ")[0]

    resolved = _stage_conflict(seeded, "jane-doe", status="resolved")
    johns = _stage_conflict(seeded, "john-doe")
    section = render.render_summary(
        seeded, "jane-doe"
    ).split("## Open Conflicts")[1].split("\n## ")[0]
    assert f"conflict #{resolved}" not in section     # resolved -> not open
    assert f"conflict #{johns}" not in section        # other person's conflict
    assert "_none_" in section


def test_summary_empty_sections_are_explicit(seeded):
    """A person with no records still renders every section, each with an explicit
    empty-state note (an absent section must not read as an overlooked one)."""
    md = render.render_summary(seeded, "john-doe")
    assert "## Active Medications" in md and "_none recorded_" in md
    for section in ("## Active Problems", "## Past Medical History",
                    "## Family History", "## Allergies"):
        assert section in md
        assert "_none recorded_" in md.split(section)[1].split("\n## ")[0]
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
    _stage_conflict(seeded, "jane-doe")
    md = render.render_brief(seeded, _upcoming_appt_id(seeded))
    section = md.split("## Open Conflicts")[1].split("##")[0]
    assert "review-conflicts" in section


def test_brief_unknown_appointment_raises(seeded):
    with pytest.raises(render.AppointmentNotFoundError):
        render.render_brief(seeded, 99999)


def test_brief_surfaces_allergies_and_active_problems(seeded):
    """A brief handed to a clinician that omits allergies is a safety gap - and after
    migration 006 the generic observation loop no longer carries them (issue #63)."""
    md = render.render_brief(seeded, _upcoming_appt_id(seeded))
    allergies = md.split("## Allergies")[1].split("\n## ")[0]
    problems = md.split("## Active Problems")[1].split("\n## ")[0]
    assert "- Sulfa [HIGH] - anaphylaxis" in allergies
    assert "- Penicillin - rash" in allergies
    assert "Type 2 Diabetes" in problems
    assert "Appendicitis" not in problems      # resolved: not an active problem
    assert "mother" not in problems            # a relative's dx is never the patient's
    # placement: before the generic context section, above the fold for a walk-in
    assert md.index("## Allergies") < md.index("## Procedures & Observations")


def test_brief_empty_allergy_section_is_explicit(seeded):
    """John has no allergies: the section must still render with an empty-state note
    rather than vanish (an absent Allergies section reads as 'none checked')."""
    aid = seeded.execute(
        "INSERT INTO appointment (person_id, scheduled_for, provider, dedup_key, "
        "dedup_base) SELECT person_id, '2099-05-01', 'Dr. Nobody', 'k-appt-j', "
        "'k-appt-j' FROM person WHERE slug='john-doe'"
    ).lastrowid
    seeded.commit()
    md = render.render_brief(seeded, aid)
    assert "_none recorded_" in md.split("## Allergies")[1].split("\n## ")[0]


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


# --- curation overlay (issue #109) --------------------------------------------
#
# The overlay is additive: with no verdicts every render is byte-identical to what it
# was before it existed (the golden test below is the guard), and a verdict changes
# output only through the four documented behaviours.

def _annotate(conn, record_type, column, value, **kwargs):
    """Record a verdict on the family holding one seeded row."""
    base = conn.execute(
        f"SELECT dedup_base FROM {record_type} WHERE {column} = ?", (value,)
    ).fetchone()["dedup_base"]
    kwargs.setdefault("note", "clinician ruled")
    curation.annotate_record(conn, record_type, base, apply=True, **kwargs)
    return base


def _renders(conn):
    d = dedup.load_dictionary(DICT_PATH)
    return {
        "summary": render.render_summary(conn, "jane-doe", dictionary=d),
        "brief": render.render_brief(conn, _upcoming_appt_id(conn), dictionary=d),
        "journal": render.render_journal(conn, "jane-doe"),
    }


def test_no_verdicts_renders_byte_identically(seeded):
    """The load-bearing guarantee: the appendix and questions sections are *omitted*,
    not rendered empty, so unannotated output is unchanged to the byte."""
    before = _renders(seeded)
    # An empty `curation` table is the state every existing database is in.
    assert seeded.execute("SELECT COUNT(*) AS n FROM curation").fetchone()["n"] == 0
    after = _renders(seeded)
    assert after == before
    for md in before.values():
        assert "Superseded / corrected" not in md
        assert "Questions for the Clinician" not in md
        assert "DISPUTED" not in md


@pytest.mark.parametrize("status", ["superseded", "erroneous-in-source"])
def test_superseded_family_leaves_its_section_for_the_appendix(seeded, status):
    _annotate(seeded, "condition", "name", "Appendicitis", status=status,
              note="never actually confirmed")
    out = _renders(seeded)
    for name, md in out.items():
        if name == "brief":
            continue  # the brief has no past-medical-history section
        assert "## Superseded / corrected" in md, name
        assert "condition: Appendicitis" in md, name
        assert "never actually confirmed" in md, name
    # Gone from Past Medical History, but the row itself is untouched.
    body = out["summary"].split("## Superseded / corrected")[0]
    assert "Appendicitis" not in body
    assert seeded.execute(
        "SELECT COUNT(*) AS n FROM condition WHERE name = 'Appendicitis'"
    ).fetchone()["n"] == 1


def test_disputed_family_renders_in_place_with_a_marker(seeded):
    _annotate(seeded, "allergy", "substance", "Sulfa", status="disputed",
              note="two notes disagree on criticality")
    out = _renders(seeded)
    for name in ("summary", "brief"):
        assert "- Sulfa" in out[name], name
        assert "[DISPUTED: two notes disagree on criticality]" in out[name], name
        assert "Superseded / corrected" not in out[name], name
    # ... and reaches the brief's clinician questions, but not the summary's.
    assert "## Questions for the Clinician" in out["brief"]
    assert "allergy: Sulfa - two notes disagree on criticality" in out["brief"]
    assert "Questions for the Clinician" not in out["summary"]


def test_disputed_marker_reaches_the_journal(seeded):
    _annotate(seeded, "condition", "name", "Type 2 Diabetes", status="disputed",
              note="onset date contested")
    md = render.render_journal(seeded, "jane-doe")
    assert "[DISPUTED: onset date contested]" in md


def test_merged_into_renders_only_the_target(seeded):
    target = seeded.execute(
        "SELECT dedup_base FROM lab_result WHERE test_name = 'LDL'"
    ).fetchone()["dedup_base"]
    _annotate(seeded, "lab_result", "test_name", "Glucose, fasting",
              status="merged-into", merged_into_base=target,
              note="same analyte, two spellings")
    md = render.render_summary(seeded, "jane-doe",
                               dictionary=dedup.load_dictionary(DICT_PATH))
    body, appendix = md.split("## Superseded / corrected")
    assert "Glucose, fasting" not in body
    assert "LDL" in body                                     # the target still renders
    assert "lab_result: Glucose, fasting" in appendix
    assert f"merged into {target[:12]}..." in appendix


def test_confirmed_changes_nothing_but_the_row_still_renders(seeded):
    before = _renders(seeded)
    _annotate(seeded, "allergy", "substance", "Penicillin", status="confirmed",
              note="clinician agreed")
    after = _renders(seeded)
    assert after == before


def test_superseded_vital_does_not_win_latest(seeded):
    """Ordering guard: the curation pass runs before the latest-wins fold, so a
    superseded reading cannot hide the good one behind it."""
    plain = render.render_summary(seeded, "jane-doe",
                                  dictionary=dedup.load_dictionary(DICT_PATH))
    assert "blood_pressure: 118.0 mmHg" in plain          # 2026 reading wins today

    base = seeded.execute(
        "SELECT dedup_base FROM observation WHERE obs_type='vital' AND "
        "key='blood_pressure' AND value_num = 118"
    ).fetchone()["dedup_base"]
    curation.annotate_record(seeded, "observation", base, status="superseded",
                             note="transcription error", apply=True)

    md = render.render_summary(seeded, "jane-doe",
                               dictionary=dedup.load_dictionary(DICT_PATH))
    body = md.split("## Superseded / corrected")[0]
    assert "blood_pressure: 118.0 mmHg" not in body
    assert "blood_pressure: 128.0 mmHg" in body           # the 2025 reading takes over


def test_superseded_lab_does_not_consume_a_brief_slot(seeded):
    """The brief's top-N cut happens after the curation pass, so a superseded lab
    frees its slot rather than spending it."""
    _annotate(seeded, "lab_result", "test_name", "Glucose, fasting",
              status="erroneous-in-source", note="wrong patient's requisition")
    md = render.render_brief(seeded, _upcoming_appt_id(seeded), recent_labs=1,
                             dictionary=dedup.load_dictionary(DICT_PATH))
    labs = md.split("## Recent Labs")[1].split("## ")[0]
    assert "Glucose, fasting" not in labs
    assert "LDL" in labs                                  # next-most-recent takes it


def test_curated_renders_stay_ascii_and_read_only(seeded):
    before = _row_counts(seeded)
    _annotate(seeded, "allergy", "substance", "Sulfa", status="disputed",
              note="two notes disagree")
    _annotate(seeded, "condition", "name", "Chickenpox", status="superseded",
              note="duplicate of a later note")
    for md in _renders(seeded).values():
        assert md.isascii(), f"non-ASCII would crash a cp437 console: {md!r}"
        md.encode("cp437")
    assert _row_counts(seeded) == before  # still a pure read

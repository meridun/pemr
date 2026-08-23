"""Phase 4 render layer: master summary, appointment brief, journal.

Pure, read-only Markdown renders over seeded fixtures -- section presence/empty-state,
abnormal-lab selection, latest-vitals pick, brief scoping, journal ordering + provenance
footnotes, ASCII output (cp1252/cp437 console lesson), and the read-only guarantee
(no row-count change after any render)."""

from datetime import date, datetime
from pathlib import Path

import pytest

from pemr import curation, db, dedup, persons, query, render, units

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


def _seed_results(conn, rows):
    """Extra `lab_result` rows on jane, committed as their own document. The `seeded`
    fixture's four results are a year apart, so no two of them share one 31-day window
    and a panel case has to seed its own."""
    dedup.commit_extraction(conn, _doc(conn, "jane-doe"), {
        "lab_result": list(rows),
    }, dedup.load_dictionary(DICT_PATH))


def _orders_section(conn, dictionary=None):
    md = render.render_summary(conn, "jane-doe", dictionary=dictionary)
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


def test_summary_orders_oldest_first_undated_last(seeded):
    """Issue #128: what survives suppression is what is still *outstanding*, and the
    order open longest is the most actionable row -- so this section leads with the
    oldest, diverging from the other event sections' newest-first. Undated last, then
    alphabetical."""
    _seed_orders(seeded, [{"key": "sleep study", "observed_at": "2026-07-01"}])
    section = _orders_section(seeded)
    assert (section.index("cervical collar")                  # 2026-02-01
            < section.index("sleep study")                    # 2026-07-01
            < section.index("outpatient physical therapy"))   # undated


# --- issue #128: an order with a result is no longer outstanding ---------------
#
# `Orders & Referrals` listed every `obs_type='order'` row ever stored, so a section
# meant to read as "still open" filled up with orders resulted years ago -- most of them
# on the order date itself. Suppression is render-only inference (no FK exists) and is
# biased hard toward *under*-suppressing: hiding a genuinely open order is the miss this
# section exists to prevent. The `seeded` fixture already carries jane's four results --
# HbA1c 2024-01-01, LDL 2025-01-01, Glucose fasting 2026-01-01, TSH 2023-01-01.

def test_summary_orders_resulted_order_is_suppressed(seeded):
    """The common case from the problem statement: ordered and resulted the same day."""
    _seed_orders(seeded, [{"key": "HbA1c", "observed_at": "2024-01-01"}])
    section = _orders_section(seeded)
    assert "HbA1c" not in section
    assert "cervical collar" in section          # control: unresulted rows untouched


def test_summary_orders_result_within_window_suppresses(seeded):
    """Matching is a bounded window around the order date, not exact-date equality: a
    draw that lands a few days after the order still closes it."""
    _seed_orders(seeded, [{"key": "LDL", "observed_at": "2024-12-20"}])  # result 2025-01-01
    assert "LDL" not in _orders_section(seeded)


def test_summary_orders_match_window_edges_are_exact(seeded):
    """...and the window is a hard `[-1, +30]` days around the order date, pinned on
    both edges so any later widening is a deliberate edit rather than a drift (widening
    is the over-suppression risk this design is built against). A result on day +30
    closes its order, day +31 does not; a draw dated one day *ahead* of the order text
    still closes it, two days ahead does not -- that one answered an earlier order."""
    _seed_orders(seeded, [
        {"key": "TSH", "observed_at": "2022-12-02"},               # result +30d: closed
        {"key": "HbA1c", "observed_at": "2023-12-01"},             # result +31d: open
        {"key": "LDL", "observed_at": "2025-01-02"},               # result -1d: closed
        {"key": "Glucose, fasting", "observed_at": "2026-01-03"},  # result -2d: open
    ])
    section = _orders_section(seeded)
    assert "TSH" not in section
    assert "LDL" not in section
    assert "- HbA1c  (ordered 2023-12-01)" in section
    assert "- Glucose, fasting  (ordered 2026-01-03)" in section


def test_summary_orders_open_order_is_never_aged_out(seeded):
    """Age is never a suppression signal (the issue's whole point): a 2019 order no
    result answers keeps rendering, and a result four years adrift does not close it."""
    _seed_orders(seeded, [{"key": "TSH", "observed_at": "2019-01-01"}])  # result 2023-01-01
    assert "- TSH  (ordered 2019-01-01)" in _orders_section(seeded)


def test_summary_orders_referral_without_result_still_renders(seeded):
    """Referral/DME orders have no `lab_result` by construction, so they fall through the
    matching untouched -- which is already the wanted "still open" behaviour."""
    _seed_orders(seeded, [{"key": "HbA1c", "observed_at": "2024-01-01"}])
    section = _orders_section(seeded)
    assert "- cervical collar - Dr. Smith, ortho  (ordered 2026-02-01)" in section
    assert "- outpatient physical therapy" in section
    assert "HbA1c" not in section                # the lab order beside them is gone


def test_summary_orders_qualifier_result_does_not_close_plain_order(seeded):
    """Matching runs on the same `key_token` the grouping fold uses (issue #71), so the
    two layers can never disagree about what one item is: a plain `HbA1c` result does not
    close a point-of-care `HbA1c (POC)` order."""
    _seed_orders(seeded, [{"key": "HbA1c (POC)", "observed_at": "2024-01-01"}])
    assert "HbA1c (POC)" in _orders_section(seeded)


def test_summary_orders_suppression_decides_on_the_latest_row(seeded):
    """Suppression is asked of the group's *latest* row -- the live restatement the
    bullet already speaks for -- and runs after the fold, so the `+N earlier` disclosure
    is unchanged for what still renders (issue #93 preserved)."""
    _seed_orders(seeded, [
        {"key": "HbA1c", "observed_at": "2024-01-01"},   # resulted the same day
        {"key": "HbA1c", "observed_at": "2026-06-01"},   # re-ordered, no result yet
    ])
    section = _orders_section(seeded)
    assert section.count("HbA1c") == 1
    assert "- HbA1c  (ordered 2026-06-01; +1 earlier, first 2024-01-01)" in section


def test_summary_orders_superseded_result_does_not_suppress(seeded):
    """A result a human ruled superseded sits in the appendix, not in the record -- so it
    cannot be the thing that closed an order, and the order comes back."""
    _seed_orders(seeded, [{"key": "HbA1c", "observed_at": "2024-01-01"}])
    assert "HbA1c" not in _orders_section(seeded)
    _annotate(seeded, "lab_result", "test_name", "HbA1c", status="superseded",
              note="wrong patient")
    assert "- HbA1c  (ordered 2024-01-01)" in _orders_section(seeded)


def test_summary_orders_other_person_result_does_not_suppress(seeded):
    """`person_id` scoping: john's identically-named result must not close jane's order
    (his LDL is collected 2026-01-01, jane's own a year earlier and out of window)."""
    _seed_orders(seeded, [{"key": "LDL", "observed_at": "2026-01-01"}])
    assert "- LDL  (ordered 2026-01-01)" in _orders_section(seeded)


def test_summary_orders_suppression_is_render_only(seeded):
    """AC8: nothing is deleted or mutated -- the suppressed order is still in the DB,
    exactly where `record show` would find it."""
    _seed_orders(seeded, [{"key": "HbA1c", "observed_at": "2024-01-01"}])
    before = _row_counts(seeded)
    assert "HbA1c" not in _orders_section(seeded)
    assert _row_counts(seeded) == before
    assert seeded.execute(
        "SELECT COUNT(*) AS n FROM observation WHERE obs_type='order' AND key='HbA1c'"
    ).fetchone()["n"] == 1


# --- issue #145: compound / panel orders ---------------------------------------
#
# #128 compares one `key_token` per order, so a *panel* order -- one key naming several
# analytes (`cbc,cmp,ldh`) -- yields a token no single-analyte result can ever equal and
# never leaves the section, however completely it was resulted. The order side now
# decomposes to a token *set*; the match itself is unchanged (still exact-token, never
# substring) and the rule is all-or-nothing, so a partially resulted panel keeps
# rendering. #128's stance is preserved throughout: every ambiguity resolves to render.

def test_summary_orders_fully_resulted_panel_is_suppressed(seeded):
    """AC1: every analyte a compound order names has an in-window result, so the panel
    is done and the section stops claiming it is outstanding."""
    _seed_orders(seeded, [{"key": "Sodium, Potassium", "observed_at": "2026-03-01"}])
    _seed_results(seeded, [
        {"test_name": "Sodium", "collected_at": "2026-03-01", "value_num": 140},
        {"test_name": "Potassium", "collected_at": "2026-03-05", "value_num": 4.1},
    ])
    section = _orders_section(seeded)
    assert "Sodium" not in section and "Potassium" not in section
    assert "cervical collar" in section          # control: unresulted rows untouched


def test_summary_orders_partially_resulted_panel_still_renders(seeded):
    """AC2: all-or-nothing. One component still outstanding *is* outstanding work, so the
    bullet stays -- with #93's `+N earlier` disclosure intact, since the fold is unchanged."""
    _seed_orders(seeded, [
        {"key": "Sodium, Potassium", "observed_at": "2026-03-01"},
        {"key": "Sodium, Potassium", "observed_at": "2026-02-01"},
    ])
    _seed_results(seeded, [
        {"test_name": "Sodium", "collected_at": "2026-03-01", "value_num": 140},
    ])
    section = _orders_section(seeded)
    assert ("- Sodium, Potassium  "
            "(ordered 2026-03-01; +1 earlier, first 2026-02-01)") in section


def test_summary_orders_single_component_is_unchanged_by_decomposition(seeded):
    """AC3: a key with no top-level separator never decomposes, so it is never
    word-stripped either -- `Lipid Panel` keeps today's whole-string token and a bare
    `Lipid` result does not close it. The #128 block above runs unmodified for the rest."""
    _seed_orders(seeded, [{"key": "Lipid Panel", "observed_at": "2026-03-01"}])
    _seed_results(seeded, [
        {"test_name": "Lipid", "collected_at": "2026-03-01", "value_num": 1.0},
    ])
    assert "- Lipid Panel  (ordered 2026-03-01)" in _orders_section(seeded)


def test_summary_orders_panel_is_never_suppressed_by_a_substring_result(seeded):
    """AC4: components are matched exact-token, never by substring, in either direction --
    a `Sodium Level` result does not answer the `Sodium` component, and an unrelated
    panel-named result answers nothing."""
    _seed_orders(seeded, [{"key": "Sodium, Potassium", "observed_at": "2026-03-01"}])
    _seed_results(seeded, [
        {"test_name": "Sodium Level", "collected_at": "2026-03-01", "value_num": 140},
        {"test_name": "Comprehensive Metabolic Panel", "collected_at": "2026-03-02",
         "value_num": 1.0},
    ])
    assert "- Sodium, Potassium  (ordered 2026-03-01)" in _orders_section(seeded)


def test_summary_orders_slash_separated_panel_drops_noise_words(seeded):
    """`/` splits like `,` does, and a structural word that names no analyte (`panel`) is
    dropped from a component -- otherwise `Immunofixation Panel` could never match the
    `Immunofixation` result that answered it."""
    _seed_orders(seeded, [
        {"key": "Serum Protein Electrophoresis / Immunofixation Panel",
         "observed_at": "2026-03-01"},
    ])
    _seed_results(seeded, [
        {"test_name": "Serum Protein Electrophoresis", "collected_at": "2026-03-01",
         "value_num": 1.0},
        {"test_name": "Immunofixation", "collected_at": "2026-03-02", "value_num": 2.0},
    ])
    assert "Immunofixation" not in _orders_section(seeded)


def test_summary_orders_panel_components_map_through_the_dictionary(seeded):
    """Each component runs through the *same* dictionary the fold and the result index
    use, so synonyms agree per analyte: `Sed Rate` -> esr, `Mg` -> magnesium."""
    _seed_orders(seeded, [{"key": "Sed Rate, Mg", "observed_at": "2026-03-01"}])
    _seed_results(seeded, [
        {"test_name": "ESR", "collected_at": "2026-03-01", "value_num": 12},
        {"test_name": "Magnesium", "collected_at": "2026-03-02", "value_num": 2.0},
    ])
    section = _orders_section(seeded, dictionary=dedup.load_dictionary(DICT_PATH))
    assert "Sed Rate" not in section
    assert "cervical collar" in section


def test_summary_orders_parenthetical_component_is_not_split(seeded):
    """A parenthetical is identity-bearing (#71), so a separator inside one is content:
    the qualifier stays one opaque component and nothing inside it is word-stripped.
    Results named after its innards therefore close nothing, and the order still renders."""
    _seed_orders(seeded, [
        {"key": "Sed Rate, SLE Profile (Profile A, Scleroderma)",
         "observed_at": "2026-03-01"},
    ])
    _seed_results(seeded, [
        {"test_name": "ESR", "collected_at": "2026-03-01", "value_num": 12},
        {"test_name": "Profile A", "collected_at": "2026-03-02", "value_num": 1.0},
        {"test_name": "Scleroderma", "collected_at": "2026-03-02", "value_num": 2.0},
    ])
    section = _orders_section(seeded, dictionary=dedup.load_dictionary(DICT_PATH))
    assert ("- Sed Rate, SLE Profile (Profile A, Scleroderma)  "
            "(ordered 2026-03-01)") in section


def test_summary_orders_malformed_compound_key_renders(seeded):
    """The never-raises contract: separator-only, unbalanced-bracket and trailing-separator
    keys all reach this layer from OCR'd documents. None may crash a summary, and each
    degrades to "still open" -- including `HbA1c,`, which has no *second* component and so
    keeps today's whole-string token rather than gaining a match it never had."""
    _seed_orders(seeded, [
        {"key": ",", "observed_at": "2026-03-01"},
        {"key": "a) b, c", "observed_at": "2026-03-02"},
        {"key": "HbA1c,", "observed_at": "2026-03-03"},
    ])
    _seed_results(seeded, [
        {"test_name": "HbA1c", "collected_at": "2026-03-03", "value_num": 5.5},
    ])
    section = _orders_section(seeded)
    assert "- ,  (ordered 2026-03-01)" in section
    assert "- a) b, c  (ordered 2026-03-02)" in section
    assert "- HbA1c,  (ordered 2026-03-03)" in section


def test_summary_orders_unreadable_component_does_not_let_the_rest_suppress(seeded):
    """Decomposition fails **closed**: a component that tokenizes empty -- all noise words
    (`Extensive Panel`), or whitespace-equivalent after normalization -- is not dropped from
    the set, it voids the whole key. Dropping it would narrow all-or-nothing to
    all-*remaining* and let a lone `CBC` result suppress an order still naming something this
    layer could not read, which is the one direction the section must never fail in."""
    _seed_orders(seeded, [
        {"key": "CBC, Extensive Panel", "observed_at": "2026-03-01"},
        {"key": "Sodium, ()", "observed_at": "2026-03-02"},
    ])
    _seed_results(seeded, [
        {"test_name": "CBC", "collected_at": "2026-03-01", "value_num": 1.0},
        {"test_name": "Sodium", "collected_at": "2026-03-02", "value_num": 140},
    ])
    section = _orders_section(seeded)
    assert "- CBC, Extensive Panel  (ordered 2026-03-01)" in section
    assert "- Sodium, ()  (ordered 2026-03-02)" in section


# A `,` or `/` is structure in a panel key but *content* in many single analytes' names
# (`Glucose, fasting`, `Kappa/Lambda Ratio`). Decomposing one of those asks for analytes
# that were never ordered, so the order stops suppressing -- #128's inversion, re-created
# for a different class of key. Two guards, tested below: the dictionary is asked whether
# the whole string is one declared analyte before any split, and the whole-key match of
# #128 is tried first regardless, so no suppression that worked before can be lost.

def test_summary_orders_declared_comma_bearing_analyte_is_not_decomposed(seeded):
    """AC3: `data/dictionary.example.toml:37` declares `glucose, fasting` a single
    analyte, so the order asks for *that* result, not for `glucose` plus `fasting`."""
    _seed_orders(seeded, [{"key": "Glucose, fasting", "observed_at": "2026-03-01"}])
    _seed_results(seeded, [
        {"test_name": "Glucose, fasting", "collected_at": "2026-03-02", "value_num": 92},
    ])
    section = _orders_section(seeded, dictionary=dedup.load_dictionary(DICT_PATH))
    assert "Glucose" not in section
    assert "cervical collar" in section          # control: unresulted rows untouched


def test_summary_orders_declared_analyte_is_not_closed_by_one_component(seeded):
    """The guard restores a *name*, it does not become a looser match: a bare `Glucose`
    result answers `glucose`, which is not the `glucose_fasting` that was ordered."""
    _seed_orders(seeded, [{"key": "Glucose, fasting", "observed_at": "2026-03-01"}])
    _seed_results(seeded, [
        {"test_name": "Glucose", "collected_at": "2026-03-02", "value_num": 92},
    ])
    section = _orders_section(seeded, dictionary=dedup.load_dictionary(DICT_PATH))
    assert "- Glucose, fasting  (ordered 2026-03-01)" in section


def test_summary_orders_declared_analyte_holds_for_a_slash_and_a_stem(seeded):
    """The same guard for the `/` spelling (`kappa/lambda ratio`, whose first component
    would otherwise map to a *different* analyte) and for a declared **stem** carrying a
    qualifier -- `identity` looks the stem up, so `norm` sees the hit either way."""
    _seed_orders(seeded, [
        {"key": "Kappa/Lambda Ratio", "observed_at": "2026-03-01"},
        {"key": "Cholesterol, Total (Calculated)", "observed_at": "2026-03-01"},
    ])
    _seed_results(seeded, [
        {"test_name": "Kappa/Lambda Ratio", "collected_at": "2026-03-02", "value_num": 1.2},
        {"test_name": "Cholesterol, Total (Calculated)", "collected_at": "2026-03-02",
         "value_num": 180},
    ])
    section = _orders_section(seeded, dictionary=dedup.load_dictionary(DICT_PATH))
    assert "Kappa" not in section and "Cholesterol" not in section


def test_summary_orders_comma_bearing_name_suppresses_without_a_dictionary(seeded):
    """AC3 for the case no dictionary can speak for: an undeclared analyte whose name
    carries a comma, rendered with no dictionary at all. #128's whole-key match is asked
    first, so an identically-named result still closes it."""
    _seed_orders(seeded, [{"key": "Ferritin, Serum", "observed_at": "2026-03-01"}])
    _seed_results(seeded, [
        {"test_name": "Ferritin, Serum", "collected_at": "2026-03-02", "value_num": 30},
    ])
    section = _orders_section(seeded)
    assert "Ferritin" not in section
    assert "cervical collar" in section


def _abnormal_section(md):
    """The abnormal-labs section body. Split on the constant-built heading (#165) so a
    change to the window can't leave these tests silently matching nothing."""
    heading = f"## Abnormal Labs (last {render._ABNORMAL_LABS_WINDOW_MONTHS} months)"
    return md.split(heading)[1].split("\n## ")[0]


def test_summary_latest_vitals_pick(seeded):
    md = render.render_summary(seeded, "jane-doe", dictionary=dedup.load_dictionary(DICT_PATH))
    assert "blood_pressure: 118.0 mmHg" in md   # most recent per key
    assert "128.0 mmHg" not in md               # older reading dropped
    assert "weight: 80.0 kg" in md


def test_summary_abnormal_lab_selection(seeded):
    # `now` is pinned because the section is age-bounded (#165): unpinned, this test
    # would start reading the wall clock and drift out of window in real 2027.
    md = render.render_summary(seeded, "jane-doe", now=_NOW)
    section = _abnormal_section(md)
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

    # Pinned for #165: the two siblings are one analyte, so once 2026-01-01 ages out the
    # keep-latest guard would retain only the newer and this ordering assertion would
    # silently stop testing an ordering at all.
    md = render.render_summary(seeded, "jane-doe", dictionary=d, now=_NOW)
    section = _abnormal_section(md)
    assert section.index("320.0") < section.index("200.0")


# --- abnormal labs: the 12-month window + keep-latest guard (issue #165) -------
#
# The section is bounded by *age*, so every test here pins `now`. jane's seeded abnormal
# rows are LDL 2025-01-01 (flag H) and Glucose, fasting 2026-01-01 (over ref_high); HbA1c
# 2024-01-01 and TSH 2023-01-01 are normal and never in the section at any window.

def _abnormal_for(conn, slug="jane-doe", *, now):
    return _abnormal_section(render.render_summary(conn, slug, now=now))


def test_summary_abnormal_labs_window_edges_are_exact(seeded):
    """The cutoff is a hard, inclusive `today - 12 months`, pinned on both edges so a
    later widening is a deliberate edit rather than a drift. Each analyte also carries a
    recent in-window abnormal, which is what stops the keep-latest guard from masking the
    drop -- without it the older row would be retained as the analyte's latest and the
    boundary would go untested."""
    d = dedup.load_dictionary(DICT_PATH)
    now = datetime(2026, 6, 1)                       # cutoff: 2025-06-01, inclusive
    dedup.commit_extraction(seeded, _doc(seeded, "john-doe"), {"lab_result": [
        {"test_name": "Ferritin", "collected_at": "2026-05-01", "value_num": 900,
         "unit": "ng/mL", "flag": "H"},              # recent: holds the guard
        {"test_name": "Ferritin", "collected_at": "2025-06-01", "value_num": 800,
         "unit": "ng/mL", "flag": "H"},              # exactly on the cutoff: kept
        {"test_name": "Ceruloplasmin", "collected_at": "2026-05-01", "value_num": 60,
         "unit": "mg/dL", "flag": "H"},              # recent: holds the guard
        {"test_name": "Ceruloplasmin", "collected_at": "2025-05-31", "value_num": 55,
         "unit": "mg/dL", "flag": "H"},              # cutoff - 1 day: dropped
    ]}, d)
    section = _abnormal_for(seeded, "john-doe", now=now)
    assert "900" in section and "800" in section     # on the boundary still renders
    assert "60.0" in section
    assert "55.0" not in section                     # one day past it does not


def test_summary_abnormal_labs_keep_latest_prevents_false_empty(seeded):
    """AC2 and the reason the window alone is not enough: with every abnormal draw older
    than the window, the section must still name each abnormal analyte once. An empty
    section reads as "nothing flagged" while the markers are live -- a worse miss than
    the unbounded dump this bounds."""
    section = _abnormal_for(seeded, now=datetime(2030, 1, 1))
    assert "_none flagged_" not in section
    assert "LDL" in section and "Glucose" in section
    assert "HbA1c" not in section and "TSH" not in section   # normal stays normal
    # One row per analyte: the guard retains the latest, not the history.
    assert len([ln for ln in section.splitlines() if ln.startswith("- ")]) == 2


def test_summary_abnormal_labs_mixes_in_window_rows_with_keep_latest(seeded):
    """AC3: an analyte with in-window abnormals shows those (and drops its superseded
    older rows), while an analyte whose only abnormals are out of window is still
    represented by its most recent one."""
    d = dedup.load_dictionary(DICT_PATH)
    now = datetime(2026, 6, 1)                       # cutoff: 2025-06-01
    dedup.commit_extraction(seeded, _doc(seeded, "john-doe"), {"lab_result": [
        {"test_name": "Ferritin", "collected_at": "2026-05-01", "value_num": 900,
         "unit": "ng/mL", "flag": "H"},              # in window
        {"test_name": "Ferritin", "collected_at": "2019-01-01", "value_num": 700,
         "unit": "ng/mL", "flag": "H"},              # superseded and stale: dropped
        {"test_name": "Ceruloplasmin", "collected_at": "2018-01-01", "value_num": 55,
         "unit": "mg/dL", "flag": "H"},              # only abnormal, stale: kept
    ]}, d)
    section = _abnormal_for(seeded, "john-doe", now=now)
    assert "900" in section                          # in-window row
    assert "55.0" in section                         # keep-latest for its analyte
    assert "700" not in section                      # not resurrected by the guard


def test_summary_abnormal_labs_heading_states_the_window(seeded):
    """AC4: the window is self-documenting, and the heading is built from the same
    constant that drives the filter so the two cannot desync."""
    md = render.render_summary(seeded, "jane-doe", now=_NOW)
    assert "## Abnormal Labs (last 12 months)" in md
    assert (
        f"## Abnormal Labs (last {render._ABNORMAL_LABS_WINDOW_MONTHS} months)" in md
    )


def test_summary_abnormal_labs_undated_row_is_never_aged_out(seeded):
    """Fail-open, mirroring `_is_resulted`: a `collected_at` that will not parse cannot
    be *proven* stale, so it renders rather than being silently dropped -- a bad date must
    never empty a medical section."""
    seeded.execute(
        "UPDATE lab_result SET collected_at = 'not-a-date' WHERE test_name = 'LDL' "
        "AND person_id = (SELECT person_id FROM person WHERE slug = 'jane-doe')"
    )
    seeded.commit()
    assert "LDL" in _abnormal_for(seeded, now=datetime(2030, 1, 1))


def test_summary_abnormal_labs_unparseable_now_skips_the_bound(seeded):
    """Same posture one level up: if the render's own date will not parse there is no
    cutoff to apply, so the section falls back to every abnormal row."""
    pid = seeded.execute(
        "SELECT person_id FROM person WHERE slug = 'jane-doe'"
    ).fetchone()["person_id"]
    unbounded = render._abnormal_labs(seeded, pid, "")
    assert unbounded == render._abnormal_labs(seeded, pid, "not-a-date")
    # 2025's LDL is a year and a half stale at any plausible clock, and still renders.
    assert [r["test_name"] for r in unbounded] == ["Glucose, fasting", "LDL"]


def test_months_before_clamps_a_short_target_month():
    """The leap-day case: 12 months before 2028-02-29 is 2027-02-28, not a ValueError."""
    assert render._months_before(date(2028, 2, 29), 12) == date(2027, 2, 28)
    assert render._months_before(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert render._months_before(date(2026, 1, 15), 12) == date(2025, 1, 15)
    assert render._months_before(date(2026, 1, 15), 13) == date(2024, 12, 15)


# --- issue #166: the summary's Procedures section -----------------------------
#
# `procedure` rows arrive largely from billing documents, so the table mixes genuine
# procedural history with routine service lines. The section narrows against a
# suppress-only pattern list, under one stance throughout: significance is never
# established by absence from a list, so every ambiguity resolves to *render*.

def _seed_procedures(conn, rows):
    """Extra `procedure` rows on jane, committed as their own document."""
    dedup.commit_extraction(conn, _doc(conn, "jane-doe"), {
        "procedure": list(rows),
    }, dedup.load_dictionary(DICT_PATH))


def _procedures_section(conn, routine=None, slug="jane-doe"):
    md = render.render_summary(conn, slug, routine_procedures=routine)
    return md.split("## Procedures")[1].split("\n## ")[0]


def _procedure_bullets(section):
    return [ln for ln in section.splitlines() if ln.startswith("- ")]


def test_summary_has_a_procedures_section(seeded):
    """AC1: procedure rows were stored, deduped and journalled but never reached the
    master summary. The section sits between results and the forward-looking sections --
    everything above it is standing state, everything below is what happens next."""
    md = render.render_summary(seeded, "jane-doe")
    assert "- 2025-03-15  Colonoscopy - normal" in _procedures_section(seeded)
    assert (md.index("## Abnormal Labs")
            < md.index("## Procedures")
            < md.index("## Upcoming / Open Appointments"))


def test_summary_procedures_no_patterns_suppress_nothing(seeded):
    """The default is default-show *and* the fast path: no list, no filtering, no
    disclosure line -- so every caller that never learns about this arg is unaffected."""
    _seed_procedures(seeded, [
        {"name": "Office Visit, Established Patient", "performed_on": "2026-02-01"},
    ])
    for routine in (None, [], ()):
        section = _procedures_section(seeded, routine)
        assert "Office Visit" in section and "Colonoscopy" in section
        assert "not shown" not in section


def test_summary_procedures_routine_pattern_is_suppressed(seeded):
    """AC3: a configured routine line leaves the summary, and the narrowing is disclosed
    on the page (issue #93's precedent) -- a summary that silently drops rows is exactly
    what the default-show rule is defending against."""
    _seed_procedures(seeded, [
        {"name": "Office Visit, Established Patient", "performed_on": "2026-02-01"},
    ])
    section = _procedures_section(seeded, ["office visit"])
    assert "Office Visit" not in section
    assert "Colonoscopy" in section                      # control: unmatched row stays
    assert "_1 routine procedure not shown" in section


def test_summary_procedures_default_show_unlisted_name(seeded):
    """AC2: a procedure nobody anticipated renders in the same pass that hides a listed
    one. Absence from the list can never establish that a row is insignificant."""
    _seed_procedures(seeded, [
        {"name": "Venipuncture", "performed_on": "2026-02-01"},
        {"name": "Cryoablation of renal tumor", "performed_on": "2026-02-02"},
    ])
    section = _procedures_section(seeded, ["venipuncture"])
    assert "Cryoablation of renal tumor" in section
    assert "Venipuncture" not in section


def test_summary_procedures_match_is_token_bounded(seeded):
    """Over-suppression is the failure that matters here, so the match lands on a token
    boundary: `cast` reaches a cast application and never `Castration`."""
    _seed_procedures(seeded, [
        {"name": "Cast application, short arm", "performed_on": "2026-02-01"},
        {"name": "Castration", "performed_on": "2026-02-02"},
    ])
    section = _procedures_section(seeded, ["cast"])
    assert "Castration" in section
    assert "Cast application" not in section
    assert "_1 routine procedure not shown" in section


def test_summary_procedures_reverse_chronological_undated_last(seeded):
    """AC4: newest first, and every undated row sorts after every dated one --
    `performed_on` is nullable *and* unvalidated, so `''` must land with `NULL` rather
    than in among the dates where SQLite's own DESC ordering would leave it."""
    _seed_procedures(seeded, [
        {"name": "Appendectomy", "performed_on": "2015-03-08"},
        {"name": "Knee arthroscopy", "performed_on": "2026-02-01"},
        {"name": "Tonsillectomy"},                                    # NULL
        {"name": "Mole removal", "performed_on": "2019-01-01"},
    ])
    seeded.execute("UPDATE procedure SET performed_on = '' WHERE name = 'Mole removal'")
    seeded.commit()
    bullets = _procedure_bullets(_procedures_section(seeded))
    assert [b.split("  ", 1)[1] for b in bullets] == [
        "Knee arthroscopy", "Colonoscopy - normal", "Appendectomy",
        "Mole removal", "Tonsillectomy",
    ]
    assert bullets[-2:] == ["- (undated)  Mole removal", "- (undated)  Tonsillectomy"]


def test_summary_procedures_empty_state(seeded):
    """AC7: john has no procedures, and an absent section must never read as an
    overlooked one -- the project's existing explicit empty state applies here too."""
    md = render.render_summary(seeded, "john-doe")
    assert "## Procedures" in md
    assert "_none recorded_" in md.split("## Procedures")[1].split("\n## ")[0]


def test_summary_procedures_all_routine_discloses_the_filter(seeded):
    """The all-filtered case must not render `_none recorded_`: the person *has*
    procedures, and claiming otherwise would be the one outright false statement this
    section could make."""
    section = _procedures_section(seeded, ["colonoscopy"])
    assert "_none recorded_" not in section
    assert "_1 routine procedure not shown" in section


def test_summary_procedures_plural_disclosure(seeded):
    _seed_procedures(seeded, [
        {"name": "Venipuncture", "performed_on": "2026-02-01"},
        {"name": "Office visit", "performed_on": "2026-02-02"},
    ])
    section = _procedures_section(seeded, ["venipuncture", "office visit"])
    assert "_2 routine procedures not shown" in section


def test_summary_procedures_filter_is_render_only(seeded):
    """AC3: nothing is deleted or mutated, and the journal stays the complete chronology
    -- the summary is the only view that narrows."""
    _seed_procedures(seeded, [{"name": "Venipuncture", "performed_on": "2026-02-01"}])
    before = _row_counts(seeded)
    assert "Venipuncture" not in _procedures_section(seeded, ["venipuncture"])
    assert _row_counts(seeded) == before
    assert "Venipuncture" in render.render_journal(seeded, "jane-doe")


def test_brief_procedures_are_unfiltered(seeded):
    """The brief takes no pattern list at all, so a routine row a clinician might still
    ask about cannot be hidden from the document handed to them."""
    _seed_procedures(seeded, [{"name": "Venipuncture", "performed_on": "2026-02-01"}])
    md = render.render_brief(seeded, _upcoming_appt_id(seeded))
    assert "Venipuncture" in md.split("## Procedures & Observations")[1]


def test_summary_procedures_dispute_and_attest_suffixes(seeded):
    """AC6: the section is a first-class one -- it carries the same dispute marker and
    attestation provenance suffix every other summary section does."""
    _annotate(seeded, "procedure", "name", "Colonoscopy", status="disputed",
              note="two reports disagree on the date")
    _attest_all(seeded)
    section = _procedures_section(seeded)
    assert "[DISPUTED: two reports disagree on the date]" in section
    assert any("Wisdom tooth extraction" in ln and _ATTEST_MARKER in ln
               for ln in section.splitlines())


def test_summary_procedures_superseded_row_leaves_for_the_curation_record(seeded):
    """Filter order: curation first, routine list second. A curated-away row must be
    routed by its verdict before the pattern list counts anything, or a superseded row
    would be reported twice -- once as curated, once as a hidden routine one."""
    _seed_procedures(seeded, [{"name": "Venipuncture", "performed_on": "2026-02-01"}])
    _annotate(seeded, "procedure", "name", "Colonoscopy", status="superseded",
              note="duplicated by the later report")
    md = render.render_summary(seeded, "jane-doe", routine_procedures=["venipuncture"])
    section = md.split("## Procedures")[1].split("\n## ")[0]
    assert "Colonoscopy" not in section
    assert "Colonoscopy" not in md                        # not anywhere in the summary
    assert "procedure: Colonoscopy" in render.render_curation(seeded, "jane-doe")
    assert "_1 routine procedure not shown" in section   # the superseded row is not counted


def test_summary_upcoming_and_open_appointments(seeded):
    md = render.render_summary(seeded, "jane-doe")
    section = md.split("## Upcoming / Open Appointments")[1].split("\n## ")[0]
    assert "Dr. Smith" in section    # upcoming
    assert "Dr. Open" in section     # past but no summary -> open
    assert "Dr. Past" not in section  # past + documented -> closed


def test_summary_open_conflicts_warning_line(seeded):
    """The summary is the doc read between appointments, so a staged correction must be
    visible there and not only in `review-conflicts` / a per-appointment brief: without
    it the summary prints the stale value with no hint a correction is pending (#59).

    Issue #168 kept that guarantee and dropped the section: one `> [!WARNING]` block,
    directly under the header, because the wording says "some values *below*"."""
    _stage_conflict(seeded, "jane-doe")
    md = render.render_summary(seeded, "jane-doe")
    assert "## Open Conflicts" not in md              # the section is gone, not moved
    warning = md.split("\n## ")[0]                    # everything above the first section
    assert "> [!WARNING]" in warning
    assert "> 1 open conflict - some values below may be superseded." in warning
    assert "curation.md" in warning
    assert "`pemr review-conflicts`" in warning       # tells the reader how to clear it


def test_summary_open_conflicts_warning_pluralizes(seeded):
    _stage_conflict(seeded, "jane-doe")
    _stage_conflict(seeded, "jane-doe")
    md = render.render_summary(seeded, "jane-doe")
    assert "> 2 open conflicts - some values below may be superseded." in md


def test_summary_open_conflicts_empty_state_and_scoping(seeded):
    """Zero open conflicts means *nothing* -- no header, no `_none_` line (the noise
    issue #168 was raised over). Resolved conflicts and another person's never count."""
    md = render.render_summary(seeded, "jane-doe")
    assert "## Open Conflicts" not in md
    assert "_none_" not in md
    assert "[!WARNING]" not in md

    _stage_conflict(seeded, "jane-doe", status="resolved")
    _stage_conflict(seeded, "john-doe")
    md = render.render_summary(seeded, "jane-doe")
    assert "[!WARNING]" not in md                    # resolved / other person -> not open
    assert "## Open Conflicts" not in md


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
    assert "## Abnormal Labs (last 12 months)" in md  # john's abnormal LDL appears
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


def test_functional_observation_surfaces_without_a_new_section(seeded):
    """Issue #132: a functional observation reaches the brief through the generic
    observation loop, and must stay out of the vitals/orders sections — those are scoped
    by obs_type equality and widening them is not how this family surfaces."""
    doc = _doc(seeded, "jane-doe", ocr="ledger transcription")
    dedup.commit_extraction(seeded, doc, {"observation": [
        {"obs_type": "functional", "key": "financial_self_management",
         "observed_at": "2025-07-20",
         "value_text": "running balance column stops mid-page"},
    ]})
    md = render.render_brief(seeded, _upcoming_appt_id(seeded))
    ctx = md.split("## Procedures & Observations")[1].split("\n## ")[0]
    assert "functional financial_self_management" in ctx
    assert "running balance column stops mid-page" in ctx
    assert "2025-07-20" in ctx

    summary = render.render_summary(seeded, "jane-doe")
    assert "financial_self_management" not in \
        summary.split("## Latest Vitals")[1].split("\n## ")[0]
    assert "financial_self_management" not in \
        summary.split("## Orders & Referrals")[1].split("\n## ")[0]


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
    # `now` is pinned: every header carries a second-resolution `Generated:` stamp, so a
    # before/after byte-identity check straddling a second boundary fails spuriously.
    d = dedup.load_dictionary(DICT_PATH)
    now = datetime(2026, 6, 1)
    return {
        "summary": render.render_summary(conn, "jane-doe", dictionary=d, now=now),
        "brief": render.render_brief(conn, _upcoming_appt_id(conn), dictionary=d, now=now),
        "journal": render.render_journal(conn, "jane-doe", now=now),
    }


def test_no_verdicts_renders_byte_identically(seeded):
    """The load-bearing guarantee: the questions section is *omitted*, not rendered
    empty, so unannotated output is unchanged to the byte. Since issue #168 the same rule
    binds the curation record itself: no verdicts means no document at all."""
    before = _renders(seeded)
    # An empty `curation` table is the state every existing database is in.
    assert seeded.execute("SELECT COUNT(*) AS n FROM curation").fetchone()["n"] == 0
    after = _renders(seeded)
    assert after == before
    for md in before.values():
        assert "Superseded / corrected" not in md
        assert "Questions for the Clinician" not in md
        assert "DISPUTED" not in md
    assert render.render_curation(seeded, "jane-doe") == ""


@pytest.mark.parametrize("status", ["superseded", "erroneous-in-source"])
def test_superseded_family_leaves_its_section_for_the_curation_record(seeded, status):
    _annotate(seeded, "condition", "name", "Appendicitis", status=status,
              note="never actually confirmed")
    out = _renders(seeded)
    for name, md in out.items():
        # Gone from every clinical document, appendix and all (issue #168).
        assert "Superseded / corrected" not in md, name
        assert "Appendicitis" not in md, name
    # ... and the trail is the curation record, which is where it now lives alone.
    record = render.render_curation(seeded, "jane-doe")
    assert "condition: Appendicitis" in record
    assert "never actually confirmed" in record
    assert f"## {status}" in record          # grouped under its ruling
    # The row itself is untouched: a verdict is an overlay, never a delete.
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
    body = render.render_summary(seeded, "jane-doe",
                                 dictionary=dedup.load_dictionary(DICT_PATH))
    assert "Glucose, fasting" not in body
    assert "LDL" in body                                     # the target still renders
    record = render.render_curation(seeded, "jane-doe")
    assert "lab_result: Glucose, fasting" in record
    assert f"## merged into {target[:12]}..." in record
    # The target base is truncated and never resolved to a label: it may name another
    # person's family (issue #161), which must not leak into this person's document.
    assert target not in record


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


# --- row-scoped verdicts over a multi-row family (issue #114) ------------------


def _keep_both_sibling(conn, value_num=205.0):
    """File a second *live* occurrence of Jane's fasting glucose — the shape an earlier
    `--keep both` conflict resolution leaves behind — and return
    ``(base, occurrence 0 id, occurrence 1 id)``."""
    row = conn.execute(
        "SELECT lab_result_id, person_id, document_id, dedup_base FROM lab_result "
        "WHERE test_name = 'Glucose, fasting'"
    ).fetchone()
    base = row["dedup_base"]
    cur = conn.execute(
        "INSERT INTO lab_result (person_id, document_id, test_name, collected_at, "
        "value_num, unit, ref_high, dedup_key, dedup_base, dedup_occurrence) "
        "VALUES (?, ?, 'Glucose, fasting', '2026-01-01', ?, 'mg/dL', 100, ?, ?, 1)",
        (row["person_id"], row["document_id"], value_num,
         dedup.occurrence_key(base, 1), base),
    )
    conn.commit()
    return base, int(row["lab_result_id"]), int(cur.lastrowid)


def test_a_row_scoped_verdict_hides_one_occurrence_and_spares_its_sibling(seeded):
    """The bug that forced #114: a family-scoped `superseded` on a keep-both family
    removes the row the verdict says should *win*. Row scope is the fix, and the
    family-scoped contrast below is why it had to exist."""
    base, _occ0, occ1 = _keep_both_sibling(seeded)
    curation.annotate_record(seeded, "lab_result", str(occ1), status="superseded",
                             note="loser of an earlier keep-both", row=True, apply=True)

    body = render.render_summary(seeded, "jane-doe",
                                 dictionary=dedup.load_dictionary(DICT_PATH))
    assert "200.0" in body                  # the sibling renders normally
    assert "205.0" not in body              # the annotated occurrence does not
    record = render.render_curation(seeded, "jane-doe")
    # Listed once, not once per row of the family.
    assert record.count("lab_result: Glucose, fasting") == 1
    assert "loser of an earlier keep-both" in record
    assert "(row)" in record                # row scope, so it covers exactly one row

    # The contrast: the same verdict at family scope takes both rows with it.
    curation.clear_curation(seeded, "lab_result", str(occ1), row=True, apply=True)
    curation.annotate_record(seeded, "lab_result", base, status="superseded",
                             note="the whole family", apply=True)
    body = render.render_summary(
        seeded, "jane-doe", dictionary=dedup.load_dictionary(DICT_PATH)
    )
    assert "200.0" not in body and "205.0" not in body
    assert "(family, 2 rows)" in render.render_curation(seeded, "jane-doe")


def test_a_row_verdict_overrides_its_family_verdict_at_render_time(seeded):
    """Precedence, end to end: the family is disputed, one occurrence is superseded.
    The superseded row leaves the summary; its sibling renders in place, marked."""
    base, _occ0, occ1 = _keep_both_sibling(seeded)
    curation.annotate_record(seeded, "lab_result", base, status="disputed",
                             note="two sources disagree", apply=True)
    curation.annotate_record(seeded, "lab_result", str(occ1), status="superseded",
                             note="this one is the transcription error", row=True,
                             apply=True)

    body = render.render_summary(seeded, "jane-doe",
                                 dictionary=dedup.load_dictionary(DICT_PATH))
    assert "200.0" in body
    assert "[DISPUTED: two sources disagree]" in body
    assert "205.0" not in body
    record = render.render_curation(seeded, "jane-doe")
    assert "this one is the transcription error" in record
    # One line: the row verdict's, not one per row of the family. And the `disputed`
    # family verdict is not in the curation record at all -- it never left its section.
    assert record.count("lab_result: Glucose, fasting") == 1
    assert "two sources disagree" not in record


def test_a_distinct_verdict_keeps_both_siblings_live(seeded):
    """AC3 (issue #122). `distinct` is a *resolving* status but deliberately not an
    appendix one, so it changes rendering not at all: the two-live-row family a
    distinct-resolved rekey leaves behind keeps both rows in their normal section, and no
    `## Superseded / corrected` appendix appears at all.

    The contrast with `superseded` on the same family is the point - that one takes both
    rows out (see the keep-both test above); this one is the ruling that says both facts
    are real."""
    base, _occ0, _occ1 = _keep_both_sibling(seeded)
    before = _renders(seeded)

    curation.annotate_record(seeded, "lab_result", base, status="distinct",
                             note="two different draws under one generic label",
                             apply=True)

    after = _renders(seeded)
    assert after == before                  # like `confirmed`: recorded, not rendered
    for name, md in after.items():
        assert "Superseded / corrected" not in md, name
    summary = after["summary"]
    assert "200.0" in summary and "205.0" in summary


def test_a_row_scoped_verdict_filters_only_its_own_journal_event(seeded):
    """The journal resolves per row too, via the `record_id` `with_identity` now
    stamps: without it a row verdict could only ever be applied family-wide."""
    _base, _occ0, occ1 = _keep_both_sibling(seeded)
    before = render.render_journal(seeded, "jane-doe")
    assert before.count("Glucose, fasting") == 2

    curation.annotate_record(seeded, "lab_result", str(occ1), status="superseded",
                             note="loser", row=True, apply=True)

    md = render.render_journal(seeded, "jane-doe")
    timeline = md.split("## Superseded / corrected")[0]
    assert timeline.count("Glucose, fasting") == 1
    assert "205.0" not in timeline and "200.0" in timeline


# --- attested rows are never mistaken for document-sourced facts (issue #110) --

_ATTESTED = {
    "medication": {"name": "Amlodipine", "dose": "5 mg", "started_on": "2026-03-01"},
    "condition": {"name": "Migraine", "status": "active", "onset_on": "2026-03-01"},
    "allergy": {"substance": "Shellfish", "reaction": "hives"},
    "lab_result": {"test_name": "Potassium", "collected_at": "2026-03-01",
                   "value_num": 6.2, "unit": "mmol/L", "ref_high": 5.2},
    "procedure": {"name": "Wisdom tooth extraction", "performed_on": "2026-03-01"},
    "appointment": {"scheduled_for": "2099-06-01", "provider": "Dr. Attested",
                    "specialty": "Neurology", "reason": "headaches"},
    "observation": {"obs_type": "vital", "key": "pulse", "observed_at": "2026-03-01",
                    "value_num": 72, "unit": "bpm"},
}
_ATTEST_MARKER = "(attested by Aunt Ada 2026-03-02; no source document)"


def _attest_all(conn, slug="jane-doe"):
    """One live attestation per typed table, plus one attested order row."""
    from pemr import attestations

    for record_type, payload in _ATTESTED.items():
        attestations.assert_record(
            conn, record_type, slug, dict(payload), attributed_to="Aunt Ada",
            attested_on="2026-03-02", dictionary=dedup.load_dictionary(DICT_PATH),
            apply=True,
        )
    attestations.assert_record(
        conn, "observation", slug,
        {"obs_type": "order", "key": "sleep study", "observed_at": "2026-03-01"},
        attributed_to="Aunt Ada", attested_on="2026-03-02",
        dictionary=dedup.load_dictionary(DICT_PATH), apply=True,
    )


def test_unattested_renders_are_byte_identical_to_today(seeded):
    """The additive-only AC: a database with no attested rows renders exactly as it did
    before migration 009. Captured before/after seeding attestations for *john*, whose
    record stays untouched."""
    d = dedup.load_dictionary(DICT_PATH)
    before = (
        render.render_summary(seeded, "john-doe", dictionary=d,
                              now=datetime(2026, 6, 1)),
        render.render_journal(seeded, "john-doe", now=datetime(2026, 6, 1)),
    )
    _attest_all(seeded)
    after = (
        render.render_summary(seeded, "john-doe", dictionary=d,
                              now=datetime(2026, 6, 1)),
        render.render_journal(seeded, "john-doe", now=datetime(2026, 6, 1)),
    )
    assert before == after
    assert "attested" not in before[0] and "attested" not in before[1]


def test_summary_marks_an_attested_row_in_every_section(seeded):
    _attest_all(seeded)
    md = render.render_summary(
        seeded, "jane-doe", dictionary=dedup.load_dictionary(DICT_PATH),
        now=datetime(2026, 6, 1),
    )
    marked = {
        line.split("  ")[0].lstrip("- ")
        for line in md.splitlines() if _ATTEST_MARKER in line
    }
    # Meds, active problems, allergies, orders, vitals, abnormal labs, procedures,
    # appointments: every section that can show an attested row does, so none can render
    # it bare.
    for token in ("Amlodipine", "Migraine", "Shellfish", "sleep study", "pulse",
                  "Potassium", "Wisdom tooth extraction", "Dr. Attested"):
        assert any(
            token in line and _ATTEST_MARKER in line for line in md.splitlines()
        ), token
    assert marked  # sanity: the marker really is on rendered lines


def test_brief_marks_an_attested_row_in_every_section(seeded):
    _attest_all(seeded)
    md = render.render_brief(
        seeded, _upcoming_appt_id(seeded), dictionary=dedup.load_dictionary(DICT_PATH),
        recent_labs=50, now=datetime(2026, 6, 1),
    )
    for token in ("Amlodipine", "Potassium", "Shellfish", "Migraine",
                  "Wisdom tooth extraction", "pulse"):
        assert any(
            token in line and _ATTEST_MARKER in line for line in md.splitlines()
        ), token


def test_journal_marks_an_attested_event_and_gives_it_no_footnote(seeded):
    _attest_all(seeded)
    md = render.render_journal(seeded, "jane-doe", now=datetime(2026, 6, 1))
    line = next(l for l in md.splitlines() if "Amlodipine" in l)
    assert _ATTEST_MARKER in line
    assert "[^" not in line          # no document to cite


def test_a_promoted_row_renders_without_the_marker(seeded):
    """Once a document backs the fact it is an ordinary sourced row again; the
    attestation survives on the row as history, not as a caveat on the page."""
    _attest_all(seeded)
    doc = _doc(seeded, "jane-doe")
    summary = dedup.commit_extraction(
        seeded, doc, {"medication": [dict(_ATTESTED["medication"])]},
        dedup.load_dictionary(DICT_PATH),
    )
    assert summary.counts["promoted"] == 1
    md = render.render_summary(
        seeded, "jane-doe", dictionary=dedup.load_dictionary(DICT_PATH),
        now=datetime(2026, 6, 1),
    )
    line = next(l for l in md.splitlines() if "Amlodipine" in l)
    assert _ATTEST_MARKER not in line
    assert "Migraine" in md and _ATTEST_MARKER in md      # the others are still marked


def test_attested_marker_is_ascii(seeded):
    _attest_all(seeded)
    md = render.render_summary(
        seeded, "jane-doe", dictionary=dedup.load_dictionary(DICT_PATH),
        now=datetime(2026, 6, 1),
    )
    assert md.isascii()
    md.encode("cp437")


def test_a_disputed_attested_row_carries_both_markers(seeded):
    """Provenance reads last: the verdict is about the fact, the suffix about where it
    came from."""
    from pemr import attestations, curation as _curation

    _attest_all(seeded)
    row_id = attestations.list_attested(seeded, "medication")[0]["row_id"]
    _curation.annotate_record(
        seeded, "medication", str(row_id), status="disputed",
        note="pharmacy has no record", apply=True,
    )
    md = render.render_summary(
        seeded, "jane-doe", dictionary=dedup.load_dictionary(DICT_PATH),
        now=datetime(2026, 6, 1),
    )
    line = next(l for l in md.splitlines() if "Amlodipine" in l)
    assert line.index("[DISPUTED:") < line.index("(attested by")


# --- corrected rows are never mistaken for verbatim source text (issue #134) --
#
# The mirror image of the attestation section above: `record edit` leaves the row's
# provenance intact on purpose, so without a mark a corrected value would read as a
# quotation of a document that never said it.

_CORRECT_MARKER = "(corrected by Aunt Ada 2026-03-02)"
_CORRECT_AT = "2026-03-02T09:00:00+00:00"


def _rid(conn, record_type, where, params):
    return int(conn.execute(
        f"SELECT {record_type}_id FROM {record_type} WHERE {where}", params
    ).fetchone()[f"{record_type}_id"])


def _correct(conn, record_type, where, params, updates, who="Aunt Ada",
             identity=False):
    from pemr import records as _records

    return _records.edit_record(
        conn, record_type, _rid(conn, record_type, where, params), updates,
        dedup.load_dictionary(DICT_PATH), note="transcription slip",
        attributed_to=who, now=_CORRECT_AT, identity=identity, apply=True,
    )


def _correct_all(conn):
    """One in-place correction per typed table, all on jane."""
    _correct(conn, "lab_result", "test_name = ?", ("Glucose, fasting",), {"flag": "H"})
    _correct(conn, "medication", "name = ?", ("Metformin",), {"frequency": "daily"})
    _correct(conn, "procedure", "name = ?", ("Colonoscopy",),
             {"outcome": "normal, no polyps"})
    _correct(conn, "appointment", "provider = ?", ("Dr. Smith",),
             {"reason": "diabetes review"})
    _correct(conn, "observation", "key = ? AND observed_at = ?",
             ("weight", "2026-01-01"), {"value_num": 81})
    _correct(conn, "allergy", "substance = ?", ("Penicillin",),
             {"reaction": "rash and hives"})
    _correct(conn, "condition", "name = ? AND status = 'active'",
             ("Type 2 Diabetes",), {"note": "diet-controlled, reviewed"})


def test_uncorrected_renders_are_byte_identical_to_today(seeded):
    """The additive-only AC: a database with no corrected rows renders exactly as it did
    before migration 016. Captured before/after correcting *jane*, since john's record is
    the untouched control."""
    d = dedup.load_dictionary(DICT_PATH)
    before = (
        render.render_summary(seeded, "john-doe", dictionary=d,
                              now=datetime(2026, 6, 1)),
        render.render_journal(seeded, "john-doe", now=datetime(2026, 6, 1)),
    )
    _correct_all(seeded)
    after = (
        render.render_summary(seeded, "john-doe", dictionary=d,
                              now=datetime(2026, 6, 1)),
        render.render_journal(seeded, "john-doe", now=datetime(2026, 6, 1)),
    )
    assert before == after
    assert "corrected" not in before[0] and "corrected" not in before[1]


def test_summary_marks_a_corrected_row_in_every_section(seeded):
    _correct_all(seeded)
    md = render.render_summary(
        seeded, "jane-doe", dictionary=dedup.load_dictionary(DICT_PATH),
        now=datetime(2026, 6, 1),
    )
    for token in ("Metformin", "Type 2 Diabetes", "Penicillin", "weight",
                  "Glucose, fasting", "Colonoscopy", "Dr. Smith"):
        assert any(
            token in line and _CORRECT_MARKER in line for line in md.splitlines()
        ), token
    assert md.isascii()


def test_brief_marks_a_corrected_row(seeded):
    _correct_all(seeded)
    md = render.render_brief(
        seeded, _upcoming_appt_id(seeded), dictionary=dedup.load_dictionary(DICT_PATH),
        recent_labs=50, now=datetime(2026, 6, 1),
    )
    for token in ("Metformin", "Penicillin", "Type 2 Diabetes"):
        assert any(
            token in line and _CORRECT_MARKER in line for line in md.splitlines()
        ), token


def test_journal_marks_a_corrected_event(seeded):
    _correct_all(seeded)
    md = render.render_journal(seeded, "jane-doe", now=datetime(2026, 6, 1))
    line = next(l for l in md.splitlines() if "Metformin" in l)
    assert _CORRECT_MARKER in line
    # The footnote stays: the row is still filed under its source document, which is
    # exactly why the caveat is needed.
    assert "[^" in line


def test_an_unattributed_correction_renders_the_date_only_form(seeded):
    _correct(seeded, "medication", "name = ?", ("Metformin",),
             {"frequency": "daily"}, who="")
    md = render.render_summary(
        seeded, "jane-doe", dictionary=dedup.load_dictionary(DICT_PATH),
        now=datetime(2026, 6, 1),
    )
    line = next(l for l in md.splitlines() if "Metformin" in l)
    assert line.endswith("(corrected 2026-03-02)")


def test_adding_an_onset_to_a_document_sourced_condition_is_disclosed(seeded):
    """The issue's headline example (#134): a row that came from a document gains a date
    the document never stated, and says so.

    `--identity` since issue #152: giving a condition an onset now moves its key, so the
    edit is a one-row rekey. The disclosure rule is unchanged by that - an identity move
    stamps the correction mark exactly as an ordinary correction does, which is the point
    of pinning it here."""
    _correct(seeded, "condition", "name = ?", ("Chickenpox",), {"onset_on": "2001-05-01"},
             identity=True)
    md = render.render_summary(
        seeded, "jane-doe", dictionary=dedup.load_dictionary(DICT_PATH),
        now=datetime(2026, 6, 1),
    )
    line = next(l for l in md.splitlines() if "Chickenpox" in l)
    assert _CORRECT_MARKER in line


def test_a_superseded_attestation_that_was_corrected_shows_only_the_correction(seeded):
    """Two independent provenance facts, each with its own rule: a promoted attestation
    is deliberately unmarked, a correction always is."""
    _attest_all(seeded)
    doc = _doc(seeded, "jane-doe")
    dedup.commit_extraction(
        seeded, doc, {"medication": [dict(_ATTESTED["medication"])]},
        dedup.load_dictionary(DICT_PATH),
    )
    _correct(seeded, "medication", "name = ?", ("Amlodipine",), {"frequency": "daily"})
    md = render.render_summary(
        seeded, "jane-doe", dictionary=dedup.load_dictionary(DICT_PATH),
        now=datetime(2026, 6, 1),
    )
    line = next(l for l in md.splitlines() if "Amlodipine" in l)
    assert _CORRECT_MARKER in line
    assert _ATTEST_MARKER not in line


def test_a_disputed_corrected_row_carries_both_markers_in_order(seeded):
    """Verdict, then provenance, then correction: the verdict is about the fact, the
    correction about the value's fidelity to its source, so the caveat reads last."""
    from pemr import curation as _curation

    row_id = _rid(seeded, "medication", "name = ?", ("Metformin",))
    _correct(seeded, "medication", "name = ?", ("Metformin",), {"frequency": "daily"})
    _curation.annotate_record(
        seeded, "medication", str(row_id), status="disputed",
        note="pharmacy has no record", row=True, apply=True,
    )
    md = render.render_summary(
        seeded, "jane-doe", dictionary=dedup.load_dictionary(DICT_PATH),
        now=datetime(2026, 6, 1),
    )
    line = next(l for l in md.splitlines() if "Metformin" in l)
    assert line.index("[DISPUTED:") < line.index("(corrected by")


def test_a_mapping_without_the_mark_columns_reads_as_uncorrected(seeded):
    """A row off a restored pre-016 database carries no mark columns at all; the suffix
    goes through `.get()`, so it reads as "not corrected" rather than raising."""
    assert render._correction_suffix({"name": "Metformin"}) == ""
    md = render.render_summary(
        seeded, "jane-doe", dictionary=dedup.load_dictionary(DICT_PATH),
        now=datetime(2026, 6, 1),
    )
    assert "corrected" not in md


# --- display units: per-person canonical unit at render time (issue #136) -----
#
# The overlay is display-only: a preference converts what a summary *prints* and
# never touches a stored row, so every test here asserts both halves.

_NOW = datetime(2026, 6, 1)


def _summary(conn, slug="jane-doe"):
    return render.render_summary(
        conn, slug, dictionary=dedup.load_dictionary(DICT_PATH), now=_NOW
    )


def _line(md, needle):
    return next(line for line in md.splitlines() if needle in line)


def _observation_rows(conn):
    return [
        dict(r) for r in conn.execute(
            "SELECT * FROM observation ORDER BY observation_id"
        ).fetchall()
    ]


def test_summary_without_a_preference_is_unchanged(seeded):
    """AC-6: a person with no preference set sees pre-#136 output, byte for byte -- and
    a preference on a key they have no rows for changes nothing either."""
    before = _summary(seeded)
    units.set_pref(seeded, "jane-doe", "height", "cm",
                   dictionary=dedup.load_dictionary(DICT_PATH))
    assert _summary(seeded) == before


def test_summary_converts_a_vital_and_discloses_the_source(seeded):
    units.set_pref(seeded, "jane-doe", "weight", "lb",
                   dictionary=dedup.load_dictionary(DICT_PATH))
    line = _line(_summary(seeded), "weight:")
    assert "weight: 176.37 lb" in line
    assert "[converted from 80.0 kg]" in line
    # ...and the stored row still says what the document said.
    row = next(r for r in _observation_rows(seeded) if r["key"] == "weight")
    assert row["value_num"] == 80.0 and row["unit"] == "kg"


def test_summary_converts_each_key_independently(seeded):
    """The field-evidence case: one person's rows arrive in a second unit system, key by
    key, and each converts under its own preference while the rest are untouched."""
    d = dedup.load_dictionary(DICT_PATH)
    doc = _doc(seeded, "jane-doe", ocr="metric vitals")
    dedup.commit_extraction(seeded, doc, {"observation": [
        {"obs_type": "vital", "key": "height", "observed_at": "2026-01-01",
         "value_num": 165.1, "unit": "cm"},
        {"obs_type": "vital", "key": "temperature", "observed_at": "2026-01-01",
         "value_num": 36.6, "unit": "C"},
    ]}, d)
    before = _observation_rows(seeded)

    for key, unit in (("weight", "lb"), ("height", "in"), ("temperature", "degF")):
        units.set_pref(seeded, "jane-doe", key, unit, dictionary=d)
    md = _summary(seeded)

    assert "weight: 176.37 lb" in _line(md, "weight:")
    assert "height: 65.0 in" in _line(md, "height:")
    assert "temperature: 97.88 degF" in _line(md, "temperature:")
    # Affine, not a bare scale factor: 36.6 C is 97.88 F, never 20.33.
    assert "[converted from 36.6 C]" in _line(md, "temperature:")
    # blood_pressure has no preference and renders exactly as before.
    assert "blood_pressure: 118.0 mmHg  (2026-01-01)" in md
    assert _observation_rows(seeded) == before      # nothing was rewritten


def test_summary_converts_a_lab_value_with_its_reference_interval(seeded):
    """A value printed in lb beside a reference interval still in kg is a clinical
    misread, so the bounds move in the same step or not at all."""
    d = dedup.load_dictionary(DICT_PATH)
    doc = _doc(seeded, "jane-doe", ocr="dialysis flowsheet")
    dedup.commit_extraction(seeded, doc, {"lab_result": [
        {"test_name": "Dry Weight", "collected_at": "2026-02-01", "value_num": 90.0,
         "unit": "kg", "ref_low": 70.0, "ref_high": 80.0},
    ]}, d)
    units.set_pref(seeded, "jane-doe", "Dry Weight", "lb", dictionary=d)

    line = _line(_summary(seeded), "Dry Weight")
    assert "198.42 lb" in line
    assert "(ref 154.32-176.37)" in line
    assert "[converted from 90.0 kg]" in line
    row = seeded.execute(
        "SELECT * FROM lab_result WHERE test_name = 'Dry Weight'"
    ).fetchone()
    assert row["value_num"] == 90.0 and row["unit"] == "kg" and row["ref_high"] == 80.0


def test_a_preference_cannot_change_which_labs_are_abnormal(seeded):
    """Section membership is decided on stored values, so a conversion can neither
    smuggle a normal lab into the section nor drop an abnormal one out of it."""
    d = dedup.load_dictionary(DICT_PATH)
    doc = _doc(seeded, "jane-doe", ocr="dialysis flowsheet")
    dedup.commit_extraction(seeded, doc, {"lab_result": [
        # Abnormal only by its reference interval, with no flag.
        {"test_name": "Dry Weight", "collected_at": "2026-02-01", "value_num": 90.0,
         "unit": "kg", "ref_high": 80.0},
        # Comfortably normal.
        {"test_name": "Post Weight", "collected_at": "2026-02-01", "value_num": 75.0,
         "unit": "kg", "ref_low": 70.0, "ref_high": 80.0},
    ]}, d)
    plain = _summary(seeded)
    for key in ("Dry Weight", "Post Weight"):
        units.set_pref(seeded, "jane-doe", key, "lb", dictionary=d)
    converted = _summary(seeded)

    assert "Dry Weight" in plain and "Dry Weight" in converted
    assert "Post Weight" not in plain and "Post Weight" not in converted


def test_rows_that_cannot_convert_render_exactly_as_before(seeded):
    """An unregistered unit, a unit-less row and a text-only vital all resolve to
    "print the stored value", with no suffix -- never a guessed scale."""
    d = dedup.load_dictionary(DICT_PATH)
    doc = _doc(seeded, "jane-doe", ocr="mixed vitals")
    dedup.commit_extraction(seeded, doc, {"observation": [
        {"obs_type": "vital", "key": "pain_score", "observed_at": "2026-01-01",
         "value_num": 4.0, "unit": "widgets"},
        {"obs_type": "vital", "key": "spo2", "observed_at": "2026-01-01",
         "value_num": 97.0},
        {"obs_type": "vital", "key": "gait", "observed_at": "2026-01-01",
         "value_text": "steady"},
    ]}, d)
    for key, unit in (("pain_score", "lb"), ("spo2", "%"), ("gait", "lb"),
                      ("weight", "cm")):   # cross-dimension preference on weight
        units.set_pref(seeded, "jane-doe", key, unit, dictionary=d)
    md = _summary(seeded)

    assert "pain_score: 4.0 widgets" in md
    assert "spo2: 97.0  (2026-01-01)" in md
    assert "gait: steady" in md
    assert "weight: 80.0 kg" in md          # kg -> cm is refused, not fudged
    assert "converted from" not in md


def test_converted_renders_stay_ascii_and_read_only(seeded):
    d = dedup.load_dictionary(DICT_PATH)
    before = _row_counts(seeded)
    for key, unit in (("weight", "lb"), ("hba1c", "%")):
        units.set_pref(seeded, "jane-doe", key, unit, dictionary=d)
    md = _summary(seeded)
    assert "converted from" in md
    assert md.isascii()
    md.encode("cp437")                       # cp1252/cp437 console contract
    render.render_brief(seeded, _upcoming_appt_id(seeded), dictionary=d)
    render.render_journal(seeded, "jane-doe")
    assert _row_counts(seeded) == before


def test_brief_and_journal_are_deliberately_not_converted(seeded):
    """Only the two read paths the issue named honour a preference; widening that
    silently would be scope this feature did not ask for."""
    d = dedup.load_dictionary(DICT_PATH)
    units.set_pref(seeded, "jane-doe", "weight", "lb", dictionary=d)
    brief = render.render_brief(seeded, _upcoming_appt_id(seeded), dictionary=d,
                                now=_NOW)
    journal = render.render_journal(seeded, "jane-doe", now=_NOW)
    assert "80.0 kg" in brief and "converted from" not in brief
    assert "80.0 kg" in journal and "converted from" not in journal


# --- the curation record: the audit trail as its own target (issue #168) -------


def _record(conn, slug="jane-doe"):
    return render.render_curation(conn, slug, now=datetime(2026, 6, 1))


def test_curation_record_header_is_self_identifying_and_counts_its_scope(seeded):
    _annotate(seeded, "condition", "name", "Appendicitis", status="superseded",
              note="never actually confirmed", attributed_to="mkd")
    md = _record(seeded)
    assert md.startswith("# Curation Record: Jane Doe\n")
    assert "- Person: jane-doe" in md
    assert "- Generated: 2026-06-01T00:00:00 (read-only view of DB state)" in md
    assert "- Verdicts: 1 covering 1 row" in md
    assert "## superseded (mkd)" in md
    assert "_1 verdict, 1 row_" in md
    assert "> never actually confirmed" in md
    assert "- condition: Appendicitis  (family, 1 row)" in md


def test_curation_record_groups_verdicts_that_share_a_ruling(seeded):
    """The observed complaint: one merge session's note recorded against many families
    rendered as one near-identical bullet per family. Same ruling -> one block."""
    for rtype, column, value in (("condition", "name", "Appendicitis"),
                                 ("condition", "name", "Chickenpox"),
                                 ("allergy", "substance", "Sulfa")):
        _annotate(seeded, rtype, column, value, status="superseded",
                  note="duplicate portal import", attributed_to="mkd")
    md = _record(seeded)
    assert md.count("## superseded (mkd)") == 1
    assert md.count("> duplicate portal import") == 1
    assert "_3 verdicts, 3 rows_" in md
    assert "- Verdicts: 3 covering 3 rows" in md
    for target in ("condition: Appendicitis", "condition: Chickenpox", "allergy: Sulfa"):
        assert target in md


@pytest.mark.parametrize("differing", ["status", "note", "attributed_to"])
def test_curation_record_splits_rulings_that_differ_in_any_key(seeded, differing):
    """The group key is the ruling itself. Differ anywhere in it and they are two
    rulings, however similar they read."""
    base = dict(status="superseded", note="duplicate portal import",
                attributed_to="mkd")
    other = dict(base, **{differing: {"status": "erroneous-in-source",
                                      "note": "wrong patient",
                                      "attributed_to": "dr-who"}[differing]})
    _annotate(seeded, "condition", "name", "Appendicitis", **base)
    _annotate(seeded, "condition", "name", "Chickenpox", **other)
    md = _record(seeded)
    assert md.count("\n## ") == 2
    assert "- Verdicts: 2 covering 2 rows" in md


def test_curation_record_row_arithmetic_counts_the_family(seeded):
    """A family-scoped verdict covers every occurrence; a row-scoped one covers one.
    The counts are what make the grouping auditable rather than an assertion."""
    base, _occ0, occ1 = _keep_both_sibling(seeded)
    curation.annotate_record(seeded, "lab_result", base, status="superseded",
                             note="the whole family", apply=True)
    md = _record(seeded)
    assert "_1 verdict, 2 rows_" in md
    assert "- lab_result: Glucose, fasting  (family, 2 rows)" in md
    assert "- Verdicts: 1 covering 2 rows" in md

    curation.clear_curation(seeded, "lab_result", base, apply=True)
    curation.annotate_record(seeded, "lab_result", str(occ1), status="superseded",
                             note="just this occurrence", row=True, apply=True)
    md = _record(seeded)
    assert "_1 verdict, 1 row_" in md
    assert "- lab_result: Glucose, fasting  (row)" in md


def test_curation_record_puts_a_multi_line_note_in_a_blockquote(seeded):
    """The heading must stay one line whatever the operator typed -- a multi-line `##`
    is broken Markdown, and multi-line merge notes are exactly what prompted the move."""
    _annotate(seeded, "condition", "name", "Appendicitis", status="superseded",
              note="first line\nsecond line", attributed_to="mkd")
    md = _record(seeded)
    assert "## superseded (mkd)\n" in md
    assert "> first line\n> second line\n" in md


def test_curation_group_block_omits_the_blockquote_for_an_empty_note():
    """A block-level unit test on purpose: `record annotate` demands a note and the
    `curation` table CHECKs it non-blank + NOT NULL, so no fixture can reach this state
    through the DB. The guard still has to exist -- a bare `>` is broken Markdown -- so
    it is exercised where it lives."""
    block = render._curation_group_block(
        {"heading": "superseded", "note": "", "verdicts": 1, "rows": 1,
         "targets": ["- condition: Appendicitis  (family, 1 row)"]}
    )
    assert ">" not in block
    assert block == (
        "## superseded\n\n_1 verdict, 1 row_\n\n"
        "- condition: Appendicitis  (family, 1 row)\n"
    )


def test_curation_record_carries_only_appendix_status_verdicts(seeded):
    """`disputed`/`confirmed`/`distinct` never left their section, so they have no audit
    trail to carry -- they are still on the page where the reader can see them."""
    _annotate(seeded, "allergy", "substance", "Sulfa", status="disputed",
              note="two notes disagree")
    _annotate(seeded, "allergy", "substance", "Penicillin", status="confirmed",
              note="clinician agreed")
    assert _record(seeded) == ""

    target = seeded.execute(
        "SELECT dedup_base FROM condition WHERE name = 'Chickenpox'"
    ).fetchone()["dedup_base"]
    _annotate(seeded, "condition", "name", "Appendicitis", status="merged-into",
              merged_into_base=target, note="one episode, two notes")
    md = _record(seeded)
    assert "condition: Appendicitis" in md
    assert "two notes disagree" not in md and "clinician agreed" not in md


def test_curation_record_is_scoped_to_one_person(seeded):
    """John's verdict is John's business. A family is single-person by construction
    (`dedup._key_parts` folds `person_id` in), so scoping is a lookup, not a guess."""
    _annotate(seeded, "condition", "name", "Appendicitis", status="superseded",
              note="jane's ruling")
    johns = seeded.execute(
        "SELECT dedup_base FROM lab_result WHERE person_id = "
        "(SELECT person_id FROM person WHERE slug='john-doe')"
    ).fetchone()["dedup_base"]
    curation.annotate_record(seeded, "lab_result", johns, status="superseded",
                             note="john's ruling", apply=True)

    jane = _record(seeded)
    assert "jane's ruling" in jane and "john's ruling" not in jane
    john = _record(seeded, "john-doe")
    assert "john's ruling" in john and "jane's ruling" not in john


def test_curation_record_excludes_an_orphan_verdict(seeded):
    """A verdict whose target is gone has no person to scope it to, so it cannot appear
    here -- unchanged from the block this replaced. `pemr verify` / `record reaffirm`
    are its surfaces."""
    base = _annotate(seeded, "condition", "name", "Appendicitis", status="superseded",
                     note="never actually confirmed")
    assert "Appendicitis" in _record(seeded)
    seeded.execute("DELETE FROM condition WHERE dedup_base = ?", (base,))
    seeded.commit()
    assert curation.get_verdict(seeded, "condition", base) is not None
    assert _record(seeded) == ""


def test_curation_record_skips_an_unknown_record_type_without_raising(seeded):
    """A hand-edited `curation` row can name anything, and `row_person`/`family_person`
    interpolate the type into a table name. The membership guard runs first."""
    _annotate(seeded, "condition", "name", "Appendicitis", status="superseded",
              note="the good one")
    seeded.execute(
        "INSERT INTO curation (record_type, dedup_base, record_id, status, note, "
        "created_at) VALUES ('not_a_table', 'deadbeef', 0, 'superseded', 'hand-edited', "
        "'2026-01-01T00:00:00')"
    )
    seeded.commit()
    md = _record(seeded)
    assert "the good one" in md
    assert "hand-edited" not in md and "not_a_table" not in md


def test_curation_record_is_empty_for_a_person_with_no_verdicts(seeded):
    assert _record(seeded, "john-doe") == ""


def test_curation_record_unknown_slug_raises(seeded):
    with pytest.raises(query.PersonNotFoundError):
        render.render_curation(seeded, "nobody")


def test_curation_record_degrades_on_a_pre_008_snapshot(tmp_path):
    """A snapshot without the `curation` table renders the empty document rather than
    raising `no such table` (the `has_table` degradation convention)."""
    conn = db.connect(tmp_path / "old.db")
    db.migrate(conn)
    persons.add_person(conn, "jane-doe", "Jane Doe")
    conn.execute("DROP TABLE curation")
    conn.commit()
    try:
        assert render.render_curation(conn, "jane-doe") == ""
    finally:
        conn.close()


def test_curation_record_stays_ascii_and_read_only(seeded):
    before = _row_counts(seeded)
    _annotate(seeded, "condition", "name", "Appendicitis", status="superseded",
              note="duplicate portal import", attributed_to="mkd")
    md = _record(seeded)
    assert md.isascii(), f"non-ASCII would crash a cp437 console: {md!r}"
    md.encode("cp437")
    assert _row_counts(seeded) == before      # still a pure read


def test_curation_record_never_resolves_a_cross_person_merge_target(seeded):
    """Issue #161: the merge target may name another person's family. The heading prints
    the truncated base and never a resolved label, so nothing of John's leaks into
    Jane's document."""
    johns = seeded.execute(
        "SELECT dedup_base FROM lab_result WHERE person_id = "
        "(SELECT person_id FROM person WHERE slug='john-doe')"
    ).fetchone()["dedup_base"]
    _annotate(seeded, "lab_result", "test_name", "Glucose, fasting",
              status="merged-into", merged_into_base=johns,
              note="same draw, filed twice", allow_cross_person=True)
    md = _record(seeded)
    assert f"## merged into {johns[:12]}..." in md
    assert johns not in md                      # truncated, never printed in full
    assert "LDL" not in md                      # the target's label never resolved


# --- self-reported symptoms (issue #167) --------------------------------------
#
# The lane's whole point is the *collapse*: a recurring complaint is reported over and
# over, and a chronological dump ("achy on the 16th / fine on the 18th") is worse than
# nothing. So the assertions here are about one line per key carrying the state of the
# complaint, and about everything the section is deliberately NOT.

_SYMPTOM_NOW = datetime(2026, 8, 18, 9, 0)
_SYMPTOM_HEADER = "## Self-Reported Symptoms"


def _seed_symptoms(conn, rows, slug="jane-doe"):
    """Attest a batch of self-reported rows the way `pemr record assert` does."""
    from pemr import attestations

    d = dedup.load_dictionary(DICT_PATH)
    for row in rows:
        attestations.assert_record(
            conn, "observation", slug, dict(row), attributed_to="Jane Doe",
            attested_on="2026-08-18", dictionary=d, apply=True,
        )


def _symptom_row(observed_at, key="right foot ache", value_num=3,
                 value_text="achy after the walk", obs_type="symptom"):
    return {"obs_type": obs_type, "key": key, "observed_at": observed_at,
            "value_num": value_num, "value_text": value_text}


def _symptom_summary(conn, slug="jane-doe", now=_SYMPTOM_NOW):
    return render.render_summary(
        conn, slug, dictionary=dedup.load_dictionary(DICT_PATH), now=now
    )


def test_summary_collapses_repeat_symptom_reports_into_one_line(seeded):
    """T5: four in-window reports fold to one line carrying the count, the latest date
    and the latest severity -- and an older report outside the window is not counted."""
    _seed_symptoms(seeded, [
        _symptom_row("2026-06-01T09:00", value_num=7),   # outside the 30d window
        _symptom_row("2026-07-25T09:00", value_num=5),
        _symptom_row("2026-08-02T09:00", value_num=4),
        _symptom_row("2026-08-10T09:00", value_num=4),
        _symptom_row("2026-08-16T09:00", value_num=3),
    ])
    line = _line(_symptom_summary(seeded), "right foot ache")
    assert line.startswith(
        "- right foot ache - 4 reports in 30d, latest 2026-08-16 (severity 3/10)"
    )


def test_summary_symptom_line_pluralizes_and_drops_a_missing_severity(seeded):
    """A lone report says 'report', and a value_num-less row is a present report of
    unknown severity -- not a resolution, and not a fabricated 0."""
    _seed_symptoms(seeded, [_symptom_row("2026-08-17T08:00", key="jaw click",
                                         value_num=None)])
    line = _line(_symptom_summary(seeded), "jaw click")
    assert line.startswith("- jaw click - 1 report in 30d, latest 2026-08-17")
    assert "severity" not in line


def test_summary_keeps_a_qualified_symptom_on_its_own_line(seeded):
    """Grouped on `key_token`, matching the dedup key: a meaningful qualifier is its own
    complaint, not an overwrite of the plain one (the `_latest_vitals` rule)."""
    _seed_symptoms(seeded, [
        _symptom_row("2026-08-16T09:00", key="right foot ache", value_num=3),
        _symptom_row("2026-08-17T09:00", key="right foot ache (morning)", value_num=6),
    ])
    md = _symptom_summary(seeded)
    assert "- right foot ache - 1 report in 30d" in md
    assert "- right foot ache (morning) - 1 report in 30d" in md


def test_summary_reports_a_resolution_as_the_last_word(seeded):
    """T6: value_num == 0 is 'reported resolved' -- the 0-10 scale's natural bottom, no
    sentinel and no second obs_type."""
    _seed_symptoms(seeded, [
        _symptom_row("2026-08-16T09:00", value_num=3),
        _symptom_row("2026-08-18T08:00", value_num=0, value_text="fine today"),
    ])
    line = _line(_symptom_summary(seeded), "right foot ache")
    assert line.startswith(
        "- right foot ache - 2 reports in 30d, latest 2026-08-16 (severity 3/10)"
        "; last reported resolved 2026-08-18"
    )


def test_summary_drops_a_resolution_older_than_the_latest_report(seeded):
    """A resolution the complaint has since outlived is not news."""
    _seed_symptoms(seeded, [
        _symptom_row("2026-08-10T09:00", value_num=0),
        _symptom_row("2026-08-16T09:00", value_num=3),
    ])
    line = _line(_symptom_summary(seeded), "right foot ache")
    assert "latest 2026-08-16 (severity 3/10)" in line
    assert "resolved" not in line


def test_summary_renders_a_key_whose_only_reports_are_resolutions(seeded):
    """Head + resolved clause, no `latest` clause: nothing is asserted to be present."""
    _seed_symptoms(seeded, [_symptom_row("2026-08-18T08:00", value_num=0)])
    line = _line(_symptom_summary(seeded), "right foot ache")
    assert line.startswith(
        "- right foot ache - 1 report in 30d; last reported resolved 2026-08-18"
    )
    assert "severity" not in line and "latest 2026" not in line


def test_summary_sorts_symptoms_by_most_recent_report(seeded):
    _seed_symptoms(seeded, [
        _symptom_row("2026-08-02T09:00", key="jaw click"),
        _symptom_row("2026-08-17T09:00", key="right foot ache"),
    ])
    md = _symptom_summary(seeded)
    assert md.index("right foot ache") < md.index("jaw click")


def test_summary_omits_the_symptom_section_entirely_when_empty(seeded):
    """T7, the additive-only guarantee. `_none recorded_` would read as "the patient
    reports no symptoms" -- an assertion the record cannot make -- and it would change
    the rendered output of every person who never uses the lane."""
    assert _SYMPTOM_HEADER not in _symptom_summary(seeded)


def test_summary_omits_the_symptom_section_when_every_report_is_stale(seeded):
    _seed_symptoms(seeded, [_symptom_row("2026-01-05T09:00")])
    assert _SYMPTOM_HEADER not in _symptom_summary(seeded)


def test_a_record_without_self_reports_renders_byte_identically(seeded):
    """The strongest form of the additive-only rule: seeding *jane's* symptoms cannot
    move a byte of *john's* summary or journal."""
    d = dedup.load_dictionary(DICT_PATH)
    before = (
        render.render_summary(seeded, "john-doe", dictionary=d, now=_SYMPTOM_NOW),
        render.render_journal(seeded, "john-doe", now=_SYMPTOM_NOW),
    )
    _seed_symptoms(seeded, [_symptom_row("2026-08-16T09:00")])
    after = (
        render.render_summary(seeded, "john-doe", dictionary=d, now=_SYMPTOM_NOW),
        render.render_journal(seeded, "john-doe", now=_SYMPTOM_NOW),
    )
    assert before == after


def test_activity_renders_in_no_summary_section(seeded):
    """Activity is the higher-volume, lower-signal lane: it stays reachable through
    `query`, `trends` and the journal's opt-in flag, and renders on no summary page."""
    _seed_symptoms(seeded, [
        _symptom_row("2026-08-16T07:30", key="morning walk", obs_type="activity",
                     value_num=40, value_text="two miles, easy"),
    ])
    md = _symptom_summary(seeded)
    assert "morning walk" not in md
    assert _SYMPTOM_HEADER not in md            # activity alone opens no section


def test_self_reports_never_reach_the_problem_list(seeded):
    """The load-bearing invariant: neither lane may reach `condition` or Active
    Problems, in either clinical document."""
    _seed_symptoms(seeded, [
        _symptom_row("2026-08-16T09:00"),
        _symptom_row("2026-08-16T07:30", key="morning walk", obs_type="activity"),
    ])
    md = _symptom_summary(seeded)
    active = md.split("## Active Problems")[1].split("\n## ")[0]
    assert "right foot ache" not in active and "morning walk" not in active
    assert seeded.execute(
        "SELECT COUNT(*) AS n FROM condition WHERE name LIKE '%foot%'"
    ).fetchone()["n"] == 0
    brief = render.render_brief(
        seeded, _upcoming_appt_id(seeded), dictionary=dedup.load_dictionary(DICT_PATH),
        now=_SYMPTOM_NOW,
    )
    brief_active = brief.split("## Active Problems")[1].split("\n## ")[0]
    assert "right foot ache" not in brief_active and "morning walk" not in brief_active
    # The brief gains no collapsed section either -- that one stays the summary's. Its
    # generic recent-observations list is opt-in for these lanes as of issue #180; the
    # pair of tests below covers both sides of that flag.
    assert _SYMPTOM_HEADER not in brief


def test_a_symptom_line_says_it_is_attested(seeded):
    """T8: `record assert` is the only entry path, so the line must never read as a
    document-sourced fact."""
    _seed_symptoms(seeded, [_symptom_row("2026-08-16T09:00")])
    line = _line(_symptom_summary(seeded), "right foot ache")
    assert line.endswith("(attested by Jane Doe 2026-08-18; no source document)")


def test_a_superseded_report_is_neither_counted_nor_latest(seeded):
    """Curation runs before the fold, as in `_latest_vitals`: a superseded report can
    neither win 'latest' nor inflate the count."""
    _seed_symptoms(seeded, [
        _symptom_row("2026-08-10T09:00", value_num=4),
        _symptom_row("2026-08-16T09:00", value_num=9, value_text="mis-typed severity"),
    ])
    base = seeded.execute(
        "SELECT dedup_base FROM observation WHERE observed_at = '2026-08-16T09:00'"
    ).fetchone()["dedup_base"]
    curation.annotate_record(seeded, "observation", base, status="superseded",
                             note="severity mis-typed", apply=True)
    line = _line(_symptom_summary(seeded), "right foot ache")
    assert line.startswith(
        "- right foot ache - 1 report in 30d, latest 2026-08-10 (severity 4/10)"
    )


def test_symptom_section_is_console_safe(seeded):
    _seed_symptoms(seeded, [_symptom_row("2026-08-16T09:00")])
    md = _symptom_summary(seeded)
    assert md.isascii(), f"non-ASCII would crash a cp437 console: {md!r}"
    md.encode("cp437")


def test_rendering_symptoms_changes_no_rows(seeded):
    _seed_symptoms(seeded, [_symptom_row("2026-08-16T09:00")])
    before = _row_counts(seeded)
    _symptom_summary(seeded)
    render.render_journal(seeded, "jane-doe", include_self_reported=True)
    assert _row_counts(seeded) == before


# --- journal filtering (issue #167) -------------------------------------------

def test_journal_excludes_self_reports_by_default(seeded):
    """T9: a few hundred attestations a year would swamp a chronology spanning decades,
    so the journal is the one reader that filters them out."""
    _seed_symptoms(seeded, [
        _symptom_row("2026-08-16T09:00"),
        _symptom_row("2026-08-16T07:30", key="morning walk", obs_type="activity"),
    ])
    md = render.render_journal(seeded, "jane-doe", now=_SYMPTOM_NOW)
    assert "right foot ache" not in md and "morning walk" not in md
    assert "blood_pressure" in md          # control: other observations still there


def test_journal_includes_self_reports_on_request(seeded):
    _seed_symptoms(seeded, [
        _symptom_row("2026-08-16T09:00"),
        _symptom_row("2026-08-16T07:30", key="morning walk", obs_type="activity"),
    ])
    md = render.render_journal(
        seeded, "jane-doe", now=_SYMPTOM_NOW, include_self_reported=True
    )
    assert "symptom right foot ache" in md
    assert "activity morning walk" in md


# --- brief filtering + mixed-separator ordering (issue #180) -------------------

_BRIEF_OBS_HEADER = "## Procedures & Observations"


def _brief(conn, **kwargs):
    return render.render_brief(
        conn, _upcoming_appt_id(conn), dictionary=dedup.load_dictionary(DICT_PATH),
        now=_SYMPTOM_NOW, **kwargs
    )


def _brief_observations(md):
    return md.split(_BRIEF_OBS_HEADER)[1].split("\n## ")[0]


def test_brief_hides_self_reports_by_default(seeded):
    """The drowning failure mode #167 fixed for the journal, left open on the brief: the
    section is uncapped, so a few hundred attestations a year push the clinician-sourced
    observations the document exists to carry off the top of it."""
    _seed_symptoms(seeded, [
        _symptom_row("2026-08-16T09:00"),
        _symptom_row("2026-08-16T07:30", key="morning walk", obs_type="activity",
                     value_num=40),
    ])
    section = _brief_observations(_brief(seeded))
    assert "right foot ache" not in section and "morning walk" not in section


def test_brief_includes_self_reports_on_request(seeded):
    _seed_symptoms(seeded, [
        _symptom_row("2026-08-16T09:00"),
        _symptom_row("2026-08-16T07:30", key="morning walk", obs_type="activity",
                     value_num=40),
    ])
    section = _brief_observations(_brief(seeded, include_self_reported=True))
    assert "symptom right foot ache" in section
    assert "activity morning walk" in section


def test_brief_still_shows_clinician_sourced_observations(seeded):
    """The exclusion is scoped to the two self-attested lanes -- it drops no `vital`,
    `order` or other clinician-sourced row."""
    _seed_symptoms(seeded, [_symptom_row("2026-08-16T09:00")])
    default = _brief_observations(_brief(seeded))
    opted_in = _brief_observations(_brief(seeded, include_self_reported=True))
    for row in seeded.execute(
        "SELECT DISTINCT key FROM observation WHERE obs_type NOT IN ('symptom','activity')"
        " AND key IS NOT NULL"
    ).fetchall():
        assert row["key"] in default, row["key"]
        assert row["key"] in opted_in, row["key"]


def test_a_record_without_self_reports_renders_a_byte_identical_brief(seeded):
    """AC bullet 1's backward-compatibility half: the default output moves for records
    that use the lanes, and for nobody else."""
    before = _brief(seeded)
    _seed_symptoms(seeded, [_symptom_row("2026-08-16T09:00")])
    # john-doe's brief is a different appointment; jane's own brief is the one that
    # changes. Seeding jane cannot move a byte of the sections that carry neither.
    assert _brief_observations(_brief(seeded)) == _brief_observations(before)


@pytest.mark.parametrize("reverse", [False, True])
def test_symptom_latest_survives_a_mixed_separator_timestamp(seeded, reverse):
    """The AC's named regression test. `T` (0x54) sorts after a space (0x20), so raw
    lexicographic ordering made the 09:00 report outrank the 20:00 resolution and the
    `; last reported resolved` clause silently vanished."""
    rows = [
        _symptom_row("2026-08-18T09:00", value_num=3),
        _symptom_row("2026-08-18 20:00", value_num=0, value_text="fine now"),
    ]
    _seed_symptoms(seeded, list(reversed(rows)) if reverse else rows)
    line = _line(_symptom_summary(seeded), "right foot ache")
    assert "; last reported resolved 2026-08-18" in line


@pytest.mark.parametrize("reverse", [False, True])
def test_latest_vitals_picks_the_newest_across_mixed_separators(seeded, reverse):
    """The same hazard on the `_latest_vitals` fold, which the symptom test does not
    exercise: it shares the `ORDER BY observed_at` idiom and the ascending-last-wins
    fold, so a mis-ordered pair silently renders the older reading."""
    rows = [
        {"obs_type": "vital", "key": "heart_rate", "observed_at": "2026-08-18T09:00",
         "value_num": 61, "unit": "bpm"},
        {"obs_type": "vital", "key": "heart_rate", "observed_at": "2026-08-18 20:00",
         "value_num": 88, "unit": "bpm"},
    ]
    _seed_symptoms(seeded, list(reversed(rows)) if reverse else rows)
    line = _line(_symptom_summary(seeded), "heart_rate")
    assert "88" in line and "61" not in line

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

    md = render.render_summary(seeded, "jane-doe",
                               dictionary=dedup.load_dictionary(DICT_PATH))
    body, appendix = md.split("## Superseded / corrected")
    assert "200.0" in body                  # the sibling renders normally
    assert "205.0" not in body              # the annotated occurrence does not
    # Listed once, not once per row of the family.
    assert appendix.count("lab_result: Glucose, fasting") == 1
    assert "loser of an earlier keep-both" in appendix

    # The contrast: the same verdict at family scope takes both rows with it.
    curation.clear_curation(seeded, "lab_result", str(occ1), row=True, apply=True)
    curation.annotate_record(seeded, "lab_result", base, status="superseded",
                             note="the whole family", apply=True)
    body = render.render_summary(
        seeded, "jane-doe", dictionary=dedup.load_dictionary(DICT_PATH)
    ).split("## Superseded / corrected")[0]
    assert "200.0" not in body and "205.0" not in body


def test_a_row_verdict_overrides_its_family_verdict_at_render_time(seeded):
    """Precedence, end to end: the family is disputed, one occurrence is superseded.
    The superseded row leaves for the appendix; its sibling renders in place, marked."""
    base, _occ0, occ1 = _keep_both_sibling(seeded)
    curation.annotate_record(seeded, "lab_result", base, status="disputed",
                             note="two sources disagree", apply=True)
    curation.annotate_record(seeded, "lab_result", str(occ1), status="superseded",
                             note="this one is the transcription error", row=True,
                             apply=True)

    md = render.render_summary(seeded, "jane-doe",
                               dictionary=dedup.load_dictionary(DICT_PATH))
    body, appendix = md.split("## Superseded / corrected")
    assert "200.0" in body
    assert "[DISPUTED: two sources disagree]" in body
    assert "205.0" not in body
    assert "this one is the transcription error" in appendix
    # One appendix line: the row verdict's, not one per row of the family.
    assert appendix.count("lab_result: Glucose, fasting") == 1


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
    # Meds, active problems, allergies, orders, vitals, abnormal labs, appointments:
    # every section that can show an attested row does, so none can render it bare.
    for token in ("Amlodipine", "Migraine", "Shellfish", "sleep study", "pulse",
                  "Potassium", "Dr. Attested"):
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

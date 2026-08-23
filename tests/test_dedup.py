"""Layer-2 dedup: norm()/dictionary, dedup_key determinism, schema validation,
new/duplicate/conflict split via commit_extraction."""

import pytest

from pemr import curation, db, dedup, persons

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


# --- issue #166: the render-only routine-procedure list ------------------------

def test_load_routine_procedures_missing_or_none(tmp_path):
    """No file, no list: the summary suppresses nothing, which is the default-show rule
    holding even when the dictionary itself is absent."""
    assert dedup.load_routine_procedures(None) == ()
    assert dedup.load_routine_procedures(tmp_path / "nope.toml") == ()


def test_load_routine_procedures_absent_table(tmp_path):
    """A dictionary that predates this feature (only `[synonyms]`) is not an error and
    not a partial filter -- it suppresses nothing."""
    p = tmp_path / "d.toml"
    p.write_text('[synonyms]\n"a1c" = "hba1c"\n', encoding="utf-8")
    assert dedup.load_routine_procedures(p) == ()

    empty = tmp_path / "e.toml"
    empty.write_text("[procedures]\nroutine = []\n", encoding="utf-8")
    assert dedup.load_routine_procedures(empty) == ()


def test_load_routine_procedures_normalizes_and_dedupes(tmp_path):
    """Authoring is lenient the way `[synonyms]` keys are: case, padding and underscores
    collapse, blanks drop, duplicates drop, and the authored order survives."""
    p = tmp_path / "d.toml"
    p.write_text(
        "[procedures]\n"
        'routine = ["  Office   Visit ", "office_visit", "", "Venipuncture"]\n',
        encoding="utf-8",
    )
    assert dedup.load_routine_procedures(p) == ("office visit", "venipuncture")


def test_example_dictionary_keeps_both_tables():
    """The shipped example must yield BOTH overlays. `[synonyms]` runs to EOF, so a
    `[procedures]` header placed above it would silently swallow every following synonym
    key -- this is the guard on that placement."""
    assert dedup.load_dictionary(DICT_PATH)          # synonyms survived the new table
    assert dedup.norm("A1c", dedup.load_dictionary(DICT_PATH)) == "hba1c"
    routine = dedup.load_routine_procedures(DICT_PATH)
    assert routine and "office visit" in routine


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


def test_declared_full_label_tolerates_edge_underscores_and_paren_padding():
    """Rule 1 matches the *collapsed* label, so the collapse has to be total: a
    leading/trailing `_` (which becomes whitespace) or padding inside the parentheses
    used to miss the declared entry and over-split a label the human already settled."""
    d = dedup.load_dictionary(DICT_PATH)
    for sloppy in ("_m-spike (spep)", "M-Spike ( SPEP )", "  m-spike (spep)_",
                   "M-SPIKE  (  spep  )"):
        assert dedup.key_token(sloppy, d) == "m_spike", sloppy


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


# Issue #104's bucket-A abbreviation/long-form pairs. Kept OUT of _CORPUS_VARIANTS on
# purpose: test_real_corpus_overlap_dedups_not_splits commits every entry of that list
# on one date with an index-derived value, so a pair whose canonical token is already
# listed there would stage a spurious conflict.
_DICT_104_VARIANTS: list[tuple[str, str]] = [
    ("MG", "magnesium"),
    ("TPro", "Total Protein"),
    ("Protein, Total", "Protein, Total (SPEP)"),
    ("Protein, Total", "Protein Electrophoresis Total Protein"),
    ("Kappa", "Kappa Free Light Chain, Serum"),
    ("Kappa", "Kappa Free Light Chains, Serum"),
    ("Lambda", "Lambda Free Light Chain, Serum"),
    ("Lambda", "Lambda Free Light Chains, Serum"),
    ("K/L Ratio", "Kappa/Lambda Ratio"),
    ("K/L Ratio", "Kappa/Lambda Free Light Chain Ratio"),
    ("BMG", "Beta-2 Microglobulin"),
    ("BUN", "Urea Nitrogen"),
    ("CO2", "CO2 (Bicarbonate)"),
    ("ALT", "ALT (SGPT)"),
    ("AST", "AST (SGOT)"),
    ("TSH", "TSH (Thyroid Stimulating Hormone)"),
    ("LYM", "Lymphocytes (absolute)"),
    ("MONO", "Monocytes (absolute)"),
    ("EO", "Eosinophils (absolute)"),
    ("BAS", "Basophils (absolute)"),
    ("LYM%", "Lymphocytes %"),
    ("NEU%", "Neutrophils %"),
    ("MON%", "Monocytes %"),
    ("EO%", "Eosinophils %"),
    ("BAS%", "Basophils %"),
]


def test_synonym_additions_share_key_token():
    """Issue #104 acceptance: every curated abbreviation/long-form pair derives ONE
    dedup identity, so the same analyte stops rendering as two rows."""
    d = dedup.load_dictionary(DICT_PATH)
    for short, long in _DICT_104_VARIANTS:
        assert dedup.key_token(short, d) == dedup.key_token(long, d), (short, long)


def test_redundant_parenthetical_needs_no_entry():
    """These four are NOT in the dictionary and must not be: identity() rule 2 already
    drops a parenthetical that maps to the same canonical token as its stem, so adding
    full-label keys for them would be dead curation."""
    d = dedup.load_dictionary(DICT_PATH)
    for label in ("alt (sgpt)", "ast (sgot)",
                  "tsh (thyroid stimulating hormone)", "urea nitrogen (bun)"):
        assert label not in d, label
    assert dedup.key_token("ALT (SGPT)", d) == dedup.key_token("ALT", d) == "alt"
    assert dedup.key_token("AST (SGOT)", d) == dedup.key_token("AST", d) == "ast"
    assert dedup.key_token("TSH (Thyroid Stimulating Hormone)", d) \
        == dedup.key_token("TSH", d) == "tsh"
    assert dedup.key_token("Urea Nitrogen (BUN)", d) \
        == dedup.key_token("BUN", d) == "bun"


def test_punctuation_distinct_labels_need_an_entry():
    """Issue #104 bucket B, both halves. Case/whitespace-only variants converge for free
    through _collapse(); only labels differing by real punctuation need a synonym line,
    because stripping punctuation globally would fuse meaningful pairs (`M-Spike, %`)."""
    d = dedup.load_dictionary(DICT_PATH)
    # (a) already-converging: no dictionary entry involved at all.
    for a, b in (("Hemoglobin", "hemoglobin"), ("bun", "BUN"), ("WBC", "wbc"),
                 ("Hemoglobin A1c", "hemoglobin  a1c"), ("Kappa", "kappa")):
        assert dedup.key_token(a, {}) == dedup.key_token(b, {}), (a, b)
        assert dedup.key_token(a, d) == dedup.key_token(b, d), (a, b)
    # (b) punctuation-distinct: converge only *because of* the entries added here.
    for a, b in (("IFE Interpretation, U", "IFE Interpretation:U"),
                 ("Protein,Total,Urine", "Protein, Total, Urine"),
                 ("Prot, 24hr Calculated", "Prot,24hr Calculated")):
        assert dedup.key_token(a, {}) != dedup.key_token(b, {}), (a, b)
        assert dedup.key_token(a, d) == dedup.key_token(b, d), (a, b)


def test_synonym_additions_keep_qualifier_distinct_labels_apart():
    """The negative half of issue #104: growing the dictionary must not collapse any
    pair issue #71 keeps apart, and must not quietly map the excluded short codes."""
    d = dedup.load_dictionary(DICT_PATH)
    for a, b in (
        ("Albumin", "Albumin (SPEP)"),
        ("Albumin", "Protein Electrophoresis Albumin Fraction"),
        # Issue #106: SPEP renderings print the BARE labels, so the CMP short codes
        # must not fuse with them. `Neutrophils (absolute)` stays unmapped until the
        # doubled corpus row is actually dropped in the data repo with
        # `pemr record rm lab_result <id>` (the verb landed with issue #107).
        ("ALB", "Albumin"),
        ("TPro", "Protein, Total"),
        ("NEU", "Neutrophils (absolute)"),
        ("Lymphocytes %", "Lymphocytes (absolute)"),
        ("Neutrophils %", "Neutrophils (absolute)"),
        ("LDL cholesterol (direct)", "LDL cholesterol (calculated)"),
        ("estimated GFR (black)", "estimated GFR (other)"),
        ("CO2 (Bicarbonate)", "Bicarbonate"),
        ("Protein, Total", "Protein, Total, Urine"),
        ("M-Spike", "M-Spike, %"),
    ):
        assert dedup.key_token(a, d) != dedup.key_token(b, d), (a, b)
    # Excluded ambiguous short codes gain no mapping (`hgb` keeps its existing one).
    for code in ("gran", "ly", "mo"):
        assert code not in d, code
    assert d["hgb"] == "hemoglobin"


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


@pytest.mark.parametrize("key", ["financial_self_management", "meal_regularity",
                                  "medication_self_administration", "iadl_bathing"])
def test_validate_accepts_functional_observation_keys(key):
    # Issue #132: `functional` is a fifth obs_type and its `key` vocabulary is only
    # lightly controlled — any sensible snake_case token validates, no dictionary gate.
    dedup.validate_row(
        "observation",
        {"obs_type": "functional", "key": key, "observed_at": "2026-07-14",
         "value_text": "stopped keeping the running balance mid-page"},
    )


def test_validate_accepts_functional_ordinal_value():
    # An ordinal/scale rating is as legal as a free-text description.
    dedup.validate_row(
        "observation",
        {"obs_type": "functional", "key": "meal_regularity",
         "observed_at": "2026-07", "value_num": 2},
    )


def test_validate_rejects_functional_without_observed_at():
    # The one field `functional` requires that the other families don't: an undated
    # functional observation is the unqueryable prose the record type exists to replace.
    with pytest.raises(dedup.ValidationError,
                       match=r"missing required field 'observed_at'.*functional"):
        dedup.validate_row(
            "observation",
            {"obs_type": "functional", "key": "financial_self_management",
             "value_text": "stopped balancing the register"},
        )


@pytest.mark.parametrize("obs_type", ["vital", "order", "screening", "immunization"])
def test_validate_still_accepts_other_obs_types_without_observed_at(obs_type):
    # Scoping regression: the observed_at requirement is `functional`-only. Widening it
    # would reject already-valid extractions (an undated vital or order is routine).
    dedup.validate_row("observation", {"obs_type": obs_type, "key": "anything"})


def test_validate_functional_date_check_runs_before_required_check():
    # The new rule must not shadow DATE_FIELDS: a present-but-junk observed_at still
    # reports as an ISO-date error, not as a missing field.
    with pytest.raises(dedup.ValidationError, match=r"observed_at.*ISO date"):
        dedup.validate_row(
            "observation",
            {"obs_type": "functional", "key": "meal_regularity",
             "observed_at": "July 2026"},
        )


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
    assert summary.counts == {"new": 1, "duplicate": 0, "enriched": 0, "conflict": 0, "promoted": 0}
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
    assert summary.counts == {"new": 0, "duplicate": 0, "enriched": 0, "conflict": 1, "promoted": 0}
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
    assert summary.counts == {"new": 0, "duplicate": 1, "enriched": 0, "conflict": 0, "promoted": 0}
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
    assert s1.counts == {
        "new": len(_CORPUS_VARIANTS), "duplicate": 0, "enriched": 0, "conflict": 0, "promoted": 0}
    s2 = dedup.commit_extraction(conn, doc_csv, {"lab_result": csv_rows}, d)
    assert s2.counts == {
        "new": 0, "duplicate": len(_CORPUS_VARIANTS), "enriched": 0, "conflict": 0, "promoted": 0}
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
    assert summary.counts == {"new": 2, "duplicate": 0, "enriched": 0, "conflict": 0, "promoted": 0}
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
    assert summary.counts == {"new": 2, "duplicate": 0, "enriched": 0, "conflict": 0, "promoted": 0}


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
    assert summary.counts == {"new": 0, "duplicate": 0, "enriched": 0, "conflict": 1, "promoted": 0}


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
    assert summary.counts == {"new": 0, "duplicate": 0, "enriched": 0, "conflict": 1, "promoted": 0}
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 1


def test_serial_same_day_draws_in_one_submission_are_rejected(conn):
    # Issue #117 flipped the lab key to the collection DATE, so two same-day timepoints
    # (GTT / peri-op / inpatient q6h) of ONE submission now derive one key with differing
    # values -> pass-1 rejection, not two rows. The message must not send the agent back
    # to the source for times that would be truncated away.
    d = dedup.load_dictionary(DICT_PATH)
    doc = _make_document(conn)
    rows = [
        {"test_name": "Glucose", "collected_at": "2026-04-01T08:00", "value_num": 92,
         "unit": "mg/dL"},
        {"test_name": "Glucose", "collected_at": "2026-04-01T14:00", "value_num": 130,
         "unit": "mg/dL"},
    ]
    with pytest.raises(dedup.ValidationError) as exc:
        dedup.commit_extraction(conn, doc, {"lab_result": rows}, d)
    message = str(exc.value)
    assert "rows 0 and 1" in message
    assert "person 1 | glucose | 2026-04-01" in message   # the identity, date-only
    assert "--keep both" in message                       # the recovery that works
    assert "date-precision rule" not in message           # the advice that no longer can
    assert message.isascii()                              # cp1252 console (issue #23)
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 0


def test_mixed_precision_same_draw_dedups_to_one_row(conn):
    # Issue #117 headline (pemr-data#9): a summary states the draw as a bare date and the
    # lab report timestamps it. Same person/analyte/value/unit/refs -> one clinical fact,
    # which the full-precision key used to fork into two rows.
    d = dedup.load_dictionary(DICT_PATH)
    payload = {"value_num": 95, "unit": "mg/dL", "ref_low": 70, "ref_high": 99}
    dedup.commit_extraction(conn, _make_document(conn), {"lab_result": [
        {"test_name": "Glucose", "collected_at": "2026-04-01", **payload}]}, d)
    summary = dedup.commit_extraction(conn, _make_document(conn), {"lab_result": [
        {"test_name": "Glucose", "collected_at": "2026-04-01T09:15", **payload}]}, d)
    assert summary.counts == {"new": 0, "duplicate": 1, "enriched": 0, "conflict": 0, "promoted": 0}
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 1
    # First writer fixes the stored precision: collected_at is payload, not compared.
    assert conn.execute(
        "SELECT collected_at FROM lab_result").fetchone()["collected_at"] == "2026-04-01"


def test_same_day_distinct_draw_stages_a_conflict(conn):
    # The cost of the date-only key, and its recovery: a genuine second same-day draw
    # (a GTT timepoint) collides instead of landing as a second clean row -- but it is
    # STAGED, never dropped, and `--keep both` admits it as occurrence 1.
    d = dedup.load_dictionary(DICT_PATH)
    dedup.commit_extraction(conn, _make_document(conn), {"lab_result": [
        {"test_name": "Glucose", "collected_at": "2026-04-01T08:00", "value_num": 92,
         "unit": "mg/dL"}]}, d)
    summary = dedup.commit_extraction(conn, _make_document(conn), {"lab_result": [
        {"test_name": "Glucose", "collected_at": "2026-04-01T14:00", "value_num": 130,
         "unit": "mg/dL"}]}, d)
    assert summary.counts == {"new": 0, "duplicate": 0, "enriched": 0, "conflict": 1, "promoted": 0}
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 1

    dedup.resolve_conflict(conn, dedup.list_conflicts(conn)[0]["conflict_id"], keep="both")
    rows = conn.execute(
        "SELECT * FROM lab_result ORDER BY dedup_occurrence").fetchall()
    assert [r["dedup_occurrence"] for r in rows] == [0, 1]
    assert [r["value_num"] for r in rows] == [92, 130]


def test_lab_key_is_date_only_and_observation_is_not(conn):
    """The scope boundary issue #117 draws: `lab_result` truncates `collected_at` to the
    date, `observation` keeps `observed_at` at full precision."""
    d = dedup.load_dictionary(DICT_PATH)
    pid = conn.execute("SELECT person_id FROM person").fetchone()["person_id"]
    bare = {"test_name": "Glucose", "collected_at": "2026-04-01", "value_num": 95}
    timed = {"test_name": "Glucose", "collected_at": "2026-04-01T09:15", "value_num": 95}
    assert dedup.dedup_key("lab_result", bare, pid, d) \
        == dedup.dedup_key("lab_result", timed, pid, d)

    o_bare = {"obs_type": "vital", "key": "systolic", "observed_at": "2026-04-01",
              "value_num": 120}
    o_timed = {"obs_type": "vital", "key": "systolic", "observed_at": "2026-04-01T09:15",
               "value_num": 120}
    assert dedup.dedup_key("observation", o_bare, pid, d) \
        != dedup.dedup_key("observation", o_timed, pid, d)


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
    assert summary.counts == {"new": 0, "duplicate": 0, "enriched": 0, "conflict": 1, "promoted": 0}
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
    assert summary.counts == {"new": 2, "duplicate": 0, "enriched": 0, "conflict": 0, "promoted": 0}
    assert conn.execute("SELECT COUNT(*) AS n FROM observation").fetchone()["n"] == 2


# --- functional observations (issue #132) -------------------------------------

def _functional(observed_at, key="financial_self_management",
                value_text="running balance column stops mid-page"):
    return {"obs_type": "functional", "key": key, "observed_at": observed_at,
            "value_text": value_text}


def test_functional_observation_commits_without_a_migration(conn):
    # A caregiver-observed functional fact stated by an ingested document lands as an
    # ordinary observation row carrying that document as its provenance.
    doc = _make_document(conn)
    summary = dedup.commit_extraction(conn, doc, {"observation": [_functional("2026-07-14")]})
    assert summary.counts["new"] == 1
    row = conn.execute("SELECT * FROM observation WHERE obs_type='functional'").fetchone()
    assert row["key"] == "financial_self_management"
    assert row["observed_at"] == "2026-07-14"
    assert row["document_id"] == doc


def test_functional_observation_never_creates_a_condition(conn):
    # The load-bearing invariant: no diagnostic code without clinician documentation.
    # A functional observation records what was observed and stops there — nothing may
    # promote it into the problem list.
    doc = _make_document(conn)
    dedup.commit_extraction(conn, doc, {"observation": [
        _functional("2026-07-14"),
        _functional("2026-08-02", key="meal_regularity",
                    value_text="one meal most days, skipped others"),
    ]})
    assert conn.execute("SELECT COUNT(*) AS n FROM condition").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM allergy").fetchone()["n"] == 0


def test_functional_series_keeps_one_row_per_date(conn):
    # The dated series *is* the trend: same key on two dates = two rows, while a second
    # document restating the same key+date collapses.
    doc1 = _make_document(conn)
    doc2 = _make_document(conn)
    dedup.commit_extraction(conn, doc1, {"observation": [_functional("2026-07-14")]})
    same = dedup.commit_extraction(conn, doc2, {"observation": [_functional("2026-07-14")]})
    assert same.counts["duplicate"] == 1
    later = dedup.commit_extraction(conn, doc2, {"observation": [_functional("2026-08-14")]})
    assert later.counts["new"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM observation WHERE obs_type='functional'"
    ).fetchone()["n"] == 2


def test_functional_commit_without_observed_at_is_rejected(conn):
    # The validation rule holds through the real commit path, and the batch is atomic.
    doc = _make_document(conn)
    with pytest.raises(dedup.ValidationError, match="missing required field 'observed_at'"):
        dedup.commit_extraction(conn, doc, {"observation": [
            {"obs_type": "functional", "key": "meal_regularity",
             "value_text": "skipping meals"},
        ]})
    assert conn.execute("SELECT COUNT(*) AS n FROM observation").fetchone()["n"] == 0


# --- self-reported symptom / activity lanes (issue #167) ----------------------

def _symptom(observed_at="2026-08-16T09:00", key="right foot ache",
             value_num=3, value_text="achy after the walk"):
    return {"obs_type": "symptom", "key": key, "observed_at": observed_at,
            "value_num": value_num, "value_text": value_text}


def test_validate_accepts_a_self_reported_symptom_row():
    # T1: the shape the lane is built for -- key = the complaint, value_num = a 0-10
    # severity, value_text = the patient's verbatim wording.
    dedup.validate_row("observation", _symptom())


def test_validate_accepts_a_self_reported_activity_row():
    dedup.validate_row(
        "observation",
        {"obs_type": "activity", "key": "walk", "observed_at": "2026-08-16T07:30",
         "value_num": 40, "unit": "min"},
    )


@pytest.mark.parametrize("obs_type", ["symptom", "activity"])
def test_validate_rejects_a_self_reported_row_without_a_key(obs_type):
    # T2a: a keyless self-report is exactly the untyped blob the lane exists to prevent.
    with pytest.raises(dedup.ValidationError,
                       match=rf"missing required field 'key'.*{obs_type}"):
        dedup.validate_row(
            "observation",
            {"obs_type": obs_type, "observed_at": "2026-08-16T09:00",
             "value_text": "sore"},
        )


@pytest.mark.parametrize("obs_type", ["symptom", "activity"])
def test_validate_rejects_a_self_reported_row_without_an_observed_at(obs_type):
    # T2c: undated, a fluctuating complaint answers no frequency question at all.
    with pytest.raises(dedup.ValidationError,
                       match=rf"missing required field 'observed_at'.*{obs_type}"):
        dedup.validate_row(
            "observation", {"obs_type": obs_type, "key": "right foot ache"}
        )


@pytest.mark.parametrize("observed_at", ["2026-08-16", "2026-08", "2026"])
def test_validate_rejects_a_self_report_dated_without_a_time(observed_at):
    # T2b/T2b2: the precision rule, and the validation-order hazard with it. A
    # year-precision value is 4 characters long, so a helper that indexed blindly would
    # raise IndexError instead of a ValidationError -- the guard is what keeps the
    # message honest at every precision.
    with pytest.raises(dedup.ValidationError) as exc:
        dedup.validate_row("observation", _symptom(observed_at=observed_at))
    message = str(exc.value)
    assert "requires a time of day (YYYY-MM-DDTHH:MM)" in message   # names the fix
    assert observed_at in message                                   # names what it saw
    assert message.isascii()                                        # cp1252 (issue #23)


def test_self_report_precision_rule_does_not_shadow_the_iso_check():
    # A present-but-junk observed_at still reports as an ISO-date error: the DATE_FIELDS
    # loop is what proves the value parseable before the precision branch indexes it.
    with pytest.raises(dedup.ValidationError, match=r"observed_at.*ISO date"):
        dedup.validate_row("observation", _symptom(observed_at="yesterday morning"))


@pytest.mark.parametrize("obs_type", ["vital", "order", "screening", "immunization",
                                      "functional"])
def test_the_time_of_day_rule_is_scoped_to_the_self_reported_lanes(obs_type):
    # Scoping regression: mandating a time on the older families would reject every
    # already-valid extraction, which state dates and not clock times.
    dedup.validate_row(
        "observation",
        {"obs_type": obs_type, "key": "anything", "observed_at": "2026-07-14"},
    )


def test_same_day_symptom_reports_at_different_times_are_distinct_rows(conn):
    # T3, the point of the whole precision rule: two reports of one complaint on one
    # calendar day must survive as two rows.
    doc = _make_document(conn)
    summary = dedup.commit_extraction(conn, doc, {"observation": [
        _symptom(observed_at="2026-08-16T09:00", value_num=3),
        _symptom(observed_at="2026-08-16T21:00", value_num=6),
    ]})
    assert summary.counts["new"] == 2
    keys = {r["dedup_key"] for r in conn.execute(
        "SELECT dedup_key FROM observation WHERE obs_type='symptom'"
    ).fetchall()}
    assert len(keys) == 2


def test_a_symptom_restated_at_the_same_timestamp_still_collides(conn):
    # The other half of T3: the correction-collides property is preserved, not traded
    # away. A re-read of the *same* report carries the same timestamp, so it dedups --
    # a differing value stages a conflict rather than becoming a phantom second report.
    doc1, doc2 = _make_document(conn), _make_document(conn)
    dedup.commit_extraction(conn, doc1, {"observation": [_symptom()]})
    same = dedup.commit_extraction(conn, doc2, {"observation": [_symptom()]})
    assert same.counts["duplicate"] == 1
    differing = dedup.commit_extraction(
        conn, doc2, {"observation": [_symptom(value_num=8)]}
    )
    assert differing.counts["conflict"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM observation WHERE obs_type='symptom'"
    ).fetchone()["n"] == 1


def test_a_self_reported_row_never_creates_a_condition(conn):
    # The load-bearing invariant (issue #167): these lanes stay inside the `observation`
    # catch-all. The problem list is clinician-sourced and heavily verdict-suppressed;
    # routing unfiltered self-attestations into it would undo that curation.
    doc = _make_document(conn)
    dedup.commit_extraction(conn, doc, {"observation": [
        _symptom(),
        {"obs_type": "activity", "key": "walk", "observed_at": "2026-08-16T07:30"},
    ]})
    assert conn.execute("SELECT COUNT(*) AS n FROM condition").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM allergy").fetchone()["n"] == 0


def test_observation_identity_is_not_forked_by_obs_type():
    # `_key_parts`/`KEY_FIELDS` are untouched: the fix is a validation-time precision
    # rule, not a second key shape. So no stored row's dedup_key moves (no `pemr rekey`
    # implied), and a symptom row keys on exactly the four parts every observation does.
    assert dedup.KEY_FIELDS["observation"] == frozenset({"obs_type", "observed_at", "key"})
    parts = dedup._key_parts("observation", _symptom(), 1)
    assert parts == [1, "symptom", "2026-08-16 09:00", "right foot ache"]
    # The severity is payload, never identity -- an edited value must keep colliding as a
    # correction instead of silently becoming a second report.
    assert dedup._key_parts("observation", _symptom(value_num=9), 1) == parts


# --- self-report write-time hardening (issue #180) ----------------------------

@pytest.mark.parametrize("value_num", [-1, -0.5, 10.5, 11, 50])
def test_validate_rejects_a_symptom_severity_out_of_range(value_num):
    # `value_num=50` used to validate and render as `(severity 50/10)` -- the document
    # repeating a data-entry error back as fact.
    with pytest.raises(dedup.ValidationError) as exc:
        dedup.validate_row("observation", _symptom(value_num=value_num))
    message = str(exc.value)
    assert "expects a severity in 0-10" in message   # names the rule
    assert repr(value_num) in message                # names what it saw
    assert message.isascii()                         # cp1252 console (issue #23)


@pytest.mark.parametrize("value_num", [0, 0.0, 10, 10.0, 3.5, None])
def test_validate_accepts_a_symptom_severity_at_the_bounds(value_num):
    # Both bounds are inclusive, and `0` is load-bearing: it is the "reported resolved"
    # sentinel the summary's symptom line reads. A NULL severity is a present report of
    # unknown intensity and stays legal too.
    dedup.validate_row("observation", _symptom(value_num=value_num))


def test_activity_value_num_is_not_range_checked():
    # The rule is obs_type-conditional, not column-wide: `activity` shares `value_num`
    # but stores minutes, which has no defensible upper bound.
    dedup.validate_row(
        "observation",
        {"obs_type": "activity", "key": "walk", "observed_at": "2026-08-16T07:30",
         "value_num": 240, "unit": "min"},
    )


@pytest.mark.parametrize("obs_type", ["symptom", "activity"])
@pytest.mark.parametrize("key", ["", " ", "\t", "  \n "])
def test_validate_rejects_a_blank_key_self_report(obs_type, key):
    # The second spelling of keyless: presence-only validation stopped `None` and let
    # `""` through, rendering a blank-labelled symptom line. Same message as the `None`
    # case above -- both are "missing" in the sense the rule means.
    with pytest.raises(dedup.ValidationError,
                       match=rf"missing required field 'key'.*{obs_type}"):
        dedup.validate_row(
            "observation",
            {"obs_type": obs_type, "key": key, "observed_at": "2026-08-16T09:00",
             "value_text": "sore"},
        )


def test_a_blank_observed_at_is_rejected_by_the_iso_check_first():
    # Scoping note for the widened check: `observed_at` is a DATE_FIELD, so the per-field
    # ISO loop rejects a whitespace-only value before the presence rule ever sees it --
    # with the more specific message, which is the right one to keep. The widening
    # therefore closes exactly one real hole (`key`), and blank dates stay rejected.
    for obs_type in ("functional", "symptom", "activity"):
        with pytest.raises(dedup.ValidationError, match=r"observed_at.*ISO date"):
            dedup.validate_row(
                "observation",
                {"obs_type": obs_type, "key": "meal_regularity", "observed_at": "   ",
                 "value_text": "skipping meals"},
            )


def test_the_blank_key_rule_holds_through_the_commit_path(conn):
    doc = _make_document(conn)
    with pytest.raises(dedup.ValidationError, match="missing required field 'key'"):
        dedup.commit_extraction(conn, doc, {"observation": [_symptom(key="")]})
    assert conn.execute("SELECT COUNT(*) AS n FROM observation").fetchone()["n"] == 0


def test_the_severity_range_rule_holds_through_the_commit_path(conn):
    doc = _make_document(conn)
    with pytest.raises(dedup.ValidationError, match="expects a severity in 0-10"):
        dedup.commit_extraction(conn, doc, {"observation": [_symptom(value_num=50)]})
    assert conn.execute("SELECT COUNT(*) AS n FROM observation").fetchone()["n"] == 0


def test_norm_ts_collapses_the_separator_spellings():
    # The rename to a public name is what lets `render` share this (issue #180); the
    # normalization itself is unchanged, and is why the two spellings sort as one.
    assert dedup.norm_ts("2026-08-18T20:00") == dedup.norm_ts("2026-08-18 20:00")
    assert dedup.norm_ts(None) == ""
    # And the hazard it exists to fix: raw, the 09:00 row outranks the 20:00 one.
    assert "2026-08-18 20:00" < "2026-08-18T09:00"
    assert dedup.norm_ts("2026-08-18 20:00") > dedup.norm_ts("2026-08-18T09:00")


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
    assert summary.counts == {"new": 1, "duplicate": 1, "enriched": 0, "conflict": 0, "promoted": 0}


def test_intra_payload_collision_checked_per_record_type(conn):
    """Distinct types never collide with each other, and a same-key observation pair is
    caught the same way a lab pair is."""
    doc = _make_document(conn)
    with pytest.raises(dedup.ValidationError, match="observation: rows 0 and 1") as exc:
        dedup.commit_extraction(conn, doc, {"observation": [
            {"obs_type": "vital", "key": "systolic", "observed_at": "2024-04-01",
             "value_num": 120},
            {"obs_type": "vital", "key": "systolic", "observed_at": "2024-04-01",
             "value_num": 138},
        ]})
    # `observation` keeps the time in its key, so "add the times" is still real advice
    # here -- the clause issue #117 dropped for `lab_result`.
    assert "date-precision rule" in str(exc.value)


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
    # Everything numbered below 005 - later migrations build ON the occurrence columns,
    # so they cannot stand in for the pre-005 shape.
    pre_005 = [p for p in all_migrations if p.name < "005"]
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
        assert "005_dedup_occurrence.sql" in db.migrate(conn, staged)

        stored = conn.execute("SELECT * FROM lab_result").fetchone()
        assert stored["dedup_key"] == legacy_key      # no key churn
        assert stored["dedup_base"] == legacy_key     # backfilled from the key
        assert stored["dedup_occurrence"] == 0

        # And the pre-005 row still dedups against a fresh commit of the same fact.
        doc = _make_document(conn)
        summary = dedup.commit_extraction(conn, doc, {"lab_result": [row]})
        assert summary.counts == {"new": 0, "duplicate": 1, "enriched": 0, "conflict": 0, "promoted": 0}
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


# The canonical "one synonym fuses two same-draw facts" setup, in one place because
# three tests need it. The SPEP albumin *fraction* is a separately measured quantity
# from the CMP's `Albumin` and carries no parenthetical to keep it apart, so it is
# deliberately absent from the shipped dictionary (issue #104's Out of scope) — which
# is what makes it a valid fuse to construct here.
_FUSING_ALBUMIN_PAIR = {"lab_result": [
    {"test_name": "Protein Electrophoresis Albumin Fraction",
     "collected_at": "2026-01-02", "value_num": 4.2},
    {"test_name": "Albumin", "collected_at": "2026-01-02", "value_num": 3.6},
]}
_FUSING_ALBUMIN_SYNONYM = {"protein electrophoresis albumin fraction": "albumin"}


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
    assert summary.counts == {"new": 0, "duplicate": 1, "enriched": 0, "conflict": 0, "promoted": 0}
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
    """Two methods for one analyte off one draw (the SPEP albumin *fraction* vs a CMP
    Albumin) must not be merged by a synonym: the colliding table keeps every stored
    key. The pair doubles as a pin on that fraction label staying unmapped in the
    shipped dictionary (issue #104) — mapping it is exactly this fuse."""
    doc = _make_document(conn)
    dedup.commit_extraction(conn, doc, _FUSING_ALBUMIN_PAIR,
                            dedup.load_dictionary(DICT_PATH))
    before = {r["lab_result_id"]: r["dedup_key"]
              for r in conn.execute("SELECT lab_result_id, dedup_key FROM lab_result")}

    report = dedup.rekey(conn, _rekey_dict(**_FUSING_ALBUMIN_SYNONYM), apply=True)

    assert [c.kind for c in report.collisions] == ["fused"]
    assert "same dedup_key" in report.collisions[0].message
    assert report.blocked == ["lab_result"] and report.writable() == []
    after = {r["lab_result_id"]: r["dedup_key"]
             for r in conn.execute("SELECT lab_result_id, dedup_key FROM lab_result")}
    assert after == before


def _collide_allergy_and_move_condition(conn):
    """The issue-#92 repro: one table holds a collision, another holds clean work.

    Two allergies the new dictionary fuses (different reactions, so it is a genuine
    two-facts-into-one), plus a condition whose key merely moves."""
    dedup.commit_extraction(conn, _make_document(conn), {
        "allergy": [
            {"substance": "PCN", "reaction": "rash"},
            {"substance": "Penicillin", "reaction": "anaphylaxis"},
        ],
        "condition": [{"name": "T2DM", "status": "active"}],
    }, dedup.load_dictionary(DICT_PATH))
    return _rekey_dict(pcn="penicillin", t2dm="type 2 diabetes")


def _keys(conn, record_type, label_column):
    return {r[label_column]: r["dedup_key"]
            for r in conn.execute(f"SELECT * FROM {record_type}")}


def test_rekey_applies_the_clean_tables_and_skips_only_the_colliding_one(conn):
    """Acceptance (issue #92): a collision in `allergy` must not block `condition`.
    The tables are scanned independently, so one bad pair quarantines its own table."""
    d_new = _collide_allergy_and_move_condition(conn)
    allergies_before = _keys(conn, "allergy", "substance")

    report = dedup.rekey(conn, d_new, apply=True)

    assert report.applied is True
    assert report.blocked == ["allergy"]
    assert [(c.record_type, c.label) for c in report.writable()] \
        == [("condition", "T2DM")]
    # The clean table was written...
    assert _keys(conn, "condition", "name")["T2DM"] == report.writable()[0].new_key
    # ...and the colliding one kept every stored key.
    assert _keys(conn, "allergy", "substance") == allergies_before


def test_rekey_dry_run_reports_every_collision_instead_of_stopping_at_the_first(conn):
    """Acceptance (issue #92): report-only mode is a survey — it writes nothing by
    definition, so it must enumerate all collisions in all tables rather than abort on
    the first and force a serial edit-and-rerun loop."""
    d_new = _collide_allergy_and_move_condition(conn)
    dedup.commit_extraction(conn, _make_document(conn), _FUSING_ALBUMIN_PAIR,
                            dedup.load_dictionary(DICT_PATH))
    d_new.update(_FUSING_ALBUMIN_SYNONYM)
    before = {t: _keys(conn, t, c) for t, c in
              (("allergy", "substance"), ("lab_result", "test_name"),
               ("condition", "name"))}

    report = dedup.rekey(conn, d_new)

    assert report.applied is False
    assert report.blocked == ["allergy", "lab_result"]     # both, not just the first
    assert {c.record_type for c in report.collisions} == {"allergy", "lab_result"}
    assert [c.kind for c in report.collisions] == ["fused", "fused"]
    assert [(c.record_type, c.label) for c in report.writable()] \
        == [("condition", "T2DM")]
    assert {t: _keys(conn, t, c) for t, c in
            (("allergy", "substance"), ("lab_result", "test_name"),
             ("condition", "name"))} == before          # a dry run writes nothing


def test_rekey_reports_a_third_row_on_the_same_key_against_the_same_anchor(conn):
    """Scanning continues past a collision within a table too, so a three-way fuse is
    reported as two pairs rather than one — the survey has to show the whole scope."""
    dedup.commit_extraction(conn, _make_document(conn), {"allergy": [
        {"substance": "PCN", "reaction": "rash"},
        {"substance": "Penicillin", "reaction": "anaphylaxis"},
        {"substance": "Pen-G", "reaction": "hives"},
    ]}, dedup.load_dictionary(DICT_PATH))

    report = dedup.rekey(conn, _rekey_dict(pcn="penicillin", **{"pen-g": "penicillin"}))

    assert len(report.collisions) == 2
    assert {c.clash_label for c in report.collisions} == {"PCN"}   # one anchor, not a chain
    assert {c.label for c in report.collisions} == {"Penicillin", "Pen-G"}


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
        "allergy": [{"substance": "Penicillin", "reaction": "rash"}],
        "condition": [{"name": "Type 2 Diabetes", "status": "active"}],
    })
    report = dedup.rekey(conn, None)
    assert report.scanned == {t: 1 for t in dedup.KNOWN_TYPES}
    assert report.changes == []  # same dictionary (none) -> keys already current


def test_rekey_report_carries_the_stored_to_recomputed_base_map(conn):
    """Issue #126. A ``dedup_base`` is a content hash overwritten in place, so once the run
    is over nothing in the database records that ``F_old`` became ``F_new``. Only `rekey`
    knows, which is why the mapping leaves on the report: it is what scopes the CLI's
    apply-time orphan report and what `record reaffirm --map-file` follows.

    Stays a plain dict of strings on purpose — `dedup` must never import `curation`."""
    doc = _make_document(conn)
    dedup.commit_extraction(conn, doc, {
        "lab_result": [{"test_name": "ZZT", "collected_at": "2026-01-02",
                        "value_num": 108}],
        "condition": [{"name": "Type 2 Diabetes", "status": "active"}],
    }, dedup.load_dictionary(DICT_PATH))
    stored = {
        table: conn.execute(f"SELECT dedup_base FROM {table}").fetchone()["dedup_base"]
        for table in ("lab_result", "condition")
    }

    report = dedup.rekey(conn, _rekey_dict(zzt="zonulin_test"))

    # Every scanned type gets a map, and a table with no rows gets an empty one.
    assert set(report.base_maps) == set(dedup.KNOWN_TYPES)
    assert report.base_maps["medication"] == {}
    # The moved family maps old -> new; the untouched one maps to itself.
    moved = report.base_maps["lab_result"][stored["lab_result"]]
    assert moved != stored["lab_result"]
    assert report.base_maps["condition"] == {
        stored["condition"]: stored["condition"]}
    # A dry run still reports the mapping: it is what the run *would* do.
    assert report.applied is False


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
    assert report.changes == [] and report.collisions == []   # no collision, no churn
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
            row["collected_at"].replace("T", " ").split(" ")[0],   # issue #117: date only
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
    assert summary.counts == {"new": 1, "duplicate": 0, "enriched": 0, "conflict": 0, "promoted": 0}
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


# --- rekey collisions a recorded verdict settles (issue #116) -----------------

def _fusing_pair(conn):
    """The fusing albumin pair, committed. Returns ``(incumbent id, clash id)``.

    Row ids are the scan order (`rekey` sorts by primary key), so the first-committed
    row is the incumbent that keeps occurrence 0 and the second is the clash."""
    dedup.commit_extraction(conn, _make_document(conn), _FUSING_ALBUMIN_PAIR,
                            dedup.load_dictionary(DICT_PATH))
    return tuple(int(r["lab_result_id"]) for r in conn.execute(
        "SELECT lab_result_id FROM lab_result ORDER BY lab_result_id"))


def _lab_rows(conn):
    return {int(r["lab_result_id"]): r
            for r in conn.execute("SELECT * FROM lab_result")}


def _rule(conn, target, status, **kwargs):
    """Record one verdict and hand back the resolver `rekey` adjudicates through."""
    curation.annotate_record(conn, "lab_result", str(target), status=status,
                             note="a clinician ruled on this pair", apply=True,
                             **kwargs)
    return curation.collision_resolver(conn)


def test_a_merged_into_verdict_resolves_a_fused_collision(conn):
    """AC1. The pair the dictionary fuses is exactly the pair a human already merged,
    so the collision is answered rather than blocking: both rows land in the surviving
    family, the later one as its next occurrence, and the table writes."""
    peaf_id, albumin_id = _fusing_pair(conn)
    resolver = _rule(conn, peaf_id, "merged-into",
                     merged_into_base=_lab_rows(conn)[albumin_id]["dedup_base"])

    report = dedup.rekey(conn, _rekey_dict(**_FUSING_ALBUMIN_SYNONYM), apply=True,
                         resolver=resolver)

    assert report.collisions == [] and report.blocked == []
    assert [(r.row_id, r.clash_row_id, r.kind, r.status, r.scope, r.new_occurrence)
            for r in report.resolved] == [
        (albumin_id, peaf_id, "fused", "merged-into", "family", 1)]
    rows = _lab_rows(conn)
    survivor = rows[peaf_id]["dedup_base"]
    assert rows[albumin_id]["dedup_base"] == survivor        # one family now
    assert (rows[peaf_id]["dedup_occurrence"],
            rows[albumin_id]["dedup_occurrence"]) == (0, 1)
    assert rows[peaf_id]["dedup_key"] == survivor            # occurrence 0 IS the base
    assert rows[albumin_id]["dedup_key"] == dedup.occurrence_key(survivor, 1)


def test_a_superseded_row_verdict_resolves_a_collision(conn):
    """AC2. Either row, either scope: the verdict here is row-scoped and recorded on the
    *clash* row, not the incumbent, and still answers the pair."""
    peaf_id, albumin_id = _fusing_pair(conn)
    resolver = _rule(conn, albumin_id, "superseded", row=True)

    report = dedup.rekey(conn, _rekey_dict(**_FUSING_ALBUMIN_SYNONYM), apply=True,
                         resolver=resolver)

    assert report.collisions == []
    assert [(r.scope, r.record_id, r.status, r.verdict_action)
            for r in report.resolved] == [
        ("row", albumin_id, "superseded", "unchanged")]
    rows = _lab_rows(conn)
    assert rows[albumin_id]["dedup_base"] == rows[peaf_id]["dedup_base"]


def test_an_unverdicted_collision_still_blocks_its_table(conn):
    """AC3 — the #92 regression. A resolver that finds nothing changes nothing: the
    table stays on its stored keys and the collision is reported as before."""
    _fusing_pair(conn)
    before = _keys(conn, "lab_result", "test_name")

    report = dedup.rekey(conn, _rekey_dict(**_FUSING_ALBUMIN_SYNONYM), apply=True,
                         resolver=curation.collision_resolver(conn))

    assert report.resolved == []
    assert [c.kind for c in report.collisions] == ["fused"]
    assert report.blocked == ["lab_result"] and report.writable() == []
    assert _keys(conn, "lab_result", "test_name") == before


@pytest.mark.parametrize("status", ["confirmed", "disputed", "erroneous-in-source"])
def test_a_non_resolving_verdict_does_not_resolve_a_collision(conn, status):
    """AC4. Only `merged-into`/`superseded` say "these two are one fact"; the other
    three rule on a row's content, not on its identity against another row."""
    peaf_id, _albumin_id = _fusing_pair(conn)
    resolver = _rule(conn, peaf_id, status)

    report = dedup.rekey(conn, _rekey_dict(**_FUSING_ALBUMIN_SYNONYM), apply=True,
                         resolver=resolver)

    assert report.resolved == []
    assert [c.kind for c in report.collisions] == ["fused"]
    assert report.blocked == ["lab_result"]


def _doubled_pair(conn):
    """One fact filed twice, the second copy under a pre-drift key — the `doubled`
    shape, which no dictionary edit is needed to reach."""
    payload = {"test_name": "ZZT", "collected_at": "2026-01-02", "value_num": 108}
    dedup.commit_extraction(conn, _make_document(conn), {"lab_result": [payload]},
                            dedup.load_dictionary(DICT_PATH))
    pid = conn.execute("SELECT person_id FROM person").fetchone()["person_id"]
    conn.execute(
        "INSERT INTO lab_result (person_id, document_id, test_name, collected_at, "
        "value_num, dedup_key, dedup_base, dedup_occurrence) "
        "VALUES (?, ?, 'ZZT', '2026-01-02', 108, 'stale-key', 'stale-key', 0)",
        (pid, _make_document(conn)),
    )
    conn.commit()
    return tuple(int(r["lab_result_id"]) for r in conn.execute(
        "SELECT lab_result_id FROM lab_result ORDER BY lab_result_id"))


def test_a_doubled_collision_is_resolved_too(conn):
    """`kind` is diagnostic, never eligibility: a covering verdict answers a doubled
    pair (same fact, two keys) exactly as it answers a fused one."""
    first_id, second_id = _doubled_pair(conn)
    resolver = _rule(conn, first_id, "superseded")

    report = dedup.rekey(conn, dedup.load_dictionary(DICT_PATH), apply=True,
                         resolver=resolver)

    assert report.collisions == []
    assert [(r.row_id, r.kind) for r in report.resolved] == [(second_id, "doubled")]
    rows = _lab_rows(conn)
    assert rows[second_id]["dedup_base"] == rows[first_id]["dedup_base"]
    assert rows[second_id]["dedup_occurrence"] == 1


def test_rekey_without_a_resolver_blocks_every_collision(conn):
    """`resolver=None` is the default and is today's behaviour byte-for-byte — the
    verdict is in the database and is simply never consulted."""
    peaf_id, _albumin_id = _fusing_pair(conn)
    _rule(conn, peaf_id, "superseded")

    report = dedup.rekey(conn, _rekey_dict(**_FUSING_ALBUMIN_SYNONYM), apply=True)

    assert report.resolved == []
    assert [c.kind for c in report.collisions] == ["fused"]


def test_a_resolved_collision_leaves_no_key_drift(conn):
    """The reason a resolved clash is rekeyed rather than left on its stored key: a row
    left behind would be permanently drifted, and `_assert_no_key_drift` would refuse
    every later ingest of that identity while `rekey` itself reported clean."""
    peaf_id, _albumin_id = _fusing_pair(conn)
    resolver = _rule(conn, peaf_id, "superseded")
    d_new = _rekey_dict(**_FUSING_ALBUMIN_SYNONYM)
    dedup.rekey(conn, d_new, apply=True, resolver=resolver)

    pid = conn.execute("SELECT person_id FROM person").fetchone()["person_id"]
    payloads = [{name: row[name] for name in dedup.FIELD_SPECS["lab_result"]}
                for row in _lab_rows(conn).values()]
    dedup._assert_no_key_drift(conn, "lab_result", payloads, pid, d_new)  # no raise


def test_rekey_is_idempotent_after_a_resolved_collision(conn):
    peaf_id, _albumin_id = _fusing_pair(conn)
    resolver = _rule(conn, peaf_id, "superseded")
    d_new = _rekey_dict(**_FUSING_ALBUMIN_SYNONYM)
    assert len(dedup.rekey(conn, d_new, apply=True, resolver=resolver).resolved) == 1

    again = dedup.rekey(conn, d_new, apply=True,
                        resolver=curation.collision_resolver(conn))
    assert (again.changes, again.collisions, again.resolved) == ([], [], [])


def _extra_occurrence(conn, row_id, value_num):
    """A second live row in ``row_id``'s family — the state `--keep both` leaves behind.

    Same identity fields (so it recomputes onto the same base), same ``dedup_base``, next
    occurrence. Returns its row id."""
    row = _lab_rows(conn)[row_id]
    occurrence = int(row["dedup_occurrence"]) + 1
    cur = conn.execute(
        "INSERT INTO lab_result (person_id, document_id, test_name, collected_at, "
        "value_num, dedup_key, dedup_base, dedup_occurrence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (row["person_id"], row["document_id"], row["test_name"], row["collected_at"],
         value_num, dedup.occurrence_key(row["dedup_base"], occurrence),
         row["dedup_base"], occurrence),
    )
    conn.commit()
    return int(cur.lastrowid)


def _curation_rows(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM curation").fetchone()["n"]


def test_a_family_verdict_narrows_onto_every_row_it_covered(conn):
    """The re-decided rule: a resolving family verdict is pinned to *exactly* the rows it
    covered when it was made — all of them when the family holds two live occurrences —
    and never reaches the row that only joined the family through this merge."""
    peaf_id, albumin_id = _fusing_pair(conn)
    peaf_occ1_id = _extra_occurrence(conn, peaf_id, 4.4)
    resolver = _rule(conn, peaf_id, "superseded")           # family scope

    report = dedup.rekey(conn, _rekey_dict(**_FUSING_ALBUMIN_SYNONYM), apply=True,
                         resolver=resolver)

    assert report.collisions == []
    assert [(r.verdict_action, r.covered_row_ids, r.narrowed_row_ids)
            for r in report.resolved] == [
        ("narrowed", [peaf_id, peaf_occ1_id], [peaf_id, peaf_occ1_id])]
    verdicts = curation.load_verdicts(conn)
    assert verdicts.family == {}                            # nothing family-scoped left
    survivor = _lab_rows(conn)[peaf_id]["dedup_base"]
    for row_id in (peaf_id, peaf_occ1_id):
        assert verdicts.for_row("lab_result", survivor, row_id)["scope"] == "row"
    # The row the merge brought in was never judged and still is not.
    assert verdicts.for_row("lab_result", survivor, albumin_id) is None


def test_one_verdict_settling_two_clashes_is_narrowed_once(conn):
    """Two occurrences of a judged family landing on two occurrences of the surviving
    one: the verdict authorizes both resolutions, so it is narrowed once and every
    resolution reports the same pinned rows — narrowing twice would find its own first
    pass and silently no-op."""
    peaf_id, albumin_id = _fusing_pair(conn)
    peaf_occ1_id = _extra_occurrence(conn, peaf_id, 4.4)
    albumin_occ1_id = _extra_occurrence(conn, albumin_id, 3.9)
    resolver = _rule(conn, peaf_id, "superseded")           # family scope

    report = dedup.rekey(conn, _rekey_dict(**_FUSING_ALBUMIN_SYNONYM), apply=True,
                         resolver=resolver)

    assert report.collisions == []
    assert [r.row_id for r in report.resolved] == [albumin_id, albumin_occ1_id]
    assert [(r.verdict_action, r.narrowed_row_ids) for r in report.resolved] == [
        ("narrowed", [peaf_id, peaf_occ1_id])] * 2
    assert _curation_rows(conn) == 2                        # one per pinned row
    rows = _lab_rows(conn)
    survivor = rows[peaf_id]["dedup_base"]
    assert {r["dedup_base"] for r in rows.values()} == {survivor}
    assert sorted(int(r["dedup_occurrence"]) for r in rows.values()) == [0, 1, 2, 3]


def test_a_resolution_in_a_quarantined_table_is_reported_as_withheld(conn):
    """Audit's advisory: a resolved pair in a table that also holds an *unresolved*
    collision is withheld with the rest of the table, so its account must not describe a
    write that never happened — and the verdict is not narrowed either."""
    dedup.commit_extraction(conn, _make_document(conn), {"lab_result": [
        {"test_name": "Protein Electrophoresis Albumin Fraction",
         "collected_at": "2026-01-02", "value_num": 4.2},
        {"test_name": "Albumin", "collected_at": "2026-01-02", "value_num": 3.6},
        {"test_name": "ALBX", "collected_at": "2026-01-02", "value_num": 3.9},
    ]}, dedup.load_dictionary(DICT_PATH))
    peaf_id, albumin_id, albx_id = (int(r["lab_result_id"]) for r in conn.execute(
        "SELECT lab_result_id FROM lab_result ORDER BY lab_result_id"))
    # Family scope on the *clash* row's own family, so it settles that pair only.
    resolver = _rule(conn, albumin_id, "superseded")
    before = _keys(conn, "lab_result", "test_name")

    report = dedup.rekey(conn, _rekey_dict(albx="albumin", **_FUSING_ALBUMIN_SYNONYM),
                         apply=True, resolver=resolver)

    assert [(c.row_id, c.clash_row_id) for c in report.collisions] \
        == [(albx_id, peaf_id)]
    assert [(r.row_id, r.verdict_action, r.narrowed_row_ids)
            for r in report.resolved] == [(albumin_id, "withheld", [])]
    assert "withheld: lab_result still holds an unresolved collision" \
        in report.resolved[0].message
    assert _keys(conn, "lab_result", "test_name") == before
    # The ruling is exactly as the human left it: still family-scoped, untouched.
    assert curation.load_verdicts(conn).rows == {}


def test_a_third_row_on_a_resolved_key_still_blocks_when_unverdicted(conn):
    """Quarantine granularity is untouched (#92): one settled pair does not license the
    unsettled third row that lands on the same key, and the table withholds everything —
    the resolved change included."""
    dedup.commit_extraction(conn, _make_document(conn), {"lab_result": [
        {"test_name": "Protein Electrophoresis Albumin Fraction",
         "collected_at": "2026-01-02", "value_num": 4.2},
        {"test_name": "Albumin", "collected_at": "2026-01-02", "value_num": 3.6},
        {"test_name": "ALBX", "collected_at": "2026-01-02", "value_num": 3.9},
    ]}, dedup.load_dictionary(DICT_PATH))
    peaf_id, albumin_id, albx_id = (int(r["lab_result_id"]) for r in conn.execute(
        "SELECT lab_result_id FROM lab_result ORDER BY lab_result_id"))
    # Row-scoped on the clash row, so it rules on that row and no other.
    resolver = _rule(conn, albumin_id, "superseded", row=True)
    before = _keys(conn, "lab_result", "test_name")

    report = dedup.rekey(conn, _rekey_dict(albx="albumin", **_FUSING_ALBUMIN_SYNONYM),
                         apply=True, resolver=resolver)

    assert [r.row_id for r in report.resolved] == [albumin_id]
    assert [(c.row_id, c.clash_row_id) for c in report.collisions] \
        == [(albx_id, peaf_id)]
    assert report.blocked == ["lab_result"] and report.writable() == []
    assert _keys(conn, "lab_result", "test_name") == before


# --- collisions ruled TWO distinct facts (issue #122) -------------------------

def test_a_distinct_verdict_resolves_a_collision_without_merging(conn):
    """AC2. A `distinct` ruling settles the identity question the opposite way from
    `merged-into`/`superseded` - two facts, not one - and that is enough to unblock the
    table. The write is the same either way, because the `--keep both` occurrence shape
    *is* "two live rows on one identity"."""
    peaf_id, albumin_id = _fusing_pair(conn)
    resolver = _rule(conn, peaf_id, "distinct")

    report = dedup.rekey(conn, _rekey_dict(**_FUSING_ALBUMIN_SYNONYM), apply=True,
                         resolver=resolver)

    assert report.collisions == [] and report.blocked == []
    assert [(r.row_id, r.clash_row_id, r.status, r.settlement, r.new_occurrence)
            for r in report.resolved] == [
        (albumin_id, peaf_id, "distinct", "distinct", 1)]
    rows = _lab_rows(conn)
    shared = rows[peaf_id]["dedup_base"]
    assert rows[albumin_id]["dedup_base"] == shared              # one family, two rows
    assert (rows[peaf_id]["dedup_occurrence"],
            rows[albumin_id]["dedup_occurrence"]) == (0, 1)
    assert rows[peaf_id]["dedup_key"] == shared
    assert rows[albumin_id]["dedup_key"] == dedup.occurrence_key(shared, 1)


@pytest.mark.parametrize("status,settlement", [
    ("distinct", "distinct"),
    ("merged-into", "merged"),
    ("superseded", "merged"),
])
def test_a_resolution_reports_which_way_the_verdict_settled_it(conn, status,
                                                               settlement):
    """AC4 at the engine level. `status` is the raw vocabulary and may grow; `settlement`
    is the two-valued contract the CLI and any consumer branch on."""
    peaf_id, albumin_id = _fusing_pair(conn)
    kwargs = ({"merged_into_base": _lab_rows(conn)[albumin_id]["dedup_base"]}
              if status == "merged-into" else {})
    resolver = _rule(conn, peaf_id, status, **kwargs)

    report = dedup.rekey(conn, _rekey_dict(**_FUSING_ALBUMIN_SYNONYM), apply=True,
                         resolver=resolver)

    assert [(r.status, r.settlement) for r in report.resolved] == [(status, settlement)]
    marker = ("rules them two distinct facts" if settlement == "distinct"
              else "settles the pair")
    assert marker in report.resolved[0].message


def test_a_distinct_resolution_leaves_no_key_drift(conn):
    """The #116 reason for rekeying a settled clash rather than leaving it behind, which
    a `distinct` ruling inherits unchanged: a stranded row stays permanently drifted and
    `_assert_no_key_drift` would refuse every later ingest of that identity."""
    peaf_id, _albumin_id = _fusing_pair(conn)
    resolver = _rule(conn, peaf_id, "distinct")
    d_new = _rekey_dict(**_FUSING_ALBUMIN_SYNONYM)
    dedup.rekey(conn, d_new, apply=True, resolver=resolver)

    pid = conn.execute("SELECT person_id FROM person").fetchone()["person_id"]
    payloads = [{name: row[name] for name in dedup.FIELD_SPECS["lab_result"]}
                for row in _lab_rows(conn).values()]
    dedup._assert_no_key_drift(conn, "lab_result", payloads, pid, d_new)  # no raise


def test_rekey_is_idempotent_after_a_distinct_resolution(conn):
    peaf_id, _albumin_id = _fusing_pair(conn)
    resolver = _rule(conn, peaf_id, "distinct")
    d_new = _rekey_dict(**_FUSING_ALBUMIN_SYNONYM)
    assert len(dedup.rekey(conn, d_new, apply=True, resolver=resolver).resolved) == 1

    again = dedup.rekey(conn, d_new, apply=True,
                        resolver=curation.collision_resolver(conn))
    assert (again.changes, again.collisions, again.resolved) == ([], [], [])


# --- pairs ruled two opposite ways (issue #124) -------------------------------

@pytest.mark.parametrize("one_fact_status", ["merged-into", "superseded"])
@pytest.mark.parametrize("distinct_on", ["incumbent", "clash"])
def test_contradictory_verdicts_block_the_collision(conn, one_fact_status, distinct_on):
    """AC1-AC3. One row ruled *two facts* and the other ruled *one fact* is not a settled
    pair, whichever row carries which - and the parametrization over `distinct_on` *is*
    the bug: first-wins made the reported settlement a function of scan order, so a run
    would tell the operator the opposite of what a human decided depending on which row
    happened to be scanned first. The pair blocks like any unresolved collision, and no
    narrowing is written on the strength of an arbitrarily chosen side."""
    peaf_id, albumin_id = _fusing_pair(conn)
    distinct_id, other_id = ((peaf_id, albumin_id) if distinct_on == "incumbent"
                             else (albumin_id, peaf_id))
    before = _keys(conn, "lab_result", "test_name")
    _rule(conn, distinct_id, "distinct")
    kwargs = ({"merged_into_base": _lab_rows(conn)[distinct_id]["dedup_base"]}
              if one_fact_status == "merged-into" else {})
    resolver = _rule(conn, other_id, one_fact_status, **kwargs)

    report = dedup.rekey(conn, _rekey_dict(**_FUSING_ALBUMIN_SYNONYM), apply=True,
                         resolver=resolver)

    assert report.resolved == []
    assert [(c.row_id, c.clash_row_id, c.kind) for c in report.collisions] \
        == [(albumin_id, peaf_id, "fused")]
    assert report.blocked == ["lab_result"] and report.writable() == []
    # Both rulings are reported, in (entry row, clash row) order, with the settlements
    # that disagree - never one side presented as the answer.
    expected = {distinct_id: ("distinct", "distinct"),
                other_id: (one_fact_status, "merged")}
    assert [(v.row_id, v.status, v.settlement, v.scope)
            for v in report.collisions[0].contradiction] == [
        (albumin_id, *expected[albumin_id], "family"),
        (peaf_id, *expected[peaf_id], "family")]
    assert "contradictory verdicts" in report.collisions[0].message
    assert report.collisions[0].message.isascii()            # issue #23
    # Nothing was written: not the keys (the #92 quarantine)...
    assert _keys(conn, "lab_result", "test_name") == before
    # ...and not the verdicts either - both are still the family-scoped rulings the
    # operator recorded, so re-ruling one of them is all that is needed.
    assert sorted(r["record_id"] for r in
                  conn.execute("SELECT record_id FROM curation")) == [0, 0]


@pytest.mark.parametrize("first_status,second_status,settlement", [
    ("distinct", "distinct", "distinct"),
    ("merged-into", "superseded", "merged"),
])
def test_agreeing_resolving_verdicts_on_both_rows_still_resolve(
    conn, first_status, second_status, settlement
):
    """AC4 regression. Two rulings that answer the identity question the *same* way are
    not a contradiction - they are the pre-#124 "one resolving verdict wins" case with a
    redundant second ruling, and it must resolve exactly as it always did."""
    peaf_id, albumin_id = _fusing_pair(conn)
    kwargs = ({"merged_into_base": _lab_rows(conn)[albumin_id]["dedup_base"]}
              if first_status == "merged-into" else {})
    _rule(conn, peaf_id, first_status, **kwargs)
    kwargs = ({"merged_into_base": _lab_rows(conn)[peaf_id]["dedup_base"]}
              if second_status == "merged-into" else {})
    resolver = _rule(conn, albumin_id, second_status, **kwargs)

    report = dedup.rekey(conn, _rekey_dict(**_FUSING_ALBUMIN_SYNONYM), apply=True,
                         resolver=resolver)

    assert report.collisions == [] and report.blocked == []
    assert [(r.row_id, r.clash_row_id, r.settlement, r.new_occurrence)
            for r in report.resolved] == [(albumin_id, peaf_id, settlement, 1)]
    rows = _lab_rows(conn)
    assert rows[albumin_id]["dedup_base"] == rows[peaf_id]["dedup_base"]
    assert (rows[peaf_id]["dedup_occurrence"],
            rows[albumin_id]["dedup_occurrence"]) == (0, 1)


def test_one_resolving_verdict_beside_an_unverdicted_row_still_resolves(conn):
    """The other half of AC4: a pair carrying exactly one ruling has nothing to disagree
    with, so the #124 check must not touch it."""
    peaf_id, albumin_id = _fusing_pair(conn)
    resolver = _rule(conn, albumin_id, "distinct")

    report = dedup.rekey(conn, _rekey_dict(**_FUSING_ALBUMIN_SYNONYM), apply=True,
                         resolver=resolver)

    assert report.collisions == []
    assert [(r.row_id, r.settlement) for r in report.resolved] \
        == [(albumin_id, "distinct")]


def test_a_contradictory_pair_blocks_a_third_row_the_same_anchor_resolves(conn):
    """Contradiction is judged per `(entry, clash)` pair against the first-seen anchor, so
    a third row on the one key is adjudicated on its own: the contradictory pair blocks
    while the third row's clash against that same anchor is settled by the family verdict.
    The table is quarantined either way (issue #92), so the resolution says it was withheld
    and not one row moves."""
    peaf_id, albumin_id = _fusing_pair(conn)
    dedup.commit_extraction(conn, _make_document(conn), {"lab_result": [
        {"test_name": "SPEP Albumin", "collected_at": "2026-01-02", "value_num": 3.9},
    ]}, dedup.load_dictionary(DICT_PATH))
    third_id = max(_lab_rows(conn))
    before = _keys(conn, "lab_result", "test_name")
    _rule(conn, peaf_id, "distinct")                       # family, on the anchor
    resolver = _rule(conn, albumin_id, "superseded", row=True)   # contradicts it

    report = dedup.rekey(conn, _rekey_dict(**_FUSING_ALBUMIN_SYNONYM,
                                           **{"spep albumin": "albumin"}),
                         apply=True, resolver=resolver)

    assert [(c.row_id, c.clash_row_id) for c in report.collisions] \
        == [(albumin_id, peaf_id)]
    assert [(v.row_id, v.settlement) for v in report.collisions[0].contradiction] \
        == [(albumin_id, "merged"), (peaf_id, "distinct")]
    # The third row carries no ruling of its own, so its pair holds exactly one verdict
    # and resolves as it always did - the contradiction next door does not spread.
    assert [(r.row_id, r.clash_row_id, r.settlement) for r in report.resolved] \
        == [(third_id, peaf_id, "distinct")]
    assert report.blocked == ["lab_result"] and report.writable() == []
    assert _keys(conn, "lab_result", "test_name") == before


def _generic_condition_pair(conn):
    """Two real, different conditions extracted under generic placeholder labels.

    The `meridun/pemr-data#12` item 6 shape: an extractor that numbers repeated fields
    emits `diagnosis` / `diagnosis 2`, so the labels carry no clinical identity at all and
    the payloads differ only in the note. Returns ``(first id, second id)``."""
    dedup.commit_extraction(conn, _make_document(conn), {"condition": [
        {"name": "diagnosis", "status": "active", "note": "hypertension, per cardiology"},
        {"name": "diagnosis 2", "status": "active", "note": "asthma, per pulmonology"},
    ]}, dedup.load_dictionary(DICT_PATH))
    return tuple(int(r["condition_id"]) for r in conn.execute(
        "SELECT condition_id FROM condition ORDER BY condition_id"))


def test_generic_condition_labels_declared_distinct_are_rekeyed_apart(conn):
    """AC6 - the real-world trigger, end to end at the engine.

    A dictionary edit folds `diagnosis 2` onto `diagnosis`, and because the labels are
    placeholders rather than synonyms of anything there is no dictionary fix available:
    both rows are real and different. Without a verdict the whole `condition` table is
    withheld; with a `distinct` verdict `--apply` writes it and both rows survive as
    occurrences 0 and 1 of one family."""
    first_id, second_id = _generic_condition_pair(conn)
    d_new = _rekey_dict(**{"diagnosis 2": "diagnosis"})
    before = _keys(conn, "condition", "name")

    blocked = dedup.rekey(conn, d_new, apply=True,
                          resolver=curation.collision_resolver(conn))
    assert [c.kind for c in blocked.collisions] == ["fused"]
    assert blocked.blocked == ["condition"]
    assert _keys(conn, "condition", "name") == before        # #92 quarantine held

    curation.annotate_record(conn, "condition", str(first_id), status="distinct",
                             note="two different diagnoses under numbered labels",
                             attributed_to="Dr Who", apply=True)

    report = dedup.rekey(conn, d_new, apply=True,
                         resolver=curation.collision_resolver(conn))

    assert report.collisions == [] and report.blocked == []
    assert [(r.record_type, r.row_id, r.clash_row_id, r.settlement)
            for r in report.resolved] == [
        ("condition", second_id, first_id, "distinct")]
    rows = {int(r["condition_id"]): r
            for r in conn.execute("SELECT * FROM condition")}
    shared = rows[first_id]["dedup_base"]
    assert rows[second_id]["dedup_base"] == shared
    assert sorted(int(r["dedup_occurrence"]) for r in rows.values()) == [0, 1]
    # Both facts are still there, unmutated: only the key machinery moved.
    assert {r["note"] for r in rows.values()} == {
        "hypertension, per cardiology", "asthma, per pulmonology"}


# --- ingest-before-rekey drift guard ------------------------------------------

def test_commit_refuses_an_ingest_against_a_drifted_key(conn):
    """The migration hazard behind issue #71: a stored row whose frozen key predates
    the current dictionary is invisible to layer-2 dedup, so re-filing that same fact
    used to land a SECOND row reported as `new` -- no duplicate, no conflict, no signal
    at all -- and then wedged `rekey` on the collision it had just created. Refuse the
    commit instead, naming the rekey that fixes it."""
    doc = _make_document(conn)
    row = {"test_name": "ZZT", "collected_at": "2026-01-02", "value_num": 108}
    dedup.commit_extraction(conn, doc, {"lab_result": [row]}, None)
    d_new = _rekey_dict(zzt="zonulin_test")       # the stored key is now stale

    with pytest.raises(dedup.DictionaryDriftError, match="rekey --apply"):
        dedup.commit_extraction(conn, _make_document(conn), {"lab_result": [row]}, d_new)
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 1

    # And the named remedy actually clears it: after the rekey the same submission
    # dedups against the stored row instead of forking it.
    dedup.rekey(conn, d_new, apply=True)
    summary = dedup.commit_extraction(
        conn, _make_document(conn), {"lab_result": [row]}, d_new
    )
    assert summary.counts == {"new": 0, "duplicate": 1, "enriched": 0, "conflict": 0, "promoted": 0}
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 1


def test_commit_drift_guard_ignores_an_unrelated_drifted_row(conn):
    """Deliberately narrow: drift somewhere else in the database is a `rekey` chore,
    not a reason to refuse an unrelated ingest."""
    doc = _make_document(conn)
    dedup.commit_extraction(conn, doc, {"lab_result": [
        {"test_name": "ZZT", "collected_at": "2026-01-02", "value_num": 108}]}, None)
    d_new = _rekey_dict(zzt="zonulin_test")       # ZZT's stored key is stale

    summary = dedup.commit_extraction(conn, _make_document(conn), {"lab_result": [
        {"test_name": "Glucose", "collected_at": "2026-01-02", "value_num": 95}]}, d_new)
    assert summary.counts == {"new": 1, "duplicate": 0, "enriched": 0, "conflict": 0, "promoted": 0}


def test_commit_drift_guard_accepts_a_keep_both_sibling(conn):
    """An admitted repeat keys on hash(base|occurrence); if the guard recomputed at
    occurrence 0 it would read every sibling as drift and refuse every later ingest."""
    d = dedup.load_dictionary(DICT_PATH)
    dedup.commit_extraction(conn, _make_document(conn), {"lab_result": [
        {"test_name": "Glucose", "collected_at": "2024-04-01", "value_num": 95}]}, d)
    dedup.commit_extraction(conn, _make_document(conn), {"lab_result": [
        {"test_name": "Glucose", "collected_at": "2024-04-01", "value_num": 148}]}, d)
    dedup.resolve_conflict(conn, dedup.list_conflicts(conn)[0]["conflict_id"], keep="both")

    summary = dedup.commit_extraction(conn, _make_document(conn), {"lab_result": [
        {"test_name": "Glucose", "collected_at": "2024-04-01", "value_num": 148}]}, d)
    assert summary.counts == {"new": 0, "duplicate": 1, "enriched": 0, "conflict": 0, "promoted": 0}


def test_rekey_names_a_doubled_fact_instead_of_blaming_the_dictionary(conn):
    """A database that drifted *before* the guard existed can still hold one fact under
    two keys. "The dictionary maps two distinct facts onto one canonical name" is then
    exactly the wrong diagnosis -- the dictionary is fine and the data is doubled."""
    doc = _make_document(conn)
    row = {"test_name": "Glucose", "collected_at": "2026-01-02", "value_num": 95}
    dedup.commit_extraction(conn, doc, {"lab_result": [row]}, None)
    pid = conn.execute("SELECT person_id FROM person").fetchone()["person_id"]
    # The pre-guard state, built directly: the same fact filed again under a stale key.
    dedup._insert_record(conn, "lab_result", row, pid, doc, "stale-base-0000")
    conn.commit()

    report = dedup.rekey(conn, None)
    assert [c.kind for c in report.collisions] == ["doubled"]
    assert "SAME fact" in report.collisions[0].message
    # The colliding table is still all-or-nothing, and the dry-run wrote nothing either.
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 2
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM lab_result WHERE dedup_key = 'stale-base-0000'"
    ).fetchone()["n"] == 1


def test_rekey_reports_a_mixed_precision_pair_as_doubled(conn):
    """The issue-#117 upgrade path: a database written *before* the date-only lab key
    holds the reported bug's pair -- one draw under two keys because the documents stated
    it at different precision. `rekey` is the migration, and it names the pair correctly:
    the dictionary is fine, the data is doubled, so `pemr record rm` is the fix."""
    import hashlib

    d = dedup.load_dictionary(DICT_PATH)
    pid = conn.execute("SELECT person_id FROM person").fetchone()["person_id"]
    doc = _make_document(conn)
    payload = {"test_name": "Glucose", "value_num": 95, "unit": "mg/dL"}
    # Seed the pre-change state directly: commit_extraction would now dedup these.
    for collected_at in ("2026-04-01", "2026-04-01T09:15"):
        legacy = hashlib.sha256("|".join([                     # pre-#117: _norm_ts()
            str(pid),
            dedup.key_token(payload["test_name"], d),
            collected_at.replace("T", " "),
        ]).encode("utf-8")).hexdigest()
        dedup._insert_record(conn, "lab_result",
                             {**payload, "collected_at": collected_at}, pid, doc, legacy)
    conn.commit()

    report = dedup.rekey(conn, d)
    assert [c.kind for c in report.collisions] == ["doubled"]
    assert "pemr record rm" in report.collisions[0].message
    assert report.blocked == ["lab_result"]
    # Dry run wrote nothing: both rows still sit on their legacy keys.
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 2

    report = dedup.rekey(conn, d, apply=True)
    assert [c.kind for c in report.collisions] == ["doubled"]
    assert report.blocked == ["lab_result"]
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 2
    assert len({r["dedup_key"] for r in conn.execute(
        "SELECT dedup_key FROM lab_result")}) == 2      # left on stored keys, unfused


def test_rekey_still_blames_the_dictionary_when_it_fuses_distinct_facts(conn):
    """The other cause keeps its own message: two *different* payloads recomputing onto
    one key really is a dictionary that merges distinct facts (here the SPEP albumin
    fraction fused with the CMP `Albumin` off one draw)."""
    doc = _make_document(conn)
    dedup.commit_extraction(conn, doc, _FUSING_ALBUMIN_PAIR,
                            dedup.load_dictionary(DICT_PATH))
    report = dedup.rekey(conn, _rekey_dict(**_FUSING_ALBUMIN_SYNONYM))
    assert [c.kind for c in report.collisions] == ["fused"]
    assert "two distinct facts" in report.collisions[0].message


# --- allergy / condition typed rows (issue #63) -------------------------------

def _commit(conn, records, doc=None, dictionary=None):
    return dedup.commit_extraction(
        conn, doc if doc is not None else _make_document(conn), records, dictionary
    )


def test_allergy_key_is_date_free(conn):
    """Allergies are standing facts restated on every document with inconsistent dates,
    so the same allergen collapses to ONE row however the dates differ."""
    _commit(conn, {"allergy": [
        {"substance": "Penicillin", "reaction": "rash", "noted_on": "2010-01-01"}]})
    summary = _commit(conn, {"allergy": [
        {"substance": "penicillin", "reaction": "rash", "noted_on": "2021-06-01"}]})
    assert summary.counts == {"new": 0, "duplicate": 0, "enriched": 0, "conflict": 1, "promoted": 0}
    assert conn.execute("SELECT COUNT(*) AS n FROM allergy").fetchone()["n"] == 1


def test_condition_family_history_does_not_collide_with_the_patients_own(conn):
    """The clinical-safety defect the typed table exists to fix: under the old
    observation key, the patient's diabetes and her mother's deduped into one row."""
    summary = _commit(conn, {"condition": [
        {"name": "Type 2 Diabetes", "status": "active"},
        {"name": "Type 2 Diabetes", "status": "family-history", "relation": "mother"},
        {"name": "Type 2 Diabetes", "status": "family-history", "relation": "father"},
    ]})
    assert summary.counts == {"new": 3, "duplicate": 0, "enriched": 0, "conflict": 0, "promoted": 0}


def test_condition_lifecycle_change_stages_a_conflict(conn):
    """active -> resolved keeps the same key (the date is payload), so the change is a
    conflict for a human rather than a silent second problem-list entry."""
    _commit(conn, {"condition": [{"name": "Anemia", "status": "active"}]})
    summary = _commit(conn, {"condition": [
        {"name": "Anemia", "status": "resolved", "resolved_on": "2025-09-01"}]})
    assert summary.counts == {"new": 0, "duplicate": 0, "enriched": 0, "conflict": 1, "promoted": 0}

    conflict_id = dedup.list_conflicts(conn)[0]["conflict_id"]
    dedup.resolve_conflict(conn, conflict_id, keep="incoming")
    row = conn.execute("SELECT * FROM condition").fetchone()
    assert (row["status"], row["resolved_on"]) == ("resolved", "2025-09-01")


# --- issue #152: the episode model on `condition` -----------------------------

def test_two_dated_episodes_of_one_condition_both_commit(conn):
    """The defect this issue exists to fix. A recurring problem - kidney stones, UTIs,
    fractures - used to fold every recurrence into the first row: the second episode
    collided with the first, there was nowhere for its date to live, and the count and
    every date but one vanished with no warning. Onset in the identity keys them apart."""
    summary = _commit(conn, {"condition": [
        {"name": "Kidney stones", "status": "resolved", "onset_on": "1998-05"},
        {"name": "Kidney stones", "status": "resolved", "onset_on": "2009-09-15"},
    ]})
    assert summary.counts == {"new": 2, "duplicate": 0, "enriched": 0, "conflict": 0,
                              "promoted": 0}
    rows = conn.execute(
        "SELECT * FROM condition ORDER BY onset_on"
    ).fetchall()
    assert [r["onset_on"] for r in rows] == ["1998-05", "2009-09-15"]
    # Two identities, not one family with two occurrences: the occurrence tiebreaker is
    # the fallback now, and a dated pair must never need it.
    assert rows[0]["dedup_base"] != rows[1]["dedup_base"]
    assert [int(r["dedup_occurrence"]) for r in rows] == [0, 0]
    # ... and re-stating one of them is still an ordinary duplicate.
    again = _commit(conn, {"condition": [
        {"name": "kidney stones", "status": "resolved", "onset_on": "2009-09-15"}]})
    assert again.counts["duplicate"] == 1


def test_an_undated_condition_key_is_unchanged_by_the_episode_model(conn):
    """The folding guarantee, pinned to a literal hash (issue #152).

    Onset is folded into the existing subject part rather than appended as a fourth key
    part, so an *undated* condition row hashes exactly what it hashed before the episode
    model landed. That is what keeps the migration to `pemr rekey --apply` cheap and
    keeps every family-scoped verdict on an undated family attached. A change here means
    the whole condition table moved, undated rows included - re-read
    `dedup._condition_episode` before touching this number."""
    assert dedup.dedup_key(
        "condition", {"name": "Chickenpox", "status": "history"}, 1
    ) == "e5c118be803abf4ec14294ceef69f624c95c2f021a1466eeedcdc6ed0ff39a44"
    # The family-history subject too - it is the same key part.
    assert dedup.dedup_key(
        "condition",
        {"name": "Chickenpox", "status": "family-history", "relation": "Mother"}, 1,
    ) == "48b2c929e52fbb0121f1f2805831a3d094a84eef88fd05f8a7312549c61185e8"
    # A blank onset is *absent*, not an empty discriminator: `""` must key like NULL.
    assert dedup.dedup_key(
        "condition", {"name": "Chickenpox", "status": "history", "onset_on": "  "}, 1
    ) == dedup.dedup_key(
        "condition", {"name": "Chickenpox", "status": "history"}, 1
    )


def test_an_undated_repeat_still_falls_back_to_the_occurrence_tiebreaker(conn):
    """`dedup_occurrence` is demoted, not replaced. Two undated statements of one problem
    still collide - which is correct, since nothing distinguishes them - and `--keep both`
    is still the recovery path that admits the second as a sibling."""
    _commit(conn, {"condition": [
        {"name": "Cellulitis", "status": "resolved", "note": "left calf"}]})
    # A *stated* disagreement, not an omission: `condition` is a sparse type, so a note
    # over a stored NULL would enrich the first row instead of colliding with it.
    summary = _commit(conn, {"condition": [
        {"name": "Cellulitis", "status": "resolved", "note": "right calf"}]})
    assert summary.counts["conflict"] == 1

    conflict_id = dedup.list_conflicts(conn)[0]["conflict_id"]
    dedup.resolve_conflict(conn, conflict_id, keep="both")
    rows = conn.execute(
        "SELECT * FROM condition ORDER BY dedup_occurrence"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["dedup_base"] == rows[1]["dedup_base"]
    assert [int(r["dedup_occurrence"]) for r in rows] == [0, 1]


def test_onset_precision_gives_three_identities_stored_verbatim(conn):
    """Partial dates, FHIR-style: `1998`, `1998-05` and `1998-05-20` are three different
    claims, stored at the precision the source stated with no sentinel padding, and each
    a lexical prefix of the next so they sort in that order."""
    summary = _commit(conn, {"condition": [
        {"name": "Migraine", "status": "history", "onset_on": "1998-05-20"},
        {"name": "Migraine", "status": "history", "onset_on": "1998"},
        {"name": "Migraine", "status": "history", "onset_on": "1998-05"},
    ]})
    assert summary.counts["new"] == 3
    stored = [
        r["onset_on"] for r in
        conn.execute("SELECT onset_on FROM condition ORDER BY onset_on").fetchall()
    ]
    # Verbatim (no `1998-01-01` padding) and lexically ordered in one assertion.
    assert stored == ["1998", "1998-05", "1998-05-20"]
    assert len({
        dedup.dedup_key("condition",
                        {"name": "Migraine", "status": "history", "onset_on": v}, 1)
        for v in stored
    }) == 3
    # A stray time component is still truncated to the date, as on every dated type.
    assert dedup.dedup_key(
        "condition",
        {"name": "Migraine", "status": "history", "onset_on": "1998-05-20T09:00"}, 1,
    ) == dedup.dedup_key(
        "condition",
        {"name": "Migraine", "status": "history", "onset_on": "1998-05-20"}, 1,
    )


def test_onset_is_identity_not_payload_in_both_tables():
    """The two tables that have to agree once onset moved (issue #152): `record edit`
    stops offering it, and the duplicate-vs-conflict comparison stops naming it - which
    is what keeps `_sparse_gains` from offering to write an identity field."""
    assert "onset_on" in dedup.KEY_FIELDS["condition"]
    assert dedup.editable_fields("condition") == ("resolved_on", "note")
    assert "onset_on" not in dedup._COMPARE_FIELDS["condition"]


def test_rekey_splits_dated_episodes_and_leaves_undated_rows_alone(conn):
    """The migration, end to end (issue #152). Over a table holding both kinds:

    * every *dated* row's key moves - that is the rekey the identity change owes;
    * every *undated* row's key does not, the folding guarantee;
    * a `--keep both` pair with different onsets **splits** into two bases, and the
      former occurrence-1 row keeps its stored occurrence rather than being renumbered
      (renumbering would rewrite sibling keys out from under any staged conflict);
    * nothing collides - adding a discriminator to a key only ever splits families.
    """
    doc = _make_document(conn)
    # Two undated statements of one problem, admitted as a family via `--keep both`,
    # then dated differently: the umbrella row that the episode model unfolds.
    _commit(conn, {"condition": [
        {"name": "UTI", "status": "resolved", "note": "first course"}]}, doc=doc)
    _commit(conn, {"condition": [
        {"name": "UTI", "status": "resolved", "note": "second course"}]}, doc=doc)
    dedup.resolve_conflict(conn, dedup.list_conflicts(conn)[0]["conflict_id"],
                           keep="both")
    _commit(conn, {"condition": [{"name": "Eczema", "status": "active"}]}, doc=doc)

    ids = [int(r["condition_id"]) for r in conn.execute(
        "SELECT condition_id FROM condition ORDER BY condition_id")]
    first, second, eczema = ids
    before = {
        int(r["condition_id"]): r["dedup_key"]
        for r in conn.execute("SELECT condition_id, dedup_key FROM condition")
    }
    for row_id, onset in ((first, "2021-03-02"), (second, "2024-11-18")):
        conn.execute("UPDATE condition SET onset_on = ? WHERE condition_id = ?",
                     (onset, row_id))
    conn.commit()

    report = dedup.rekey(conn, None, apply=True)
    assert report.collisions == [] and report.blocked == []
    assert {c.row_id for c in report.changes} == {first, second}

    rows = {int(r["condition_id"]): r
            for r in conn.execute("SELECT * FROM condition")}
    assert rows[eczema]["dedup_key"] == before[eczema]          # undated: untouched
    assert rows[first]["dedup_base"] != rows[second]["dedup_base"]   # split
    # The hole is deliberate: the second row stays occurrence 1 on its own new base.
    assert int(rows[first]["dedup_occurrence"]) == 0
    assert int(rows[second]["dedup_occurrence"]) == 1
    assert rows[second]["dedup_key"] == dedup.occurrence_key(
        rows[second]["dedup_base"], 1
    )
    # Idempotent: a second run has nothing left to move.
    assert dedup.rekey(conn, None, apply=True).changes == []


def test_sparse_types_do_not_conflict_on_an_omitted_field(conn):
    """A document that simply doesn't restate criticality means "didn't say", not
    "cleared" - otherwise re-ingesting next year's summary stages a conflict per allergy."""
    _commit(conn, {"allergy": [
        {"substance": "Sulfa", "reaction": "hives", "criticality": "high"}]})
    summary = _commit(conn, {"allergy": [{"substance": "Sulfa"}]})
    assert summary.counts == {"new": 0, "duplicate": 1, "enriched": 0, "conflict": 0, "promoted": 0}
    # ... but a stated disagreement still conflicts.
    summary = _commit(conn, {"allergy": [
        {"substance": "Sulfa", "reaction": "anaphylaxis"}]})
    assert summary.counts == {"new": 0, "duplicate": 0, "enriched": 0, "conflict": 1, "promoted": 0}


def test_a_stated_field_over_a_stored_null_enriches_rather_than_dedups(conn):
    """The other half of the sparse rule: silence is only silence in the *incoming*
    direction. A later document supplying criticality/reaction the record lacks is new
    information with nothing to adjudicate - fill the NULLs, don't drop the row."""
    _commit(conn, {"allergy": [{"substance": "Bee sting"}]})
    summary = _commit(conn, {"allergy": [
        {"substance": "Bee sting", "criticality": "high", "reaction": "anaphylaxis",
         "noted_on": "2024-07-07"}]})
    assert summary.counts == {"new": 0, "duplicate": 0, "enriched": 1, "conflict": 0, "promoted": 0}
    row = conn.execute("SELECT * FROM allergy").fetchone()
    assert (row["criticality"], row["reaction"], row["noted_on"]) == (
        "high", "anaphylaxis", "2024-07-07")
    assert summary.enriched == [("allergy", row["allergy_id"])]
    # Re-stating exactly what is now stored is a plain duplicate again.
    summary = _commit(conn, {"allergy": [
        {"substance": "Bee sting", "criticality": "high", "reaction": "anaphylaxis",
         "noted_on": "2024-07-07"}]})
    assert summary.counts["duplicate"] == 1 and summary.counts["enriched"] == 0


def test_enrichment_fills_only_nulls_and_never_launders_a_disagreement(conn):
    """A stated value must never overwrite a stored one on the quiet path - that is
    still a conflict, even when the same row also has a NULL the document could fill.

    The fillable NULL is `resolved_on`, not `onset_on`: since issue #152 onset is part of
    a condition's identity, so an incoming row stating one keys elsewhere entirely and
    never reaches the duplicate-vs-conflict comparison this test is about."""
    _commit(conn, {"condition": [{"name": "Chickenpox", "status": "history"}]})
    summary = _commit(conn, {"condition": [
        {"name": "Chickenpox", "status": "resolved", "resolved_on": "1988-03-01"}]})
    assert summary.counts == {"new": 0, "duplicate": 0, "enriched": 0, "conflict": 1, "promoted": 0}
    row = conn.execute("SELECT * FROM condition").fetchone()
    assert (row["status"], row["resolved_on"]) == ("history", None)


def test_a_terser_row_later_in_one_submission_does_not_undo_an_enrichment(conn):
    """Both directions inside a single batch: the detailed row enriches the terse one,
    and a third terse row after it is still just a duplicate.

    `resolved_on` rather than `onset_on`, for the reason the test above states."""
    summary = _commit(conn, {"condition": [
        {"name": "Anemia", "status": "resolved"},
        {"name": "Anemia", "status": "resolved", "note": "iron deficiency",
         "resolved_on": "2019-02-01"},
        {"name": "Anemia", "status": "resolved"},
    ]})
    assert summary.counts == {"new": 1, "duplicate": 1, "enriched": 1, "conflict": 0, "promoted": 0}
    row = conn.execute("SELECT * FROM condition").fetchone()
    assert (row["note"], row["resolved_on"]) == ("iron deficiency", "2019-02-01")


def test_keep_incoming_on_a_sparse_type_keeps_fields_the_document_did_not_state(conn):
    """Adjudicating one field must not erase the rest of a standing fact's payload:
    the incoming document not repeating `reaction` means "didn't say", here too."""
    _commit(conn, {"allergy": [
        {"substance": "Penicillin", "reaction": "anaphylaxis", "criticality": "high",
         "noted_on": "2010-05-05"}]})
    summary = _commit(conn, {"allergy": [
        {"substance": "Penicillin", "criticality": "low"}]})
    assert summary.counts["conflict"] == 1
    dedup.resolve_conflict(conn, dedup.list_conflicts(conn)[0]["conflict_id"],
                           keep="incoming")
    row = conn.execute("SELECT * FROM allergy").fetchone()
    assert (row["criticality"], row["reaction"], row["noted_on"]) == (
        "low", "anaphylaxis", "2010-05-05")


def test_keep_incoming_still_clears_an_unstated_field_on_a_dated_type(conn):
    """Scoped to sparse types: for a lab draw an unstated field IS a clearing, and
    keep-incoming keeps its documented overwrite-the-row semantics."""
    _commit(conn, {"lab_result": [
        {"test_name": "Glucose", "collected_at": "2026-01-02", "value_num": 95,
         "unit": "mg/dL", "flag": "H"}]})
    _commit(conn, {"lab_result": [
        {"test_name": "Glucose", "collected_at": "2026-01-02", "value_num": 101}]})
    dedup.resolve_conflict(conn, dedup.list_conflicts(conn)[0]["conflict_id"],
                           keep="incoming")
    row = conn.execute("SELECT * FROM lab_result").fetchone()
    assert (row["value_num"], row["unit"], row["flag"]) == (101, None, None)


def test_keep_both_no_op_fills_what_the_sibling_was_missing(conn):
    """A strictly-thinner sibling "matches" the staged payload under the sparse
    comparison, so keep-both is still a no-op - but it must not swallow the fields the
    payload adds on its way to that no-op."""
    # occ0 is Latex/hives; two later documents both stage a "rash" conflict against it,
    # one of them also stating criticality.
    _commit(conn, {"allergy": [{"substance": "Latex", "reaction": "hives"}]})
    _commit(conn, {"allergy": [
        {"substance": "Latex", "reaction": "rash", "criticality": "high"}]})
    _commit(conn, {"allergy": [{"substance": "Latex", "reaction": "rash"}]})
    staged = {
        ("criticality" in c["incoming_json"]): c["conflict_id"]
        for c in dedup.list_conflicts(conn)
    }
    rich, thin = staged[True], staged[False]
    # The thin one is admitted first, so the rich one now matches a sibling missing
    # criticality.
    dedup.resolve_conflict(conn, thin, keep="both")
    result = dedup.resolve_conflict(conn, rich, keep="both")
    assert result.no_op is True and result.gains == {"criticality": "high"}
    assert conn.execute("SELECT COUNT(*) AS n FROM allergy").fetchone()["n"] == 2
    admitted = conn.execute(
        "SELECT * FROM allergy WHERE allergy_id = ?", (result.row_id,)
    ).fetchone()
    assert (admitted["reaction"], admitted["criticality"]) == ("rash", "high")
    assert "filled criticality" in dedup.list_conflicts(
        conn, status="resolved")[0]["resolution"]


def test_labs_keep_the_strict_comparison(conn):
    """The sparse rule is scoped to standing facts: for a dated lab draw a cleared unit
    is still news, so a one-sided None must stay a conflict."""
    _commit(conn, {"lab_result": [
        {"test_name": "Glucose", "collected_at": "2026-01-02", "value_num": 95,
         "unit": "mg/dL"}]})
    summary = _commit(conn, {"lab_result": [
        {"test_name": "Glucose", "collected_at": "2026-01-02", "value_num": 95}]})
    assert summary.counts["conflict"] == 1


def test_condition_status_is_required_and_enum_checked(conn):
    with pytest.raises(dedup.ValidationError, match="missing required field 'status'"):
        dedup.validate_row("condition", {"name": "Anemia"})
    with pytest.raises(dedup.ValidationError, match="active, family-history"):
        dedup.validate_row("condition", {"name": "Anemia", "status": "inactive"})


def test_enum_values_are_matched_leniently_but_stored_verbatim(conn):
    """A source's own casing/punctuation validates; the column keeps what it wrote."""
    dedup.validate_row(
        "allergy", {"substance": "Latex", "criticality": "Unable to Assess"})
    _commit(conn, {"condition": [{"name": "Asthma", "status": "Family_History",
                                  "relation": "Mother"}]})
    row = conn.execute("SELECT * FROM condition").fetchone()
    assert row["status"] == "Family_History"        # verbatim
    # ... and it still keys as family history, so it never joins the patient's own list.
    summary = _commit(conn, {"condition": [{"name": "Asthma", "status": "active"}]})
    assert summary.counts["new"] == 1


def test_allergy_criticality_is_optional_but_checked(conn):
    dedup.validate_row("allergy", {"substance": "Latex"})              # no raise
    with pytest.raises(dedup.ValidationError, match="high, low"):
        dedup.validate_row("allergy", {"substance": "Latex", "criticality": "severe"})


def test_new_types_have_date_validation(conn):
    with pytest.raises(dedup.ValidationError, match="expected ISO date"):
        dedup.validate_row("allergy", {"substance": "Latex", "noted_on": "06/15/2026"})
    with pytest.raises(dedup.ValidationError, match="expected ISO date"):
        dedup.validate_row(
            "condition", {"name": "Anemia", "status": "active", "resolved_on": "soon"})


def test_rekey_rederives_a_carried_forward_migration_key(conn):
    """Migration 006 moves rows carrying their OLD observation key (SQL can't compute
    sha256 over normalized fields); `pemr rekey --apply` is what re-derives them, and it
    must name the row by substance/name rather than blowing up on `obs_type`."""
    pid = conn.execute("SELECT person_id FROM person").fetchone()["person_id"]
    conn.execute(
        "INSERT INTO allergy (person_id, substance, reaction, dedup_key, dedup_base) "
        "VALUES (?, 'Penicillin', 'rash', 'legacy-obs-key', 'legacy-obs-key')", (pid,))
    conn.commit()

    report = dedup.rekey(conn, None)
    change = next(c for c in report.changes if c.record_type == "allergy")
    assert change.old_key == "legacy-obs-key"
    assert change.label == "Penicillin"
    assert change.new_key == dedup.dedup_key("allergy", {"substance": "Penicillin"}, pid)

    dedup.rekey(conn, None, apply=True)
    row = conn.execute("SELECT * FROM allergy").fetchone()
    assert row["dedup_key"] == change.new_key and row["dedup_base"] == change.new_key
    # ... and now a fresh commit of the same allergy dedups instead of forking.
    summary = _commit(conn, {"allergy": [{"substance": "Penicillin", "reaction": "rash"}]})
    assert summary.counts == {"new": 0, "duplicate": 1, "enriched": 0, "conflict": 0, "promoted": 0}


# --- attested rows meet the document that proves them (issue #110) ------------

_ATTEST_MED = {"name": "Metformin", "dose": "500 mg", "started_on": "2025-06-01"}


def _attest(conn, record_type="medication", payload=None, **kwargs):
    from pemr import attestations

    return attestations.assert_record(
        conn, record_type, "jane-doe", dict(payload or _ATTEST_MED),
        attributed_to=kwargs.pop("attributed_to", "Mom"),
        attested_on=kwargs.pop("attested_on", "2026-08-09"),
        apply=True, **kwargs,
    )


def test_a_commit_with_no_attestation_in_play_still_reports_promoted_zero(conn):
    summary = _commit(conn, {"medication": [_ATTEST_MED]})
    assert summary.counts == {
        "new": 1, "duplicate": 0, "enriched": 0, "conflict": 0, "promoted": 0}
    assert summary.promoted == []


def test_an_agreeing_document_promotes_the_attestation_in_place(conn):
    """Document provenance outranks an attestation, and the agreeing case has nothing
    to adjudicate: the row acquires the document_id, keeps its attestation as history,
    and no second row is filed."""
    report = _attest(conn)
    summary = _commit(conn, {"medication": [_ATTEST_MED]})
    assert summary.counts == {
        "new": 0, "duplicate": 0, "enriched": 0, "conflict": 0, "promoted": 1}
    assert summary.promoted == [("medication", report.row_id)]

    rows = conn.execute("SELECT * FROM medication").fetchall()
    assert len(rows) == 1                       # promoted, not duplicated
    assert rows[0]["document_id"] is not None
    assert (rows[0]["attested_by"], rows[0]["attested_on"]) == ("Mom", "2026-08-09")
    assert dedup.attestation_state(rows[0]) == "superseded"
    assert dedup.is_attested(rows[0]) is False


def test_a_promoted_row_is_an_ordinary_duplicate_on_the_next_commit(conn):
    _attest(conn)
    _commit(conn, {"medication": [_ATTEST_MED]})
    summary = _commit(conn, {"medication": [_ATTEST_MED]})
    assert summary.counts["promoted"] == 0 and summary.counts["duplicate"] == 1


def test_a_disagreeing_document_stages_a_conflict_and_leaves_the_attestation(conn):
    """No silent auto-resolution for a differing payload: it takes the same
    human-adjudicated path every other dedup disagreement takes."""
    _attest(conn, payload=_ATTEST_MED | {"frequency": "BID"})
    summary = _commit(conn, {"medication": [_ATTEST_MED | {"frequency": "daily"}]})
    assert summary.counts == {
        "new": 0, "duplicate": 0, "enriched": 0, "conflict": 1, "promoted": 0}
    row = conn.execute("SELECT * FROM medication").fetchone()
    assert row["document_id"] is None and row["frequency"] == "BID"
    assert dedup.is_attested(row) is True


def test_keep_incoming_on_an_attested_anchor_is_the_conflict_flow_supersession(conn):
    _attest(conn, payload=_ATTEST_MED | {"frequency": "BID"})
    summary = _commit(conn, {"medication": [_ATTEST_MED | {"frequency": "daily"}]})
    dedup.resolve_conflict(conn, summary.conflict[0][1], keep="incoming")
    row = conn.execute("SELECT * FROM medication").fetchone()
    assert row["document_id"] is not None and row["frequency"] == "daily"
    assert row["attested_by"] == "Mom"          # history, never cleared
    assert dedup.attestation_state(row) == "superseded"


def test_keep_existing_leaves_the_attestation_live(conn):
    _attest(conn, payload=_ATTEST_MED | {"frequency": "BID"})
    summary = _commit(conn, {"medication": [_ATTEST_MED | {"frequency": "daily"}]})
    dedup.resolve_conflict(conn, summary.conflict[0][1], keep="existing")
    row = conn.execute("SELECT * FROM medication").fetchone()
    assert row["document_id"] is None and dedup.is_attested(row) is True


def test_keep_both_admits_the_document_row_beside_the_attestation(conn):
    _attest(conn, payload=_ATTEST_MED | {"frequency": "BID"})
    summary = _commit(conn, {"medication": [_ATTEST_MED | {"frequency": "daily"}]})
    dedup.resolve_conflict(conn, summary.conflict[0][1], keep="both")
    rows = conn.execute(
        "SELECT * FROM medication ORDER BY dedup_occurrence"
    ).fetchall()
    assert [int(r["dedup_occurrence"]) for r in rows] == [0, 1]
    assert dedup.is_attested(rows[0]) is True          # still needs a source
    assert rows[1]["document_id"] is not None
    assert rows[1]["attested_by"] is None


def test_rekey_is_blind_to_the_attestation_columns(conn):
    """The columns are absent from FIELD_SPECS, so keys are bit-identical for attested
    and document-sourced rows and `rekey` has nothing to move."""
    _attest(conn)
    _attest(conn, "allergy", {"substance": "Penicillin", "reaction": "rash"})
    report = dedup.rekey(conn, None)
    assert report.changes == [] and report.collisions == []

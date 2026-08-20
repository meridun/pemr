"""Misfiled-document recovery: `pemr document list|edit|reassign|rm` (issue #54),
plus `document show` and `document set-text` (issue #62).

Covers the module surface (pemr/documents.py) and the CLI wiring, in the shape
test_persons.py uses for the `person` group.
"""

import json

import pytest

from pemr import cli, db, dedup, documents, ingest, persons

RECORDS = {
    "lab_result": [
        {"test_name": "HbA1c", "collected_at": "2026-01-02", "value_num": 5.7,
         "unit": "%"},
    ],
    "medication": [
        {"name": "Metformin", "dose": "500 mg", "started_on": "2025-06-01"},
    ],
    "procedure": [
        {"name": "Colonoscopy", "performed_on": "2025-03-04"},
    ],
    "appointment": [
        {"scheduled_for": "2026-02-01", "provider": "Dr Who", "reason": "checkup"},
    ],
    "observation": [
        {"obs_type": "blood_pressure", "observed_at": "2026-01-02",
         "key": "systolic", "value_num": 120},
    ],
    "allergy": [
        {"substance": "Penicillin", "reaction": "rash", "criticality": "high"},
    ],
    "condition": [
        {"name": "Type 2 Diabetes", "status": "active", "onset_on": "2024-01-01"},
    ],
}

# One row of every known record type -- the fixture's whole point, so counts assert on
# this rather than on a literal that goes stale the next time a type is added.
_SEEDED_ROWS = sum(len(rows) for rows in RECORDS.values())


@pytest.fixture()
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    yield conn
    conn.close()


def _insert_document(conn, person_id, sha, *, doc_date="2026-03-15",
                     category="labs", provider="Quest", ocr_text="scan text"):
    cur = conn.execute(
        """
        INSERT INTO document
          (sha256, person_id, doc_date, category, provider, source_path,
           ocr_text, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (sha, person_id, doc_date, category, provider, f"{sha[:2]}/{sha}.pdf",
         ocr_text, "2026-03-16T00:00:00"),
    )
    conn.commit()
    return int(cur.lastrowid)


@pytest.fixture()
def seeded(conn):
    """Jane owns document #1 with one row of every record type; John is empty."""
    jane = persons.add_person(conn, "jane-doe", "Jane Doe")
    john = persons.add_person(conn, "john-doe", "John Doe")
    doc_id = _insert_document(conn, jane.person_id, "aa11bb22cc33dd44")
    dedup.commit_extraction(conn, doc_id, RECORDS)
    return {"conn": conn, "jane": jane, "john": john, "doc": doc_id}


def _fts_person_ids(conn, source_table):
    return [
        row["person_id"] for row in conn.execute(
            "SELECT person_id FROM record_fts WHERE source_table = ?", (source_table,)
        ).fetchall()
    ]


# --- list -------------------------------------------------------------------


def test_list_empty(conn):
    assert documents.list_documents(conn) == []


def test_list_newest_first_with_counts(seeded):
    conn, jane = seeded["conn"], seeded["jane"]
    second = _insert_document(conn, jane.person_id, "bb99")
    views = documents.list_documents(conn)
    assert [v["document_id"] for v in views] == [second, seeded["doc"]]
    assert views[1]["record_count"] == _SEEDED_ROWS
    assert views[1]["records"]["lab_result"] == 1
    assert views[0]["record_count"] == 0
    assert views[1]["person"] == "jane-doe"


def test_list_filtered_by_person(seeded):
    conn, john = seeded["conn"], seeded["john"]
    _insert_document(conn, john.person_id, "cc77")
    assert [v["person"] for v in documents.list_documents(conn, "john-doe")] == [
        "john-doe"
    ]
    assert len(documents.list_documents(conn, "jane-doe")) == 1


def test_list_unknown_slug_raises(seeded):
    with pytest.raises(persons.PersonNotFoundError):
        documents.list_documents(seeded["conn"], "nobody")


def test_list_replaces_ocr_text_with_a_flag(seeded):
    view = documents.list_documents(seeded["conn"])[0]
    assert "ocr_text" not in view
    assert view["has_ocr_text"] is True


# --- show (get_document_view / get_document_text) ---------------------------


def test_show_unknown_document_raises(conn):
    with pytest.raises(documents.DocumentNotFoundError):
        documents.get_document_view(conn, 999)


def test_show_carries_the_full_stable_record_key_set(seeded):
    view = documents.get_document_view(seeded["conn"], seeded["doc"])
    assert set(view["records"]) == set(dedup.KNOWN_TYPES)
    assert view["record_count"] == _SEEDED_ROWS
    assert "ocr_text" not in view
    assert view["has_ocr_text"] is True
    assert view["ocr_text_chars"] == len("scan text")


def test_show_reports_no_conflicts_by_default(seeded):
    assert documents.get_document_view(
        seeded["conn"], seeded["doc"]
    )["conflicts_open"] == []


def test_show_reports_conflicts_at_both_ends(seeded):
    """`show` is the pre-flight for `reassign`/`rm`, so it must union the same two ends
    those two refuse on: conflicts *raised by* the document and conflicts *anchored to*
    a row it owns (staged by some other document)."""
    conn = seeded["conn"]
    other = _insert_document(conn, seeded["jane"].person_id, "ee55")
    summary = dedup.commit_extraction(conn, other, {"lab_result": [
        {"test_name": "HbA1c", "collected_at": "2026-01-02", "value_num": 7.4,
         "unit": "%"},
    ]})
    assert summary.counts["conflict"] == 1
    # Raised by `other`...
    assert documents.get_document_view(conn, other)["conflicts_open"] == [1]
    # ...and anchored to the row the seeded document owns.
    assert documents.get_document_view(conn, seeded["doc"])["conflicts_open"] == [1]


def test_show_view_does_not_change_the_list_view(seeded):
    """Regression guard for the `_document_view` / `_show_view` split: `list`'s shape is
    what issue #54 shipped and must stay byte-identical."""
    listed = documents.list_documents(seeded["conn"])[0]
    assert "conflicts_open" not in listed
    assert "ocr_text_chars" not in listed
    shown = documents.get_document_view(seeded["conn"], seeded["doc"])
    assert {k: v for k, v in shown.items() if k in listed} == listed


def test_get_document_text_returns_the_stored_text(seeded):
    assert documents.get_document_text(seeded["conn"], seeded["doc"]) == "scan text"


def test_get_document_text_is_empty_when_unset(conn):
    person = persons.add_person(conn, "jane-doe", "Jane Doe")
    doc = _insert_document(conn, person.person_id, "ff00", ocr_text=None)
    assert documents.get_document_text(conn, doc) == ""
    assert documents.get_document_view(conn, doc)["ocr_text_chars"] == 0


# --- set-text ---------------------------------------------------------------


def _fts_text(conn, document_id):
    row = conn.execute(
        "SELECT text FROM record_fts WHERE source_table = 'document' AND source_id = ?",
        (document_id,),
    ).fetchone()
    return row["text"] if row is not None else None


def test_set_text_fills_an_empty_document_and_reindexes_fts(conn):
    """No explicit FTS maintenance: migration 003's AFTER UPDATE trigger on `document`
    delete+reinserts the row from NEW.ocr_text, which is what makes `find` see it."""
    person = persons.add_person(conn, "jane-doe", "Jane Doe")
    doc = _insert_document(conn, person.person_id, "ff00", ocr_text=None)
    view = documents.set_document_text(conn, doc, "  cholesterol panel  ")
    assert view["has_ocr_text"] is True
    assert view["ocr_text_chars"] == len("cholesterol panel")   # stored stripped
    assert documents.get_document_text(conn, doc) == "cholesterol panel"
    assert _fts_text(conn, doc) == "cholesterol panel"


def test_set_text_refuses_a_populated_document_without_force(seeded):
    conn = seeded["conn"]
    with pytest.raises(documents.OcrTextPresentError) as exc:
        documents.set_document_text(conn, seeded["doc"], "replacement")
    assert "--force" in str(exc.value) and "nothing was written" in str(exc.value)
    assert documents.get_document_text(conn, seeded["doc"]) == "scan text"


def test_set_text_force_replaces_and_drops_the_old_fts_row(seeded):
    conn = seeded["conn"]
    documents.set_document_text(conn, seeded["doc"], "replacement text", force=True)
    assert documents.get_document_text(conn, seeded["doc"]) == "replacement text"
    # Exactly one FTS row for the document, carrying only the new text - proves the
    # trigger's delete+insert rather than an accumulating insert.
    rows = conn.execute(
        "SELECT text FROM record_fts WHERE source_table = 'document' AND source_id = ?",
        (seeded["doc"],),
    ).fetchall()
    assert [r["text"] for r in rows] == ["replacement text"]


def test_set_text_refuses_empty_text(seeded):
    conn = seeded["conn"]
    for empty in ("", "   \n\t "):
        with pytest.raises(ValueError):
            documents.set_document_text(conn, seeded["doc"], empty, force=True)
    assert documents.get_document_text(conn, seeded["doc"]) == "scan text"


def test_set_text_unknown_document_raises(conn):
    with pytest.raises(documents.DocumentNotFoundError):
        documents.set_document_text(conn, 999, "text")


# --- the emptiness predicate (issue #87) -------------------------------------
#
# `str.strip()` removes only characters where `.isspace()` is true, so a payload of
# nothing but zero-width characters used to pass every guard and store one invisible
# character - `has_ocr_text`/`ocr_text_populated` true on a document carrying no text.
# Engine-level because MCP's `document_set_text` has no `force` to recover with.

ZWSP = chr(0x200B)          # ZERO WIDTH SPACE
ZWNJ = chr(0x200C)          # ZERO WIDTH NON-JOINER
ZWJ = chr(0x200D)           # ZERO WIDTH JOINER
BOM_CHAR = chr(0xFEFF)      # what a *doubled* BOM leaves behind: `utf-8-sig` eats one


@pytest.mark.parametrize("text", ["", "   \n\t ", ZWSP, ZWNJ, ZWJ, BOM_CHAR,
                                  BOM_CHAR * 2, "\x00", ZWSP + " " + BOM_CHAR])
def test_normalize_document_text_treats_invisible_only_text_as_empty(text):
    assert documents.normalize_document_text(text) == ""


def test_normalize_document_text_handles_none():
    assert documents.normalize_document_text(None) == ""


def test_normalize_document_text_keeps_visible_text_and_its_interior():
    # Only the two ends are touched: a zero-width joiner inside a word is part of
    # the transcription, not padding (the contract is "no visible content is empty").
    assert documents.normalize_document_text(
        BOM_CHAR + "  sodium" + ZWJ + " 140  " + ZWSP
    ) == "sodium" + ZWJ + " 140"


def test_set_text_refuses_zero_width_only_text(seeded):
    conn = seeded["conn"]
    for empty in (ZWSP, BOM_CHAR * 2, ZWJ + ZWNJ, " " + ZWSP + " "):
        with pytest.raises(ValueError):
            documents.set_document_text(conn, seeded["doc"], empty, force=True)
    assert documents.get_document_text(conn, seeded["doc"]) == "scan text"


def test_set_text_strips_invisible_padding_from_what_it_stores(seeded):
    """Normalise what gets *stored*, not just what gets rejected: a leading
    zero-width character must never land in `ocr_text` either."""
    conn = seeded["conn"]
    documents.set_document_text(
        conn, seeded["doc"], ZWSP + " cholesterol panel " + BOM_CHAR, force=True
    )
    assert documents.get_document_text(conn, seeded["doc"]) == "cholesterol panel"
    view = documents.get_document_view(conn, seeded["doc"])
    assert view["ocr_text_chars"] == len("cholesterol panel")


def test_set_text_repairs_an_invisible_only_row_without_force(conn):
    """The recovery half of the fix: an `ocr_text` of nothing but zero-widths (every
    row ingested before this fix) counts as *unpopulated*, so it is repairable over
    MCP - where `document_set_text` deliberately has no `force` (#87)."""
    person = persons.add_person(conn, "jane-doe", "Jane Doe")
    doc = _insert_document(conn, person.person_id, "ff00", ocr_text=ZWSP)
    view = documents.set_document_text(conn, doc, "Real transcription of the page.")
    assert view["has_ocr_text"] is True
    assert documents.get_document_text(conn, doc) == "Real transcription of the page."
    assert _fts_text(conn, doc) == "Real transcription of the page."


def test_set_text_still_refuses_a_visibly_populated_row_without_force(conn):
    """The widened check must not widen into "replace anything": text with even one
    visible character is still guarded by `force` (#87)."""
    person = persons.add_person(conn, "jane-doe", "Jane Doe")
    doc = _insert_document(conn, person.person_id, "ff01", ocr_text=ZWSP + "a")
    with pytest.raises(documents.OcrTextPresentError):
        documents.set_document_text(conn, doc, "replacement")
    assert documents.get_document_text(conn, doc) == ZWSP + "a"


def test_set_text_leaves_records_and_keys_untouched(seeded):
    conn = seeded["conn"]
    before = [
        (r["lab_result_id"], r["dedup_key"], r["person_id"])
        for r in conn.execute("SELECT * FROM lab_result").fetchall()
    ]
    documents.set_document_text(conn, seeded["doc"], "brand new text", force=True)
    after = [
        (r["lab_result_id"], r["dedup_key"], r["person_id"])
        for r in conn.execute("SELECT * FROM lab_result").fetchall()
    ]
    assert before == after
    assert documents.get_document_view(conn, seeded["doc"])["record_count"] == _SEEDED_ROWS


# --- text_source provenance (issue #175) ------------------------------------


def _text_source(conn, document_id):
    return conn.execute(
        "SELECT text_source FROM document WHERE document_id = ?", (document_id,)
    ).fetchone()["text_source"]


def test_set_text_defaults_to_attached_provenance(conn):
    """The function's own contract is a hand-attach - `pemr document set-text` and the
    `document_set_text` MCP tool - so `attached` is what an unqualified call records."""
    person = persons.add_person(conn, "jane-doe", "Jane Doe")
    doc = _insert_document(conn, person.person_id, "ff00", ocr_text=None)
    view = documents.set_document_text(conn, doc, "cholesterol panel")
    assert view["text_source"] == documents.TEXT_SOURCE_ATTACHED == "attached"
    assert _text_source(conn, doc) == "attached"


def test_set_text_records_engine_provenance_when_asked(conn):
    """The one engine caller (`reocr_documents`) overrides the default explicitly - the
    whole point of the parameter, since both callers pass `force=True`."""
    person = persons.add_person(conn, "jane-doe", "Jane Doe")
    doc = _insert_document(conn, person.person_id, "ff00", ocr_text=None)
    view = documents.set_document_text(
        conn, doc, "extracted page", source=documents.TEXT_SOURCE_ENGINE
    )
    assert view["text_source"] == "engine"
    assert _text_source(conn, doc) == "engine"


def test_set_text_flips_provenance_on_a_forced_replace(conn):
    """Provenance describes the text that is *there now*, so a replace overwrites it."""
    person = persons.add_person(conn, "jane-doe", "Jane Doe")
    doc = _insert_document(conn, person.person_id, "ff00", ocr_text=None)
    documents.set_document_text(conn, doc, "typed by hand")
    documents.set_document_text(
        conn, doc, "re-extracted", force=True, source=documents.TEXT_SOURCE_ENGINE
    )
    assert documents.get_document_text(conn, doc) == "re-extracted"
    assert _text_source(conn, doc) == "engine"


def test_set_text_rejects_an_unknown_source_and_writes_nothing(seeded):
    """The closed vocabulary is enforced in Python rather than by a DDL `CHECK` (013's
    precedent), so the guard has to be here - and has to fire before any write."""
    conn = seeded["conn"]
    with pytest.raises(ValueError, match="unknown text source"):
        documents.set_document_text(
            conn, seeded["doc"], "replacement", force=True, source="guessed"
        )
    assert documents.get_document_text(conn, seeded["doc"]) == "scan text"
    assert _text_source(conn, seeded["doc"]) is None


def test_set_text_keeps_the_fts_row_single_despite_the_two_column_update(conn):
    """`text_source` rides in the *same* UPDATE as `ocr_text`, so migration 003's
    AFTER UPDATE trigger still fires exactly once - a second statement would double-fire
    it for no benefit."""
    person = persons.add_person(conn, "jane-doe", "Jane Doe")
    doc = _insert_document(conn, person.person_id, "ff00", ocr_text=None)
    documents.set_document_text(conn, doc, "cholesterol panel")
    documents.set_document_text(conn, doc, "lipid panel", force=True)
    rows = conn.execute(
        "SELECT text FROM record_fts WHERE source_table = 'document' AND source_id = ?",
        (doc,),
    ).fetchall()
    assert [r["text"] for r in rows] == ["lipid panel"]   # replaced, not accumulated


def test_views_report_no_provenance_for_a_pre_015_row(seeded):
    """A row written before 015 (here: the fixture's direct INSERT) reports `None` rather
    than guessing - `has_ocr_text` is what tells "unknown" apart from "no text"."""
    conn = seeded["conn"]
    listed = documents.list_documents(conn)[0]
    shown = documents.get_document_view(conn, seeded["doc"])
    assert listed["has_ocr_text"] is True and listed["text_source"] is None
    assert shown["text_source"] is None


def test_views_tolerate_a_database_with_no_text_source_column(tmp_path):
    """A snapshot restored from a pre-015 database still passes `db.is_migrated` (it only
    checks 001's sentinel), so `SELECT *` hands back a row with no `text_source` key. The
    read has to degrade to `None` rather than raise - the same reason
    `curation.has_table` exists."""
    import shutil

    staged = tmp_path / "pre015"
    staged.mkdir()
    for path in sorted(db.DEFAULT_MIGRATIONS_DIR.glob("*.sql")):
        if path.name < "015":
            shutil.copy(path, staged / path.name)
    old = db.connect(tmp_path / "old.db")
    try:
        db.migrate(old, staged)
        person = persons.add_person(old, "jane-doe", "Jane Doe")
        doc = _insert_document(old, person.person_id, "aa11bb22")
        assert "text_source" not in {
            r[1] for r in old.execute("PRAGMA table_info(document)").fetchall()
        }
        assert documents.list_documents(old)[0]["text_source"] is None
        assert documents.get_document_view(old, doc)["text_source"] is None
    finally:
        old.close()


# --- edit -------------------------------------------------------------------


def test_edit_partial_update(seeded):
    view = documents.edit_document(
        seeded["conn"], seeded["doc"], doc_date="2026-04-01"
    )
    assert view["doc_date"] == "2026-04-01"
    assert view["category"] == "labs"       # untouched
    assert view["provider"] == "Quest"      # untouched


def test_edit_clears_with_empty_string(seeded):
    view = documents.edit_document(seeded["conn"], seeded["doc"], provider="")
    assert view["provider"] is None


def test_edit_requires_a_field(seeded):
    with pytest.raises(ValueError):
        documents.edit_document(seeded["conn"], seeded["doc"])


def test_edit_rejects_unknown_field(seeded):
    with pytest.raises(ValueError):
        documents.edit_document(seeded["conn"], seeded["doc"], sha256="x")


def test_edit_unknown_document_raises(conn):
    with pytest.raises(documents.DocumentNotFoundError):
        documents.edit_document(conn, 999, category="labs")


def test_edit_does_not_move_dedup_keys(seeded):
    conn = seeded["conn"]
    before = conn.execute("SELECT dedup_key FROM lab_result").fetchone()["dedup_key"]
    documents.edit_document(conn, seeded["doc"], category="imaging")
    after = conn.execute("SELECT dedup_key FROM lab_result").fetchone()["dedup_key"]
    assert before == after


# --- reassign ---------------------------------------------------------------


def test_reassign_moves_document_and_every_record_type(seeded):
    conn, john = seeded["conn"], seeded["john"]
    report = documents.reassign_document(
        conn, seeded["doc"], "john-doe", apply=True
    )
    assert report.applied is True
    assert sorted(report.counts) == sorted(dedup.KNOWN_TYPES)
    assert len(report.changes) == len(dedup.KNOWN_TYPES)  # one row seeded per type

    assert conn.execute(
        "SELECT person_id FROM document WHERE document_id = ?", (seeded["doc"],)
    ).fetchone()["person_id"] == john.person_id
    for record_type in dedup.KNOWN_TYPES:
        owners = [
            r["person_id"] for r in conn.execute(
                f"SELECT person_id FROM {record_type}"
            ).fetchall()
        ]
        assert owners == [john.person_id], record_type
        # FTS follows via migration 003's AFTER UPDATE triggers - `find --person`
        # must not keep pointing at the old owner.
        assert _fts_person_ids(conn, record_type) == [john.person_id], record_type
    assert _fts_person_ids(conn, "document") == [john.person_id]


def test_reassign_rederives_dedup_keys(seeded):
    conn = seeded["conn"]
    before = conn.execute("SELECT dedup_key FROM lab_result").fetchone()["dedup_key"]
    report = documents.reassign_document(
        conn, seeded["doc"], "john-doe", apply=True
    )
    after = conn.execute("SELECT dedup_key FROM lab_result").fetchone()["dedup_key"]
    assert after != before
    change = next(c for c in report.changes if c.record_type == "lab_result")
    assert (change.old_key, change.new_key) == (before, after)


def test_reassign_dry_run_writes_nothing(seeded):
    conn, jane = seeded["conn"], seeded["jane"]
    report = documents.reassign_document(conn, seeded["doc"], "john-doe")
    assert report.applied is False and len(report.changes) == _SEEDED_ROWS
    assert conn.execute(
        "SELECT person_id FROM document WHERE document_id = ?", (seeded["doc"],)
    ).fetchone()["person_id"] == jane.person_id
    assert [r["person_id"] for r in conn.execute(
        "SELECT person_id FROM lab_result"
    ).fetchall()] == [jane.person_id]


def test_reassign_to_current_owner_is_a_noop(seeded):
    report = documents.reassign_document(
        seeded["conn"], seeded["doc"], "jane-doe", apply=True
    )
    assert report.unchanged is True and report.changes == []


def test_reassign_to_deactivated_person_is_allowed_with_a_note(seeded):
    conn = seeded["conn"]
    persons.deactivate_person(conn, "john-doe")
    report = documents.reassign_document(conn, seeded["doc"], "john-doe", apply=True)
    assert report.target_inactive is True and report.applied is True


def test_reassign_collision_refused_and_writes_nothing(seeded):
    conn, jane, john = seeded["conn"], seeded["jane"], seeded["john"]
    # John already has the same lab fact, from his own document.
    john_doc = _insert_document(conn, john.person_id, "dd44")
    dedup.commit_extraction(conn, john_doc, {"lab_result": RECORDS["lab_result"]})

    with pytest.raises(documents.ReassignCollisionError) as exc:
        documents.reassign_document(conn, seeded["doc"], "john-doe", apply=True)
    assert "nothing was written" in str(exc.value)
    assert conn.execute(
        "SELECT person_id FROM document WHERE document_id = ?", (seeded["doc"],)
    ).fetchone()["person_id"] == jane.person_id
    assert [r["person_id"] for r in conn.execute(
        "SELECT person_id FROM medication"
    ).fetchall()] == [jane.person_id]


def test_reassign_refused_while_a_conflict_is_open(seeded):
    conn = seeded["conn"]
    other = _insert_document(conn, seeded["jane"].person_id, "ee55")
    summary = dedup.commit_extraction(conn, other, {"lab_result": [
        {"test_name": "HbA1c", "collected_at": "2026-01-02", "value_num": 6.2,
         "unit": "%"},
    ]})
    assert summary.counts["conflict"] == 1

    with pytest.raises(documents.OpenConflictsError) as exc:
        documents.reassign_document(conn, other, "john-doe", apply=True)
    assert "review-conflicts" in str(exc.value)


def test_reassign_refused_when_another_document_conflicts_with_its_rows(seeded):
    """A conflict has two ends. ``conflict.document_id`` names the document whose
    *incoming* row lost; the ``dedup_key`` anchors it to the *stored* row, which a
    different document owns. Reassigning that owner re-derives the key out from under
    the conflict, and a later `keep incoming` then writes nothing, silently (#54 audit).
    """
    conn = seeded["conn"]
    other = _insert_document(conn, seeded["jane"].person_id, "ee55")
    summary = dedup.commit_extraction(conn, other, {"lab_result": [
        {"test_name": "HbA1c", "collected_at": "2026-01-02", "value_num": 7.4,
         "unit": "%"},
    ]})
    assert summary.counts["conflict"] == 1
    # The conflict cites `other`, NOT the document being moved - that is the point.
    assert conn.execute(
        "SELECT document_id FROM conflict WHERE conflict_id = 1"
    ).fetchone()["document_id"] == other

    with pytest.raises(documents.OpenConflictsError) as exc:
        documents.reassign_document(conn, seeded["doc"], "john-doe", apply=True)
    assert "#1" in str(exc.value) and "review-conflicts" in str(exc.value)
    assert conn.execute(
        "SELECT person_id FROM document WHERE document_id = ?", (seeded["doc"],)
    ).fetchone()["person_id"] == seeded["jane"].person_id
    assert [r["person_id"] for r in conn.execute(
        "SELECT person_id FROM lab_result"
    ).fetchall()] == [seeded["jane"].person_id]


def test_reassign_refusal_lists_a_two_ended_conflict_once(seeded):
    """A conflict raised by *and* anchored to the same document (a re-commit against the
    same document collides with the row it already produced) appears once in the
    refusal, not twice."""
    conn = seeded["conn"]
    other = _insert_document(conn, seeded["jane"].person_id, "dd99")
    dedup.commit_extraction(conn, other, {"lab_result": [
        {"test_name": "Glucose", "collected_at": "2026-01-02", "value_num": 88},
    ]})
    summary = dedup.commit_extraction(conn, other, {"lab_result": [
        {"test_name": "Glucose", "collected_at": "2026-01-02", "value_num": 99},
    ]})
    assert summary.counts["conflict"] == 1

    with pytest.raises(documents.OpenConflictsError) as exc:
        documents.reassign_document(conn, other, "john-doe", apply=True)
    assert str(exc.value).count("#1") == 1


def test_reassign_moves_a_keep_both_family_intact(seeded):
    """A row admitted by `review-conflicts --keep both` is occurrence >0 of its family:
    its key hashes the base together with that stored occurrence, so a move has to carry
    the occurrence (else the recompute reads as dictionary drift) and re-derive both
    dedup_key and dedup_base under the new owner."""
    conn = seeded["conn"]
    doc = _insert_document(conn, seeded["john"].person_id, "ff77ee88")
    draw = {"test_name": "glucose", "collected_at": "2024-04-01"}
    dedup.commit_extraction(conn, doc, {"lab_result": [draw | {"value_num": 95}]})
    dedup.commit_extraction(conn, doc, {"lab_result": [draw | {"value_num": 148}]})
    conflict_id = dedup.list_conflicts(conn)[0]["conflict_id"]
    dedup.resolve_conflict(conn, conflict_id, keep="both")

    report = documents.reassign_document(conn, doc, "jane-doe", apply=True)
    assert len(report.changes) == 2

    rows = conn.execute(
        "SELECT * FROM lab_result WHERE test_name = 'glucose' ORDER BY lab_result_id"
    ).fetchall()
    assert [r["person_id"] for r in rows] == [seeded["jane"].person_id] * 2
    assert [r["dedup_occurrence"] for r in rows] == [0, 1]
    assert rows[0]["dedup_base"] == rows[1]["dedup_base"]      # family stayed together
    assert rows[0]["dedup_key"] == rows[0]["dedup_base"]       # occurrence 0
    assert rows[1]["dedup_key"] == dedup.occurrence_key(rows[1]["dedup_base"], 1)
    # Keys are Jane's now, and the family still dedups a re-commit of the admitted draw.
    summary = dedup.commit_extraction(
        conn, seeded["doc"], {"lab_result": [draw | {"value_num": 148}]}
    )
    assert summary.counts == {"new": 0, "duplicate": 1, "enriched": 0, "conflict": 0, "promoted": 0}


def test_reassign_refused_on_dictionary_drift(seeded):
    conn = seeded["conn"]
    # Keys were committed with no dictionary; this one renames the stored analyte,
    # so a naive recompute would silently rekey as a side effect of the move.
    with pytest.raises(documents.DictionaryDriftError) as exc:
        documents.reassign_document(
            conn, seeded["doc"], "john-doe", {"hba1c": "glycated_hemoglobin"},
            apply=True,
        )
    assert "rekey" in str(exc.value)


def test_reassign_across_a_partially_rekeyed_database(conn):
    """A *partially* rekeyed database is a state that could not exist before issue #92
    (`rekey` was all-or-nothing), so `reassign` — which recomputes keys through the same
    function — gets exercised against it: the documents whose rows sit in a rekeyed
    table move, and only the ones owning the skipped table's stale keys are refused."""
    persons.add_person(conn, "jane-doe", "Jane Doe")
    john = persons.add_person(conn, "john-doe", "John Doe")
    fused = _insert_document(conn, 1, "ee55")
    clean = _insert_document(conn, 1, "ff66")
    # Two allergies the new dictionary merges (distinct reactions = two real facts)...
    dedup.commit_extraction(conn, fused, {"allergy": [
        {"substance": "PCN", "reaction": "rash"},
        {"substance": "Penicillin", "reaction": "anaphylaxis"},
    ]})
    # ...and, on a second document, a condition whose key merely moves.
    dedup.commit_extraction(conn, clean, {
        "condition": [{"name": "T2DM", "status": "active"}]})

    d_new = {"pcn": "penicillin", "t2dm": "type 2 diabetes"}
    report = dedup.rekey(conn, d_new, apply=True)
    assert report.blocked == ["allergy"]        # the partial state, as set up

    moved = documents.reassign_document(conn, clean, "john-doe", d_new, apply=True)
    assert moved.applied is True
    assert conn.execute(
        "SELECT person_id FROM condition"
    ).fetchone()["person_id"] == john.person_id

    stored = [r["dedup_key"] for r in conn.execute("SELECT dedup_key FROM allergy")]
    with pytest.raises(documents.DictionaryDriftError) as exc:
        documents.reassign_document(conn, fused, "john-doe", d_new, apply=True)
    assert "rekey" in str(exc.value)
    # Refused before any write: the skipped table keeps its keys and its owner.
    assert [r["dedup_key"] for r in
            conn.execute("SELECT dedup_key FROM allergy")] == stored
    assert conn.execute(
        "SELECT person_id FROM document WHERE document_id = ?", (fused,)
    ).fetchone()["person_id"] == 1


def test_reassign_unknown_document_and_slug(seeded):
    conn = seeded["conn"]
    with pytest.raises(documents.DocumentNotFoundError):
        documents.reassign_document(conn, 999, "john-doe")
    with pytest.raises(persons.PersonNotFoundError):
        documents.reassign_document(conn, seeded["doc"], "nobody")


# --- rm ---------------------------------------------------------------------


def test_rm_dry_run_reports_but_deletes_nothing(seeded):
    conn = seeded["conn"]
    report = documents.remove_document(conn, seeded["doc"])
    assert report.applied is False and report.record_count == _SEEDED_ROWS
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 1


def test_rm_apply_cascades_records_and_fts(seeded):
    conn = seeded["conn"]
    report = documents.remove_document(conn, seeded["doc"], apply=True)
    assert report.applied is True and report.record_count == _SEEDED_ROWS
    for record_type in dedup.KNOWN_TYPES:
        assert conn.execute(
            f"SELECT COUNT(*) AS n FROM {record_type}"
        ).fetchone()["n"] == 0, record_type
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM record_fts").fetchone()["n"] == 0


def test_rm_deletes_open_conflicts_and_detaches_resolved_ones(seeded):
    conn = seeded["conn"]
    other = _insert_document(conn, seeded["jane"].person_id, "ff66")
    dedup.commit_extraction(conn, other, {"lab_result": [
        {"test_name": "HbA1c", "collected_at": "2026-01-02", "value_num": 6.2},
    ]})
    third = _insert_document(conn, seeded["jane"].person_id, "aa77")
    dedup.commit_extraction(conn, third, {"lab_result": [
        {"test_name": "HbA1c", "collected_at": "2026-01-02", "value_num": 7.1},
    ]})
    # Resolve the first conflict (keep-existing leaves the stored row's provenance
    # alone), leaving one open and one resolved conflict citing two documents.
    dedup.resolve_conflict(conn, 1, keep="existing")

    resolved = documents.remove_document(conn, other, apply=True)
    assert (resolved.conflicts_deleted, resolved.conflicts_detached) == (0, 1)
    row = conn.execute(
        "SELECT document_id, status FROM conflict WHERE conflict_id = 1"
    ).fetchone()
    assert row["document_id"] is None and row["status"] == "resolved"

    still_open = documents.remove_document(conn, third, apply=True)
    assert (still_open.conflicts_deleted, still_open.conflicts_detached) == (1, 0)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM conflict WHERE status = 'open'"
    ).fetchone()["n"] == 0


def test_rm_reports_and_deletes_conflicts_staged_against_its_rows(seeded):
    """The blast radius is `rm`'s only safety mechanism (dry run by default, no
    --yes), so it has to count the conflicts anchored to the rows being destroyed -
    not just the ones this document raised (#54 audit)."""
    conn = seeded["conn"]
    other = _insert_document(conn, seeded["jane"].person_id, "ee55")
    dedup.commit_extraction(conn, other, {"lab_result": [
        {"test_name": "HbA1c", "collected_at": "2026-01-02", "value_num": 7.4},
    ]})

    dry = documents.remove_document(conn, seeded["doc"])
    assert (dry.conflicts_deleted, dry.conflicts_anchored) == (0, 1)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM conflict WHERE status = 'open'"
    ).fetchone()["n"] == 1          # dry run wrote nothing

    applied = documents.remove_document(conn, seeded["doc"], apply=True)
    assert (applied.conflicts_deleted, applied.conflicts_anchored) == (0, 1)
    assert conn.execute("SELECT COUNT(*) AS n FROM conflict").fetchone()["n"] == 0
    # ...so the orphaned conflict can no longer resolve to a silent no-op.
    with pytest.raises(ValueError):
        dedup.resolve_conflict(conn, 1, keep="incoming")


def test_rm_counts_a_two_ended_conflict_once(seeded):
    """Raised by and anchored to the same document: counted on the `document_id` side
    only, and deleted exactly once."""
    conn = seeded["conn"]
    other = _insert_document(conn, seeded["jane"].person_id, "dd99")
    dedup.commit_extraction(conn, other, {"lab_result": [
        {"test_name": "Glucose", "collected_at": "2026-01-02", "value_num": 88},
    ]})
    summary = dedup.commit_extraction(conn, other, {"lab_result": [
        {"test_name": "Glucose", "collected_at": "2026-01-02", "value_num": 99},
    ]})
    assert summary.counts["conflict"] == 1

    report = documents.remove_document(conn, other, apply=True)
    assert (report.conflicts_deleted, report.conflicts_anchored) == (1, 0)
    assert conn.execute("SELECT COUNT(*) AS n FROM conflict").fetchone()["n"] == 0


def test_rm_keeps_the_blob_by_default(seeded, tmp_path):
    conn = seeded["conn"]
    sources = tmp_path / "sources"
    doc = conn.execute(
        "SELECT source_path FROM document WHERE document_id = ?", (seeded["doc"],)
    ).fetchone()
    blob = sources / doc["source_path"]
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"scan")

    report = documents.remove_document(
        conn, seeded["doc"], sources_dir=sources, apply=True
    )
    assert report.blob_purged is False
    assert blob.exists()
    assert report.blob_path == str(blob)


def test_rm_purge_blob_unlinks_the_scan(seeded, tmp_path):
    conn = seeded["conn"]
    sources = tmp_path / "sources"
    doc = conn.execute(
        "SELECT source_path FROM document WHERE document_id = ?", (seeded["doc"],)
    ).fetchone()
    blob = sources / doc["source_path"]
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"scan")

    report = documents.remove_document(
        conn, seeded["doc"], sources_dir=sources, purge_blob=True, apply=True
    )
    assert report.blob_purged is True
    assert not blob.exists()
    assert not blob.parent.exists()      # empty shard dir cleaned up


def test_rm_purge_blob_does_not_claim_a_delete_that_never_happened(seeded, tmp_path):
    """`unlink(missing_ok=True)` on an already-missing blob is fine, but reporting it
    as deleted is not (#54 audit, non-blocking item 3)."""
    conn = seeded["conn"]
    report = documents.remove_document(
        conn, seeded["doc"], sources_dir=tmp_path / "sources",
        purge_blob=True, apply=True,
    )
    assert report.applied is True and report.blob_purged is False


def test_rm_purge_blob_without_a_sources_dir_is_refused(seeded):
    with pytest.raises(ValueError):
        documents.remove_document(seeded["conn"], seeded["doc"], purge_blob=True)


def test_rm_unknown_document_raises(conn):
    with pytest.raises(documents.DocumentNotFoundError):
        documents.remove_document(conn, 999)


# --- CLI surface ------------------------------------------------------------


def _run(tmp_path, *argv):
    return cli.main(["--db", str(tmp_path / "cli.db"), *argv])


@pytest.fixture()
def cli_ready(tmp_path):
    """Migrated DB, two people, one ingested+committed document owned by jane."""
    assert _run(tmp_path, "migrate", "--create") == 0
    assert _run(tmp_path, "person", "add", "--slug", "jane-doe", "--name", "Jane") == 0
    assert _run(tmp_path, "person", "add", "--slug", "john-doe", "--name", "John") == 0
    scan = tmp_path / "scan.txt"
    scan.write_bytes(b"hba1c 5.7 percent")
    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources"), "--ocr-text-file",
                str(scan)) == 0
    payload = tmp_path / "extract.json"
    payload.write_text(json.dumps(RECORDS), encoding="utf-8")
    assert _run(tmp_path, "commit-extraction", "--document", "1",
                "--json", str(payload)) == 0
    return tmp_path


def test_cli_document_list_empty(tmp_path, capsys):
    assert _run(tmp_path, "migrate", "--create") == 0
    capsys.readouterr()
    assert _run(tmp_path, "document", "list") == 0
    assert "no documents yet" in capsys.readouterr().out


def test_cli_document_list_and_json(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "list") == 0
    out = capsys.readouterr().out
    assert "#1" in out and "jane-doe" in out and f"{_SEEDED_ROWS} rec" in out

    assert _run(cli_ready, "document", "list", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["record_count"] == _SEEDED_ROWS
    assert payload[0]["has_ocr_text"] is True
    assert "ocr_text" not in payload[0]


def test_cli_document_list_unknown_person_fails(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "list", "--person", "nobody") == 1
    assert "no person with slug" in capsys.readouterr().err


def test_cli_document_show(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "show", "1") == 0
    out = capsys.readouterr().out
    assert "document_id    1" in out
    assert "person         jane-doe" in out
    # Total plus the non-zero per-type breakdown only (zeros are a --json concern).
    assert f"records        {_SEEDED_ROWS}  (" in out and "lab_result 1" in out
    assert "procedure 0" not in out
    assert "has_ocr_text   yes (" in out
    assert "conflicts      none" in out


def test_cli_document_show_json_keeps_the_stable_shape(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "show", "1", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["records"]) == set(dedup.KNOWN_TYPES)
    assert payload["conflicts_open"] == []
    assert payload["has_ocr_text"] is True
    assert payload["ocr_text_chars"] > 0
    assert "ocr_text" not in payload


def test_cli_document_show_names_the_text_provenance(cli_ready, capsys):
    """The fixture ingests with `--ocr-text-file`, i.e. the caller's own transcription -
    so `attached`, even though `ingest` is the engine's own command (issue #175)."""
    capsys.readouterr()
    assert _run(cli_ready, "document", "show", "1") == 0
    assert "text_source    attached" in capsys.readouterr().out

    assert _run(cli_ready, "document", "show", "1", "--json") == 0
    assert json.loads(capsys.readouterr().out)["text_source"] == "attached"


def test_cli_document_show_renders_unknown_provenance_rather_than_a_blank(
    cli_ready, capsys
):
    """A document ingested with no text at all has no provenance to report. `_fmt` would
    print an empty string there, which reads as a rendering bug rather than as "unknown"."""
    blank = cli_ready / "untranscribed.txt"
    blank.write_bytes(b"nothing extracted from this one")
    assert _run(cli_ready, "ingest", str(blank), "--person", "jane-doe",
                "--sources", str(cli_ready / "sources")) == 0
    capsys.readouterr()
    assert _run(cli_ready, "document", "show", "2") == 0
    out = capsys.readouterr().out
    assert "has_ocr_text   no" in out and "text_source    unknown" in out

    assert _run(cli_ready, "document", "show", "2", "--json") == 0
    assert json.loads(capsys.readouterr().out)["text_source"] is None


def test_cli_provenance_lifecycle_ingest_set_text_reocr(cli_ready, capsys):
    """The whole provenance chain through the CLI, one document, in order (issue #175).

    The per-step assertions exist elsewhere in this file at the API level; what this pins
    is the *sequence* a real operator walks - extraction at ingest, a hand-attach over it,
    then a re-OCR that takes it back - together with the FTS resync that rides on the
    same two-column UPDATE. `record_fts` is asserted at each step because the trigger
    firing once (not zero or twice) is what keeps `find` honest after a replace.
    """
    scan = cli_ready / "engine-scan.txt"
    scan.write_bytes(b"jane engine token enginetoken alpha")
    assert _run(cli_ready, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(cli_ready / "sources"), "--ocr", "auto") == 0

    conn = db.connect(cli_ready / "cli.db")
    try:
        def fts_rows():
            return conn.execute(
                "SELECT COUNT(*) AS n FROM record_fts "
                "WHERE source_table = 'document' AND document_id = 2"
            ).fetchone()["n"]

        # 1. pemr extracted it -> engine.
        assert _text_source(conn, 2) == "engine"
        assert fts_rows() == 1

        # 2. a human replaces it -> attached, and `find` follows the new text.
        hand = cli_ready / "hand.txt"
        hand.write_text("jane corrected by hand handtoken bravo", encoding="utf-8")
        assert _run(cli_ready, "document", "set-text", "2", "--force",
                    "--ocr-text-file", str(hand)) == 0
        assert _text_source(conn, 2) == "attached"
        assert fts_rows() == 1
        capsys.readouterr()
        assert _run(cli_ready, "find", "handtoken") == 0
        assert "document#2" in capsys.readouterr().out
        assert _run(cli_ready, "find", "enginetoken") == 0
        assert "no matches" in capsys.readouterr().out

        # 3. re-OCR re-derives from the stored blob -> back to engine. Same
        # `set_document_text` call as step 2, distinguished only by `source`.
        assert _run(cli_ready, "document", "reocr", "2", "--force", "--allow-shrink",
                    "--sources", str(cli_ready / "sources")) == 0
        assert _text_source(conn, 2) == "engine"
        assert fts_rows() == 1
        capsys.readouterr()
        assert _run(cli_ready, "find", "enginetoken") == 0
        assert "document#2" in capsys.readouterr().out
    finally:
        conn.close()


def test_cli_document_show_reports_open_conflicts(cli_ready, capsys):
    scan2 = cli_ready / "scan2.txt"
    scan2.write_bytes(b"hba1c 7.4 percent")
    assert _run(cli_ready, "ingest", str(scan2), "--person", "jane-doe",
                "--sources", str(cli_ready / "sources"),
                "--ocr-text-file", str(scan2)) == 0
    payload = cli_ready / "extract2.json"
    payload.write_text(json.dumps({"lab_result": [
        {"test_name": "HbA1c", "collected_at": "2026-01-02", "value_num": 7.4,
         "unit": "%"},
    ]}), encoding="utf-8")
    assert _run(cli_ready, "commit-extraction", "--document", "2",
                "--json", str(payload)) == 0

    capsys.readouterr()
    assert _run(cli_ready, "document", "show", "1") == 0
    out = capsys.readouterr().out
    assert "conflicts      #1 open - resolve with `pemr review-conflicts`" in out
    assert out.isascii(), repr(out)


def test_cli_document_show_text_dumps_the_transcription(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "show", "1", "--text") == 0
    captured = capsys.readouterr()
    assert captured.out == "hba1c 5.7 percent\n"
    assert captured.err == ""


def test_cli_document_show_text_on_an_empty_document_notes_and_succeeds(
    cli_ready, capsys
):
    scan2 = cli_ready / "scan2.txt"
    scan2.write_bytes(b"no text supplied")
    assert _run(cli_ready, "ingest", str(scan2), "--person", "jane-doe",
                "--sources", str(cli_ready / "sources")) == 0
    capsys.readouterr()
    assert _run(cli_ready, "document", "show", "2", "--text") == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "has no ocr_text stored" in captured.err


def test_cli_document_show_json_and_text_are_mutually_exclusive(cli_ready):
    with pytest.raises(SystemExit) as exc:
        _run(cli_ready, "document", "show", "1", "--json", "--text")
    assert exc.value.code == 2


def test_cli_document_show_unknown_id_fails(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "show", "999") == 1
    assert "no document with id" in capsys.readouterr().err


def test_cli_document_set_text_fills_an_empty_document_and_find_sees_it(
    cli_ready, capsys
):
    scan2 = cli_ready / "scan2.txt"
    scan2.write_bytes(b"no text supplied at ingest")
    assert _run(cli_ready, "ingest", str(scan2), "--person", "jane-doe",
                "--sources", str(cli_ready / "sources")) == 0
    capsys.readouterr()
    assert _run(cli_ready, "find", "--person", "jane-doe", "pericarditis") == 0
    assert "pericarditis" not in capsys.readouterr().out

    text = cli_ready / "transcript.txt"
    text.write_text("acute pericarditis noted on review", encoding="utf-8")
    assert _run(cli_ready, "document", "set-text", "2",
                "--ocr-text-file", str(text)) == 0
    out = capsys.readouterr().out
    assert "set ocr_text on document #2: 34 chars (was empty)" in out
    assert "has_ocr_text   yes (34 chars)" in out

    # No explicit reindex anywhere: the FTS trigger did it.
    assert _run(cli_ready, "find", "--person", "jane-doe", "pericarditis") == 0
    assert "pericarditis" in capsys.readouterr().out.lower()


def test_cli_document_set_text_refuses_a_populated_document(cli_ready, capsys):
    text = cli_ready / "transcript.txt"
    text.write_text("replacement transcription", encoding="utf-8")
    capsys.readouterr()
    assert _run(cli_ready, "document", "set-text", "1",
                "--ocr-text-file", str(text)) == 1
    err = capsys.readouterr().err
    assert "already has ocr_text" in err and "--force" in err
    assert _run(cli_ready, "document", "show", "1", "--text") == 0
    assert capsys.readouterr().out == "hba1c 5.7 percent\n"


def test_cli_document_set_text_force_replaces_and_moves_the_index(cli_ready, capsys):
    text = cli_ready / "transcript.txt"
    text.write_text("replacement transcription", encoding="utf-8")
    capsys.readouterr()
    assert _run(cli_ready, "document", "set-text", "1", "--force",
                "--ocr-text-file", str(text)) == 0
    out = capsys.readouterr().out
    assert "set ocr_text on document #1: 25 chars (replaced 17 chars)" in out

    assert _run(cli_ready, "find", "--person", "jane-doe", "replacement") == 0
    assert "replacement" in capsys.readouterr().out.lower()
    # The old text is gone from the index, not merely shadowed by the new row.
    assert _run(cli_ready, "find", "--person", "jane-doe", "percent") == 0
    assert "document" not in capsys.readouterr().out.lower()


def test_cli_document_set_text_json(cli_ready, capsys):
    text = cli_ready / "transcript.txt"
    text.write_text("replacement transcription", encoding="utf-8")
    capsys.readouterr()
    assert _run(cli_ready, "document", "set-text", "1", "--force",
                "--ocr-text-file", str(text), "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ocr_text_chars"] == 25
    assert payload["has_ocr_text"] is True
    assert "ocr_text" not in payload


def test_cli_document_set_text_rejects_an_empty_file(cli_ready, capsys):
    text = cli_ready / "empty.txt"
    text.write_text("   \n", encoding="utf-8")
    capsys.readouterr()
    assert _run(cli_ready, "document", "set-text", "1", "--force",
                "--ocr-text-file", str(text)) == 1
    assert "is empty - nothing to store" in capsys.readouterr().err
    assert _run(cli_ready, "document", "show", "1", "--text") == 0
    assert capsys.readouterr().out == "hba1c 5.7 percent\n"


def test_cli_document_set_text_rejects_a_bom_only_file(cli_ready, capsys):
    """A BOM-only "empty" file (Notepad/PowerShell 5.1) must not defeat the guard.

    U+FEFF survives `str.strip()`, so a `utf-8` read would store one invisible
    character and report `has_ocr_text: true` on a document with no text (#78).
    """
    text = cli_ready / "bomonly.txt"
    text.write_bytes(b"\xef\xbb\xbf")
    capsys.readouterr()
    assert _run(cli_ready, "document", "set-text", "1", "--force",
                "--ocr-text-file", str(text)) == 1
    assert "is empty - nothing to store" in capsys.readouterr().err
    assert _run(cli_ready, "document", "show", "1", "--text") == 0
    assert capsys.readouterr().out == "hba1c 5.7 percent\n"


def test_cli_document_set_text_rejects_a_zero_width_only_file(cli_ready, capsys):
    """One U+200B is three bytes of nothing, not a transcription (#87).

    Untouched by any encoding choice - `utf-8-sig` has nothing to strip - so this is
    the guard's job, not the read path's (#78 fixed that half).
    """
    text = cli_ready / "zwsp.txt"
    text.write_bytes(chr(0x200B).encode("utf-8"))
    capsys.readouterr()
    assert _run(cli_ready, "document", "set-text", "1", "--force",
                "--ocr-text-file", str(text)) == 1
    assert "is empty - nothing to store" in capsys.readouterr().err
    assert _run(cli_ready, "document", "show", "1", "--text") == 0
    assert capsys.readouterr().out == "hba1c 5.7 percent\n"


def test_cli_document_set_text_rejects_a_doubled_bom_file(cli_ready, capsys):
    """`utf-8-sig` strips exactly one leading BOM; the survivor is U+FEFF, which
    `str.strip()` then keeps (#87)."""
    text = cli_ready / "doublebom.txt"
    text.write_bytes((chr(0xFEFF) * 2).encode("utf-8"))
    capsys.readouterr()
    assert _run(cli_ready, "document", "set-text", "1", "--force",
                "--ocr-text-file", str(text)) == 1
    assert "is empty - nothing to store" in capsys.readouterr().err
    assert _run(cli_ready, "document", "show", "1", "--text") == 0
    assert capsys.readouterr().out == "hba1c 5.7 percent\n"


def test_cli_document_set_text_strips_a_leading_bom(cli_ready, capsys):
    """A BOM on a real transcription must not inflate the stored text (#78)."""
    text = cli_ready / "bommed.txt"
    text.write_bytes(b"\xef\xbb\xbfreplacement transcription")
    capsys.readouterr()
    assert _run(cli_ready, "document", "set-text", "1", "--force",
                "--ocr-text-file", str(text)) == 0
    assert "set ocr_text on document #1: 25 chars" in capsys.readouterr().out
    assert _run(cli_ready, "document", "show", "1", "--text") == 0
    assert capsys.readouterr().out == "replacement transcription\n"


def test_cli_document_set_text_non_utf8_file_is_a_friendly_error(cli_ready, capsys):
    """`UnicodeDecodeError` is a `ValueError`, not an `OSError` - handing in a PDF
    or a UTF-16 file used to escape the `except` and traceback out of `main` (#78)."""
    text = cli_ready / "scan.pdf"
    text.write_bytes(b"%PDF-1.4\xff\xfe\x00binary junk")
    capsys.readouterr()
    assert _run(cli_ready, "document", "set-text", "1", "--force",
                "--ocr-text-file", str(text)) == 1
    err = capsys.readouterr().err
    assert "error: cannot read" in err and "scan.pdf" in err
    # Nothing was written: the read fails before the DB is even opened.
    assert _run(cli_ready, "document", "show", "1", "--text") == 0
    assert capsys.readouterr().out == "hba1c 5.7 percent\n"


def test_cli_document_set_text_unreadable_path_and_unknown_id_fail(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "set-text", "1",
                "--ocr-text-file", str(cli_ready / "nope.txt")) == 1
    assert "cannot read" in capsys.readouterr().err

    text = cli_ready / "transcript.txt"
    text.write_text("some text", encoding="utf-8")
    assert _run(cli_ready, "document", "set-text", "999",
                "--ocr-text-file", str(text)) == 1
    assert "no document with id" in capsys.readouterr().err


def test_cli_document_set_text_leaves_records_untouched(cli_ready, capsys):
    text = cli_ready / "transcript.txt"
    text.write_text("replacement transcription", encoding="utf-8")
    capsys.readouterr()
    assert _run(cli_ready, "document", "set-text", "1", "--force",
                "--ocr-text-file", str(text)) == 0
    capsys.readouterr()
    assert _run(cli_ready, "document", "show", "1", "--json") == 0
    assert json.loads(capsys.readouterr().out)["record_count"] == _SEEDED_ROWS
    assert _run(cli_ready, "query", "labs", "--person", "jane-doe") == 0
    assert "HbA1c" in capsys.readouterr().out


def test_cli_document_edit(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "edit", "1", "--category", "imaging") == 0
    assert "imaging" in capsys.readouterr().out
    assert _run(cli_ready, "document", "edit", "1") == 1
    assert "nothing to update" in capsys.readouterr().err
    assert _run(cli_ready, "document", "edit", "999", "--category", "labs") == 1
    assert "no document with id" in capsys.readouterr().err


def test_cli_document_reassign_dry_run_then_apply(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "reassign", "1", "--person", "john-doe") == 0
    out = capsys.readouterr().out
    assert "jane-doe -> john-doe" in out
    assert f"dry run: {_SEEDED_ROWS} record(s) would move" in out and "--apply" in out

    assert _run(cli_ready, "document", "reassign", "1", "--person", "john-doe",
                "--apply") == 0
    assert f"reassigned {_SEEDED_ROWS} record(s)" in capsys.readouterr().out

    # The move is visible to the person-scoped reads.
    assert _run(cli_ready, "query", "labs", "--person", "john-doe") == 0
    assert "HbA1c" in capsys.readouterr().out
    assert _run(cli_ready, "query", "labs", "--person", "jane-doe") == 0
    assert "no lab results" in capsys.readouterr().out

    assert _run(cli_ready, "document", "reassign", "1", "--person", "john-doe") == 0
    assert "already owned by" in capsys.readouterr().out


def test_cli_document_reassign_json(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "reassign", "1", "--person", "john-doe",
                "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False and payload["to"] == "john-doe"
    assert len(payload["moved"]) == _SEEDED_ROWS


def test_cli_document_reassign_unknown_person_fails(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "reassign", "1", "--person", "nobody") == 1
    assert "no person with slug" in capsys.readouterr().err


def test_cli_document_rm_dry_run_then_apply(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "rm", "1") == 0
    out = capsys.readouterr().out
    assert f"records: {_SEEDED_ROWS} total" in out and "blob kept" in out
    assert "dry run: nothing was deleted" in out

    assert _run(cli_ready, "document", "rm", "1", "--apply") == 0
    assert "removed document #1" in capsys.readouterr().out
    assert _run(cli_ready, "document", "list") == 0
    assert "no documents yet" in capsys.readouterr().out


def test_cli_document_rm_purge_blob(cli_ready, capsys):
    sources = cli_ready / "sources"
    blobs = [p for p in sources.rglob("*") if p.is_file()]
    assert len(blobs) == 1
    capsys.readouterr()
    assert _run(cli_ready, "document", "rm", "1", "--apply", "--purge-blob",
                "--sources", str(sources)) == 0
    assert "blob deleted" in capsys.readouterr().out
    assert not blobs[0].exists()


def test_cli_document_rm_reports_conflicts_staged_against_its_rows(cli_ready, capsys):
    scan2 = cli_ready / "scan2.txt"
    scan2.write_bytes(b"hba1c 7.4 percent")
    assert _run(cli_ready, "ingest", str(scan2), "--person", "jane-doe",
                "--sources", str(cli_ready / "sources"),
                "--ocr-text-file", str(scan2)) == 0
    payload = cli_ready / "extract2.json"
    payload.write_text(json.dumps({"lab_result": [
        {"test_name": "HbA1c", "collected_at": "2026-01-02", "value_num": 7.4,
         "unit": "%"},
    ]}), encoding="utf-8")
    assert _run(cli_ready, "commit-extraction", "--document", "2",
                "--json", str(payload)) == 0

    capsys.readouterr()
    assert _run(cli_ready, "document", "rm", "1") == 0
    out = capsys.readouterr().out
    assert "conflicts deleted (staged against these records): 1" in out
    assert out.isascii(), repr(out)
    out.encode("cp437")

    # reassign refuses the same case rather than deleting anything.
    assert _run(cli_ready, "document", "reassign", "1", "--person", "john-doe",
                "--apply") == 1
    assert "review-conflicts" in capsys.readouterr().err


def test_cli_document_rm_purge_blob_already_gone(cli_ready, capsys):
    sources = cli_ready / "sources"
    for blob in [p for p in sources.rglob("*") if p.is_file()]:
        blob.unlink()
    capsys.readouterr()
    assert _run(cli_ready, "document", "rm", "1", "--apply", "--purge-blob",
                "--sources", str(sources)) == 0
    out = capsys.readouterr().out
    assert "blob already gone" in out and "blob deleted" not in out
    assert out.isascii(), repr(out)


def test_cli_document_rm_unknown_id_fails(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "rm", "999") == 1
    assert "no document with id" in capsys.readouterr().err


def test_cli_document_output_is_console_safe(cli_ready, capsys):
    """Issue #23 convention: static CLI strings must survive cp437/cp1252."""
    capsys.readouterr()
    for argv in (
        ("document", "list"),
        ("document", "show", "1"),
        ("document", "reassign", "1", "--person", "john-doe"),
        ("document", "rm", "1"),
    ):
        assert _run(cli_ready, *argv) == 0
        captured = capsys.readouterr()
        for text in (captured.out, captured.err):
            assert text.isascii(), repr(text)
            text.encode("cp437")


# --- re-extraction from the stored blob: `document reocr` (issue #143) -------


def _sources(tmp_path):
    return str(tmp_path / "sources")


def _ingest_textless(tmp_path, name, content, person="jane-doe"):
    """Ingest a blob with no ocr_text - the population `reocr` exists to repair."""
    src = tmp_path / name
    src.write_bytes(content)
    assert _run(tmp_path, "ingest", str(src), "--person", person,
                "--sources", _sources(tmp_path)) == 0


def _ocr_text_of(tmp_path, document_id):
    conn = db.connect(tmp_path / "cli.db")
    try:
        return conn.execute(
            "SELECT ocr_text FROM document WHERE document_id = ?", (document_id,)
        ).fetchone()["ocr_text"]
    finally:
        conn.close()


def test_list_documents_without_text_uses_the_invisible_aware_predicate(conn):
    """The #87 rows are the target population: raw truthiness calls them populated,
    `normalize_document_text` calls them empty, and only the second one is right."""
    jane = persons.add_person(conn, "jane-doe", "Jane Doe")
    john = persons.add_person(conn, "john-doe", "John Doe")
    populated = _insert_document(conn, jane.person_id, "aa11", ocr_text="real text")
    empty = _insert_document(conn, jane.person_id, "bb22", ocr_text=None)
    invisible = _insert_document(conn, jane.person_id, "cc33", ocr_text="​﻿")
    johns = _insert_document(conn, john.person_id, "dd44", ocr_text="")

    ids = documents.list_documents_without_text(conn)

    assert ids == sorted([empty, invisible, johns])   # ascending, resumable by eye
    assert populated not in ids
    assert documents.list_documents_without_text(conn, "jane-doe") == sorted(
        [empty, invisible]
    )


def test_list_documents_without_text_unknown_slug_raises(seeded):
    with pytest.raises(persons.PersonNotFoundError):
        documents.list_documents_without_text(seeded["conn"], "nobody")


def test_cli_document_reocr_fills_an_empty_document_and_find_sees_it(
    cli_ready, capsys
):
    _ingest_textless(cli_ready, "scan2.txt", b"acute pericarditis noted on review")
    capsys.readouterr()
    assert _run(cli_ready, "find", "--person", "jane-doe", "pericarditis") == 0
    assert "pericarditis" not in capsys.readouterr().out

    assert _run(cli_ready, "document", "reocr", "2",
                "--sources", _sources(cli_ready)) == 0
    out = capsys.readouterr().out
    assert "#2  written" in out
    assert "34 chars (was empty)" in out
    assert "route native" in out
    assert out.isascii(), repr(out)

    # No explicit reindex anywhere: the FTS trigger followed the ocr_text write.
    assert _run(cli_ready, "find", "--person", "jane-doe", "pericarditis") == 0
    assert "pericarditis" in capsys.readouterr().out.lower()


def test_cli_document_reocr_refuses_a_populated_document_without_force(
    cli_ready, capsys
):
    capsys.readouterr()
    assert _run(cli_ready, "document", "reocr", "1",
                "--sources", _sources(cli_ready)) == 1
    out = capsys.readouterr().out
    assert "#1  skipped" in out and "--force" in out
    assert _ocr_text_of(cli_ready, 1) == "hba1c 5.7 percent"


def test_cli_document_reocr_force_replaces_existing_text(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "reocr", "1", "--force",
                "--sources", _sources(cli_ready)) == 0
    assert "was 17 chars" in capsys.readouterr().out
    # The blob is the same bytes the transcription came from, so this is a no-change
    # replace - what matters is that force got past the guard at all.
    assert _ocr_text_of(cli_ready, 1) == "hba1c 5.7 percent"


def test_cli_document_reocr_dry_run_prints_char_count_and_names_truncation(
    cli_ready, capsys, monkeypatch
):
    _ingest_textless(cli_ready, "scan2.txt", b"a long scanned bundle of pages")
    # The page count is the extractor's business; here the unit under test is whether
    # the operator is *told* about the cap without having to read the write path.
    monkeypatch.setattr(ingest, "pdf_page_count", lambda src: 21)
    capsys.readouterr()

    assert _run(cli_ready, "document", "reocr", "2", "--dry-run",
                "--sources", _sources(cli_ready)) == 0
    out = capsys.readouterr().out
    assert "#2  would write  30 chars (was empty)" in out
    assert "[dry run]" in out
    assert f"pages: 21 (over the {ingest.OCR_MAX_PAGES}-page cap" in out
    assert out.isascii(), repr(out)
    assert _ocr_text_of(cli_ready, 2) is None


def test_cli_document_reocr_where_empty_selects_only_textless_documents(
    cli_ready, capsys
):
    _ingest_textless(cli_ready, "scan2.txt", b"second document text")
    capsys.readouterr()

    assert _run(cli_ready, "document", "reocr", "--where-empty",
                "--sources", _sources(cli_ready)) == 0
    out = capsys.readouterr().out
    assert "#2  written" in out
    assert "#1" not in out                 # already populated -> never selected
    assert "1 document(s): 1 written" in out


def test_cli_document_reocr_where_empty_scoped_by_person(cli_ready, capsys):
    _ingest_textless(cli_ready, "jane2.txt", b"jane's untranscribed scan")
    _ingest_textless(cli_ready, "john2.txt", b"john's untranscribed scan", "john-doe")
    capsys.readouterr()

    assert _run(cli_ready, "document", "reocr", "--where-empty",
                "--person", "john-doe", "--sources", _sources(cli_ready)) == 0
    out = capsys.readouterr().out
    assert "#3  written" in out and "#2" not in out
    assert _ocr_text_of(cli_ready, 2) is None
    assert _ocr_text_of(cli_ready, 3) == "john's untranscribed scan"


def test_cli_document_reocr_where_empty_on_a_clean_archive_is_a_noop(
    cli_ready, capsys
):
    capsys.readouterr()
    assert _run(cli_ready, "document", "reocr", "--where-empty",
                "--sources", _sources(cli_ready)) == 0
    assert "nothing to do" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ("document", "reocr", "1", "--where-empty"),        # both selectors
    ("document", "reocr"),                              # neither
    ("document", "reocr", "1", "--person", "jane-doe"),  # --person without the filter
])
def test_cli_document_reocr_rejects_bad_flag_combinations(cli_ready, argv):
    with pytest.raises(SystemExit) as exc:
        _run(cli_ready, *argv, "--sources", _sources(cli_ready))
    assert exc.value.code == 2


def test_cli_document_reocr_multi_id_summary_and_exit_code(cli_ready, capsys):
    _ingest_textless(cli_ready, "scan2.txt", b"recovered from the blob")
    capsys.readouterr()

    # One written, one refused: the write still lands, and the refusal still sets rc=1.
    assert _run(cli_ready, "document", "reocr", "1", "2",
                "--sources", _sources(cli_ready)) == 1
    out = capsys.readouterr().out
    assert "#1  skipped" in out and "#2  written" in out
    assert "2 document(s): 1 has-text, 1 written" in out
    assert _ocr_text_of(cli_ready, 2) == "recovered from the blob"


def test_cli_document_reocr_json_carries_a_stable_key_set(cli_ready, capsys):
    _ingest_textless(cli_ready, "scan2.txt", b"Patient: Jane  findings here")
    capsys.readouterr()

    assert _run(cli_ready, "document", "reocr", "1", "2", "--json",
                "--sources", _sources(cli_ready)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert [row["document_id"] for row in payload] == [1, 2]
    assert set(payload[0]) == {
        "document_id", "status", "chars", "previous_chars", "route", "pages",
        "truncated", "shrunk", "blob_path", "owner_check",
    }
    assert payload[0]["status"] == "has-text"
    assert payload[0]["owner_check"] is None            # refused before any check
    assert payload[1]["status"] == "written"
    assert set(payload[1]["owner_check"]) == {"verdict", "matched_slug", "evidence"}


def test_cli_document_reocr_missing_blob_is_reported_not_crashed(cli_ready, capsys):
    _ingest_textless(cli_ready, "scan2.txt", b"about to vanish from sources")
    conn = db.connect(cli_ready / "cli.db")
    try:
        source_path = conn.execute(
            "SELECT source_path FROM document WHERE document_id = 2"
        ).fetchone()["source_path"]
    finally:
        conn.close()
    (cli_ready / "sources" / source_path).unlink()
    capsys.readouterr()

    assert _run(cli_ready, "document", "reocr", "2",
                "--sources", _sources(cli_ready)) == 1
    out = capsys.readouterr().out
    assert "#2  missing blob" in out
    assert out.isascii(), repr(out)


def test_cli_document_reocr_owner_mismatch_refuses_and_names_the_remedy(
    cli_ready, capsys
):
    # A roster person with a usable multi-token name: `name_tokens` deliberately
    # ignores mononyms, so the fixture's "Jane"/"John" can never produce a verdict.
    assert _run(cli_ready, "person", "add", "--slug", "bob-roe",
                "--name", "Robert Alan Roe") == 0
    _ingest_textless(cli_ready, "scan2.txt", b"Patient: ROE, ROBERT ALAN  summary")
    capsys.readouterr()

    assert _run(cli_ready, "document", "reocr", "2",
                "--sources", _sources(cli_ready)) == 1
    out = capsys.readouterr().out
    assert "#2  refused" in out
    assert "text matches 'bob-roe'" in out
    assert "pemr document reassign 2 --person bob-roe" in out
    assert out.isascii(), repr(out)
    assert _ocr_text_of(cli_ready, 2) is None

    # ...and --force is the documented way past it.
    assert _run(cli_ready, "document", "reocr", "2", "--force",
                "--sources", _sources(cli_ready)) == 0
    assert _ocr_text_of(cli_ready, 2) == "Patient: ROE, ROBERT ALAN  summary"


# --- the shrinkage guard at the operator's surface (issue #174) --------------


def _set_long_text(tmp_path, document_id, text):
    """`document set-text --force` a long transcription onto a document - the issue's
    own verification setup, and the shape that makes the blob's text a shrinkage."""
    path = tmp_path / f"long-{document_id}.txt"
    path.write_text(text, encoding="utf-8")
    assert _run(tmp_path, "document", "set-text", str(document_id),
                "--ocr-text-file", str(path), "--force") == 0


_LONG = "a long stored transcription that took somebody real effort to type out"


def test_cli_document_reocr_refuses_a_shrinking_replacement(cli_ready, capsys):
    """The issue's verification case verbatim: ingest, set-text a long text, then
    `reocr --force --dry-run` against a blob that extracts to something shorter."""
    _ingest_textless(cli_ready, "scan2.txt", b"three words only")
    _set_long_text(cli_ready, 2, _LONG)
    capsys.readouterr()

    assert _run(cli_ready, "document", "reocr", "2", "--force", "--dry-run",
                "--sources", _sources(cli_ready)) == 1
    out = capsys.readouterr().out
    assert "#2  refused" in out
    assert "shorter than stored" in out
    assert f"16 chars (was {len(_LONG)} chars)" in out
    assert "--allow-shrink" in out
    assert "would write" not in out
    assert "1 document(s): 1 shorter-text" in out
    assert out.isascii(), repr(out)
    assert _ocr_text_of(cli_ready, 2) == _LONG


def test_cli_document_reocr_allow_shrink_stores_and_warns(cli_ready, capsys):
    _ingest_textless(cli_ready, "scan2.txt", b"three words only")
    _set_long_text(cli_ready, 2, _LONG)
    capsys.readouterr()

    assert _run(cli_ready, "document", "reocr", "2", "--force", "--allow-shrink",
                "--sources", _sources(cli_ready)) == 0
    out = capsys.readouterr().out
    assert "#2  written" in out
    assert f"shrink: 16 chars replaces {len(_LONG)}" in out
    assert "the difference is discarded" in out
    assert out.isascii(), repr(out)
    assert _ocr_text_of(cli_ready, 2) == "three words only"


def test_cli_document_reocr_json_reports_shrinkage(cli_ready, capsys):
    _ingest_textless(cli_ready, "scan2.txt", b"three words only")
    _set_long_text(cli_ready, 2, _LONG)
    capsys.readouterr()

    assert _run(cli_ready, "document", "reocr", "2", "--force", "--json",
                "--sources", _sources(cli_ready)) == 1
    (row,) = json.loads(capsys.readouterr().out)
    assert row["status"] == "shorter-text"
    assert row["shrunk"] is True
    assert row["chars"] == 16 and row["previous_chars"] == len(_LONG)
    # The owner audit the guard sits behind still gets its verdict.
    assert row["owner_check"]["verdict"] == "unverified"
    assert _ocr_text_of(cli_ready, 2) == _LONG


def test_cli_document_reocr_truncated_and_shrunk_prints_both_notices(
    cli_ready, capsys, monkeypatch
):
    """Two independent conditions on one document, so neither notice may swallow the
    other - the page cap is one cause of a shorter replacement, not the condition."""
    _ingest_textless(cli_ready, "bundle.txt", b"three words only")
    _set_long_text(cli_ready, 2, _LONG)
    monkeypatch.setattr(ingest, "pdf_page_count", lambda src: 21)
    capsys.readouterr()

    assert _run(cli_ready, "document", "reocr", "2", "--force",
                "--sources", _sources(cli_ready)) == 1
    out = capsys.readouterr().out
    assert "#2  refused" in out and "shorter than stored" in out
    assert f"pages: 21 (over the {ingest.OCR_MAX_PAGES}-page cap" in out
    assert out.isascii(), repr(out)


def test_cli_document_reocr_allow_shrink_alone_does_not_replace_text(
    cli_ready, capsys
):
    """It overrides one refusal, not the has-text guard - `--force` still means what it
    meant, and neither flag implies the other."""
    capsys.readouterr()
    assert _run(cli_ready, "document", "reocr", "1", "--allow-shrink",
                "--sources", _sources(cli_ready)) == 1
    assert "#1  skipped" in capsys.readouterr().out
    assert _ocr_text_of(cli_ready, 1) == "hba1c 5.7 percent"


def test_cli_document_reocr_mixed_sweep_summarises_both_outcomes(cli_ready, capsys):
    """The shape a real sweep takes: one document re-derives to the same text and is
    written, another shrinks and is refused. The summary must name both, and one
    refusal must carry the whole run to rc=1 - a sweep that exits 0 because most of it
    succeeded is how the silent loss this issue reports goes unnoticed."""
    _ingest_textless(cli_ready, "scan2.txt", b"three words only")
    _set_long_text(cli_ready, 2, _LONG)
    capsys.readouterr()

    assert _run(cli_ready, "document", "reocr", "1", "2", "--force",
                "--sources", _sources(cli_ready)) == 1
    out = capsys.readouterr().out
    assert "#1  written" in out
    assert "#2  refused" in out and "shorter than stored" in out
    assert "2 document(s): 1 shorter-text, 1 written" in out
    # The written one did not shrink, so it draws no shrink notice.
    assert "shrink:" not in out
    assert out.isascii(), repr(out)
    assert _ocr_text_of(cli_ready, 1) == "hba1c 5.7 percent"
    assert _ocr_text_of(cli_ready, 2) == _LONG


def test_cli_document_reocr_unknown_id_is_a_friendly_error(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "reocr", "999",
                "--sources", _sources(cli_ready)) == 1
    assert "no document with id" in capsys.readouterr().err


def test_cli_document_reocr_without_a_sources_dir_is_a_friendly_error(
    cli_ready, monkeypatch
):
    monkeypatch.delenv("PEMR_SOURCES", raising=False)
    monkeypatch.delenv("PEMR_CONFIG", raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.main([
            "--db", str(cli_ready / "cli.db"),
            "--config", str(cli_ready / "absent.toml"),
            "document", "reocr", "1",
        ])
    assert "no sources dir" in str(exc.value.code)


def test_cli_document_reocr_leaves_records_and_keys_untouched(cli_ready, capsys):
    """`reocr` touches one column. Record rows, dedup keys and the blob are not its
    business, and extraction against a repaired document behaves normally."""
    conn = db.connect(cli_ready / "cli.db")
    try:
        before = conn.execute(
            "SELECT lab_result_id, dedup_key FROM lab_result ORDER BY lab_result_id"
        ).fetchall()
    finally:
        conn.close()
    _ingest_textless(cli_ready, "scan2.txt", b"repaired document text")
    blob_bytes = sorted(p.read_bytes() for p in (cli_ready / "sources").rglob("*.txt"))
    capsys.readouterr()

    assert _run(cli_ready, "document", "reocr", "2",
                "--sources", _sources(cli_ready)) == 0
    capsys.readouterr()

    payload = cli_ready / "extract2.json"
    payload.write_text(json.dumps({"lab_result": [
        {"test_name": "Sodium", "collected_at": "2026-02-02", "value_num": 140,
         "unit": "mmol/L"},
    ]}), encoding="utf-8")
    assert _run(cli_ready, "commit-extraction", "--document", "2",
                "--json", str(payload)) == 0

    conn = db.connect(cli_ready / "cli.db")
    try:
        after = conn.execute(
            "SELECT lab_result_id, dedup_key FROM lab_result WHERE document_id = 1 "
            "ORDER BY lab_result_id"
        ).fetchall()
        added = conn.execute(
            "SELECT COUNT(*) AS n FROM lab_result WHERE document_id = 2"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    assert added == 1
    # The blob store is read-only to this verb.
    assert sorted(
        p.read_bytes() for p in (cli_ready / "sources").rglob("*.txt")
    ) == blob_bytes


def test_cli_document_reocr_names_truncation_on_the_write_path_too(
    cli_ready, capsys, monkeypatch
):
    """The dry-run names an over-cap document; so must the run that writes for real.
    A sweep is exactly where the operator is *not* looking at the document, so a
    truncation notice that only appears under `--dry-run` is a notice nobody sees."""
    _ingest_textless(cli_ready, "bundle.txt", b"a long scanned bundle of pages")
    monkeypatch.setattr(ingest, "pdf_page_count", lambda src: 21)
    capsys.readouterr()

    assert _run(cli_ready, "document", "reocr", "2",
                "--sources", _sources(cli_ready)) == 0
    out = capsys.readouterr().out
    assert "#2  written" in out
    assert f"pages: 21 (over the {ingest.OCR_MAX_PAGES}-page cap" in out
    assert out.isascii(), repr(out)
    assert _ocr_text_of(cli_ready, 2) == "a long scanned bundle of pages"


def test_cli_document_reocr_stores_what_ingest_would_have_stored(cli_ready, capsys):
    """The anti-drift claim at the operator's surface: the same bytes ingested with
    `--ocr auto` today, and ingested textless then repaired with `reocr`, end up with
    byte-identical `ocr_text`. Two archives, because layer-1 dedup rightly refuses the
    same sha twice inside one."""
    content = b"MERIDIAN FAMILY CLINIC\nSodium 140 mmol/L\nrepeat panel in six months\n"
    blob = cli_ready / "panel.txt"
    blob.write_bytes(content)

    # Archive A - ingested after the extractor could read it.
    other = cli_ready / "other"
    other.mkdir()
    assert cli.main(["--db", str(other / "cli.db"), "migrate", "--create"]) == 0
    assert cli.main(["--db", str(other / "cli.db"), "person", "add",
                     "--slug", "jane-doe", "--name", "Jane"]) == 0
    assert cli.main(["--db", str(other / "cli.db"), "ingest", str(blob),
                     "--person", "jane-doe", "--sources", str(other / "sources"),
                     "--ocr", "auto"]) == 0

    # Archive B - the #143 population: same bytes, ingested before it could.
    _ingest_textless(cli_ready, "panel-copy.txt", content)
    capsys.readouterr()
    assert _run(cli_ready, "document", "reocr", "2",
                "--sources", _sources(cli_ready)) == 0

    conn = db.connect(other / "cli.db")
    try:
        at_ingest = conn.execute(
            "SELECT ocr_text FROM document WHERE document_id = 1"
        ).fetchone()["ocr_text"]
    finally:
        conn.close()
    assert at_ingest, "the --ocr auto ingest stored nothing; the comparison is vacuous"
    assert _ocr_text_of(cli_ready, 2) == at_ingest

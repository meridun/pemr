"""Misfiled-document recovery: `pemr document list|edit|reassign|rm` (issue #54),
plus `document show` and `document set-text` (issue #62).

Covers the module surface (pemr/documents.py) and the CLI wiring, in the shape
test_persons.py uses for the `person` group.
"""

import json

import pytest

from pemr import cli, db, dedup, documents, persons

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
    assert summary.counts == {"new": 0, "duplicate": 1, "enriched": 0, "conflict": 0}


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

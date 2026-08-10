"""Recorded human verdicts over record families: `pemr record annotate` (issue #109).

Covers the engine module (pemr/curation.py) and its CLI wiring, in the shape
test_records.py uses for `record rm`. The two tests that matter most are the identity
regressions: a verdict is keyed by ``dedup_base``, so it must survive a `record rm` of
occurrence 0 and must cover a later `--keep both` sibling — the two things a row-id or
``dedup_key`` key would get wrong.

Renderer behaviour lives in test_render.py, next to the rest of the render mechanics;
the `verify` orphan warning lives here (it is curation's own check) and in the
integration drill at the bottom.
"""

import json

import pytest

from pemr import cli, curation, db, dedup, documents, persons, records, verify

RECORDS = {
    "lab_result": [
        {"test_name": "HbA1c", "collected_at": "2026-01-02", "value_num": 5.7,
         "unit": "%"},
        {"test_name": "Glucose", "collected_at": "2026-01-02", "value_num": 95,
         "unit": "mg/dL"},
    ],
    "condition": [
        {"name": "Type 2 Diabetes", "status": "active", "onset_on": "2024-01-01"},
        {"name": "Prediabetes", "status": "history", "onset_on": "2022-01-01"},
    ],
}


@pytest.fixture()
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    yield conn
    conn.close()


def _insert_document(conn, person_id, sha):
    cur = conn.execute(
        """
        INSERT INTO document
          (sha256, person_id, doc_date, category, provider, source_path,
           ocr_text, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (sha, person_id, "2026-03-15", "visit-note", "Dr Who",
         f"{sha[:2]}/{sha}.pdf", "scan text", "2026-03-16T00:00:00"),
    )
    conn.commit()
    return int(cur.lastrowid)


@pytest.fixture()
def seeded(conn):
    """Jane owns one document holding two labs and two conditions."""
    jane = persons.add_person(conn, "jane-doe", "Jane Doe")
    doc_id = _insert_document(conn, jane.person_id, "aa11bb22cc33dd44")
    dedup.commit_extraction(conn, doc_id, RECORDS)
    return {"conn": conn, "jane": jane, "doc": doc_id}


def _row_id(conn, record_type, column, value):
    return int(conn.execute(
        f"SELECT {record_type}_id FROM {record_type} WHERE {column} = ?", (value,)
    ).fetchone()[f"{record_type}_id"])


def _base(conn, record_type, column, value):
    return conn.execute(
        f"SELECT dedup_base FROM {record_type} WHERE {column} = ?", (value,)
    ).fetchone()["dedup_base"]


def _curation_count(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM curation").fetchone()["n"]


def _record_counts(conn):
    return {
        record_type: conn.execute(
            f"SELECT COUNT(*) AS n FROM {record_type}"
        ).fetchone()["n"]
        for record_type in dedup.KNOWN_TYPES
    }


# --- the core claim: an overlay, not a mutation ------------------------------


def test_annotate_writes_a_verdict_and_touches_no_record_row(seeded):
    conn = seeded["conn"]
    before = _record_counts(conn)
    row_id = _row_id(conn, "lab_result", "test_name", "Glucose")
    stored = dict(conn.execute(
        "SELECT * FROM lab_result WHERE lab_result_id = ?", (row_id,)
    ).fetchone())

    report = curation.annotate_record(
        conn, "lab_result", str(row_id),
        status="erroneous-in-source", note="requisition coding artifact",
        attributed_to="Dr Who", apply=True,
    )

    assert report.applied is True
    assert report.action == "create"
    assert report.label == "Glucose"
    assert report.family_size == 1
    assert report.dedup_base == stored["dedup_base"]
    # Not one source row moved.
    assert _record_counts(conn) == before
    assert dict(conn.execute(
        "SELECT * FROM lab_result WHERE lab_result_id = ?", (row_id,)
    ).fetchone()) == stored
    assert curation.get_verdict(conn, "lab_result", report.dedup_base) == {
        "record_type": "lab_result",
        "dedup_base": report.dedup_base,
        "record_id": 0,
        "scope": "family",
        "status": "erroneous-in-source",
        "note": "requisition coding artifact",
        "merged_into_base": None,
        "attributed_to": "Dr Who",
        "created_at": report.created_at,
    }


@pytest.mark.parametrize("status", curation.STATUSES)
def test_every_status_round_trips(seeded, status):
    conn = seeded["conn"]
    base = _base(conn, "lab_result", "test_name", "Glucose")
    target = _base(conn, "lab_result", "test_name", "HbA1c")
    curation.annotate_record(
        conn, "lab_result", base, status=status, note="because",
        merged_into_base=target if status == "merged-into" else None, apply=True,
    )
    stored = curation.get_verdict(conn, "lab_result", base)
    assert stored["status"] == status
    assert stored["merged_into_base"] == (
        target if status == "merged-into" else None
    )


def test_dry_run_is_the_default_and_writes_nothing(seeded):
    conn = seeded["conn"]
    base = _base(conn, "lab_result", "test_name", "Glucose")
    report = curation.annotate_record(
        conn, "lab_result", base, status="disputed", note="two sources disagree"
    )
    assert report.applied is False
    assert report.action == "create"
    assert _curation_count(conn) == 0


def test_re_annotating_overwrites_and_reports_the_previous_verdict(seeded):
    conn = seeded["conn"]
    base = _base(conn, "lab_result", "test_name", "Glucose")
    first = curation.annotate_record(
        conn, "lab_result", base, status="disputed", note="two sources disagree",
        now="2026-01-01T00:00:00+00:00", apply=True,
    )
    second = curation.annotate_record(
        conn, "lab_result", base, status="superseded", note="repeat draw supersedes",
        now="2026-02-02T00:00:00+00:00", apply=True,
    )
    assert second.action == "overwrite"
    assert second.previous["status"] == "disputed"
    assert second.previous["created_at"] == first.created_at
    assert _curation_count(conn) == 1  # one live verdict per family
    live = curation.get_verdict(conn, "lab_result", base)
    assert live["status"] == "superseded"
    assert live["created_at"] == "2026-02-02T00:00:00+00:00"


# --- validation --------------------------------------------------------------


@pytest.mark.parametrize("note", ["", "   ", "\t\n"])
def test_empty_note_is_refused(seeded, note):
    conn = seeded["conn"]
    base = _base(conn, "lab_result", "test_name", "Glucose")
    with pytest.raises(ValueError, match="note is required"):
        curation.annotate_record(
            conn, "lab_result", base, status="disputed", note=note, apply=True
        )
    assert _curation_count(conn) == 0


def test_unknown_record_type_is_refused(seeded):
    with pytest.raises(ValueError, match="unknown record type"):
        curation.annotate_record(
            seeded["conn"], "family_history", "abc", status="disputed", note="x"
        )


def test_unknown_status_is_refused(seeded):
    conn = seeded["conn"]
    base = _base(conn, "lab_result", "test_name", "Glucose")
    with pytest.raises(ValueError, match="unknown curation status"):
        curation.annotate_record(
            conn, "lab_result", base, status="wrong-ish", note="x"
        )


def test_merged_into_is_required_for_merged_into_and_refused_otherwise(seeded):
    conn = seeded["conn"]
    base = _base(conn, "lab_result", "test_name", "Glucose")
    other = _base(conn, "lab_result", "test_name", "HbA1c")
    with pytest.raises(ValueError, match="needs the family it merges into"):
        curation.annotate_record(
            conn, "lab_result", base, status="merged-into", note="x"
        )
    with pytest.raises(ValueError, match="only meaningful with status 'merged-into'"):
        curation.annotate_record(
            conn, "lab_result", base, status="disputed", note="x",
            merged_into_base=other,
        )
    assert _curation_count(conn) == 0


def test_merge_target_must_be_a_live_other_family(seeded):
    conn = seeded["conn"]
    base = _base(conn, "lab_result", "test_name", "Glucose")
    with pytest.raises(curation.FamilyNotFoundError):
        curation.annotate_record(
            conn, "lab_result", base, status="merged-into", note="x",
            merged_into_base="deadbeef" * 8,
        )
    with pytest.raises(ValueError, match="into itself"):
        curation.annotate_record(
            conn, "lab_result", base, status="merged-into", note="x",
            merged_into_base=base,
        )
    assert _curation_count(conn) == 0


def test_a_bad_verdict_never_overwrites_a_good_one(seeded):
    """Validation is front-loaded because the write is an upsert: a typo'd status must
    not take the previous verdict down with it."""
    conn = seeded["conn"]
    base = _base(conn, "lab_result", "test_name", "Glucose")
    curation.annotate_record(
        conn, "lab_result", base, status="confirmed", note="clinician agreed",
        apply=True,
    )
    with pytest.raises(ValueError):
        curation.annotate_record(
            conn, "lab_result", base, status="nonsense", note="oops", apply=True
        )
    assert curation.get_verdict(conn, "lab_result", base)["status"] == "confirmed"


def test_schema_check_backstops_a_hand_edited_status(seeded):
    """Python raises first for every operator path; the CHECK exists for the row a
    human (or a corrupted restore) writes around it."""
    conn = seeded["conn"]
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO curation (record_type, dedup_base, status, note, created_at) "
            "VALUES ('lab_result', 'b', 'not-a-status', 'why', '2026-01-01T00:00:00')"
        )
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO curation (record_type, dedup_base, status, note, created_at) "
            "VALUES ('lab_result', 'b', 'disputed', '   ', '2026-01-01T00:00:00')"
        )
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO curation (record_type, dedup_base, status, note, "
            "merged_into_base, created_at) VALUES "
            "('lab_result', 'b', 'disputed', 'why', 'other', '2026-01-01T00:00:00')"
        )


# --- target resolution -------------------------------------------------------


def test_resolve_base_accepts_a_row_id_or_a_base(seeded):
    conn = seeded["conn"]
    row_id = _row_id(conn, "lab_result", "test_name", "Glucose")
    base = _base(conn, "lab_result", "test_name", "Glucose")
    assert curation.resolve_base(conn, "lab_result", str(row_id)) == base
    assert curation.resolve_base(conn, "lab_result", base) == base


def test_resolve_base_reports_both_not_found_cases(seeded):
    conn = seeded["conn"]
    with pytest.raises(curation.FamilyNotFoundError, match="no lab_result with id 9999"):
        curation.resolve_base(conn, "lab_result", "9999")
    with pytest.raises(curation.FamilyNotFoundError, match="no live lab_result family"):
        curation.resolve_base(conn, "lab_result", "deadbeef" * 8)


# --- list / clear ------------------------------------------------------------


def test_list_curation_filters_by_type_and_flags_orphans(seeded):
    conn = seeded["conn"]
    lab_base = _base(conn, "lab_result", "test_name", "Glucose")
    cond_base = _base(conn, "condition", "name", "Prediabetes")
    curation.annotate_record(conn, "lab_result", lab_base, status="disputed",
                            note="a", now="2026-01-01T00:00:00+00:00", apply=True)
    curation.annotate_record(conn, "condition", cond_base, status="superseded",
                            note="b", now="2026-02-01T00:00:00+00:00", apply=True)

    rows = curation.list_curation(conn)
    assert [r["record_type"] for r in rows] == ["condition", "lab_result"]  # newest first
    assert rows[0]["label"] == "Prediabetes" and rows[0]["family_size"] == 1
    assert [r["record_type"] for r in curation.list_curation(conn, "lab_result")] == [
        "lab_result"
    ]

    records.remove_record(
        conn, "condition", _row_id(conn, "condition", "name", "Prediabetes"), apply=True
    )
    orphan = [r for r in curation.list_curation(conn) if r["record_type"] == "condition"]
    assert orphan[0]["family_size"] == 0 and orphan[0]["label"] == ""


def test_clear_curation_dry_run_then_apply(seeded):
    conn = seeded["conn"]
    base = _base(conn, "lab_result", "test_name", "Glucose")
    curation.annotate_record(conn, "lab_result", base, status="confirmed",
                            note="agreed", apply=True)

    dry = curation.clear_curation(conn, "lab_result", base)
    assert dry.action == "clear" and dry.applied is False
    assert _curation_count(conn) == 1

    done = curation.clear_curation(conn, "lab_result", base, apply=True)
    assert done.applied is True
    assert done.previous["status"] == "confirmed"
    assert _curation_count(conn) == 0


def test_clear_curation_without_a_verdict_is_an_error_not_a_success(seeded):
    conn = seeded["conn"]
    base = _base(conn, "lab_result", "test_name", "Glucose")
    with pytest.raises(curation.CurationNotFoundError, match="no curation verdict"):
        curation.clear_curation(conn, "lab_result", base, apply=True)


def test_clear_curation_can_lift_an_orphaned_verdict(seeded):
    """The verdict `verify` warns about: its family is gone, so no row id names it."""
    conn = seeded["conn"]
    base = _base(conn, "lab_result", "test_name", "Glucose")
    curation.annotate_record(conn, "lab_result", base, status="superseded",
                            note="gone", apply=True)
    records.remove_record(
        conn, "lab_result", _row_id(conn, "lab_result", "test_name", "Glucose"),
        apply=True,
    )
    report = curation.clear_curation(conn, "lab_result", base, apply=True)
    assert report.family_size == 0
    assert _curation_count(conn) == 0


# --- identity: the whole reason for keying on dedup_base ---------------------


def _second_occurrence(seeded):
    """Re-file the same Glucose fact off a second document as occurrence 1 (the
    `--keep both` shape), and return the family's base."""
    conn = seeded["conn"]
    base = _base(conn, "lab_result", "test_name", "Glucose")
    doc2 = _insert_document(conn, seeded["jane"].person_id, "bb22cc33dd44ee55")
    conn.execute(
        "INSERT INTO lab_result (person_id, document_id, test_name, collected_at, "
        "value_num, unit, dedup_key, dedup_base, dedup_occurrence) "
        "VALUES (?, ?, 'Glucose', '2026-01-02', 95, 'mg/dL', ?, ?, 1)",
        (seeded["jane"].person_id, doc2, dedup.occurrence_key(base, 1), base),
    )
    conn.commit()
    return base


def test_verdict_survives_removal_of_occurrence_zero(seeded):
    """A row-id key would dangle here: `record rm` deliberately does not renumber, so
    the family lives on at occurrence 1 under the same base."""
    conn = seeded["conn"]
    base = _second_occurrence(seeded)
    curation.annotate_record(conn, "lab_result", base, status="disputed",
                            note="two sources disagree", apply=True)
    occ0 = int(conn.execute(
        "SELECT lab_result_id FROM lab_result WHERE dedup_base = ? AND "
        "dedup_occurrence = 0", (base,)
    ).fetchone()["lab_result_id"])

    records.remove_record(conn, "lab_result", occ0, apply=True)

    verdict = curation.get_verdict(conn, "lab_result", base)
    assert verdict is not None and verdict["status"] == "disputed"
    label, size = curation.family_label(conn, "lab_result", base)
    assert (label, size) == ("Glucose", 1)  # still resolves to a live family


def test_verdict_covers_a_later_keep_both_sibling(seeded):
    """A `dedup_key` key would only have covered occurrence 0; the base covers the
    whole family, which is what "one verdict per fact" means."""
    conn = seeded["conn"]
    base = _base(conn, "lab_result", "test_name", "Glucose")
    curation.annotate_record(conn, "lab_result", base, status="superseded",
                            note="corrected downstream", apply=True)
    _second_occurrence(seeded)

    assert curation.family_label(conn, "lab_result", base)[1] == 2
    assert (
        curation.load_verdicts(conn).family[("lab_result", base)]["status"]
        == "superseded"
    )


def test_verdict_survives_a_no_drift_rekey(seeded):
    """`rekey` under an unchanged dictionary is key-neutral, so the verdict still
    resolves. (A dictionary edit that renames the canonical fact *does* move the base
    -- that verdict orphans and `pemr verify` warns about it, by design.)"""
    conn = seeded["conn"]
    base = _second_occurrence(seeded)
    curation.annotate_record(conn, "lab_result", base, status="confirmed",
                            note="agreed", apply=True)
    report = dedup.rekey(conn, None, apply=True)
    assert not report.collisions
    assert curation.get_verdict(conn, "lab_result", base) is not None


# --- row scope: one occurrence of a multi-row family (issue #114) -------------


def _occurrence_ids(conn, base):
    """``(occurrence 0 id, occurrence 1 id)`` for a two-row lab_result family."""
    rows = conn.execute(
        "SELECT lab_result_id FROM lab_result WHERE dedup_base = ? "
        "ORDER BY dedup_occurrence", (base,)
    ).fetchall()
    return tuple(int(r["lab_result_id"]) for r in rows)


def test_annotate_row_scopes_the_verdict_to_a_single_row(seeded):
    """The case that forced #114: a `--keep both` family holds two *live* rows, and a
    family-scoped verdict on the loser hides the winner too."""
    conn = seeded["conn"]
    base = _second_occurrence(seeded)
    _occ0, occ1 = _occurrence_ids(conn, base)

    report = curation.annotate_record(
        conn, "lab_result", str(occ1), status="superseded",
        note="loser of an earlier keep-both", row=True, apply=True,
    )

    assert report.scope == curation.SCOPE_ROW and report.record_id == occ1
    assert report.dedup_base == base       # stored as the breadcrumb
    assert report.label == "Glucose" and report.family_size == 2
    stored = curation.get_verdict(conn, "lab_result", base, record_id=occ1)
    assert stored["scope"] == "row" and stored["record_id"] == occ1
    # No family verdict was created as a side effect: the sibling is untouched.
    assert curation.get_verdict(conn, "lab_result", base) is None
    assert _curation_count(conn) == 1


def test_family_scope_is_unchanged_and_stores_the_sentinel(seeded):
    """AC 3, the backward-compatibility pin: with no `row=True` anywhere, storage is
    bit-identical to #109's - one row, `record_id = 0`, resolvable by base."""
    conn = seeded["conn"]
    base = _second_occurrence(seeded)
    report = curation.annotate_record(
        conn, "lab_result", base, status="superseded", note="whole family", apply=True
    )
    assert report.scope == curation.SCOPE_FAMILY
    assert report.record_id == curation.FAMILY_SCOPE
    stored = conn.execute("SELECT * FROM curation").fetchone()
    assert stored["record_id"] == 0 and stored["dedup_base"] == base
    verdicts = curation.load_verdicts(conn)
    assert verdicts.rows == {} and set(verdicts.family) == {("lab_result", base)}
    # Every row of the family sees it.
    for record_id in _occurrence_ids(conn, base):
        assert verdicts.for_row("lab_result", base, record_id)["note"] == "whole family"


def test_a_row_verdict_wins_over_its_family_verdict_for_that_row_only(seeded):
    conn = seeded["conn"]
    base = _second_occurrence(seeded)
    occ0, occ1 = _occurrence_ids(conn, base)
    curation.annotate_record(conn, "lab_result", base, status="disputed",
                            note="the family is contested", apply=True)
    curation.annotate_record(conn, "lab_result", str(occ1), status="superseded",
                            note="this occurrence lost", row=True, apply=True)

    verdicts = curation.load_verdicts(conn)
    assert len(verdicts) == 2                       # both scopes coexist
    assert verdicts.for_row("lab_result", base, occ1)["status"] == "superseded"
    assert verdicts.for_row("lab_result", base, occ0)["status"] == "disputed"
    # A carrier with no row identity can still only see the family verdict.
    assert verdicts.for_row("lab_result", base, None)["status"] == "disputed"


def test_re_annotating_a_row_overwrites_only_that_row(seeded):
    conn = seeded["conn"]
    base = _second_occurrence(seeded)
    _occ0, occ1 = _occurrence_ids(conn, base)
    curation.annotate_record(conn, "lab_result", str(occ1), status="disputed",
                            note="first", row=True, now="2026-01-01T00:00:00+00:00",
                            apply=True)
    second = curation.annotate_record(
        conn, "lab_result", str(occ1), status="superseded", note="second", row=True,
        now="2026-02-02T00:00:00+00:00", apply=True,
    )
    assert second.action == "overwrite" and second.previous["note"] == "first"
    assert _curation_count(conn) == 1
    live = curation.get_verdict(conn, "lab_result", base, record_id=occ1)
    assert live["status"] == "superseded"
    assert live["created_at"] == "2026-02-02T00:00:00+00:00"


def test_resolve_row_requires_a_live_row_id(seeded):
    conn = seeded["conn"]
    row_id = _row_id(conn, "lab_result", "test_name", "Glucose")
    base = _base(conn, "lab_result", "test_name", "Glucose")
    assert curation.resolve_row(conn, "lab_result", str(row_id)) == (row_id, base)
    with pytest.raises(curation.RowNotFoundError, match="no lab_result with id 9999"):
        curation.resolve_row(conn, "lab_result", "9999")
    with pytest.raises(ValueError, match="drop --row"):
        curation.resolve_row(conn, "lab_result", base)


def test_row_and_family_verdicts_are_cleared_independently(seeded):
    conn = seeded["conn"]
    base = _second_occurrence(seeded)
    _occ0, occ1 = _occurrence_ids(conn, base)
    curation.annotate_record(conn, "lab_result", base, status="disputed",
                            note="family", apply=True)
    curation.annotate_record(conn, "lab_result", str(occ1), status="superseded",
                            note="row", row=True, apply=True)

    cleared = curation.clear_curation(conn, "lab_result", str(occ1), row=True,
                                      apply=True)
    assert cleared.scope == "row" and cleared.record_id == occ1
    assert curation.get_verdict(conn, "lab_result", base, record_id=occ1) is None
    assert curation.get_verdict(conn, "lab_result", base)["note"] == "family"

    curation.annotate_record(conn, "lab_result", str(occ1), status="superseded",
                            note="row again", row=True, apply=True)
    curation.clear_curation(conn, "lab_result", base, apply=True)
    assert curation.get_verdict(conn, "lab_result", base) is None
    assert curation.get_verdict(
        conn, "lab_result", base, record_id=occ1
    )["note"] == "row again"


def test_clearing_an_empty_scope_is_an_error_not_a_success(seeded):
    conn = seeded["conn"]
    base = _second_occurrence(seeded)
    _occ0, occ1 = _occurrence_ids(conn, base)
    curation.annotate_record(conn, "lab_result", base, status="disputed",
                            note="family only", apply=True)
    with pytest.raises(curation.CurationNotFoundError, match="row-scoped"):
        curation.clear_curation(conn, "lab_result", str(occ1), row=True, apply=True)
    with pytest.raises(ValueError, match="drop --row"):
        curation.clear_curation(conn, "lab_result", base, row=True, apply=True)
    assert _curation_count(conn) == 1


def test_list_curation_reports_scope_for_both_kinds(seeded):
    conn = seeded["conn"]
    base = _second_occurrence(seeded)
    _occ0, occ1 = _occurrence_ids(conn, base)
    curation.annotate_record(conn, "lab_result", base, status="disputed", note="fam",
                            now="2026-01-01T00:00:00+00:00", apply=True)
    curation.annotate_record(conn, "lab_result", str(occ1), status="superseded",
                            note="row", row=True, now="2026-02-01T00:00:00+00:00",
                            apply=True)

    rows = curation.list_curation(conn)
    assert [r["scope"] for r in rows] == ["row", "family"]     # newest first
    assert rows[0]["record_id"] == occ1 and rows[1]["record_id"] == 0
    assert all(r["label"] == "Glucose" and r["family_size"] == 2 for r in rows)


def test_a_row_verdict_survives_the_rekey_that_orphans_a_family_verdict(seeded):
    """The rekey answer AC 8 asks to *document*, pinned as behaviour: `rekey` rewrites
    dedup_key/dedup_base but never renumbers a row id, so a row-scoped verdict follows
    its row while the family-scoped one on the same family goes inert."""
    conn = seeded["conn"]
    base = _second_occurrence(seeded)
    _occ0, occ1 = _occurrence_ids(conn, base)
    curation.annotate_record(conn, "lab_result", base, status="disputed",
                            note="family", apply=True)
    curation.annotate_record(conn, "lab_result", str(occ1), status="superseded",
                            note="row", row=True, apply=True)

    report = dedup.rekey(conn, {"glucose": "blood-sugar"}, apply=True)
    assert not report.collisions and report.changes

    new_base = _base(conn, "lab_result", "test_name", "Glucose")
    assert new_base != base
    # The family verdict is orphaned - its base names nothing live.
    assert curation.family_label(conn, "lab_result", base) == ("", 0)
    # The row verdict still resolves, by row id; its stored base is now a stale
    # breadcrumb that nothing resolves on.
    live = curation.get_verdict(conn, "lab_result", "", record_id=occ1)
    assert live["status"] == "superseded" and live["dedup_base"] == base
    assert curation.load_verdicts(conn).for_row(
        "lab_result", new_base, occ1
    )["status"] == "superseded"
    # `--list` re-reads the live base off the row, so it is not reported as an orphan.
    entry = next(r for r in curation.list_curation(conn) if r["record_id"] == occ1)
    assert entry["label"] == "Glucose" and entry["family_size"] == 2

    warnings = verify.verify_report(conn).warnings
    assert any("has no live family" in w for w in warnings)
    assert not any("names no live row" in w for w in warnings)


def test_re_annotating_a_row_after_a_rekey_collapses_the_stale_breadcrumb(seeded):
    """The delete-by-(record_type, record_id) write path: the second verdict carries the
    new base, and the row must still hold exactly one."""
    conn = seeded["conn"]
    base = _second_occurrence(seeded)
    _occ0, occ1 = _occurrence_ids(conn, base)
    curation.annotate_record(conn, "lab_result", str(occ1), status="disputed",
                            note="before", row=True, apply=True)
    dedup.rekey(conn, {"glucose": "blood-sugar"}, apply=True)
    new_base = _base(conn, "lab_result", "test_name", "Glucose")

    curation.annotate_record(conn, "lab_result", str(occ1), status="superseded",
                            note="after", row=True, apply=True)

    assert _curation_count(conn) == 1
    live = curation.get_verdict(conn, "lab_result", "", record_id=occ1)
    assert live["dedup_base"] == new_base and live["note"] == "after"


# --- the rekey collision seam (issue #116) -----------------------------------

def _fusing_ids(conn):
    """``(HbA1c id, Glucose id)`` — the pair `{"glucose": "hba1c"}` fuses.

    `rekey` scans by primary key, so HbA1c is the incumbent that keeps occurrence 0 and
    Glucose is the clash a verdict has to settle."""
    return (_row_id(conn, "lab_result", "test_name", "HbA1c"),
            _row_id(conn, "lab_result", "test_name", "Glucose"))


def test_resolving_statuses_are_only_merged_into_and_superseded():
    """The constant guard. `confirmed`/`disputed`/`erroneous-in-source` rule on a row's
    content; only these two say two rows are one fact, which is what a collision asks."""
    assert curation.RESOLVING_STATUSES == ("merged-into", "superseded")
    assert set(curation.RESOLVING_STATUSES) <= set(curation.STATUSES)


def test_a_resolving_family_verdict_is_narrowed_to_the_rows_it_covered(seeded):
    """AC5, as re-decided: the verdict that authorized the rekey is pinned to exactly the
    rows it already covered — by UPDATE, so the human's words and timestamp survive
    verbatim — instead of following its family onto the (larger) surviving base. Nothing
    is orphaned, and the row it never judged is left unruled."""
    conn = seeded["conn"]
    hba1c_id, glucose_id = _fusing_ids(conn)
    glucose_base = _base(conn, "lab_result", "test_name", "Glucose")
    hba1c_base = _base(conn, "lab_result", "test_name", "HbA1c")
    curation.annotate_record(conn, "lab_result", glucose_base, status="superseded",
                            note="restated as the A1c", attributed_to="Dr Who",
                            now="2026-01-01T00:00:00+00:00", apply=True)

    report = dedup.rekey(conn, {"glucose": "hba1c"}, apply=True,
                         resolver=curation.collision_resolver(conn))

    assert [r.verdict_action for r in report.resolved] == ["narrowed"]
    assert [r.narrowed_row_ids for r in report.resolved] == [[glucose_id]]
    assert [r.covered_row_ids for r in report.resolved] == [[glucose_id]]
    assert _curation_count(conn) == 1               # narrowed, not copied
    pinned = curation.get_verdict(conn, "lab_result", "", record_id=glucose_id)
    assert (pinned["status"], pinned["note"], pinned["attributed_to"],
            pinned["created_at"]) == ("superseded", "restated as the A1c", "Dr Who",
                                      "2026-01-01T00:00:00+00:00")
    assert pinned["scope"] == "row"
    # No family verdict anywhere: not on the dead base, not on the surviving one.
    assert curation.get_verdict(conn, "lab_result", glucose_base) is None
    assert curation.get_verdict(conn, "lab_result", hba1c_base) is None
    # ... so the row that was never judged carries no verdict at all.
    assert curation.load_verdicts(conn).for_row(
        "lab_result", hba1c_base, hba1c_id) is None
    # Nothing orphaned; the narrowing announces itself as a re-affirm notice instead.
    warnings = verify.verify_report(conn).warnings
    assert not any("has no live family" in w for w in warnings)
    assert [w for w in warnings if "a rekey moved it" in w]


def test_a_verdict_on_the_surviving_family_is_untouched_by_the_narrowing(seeded):
    """Never destroy a human ruling: the incumbent on the surviving base is not
    rewritten or cleared — narrowing writes only the resolver, row-scoped, and row scope
    is what keeps the incumbent off those rows."""
    conn = seeded["conn"]
    _hba1c_id, glucose_id = _fusing_ids(conn)
    glucose_base = _base(conn, "lab_result", "test_name", "Glucose")
    hba1c_base = _base(conn, "lab_result", "test_name", "HbA1c")
    curation.annotate_record(conn, "lab_result", hba1c_base, status="confirmed",
                            note="incumbent", now="2026-01-01T00:00:00+00:00",
                            apply=True)
    curation.annotate_record(conn, "lab_result", glucose_base, status="superseded",
                            note="the resolver", now="2026-02-01T00:00:00+00:00",
                            apply=True)

    report = dedup.rekey(conn, {"glucose": "hba1c"}, apply=True,
                         resolver=curation.collision_resolver(conn))

    assert [r.verdict_action for r in report.resolved] == ["narrowed"]
    assert _curation_count(conn) == 2               # nothing deleted, nothing added
    incumbent = curation.get_verdict(conn, "lab_result", hba1c_base)
    assert (incumbent["status"], incumbent["note"], incumbent["created_at"]) == \
        ("confirmed", "incumbent", "2026-01-01T00:00:00+00:00")
    resolver_verdict = curation.get_verdict(
        conn, "lab_result", "", record_id=glucose_id)
    assert resolver_verdict["note"] == "the resolver"
    # Precedence, not deletion, keeps the incumbent off the row the resolver ruled on.
    assert curation.load_verdicts(conn).for_row(
        "lab_result", hba1c_base, glucose_id)["note"] == "the resolver"


def test_a_resolved_merge_verdict_follows_a_target_family_that_also_moved(seeded):
    """A merge pointer is re-pointed onto the narrowed verdict, through the same run's
    base map: the target family moved under this very dictionary edit, and carrying the
    dead pointer over would trade one `verify` warning for another."""
    conn = seeded["conn"]
    _hba1c_id, glucose_id = _fusing_ids(conn)
    doc2 = _insert_document(conn, seeded["jane"].person_id, "cc33dd44ee55ff66")
    dedup.commit_extraction(conn, doc2, {"lab_result": [
        {"test_name": "ZZT", "collected_at": "2026-01-02", "value_num": 108}]})
    glucose_base = _base(conn, "lab_result", "test_name", "Glucose")
    zzt_base = _base(conn, "lab_result", "test_name", "ZZT")
    curation.annotate_record(conn, "lab_result", glucose_base, status="merged-into",
                            note="filed under the zonulin panel",
                            merged_into_base=zzt_base, apply=True)

    report = dedup.rekey(conn, {"glucose": "hba1c", "zzt": "zonulin"}, apply=True,
                         resolver=curation.collision_resolver(conn))

    assert [r.verdict_action for r in report.resolved] == ["narrowed"]
    new_zzt = _base(conn, "lab_result", "test_name", "ZZT")
    assert new_zzt != zzt_base                       # the target moved in this same run
    pinned = curation.get_verdict(conn, "lab_result", "", record_id=glucose_id)
    assert pinned["merged_into_base"] == new_zzt
    assert not any("no live family" in w
                   for w in verify.verify_report(conn).warnings)


def test_a_merge_verdict_whose_target_is_the_surviving_family_completes_the_merge(
    seeded
):
    """The common shape of the real trigger: the pair is merged into *each other*, so
    after the rekey the narrowed verdict's row sits in the very family it merges into.

    That is the merge having actually completed. Nothing consumes `merged_into_base` for
    placement (`APPENDIX_STATUSES` does that), `verify` sees a live target, and the
    alternative — leaving the pointer on the dead base — is the orphan warning AC5
    forbids."""
    conn = seeded["conn"]
    _hba1c_id, glucose_id = _fusing_ids(conn)
    glucose_base = _base(conn, "lab_result", "test_name", "Glucose")
    hba1c_base = _base(conn, "lab_result", "test_name", "HbA1c")
    curation.annotate_record(conn, "lab_result", glucose_base, status="merged-into",
                            note="the same draw, restated", merged_into_base=hba1c_base,
                            apply=True)

    report = dedup.rekey(conn, {"glucose": "hba1c"}, apply=True,
                         resolver=curation.collision_resolver(conn))

    assert [r.verdict_action for r in report.resolved] == ["narrowed"]
    pinned = curation.get_verdict(conn, "lab_result", "", record_id=glucose_id)
    assert pinned["merged_into_base"] == hba1c_base
    assert _base(conn, "lab_result", "test_name", "Glucose") == hba1c_base
    assert not any("no live family" in w
                   for w in verify.verify_report(conn).warnings)


def test_the_re_affirm_notice_can_be_followed_for_a_narrowed_merge_verdict(seeded):
    """The narrowing is only "loud" if the human can act on what it says. `verify` tells
    them to re-affirm the moved row verdict; for `merged-into` the same rekey has already
    put that row *into* its merge target, so the re-affirm has no third family to name.
    Row scope must therefore accept the row's own live family as the target — otherwise
    the notice is permanent and the only exits are downgrading or lifting a human
    ruling, which the #116 ruling forbids."""
    conn = seeded["conn"]
    hba1c_id, glucose_id = _fusing_ids(conn)
    glucose_base = _base(conn, "lab_result", "test_name", "Glucose")
    hba1c_base = _base(conn, "lab_result", "test_name", "HbA1c")
    curation.annotate_record(conn, "lab_result", glucose_base, status="merged-into",
                             note="the same draw, restated", merged_into_base=hba1c_base,
                             apply=True)
    dedup.rekey(conn, {"glucose": "hba1c"}, apply=True,
                resolver=curation.collision_resolver(conn))
    surviving = _base(conn, "lab_result", "test_name", "Glucose")
    assert [w for w in verify.verify_report(conn).warnings if "a rekey moved it" in w]

    # The literal follow-up the notice asks for: re-affirm this row, same ruling.
    report = curation.annotate_record(
        conn, "lab_result", str(glucose_id), status="merged-into",
        note="re-affirmed after the rekey", merged_into_base=surviving,
        row=True, apply=True,
    )

    assert (report.scope, report.record_id) == ("row", glucose_id)
    assert report.merged_into_base == surviving
    live = curation.get_verdict(conn, "lab_result", "", record_id=glucose_id)
    assert live["dedup_base"] == surviving          # breadcrumb collapsed
    assert live["note"] == "re-affirmed after the rekey"
    assert _curation_count(conn) == 1               # re-affirmed, not duplicated
    # ... and the notice it was meant to clear is gone, with no orphan taking its place.
    warnings = verify.verify_report(conn).warnings
    assert not any("a rekey moved it" in w or "no live family" in w for w in warnings)
    # The row never judged is still unruled and still live in its own section.
    assert curation.load_verdicts(conn).for_row(
        "lab_result", surviving, hba1c_id) is None


def test_a_family_verdict_still_cannot_merge_into_its_own_family(seeded):
    """The relaxation above is row-scoped only. At family scope a self-merge still hides
    the fact entirely — there is no sibling left to render it — so it stays refused."""
    conn = seeded["conn"]
    glucose_base = _base(conn, "lab_result", "test_name", "Glucose")
    with pytest.raises(ValueError, match="into itself"):
        curation.annotate_record(
            conn, "lab_result", glucose_base, status="merged-into",
            note="x", merged_into_base=glucose_base, apply=True,
        )
    assert _curation_count(conn) == 0


def test_a_row_verdict_may_be_merged_into_a_sibling_row_id(seeded):
    """`--merged-into` takes BASE-OR-ID, and after a narrowing the only sibling an
    operator can see is a row id of the same surviving family. That resolves to the row's
    own base, i.e. the case above, and must be accepted by the same rule."""
    conn = seeded["conn"]
    hba1c_id, glucose_id = _fusing_ids(conn)
    glucose_base = _base(conn, "lab_result", "test_name", "Glucose")
    hba1c_base = _base(conn, "lab_result", "test_name", "HbA1c")
    curation.annotate_record(conn, "lab_result", glucose_base, status="merged-into",
                             note="the same draw, restated", merged_into_base=hba1c_base,
                             apply=True)
    dedup.rekey(conn, {"glucose": "hba1c"}, apply=True,
                resolver=curation.collision_resolver(conn))

    report = curation.annotate_record(
        conn, "lab_result", str(glucose_id), status="merged-into",
        note="absorbed by the sibling", merged_into_base=str(hba1c_id),
        row=True, apply=True,
    )

    assert report.merged_into_base == _base(conn, "lab_result", "test_name", "HbA1c")
    assert not any("no live family" in w
                   for w in verify.verify_report(conn).warnings)


def test_a_row_scoped_verdict_needs_no_narrowing(seeded):
    """Row scope resolves by row id, which `rekey` never renumbers, and it already names
    exactly one row — so there is nothing to narrow, and its stored base stays the
    deliberately stale breadcrumb 010 describes."""
    conn = seeded["conn"]
    _hba1c_id, glucose_id = _fusing_ids(conn)
    glucose_base = _base(conn, "lab_result", "test_name", "Glucose")
    curation.annotate_record(conn, "lab_result", str(glucose_id), status="superseded",
                            note="this occurrence only", row=True, apply=True)

    report = dedup.rekey(conn, {"glucose": "hba1c"}, apply=True,
                         resolver=curation.collision_resolver(conn))

    assert [r.verdict_action for r in report.resolved] == ["unchanged"]
    assert [r.narrowed_row_ids for r in report.resolved] == [[]]
    live = curation.get_verdict(conn, "lab_result", "", record_id=glucose_id)
    assert live["dedup_base"] == glucose_base
    new_base = _base(conn, "lab_result", "test_name", "Glucose")
    assert new_base != glucose_base
    assert curation.load_verdicts(conn).for_row(
        "lab_result", new_base, glucose_id)["status"] == "superseded"
    assert not any("no live family" in w
                   for w in verify.verify_report(conn).warnings)


def test_a_row_verdict_shadows_its_familys_resolving_verdict(seeded):
    """`covering` resolves through :meth:`VerdictMap.for_row` rather than adding a second
    precedence site: a row-scoped non-resolving verdict shadows the resolving family
    verdict on the same row, so the collision still blocks."""
    conn = seeded["conn"]
    _hba1c_id, glucose_id = _fusing_ids(conn)
    glucose_base = _base(conn, "lab_result", "test_name", "Glucose")
    curation.annotate_record(conn, "lab_result", glucose_base, status="superseded",
                            note="the family is settled", apply=True)
    curation.annotate_record(conn, "lab_result", str(glucose_id), status="disputed",
                            note="but not this row", row=True, apply=True)

    report = dedup.rekey(conn, {"glucose": "hba1c"}, apply=True,
                         resolver=curation.collision_resolver(conn))

    assert report.resolved == []
    assert [c.kind for c in report.collisions] == ["fused"]


# --- verify integration ------------------------------------------------------


def test_verify_warns_about_an_orphaned_verdict_without_failing(seeded):
    conn = seeded["conn"]
    base = _base(conn, "lab_result", "test_name", "Glucose")
    curation.annotate_record(conn, "lab_result", base, status="superseded",
                            note="gone", apply=True)
    assert verify.verify_report(conn).warnings == []

    records.remove_record(
        conn, "lab_result", _row_id(conn, "lab_result", "test_name", "Glucose"),
        apply=True,
    )
    report = verify.verify_report(conn)
    assert report.ok is True and report.problems == []
    assert any("has no live family" in w for w in report.warnings)
    assert "warnings" in report.as_dict()
    assert any("warnings       1" in line for line in verify.format_report(report))


def test_verify_is_silent_about_warnings_when_there_are_none(seeded):
    """The console block only appears when non-empty, so a clean database's output is
    unchanged by this feature."""
    report = verify.verify_report(seeded["conn"])
    assert report.warnings == []
    assert not any(line.startswith("warnings") for line in verify.format_report(report))


def test_verify_warns_about_an_orphaned_row_scoped_verdict(seeded):
    """AC 7: the row-scope twin of the family orphan warning - the **backstop**.

    Since the row-id-reuse fix, the two write paths that delete a record row retire the
    verdict themselves, so this state can only be reached some other way (a hand-edited
    or restored database). It still has to be named, and named by row, because `--clear`
    needs `--row` to lift it.
    """
    conn = seeded["conn"]
    base = _second_occurrence(seeded)
    _occ0, occ1 = _occurrence_ids(conn, base)
    curation.annotate_record(conn, "lab_result", str(occ1), status="superseded",
                            note="loser", row=True, apply=True)
    assert verify.verify_report(conn).warnings == []

    # Straight to the table, behind the write paths' backs - the only way here now.
    conn.execute("DELETE FROM lab_result WHERE lab_result_id = ?", (occ1,))
    conn.commit()

    report = verify.verify_report(conn)
    assert report.ok is True and report.problems == []
    assert any("row #" in w and "names no live row" in w for w in report.warnings)
    # The sibling family is still live, so the *family* warning must not fire.
    assert not any("has no live family" in w for w in report.warnings)
    # And the orphan is liftable without a live row to name it by.
    curation.clear_curation(conn, "lab_result", str(occ1), row=True, apply=True)
    assert _curation_count(conn) == 0


# --- row ids are reusable, so removing the row retires its verdict (issue #114) ---


def _max_row_id(conn, record_type):
    return int(conn.execute(
        f"SELECT MAX({record_type}_id) AS n FROM {record_type}"
    ).fetchone()["n"])


def test_record_rm_retires_the_row_verdict_and_keeps_the_family_one(seeded):
    conn = seeded["conn"]
    base = _second_occurrence(seeded)
    _occ0, occ1 = _occurrence_ids(conn, base)
    curation.annotate_record(conn, "lab_result", base, status="disputed",
                            note="two sources disagree", apply=True)
    curation.annotate_record(conn, "lab_result", str(occ1), row=True,
                            status="superseded", note="the keep-both loser", apply=True)

    report = records.remove_record(conn, "lab_result", occ1, apply=True)

    assert [v["record_id"] for v in report.curation_retired] == [occ1]
    assert curation.get_verdict(conn, "lab_result", "", record_id=occ1) is None
    # The family verdict survives on purpose: dedup_base is content-derived, so it has
    # no reuse hazard and occurrence 0 is still there for it to rule on.
    family = curation.get_verdict(conn, "lab_result", base)
    assert family is not None and family["status"] == "disputed"
    assert verify.verify_report(conn).warnings == []


def test_record_rm_dry_run_names_the_verdict_it_would_lift(seeded):
    """The dry run is `record rm`'s whole safety mechanism, and a recorded clinical
    ruling going away is part of the blast radius it promises to be truthful about."""
    conn = seeded["conn"]
    base = _second_occurrence(seeded)
    _occ0, occ1 = _occurrence_ids(conn, base)
    curation.annotate_record(conn, "lab_result", str(occ1), row=True,
                            status="superseded", note="the keep-both loser", apply=True)

    report = records.remove_record(conn, "lab_result", occ1)

    assert report.applied is False
    assert [v["note"] for v in report.curation_retired] == ["the keep-both loser"]
    assert curation.get_verdict(conn, "lab_result", "", record_id=occ1) is not None


def test_a_reused_row_id_does_not_inherit_the_removed_rows_verdict(seeded):
    """The regression this whole retirement rule exists for.

    Record ids are plain rowid aliases (INTEGER PRIMARY KEY, no AUTOINCREMENT), so
    deleting the highest-id row hands that id to the next insert. Before the fix, the
    stale row-scoped verdict re-attached to the new, unrelated record - and `verify`
    went *quiet* about it, because the id resolved again.
    """
    conn = seeded["conn"]
    glucose = _row_id(conn, "lab_result", "test_name", "Glucose")
    assert glucose == _max_row_id(conn, "lab_result")   # the reuse precondition
    curation.annotate_record(conn, "lab_result", str(glucose), row=True,
                            status="superseded", note="loser of a keep-both", apply=True)

    records.remove_record(conn, "lab_result", glucose, apply=True)
    doc2 = _insert_document(conn, seeded["jane"].person_id, "cc33dd44ee55ff66")
    dedup.commit_extraction(conn, doc2, {"lab_result": [
        {"test_name": "Creatinine", "collected_at": "2026-02-02", "value_num": 0.9,
         "unit": "mg/dL"},
    ]})
    recycled = _row_id(conn, "lab_result", "test_name", "Creatinine")
    assert recycled == glucose          # SQLite handed the id straight back

    verdicts = curation.load_verdicts(conn)
    base = _base(conn, "lab_result", "test_name", "Creatinine")
    assert verdicts.for_row("lab_result", base, recycled) is None
    assert _curation_count(conn) == 0
    assert not any("curation" in w for w in verify.verify_report(conn).warnings)


def test_document_rm_retires_the_row_verdicts_of_the_rows_it_deletes(seeded):
    """`document rm` cascades to every row the document produced, freeing every one of
    those ids - the same hazard as `record rm`, at document scale.

    Two *different* record types carry a row verdict here on purpose:
    :func:`documents._doomed_row_verdicts` sweeps ``dedup.KNOWN_TYPES``, and a sweep
    narrowed to one type would leave the freed ids of every other type unguarded while
    still passing a single-type assertion.
    """
    conn = seeded["conn"]
    glucose = _row_id(conn, "lab_result", "test_name", "Glucose")
    prediabetes = _row_id(conn, "condition", "name", "Prediabetes")
    cond_base = _base(conn, "condition", "name", "Type 2 Diabetes")
    curation.annotate_record(conn, "lab_result", str(glucose), row=True,
                            status="superseded", note="row scope", apply=True)
    curation.annotate_record(conn, "condition", str(prediabetes), row=True,
                            status="erroneous-in-source", note="row scope, other type",
                            apply=True)
    curation.annotate_record(conn, "condition", cond_base, status="disputed",
                            note="family scope", apply=True)

    def _retired(report):
        return sorted(
            (v["record_type"], v["record_id"]) for v in report.curation_retired
        )

    expected = sorted(
        [("lab_result", glucose), ("condition", prediabetes)]
    )

    dry = documents.remove_document(conn, seeded["doc"])
    assert _retired(dry) == expected
    assert curation.get_verdict(conn, "lab_result", "", record_id=glucose) is not None
    assert curation.get_verdict(
        conn, "condition", "", record_id=prediabetes
    ) is not None

    report = documents.remove_document(conn, seeded["doc"], apply=True)

    assert _retired(report) == expected
    assert curation.get_verdict(conn, "lab_result", "", record_id=glucose) is None
    assert curation.get_verdict(conn, "condition", "", record_id=prediabetes) is None
    # The family verdict outlives the cascade: `dedup_base` is content-derived, so a
    # re-ingest of the same document re-attaches it, which is the point of family scope.
    assert curation.get_verdict(conn, "condition", cond_base) is not None


def test_verify_warns_about_a_dangling_merge_target(seeded):
    conn = seeded["conn"]
    base = _base(conn, "lab_result", "test_name", "Glucose")
    target = _base(conn, "lab_result", "test_name", "HbA1c")
    curation.annotate_record(conn, "lab_result", base, status="merged-into",
                            note="same draw", merged_into_base=target, apply=True)
    records.remove_record(
        conn, "lab_result", _row_id(conn, "lab_result", "test_name", "HbA1c"),
        apply=True,
    )
    report = verify.verify_report(conn)
    assert report.ok is True
    assert any("merges into" in w for w in report.warnings)


def test_verify_warns_about_a_hand_edited_unknown_record_type(seeded):
    """A read-path re-validation: the stored type must never be interpolated into a
    query just because it is in the table."""
    conn = seeded["conn"]
    conn.execute(
        "INSERT INTO curation (record_type, dedup_base, status, note, created_at) "
        "VALUES ('family_history', 'b', 'disputed', 'hand-edited', "
        "'2026-01-01T00:00:00')"
    )
    conn.commit()
    report = verify.verify_report(conn)
    assert report.ok is True
    assert any("unknown record type" in w for w in report.warnings)


# --- CLI surface -------------------------------------------------------------


def _run(tmp_path, *argv):
    return cli.main(["--db", str(tmp_path / "cli.db"), *argv])


@pytest.fixture()
def cli_ready(tmp_path):
    """Migrated DB, one person, one ingested+committed multi-record document."""
    assert _run(tmp_path, "migrate", "--create") == 0
    assert _run(tmp_path, "person", "add", "--slug", "jane-doe", "--name", "Jane") == 0
    scan = tmp_path / "scan.txt"
    scan.write_bytes(b"visit note: hba1c 5.7 percent, glucose 95")
    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources"), "--ocr-text-file",
                str(scan)) == 0
    payload = tmp_path / "extract.json"
    payload.write_text(json.dumps(RECORDS), encoding="utf-8")
    assert _run(tmp_path, "commit-extraction", "--document", "1",
                "--json", str(payload)) == 0
    return tmp_path


def _cli_conn(tmp_path):
    return db.connect(tmp_path / "cli.db")


def _cli_row_id(tmp_path, record_type, column, value):
    conn = _cli_conn(tmp_path)
    try:
        return _row_id(conn, record_type, column, value)
    finally:
        conn.close()


def _cli_curation_count(tmp_path):
    conn = _cli_conn(tmp_path)
    try:
        return _curation_count(conn)
    finally:
        conn.close()


def test_cli_annotate_dry_run_then_apply(cli_ready, capsys):
    target = _cli_row_id(cli_ready, "lab_result", "test_name", "Glucose")
    capsys.readouterr()
    assert _run(cli_ready, "record", "annotate", "lab_result", str(target),
                "--status", "disputed", "--note", "two sources disagree") == 0
    out = capsys.readouterr().out
    assert "lab_result  Glucose" in out
    assert "status: disputed" in out
    assert "note: two sources disagree" in out
    assert "dry run: nothing was written - re-run with --apply" in out
    assert _cli_curation_count(cli_ready) == 0

    assert _run(cli_ready, "record", "annotate", "lab_result", str(target),
                "--status", "disputed", "--note", "two sources disagree",
                "--attributed-to", "Dr Who", "--apply") == 0
    out = capsys.readouterr().out
    assert "attributed to: Dr Who" in out
    assert "annotated lab_result" in out and "(create)" in out
    assert _cli_curation_count(cli_ready) == 1


def test_cli_annotate_json_shape_is_stable(cli_ready, capsys):
    target = _cli_row_id(cli_ready, "lab_result", "test_name", "Glucose")
    expected = {
        "record_type", "dedup_base", "status", "note", "attributed_to",
        "created_at", "merged_into_base", "label", "family_size", "previous",
        "action", "applied", "record_id", "scope",
    }
    capsys.readouterr()
    assert _run(cli_ready, "record", "annotate", "lab_result", str(target),
                "--status", "confirmed", "--note", "agreed", "--json") == 0
    dry = json.loads(capsys.readouterr().out)
    assert set(dry) == expected
    assert dry["applied"] is False and dry["action"] == "create"
    assert dry["previous"] is None and dry["label"] == "Glucose"

    assert _run(cli_ready, "record", "annotate", "lab_result", str(target),
                "--status", "confirmed", "--note", "agreed", "--json",
                "--apply") == 0
    applied = json.loads(capsys.readouterr().out)
    assert set(applied) == expected and applied["applied"] is True


def test_cli_annotate_list_with_and_without_a_table_filter(cli_ready, capsys):
    lab = _cli_row_id(cli_ready, "lab_result", "test_name", "Glucose")
    cond = _cli_row_id(cli_ready, "condition", "name", "Prediabetes")
    assert _run(cli_ready, "record", "annotate", "lab_result", str(lab),
                "--status", "disputed", "--note", "a", "--apply") == 0
    assert _run(cli_ready, "record", "annotate", "condition", str(cond),
                "--status", "superseded", "--note", "b", "--apply") == 0

    capsys.readouterr()
    assert _run(cli_ready, "record", "annotate", "--list") == 0
    out = capsys.readouterr().out
    assert "Glucose" in out and "Prediabetes" in out

    assert _run(cli_ready, "record", "annotate", "--list", "lab_result",
                "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["record_type"] for r in rows] == ["lab_result"]
    assert rows[0]["label"] == "Glucose" and rows[0]["family_size"] == 1


def test_cli_annotate_list_is_friendly_when_empty(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "record", "annotate", "--list") == 0
    assert "no curation verdicts recorded" in capsys.readouterr().out


def test_cli_annotate_clear(cli_ready, capsys):
    target = _cli_row_id(cli_ready, "lab_result", "test_name", "Glucose")
    assert _run(cli_ready, "record", "annotate", "lab_result", str(target),
                "--status", "confirmed", "--note", "agreed", "--apply") == 0
    capsys.readouterr()
    assert _run(cli_ready, "record", "annotate", "lab_result", str(target),
                "--clear") == 0
    assert "dry run: nothing was written" in capsys.readouterr().out
    assert _cli_curation_count(cli_ready) == 1

    assert _run(cli_ready, "record", "annotate", "lab_result", str(target),
                "--clear", "--apply") == 0
    assert "cleared curation verdict for lab_result" in capsys.readouterr().out
    assert _cli_curation_count(cli_ready) == 0


def test_cli_annotate_requires_table_and_target_unless_list(cli_ready):
    with pytest.raises(SystemExit) as exc:
        _run(cli_ready, "record", "annotate", "lab_result")
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        _run(cli_ready, "record", "annotate", "--list", "lab_result", "1")
    assert exc.value.code == 2


def test_cli_annotate_requires_status_and_note(cli_ready):
    target = _cli_row_id(cli_ready, "lab_result", "test_name", "Glucose")
    with pytest.raises(SystemExit) as exc:
        _run(cli_ready, "record", "annotate", "lab_result", str(target))
    assert exc.value.code == 2


def test_cli_annotate_unknown_target_fails_friendly(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "record", "annotate", "lab_result", "9999",
                "--status", "disputed", "--note", "x", "--apply") == 1
    assert "error: no lab_result with id 9999" in capsys.readouterr().err
    assert _cli_curation_count(cli_ready) == 0


def test_cli_annotate_empty_note_fails_friendly(cli_ready, capsys):
    target = _cli_row_id(cli_ready, "lab_result", "test_name", "Glucose")
    capsys.readouterr()
    assert _run(cli_ready, "record", "annotate", "lab_result", str(target),
                "--status", "disputed", "--note", "   ", "--apply") == 1
    assert "error: a curation note is required" in capsys.readouterr().err
    assert _cli_curation_count(cli_ready) == 0


def test_cli_annotate_unknown_table_is_argparse_misuse(cli_ready):
    with pytest.raises(SystemExit) as exc:
        _run(cli_ready, "record", "annotate", "family_history", "1",
             "--status", "disputed", "--note", "x")
    assert exc.value.code == 2


def test_cli_annotate_clear_without_a_verdict_fails_friendly(cli_ready, capsys):
    target = _cli_row_id(cli_ready, "lab_result", "test_name", "Glucose")
    capsys.readouterr()
    assert _run(cli_ready, "record", "annotate", "lab_result", str(target),
                "--clear", "--apply") == 1
    assert "error: no curation verdict" in capsys.readouterr().err


# --- CLI surface: row scope (issue #114) -------------------------------------


def _cli_second_occurrence(tmp_path):
    """Re-file the CLI fixture's Glucose fact as occurrence 1, and return
    ``(base, occurrence 0 id, occurrence 1 id)`` — the keep-both shape."""
    conn = _cli_conn(tmp_path)
    try:
        base = _base(conn, "lab_result", "test_name", "Glucose")
        person_id = int(conn.execute(
            "SELECT person_id FROM lab_result WHERE dedup_base = ?", (base,)
        ).fetchone()["person_id"])
        doc2 = _insert_document(conn, person_id, "bb22cc33dd44ee55")
        conn.execute(
            "INSERT INTO lab_result (person_id, document_id, test_name, collected_at, "
            "value_num, unit, dedup_key, dedup_base, dedup_occurrence) "
            "VALUES (?, ?, 'Glucose', '2026-01-02', 95, 'mg/dL', ?, ?, 1)",
            (person_id, doc2, dedup.occurrence_key(base, 1), base),
        )
        conn.commit()
        return (base, *_occurrence_ids(conn, base))
    finally:
        conn.close()


def test_cli_annotate_row_dry_run_then_apply(cli_ready, capsys):
    _base_hex, _occ0, occ1 = _cli_second_occurrence(cli_ready)
    capsys.readouterr()
    assert _run(cli_ready, "record", "annotate", "lab_result", str(occ1), "--row",
                "--status", "superseded", "--note", "loser of a keep-both") == 0
    out = capsys.readouterr().out
    assert f"scope: row #{occ1}" in out
    assert "dry run: nothing was written - re-run with --apply" in out
    assert _cli_curation_count(cli_ready) == 0

    assert _run(cli_ready, "record", "annotate", "lab_result", str(occ1), "--row",
                "--status", "superseded", "--note", "loser of a keep-both",
                "--apply") == 0
    out = capsys.readouterr().out
    assert f"annotated lab_result row #{occ1} (create)" in out
    assert _cli_curation_count(cli_ready) == 1


def test_cli_annotate_row_json_carries_scope(cli_ready, capsys):
    _base_hex, _occ0, occ1 = _cli_second_occurrence(cli_ready)
    capsys.readouterr()
    assert _run(cli_ready, "record", "annotate", "lab_result", str(occ1), "--row",
                "--status", "disputed", "--note", "one occurrence only", "--json",
                "--apply") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"] == "row" and payload["record_id"] == occ1

    assert _run(cli_ready, "record", "annotate", "--list", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["scope"], r["record_id"]) for r in rows] == [("row", occ1)]


def test_cli_annotate_list_shows_the_scope_column(cli_ready, capsys):
    _base_hex, _occ0, occ1 = _cli_second_occurrence(cli_ready)
    cond = _cli_row_id(cli_ready, "condition", "name", "Prediabetes")
    assert _run(cli_ready, "record", "annotate", "lab_result", str(occ1), "--row",
                "--status", "superseded", "--note", "a", "--apply") == 0
    assert _run(cli_ready, "record", "annotate", "condition", str(cond),
                "--status", "disputed", "--note", "b", "--apply") == 0
    capsys.readouterr()
    assert _run(cli_ready, "record", "annotate", "--list") == 0
    out = capsys.readouterr().out
    assert "scope" in out.splitlines()[0]
    assert f"row #{occ1}" in out and "family" in out


def test_cli_annotate_clear_row_leaves_the_family_verdict(cli_ready, capsys):
    base, _occ0, occ1 = _cli_second_occurrence(cli_ready)
    assert _run(cli_ready, "record", "annotate", "lab_result", base,
                "--status", "disputed", "--note", "family", "--apply") == 0
    assert _run(cli_ready, "record", "annotate", "lab_result", str(occ1), "--row",
                "--status", "superseded", "--note", "row", "--apply") == 0
    capsys.readouterr()

    assert _run(cli_ready, "record", "annotate", "lab_result", str(occ1), "--row",
                "--clear", "--apply") == 0
    assert f"cleared curation verdict for lab_result row #{occ1}" in (
        capsys.readouterr().out
    )
    assert _cli_curation_count(cli_ready) == 1


def test_cli_annotate_row_misuse_is_friendly(cli_ready, capsys):
    base, _occ0, _occ1 = _cli_second_occurrence(cli_ready)
    with pytest.raises(SystemExit) as exc:
        _run(cli_ready, "record", "annotate", "--list", "--row")
    assert exc.value.code == 2

    capsys.readouterr()
    assert _run(cli_ready, "record", "annotate", "lab_result", base, "--row",
                "--status", "disputed", "--note", "x", "--apply") == 1
    assert "drop --row" in capsys.readouterr().err

    assert _run(cli_ready, "record", "annotate", "lab_result", "9999", "--row",
                "--status", "disputed", "--note", "x", "--apply") == 1
    assert "no lab_result with id 9999" in capsys.readouterr().err
    assert _cli_curation_count(cli_ready) == 0


def test_cli_record_rm_names_and_lifts_the_row_verdict(cli_ready, capsys):
    target = _cli_row_id(cli_ready, "lab_result", "test_name", "Glucose")
    assert _run(cli_ready, "record", "annotate", "lab_result", str(target), "--row",
                "--status", "superseded", "--note", "loser of a keep-both",
                "--apply") == 0
    capsys.readouterr()

    assert _run(cli_ready, "record", "rm", "lab_result", str(target)) == 0
    out = capsys.readouterr().out
    assert "curation verdict (row scope) would be lifted" in out
    assert "loser of a keep-both" in out
    assert _cli_curation_count(cli_ready) == 1        # dry run wrote nothing

    assert _run(cli_ready, "record", "rm", "lab_result", str(target), "--apply") == 0
    assert "curation verdict (row scope) lifted" in capsys.readouterr().out
    assert _cli_curation_count(cli_ready) == 0


def test_cli_a_reused_row_id_is_not_filed_under_the_superseded_appendix(
    cli_ready, capsys
):
    """The end-to-end shape of the regression: annotate the highest-id row, remove it,
    ingest something else onto the recycled id, and the new fact must render in its own
    section rather than under a stranger's verdict."""
    target = _cli_row_id(cli_ready, "condition", "name", "Prediabetes")
    assert _run(cli_ready, "record", "annotate", "condition", str(target), "--row",
                "--status", "superseded", "--note", "loser of a keep-both",
                "--apply") == 0
    assert _run(cli_ready, "record", "rm", "condition", str(target), "--apply") == 0

    scan2 = cli_ready / "scan2.txt"
    scan2.write_bytes(b"visit note: anaphylaxis, epinephrine plan in place")
    assert _run(cli_ready, "ingest", str(scan2), "--person", "jane-doe",
                "--sources", str(cli_ready / "sources"), "--ocr-text-file",
                str(scan2)) == 0
    payload = cli_ready / "extract2.json"
    payload.write_text(json.dumps({"condition": [
        {"name": "Anaphylaxis - epinephrine plan", "status": "active",
         "onset_on": "2025-05-05"},
    ]}), encoding="utf-8")
    assert _run(cli_ready, "commit-extraction", "--document", "2",
                "--json", str(payload)) == 0
    recycled = _cli_row_id(cli_ready, "condition", "name",
                           "Anaphylaxis - epinephrine plan")
    assert recycled == target       # SQLite handed the id straight back

    capsys.readouterr()
    assert _run(cli_ready, "render", "summary", "--person", "jane-doe") == 0
    summary = capsys.readouterr().out
    assert "Anaphylaxis - epinephrine plan" in summary
    assert "## Superseded / corrected" not in summary
    assert "loser of a keep-both" not in summary

    capsys.readouterr()
    assert _run(cli_ready, "verify") == 0
    out = capsys.readouterr().out
    assert "names no live row" not in out
    assert not any(line.startswith("warnings") for line in out.splitlines())


# --- the integration drill from the issue ------------------------------------


def test_cli_annotate_render_remove_verify_drill(cli_ready, capsys):
    """annotate --apply -> render summary shows the appendix -> record rm the whole
    family -> verify warns about the orphan and still exits ok."""
    target = _cli_row_id(cli_ready, "lab_result", "test_name", "Glucose")
    assert _run(cli_ready, "record", "annotate", "lab_result", str(target),
                "--status", "superseded", "--note", "repeat draw supersedes it",
                "--apply") == 0

    capsys.readouterr()
    assert _run(cli_ready, "render", "summary", "--person", "jane-doe") == 0
    summary = capsys.readouterr().out
    assert "## Superseded / corrected" in summary
    assert "lab_result: Glucose" in summary
    assert "repeat draw supersedes it" in summary

    assert _run(cli_ready, "record", "rm", "lab_result", str(target), "--apply") == 0

    capsys.readouterr()
    assert _run(cli_ready, "verify") == 0
    out = capsys.readouterr().out
    assert "warnings       1" in out
    assert "has no live family" in out

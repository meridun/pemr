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

from pemr import cli, curation, db, dedup, persons, records, verify

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
    assert curation.load_verdicts(conn)[("lab_result", base)]["status"] == "superseded"


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
        "action", "applied",
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

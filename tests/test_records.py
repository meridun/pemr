"""Row-level record repair: `pemr record rm` (issue #107), `record edit` (issue #129).

Covers the module surface (pemr/records.py) and the CLI wiring, in the shape
test_documents.py uses for the `document` group. Each verb gets the end-to-end test of
the scenario it exists for: a doubled fact quarantining a table from `rekey` for
`record rm`, and the reporter's unit-label normalisation for `record edit`.
"""

import json

import pytest

from pemr import cli, curation, db, dedup, documents, persons, records

RECORDS = {
    "lab_result": [
        {"test_name": "HbA1c", "collected_at": "2026-01-02", "value_num": 5.7,
         "unit": "%"},
        {"test_name": "Glucose", "collected_at": "2026-01-02", "value_num": 95,
         "unit": "mg/dL"},
    ],
    "medication": [
        {"name": "Metformin", "dose": "500 mg", "started_on": "2025-06-01"},
    ],
    "condition": [
        {"name": "Type 2 Diabetes", "status": "active", "onset_on": "2024-01-01"},
    ],
}

_SEEDED_ROWS = sum(len(rows) for rows in RECORDS.values())


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
    """Jane owns one multi-record document: two labs, a med and a condition."""
    jane = persons.add_person(conn, "jane-doe", "Jane Doe")
    doc_id = _insert_document(conn, jane.person_id, "aa11bb22cc33dd44")
    dedup.commit_extraction(conn, doc_id, RECORDS)
    return {"conn": conn, "jane": jane, "doc": doc_id}


def _counts(conn):
    return {
        record_type: conn.execute(
            f"SELECT COUNT(*) AS n FROM {record_type}"
        ).fetchone()["n"]
        for record_type in dedup.KNOWN_TYPES
    }


def _row_id(conn, record_type, column, value):
    return int(conn.execute(
        f"SELECT {record_type}_id FROM {record_type} WHERE {column} = ?", (value,)
    ).fetchone()[f"{record_type}_id"])


def _fts_ids(conn, source_table):
    return [
        int(row["source_id"]) for row in conn.execute(
            "SELECT source_id FROM record_fts WHERE source_table = ?", (source_table,)
        ).fetchall()
    ]


# --- the core claim: one row, nothing else ----------------------------------


def test_removes_exactly_one_row_and_leaves_the_document_intact(seeded):
    """The whole point of the verb: `document rm` would take all four rows and the
    document with them; this takes one lab and nothing else (issue #107)."""
    conn = seeded["conn"]
    target = _row_id(conn, "lab_result", "test_name", "Glucose")
    before = _counts(conn)

    report = records.remove_record(conn, "lab_result", target, apply=True)

    after = _counts(conn)
    assert after["lab_result"] == before["lab_result"] - 1
    assert {k: v for k, v in after.items() if k != "lab_result"} == {
        k: v for k, v in before.items() if k != "lab_result"
    }
    assert sum(after.values()) == _SEEDED_ROWS - 1
    # The document itself, and the other lab off the same document, survive.
    assert documents.get_document_view(conn, seeded["doc"])["record_count"] == (
        _SEEDED_ROWS - 1
    )
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM lab_result WHERE test_name = 'HbA1c'"
    ).fetchone()["n"] == 1
    assert report.applied is True
    assert report.record_type == "lab_result"
    assert report.row_id == target
    assert report.label == "Glucose"
    assert report.person_slug == "jane-doe"
    assert report.document_id == seeded["doc"]


def test_dry_run_is_the_default_and_writes_nothing(seeded):
    conn = seeded["conn"]
    target = _row_id(conn, "lab_result", "test_name", "Glucose")
    before = _counts(conn)

    report = records.remove_record(conn, "lab_result", target)

    assert report.applied is False
    assert _counts(conn) == before
    # The dry run is the safety mechanism, so it has to report the payload it would
    # delete, not merely the row id.
    assert report.fields["test_name"] == "Glucose"
    assert report.fields["value_num"] == 95
    assert report.fields["unit"] == "mg/dL"
    assert set(report.fields) == set(dedup.FIELD_SPECS["lab_result"])
    assert report.dedup_key and report.dedup_base
    assert report.family_size == 1 and report.family_remaining == 0


def test_removal_cleans_up_the_fts_row_and_spares_the_documents(seeded):
    """Migration 003/006 AFTER DELETE triggers do this; a test proves the coverage
    rather than assuming it."""
    conn = seeded["conn"]
    target = _row_id(conn, "lab_result", "test_name", "Glucose")
    assert target in _fts_ids(conn, "lab_result")

    records.remove_record(conn, "lab_result", target, apply=True)

    assert target not in _fts_ids(conn, "lab_result")
    assert len(_fts_ids(conn, "lab_result")) == 1     # HbA1c still indexed
    assert _fts_ids(conn, "document") == [seeded["doc"]]


def test_every_known_type_can_be_removed(conn):
    """`record rm` accepts the whole typed record surface, not just lab_result."""
    jane = persons.add_person(conn, "jane-doe", "Jane Doe")
    doc_id = _insert_document(conn, jane.person_id, "bb22")
    payload = {
        "lab_result": [{"test_name": "CL", "collected_at": "2026-01-02",
                        "value_num": 101}],
        "medication": [{"name": "Metformin"}],
        "procedure": [{"name": "Colonoscopy", "performed_on": "2025-03-04"}],
        "appointment": [{"scheduled_for": "2026-02-01", "provider": "Dr Who"}],
        "observation": [{"obs_type": "blood_pressure", "key": "systolic",
                         "value_num": 120}],
        "allergy": [{"substance": "Penicillin"}],
        "condition": [{"name": "Asthma", "status": "active"}],
    }
    dedup.commit_extraction(conn, doc_id, payload)
    for record_type in dedup.KNOWN_TYPES:
        row_id = int(conn.execute(
            f"SELECT {record_type}_id FROM {record_type}"
        ).fetchone()[f"{record_type}_id"])
        records.remove_record(conn, record_type, row_id, apply=True)
    assert sum(_counts(conn).values()) == 0


# --- refusals ----------------------------------------------------------------


def test_unknown_row_id_raises(seeded):
    with pytest.raises(records.RecordNotFoundError):
        records.remove_record(seeded["conn"], "lab_result", 9999)


def test_unknown_table_raises_naming_the_known_types(seeded):
    with pytest.raises(ValueError, match="lab_result"):
        records.remove_record(seeded["conn"], "family_history", 1)


# --- conflicts ---------------------------------------------------------------


def _stage_conflict_on(conn, doc_id, row, bump=1):
    """File a second document that disagrees with ``row``, staging a conflict.

    ``bump`` has to differ per call: commit-time matching is family-aware, so a value
    an earlier `keep both` already admitted comes back as a duplicate, not a conflict.
    """
    n = conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"]
    second = _insert_document(conn, row["person_id"], f"cc33dd{n:02d}")
    incoming = {name: row[name] for name in dedup.FIELD_SPECS["lab_result"]
                if row[name] is not None}
    incoming["value_num"] = (row["value_num"] or 0) + bump
    summary = dedup.commit_extraction(conn, second, {"lab_result": [incoming]})
    assert summary.conflict, summary.counts
    return int(summary.conflict[0][1])


@pytest.fixture()
def conflicted(seeded):
    conn = seeded["conn"]
    target = _row_id(conn, "lab_result", "test_name", "Glucose")
    row = conn.execute(
        "SELECT * FROM lab_result WHERE lab_result_id = ?", (target,)
    ).fetchone()
    conflict_id = _stage_conflict_on(conn, seeded["doc"], row)
    return {**seeded, "row_id": target, "conflict": conflict_id}


def test_removing_the_last_row_of_a_conflicts_family_is_refused(conflicted):
    """`document rm` deletes a stranded conflict because its document is going away
    too. Here the document survives, so `keep both` is still live and the staged
    payload must not be destroyed - refuse instead."""
    conn = conflicted["conn"]
    with pytest.raises(records.AnchoredConflictError, match="review-conflicts"):
        records.remove_record(conn, "lab_result", conflicted["row_id"], apply=True)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM lab_result WHERE lab_result_id = ?",
        (conflicted["row_id"],),
    ).fetchone()["n"] == 1
    assert conn.execute(
        "SELECT status FROM conflict WHERE conflict_id = ?",
        (conflicted["conflict"],),
    ).fetchone()["status"] == "open"


def test_the_refusal_is_pre_flight_so_a_dry_run_refuses_too(conflicted):
    with pytest.raises(records.AnchoredConflictError):
        records.remove_record(conn=conflicted["conn"], record_type="lab_result",
                              row_id=conflicted["row_id"])


def test_a_surviving_sibling_lets_the_conflict_re_anchor(conflicted):
    """With a `--keep both` sibling admitted, occurrence 0 can go: the conflict
    re-anchors to the lowest surviving occurrence (dedup._anchor_row), which the
    report says out loud."""
    conn = conflicted["conn"]
    admitted = dedup.resolve_conflict(conn, conflicted["conflict"], "both")
    second_conflict = _stage_conflict_on(
        conn,
        conflicted["doc"],
        conn.execute(
            "SELECT * FROM lab_result WHERE lab_result_id = ?",
            (conflicted["row_id"],),
        ).fetchone(),
        bump=2,
    )

    report = records.remove_record(
        conn, "lab_result", conflicted["row_id"], apply=True
    )

    assert report.family_size == 2 and report.family_remaining == 1
    assert report.conflicts_reanchored == [second_conflict]
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM lab_result WHERE lab_result_id = ?",
        (conflicted["row_id"],),
    ).fetchone()["n"] == 0
    # Re-anchoring is real, not merely reported: the conflict still resolves, and it
    # resolves against the surviving occurrence.
    result = dedup.resolve_conflict(conn, second_conflict, "incoming")
    assert result.row_id == admitted.row_id
    assert result.occurrence == 1


def test_resolved_conflicts_are_left_alone(conflicted):
    """A resolved conflict is an audit record; removing a row it once cited must not
    disturb it (and cannot strand it - nothing resolves against it again)."""
    conn = conflicted["conn"]
    dedup.resolve_conflict(conn, conflicted["conflict"], "existing")

    records.remove_record(conn, "lab_result", conflicted["row_id"], apply=True)

    row = conn.execute(
        "SELECT * FROM conflict WHERE conflict_id = ?", (conflicted["conflict"],)
    ).fetchone()
    assert row is not None and row["status"] == "resolved"
    assert row["incoming_json"]


# --- the doubled-collision loop, end to end ----------------------------------


def test_record_rm_unblocks_a_doubled_rekey_collision(conn):
    """The issue's actual scenario (#107): one fact filed twice under two keys
    quarantines its table from `rekey`; dropping the degraded row with `record rm`
    lets `rekey --apply` run clean."""
    jane = persons.add_person(conn, "jane-doe", "Jane Doe")
    doc_id = _insert_document(conn, jane.person_id, "dd44ee55")
    row = {"test_name": "Neutrophils (absolute)", "collected_at": "2026-01-02",
           "value_num": 3.1}
    dedup.commit_extraction(conn, doc_id, {"lab_result": [row]}, None)
    # The pre-guard state, built directly: the same fact filed again under a stale
    # key, the way test_dedup.py builds it.
    doubled = dedup._insert_record(
        conn, "lab_result", row, jane.person_id, doc_id, "stale-base-0000"
    )
    # ... plus an unrelated record on the same document, which must survive.
    dedup.commit_extraction(conn, doc_id, {"medication": [{"name": "Metformin"}]})
    conn.commit()

    report = dedup.rekey(conn, None)
    assert [c.kind for c in report.collisions] == ["doubled"]
    assert report.blocked == ["lab_result"]
    assert "SAME fact" in report.collisions[0].message
    assert "pemr record rm lab_result" in report.collisions[0].message

    records.remove_record(conn, "lab_result", doubled, apply=True)

    after = dedup.rekey(conn, None, apply=True)
    assert after.collisions == []
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM lab_result"
    ).fetchone()["n"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM medication"
    ).fetchone()["n"] == 1


def test_the_doubled_message_still_names_the_document_option(conn):
    """`document rm` stays in the message: it is still right when the re-filing
    document holds nothing else."""
    jane = persons.add_person(conn, "jane-doe", "Jane Doe")
    doc_id = _insert_document(conn, jane.person_id, "ee55ff66")
    row = {"test_name": "Glucose", "collected_at": "2026-01-02", "value_num": 95}
    dedup.commit_extraction(conn, doc_id, {"lab_result": [row]}, None)
    dedup._insert_record(
        conn, "lab_result", row, jane.person_id, doc_id, "stale-base-0000"
    )
    conn.commit()

    message = dedup.rekey(conn, None).collisions[0].message
    assert "pemr document rm" in message
    assert "--apply" in message


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


def _cli_row_id(tmp_path, record_type, column, value):
    conn = db.connect(tmp_path / "cli.db")
    try:
        return _row_id(conn, record_type, column, value)
    finally:
        conn.close()


def _cli_count(tmp_path, record_type):
    conn = db.connect(tmp_path / "cli.db")
    try:
        return conn.execute(
            f"SELECT COUNT(*) AS n FROM {record_type}"
        ).fetchone()["n"]
    finally:
        conn.close()


def test_cli_record_rm_dry_run(cli_ready, capsys):
    target = _cli_row_id(cli_ready, "lab_result", "test_name", "Glucose")
    capsys.readouterr()
    assert _run(cli_ready, "record", "rm", "lab_result", str(target)) == 0
    out = capsys.readouterr().out
    assert f"lab_result #{target}" in out
    assert "jane-doe" in out and "Glucose" in out
    assert "test_name: Glucose" in out
    assert "document: #1 (kept, with its other records)" in out
    assert "dry run: nothing was deleted - re-run with --apply" in out
    assert "back up first: `pemr backup`" in out
    assert _cli_count(cli_ready, "lab_result") == 2


def test_cli_record_rm_apply(cli_ready, capsys):
    target = _cli_row_id(cli_ready, "lab_result", "test_name", "Glucose")
    capsys.readouterr()
    assert _run(cli_ready, "record", "rm", "lab_result", str(target), "--apply") == 0
    out = capsys.readouterr().out
    assert f"removed lab_result #{target}" in out
    assert _cli_count(cli_ready, "lab_result") == 1
    # Other records off the same document are untouched.
    assert _cli_count(cli_ready, "medication") == 1
    assert _cli_count(cli_ready, "condition") == 1


def test_cli_record_rm_json_shape_is_stable(cli_ready, capsys):
    target = _cli_row_id(cli_ready, "lab_result", "test_name", "Glucose")
    expected = {
        "record_type", "row_id", "person", "document_id", "label", "fields",
        "dedup_key", "dedup_base", "dedup_occurrence", "family_size",
        "family_remaining", "conflicts_reanchored", "applied",
        # Appended by #114: the row-scoped curation verdicts retired with the row.
        # Appended, never inserted - the --json key set is a contract.
        "curation_retired",
        # Appended by #129: the edit-ledger entries naming the row. Disclosed, not
        # retired - the contrast with `curation_retired` is the point.
        "edits_recorded",
    }
    capsys.readouterr()
    assert _run(cli_ready, "record", "rm", "lab_result", str(target), "--json") == 0
    dry = json.loads(capsys.readouterr().out)
    assert set(dry) == expected
    assert dry["applied"] is False
    assert set(dry["fields"]) == set(dedup.FIELD_SPECS["lab_result"])

    assert _run(cli_ready, "record", "rm", "lab_result", str(target), "--apply",
                "--json") == 0
    applied = json.loads(capsys.readouterr().out)
    assert set(applied) == expected
    assert applied["applied"] is True
    assert applied["row_id"] == target


def test_cli_record_rm_unknown_table_is_argparse_misuse(cli_ready):
    with pytest.raises(SystemExit) as exc:
        _run(cli_ready, "record", "rm", "family_history", "1")
    assert exc.value.code == 2


def test_cli_record_rm_unknown_id_fails_friendly(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "record", "rm", "lab_result", "9999") == 1
    assert "error: no lab_result with id 9999" in capsys.readouterr().err


def test_cli_record_rm_refusal_writes_nothing(cli_ready, capsys):
    """The anchored-conflict refusal reaches the CLI as rc=1 on stderr, not a
    traceback."""
    scan2 = cli_ready / "scan2.txt"
    scan2.write_bytes(b"repeat glucose 96")
    assert _run(cli_ready, "ingest", str(scan2), "--person", "jane-doe",
                "--sources", str(cli_ready / "sources"),
                "--ocr-text-file", str(scan2)) == 0
    payload = cli_ready / "extract2.json"
    payload.write_text(json.dumps({"lab_result": [
        {"test_name": "Glucose", "collected_at": "2026-01-02", "value_num": 96,
         "unit": "mg/dL"},
    ]}), encoding="utf-8")
    assert _run(cli_ready, "commit-extraction", "--document", "2",
                "--json", str(payload)) == 0
    target = _cli_row_id(cli_ready, "lab_result", "value_num", 95)

    capsys.readouterr()
    assert _run(cli_ready, "record", "rm", "lab_result", str(target),
                "--apply") == 1
    assert "review-conflicts" in capsys.readouterr().err
    assert _cli_count(cli_ready, "lab_result") == 2


# --- smoke: the operator loop, end to end through the CLI --------------------


_DICT_BASE = '[synonyms]\n"neu" = "neutrophils_abs"\n'
_DICT_FUSED = _DICT_BASE + '"neutrophils (absolute)" = "neutrophils_abs"\n'


def _ingest(tmp_path, name, text):
    path = tmp_path / name
    path.write_bytes(text)
    assert _run(tmp_path, "ingest", str(path), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources"),
                "--ocr-text-file", str(path)) == 0


def _commit(tmp_path, name, document, payload, dictionary):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert _run(tmp_path, "commit-extraction", "--document", str(document),
                "--json", str(path), "--dictionary", str(dictionary)) == 0


def test_cli_smoke_walks_the_whole_doubled_collision_loop(tmp_path, capsys):
    """The issue's operator story, driven entirely through the CLI with a real
    dictionary edit (issue #107).

    The engine-level end-to-end test builds the doubled row with `dedup._insert_record`;
    this one earns it the way an operator does — two documents, then a synonym addition
    that fuses their labels — and then walks `rekey` (doubled, quarantined table) ->
    `record rm` (dry run, then --apply) -> `rekey --apply` (clean), proving the AC's
    claim that resolving a doubled fact inside a multi-record document needs no direct
    SQL edit and costs the document none of its other records.
    """
    base = tmp_path / "base.toml"
    base.write_text(_DICT_BASE, encoding="utf-8")
    fused = tmp_path / "fused.toml"
    fused.write_text(_DICT_FUSED, encoding="utf-8")

    assert _run(tmp_path, "migrate", "--create") == 0
    assert _run(tmp_path, "person", "add", "--slug", "jane-doe", "--name", "Jane") == 0
    _ingest(tmp_path, "visit.txt", b"visit note: neu 3.1, glucose 95, metformin")
    _ingest(tmp_path, "lab.txt", b"lab report: Neutrophils (absolute) 3.1")
    # The visit note quotes the lab's neutrophil count and holds three other records;
    # the lab report states the same fact under its long-form label.
    _commit(tmp_path, "visit.json", 1, {
        "lab_result": [
            {"test_name": "NEU", "collected_at": "2026-01-02", "value_num": 3.1,
             "unit": "10*9/L"},
            {"test_name": "Glucose", "collected_at": "2026-01-02", "value_num": 95,
             "unit": "mg/dL"},
        ],
        "medication": [{"name": "Metformin", "dose": "500 mg"}],
        "condition": [{"name": "Type 2 Diabetes", "status": "active"}],
    }, base)
    _commit(tmp_path, "lab.json", 2, {
        "lab_result": [{"test_name": "Neutrophils (absolute)",
                        "collected_at": "2026-01-02", "value_num": 3.1,
                        "unit": "10*9/L"}],
    }, base)
    quoted = _cli_row_id(tmp_path, "lab_result", "test_name", "NEU")
    filed = _cli_row_id(tmp_path, "lab_result", "test_name", "Neutrophils (absolute)")
    glucose = _cli_row_id(tmp_path, "lab_result", "test_name", "Glucose")

    # 1. The synonym addition fuses the two labels: rekey quarantines the table and
    #    hands back a runnable command for each candidate row.
    capsys.readouterr()
    assert _run(tmp_path, "rekey", "--dictionary", str(fused)) == 1
    rekey_out = capsys.readouterr()
    message = rekey_out.out + rekey_out.err
    assert "SAME fact" in message
    # Both candidate rows are named, one of them as a directly runnable command.
    assert f"pemr record rm lab_result {filed}" in message
    assert f"`... {quoted}`" in message
    assert "pemr document rm" in message

    # 2. Dry run on the quoted copy - the one inside the multi-record visit note,
    #    exactly the row `document rm` could not take on its own.
    assert _run(tmp_path, "record", "rm", "lab_result", str(quoted),
                "--dictionary", str(fused)) == 0
    dry = capsys.readouterr().out
    assert "dry run: nothing was deleted - re-run with --apply" in dry
    assert _cli_count(tmp_path, "lab_result") == 3

    # 3. --apply takes that row and nothing else - the visit note keeps its glucose,
    #    its medication and its condition.
    assert _run(tmp_path, "record", "rm", "lab_result", str(quoted), "--apply",
                "--dictionary", str(fused)) == 0
    assert f"removed lab_result #{quoted}" in capsys.readouterr().out
    assert _cli_count(tmp_path, "lab_result") == 2
    assert _cli_count(tmp_path, "medication") == 1
    assert _cli_count(tmp_path, "condition") == 1
    assert _cli_count(tmp_path, "document") == 2

    # 4. The collision is gone: rekey moves the surviving row onto the fused key.
    assert _run(tmp_path, "rekey", "--dictionary", str(fused), "--apply") == 0
    assert "rekeyed 1 row(s)" in capsys.readouterr().out
    assert _run(tmp_path, "rekey", "--dictionary", str(fused)) == 0

    # 5. No orphaned FTS row, and the visit note's own text is still searchable.
    conn = db.connect(tmp_path / "cli.db")
    try:
        assert sorted(_fts_ids(conn, "lab_result")) == sorted([glucose, filed])
        assert sorted(_fts_ids(conn, "document")) == [1, 2]
    finally:
        conn.close()

    # 6. And the database is still internally consistent.
    assert _run(tmp_path, "verify", "--sources", str(tmp_path / "sources")) == 0


# =============================================================================
# in-place correction of non-key fields: `record edit` (issue #129)
# =============================================================================
#
# The reporting case: stored values correct and on one scale, but the unit STRING
# varies by whichever extraction submitted it (`lbs` beside `lb`, `F` beside `degF`).
# Both pre-existing repairs — re-submitting through `commit-extraction`, or
# delete-and-recommit — re-attribute the row to whichever document is passed, so a 2022
# measurement ends up sourced to a 2026 chart export. `record edit` corrects the field
# and leaves provenance alone.

# One fully-populated payload per record type, plus a different-but-valid value for
# every field. Used to prove the KEY_FIELDS/`_key_parts` agreement behaviourally, for
# every type, rather than by eyeballing the two lists.
FIELD_VALUES = {
    "lab_result": {
        "test_name": ("HbA1c", "Glucose"),
        "loinc": ("4548-4", "2345-7"),
        "value_num": (5.7, 6.1),
        "value_text": ("normal", "high"),
        "unit": ("%", "pct"),
        "ref_low": (4.0, 3.5),
        "ref_high": (6.0, 6.5),
        "flag": ("H", "L"),
        "collected_at": ("2026-01-02", "2026-02-03"),
    },
    "medication": {
        "name": ("Metformin", "Insulin"),
        "dose": ("500 mg", "1000 mg"),
        "route": ("PO", "SC"),
        "frequency": ("BID", "QD"),
        "started_on": ("2025-06-01", "2025-07-01"),
        "ended_on": ("2026-01-01", "2026-02-01"),
        "prescriber": ("Dr Who", "Dr No"),
        "status": ("active", "discontinued"),
    },
    "procedure": {
        "name": ("Colonoscopy", "Endoscopy"),
        "performed_on": ("2025-03-04", "2025-04-05"),
        "provider": ("Dr Who", "Dr No"),
        "outcome": ("normal", "abnormal"),
    },
    "appointment": {
        "scheduled_for": ("2026-02-01", "2026-03-01"),
        "provider": ("Dr Who", "Dr No"),
        "specialty": ("cardiology", "neurology"),
        "reason": ("follow-up", "consult"),
        "summary": ("seen", "rescheduled"),
    },
    "observation": {
        "obs_type": ("vital", "anthropometric"),
        "observed_at": ("2026-01-02T09:00", "2026-01-03T09:00"),
        "key": ("weight", "height"),
        "value_num": (180.0, 175.0),
        "value_text": ("one eighty", "one seventy five"),
        "unit": ("lb", "lbs"),
    },
    "allergy": {
        "substance": ("Penicillin", "Sulfa"),
        "reaction": ("rash", "hives"),
        "criticality": ("high", "low"),
        "noted_on": ("2010-01-01", "2011-01-01"),
    },
    "condition": {
        "name": ("Asthma", "Eczema"),
        "status": ("active", "resolved"),
        "onset_on": ("2024-01-01", "2025-01-01"),
        "resolved_on": ("2026-01-01", "2026-02-01"),
        "relation": ("mother", "father"),
        "note": ("dx by PCP", "dx by specialist"),
    },
}

# One key-participating change per type that really does move the dedup_key — the
# positive direction, so the KEY_FIELDS test cannot pass by the key being inert.
KEY_MOVES = {
    "lab_result": {"test_name": "Glucose"},
    "medication": {"dose": "1000 mg"},
    "procedure": {"performed_on": "2025-04-05"},
    "appointment": {"provider": "Dr No"},
    "observation": {"key": "height"},
    "allergy": {"substance": "Sulfa"},
    # The one status change that moves the key: `_condition_subject` keys on the
    # relative once the status is family-history, and on "self" for every other status.
    "condition": {"status": "family-history"},
}


def _full_payload(record_type):
    return {name: pair[0] for name, pair in FIELD_VALUES[record_type].items()}


# --- KEY_FIELDS must stay true to dedup._key_parts ---------------------------


@pytest.mark.parametrize("record_type", dedup.KNOWN_TYPES)
def test_no_editable_field_can_move_the_dedup_key(record_type):
    """The safety-critical direction, proved behaviourally for every type.

    `record edit` derives its editable set as FIELD_SPECS - KEY_FIELDS, so if
    KEY_FIELDS ever drifts from `dedup._key_parts` an "editable" field would silently
    re-key a stored row. This is what stops that drift: mutate every non-key field, one
    at a time, and require the key byte-identical each time.
    """
    base = _full_payload(record_type)
    expected = dedup.dedup_key(record_type, base, 1)
    editable = dedup.editable_fields(record_type)
    assert editable, record_type
    for name in editable:
        other = FIELD_VALUES[record_type][name][1]
        moved = {**base, name: other}
        assert dedup.dedup_key(record_type, moved, 1) == expected, name
        # And clearing it is equally key-neutral (`--set NAME=` writes None).
        cleared = {**base, name: None}
        assert dedup.dedup_key(record_type, cleared, 1) == expected, name


@pytest.mark.parametrize("record_type", dedup.KNOWN_TYPES)
def test_key_fields_really_do_move_the_key(record_type):
    """The positive direction: a hand-listed key change per type moves the key, so the
    test above is not passing because the key is inert."""
    base = _full_payload(record_type)
    moved = {**base, **KEY_MOVES[record_type]}
    assert dedup.dedup_key(record_type, moved, 1) != dedup.dedup_key(
        record_type, base, 1
    )


def test_editable_fields_excludes_identity_and_never_names_provenance():
    for record_type in dedup.KNOWN_TYPES:
        editable = set(dedup.editable_fields(record_type))
        assert editable.isdisjoint(dedup.KEY_FIELDS[record_type])
        assert editable == (
            set(dedup.FIELD_SPECS[record_type]) - dedup.KEY_FIELDS[record_type]
        )
        # Provenance is out for free: it was never in FIELD_SPECS to begin with.
        for column in ("document_id", "person_id", *dedup.INTERNAL_COLUMNS,
                       *dedup.ATTESTATION_COLUMNS):
            assert column not in editable


def test_editable_fields_rejects_an_unknown_type():
    with pytest.raises(ValueError, match="lab_result"):
        dedup.editable_fields("family_history")


# --- the module surface ------------------------------------------------------


def _ledger(conn, record_type=None):
    sql = "SELECT * FROM record_edit"
    params = ()
    if record_type is not None:
        sql += " WHERE record_type = ?"
        params = (record_type,)
    return [dict(r) for r in conn.execute(sql + " ORDER BY record_edit_id", params)]


def _row(conn, record_type, row_id):
    return dict(conn.execute(
        f"SELECT * FROM {record_type} WHERE {record_type}_id = ?", (row_id,)
    ).fetchone())


def test_dry_run_reports_the_change_and_writes_nothing(seeded):
    conn = seeded["conn"]
    target = _row_id(conn, "lab_result", "test_name", "HbA1c")
    before = _row(conn, "lab_result", target)
    before_fts = _fts_ids(conn, "lab_result")

    report = records.edit_record(
        conn, "lab_result", target, {"unit": "percent"}, note="normalise the unit"
    )

    assert report.applied is False
    assert report.changes == [{"field": "unit", "old": "%", "new": "percent"}]
    assert report.unchanged == []
    assert report.label == "HbA1c"
    assert report.person_slug == "jane-doe"
    assert report.document_id == seeded["doc"]
    assert _row(conn, "lab_result", target) == before
    assert _ledger(conn) == []
    assert _fts_ids(conn, "lab_result") == before_fts


def test_apply_moves_the_field_and_leaves_identity_and_provenance_alone(seeded):
    """AC 1 and AC 4 together: the display field changes, and every column that says
    where the fact came from or which fact it is stays bit-identical."""
    conn = seeded["conn"]
    target = _row_id(conn, "lab_result", "test_name", "HbA1c")
    before = _row(conn, "lab_result", target)

    report = records.edit_record(
        conn, "lab_result", target, {"unit": "percent", "flag": "H"},
        note="normalise the unit", attributed_to="Jane", apply=True,
    )

    after = _row(conn, "lab_result", target)
    assert after["unit"] == "percent" and after["flag"] == "H"
    for column in ("document_id", "person_id", "test_name", "collected_at",
                   "dedup_key", "dedup_base", "dedup_occurrence",
                   *dedup.ATTESTATION_COLUMNS):
        assert after[column] == before[column], column
    assert report.applied is True

    entries = _ledger(conn)
    assert len(entries) == 2                       # one row per changed field
    assert {e["field"] for e in entries} == {"unit", "flag"}
    assert {e["edited_at"] for e in entries} == {report.edited_at}   # one act
    unit_entry = next(e for e in entries if e["field"] == "unit")
    assert (unit_entry["old_value"], unit_entry["new_value"]) == ("%", "percent")
    assert unit_entry["record_type"] == "lab_result"
    assert unit_entry["record_id"] == target
    assert unit_entry["dedup_base"] == before["dedup_base"]
    assert unit_entry["note"] == "normalise the unit"
    assert unit_entry["attributed_to"] == "Jane"


def test_editing_to_the_stored_value_is_a_no_op(seeded):
    """A re-run must not accrete history: the ledger is append-only, so a second
    identical `--apply` has to write nothing at all."""
    conn = seeded["conn"]
    target = _row_id(conn, "lab_result", "test_name", "HbA1c")
    records.edit_record(conn, "lab_result", target, {"unit": "percent"},
                        note="normalise", apply=True)
    assert len(_ledger(conn)) == 1

    report = records.edit_record(conn, "lab_result", target, {"unit": "percent"},
                                 note="normalise", apply=True)

    assert report.changes == []
    assert report.unchanged == ["unit"]
    assert len(_ledger(conn)) == 1


def test_a_numeric_no_op_compares_numerically(seeded):
    """A numeric column round-trips out of SQLite as float, so `--set value_num=95`
    against a stored 95.0 is a no-op, not a ledgered change."""
    conn = seeded["conn"]
    target = _row_id(conn, "lab_result", "test_name", "Glucose")
    report = records.edit_record(conn, "lab_result", target, {"value_num": 95},
                                 note="restate", apply=True)
    assert report.changes == [] and report.unchanged == ["value_num"]
    assert _ledger(conn) == []


def test_clearing_a_field_nulls_it_and_ledgers_the_old_value(seeded):
    conn = seeded["conn"]
    target = _row_id(conn, "lab_result", "test_name", "HbA1c")

    report = records.edit_record(conn, "lab_result", target, {"unit": None},
                                 note="the source states no unit", apply=True)

    assert report.changes == [{"field": "unit", "old": "%", "new": None}]
    assert _row(conn, "lab_result", target)["unit"] is None
    entry = _ledger(conn)[0]
    assert (entry["old_value"], entry["new_value"]) == ("%", None)


def test_every_known_type_can_be_edited(conn):
    """The verb covers the whole typed record surface, not just lab_result."""
    jane = persons.add_person(conn, "jane-doe", "Jane Doe")
    doc_id = _insert_document(conn, jane.person_id, "cc33")
    dedup.commit_extraction(
        conn, doc_id,
        {record_type: [_full_payload(record_type)]
         for record_type in dedup.KNOWN_TYPES},
    )
    for record_type in dedup.KNOWN_TYPES:
        row_id = int(conn.execute(
            f"SELECT {record_type}_id FROM {record_type}"
        ).fetchone()[f"{record_type}_id"])
        name = dedup.editable_fields(record_type)[0]
        before = _row(conn, record_type, row_id)
        report = records.edit_record(
            conn, record_type, row_id,
            {name: FIELD_VALUES[record_type][name][1]},
            note="correction", apply=True,
        )
        assert report.changes, record_type
        after = _row(conn, record_type, row_id)
        assert after["dedup_key"] == before["dedup_key"], record_type
        assert after["document_id"] == before["document_id"], record_type


# --- refusals: nothing is written on any raise -------------------------------


def _assert_untouched(conn, record_type, row_id, before):
    assert _row(conn, record_type, row_id) == before
    assert _ledger(conn) == []


def test_a_key_field_is_refused_and_points_at_the_right_operation(seeded):
    conn = seeded["conn"]
    target = _row_id(conn, "lab_result", "test_name", "HbA1c")
    before = _row(conn, "lab_result", target)
    with pytest.raises(records.FieldNotEditableError) as exc:
        records.edit_record(conn, "lab_result", target, {"test_name": "A1c"},
                            note="rename", apply=True)
    message = str(exc.value)
    assert "identity" in message and "rekey" in message
    assert "unit" in message                       # names the editable set
    _assert_untouched(conn, "lab_result", target, before)


def test_an_unknown_field_is_refused(seeded):
    conn = seeded["conn"]
    target = _row_id(conn, "lab_result", "test_name", "HbA1c")
    with pytest.raises(records.FieldNotEditableError, match="editable fields"):
        records.edit_record(conn, "lab_result", target, {"colour": "blue"},
                            note="n", apply=True)
    assert _ledger(conn) == []


def test_an_unknown_row_id_is_refused(seeded):
    with pytest.raises(records.RecordNotFoundError):
        records.edit_record(seeded["conn"], "lab_result", 9999, {"unit": "lb"},
                            note="n", apply=True)


def test_an_unknown_type_is_refused_by_edit(seeded):
    with pytest.raises(ValueError, match="lab_result"):
        records.edit_record(seeded["conn"], "family_history", 1, {"unit": "lb"},
                            note="n")


def test_no_updates_is_refused(seeded):
    conn = seeded["conn"]
    target = _row_id(conn, "lab_result", "test_name", "HbA1c")
    with pytest.raises(ValueError, match="at least one"):
        records.edit_record(conn, "lab_result", target, {}, note="n", apply=True)


def test_a_blank_note_is_refused(seeded):
    """The `curation.annotate_record` rule: an unexplained mutation of a stored clinical
    value is not an audit trail."""
    conn = seeded["conn"]
    target = _row_id(conn, "lab_result", "test_name", "HbA1c")
    before = _row(conn, "lab_result", target)
    with pytest.raises(ValueError, match="note is required"):
        records.edit_record(conn, "lab_result", target, {"unit": "percent"},
                            note="   ", apply=True)
    _assert_untouched(conn, "lab_result", target, before)


@pytest.mark.parametrize("record_type,updates", [
    ("lab_result", {"value_num": "abc"}),          # wrong type
    ("medication", {"ended_on": "06/15/2026"}),    # non-ISO date
    ("condition", {"note": 5}),                    # wrong type
])
def test_a_value_that_fails_validation_is_refused(conn, record_type, updates):
    jane = persons.add_person(conn, "jane-doe", "Jane Doe")
    doc_id = _insert_document(conn, jane.person_id, "dd44")
    dedup.commit_extraction(conn, doc_id, {record_type: [_full_payload(record_type)]})
    row_id = int(conn.execute(
        f"SELECT {record_type}_id FROM {record_type}"
    ).fetchone()[f"{record_type}_id"])
    before = _row(conn, record_type, row_id)

    with pytest.raises(dedup.ValidationError):
        records.edit_record(conn, record_type, row_id, updates, note="n", apply=True)

    _assert_untouched(conn, record_type, row_id, before)


def test_an_enum_value_that_fails_validation_is_refused(conn):
    """`allergy.criticality` is an ENUM_FIELDS column and editable — the enum rule has
    to bind at edit time exactly as it does at commit."""
    jane = persons.add_person(conn, "jane-doe", "Jane Doe")
    doc_id = _insert_document(conn, jane.person_id, "ee55")
    dedup.commit_extraction(conn, doc_id, {"allergy": [_full_payload("allergy")]})
    row_id = int(conn.execute("SELECT allergy_id FROM allergy").fetchone()[0])
    before = _row(conn, "allergy", row_id)

    with pytest.raises(dedup.ValidationError, match="criticality"):
        records.edit_record(conn, "allergy", row_id, {"criticality": "extreme"},
                            note="n", apply=True)

    _assert_untouched(conn, "allergy", row_id, before)


# --- the seams the plan calls risky ------------------------------------------


def test_the_fts_row_follows_an_edit(conn):
    """Migration 003's AFTER UPDATE trigger does this. `observation.value_text` is the
    field that proves it: it is editable AND indexed (the trigger indexes `key` +
    `value_text`; `unit` is deliberately not in the FTS text at all)."""
    jane = persons.add_person(conn, "jane-doe", "Jane Doe")
    doc_id = _insert_document(conn, jane.person_id, "ff66")
    dedup.commit_extraction(conn, doc_id, {"observation": [
        {"obs_type": "vital", "observed_at": "2026-01-02T09:00", "key": "weight",
         "value_text": "onehundredeighty"},
    ]})
    row_id = int(conn.execute("SELECT observation_id FROM observation").fetchone()[0])

    def _matches(token):
        return [int(r["source_id"]) for r in conn.execute(
            "SELECT source_id FROM record_fts WHERE source_table = 'observation' "
            "AND record_fts MATCH ?", (token,)
        ).fetchall()]

    assert _matches("onehundredeighty") == [row_id]

    records.edit_record(conn, "observation", row_id,
                        {"value_text": "onehundredseventyfive"},
                        note="transcription error", apply=True)

    assert _matches("onehundredseventyfive") == [row_id]
    assert _matches("onehundredeighty") == []


def test_curation_verdicts_in_both_scopes_survive_an_edit(seeded):
    """No editable field feeds the key, so `dedup_base` never moves — which means an
    edit orphans nothing, in either scope (contrast `rekey`, issue #126)."""
    conn = seeded["conn"]
    target = _row_id(conn, "lab_result", "test_name", "HbA1c")
    base = _row(conn, "lab_result", target)["dedup_base"]
    curation.annotate_record(conn, "lab_result", base, status="confirmed",
                             note="family verdict", apply=True)
    curation.annotate_record(conn, "lab_result", str(target), status="disputed",
                             note="row verdict", row=True, apply=True)

    records.edit_record(conn, "lab_result", target, {"unit": "percent"},
                        note="normalise", apply=True)

    verdicts = curation.load_verdicts(conn)
    assert verdicts.family[("lab_result", base)]["status"] == "confirmed"
    assert verdicts.rows[("lab_result", target)]["status"] == "disputed"
    # Neither is orphaned: both still resolve to a live target.
    assert all(entry["family_size"] for entry in curation.list_curation(conn))


def test_record_rm_discloses_the_ledger_and_keeps_it(seeded):
    """The opposite ending to a row-scoped verdict (issue #114): the verdict is retired
    with the row, the ledger entry is disclosed and kept — nothing resolves through it,
    and retiring it would destroy the audit trail."""
    conn = seeded["conn"]
    target = _row_id(conn, "lab_result", "test_name", "HbA1c")
    base = _row(conn, "lab_result", target)["dedup_base"]
    records.edit_record(conn, "lab_result", target, {"unit": "percent"},
                        note="normalise", apply=True)
    curation.annotate_record(conn, "lab_result", str(target), status="disputed",
                             note="row verdict", row=True, apply=True)

    dry = records.remove_record(conn, "lab_result", target)
    assert [e["field"] for e in dry.edits_recorded] == ["unit"]
    assert dry.edits_recorded[0]["dedup_base"] == base
    assert len(dry.curation_retired) == 1

    records.remove_record(conn, "lab_result", target, apply=True)

    assert len(_ledger(conn)) == 1                      # kept
    assert curation.row_verdicts_for(conn, "lab_result", [target]) == []   # retired
    # And the entry is still listable, annotated as naming no live row.
    listed = records.list_edits(conn)
    assert len(listed) == 1 and listed[0]["label"] == ""


def test_re_ingesting_the_original_document_stages_a_conflict_after_a_correction(
    seeded,
):
    """Pinned deliberately: `unit` is one of `dedup._COMPARE_FIELDS`, so once a unit is
    corrected the original document no longer matches the stored row and re-committing
    it stages a CONFLICT rather than deduping. Loud and correct — the divergence is
    real — and the sharpest argument for #136."""
    conn = seeded["conn"]
    target = _row_id(conn, "lab_result", "test_name", "HbA1c")
    original = RECORDS["lab_result"][0]

    # Before the edit, re-committing the same payload is a plain duplicate.
    doc2 = _insert_document(conn, seeded["jane"].person_id, "9999aaaa")
    assert dedup.commit_extraction(
        conn, doc2, {"lab_result": [original]}
    ).counts["duplicate"] == 1

    records.edit_record(conn, "lab_result", target, {"unit": "percent"},
                        note="normalise", apply=True)

    doc3 = _insert_document(conn, seeded["jane"].person_id, "9999bbbb")
    summary = dedup.commit_extraction(conn, doc3, {"lab_result": [original]})
    assert summary.counts["conflict"] == 1
    assert summary.counts["duplicate"] == 0


def test_list_edits_is_empty_on_a_database_predating_the_migration(seeded):
    """The `curation.has_table` guard: a restored older snapshot reports no edits rather
    than raising `no such table`."""
    conn = seeded["conn"]
    conn.execute("DROP TABLE record_edit")
    conn.commit()
    assert records.has_edit_table(conn) is False
    assert records.list_edits(conn) == []
    assert records.edits_for_rows(conn, "lab_result", [1]) == []
    # And `record rm` still works against it.
    target = _row_id(conn, "lab_result", "test_name", "HbA1c")
    assert records.remove_record(conn, "lab_result", target).edits_recorded == []


# --- the reporter's scenario, end to end -------------------------------------


def test_the_reporters_unit_normalisation_scenario(conn):
    """Issue #129's own case: a majority unit beside a minority spelling of it. Correct
    the minority rows, and `rekey` stays clean while every corrected row still points at
    the document it came from (AC 4)."""
    jane = persons.add_person(conn, "jane-doe", "Jane Doe")
    majority_doc = _insert_document(conn, jane.person_id, "a" * 16)
    minority_doc = _insert_document(conn, jane.person_id, "b" * 16)

    def _weights(count, unit, start_day):
        return [
            {"obs_type": "vital", "observed_at": f"2026-01-{start_day + i:02d}T09:00",
             "key": "weight", "value_num": 180.0 + i, "unit": unit}
            for i in range(count)
        ]

    dedup.commit_extraction(conn, majority_doc, {"observation": _weights(5, "lb", 1)})
    dedup.commit_extraction(conn, minority_doc, {"observation": _weights(3, "lbs", 10)})

    doomed = [
        (int(r["observation_id"]), r["document_id"], r["dedup_key"])
        for r in conn.execute(
            "SELECT * FROM observation WHERE unit = 'lbs' ORDER BY observation_id"
        ).fetchall()
    ]
    assert len(doomed) == 3

    for row_id, _doc, _key in doomed:
        records.edit_record(conn, "observation", row_id, {"unit": "lb"},
                            note="normalise unit label to the majority spelling",
                            attributed_to="Jane", apply=True)

    assert conn.execute(
        "SELECT COUNT(*) AS n FROM observation WHERE unit = 'lb'"
    ).fetchone()["n"] == 8
    # AC 4: provenance and identity untouched on every corrected row.
    for row_id, document_id, key in doomed:
        after = _row(conn, "observation", row_id)
        assert after["document_id"] == document_id
        assert after["dedup_key"] == key
    # AC 2: a discoverable audit trail, one entry per corrected row.
    assert len(records.list_edits(conn, "observation")) == 3
    # And the identity layer is untouched by the whole batch.
    assert dedup.rekey(conn).collisions == []
    assert dedup.rekey(conn).changes == []


# --- CLI surface -------------------------------------------------------------


def _cli_field(tmp_path, record_type, row_id, column):
    conn = db.connect(tmp_path / "cli.db")
    try:
        return _row(conn, record_type, row_id)[column]
    finally:
        conn.close()


def test_cli_record_edit_dry_run(cli_ready, capsys):
    target = _cli_row_id(cli_ready, "lab_result", "test_name", "HbA1c")
    capsys.readouterr()
    assert _run(cli_ready, "record", "edit", "lab_result", str(target),
                "--set", "unit=percent", "--note", "normalise") == 0
    out = capsys.readouterr().out
    assert f"lab_result #{target}" in out
    assert "unit: % -> percent" in out
    assert "document: #1 (unchanged - a correction is not a re-attribution)" in out
    assert "dry run: nothing was written - re-run with --apply" in out
    assert _cli_field(cli_ready, "lab_result", target, "unit") == "%"


def test_cli_record_edit_apply(cli_ready, capsys):
    target = _cli_row_id(cli_ready, "lab_result", "test_name", "HbA1c")
    capsys.readouterr()
    assert _run(cli_ready, "record", "edit", "lab_result", str(target),
                "--set", "unit=percent", "--note", "normalise",
                "--attributed-to", "Jane", "--apply") == 0
    out = capsys.readouterr().out
    assert f"corrected lab_result #{target}" in out
    assert _cli_field(cli_ready, "lab_result", target, "unit") == "percent"


def test_cli_record_edit_clears_with_a_bare_name(cli_ready, capsys):
    """`document edit`'s documented `"" clears` convention, carried over."""
    target = _cli_row_id(cli_ready, "lab_result", "test_name", "HbA1c")
    capsys.readouterr()
    assert _run(cli_ready, "record", "edit", "lab_result", str(target),
                "--set", "unit=", "--note", "the source states no unit",
                "--apply") == 0
    assert "unit: % -> (none)" in capsys.readouterr().out
    assert _cli_field(cli_ready, "lab_result", target, "unit") is None


def test_cli_record_edit_json_shape_is_stable(cli_ready, capsys):
    target = _cli_row_id(cli_ready, "lab_result", "test_name", "HbA1c")
    capsys.readouterr()
    assert _run(cli_ready, "record", "edit", "lab_result", str(target),
                "--set", "unit=percent", "--note", "normalise", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "record_type", "row_id", "person", "person_id", "document_id", "label",
        "changes", "unchanged", "dedup_key", "dedup_base", "dedup_occurrence",
        "note", "attributed_to", "edited_at", "applied",
    }
    assert payload["changes"] == [{"field": "unit", "old": "%", "new": "percent"}]
    assert payload["applied"] is False


def test_cli_record_rm_json_gained_the_edit_disclosure(cli_ready, capsys):
    """Appended, never inserted: the `record rm` --json key set is a contract."""
    target = _cli_row_id(cli_ready, "lab_result", "test_name", "HbA1c")
    assert _run(cli_ready, "record", "edit", "lab_result", str(target),
                "--set", "unit=percent", "--note", "normalise", "--apply") == 0
    capsys.readouterr()
    assert _run(cli_ready, "record", "rm", "lab_result", str(target), "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert [e["field"] for e in payload["edits_recorded"]] == ["unit"]


def test_cli_record_edit_list(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "record", "edit", "--list") == 0
    assert "no record corrections recorded" in capsys.readouterr().out

    target = _cli_row_id(cli_ready, "lab_result", "test_name", "HbA1c")
    assert _run(cli_ready, "record", "edit", "lab_result", str(target),
                "--set", "unit=percent", "--note", "normalise", "--apply") == 0
    capsys.readouterr()
    assert _run(cli_ready, "record", "edit", "--list", "lab_result") == 0
    out = capsys.readouterr().out
    assert "unit: % -> percent" in out and "HbA1c" in out

    capsys.readouterr()
    assert _run(cli_ready, "record", "edit", "--list", "--json") == 0
    entries = json.loads(capsys.readouterr().out)
    assert len(entries) == 1 and entries[0]["field"] == "unit"


def test_cli_record_edit_refusals_are_friendly_rc1(cli_ready, capsys):
    target = _cli_row_id(cli_ready, "lab_result", "test_name", "HbA1c")
    capsys.readouterr()
    assert _run(cli_ready, "record", "edit", "lab_result", str(target),
                "--set", "test_name=A1c", "--note", "rename", "--apply") == 1
    assert "part of the row's identity" in capsys.readouterr().err

    assert _run(cli_ready, "record", "edit", "lab_result", "9999",
                "--set", "unit=lb", "--note", "n", "--apply") == 1
    assert "no lab_result with id 9999" in capsys.readouterr().err

    assert _run(cli_ready, "record", "edit", "lab_result", str(target),
                "--set", "value_num=abc", "--note", "n", "--apply") == 1
    assert "value_num" in capsys.readouterr().err
    assert _cli_field(cli_ready, "lab_result", target, "unit") == "%"


@pytest.mark.parametrize("argv", [
    ("record", "edit", "lab_result", "1", "--set", "unit", "--note", "n"),
    ("record", "edit", "lab_result", "1", "--set", "unit=a", "--set", "unit=b",
     "--note", "n"),
    ("record", "edit", "lab_result", "1", "--set", "colour=blue", "--note", "n"),
    ("record", "edit", "lab_result", "1", "--note", "n"),          # no --set
    ("record", "edit", "lab_result", "1", "--set", "unit=lb"),     # no --note
    ("record", "edit", "lab_result", "--set", "unit=lb", "--note", "n"),  # no row id
    ("record", "edit", "family_history", "1", "--set", "unit=lb", "--note", "n"),
])
def test_cli_record_edit_misuse_is_argparse_rc2(cli_ready, argv):
    with pytest.raises(SystemExit) as exc:
        _run(cli_ready, *argv)
    assert exc.value.code == 2

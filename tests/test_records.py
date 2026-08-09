"""Row-level record repair: `pemr record rm` (issue #107).

Covers the module surface (pemr/records.py) and the CLI wiring, in the shape
test_documents.py uses for the `document` group. The scenario the verb exists for
— a doubled fact quarantining a table from `rekey` — gets its own end-to-end test.
"""

import json

import pytest

from pemr import cli, db, dedup, documents, persons, records

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

"""Misfiled-document recovery: `pemr document list|edit|reassign|rm` (issue #54).

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
}


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
    assert views[1]["record_count"] == 5
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
    assert len(report.changes) == 5

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
    assert report.applied is False and len(report.changes) == 5
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
    """A conflict raised by *and* anchored to the same document (two colliding rows in
    one extraction) appears once in the refusal, not twice."""
    conn = seeded["conn"]
    other = _insert_document(conn, seeded["jane"].person_id, "dd99")
    summary = dedup.commit_extraction(conn, other, {"lab_result": [
        {"test_name": "Glucose", "collected_at": "2026-01-02", "value_num": 88},
        {"test_name": "Glucose", "collected_at": "2026-01-02", "value_num": 99},
    ]})
    assert summary.counts["conflict"] == 1

    with pytest.raises(documents.OpenConflictsError) as exc:
        documents.reassign_document(conn, other, "john-doe", apply=True)
    assert str(exc.value).count("#1") == 1


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
    assert report.applied is False and report.record_count == 5
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 1


def test_rm_apply_cascades_records_and_fts(seeded):
    conn = seeded["conn"]
    report = documents.remove_document(conn, seeded["doc"], apply=True)
    assert report.applied is True and report.record_count == 5
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
    summary = dedup.commit_extraction(conn, other, {"lab_result": [
        {"test_name": "Glucose", "collected_at": "2026-01-02", "value_num": 88},
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
    assert _run(tmp_path, "migrate") == 0
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
    assert _run(tmp_path, "migrate") == 0
    capsys.readouterr()
    assert _run(tmp_path, "document", "list") == 0
    assert "no documents yet" in capsys.readouterr().out


def test_cli_document_list_and_json(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "list") == 0
    out = capsys.readouterr().out
    assert "#1" in out and "jane-doe" in out and "5 rec" in out

    assert _run(cli_ready, "document", "list", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["record_count"] == 5
    assert payload[0]["has_ocr_text"] is True
    assert "ocr_text" not in payload[0]


def test_cli_document_list_unknown_person_fails(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "list", "--person", "nobody") == 1
    assert "no person with slug" in capsys.readouterr().err


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
    assert "dry run: 5 record(s) would move" in out and "--apply" in out

    assert _run(cli_ready, "document", "reassign", "1", "--person", "john-doe",
                "--apply") == 0
    assert "reassigned 5 record(s)" in capsys.readouterr().out

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
    assert len(payload["moved"]) == 5


def test_cli_document_reassign_unknown_person_fails(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "reassign", "1", "--person", "nobody") == 1
    assert "no person with slug" in capsys.readouterr().err


def test_cli_document_rm_dry_run_then_apply(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "rm", "1") == 0
    out = capsys.readouterr().out
    assert "records: 5 total" in out and "blob kept" in out
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
        ("document", "reassign", "1", "--person", "john-doe"),
        ("document", "rm", "1"),
    ):
        assert _run(cli_ready, *argv) == 0
        captured = capsys.readouterr()
        for text in (captured.out, captured.err):
            assert text.isascii(), repr(text)
            text.encode("cp437")

"""Intentional-removal memory for layer-1 dedup (issue #80).

The bug: layer-1 identity is a lookup against the *live* `document` table, so
`document rm` erases every trace that a hash was ever seen and the next bulk sweep
re-ingests the file silently. These cover the opt-in fix end to end — the engine
(`pemr/tombstones.py`), the ingest check, `document rm --tombstone`, the
`document tombstone list|add|rm` verbs, and the `--purge-blob` over-promise warning.

`restore`'s pre-replace loss note lives in test_restore.py, next to the rest of the
restore mechanics.
"""

import json

import pytest

from pemr import cli, db, documents, ingest, persons, tombstones, verify


@pytest.fixture()
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    persons.add_person(conn, "jane-doe", "Jane Doe")
    yield conn
    conn.close()


@pytest.fixture()
def sources(tmp_path):
    return tmp_path / "sources"


SHA_A = "a" * 64
SHA_B = "b" * 64


def _make_file(tmp_path, name="scan.txt", content=b"lab report bytes"):
    p = tmp_path / name
    p.write_bytes(content)
    return p


# --- engine -----------------------------------------------------------------


def test_add_and_get_round_trip(conn):
    row = tombstones.add_tombstone(
        conn, SHA_A, reason="identifiers", note="dad's 2019 insurance card"
    )
    assert row["sha256"] == SHA_A
    assert row["reason"] == "identifiers"
    assert tombstones.get_tombstone(conn, SHA_A) == row
    assert tombstones.get_tombstone(conn, SHA_B) is None


def test_blank_reason_and_note_store_as_null(conn):
    row = tombstones.add_tombstone(conn, SHA_A, reason="   ", note="")
    assert row["reason"] is None and row["note"] is None


def test_add_is_an_upsert_not_an_error(conn):
    first = tombstones.add_tombstone(conn, SHA_A, reason="typo", now="2026-01-01T00:00:00+00:00")
    second = tombstones.add_tombstone(
        conn, SHA_A, reason="identifiers", now="2026-02-02T00:00:00+00:00"
    )
    assert first["reason"] == "typo" and second["reason"] == "identifiers"
    assert second["removed_at"].startswith("2026-02-02")
    assert len(tombstones.list_tombstones(conn)) == 1


def test_add_refuses_a_live_document(conn, tmp_path, sources):
    result = ingest.ingest_document(conn, _make_file(tmp_path), "jane-doe", sources)
    with pytest.raises(tombstones.DocumentLiveError) as exc:
        tombstones.add_tombstone(conn, result.document.sha256)
    assert f"#{result.document.document_id}" in str(exc.value)
    assert tombstones.get_tombstone(conn, result.document.sha256) is None


def test_list_is_newest_first_and_marks_live_hashes(conn, tmp_path, sources):
    tombstones.add_tombstone(conn, SHA_A, now="2026-01-01T00:00:00+00:00")
    tombstones.add_tombstone(conn, SHA_B, now="2026-03-03T00:00:00+00:00")
    rows = tombstones.list_tombstones(conn)
    assert [r["sha256"] for r in rows] == [SHA_B, SHA_A]
    assert all(r["live_document_id"] is None for r in rows)

    # `ingest --force` leaves a hash both filed and excluded - deliberate, so it is
    # labelled rather than hidden.
    src = _make_file(tmp_path)
    tombstones.add_tombstone(conn, ingest.hash_file(src))
    forced = ingest.ingest_document(conn, src, "jane-doe", sources, force=True)
    live = [r for r in tombstones.list_tombstones(conn) if r["live_document_id"]]
    assert [r["live_document_id"] for r in live] == [forced.document.document_id]


def test_remove_lifts_and_unknown_hash_raises(conn):
    tombstones.add_tombstone(conn, SHA_A, reason="identifiers")
    lifted = tombstones.remove_tombstone(conn, SHA_A)
    assert lifted["reason"] == "identifiers"
    assert tombstones.get_tombstone(conn, SHA_A) is None
    with pytest.raises(tombstones.TombstoneNotFoundError):
        tombstones.remove_tombstone(conn, SHA_A)


@pytest.mark.parametrize(
    "bad", ["", "   ", "abc", SHA_A[:63], SHA_A + "a", "z" * 64, "not a hash"]
)
def test_normalize_sha256_rejects_malformed_input(bad):
    with pytest.raises(ValueError):
        tombstones.normalize_sha256(bad)


def test_normalize_sha256_strips_and_lowercases():
    assert tombstones.normalize_sha256(f"  {SHA_A.upper()}\n") == SHA_A


# --- ingest check -----------------------------------------------------------


def test_ingest_of_a_tombstoned_hash_writes_nothing(conn, tmp_path, sources):
    src = _make_file(tmp_path)
    sha = ingest.hash_file(src)
    tombstones.add_tombstone(conn, sha, reason="identifiers", note="insurance card")

    result = ingest.ingest_document(conn, src, "jane-doe", sources)
    assert result.status == "tombstoned" and result.is_tombstoned
    assert result.document is None
    assert result.tombstone["reason"] == "identifiers"
    assert result.ocr_text_populated is False       # must not dereference a None document
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 0
    assert not sources.exists()                     # no blob copied either


def test_a_live_document_still_wins_over_a_tombstone(conn, tmp_path, sources):
    """`document` is checked first: filed is a different answer than excluded."""
    src = _make_file(tmp_path)
    first = ingest.ingest_document(conn, src, "jane-doe", sources)
    tombstones.add_tombstone(
        conn, first.document.sha256, allow_live=True  # only reachable deliberately
    )
    again = ingest.ingest_document(conn, src, "jane-doe", sources)
    assert again.status == "duplicate"


def test_force_ingests_but_does_not_lift_the_tombstone(conn, tmp_path, sources):
    src = _make_file(tmp_path)
    sha = ingest.hash_file(src)
    tombstones.add_tombstone(conn, sha, reason="identifiers")

    forced = ingest.ingest_document(conn, src, "jane-doe", sources, force=True)
    assert forced.status == "new"
    # The row rides back so the caller can warn, and is still there afterwards: force
    # is a one-shot override, not a lift.
    assert forced.tombstone is not None
    assert tombstones.get_tombstone(conn, sha) is not None


def test_tombstone_force_false_withholds_the_override(conn, tmp_path, sources):
    """The MCP stance: a tombstone is the human's recorded decision, not a heuristic."""
    src = _make_file(tmp_path)
    tombstones.add_tombstone(conn, ingest.hash_file(src))
    result = ingest.ingest_document(
        conn, src, "jane-doe", sources, force=True, tombstone_force=False
    )
    assert result.status == "tombstoned"


def test_study_ingest_honours_a_tombstone(conn, tmp_path, sources):
    from test_study import make_study

    study_dir = make_study(tmp_path / "disc")
    first = ingest.ingest_study_dir(conn, study_dir, "jane-doe", sources)
    report = documents.remove_document(
        conn, first.document.document_id, tombstone=True, reason="wrong-household",
        apply=True,
    )
    assert report.tombstoned
    again = ingest.ingest_study_dir(conn, study_dir, "jane-doe", sources)
    assert again.status == "tombstoned"
    assert again.tombstone["reason"] == "wrong-household"


# --- document rm --tombstone ------------------------------------------------


def _ingested(conn, tmp_path, sources, content=b"lab report bytes"):
    src = _make_file(tmp_path, "scan.txt", content)
    return src, ingest.ingest_document(conn, src, "jane-doe", sources)


def test_rm_without_tombstone_leaves_no_memory(conn, tmp_path, sources):
    src, result = _ingested(conn, tmp_path, sources)
    documents.remove_document(conn, result.document.document_id, apply=True)
    assert tombstones.list_tombstones(conn) == []
    # The bug as filed: a re-sweep re-ingests it as brand new.
    assert ingest.ingest_document(conn, src, "jane-doe", sources).status == "new"


def test_rm_dry_run_reports_the_tombstone_but_writes_nothing(conn, tmp_path, sources):
    src, result = _ingested(conn, tmp_path, sources)
    report = documents.remove_document(
        conn, result.document.document_id, tombstone=True, reason="identifiers"
    )
    assert report.tombstoned is True and report.tombstone_reason == "identifiers"
    assert report.applied is False
    assert tombstones.list_tombstones(conn) == []
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 1


def test_rm_apply_records_the_tombstone_and_blocks_re_ingest(conn, tmp_path, sources):
    src, result = _ingested(conn, tmp_path, sources)
    doc_id, sha = result.document.document_id, result.document.sha256
    report = documents.remove_document(
        conn, doc_id, tombstone=True, reason="identifiers", note="insurance card",
        apply=True,
    )
    assert report.tombstoned and report.tombstone_existed is False
    stored = tombstones.get_tombstone(conn, sha)
    assert stored["document_id"] == doc_id     # forensics: the id it had
    assert stored["note"] == "insurance card"
    assert ingest.ingest_document(conn, src, "jane-doe", sources).status == "tombstoned"


def test_rm_tombstone_is_one_transaction_with_the_delete(
    conn, tmp_path, sources, monkeypatch
):
    """A delete that commits without its tombstone is the exact failure being fixed."""
    src, result = _ingested(conn, tmp_path, sources)
    sha = result.document.sha256

    def exploding(*args, **kwargs):
        raise RuntimeError("write failed mid-transaction")

    monkeypatch.setattr(documents.tombstones, "add_tombstone", exploding)
    with pytest.raises(RuntimeError):
        documents.remove_document(
            conn, result.document.document_id, tombstone=True, apply=True
        )
    # The delete rolled back with it: the document and its rows are still there.
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM document WHERE sha256 = ?", (sha,)
    ).fetchone()["n"] == 1


def test_rm_tombstone_after_a_forced_ingest_updates_in_place(conn, tmp_path, sources):
    src = _make_file(tmp_path)
    sha = ingest.hash_file(src)
    tombstones.add_tombstone(conn, sha, reason="typo")
    forced = ingest.ingest_document(conn, src, "jane-doe", sources, force=True)

    report = documents.remove_document(
        conn, forced.document.document_id, tombstone=True, reason="identifiers",
        apply=True,
    )
    assert report.tombstone_existed is True
    assert tombstones.get_tombstone(conn, sha)["reason"] == "identifiers"
    assert len(tombstones.list_tombstones(conn)) == 1


# --- CLI --------------------------------------------------------------------


def _run(tmp_path, *argv):
    return cli.main([
        "--db", str(tmp_path / "cli.db"),
        "--config", str(tmp_path / "absent.toml"), *argv,
    ])


@pytest.fixture()
def cli_ready(tmp_path):
    """Migrated DB, one person, one ingested document owned by jane."""
    assert _run(tmp_path, "migrate", "--create") == 0
    assert _run(tmp_path, "person", "add", "--slug", "jane-doe", "--name", "Jane") == 0
    scan = tmp_path / "scan.txt"
    scan.write_bytes(b"hba1c 5.7 percent")
    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources")) == 0
    return tmp_path


def _assert_console_safe(text: str) -> None:
    assert text.isascii(), f"non-ASCII CLI output would garble on cp437: {text!r}"
    text.encode("cp437")


def test_cli_rm_tombstone_then_ingest_is_skipped(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "rm", "1", "--tombstone", "--reason",
                "identifiers", "--note", "dad's card", "--apply") == 0
    out = capsys.readouterr().out
    assert "tombstone recorded (reason: identifiers)" in out

    capsys.readouterr()
    assert _run(cli_ready, "ingest", str(cli_ready / "scan.txt"), "--person",
                "jane-doe", "--sources", str(cli_ready / "sources")) == 0
    captured = capsys.readouterr()
    assert "skipped: tombstoned" in captured.out
    assert "reason: identifiers" in captured.out
    assert "dad's card" in captured.out
    assert "next:" not in captured.out          # nothing to extract
    _assert_console_safe(captured.out)
    _assert_console_safe(captured.err)


def test_cli_rm_dry_run_says_would_record(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "rm", "1", "--tombstone") == 0
    assert "would record tombstone" in capsys.readouterr().out
    assert _run(cli_ready, "document", "tombstone", "list") == 0
    assert "no tombstones recorded" in capsys.readouterr().out


def test_cli_reason_without_tombstone_is_an_argparse_error(cli_ready, capsys):
    with pytest.raises(SystemExit):
        _run(cli_ready, "document", "rm", "1", "--reason", "identifiers")
    assert "--reason/--note only apply with --tombstone" in capsys.readouterr().err


def test_cli_ingest_force_past_a_tombstone_warns_and_keeps_it(cli_ready, capsys):
    assert _run(cli_ready, "document", "rm", "1", "--tombstone", "--apply") == 0
    capsys.readouterr()
    assert _run(cli_ready, "ingest", str(cli_ready / "scan.txt"), "--person",
                "jane-doe", "--sources", str(cli_ready / "sources"), "--force") == 0
    captured = capsys.readouterr()
    assert "ingested document" in captured.out
    assert "tombstone was NOT lifted" in captured.err
    _assert_console_safe(captured.err)

    capsys.readouterr()
    assert _run(cli_ready, "document", "tombstone", "list") == 0
    assert "(live as document #" in capsys.readouterr().out


def test_cli_purge_blob_warns_without_a_tombstone(cli_ready, capsys):
    # Dry run too: a warning that only appears after the irreversible run is useless.
    capsys.readouterr()
    assert _run(cli_ready, "document", "rm", "1", "--purge-blob",
                "--sources", str(cli_ready / "sources")) == 0
    err = capsys.readouterr().err
    assert "does not prevent re-ingest" in err
    _assert_console_safe(err)

    capsys.readouterr()
    assert _run(cli_ready, "document", "rm", "1", "--purge-blob", "--apply",
                "--sources", str(cli_ready / "sources")) == 0
    err = capsys.readouterr().err
    assert "does not prevent re-ingest" in err
    assert "git history" in err          # the unconditional purge caveat


def test_cli_purge_blob_with_a_tombstone_does_not_warn(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "rm", "1", "--purge-blob", "--tombstone",
                "--apply", "--sources", str(cli_ready / "sources")) == 0
    err = capsys.readouterr().err
    assert "does not prevent re-ingest" not in err


def test_cli_purge_blob_does_not_nag_when_a_tombstone_already_exists(
    cli_ready, capsys
):
    assert _run(cli_ready, "document", "rm", "1", "--tombstone", "--apply") == 0
    scan = cli_ready / "scan.txt"
    assert _run(cli_ready, "ingest", str(scan), "--person", "jane-doe", "--sources",
                str(cli_ready / "sources"), "--force") == 0
    # SQLite reuses the rowid of the only deleted row, so the re-ingest is #1 again.
    capsys.readouterr()
    assert _run(cli_ready, "document", "rm", "1", "--purge-blob", "--apply",
                "--sources", str(cli_ready / "sources")) == 0
    err = capsys.readouterr().err
    # The tombstone from the first rm still exists, so the nag is suppressed.
    assert "does not prevent re-ingest" not in err


def _sha_of(tmp_path, document_id: int) -> str:
    conn = db.connect(tmp_path / "cli.db")
    try:
        return conn.execute(
            "SELECT sha256 FROM document WHERE document_id = ?", (document_id,)
        ).fetchone()["sha256"]
    finally:
        conn.close()


def test_cli_tombstone_add_by_file_ingests_nothing(cli_ready, capsys):
    excluded = cli_ready / "never-file-this.txt"
    excluded.write_bytes(b"a document carrying identifiers")
    capsys.readouterr()
    assert _run(cli_ready, "document", "tombstone", "add", "--file", str(excluded),
                "--reason", "identifiers") == 0
    out = capsys.readouterr().out
    assert "tombstoned" in out
    _assert_console_safe(out)

    # No blob copied, no document row added (the document from cli_ready is #1).
    assert _run(cli_ready, "document", "list", "--json") == 0
    assert len(json.loads(capsys.readouterr().out)) == 1
    sha = ingest.hash_file(excluded)
    assert not (cli_ready / "sources" / sha[:2] / f"{sha}.txt").exists()

    capsys.readouterr()
    assert _run(cli_ready, "ingest", str(excluded), "--person", "jane-doe",
                "--sources", str(cli_ready / "sources")) == 0
    assert "skipped: tombstoned" in capsys.readouterr().out


def test_cli_tombstone_add_by_sha256_and_bad_hash(cli_ready, capsys):
    assert _run(cli_ready, "document", "tombstone", "add", "--sha256", SHA_A) == 0
    capsys.readouterr()
    assert _run(cli_ready, "document", "tombstone", "add", "--sha256", "nope") == 1
    assert "not a sha256 content hash" in capsys.readouterr().err


def test_cli_tombstone_add_needs_exactly_one_source(cli_ready):
    with pytest.raises(SystemExit):
        _run(cli_ready, "document", "tombstone", "add")
    with pytest.raises(SystemExit):
        _run(cli_ready, "document", "tombstone", "add", "--sha256", SHA_A,
             "--file", str(cli_ready / "scan.txt"))


def test_cli_tombstone_add_for_a_live_document_is_refused(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "tombstone", "add", "--file",
                str(cli_ready / "scan.txt")) == 1
    err = capsys.readouterr().err
    assert "live as document #1" in err
    assert "--tombstone --apply" in err


def test_cli_tombstone_add_unreadable_file_is_friendly(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "tombstone", "add", "--file",
                str(cli_ready / "absent.txt")) == 1
    assert "cannot read" in capsys.readouterr().err


def test_cli_tombstone_list_empty_and_populated(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "tombstone", "list") == 0
    assert capsys.readouterr().out.strip() == "no tombstones recorded"

    assert _run(cli_ready, "document", "rm", "1", "--tombstone", "--reason",
                "identifiers", "--note", "insurance card", "--apply") == 0
    capsys.readouterr()
    assert _run(cli_ready, "document", "tombstone", "list") == 0
    out = capsys.readouterr().out
    assert "identifiers" in out and "insurance card" in out and "#1" in out
    _assert_console_safe(out)

    assert _run(cli_ready, "document", "tombstone", "list", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows[0]["sha256"]) == 64          # full hash in --json
    assert rows[0]["live_document_id"] is None


def test_cli_tombstone_rm_lifts_and_re_ingest_succeeds(cli_ready, capsys):
    sha = _sha_of(cli_ready, 1)
    assert _run(cli_ready, "document", "rm", "1", "--tombstone", "--apply") == 0
    capsys.readouterr()
    assert _run(cli_ready, "document", "tombstone", "rm", sha) == 0
    assert "lifted tombstone" in capsys.readouterr().out

    assert _run(cli_ready, "ingest", str(cli_ready / "scan.txt"), "--person",
                "jane-doe", "--sources", str(cli_ready / "sources")) == 0
    capsys.readouterr()
    assert _run(cli_ready, "document", "list", "--json") == 0
    assert len(json.loads(capsys.readouterr().out)) == 1


def test_cli_tombstone_rm_unknown_hash_is_rc1(cli_ready, capsys):
    capsys.readouterr()
    assert _run(cli_ready, "document", "tombstone", "rm", SHA_B) == 1
    assert "no tombstone for" in capsys.readouterr().err
    # A truncated hash is a format error, not a silent miss.
    assert _run(cli_ready, "document", "tombstone", "rm", SHA_B[:12]) == 1
    assert "not a sha256 content hash" in capsys.readouterr().err


# --- verify -----------------------------------------------------------------


def test_verify_counts_tombstones(conn):
    tombstones.add_tombstone(conn, SHA_A)
    assert verify.row_counts(conn)["document_tombstone"] == 1


def test_verify_omits_the_table_on_a_pre_007_schema(tmp_path):
    """The existing "omit tables missing from the schema" rule covers old snapshots."""
    import shutil

    staged = tmp_path / "pre007"
    staged.mkdir()
    for path in sorted(db.DEFAULT_MIGRATIONS_DIR.glob("*.sql")):
        if path.name < "007":
            shutil.copy(path, staged / path.name)
    old = db.connect(tmp_path / "old.db")
    try:
        db.migrate(old, staged)
        assert "document_tombstone" not in verify.row_counts(old)
        assert tombstones.has_table(old) is False
    finally:
        old.close()

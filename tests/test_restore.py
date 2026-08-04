"""Issue #55: `pemr restore` / `pemr verify` + the `migrate` bootstrap gate.

This is the issue's own recovery drill, automated: seed a populated DB, `pemr backup`,
delete `pemr.db`, `pemr restore latest`, and assert the row counts, `schema_migrations`
state and `document.source_path` blob resolution all match pre-delete.

Around that, the failure modes the manual drill exposed - which are the point of having
a command at all:

* `pemr migrate` on a missing (or zero-byte) DB must refuse instead of manufacturing a
  convincing empty archive that the next `pemr backup` would then snapshot;
* restoring over a live DB requires `--force` and banks a `pemr-prerestore-*.sqlite`
  rescue copy that rotation is structurally unable to prune;
* stale `-wal`/`-shm` sidecars are cleared, so SQLite cannot replay a stale WAL over
  the restored file;
* a corrupt/foreign snapshot is rejected before the live DB is touched;
* missing blobs are reported as a warning, not a failure.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from pemr import backup, cli, db, restore, tombstones, verify


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _run(db_path: Path, *argv) -> int:
    # --config points at a file that does not exist so a stray ./config.toml in the
    # working dir can never leak into resolution.
    return cli.main(
        ["--db", str(db_path), "--config", str(db_path.parent / "absent.toml"), *argv]
    )


def _populated(tmp_path: Path) -> tuple[Path, Path]:
    """A migrated DB with a person and a real ingested document (so blobs exist)."""
    db_path = tmp_path / "pemr.db"
    sources = tmp_path / "sources"
    assert _run(db_path, "migrate", "--create") == 0
    assert _run(db_path, "person", "add", "--slug", "jane-doe", "--name", "Jane Doe") == 0
    scan = tmp_path / "scan.txt"
    scan.write_bytes(b"hba1c 5.7 percent")
    assert _run(db_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(sources)) == 0
    return db_path, sources


def _snapshot_state(db_path: Path) -> tuple[dict[str, int], list[str]]:
    conn = db.connect(db_path)
    try:
        return verify.row_counts(conn), sorted(db.applied_versions(conn))
    finally:
        conn.close()


def _corrupt_file(path: Path) -> Path:
    path.write_bytes(b"this is not a sqlite database at all" * 8)
    return path


# --------------------------------------------------------------------------- #
# the drill (the issue's acceptance criterion)
# --------------------------------------------------------------------------- #

def test_full_restore_drill(tmp_path, capsys):
    db_path, sources = _populated(tmp_path)
    backups = tmp_path / "backups"
    before_counts, before_migrations = _snapshot_state(db_path)

    assert _run(db_path, "backup", "--backup-dir", str(backups)) == 0

    # The disaster: the live DB (and its sidecars) are gone.
    for path in [db_path, *[db_path.with_name(db_path.name + s) for s in ("-wal", "-shm")]]:
        if path.exists():
            path.unlink()
    assert not db_path.exists()

    capsys.readouterr()
    rc = _run(db_path, "restore", "latest", "--backup-dir", str(backups),
              "--sources", str(sources))
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert db_path.is_file()

    after_counts, after_migrations = _snapshot_state(db_path)
    assert after_counts == before_counts
    assert after_migrations == before_migrations
    assert before_counts["document"] >= 1  # the drill would be vacuous otherwise

    # Every document.source_path still resolves with a matching sha256.
    assert "0 missing, 0 mismatched" in out.out
    assert "warning" not in out.err


def test_restore_of_absent_db_needs_no_force(tmp_path):
    """Ergonomics are deliberately easiest in the case where the user is panicking."""
    db_path, sources = _populated(tmp_path)
    backups = tmp_path / "backups"
    assert _run(db_path, "backup", "--backup-dir", str(backups)) == 0
    db_path.unlink()
    assert _run(db_path, "restore", "latest", "--backup-dir", str(backups),
                "--sources", str(sources)) == 0


# --------------------------------------------------------------------------- #
# migrate bootstrap gate
# --------------------------------------------------------------------------- #

def test_migrate_on_missing_db_refuses_and_creates_nothing(tmp_path, capsys):
    db_path = tmp_path / "pemr.db"
    with pytest.raises(SystemExit) as exc:
        _run(db_path, "migrate")
    message = str(exc.value)
    assert "no database at" in message
    assert "pemr migrate --create" in message
    assert "pemr restore latest" in message
    assert not db_path.exists(), "the refusal must not leave a database behind"


def test_migrate_create_bootstraps(tmp_path, capsys):
    db_path = tmp_path / "pemr.db"
    assert _run(db_path, "migrate", "--create") == 0
    assert "applied 001_init.sql" in capsys.readouterr().out
    conn = db.connect(db_path)
    try:
        assert db.is_migrated(conn)
    finally:
        conn.close()
    # Once it exists, plain `migrate` works again (idempotent, no --create needed).
    assert _run(db_path, "migrate") == 0
    assert "up to date" in capsys.readouterr().out


def test_migrate_on_zero_byte_db_refuses(tmp_path):
    """A 0-byte pemr.db is sqlite3.connect debris, not an archive (db.database_exists)."""
    db_path = tmp_path / "pemr.db"
    db_path.touch()
    assert db_path.stat().st_size == 0
    with pytest.raises(SystemExit) as exc:
        _run(db_path, "migrate")
    assert "no database at" in str(exc.value)
    assert db_path.stat().st_size == 0, "must not have been silently populated"


def test_read_commands_refuse_on_missing_db(tmp_path):
    db_path = tmp_path / "pemr.db"
    with pytest.raises(SystemExit) as exc:
        _run(db_path, "person", "list")
    assert "no database at" in str(exc.value)
    assert not db_path.exists()


@pytest.mark.parametrize("argv", [
    ("document", "list"),
    ("document", "edit", "1", "--category", "labs"),
    ("document", "reassign", "1", "--person", "jane-doe"),
    ("document", "rm", "1"),
])
def test_document_commands_refuse_on_missing_db(tmp_path, argv):
    """Issue #54's `document` subcommands landed after this gate was designed and
    opened the archive themselves, so on `dev` `pemr document list` against an absent
    `pemr.db` created a schema-less 4 KB file and then advised `pemr migrate` - the
    manufacture-an-empty-archive chain this issue exists to close, reopened by a
    *read* command. They go through `cli._connect_db` like every other command.
    """
    db_path = tmp_path / "pemr.db"
    with pytest.raises(SystemExit) as exc:
        _run(db_path, *argv)
    assert "no database at" in str(exc.value)
    assert not db_path.exists(), "a read command must never manufacture an archive"


def test_no_cli_command_bypasses_the_connect_gate():
    """Structural guard on the invariant above.

    The gate is only as good as its coverage: one new `db.connect(_resolve_db_path(
    args))` call site silently reopens the hole (that is exactly how #54's `document`
    commands arrived). `_cmd_migrate` is the single documented exception - it must be
    able to create a database under `--create`.
    """
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "db.connect(_resolve_db_path(args))" not in source, (
        "a CLI command connects without going through cli._connect_db - route it "
        "through the gate (issue #55)"
    )
    # Two legitimate `db.connect(` sites remain: inside _connect_db, and _cmd_migrate.
    assert source.count("db.connect(") == 2


def test_database_exists_semantics(tmp_path):
    missing = tmp_path / "nope.db"
    assert not db.database_exists(missing)
    missing.touch()
    assert not db.database_exists(missing)  # zero bytes
    missing.write_bytes(b"x")
    assert db.database_exists(missing)
    assert not db.database_exists(tmp_path)  # a directory is not a database
    assert db.database_exists(":memory:")


# --------------------------------------------------------------------------- #
# destination guard + rescue copy
# --------------------------------------------------------------------------- #

def test_restore_over_existing_db_requires_force(tmp_path, capsys):
    db_path, sources = _populated(tmp_path)
    backups = tmp_path / "backups"
    assert _run(db_path, "backup", "--backup-dir", str(backups)) == 0
    before = db_path.read_bytes()

    capsys.readouterr()
    rc = _run(db_path, "restore", "latest", "--backup-dir", str(backups),
              "--sources", str(sources))
    assert rc == 1
    err = capsys.readouterr().err
    assert "already exists" in err and "--force" in err
    assert db_path.read_bytes() == before, "a refused restore must change nothing"


def test_force_restore_banks_a_rescue_copy_rotation_cannot_prune(tmp_path):
    db_path, sources = _populated(tmp_path)
    backups = tmp_path / "backups"
    assert _run(db_path, "backup", "--backup-dir", str(backups)) == 0
    assert _run(db_path, "restore", "latest", "--backup-dir", str(backups),
                "--sources", str(sources), "--force") == 0

    rescues = list(backups.glob(f"{restore.RESCUE_PREFIX}*.sqlite"))
    assert len(rescues) == 1
    rescue = rescues[0]

    # The off-pattern name is the whole point: rotation cannot even parse it, so the
    # most destructive retention setting there is leaves it alone.
    assert backup._parse_ts(rescue.name) is None
    assert _run(db_path, "backup", "--backup-dir", str(backups),
                "--keep-daily", "0", "--keep-weekly", "0") == 0
    assert rescue.exists()

    # And it is a real, usable database - not just a file with the right name.
    assert backup.integrity_check(rescue) == "ok"


def test_two_force_restores_in_the_same_second_both_keep_their_rescue_copy(tmp_path):
    """The rescue name collides at second precision; that must not abort a restore."""
    db_path, sources = _populated(tmp_path)
    backups = tmp_path / "backups"
    assert _run(db_path, "backup", "--backup-dir", str(backups)) == 0
    frozen = datetime(2026, 8, 2, 4, 8, 15)

    for _ in range(2):
        restore.restore("latest", db_path, backup_dir=backups, sources_dir=sources,
                        force=True, now=frozen)

    rescues = {p.name for p in backups.glob(f"{restore.RESCUE_PREFIX}*.sqlite")}
    assert rescues == {
        "pemr-prerestore-20260802-040815.sqlite",
        "pemr-prerestore-20260802-040815-2.sqlite",
    }
    # The collision suffix must stay off the rotation pattern, like the base name.
    assert all(backup._parse_ts(name) is None for name in rescues)


def test_a_failed_install_leaves_the_sidecars_intact(tmp_path, monkeypatch):
    """A stale -wal holds committed transactions: it must outlive an aborted install.

    Hence the unlink runs *after* `os.replace`, not before: deleting the sidecars on a
    path that then fails to install is the one way this code could lose data.
    """
    db_path, sources = _populated(tmp_path)
    backups = tmp_path / "backups"
    assert _run(db_path, "backup", "--backup-dir", str(backups)) == 0
    db_path.unlink()
    wal = db_path.with_name(db_path.name + "-wal")
    shm = db_path.with_name(db_path.name + "-shm")
    wal.write_bytes(b"stale wal holding committed transactions")
    shm.write_bytes(b"stale shm")

    def boom(src, dst):
        raise OSError(5, "Access is denied")

    monkeypatch.setattr(restore.os, "replace", boom)
    with pytest.raises(restore.RestoreError, match="cannot install"):
        restore.restore("latest", db_path, backup_dir=backups, sources_dir=sources)

    assert wal.read_bytes() == b"stale wal holding committed transactions"
    assert shm.exists()
    assert not list(tmp_path.glob("*.restore-tmp"))


def test_restore_clears_stale_wal_and_shm_sidecars(tmp_path):
    db_path, sources = _populated(tmp_path)
    backups = tmp_path / "backups"
    assert _run(db_path, "backup", "--backup-dir", str(backups)) == 0
    wal = db_path.with_name(db_path.name + "-wal")
    shm = db_path.with_name(db_path.name + "-shm")
    wal.write_bytes(b"stale wal that sqlite must never replay")
    shm.write_bytes(b"stale shm")

    assert _run(db_path, "restore", "latest", "--backup-dir", str(backups),
                "--sources", str(sources), "--force") == 0
    assert not wal.exists() and not shm.exists()


# --------------------------------------------------------------------------- #
# source validation - nothing is touched until the snapshot proves itself
# --------------------------------------------------------------------------- #

def test_restore_of_corrupt_snapshot_leaves_live_db_untouched(tmp_path, capsys):
    db_path, sources = _populated(tmp_path)
    before = db_path.read_bytes()
    bogus = _corrupt_file(tmp_path / "not-a-db.sqlite")

    capsys.readouterr()
    rc = _run(db_path, "restore", str(bogus), "--sources", str(sources), "--force")
    assert rc == 1
    assert "integrity_check" in capsys.readouterr().err
    assert db_path.read_bytes() == before
    assert not list(tmp_path.glob("*.restore-tmp"))


def test_restore_of_valid_sqlite_without_pemr_schema_is_rejected(tmp_path, capsys):
    db_path, sources = _populated(tmp_path)
    before = db_path.read_bytes()
    foreign = tmp_path / "foreign.sqlite"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()

    capsys.readouterr()
    rc = _run(db_path, "restore", str(foreign), "--sources", str(sources), "--force")
    assert rc == 1
    assert "not a pemr snapshot" in capsys.readouterr().err
    assert db_path.read_bytes() == before


def test_restore_unknown_snapshot_is_friendly(tmp_path, capsys):
    db_path, _ = _populated(tmp_path)
    capsys.readouterr()
    assert _run(db_path, "restore", "nope.sqlite", "--backup-dir",
                str(tmp_path / "backups"), "--force") == 1
    assert "no such snapshot" in capsys.readouterr().err


def test_restore_latest_with_no_snapshots_is_friendly(tmp_path, capsys):
    db_path, _ = _populated(tmp_path)
    backups = tmp_path / "backups"
    backups.mkdir()
    capsys.readouterr()
    assert _run(db_path, "restore", "latest", "--backup-dir", str(backups),
                "--force") == 1
    assert "no snapshots found" in capsys.readouterr().err


def test_restore_refuses_the_live_database_as_its_own_source(tmp_path, capsys):
    db_path, _ = _populated(tmp_path)
    capsys.readouterr()
    assert _run(db_path, "restore", str(db_path), "--force") == 1
    assert "is the live database" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# snapshot resolution + forward migration
# --------------------------------------------------------------------------- #

def test_resolve_snapshot_accepts_path_bare_name_and_latest(tmp_path):
    db_path, _ = _populated(tmp_path)
    backups = tmp_path / "backups"
    assert _run(db_path, "backup", "--backup-dir", str(backups)) == 0
    snap = next(backups.glob("pemr-*.sqlite"))

    assert restore.resolve_snapshot(str(snap), None) == snap
    assert restore.resolve_snapshot(snap.name, backups) == snap
    assert restore.resolve_snapshot("latest", backups) == snap
    with pytest.raises(restore.RestoreError, match="needs a backup dir"):
        restore.resolve_snapshot("latest", None)


def test_latest_snapshot_orders_by_filename_not_mtime(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    older = backups / "pemr-20260101-1200.sqlite"
    newer = backups / "pemr-20260707-1200.sqlite"
    newer.write_bytes(b"stub")
    older.write_bytes(b"stub")  # written *last*, so mtime ordering would pick it
    assert restore.latest_snapshot(backups) == newer


def test_restore_of_snapshot_predating_a_migration_migrates_forward(tmp_path, capsys):
    db_path, sources = _populated(tmp_path)
    backups = tmp_path / "backups"
    assert _run(db_path, "backup", "--backup-dir", str(backups)) == 0
    db_path.unlink()

    # A migrations dir that is the shipped set plus one new migration: the snapshot is
    # now "older than the code", which is the normal restore case.
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    for src in sorted(Path(db.DEFAULT_MIGRATIONS_DIR).glob("*.sql")):
        (mdir / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    (mdir / "900_later.sql").write_text("CREATE TABLE later_table (x);", encoding="utf-8")

    capsys.readouterr()
    rc = _run(db_path, "restore", "latest", "--backup-dir", str(backups),
              "--sources", str(sources), "--migrations-dir", str(mdir))
    out = capsys.readouterr().out
    assert rc == 0
    assert "applied 900_later.sql" in out
    conn = db.connect(db_path)
    try:
        assert "900_later.sql" in db.applied_versions(conn)
    finally:
        conn.close()


def test_restore_json_payload(tmp_path, capsys):
    db_path, sources = _populated(tmp_path)
    backups = tmp_path / "backups"
    assert _run(db_path, "backup", "--backup-dir", str(backups)) == 0
    capsys.readouterr()
    assert _run(db_path, "restore", "latest", "--backup-dir", str(backups),
                "--sources", str(sources), "--force", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert Path(payload["database"]) == db_path
    assert payload["rescue"] is not None
    assert payload["report"]["ok"] is True
    assert payload["report"]["row_counts"]["document"] >= 1


# --------------------------------------------------------------------------- #
# blob resolution (`pemr verify`, and the tail of `pemr restore`)
# --------------------------------------------------------------------------- #

def test_missing_blob_is_a_warning_not_a_failure(tmp_path, capsys):
    db_path, sources = _populated(tmp_path)
    backups = tmp_path / "backups"
    assert _run(db_path, "backup", "--backup-dir", str(backups)) == 0
    db_path.unlink()
    for blob in sources.rglob("*"):
        if blob.is_file():
            blob.unlink()

    capsys.readouterr()
    rc = _run(db_path, "restore", "latest", "--backup-dir", str(backups),
              "--sources", str(sources))
    captured = capsys.readouterr()
    assert rc == 0, "the database restore genuinely succeeded"
    assert "blob missing" in captured.out
    assert "warning" in captured.err


def test_verify_reports_sha_mismatch(tmp_path, capsys):
    db_path, sources = _populated(tmp_path)
    blob = next(p for p in sources.rglob("*") if p.is_file())
    blob.write_bytes(b"tampered content")

    capsys.readouterr()
    rc = _run(db_path, "verify", "--sources", str(sources))
    out = capsys.readouterr().out
    assert rc == 1
    assert "sha256 mismatch" in out
    assert "1 mismatched" in out


def test_verify_ok_on_a_healthy_archive(tmp_path, capsys):
    db_path, sources = _populated(tmp_path)
    capsys.readouterr()
    assert _run(db_path, "verify", "--sources", str(sources)) == 0
    out = capsys.readouterr().out
    assert "integrity      ok" in out
    assert "0 missing, 0 mismatched" in out


def test_verify_json_and_skipped_blob_pass(tmp_path, capsys, monkeypatch):
    db_path, _ = _populated(tmp_path)
    monkeypatch.delenv("PEMR_SOURCES", raising=False)
    capsys.readouterr()
    # No --sources and no config: the blob pass is skipped with a note, never fatal.
    assert _run(db_path, "verify", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["blobs"]["skipped"]
    assert payload["ok"] is True
    assert payload["migrations"]


def test_verify_on_unmigrated_db_is_not_ok(tmp_path, capsys, unmigrated_db):
    """A schema-less file is a real problem, even though integrity_check says ok."""
    db_path = unmigrated_db(tmp_path / "pemr.db")
    capsys.readouterr()
    rc = _run(db_path, "verify", "--sources", str(tmp_path / "sources"))
    out = capsys.readouterr().out
    assert rc == 1
    assert "no schema applied" in out


def test_verify_json_exit_code_matches_the_console_one(tmp_path, capsys, unmigrated_db):
    """`--json` is the mode a monitoring cron picks; it must not report rc=0 here."""
    db_path = unmigrated_db(tmp_path / "pemr.db")
    capsys.readouterr()
    rc = _run(db_path, "verify", "--json", "--sources", str(tmp_path / "sources"))
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert rc == 1


def test_verify_report_caps_displayed_problems(tmp_path):
    report = verify.VerifyReport(integrity="ok")
    report.problems = [f"problem {i}" for i in range(verify.PROBLEM_DISPLAY_LIMIT + 5)]
    lines = "\n".join(verify.format_report(report))
    assert "and 5 more" in lines
    assert "problem 0" in lines
    assert f"problem {verify.PROBLEM_DISPLAY_LIMIT}" not in lines


# --------------------------------------------------------------------------- #
# write-time snapshot verification (issue #55, backup side)
# --------------------------------------------------------------------------- #

def test_snapshot_of_corrupt_db_raises_and_leaves_no_target(tmp_path):
    src = _corrupt_file(tmp_path / "pemr.db")
    out = tmp_path / "backups"
    with pytest.raises(backup.BackupError):
        backup.snapshot(src, out)
    assert list(out.glob("pemr-*.sqlite")) == []


def test_snapshot_failing_verification_is_unlinked(tmp_path, monkeypatch):
    """The verify step itself: a snapshot that does not check out never survives."""
    db_path = tmp_path / "pemr.db"
    assert _run(db_path, "migrate", "--create") == 0
    out = tmp_path / "backups"
    monkeypatch.setattr(backup, "integrity_check", lambda path: "*** in page 3")
    with pytest.raises(backup.BackupError, match="failed verification"):
        backup.snapshot(db_path, out)
    assert list(out.glob("pemr-*.sqlite")) == []


def test_corrupt_db_backup_never_reaches_rotation(tmp_path, capsys):
    """Ordering is the point: rotation must not prune good snapshots for a bad one."""
    out = tmp_path / "backups"
    out.mkdir()
    good = out / "pemr-20200101-1200.sqlite"
    good.write_bytes(b"stub")
    _corrupt_file(tmp_path / "pemr.db")

    capsys.readouterr()
    rc = _run(tmp_path / "pemr.db", "backup", "--backup-dir", str(out),
              "--keep-daily", "0", "--keep-weekly", "0")
    assert rc == 1
    assert "snapshot failed" in capsys.readouterr().err
    assert good.exists(), "rotation must never have run"


def test_backup_of_zero_byte_db_refuses_and_spares_the_real_snapshots(tmp_path, capsys):
    """The drill's chain, link 2: an empty archive must not become a backup source.

    `VACUUM INTO` on a 0-byte file writes a structurally valid (integrity-clean) 4 KB
    database, so the write-time verification cannot catch this - only refusing the
    *source* can. Until it did, one `pemr backup` here rotated the real snapshot away.
    """
    backups = tmp_path / "backups"
    backups.mkdir()
    real = backups / "pemr-20260101-1200.sqlite"
    real.write_bytes(b"the irreplaceable one")
    db_path = tmp_path / "pemr.db"
    db_path.touch()

    rc = _run(db_path, "backup", "--backup-dir", str(backups),
              "--keep-daily", "0", "--keep-weekly", "0")
    assert rc == 1
    assert "no database at" in capsys.readouterr().err
    assert list(backups.glob("pemr-*.sqlite")) == [real], "rotation must never have run"
    assert db_path.stat().st_size == 0


def test_backup_of_unmigrated_db_refuses_and_spares_the_real_snapshots(tmp_path, capsys,
                                                                       unmigrated_db):
    """Same chain from the other route: a real file with no pemr schema."""
    backups = tmp_path / "backups"
    backups.mkdir()
    real = backups / "pemr-20260101-1200.sqlite"
    real.write_bytes(b"the irreplaceable one")
    db_path = unmigrated_db(tmp_path / "pemr.db")

    rc = _run(db_path, "backup", "--backup-dir", str(backups),
              "--keep-daily", "0", "--keep-weekly", "0")
    assert rc == 1
    assert "no pemr schema" in capsys.readouterr().err
    assert list(backups.glob("pemr-*.sqlite")) == [real]


def test_integrity_check_of_a_missing_file_is_not_ok(tmp_path):
    """It must not answer "healthy" - nor leave 0-byte sqlite3.connect debris behind."""
    missing = tmp_path / "gone.sqlite"
    assert backup.integrity_check(missing) != "ok"
    assert not missing.exists()


def test_snapshot_name_override_is_verbatim_and_never_overwrites(tmp_path):
    db_path = tmp_path / "pemr.db"
    assert _run(db_path, "migrate", "--create") == 0
    out = tmp_path / "backups"
    snap, size = backup.snapshot(db_path, out, name="pemr-prerestore-20260101-000000.sqlite")
    assert snap.name == "pemr-prerestore-20260101-000000.sqlite"
    assert size > 0
    with pytest.raises(backup.BackupError):
        backup.snapshot(db_path, out, name=snap.name)
    assert snap.exists(), "the refused second write must not clobber the first"


# --------------------------------------------------------------------------- #
# tombstones lost across a restore (issue #80)
# --------------------------------------------------------------------------- #

def test_restore_reports_tombstones_the_snapshot_does_not_have(tmp_path, capsys):
    """A snapshot predating a tombstone loses it - correct, but the *consequence* is
    issue #80's bug resurrected, so the loss is named before the replace."""
    db_path, sources = _populated(tmp_path)
    backups = tmp_path / "backups"
    assert _run(db_path, "backup", "--backup-dir", str(backups)) == 0

    # Recorded *after* the snapshot, so the restore drops it.
    assert _run(db_path, "document", "rm", "1", "--tombstone", "--reason",
                "identifiers", "--apply") == 0

    capsys.readouterr()
    assert _run(db_path, "restore", "latest", "--backup-dir", str(backups),
                "--sources", str(sources), "--force") == 0
    err = capsys.readouterr().err
    assert "1 tombstone(s) in the current database are not in this snapshot" in err
    assert "identifiers" in err
    assert restore.RESCUE_PREFIX in err          # where they are still recoverable
    assert "tombstone add --sha256" in err       # and how to put them back
    assert err.isascii()

    # Reported, never merged: the restored database is the snapshot's, tombstone-free.
    conn = db.connect(db_path)
    try:
        assert verify.row_counts(conn)["document_tombstone"] == 0
    finally:
        conn.close()


def test_restore_says_nothing_when_no_tombstone_is_lost(tmp_path, capsys):
    db_path, sources = _populated(tmp_path)
    backups = tmp_path / "backups"
    assert _run(db_path, "document", "rm", "1", "--tombstone", "--apply") == 0
    assert _run(db_path, "backup", "--backup-dir", str(backups)) == 0

    capsys.readouterr()
    assert _run(db_path, "restore", "latest", "--backup-dir", str(backups),
                "--sources", str(sources), "--force") == 0
    assert "will be lost" not in capsys.readouterr().err


def test_restore_from_a_pre_migration_snapshot_does_not_crash(tmp_path, capsys):
    """The snapshot has no `document_tombstone` table at all - guarded on both sides,
    exactly as `verify.row_counts` omits tables missing from an older schema."""
    import shutil

    db_path = tmp_path / "pemr.db"
    backups = tmp_path / "backups"
    staged = tmp_path / "pre007"
    staged.mkdir()
    for path in sorted(db.DEFAULT_MIGRATIONS_DIR.glob("*.sql")):
        if path.name < "007":
            shutil.copy(path, staged / path.name)
    old = db.connect(db_path)
    try:
        db.migrate(old, staged)
    finally:
        old.close()
    assert _run(db_path, "backup", "--backup-dir", str(backups)) == 0

    # Bring the live database forward and record a tombstone the snapshot cannot hold.
    assert _run(db_path, "migrate") == 0
    conn = db.connect(db_path)
    try:
        tombstones.add_tombstone(conn, "c" * 64, reason="identifiers")
    finally:
        conn.close()

    capsys.readouterr()
    assert _run(db_path, "restore", "latest", "--backup-dir", str(backups),
                "--force") == 0
    err = capsys.readouterr().err
    assert "1 tombstone(s)" in err
    # migrate ran forward again, so the table is back (empty).
    conn = db.connect(db_path)
    try:
        assert verify.row_counts(conn)["document_tombstone"] == 0
    finally:
        conn.close()

"""Migrations runner + connection pragmas."""

import sqlite3

import pytest

from pemr import db

EXPECTED_TABLES = {
    "person",
    "document",
    "lab_result",
    "medication",
    "procedure",
    "appointment",
    "observation",
    "schema_migrations",
}


@pytest.fixture()
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield conn
    conn.close()


def test_connect_applies_pragmas(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrate_creates_all_tables(conn):
    applied = db.migrate(conn)
    assert applied == ["001_init.sql"]
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert EXPECTED_TABLES <= tables


def test_migrate_is_idempotent(conn):
    assert db.migrate(conn) == ["001_init.sql"]
    assert db.migrate(conn) == []  # second run: nothing pending


def test_migrate_records_versions(conn):
    db.migrate(conn)
    assert db.applied_versions(conn) == {"001_init.sql"}


def test_migrate_applies_in_order_and_tracks_new_files(conn, tmp_path):
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "001_a.sql").write_text("CREATE TABLE a (x INTEGER);")
    (mdir / "002_b.sql").write_text("CREATE TABLE b (y INTEGER);")
    assert db.migrate(conn, mdir) == ["001_a.sql", "002_b.sql"]
    # a later-added migration applies alone on the next run
    (mdir / "003_c.sql").write_text("CREATE TABLE c (z INTEGER);")
    assert db.migrate(conn, mdir) == ["003_c.sql"]


def test_failing_migration_rolls_back_and_raises(conn, tmp_path):
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "001_bad.sql").write_text("CREATE TABLE t (x INTEGER); SYNTAX ERROR;")
    with pytest.raises(RuntimeError, match="001_bad.sql"):
        db.migrate(conn, mdir)
    assert db.applied_versions(conn) == set()  # not recorded as applied


def test_missing_migrations_dir_raises(conn, tmp_path):
    with pytest.raises(FileNotFoundError):
        db.migrate(conn, tmp_path / "nope")


def test_foreign_keys_enforced(conn):
    db.migrate(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lab_result (person_id, test_name, collected_at, dedup_key)"
            " VALUES (999, 'hba1c', '2026-01-01', 'k1')"
        )

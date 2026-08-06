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
    "condition",
    "allergy",
    "conflict",
    "document_tombstone",
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


ALL_MIGRATIONS = [
    "001_init.sql",
    "002_conflict.sql",
    "003_fts.sql",
    "004_person_deactivate.sql",
    "005_dedup_occurrence.sql",
    "006_condition_allergy.sql",
    "007_document_tombstone.sql",
]

# Every record table carries the occurrence-family columns (migration 005; 006's two
# new tables were born with them).
RECORD_TABLES = (
    "lab_result", "medication", "procedure", "appointment", "observation",
    "condition", "allergy",
)


def test_migrate_creates_all_tables(conn):
    applied = db.migrate(conn)
    assert applied == ALL_MIGRATIONS
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert EXPECTED_TABLES <= tables


def test_migrate_is_idempotent(conn):
    assert db.migrate(conn) == ALL_MIGRATIONS
    assert db.migrate(conn) == []  # second run: nothing pending


def test_migrate_records_versions(conn):
    db.migrate(conn)
    assert db.applied_versions(conn) == set(ALL_MIGRATIONS)


def test_person_has_deactivated_at_column(conn):
    db.migrate(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(person)").fetchall()}
    assert "deactivated_at" in cols


def test_record_tables_have_occurrence_columns(conn):
    db.migrate(conn)
    for table in RECORD_TABLES:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        assert {"dedup_base", "dedup_occurrence"} <= cols, table


def test_occurrence_defaults_to_zero_for_a_bare_insert(conn):
    """A row written without naming the column is occurrence 0 - the pre-005 shape."""
    db.migrate(conn)
    conn.execute("INSERT INTO person (slug, full_name) VALUES ('jane', 'Jane')")
    conn.execute(
        "INSERT INTO lab_result (person_id, test_name, collected_at, dedup_key) "
        "VALUES (1, 'hba1c', '2026-01-01', 'k1')"
    )
    row = conn.execute("SELECT dedup_occurrence FROM lab_result").fetchone()
    assert row["dedup_occurrence"] == 0


def _migrate_through_005(conn, tmp_path):
    """Apply every migration up to 005 into a staging dir, leaving 006 pending."""
    import shutil

    staged = tmp_path / "pre006"
    staged.mkdir()
    for path in sorted(db.DEFAULT_MIGRATIONS_DIR.glob("*.sql")):
        if path.name < "006":
            shutil.copy(path, staged / path.name)
    db.migrate(conn, staged)
    return staged


def test_migration_006_moves_condition_and_allergy_observations(conn, tmp_path):
    """The upgrade path (issue #63): rows committed under the old `obs_type` convention
    land in the typed tables, keep their keys and provenance, and leave `observation`."""
    _migrate_through_005(conn, tmp_path)
    conn.execute("INSERT INTO person (slug, full_name) VALUES ('jane', 'Jane')")
    conn.executemany(
        "INSERT INTO observation (person_id, obs_type, observed_at, key, value_text, "
        "dedup_key, dedup_base) VALUES (1, ?, ?, ?, ?, ?, ?)",
        [
            ("allergy", "2010-01-01", "Penicillin", "rash", "k-allergy", "k-allergy"),
            ("condition", "2024-01-01", "Type 2 Diabetes", "dx by PCP", "k-cond", "k-cond"),
            ("vital", "2026-01-01", "weight", None, "k-vital", "k-vital"),
        ],
    )
    conn.commit()

    assert db.migrate(conn) == [
        "006_condition_allergy.sql", "007_document_tombstone.sql"
    ]

    a = conn.execute("SELECT * FROM allergy").fetchone()
    assert (a["substance"], a["reaction"], a["noted_on"]) == (
        "Penicillin", "rash", "2010-01-01")
    assert a["dedup_key"] == "k-allergy" and a["dedup_base"] == "k-allergy"
    c = conn.execute("SELECT * FROM condition").fetchone()
    assert (c["name"], c["note"], c["onset_on"]) == (
        "Type 2 Diabetes", "dx by PCP", "2024-01-01")
    assert c["status"] == "active"       # the honest reading: the old convention had none
    assert c["dedup_key"] == "k-cond"

    # The vital stays behind; the two moved rows are gone from `observation`.
    left = [r["obs_type"] for r in conn.execute("SELECT * FROM observation")]
    assert left == ["vital"]

    # FTS follows them (the triggers are created before the move, so no backfill).
    hits = {
        (r["source_table"], r["source_id"])
        for r in conn.execute(
            "SELECT source_table, source_id FROM record_fts WHERE record_fts MATCH ?",
            ("Penicillin OR Diabetes",),
        )
    }
    assert hits == {("allergy", 1), ("condition", 1)}


def test_migration_006_keeps_keyless_rows_in_observation(conn, tmp_path):
    """A legacy row with no `key` has no allergen/problem name, and `substance`/`name`
    are NOT NULL - it stays put for a human rather than being dropped."""
    _migrate_through_005(conn, tmp_path)
    conn.execute("INSERT INTO person (slug, full_name) VALUES ('jane', 'Jane')")
    conn.execute(
        "INSERT INTO observation (person_id, obs_type, value_text, dedup_key, dedup_base) "
        "VALUES (1, 'allergy', 'unspecified reaction', 'k1', 'k1')"
    )
    conn.commit()
    db.migrate(conn)
    assert conn.execute("SELECT COUNT(*) AS n FROM allergy").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM observation").fetchone()["n"] == 1


def test_multi_statement_migration_rolls_back_partial_ddl(conn, tmp_path):
    # A migration whose 2nd statement fails must leave NO partial schema behind —
    # executescript implicitly commits, so atomicity lives in the wrapping txn.
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "001_partial.sql").write_text(
        "CREATE TABLE good (x INTEGER);\n"
        "INSERT INTO does_not_exist (x) VALUES (1);"  # runtime failure, 2nd statement
    )
    with pytest.raises(RuntimeError, match="001_partial.sql"):
        db.migrate(conn, mdir)
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "good" not in tables  # first statement rolled back too
    assert db.applied_versions(conn) == set()


def test_require_migrated(conn):
    with pytest.raises(db.NotMigratedError, match="pemr migrate"):
        db.require_migrated(conn)
    assert not db.is_migrated(conn)
    db.migrate(conn)
    db.require_migrated(conn)  # no raise
    assert db.is_migrated(conn)


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

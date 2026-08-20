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
    "curation",
    "person_unit_pref",
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
    "008_curation.sql",
    "009_record_attestation.sql",
    "010_curation_row_scope.sql",
    "011_curation_distinct_status.sql",
    "012_record_edit.sql",
    "013_person_unit_pref.sql",
    "014_medication_status_reason.sql",
    "015_document_text_source.sql",
    "016_record_correction_mark.sql",
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


def test_medication_has_status_reason_column(conn):
    """Migration 014 (issue #159): the CCDA discontinue reason gets its own nullable
    column, so a renewal is distinguishable from a completed course after commit."""
    db.migrate(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(medication)").fetchall()}
    assert "status_reason" in cols
    conn.execute("INSERT INTO person (slug, full_name) VALUES ('jane', 'Jane')")
    conn.execute(
        "INSERT INTO medication (person_id, name, dedup_key) VALUES (1, 'Metformin', 'k')"
    )
    assert conn.execute(
        "SELECT status_reason FROM medication"
    ).fetchone()["status_reason"] is None   # nullable, no backfill


def _migrate_through_014(conn, tmp_path):
    """Apply every migration up to 014, leaving 015 pending (a 014-era database)."""
    import shutil

    staged = tmp_path / "pre015"
    staged.mkdir()
    for path in sorted(db.DEFAULT_MIGRATIONS_DIR.glob("*.sql")):
        if path.name < "015":
            shutil.copy(path, staged / path.name)
    db.migrate(conn, staged)
    return staged


def test_document_has_text_source_column(conn):
    """Migration 015 (issue #175): which write path produced the current `ocr_text`."""
    db.migrate(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(document)").fetchall()}
    assert "text_source" in cols
    conn.execute("INSERT INTO person (slug, full_name) VALUES ('jane', 'Jane')")
    conn.execute(
        "INSERT INTO document (sha256, person_id, source_path, ocr_text, ingested_at) "
        "VALUES ('aa11', 1, 'aa/aa11.pdf', 'scan text', '2026-03-16T00:00:00')"
    )
    assert conn.execute(
        "SELECT text_source FROM document"
    ).fetchone()["text_source"] is None   # nullable, no backfill


def test_migration_015_applies_on_a_014_era_database_without_backfilling(
    conn, tmp_path
):
    """The upgrade path: one additive nullable column, so an existing document keeps its
    text and gains `text_source IS NULL` — deliberately *not* backfilled to 'engine',
    which would misclassify the hand-attached rows `document set-text` has been writing
    since #62."""
    _migrate_through_014(conn, tmp_path)
    conn.execute("INSERT INTO person (slug, full_name) VALUES ('jane', 'Jane')")
    conn.execute(
        "INSERT INTO document (sha256, person_id, source_path, ocr_text, ingested_at) "
        "VALUES ('aa11', 1, 'aa/aa11.pdf', 'hand typed', '2026-03-16T00:00:00')"
    )
    conn.commit()

    assert db.migrate(conn) == [
        "015_document_text_source.sql", "016_record_correction_mark.sql",
    ]

    row = conn.execute("SELECT * FROM document").fetchone()
    assert row["ocr_text"] == "hand typed"
    assert row["text_source"] is None


def _migrate_through_015(conn, tmp_path):
    """Apply every migration up to 015, leaving 016 pending (a 015-era database)."""
    import shutil

    staged = tmp_path / "pre016"
    staged.mkdir()
    for path in sorted(db.DEFAULT_MIGRATIONS_DIR.glob("*.sql")):
        if path.name < "016":
            shutil.copy(path, staged / path.name)
    db.migrate(conn, staged)
    return staged


def _seed_pre016_lab(conn, row_id, base, key):
    conn.execute(
        "INSERT INTO lab_result (lab_result_id, person_id, test_name, collected_at, "
        "unit, dedup_key, dedup_base) VALUES (?, 1, 'HbA1c', '2024-01-01', ?, ?, ?)",
        (row_id, "percent", key, base),
    )


def _seed_ledger(conn, row_id, base, field, edited_at, attributed_to):
    conn.execute(
        "INSERT INTO record_edit (record_type, record_id, dedup_base, field, "
        "old_value, new_value, note, attributed_to, edited_at) VALUES "
        "('lab_result', ?, ?, ?, '%', 'percent', 'normalise', ?, ?)",
        (row_id, base, field, attributed_to, edited_at),
    )


def test_record_tables_have_correction_mark_columns(conn):
    """Migration 016 (issue #134): a corrected row says so on the row, so `render` and
    `query` can disclose it without joining the ledger."""
    db.migrate(conn)
    for table in RECORD_TABLES:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        assert {"edited_at", "edited_by"} <= cols, table


def test_migration_016_backfills_the_newest_ledger_entry_per_row(conn, tmp_path):
    """The upgrade path: rows corrected before the columns existed are reconstructed from
    the ledger, newest entry wins, and an unattributed correction backfills a NULL
    `edited_by` rather than being skipped."""
    _migrate_through_015(conn, tmp_path)
    conn.execute("INSERT INTO person (slug, full_name) VALUES ('jane', 'Jane')")
    _seed_pre016_lab(conn, 1, "base-a", "base-a")
    _seed_pre016_lab(conn, 2, "base-b", "base-b")
    _seed_pre016_lab(conn, 3, "base-c", "base-c")     # never corrected
    _seed_ledger(conn, 1, "base-a", "unit", "2026-01-01T00:00:00+00:00", "Jane")
    _seed_ledger(conn, 1, "base-a", "flag", "2026-02-01T00:00:00+00:00", "Sam")
    _seed_ledger(conn, 2, "base-b", "unit", "2026-01-15T00:00:00+00:00", None)
    conn.commit()

    assert db.migrate(conn) == ["016_record_correction_mark.sql"]

    marks = {
        row["lab_result_id"]: (row["edited_at"], row["edited_by"])
        for row in conn.execute("SELECT * FROM lab_result").fetchall()
    }
    assert marks[1] == ("2026-02-01T00:00:00+00:00", "Sam")   # newest wins
    assert marks[2] == ("2026-01-15T00:00:00+00:00", None)    # unattributed, still marked
    assert marks[3] == (None, None)


def test_migration_016_leaves_a_recycled_row_id_unmarked(conn, tmp_path):
    """The row-id-reuse hazard (issue #114), answered at backfill time by the ledger's
    `dedup_base` breadcrumb: a stale entry naming a *different* family must not stamp a
    correction caveat onto whichever row later inherited the id."""
    _migrate_through_015(conn, tmp_path)
    conn.execute("INSERT INTO person (slug, full_name) VALUES ('jane', 'Jane')")
    _seed_pre016_lab(conn, 1, "base-new", "base-new")
    _seed_ledger(conn, 1, "base-gone", "unit", "2026-01-01T00:00:00+00:00", "Jane")
    conn.commit()

    db.migrate(conn)

    row = conn.execute("SELECT * FROM lab_result").fetchone()
    assert (row["edited_at"], row["edited_by"]) == (None, None)
    # The ledger entry is kept regardless: nothing resolves through it (migration 012).
    assert conn.execute("SELECT COUNT(*) AS n FROM record_edit").fetchone()["n"] == 1


def test_a_pre016_snapshot_renders_and_queries_without_the_columns(conn, tmp_path):
    """`restore` can bring back a database predating 016. Every reader goes through
    `.get()`/`_row_get` on a mapping, so a missing column reads as "not corrected"
    rather than raising `no such column`."""
    from pemr import query, render

    _migrate_through_015(conn, tmp_path)
    conn.execute("INSERT INTO person (slug, full_name) VALUES ('jane', 'Jane')")
    _seed_pre016_lab(conn, 1, "base-a", "base-a")
    conn.commit()

    md = render.render_summary(conn, "jane")
    assert "corrected" not in md
    events = query.query_timeline(conn, "jane")
    assert all("edited_at" not in e for e in events)


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
        "006_condition_allergy.sql", "007_document_tombstone.sql",
        "008_curation.sql", "009_record_attestation.sql",
        "010_curation_row_scope.sql", "011_curation_distinct_status.sql",
        "012_record_edit.sql", "013_person_unit_pref.sql",
        "014_medication_status_reason.sql", "015_document_text_source.sql",
        "016_record_correction_mark.sql",
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


def _migrate_through_007(conn, tmp_path):
    """Apply every migration up to 007, leaving 008 pending (a 007-era database)."""
    import shutil

    staged = tmp_path / "pre008"
    staged.mkdir()
    for path in sorted(db.DEFAULT_MIGRATIONS_DIR.glob("*.sql")):
        if path.name < "008":
            shutil.copy(path, staged / path.name)
    db.migrate(conn, staged)
    return staged


def test_migration_008_applies_on_a_007_era_database(conn, tmp_path):
    """The upgrade path for the curation overlay (issue #109): the table is additive,
    so an existing database gains it without touching a single record row."""
    from pemr import curation

    _migrate_through_007(conn, tmp_path)
    conn.execute("INSERT INTO person (slug, full_name) VALUES ('jane', 'Jane')")
    conn.commit()
    assert curation.has_table(conn) is False
    assert not curation.load_verdicts(conn)  # readable before the migration exists

    assert db.migrate(conn) == [
        "008_curation.sql", "009_record_attestation.sql",
        "010_curation_row_scope.sql", "011_curation_distinct_status.sql",
        "012_record_edit.sql", "013_person_unit_pref.sql",
        "014_medication_status_reason.sql", "015_document_text_source.sql",
        "016_record_correction_mark.sql",
    ]

    assert curation.has_table(conn) is True
    cols = {row[1] for row in conn.execute("PRAGMA table_info(curation)").fetchall()}
    assert cols == {
        "record_type", "dedup_base", "record_id", "status", "note",
        "merged_into_base", "attributed_to", "created_at",
    }
    assert conn.execute("SELECT COUNT(*) AS n FROM person").fetchone()["n"] == 1


def _migrate_through_008(conn, tmp_path):
    """Apply every migration up to 008, leaving 009 pending (a 008-era database)."""
    import shutil

    staged = tmp_path / "pre009"
    staged.mkdir()
    for path in sorted(db.DEFAULT_MIGRATIONS_DIR.glob("*.sql")):
        if path.name < "009":
            shutil.copy(path, staged / path.name)
    db.migrate(conn, staged)
    return staged


def test_migration_009_adds_attestation_columns_to_every_record_table(conn):
    """Issue #110: provenance lives on the row, so all seven typed tables gain the same
    three nullable columns - a per-table check, because a table missed here is a table
    whose attested rows could never be recorded."""
    db.migrate(conn)
    for table in RECORD_TABLES:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        assert {"attested_by", "attested_on", "attested_at"} <= cols, table


def test_migration_009_applies_on_an_008_era_database(conn, tmp_path):
    """The upgrade path: additive columns, so an existing database gains them without
    touching a record row."""
    from pemr import attestations

    _migrate_through_008(conn, tmp_path)
    conn.execute("INSERT INTO person (slug, full_name) VALUES ('jane', 'Jane')")
    conn.execute(
        "INSERT INTO lab_result (person_id, test_name, collected_at, dedup_key, "
        "dedup_base) VALUES (1, 'HbA1c', '2026-01-01', 'k1', 'k1')"
    )
    conn.commit()
    assert attestations.has_columns(conn) is False
    assert attestations.list_attested(conn) == []   # readable before the migration

    assert db.migrate(conn) == [
        "009_record_attestation.sql", "010_curation_row_scope.sql",
        "011_curation_distinct_status.sql", "012_record_edit.sql",
        "013_person_unit_pref.sql", "014_medication_status_reason.sql",
        "015_document_text_source.sql",
        "016_record_correction_mark.sql",
    ]

    assert attestations.has_columns(conn) is True
    row = conn.execute("SELECT * FROM lab_result").fetchone()
    assert row["test_name"] == "HbA1c"
    # A pre-009 row is document-sourced by construction: the new columns default to NULL.
    assert (row["attested_by"], row["attested_on"], row["attested_at"]) == (
        None, None, None
    )


def _migrate_through_009(conn, tmp_path):
    """Apply every migration up to 009, leaving 010 pending (a 009-era database)."""
    import shutil

    staged = tmp_path / "pre010"
    staged.mkdir()
    for path in sorted(db.DEFAULT_MIGRATIONS_DIR.glob("*.sql")):
        if path.name < "010":
            shutil.copy(path, staged / path.name)
    db.migrate(conn, staged)
    return staged


def test_migration_010_rebuilds_curation_and_preserves_every_verdict(conn, tmp_path):
    """Issue #114's table rebuild is the only destructive-shaped DDL in this repo's
    history (001-009 are pure CREATE / ADD COLUMN), so the round trip is the test: every
    009-era verdict must come out the other side byte-for-byte, `created_at` included,
    and family-scoped by definition."""
    from pemr import curation

    _migrate_through_009(conn, tmp_path)
    conn.execute("INSERT INTO person (slug, full_name) VALUES ('jane', 'Jane')")
    conn.execute(
        "INSERT INTO curation (record_type, dedup_base, status, note, attributed_to, "
        "created_at) VALUES ('lab_result', 'base-a', 'disputed', 'two sources', "
        "'Dr Who', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO curation (record_type, dedup_base, status, note, "
        "merged_into_base, created_at) VALUES ('condition', 'base-b', 'merged-into', "
        "'same episode', 'base-c', '2026-02-02T00:00:00+00:00')"
    )
    conn.commit()
    before = [dict(r) for r in conn.execute(
        "SELECT * FROM curation ORDER BY dedup_base"
    ).fetchall()]

    assert db.migrate(conn) == [
        "010_curation_row_scope.sql", "011_curation_distinct_status.sql",
        "012_record_edit.sql", "013_person_unit_pref.sql",
        "014_medication_status_reason.sql", "015_document_text_source.sql",
        "016_record_correction_mark.sql",
    ]

    after = [dict(r) for r in conn.execute(
        "SELECT * FROM curation ORDER BY dedup_base"
    ).fetchall()]
    assert [{k: v for k, v in r.items() if k != "record_id"} for r in after] == before
    assert [r["record_id"] for r in after] == [0, 0]   # 008 verdicts were family-scoped
    verdicts = curation.load_verdicts(conn)
    assert len(verdicts.family) == 2 and verdicts.rows == {}


def test_migration_010_lets_scopes_coexist_but_pins_one_verdict_per_row(conn):
    """The two uniqueness rules #114 needs, which 008's PK could not express: a family
    verdict and a row verdict may share a base (that is the precedence case), and two
    rows may be annotated inside one family - but a row may carry only one verdict,
    whatever base it was recorded under (the partial index, which also collapses the
    stale-breadcrumb duplicate a rekey could otherwise produce)."""
    db.migrate(conn)
    for record_type, base, record_id in [
        ("lab_result", "base-a", 0),   # family scope
        ("lab_result", "base-a", 7),   # a row inside it
        ("lab_result", "base-a", 8),   # its sibling, ruled separately
    ]:
        conn.execute(
            "INSERT INTO curation (record_type, dedup_base, record_id, status, note, "
            "created_at) VALUES (?, ?, ?, 'disputed', 'why', '2026-01-01T00:00:00')",
            (record_type, base, record_id),
        )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):   # same row, a different base
        conn.execute(
            "INSERT INTO curation (record_type, dedup_base, record_id, status, note, "
            "created_at) VALUES ('lab_result', 'base-z', 7, 'disputed', 'why', "
            "'2026-01-01T00:00:00')"
        )
    with pytest.raises(sqlite3.IntegrityError):   # negative scope sentinel
        conn.execute(
            "INSERT INTO curation (record_type, dedup_base, record_id, status, note, "
            "created_at) VALUES ('lab_result', 'base-a', -1, 'disputed', 'why', "
            "'2026-01-01T00:00:00')"
        )


def _migrate_through_010(conn, tmp_path):
    """Apply every migration up to 010, leaving 011 pending (a 010-era database)."""
    import shutil

    staged = tmp_path / "pre011"
    staged.mkdir()
    for path in sorted(db.DEFAULT_MIGRATIONS_DIR.glob("*.sql")):
        if path.name < "011":
            shutil.copy(path, staged / path.name)
    db.migrate(conn, staged)
    return staged


def test_migration_011_widens_the_status_check_and_preserves_every_verdict(
    conn, tmp_path
):
    """Issue #122's rebuild, checked the way 010's is: a widened CHECK cannot be ALTERed
    in, so the table is rebuilt, and the round trip must carry every 010-era verdict of
    *both* scopes through byte-for-byte. The partial index is the failure mode of a
    rebuild - it is dropped with the old table - so its survival is asserted explicitly."""
    from pemr import curation

    _migrate_through_010(conn, tmp_path)
    conn.execute(
        "INSERT INTO curation (record_type, dedup_base, record_id, status, note, "
        "attributed_to, created_at) VALUES ('lab_result', 'base-a', 0, 'disputed', "
        "'two sources', 'Dr Who', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO curation (record_type, dedup_base, record_id, status, note, "
        "merged_into_base, created_at) VALUES ('condition', 'base-b', 7, "
        "'merged-into', 'same episode', 'base-c', '2026-02-02T00:00:00+00:00')"
    )
    conn.commit()
    before = [dict(r) for r in conn.execute(
        "SELECT * FROM curation ORDER BY dedup_base"
    ).fetchall()]

    assert db.migrate(conn) == [
        "011_curation_distinct_status.sql", "012_record_edit.sql",
        "013_person_unit_pref.sql", "014_medication_status_reason.sql",
        "015_document_text_source.sql",
        "016_record_correction_mark.sql",
    ]

    after = [dict(r) for r in conn.execute(
        "SELECT * FROM curation ORDER BY dedup_base"
    ).fetchall()]
    assert after == before
    assert [r["record_id"] for r in after] == [0, 7]      # both scopes survived
    verdicts = curation.load_verdicts(conn)
    assert len(verdicts.family) == 1 and len(verdicts.rows) == 1

    # The uniqueness rule the rebuild would silently lose.
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_curation_row'"
    ).fetchone() is not None
    with pytest.raises(sqlite3.IntegrityError):          # same row, a different base
        conn.execute(
            "INSERT INTO curation (record_type, dedup_base, record_id, status, note, "
            "created_at) VALUES ('condition', 'base-z', 7, 'disputed', 'why', "
            "'2026-03-03T00:00:00+00:00')"
        )

    # The point of the migration: the new status is accepted, and only it widened.
    conn.execute(
        "INSERT INTO curation (record_type, dedup_base, record_id, status, note, "
        "created_at) VALUES ('condition', 'base-d', 0, 'distinct', 'two diagnoses', "
        "'2026-03-03T00:00:00+00:00')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO curation (record_type, dedup_base, record_id, status, note, "
            "created_at) VALUES ('condition', 'base-e', 0, 'made-up', 'nope', "
            "'2026-03-03T00:00:00+00:00')"
        )


def test_a_bare_insert_leaves_the_attestation_columns_null(conn):
    db.migrate(conn)
    conn.execute("INSERT INTO person (slug, full_name) VALUES ('jane', 'Jane')")
    conn.execute(
        "INSERT INTO medication (person_id, name, dedup_key, dedup_base) "
        "VALUES (1, 'Metformin', 'k1', 'k1')"
    )
    conn.commit()
    row = conn.execute("SELECT * FROM medication").fetchone()
    assert row["attested_by"] is None and row["attested_at"] is None

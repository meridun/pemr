"""Connection handling and migrations runner.

Pragmas on every connect (Architecture.md §1/§8): WAL journal mode and
foreign_keys=ON. Migrations are plain numbered .sql files in migrations/,
applied in lexicographic order and tracked in schema_migrations.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Repo-relative default; callers (CLI, tests) may pass any directory.
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# A core table from 001_init used to detect an un-migrated database.
_SENTINEL_TABLE = "document"


class NotMigratedError(RuntimeError):
    """Raised when an operation runs against a database with no schema applied."""

    def __init__(self, message: str = "database not migrated - run `pemr migrate` first"):
        super().__init__(message)


def database_exists(db_path: str | Path) -> bool:
    """True if ``db_path`` names a real, non-empty database file.

    Size matters, not just presence: ``sqlite3.connect`` creates the file eagerly and
    leaves a **zero-byte** file behind when nothing is written, so a stale 0-byte
    ``pemr.db`` must not be mistaken for an archive. Callers (CLI/MCP) use this to
    refuse to silently manufacture an empty database over a missing one (issue #55).

    ``:memory:`` is always "existing" — it is the in-process primitive used by tests.
    """
    if str(db_path) == ":memory:":
        return True
    path = Path(db_path)
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with PEMR's required pragmas applied.

    Deliberately still *creates on connect* — this is the low-level primitive, used
    with ``:memory:`` and scratch paths throughout the tests. The "refuse to create a
    database that should already exist" gate lives one layer up, in the CLI/MCP entry
    points (:func:`database_exists`).
    """
    db_path = Path(db_path)
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version     TEXT PRIMARY KEY,   -- filename, e.g. '001_init.sql'
          applied_at  TEXT NOT NULL
        )
        """
    )


def applied_versions(conn: sqlite3.Connection) -> set[str]:
    _ensure_migrations_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row["version"] for row in rows}


def pending_migrations(
    conn: sqlite3.Connection, migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR
) -> list[Path]:
    """Numbered .sql files not yet recorded in schema_migrations, in order."""
    migrations_dir = Path(migrations_dir)
    if not migrations_dir.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {migrations_dir}")
    done = applied_versions(conn)
    return sorted(
        p for p in migrations_dir.glob("*.sql") if p.name not in done
    )


def migrate(
    conn: sqlite3.Connection, migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR
) -> list[str]:
    """Apply pending migrations; returns the list of applied filenames.

    Each migration runs atomically in its own transaction: the whole script plus
    its ``schema_migrations`` row commit together, or nothing does. A script that
    fails partway (e.g. a multi-statement DDL migration) rolls back fully, so no
    partial schema is left behind; earlier migrations stay applied.

    Note: ``executescript`` cannot be wrapped in a Python-managed ``with conn:``
    transaction because it issues an implicit COMMIT of any pending transaction
    first. Transaction control therefore lives *inside* the script string — an
    explicit ``BEGIN``/``COMMIT`` around the DDL — with an explicit ROLLBACK on
    error to undo the partial (still-open) transaction.
    """
    applied: list[str] = []
    for path in pending_migrations(conn, migrations_dir):
        version = path.name.replace("'", "''")
        applied_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        script = (
            "BEGIN;\n"
            + path.read_text(encoding="utf-8")
            + "\nINSERT INTO schema_migrations (version, applied_at) VALUES "
            + f"('{version}', '{applied_at}');\n"
            + "COMMIT;\n"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # no transaction was left open
            raise RuntimeError(f"migration {path.name} failed: {exc}") from exc
        applied.append(path.name)
    return applied


def is_migrated(conn: sqlite3.Connection) -> bool:
    """True once the core schema (001_init) has been applied."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (_SENTINEL_TABLE,),
    ).fetchone()
    return row is not None


def require_migrated(conn: sqlite3.Connection) -> None:
    """Raise :class:`NotMigratedError` if the schema has not been applied yet."""
    if not is_migrated(conn):
        raise NotMigratedError()

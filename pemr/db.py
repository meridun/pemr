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


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with PEMR's required pragmas applied."""
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

    Each migration runs in its own transaction — a failing script rolls back
    fully and leaves earlier ones applied.
    """
    applied: list[str] = []
    for path in pending_migrations(conn, migrations_dir):
        try:
            with conn:  # transaction per migration
                conn.executescript(path.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (path.name, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
        except sqlite3.Error as exc:
            raise RuntimeError(f"migration {path.name} failed: {exc}") from exc
        applied.append(path.name)
    return applied

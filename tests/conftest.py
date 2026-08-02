"""Shared test helpers.

Issue #55 gave the CLI/MCP a missing-database gate, which makes "no database file" and
"database file with no schema" genuinely different states. Tests that mean the *latter*
must therefore put a real file on disk rather than pointing at a nonexistent path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def make_unmigrated_db(path: str | Path) -> Path:
    """A real, non-empty SQLite file with no pemr schema applied."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE _not_pemr (x)")
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture()
def unmigrated_db():
    """Factory fixture: ``unmigrated_db(tmp_path / "cli.db")``."""
    return make_unmigrated_db

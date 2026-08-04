"""`pemr verify` — read-only health report over a database and its source blobs.

Issue #55: a restore is only *complete* if the blobs the restored rows point at are
still there. `document` stores both a content hash and a relative, content-addressed
`source_path`, so the check is exact rather than heuristic: resolve
``sources_dir / source_path``, confirm it exists, re-hash it, compare to
``document.sha256``.

One function, two callers: `pemr verify` prints this report, and `pemr restore` prints
the same report as its tail so the issue's acceptance drill is a single command
afterwards.

**Reports, never repairs.** No FTS rebuild, no blob refetch, no orphan sweep (blobs in
`sources/` with no `document` row) — those are deliberate non-goals, see the issue's
design comment.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import db, ingest

# Row counts reported, in a stable order. A table missing from the schema (a snapshot
# predating its migration, checked before `restore` runs `migrate`) is simply omitted.
COUNTED_TABLES = (
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
    "record_fts",
)

# Problems are all retained on the report; printers show at most this many.
PROBLEM_DISPLAY_LIMIT = 10


@dataclass
class VerifyReport:
    """Structured result of :func:`verify_report` (also the ``--json`` payload shape)."""

    integrity: str
    migrations: list[str] = field(default_factory=list)
    row_counts: dict[str, int] = field(default_factory=dict)
    blobs_checked: int = 0
    blobs_ok: int = 0
    blobs_missing: int = 0
    blobs_mismatched: int = 0
    blobs_skipped: str | None = None  # why the blob pass was skipped, if it was
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing needs a human.

        Every failure this module knows about appends to :attr:`problems` - failed
        integrity, an un-migrated database, a missing or mismatched blob - so that list
        is the single source of truth. A *skipped* blob pass is deliberately not a
        problem: unconfigured or mid-sync `sources/` must not fail a restore.
        """
        return not self.problems

    def as_dict(self) -> dict:
        return {
            "integrity": self.integrity,
            "migrations": list(self.migrations),
            "row_counts": dict(self.row_counts),
            "blobs": {
                "checked": self.blobs_checked,
                "ok": self.blobs_ok,
                "missing": self.blobs_missing,
                "mismatched": self.blobs_mismatched,
                "skipped": self.blobs_skipped,
            },
            "problems": list(self.problems),
            "ok": self.ok,
        }


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    ).fetchall()
    return {row[0] for row in rows}


def row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """``{table: count}`` for the tables in :data:`COUNTED_TABLES` that exist."""
    present = _existing_tables(conn)
    counts: dict[str, int] = {}
    for table in COUNTED_TABLES:
        if table not in present:
            continue
        # Table names come from the module-level constant, never from user input.
        counts[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return counts


def _check_blobs(
    conn: sqlite3.Connection, sources_dir: Path, report: VerifyReport
) -> None:
    rows = conn.execute(
        "SELECT document_id, sha256, source_path FROM document ORDER BY document_id"
    ).fetchall()
    for row in rows:
        report.blobs_checked += 1
        doc_id, sha, rel = row[0], row[1], row[2]
        blob = sources_dir / rel
        if not blob.is_file():
            report.blobs_missing += 1
            report.problems.append(f"document #{doc_id}: blob missing at {blob}")
            continue
        try:
            actual = ingest.hash_file(blob)
        except OSError as exc:
            report.blobs_missing += 1
            report.problems.append(f"document #{doc_id}: cannot read {blob}: {exc}")
            continue
        if actual != sha:
            report.blobs_mismatched += 1
            report.problems.append(
                f"document #{doc_id}: sha256 mismatch at {blob} "
                f"(recorded {sha[:12]}..., on disk {actual[:12]}...)"
            )
            continue
        report.blobs_ok += 1


def verify_report(
    conn: sqlite3.Connection, sources_dir: str | Path | None = None
) -> VerifyReport:
    """Integrity + migrations + row counts (+ blob resolution when ``sources_dir``).

    ``sources_dir=None`` skips the blob pass with a recorded reason rather than
    failing: a restore must still report on the database when `sources/` is
    unconfigured or mid-sync.
    """
    integrity_row = conn.execute("PRAGMA integrity_check").fetchone()
    integrity = str(integrity_row[0]) if integrity_row else "integrity_check returned nothing"
    report = VerifyReport(integrity=integrity)
    if integrity != "ok":
        report.problems.append(f"integrity_check: {integrity}")

    if db.is_migrated(conn):
        report.migrations = sorted(db.applied_versions(conn))
    else:
        report.problems.append("database has no schema applied")

    report.row_counts = row_counts(conn)

    if sources_dir is None:
        report.blobs_skipped = "no sources dir configured"
    else:
        sources_dir = Path(sources_dir)
        if not sources_dir.is_dir():
            report.blobs_skipped = f"sources dir not found: {sources_dir}"
        elif "document" not in report.row_counts:
            report.blobs_skipped = "no document table"
        else:
            _check_blobs(conn, sources_dir, report)
    return report


def format_report(report: VerifyReport) -> list[str]:
    """ASCII-only console lines for ``pemr verify`` / the tail of ``pemr restore``.

    ASCII by construction (issue #23): a cp437/cp1252 Windows console must not garble
    or crash on this output.
    """
    lines = [f"integrity      {report.integrity}"]
    lines.append(
        f"migrations     {len(report.migrations)} applied"
        + (f" (latest {report.migrations[-1]})" if report.migrations else "")
    )
    for table, count in report.row_counts.items():
        lines.append(f"  {table:14} {count}")
    if report.blobs_skipped:
        lines.append(f"blobs          skipped ({report.blobs_skipped})")
    else:
        lines.append(
            f"blobs          {report.blobs_ok}/{report.blobs_checked} ok, "
            f"{report.blobs_missing} missing, {report.blobs_mismatched} mismatched"
        )
    if report.problems:
        lines.append(f"problems       {len(report.problems)}")
        for problem in report.problems[:PROBLEM_DISPLAY_LIMIT]:
            lines.append(f"  - {problem}")
        hidden = len(report.problems) - PROBLEM_DISPLAY_LIMIT
        if hidden > 0:
            lines.append(f"  ... and {hidden} more (use --json for the full list)")
    return lines

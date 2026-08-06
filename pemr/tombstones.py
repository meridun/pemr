"""Removal memory for layer-1 dedup — `pemr document tombstone list|add|rm` (issue #80).

Layer-1 document identity is a single lookup against the **live** `document` table
(:func:`ingest.get_document`), so `document rm` erases every trace that a hash was ever
seen. The next bulk-intake sweep over the same source folder re-ingests the file as
brand new, silently, and `--purge-blob` reads far more final than it is.

A blanket ignore-list of removed hashes would be the wrong fix: most removals are
*corrections* (wrong owner, bad scan, superseded version) and there the file should be
re-ingestable later. So removal has two intents that want opposite behaviour, and only
the second wants a tombstone:

    correction          -> re-ingest normally    (the default; unchanged)
    permanent exclusion -> refuse on re-ingest   (opt-in: `--tombstone`)

Hence one small table and four functions. The module imports :mod:`db` only — `ingest`
imports *it* for the hot-path lookup, and the CLI resolves a file path to a hex digest
(``ingest.hash_file``) before calling :func:`add_tombstone`, so the engine never takes a
path and the import graph stays acyclic.

A tombstone is never a one-way door: :func:`remove_tombstone` lifts one, and
``ingest --force`` overrides a single run without lifting it (see
:func:`ingest.ingest_document`).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

from . import db

#: A sha256 hex digest, post-``strip().lower()``. Validated in Python rather than as a
#: schema CHECK: a typo'd hash is a tombstone that silently matches nothing — exactly
#: the class of silent failure this feature exists to remove — so it earns a real
#: message naming the expected format.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class TombstoneNotFoundError(ValueError):
    """Raised when a sha256 has no tombstone (friendly rc=1 at the CLI)."""


class DocumentLiveError(ValueError):
    """`tombstone add` refused: that hash is a live `document`, so `rm --tombstone` is
    the coherent path (delete + record, one transaction)."""


def _now(now: str | None = None) -> str:
    """ISO8601 UTC to the second — the same stamp `document.ingested_at` carries."""
    return now or datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_sha256(sha256: str) -> str:
    """``strip().lower()`` a hex digest, or raise ``ValueError`` naming the format."""
    candidate = (sha256 or "").strip().lower()
    if not _SHA256_RE.match(candidate):
        raise ValueError(
            f"not a sha256 content hash: {sha256!r} - expected 64 hex characters "
            "(see `pemr document list --json` or an old `document rm --json` report)"
        )
    return candidate


def _row_view(row: sqlite3.Row) -> dict:
    return {
        "sha256": row["sha256"],
        "removed_at": row["removed_at"],
        "reason": row["reason"],
        "note": row["note"],
        "document_id": row["document_id"],
    }


def _clean(value: str | None) -> str | None:
    """Free text in, NULL out for empty/whitespace-only (no taxonomy, no validation)."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def live_document_id(conn: sqlite3.Connection, sha256: str) -> int | None:
    """The `document_id` currently filed under ``sha256``, or None."""
    row = conn.execute(
        "SELECT document_id FROM document WHERE sha256 = ?", (sha256,)
    ).fetchone()
    return int(row["document_id"]) if row is not None else None


def get_tombstone(conn: sqlite3.Connection, sha256: str) -> dict | None:
    """The tombstone for ``sha256``, or None. The hot path `ingest` calls.

    Deliberately does **not** normalize: `ingest` passes a digest it just computed.
    Callers taking human input go through :func:`normalize_sha256` first.
    """
    row = conn.execute(
        "SELECT * FROM document_tombstone WHERE sha256 = ?", (sha256,)
    ).fetchone()
    return _row_view(row) if row is not None else None


def add_tombstone(
    conn: sqlite3.Connection,
    sha256: str,
    *,
    reason: str | None = None,
    note: str | None = None,
    document_id: int | None = None,
    now: str | None = None,
    allow_live: bool = False,
    conn_managed: bool = False,
) -> dict:
    """Record (or refresh) the tombstone for ``sha256``; returns the stored row.

    ``UPSERT`` rather than a plain insert: a `--force`d ingest past a tombstone leaves
    the hash both live and tombstoned, and the later `document rm --tombstone` must
    refresh ``removed_at``/``reason``/``note``/``document_id`` rather than raising. That
    also makes a re-run of the same command a safe no-op-shaped write.

    Raises :class:`DocumentLiveError` when the hash is a live document and ``allow_live``
    is off — creating a tombstone *beside* a live document through a command whose whole
    purpose is exclusion would be incoherent. ``remove_document`` passes ``allow_live``
    because it is deleting that document in the same transaction.

    ``conn_managed=True`` skips the ``with conn:`` block, for callers that already own
    an open transaction (``documents.remove_document`` — a delete that commits without
    its tombstone is the exact failure being fixed, so they cannot separate).
    """
    db.require_migrated(conn)
    if not allow_live:
        live = live_document_id(conn, sha256)
        if live is not None:
            raise DocumentLiveError(
                f"sha256 {sha256[:12]}... is live as document #{live} - a tombstone "
                "beside a filed document would be incoherent. Remove and record it in "
                f"one step: `pemr document rm {live} --tombstone --apply`; "
                "nothing was written"
            )
    params = (sha256, _now(now), _clean(reason), _clean(note), document_id)
    sql = """
        INSERT INTO document_tombstone
          (sha256, removed_at, reason, note, document_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(sha256) DO UPDATE SET
          removed_at = excluded.removed_at,
          reason     = excluded.reason,
          note       = excluded.note,
          document_id = excluded.document_id
    """
    if conn_managed:
        conn.execute(sql, params)
    else:
        with conn:
            conn.execute(sql, params)
    stored = get_tombstone(conn, sha256)
    assert stored is not None  # just written
    return stored


def list_tombstones(conn: sqlite3.Connection) -> list[dict]:
    """Every tombstone, newest first, each with ``live_document_id``.

    ``live_document_id`` is normally None. It is not None exactly when somebody ran
    `ingest --force` past the tombstone: the row stays (force is a one-shot override,
    not a lift), so the hash is both filed and excluded. That state is deliberate and
    reachable, so `list` labels it rather than hiding it.
    """
    db.require_migrated(conn)
    rows = conn.execute(
        "SELECT * FROM document_tombstone ORDER BY removed_at DESC, sha256"
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        view = _row_view(row)
        view["live_document_id"] = live_document_id(conn, row["sha256"])
        out.append(view)
    return out


def remove_tombstone(conn: sqlite3.Connection, sha256: str) -> dict:
    """Lift the tombstone for ``sha256``; returns the row that was removed.

    Raises :class:`TombstoneNotFoundError` for an unknown hash — a typo must not read as
    success. Full 64-character hashes only (no prefix matching: a prefix matching two
    tombstones has no safe answer, and `list --json` supplies the full value).
    """
    db.require_migrated(conn)
    existing = get_tombstone(conn, sha256)
    if existing is None:
        raise TombstoneNotFoundError(
            f"no tombstone for {sha256} - see `pemr document tombstone list`"
        )
    with conn:
        conn.execute("DELETE FROM document_tombstone WHERE sha256 = ?", (sha256,))
    return existing


def has_table(conn: sqlite3.Connection) -> bool:
    """Whether `document_tombstone` exists — false for a snapshot predating migration
    007. `restore` needs this on both sides of its pre-replace diff, the same way
    :func:`verify.row_counts` omits tables missing from an older schema."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_tombstone'"
    ).fetchone()
    return row is not None


def describe(tombstone: dict) -> str:
    """One-line ``<date> (reason: x)`` summary shared by the CLI and restore printers."""
    day = (tombstone.get("removed_at") or "")[:10]
    reason = tombstone.get("reason")
    return f"{day} (reason: {reason})" if reason else f"{day} (no reason recorded)"

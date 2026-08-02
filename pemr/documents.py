"""Document recovery — `pemr document list|edit|reassign|rm`.

The escape hatch for a misfiled document (issue #54). Once `ingest` +
`commit-extraction` have run against the wrong person (or with the wrong
doc-date/category), the rows are otherwise stuck: `person remove` correctly
refuses once a person has records, and hand-editing SQLite means hand-fixing
``dedup_key`` and the FTS index too.

Four operations, mirroring `persons.py` in shape:

    * ``list_documents``   — what is on file, with the blast-radius record count
    * ``edit_document``    — correct doc_date/category/provider (no key impact)
    * ``reassign_document``— right document, wrong owner: move the document and
      every attached row, re-deriving each ``dedup_key`` (``person_id`` is part
      of every key — see :func:`dedup.dedup_key`)
    * ``remove_document``  — wrong file entirely: cascade-delete the attached
      rows, then the document

Safety idiom (matching `pemr rekey`): ``reassign`` and ``rm`` are **dry-run by
default**; ``apply=True`` writes. ``record_fts`` needs no explicit maintenance —
migration 003's AFTER UPDATE/DELETE triggers on each base table keep it in sync.

Single-provenance semantics: ``document_id`` is a row's sole owner. A fact a
second document also attests leaves no trace in the schema (``commit_extraction``
reports layer-2 duplicates only in its in-memory summary), so ``rm`` cannot
detect multi-document attestation and does not pretend to.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import db, dedup
from .persons import PersonNotFoundError, get_person


class DocumentNotFoundError(ValueError):
    """Raised when a document id does not resolve (friendly rc=1 at the CLI)."""


class ReassignCollisionError(RuntimeError):
    """A moved row's recomputed key already exists for the target person."""


class OpenConflictsError(ValueError):
    """Raised when a reassign is refused because open conflicts cite the document."""


class DictionaryDriftError(ValueError):
    """Stored keys no longer match the current dictionary; `pemr rekey` comes first."""


# Editable via `document edit`. None of these feed a dedup_key (keys are built from
# record fields only), so correcting them is a pure metadata UPDATE. sha256 and
# source_path are excluded by construction: the blob is content-addressed.
_EDITABLE_FIELDS = ("doc_date", "category", "provider")


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _require_document(conn: sqlite3.Connection, document_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM document WHERE document_id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise DocumentNotFoundError(
            f"no document with id {document_id} - see `pemr document list`"
        )
    return row


def _slug_for(conn: sqlite3.Connection, person_id: int | None) -> str | None:
    if person_id is None:
        return None
    row = conn.execute(
        "SELECT slug FROM person WHERE person_id = ?", (person_id,)
    ).fetchone()
    return row["slug"] if row is not None else None


def _record_counts(conn: sqlite3.Connection, document_id: int) -> dict[str, int]:
    """Rows attached to a document, per record type (stable key set for --json)."""
    counts: dict[str, int] = {}
    for record_type in dedup.KNOWN_TYPES:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {record_type} WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        counts[record_type] = int(row["n"])
    return counts


def _document_view(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """One document as a JSON-safe dict. ``ocr_text`` is deliberately replaced by
    ``has_ocr_text`` — a full document transcription has no business in list output."""
    counts = _record_counts(conn, row["document_id"])
    return {
        "document_id": row["document_id"],
        "sha256": row["sha256"],
        "person_id": row["person_id"],
        "person": _slug_for(conn, row["person_id"]),
        "doc_date": row["doc_date"],
        "category": row["category"],
        "provider": row["provider"],
        "source_path": row["source_path"],
        "ingested_at": row["ingested_at"],
        "has_ocr_text": bool(row["ocr_text"]),
        "records": counts,
        "record_count": sum(counts.values()),
    }


# --------------------------------------------------------------------------- #
# list / edit
# --------------------------------------------------------------------------- #

def list_documents(
    conn: sqlite3.Connection, person_slug: str | None = None
) -> list[dict]:
    """Documents newest-first (``document_id`` DESC), optionally one person's only.

    Newest-first because the recovery question is "which document did I just
    misfile", not alphabetical browsing. Raises :class:`PersonNotFoundError` for an
    unknown ``person_slug`` (an unknown slug is a typo, not an empty result).
    """
    db.require_migrated(conn)
    params: list[object] = []
    where = ""
    if person_slug is not None:
        person = get_person(conn, person_slug)
        if person is None:
            raise PersonNotFoundError(f"no person with slug '{person_slug}'")
        where = " WHERE person_id = ?"
        params.append(person.person_id)
    rows = conn.execute(
        f"SELECT * FROM document{where} ORDER BY document_id DESC", params
    ).fetchall()
    return [_document_view(conn, row) for row in rows]


def get_document_view(conn: sqlite3.Connection, document_id: int) -> dict:
    """One document in the :func:`list_documents` shape."""
    db.require_migrated(conn)
    return _document_view(conn, _require_document(conn, document_id))


def edit_document(
    conn: sqlite3.Connection, document_id: int, **fields: str | None
) -> dict:
    """Partial metadata update (``pemr document edit``) — the wrong-doc-date/category
    half of misfiling. Only the fields passed change; an explicit empty string clears
    the column to NULL. No ``dedup_key`` is affected (see :data:`_EDITABLE_FIELDS`).

    Raises :class:`DocumentNotFoundError` for an unknown id and ``ValueError`` for an
    unknown field or no fields to update.
    """
    db.require_migrated(conn)
    unknown = set(fields) - set(_EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"cannot edit field(s): {', '.join(sorted(unknown))}")
    if not fields:
        raise ValueError("nothing to update - pass at least one field to change")

    updates = {
        name: (value if value not in (None, "") else None)
        for name, value in fields.items()
    }
    _require_document(conn, document_id)
    assignments = ", ".join(f"{name} = ?" for name in updates)
    values = list(updates.values())
    values.append(document_id)
    with conn:
        conn.execute(
            f"UPDATE document SET {assignments} WHERE document_id = ?", values
        )
    return _document_view(conn, _require_document(conn, document_id))


# --------------------------------------------------------------------------- #
# reassign (right document, wrong owner)
# --------------------------------------------------------------------------- #

@dataclass
class ReassignChange:
    record_type: str
    row_id: int
    label: str
    old_key: str
    new_key: str


@dataclass
class ReassignReport:
    document_id: int
    from_slug: str | None
    to_slug: str
    changes: list[ReassignChange] = field(default_factory=list)
    applied: bool = False
    unchanged: bool = False          # target already owns the document
    target_inactive: bool = False    # allowed, but worth saying out loud

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for change in self.changes:
            out[change.record_type] = out.get(change.record_type, 0) + 1
        return out


def reassign_document(
    conn: sqlite3.Connection,
    document_id: int,
    person_slug: str,
    dictionary: dict[str, str] | None = None,
    *,
    apply: bool = False,
) -> ReassignReport:
    """Move a document and every row it produced to another person.

    ``person_id`` is part of every ``dedup_key`` (:func:`dedup.dedup_key`), so each
    attached row's key is re-derived under the new owner; ``record_fts`` follows via
    migration 003's AFTER UPDATE triggers, keeping `find --person` correct.

    Dry-run by default: pass ``apply=True`` to write. Three refusals, all before any
    write, all leaving the database untouched:

    * **open conflicts** citing this document (:class:`OpenConflictsError`) — such a
      conflict pairs an incoming row from this document against a stored row owned by
      the *old* person; resolving it after a move would write data into records the
      document no longer owns. Resolve them first (`pemr review-conflicts`).
    * **dictionary drift** (:class:`DictionaryDriftError`) — a stored key that does not
      match its recompute under the current dictionary means a reassign would silently
      perform a `rekey` too. Run `pemr rekey --apply` first; reassign changes ownership
      and nothing else.
    * **collision** (:class:`ReassignCollisionError`) — the target person already has
      that exact fact on file.

    A reassign to the current owner is a reported no-op, not an error.
    """
    db.require_migrated(conn)
    doc = _require_document(conn, document_id)
    target = get_person(conn, person_slug)
    if target is None:
        raise PersonNotFoundError(f"no person with slug '{person_slug}'")

    from_slug = _slug_for(conn, doc["person_id"])
    report = ReassignReport(
        document_id=document_id,
        from_slug=from_slug,
        to_slug=target.slug,
        target_inactive=target.deactivated_at is not None,
    )
    if doc["person_id"] == target.person_id:
        report.unchanged = True
        report.applied = apply
        return report

    open_conflicts = conn.execute(
        "SELECT conflict_id FROM conflict WHERE document_id = ? AND status = 'open' "
        "ORDER BY conflict_id",
        (document_id,),
    ).fetchall()
    if open_conflicts:
        ids = ", ".join(f"#{row['conflict_id']}" for row in open_conflicts)
        raise OpenConflictsError(
            f"document {document_id} has open conflict(s) {ids} staged against "
            f"'{from_slug}'; resolving one after the move would write this document's "
            "data into records it no longer owns. Resolve them first with "
            "`pemr review-conflicts`; nothing was written"
        )

    # Plan every move first: a refusal must leave the database untouched.
    for record_type in dedup.KNOWN_TYPES:
        pk = f"{record_type}_id"
        rows = conn.execute(
            f"SELECT * FROM {record_type} WHERE document_id = ? ORDER BY {pk}",
            (document_id,),
        ).fetchall()
        for row in rows:
            label = dedup._rekey_label(record_type, row)
            payload = {name: row[name] for name in dedup.FIELD_SPECS[record_type]}
            current = dedup.dedup_key(
                record_type, payload, row["person_id"], dictionary
            )
            if current != row["dedup_key"]:
                raise DictionaryDriftError(
                    f"{record_type}: {pk} {row[pk]} ({label!r}) does not match its "
                    "stored dedup_key under the current dictionary - the dictionary "
                    "changed since it was committed. Run `pemr rekey --apply` first, "
                    "then retry; nothing was written"
                )
            new_key = dedup.dedup_key(
                record_type, payload, target.person_id, dictionary
            )
            # Every moving row shares one old person_id and one new one, and the drift
            # check above proves the stored keys are injective under the current
            # dictionary — so new keys cannot collide with each other or with the
            # moving rows' own old keys. Any hit here is a non-moving row, i.e. a fact
            # the target person already has. (No two-pass placeholder dance needed,
            # unlike `dedup.rekey`, where arbitrary rows swap keys mid-flight.)
            clash = conn.execute(
                f"SELECT {pk} FROM {record_type} WHERE dedup_key = ?", (new_key,)
            ).fetchone()
            if clash is not None:
                raise ReassignCollisionError(
                    f"{record_type}: {pk} {row[pk]} ({label!r}) would collide with "
                    f"{pk} {clash[pk]}, which '{target.slug}' already has on file - "
                    "the same fact cannot land twice; nothing was written"
                )
            report.changes.append(
                ReassignChange(
                    record_type, row[pk], label, row["dedup_key"], new_key
                )
            )

    if apply:
        # One transaction: a half-moved document splits a person's history in two.
        with conn:
            conn.execute(
                "UPDATE document SET person_id = ? WHERE document_id = ?",
                (target.person_id, document_id),
            )
            for change in report.changes:
                conn.execute(
                    f"UPDATE {change.record_type} SET person_id = ?, dedup_key = ? "
                    f"WHERE {change.record_type}_id = ?",
                    (target.person_id, change.new_key, change.row_id),
                )
    report.applied = apply
    return report


# --------------------------------------------------------------------------- #
# rm (wrong file entirely)
# --------------------------------------------------------------------------- #

@dataclass
class RemoveReport:
    document_id: int
    person_slug: str | None
    sha256: str
    source_path: str
    records: dict[str, int] = field(default_factory=dict)
    conflicts_deleted: int = 0      # open conflicts, removed with the document
    conflicts_detached: int = 0     # resolved conflicts, document_id nulled
    blob_path: str = ""             # absolute when a sources dir is known
    blob_purged: bool = False
    applied: bool = False

    @property
    def record_count(self) -> int:
        return sum(self.records.values())


def remove_document(
    conn: sqlite3.Connection,
    document_id: int,
    *,
    sources_dir: str | Path | None = None,
    purge_blob: bool = False,
    apply: bool = False,
) -> RemoveReport:
    """Delete a document and cascade to every row it produced.

    Deliberately the opposite of :func:`persons.remove_person`, which refuses when
    child rows exist: a person with records has a non-destructive alternative
    (`person deactivate`), a misfiled document has none, so refusing would defeat the
    command. The dry run (default) supplies the safety instead — it reports the exact
    blast radius, and only ``apply=True`` writes.

    Conflicts citing the document: open ones are deleted (their incoming rows never
    landed and their document is going away), resolved ones keep their audit trail
    with ``document_id`` nulled out. Both counts are reported.

    The scan under ``sources_dir`` is **kept** unless ``purge_blob`` is set — it is the
    one thing here that cannot be regenerated, and an orphan blob is harmless
    (content-addressed; a later re-ingest of the same file reuses it). Deleting the
    blob happens after the transaction commits, so a failed unlink leaves an orphan
    file rather than a document row pointing at a file that no longer exists.
    """
    db.require_migrated(conn)
    doc = _require_document(conn, document_id)
    if purge_blob and sources_dir is None:
        raise ValueError(
            "purging the blob needs a sources dir - pass --sources, set PEMR_SOURCES, "
            "or set [paths].sources_dir in config.toml"
        )

    blob = Path(sources_dir) / doc["source_path"] if sources_dir is not None else None
    report = RemoveReport(
        document_id=document_id,
        person_slug=_slug_for(conn, doc["person_id"]),
        sha256=doc["sha256"],
        source_path=doc["source_path"],
        records=_record_counts(conn, document_id),
        blob_path=str(blob) if blob is not None else doc["source_path"],
    )
    report.conflicts_deleted = int(conn.execute(
        "SELECT COUNT(*) AS n FROM conflict WHERE document_id = ? AND status = 'open'",
        (document_id,),
    ).fetchone()["n"])
    report.conflicts_detached = int(conn.execute(
        "SELECT COUNT(*) AS n FROM conflict WHERE document_id = ? AND status != 'open'",
        (document_id,),
    ).fetchone()["n"])

    if apply:
        # Children first: foreign_keys=ON with NO ACTION means the document DELETE
        # raises while anything still references it. record_fts is trigger-maintained.
        with conn:
            conn.execute(
                "DELETE FROM conflict WHERE document_id = ? AND status = 'open'",
                (document_id,),
            )
            conn.execute(
                "UPDATE conflict SET document_id = NULL WHERE document_id = ?",
                (document_id,),
            )
            for record_type in dedup.KNOWN_TYPES:
                conn.execute(
                    f"DELETE FROM {record_type} WHERE document_id = ?", (document_id,)
                )
            conn.execute(
                "DELETE FROM document WHERE document_id = ?", (document_id,)
            )
        if purge_blob and blob is not None:
            blob.unlink(missing_ok=True)
            try:
                blob.parent.rmdir()   # drop the 2-char shard dir once it is empty
            except OSError:
                pass                  # not empty (or gone) - leave it alone
            report.blob_purged = True
    report.applied = apply
    return report

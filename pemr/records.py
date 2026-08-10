"""Row-level record repair — `pemr record rm` (issue #107).

The scalpel next to `document rm`'s hammer. When `pemr rekey` reports a
**doubled** collision — one clinical fact filed twice, once under a pre-drift key
(:func:`dedup.rekey`) — the offending row often lives inside a visit note that
also holds a dozen perfectly good records. `document rm` would take all of them,
and the project's write-path rule (`AGENTS.md`) forbids the other option, a hand
edit in SQLite. This module is the missing third door: delete **one** row, by
primary key, and nothing else.

One operation, shaped like `documents.remove_document`:

    * ``remove_record`` — delete a single typed record row, dry-run by default

``record_fts`` needs no explicit maintenance: migrations 003 and 006 put an
AFTER DELETE trigger on every one of :data:`dedup.KNOWN_TYPES`, which is the same
thing `documents.remove_document` relies on.

**Occurrence numbers are not renumbered.** Deleting occurrence 0 of a
multi-occurrence family leaves the base occupied only at ``n >= 1``; that is a
state `dedup._conflict_family` / `dedup._anchor_row` already handle (`document rm`
produces it today), and renumbering would rewrite sibling ``dedup_key``s out from
under any conflict staged against them.

**Open conflicts are refused, not deleted.** `document rm` deletes an open
conflict anchored to a row it is removing, because the conflict's document is
going away with it. Here the document *survives*, so `keep both` is still a live
resolution and the conflict's ``incoming_json`` is still worth something — no dry
run could undo destroying it. So: removal is refused when it would empty the
conflict's identity family, and allowed (with the conflict re-anchoring itself to
the lowest surviving occurrence) when a sibling remains.

**Row-scoped curation verdicts are retired with the row** (issue #114). A record
id is a plain rowid alias — no ``AUTOINCREMENT`` on any record table — so removing
the highest-id row frees that id for the next insert, and a `record annotate --row`
verdict left behind would re-attach to whatever unrelated record lands on it. The
dry run names the verdict; ``apply`` lifts it in the same transaction as the delete
(:func:`curation.retire_row_verdicts`). Family-scoped verdicts are untouched:
``dedup_base`` is content-derived, so re-attachment there is intentional.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import curation, db, dedup


class RecordNotFoundError(ValueError):
    """Raised when a record row id does not resolve (friendly rc=1 at the CLI)."""


class AnchoredConflictError(ValueError):
    """Refused: removing the row would strand an open conflict with nothing to
    resolve against (its identity family would be empty)."""


@dataclass
class RecordRemoveReport:
    record_type: str
    row_id: int
    person_id: int | None
    person_slug: str | None
    document_id: int | None
    label: str
    # The row's FIELD_SPECS payload columns — what the operator needs to confirm
    # they are deleting the right fact. Internal key columns are reported
    # separately below rather than mixed into the payload.
    fields: dict = field(default_factory=dict)
    dedup_key: str = ""
    dedup_base: str = ""
    dedup_occurrence: int = 0
    family_size: int = 0          # rows sharing this dedup_base, including this one
    family_remaining: int = 0     # ... after the removal
    conflicts_reanchored: list[int] = field(default_factory=list)
    # Row-scoped curation verdicts naming this row - lifted with it (issue #114), and
    # named in the dry run because a recorded clinical judgment is going away.
    curation_retired: list[dict] = field(default_factory=list)
    applied: bool = False


def _slug_for(conn: sqlite3.Connection, person_id: int | None) -> str | None:
    if person_id is None:
        return None
    row = conn.execute(
        "SELECT slug FROM person WHERE person_id = ?", (person_id,)
    ).fetchone()
    return row["slug"] if row is not None else None


def remove_record(
    conn: sqlite3.Connection,
    record_type: str,
    row_id: int,
    dictionary: dict[str, str] | None = None,
    *,
    apply: bool = False,
) -> RecordRemoveReport:
    """Delete one row of one record table, addressed by primary key.

    Dry-run by default: pass ``apply=True`` to write. The dry run *is* the safety
    mechanism (there is no undo and no tombstone — a record row has no content
    hash to key one on), so the report has to be truthful about the blast radius:
    the row's payload, its position in its identity family, any open conflict
    the removal re-anchors, and any row-scoped curation verdict it retires.

    Deliberately narrow. It touches exactly one record row: the document survives
    with every other record it produced, ``dedup_key``s are neither recomputed nor
    renumbered (deletion is key-neutral), and the ``conflict`` table is never
    written. The one thing outside the row it does write is the row's own
    ``curation`` verdict, and only the row-scoped one (issue #114 — see the module
    docstring: the id becomes reusable, the verdict must not outlive it). That makes
    it composable with the collision it exists to resolve —
    `pemr rekey` names the two row ids, `pemr record rm` drops the degraded one,
    `pemr rekey --apply` then runs clean.

    Raises ``ValueError`` for an unknown ``record_type``,
    :class:`RecordNotFoundError` for an unknown id, and
    :class:`AnchoredConflictError` when an open conflict is anchored to the row's
    identity family and the row is the last member of it — resolve the conflict
    first (`pemr review-conflicts`), which is the only way its staged payload
    survives.
    """
    db.require_migrated(conn)
    if record_type not in dedup.FIELD_SPECS:
        raise ValueError(
            f"unknown record type '{record_type}' - known types: "
            f"{', '.join(dedup.KNOWN_TYPES)}"
        )
    pk = f"{record_type}_id"
    row = conn.execute(
        f"SELECT * FROM {record_type} WHERE {pk} = ?", (row_id,)
    ).fetchone()
    if row is None:
        raise RecordNotFoundError(
            f"no {record_type} with id {row_id} - row ids come from `pemr rekey`'s "
            "collision report, or `pemr query`"
        )

    family = dedup.load_family(conn, record_type, row["dedup_base"])
    family_size = len(family)
    report = RecordRemoveReport(
        record_type=record_type,
        row_id=row_id,
        person_id=row["person_id"],
        person_slug=_slug_for(conn, row["person_id"]),
        document_id=row["document_id"],
        label=dedup._rekey_label(record_type, row),
        fields={name: row[name] for name in dedup.FIELD_SPECS[record_type]},
        dedup_key=row["dedup_key"],
        dedup_base=row["dedup_base"],
        dedup_occurrence=int(row["dedup_occurrence"] or 0),
        family_size=family_size,
        family_remaining=max(family_size - 1, 0),
    )

    anchored = dedup.conflicts_anchored_to_row(conn, record_type, row, dictionary)
    if anchored and report.family_remaining == 0:
        ids = ", ".join(f"#{cid}" for cid in anchored)
        raise AnchoredConflictError(
            f"{record_type} {row_id} ({report.label!r}) is the last row of the "
            f"identity open conflict(s) {ids} are staged against - removing it "
            "would leave them with nothing to resolve against, and their staged "
            "payload is not recoverable. Resolve them first with "
            "`pemr review-conflicts --resolve <id> --keep both|existing`; "
            "nothing was written"
        )
    # Siblings survive, so the conflict simply re-anchors to the lowest remaining
    # occurrence on its next resolution (`dedup._anchor_row`) - allowed, but the
    # report says so out loud because the anchor moving is not obvious.
    report.conflicts_reanchored = anchored
    # The row id is about to become reusable, so any row-scoped verdict naming it is
    # about to become a mis-render waiting for the next insert (issue #114).
    report.curation_retired = curation.row_verdicts_for(conn, record_type, [row_id])

    if apply:
        # record_fts follows via the per-table AFTER DELETE triggers (migrations
        # 003/006). A rowcount other than 1 is an error, never a silent success -
        # the same guard idiom as `dedup._overwrite_record`.
        with conn:
            cur = conn.execute(f"DELETE FROM {record_type} WHERE {pk} = ?", (row_id,))
            if cur.rowcount != 1:
                raise RecordNotFoundError(
                    f"no {record_type} with id {row_id} - nothing was deleted"
                )
            # Same transaction as the delete: a commit that dropped the row but kept
            # its verdict is exactly the state that mis-renders once the id is reused.
            curation.retire_row_verdicts(conn, record_type, [row_id])
    report.applied = apply
    return report

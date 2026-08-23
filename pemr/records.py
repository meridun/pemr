"""Row-level record repair — `pemr record rm` (issue #107), `record edit` (issue #129).

The scalpel next to `document rm`'s hammer. When `pemr rekey` reports a
**doubled** collision — one clinical fact filed twice, once under a pre-drift key
(:func:`dedup.rekey`) — the offending row often lives inside a visit note that
also holds a dozen perfectly good records. `document rm` would take all of them,
and the project's write-path rule (`AGENTS.md`) forbids the other option, a hand
edit in SQLite. This module is the missing third door: reach **one** row, by
primary key, and nothing else.

Two operations, both shaped like `documents.remove_document`:

    * ``remove_record`` — delete a single typed record row, dry-run by default
    * ``edit_record`` — correct a single row's **non-key** fields in place,
      dry-run by default, every change written to an append-only ledger; with
      ``identity=True`` (`record edit --identity`) it instead *moves* the row onto
      the identity its corrected fields derive — a one-row rekey (issue #152)

``record_fts`` needs no explicit maintenance for either: migrations 003 and 006
put AFTER DELETE **and** AFTER UPDATE triggers on every one of
:data:`dedup.KNOWN_TYPES`, which is the same thing `documents.remove_document`
relies on.

**An edit corrects a field; it never rewrites provenance** (issue #129). The
editable set is *derived* — :func:`dedup.editable_fields`, i.e. ``FIELD_SPECS``
minus ``KEY_FIELDS`` — so ``document_id``, ``person_id``, the ``dedup_*`` columns
and the ``attested_*`` columns are unreachable from an edit by construction, not
by a denylist. That is the whole point: the two workarounds this replaces
(re-committing the corrected row through `commit-extraction`, or
delete-and-recommit) both re-attribute the row to whichever document is passed at
correction time, which is a lie about where the fact came from. After an edit the
row still traces to its original source; what changed is that it now *differs*
from what that source literally said — and that is exactly what the ledger
records.

**Ledger entries outlive their row, deliberately.** A record id is a plain rowid
alias, so an entry naming a deleted row could later look like it belongs to the
id's next occupant — the #114 hazard, which row-scoped curation answers by
retiring the verdict with the row. Here the answer is the opposite: nothing
*resolves through* the ledger (render, verify, rekey and rekey's collision
resolver never read it), so a stale entry mis-renders nothing, while retiring one
would destroy the audit trail this feature exists to create. Handled by
disclosure instead — :func:`remove_record` names the entries pointing at the row
it is about to delete, and each entry keeps its ``dedup_base`` breadcrumb so a
reader can tell whether a later occupant is even the same family.

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

**An identity edit is a one-row rekey, not a fourth mechanism** (issue #152). Once
``condition.onset_on`` identifies the *episode*, correcting a wrong onset date is by
construction an identity change, and the old advice — "`record rm` and re-commit" —
throws away the row's provenance and its ledger to fix a typo. ``--identity`` moves
the row instead: same payload write, same ledger, same correction mark, plus the
three ``dedup_*`` columns. Its consequences are deliberately the *same* consequences
`pemr rekey` already has, reported the same way:

    * **row-scoped** verdicts follow the row for free — :mod:`pemr.curation`
      resolves them by ``record_id``, and a row verdict's stored ``dedup_base`` is a
      breadcrumb that is never consulted (:data:`curation.ORPHAN_KINDS`);
    * a **family-scoped** verdict on the base the row vacated orphans exactly as a
      `rekey` base move orphans one — and *only* when the row was the last of its
      family, :data:`curation.ORPHAN_NO_FAMILY`'s own rule. The report carries it in
      the shape `pemr rekey --apply --json` emits, so `pemr record reaffirm
      --map-file` re-points it with no new plumbing;
    * ``dedup_occurrence`` is **not** renumbered on either side, for the reason
      stated above for deletion;
    * an open conflict anchored to the row is refused when the move would empty its
      family, and disclosed as re-anchoring when a sibling survives — the
      :func:`remove_record` rule, for the same reason: the document survives, so
      `keep both` is still a live resolution.

What it refuses outright is a *merge*: if the target family already holds a row that
says the same thing, moving this one there would file one fact twice under one
identity. That is `record rm` / `record annotate --status merged-into` work, and the
error says so (:class:`IdentityCollisionError`).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import curation, db, dedup


class RecordNotFoundError(ValueError):
    """Raised when a record row id does not resolve (friendly rc=1 at the CLI)."""


class AnchoredConflictError(ValueError):
    """Refused: removing the row would strand an open conflict with nothing to
    resolve against (its identity family would be empty)."""


class FieldNotEditableError(ValueError):
    """Refused: a named field is unknown, or participates in the row's identity.

    Identity fields are not off-limits because editing them is unthinkable — it is
    because an identity change is a *different operation*, with its own answers
    (a dictionary edit + `pemr rekey`, or — since issue #152 — the same command with
    ``--identity``). The message points there rather than leaving the operator
    guessing.
    """


class IdentityCollisionError(ValueError):
    """Refused: the identity the edit moves the row onto is already occupied by a row
    saying the same thing (issue #152).

    Not a correction but a **merge**, and merging is a recorded human judgment with its
    own verbs (`record annotate --status merged-into`, or `record rm` once the operator
    has decided which row is the survivor). Doing it silently here would file one
    clinical fact twice under one identity and hide the duplicate behind an occurrence
    number.
    """


def _require_type(record_type: str) -> None:
    """Guard every f-string table interpolation below: a record type reaches SQL only
    after matching a known key."""
    if record_type not in dedup.FIELD_SPECS:
        raise ValueError(
            f"unknown record type '{record_type}' - known types: "
            f"{', '.join(dedup.KNOWN_TYPES)}"
        )


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
    # Ledger entries (issue #129) naming this row — DISCLOSED, never retired, unlike the
    # row-scoped verdicts above. See the module docstring for why the two hazards get
    # opposite answers.
    edits_recorded: list[dict] = field(default_factory=list)
    applied: bool = False


@dataclass
class RecordEditReport:
    record_type: str
    row_id: int
    person_id: int | None
    person_slug: str | None
    document_id: int | None
    label: str
    # One entry per field this edit actually moves: {"field", "old", "new"}.
    changes: list[dict] = field(default_factory=list)
    # Named fields whose stored value already equalled the requested one. Reported, not
    # silently dropped: a re-run of the same command has to be visibly a no-op.
    unchanged: list[str] = field(default_factory=list)
    dedup_key: str = ""
    dedup_base: str = ""
    dedup_occurrence: int = 0
    # --- identity edit (issue #152) --------------------------------------------
    # Whether `--identity` was passed AND the corrected payload actually moves the key.
    # False on an ordinary correction and on an inert `--identity` (nothing moved), so
    # a reader never has to distinguish "asked for" from "happened".
    identity: bool = False
    # Where the row lands. Equal to the three columns above when `identity` is False.
    new_dedup_key: str = ""
    new_dedup_base: str = ""
    new_dedup_occurrence: int = 0
    # Family-scoped verdicts left pointing at the base this row vacated - only when it
    # was the family's last row. Each entry is shaped like an entry of `pemr rekey
    # --apply --json`'s `orphans` array, so the report feeds `pemr record reaffirm
    # --map-file` unmodified. Empty on an ordinary correction.
    curation_orphaned: list[dict] = field(default_factory=list)
    # Open conflicts whose anchor row moves out from under them (siblings survive, so
    # they re-anchor on their next resolution). The `remove_record` disclosure.
    conflicts_reanchored: list[int] = field(default_factory=list)
    note: str = ""
    attributed_to: str | None = None
    edited_at: str = ""
    applied: bool = False

    def as_dict(self) -> dict:
        return {
            "record_type": self.record_type,
            "row_id": self.row_id,
            "person": self.person_slug,
            "person_id": self.person_id,
            "document_id": self.document_id,
            "label": self.label,
            "changes": self.changes,
            "unchanged": self.unchanged,
            "dedup_key": self.dedup_key,
            "dedup_base": self.dedup_base,
            "dedup_occurrence": self.dedup_occurrence,
            # Additive-only, per the `--json` contract rule: an existing consumer that
            # never asked about identity edits reads exactly what it read before.
            "identity": self.identity,
            "new_dedup_key": self.new_dedup_key,
            "new_dedup_base": self.new_dedup_base,
            "new_dedup_occurrence": self.new_dedup_occurrence,
            "curation_orphaned": self.curation_orphaned,
            "conflicts_reanchored": self.conflicts_reanchored,
            "note": self.note,
            "attributed_to": self.attributed_to,
            "edited_at": self.edited_at,
            "applied": self.applied,
        }


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
    _require_type(record_type)
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
    # Corrections recorded against this row (issue #129). Disclosed and left in place:
    # the ledger is append-only, and nothing resolves through it. The operator still has
    # to be told, because a row someone deliberately corrected is more likely to be the
    # wrong one to delete.
    report.edits_recorded = edits_for_rows(conn, record_type, [row_id])

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


# --------------------------------------------------------------------------- #
# in-place correction of non-key fields (`record edit`) - issue #129
# --------------------------------------------------------------------------- #

def has_edit_table(conn: sqlite3.Connection) -> bool:
    """Whether `record_edit` exists — false for a database predating migration 012.

    The :func:`curation.has_table` guard: a restored older snapshot must report "no
    edits" rather than raise ``no such table``.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='record_edit'"
    ).fetchone()
    return row is not None


def _ledger_view(row: sqlite3.Row) -> dict:
    return {
        "record_edit_id": int(row["record_edit_id"]),
        "record_type": row["record_type"],
        "record_id": int(row["record_id"]),
        "dedup_base": row["dedup_base"],
        "field": row["field"],
        "old": row["old_value"],
        "new": row["new_value"],
        "note": row["note"],
        "attributed_to": row["attributed_to"],
        "edited_at": row["edited_at"],
    }


def _ledger_text(value: object) -> str | None:
    """How a field value is rendered into the ledger: ``None`` stays NULL (the column
    *was* unset, which is not the same as an empty string), everything else becomes its
    ``str``. The ledger is a human-readable audit record, not a replayable patch."""
    return None if value is None else str(value)


def edits_for_rows(
    conn: sqlite3.Connection, record_type: str, record_ids: Iterable[int]
) -> list[dict]:
    """The ledger entries naming any of ``record_ids``, newest first.

    The :func:`curation.row_verdicts_for` twin, for :func:`remove_record`'s disclosure —
    but note the different ending: these entries are reported and then *left alone*.
    """
    _require_type(record_type)
    wanted = {int(rid) for rid in record_ids}
    if not wanted or not has_edit_table(conn):
        return []
    rows = conn.execute(
        "SELECT * FROM record_edit WHERE record_type = ? "
        "ORDER BY edited_at DESC, record_edit_id DESC",
        (record_type,),
    ).fetchall()
    return [e for e in (_ledger_view(r) for r in rows) if e["record_id"] in wanted]


def list_edits(
    conn: sqlite3.Connection, record_type: str | None = None
) -> list[dict]:
    """Every recorded correction (optionally one record type), newest first.

    Each entry carries the **live** ``label`` of the row it names, or ``""`` when that
    row is gone — the ledger outlives its row on purpose (module docstring), and an
    entry whose row has since been deleted is history, not an error.
    """
    db.require_migrated(conn)
    if record_type is not None:
        _require_type(record_type)
    if not has_edit_table(conn):
        return []
    sql = "SELECT * FROM record_edit"
    params: tuple = ()
    if record_type is not None:
        sql += " WHERE record_type = ?"
        params = (record_type,)
    sql += " ORDER BY edited_at DESC, record_edit_id DESC"
    out: list[dict] = []
    for row in conn.execute(sql, params).fetchall():
        entry = _ledger_view(row)
        entry["label"] = _live_label(conn, entry["record_type"], entry["record_id"])
        out.append(entry)
    return out


def _live_label(conn: sqlite3.Connection, record_type: str, record_id: int) -> str:
    """``dedup._rekey_label`` for a row that may no longer exist (``""`` when it does
    not). A stored ledger row can name anything, so the type is re-validated here before
    it is interpolated into a table name — the :func:`curation.list_curation` idiom."""
    if record_type not in dedup.FIELD_SPECS:
        return ""
    pk = f"{record_type}_id"
    row = conn.execute(
        f"SELECT * FROM {record_type} WHERE {pk} = ?", (record_id,)
    ).fetchone()
    return "" if row is None else dedup._rekey_label(record_type, row)


def _same_value(stored: object, incoming: object) -> bool:
    """Whether an edit would actually move the column.

    Numbers compare numerically (`dedup._rows_equal`'s rule): a numeric column
    round-trips out of SQLite as ``float``, so ``--set value_num=45`` against a stored
    ``45.0`` is a no-op, not a change to be ledgered.
    """
    if stored is None or incoming is None:
        return stored is incoming
    if isinstance(stored, (int, float)) and isinstance(incoming, (int, float)):
        if not isinstance(stored, bool) and not isinstance(incoming, bool):
            return float(stored) == float(incoming)
    return stored == incoming


def _plan_identity_move(
    conn: sqlite3.Connection,
    record_type: str,
    row: sqlite3.Row,
    before: dict,
    after: dict,
    after_base: str,
    dictionary: dict[str, str] | None,
    report: RecordEditReport,
) -> None:
    """Gate a one-row rekey and fill in where the row lands (issue #152).

    Everything that can refuse the move happens here, before the transaction opens —
    :func:`dedup._plan_keep_both`'s shape, and for its reason. Writes nothing; it only
    raises, or populates ``report``'s ``new_dedup_*`` / ``curation_orphaned`` /
    ``conflicts_reanchored`` fields.

    Called only when the corrected payload genuinely derives a different base, so the
    "``--identity`` is inert" case never reaches here.
    """
    pk = f"{record_type}_id"
    row_id = int(row[pk])
    person_id = row["person_id"]

    # 1. Drift. Both bases are checked, not just the one being left: moving *into* a
    # family whose rows are stored under stale keys would file the row beside a sibling
    # the engine can no longer see as one. Either way the remedy is the same run of
    # `pemr rekey --apply`, and its message already says so.
    dedup._assert_no_key_drift(conn, record_type, [before, after], person_id, dictionary)

    # 2. Occupied target. `_rows_equal` is the engine's own duplicate-vs-conflict
    # question, so "the same thing" here means exactly what it means at commit time -
    # including the sparse-type reading, where a thinner stored sibling still counts as
    # agreeing. The self-exclusion is belt-and-braces: gate 1 guarantees the row is
    # stored under its pre-move base, so it cannot already be in the target family.
    target = dedup.load_family(conn, record_type, after_base)
    twin = next(
        (m for m in target
         if int(m[pk]) != row_id and dedup._rows_equal(record_type, m, after)),
        None,
    )
    if twin is not None:
        raise IdentityCollisionError(
            f"refusing to move {record_type} {row_id} onto identity "
            f"{after_base[:12]}...: {record_type} {int(twin[pk])} "
            f"({dedup._rekey_label(record_type, twin)!r}) already says the same thing "
            "there. Filing both under one identity is a merge, not a correction - "
            f"record it with `pemr record annotate {record_type} {row_id} --status "
            f"merged-into --merged-into {after_base}`, or drop the redundant row with "
            f"`pemr record rm {record_type} {row_id}`; nothing was written"
        )
    # Monotonic over the target family and never reused, so a hole left by an earlier
    # removal stays a hole rather than letting this row inherit a departed sibling's key.
    occurrence = max((int(m["dedup_occurrence"] or 0) for m in target), default=-1) + 1
    report.new_dedup_base = after_base
    report.new_dedup_occurrence = occurrence
    report.new_dedup_key = dedup.occurrence_key(after_base, occurrence)

    # 3. Anchored conflicts, and what the vacated family loses. Both turn on the same
    # question - does anything survive on the old base - so the survivors are computed
    # once. Occurrence numbers on *that* side are not renumbered either.
    survivors = [
        m for m in dedup.load_family(conn, record_type, row["dedup_base"])
        if int(m[pk]) != row_id
    ]
    anchored = dedup.conflicts_anchored_to_row(conn, record_type, row, dictionary)
    if anchored and not survivors:
        ids = ", ".join(f"#{cid}" for cid in anchored)
        raise AnchoredConflictError(
            f"{record_type} {row_id} ({report.label!r}) is the last row of the "
            f"identity open conflict(s) {ids} are staged against - moving it to a new "
            "identity would leave them with nothing to resolve against, and their "
            "staged payload is not recoverable. Resolve them first with "
            "`pemr review-conflicts --resolve <id> --keep both|existing`; "
            "nothing was written"
        )
    report.conflicts_reanchored = anchored

    # 4. Verdict carry-over. Row-scoped verdicts need nothing: `curation` resolves them
    # by `record_id` and never consults the stored base. A family-scoped verdict is
    # orphaned only when the base it names goes empty - `curation.ORPHAN_NO_FAMILY`'s own
    # rule - so a family with survivors keeps its ruling and this stays quiet.
    if not survivors:
        verdict = curation.get_verdict(conn, record_type, row["dedup_base"])
        if verdict is not None:
            report.curation_orphaned = [{
                **verdict,
                "kinds": [curation.ORPHAN_NO_FAMILY],
                "label": "",
                "family_size": 0,
                # The two keys `pemr record reaffirm --map-file` re-points on, named
                # exactly as `pemr rekey --apply --json` names them, so this report is a
                # valid map file with no reshaping.
                "successor_base": after_base,
                "successor_merge_base": None,
            }]


def edit_record(
    conn: sqlite3.Connection,
    record_type: str,
    row_id: int,
    updates: dict,
    dictionary: dict[str, str] | None = None,
    *,
    note: str,
    attributed_to: str | None = None,
    now: str | None = None,
    identity: bool = False,
    apply: bool = False,
) -> RecordEditReport:
    """Correct one row's **non-key** fields in place, addressed by primary key.

    Dry-run by default: pass ``apply=True`` to write. The third door beside
    :func:`remove_record` — same single-row scalpel, same dry-run contract — except it
    corrects rather than deletes. The case it exists for is a display field that is
    simply *mislabelled*: a unit recorded as ``lbs`` beside forty-five ``lb``, same
    scale, same value, differing only by which extraction submitted it.

    ``updates`` maps field name -> new value; ``None`` clears the column. Which names
    are accepted is **derived**, not listed: :func:`dedup.editable_fields`, i.e. every
    ``FIELD_SPECS`` column that is not in ``KEY_FIELDS``. So the identity fields are
    refused (:class:`FieldNotEditableError`), and ``document_id`` / ``person_id`` /
    ``dedup_*`` / ``attested_*`` are not nameable at all — they are outside
    ``FIELD_SPECS``. That is the provenance guarantee: after an edit the row still
    traces to its original document (or attestation), which is exactly what the
    workarounds this replaces could not promise.

    ``note`` is required and must be non-empty after ``strip()`` (the
    :func:`curation.annotate_record` rule): an unexplained mutation of a stored clinical
    value is not an audit trail. Every changed field is written to the append-only
    ``record_edit`` ledger in the **same transaction** as the UPDATE.

    That same transaction stamps :data:`dedup.EDIT_MARK_COLUMNS` on the row (migration
    016, issue #134), which is what makes the correction visible in ``render`` and
    ``query``: the mark is **disclosure**, the ledger is the **audit trail**. A corrected
    row must never read as a verbatim quotation of its source, and only the row itself can
    say so safely — the ledger outlives its row by design, so deriving the mark from it at
    read time would caveat whichever row later inherits a recycled id.

    Validation is entirely front-loaded — nothing is written on any raise. In order:
    schema present; known type; row exists; at least one update; every name editable;
    a real note; the merged payload passes :func:`dedup.validate_row` (so type, ISO-date
    and enum rules bind exactly as they did at commit); and finally the identity guard —
    the ``dedup_key`` recomputed over the payload *before* and *after* must be equal.
    That last check is belt-and-braces over :data:`dedup.KEY_FIELDS`, and is drift-immune
    because it compares like with like: a row whose stored key predates a dictionary edit
    is still editable, since both sides are recomputed under the same dictionary.

    **``identity=True`` inverts that last guard into a move** (issue #152, the module
    docstring). Identity fields become nameable, and instead of refusing the recomputed
    key it *writes* it, carrying the row onto the corrected identity. The case it exists
    for is the one the episode model creates: a ``condition`` whose ``onset_on`` — now
    part of its key — was recorded wrong. Its own front-loaded gates, in order, nothing
    written on any raise:

        1. **drift** — :func:`dedup._assert_no_key_drift`. A row already stored under a
           stale key has no meaningful base to move *from*; `pemr rekey --apply` first.
        2. **no-move** — the corrected payload derives the same base, so ``--identity``
           is inert and this is an ordinary correction. Not an error: passing the flag
           defensively must not turn a no-op into a failure.
        3. **occupied target** — a row in the destination family already says the same
           thing (:func:`dedup._rows_equal`): :class:`IdentityCollisionError`, because
           that is a merge (see the module docstring). Otherwise the row takes
           ``max(dedup_occurrence) + 1`` of the target family, the monotonic rule
           :func:`dedup._plan_keep_both` uses, so it can never land on an occupied key.
        4. **anchored conflict** — :class:`AnchoredConflictError` when the row is the
           last member of a family an open conflict is staged against; a survivable
           re-anchoring is disclosed on the report instead.

    A move can only ever *split* a family — adding a discriminator to an identity never
    fuses two — so, gate 3 aside, it cannot manufacture a ``UNIQUE(dedup_key)`` violation.

    **Re-ingest divergence.** Once a unit is corrected, re-committing the *original*
    document stages a CONFLICT rather than deduping, because ``unit`` is one of
    ``dedup._COMPARE_FIELDS`` for ``lab_result`` and ``observation``. That is correct and
    loud: the stored row genuinely no longer says what its source says.

    Raises :class:`FieldNotEditableError`, :class:`IdentityCollisionError`,
    :class:`AnchoredConflictError`, :class:`RecordNotFoundError`,
    ``dedup.DictionaryDriftError``, ``dedup.ValidationError`` or plain ``ValueError`` —
    all friendly rc=1 at the CLI.
    """
    db.require_migrated(conn)
    _require_type(record_type)
    pk = f"{record_type}_id"
    row = conn.execute(
        f"SELECT * FROM {record_type} WHERE {pk} = ?", (row_id,)
    ).fetchone()
    if row is None:
        raise RecordNotFoundError(
            f"no {record_type} with id {row_id} - row ids come from `pemr rekey`'s "
            "collision report, or `pemr query`"
        )

    if not updates:
        raise ValueError(
            f"no fields to edit - pass at least one --set NAME=VALUE; editable "
            f"{record_type} fields: {', '.join(dedup.editable_fields(record_type))}; "
            "nothing was written"
        )
    editable = dedup.editable_fields(record_type)
    key_fields = dedup.KEY_FIELDS[record_type]
    # With `--identity` the key fields join the nameable set; everything outside
    # FIELD_SPECS stays unnameable in both modes, which is the provenance guarantee.
    nameable = (*editable, *sorted(key_fields)) if identity else editable
    for name in updates:
        if name in nameable:
            continue
        if name in key_fields:
            raise FieldNotEditableError(
                f"{record_type}.{name} is part of the row's identity (it feeds the "
                "dedup_key), so it cannot be corrected in place - that is a different "
                "operation: edit `data/dictionary.toml` and run `pemr rekey`, "
                f"`pemr record rm {record_type} {row_id}` and re-commit, or pass "
                "`--identity` to move the row onto the corrected identity. Editable "
                f"{record_type} fields: {', '.join(editable)}; nothing was written"
            )
        raise FieldNotEditableError(
            f"unknown {record_type} field '{name}' - editable fields: "
            f"{', '.join(editable)}; nothing was written"
        )

    cleaned_note = (note or "").strip()
    if not cleaned_note:
        raise ValueError(
            "an edit note is required - it records why the correction was made and who "
            "made it; a stored clinical value changed for no stated reason is not an "
            "audit trail; nothing was written"
        )

    before = {name: row[name] for name in dedup.FIELD_SPECS[record_type]}
    after = {**before, **updates}
    # Exactly the validation a commit would apply, so an edit can never leave a row in a
    # state `commit-extraction` would have refused.
    dedup.validate_row(record_type, {k: v for k, v in after.items() if v is not None})

    person_id = row["person_id"]
    before_base = dedup.dedup_key(record_type, before, person_id, dictionary)
    after_base = dedup.dedup_key(record_type, after, person_id, dictionary)
    moves = before_base != after_base
    if moves and not identity:
        # Reachable only when KEY_FIELDS has fallen out of step with dedup._key_parts:
        # a named field the table does not list as identity nonetheless moved the key.
        # (An intentional identity change comes through `identity=True` instead.)
        raise FieldNotEditableError(
            f"refusing to edit {record_type} {row_id}: the requested change would move "
            "its dedup_key, which is an identity change rather than a correction "
            "(this means dedup.KEY_FIELDS has fallen out of step with the key "
            "derivation - please report it); nothing was written"
        )

    changes = [
        {"field": name, "old": _ledger_text(before[name]),
         "new": _ledger_text(updates[name])}
        for name in nameable
        if name in updates and not _same_value(before[name], updates[name])
    ]
    unchanged = [
        name for name in nameable
        if name in updates and _same_value(before[name], updates[name])
    ]

    stamp = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = RecordEditReport(
        record_type=record_type,
        row_id=row_id,
        person_id=person_id,
        person_slug=_slug_for(conn, person_id),
        document_id=row["document_id"],
        label=dedup._rekey_label(record_type, row),
        changes=changes,
        unchanged=unchanged,
        dedup_key=row["dedup_key"],
        dedup_base=row["dedup_base"],
        dedup_occurrence=int(row["dedup_occurrence"] or 0),
        identity=moves,
        # Default to standing still: an ordinary correction reports the same three
        # columns on both sides, so a reader never has to special-case the common path.
        new_dedup_key=row["dedup_key"],
        new_dedup_base=row["dedup_base"],
        new_dedup_occurrence=int(row["dedup_occurrence"] or 0),
        note=cleaned_note,
        attributed_to=(attributed_to or "").strip() or None,
        edited_at=stamp,
    )

    key_assignments: list[str] = []
    key_values: list[object] = []
    if moves:
        _plan_identity_move(
            conn, record_type, row, before, after, after_base, dictionary, report
        )
        key_assignments = [f"{name} = ?" for name in dedup.INTERNAL_COLUMNS]
        key_values = [
            report.new_dedup_key, report.new_dedup_base, report.new_dedup_occurrence,
        ]

    if apply and changes:
        # One transaction for the UPDATE and its ledger rows: a correction that commits
        # without its audit trail is exactly the failure the trail exists to prevent
        # (the `curation.retire_row_verdicts` precedent). record_fts follows via the
        # per-table AFTER UPDATE triggers (migrations 003/006).
        # The row also gets the correction *mark* (migration 016, issue #134) in the same
        # statement: the payload change and its disclosure must land together or not at
        # all. These two columns are outside FIELD_SPECS, so they can never collide with a
        # named change. Last correction wins on the row; the ledger below keeps every one.
        # An identity move carries the three dedup_* columns in the *same* statement
        # (issue #152): a payload that landed under its old key, or a key with no
        # payload behind it, is precisely the drift `_assert_no_key_drift` exists to
        # catch. They are outside FIELD_SPECS too, so they cannot collide with a change.
        assignments = ", ".join(
            [f"{c['field']} = ?" for c in changes]
            + [f"{name} = ?" for name in dedup.EDIT_MARK_COLUMNS]
            + key_assignments
        )
        values = [
            *(updates[c["field"]] for c in changes), stamp, report.attributed_to,
            *key_values,
        ]
        with conn:
            cur = conn.execute(
                f"UPDATE {record_type} SET {assignments} WHERE {pk} = ?",
                [*values, row_id],
            )
            if cur.rowcount != 1:
                raise RecordNotFoundError(
                    f"no {record_type} with id {row_id} - nothing was edited"
                )
            for change in changes:
                conn.execute(
                    """
                    INSERT INTO record_edit
                      (record_type, record_id, dedup_base, field, old_value, new_value,
                       note, attributed_to, edited_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (record_type, row_id, row["dedup_base"], change["field"],
                     change["old"], change["new"], cleaned_note,
                     report.attributed_to, stamp),
                )
    report.applied = apply
    return report

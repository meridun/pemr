"""Recorded human verdicts on record families — `pemr record annotate` (issue #109).

Some record states are not resolvable by deterministic tooling over documents. Two
source documents contradict each other and the extraction is faithful to both. A row is
real in the source but known-erroneous (a requisition coding artifact). A diagnosis
*evolved*, so collapsing the two statements would erase real history. The engine's
durable outcomes are keep / drop / keep-both, and none of them says "a clinician looked
at this and ruled" — so the question reopens on every re-render and re-extraction.

This module is that missing durable state: one small overlay table, and the verbs that
write it.

    annotate_record  -- record (or overwrite) one family's verdict, dry-run by default
    list_curation    -- every verdict, newest first
    clear_curation   -- lift one verdict, dry-run by default
    load_verdicts    -- the whole table as a lookup, for the render/verify hot path

**Pure overlay.** No record row is ever written. `render` stays a pure function of DB
state: it just reads two tables per section instead of one, and re-rendering after a
verdict changes output because the database changed, which is the point.

**Keyed by ``(record_type, dedup_base)``**, not by row id and not by ``dedup_key``.
``dedup_base`` is the stable per-family identity across occurrence renumbering
(Architecture.md §3), so a verdict survives a re-ingest of the same document and the
occurrence shifts `record rm` (#107) leaves behind. It does *not* survive every
`pemr rekey`: a dictionary edit that changes this family's own canonical name moves its
``dedup_base``, orphaning the verdict (`pemr verify` warns; `record annotate --clear`
then re-annotate). One live verdict per family: re-annotating overwrites (last verdict
wins), which is what "a human ruled" means — there is no verdict history here by
design.

The module imports :mod:`db` and :mod:`dedup` only, keeping the import graph acyclic;
`render` and `verify` import *it*.

`record annotate` is **CLI-only** and deliberately absent from
``mcp_server.WRITE_TOOLS``, matching `document rm` / `record rm`: a human's clinical
verdict is exactly the thing the blessed-write-set boundary exists to keep an agent out
of.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from . import db, dedup

#: Every legal ``curation.status``. Also the CLI's ``choices`` — one source of truth.
STATUSES: tuple[str, ...] = (
    "confirmed",
    "superseded",
    "erroneous-in-source",
    "disputed",
    "merged-into",
)

#: The statuses that move a family **out** of its normal rendered section and into the
#: "Superseded / corrected" appendix. ``confirmed`` renders exactly as today (it records
#: agreement, it does not change the document); ``disputed`` renders in place, marked —
#: a disputed fact that vanished from the summary would be worse than an unmarked one.
APPENDIX_STATUSES: tuple[str, ...] = (
    "superseded",
    "erroneous-in-source",
    "merged-into",
)

#: The key a rendered row carries its verdict under, when it has one.
CURATION_FIELD = "_curation"


class CurationNotFoundError(ValueError):
    """Raised when a family carries no verdict (friendly rc=1 at the CLI)."""


class FamilyNotFoundError(ValueError):
    """Raised when a base-or-id token does not resolve to a live family."""


@dataclass
class CurationReport:
    """Structured result of :func:`annotate_record` / :func:`clear_curation`.

    Also the ``--json`` payload shape, so the key names here are a contract.
    """

    record_type: str
    dedup_base: str
    status: str = ""
    note: str = ""
    attributed_to: str | None = None
    created_at: str = ""
    merged_into_base: str | None = None
    label: str = ""                    # which fact is being ruled on (occurrence 0)
    family_size: int = 0
    previous: dict | None = None       # the verdict this one replaces, if any
    action: str = "create"             # "create" | "overwrite" | "clear"
    applied: bool = False

    def as_dict(self) -> dict:
        return {
            "record_type": self.record_type,
            "dedup_base": self.dedup_base,
            "status": self.status,
            "note": self.note,
            "attributed_to": self.attributed_to,
            "created_at": self.created_at,
            "merged_into_base": self.merged_into_base,
            "label": self.label,
            "family_size": self.family_size,
            "previous": self.previous,
            "action": self.action,
            "applied": self.applied,
        }


def _now(now: str | None = None) -> str:
    """ISO8601 UTC to the second — the stamp `document.ingested_at` carries."""
    return now or datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_type(record_type: str) -> None:
    """Guard every f-string table interpolation below (the `records.remove_record`
    idiom): a record type reaches SQL only after matching a known key."""
    if record_type not in dedup.FIELD_SPECS:
        raise ValueError(
            f"unknown record type '{record_type}' - known types: "
            f"{', '.join(dedup.KNOWN_TYPES)}"
        )


def _row_view(row: sqlite3.Row) -> dict:
    return {
        "record_type": row["record_type"],
        "dedup_base": row["dedup_base"],
        "status": row["status"],
        "note": row["note"],
        "merged_into_base": row["merged_into_base"],
        "attributed_to": row["attributed_to"],
        "created_at": row["created_at"],
    }


def has_table(conn: sqlite3.Connection) -> bool:
    """Whether `curation` exists — false for a database predating migration 008.

    The :func:`tombstones.has_table` guard: `render` and `verify` must keep working
    against an older snapshot rather than raising ``no such table``.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='curation'"
    ).fetchone()
    return row is not None


def get_verdict(
    conn: sqlite3.Connection, record_type: str, base: str
) -> dict | None:
    """The live verdict for one family, or None."""
    if not has_table(conn):
        return None
    row = conn.execute(
        "SELECT * FROM curation WHERE record_type = ? AND dedup_base = ?",
        (record_type, base),
    ).fetchone()
    return _row_view(row) if row is not None else None


def load_verdicts(conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    """Every verdict as ``{(record_type, dedup_base): verdict}`` — one query.

    The render/verify hot path: a document render touches every section, and a
    per-family lookup would be one SELECT per row. Returns ``{}`` when the table is
    absent (pre-008 snapshot), so callers need no second guard.
    """
    if not has_table(conn):
        return {}
    rows = conn.execute("SELECT * FROM curation").fetchall()
    return {(r["record_type"], r["dedup_base"]): _row_view(r) for r in rows}


def family_label(conn: sqlite3.Connection, record_type: str, base: str) -> tuple[str, int]:
    """``(label, family_size)`` for one family — the label off occurrence 0.

    The operator has to see *which fact* they are ruling on; a 64-character hex base
    is not that. An empty family reports ``("", 0)`` rather than raising: `list` and
    `verify` both need to describe an orphaned verdict.

    Re-validates ``record_type`` at the boundary even though every caller already has:
    the value can come off a *stored* `curation` row, and :func:`dedup.load_family`
    interpolates it into a table name.
    """
    _require_type(record_type)
    family = dedup.load_family(conn, record_type, base)
    if not family:
        return "", 0
    return dedup._rekey_label(record_type, family[0]), len(family)


def resolve_base(conn: sqlite3.Connection, record_type: str, token: str) -> str:
    """Resolve a ``<base-or-id>`` token to a ``dedup_base`` in ``record_type``.

    An all-digits token is a row id in ``<table>`` and resolves to that row's
    ``dedup_base``; anything else is treated as a ``dedup_base`` literal and is
    confirmed to name a live family. The disambiguation is safe rather than
    heuristic: a ``dedup_base`` is a sha256 hex digest (:func:`dedup._hash_parts`),
    so it always carries at least one non-digit character.

    Accepting the row id matters more than it looks — every other verb in the repo
    names rows by id (`pemr rekey`'s collision report, `pemr query`, `record rm`), and
    forcing the operator to look up a base by hand between reading the collision and
    ruling on it is where a wrong base gets pasted.

    Raises :class:`FamilyNotFoundError` when the token resolves to nothing.
    """
    _require_type(record_type)
    token = (token or "").strip()
    if not token:
        raise FamilyNotFoundError(
            f"no {record_type} target given - pass a dedup_base or a row id"
        )
    if token.isdigit():
        pk = f"{record_type}_id"
        row = conn.execute(
            f"SELECT dedup_base FROM {record_type} WHERE {pk} = ?", (int(token),)
        ).fetchone()
        if row is None:
            raise FamilyNotFoundError(
                f"no {record_type} with id {token} - row ids come from `pemr rekey`'s "
                "collision report, or `pemr query`"
            )
        return row["dedup_base"]
    if not dedup.load_family(conn, record_type, token):
        raise FamilyNotFoundError(
            f"no live {record_type} family with dedup_base {token[:12]}... - pass a "
            "row id instead, or see `pemr record annotate --list`"
        )
    return token


def annotate_record(
    conn: sqlite3.Connection,
    record_type: str,
    token: str,
    *,
    status: str,
    note: str,
    attributed_to: str | None = None,
    merged_into_base: str | None = None,
    now: str | None = None,
    apply: bool = False,
) -> CurationReport:
    """Record one family's verdict; dry-run by default (``apply=True`` writes).

    Validation is deliberately front-loaded, because the write is an upsert that
    silently replaces the previous verdict: an unknown status or a dangling
    ``merged_into_base`` must fail *before* a good verdict is overwritten by a bad one.
    Nothing is written on any raise.

    ``note`` is required and must be non-empty after ``strip()`` — the point of the
    table is the why and who said so, and a verdict with no reason is a verdict nobody
    can audit later.

    ``merged_into_base`` is set iff ``status='merged-into'``, must name a live family
    of the same ``record_type``, and must not be the annotated family itself (a family
    merged into itself would render nowhere at all).

    Raises ``ValueError`` for an unknown ``record_type``/``status``/empty note or a bad
    merge target, and :class:`FamilyNotFoundError` when the target does not resolve.
    """
    db.require_migrated(conn)
    _require_type(record_type)
    if status not in STATUSES:
        raise ValueError(
            f"unknown curation status '{status}' - known statuses: "
            f"{', '.join(STATUSES)}"
        )
    cleaned_note = (note or "").strip()
    if not cleaned_note:
        raise ValueError(
            "a curation note is required - it records why the verdict was made and "
            "who made it; nothing was written"
        )
    base = resolve_base(conn, record_type, token)

    merge_target = (merged_into_base or "").strip() or None
    if status == "merged-into":
        if merge_target is None:
            raise ValueError(
                "status 'merged-into' needs the family it merges into - pass "
                "--merged-into <BASE-OR-ID>; nothing was written"
            )
        merge_target = resolve_base(conn, record_type, merge_target)
        if merge_target == base:
            raise ValueError(
                f"cannot merge {record_type} {base[:12]}... into itself - the merged "
                "family renders only under its target, so this would hide it "
                "entirely; nothing was written"
            )
    elif merge_target is not None:
        raise ValueError(
            f"--merged-into is only meaningful with status 'merged-into', not "
            f"'{status}'; nothing was written"
        )

    previous = get_verdict(conn, record_type, base)
    label, family_size = family_label(conn, record_type, base)
    stamp = _now(now)
    report = CurationReport(
        record_type=record_type,
        dedup_base=base,
        status=status,
        note=cleaned_note,
        attributed_to=(attributed_to or "").strip() or None,
        created_at=stamp,
        merged_into_base=merge_target,
        label=label,
        family_size=family_size,
        previous=previous,
        action="overwrite" if previous is not None else "create",
    )

    if apply:
        # UPSERT, the `tombstones.add_tombstone` idiom: one live verdict per family,
        # last verdict wins, and a re-run of the same command is a safe no-op-shaped
        # write. created_at is refreshed - it stamps *this* verdict, not the first one.
        with conn:
            conn.execute(
                """
                INSERT INTO curation
                  (record_type, dedup_base, status, note, merged_into_base,
                   attributed_to, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_type, dedup_base) DO UPDATE SET
                  status           = excluded.status,
                  note             = excluded.note,
                  merged_into_base = excluded.merged_into_base,
                  attributed_to    = excluded.attributed_to,
                  created_at       = excluded.created_at
                """,
                (record_type, base, status, cleaned_note, merge_target,
                 report.attributed_to, stamp),
            )
    report.applied = apply
    return report


def list_curation(
    conn: sqlite3.Connection, record_type: str | None = None
) -> list[dict]:
    """Every verdict (optionally one record type), newest first.

    Each entry carries ``label`` and ``family_size``. ``family_size == 0`` is an
    **orphan**: the family it rules on is gone (a `record rm` of every occurrence, or a
    restore from a snapshot that never had it). Surfaced rather than hidden, the
    ``tombstones.list_tombstones`` ``live_document_id`` precedent — `pemr verify` warns
    about the same state.
    """
    db.require_migrated(conn)
    if record_type is not None:
        _require_type(record_type)
    if not has_table(conn):
        return []
    sql = "SELECT * FROM curation"
    params: tuple = ()
    if record_type is not None:
        sql += " WHERE record_type = ?"
        params = (record_type,)
    sql += " ORDER BY created_at DESC, record_type, dedup_base"
    out: list[dict] = []
    for row in conn.execute(sql, params).fetchall():
        view = _row_view(row)
        if view["record_type"] in dedup.FIELD_SPECS:
            label, size = family_label(conn, view["record_type"], view["dedup_base"])
        else:
            # A hand-edited row can name anything; never build a query from it.
            label, size = "", 0
        view["label"] = label
        view["family_size"] = size
        out.append(view)
    return out


def clear_curation(
    conn: sqlite3.Connection,
    record_type: str,
    token: str,
    *,
    apply: bool = False,
) -> CurationReport:
    """Lift one family's verdict; dry-run by default.

    The target is resolved leniently on purpose: a row id still needs a live row, but a
    ``dedup_base`` literal is accepted even when the family is gone, because clearing an
    **orphaned** verdict (the one `pemr verify` warns about) is exactly a case where no
    live row exists to name it by.

    Raises :class:`CurationNotFoundError` when there is no verdict to lift — a typo
    must not read as success.
    """
    db.require_migrated(conn)
    _require_type(record_type)
    token = (token or "").strip()
    if token.isdigit():
        base = resolve_base(conn, record_type, token)
    else:
        base = token
    existing = get_verdict(conn, record_type, base)
    if existing is None:
        raise CurationNotFoundError(
            f"no curation verdict for {record_type} {base[:12]}... - see "
            "`pemr record annotate --list`"
        )
    label, family_size = family_label(conn, record_type, base)
    report = CurationReport(
        record_type=record_type,
        dedup_base=base,
        status=existing["status"],
        note=existing["note"],
        attributed_to=existing["attributed_to"],
        created_at=existing["created_at"],
        merged_into_base=existing["merged_into_base"],
        label=label,
        family_size=family_size,
        previous=existing,
        action="clear",
    )
    if apply:
        with conn:
            conn.execute(
                "DELETE FROM curation WHERE record_type = ? AND dedup_base = ?",
                (record_type, base),
            )
    report.applied = apply
    return report


def describe(verdict: dict) -> str:
    """One-line ``<status> - <note>`` summary shared by the CLI and render printers."""
    status = verdict.get("status") or ""
    if status == "merged-into" and verdict.get("merged_into_base"):
        status = f"merged into {str(verdict['merged_into_base'])[:12]}..."
    note = verdict.get("note") or ""
    who = verdict.get("attributed_to")
    attribution = f" ({who})" if who else ""
    return f"{status} - {note}{attribution}"

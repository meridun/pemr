"""Recorded human verdicts on records — `pemr record annotate` (issues #109, #114).

Some record states are not resolvable by deterministic tooling over documents. Two
source documents contradict each other and the extraction is faithful to both. A row is
real in the source but known-erroneous (a requisition coding artifact). A diagnosis
*evolved*, so collapsing the two statements would erase real history. The engine's
durable outcomes are keep / drop / keep-both, and none of them says "a clinician looked
at this and ruled" — so the question reopens on every re-render and re-extraction.

This module is that missing durable state: one small overlay table, and the verbs that
write it.

    annotate_record      -- record (or overwrite) one verdict, dry-run by default
    list_curation        -- every verdict, newest first
    clear_curation       -- lift one verdict, dry-run by default
    load_verdicts        -- the whole table as a lookup, for the render/verify hot path
    annotate_rows        -- stamp a section's rows with their verdicts (read-time overlay)
    annotate_events      -- ... the same for timeline events
    is_appendix          -- does a stamped carrier leave the live view?
    row_verdicts_for     -- the row-scoped verdicts naming a set of rows
    retire_row_verdicts  -- ... and drop them, for the row-removal write paths
    orphan_kinds         -- why (if at all) one verdict points at nothing live
    list_orphans         -- ... over the whole table, for `verify` and `record reaffirm`

**Pure overlay.** No record row is ever written. `render` stays a pure function of DB
state: it just reads two tables per section instead of one, and re-rendering after a
verdict changes output because the database changed, which is the point.

**Two scopes** (issue #114). A verdict names either a whole dedup **family** or a
single **row**:

*Family scope* — keyed by ``(record_type, dedup_base)``, stored with ``record_id = 0``.
``dedup_base`` is the stable per-family identity across occurrence renumbering
(Architecture.md §3), so the verdict survives a re-ingest of the same document and the
occurrence shifts `record rm` (#107) leaves behind. It does *not* survive every
`pemr rekey`: a dictionary edit that changes this family's own canonical name moves its
``dedup_base``, orphaning the verdict (`pemr verify` warns; `record annotate --clear`
then re-annotate). The one exception is the verdict that *resolved* a rekey collision
(issue #116, :class:`CollisionResolver`): rather than being left orphaned, it is
**narrowed to row scope** — pinned to exactly the rows it covered when it was made — in
the same transaction as the keys it authorized. It is narrowed rather than re-pointed
because the surviving family is *larger* than the one the human ruled on: re-pointing
would silently extend the ruling over a row nobody judged, and a ``superseded`` verdict
reaching a previously-live row drops that row out of its clinical section. A ruling's
judgment is never destroyed or reinterpreted; re-pointing and scope-narrowing that
preserve its original extension are what this module permits.

*Row scope* — ``--row``, keyed by ``(record_type, record_id)`` **alone**. It exists for
the family a ``--keep both`` conflict resolution left holding two *live* rows: a
family-scoped ``superseded`` there hides the winner along with the loser. A row-scoped
verdict affects only its own occurrence; siblings render as if unannotated unless they
carry their own verdict. `rekey` rewrites ``dedup_key``/``dedup_base`` but never
renumbers a row id (:func:`dedup.rekey`: "Values, provenance and row ids are
untouched"), so a row-scoped verdict genuinely **survives** the dictionary-driven rekey
that orphans a family-scoped one; it is orphaned only by the removal of its row. The
``dedup_base`` stored beside it is a *breadcrumb* — which family it ruled in, at
annotate time — that may go stale after such a rekey and is never consulted for
resolution: every reader re-reads the live base off the row. A stale breadcrumb is not
an error, but it *is* reported: `pemr verify` notices that a rekey moved the row out of
the family the ruling was made in, so the human can re-affirm (re-annotating collapses
the breadcrumb) or re-rule. That notice is what makes a collision-resolving narrowing
loud rather than silent.

**Removing the row retires the verdict.** A ``dedup_base`` is content-derived, so a
family verdict re-attaching to a re-ingested identical fact is correct. A row id is not:
every record table is ``INTEGER PRIMARY KEY`` without ``AUTOINCREMENT``, so deleting the
highest-id row frees that id for the **next insert** — a surviving row-scoped verdict
would silently re-target an unrelated new record, filing a live clinical fact under a
note about a different one, and `pemr verify`'s orphan warning goes quiet the moment the
id is reused. So the two write paths that can delete a record row —
:func:`records.remove_record` and :func:`documents.remove_document` — disclose any
row-scoped verdict naming a doomed row in their dry run and lift it, in the same
transaction as the delete (:func:`row_verdicts_for`, :func:`retire_row_verdicts`). The
window is closed where it opens; `verify`'s warning stays as the backstop for a verdict
orphaned some other way.

**Precedence**: a row-scoped verdict wins over its family's verdict, for that row only —
resolved in exactly one place, :meth:`VerdictMap.for_row`, so render, journal and verify
cannot drift apart.

Row scope is **opt-in** and never inferred from the token's shape: :func:`resolve_base`
has accepted a bare row id as "the family containing this row" since #109, and flipping
that would silently change the meaning of commands already recorded in a curation ledger.

One live verdict per target: re-annotating overwrites (last verdict wins), which is what
"a human ruled" means — there is no verdict history here by design.

The module imports :mod:`db` and :mod:`dedup` only, keeping the import graph acyclic;
`render` and `verify` import *it*.

`record annotate` is **CLI-only** and deliberately absent from
``mcp_server.WRITE_TOOLS``, matching `document rm` / `record rm`: a human's clinical
verdict is exactly the thing the blessed-write-set boundary exists to keep an agent out
of.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import db, dedup

#: Every legal ``curation.status``. Also the CLI's ``choices`` — one source of truth.
STATUSES: tuple[str, ...] = (
    "confirmed",
    "superseded",
    "erroneous-in-source",
    "disputed",
    "merged-into",
    "distinct",
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

#: The statuses that say "a human settled *which fact this is*" — and therefore the only
#: ones that may resolve a `pemr rekey` collision (issues #116, #122). They answer that
#: question two opposite ways: ``merged-into``/``superseded`` say **one fact** (the pair
#: is a single fact filed twice), ``distinct`` says **two facts** (issue #122 — the labels
#: are generic extraction placeholders that recompute onto one key, and there is no
#: dictionary entry to narrow because they are synonyms of nothing). Both settle the
#: identity question, so both let `rekey` proceed; which of the two was recorded is
#: reported as the resolution's ``settlement`` (:meth:`CollisionResolver.settlement`).
#:
#: ``confirmed`` records agreement with the row as filed, ``disputed`` records that the
#: question is still open, and ``erroneous-in-source`` rules on the *content* of one row
#: without saying anything about its identity relative to another — none of the three
#: settles the pair either way, so none of them resolves a collision.
#:
#: Every resolving verdict is **unary**: it names one row or one family and no
#: counterpart, so it settles *any* collision that target takes part in, including a
#: future one that would otherwise have blocked. That generality is deliberate and
#: unchanged since #116 — every resolution is reported with both row ids, the status and
#: the scope, so the reach of a ruling is never silent.
RESOLVING_STATUSES: tuple[str, ...] = (
    "merged-into",
    "superseded",
    "distinct",
)

#: The resolving statuses that say **two facts**, not one (issue #122) — the sub-vocabulary
#: :meth:`CollisionResolver.settlement` reports as ``"distinct"``.
#:
#: Deliberately **disjoint from** :data:`APPENDIX_STATUSES`, and that disjointness *is* the
#: guarantee both rows keep rendering as live, independent facts: a distinct-resolved pair
#: lands in one family at two occurrences — the ``--keep both`` shape — and nothing about
#: rendering changes. If a status ever appeared in both tuples, a live row would silently
#: leave its clinical section, the failure issue #114 was raised for.
DISTINCT_STATUSES: tuple[str, ...] = (
    "distinct",
)

#: The key a rendered row carries its verdict under, when it has one.
CURATION_FIELD = "_curation"

#: ``curation.record_id`` for a family-scoped verdict (migration 010). A sentinel rather
#: than NULL: SQLite treats NULLs as distinct in a unique index, so a nullable scope
#: column would silently permit duplicate family verdicts on one base.
FAMILY_SCOPE = 0

#: The scope vocabulary — `--list` / `--json` / the report block all speak it, and like
#: :data:`STATUSES` it has exactly one source of truth.
SCOPE_FAMILY = "family"
SCOPE_ROW = "row"

#: The orphan vocabulary (issue #126) — the *one* source of truth for "this verdict
#: points at nothing live", spoken by :func:`orphan_kinds`, :func:`list_orphans`,
#: `pemr verify`'s warnings, `pemr rekey --apply`'s orphan block and
#: `pemr record reaffirm`'s ``--json``.
#:
#: ``ORPHAN_NO_FAMILY`` — a **family-scoped** verdict whose ``dedup_base`` names no live
#: family. Its ruling is inert: nothing renders the fact it judged.
#:
#: ``ORPHAN_DANGLING_MERGE`` — a verdict in **either** scope whose ``merged_into_base``
#: names no live family. The merge target is always a family (``--merged-into`` takes a
#: ``dedup_base``), so this one is scope-independent.
#:
#: Deliberately **not** an orphan kind, and this exclusion is load-bearing: the
#: *row-scoped stale breadcrumb* — a row verdict whose stored ``dedup_base`` a rekey left
#: behind. That verdict still resolves, by ``record_id``; the stale base is never consulted
#: (see the module docstring). `verify` keeps reporting it as its own notice, and nothing
#: here may re-annotate it.
ORPHAN_NO_FAMILY = "no-live-family"
ORPHAN_DANGLING_MERGE = "dangling-merge-target"

#: Every orphan kind, in the order :func:`orphan_kinds` reports them.
ORPHAN_KINDS: tuple[str, ...] = (ORPHAN_NO_FAMILY, ORPHAN_DANGLING_MERGE)


class CurationNotFoundError(ValueError):
    """Raised when the named target carries no verdict (friendly rc=1 at the CLI)."""


class FamilyNotFoundError(ValueError):
    """Raised when a base-or-id token does not resolve to a live family."""


class RowNotFoundError(ValueError):
    """Raised when a ``--row`` token does not name a live row.

    Distinct from :class:`FamilyNotFoundError` so the CLI can say "drop ``--row`` to
    annotate the whole family" — the two failures have different fixes.
    """


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
    label: str = ""                    # which fact is being ruled on
    family_size: int = 0
    previous: dict | None = None       # the verdict this one replaces, if any
    action: str = "create"             # "create" | "overwrite" | "clear"
    applied: bool = False
    record_id: int = FAMILY_SCOPE      # 0 = family scope; else the annotated row
    scope: str = SCOPE_FAMILY          # SCOPE_FAMILY | SCOPE_ROW (derived from record_id)

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
            # Appended, never inserted: the --json key set is a contract, and #114 may
            # only widen it (Architecture.md's additive-only rule).
            "record_id": self.record_id,
            "scope": self.scope,
        }


@dataclass
class VerdictMap:
    """Every stored verdict, partitioned by scope — the render/verify lookup.

    Replaces #109's bare ``{(record_type, dedup_base): verdict}`` dict, which had no way
    to express "this row but not its sibling". Still **one query**: the partition happens
    in Python, because a per-row SELECT is exactly what this map exists to avoid.

    Falsy when empty, so render's no-verdicts fast path (and its
    ``with_identity=bool(...)`` journal call) keeps working unchanged.
    """

    family: dict[tuple[str, str], dict] = field(default_factory=dict)
    rows: dict[tuple[str, int], dict] = field(default_factory=dict)

    def for_row(
        self, record_type: str, base: str | None, record_id: int | None
    ) -> dict | None:
        """The verdict that applies to one row: **row scope wins over family scope**.

        The single precedence site (issue #114). Render, journal and verify all resolve
        through here, so the rule cannot drift between them. ``record_id`` may be None
        (a carrier without row identity — an event from a ``with_identity=False``
        timeline), in which case only the family verdict can apply.
        """
        if record_id:
            hit = self.rows.get((record_type, int(record_id)))
            if hit is not None:
                return hit
        if base is None:
            return None
        return self.family.get((record_type, base))

    def all(self) -> list[dict]:
        """Every verdict, both scopes — for `verify` and `--list`."""
        return [*self.family.values(), *self.rows.values()]

    def __bool__(self) -> bool:
        return bool(self.family or self.rows)

    def __len__(self) -> int:
        return len(self.family) + len(self.rows)


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
    # `record_id` is read defensively rather than by subscript: `render`/`verify` must
    # keep working against a pre-010 snapshot (the `has_table` precedent), where the
    # column does not exist and every verdict is family-scoped by definition.
    record_id = int(row["record_id"]) if "record_id" in row.keys() else FAMILY_SCOPE
    return {
        "record_type": row["record_type"],
        "dedup_base": row["dedup_base"],
        "record_id": record_id,
        "scope": SCOPE_ROW if record_id else SCOPE_FAMILY,
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
    conn: sqlite3.Connection,
    record_type: str,
    base: str,
    *,
    record_id: int = FAMILY_SCOPE,
) -> dict | None:
    """The live verdict for one family (default) or one row, or None.

    With ``record_id > 0`` the ``base`` argument is deliberately **ignored**: a
    row-scoped verdict resolves by row id alone, and its stored base is a breadcrumb
    that a `rekey` may have left stale.
    """
    if not has_table(conn):
        return None
    if record_id:
        row = conn.execute(
            "SELECT * FROM curation WHERE record_type = ? AND record_id = ?",
            (record_type, int(record_id)),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM curation WHERE record_type = ? AND dedup_base = ? "
            "AND record_id = 0",
            (record_type, base),
        ).fetchone()
    return _row_view(row) if row is not None else None


def load_verdicts(conn: sqlite3.Connection) -> VerdictMap:
    """Every verdict as a :class:`VerdictMap` — one query, both scopes.

    The render/verify hot path: a document render touches every section, and a
    per-row lookup would be one SELECT per row. Returns an empty (falsy) map when the
    table is absent (pre-008 snapshot), so callers need no second guard.
    """
    out = VerdictMap()
    if not has_table(conn):
        return out
    for r in conn.execute("SELECT * FROM curation").fetchall():
        view = _row_view(r)
        if view["record_id"]:
            out.rows[(view["record_type"], view["record_id"])] = view
        else:
            out.family[(view["record_type"], view["dedup_base"])] = view
    return out


# --------------------------------------------------------------------------- #
# Read-time overlay — the stamping half, shared by every front door (issue #131)
# --------------------------------------------------------------------------- #
#
# The *stamping* rule (which verdict applies to which carrier) lives here, beside
# :meth:`VerdictMap.for_row` and :data:`APPENDIX_STATUSES`; the *policy* rule (what to do
# with a carrier bound for the appendix) stays with each front door, because they
# legitimately differ: `render` collects the carrier into its "Superseded / corrected"
# section, `pemr query` hides it behind a count, and the MCP payload hides nothing at all.
#
# Before #131 the stamping half lived only inside `render`, and `pemr query` had no
# overlay at all — the two verbs disagreed about which medications a person is on.


def annotate_rows(
    rows: list[dict],
    record_type: str,
    verdicts: VerdictMap,
    *,
    on_verdict: Callable[[dict], None] | None = None,
) -> list[dict]:
    """Stamp each row of one section with the verdict that applies to it.

    Rows are mutated **in place** and the same list is returned, so a caller may either
    take the return value or ignore it. A row whose verdict resolves carries it under
    :data:`CURATION_FIELD`; a row with no verdict is left exactly as it was — no key is
    added — which is what keeps unannotated output byte-identical (Architecture.md's
    additive-only rule). An empty ``verdicts`` short-circuits the whole pass.

    Resolution is **per row**, not per family (issue #114), and runs solely through
    :meth:`VerdictMap.for_row` so the row-over-family precedence rule is never restated.
    That needs the row's own identity, so ``rows`` must come from a ``SELECT *``: a
    projected row missing ``dedup_base``/``<record_type>_id`` silently degrades to family
    scope, or to no verdict at all.

    ``on_verdict`` fires once per stamped row (`render` passes ``_CurationPass.record``,
    which files the verdict into the appendix / open-questions sections). It is called
    for **every** resolved verdict, including one bound for the appendix — the caller
    decides what leaves the live view, via :func:`is_appendix`.
    """
    if not verdicts:
        return rows
    pk = f"{record_type}_id"
    for row in rows:
        verdict = verdicts.for_row(record_type, row.get("dedup_base"), row.get(pk))
        if verdict is None:
            continue
        if on_verdict is not None:
            on_verdict(verdict)
        row[CURATION_FIELD] = verdict
    return rows


def annotate_events(
    events: list[dict],
    verdicts: VerdictMap,
    *,
    on_verdict: Callable[[dict], None] | None = None,
) -> list[dict]:
    """:func:`annotate_rows` for timeline events.

    Same rule, different carrier: an event is a rendered sentence rather than a row, and
    it carries ``record_type``/``dedup_base``/``record_id`` only when
    :func:`query.query_timeline` was asked for them (``with_identity=True``). An event
    without identity passes through untouched — that is also the no-verdicts fast path,
    where no caller asks for the extra keys in the first place.
    """
    if not verdicts:
        return events
    for event in events:
        base = event.get("dedup_base")
        if base is None:
            continue
        verdict = verdicts.for_row(
            event["record_type"], base, event.get("record_id")
        )
        if verdict is None:
            continue
        if on_verdict is not None:
            on_verdict(verdict)
        event[CURATION_FIELD] = verdict
    return events


def verdict_of(carrier: dict) -> dict | None:
    """The verdict :func:`annotate_rows`/:func:`annotate_events` stamped, or None."""
    return carrier.get(CURATION_FIELD) if isinstance(carrier, dict) else None


def is_appendix(carrier: dict) -> bool:
    """Whether a stamped carrier leaves the live view — the one predicate every front
    door asks.

    True for exactly the :data:`APPENDIX_STATUSES`. ``disputed`` is deliberately false:
    it renders in place, marked, because a disputed fact that vanished from a clinical
    list would be worse than an unmarked one.
    """
    verdict = verdict_of(carrier)
    return verdict is not None and verdict.get("status") in APPENDIX_STATUSES


def row_verdicts_for(
    conn: sqlite3.Connection, record_type: str, record_ids: Iterable[int]
) -> list[dict]:
    """The **row-scoped** verdicts naming any of ``record_ids``, newest first.

    What a row-removal write path has to disclose before it deletes: those rows' ids are
    about to become free for reuse, so the verdicts naming them are about to become
    hazardous (see the module docstring). Family-scoped verdicts are deliberately not
    returned — ``dedup_base`` is content-derived and survives the removal legitimately.

    One query, intersected in Python: `curation` holds one row per human ruling, so it is
    always the small side, while ``record_ids`` can be every row of a large document.
    """
    _require_type(record_type)
    wanted = {int(rid) for rid in record_ids if int(rid)}
    if not wanted or not has_table(conn):
        return []
    rows = conn.execute(
        "SELECT * FROM curation WHERE record_type = ? AND record_id <> 0 "
        "ORDER BY created_at DESC, record_id",
        (record_type,),
    ).fetchall()
    return [v for v in (_row_view(r) for r in rows) if v["record_id"] in wanted]


def retire_row_verdicts(
    conn: sqlite3.Connection, record_type: str, record_ids: Iterable[int]
) -> list[dict]:
    """Lift the row-scoped verdicts naming ``record_ids``; return what was lifted.

    **Caller-managed transaction** (the :func:`tombstones.add_tombstone`
    ``conn_managed`` precedent): the caller is deleting the rows themselves, and a
    delete that commits without retiring the verdict is the exact failure this closes,
    so both must land in one ``with conn:``.

    Deletes by ``(record_type, record_id)`` — never by ``dedup_base``, which is a
    breadcrumb a `rekey` may have left stale — and touches no family-scoped verdict.
    """
    doomed = row_verdicts_for(conn, record_type, record_ids)
    for verdict in doomed:
        conn.execute(
            "DELETE FROM curation WHERE record_type = ? AND record_id = ?",
            (record_type, verdict["record_id"]),
        )
    return doomed


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


def row_label(
    conn: sqlite3.Connection, record_type: str, record_id: int
) -> tuple[str, str, int]:
    """``(label, live_base, family_size)`` for one row — :func:`family_label`'s row twin.

    The label comes off the annotated row itself, not off occurrence 0: the whole point
    of row scope is that the siblings are different facts. ``live_base`` is re-read here
    rather than taken from the verdict, because a `rekey` may have moved the family since
    the verdict was recorded and the stored base is only a breadcrumb.

    A removed row reports ``("", "", 0)`` rather than raising — the orphan case `list`
    and `verify` both have to describe.
    """
    _require_type(record_type)
    pk = f"{record_type}_id"
    row = conn.execute(
        f"SELECT * FROM {record_type} WHERE {pk} = ?", (int(record_id),)
    ).fetchone()
    if row is None:
        return "", "", 0
    base = row["dedup_base"]
    return (
        dedup._rekey_label(record_type, row),
        base,
        len(dedup.load_family(conn, record_type, base)),
    )


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


def resolve_row(
    conn: sqlite3.Connection, record_type: str, token: str
) -> tuple[int, str]:
    """Resolve a ``--row`` token to ``(record_id, live dedup_base)``.

    Only a row id will do here: a ``dedup_base`` literal names a family, and silently
    row-scoping it would rule on whichever occurrence happened to be first. The base is
    returned alongside because the write stores it as a breadcrumb (which family this
    verdict ruled in), never as a resolution key.

    Raises :class:`RowNotFoundError` when the id names no live row, ``ValueError`` when
    the token is not a row id at all.
    """
    _require_type(record_type)
    token = (token or "").strip()
    if not token:
        raise RowNotFoundError(
            f"no {record_type} target given - pass a row id with --row"
        )
    if not token.isdigit():
        raise ValueError(
            f"--row needs a {record_type} row id, not a dedup_base "
            f"({token[:12]}...) - a base names a whole family; drop --row to annotate it"
        )
    pk = f"{record_type}_id"
    row = conn.execute(
        f"SELECT dedup_base FROM {record_type} WHERE {pk} = ?", (int(token),)
    ).fetchone()
    if row is None:
        raise RowNotFoundError(
            f"no {record_type} with id {token} - row ids come from `pemr rekey`'s "
            "collision report, or `pemr query`"
        )
    return int(token), row["dedup_base"]


def annotate_record(
    conn: sqlite3.Connection,
    record_type: str,
    token: str,
    *,
    status: str,
    note: str,
    attributed_to: str | None = None,
    merged_into_base: str | None = None,
    row: bool = False,
    now: str | None = None,
    apply: bool = False,
) -> CurationReport:
    """Record one verdict; dry-run by default (``apply=True`` writes).

    ``row=True`` scopes the verdict to the single row ``token`` names, instead of to its
    whole dedup family (issue #114). Opt-in on purpose: without it the behaviour — and
    the stored row — is bit-identical to #109's.

    Validation is deliberately front-loaded, because the write is an upsert that
    silently replaces the previous verdict: an unknown status or a dangling
    ``merged_into_base`` must fail *before* a good verdict is overwritten by a bad one.
    Nothing is written on any raise.

    ``note`` is required and must be non-empty after ``strip()`` — the point of the
    table is the why and who said so, and a verdict with no reason is a verdict nobody
    can audit later.

    ``merged_into_base`` is set iff ``status='merged-into'``, and must name a live family
    of the same ``record_type``. At **family** scope it must not be the annotated family
    itself: a family merged into itself would render nowhere at all. At **row** scope it
    may be — "this occurrence is absorbed into the family it sits in" is a coherent
    ruling (the row moves to the appendix, its siblings keep rendering live), and it is
    exactly the state :meth:`CollisionResolver.narrow` produces when it pins a
    collision-resolving ``merged-into`` family verdict to its rows (#116): the same rekey
    that narrows the verdict also moves those rows into the merge target. Refusing it
    here would make `pemr verify`'s "re-affirm it" notice impossible to follow for the
    one status that most often carries it.

    Raises ``ValueError`` for an unknown ``record_type``/``status``/empty note or a bad
    merge target, :class:`FamilyNotFoundError` when a family target does not resolve, and
    :class:`RowNotFoundError` when a ``--row`` target does not.
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
    if row:
        record_id, base = resolve_row(conn, record_type, token)
    else:
        record_id, base = FAMILY_SCOPE, resolve_base(conn, record_type, token)

    merge_target = (merged_into_base or "").strip() or None
    if status == "merged-into":
        if merge_target is None:
            raise ValueError(
                "status 'merged-into' needs the family it merges into - pass "
                "--merged-into <BASE-OR-ID>; nothing was written"
            )
        merge_target = resolve_base(conn, record_type, merge_target)
        if merge_target == base and not record_id:
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

    previous = get_verdict(conn, record_type, base, record_id=record_id)
    if record_id:
        label, _live_base, family_size = row_label(conn, record_type, record_id)
    else:
        label, family_size = family_label(conn, record_type, base)
    stamp = _now(now)
    report = CurationReport(
        record_type=record_type,
        dedup_base=base,
        record_id=record_id,
        scope=SCOPE_ROW if record_id else SCOPE_FAMILY,
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
        # DELETE-by-scope then INSERT, where #109 upserted: since 010 there are two
        # disjoint uniqueness rules - the PK for family scope, the partial index for row
        # scope - and one ON CONFLICT target cannot name both. (A row-scoped
        # re-annotate after a rekey can collide with both at once: same row id, new
        # base.) The observable semantics are unchanged - one live verdict per target,
        # last verdict wins - and the delete additionally collapses a stale-base copy.
        # created_at is refreshed: it stamps *this* verdict, not the first one.
        with conn:
            if record_id:
                conn.execute(
                    "DELETE FROM curation WHERE record_type = ? AND record_id = ?",
                    (record_type, record_id),
                )
            else:
                conn.execute(
                    "DELETE FROM curation WHERE record_type = ? AND dedup_base = ? "
                    "AND record_id = 0",
                    (record_type, base),
                )
            conn.execute(
                """
                INSERT INTO curation
                  (record_type, dedup_base, record_id, status, note, merged_into_base,
                   attributed_to, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (record_type, base, record_id, status, cleaned_note, merge_target,
                 report.attributed_to, stamp),
            )
    report.applied = apply
    return report


def list_curation(
    conn: sqlite3.Connection, record_type: str | None = None
) -> list[dict]:
    """Every verdict (optionally one record type), newest first.

    Each entry carries ``scope``, ``record_id``, ``label`` and ``family_size``.
    ``family_size == 0`` is an **orphan**: the target it rules on is gone — the whole
    family for a family-scoped verdict, the single row for a row-scoped one. Surfaced
    rather than hidden, the ``tombstones.list_tombstones`` ``live_document_id``
    precedent — `pemr verify` warns about the same state.

    A row-scoped entry is labelled from its **row**, whose live ``dedup_base`` is re-read
    here: after a `rekey` the stored base is stale, and resolving on it would make a
    perfectly live row verdict look orphaned.
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
    sql += " ORDER BY created_at DESC, record_type, dedup_base, record_id"
    out: list[dict] = []
    for row in conn.execute(sql, params).fetchall():
        view = _row_view(row)
        if view["record_type"] not in dedup.FIELD_SPECS:
            # A hand-edited row can name anything; never build a query from it.
            label, size = "", 0
        elif view["record_id"]:
            label, _live_base, size = row_label(
                conn, view["record_type"], view["record_id"]
            )
        else:
            label, size = family_label(conn, view["record_type"], view["dedup_base"])
        view["label"] = label
        view["family_size"] = size
        out.append(view)
    return out


# --------------------------------------------------------------------------- #
# orphaned verdicts — one detector, three readers (issue #126)
# --------------------------------------------------------------------------- #

@dataclass
class OrphanVerdict:
    """One stored verdict that points at nothing live, plus why.

    Also the ``--json`` payload shape for `pemr record reaffirm` and the per-entry shape of
    `pemr rekey --apply`'s ``orphans`` key, so — like :class:`CurationReport` — these key
    names are a contract that may only widen.

    ``label``/``family_size`` describe *what is left*, which for an orphan is usually
    nothing: a ``no-live-family`` entry reports ``("", 0)``, while a
    ``dangling-merge-target`` one still names the live family the verdict itself rules on.
    """

    record_type: str
    dedup_base: str
    record_id: int
    scope: str
    status: str
    note: str
    attributed_to: str | None
    merged_into_base: str | None
    created_at: str
    kinds: list[str] = field(default_factory=list)
    label: str = ""
    family_size: int = 0

    def as_dict(self) -> dict:
        return {
            "record_type": self.record_type,
            "dedup_base": self.dedup_base,
            "record_id": self.record_id,
            "scope": self.scope,
            "status": self.status,
            "note": self.note,
            "attributed_to": self.attributed_to,
            "merged_into_base": self.merged_into_base,
            "created_at": self.created_at,
            "kinds": list(self.kinds),
            "label": self.label,
            "family_size": self.family_size,
        }


def orphan_kinds(conn: sqlite3.Connection, verdict: dict) -> list[str]:
    """Why ``verdict`` points at nothing live — ``[]`` when it is healthy.

    The single detector behind `pemr verify`'s two orphan warnings, `pemr record
    reaffirm`'s listing and `pemr rekey --apply`'s orphan block (issue #126): all three
    used to derive this state their own way, and a bulk remedy that disagreed with the
    warning it answers would re-annotate the wrong rows.

    ``verdict`` is a ``_row_view``-shaped dict, so both :meth:`VerdictMap.all` entries and
    :func:`list_curation` entries are valid input. A verdict whose ``record_type`` is not
    a known table reports ``[]``: the value can come off a hand-edited row and
    :func:`dedup.load_family` interpolates it into a table name, so it must never reach
    SQL — `verify` keeps its own separate warning for that case.

    Kinds are returned in :data:`ORPHAN_KINDS` order, and a verdict can carry both.
    """
    record_type = verdict["record_type"]
    if record_type not in dedup.FIELD_SPECS:
        return []
    kinds: list[str] = []
    # Family scope only: a row-scoped verdict resolves by `record_id`, and its stored base
    # is a breadcrumb a rekey may have left stale (the module docstring's scope rule).
    if not verdict["record_id"] and not dedup.load_family(
        conn, record_type, verdict["dedup_base"]
    ):
        kinds.append(ORPHAN_NO_FAMILY)
    target = verdict.get("merged_into_base")
    if target and not dedup.load_family(conn, record_type, target):
        kinds.append(ORPHAN_DANGLING_MERGE)
    return kinds


def list_orphans(
    conn: sqlite3.Connection, record_type: str | None = None
) -> list[OrphanVerdict]:
    """Every orphaned verdict (optionally one record type), newest first.

    **One entry per verdict**, carrying every kind that flagged it: a verdict in both
    classes must be re-annotated once, not twice.

    Returns ``[]`` when `curation` is absent (pre-008 snapshot) — the
    :func:`list_curation` guard, so callers need no second check. Deliberately narrower
    than :func:`list_curation`'s ``family_size == 0`` signal, which also flags the
    row-scoped orphan whose remedy is ``--clear --row``, not a re-point.
    """
    db.require_migrated(conn)
    if record_type is not None:
        _require_type(record_type)
    if not has_table(conn):
        return []
    out: list[OrphanVerdict] = []
    for verdict in list_curation(conn, record_type):
        kinds = orphan_kinds(conn, verdict)
        if not kinds:
            continue
        out.append(OrphanVerdict(
            record_type=verdict["record_type"],
            dedup_base=verdict["dedup_base"],
            record_id=verdict["record_id"],
            scope=verdict["scope"],
            status=verdict["status"],
            note=verdict["note"],
            attributed_to=verdict["attributed_to"],
            merged_into_base=verdict["merged_into_base"],
            created_at=verdict["created_at"],
            kinds=kinds,
            label=verdict["label"],
            family_size=verdict["family_size"],
        ))
    return out


def clear_curation(
    conn: sqlite3.Connection,
    record_type: str,
    token: str,
    *,
    row: bool = False,
    apply: bool = False,
) -> CurationReport:
    """Lift one verdict — a family's, or with ``row=True`` a single row's; dry-run by
    default.

    Targeting mirrors :func:`annotate_record`, and clearing one scope never touches the
    other: a family verdict and a row verdict may legitimately coexist on the same
    family (that is the precedence case), so a `--clear` that took both would silently
    lift a ruling the operator never named.

    The target is resolved leniently on purpose, in both scopes: a ``dedup_base``
    literal is accepted even when the family is gone, and a ``--row`` id even when the
    row is gone, because clearing an **orphaned** verdict (the one `pemr verify` warns
    about) is exactly a case where nothing live names it.

    Raises :class:`CurationNotFoundError` when there is no verdict to lift — a typo
    must not read as success.
    """
    db.require_migrated(conn)
    _require_type(record_type)
    token = (token or "").strip()
    if row:
        if not token.isdigit():
            raise ValueError(
                f"--row needs a {record_type} row id, not a dedup_base "
                f"({token[:12]}...) - drop --row to clear the family's verdict"
            )
        record_id = int(token)
        existing = get_verdict(conn, record_type, "", record_id=record_id)
        if existing is None:
            raise CurationNotFoundError(
                f"no row-scoped curation verdict for {record_type} row #{record_id} - "
                "see `pemr record annotate --list`"
            )
        base = existing["dedup_base"]
        label, _live_base, family_size = row_label(conn, record_type, record_id)
    else:
        record_id = FAMILY_SCOPE
        base = resolve_base(conn, record_type, token) if token.isdigit() else token
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
        record_id=record_id,
        scope=SCOPE_ROW if record_id else SCOPE_FAMILY,
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
            if record_id:
                conn.execute(
                    "DELETE FROM curation WHERE record_type = ? AND record_id = ?",
                    (record_type, record_id),
                )
            else:
                conn.execute(
                    "DELETE FROM curation WHERE record_type = ? AND dedup_base = ? "
                    "AND record_id = 0",
                    (record_type, base),
                )
    report.applied = apply
    return report


# --------------------------------------------------------------------------- #
# The `pemr rekey` collision seam (issue #116)
# --------------------------------------------------------------------------- #

class CollisionResolver:
    """The overlay half of :class:`dedup.CollisionResolver` — the seam `rekey` asks
    "did a human already settle this pair?" through.

    Injected rather than imported: this module imports :mod:`dedup`, so ``dedup`` cannot
    import it back. ``dedup`` declares the shape as a ``Protocol`` and the CLI builds the
    implementation, which keeps every `curation`-table SQL statement here (the
    :func:`retire_row_verdicts` precedent) and keeps the import graph acyclic.
    """

    def __init__(self, verdicts: VerdictMap) -> None:
        self._verdicts = verdicts

    def covering_verdicts(
        self, record_type: str, row: sqlite3.Row, clash: sqlite3.Row
    ) -> list[dict]:
        """**Every** verdict that rules on a collision between two rows, in ``(row,
        clash)`` order — 0, 1 or 2 of them.

        Either row, either scope: the operator annotated whichever of the pair they were
        looking at, and a family verdict on one is as much a ruling on the pair as a row
        verdict on the other. Resolution runs through :meth:`VerdictMap.for_row`, so the
        row-over-family precedence rule stays defined in exactly one place.

        Reporting *both* rulings is the point (issue #124). Since #122 the resolving
        vocabulary holds opposite answers — ``distinct`` says two facts, the others say
        one — so a pair carrying a resolving verdict on each row can be contradictory,
        and returning only the first would report one side's settlement as if it were
        unanimous, decided by nothing but scan order. Choosing among the rulings, or
        refusing to, is no longer this method's job: the caller decides on the set of
        :meth:`settlement` values.

        When a single family verdict covers both rows, ``for_row`` returns the same dict
        twice and it is returned twice — deliberate, and harmless precisely because the
        caller decides on settlements rather than on verdict identity.

        The **stored** ``dedup_base`` is the lookup key, not the recomputed one: the
        human ruled on the family as it exists today, before this rekey moves it.
        """
        pk = f"{record_type}_id"
        verdicts: list[dict] = []
        for candidate in (row, clash):
            verdict = self._verdicts.for_row(
                record_type, candidate["dedup_base"], candidate[pk]
            )
            if verdict is not None and verdict["status"] in RESOLVING_STATUSES:
                verdicts.append(verdict)
        return verdicts

    def settlement(self, verdict: dict) -> str:
        """Which question the resolving ``verdict`` answered: ``"merged"`` | ``"distinct"``.

        ``"merged"`` — the pair is **one fact** (``merged-into``/``superseded``).
        ``"distinct"`` — it is **two facts** that share a recomputed key
        (:data:`DISTINCT_STATUSES`, issue #122).

        A method rather than a constant `dedup` could read: the status vocabulary lives
        here and must not leak across the seam, since ``dedup`` cannot import ``curation``.
        The two return values are the stable contract `rekey` and the CLI report on, while
        the vocabulary behind them stays free to grow.
        """
        return "distinct" if verdict["status"] in DISTINCT_STATUSES else "merged"

    def narrow(
        self,
        conn: sqlite3.Connection,
        record_type: str,
        verdict: dict,
        covered_row_ids: Iterable[int],
        base_map: dict[str, str],
    ) -> tuple[str, list[int]]:
        """Pin a collision-resolving family verdict to the rows it actually ruled on.

        Returns ``(action, row_ids)`` where ``action`` is ``"unchanged"`` or
        ``"narrowed"``. **Caller-managed transaction** (the
        :func:`retire_row_verdicts` precedent): `rekey` writes the keys and this
        conversion in one ``with conn:``, because a committed rekey whose authorizing
        verdict was left orphaned is the exact failure the seam exists to avoid.

        A *row-scoped* verdict needs nothing (``"unchanged"``): it already names exactly
        one row, `rekey` never renumbers a row id, and its stored base is a breadcrumb
        (migration 010).

        A *family-scoped* one may **not** simply follow its family onto the surviving
        base. Resolution merges a judged family with an unjudged one, so the surviving
        family is larger than the one the human ruled on: a verdict that moved wholesale
        would silently extend over a row nobody judged, and — for the
        :data:`APPENDIX_STATUSES` that resolve collisions — would drop that previously
        live row out of its clinical section. Criticality-blind by design: no live row
        ever silently leaves its rendered section, whatever it records. So the verdict is
        narrowed instead, to row-scoped verdicts on ``covered_row_ids`` — the rows whose
        *stored* ``dedup_base`` was the verdict's, i.e. its extension at ruling time.

        ``status``, ``note``, ``attributed_to`` and ``created_at`` are carried verbatim
        onto each pinned row (the first by ``UPDATE``, so the original ledger row itself
        survives), and ``merged_into_base`` follows ``base_map`` when the merge target
        moved in this same run. The breadcrumb ``dedup_base`` deliberately stays the
        family the ruling was made in — which is what `pemr verify` notices, so the
        narrowing is announced rather than silent.

        A row that already carries its **own** row-scoped verdict is skipped: that
        verdict already wins over the family one for that row (:meth:`VerdictMap.for_row`),
        so the family verdict never applied there and narrowing must not overwrite a
        second human ruling. If that skip leaves nothing to pin — defensive; the verdict
        was reached *through* a row that had none — nothing is written at all
        (``"unchanged"``): a verdict is a human's, and nothing here deletes one outright.
        """
        if verdict["record_id"]:
            return ("unchanged", [])
        _require_type(record_type)
        base = verdict["dedup_base"]
        target = verdict["merged_into_base"]
        new_target = base_map.get(target, target) if target else target
        taken = {
            int(r["record_id"])
            for r in conn.execute(
                "SELECT record_id FROM curation "
                "WHERE record_type = ? AND record_id <> 0",
                (record_type,),
            ).fetchall()
        }
        pinned = sorted(
            {int(rid) for rid in covered_row_ids if int(rid)} - taken
        )
        if not pinned:
            return ("unchanged", [])
        conn.execute(
            "UPDATE curation SET record_id = ?, merged_into_base = ? "
            "WHERE record_type = ? AND dedup_base = ? AND record_id = 0",
            (pinned[0], new_target, record_type, base),
        )
        for row_id in pinned[1:]:
            # The same ruling, restated on a sibling it already covered - not a new
            # verdict, hence the verbatim note/attribution/timestamp.
            conn.execute(
                "INSERT INTO curation (record_type, dedup_base, record_id, status, "
                "note, merged_into_base, attributed_to, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (record_type, base, row_id, verdict["status"], verdict["note"],
                 new_target, verdict["attributed_to"], verdict["created_at"]),
            )
        return ("narrowed", pinned)


def collision_resolver(conn: sqlite3.Connection) -> CollisionResolver:
    """Build the resolver `pemr rekey` adjudicates its collisions through.

    One :func:`load_verdicts` query up front (the render/verify hot-path idiom) — a rekey
    scans every row of every table, and a per-collision SELECT would be the wrong shape.
    A pre-008 snapshot yields an empty map, hence a resolver that resolves nothing, which
    is exactly today's behaviour.
    """
    return CollisionResolver(load_verdicts(conn))


def describe(verdict: dict) -> str:
    """One-line ``<status> - <note>`` summary shared by the CLI and render printers."""
    status = verdict.get("status") or ""
    if status == "merged-into" and verdict.get("merged_into_base"):
        status = f"merged into {str(verdict['merged_into_base'])[:12]}..."
    note = verdict.get("note") or ""
    who = verdict.get("attributed_to")
    attribution = f" ({who})" if who else ""
    return f"{status} - {note}{attribution}"

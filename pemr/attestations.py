"""Human-attested records — `pemr record assert` (issue #110).

A fact can be known to the family and still have no document behind it: a medication the
operator personally manages, whose renewal note is sitting in a portal nobody has pulled.
Until now the record had exactly two options, and both were wrong — omit the fact (and
hand a clinician a summary that is missing a live drug) or invent a source (forbidden).
This module is the third: **attestation**, provenance that is a named person rather than a
document.

    assert_record  -- commit one attested row, dry-run by default
    list_attested  -- the "needs source" queue: live attestations, oldest first

**Provenance on the row, not an overlay.** The curation overlay (#109) is keyed by
``(record_type, dedup_base)`` because a verdict is a family-level judgment that must
outlive the rows it rules on. An attestation is the opposite: it is what stands in for
``document_id``, it means nothing without its row, and it dies with the row. So it lives in
columns beside ``document_id`` (migration 009), and the three row states are derived, never
stored twice — see :func:`dedup.attestation_state`.

**Supersession is promotion.** When a later `commit-extraction` lands a document-sourced
row on the same ``dedup_base``, document provenance wins: the attested row acquires the
``document_id`` and keeps its attestation columns as history
(:func:`dedup._promote_attestation`). Nothing is deleted, and the row stops rendering as
unsourced the moment a real source backs it. A document that *disagrees* on the payload
stages an ordinary conflict instead — there is no justification for auto-resolving a
disagreement, only for recording an agreement.

**Refuse, don't stage.** The mirror case — asserting a fact the record already holds —
never stages a conflict. An equal payload reports ``duplicate`` and writes nothing; a
differing one raises :class:`AttestationCollisionError` naming the stored row. Precedent:
``commit_extraction``'s pass 1 already refuses two colliding rows of one submission,
because the human is at the keyboard and a conflict staged against oneself has no
independent provenance to adjudicate. A conflict would also have to carry
``document_id = NULL``, which every resolution path treats as a document write.

`record assert` is **CLI-only** and deliberately absent from ``mcp_server.WRITE_TOOLS`` and
``TOOL_NAMES``, matching `document rm` / `record rm` / `record annotate`. It is the one
path that can put a fact in the record with no external source, which is precisely the
thing the blessed-write-set boundary exists to keep an agent out of.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import db, dedup, query


class AttestationCollisionError(ValueError):
    """Refused: the identity is already stored with a *different* payload.

    Not a staged conflict — see the module docstring. Friendly rc=1 at the CLI.
    """


@dataclass
class AttestReport:
    """Structured result of :func:`assert_record`; also the ``--json`` payload shape, so
    the key names in :meth:`as_dict` are a contract (the `record rm` / `record annotate`
    rule)."""

    record_type: str
    person_id: int
    person_slug: str
    attributed_to: str
    attested_on: str
    attested_at: str
    label: str = ""
    fields: dict = field(default_factory=dict)
    dedup_key: str = ""
    dedup_base: str = ""
    dedup_occurrence: int = 0
    outcome: str = "new"                  # "new" | "duplicate"
    row_id: int | None = None             # the row written (apply + new) or matched
    existing_provenance: str = ""         # "document #N" | "attestation" | ""
    applied: bool = False

    def as_dict(self) -> dict:
        return {
            "record_type": self.record_type,
            "person": self.person_slug,
            "person_id": self.person_id,
            "attributed_to": self.attributed_to,
            "attested_on": self.attested_on,
            "attested_at": self.attested_at,
            "label": self.label,
            "fields": self.fields,
            "dedup_key": self.dedup_key,
            "dedup_base": self.dedup_base,
            "dedup_occurrence": self.dedup_occurrence,
            "outcome": self.outcome,
            "row_id": self.row_id,
            "existing_provenance": self.existing_provenance,
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


def has_columns(conn: sqlite3.Connection, record_type: str = "lab_result") -> bool:
    """Whether the attestation columns exist — false for a database predating migration
    009.

    The :func:`curation.has_table` guard, one migration later: a snapshot restored from
    before 009 must give an empty list rather than ``no such column``. Every other read
    here goes through :func:`dedup.attestation_state`, which reads a row mapping and so
    needs no guard of its own.
    """
    _require_type(record_type)
    cols = {
        row["name"] for row in conn.execute(f"PRAGMA table_info({record_type})").fetchall()
    }
    return "attested_by" in cols


def _provenance(row: sqlite3.Row | dict) -> str:
    """How a stored row got here, in one phrase for an error message or a report."""
    row_map = dict(row)
    if row_map.get("document_id") is not None:
        return f"document #{row_map['document_id']}"
    return "attestation"


def assert_record(
    conn: sqlite3.Connection,
    record_type: str,
    person_slug: str,
    payload: dict,
    *,
    attributed_to: str,
    attested_on: str,
    dictionary: dict[str, str] | None = None,
    now: str | None = None,
    apply: bool = False,
) -> AttestReport:
    """Commit one row whose provenance is a person; dry-run by default.

    The row goes through the *same* three gates ``commit_extraction`` uses —
    :func:`dedup.validate_row`, :func:`dedup._assert_no_key_drift`,
    :func:`dedup._insert_record` — so "attestations use the validated write path" is true
    by construction rather than by convention, and the derived ``dedup_key`` /
    ``dedup_base`` are byte-identical to what a document-sourced commit of the same
    payload would produce.

    Validation is front-loaded and nothing is written on any raise. The dry run *is* the
    safety mechanism (the `record rm` lesson), so the report states which branch it took
    and, for ``duplicate``, the stored row's id and provenance — otherwise an operator
    cannot tell "nothing to do" from "about to write".

    Raises ``ValueError`` for an unknown type / bad payload / non-ISO or empty attestation
    metadata, :class:`query.PersonNotFoundError` for an unknown slug,
    :class:`dedup.DictionaryDriftError` when a stored row of this identity is on a stale
    key, and :class:`AttestationCollisionError` when the identity is already stored with a
    different payload.
    """
    db.require_migrated(conn)
    _require_type(record_type)
    if not has_columns(conn, record_type):
        raise ValueError(
            "this database predates migration 009 and has nowhere to record an "
            "attestation - run `pemr migrate` first; nothing was written"
        )

    person_id = query.resolve_person_id(conn, person_slug)
    dedup.validate_row(record_type, payload)

    who = (attributed_to or "").strip()
    if not who:
        raise ValueError(
            "an attestation needs someone to attribute it to - pass "
            "--attributed-to '<who>'; there is no anonymous attestation, because the "
            "attribution is the only provenance this row will have; nothing was written"
        )
    when = (attested_on or "").strip()
    if not when:
        raise ValueError(
            "an attestation needs the date it was made - pass --date YYYY-MM-DD; "
            "nothing was written"
        )
    if not dedup._is_iso_date(when):
        raise ValueError(
            f"--date: expected ISO date at year (YYYY), month (YYYY-MM) or full "
            f"(YYYY-MM-DD) precision, got {when!r}; nothing was written"
        )

    # Same guard as an ingest: a stored row of this identity sitting on a stale key would
    # be missed below, filing the fact a second time instead of colliding with it.
    dedup._assert_no_key_drift(conn, record_type, [payload], person_id, dictionary)

    base = dedup.dedup_key(record_type, payload, person_id, dictionary)
    family = dedup.load_family(conn, record_type, base)
    stamp = _now(now)
    report = AttestReport(
        record_type=record_type,
        person_id=person_id,
        person_slug=person_slug.strip().lower(),
        attributed_to=who,
        attested_on=when,
        attested_at=stamp,
        # `_rekey_label` reads every column its type names, so it is given a fully
        # keyed view; `fields` stays the sparse, operator-facing payload.
        label=dedup._rekey_label(
            record_type,
            {name: payload.get(name) for name in dedup.FIELD_SPECS[record_type]},
        ),
        fields={
            name: payload[name]
            for name in dedup.FIELD_SPECS[record_type]
            if payload.get(name) is not None
        },
        dedup_key=base,
        dedup_base=base,
    )

    if family:
        twin = next(
            (f for f in family if dedup._rows_equal(record_type, f, payload)), None
        )
        pk = f"{record_type}_id"
        if twin is None:
            stored = family[0]
            raise AttestationCollisionError(
                f"{record_type} {stored[pk]} "
                f"({dedup._rekey_label(record_type, stored)!r}, "
                f"{_provenance(stored)}) already holds this identity with a different "
                "value. An attestation is refused rather than staged as a conflict - "
                "there is no second source to adjudicate against, only you. Correct the "
                f"stored row (`pemr record rm {record_type} {stored[pk]}`, or "
                "`pemr record annotate` to rule on it) and retry; nothing was written"
            )
        report.outcome = "duplicate"
        report.row_id = int(twin[pk])
        report.label = dedup._rekey_label(record_type, twin)
        report.dedup_key = twin["dedup_key"]
        report.dedup_occurrence = int(twin["dedup_occurrence"] or 0)
        report.existing_provenance = _provenance(twin)
        report.applied = apply
        return report

    if apply:
        with conn:
            report.row_id = dedup._insert_record(
                conn, record_type, payload, person_id, None, base,
                attestation=(who, when, stamp),
            )
    report.applied = apply
    return report


def list_attested(
    conn: sqlite3.Connection,
    record_type: str | None = None,
    *,
    include_superseded: bool = False,
) -> list[dict]:
    """Attested rows as plain dicts, oldest attestation first.

    Live attestations only by default — that set *is* the "needs source" queue, and it is
    the queryable surface a future `pemr review list` verb (the #109/#110 follow-up)
    consumes without needing another schema change. ``include_superseded=True`` adds the
    rows a document has since backed, which is how an operator confirms a source landed.

    Returns dicts rather than ``sqlite3.Row``s deliberately: the caller is a report or a
    ``--json`` payload, not a further query.
    """
    db.require_migrated(conn)
    if record_type is not None:
        _require_type(record_type)
    if not has_columns(conn):
        return []
    types = (record_type,) if record_type is not None else dedup.KNOWN_TYPES

    out: list[dict] = []
    for rtype in types:
        pk = f"{rtype}_id"
        sql = (
            f"SELECT {rtype}.*, person.slug AS person FROM {rtype} "
            f"LEFT JOIN person ON person.person_id = {rtype}.person_id "
            "WHERE attested_by IS NOT NULL"
        )
        if not include_superseded:
            sql += " AND document_id IS NULL"
        for row in conn.execute(sql).fetchall():
            out.append({
                "record_type": rtype,
                "row_id": int(row[pk]),
                "person": row["person"],
                "label": dedup._rekey_label(rtype, row),
                "attested_by": row["attested_by"],
                "attested_on": row["attested_on"],
                "attested_at": row["attested_at"],
                "document_id": row["document_id"],
                "needs_source": row["document_id"] is None,
                "dedup_base": row["dedup_base"],
            })
    out.sort(key=lambda r: (r["attested_at"] or "", r["record_type"], r["row_id"]))
    return out

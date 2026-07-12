"""Phase 3 read layer: structured queries, full-text `find`, and lab `trends`.

All functions are pure reads over the tables phases 1–2 populate (Architecture.md §5).
They return plain Python data (dicts / lists of dicts) with stable field names; the CLI
owns human-readable vs ``--json`` formatting, and phase 5's MCP wrapper reuses the same
shapes as its payload. Person is always addressed by slug and resolved to ``person_id``
here — an unknown slug raises :class:`PersonNotFoundError` (a friendly rc=1 at the CLI).

Analyte matching (``--test`` on labs/trends) runs through the same ``norm()`` +
dictionary as dedup, so ``A1c`` finds rows stored as ``HbA1c``. Because the typed tables
store the *original* spelling (only ``dedup_key`` is normalized), that match is done in
Python over the candidate rows rather than in SQL.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date

from . import db
from .dedup import norm

# Word tokens for a safe FTS5 query: strips punctuation/operators so raw user input
# can never be mis-parsed as FTS syntax (each token is quoted as a phrase, AND-ed).
_WORD = re.compile(r"\w+", re.UNICODE)


class PersonNotFoundError(ValueError):
    """Raised when a slug does not resolve to a person (friendly rc=1 at the CLI)."""


def resolve_person_id(conn: sqlite3.Connection, slug: str) -> int:
    db.require_migrated(conn)
    row = conn.execute(
        "SELECT person_id FROM person WHERE slug = ?", (slug.strip().lower(),)
    ).fetchone()
    if row is None:
        raise PersonNotFoundError(f"no person with slug '{slug}'")
    return row["person_id"]


def _date_part(value: object) -> str:
    """Date portion of an ISO date/datetime string ('2026-01-02T09:00' -> '2026-01-02')."""
    if value is None:
        return ""
    return str(value).strip().replace("T", " ").split(" ")[0]


# --------------------------------------------------------------------------- #
# Structured queries
# --------------------------------------------------------------------------- #

def query_labs(
    conn: sqlite3.Connection,
    slug: str,
    test: str | None = None,
    since: str | None = None,
    dictionary: dict[str, str] | None = None,
) -> list[dict]:
    """Lab rows for a person, oldest first. ``test`` is dictionary-normalized and matched
    against each row's normalized ``test_name``; ``since`` keeps rows on/after that date."""
    person_id = resolve_person_id(conn, slug)
    sql = "SELECT * FROM lab_result WHERE person_id = ?"
    params: list[object] = [person_id]
    if since:
        sql += " AND date(collected_at) >= date(?)"
        params.append(since)
    sql += " ORDER BY collected_at, test_name"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if test:
        target = norm(test, dictionary)
        rows = [r for r in rows if norm(r["test_name"], dictionary) == target]
    return rows


def query_meds(
    conn: sqlite3.Connection, slug: str, active: bool = False
) -> list[dict]:
    """Medications for a person. ``active`` keeps only current ones — no end date, or
    an explicit ``status='active'`` (Architecture.md §5)."""
    person_id = resolve_person_id(conn, slug)
    sql = "SELECT * FROM medication WHERE person_id = ?"
    params: list[object] = [person_id]
    if active:
        sql += " AND (ended_on IS NULL OR status = 'active')"
    sql += " ORDER BY (started_on IS NULL), started_on, name"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def query_timeline(
    conn: sqlite3.Connection, slug: str, since: str | None = None
) -> list[dict]:
    """Merged chronological event stream across the typed tables + observations.

    Each event carries ``date``, ``type``, a one-line ``summary`` and ``document_id``
    provenance. Medications contribute up to two events (start and, if ended, stop).
    Events without a usable date are omitted (they cannot be placed on a timeline).
    ``since`` (a date) drops events strictly before it. Ordered oldest first.
    """
    person_id = resolve_person_id(conn, slug)
    events: list[dict] = []

    def add(when: object, etype: str, summary: str, document_id: object) -> None:
        d = _date_part(when)
        if not d:
            return
        events.append(
            {"date": d, "type": etype, "summary": summary, "document_id": document_id}
        )

    for r in conn.execute(
        "SELECT * FROM lab_result WHERE person_id = ?", (person_id,)
    ).fetchall():
        value = r["value_num"] if r["value_num"] is not None else r["value_text"]
        unit = f" {r['unit']}" if r["unit"] else ""
        flag = f" [{r['flag']}]" if r["flag"] else ""
        val = "" if value is None else f" {value}{unit}"
        add(r["collected_at"], "lab", f"{r['test_name']}{val}{flag}".strip(), r["document_id"])

    for r in conn.execute(
        "SELECT * FROM medication WHERE person_id = ?", (person_id,)
    ).fetchall():
        dose = f" {r['dose']}" if r["dose"] else ""
        add(r["started_on"], "med-start", f"started {r['name']}{dose}", r["document_id"])
        add(r["ended_on"], "med-stop", f"stopped {r['name']}", r["document_id"])

    for r in conn.execute(
        "SELECT * FROM procedure WHERE person_id = ?", (person_id,)
    ).fetchall():
        outcome = f" - {r['outcome']}" if r["outcome"] else ""
        add(r["performed_on"], "procedure", f"{r['name']}{outcome}", r["document_id"])

    for r in conn.execute(
        "SELECT * FROM appointment WHERE person_id = ?", (person_id,)
    ).fetchall():
        who = " ".join(p for p in (r["provider"], r["specialty"]) if p)
        why = r["reason"] or r["summary"] or ""
        summary = " - ".join(p for p in (who, why) if p) or "appointment"
        add(r["scheduled_for"], "appointment", summary, r["document_id"])

    for r in conn.execute(
        "SELECT * FROM observation WHERE person_id = ?", (person_id,)
    ).fetchall():
        value = r["value_num"] if r["value_num"] is not None else r["value_text"]
        unit = f" {r['unit']}" if r["unit"] else ""
        parts = [r["obs_type"]]
        if r["key"]:
            parts.append(str(r["key"]))
        detail = " ".join(parts)
        val = "" if value is None else f" = {value}{unit}"
        add(r["observed_at"], "observation", f"{detail}{val}".strip(), r["document_id"])

    if since:
        cutoff = _date_part(since)
        events = [e for e in events if e["date"] >= cutoff]
    events.sort(key=lambda e: (e["date"], e["type"]))
    return events


# --------------------------------------------------------------------------- #
# Full-text search
# --------------------------------------------------------------------------- #

def _fts_query(raw: str) -> str | None:
    """Build a safe FTS5 MATCH expression from free user text.

    Each word token is quoted as a phrase and the tokens are AND-ed (implicit), so
    punctuation or FTS operators in the input can't blow up or change the query's
    meaning. Returns ``None`` when the input has no searchable tokens.
    """
    tokens = _WORD.findall(raw or "")
    if not tokens:
        return None
    return " ".join(f'"{t}"' for t in tokens)


def find(conn: sqlite3.Connection, slug: str, query: str) -> list[dict]:
    """Full-text search across OCR text + record text fields for one person.

    Returns hits ranked best-first, each with its source (table + id), a highlighted
    ``snippet`` and ``document_id`` provenance.
    """
    person_id = resolve_person_id(conn, slug)
    match = _fts_query(query)
    if match is None:
        return []
    rows = conn.execute(
        "SELECT source_table, source_id, document_id, "
        "snippet(record_fts, 4, '[', ']', '...', 12) AS snippet, rank "
        "FROM record_fts WHERE record_fts MATCH ? AND person_id = ? "
        "ORDER BY rank",
        (match, person_id),
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Trends
# --------------------------------------------------------------------------- #

def _ordinal(value: object) -> int | None:
    try:
        return date.fromisoformat(_date_part(value)).toordinal()
    except (ValueError, TypeError):
        return None


def _slope_per_day(points: list[tuple[int, float]]) -> float | None:
    """Least-squares slope (value units per day) of y vs x=ordinal-day. ``None`` when
    fewer than two points or all points share one date (zero x-variance)."""
    n = len(points)
    if n < 2:
        return None
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    denom = sum((x - mx) ** 2 for x, _ in points)
    if denom == 0:
        return None
    num = sum((x - mx) * (y - my) for x, y in points)
    return num / denom


def trends(
    conn: sqlite3.Connection,
    slug: str,
    test: str,
    dictionary: dict[str, str] | None = None,
) -> dict:
    """Summary stats for one dictionary-normalized analyte over time.

    Returns ``{test, count, unit, min, max, latest, latest_at, slope_per_day}``.
    ``count`` is the number of numeric points; ``slope_per_day`` degrades to ``None``
    with fewer than two numeric points (or a single collection date).
    """
    person_id = resolve_person_id(conn, slug)
    target = norm(test, dictionary)
    rows = conn.execute(
        "SELECT value_num, unit, collected_at, test_name FROM lab_result "
        "WHERE person_id = ? AND value_num IS NOT NULL ORDER BY collected_at",
        (person_id,),
    ).fetchall()
    matched = [r for r in rows if norm(r["test_name"], dictionary) == target]

    result: dict = {
        "test": target,
        "count": len(matched),
        "unit": None,
        "min": None,
        "max": None,
        "latest": None,
        "latest_at": None,
        "slope_per_day": None,
    }
    if not matched:
        return result

    values = [float(r["value_num"]) for r in matched]
    units = {r["unit"] for r in matched if r["unit"]}
    result["unit"] = next(iter(units)) if len(units) == 1 else None
    result["min"] = min(values)
    result["max"] = max(values)
    latest = matched[-1]  # rows came back ORDER BY collected_at
    result["latest"] = float(latest["value_num"])
    result["latest_at"] = latest["collected_at"]

    points = [
        (o, float(r["value_num"]))
        for r in matched
        if (o := _ordinal(r["collected_at"])) is not None
    ]
    result["slope_per_day"] = _slope_per_day(points)
    return result

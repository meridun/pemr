"""Phase 3 read layer: structured queries, full-text `find`, and lab `trends`.

All functions are pure reads over the tables phases 1–2 populate (Architecture.md §5).
They return plain Python data (dicts / lists of dicts) with stable field names; the CLI
owns human-readable vs ``--json`` formatting, and phase 5's MCP wrapper reuses the same
shapes as its payload. Person is always addressed by slug and resolved to ``person_id``
here — an unknown slug raises :class:`PersonNotFoundError` (a friendly rc=1 at the CLI).

Analyte matching (``--test`` on labs/trends) runs through the same dictionary as dedup,
so ``A1c`` finds rows stored as ``HbA1c``. Because the typed tables store the *original*
spelling (only ``dedup_key`` is normalized), that match is done in Python over the
candidate rows rather than in SQL.

The two readers match at deliberately **different granularities** (issue #71):
``query_labs`` uses ``norm()`` and shows the whole analyte family, while ``trends`` uses
``key_token()`` so a numeric series never interleaves two different assays of one
analyte (a CMP ``Albumin`` and an SPEP ``Albumin (SPEP)``) — and discloses the rows it
excluded on that basis rather than dropping them silently.
"""

from __future__ import annotations

import calendar
import re
import sqlite3
from datetime import date, datetime

from . import db
from .dedup import key_token, norm

# Word tokens for a safe FTS5 query: strips punctuation/operators so raw user input
# can never be mis-parsed as FTS syntax (each token is quoted as a phrase, AND-ed).
_WORD = re.compile(r"\w+", re.UNICODE)

# Medication ``status`` values that end the course even when no explicit ``ended_on``
# date was extracted (issue #21). ``status`` is unconstrained at the DB layer
# (migrations/001_init.sql documents active|discontinued|prn, but extraction agents emit
# terminal values like completed/stopped as well), so matching is lowercased and trimmed.
TERMINAL_MED_STATUSES = frozenset({"completed", "stopped", "discontinued"})


def _row_get(row: sqlite3.Row | dict, key: str) -> object:
    """Column access that tolerates a missing key on either a dict or ``sqlite3.Row``."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _end_of_period(value: object) -> date | None:
    """Last day covered by an ``ended_on``, or ``None`` when it can't be parsed.

    ``ended_on`` is stored at year (``2024``), month (``2024-01``) or full precision
    (``2024-01-01``, possibly as a timestamp) — see ``dedup.DATE_FIELDS``. A coarse
    date is widened to the **end** of the period it names (``2024`` -> ``2024-12-31``),
    so a course only counts as over once every day it could have covered is past. That
    direction is deliberate: dropping a med the record may still cover is the worse
    error in a document handed to a clinician.
    """
    text = str(value).strip().replace("T", " ").split(" ")[0]
    parts = text.split("-")
    try:
        if len(parts) == 1:
            return date(int(parts[0]), 12, 31)
        if len(parts) == 2:
            year, month = int(parts[0]), int(parts[1])
            return date(year, month, calendar.monthrange(year, month)[1])
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, TypeError):
        return None


def med_is_current(row: sqlite3.Row | dict, *, now: datetime | None = None) -> bool:
    """True when a medication is still current: the course hasn't ended and no terminal
    status ended it.

    A past ``ended_on`` ends the course whatever the ``status`` says (issue #57):
    printed med lists head a section "Active", so a faithful extraction of a 2024 visit
    note carries ``status='active'`` on a finished ten-day antibiotic course, and that
    stale label must not outrank an explicit end date. ``status='active'`` wins only
    when ``ended_on`` is absent, still in the future, or unparseable (the prior-auth
    case: approved *through* a future date). Any other end date still ends the course
    as before — only ``status='active'`` overrides a future one.

    A terminal status (:data:`TERMINAL_MED_STATUSES`) ends the course even when no
    ``ended_on`` was extracted — the contradiction behind issue #21, where a
    ``completed`` med with a null ``ended_on`` rendered as ``(current)``.

    ``now`` is injectable for deterministic tests/renders, like the ``render`` layer's.
    """
    status = str(_row_get(row, "status") or "").strip().lower()
    ended_on = _row_get(row, "ended_on")
    if ended_on:
        end = _end_of_period(ended_on)
        if end is not None and end < (now or datetime.now()).date():
            return False
        return status == "active"
    return status not in TERMINAL_MED_STATUSES


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
    against each row's normalized ``test_name``; ``since`` keeps rows on/after that date.

    Matching is on ``norm()``, the **analyte family** — deliberately coarser than the
    dedup key (issue #71). ``--test albumin`` lists the CMP albumin *and* the SPEP
    ``Albumin (SPEP)`` fraction, because a listing should show everything filed under
    that analyte. Only the numeric series (:func:`trends`) needs assay precision.
    """
    person_id = resolve_person_id(conn, slug)
    sql = "SELECT * FROM lab_result WHERE person_id = ?"
    params: list[object] = [person_id]
    if since:
        sql += " AND date(collected_at) >= date(?)"
        params.append(since)
    # Row id breaks same-timestamp ties: `--keep both` admits a second draw under the
    # same date, and "later row id = later point" keeps the order deterministic.
    sql += " ORDER BY collected_at, test_name, lab_result_id"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if test:
        target = norm(test, dictionary)
        rows = [r for r in rows if norm(r["test_name"], dictionary) == target]
    return rows


def query_meds(
    conn: sqlite3.Connection,
    slug: str,
    active: bool = False,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Medications for a person. ``active`` keeps only current ones — no end date and
    no terminal status, or a still-future end date with ``status='active'``
    (Architecture.md §5, :func:`med_is_current`). A terminal status
    (completed/stopped/discontinued) ends the course even without an ``ended_on``
    (issue #21), and a *past* ``ended_on`` ends it even under ``status='active'``
    (issue #57), so both are excluded from ``active``. ``now`` is injectable so the
    render layer's deterministic clock reaches the currency test."""
    person_id = resolve_person_id(conn, slug)
    sql = ("SELECT * FROM medication WHERE person_id = ? "
           "ORDER BY (started_on IS NULL), started_on, name")
    rows = [dict(r) for r in conn.execute(sql, (person_id,)).fetchall()]
    if active:
        rows = [r for r in rows if med_is_current(r, now=now)]
    return rows


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


def find(conn: sqlite3.Connection, slug: str | None, query: str) -> list[dict]:
    """Full-text search across OCR text + record text fields.

    With ``slug`` set, restricts to that person; with ``slug=None`` searches the
    whole household. Returns hits ranked best-first, each carrying its owning
    ``person`` slug, source (table + id), a highlighted ``snippet`` and
    ``document_id`` provenance.
    """
    match = _fts_query(query)
    if match is None:
        return []
    sql = (
        "SELECT person.slug AS person, record_fts.source_table, "
        "record_fts.source_id, record_fts.document_id, "
        "snippet(record_fts, 4, '[', ']', '...', 12) AS snippet, record_fts.rank "
        "FROM record_fts JOIN person ON person.person_id = record_fts.person_id "
        "WHERE record_fts MATCH ?"
    )
    params: list = [match]
    if slug is None:
        db.require_migrated(conn)
    else:
        sql += " AND record_fts.person_id = ?"
        params.append(resolve_person_id(conn, slug))
    sql += " ORDER BY record_fts.rank"
    rows = conn.execute(sql, params).fetchall()
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
    """Summary stats for one **assay** of one analyte over time.

    Returns ``{test, count, unit, min, max, latest, latest_at, latest_tie,
    slope_per_day, other_assays, other_assay_count}``. ``count`` is the number of
    numeric points; ``slope_per_day`` degrades to ``None`` with fewer than two distinct
    collection dates. ``latest`` is the row with the greatest ``collected_at``, ties
    broken by the greatest ``lab_result_id`` (most-recently-ingested wins);
    ``latest_tie`` counts how many matched rows share that exact ``collected_at``
    timestamp.

    Matching is on ``key_token()``, not ``norm()`` (issue #71): a series that silently
    interleaves a CMP albumin with an SPEP albumin is a wrong chart, the same class of
    harm as the dedup collision this splits apart. Rows of the *same* analyte family
    excluded by a differing qualifier are therefore **disclosed, not dropped** —
    ``other_assays`` lists their key tokens (each usable verbatim as ``--test``) and
    ``other_assay_count`` counts the numeric rows behind them.
    """
    person_id = resolve_person_id(conn, slug)
    target = key_token(test, dictionary)
    family = norm(test, dictionary)
    rows = conn.execute(
        "SELECT lab_result_id, value_num, unit, collected_at, test_name FROM lab_result "
        "WHERE person_id = ? AND value_num IS NOT NULL "
        "ORDER BY collected_at, lab_result_id",
        (person_id,),
    ).fetchall()
    matched = []
    others: dict[str, int] = {}
    for r in rows:
        token = key_token(r["test_name"], dictionary)
        if token == target:
            matched.append(r)
        elif norm(r["test_name"], dictionary) == family:
            others[token] = others.get(token, 0) + 1

    result: dict = {
        "test": target,
        "count": len(matched),
        "unit": None,
        "min": None,
        "max": None,
        "latest": None,
        "latest_at": None,
        "latest_tie": 0,
        "slope_per_day": None,
        # Same analyte, different assay: reported so the excluded rows stay findable.
        "other_assays": sorted(others),
        "other_assay_count": sum(others.values()),
    }
    if not matched:
        return result

    values = [float(r["value_num"]) for r in matched]
    units = {r["unit"] for r in matched if r["unit"]}
    result["unit"] = next(iter(units)) if len(units) == 1 else None
    result["min"] = min(values)
    result["max"] = max(values)
    latest = matched[-1]  # rows came back ORDER BY collected_at, lab_result_id
    result["latest"] = float(latest["value_num"])
    result["latest_at"] = latest["collected_at"]
    result["latest_tie"] = sum(
        1 for r in matched if r["collected_at"] == latest["collected_at"]
    )

    points = [
        (o, float(r["value_num"]))
        for r in matched
        if (o := _ordinal(r["collected_at"])) is not None
    ]
    result["slope_per_day"] = _slope_per_day(points)
    return result

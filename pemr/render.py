"""Phase 4 render layer: generated Markdown documents as pure functions of DB state.

Architecture.md §6/§9. ``render.py`` produces the project's current deliverables --
master summary, appointment brief, journal -- as pure, **read-only** functions of DB
state, so they never drift from truth (old exports are disposable). Every function is
callable as plain Python with structured args (a person slug or appointment id, plus an
optional synonym ``dictionary`` and a ``now`` for a deterministic generated-at stamp) so
phase 5's MCP wrapper stays thin. The CLI owns argument parsing and the §5
stdout/redirect contract; nothing here writes to the DB, ever.

Observation conventions (decided for this phase, documented alongside the dictionary's
canonical *vital* vocabulary in ``data/dictionary.example.toml``):

  * **conditions** -> ``observation`` rows with ``obs_type='condition'`` (the condition
    name in ``key``, optional detail in ``value_text``).
  * **allergies**  -> ``observation`` rows with ``obs_type='allergy'`` (the allergen in
    ``key``, optional reaction in ``value_text``).
  * **vitals**     -> ``observation`` rows with ``obs_type='vital'`` whose ``key`` is one
    of the dictionary's canonical vital tokens (``blood_pressure``, ``weight`` ...).
    "Latest vitals" is the most recent row per normalized ``key``.

Output is **ASCII-only** (the cp1252/cp437 Windows-console lesson from phases 2-3):
plain hyphens, never em-dashes -- a non-ASCII byte crashes a non-UTF-8 console.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from . import db, query
from .dedup import norm

# Observation obs_type conventions this layer reads (see module docstring).
OBS_CONDITION = "condition"
OBS_ALLERGY = "allergy"
OBS_VITAL = "vital"

# Default window for "recent labs" in an appointment brief (last N most recent).
_BRIEF_RECENT_LABS = 10


class AppointmentNotFoundError(ValueError):
    """Raised when an appointment id does not resolve (friendly rc=1 at the CLI)."""


# --------------------------------------------------------------------------- #
# Small formatting helpers (shared with the phase 3 human-table style)
# --------------------------------------------------------------------------- #

def _fmt(value: object) -> str:
    return "" if value is None else str(value)


def _date_part(value: object) -> str:
    """Date portion of an ISO date/datetime ('2026-01-02T09:00' -> '2026-01-02')."""
    if value is None:
        return ""
    return str(value).strip().replace("T", " ").split(" ")[0]


def _generated_at(now: datetime | None) -> str:
    return (now or datetime.now()).replace(microsecond=0).isoformat()


def _lab_value(row: sqlite3.Row | dict) -> str:
    value = row["value_num"] if row["value_num"] is not None else row["value_text"]
    unit = f" {row['unit']}" if row["unit"] else ""
    return f"{_fmt(value)}{unit}".strip()


def _is_abnormal(row: sqlite3.Row | dict) -> bool:
    """A lab is abnormal if it carries a non-normal ``flag`` OR its numeric value falls
    outside the reference interval (Architecture.md §6, issue #9)."""
    flag = (row["flag"] or "").strip().lower()
    if flag and flag not in ("normal", "n", "none"):
        return True
    value = row["value_num"]
    if value is not None:
        low, high = row["ref_low"], row["ref_high"]
        if low is not None and value < low:
            return True
        if high is not None and value > high:
            return True
    return False


# --------------------------------------------------------------------------- #
# Read helpers (pure SELECTs; person already resolved to an id)
# --------------------------------------------------------------------------- #

def _observations(conn: sqlite3.Connection, person_id: int, obs_type: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM observation WHERE person_id = ? AND obs_type = ? "
        "ORDER BY COALESCE(key, ''), observed_at, observation_id",
        (person_id, obs_type),
    ).fetchall()
    return [dict(r) for r in rows]


def _latest_vitals(
    conn: sqlite3.Connection, person_id: int, dictionary: dict[str, str] | None
) -> list[dict]:
    """Most recent ``obs_type='vital'`` row per normalized ``key``. Rows are ordered so
    that, for a given key, the latest date (then highest id) wins deterministically."""
    rows = conn.execute(
        "SELECT * FROM observation WHERE person_id = ? AND obs_type = ? "
        "ORDER BY observed_at, observation_id",
        (person_id, OBS_VITAL),
    ).fetchall()
    latest: dict[str, dict] = {}
    for r in rows:
        latest[norm(r["key"], dictionary)] = dict(r)  # ascending order -> last wins
    return [latest[k] for k in sorted(latest)]


def _abnormal_labs(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM lab_result WHERE person_id = ? "
        "ORDER BY collected_at DESC, test_name",
        (person_id,),
    ).fetchall()
    return [dict(r) for r in rows if _is_abnormal(r)]


def _open_appointments(
    conn: sqlite3.Connection, person_id: int, today: str
) -> list[dict]:
    """Upcoming (scheduled on/after ``today``) or open (no post-visit ``summary``)
    appointments -- the ones a summary reader still needs to act on."""
    rows = conn.execute(
        "SELECT * FROM appointment WHERE person_id = ? "
        "ORDER BY (scheduled_for IS NULL), scheduled_for, appointment_id",
        (person_id,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = _date_part(r["scheduled_for"])
        upcoming = bool(d) and d >= today
        is_open = not (r["summary"] or "").strip()
        if upcoming or is_open:
            out.append(dict(r))
    return out


def _row_counts(conn: sqlite3.Connection, person_id: int) -> dict[str, int]:
    counts = {}
    for table in ("lab_result", "medication", "procedure", "appointment", "observation"):
        counts[table] = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE person_id = ?", (person_id,)
        ).fetchone()["n"]
    return counts


def _appt_who(row: sqlite3.Row | dict) -> str:
    return " ".join(p for p in (row["provider"], row["specialty"]) if p)


def _appt_line(row: sqlite3.Row | dict) -> str:
    who = _appt_who(row)
    why = row["reason"] or row["summary"] or ""
    detail = " - ".join(p for p in (who, why) if p) or "appointment"
    return f"{_date_part(row['scheduled_for']) or '(undated)'}  {detail}"


# --------------------------------------------------------------------------- #
# Markdown block helpers
# --------------------------------------------------------------------------- #

def _section(title: str, lines: list[str], *, empty: str = "_none recorded_") -> str:
    """A Markdown section: an always-present ``## title`` header, then either the bullet
    lines or an explicit empty-state note (so an absent section never reads as an
    overlooked one in a medical summary)."""
    body = "\n".join(lines) if lines else empty
    return f"## {title}\n\n{body}\n"


# --------------------------------------------------------------------------- #
# Master summary (Architecture.md §6)
# --------------------------------------------------------------------------- #

def render_summary(
    conn: sqlite3.Connection,
    slug: str,
    *,
    dictionary: dict[str, str] | None = None,
    now: datetime | None = None,
) -> str:
    """Markdown master summary for a person: active meds, conditions, allergies, latest
    vitals, recent abnormal labs, upcoming/open appointments -- with a self-identifying
    header (name, DOB, generated-at, source row counts). Read-only.

    Raises :class:`query.PersonNotFoundError` for an unknown slug (friendly rc=1)."""
    person_id = query.resolve_person_id(conn, slug)
    person = conn.execute(
        "SELECT * FROM person WHERE person_id = ?", (person_id,)
    ).fetchone()
    today = _date_part(now or datetime.now())
    counts = _row_counts(conn, person_id)

    header = (
        f"# Master Summary: {person['full_name']}\n\n"
        f"- Person: {person['slug']}\n"
        f"- DOB: {person['dob'] or 'unknown'}\n"
        f"- Generated: {_generated_at(now)} (read-only view of DB state)\n"
        f"- Source rows: labs={counts['lab_result']}, "
        f"medications={counts['medication']}, procedures={counts['procedure']}, "
        f"appointments={counts['appointment']}, "
        f"observations={counts['observation']}\n"
    )

    meds = query.query_meds(conn, slug, active=True)
    med_lines = []
    for m in meds:
        dose = f" {m['dose']}" if m["dose"] else ""
        freq = f" {m['frequency']}" if m["frequency"] else ""
        since = f" (since {m['started_on']})" if m["started_on"] else ""
        med_lines.append(f"- {m['name']}{dose}{freq}{since}")

    cond_lines = [
        f"- {c['key'] or c['value_text'] or '(unspecified)'}"
        + (f" - {c['value_text']}" if c["key"] and c["value_text"] else "")
        for c in _observations(conn, person_id, OBS_CONDITION)
    ]
    allergy_lines = [
        f"- {a['key'] or a['value_text'] or '(unspecified)'}"
        + (f" - {a['value_text']}" if a["key"] and a["value_text"] else "")
        for a in _observations(conn, person_id, OBS_ALLERGY)
    ]

    vital_lines = []
    for v in _latest_vitals(conn, person_id, dictionary):
        value = v["value_num"] if v["value_num"] is not None else v["value_text"]
        unit = f" {v['unit']}" if v["unit"] else ""
        when = f"  ({_date_part(v['observed_at'])})" if v["observed_at"] else ""
        vital_lines.append(f"- {v['key']}: {_fmt(value)}{unit}{when}")

    lab_lines = []
    for r in _abnormal_labs(conn, person_id):
        flag = f" [{r['flag']}]" if r["flag"] else ""
        low, high = r["ref_low"], r["ref_high"]
        if low is not None and high is not None:
            ref = f"  (ref {_fmt(low)}-{_fmt(high)})"
        elif high is not None:
            ref = f"  (ref <= {_fmt(high)})"
        elif low is not None:
            ref = f"  (ref >= {_fmt(low)})"
        else:
            ref = ""
        lab_lines.append(
            f"- {_date_part(r['collected_at'])}  {r['test_name']}  "
            f"{_lab_value(r)}{flag}{ref}"
        )

    appt_lines = [
        f"- {_appt_line(a)}" for a in _open_appointments(conn, person_id, today)
    ]

    parts = [
        header,
        _section("Active Medications", med_lines),
        _section("Conditions", cond_lines),
        _section("Allergies", allergy_lines),
        _section("Latest Vitals", vital_lines),
        _section("Recent Abnormal Labs", lab_lines, empty="_none flagged_"),
        _section("Upcoming / Open Appointments", appt_lines),
    ]
    return "\n".join(parts).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# Appointment brief -- "walk-in readiness" (Architecture.md §6)
# --------------------------------------------------------------------------- #

def render_brief(
    conn: sqlite3.Connection,
    appointment_id: int,
    *,
    dictionary: dict[str, str] | None = None,
    recent_labs: int = _BRIEF_RECENT_LABS,
    now: datetime | None = None,
) -> str:
    """Markdown walk-in brief for one appointment: the appointment header, current meds,
    recent labs (last N), procedures + observations, and an open-conflicts warning if any
    staged rows touch this person. Read-only.

    Med-interaction flags and suggested questions require external drug knowledge and are
    NOT deterministic engine work (Architecture.md §Open questions); a placeholder section
    is rendered here for the phase-5 agent layer to fill. Raises
    :class:`AppointmentNotFoundError` for an unknown id (friendly rc=1)."""
    db.require_migrated(conn)
    appt = conn.execute(
        "SELECT * FROM appointment WHERE appointment_id = ?", (appointment_id,)
    ).fetchone()
    if appt is None:
        raise AppointmentNotFoundError(f"no appointment with id {appointment_id}")
    person_id = appt["person_id"]
    person = conn.execute(
        "SELECT * FROM person WHERE person_id = ?", (person_id,)
    ).fetchone()

    header = (
        f"# Appointment Brief: {person['full_name']}\n\n"
        f"- Generated: {_generated_at(now)} (read-only view of DB state)\n"
    )

    appt_block = _section("Appointment", [
        f"- When: {_date_part(appt['scheduled_for']) or 'unscheduled'}",
        f"- Provider: {appt['provider'] or 'unknown'}",
        f"- Specialty: {appt['specialty'] or 'unknown'}",
        f"- Reason: {appt['reason'] or '(none given)'}",
    ])

    meds = query.query_meds(conn, person["slug"], active=True)
    med_lines = []
    for m in meds:
        dose = f" {m['dose']}" if m["dose"] else ""
        freq = f" {m['frequency']}" if m["frequency"] else ""
        med_lines.append(f"- {m['name']}{dose}{freq}")

    lab_rows = conn.execute(
        "SELECT * FROM lab_result WHERE person_id = ? "
        "ORDER BY collected_at DESC, test_name LIMIT ?",
        (person_id, max(recent_labs, 0)),
    ).fetchall()
    lab_lines = []
    for r in lab_rows:
        flag = f" [{r['flag']}]" if r["flag"] else ""
        lab_lines.append(
            f"- {_date_part(r['collected_at'])}  {r['test_name']}  {_lab_value(r)}{flag}"
        )

    proc_rows = conn.execute(
        "SELECT * FROM procedure WHERE person_id = ? "
        "ORDER BY performed_on DESC, procedure_id",
        (person_id,),
    ).fetchall()
    obs_rows = conn.execute(
        "SELECT * FROM observation WHERE person_id = ? "
        "ORDER BY observed_at DESC, observation_id",
        (person_id,),
    ).fetchall()
    ctx_lines = []
    for p in proc_rows:
        outcome = f" - {p['outcome']}" if p["outcome"] else ""
        ctx_lines.append(
            f"- {_date_part(p['performed_on']) or '(undated)'}  procedure: "
            f"{p['name']}{outcome}"
        )
    for o in obs_rows:
        value = o["value_num"] if o["value_num"] is not None else o["value_text"]
        val = f" = {_fmt(value)}{(' ' + o['unit']) if o['unit'] else ''}" if value is not None else ""
        detail = " ".join(p for p in (o["obs_type"], o["key"]) if p)
        ctx_lines.append(
            f"- {_date_part(o['observed_at']) or '(undated)'}  {detail}{val}"
        )

    open_conflicts = conn.execute(
        "SELECT * FROM conflict WHERE person_id = ? AND status = 'open' "
        "ORDER BY conflict_id",
        (person_id,),
    ).fetchall()
    conflict_lines = [
        f"- conflict #{c['conflict_id']} ({c['record_type']}) - resolve with "
        "`pemr review-conflicts`"
        for c in open_conflicts
    ]

    interaction = _section(
        "Medication Interaction Review",
        [],
        empty=(
            "_Filled by the agent layer (phase 5). Med-interaction flags and suggested "
            "questions require external drug knowledge and are not part of the "
            "deterministic engine -- verify medications with a pharmacist._"
        ),
    )

    parts = [
        header,
        appt_block,
        _section("Current Medications", med_lines),
        _section(f"Recent Labs (last {recent_labs})", lab_lines),
        _section("Procedures & Observations", ctx_lines),
        _section("Open Conflicts", conflict_lines, empty="_none_"),
        interaction,
    ]
    return "\n".join(parts).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# Journal -- narrative chronology (Architecture.md §6)
# --------------------------------------------------------------------------- #

def render_journal(
    conn: sqlite3.Connection,
    slug: str,
    *,
    since: str | None = None,
    now: datetime | None = None,
) -> str:
    """The phase-3 timeline event stream rendered as a narrative Markdown chronology,
    grouped by date, with document-provenance footnotes. Read-only.

    Raises :class:`query.PersonNotFoundError` for an unknown slug (friendly rc=1)."""
    person_id = query.resolve_person_id(conn, slug)
    person = conn.execute(
        "SELECT * FROM person WHERE person_id = ?", (person_id,)
    ).fetchone()
    events = query.query_timeline(conn, slug, since=since)

    header = (
        f"# Journal: {person['full_name']}\n\n"
        f"- Generated: {_generated_at(now)} (read-only view of DB state)\n"
    )
    if not events:
        return header + "\n_No dated events on record._\n"

    # Provenance footnotes: assign a stable [^n] marker per referenced document, in
    # first-appearance order, and resolve each to a one-line source description.
    footnote_order: list[int] = []
    for e in events:
        doc_id = e["document_id"]
        if doc_id is not None and doc_id not in footnote_order:
            footnote_order.append(doc_id)
    footnote_num = {doc_id: i + 1 for i, doc_id in enumerate(footnote_order)}

    lines: list[str] = [header]
    current_date = None
    for e in events:
        if e["date"] != current_date:
            current_date = e["date"]
            lines.append(f"\n## {current_date}\n")
        ref = ""
        if e["document_id"] is not None:
            ref = f" [^{footnote_num[e['document_id']]}]"
        lines.append(f"- **{e['type']}** -- {e['summary']}{ref}")

    if footnote_order:
        lines.append("\n---\n")
        for doc_id in footnote_order:
            doc = conn.execute(
                "SELECT * FROM document WHERE document_id = ?", (doc_id,)
            ).fetchone()
            lines.append(f"[^{footnote_num[doc_id]}]: {_document_citation(doc, doc_id)}")

    return "\n".join(lines).rstrip() + "\n"


def _document_citation(doc: sqlite3.Row | None, doc_id: int) -> str:
    if doc is None:
        return f"document #{doc_id} (not found)"
    bits = [f"document #{doc_id}"]
    if doc["category"]:
        bits.append(str(doc["category"]))
    if doc["provider"]:
        bits.append(str(doc["provider"]))
    if doc["doc_date"]:
        bits.append(f"dated {doc['doc_date']}")
    if doc["source_path"]:
        bits.append(f"sources/{doc['source_path']}")
    return ", ".join(bits)

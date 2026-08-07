"""Phase 4 render layer: generated Markdown documents as pure functions of DB state.

Architecture.md §6/§9. ``render.py`` produces the project's current deliverables --
master summary, appointment brief, journal -- as pure, **read-only** functions of DB
state, so they never drift from truth (old exports are disposable). Every function is
callable as plain Python with structured args (a person slug or appointment id, plus an
optional synonym ``dictionary`` and a ``now`` for a deterministic generated-at stamp) so
phase 5's MCP wrapper stays thin. The CLI owns argument parsing and the §5
stdout/redirect contract; nothing here writes to the DB, ever.

**Conditions and allergies are typed tables**, not observations, since migration 006
(issue #63): ``condition`` (``name`` + a ``status`` of active/resolved/history/
family-history, ``onset_on``/``resolved_on``, ``relation`` for a family entry) feeds the
Active Problems / Past Medical History / Family History sections, and ``allergy``
(``substance``, ``reaction``, ``criticality``, ``noted_on``) feeds Allergies.

Observation conventions (decided for this phase, documented alongside the dictionary's
canonical *vital* vocabulary in ``data/dictionary.example.toml``):

  * **vitals**     -> ``observation`` rows with ``obs_type='vital'`` whose ``key`` is one
    of the dictionary's canonical vital tokens (``blood_pressure``, ``weight`` ...).
    "Latest vitals" is the most recent row per normalized ``key``.
  * **orders**     -> ``observation`` rows with ``obs_type='order'`` for non-medication
    orders mined from the med-list section (DME, outpatient PT, referrals, consults):
    ``key`` = free-text item/order name (no canonical vocabulary), optional
    ``value_text`` = instructions and/or prescriber/target specialty. Rows are
    **grouped at render time** by normalized ``key``, newest first, with the
    collapse disclosed on the line (issue #93) -- the stored rows keep their dates
    and stay distinct, because an order is an event, not a standing fact.

Output is **ASCII-only** (the cp1252/cp437 Windows-console lesson from phases 2-3):
plain hyphens, never em-dashes -- a non-ASCII byte crashes a non-UTF-8 console.

This ASCII rule governs *static, CLI-authored literals* only. *Dynamic stored
document content* (e.g. the ``find`` snippet -- OCR'd text that can legitimately
carry accents/em-dashes/smart quotes) is echoed verbatim, never ASCII-normalized;
``cli.main`` reconfigures stdout/stderr to UTF-8 so a legacy Windows console
prints it correctly instead of ``?`` (issue #46). Literal vs. data are orthogonal.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from . import db, query
from .dedup import enum_token, key_token

# Observation obs_type conventions this layer reads (see module docstring).
OBS_VITAL = "vital"
OBS_ORDER = "order"

# `condition.status` buckets, one rendered section each (family history last: it is
# context about relatives, not the patient's own record).
CONDITION_ACTIVE = ("active",)
CONDITION_PAST = ("resolved", "history")
CONDITION_FAMILY = "family-history"

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


def _ref_range(row: sqlite3.Row | dict) -> str:
    """Trailing ``(ref ...)`` note for a lab's reference interval, or ``""`` when the
    row carries no bounds. Leading double space so callers can append it directly."""
    low, high = row["ref_low"], row["ref_high"]
    if low is not None and high is not None:
        return f"  (ref {_fmt(low)}-{_fmt(high)})"
    if high is not None:
        return f"  (ref <= {_fmt(high)})"
    if low is not None:
        return f"  (ref >= {_fmt(low)})"
    return ""


# --------------------------------------------------------------------------- #
# Read helpers (pure SELECTs; person already resolved to an id)
# --------------------------------------------------------------------------- #

def _conditions(
    conn: sqlite3.Connection, person_id: int, statuses: tuple[str, ...] | str
) -> list[dict]:
    """Condition rows in one status bucket, ordered by name.

    Filtering happens in Python on :func:`enum_token` rather than in SQL: the column
    stores the source's verbatim casing (``"Family History"`` validates and is stored as
    written), so a literal ``WHERE status = 'family-history'`` would silently drop rows.
    """
    wanted = {statuses} if isinstance(statuses, str) else set(statuses)
    rows = conn.execute(
        "SELECT * FROM condition WHERE person_id = ? ORDER BY name, condition_id",
        (person_id,),
    ).fetchall()
    return [dict(r) for r in rows if enum_token(r["status"]) in wanted]


def _allergies(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    """Allergy rows, ``criticality='high'`` first then alphabetical -- the dangerous ones
    have to survive a skim of the list."""
    rows = conn.execute(
        "SELECT * FROM allergy WHERE person_id = ? ORDER BY substance, allergy_id",
        (person_id,),
    ).fetchall()
    return sorted(
        (dict(r) for r in rows), key=lambda r: enum_token(r["criticality"]) != "high"
    )


def _condition_line(row: dict, *, past: bool = False) -> str:
    since = f"  (since {row['onset_on']})" if row["onset_on"] else ""
    resolved = (
        f"  (resolved {row['resolved_on']})" if past and row["resolved_on"] else ""
    )
    note = f" - {row['note']}" if row["note"] else ""
    return f"- {row['name']}{since}{resolved}{note}"


def _allergy_line(row: dict) -> str:
    crit = f" [{str(row['criticality']).upper()}]" if row["criticality"] else ""
    reaction = f" - {row['reaction']}" if row["reaction"] else ""
    noted = f"  (noted {row['noted_on']})" if row["noted_on"] else ""
    return f"- {row['substance']}{crit}{reaction}{noted}"


def _latest_vitals(
    conn: sqlite3.Connection, person_id: int, dictionary: dict[str, str] | None
) -> list[dict]:
    """Most recent ``obs_type='vital'`` row per normalized ``key``. Rows are ordered so
    that, for a given key, the latest date (then highest id) wins deterministically.

    Grouped by ``key_token()``, matching the dedup key (issue #71), so a meaningful
    qualifier keeps its own row: ``Blood Pressure (sitting)`` and ``(standing)`` are two
    readings to show, not one that overwrites the other."""
    rows = conn.execute(
        "SELECT * FROM observation WHERE person_id = ? AND obs_type = ? "
        "ORDER BY observed_at, observation_id",
        (person_id, OBS_VITAL),
    ).fetchall()
    latest: dict[str, dict] = {}
    for r in rows:
        latest[key_token(r["key"], dictionary)] = dict(r)  # ascending -> last wins
    return [latest[k] for k in sorted(latest)]


def _order_display(row: dict) -> str:
    """Display name of an order row: the item name, falling back to the detail text and
    then to an explicit placeholder (an unnamed order still has to be visible)."""
    return row["key"] or row["value_text"] or "(unspecified)"


def _grouped_orders(
    conn: sqlite3.Connection, person_id: int, dictionary: dict[str, str] | None
) -> list[dict]:
    """``obs_type='order'`` rows folded to one entry per normalized item, newest first.

    A repeated order/referral is restated by every document that mentions it, so a raw
    row dump renders one identical bullet per document (issue #93). Orders are *events*,
    not standing facts, so unlike conditions/allergies (issue #63) they must keep their
    date in the storage dedup key -- a January CBC and a June CBC are two real orders.
    The collapse therefore happens **here**, at render time: reversible, provenance
    intact in the DB, and always disclosed by the ``+N earlier`` note on the line.

    Grouped by :func:`key_token`, the same token the observation dedup key uses for
    ``key`` (issue #71), so the render never disagrees with storage about what counts as
    one item; ``value_text`` is the fallback identity for a keyless row, matching the
    display fallback. A row with neither renders on its own -- bucketing all of those
    together would fabricate a merge.

    Each returned dict is the group's **latest** row (most recent ``observed_at``, then
    highest ``observation_id``) plus ``group_count`` and ``group_first`` (earliest dated
    ``observed_at`` among the *earlier* rows, ``""`` when none of them is dated).
    """
    rows = conn.execute(
        "SELECT * FROM observation WHERE person_id = ? AND obs_type = ? "
        "ORDER BY observed_at, observation_id",
        (person_id, OBS_ORDER),
    ).fetchall()
    groups: dict[object, list[dict]] = {}
    for r in rows:
        token = key_token(r["key"], dictionary) or key_token(r["value_text"], dictionary)
        groups.setdefault(token or ("", r["observation_id"]), []).append(dict(r))

    out = []
    for members in groups.values():
        latest = members[-1]                      # ascending -> last is the latest
        earlier = sorted(
            d for d in (_date_part(m["observed_at"]) for m in members[:-1]) if d
        )
        out.append(dict(latest, group_count=len(members),
                        group_first=earlier[0] if earlier else ""))
    # Two stable passes: alphabetical, then latest-date descending (undated sorts last,
    # keeping its alphabetical order). Orders are actionable events, so recency leads --
    # matching the other event sections rather than the vitals panel's fixed A-Z.
    out.sort(key=lambda g: (_order_display(g).lower(), g["observation_id"]))
    out.sort(key=lambda g: _date_part(g["observed_at"]), reverse=True)
    return out


def _order_line(row: dict) -> str:
    detail = f" - {row['value_text']}" if row["key"] and row["value_text"] else ""
    notes = []
    when = _date_part(row["observed_at"])
    if when:
        notes.append(f"ordered {when}")
    if row["group_count"] > 1:
        first = f", first {row['group_first']}" if row["group_first"] else ""
        notes.append(f"+{row['group_count'] - 1} earlier{first}")
    note = f"  ({'; '.join(notes)})" if notes else ""
    return f"- {_order_display(row)}{detail}{note}"


def _abnormal_labs(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    rows = conn.execute(
        # lab_result_id DESC breaks same-timestamp ties (a `--keep both` sibling shares
        # its date): newest-first ordering treats the later row id as the later point.
        "SELECT * FROM lab_result WHERE person_id = ? "
        "ORDER BY collected_at DESC, test_name, lab_result_id DESC",
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
    for table in ("lab_result", "medication", "procedure", "appointment",
                  "observation", "condition", "allergy"):
        counts[table] = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE person_id = ?", (person_id,)
        ).fetchone()["n"]
    return counts


def _open_conflict_lines(conn: sqlite3.Connection, person_id: int) -> list[str]:
    """Bullet lines for this person's open (unresolved) conflicts.

    Shared by the summary and the brief: a staged correction means "a value in this
    record is disputed and the corrected one is not committed yet", so every rendered
    view that a reader treats as current has to say so (issue #59)."""
    rows = conn.execute(
        "SELECT * FROM conflict WHERE person_id = ? AND status = 'open' "
        "ORDER BY conflict_id",
        (person_id,),
    ).fetchall()
    return [
        f"- conflict #{c['conflict_id']} ({c['record_type']}) - resolve with "
        "`pemr review-conflicts`"
        for c in rows
    ]


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
    """Markdown master summary for a person: active meds, active problems, past medical
    history, family history, allergies, orders, latest vitals, recent abnormal labs,
    upcoming/open appointments, and any open conflicts --
    with a self-identifying header (name, DOB, generated-at, source row counts).
    Read-only.

    The summary is the document read *between* appointments, so an open conflict has to
    surface here too: without it a staged correction is invisible and the summary prints
    the stale value with no hint that a corrected one is pending (issue #59).

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
        f"observations={counts['observation']}, "
        f"conditions={counts['condition']}, allergies={counts['allergy']}\n"
    )

    meds = query.query_meds(conn, slug, active=True, now=now)
    med_lines = []
    for m in meds:
        dose = f" {m['dose']}" if m["dose"] else ""
        freq = f" {m['frequency']}" if m["frequency"] else ""
        since = f" (since {m['started_on']})" if m["started_on"] else ""
        med_lines.append(f"- {m['name']}{dose}{freq}{since}")

    active_lines = [
        _condition_line(c) for c in _conditions(conn, person_id, CONDITION_ACTIVE)
    ]
    past_lines = [
        _condition_line(c, past=True)
        for c in _conditions(conn, person_id, CONDITION_PAST)
    ]
    family_lines = [
        f"- {c['relation'] or 'family'}: {c['name']}"
        for c in _conditions(conn, person_id, CONDITION_FAMILY)
    ]
    allergy_lines = [_allergy_line(a) for a in _allergies(conn, person_id)]
    order_lines = [
        _order_line(o) for o in _grouped_orders(conn, person_id, dictionary)
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
        lab_lines.append(
            f"- {_date_part(r['collected_at'])}  {r['test_name']}  "
            f"{_lab_value(r)}{flag}{_ref_range(r)}"
        )

    appt_lines = [
        f"- {_appt_line(a)}" for a in _open_appointments(conn, person_id, today)
    ]

    parts = [
        header,
        _section("Active Medications", med_lines),
        _section("Active Problems", active_lines),
        _section("Past Medical History", past_lines),
        _section("Family History", family_lines),
        _section("Allergies", allergy_lines),
        _section("Orders & Referrals", order_lines),
        _section("Latest Vitals", vital_lines),
        _section("Recent Abnormal Labs", lab_lines, empty="_none flagged_"),
        _section("Upcoming / Open Appointments", appt_lines),
        _section(
            "Open Conflicts", _open_conflict_lines(conn, person_id), empty="_none_"
        ),
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
    recent labs (last N, newest draw first and abnormal-before-normal inside each draw),
    allergies, active problems, procedures + observations, and an open-conflicts warning
    if any staged rows touch this person. Read-only.

    Allergies and active problems are their own sections here (issue #63): a brief handed
    to a clinician that omits allergies is a safety gap, and since migration 006 moved
    them out of ``observation`` the generic observation loop no longer surfaces them.

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

    meds = query.query_meds(conn, person["slug"], active=True, now=now)
    med_lines = []
    for m in meds:
        dose = f" {m['dose']}" if m["dose"] else ""
        freq = f" {m['frequency']}" if m["frequency"] else ""
        med_lines.append(f"- {m['name']}{dose}{freq}")

    # Recency stays the primary axis, but a single draw can carry a 50+ analyte panel —
    # a flat `LIMIT N` over `collected_at DESC, test_name` then returns the
    # alphabetically-first N of that one draw and silently drops every abnormal in it.
    # So rank abnormal-before-normal *within* each collection timestamp before the cut:
    # the brief is the doc handed to a clinician, and the out-of-range values are the
    # ones that must survive truncation. Abnormality is `_is_abnormal` (flag OR
    # reference interval), which SQL can't express, so the cut happens here.
    lab_rows = conn.execute(
        "SELECT * FROM lab_result WHERE person_id = ? "
        "ORDER BY collected_at DESC, test_name, lab_result_id DESC",
        (person_id,),
    ).fetchall()
    draw_rank = {
        ts: i for i, ts in enumerate(dict.fromkeys(r["collected_at"] for r in lab_rows))
    }
    # Stable sort -> test_name order survives inside each (draw, abnormal?) bucket.
    lab_rows.sort(key=lambda r: (draw_rank[r["collected_at"]], not _is_abnormal(r)))
    lab_rows = lab_rows[: max(recent_labs, 0)]
    lab_lines = []
    for r in lab_rows:
        flag = f" [{r['flag']}]" if r["flag"] else ""
        # The brief is the doc handed to a clinician: an abnormal value must not read as
        # ordinary, so mark it and surface its reference interval (mirrors render summary).
        abnormal = f"  [!]{_ref_range(r)}" if _is_abnormal(r) else ""
        lab_lines.append(
            f"- {_date_part(r['collected_at'])}  {r['test_name']}  "
            f"{_lab_value(r)}{flag}{abnormal}"
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

    brief_allergy_lines = [_allergy_line(a) for a in _allergies(conn, person_id)]
    brief_problem_lines = [
        _condition_line(c) for c in _conditions(conn, person_id, CONDITION_ACTIVE)
    ]

    conflict_lines = _open_conflict_lines(conn, person_id)

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
        _section(f"Recent Labs (last {recent_labs}, abnormal first)", lab_lines),
        _section("Allergies", brief_allergy_lines),
        _section("Active Problems", brief_problem_lines),
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

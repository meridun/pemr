"""Phase 4 render layer: generated Markdown documents as pure functions of DB state.

Architecture.md §6/§9. ``render.py`` produces the project's current deliverables --
master summary, appointment brief, journal, curation record -- as pure, **read-only**
functions of DB state, so they never drift from truth (old exports are disposable). Every
function is
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

**Procedures on the summary** (issue #166). ``procedure`` rows arrive largely from billing
documents, so the table mixes genuine procedural history with routine service lines
(office visits, serial radiographs, venipuncture). ``render_summary`` therefore narrows
its ``## Procedures`` section against a **routine-pattern list** authored in
``dictionary.toml`` (``[procedures].routine``, loaded by
:func:`dedup.load_routine_procedures`), under three rules that are all one stance --
significance is never established by *absence* from a list:

  * **default-show** -- a name matching no configured pattern always renders, so a
    procedure type nobody anticipated cannot be silently dropped;
  * **suppress-only, summary-only** -- the list can remove a row from this one section and
    nothing else. ``render_brief`` and ``render_journal`` stay the complete record, and
    the stored rows are untouched;
  * **disclosed on the page** -- when anything was filtered the section says how many, the
    way order grouping discloses its collapse (issue #93). A summary that silently drops
    rows is the thing default-show is defending against.

Matching is token-boundary on :func:`dedup.norm`-normalized text, both sides, so ``cast``
cannot suppress ``Castration``: over-suppression is the failure mode here, and
under-suppression merely leaves a line on the page.

**Curation overlay** (issues #109, #114). Every section is filtered at read time against
the ``curation`` table, a pure overlay of recorded human verdicts scoped to a whole dedup
family or to a **single row** (:meth:`curation.VerdictMap.for_row` resolves per row, row
scope winning over family scope): ``superseded`` / ``erroneous-in-source`` /
``merged-into`` leave their section entirely, ``disputed`` renders in place with a
``[DISPUTED: ...]`` marker (and reaches the brief's ``## Questions for the Clinician``),
and ``confirmed`` — like ``distinct``, the collision ruling that says both rows are real
facts (issue #122) — renders exactly
as before. The questions section is **omitted entirely** when empty, so a record with no
verdicts renders byte-identically to what it did before the overlay existed. This is
still a pure function of DB state -- the filter is a read, and output changes after a
verdict because the database changed.

**The audit trail is its own target** (issue #168). Where the three clinical documents
once each carried a ``## Superseded / corrected`` appendix at their foot, the verdicts
now render as :func:`render_curation` -- ``pemr render curation``, redirected to a
``curation.md`` of the operator's choosing. That block was merge bookkeeping addressed to
a future curation session, not chart content, and on a curated dataset it dominated the
summary. Two consequences are deliberate:

  * the curation record is a **superset** of the block it replaced -- it enumerates
    :func:`curation.list_curation` scoped to the person, rather than the verdicts whichever
    sections happened to select, because a standalone audit trail must not depend on which
    render pass produced it;
  * it groups **by ruling**, not by row: one merge session's note recorded against forty
    families renders once with a count.

Nothing replaces the appendix inline. An ``APPENDIX_STATUSES`` verdict *removes* its row,
so no reader is shown a stale value -- the hazard :func:`_open_conflicts_warning` answers
(issue #59: the summary printing a value a staged correction disputes) has no analogue
here. The empty-state rule the appendix documented survives the move: a subject with no
verdicts gets ``""``, never a header implying a review happened.

**Attested rows** (issue #110). A row whose provenance is a named human rather than a
document (`pemr record assert`) is tagged wherever it renders, by :func:`_attest_suffix` at
every line builder: ``(attested by <who> <date>; no source document)``. It must never read
as a document-sourced fact. Once a document backs it the row is promoted and renders
unmarked, like any other sourced fact.

**Display units** (issue #136). A person may record one canonical display unit per
measurement key (``person_unit_pref``, migration 013). ``render_summary`` reads that
overlay the way it reads ``curation`` -- once, at the top -- and converts Latest Vitals
and Abnormal Labs into it, value and reference bounds together, disclosing every
conversion with :func:`_unit_suffix`. The stored row is never touched, abnormality is
still decided on stored values, and a person with no preference renders byte-identically
to what they did before the overlay existed. Like curation, this is still a pure function
of DB state: the preference *is* DB state.

Output is **ASCII-only** (the cp1252/cp437 Windows-console lesson from phases 2-3):
plain hyphens, never em-dashes -- a non-ASCII byte crashes a non-UTF-8 console.

This ASCII rule governs *static, CLI-authored literals* only. *Dynamic stored
document content* (e.g. the ``find`` snippet -- OCR'd text that can legitimately
carry accents/em-dashes/smart quotes) is echoed verbatim, never ASCII-normalized;
``cli.main`` reconfigures stdout/stderr to UTF-8 so a legacy Windows console
prints it correctly instead of ``?`` (issue #46). Literal vs. data are orthogonal.
"""

from __future__ import annotations

import calendar
import re
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime

from . import curation, db, dedup, query, units
from .dedup import enum_token, is_attested, key_token, norm

# Observation obs_type conventions this layer reads (see module docstring).
OBS_VITAL = "vital"
OBS_ORDER = "order"

# How an order is matched to the `lab_result` that answered it (issue #128). No FK links
# the two tables, so the linkage is *inferred* at render time from item-name and date
# proximity -- and deliberately narrowly. Over-suppression hides a genuinely open order,
# the exact miss this section exists to prevent; under-suppression merely leaves the
# noise the section already had. Every ambiguity therefore resolves to "still open" (see
# `_is_resulted`), and age is never a suppression signal on its own.
#: How long after an order a result may land and still count as *that* order's result.
ORDER_RESULT_WINDOW_DAYS = 30
#: Slack on the early side, for a document dating the draw a day ahead of the order
#: text. A result genuinely predating its order answers an *earlier* order, so this side
#: stays tight.
ORDER_RESULT_BACKDATE_DAYS = 1
#: Separators a *compound* order key uses to name several analytes at once (issue #145),
#: e.g. ``cbc,cmp,ldh`` or ``spep / immunofixation panel``. Only the two actually observed
#: in order text; extended on evidence, never speculatively -- every extra separator is a
#: new way to split an identity that was never compound.
_ORDER_SEPARATORS = (",", "/")
#: Structural words in a compound key that name no analyte, dropped so a component can
#: match the result that answered it. Deliberately tiny, for the same reason.
_ORDER_NOISE_WORDS = frozenset({"panel", "profile", "extensive"})

# `condition.status` buckets, one rendered section each (family history last: it is
# context about relatives, not the patient's own record).
CONDITION_ACTIVE = ("active",)
CONDITION_PAST = ("resolved", "history")
CONDITION_FAMILY = "family-history"

# Default window for "recent labs" in an appointment brief (last N most recent).
_BRIEF_RECENT_LABS = 10

#: Age bound for the summary's abnormal-labs section (issue #165). Unlike
#: `_BRIEF_RECENT_LABS` above this bounds *age*, not count: the summary is the document
#: read between visits, and unbounded it renders the whole abnormal history -- on a real
#: record 250 of 475 lines across 16 years, in which a marker abnormal now reads exactly
#: like one abnormal a decade ago. 6 months was measured against real data and rejected:
#: for a person on a slower draw cadence (most recent abnormal draw ~7 months old) it
#: renders the section *empty*, and an empty section reads as "nothing flagged" -- a worse
#: miss than the dump it bounds. The window controls volume; the keep-latest guard in
#: `_abnormal_labs` controls that false-empty, which recurs at *any* fixed window.
_ABNORMAL_LABS_WINDOW_MONTHS = 12


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


def _as_date(value: object) -> date | None:
    """:func:`_date_part` parsed to a real date; ``None`` for empty or malformed input.

    Never raises. ``observation.observed_at`` is nullable and free-form-ish and
    ``lab_result.collected_at`` is only ``NOT NULL``, not validated to ISO -- a
    malformed date must not crash a summary, it must merely fail to close an order.
    """
    text = _date_part(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _months_before(day: date, months: int) -> date:
    """``day`` shifted back ``months`` calendar months, day-of-month clamped *down* to the
    target month's last day (2028-02-29 -> 2027-02-28).

    Never raises: like :func:`_as_date` this sits on a render path, so it resolves every
    input to a real date rather than letting a leap day crash a summary.
    """
    index = (day.year * 12 + day.month - 1) - months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


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
# Curation overlay (issue #109) -- a read-time filter, never a write
# --------------------------------------------------------------------------- #

class _CurationPass:
    """One render's view of the `curation` overlay: the verdict map plus the
    out-of-band collection a render builds while filtering its sections.

    One object rather than threading ``verdicts``/``disputed`` through every read
    helper: a render touches ten sections and the two always travel together. It is
    created once per render and is read-only with respect to the DB -- ``render`` never
    writes, and the overlay does not change that.

    ``verdicts`` empty (the overwhelmingly common case, and every pre-008 snapshot) is
    the fast path: :func:`_apply_curation` returns its rows untouched, no row is given a
    ``_curation`` key, and the questions section is omitted -- which is what keeps output
    byte-identical for unannotated data.

    There is no ``appendix`` bucket since issue #168: the audit trail moved to
    :func:`render_curation`, which reads the stored verdict table directly, and keeping a
    write-only bucket here would cost a ``row_label``/``family_label`` query per verdict
    on every curated render for nothing. The :data:`curation.APPENDIX_STATUSES` *filter*
    is unaffected -- it keys off :func:`curation.is_appendix`, not off this bucket.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.verdicts = curation.load_verdicts(conn)
        # Keyed by (record_type, dedup_base, record_id) so a multi-occurrence family is
        # listed once however many of its rows a section selected -- while two rows of
        # one family carrying *different* row-scoped verdicts (issue #114) are two
        # entries rather than one swallowing the other. Insertion-ordered.
        self.disputed: dict[tuple[str, str, int], dict] = {}

    def _entry(self, verdict: dict) -> dict:
        record_type = verdict["record_type"]
        if verdict["record_id"]:
            label, _live_base, family_size = curation.row_label(
                self.conn, record_type, verdict["record_id"]
            )
        else:
            label, family_size = curation.family_label(
                self.conn, record_type, verdict["dedup_base"]
            )
        return dict(verdict, label=label, family_size=family_size)

    def record(self, verdict: dict) -> None:
        """File a ``disputed`` verdict for the questions section, once.

        Identity comes off the verdict itself rather than off the row that matched it:
        the key must be the verdict's *scope* (family or this one row), and passing the
        matching row's id alongside a family-scoped verdict would split one family into
        a question line per occurrence.

        Every other status is filed nowhere. ``confirmed`` records agreement, so it
        neither leaves its section nor raises a question; the
        :data:`curation.APPENDIX_STATUSES` verdicts do leave their section, but their
        audit trail is :func:`render_curation`'s job (issue #168), read from the stored
        table rather than accumulated here.
        """
        if verdict["status"] != "disputed":
            return
        key = (verdict["record_type"], verdict["dedup_base"], verdict["record_id"])
        if key not in self.disputed:
            self.disputed[key] = self._entry(verdict)


def _apply_curation(
    rows: list[dict], record_type: str, cur: "_CurationPass | None"
) -> list[dict]:
    """Filter one section's rows through the overlay, before any grouping.

    Returns the rows that still render, stamping each annotated survivor with its
    verdict under ``_curation``; rows whose verdict is in
    :data:`curation.APPENDIX_STATUSES` are dropped from the section, their audit trail
    left to :func:`render_curation` (issue #168).

    Resolution is **per row**, not per family (issue #114): a row-scoped verdict applies
    to its own occurrence and a family-scoped one to every row that has no verdict of its
    own, all decided in :meth:`curation.VerdictMap.for_row`. Every section selects
    ``SELECT *``, so the row id needed for that is already in hand.

    Called immediately after each section's ``SELECT`` and **before** any latest-wins,
    grouping or top-N logic, so a superseded reading can neither win "latest" nor
    consume a slot in a truncated list.

    The stamping itself is :func:`curation.annotate_rows` (issue #131) — shared with
    `pemr query`, so the two verbs cannot drift apart about which rows carry which
    verdict. What stays here is the *policy*: drop the appendix-bound rows. Such a row is
    stamped a moment before it is dropped, which is invisible (the row is discarded) but
    is why this is a filter over stamped rows rather than a stamp-only-survivors loop.
    """
    if cur is None or not cur.verdicts:
        return rows
    return [
        row
        for row in curation.annotate_rows(
            rows, record_type, cur.verdicts, on_verdict=cur.record
        )
        if not curation.is_appendix(row)
    ]


def _apply_curation_events(
    events: list[dict], cur: "_CurationPass | None"
) -> list[dict]:
    """:func:`_apply_curation` for timeline events.

    Same rule, different carrier: an event is a rendered sentence rather than a row, and
    it only carries ``record_type``/``dedup_base``/``record_id`` when
    :func:`query.query_timeline` was asked for them (``with_identity``). An event
    without identity is passed through -- that is the no-verdicts fast path, where the
    journal never asks for the extra keys in the first place.

    Stamping is :func:`curation.annotate_events`, shared with `pemr query timeline`
    (issue #131); the drop policy stays here, as in :func:`_apply_curation`.
    """
    if cur is None or not cur.verdicts:
        return events
    return [
        event
        for event in curation.annotate_events(
            events, cur.verdicts, on_verdict=cur.record
        )
        if not curation.is_appendix(event)
    ]


def _dispute_suffix(row: dict) -> str:
    """``  [DISPUTED: <note>]`` for a disputed row, ``""`` otherwise.

    A disputed fact stays in place -- silently moving it to an appendix would hide a
    value a clinician is still acting on -- so every line builder marks it instead.
    """
    verdict = row.get(curation.CURATION_FIELD) if isinstance(row, dict) else None
    if verdict is not None and verdict["status"] == "disputed":
        return f"  [DISPUTED: {verdict['note']}]"
    return ""


def _attest_suffix(row: dict) -> str:
    """``  (attested by <who> <date>; no source document)`` for a live attestation,
    ``""`` otherwise (issue #110).

    The single most important failure mode this feature can have is an unsourced fact
    rendering identically to a document-sourced one, so the mitigation is structural: one
    predicate (:func:`dedup.is_attested`), one suffix builder, appended at **every** line
    builder that renders a typed-table row. A *superseded* attestation is deliberately
    unmarked - a real document backs it now, and its attestation survives as history on
    the row, not as a caveat on the page.

    Placed after :func:`_dispute_suffix` wherever both apply: the verdict is about the
    fact, provenance is about where the fact came from, and provenance reads last.
    """
    if not isinstance(row, dict) or not is_attested(row):
        return ""
    when = str(row.get("attested_on") or "").strip()
    stamp = f" {when}" if when else ""
    return f"  (attested by {row['attested_by']}{stamp}; no source document)"


def _unit_suffix(d: units.Displayed) -> str:
    """``  [converted from 77.6 kg]`` for a display-converted number, ``""`` otherwise
    (issue #136).

    The :func:`_attest_suffix` shape, for the same reason: a derived number must never
    read as the one the document printed. One predicate
    (:attr:`units.Displayed.converted`), one suffix builder, appended at every line that
    can carry a converted value -- and appended **last**, after
    :func:`_dispute_suffix`/:func:`_attest_suffix`, so existing suffix ordering is
    untouched.
    """
    if not d.converted:
        return ""
    return f"  [converted from {_fmt(d.source_value)} {d.source_unit}]"


def _target_unit(
    prefs: dict[str, str], name: object, dictionary: dict[str, str] | None
) -> str | None:
    """This person's canonical display unit for one measurement name, or ``None``.

    Keyed by the same :func:`key_token` the vitals fold and the dedup key group on, so
    a preference set as ``--key A1c`` reaches rows stored as ``HbA1c``. An empty
    ``prefs`` short-circuits before the token is even derived -- the fast path that
    keeps an unset person's render byte-identical *and* free.
    """
    if not prefs:
        return None
    return prefs.get(key_token(name, dictionary))


def _converted_lab(row: dict, d: units.Displayed) -> dict:
    """Shallow copy of a lab row with its value **and reference bounds** in ``d``'s unit.

    The bounds are not optional: a value printed as ``171.08 lb`` beside a
    ``(ref 70-100)`` still in kilograms is a clinical misread. They move in the same
    step, through the same converter, or not at all.

    Only the display copy is built -- the stored row is never touched, and
    :func:`_is_abnormal` keeps reading the original, so no preference can change which
    labs appear in a section.
    """
    bounds: dict[str, object] = {}
    for name in ("ref_low", "ref_high"):
        raw = row[name]
        moved = None if raw is None else units.convert(raw, d.source_unit, d.unit)
        bounds[name] = raw if moved is None else units.round_display(moved)
    return dict(row, value_num=d.value, unit=d.unit, **bounds)


def _questions_section(entries: dict[tuple[str, str, int], dict]) -> str | None:
    """The ``## Questions for the Clinician`` section, or ``None`` when empty.

    A ``disputed`` verdict is a recorded "two sources disagree and a human has to
    rule", so the brief -- the document actually handed over at the appointment --
    surfaces it as a question rather than leaving it as an inline marker only.
    """
    if not entries:
        return None
    lines = []
    for (record_type, _base, _record_id), entry in entries.items():
        label = entry["label"] or "(no live rows)"
        who = f" ({entry['attributed_to']})" if entry.get("attributed_to") else ""
        lines.append(f"- {record_type}: {label} - {entry['note']}{who}")
    return _section("Questions for the Clinician", lines)


# --------------------------------------------------------------------------- #
# Read helpers (pure SELECTs; person already resolved to an id)
# --------------------------------------------------------------------------- #

def _conditions(
    conn: sqlite3.Connection,
    person_id: int,
    statuses: tuple[str, ...] | str,
    cur: "_CurationPass | None" = None,
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
    rows = _apply_curation([dict(r) for r in rows], "condition", cur)
    return [r for r in rows if enum_token(r["status"]) in wanted]


def _allergies(
    conn: sqlite3.Connection, person_id: int, cur: "_CurationPass | None" = None
) -> list[dict]:
    """Allergy rows, ``criticality='high'`` first then alphabetical -- the dangerous ones
    have to survive a skim of the list."""
    rows = conn.execute(
        "SELECT * FROM allergy WHERE person_id = ? ORDER BY substance, allergy_id",
        (person_id,),
    ).fetchall()
    return sorted(
        _apply_curation([dict(r) for r in rows], "allergy", cur),
        key=lambda r: enum_token(r["criticality"]) != "high",
    )


def _condition_line(row: dict, *, past: bool = False) -> str:
    since = f"  (since {row['onset_on']})" if row["onset_on"] else ""
    resolved = (
        f"  (resolved {row['resolved_on']})" if past and row["resolved_on"] else ""
    )
    note = f" - {row['note']}" if row["note"] else ""
    return (
        f"- {row['name']}{since}{resolved}{note}"
        f"{_dispute_suffix(row)}{_attest_suffix(row)}"
    )


def _allergy_line(row: dict) -> str:
    crit = f" [{str(row['criticality']).upper()}]" if row["criticality"] else ""
    reaction = f" - {row['reaction']}" if row["reaction"] else ""
    noted = f"  (noted {row['noted_on']})" if row["noted_on"] else ""
    return (
        f"- {row['substance']}{crit}{reaction}{noted}"
        f"{_dispute_suffix(row)}{_attest_suffix(row)}"
    )


def _latest_vitals(
    conn: sqlite3.Connection,
    person_id: int,
    dictionary: dict[str, str] | None,
    cur: "_CurationPass | None" = None,
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
    # Curation runs before the latest-wins fold, so a superseded reading cannot win
    # "latest" and hide the good one behind it.
    kept = _apply_curation([dict(r) for r in rows], "observation", cur)
    latest: dict[str, dict] = {}
    for r in kept:
        latest[key_token(r["key"], dictionary)] = r  # ascending -> last wins
    return [latest[k] for k in sorted(latest)]


def _order_display(row: dict) -> str:
    """Display name of an order row: the item name, falling back to the detail text and
    then to an explicit placeholder (an unnamed order still has to be visible)."""
    return row["key"] or row["value_text"] or "(unspecified)"


def _result_index(
    conn: sqlite3.Connection,
    person_id: int,
    dictionary: dict[str, str] | None,
    cur: "_CurationPass | None" = None,
) -> dict[str, list[date]]:
    """``key_token`` -> collection dates of this person's live ``lab_result`` rows.

    The lookup side of the order/result linkage (issue #128). Keyed by the *same*
    :func:`key_token` -- and the same ``dictionary`` -- the order grouping folds on, so
    matching can never disagree with #71/#93 about what counts as one item: a
    qualifier-bearing order (``HbA1c (POC)``) is not closed by a plain-stem result.

    Scoped to ``person_id``: one person's results can never close another's order.

    Verdicts are read **directly** rather than through :func:`_apply_curation`, on
    purpose: a result in :data:`curation.APPENDIX_STATUSES` must not close an order, and
    this helper only needs to *ask*, not to file anything. Reading without recording keeps
    the overlay additive. ``disputed``/``confirmed``/``distinct`` results still count:
    they are live rows.
    """
    index: dict[str, list[date]] = {}
    rows = conn.execute(
        "SELECT * FROM lab_result WHERE person_id = ?", (person_id,)
    ).fetchall()
    annotated = cur is not None and bool(cur.verdicts)   # fast path: no verdicts at all
    for raw in rows:
        row = dict(raw)
        if annotated:
            verdict = cur.verdicts.for_row(
                "lab_result", row["dedup_base"], row.get("lab_result_id")
            )
            if verdict is not None and verdict["status"] in curation.APPENDIX_STATUSES:
                continue
        token = key_token(row["test_name"], dictionary)
        when = _as_date(row["collected_at"])
        if token and when:
            index.setdefault(token, []).append(when)
    return index


def _is_resulted(
    token: str, observed_at: object, index: dict[str, list[date]]
) -> bool:
    """Has a ``lab_result`` landed that plausibly answers this order? (issue #128)

    An unusable identity token and an unusable order date both answer *no*: the section
    exists to surface still-open orders, so every ambiguity resolves to "render".
    Matching is exact-token plus a bounded window around the order date -- never a
    substring match, never an unbounded window, and never age alone.
    """
    ordered = _as_date(observed_at)
    if not token or ordered is None:
        return False
    return any(
        -ORDER_RESULT_BACKDATE_DAYS <= (d - ordered).days <= ORDER_RESULT_WINDOW_DAYS
        for d in index.get(token, ())
    )


def _split_components(text: str) -> list[str]:
    """``text`` split on :data:`_ORDER_SEPARATORS` occurring at parenthesis depth 0.

    Parts are stripped and empties dropped, so a leading/trailing/doubled separator
    contributes nothing. Depth is clamped at 0: a stray ``)`` (OCR loses brackets) must
    not drive it negative and start splitting inside a later parenthetical. A
    parenthetical is identity-bearing (issue #71), so a separator inside one is content,
    not structure -- ``SLE Profile (Profile A, Scleroderma)`` is one component, not two.

    Never raises: like :func:`_as_date`, a malformed key must render, not crash.
    """
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and ch in _ORDER_SEPARATORS:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return [p for p in (part.strip() for part in parts) if p]


def _declared_analyte(text: str, dictionary: dict[str, str] | None) -> bool:
    """Does the dictionary declare ``text`` -- separators and all -- as **one** analyte?

    ``,`` and ``/`` are structure in a panel key (``cbc,cmp,ldh``) but *content* in many
    single analytes' canonical labels: ``Glucose, fasting``, ``Cholesterol, Total``,
    ``Kappa/Lambda Ratio``. Decomposing one of those asks for two analytes that were
    never ordered and no result can answer, so the order never suppresses -- the #128
    inversion, re-created for a different class of key. The dictionary already knows
    which strings are whole names: this asks it before :func:`_split_components` runs.

    The question is asked through :func:`norm`, not by looking keys up directly, because
    :func:`identity`'s two synonym rules (a declared *full label*, else the
    qualifier-stripped *stem*) are what decide the matter, and only ``norm`` speaks for
    both -- so ``Cholesterol, Total (Calculated)`` is recognized by its declared stem.
    A hit shows up as a canonical token the bare normalization would not have produced.
    Qualifier-only synonyms deliberately do **not** count: ``norm`` drops the qualifier,
    so ``Sodium, Potassium (POC)`` still decomposes as the panel it is.

    Conservative both ways: no dictionary means nothing is declared (decompose, as
    before), and a false positive only costs suppression -- which is the safe side.
    """
    if not dictionary:
        return False
    return norm(text, dictionary) != norm(text)


def _order_tokens(
    value: object, dictionary: dict[str, str] | None = None
) -> tuple[str, ...]:
    """An order's identity token **set** -- one token per analyte it names (issue #145).

    ``_is_resulted`` compares a single :func:`key_token`, so a *panel* order (one key
    naming several analytes: ``cbc,cmp,ldh``) yields one token no single-analyte result
    can ever equal, and the order never leaves the section however completely it was
    resulted. Decomposing the order side fixes that without loosening the match itself:
    every component is still compared exact-token, never by substring.

    Only the **order** side decomposes. A ``lab_result.test_name`` names one analyte by
    construction, so splitting it would invent components that were never ordered.

    A key with no top-level separator returns exactly today's single token -- original
    text, no word stripping -- so single-analyte behaviour is unchanged by construction,
    not merely by test. So does a key the dictionary declares as one analyte despite its
    separators (:func:`_declared_analyte`): ``Glucose, fasting`` is a name, not a panel.
    Noise-word removal applies only to a key that actually decomposed, and never inside a
    parenthetical (see :func:`_split_components`).

    ``()`` for an empty or wholly unusable key, which :func:`_all_resulted` reads as
    "nothing known" -> render. Decomposition **fails closed** on the same rule: if any one
    component tokenizes empty -- it was entirely noise words (``CBC, Extensive Panel``), or
    whitespace-equivalent after normalization -- the whole key returns ``()``. Dropping just
    that component would silently narrow all-or-nothing to all-*remaining* and let the
    surviving analytes suppress an order naming something this layer could not read, which
    is the one direction (#128's inversion) the section must never fail in.
    """
    if value is None:
        return ()
    text = str(value).strip()
    if not text:
        return ()
    parts = _split_components(text)
    if len(parts) <= 1 or _declared_analyte(text, dictionary):
        token = key_token(text, dictionary)
        return (token,) if token else ()
    tokens: list[str] = []
    for part in parts:
        if "(" not in part:
            part = " ".join(
                w for w in part.split() if w.lower() not in _ORDER_NOISE_WORDS
            )
        token = key_token(part, dictionary)
        if not token:
            return ()                            # one unreadable component -> render
        tokens.append(token)
    return tuple(dict.fromkeys(tokens))          # de-dup, order preserved


def _all_resulted(
    tokens: tuple[str, ...], observed_at: object, index: dict[str, list[date]]
) -> bool:
    """Is **every** analyte this order names already resulted? (issue #145)

    All-or-nothing, composing :func:`_is_resulted` per component: one component still
    outstanding keeps the whole panel rendering, because a partially resulted panel *is*
    outstanding work. An empty token set answers no, on the same "ambiguity renders"
    rule as an unusable single token.
    """
    if not tokens:
        return False
    return all(_is_resulted(token, observed_at, index) for token in tokens)


def _order_resulted(
    source: object,
    observed_at: object,
    index: dict[str, list[date]],
    dictionary: dict[str, str] | None = None,
) -> bool:
    """Has this order been answered -- as a whole, or analyte by analyte? (issues #128, #145)

    The two questions are a **union**, asked whole-key first, and that order is the point:
    the whole-key arm is #128's rule verbatim, so nothing it used to suppress can stop
    suppressing here whatever the decomposition does with the same text. That matters for
    any single analyte whose own name carries a ``,`` or ``/`` -- ``Ferritin, Serum``
    ordered and ``Ferritin, Serum`` resulted -- including the ones no dictionary declares
    (:func:`_declared_analyte` can only speak for the ones it knows) and every render that
    runs without a dictionary at all.

    Only when that fails does the panel question run, and only then can decomposition
    change an outcome -- always from "renders" toward "suppressed", never the reverse.
    """
    if _is_resulted(key_token(source, dictionary), observed_at, index):
        return True
    return _all_resulted(_order_tokens(source, dictionary), observed_at, index)


def _grouped_orders(
    conn: sqlite3.Connection,
    person_id: int,
    dictionary: dict[str, str] | None,
    cur: "_CurationPass | None" = None,
) -> list[dict]:
    """Still-open ``obs_type='order'`` rows, folded per normalized item, oldest first.

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

    A group whose item already has a matching ``lab_result`` is then dropped entirely
    (issue #128): the section is a list of things still outstanding, and before this it
    listed every order ever placed -- inverting its meaning, since most were resulted on
    the order date itself. The question is asked **after** the fold, of the group's
    *latest* row -- the live restatement the bullet already speaks for -- so
    ``group_count``/``+N earlier`` disclosure is unchanged for everything that still
    renders. Referral-type orders have no ``lab_result`` by construction and so are
    never suppressed by this; closing them needs a mechanism that does not exist yet.

    A **compound** key -- one order naming several analytes (``cbc,cmp,ldh``) -- is asked
    the same question per component (issue #145): the group's identity text decomposes to
    a token *set* (:func:`_order_tokens`) and the order drops when every one of them
    resulted in window, *or* when the key as a whole did (:func:`_order_resulted`, which
    keeps #128's rule reachable for a single analyte whose own name carries a separator).
    Grouping still folds on the single :func:`key_token`, so #93's ``+N earlier``
    disclosure is untouched; only the suppression question changed.
    """
    rows = conn.execute(
        "SELECT * FROM observation WHERE person_id = ? AND obs_type = ? "
        "ORDER BY observed_at, observation_id",
        (person_id, OBS_ORDER),
    ).fetchall()
    # Before the grouping fold: a superseded order must not become the group's
    # "latest" row and speak for the ones behind it.
    kept = _apply_curation([dict(r) for r in rows], "observation", cur)
    groups: dict[object, list[dict]] = {}
    for r in kept:
        token = key_token(r["key"], dictionary) or key_token(r["value_text"], dictionary)
        groups.setdefault(token or ("", r["observation_id"]), []).append(r)

    # Carry each group's identity *text* alongside it: the suppression lookup asks about
    # the very text the fold grouped on -- as a whole (issue #128) and decomposed per
    # analyte (issue #145). A keyless group (identity is the `("", observation_id)`
    # fallback tuple) carries `None` and is never suppressed.
    folded: list[tuple[object, dict]] = []
    for identity, members in groups.items():
        latest = members[-1]                      # ascending -> last is the latest
        earlier = sorted(
            d for d in (_date_part(m["observed_at"]) for m in members[:-1]) if d
        )
        source = None
        if isinstance(identity, str):
            # Same test the fold used, so suppression speaks for the same text.
            source = (latest["key"] if key_token(latest["key"], dictionary)
                      else latest["value_text"])
        folded.append((
            source,
            dict(latest, group_count=len(members),
                 group_first=earlier[0] if earlier else ""),
        ))

    # Drop what a result already answered (issue #128), asking the group's latest row --
    # and, for a compound key, only when every analyte it names resulted (issue #145).
    index = _result_index(conn, person_id, dictionary, cur) if folded else {}
    out = [g for source, g in folded
           if source is None
           or not _order_resulted(source, g["observed_at"], index, dictionary)]

    # Two stable passes: alphabetical, then order-date ascending (undated sorts last,
    # keeping its alphabetical order). This section diverges from the other event
    # sections' newest-first on purpose (issue #128): what is left after suppression is
    # what is still outstanding, and an order still open after years is the *most*
    # actionable row on the page, not the least.
    out.sort(key=lambda g: (_order_display(g).lower(), g["observation_id"]))
    out.sort(key=lambda g: (not _date_part(g["observed_at"]),
                            _date_part(g["observed_at"])))
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
    # `_grouped_orders` folds a group to its newest member, so the suffix describes the
    # displayed row - the same rule `_dispute_suffix` already follows here.
    return (
        f"- {_order_display(row)}{detail}{note}"
        f"{_dispute_suffix(row)}{_attest_suffix(row)}"
    )


def _abnormal_labs(
    conn: sqlite3.Connection,
    person_id: int,
    today: str,
    cur: "_CurationPass | None" = None,
) -> list[dict]:
    """Abnormal results, newest first, bounded to the last
    ``_ABNORMAL_LABS_WINDOW_MONTHS`` months of ``today`` (issue #165).

    Four selection rules, applied in the query's newest-first order to rows the curation
    overlay and :func:`_is_abnormal` have already decided on -- the window never
    resurrects a row a verdict removed, and never changes what counts as abnormal:

    * a row collected **on or after** the cutoff renders (the boundary is inclusive);
    * the most recent abnormal result for an analyte (``test_name``) always renders, even
      when it falls outside the window -- the *keep-latest guard*, without which a person
      on a slow draw cadence sees an empty section while their marker is live and
      abnormal;
    * a row whose ``collected_at`` will not parse renders anyway: an undated row cannot be
      *proven* stale, and the fail-open posture of :func:`_as_date` / :func:`_is_resulted`
      holds here too -- a bad date must never silently empty a medical section. An
      unparseable ``today`` skips the bound entirely, for the same reason;
    * a future-dated row is untouched; only the old side is bounded.
    """
    rows = conn.execute(
        # lab_result_id DESC breaks same-timestamp ties (a `--keep both` sibling shares
        # its date): newest-first ordering treats the later row id as the later point.
        "SELECT * FROM lab_result WHERE person_id = ? "
        "ORDER BY collected_at DESC, test_name, lab_result_id DESC",
        (person_id,),
    ).fetchall()
    kept = _apply_curation([dict(r) for r in rows], "lab_result", cur)
    abnormal = [r for r in kept if _is_abnormal(r)]
    anchor = _as_date(today)
    if anchor is None:
        return abnormal
    start = _months_before(anchor, _ABNORMAL_LABS_WINDOW_MONTHS)
    # Filtered in place, never re-sorted: the query's ordering is load-bearing both for
    # the same-date sibling rule (#58) and for "first row seen for an analyte" being that
    # analyte's most recent -- which is what makes the guard a single pass.
    out: list[dict] = []
    seen: set[str] = set()
    for row in abnormal:
        when = _as_date(row["collected_at"])
        if when is None or when >= start or row["test_name"] not in seen:
            out.append(row)
        seen.add(row["test_name"])
    return out


def _routine_matcher(patterns: Sequence[str] | None) -> re.Pattern[str] | None:
    """One compiled, token-boundary alternation over already-normalized routine-procedure
    patterns -- or ``None`` when there is nothing to suppress (issue #166).

    ``None`` is both the fast path and the default: no list, no compile, no filtering, so
    a caller that passes nothing renders every procedure exactly as it would have before
    the section learned to narrow.

    The boundary is the point. A bare substring test would let ``cast`` suppress
    ``Castration``, and over-suppression is precisely what the default-show rule exists to
    prevent -- a procedure nobody anticipated must never vanish silently, while a routine
    one that slips through merely costs a line. After :func:`norm` the alphabet is
    ``[a-z0-9]`` plus punctuation, so a lookaround pair on the alphanumerics is a word
    boundary that still lets a pattern begin or end on punctuation (``x-ray``).
    """
    cleaned = [p for p in (patterns or ()) if p]
    if not cleaned:
        return None
    alternation = "|".join(re.escape(p) for p in cleaned)
    return re.compile(rf"(?<![a-z0-9])(?:{alternation})(?![a-z0-9])")


def _is_routine(name: object, matcher: re.Pattern[str] | None) -> bool:
    """True when a procedure ``name`` matches a configured routine pattern.

    Normalized with **no dictionary** (as the patterns were, on load): ``[synonyms]`` is
    lab-analyte vocabulary and must never rewrite a procedure name on either side of the
    match.
    """
    return matcher is not None and bool(matcher.search(norm(name)))


def _procedures(
    conn: sqlite3.Connection, person_id: int, cur: "_CurationPass | None" = None
) -> list[dict]:
    """Procedure rows, newest first, undated last -- the summary's ``## Procedures``
    source (issue #166).

    ``performed_on`` is nullable *and* unvalidated free text, so the SQL ordering is not
    trusted to place undated rows on its own: SQLite's ``DESC`` puts ``NULL`` last but
    sorts ``''`` in among the dated rows. One extra **stable** pass on
    :func:`_date_part`'s truthiness moves every undated row -- ``NULL`` and ``''`` alike --
    behind the dated ones without disturbing their relative order.

    Curation runs first, as everywhere else: an appendix-bound verdict must route the row
    before the routine filter (applied by the caller) ever sees it, or a superseded row
    would be reported twice.
    """
    rows = conn.execute(
        "SELECT * FROM procedure WHERE person_id = ? "
        "ORDER BY performed_on DESC, procedure_id",
        (person_id,),
    ).fetchall()
    kept = _apply_curation([dict(r) for r in rows], "procedure", cur)
    kept.sort(key=lambda p: not _date_part(p["performed_on"]))
    return kept


def _procedure_line(row: dict) -> str:
    """One ``## Procedures`` bullet. No ``procedure:`` prefix -- the brief needs one
    because its section is shared with observations, a dedicated section does not."""
    outcome = f" - {row['outcome']}" if row["outcome"] else ""
    who = f"  ({row['provider']})" if row["provider"] else ""
    return (
        f"- {_date_part(row['performed_on']) or '(undated)'}  {row['name']}"
        f"{outcome}{who}{_dispute_suffix(row)}{_attest_suffix(row)}"
    )


def _open_appointments(
    conn: sqlite3.Connection,
    person_id: int,
    today: str,
    cur: "_CurationPass | None" = None,
) -> list[dict]:
    """Upcoming (scheduled on/after ``today``) or open (no post-visit ``summary``)
    appointments -- the ones a summary reader still needs to act on."""
    rows = conn.execute(
        "SELECT * FROM appointment WHERE person_id = ? "
        "ORDER BY (scheduled_for IS NULL), scheduled_for, appointment_id",
        (person_id,),
    ).fetchall()
    out: list[dict] = []
    for r in _apply_curation([dict(x) for x in rows], "appointment", cur):
        d = _date_part(r["scheduled_for"])
        upcoming = bool(d) and d >= today
        is_open = not (r["summary"] or "").strip()
        if upcoming or is_open:
            out.append(r)
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


def _open_conflicts_warning(conn: sqlite3.Connection, person_id: int) -> str | None:
    """The summary's one-line open-conflicts warning, or ``None`` at zero (issue #168).

    #59's safety property, at a section's worth less page: an open conflict means the
    summary is *currently displaying* a value a staged correction disputes, so the
    document has to say so -- but it does not need a per-conflict list to say it, and an
    always-present ``## Open Conflicts`` / ``_none_`` header on the common case was
    exactly the noise this issue is about. The per-conflict lines survive in the brief
    (:func:`_open_conflict_lines`, unchanged) and in `pemr review-conflicts`.

    Not built with :func:`_section` for :func:`_questions_section`'s reason: emptiness
    here is not information, it is the normal case.
    """
    count = len(_open_conflict_lines(conn, person_id))
    if not count:
        return None
    plural = "" if count == 1 else "s"
    return (
        "> [!WARNING]\n"
        f"> {count} open conflict{plural} - some values below may be superseded. See "
        "curation.md, or run\n"
        "> `pemr review-conflicts`.\n"
    )


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
    routine_procedures: Sequence[str] | None = None,
    now: datetime | None = None,
) -> str:
    """Markdown master summary for a person: active meds, active problems, past medical
    history, family history, allergies, orders, latest vitals, recent abnormal labs,
    procedures and upcoming/open appointments --
    with a self-identifying header (name, DOB, generated-at, source row counts).
    Read-only.

    The summary is the document read *between* appointments, so an open conflict has to
    surface here too: without it a staged correction is invisible and the summary prints
    the stale value with no hint that a corrected one is pending (issue #59). Since issue
    #168 it says so in one :func:`_open_conflicts_warning` line under the header, emitted
    only when the count is non-zero, rather than an always-present section. The curation
    audit trail is no longer inlined here either -- it is :func:`render_curation`.

    ``routine_procedures`` is the ``[procedures].routine`` pattern list (issue #166,
    module docstring). It is **suppress-only** and defaults to ``None``, which suppresses
    nothing -- so every caller that does not pass one gets default-show behavior for free.
    A section that filtered anything says so; a section that filtered *everything* says
    that too, rather than the false ``_none recorded_``.

    Raises :class:`query.PersonNotFoundError` for an unknown slug (friendly rc=1)."""
    person_id = query.resolve_person_id(conn, slug)
    person = conn.execute(
        "SELECT * FROM person WHERE person_id = ?", (person_id,)
    ).fetchone()
    today = _date_part(now or datetime.now())
    counts = _row_counts(conn, person_id)
    # Source rows stay a count of what is *stored*: the overlay hides nothing from the
    # database, only from the sections below.
    cur = _CurationPass(conn)
    # The display-unit overlay (issue #136), read once alongside the curation one. Empty
    # -- no preferences, or a pre-013 snapshot -- is the fast path: every conversion
    # branch below is a no-op, which is what keeps an unset person's output identical.
    prefs = units.load_prefs(conn, person_id)

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

    meds = _apply_curation(
        query.query_meds(conn, slug, active=True, now=now), "medication", cur
    )
    med_lines = []
    for m in meds:
        dose = f" {m['dose']}" if m["dose"] else ""
        freq = f" {m['frequency']}" if m["frequency"] else ""
        since = f" (since {m['started_on']})" if m["started_on"] else ""
        med_lines.append(
            f"- {m['name']}{dose}{freq}{since}{_dispute_suffix(m)}{_attest_suffix(m)}"
        )

    active_lines = [
        _condition_line(c) for c in _conditions(conn, person_id, CONDITION_ACTIVE, cur)
    ]
    past_lines = [
        _condition_line(c, past=True)
        for c in _conditions(conn, person_id, CONDITION_PAST, cur)
    ]
    family_lines = [
        f"- {c['relation'] or 'family'}: {c['name']}"
        f"{_dispute_suffix(c)}{_attest_suffix(c)}"
        for c in _conditions(conn, person_id, CONDITION_FAMILY, cur)
    ]
    allergy_lines = [_allergy_line(a) for a in _allergies(conn, person_id, cur)]
    order_lines = [
        _order_line(o) for o in _grouped_orders(conn, person_id, dictionary, cur)
    ]

    vital_lines = []
    for v in _latest_vitals(conn, person_id, dictionary, cur):
        # A `value_text`-only vital has no number to convert, so `display` passes it
        # through and the line is built exactly as before.
        d = units.display(
            v["value_num"], v["unit"], _target_unit(prefs, v["key"], dictionary)
        )
        value = d.value if v["value_num"] is not None else v["value_text"]
        unit = f" {d.unit}" if d.unit else ""
        when = f"  ({_date_part(v['observed_at'])})" if v["observed_at"] else ""
        vital_lines.append(
            f"- {v['key']}: {_fmt(value)}{unit}{when}"
            f"{_dispute_suffix(v)}{_attest_suffix(v)}{_unit_suffix(d)}"
        )

    lab_lines = []
    for r in _abnormal_labs(conn, person_id, today, cur):
        d = units.display(
            r["value_num"], r["unit"], _target_unit(prefs, r["test_name"], dictionary)
        )
        shown = _converted_lab(r, d) if d.converted else r
        flag = f" [{r['flag']}]" if r["flag"] else ""
        lab_lines.append(
            f"- {_date_part(r['collected_at'])}  {r['test_name']}  "
            f"{_lab_value(shown)}{flag}{_ref_range(shown)}"
            f"{_dispute_suffix(r)}{_attest_suffix(r)}{_unit_suffix(d)}"
        )

    # Curation first, routine filter second (issue #166): an appendix-bound row has to be
    # routed by its verdict before the pattern list sees it, or the disclosure count below
    # would report a superseded row as a hidden routine one.
    proc_rows = _procedures(conn, person_id, cur)
    matcher = _routine_matcher(routine_procedures)
    proc_kept = [p for p in proc_rows if not _is_routine(p["name"], matcher)]
    proc_lines = [_procedure_line(p) for p in proc_kept]
    hidden = len(proc_rows) - len(proc_kept)
    if hidden:
        note = (
            f"_{hidden} routine procedure{'' if hidden == 1 else 's'} not shown "
            "(name matches the dictionary's routine list); "
            "`pemr render journal` lists them._"
        )
        proc_lines.append(f"\n{note}" if proc_lines else note)

    appt_lines = [
        f"- {_appt_line(a)}{_dispute_suffix(a)}{_attest_suffix(a)}"
        for a in _open_appointments(conn, person_id, today, cur)
    ]

    # Immediately after the header, not at the foot: the wording says "some values
    # *below* may be superseded", and a warning at the end of the document warns nobody.
    parts = [
        header,
        *(w for w in (_open_conflicts_warning(conn, person_id),) if w is not None),
        _section("Active Medications", med_lines),
        _section("Active Problems", active_lines),
        _section("Past Medical History", past_lines),
        _section("Family History", family_lines),
        _section("Allergies", allergy_lines),
        _section("Orders & Referrals", order_lines),
        _section("Latest Vitals", vital_lines),
        _section(
            f"Abnormal Labs (last {_ABNORMAL_LABS_WINDOW_MONTHS} months)",
            lab_lines,
            empty="_none flagged_",
        ),
        _section("Procedures", proc_lines),
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
    cur = _CurationPass(conn)

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

    meds = _apply_curation(
        query.query_meds(conn, person["slug"], active=True, now=now), "medication", cur
    )
    med_lines = []
    for m in meds:
        dose = f" {m['dose']}" if m["dose"] else ""
        freq = f" {m['frequency']}" if m["frequency"] else ""
        med_lines.append(
            f"- {m['name']}{dose}{freq}{_dispute_suffix(m)}{_attest_suffix(m)}"
        )

    # Recency stays the primary axis, but a single draw can carry a 50+ analyte panel —
    # a flat `LIMIT N` over `collected_at DESC, test_name` then returns the
    # alphabetically-first N of that one draw and silently drops every abnormal in it.
    # So rank abnormal-before-normal *within* each collection timestamp before the cut:
    # the brief is the doc handed to a clinician, and the out-of-range values are the
    # ones that must survive truncation. Abnormality is `_is_abnormal` (flag OR
    # reference interval), which SQL can't express, so the cut happens here.
    lab_rows = _apply_curation(
        [
            dict(r) for r in conn.execute(
                "SELECT * FROM lab_result WHERE person_id = ? "
                "ORDER BY collected_at DESC, test_name, lab_result_id DESC",
                (person_id,),
            ).fetchall()
        ],
        "lab_result",
        cur,
    )
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
            f"{_lab_value(r)}{flag}{abnormal}{_dispute_suffix(r)}{_attest_suffix(r)}"
        )

    proc_rows = _apply_curation(
        [
            dict(r) for r in conn.execute(
                "SELECT * FROM procedure WHERE person_id = ? "
                "ORDER BY performed_on DESC, procedure_id",
                (person_id,),
            ).fetchall()
        ],
        "procedure",
        cur,
    )
    obs_rows = _apply_curation(
        [
            dict(r) for r in conn.execute(
                "SELECT * FROM observation WHERE person_id = ? "
                "ORDER BY observed_at DESC, observation_id",
                (person_id,),
            ).fetchall()
        ],
        "observation",
        cur,
    )
    ctx_lines = []
    for p in proc_rows:
        outcome = f" - {p['outcome']}" if p["outcome"] else ""
        ctx_lines.append(
            f"- {_date_part(p['performed_on']) or '(undated)'}  procedure: "
            f"{p['name']}{outcome}{_dispute_suffix(p)}{_attest_suffix(p)}"
        )
    for o in obs_rows:
        value = o["value_num"] if o["value_num"] is not None else o["value_text"]
        val = f" = {_fmt(value)}{(' ' + o['unit']) if o['unit'] else ''}" if value is not None else ""
        detail = " ".join(p for p in (o["obs_type"], o["key"]) if p)
        ctx_lines.append(
            f"- {_date_part(o['observed_at']) or '(undated)'}  {detail}{val}"
            f"{_dispute_suffix(o)}{_attest_suffix(o)}"
        )

    brief_allergy_lines = [_allergy_line(a) for a in _allergies(conn, person_id, cur)]
    brief_problem_lines = [
        _condition_line(c) for c in _conditions(conn, person_id, CONDITION_ACTIVE, cur)
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
    ]
    # Omitted entirely when empty, so an unannotated brief is byte-identical to what it
    # was before the overlay existed. The clinician questions sit immediately before the
    # interaction block: the last thing read is what to ask about. The superseded/
    # corrected appendix used to sit here too; issue #168 dropped it -- what belongs in
    # front of a clinician is the questions, and the audit trail is `render curation`.
    parts += [p for p in (_questions_section(cur.disputed),) if p is not None]
    parts.append(interaction)
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

    Curated-away events simply do not appear; their audit trail is
    :func:`render_curation` (issue #168), not a tail section here.

    Raises :class:`query.PersonNotFoundError` for an unknown slug (friendly rc=1)."""
    person_id = query.resolve_person_id(conn, slug)
    person = conn.execute(
        "SELECT * FROM person WHERE person_id = ?", (person_id,)
    ).fetchone()
    cur = _CurationPass(conn)
    # `with_identity` is what makes the overlay reachable from here: a timeline event
    # is a rendered sentence, not a row, so it carries no family identity by default.
    events = query.query_timeline(
        conn, slug, since=since, with_identity=bool(cur.verdicts)
    )
    events = _apply_curation_events(events, cur)

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
        # An attested event has no document_id, so it takes no [^n] footnote marker and
        # the suffix is the only thing that says where the fact came from (issue #110).
        lines.append(
            f"- **{e['type']}** -- {e['summary']}{ref}"
            f"{_dispute_suffix(e)}{_attest_suffix(e)}"
        )

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


# --------------------------------------------------------------------------- #
# Curation record -- the audit trail as its own target (Architecture.md §6)
# --------------------------------------------------------------------------- #

def _verdict_heading(verdict: dict) -> str:
    """One ruling's ``##`` heading: :func:`curation.describe` **minus the note**.

    Deliberately not ``describe()`` itself. An operator note is free-form text that may
    run to several lines -- the merge-bookkeeping notes issue #168 was raised over do
    exactly that -- and a multi-line ``##`` heading is broken Markdown, so the note goes
    in a blockquote under the heading instead.

    ``merged_into_base`` keeps ``describe()``'s 12-character truncation and carries **no**
    resolved label: a merge target may be another person's family (issue #161), which must
    never leak into this person's document.
    """
    status = verdict.get("status") or ""
    if status == "merged-into" and verdict.get("merged_into_base"):
        status = f"merged into {str(verdict['merged_into_base'])[:12]}..."
    who = verdict.get("attributed_to")
    return f"{status} ({who})" if who else status


def _curation_target_line(verdict: dict) -> str:
    """One ``- <record_type>: <label>  (<scope>)`` bullet under a ruling.

    The scope note carries the row count a family-scoped verdict covers, which is what
    makes the group counts auditable rather than assertions: ``(family, 4 rows)`` against
    ``(row)``.
    """
    label = verdict["label"] or "(no live rows)"
    if verdict["record_id"]:
        scope = "row"
    else:
        size = verdict["family_size"]
        scope = f"family, {size} row{'' if size == 1 else 's'}"
    return f"- {verdict['record_type']}: {label}  ({scope})"


def _curation_groups(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    """This person's :data:`curation.APPENDIX_STATUSES` verdicts, grouped by ruling.

    Read from :func:`curation.list_curation` -- the stored table -- rather than from a
    render pass's collected verdicts, and that is the point of the target: the block this
    replaced listed only whatever verdicts some section's ``SELECT`` happened to hit, so
    "the appendix" was a different set per document (the summary's missed a superseded
    *inactive* med and a superseded lab older than the abnormal-labs window). A standalone
    audit trail must not depend on which sections ran, so the curation record is a
    deliberate **superset** of the block it replaces.

    The group key is the ruling itself -- ``(status, merged_into_base, note,
    attributed_to)`` -- so one merge session's note recorded against forty families is one
    block with a count instead of forty near-identical bullets.

    Orphan verdicts (the target is gone, so there is no person to scope them to) are
    excluded exactly as they were before, and keep their own surfaces: `pemr verify`,
    `pemr record reaffirm`, `pemr record --list`.

    Order is :func:`curation.list_curation`'s (``created_at DESC, record_type,
    dedup_base, record_id``), both across groups and inside one; it is already
    deterministic, so nothing re-sorts.
    """
    groups: dict[tuple, dict] = {}
    for verdict in curation.list_curation(conn):
        if verdict["status"] not in curation.APPENDIX_STATUSES:
            continue
        record_type = verdict["record_type"]
        if record_type not in dedup.FIELD_SPECS:
            # A hand-edited row can name anything, and `row_person`/`family_person`
            # interpolate the type into a table name. Guard before, never after.
            continue
        if verdict["record_id"]:
            owner, _slug = curation.row_person(conn, record_type, verdict["record_id"])
        else:
            owner, _slug = curation.family_person(
                conn, record_type, verdict["dedup_base"]
            )
        if owner is None or owner != person_id:
            continue
        key = (
            verdict["status"],
            verdict["merged_into_base"],
            verdict["note"],
            verdict["attributed_to"],
        )
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "heading": _verdict_heading(verdict),
                "note": verdict["note"] or "",
                "verdicts": 0,
                "rows": 0,
                "targets": [],
            }
        group["verdicts"] += 1
        group["rows"] += 1 if verdict["record_id"] else verdict["family_size"]
        group["targets"].append(_curation_target_line(verdict))
    return list(groups.values())


def _curation_group_block(group: dict) -> str:
    """One ruling rendered to Markdown: heading, counts, the note as a blockquote, then
    the targets it covers.

    Not :func:`_section`, for :func:`_questions_section`'s reason -- and because the body
    is not a bullet list but three stanzas. An empty note emits no blockquote rather than
    a bare ``>``.
    """
    lines = [f"## {group['heading']}", ""]
    verdicts, rows = group["verdicts"], group["rows"]
    lines.append(
        f"_{verdicts} verdict{'' if verdicts == 1 else 's'}, "
        f"{rows} row{'' if rows == 1 else 's'}_"
    )
    if group["note"]:
        lines.append("")
        lines.extend(f"> {line}" for line in group["note"].splitlines())
    lines.append("")
    lines.extend(group["targets"])
    return "\n".join(lines) + "\n"


def render_curation(
    conn: sqlite3.Connection,
    slug: str,
    *,
    now: datetime | None = None,
) -> str:
    """Markdown curation record for a person -- the audit trail of every recorded verdict
    that removed a row from the clinical documents, grouped by ruling. Read-only.

    ``""`` when there is nothing to say: no qualifying verdicts, or a pre-008 snapshot
    with no ``curation`` table at all (:func:`curation.list_curation` returns ``[]``
    there, the `has_table` degradation convention `render`/`verify` already use). That
    empty state is a **rule, not an optimisation** -- it is the additive-only guarantee
    the old ``_appendix_section`` documented, carried across the move: a subject nobody
    has curated must never be handed a document whose header implies a review happened.

    Raises :class:`query.PersonNotFoundError` for an unknown slug (friendly rc=1)."""
    person_id = query.resolve_person_id(conn, slug)
    groups = _curation_groups(conn, person_id)
    if not groups:
        return ""
    person = conn.execute(
        "SELECT * FROM person WHERE person_id = ?", (person_id,)
    ).fetchone()
    verdicts = sum(g["verdicts"] for g in groups)
    rows = sum(g["rows"] for g in groups)
    header = (
        f"# Curation Record: {person['full_name']}\n\n"
        f"- Person: {person['slug']}\n"
        f"- Generated: {_generated_at(now)} (read-only view of DB state)\n"
        f"- Verdicts: {verdicts} covering {rows} row{'' if rows == 1 else 's'}\n"
    )
    parts = [header] + [_curation_group_block(g) for g in groups]
    return "\n".join(parts).rstrip() + "\n"

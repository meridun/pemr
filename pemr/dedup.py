"""Layer-2 dedup: semantic keys, schema validation, conflict staging.

The deterministic second half of the ingestion pipeline (Architecture.md §3–4).
The agent proposes extracted rows as JSON; everything here is plain Python the
agent can't subtly get wrong:

    * validate the JSON against the record schemas (types, required fields;
      reject unknown record types)
    * compute a deterministic ``dedup_key`` per record type from normalized fields
    * split incoming rows into {new, duplicate, conflict}; insert new, stage
      conflicts (a dedup_key collision with differing non-key fields), report the rest

``norm()`` lowercases/trims/collapses whitespace and maps synonyms through the
analyte/name dictionary (``data/dictionary.toml``) so ``A1c`` / ``HbA1c`` /
``Hemoglobin A1c`` collapse to one canonical token — the one place fuzzy naming
gets pinned down deterministically.

``key_token()`` is the *identity* form of that name: the canonical token plus any
**meaningful** parenthetical qualifier (``albumin (spep)``), so two genuinely
distinct assays of one analyte off one draw keep distinct keys (issue #71).
``norm()`` remains the *family* form (``albumin`` for both) and is what analyte
listings match on.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from . import db

_NUM = (int, float)

# Per record type: field name -> (accepted python type(s), required?).
# person_id (inherited from the document), document_id (provenance) and dedup_key
# (computed) are managed here and are NOT accepted from the agent's JSON.
FIELD_SPECS: dict[str, dict[str, tuple[object, bool]]] = {
    "lab_result": {
        "test_name": (str, True),
        "loinc": (str, False),
        "value_num": (_NUM, False),
        "value_text": (str, False),
        "unit": (str, False),
        "ref_low": (_NUM, False),
        "ref_high": (_NUM, False),
        "flag": (str, False),
        "collected_at": (str, True),
    },
    "medication": {
        "name": (str, True),
        "dose": (str, False),
        "route": (str, False),
        "frequency": (str, False),
        "started_on": (str, False),
        "ended_on": (str, False),
        "prescriber": (str, False),
        "status": (str, False),
    },
    "procedure": {
        "name": (str, True),
        "performed_on": (str, False),
        "provider": (str, False),
        "outcome": (str, False),
    },
    "appointment": {
        "scheduled_for": (str, True),
        "provider": (str, False),
        "specialty": (str, False),
        "reason": (str, False),
        "summary": (str, False),
    },
    "observation": {
        "obs_type": (str, True),
        "observed_at": (str, False),
        "key": (str, False),
        "value_num": (_NUM, False),
        "value_text": (str, False),
        "unit": (str, False),
    },
}

KNOWN_TYPES = tuple(FIELD_SPECS)

# Key-machinery columns on every record table. Internal: they are stripped from the
# CLI `--json` and MCP read payloads (unstable, not part of either contract).
INTERNAL_COLUMNS = ("dedup_key", "dedup_base", "dedup_occurrence")

# Date-typed fields per record type. These feed timeline sort / trends date math and
# the dedup_key (via _date_only), all of which assume a lexically-sortable ISO date —
# so validate_row enforces ISO on them, not just the base `str` type. A partial
# prefix (YYYY / YYYY-MM) is accepted alongside a full date; see _is_iso_date.
DATE_FIELDS: dict[str, frozenset[str]] = {
    "lab_result": frozenset({"collected_at"}),
    "medication": frozenset({"started_on", "ended_on"}),
    "procedure": frozenset({"performed_on"}),
    "appointment": frozenset({"scheduled_for"}),
    "observation": frozenset({"observed_at"}),
}

_WS = re.compile(r"\s+")
# Capturing group so the same pattern both removes a parenthetical (`sub`, giving the
# bare analyte stem) and yields its contents (`findall`, giving the candidate qualifier).
_PAREN = re.compile(r"\(([^)]*)\)")
# Padding just inside a parenthesis, squeezed out by _collapse so `M-Spike ( SPEP )`
# is the same label as `M-Spike (SPEP)` (and so hits the same dictionary entry).
_PAREN_PAD = re.compile(r"\(\s+|\s+\)")

# A date value is accepted at one of three precisions, each a lexically-sortable ISO
# prefix (so `_date_only` slicing, timeline sort and every `ORDER BY <datecol>` keep
# working — a coarse date sorts at the start of its period):
#   * YYYY-MM-DD (full date), optionally followed by `T`/space + a time component;
#   * YYYY-MM    (month precision) — no time component permitted;
#   * YYYY       (year precision)  — no time component permitted.
# Calendar validity (real month/day, valid time, plausible year) is confirmed by the
# datetime/date constructors in _is_iso_date below.
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ].*)?")
_ISO_MONTH_RE = re.compile(r"\d{4}-\d{2}")
_ISO_YEAR_RE = re.compile(r"\d{4}")


class ValidationError(ValueError):
    """Raised when the extraction JSON violates a record schema."""


class DictionaryDriftError(ValueError):
    """Stored keys no longer match the current dictionary; `pemr rekey` comes first.

    Defined here rather than in :mod:`pemr.documents` because both the write paths
    that can be corrupted by drift — `document reassign` and `commit-extraction` —
    have to raise it, and only this module is importable from both.
    """


def _is_iso_date(value: str) -> bool:
    """True when ``value`` is an ISO date/timestamp at one of three precisions:

        * full date ``YYYY-MM-DD``, optionally + ``T``/space + a valid time
          (optionally with offset/``Z``) — e.g. ``2026-03-15``, ``2026-03-15T09:30Z``;
        * month precision ``YYYY-MM`` — e.g. ``2026-03``;
        * year precision ``YYYY`` — e.g. ``2026``.

    A time component is permitted **only** at full-date precision (``2026-03T09:00``
    is rejected). Non-ISO forms like ``06/15/2026``, ``2026-13``, ``2026-3`` or
    ``not-a-date`` are rejected — they would otherwise sort lexically ahead of real
    ISO dates and corrupt the timeline. Each accepted form is a prefix of the next, so
    all three sort correctly against each other and against full dates."""
    if _ISO_DATE_RE.fullmatch(value):
        try:
            # datetime.fromisoformat (3.11+) parses date-only and full timestamps and
            # enforces calendar/time validity; `Z` is accepted only from 3.11 onward.
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return True
    if _ISO_MONTH_RE.fullmatch(value):
        year, month = value.split("-")
        try:
            # date() enforces month 01-12 and a plausible (1-9999) year in one check.
            date(int(year), int(month), 1)
        except ValueError:
            return False
        return True
    if _ISO_YEAR_RE.fullmatch(value):
        try:
            # Rejects the one implausible 4-digit year, 0000 (date min year is 1).
            date(int(value), 1, 1)
        except ValueError:
            return False
        return True
    return False


# --------------------------------------------------------------------------- #
# Dictionary + normalization
# --------------------------------------------------------------------------- #

def load_dictionary(path: str | Path | None) -> dict[str, str]:
    """Load the synonym dictionary (``[synonyms]`` table) from a TOML file.

    Returns a ``normalized-synonym -> canonical`` map. A missing path yields an
    empty dict so ``norm()`` still lowercases/trims/collapses without mapping.
    Keys are themselves whitespace/case-normalized on load so authoring is lenient.
    """
    if path is None:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    with p.open("rb") as fh:
        data = tomllib.load(fh)
    raw = data.get("synonyms", {})
    return {_collapse(str(k)): str(v).strip() for k, v in raw.items()}


def _collapse(value: str) -> str:
    """Case/whitespace-normalized spelling of a free-text name.

    Underscores count as separators: machine-generated keys like ``blood_pressure``
    must collapse to the same token as ``Blood Pressure``. Trimming happens *after*
    that substitution — a leading/trailing ``_`` becomes whitespace, and a stray edge
    space would otherwise defeat :func:`identity`'s declared-full-label lookup
    (``"_m-spike (spep)"`` must still find the ``"m-spike (spep)"`` entry). Padding
    just inside a parenthesis is squeezed out for the same reason.
    """
    collapsed = _WS.sub(" ", value.lower().replace("_", " ")).strip()
    return _PAREN_PAD.sub(lambda m: m.group(0).strip(), collapsed)


def _strip_qualifiers(value: str) -> str:
    """Drop parenthetical qualifiers (``(SPEP)``, ``(HGB)``, ``(calculated)``) to leave
    the bare analyte *stem*, then re-collapse whitespace. The stem is what
    :func:`identity` looks up in the dictionary, so the dictionary only has to carry
    the minimal spelling, not every parenthesized variant a lab happens to print."""
    return _WS.sub(" ", _PAREN.sub(" ", value)).strip()


def _qualifier_text(value: str) -> str:
    """The concatenated contents of ``value``'s parentheticals (``""`` when there are
    none). ``"albumin (spep)"`` -> ``"spep"``; multiple groups join with a space."""
    return _WS.sub(" ", " ".join(_PAREN.findall(value))).strip()


def identity(
    value: object, dictionary: dict[str, str] | None = None
) -> tuple[str, str]:
    """``(canonical, qualifier)`` — the analyte family and, when the name carries a
    *meaningful* parenthetical, the assay that distinguishes it within that family.

    A parenthetical is meaningful **unless proven otherwise**, which is the deliberate
    inversion behind issue #71. Collapsing two distinct assays (a CMP ``Albumin`` and an
    SPEP ``Albumin (SPEP)`` off one draw) is lossy and near-silent — one row stores, the
    other stages as a conflict. Over-splitting is visible and non-lossy: two parallel
    series, repaired by one dictionary line plus ``pemr rekey``. Default to the
    recoverable failure.

    Three rules on ``full = _collapse(value)`` (parentheses preserved), in order:

    1. **A declared full label wins.** ``dictionary["m-spike (spep)"]`` is the human
       saying "this exact parenthesized label *is* that analyte" -> no qualifier. This
       needs no schema change: ``[synonyms]`` keys may simply contain parentheses.
    2. **A redundant alias is noise, with zero curation.** When the parenthetical maps
       to the same canonical token as the stem — ``Hemoglobin (HGB)``, ``Hematocrit
       (HCT)``, ``Platelet Count (PLT)`` — it is just another spelling of the stem, so
       it drops out with no dictionary entry of its own.
    3. **Everything else is meaningful** and becomes the qualifier, mapped through the
       dictionary so ``(SPEP)`` and ``(Serum Protein Electrophoresis)`` agree.
    """
    if value is None:
        return "", ""
    full = _collapse(str(value))
    if dictionary and full in dictionary:
        return dictionary[full], ""
    stem = _strip_qualifiers(full)
    canonical = dictionary.get(stem, stem) if dictionary else stem
    inner = _qualifier_text(full)
    if not inner:
        return canonical, ""
    qualifier = dictionary.get(inner, inner) if dictionary else inner
    if qualifier == canonical:
        return canonical, ""
    return canonical, qualifier


def norm(value: object, dictionary: dict[str, str] | None = None) -> str:
    """Normalize a free-text field to its **analyte family** token: lowercase, trim,
    collapse whitespace (underscores count as whitespace), drop parenthetical
    qualifiers, then map synonyms through the dictionary. ``None`` -> ``""``
    (deterministic key part).

    This is the *family* form — ``Albumin`` and ``Albumin (SPEP)`` both normalize to
    ``albumin`` — which is what analyte listings (``pemr labs --test``) match on so a
    listing shows the whole family. Dedup keys and numeric series use the finer
    :func:`key_token` instead.
    """
    return identity(value, dictionary)[0]


def key_token(value: object, dictionary: dict[str, str] | None = None) -> str:
    """The **dedup identity** token for a name: :func:`norm`'s canonical token, plus a
    meaningful qualifier in parentheses when :func:`identity` finds one.

    ``"albumin"`` / ``"albumin (spep)"``. Folded into the existing key part rather than
    appended as a new one, deliberately: a fourth hash part would change ``"a|b|c"`` to
    ``"a|b||c"`` and move the key of *every* stored row. Folding leaves every unqualified
    row's ``dedup_key`` bit-identical, so ``pemr rekey`` moves only the rows this bug
    actually affects.
    """
    canonical, qualifier = identity(value, dictionary)
    if not qualifier:
        return canonical
    if not canonical:
        return qualifier      # degenerate "(SPEP)"-only name: the qualifier is the name
    return f"{canonical} ({qualifier})"


def _date_only(value: object) -> str:
    """Date portion of an ISO datetime/date string ('2026-01-02T09:00' -> '2026-01-02')."""
    if value is None:
        return ""
    text = str(value).strip()
    return text.replace("T", " ").split(" ")[0]


def _norm_ts(value: object) -> str:
    """Normalize an ISO date/datetime for use as a dedup-key identity part, keeping
    *full precision* (unlike :func:`_date_only`, which truncates to the date).

    A timestamped draw (``2026-01-02T09:00``) and a bare-date draw (``2026-01-02``)
    stay distinct, and two draws on the same day at different times keep distinct keys
    — so serial same-day repeats (GTT, peri-op, inpatient q6h) survive as separate
    rows. A re-read/correction of the *same* draw carries the same timestamp, collides,
    and surfaces as a CONFLICT via :data:`_COMPARE_FIELDS`. Only the ``T``/space
    separator and surrounding/collapsed whitespace are normalized, so trivial
    formatting differences don't fork the key."""
    if value is None:
        return ""
    return _WS.sub(" ", str(value).strip().replace("T", " "))


def _hash_parts(parts: list[object]) -> str:
    joined = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _key_parts(
    record_type: str, row: dict, person_id: int, dictionary: dict[str, str] | None = None
) -> list[object]:
    """The normalized identity tuple a ``dedup_base`` hashes (Architecture.md §3).

    Kept separate from the hashing so an error message can name *why* two rows
    collide (see :func:`identity_label`) instead of quoting a bare hash.

    Analyte-style names — ``lab_result.test_name`` and ``observation.key`` — key on
    :func:`key_token`, so a meaningful assay qualifier stays part of the identity
    (issue #71). Everything else keys on :func:`norm`: medication / procedure /
    appointment names already carry dose / date / provider in the key, and their
    parentheticals are usually brand or descriptive (``Insulin (Lantus)``) rather than a
    second measurement of one thing.
    """
    def n(field_name: str) -> str:
        return norm(row.get(field_name), dictionary)

    def kt(field_name: str) -> str:
        return key_token(row.get(field_name), dictionary)

    if record_type == "lab_result":
        return [person_id, kt("test_name"), _norm_ts(row.get("collected_at"))]
    if record_type == "medication":
        return [person_id, n("name"), _collapse(str(row.get("dose") or "")),
                _date_only(row.get("started_on"))]
    if record_type == "procedure":
        return [person_id, n("name"), _date_only(row.get("performed_on"))]
    if record_type == "appointment":
        return [person_id, n("provider"), _date_only(row.get("scheduled_for"))]
    if record_type == "observation":
        return [person_id, n("obs_type"), _norm_ts(row.get("observed_at")), kt("key")]
    raise ValidationError(f"unknown record type: {record_type}")  # guarded by validate()


def identity_label(
    record_type: str, row: dict, person_id: int, dictionary: dict[str, str] | None = None
) -> str:
    """Human-readable rendering of the identity tuple, e.g.
    ``person 1 | glucose | 2024-04-01``."""
    parts = _key_parts(record_type, row, person_id, dictionary)
    return " | ".join([f"person {parts[0]}"] + [str(p) for p in parts[1:]])


def occurrence_key(base: str, occurrence: int) -> str:
    """``dedup_key`` for occurrence *n* of an identity family.

    Occurrence 0 IS the base, byte-for-byte — every row committed before
    migration 005 is occurrence 0, so the numbering added no key churn. Repeats
    admitted by ``--keep both`` hash the base together with their occurrence, which
    keeps the key a pure function of persisted columns and therefore survives
    :func:`rekey` (a resolution-time suffix would not).
    """
    if not occurrence:
        return base
    return _hash_parts([base, occurrence])


def dedup_key(
    record_type: str,
    row: dict,
    person_id: int,
    dictionary: dict[str, str] | None = None,
    occurrence: int = 0,
) -> str:
    """Deterministic semantic key per Architecture.md §3. Same clinical fact from
    two different documents -> identical key -> collapses to one row.

    With the default ``occurrence=0`` this returns the family's ``dedup_base``; a
    non-zero ``occurrence`` returns the key of that sibling (see
    :func:`occurrence_key`).
    """
    return occurrence_key(
        _hash_parts(_key_parts(record_type, row, person_id, dictionary)), occurrence
    )


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def validate_row(record_type: str, row: object) -> None:
    if record_type not in FIELD_SPECS:
        raise ValidationError(
            f"unknown record type '{record_type}' "
            f"(known: {', '.join(KNOWN_TYPES)})"
        )
    if not isinstance(row, dict):
        raise ValidationError(f"{record_type}: each record must be a JSON object")
    spec = FIELD_SPECS[record_type]
    unknown = set(row) - set(spec)
    if unknown:
        raise ValidationError(
            f"{record_type}: unknown field(s): {', '.join(sorted(unknown))}"
        )
    for name, (types, required) in spec.items():
        if name not in row or row[name] is None:
            if required:
                raise ValidationError(f"{record_type}: missing required field '{name}'")
            continue
        value = row[name]
        # bool is an int subclass but is never a valid numeric/text medical value.
        if isinstance(value, bool) or not isinstance(value, types):
            raise ValidationError(
                f"{record_type}.{name}: expected {_type_names(types)}, "
                f"got {type(value).__name__}"
            )
        if name in DATE_FIELDS.get(record_type, frozenset()) and not _is_iso_date(value):
            raise ValidationError(
                f"{record_type}.{name}: expected ISO date at year (YYYY), month "
                f"(YYYY-MM) or full (YYYY-MM-DD) precision, or a full-date ISO "
                f"timestamp, got {value!r}"
            )


def _type_names(types: object) -> str:
    if isinstance(types, tuple):
        return "/".join(t.__name__ for t in types)
    return types.__name__  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Commit extraction (new / duplicate / conflict split)
# --------------------------------------------------------------------------- #

@dataclass
class CommitSummary:
    new: list[tuple[str, int]] = field(default_factory=list)          # (type, row_id)
    duplicate: list[tuple[str, str]] = field(default_factory=list)    # (type, dedup_key)
    conflict: list[tuple[str, int]] = field(default_factory=list)     # (type, conflict_id)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "new": len(self.new),
            "duplicate": len(self.duplicate),
            "conflict": len(self.conflict),
        }


# Payload fields compared to decide duplicate-vs-conflict once dedup_keys match.
# Identity fields (those feeding the dedup_key) are equal by construction — a
# difference there yields a *different* key and hence a new row, never a collision —
# so they are deliberately excluded here. The measured value is deliberately NOT an
# identity field: the lab/observation keys carry temporal identity (collected_at /
# observed_at at full precision) but not the value, so a corrected or re-read value on
# an otherwise-matching key collides and surfaces here as a CONFLICT instead of a
# silent duplicate — hence value_num/value_text appear below.
_COMPARE_FIELDS: dict[str, list[str]] = {
    "lab_result": ["value_num", "value_text", "unit", "ref_low", "ref_high", "flag", "loinc"],
    "medication": ["route", "frequency", "ended_on", "prescriber", "status"],
    "procedure": ["provider", "outcome"],
    "appointment": ["specialty", "reason", "summary"],
    "observation": ["value_num", "value_text", "unit"],
}


def _stored_fields(record_type: str, row_map: dict) -> dict:
    """Subset of a DB row limited to the agent-provided columns (conflict snapshot)."""
    return {name: row_map.get(name) for name in FIELD_SPECS[record_type]}


def _norm_unit(value: object) -> str:
    """Units compare case-insensitively: ``MG/DL`` and ``mg/dL`` are the same unit,
    not a conflict (real reports vary the casing freely). Stored display casing is
    left untouched — this only governs the duplicate-vs-conflict decision."""
    return "" if value is None else str(value).strip().lower()


def _rows_equal(
    record_type: str, existing: sqlite3.Row | dict, incoming: dict
) -> bool:
    """True when two rows in one identity family are the *same fact* (a duplicate)
    rather than a conflicting one — i.e. their payload fields all agree.

    ``existing`` is a stored row (or, in the pass-1 intra-payload check, an earlier
    row of the same submission)."""
    existing_map = dict(existing)
    for name in _COMPARE_FIELDS[record_type]:
        ev = existing_map.get(name)
        iv = incoming.get(name)
        if ev is None and iv is None:
            continue
        # Unit strings are compared case-insensitively so casing variants across
        # documents don't stage a spurious conflict.
        if name == "unit":
            if _norm_unit(ev) != _norm_unit(iv):
                return False
            continue
        # Numeric columns round-trip through SQLite as float; compare numerically.
        if isinstance(ev, _NUM) and isinstance(iv, _NUM):
            if float(ev) != float(iv):
                return False
        elif ev != iv:
            return False
    return True


def _assert_no_key_drift(
    conn: sqlite3.Connection,
    record_type: str,
    rows: list,
    person_id: int,
    dictionary: dict[str, str] | None,
) -> None:
    """Refuse a commit that would fork a fact already stored under a **stale** key.

    A ``dedup_key`` is frozen at commit time, so once the current dictionary (or the
    key derivation itself — issue #71 gave every parenthesized analyte name a new key)
    disagrees with what a stored row carries, layer-2 dedup silently misses: the same
    fact lands a *second* time, reported as ``new``, with no duplicate/conflict signal
    for anyone to notice. `pemr rekey --apply` is the fix, but nothing used to make the
    user run it before their next ingest — this is what does.

    Deliberately *narrow*: it fires only when a stored row recomputes onto an identity
    this submission also derives, so an unrelated drifted row elsewhere in the database
    never blocks an ingest. `document reassign` carries the equivalent guard
    (:class:`DictionaryDriftError` there too), for the same reason.
    """
    bases = {dedup_key(record_type, row, person_id, dictionary) for row in rows}
    if not bases:
        return
    pk = f"{record_type}_id"
    stored = conn.execute(
        f"SELECT * FROM {record_type} WHERE person_id = ?", (person_id,)
    ).fetchall()
    for row in stored:
        payload = {name: row[name] for name in FIELD_SPECS[record_type]}
        # An admitted repeat (`--keep both`) keys on hash(base|occurrence), so the
        # recompute has to carry the stored occurrence or every sibling reads as drift.
        occurrence = int(row["dedup_occurrence"] or 0)
        base = dedup_key(record_type, payload, person_id, dictionary)
        if base not in bases:
            continue
        if occurrence_key(base, occurrence) != row["dedup_key"]:
            raise DictionaryDriftError(
                f"{record_type}: {pk} {row[pk]} "
                f"({_rekey_label(record_type, row)!r}) is stored under a dedup_key "
                "that no longer matches the current dictionary, and this submission "
                "derives that same identity - committing now would file the fact a "
                "second time instead of deduping (or staging a conflict) against the "
                "stored row. Run `pemr rekey --apply` first, then retry; nothing was "
                "written"
            )


def commit_extraction(
    conn: sqlite3.Connection,
    document_id: int,
    records: dict,
    dictionary: dict[str, str] | None = None,
) -> CommitSummary:
    """Validate + dedup + insert extracted rows for one document, atomically.

    ``records`` maps record-type -> list of row dicts (e.g. ``{"lab_result": [...]}``).
    All person_id values are inherited from the document (a document belongs to one
    person). The whole commit is one transaction: any validation error rolls the
    entire batch back so no partial extraction lands.
    """
    db.require_migrated(conn)

    doc = conn.execute(
        "SELECT * FROM document WHERE document_id = ?", (document_id,)
    ).fetchone()
    if doc is None:
        raise ValidationError(f"no document with id {document_id} - ingest it first")
    person_id = doc["person_id"]
    if person_id is None:
        raise ValidationError(
            f"document {document_id} has no person_id; cannot attach records"
        )

    if not isinstance(records, dict):
        raise ValidationError("extraction JSON must be an object of type -> [rows]")

    # Pass 1: validate everything up front so a bad row never half-commits.
    for record_type, rows in records.items():
        if record_type not in FIELD_SPECS:
            raise ValidationError(
                f"unknown record type '{record_type}' "
                f"(known: {', '.join(KNOWN_TYPES)})"
            )
        if not isinstance(rows, list):
            raise ValidationError(f"{record_type}: value must be a list of records")
        # Two rows of ONE submission deriving one key and disagreeing on the payload is
        # an extraction error, not a conflict for a human to adjudicate: the "existing"
        # side would have been inserted milliseconds earlier in the same batch, so it
        # carries no independent provenance. Reject up front (issue #58). Two *identical*
        # rows stay benign — that is an agent listing one fact twice, and pass 2 reports
        # it as a duplicate.
        seen: dict[str, tuple[int, dict]] = {}
        for index, row in enumerate(rows):
            validate_row(record_type, row)
            base = dedup_key(record_type, row, person_id, dictionary)
            prior = seen.get(base)
            if prior is None:
                seen[base] = (index, row)
            elif not _rows_equal(record_type, prior[1], row):
                raise ValidationError(
                    f"{record_type}: rows {prior[0]} and {index} of this submission "
                    f"derive the same dedup_key "
                    f"({identity_label(record_type, row, person_id, dictionary)}) "
                    "but carry different values. If the source gives distinct "
                    "collection times, add them (AGENTS.md date-precision rule). If "
                    "these are two genuine same-day results the source cannot "
                    "timestamp, commit them in separate submissions and resolve the "
                    "conflict with `--keep both`"
                )
        # Still pass 1 (nothing written yet): a stored row whose frozen key no longer
        # matches its recompute would be missed by pass 2's dedup, forking the fact.
        _assert_no_key_drift(conn, record_type, rows, person_id, dictionary)

    summary = CommitSummary()
    detected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Pass 2: dedup + insert inside a single transaction.
    with conn:
        for record_type, rows in records.items():
            for row in rows:
                base = dedup_key(record_type, row, person_id, dictionary)
                # Match against the whole occurrence FAMILY, not one row: once
                # `--keep both` has admitted a repeat, a third commit of that same
                # payload must dedup against the sibling rather than fork or re-stage.
                family = load_family(conn, record_type, base)
                if not family:
                    row_id = _insert_record(
                        conn, record_type, row, person_id, document_id, base
                    )
                    summary.new.append((record_type, row_id))
                    continue
                twin = next(
                    (f for f in family if _rows_equal(record_type, f, row)), None
                )
                if twin is not None:
                    summary.duplicate.append((record_type, twin["dedup_key"]))
                else:
                    # Stage against occurrence 0: it is the row the conflict's
                    # dedup_key anchors to, and the reviewer sees the family size.
                    conflict_id = _stage_conflict(
                        conn, record_type, base, person_id, document_id,
                        family[0], row, detected_at,
                    )
                    summary.conflict.append((record_type, conflict_id))
    return summary


def load_family(
    conn: sqlite3.Connection, record_type: str, base: str
) -> list[sqlite3.Row]:
    """Every stored row sharing one ``dedup_base``, occurrence order (0 first).

    Relies on migration 005's invariant that ``dedup_base`` is populated on every
    row; every write path here maintains it.
    """
    return conn.execute(
        f"SELECT * FROM {record_type} WHERE dedup_base = ? ORDER BY dedup_occurrence",
        (base,),
    ).fetchall()


def count_occurrences(conn: sqlite3.Connection, record_type: str, base: str) -> int:
    """How many rows are already stored under one identity (family size)."""
    if record_type not in FIELD_SPECS:
        return 0
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM {record_type} WHERE dedup_base = ?", (base,)
    ).fetchone()
    return int(row["n"])


def conflict_occurrences(
    conn: sqlite3.Connection,
    conflict: sqlite3.Row,
    dictionary: dict[str, str] | None = None,
) -> int:
    """Family size for a conflict's identity, resolved the same way a resolution
    resolves it (:func:`_conflict_family`) — a conflict staged before a dictionary
    edit carries a key no row still holds, and counting on it reads 0 for a family
    that exists."""
    if conflict["record_type"] not in FIELD_SPECS:
        return 0
    return len(_conflict_family(conn, conflict, dictionary)[1])


def _insert_record(
    conn: sqlite3.Connection,
    record_type: str,
    row: dict,
    person_id: int,
    document_id: int | None,
    base: str,
    occurrence: int = 0,
) -> int:
    columns = ["person_id", "document_id", "dedup_key", "dedup_base", "dedup_occurrence"]
    values: list[object] = [
        person_id, document_id, occurrence_key(base, occurrence), base, occurrence,
    ]
    for name in FIELD_SPECS[record_type]:
        if name in row and row[name] is not None:
            columns.append(name)
            values.append(row[name])
    placeholders = ", ".join("?" for _ in values)
    cur = conn.execute(
        f"INSERT INTO {record_type} ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    return int(cur.lastrowid)


def _stage_conflict(
    conn: sqlite3.Connection,
    record_type: str,
    key: str,
    person_id: int,
    document_id: int,
    existing: sqlite3.Row,
    incoming: dict,
    detected_at: str,
) -> int:
    existing_json = json.dumps(_stored_fields(record_type, dict(existing)), default=str)
    incoming_json = json.dumps(incoming, default=str)
    cur = conn.execute(
        """
        INSERT INTO conflict
          (record_type, dedup_key, person_id, document_id, existing_json,
           incoming_json, status, detected_at)
        VALUES (?, ?, ?, ?, ?, ?, 'open', ?)
        """,
        (record_type, key, person_id, document_id, existing_json, incoming_json,
         detected_at),
    )
    return int(cur.lastrowid)


# --------------------------------------------------------------------------- #
# Conflict review
# --------------------------------------------------------------------------- #

def list_conflicts(
    conn: sqlite3.Connection, status: str | None = "open"
) -> list[sqlite3.Row]:
    db.require_migrated(conn)
    if status is None:
        return conn.execute(
            "SELECT * FROM conflict ORDER BY conflict_id"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM conflict WHERE status = ? ORDER BY conflict_id", (status,)
    ).fetchall()


@dataclass
class ResolveResult:
    """Outcome of :func:`resolve_conflict`.

    ``row_id``/``occurrence`` describe the row the resolution landed on: the
    *admitted* row for ``keep='both'``, the *overwritten* one for
    ``keep='incoming'``. ``keep='existing'`` writes nothing, so it leaves them
    unset. ``dedup_key`` is that row's key (which for an occurrence >= 1 row is
    *not* the conflict's key — see :func:`_anchor_row`); ``dedup_base`` is the
    family it joined, re-derived under the current dictionary rather than taken
    from the conflict (see :func:`_derive_base`).
    """
    kept: str
    record_type: str = ""
    row_id: int | None = None
    occurrence: int | None = None
    dedup_key: str | None = None
    dedup_base: str | None = None
    no_op: bool = False          # keep-both that matched an existing sibling


KEEP_CHOICES = ("existing", "incoming", "both")


def resolve_conflict(
    conn: sqlite3.Connection,
    conflict_id: int,
    keep: str,
    note: str | None = None,
    dictionary: dict[str, str] | None = None,
) -> ResolveResult:
    """Resolve a staged conflict. ``keep`` is 'existing' (drop the incoming row),
    'incoming' (overwrite the stored record's payload fields with the incoming row) or
    'both' (admit the incoming row *alongside* the stored one as the next occurrence of
    that identity — the recovery path for a genuine repeat the source cannot timestamp,
    issue #58).

    Overwriting touches only the payload columns (``_COMPARE_FIELDS``) whose
    disagreement defined the conflict, plus provenance ``document_id``. Identity
    fields keep their stored display form (they are equal after norm() by
    construction, but may differ in casing/spacing); the dedup_key stays put.

    ``keep='both'`` re-validates the staged JSON before it becomes a row, and is
    idempotent by payload: if a sibling already carries that exact payload (two
    conflicts staged from one submission, both resolved 'both') nothing is inserted
    and the resolution records the no-op.

    ``dictionary`` is the *current* synonym dictionary. Both writing resolutions
    re-derive the conflict's identity family through it rather than trusting the
    key frozen on the conflict row (:func:`_derive_base`) — pass the same dictionary
    the caller would pass to :func:`commit_extraction` / :func:`rekey`.
    """
    db.require_migrated(conn)
    if keep not in KEEP_CHOICES:
        raise ValueError("keep must be 'existing', 'incoming' or 'both'")

    row = conn.execute(
        "SELECT * FROM conflict WHERE conflict_id = ?", (conflict_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no conflict with id {conflict_id}")
    if row["status"] != "open":
        raise ValueError(f"conflict {conflict_id} is already {row['status']}")

    record_type = row["record_type"]
    if keep == "both":
        result = _plan_keep_both(conn, row, dictionary)
    elif keep == "incoming":
        # Resolve the target *before* the transaction: a family with no rows left
        # can no longer be overwritten, and that must refuse rather than resolve.
        anchor = _anchor_row(conn, row, dictionary)
        result = ResolveResult(
            kept=keep, record_type=record_type,
            row_id=int(anchor[f"{record_type}_id"]),
            occurrence=int(anchor["dedup_occurrence"]),
            dedup_key=anchor["dedup_key"], dedup_base=anchor["dedup_base"],
        )
    else:
        result = ResolveResult(
            kept=keep, record_type=record_type, dedup_key=row["dedup_key"]
        )
    resolved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with conn:
        if keep == "incoming":
            incoming = json.loads(row["incoming_json"])
            _overwrite_record(
                conn, record_type, int(result.row_id), row["document_id"], incoming
            )
        elif keep == "both" and not result.no_op:
            result.row_id = _insert_record(
                conn, record_type, json.loads(row["incoming_json"]),
                row["person_id"], row["document_id"],
                result.dedup_base or row["dedup_key"], result.occurrence or 0,
            )
        resolution = _resolution_text(result) + (f": {note}" if note else "")
        conn.execute(
            "UPDATE conflict SET status='resolved', resolution=?, resolved_at=? "
            "WHERE conflict_id=?",
            (resolution, resolved_at, conflict_id),
        )
    return result


def _resolution_text(result: ResolveResult) -> str:
    """The auditable resolution string stored on the conflict row. keep-both records
    which row it admitted (or matched) so the decision stays reconstructable."""
    if result.kept != "both":
        return f"keep-{result.kept}"
    if result.no_op:
        return f"keep-both (no-op: matches {result.record_type} #{result.row_id})"
    return (
        f"keep-both -> {result.record_type} #{result.row_id} "
        f"occurrence={result.occurrence} key={(result.dedup_key or '')[:12]}..."
    )


def _derive_base(
    conflict: sqlite3.Row, dictionary: dict[str, str] | None
) -> str:
    """The ``dedup_base`` this conflict's identity hashes to under the *current*
    dictionary.

    ``conflict["dedup_key"]`` is only the live base while the dictionary is
    unchanged: ``rekey`` rewrites the record tables' keys and leaves the ``conflict``
    table alone, so a dictionary edit made while a conflict sits open strands that
    conflict on a base no row carries. Re-deriving from the payload is what keeps a
    resolution landing in the live family instead of minting an underivable key
    (which would wedge every later ``rekey``).

    Falls back to the stored key when the payload cannot produce one (no
    ``person_id``, unparseable JSON, unknown type) — those cases are refused, or
    handled, further along by the callers.
    """
    if conflict["person_id"] is None:
        return conflict["dedup_key"]
    try:
        incoming = json.loads(conflict["incoming_json"])
        if not isinstance(incoming, dict):
            return conflict["dedup_key"]
        return dedup_key(
            conflict["record_type"], incoming, int(conflict["person_id"]), dictionary
        )
    except (ValueError, TypeError):     # includes ValidationError, JSONDecodeError
        return conflict["dedup_key"]


def _conflict_family(
    conn: sqlite3.Connection,
    conflict: sqlite3.Row,
    dictionary: dict[str, str] | None,
) -> tuple[str, list[sqlite3.Row]]:
    """``(base, rows)`` — the identity family a resolution of this conflict acts on.

    **The staged key wins whenever it still has rows.** ``conflict["dedup_key"]``
    names the family the conflict was actually staged against, so preferring it is
    correct by construction. Re-derivation (:func:`_derive_base`) is only needed for
    the case it was added for — ``rekey`` having moved that family off the staged key
    — so it applies only when the staged key is empty.

    Preferring the derived base instead would be wrong whenever the derived base
    holds a *different, pre-existing* family, which is exactly what a dictionary edit
    that fuses two identities produces (and ``rekey`` refuses to run in that state, so
    the database stays there): the resolution would overwrite, or join, an unrelated
    record. The five states this covers:

    * no drift — derived base == staged key, same family either way;
    * drift with ``rekey`` applied — staged key empty, fall through to the live family;
    * drift without ``rekey`` — staged key still has the rows, stay with them so a
      later ``rekey`` moves the whole family together instead of colliding two rows;
    * fusing drift — staged key still has the rows, so the conflict's own anchor wins
      over the unrelated family sitting on the derived base;
    * family fully removed — both empty, and :func:`_anchor_row` refuses.
    """
    record_type = conflict["record_type"]
    base = _derive_base(conflict, dictionary)
    if base != conflict["dedup_key"]:
        staged = load_family(conn, record_type, conflict["dedup_key"])
        if staged:
            return conflict["dedup_key"], staged
    return base, load_family(conn, record_type, base)


def _anchor_row(
    conn: sqlite3.Connection,
    conflict: sqlite3.Row,
    dictionary: dict[str, str] | None,
) -> sqlite3.Row:
    """The stored row a conflict is anchored to: occurrence 0, or the lowest
    surviving occurrence of its identity family.

    A conflict's ``dedup_key`` is always a family *base*, never the anchor row's
    own key once occurrence 0 is gone (an occurrence >= 1 row keys on
    ``hash(base|n)``). So resolutions must reach the row through the family, by
    primary key - targeting ``WHERE dedup_key = <conflict key>`` matches nothing in
    an orphaned family and would report success while writing nothing.
    """
    base, family = _conflict_family(conn, conflict, dictionary)
    if not family:
        drifted = ""
        if base != conflict["dedup_key"]:
            drifted = (
                " (the dictionary also changed since it was staged, so its key was "
                "re-derived; neither key has rows)"
            )
        raise ValueError(
            f"conflict {conflict['conflict_id']} has no stored "
            f"{conflict['record_type']} row left to overwrite - every occurrence of "
            f"that identity was removed after the conflict was staged{drifted}. "
            "Resolve with keep 'both' to admit the incoming row as a new record "
            "instead."
        )
    return family[0]


def _plan_keep_both(
    conn: sqlite3.Connection,
    conflict: sqlite3.Row,
    dictionary: dict[str, str] | None,
) -> ResolveResult:
    """Validate + number the row a ``keep='both'`` resolution would admit.

    Everything that can refuse the resolution happens here, before the transaction
    opens.
    """
    record_type = conflict["record_type"]
    incoming = json.loads(conflict["incoming_json"])
    validate_row(record_type, incoming)

    if conflict["person_id"] is None:
        raise ValueError(
            f"conflict {conflict['conflict_id']} has no person_id - cannot admit the "
            "incoming row as a new record"
        )

    base, family = _conflict_family(conn, conflict, dictionary)
    pk = f"{record_type}_id"
    twin = next((f for f in family if _rows_equal(record_type, f, incoming)), None)
    if twin is not None:
        # Two conflicts staged from one payload, both resolved 'both': the second
        # must not produce a twin row.
        return ResolveResult(
            kept="both", record_type=record_type, row_id=int(twin[pk]),
            occurrence=int(twin["dedup_occurrence"]),
            dedup_key=twin["dedup_key"], dedup_base=twin["dedup_base"], no_op=True,
        )

    # Occurrence numbers are monotonic over the family and never reused, so a removed
    # sibling leaves a hole rather than letting a later row inherit its key.
    occurrence = max((int(f["dedup_occurrence"]) for f in family), default=-1) + 1
    return ResolveResult(
        kept="both", record_type=record_type, occurrence=occurrence,
        dedup_key=occurrence_key(base, occurrence), dedup_base=base,
    )


def _rekey_label(record_type: str, row: sqlite3.Row) -> str:
    """Short human identifier for a row in `pemr rekey` output ("CL", "Metformin")."""
    if record_type == "lab_result":
        return row["test_name"] or ""
    if record_type in ("medication", "procedure"):
        return row["name"] or ""
    if record_type == "appointment":
        return " ".join(p for p in (row["provider"], row["scheduled_for"]) if p)
    return " ".join(p for p in (row["obs_type"], row["key"]) if p)  # observation


@dataclass
class RekeyChange:
    record_type: str
    row_id: int
    label: str
    old_key: str
    new_key: str
    new_base: str = ""      # recomputed dedup_base (new_key == new_base at occurrence 0)


@dataclass
class RekeyReport:
    scanned: dict[str, int] = field(default_factory=dict)      # type -> rows examined
    changes: list[RekeyChange] = field(default_factory=list)
    applied: bool = False


class RekeyCollisionError(Exception):
    """Two rows recompute to one dedup_key — the dictionary would fuse distinct facts."""


def rekey(
    conn: sqlite3.Connection,
    dictionary: dict[str, str] | None = None,
    *,
    apply: bool = False,
) -> RekeyReport:
    """Recompute every stored ``dedup_key`` under the *current* dictionary.

    A ``dedup_key`` is frozen at commit time, so adding a synonym (``cl`` ->
    ``chloride``) changes the key a future commit computes for a fact already in the
    DB: layer-2 dedup misses and the same fact lands twice. This walks the record
    tables and re-derives each key, so stored rows keep deduping after a dictionary
    edit. Values, provenance and row ids are untouched — only ``dedup_key`` moves.

    Dry-run by default: pass ``apply=True`` to write. If two rows in a table recompute
    to the same key nothing is written and :class:`RekeyCollisionError` is raised, with
    a message that names which of the two causes it is:

    * the rows carry *different* payloads — the dictionary would merge two distinct
      facts (typically two methods for one analyte off one draw): fix the dictionary;
    * the rows carry the *same* payload — one fact was filed twice, once under a
      pre-drift key, so it is the data that needs fixing. :func:`_assert_no_key_drift`
      refuses the ingest that would create this state, so it should only be reachable
      in a database that drifted before that guard existed.
    """
    db.require_migrated(conn)
    report = RekeyReport(applied=False)

    for record_type in KNOWN_TYPES:
        pk = f"{record_type}_id"
        rows = conn.execute(f"SELECT * FROM {record_type}").fetchall()
        report.scanned[record_type] = len(rows)
        seen: dict[str, sqlite3.Row] = {}
        for row in rows:
            payload = {name: row[name] for name in FIELD_SPECS[record_type]}
            # The occurrence is a stored column, so an admitted repeat (`--keep both`)
            # recomputes to its own key rather than colliding with its sibling.
            occurrence = int(row["dedup_occurrence"] or 0)
            base = dedup_key(record_type, payload, row["person_id"], dictionary)
            key = occurrence_key(base, occurrence)
            clash = seen.get(key)
            if clash is not None:
                if _rows_equal(record_type, clash, payload):
                    # Same payload, two keys: not a dictionary fault at all - one fact
                    # was filed twice, once under a pre-drift key and once under the
                    # current one. Say so, because "fix the dictionary" is exactly the
                    # wrong advice here (`_assert_no_key_drift` now stops new ingests
                    # from reaching this state).
                    raise RekeyCollisionError(
                        f"{record_type}: {pk} {row[pk]} and {pk} {clash[pk]} "
                        f"({_rekey_label(record_type, row)!r}) hold the SAME fact "
                        "under two dedup_keys - it was filed a second time by an "
                        "ingest that ran against drifted keys before this rekey. The "
                        "dictionary is fine; the data is doubled. Drop the duplicate "
                        "(`pemr document rm` on the document that re-filed it, or "
                        "resolve it by hand) and re-run; nothing was written"
                    )
                raise RekeyCollisionError(
                    f"{record_type}: {pk} {row[pk]} "
                    f"({_rekey_label(record_type, row)!r}) and {pk} {clash[pk]} "
                    f"({_rekey_label(record_type, clash)!r}) recompute to the same "
                    "dedup_key - the dictionary maps two distinct facts onto one "
                    "canonical name; nothing was written"
                )
            seen[key] = row
            if key != row["dedup_key"]:
                report.changes.append(RekeyChange(
                    record_type, row[pk], _rekey_label(record_type, row),
                    row["dedup_key"], key, base,
                ))

    if apply and report.changes:
        # One transaction: a half-rekeyed table dedups inconsistently. Two passes,
        # because `UNIQUE(dedup_key)` is enforced per statement: if two rows swap keys
        # (or one takes a key another is about to vacate) a single-pass update trips
        # the index mid-flight. Park every moving row on a unique placeholder first.
        with conn:
            for change in report.changes:
                conn.execute(
                    f"UPDATE {change.record_type} SET dedup_key = ? "
                    f"WHERE {change.record_type}_id = ?",
                    (f"rekey-pending-{change.record_type}-{change.row_id}",
                     change.row_id),
                )
            for change in report.changes:
                # dedup_base moves with the key: base and key are injective in each
                # other for a fixed occurrence, so a changed key means a changed base.
                conn.execute(
                    f"UPDATE {change.record_type} SET dedup_key = ?, dedup_base = ? "
                    f"WHERE {change.record_type}_id = ?",
                    (change.new_key, change.new_base, change.row_id),
                )
    report.applied = apply
    return report


def _overwrite_record(
    conn: sqlite3.Connection,
    record_type: str,
    row_id: int,
    document_id: int | None,
    incoming: dict,
) -> None:
    """Overwrite one stored row's payload columns, addressed by primary key.

    Primary key, not ``dedup_key``: see :func:`_anchor_row`. A zero-row UPDATE is an
    error, never a silent success - it would discard the incoming row while the
    conflict is marked resolved. The guard only catches *no* row, not the *wrong*
    row; picking the right one is :func:`_conflict_family`'s job.
    """
    assignments = ["document_id = ?"]
    values: list[object] = [document_id]
    for name in _COMPARE_FIELDS[record_type]:
        assignments.append(f"{name} = ?")
        values.append(incoming.get(name))
    values.append(row_id)
    cur = conn.execute(
        f"UPDATE {record_type} SET {', '.join(assignments)} "
        f"WHERE {record_type}_id = ?",
        values,
    )
    if cur.rowcount != 1:
        raise ValueError(
            f"keep-incoming matched no {record_type} row (id {row_id}) - refusing to "
            "resolve the conflict, the incoming row would have been discarded"
        )

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
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Protocol

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
    # Promoted out of `observation` by migration 006 (issue #63): both carry fields the
    # generic shape has no legal home for (two dates on a resolved problem; an allergy's
    # machine-readable criticality) and both need a subject discriminator in the key so a
    # relative's diagnosis never dedups into the patient's own problem list.
    "allergy": {
        "substance": (str, True),
        "reaction": (str, False),
        "criticality": (str, False),
        "noted_on": (str, False),
    },
    "condition": {
        "name": (str, True),
        "status": (str, True),
        "onset_on": (str, False),
        "resolved_on": (str, False),
        "relation": (str, False),
        "note": (str, False),
    },
}

KNOWN_TYPES = tuple(FIELD_SPECS)

# Key-machinery columns on every record table. Internal: they are stripped from the
# CLI `--json` and MCP read payloads (unstable, not part of either contract).
INTERNAL_COLUMNS = ("dedup_key", "dedup_base", "dedup_occurrence")

# Human-attestation provenance columns on every record table (migration 009, issue #110).
# Deliberately NOT in :data:`INTERNAL_COLUMNS` — those are stripped from `--json`/MCP
# payloads, and provenance is the one thing that must stay visible. Equally deliberately
# NOT in :data:`FIELD_SPECS`: like ``document_id`` they are never accepted from an agent's
# extraction JSON, and their absence from the specs is what keeps every dedup key
# bit-identical for attested and document-sourced rows alike.
ATTESTATION_COLUMNS = ("attested_by", "attested_on", "attested_at")

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
    "allergy": frozenset({"noted_on"}),
    "condition": frozenset({"onset_on", "resolved_on"}),
}

# Closed vocabularies per record type, enforced by validate_row right after the
# DATE_FIELDS check. Only fields the read layer *branches on* belong here: a typo'd
# `condition.status='inactive'` would vanish from every rendered section rather than
# show up wrong, which is the failure mode worth a hard reject. Membership is tested on
# :func:`enum_token` (case/space/underscore/hyphen-insensitive) but the **verbatim**
# value is stored, mirroring how `_norm_unit` compares case-insensitively while display
# casing survives.
ENUM_FIELDS: dict[str, dict[str, frozenset[str]]] = {
    "allergy": {"criticality": frozenset({"high", "low", "unable-to-assess"})},
    "condition": {
        "status": frozenset({"active", "resolved", "history", "family-history"})
    },
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


def enum_token(value: object) -> str:
    """Canonical comparison form of an :data:`ENUM_FIELDS` value.

    Case-, space-, underscore- and hyphen-insensitive: ``"Unable to Assess"``,
    ``"unable_to_assess"`` and ``"unable-to-assess"`` all reduce to
    ``unable-to-assess``, so a source's own casing/punctuation validates while the
    verbatim value is what gets stored. Also the comparison the read layer uses when it
    branches on a stored ``status`` (render sections, timeline) — reading the raw column
    would miss a row stored as ``"Family History"``.
    """
    if value is None:
        return ""
    return _collapse(str(value)).replace(" ", "-")


def _date_only(value: object) -> str:
    """Date portion of an ISO datetime/date string ('2026-01-02T09:00' -> '2026-01-02').

    The identity granularity for every dated type except ``observation``: medication
    start / procedure date / appointment date, and — since issue #117 —
    ``lab_result.collected_at``, whose documents state the same draw at mixed precision.
    """
    if value is None:
        return ""
    text = str(value).strip()
    return text.replace("T", " ").split(" ")[0]


def _norm_ts(value: object) -> str:
    """Normalize an ISO date/datetime for use as a dedup-key identity part, keeping
    *full precision* (unlike :func:`_date_only`, which truncates to the date).

    Its one remaining consumer is ``observation.observed_at``: a timestamped
    observation (``2026-01-02T09:00``) and a bare-date one (``2026-01-02``) stay
    distinct, and two observations on the same day at different times keep distinct
    keys, so serial same-day repeats survive as separate rows. A re-read/correction of
    the *same* observation carries the same timestamp, collides, and surfaces as a
    CONFLICT via :data:`_COMPARE_FIELDS`. Only the ``T``/space separator and
    surrounding/collapsed whitespace are normalized, so trivial formatting differences
    don't fork the key."""
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
        return [person_id, kt("test_name"), _date_only(row.get("collected_at"))]
    if record_type == "medication":
        return [person_id, n("name"), _collapse(str(row.get("dose") or "")),
                _date_only(row.get("started_on"))]
    if record_type == "procedure":
        return [person_id, n("name"), _date_only(row.get("performed_on"))]
    if record_type == "appointment":
        return [person_id, n("provider"), _date_only(row.get("scheduled_for"))]
    if record_type == "observation":
        return [person_id, n("obs_type"), _norm_ts(row.get("observed_at")), kt("key")]
    # allergy/condition are DATE-FREE by design: they are standing facts restated on
    # every document with inconsistent or absent dates, so a date in the key would fork
    # one allergy into one row per document. The dates are payload, and a disagreement
    # in them stages a conflict (issue #63 design).
    if record_type == "allergy":
        return [person_id, n("substance")]
    if record_type == "condition":
        return [person_id, n("name"), _condition_subject(row, dictionary)]
    raise ValidationError(f"unknown record type: {record_type}")  # guarded by validate()


def _condition_subject(row: dict, dictionary: dict[str, str] | None = None) -> str:
    """Whose condition this is — the discriminator that keeps a relative's diagnosis out
    of the patient's own problem list.

    ``self`` for every status but ``family-history``, which keys on the relative instead
    (``family:mother``). Two relatives with the same disease therefore stay distinct rows,
    and neither collides with the patient's.
    """
    if enum_token(row.get("status")) != "family-history":
        return "self"
    return "family:" + norm(row.get("relation"), dictionary)


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
        allowed = ENUM_FIELDS.get(record_type, {}).get(name)
        if allowed is not None and enum_token(value) not in allowed:
            raise ValidationError(
                f"{record_type}.{name}: expected one of "
                f"{', '.join(sorted(allowed))}, got {value!r}"
            )


def _type_names(types: object) -> str:
    if isinstance(types, tuple):
        return "/".join(t.__name__ for t in types)
    return types.__name__  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Attestation provenance (issue #110)
# --------------------------------------------------------------------------- #

def attestation_state(row: sqlite3.Row | dict) -> str:
    """Which of the three provenance states a stored row is in (migration 009):

        ``""``            document-sourced — the only state before this feature;
        ``"attested"``    a live human attestation, still needing a source document;
        ``"superseded"``  attested, and since backed by a real document.

    The single place the predicates are written down, so nothing else has to re-derive
    "is this row unsourced" from two nullable columns. A pre-009 row (a snapshot restored
    from before the migration) has no attestation columns at all and reads as
    document-sourced, which is exactly what it is.
    """
    row_map = dict(row)
    if not (row_map.get("attested_by") or "").strip():
        return ""
    return "superseded" if row_map.get("document_id") is not None else "attested"


def is_attested(row: sqlite3.Row | dict) -> bool:
    """True for a **live** attestation — attested and not yet backed by a document.

    This is the render/query predicate: a superseded row has real document provenance
    now, so it renders as an ordinary fact (its attestation survives as history on the
    row, not as a marker on the page).
    """
    return attestation_state(row) == "attested"


def public_row(row: sqlite3.Row | dict) -> dict:
    """A record row reduced to its published read contract (CLI ``--json`` / MCP).

    :data:`INTERNAL_COLUMNS` always go — unstable key machinery, never part of either
    contract. The attestation columns go only when they are **NULL**: a document-sourced
    row keeps the exact key set it had before migration 009 (the additive-only
    guarantee), while an attested row carries its provenance, which is the whole point of
    the feature and must never be quietly stripped.

    One function rather than one rule per front door: a payload that discloses provenance
    at the CLI but not over MCP is the drift this feature can least afford.
    """
    return {
        name: value for name, value in dict(row).items()
        if name not in INTERNAL_COLUMNS
        and not (name in ATTESTATION_COLUMNS and value is None)
    }


# --------------------------------------------------------------------------- #
# Commit extraction (new / duplicate / conflict split)
# --------------------------------------------------------------------------- #

@dataclass
class CommitSummary:
    new: list[tuple[str, int]] = field(default_factory=list)          # (type, row_id)
    duplicate: list[tuple[str, str]] = field(default_factory=list)    # (type, dedup_key)
    enriched: list[tuple[str, int]] = field(default_factory=list)     # (type, row_id)
    conflict: list[tuple[str, int]] = field(default_factory=list)     # (type, conflict_id)
    # Live attestations this commit backed with a real document (issue #110).
    promoted: list[tuple[str, int]] = field(default_factory=list)     # (type, row_id)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "new": len(self.new),
            "duplicate": len(self.duplicate),
            "enriched": len(self.enriched),
            "conflict": len(self.conflict),
            "promoted": len(self.promoted),
        }


# Payload fields compared to decide duplicate-vs-conflict once dedup_keys match.
# Identity fields (those feeding the dedup_key) are equal by construction — a
# difference there yields a *different* key and hence a new row, never a collision —
# so they are deliberately excluded here. The measured value is deliberately NOT an
# identity field: the lab and observation keys carry temporal identity — the collection
# *date* for lab_result (issue #117), the full observed_at timestamp for observation —
# but not the value, so a corrected or re-read value on an otherwise-matching key
# collides and surfaces here as a CONFLICT instead of a silent duplicate — hence
# value_num/value_text appear below.
_COMPARE_FIELDS: dict[str, list[str]] = {
    "lab_result": ["value_num", "value_text", "unit", "ref_low", "ref_high", "flag", "loinc"],
    "medication": ["route", "frequency", "ended_on", "prescriber", "status"],
    "procedure": ["provider", "outcome"],
    "appointment": ["specialty", "reason", "summary"],
    "observation": ["value_num", "value_text", "unit"],
    "allergy": ["reaction", "criticality", "noted_on"],
    "condition": ["status", "onset_on", "resolved_on", "relation", "note"],
}

# Types whose rows are standing facts restated across documents. For these, an absent
# field means "this document didn't say" — never "the value was cleared" (issue #63).
# That reading is asymmetric, and both halves matter:
#
#   incoming None over a stored value -> silence. Re-ingesting next year's health
#       summary, which reprints the same 8 allergies but omits criticality, must not
#       stage 8 conflicts that mean nothing.
#   stored None under a stated incoming value -> a GAIN, not silence. The document is
#       supplying a fact the record simply lacked; there is no competing value to
#       adjudicate, so it is neither a conflict nor a duplicate to drop. The stored
#       row's NULL columns are filled in place (:func:`_sparse_gains`) and the commit
#       reports it as `enriched`.
#
# Present-and-different still always conflicts, and `condition.status` is required so a
# lifecycle change (active -> resolved) is never skipped. For the date-keyed types
# (labs, meds ...) a cleared field IS news, so they keep the strict comparison and can
# never produce a gain.
_SPARSE_TYPES = frozenset({"allergy", "condition"})


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
    row of the same submission).

    For a :data:`_SPARSE_TYPES` row this answers "is there anything to *adjudicate*",
    which is not the same as "is there nothing to write": a field the incoming row
    states over a stored NULL agrees here but is reported by :func:`_sparse_gains`."""
    existing_map = dict(existing)
    sparse = record_type in _SPARSE_TYPES
    for name in _COMPARE_FIELDS[record_type]:
        ev = existing_map.get(name)
        iv = incoming.get(name)
        if ev is None and iv is None:
            continue
        # Standing facts: one side simply not stating a field is silence, not a change.
        if sparse and (ev is None or iv is None):
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


def _sparse_gains(
    record_type: str, existing: sqlite3.Row | dict, incoming: dict
) -> dict:
    """Payload fields the incoming row states that the stored row is missing.

    Only :data:`_SPARSE_TYPES` can gain: for every other type a stored NULL under a
    stated incoming value is a disagreement, so it conflicts and never reaches here.
    Empty dict = the incoming row adds nothing (a plain duplicate).

    Only NULL columns are filled — a stated value never overwrites a stored one, so
    enrichment can't launder a disagreement into a silent overwrite (that path still
    stages a conflict via :func:`_rows_equal`)."""
    if record_type not in _SPARSE_TYPES:
        return {}
    existing_map = dict(existing)
    return {
        name: incoming[name]
        for name in _COMPARE_FIELDS[record_type]
        if existing_map.get(name) is None and incoming.get(name) is not None
    }


def _enrich_record(
    conn: sqlite3.Connection, record_type: str, row_id: int, gains: dict
) -> None:
    """Fill a stored row's NULL payload columns from a later document's statement.

    ``document_id`` is deliberately left alone: it records which document the row
    (and its identity) came from, and the identity fields are unchanged here. A row
    attested by several documents already has this limitation for plain duplicates —
    enrichment doesn't deepen it."""
    assignments = ", ".join(f"{name} = ?" for name in gains)
    conn.execute(
        f"UPDATE {record_type} SET {assignments} WHERE {record_type}_id = ?",
        [*gains.values(), row_id],
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
                # Adding times only helps where the key still keeps the time
                # component, which after issue #117 is `observation` alone —
                # lab_result keys on the collection *date*, so for it that advice
                # cannot separate the rows.
                time_hint = (
                    "If the source gives distinct times, add them (AGENTS.md "
                    "date-precision rule). "
                    if record_type == "observation"
                    else ""
                )
                raise ValidationError(
                    f"{record_type}: rows {prior[0]} and {index} of this submission "
                    f"derive the same dedup_key "
                    f"({identity_label(record_type, row, person_id, dictionary)}) "
                    f"but carry different values. {time_hint}If these are two genuine "
                    "same-day results, commit them in separate submissions and "
                    "resolve the conflict with `--keep both`"
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
                    twin_id = int(twin[f"{record_type}_id"])
                    # The document confirms a fact the family attested before it had a
                    # source (issue #110). Payloads agree, so by construction there is
                    # nothing to adjudicate: this is the duplicate path, and the stored
                    # NULL being filled happens to be `document_id`. Framed exactly as
                    # #63 framed enrichment — a stored NULL under a stated incoming value
                    # is a GAIN, not a disagreement. A *differing* payload never reaches
                    # here; it stages a conflict below, which is the honest handling of
                    # "the document contradicts what we were told".
                    promoted = is_attested(twin)
                    if promoted:
                        _promote_attestation(conn, record_type, twin_id, document_id)
                        summary.promoted.append((record_type, twin_id))
                    # A standing fact whose stored row lacks a field this document
                    # states is not a duplicate to drop — fill the NULL in place
                    # (issue #63). Nothing to adjudicate, so no conflict is staged,
                    # but the write is reported rather than being invisible.
                    gains = _sparse_gains(record_type, twin, row)
                    if gains:
                        _enrich_record(conn, record_type, twin_id, gains)
                        summary.enriched.append((record_type, twin_id))
                    elif not promoted:
                        # `duplicate` means "nothing was written". A promotion writes, so
                        # it is reported under its own bucket instead of both.
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


def conflicts_anchored_to_row(
    conn: sqlite3.Connection,
    record_type: str,
    row: sqlite3.Row,
    dictionary: dict[str, str] | None = None,
) -> list[int]:
    """Open conflict ids whose identity family contains ``row`` (issue #107).

    The row-level counterpart of ``documents._conflicts_anchored_to``, which asks
    the same question of a whole document with cheaper SQL. Cheaper, but coarser:
    it matches on ``dedup_key`` and so misses a family whose occurrence 0 is
    already gone. A single-row removal cannot afford that miss — the row it is
    about to delete may *be* the anchor — so this walks each open conflict of the
    type through :func:`_conflict_family`, the same resolution a real resolution
    uses. Public because :mod:`pemr.records` needs the engine's own anchor logic
    rather than a re-derivation of it.
    """
    if record_type not in FIELD_SPECS:
        return []
    pk = f"{record_type}_id"
    row_id = int(row[pk])
    ids: list[int] = []
    for conflict in conn.execute(
        "SELECT * FROM conflict WHERE status = 'open' AND record_type = ? "
        "ORDER BY conflict_id",
        (record_type,),
    ).fetchall():
        _, family = _conflict_family(conn, conflict, dictionary)
        if any(int(member[pk]) == row_id for member in family):
            ids.append(int(conflict["conflict_id"]))
    return ids


def _insert_record(
    conn: sqlite3.Connection,
    record_type: str,
    row: dict,
    person_id: int,
    document_id: int | None,
    base: str,
    occurrence: int = 0,
    *,
    attestation: tuple[str, str, str] | None = None,
) -> int:
    """Insert one validated row. ``attestation`` is ``(by, on, at)`` for a row whose
    provenance is a person rather than a document (issue #110); every other caller leaves
    it ``None`` and the columns stay NULL, exactly as before migration 009."""
    columns = ["person_id", "document_id", "dedup_key", "dedup_base", "dedup_occurrence"]
    values: list[object] = [
        person_id, document_id, occurrence_key(base, occurrence), base, occurrence,
    ]
    if attestation is not None:
        columns.extend(ATTESTATION_COLUMNS)
        values.extend(attestation)
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


def _promote_attestation(
    conn: sqlite3.Connection, record_type: str, row_id: int, document_id: int
) -> None:
    """Back a live attestation with the document that just confirmed it (issue #110).

    Sets ``document_id`` on the attested row and touches nothing else — not the payload,
    not the keys, not the attestation columns, which stay as the history of what the
    record knew before the document arrived. The row simply stops being renderable as
    unsourced.

    Confined to ``NULL -> id`` on a row that carries ``attested_by``: this is the only
    path outside conflict resolution that writes ``document_id`` onto a stored row, and it
    can never overwrite an existing one. The complement of :func:`_enrich_record`, which
    deliberately leaves provenance alone because there the NULL is a payload column; here
    provenance *is* the NULL being filled. A rowcount other than 1 is an error, never a
    silent success (the :func:`_overwrite_record` guard idiom).
    """
    cur = conn.execute(
        f"UPDATE {record_type} SET document_id = ? "
        f"WHERE {record_type}_id = ? AND document_id IS NULL "
        "AND attested_by IS NOT NULL",
        (document_id, row_id),
    )
    if cur.rowcount != 1:
        raise ValidationError(
            f"promoting {record_type} row {row_id} to document {document_id} matched no "
            "live attestation - refusing to record the promotion"
        )


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
    # Sparse-type fields the matched sibling was missing and the staged payload
    # supplies; filled in place so a no-op still can't drop stated data (issue #63).
    gains: dict = field(default_factory=dict)


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
        elif keep == "both" and result.no_op:
            if result.gains:
                _enrich_record(
                    conn, record_type, int(result.row_id), result.gains
                )
        elif keep == "both":
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
        matched = f"keep-both (no-op: matches {result.record_type} #{result.row_id}"
        if result.gains:
            matched += f"; filled {', '.join(sorted(result.gains))}"
        return matched + ")"
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
        # must not produce a twin row. On a sparse type the matched sibling may be
        # strictly thinner than the staged payload (it "matches" because an absent
        # field is silence); absorbing it would drop the fields the payload adds, so
        # the no-op still fills those NULLs (issue #63).
        return ResolveResult(
            kept="both", record_type=record_type, row_id=int(twin[pk]),
            occurrence=int(twin["dedup_occurrence"]),
            dedup_key=twin["dedup_key"], dedup_base=twin["dedup_base"], no_op=True,
            gains=_sparse_gains(record_type, twin, incoming),
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
    if record_type in ("medication", "procedure", "condition"):
        return row["name"] or ""
    if record_type == "allergy":
        return row["substance"] or ""
    if record_type == "appointment":
        return " ".join(p for p in (row["provider"], row["scheduled_for"]) if p)
    return " ".join(p for p in (row["obs_type"], row["key"]) if p)  # observation


class CollisionResolver(Protocol):
    """How :func:`rekey` asks the curation overlay whether a human already settled a
    collision (issue #116) — a structural type, deliberately not an import.

    ``curation`` imports *this* module, so the dependency can only run one way: the
    implementation lives in :class:`curation.CollisionResolver`, the CLI builds it, and
    `rekey` takes it as an argument. A lazy ``import curation`` inside `rekey` would
    silently reintroduce the cycle this seam exists to prevent.
    """

    def covering(
        self, record_type: str, row: sqlite3.Row, clash: sqlite3.Row
    ) -> dict | None:
        """The verdict settling this pair's identity, or None if none does."""

    def settlement(self, verdict: dict) -> str:
        """Which way ``verdict`` settled the pair (issue #122): ``"merged"`` — the two
        rows are one fact — or ``"distinct"`` — they are two different facts that
        recompute onto one key. Both let the rekey proceed and both file the clash row as
        a new occurrence; the difference is what the operator is told, and (outside this
        module) that a distinct-resolved pair keeps rendering as two live siblings."""

    def narrow(
        self,
        conn: sqlite3.Connection,
        record_type: str,
        verdict: dict,
        covered_row_ids: Iterable[int],
        base_map: dict[str, str],
    ) -> tuple[str, list[int]]:
        """Pin ``verdict`` to the rows it already covered, so resolution cannot widen
        it over the row that survived; caller-managed transaction. Returns
        ``(action, pinned_row_ids)`` with action ``"unchanged"`` | ``"narrowed"``."""


@dataclass
class RekeyChange:
    record_type: str
    row_id: int
    label: str
    old_key: str
    new_key: str
    new_base: str = ""      # recomputed dedup_base (new_key == new_base at occurrence 0)
    # Set only for a row whose collision a verdict resolved (issue #116): it moves onto
    # the surviving family as a new occurrence. None -- every other change -- means the
    # stored occurrence is kept, which is what `rekey` has always done.
    new_occurrence: int | None = None


@dataclass
class RekeyResolution:
    """A collision a recorded human verdict settled, instead of blocking on (issue #116).

    The pair still recomputes onto one identity; what the verdict supplies is the answer
    `rekey` cannot derive — that a human already ruled on these two rows. The ruling comes
    in two kinds, reported as :attr:`settlement` (issue #122): ``"merged"`` says the pair
    is **one fact**, ``"distinct"`` says it is **two facts** whose extracted labels are
    generic enough to recompute onto one key. Both are settled rather than blocking, and
    both take the same write: the clash row is filed as the next free **occurrence** of
    the shared family, the shape a ``--keep both`` conflict resolution produces, rather
    than left on its stored key — a row left behind would stay permanently drifted, and
    :func:`_assert_no_key_drift` would refuse the next ingest deriving that identity
    while this very command reported clean. A distinct-resolved pair therefore renders as
    two live siblings, which is exactly what "two facts" has to mean.

    Resolution merges a judged family into a larger one, so the authorizing verdict is
    **narrowed** to the rows it already covered rather than moved onto the surviving
    base: a ruling keeps exactly the extension it had when it was made, and the row that
    was never judged stays live in its own section. ``covered_row_ids`` is that
    extension; ``narrowed_row_ids`` is what the write actually pinned.
    """
    record_type: str
    row_id: int
    label: str
    clash_row_id: int
    clash_label: str
    kind: str               # "fused" | "doubled", as RekeyCollision — diagnostic only
    status: str             # the verdict's status (one of curation.RESOLVING_STATUSES)
    scope: str              # "family" | "row"
    record_id: int          # the verdict's row id; 0 for family scope
    verdict_base: str       # dedup_base the verdict is stored under
    new_base: str           # the surviving family both rows land in
    new_occurrence: int     # occurrence the clash row takes under new_base
    message: str            # full account, ready to print
    # Which question the verdict answered (issue #122): "merged" — the pair is one fact —
    # or "distinct" — two facts sharing a recomputed key. Kept apart from `status` on
    # purpose: `status` is the raw curation vocabulary and may grow, `settlement` is the
    # stable two-valued contract the CLI reports. Defaults to the #116 answer, so a
    # resolution built without one reads as it always did.
    settlement: str = "merged"
    # Rows the verdict covered before this rekey — its extension at ruling time. For a
    # row-scoped verdict, the one row it names.
    covered_row_ids: list[int] = field(default_factory=list)
    # "unchanged" | "narrowed" | "withheld", filled in by the write; "" on a dry run,
    # which writes nothing.
    verdict_action: str = ""
    # The rows a "narrowed" write pinned the verdict to (empty for every other action).
    narrowed_row_ids: list[int] = field(default_factory=list)


@dataclass
class RekeyCollision:
    """Two rows in one table that recompute onto a single ``dedup_key``.

    Collected, not raised (issue #92): a collision in ``allergy`` says nothing about
    ``condition``, so it blocks its own table and no other. ``kind`` names which of the
    two causes it is — see :func:`rekey`.
    """
    record_type: str
    row_id: int
    label: str
    clash_row_id: int
    clash_label: str
    kind: str               # "fused" (dictionary merges two facts) | "doubled" (data)
    message: str            # full diagnosis, ready to print


@dataclass
class RekeyReport:
    scanned: dict[str, int] = field(default_factory=dict)      # type -> rows examined
    changes: list[RekeyChange] = field(default_factory=list)
    collisions: list[RekeyCollision] = field(default_factory=list)
    # Collisions a human verdict settled (issue #116). Deliberately NOT part of
    # `blocked`/`writable()`: a resolved pair does not quarantine its table.
    resolved: list[RekeyResolution] = field(default_factory=list)
    applied: bool = False

    @property
    def blocked(self) -> list[str]:
        """Record types left untouched because they hold at least one collision."""
        return sorted({c.record_type for c in self.collisions})

    def writable(self) -> list[RekeyChange]:
        """The subset of :attr:`changes` that is safe to write: every table that has no
        collision. This is what ``apply=True`` actually writes."""
        blocked = set(self.blocked)
        return [c for c in self.changes if c.record_type not in blocked]


@dataclass
class _RekeyEntry:
    """One scanned row plus what the current dictionary makes of it.

    Mutable on purpose: adjudicating a verdict-resolved collision moves the entry to a
    new occurrence, and the *next* clash in the same family has to see that move when it
    picks its own free occurrence.
    """
    row: sqlite3.Row
    row_id: int
    payload: dict
    base: str               # recomputed dedup_base
    occurrence: int
    key: str                # occurrence_key(base, occurrence)


def rekey(
    conn: sqlite3.Connection,
    dictionary: dict[str, str] | None = None,
    *,
    apply: bool = False,
    resolver: CollisionResolver | None = None,
) -> RekeyReport:
    """Recompute every stored ``dedup_key`` under the *current* dictionary.

    A ``dedup_key`` is frozen at commit time, so adding a synonym (``cl`` ->
    ``chloride``) changes the key a future commit computes for a fact already in the
    DB: layer-2 dedup misses and the same fact lands twice. This walks the record
    tables and re-derives each key, so stored rows keep deduping after a dictionary
    edit. Values, provenance and row ids are untouched — only the key-machinery columns
    move (``dedup_occurrence`` among them, but for a verdict-resolved row only; see
    ``resolver`` below).

    Dry-run by default: pass ``apply=True`` to write. Two rows in one table that
    recompute to the same key are a **collision**, and each one names which of the two
    causes it is:

    * ``"fused"`` — the rows carry *different* payloads, so the dictionary would merge
      two distinct facts (typically two methods for one analyte off one draw): fix the
      dictionary;
    * ``"doubled"`` — the rows carry the *same* payload, so one fact was filed twice,
      once under a pre-drift key, and it is the data that needs fixing.
      :func:`_assert_no_key_drift` refuses the ingest that would create this state, so
      it should only be reachable in a database that drifted before that guard existed.

    Collisions are **collected, never raised** (issue #92). The tables are scanned
    independently, so a collision quarantines only its own table: scanning always covers
    every table and reports every collision it finds (a dry run is a survey and must not
    stop at the first problem), and ``apply=True`` writes
    :meth:`RekeyReport.writable` — the changes of the tables that came out clean —
    leaving the colliding tables on their stored keys and naming them in
    :attr:`RekeyReport.blocked`. Callers surface a non-empty ``collisions`` as a failure;
    partial progress with an explicit account of what was skipped beats all-or-nothing.

    ``resolver`` is the optional curation-overlay seam (issues #116, #122). Many
    collisions are exactly the pair a human already ruled on — since migrations 008/010 a
    ``merged-into`` or ``superseded`` verdict says "these are one fact", and since
    migration 011 a ``distinct`` verdict says "these are two different facts whose
    extracted labels merely recompute onto one key" — either way the very question the
    guard is stuck on. Both are reported as :attr:`RekeyResolution.settlement`
    (``"merged"`` / ``"distinct"``) and both take the same write, because "two live rows
    on one identity" already has exactly one representation: occurrence numbering, the
    ``--keep both`` shape that renders as two independent siblings. With a resolver
    injected, a clash whose pair carries such a verdict (either row, either scope) is
    **not** blocking: the clash row
    is filed as the next free occurrence of the surviving family — the shape a
    ``--keep both`` conflict resolution produces — and reported in
    :attr:`RekeyReport.resolved` instead of :attr:`RekeyReport.collisions`, so the table
    still writes. Under ``apply=True`` the authorizing verdict is **narrowed to row
    scope** in the *same* transaction as the keys, pinned to exactly the rows it covered
    before the merge: the surviving family is bigger than the one the human ruled on, so
    a verdict carried over wholesale would extend a ruling over a row nobody judged and
    (for the appendix statuses) pull that live row out of its rendered section. With
    ``resolver=None`` (the default) every collision blocks, exactly as before.
    """
    db.require_migrated(conn)
    report = RekeyReport(applied=False)
    # Stored dedup_base -> recomputed base, per table: how a family verdict's merge
    # target is followed when the target family moved in this same run.
    base_maps: dict[str, dict[str, str]] = {}
    # (record_type, verdict, resolution) — the narrowings the write block owes. Kept
    # beside the report rather than inside it so RekeyResolution stays a plain,
    # serializable account of what happened (the raw verdict dict is not part of it).
    pending_narrowings: list[tuple[str, dict, RekeyResolution]] = []

    for record_type in KNOWN_TYPES:
        pk = f"{record_type}_id"
        # ORDER BY the primary key: incumbency now decides which row of a resolved pair
        # keeps its occurrence, so the scan order has to be stable rather than whatever
        # the table happens to yield.
        rows = conn.execute(f"SELECT * FROM {record_type} ORDER BY {pk}").fetchall()
        report.scanned[record_type] = len(rows)

        # Pass 1 (scan): what the current dictionary makes of every row. Collected in
        # full before anything is adjudicated, because giving a verdict-resolved clash a
        # free occurrence needs its whole recomputed family in hand.
        entries: list[_RekeyEntry] = []
        by_base: dict[str, list[_RekeyEntry]] = {}
        base_map: dict[str, str] = {}
        # Stored dedup_base -> the row ids filed under it *before* this rekey: a
        # family-scoped verdict's extension at ruling time, which is what a resolution
        # narrows it to rather than widening it over the family that survives.
        stored_family: dict[str, list[int]] = {}
        for row in rows:
            payload = {name: row[name] for name in FIELD_SPECS[record_type]}
            # The occurrence is a stored column, so an admitted repeat (`--keep both`)
            # recomputes to its own key rather than colliding with its sibling.
            occurrence = int(row["dedup_occurrence"] or 0)
            base = dedup_key(record_type, payload, row["person_id"], dictionary)
            entry = _RekeyEntry(
                row=row, row_id=row[pk], payload=payload, base=base,
                occurrence=occurrence, key=occurrence_key(base, occurrence),
            )
            entries.append(entry)
            by_base.setdefault(base, []).append(entry)
            base_map[row["dedup_base"]] = base
            stored_family.setdefault(row["dedup_base"], []).append(entry.row_id)
        base_maps[record_type] = base_map

        # Pass 2 (adjudicate): first-seen wins the key; anything landing on a taken one
        # is either settled by a human verdict or a blocking collision.
        seen: dict[str, _RekeyEntry] = {}
        for entry in entries:
            clash = seen.get(entry.key)
            if clash is None:
                seen[entry.key] = entry
                if entry.key != entry.row["dedup_key"]:
                    report.changes.append(RekeyChange(
                        record_type, entry.row_id,
                        _rekey_label(record_type, entry.row),
                        entry.row["dedup_key"], entry.key, entry.base,
                    ))
                continue

            label, clash_label = (_rekey_label(record_type, entry.row),
                                  _rekey_label(record_type, clash.row))
            kind = ("doubled" if _rows_equal(record_type, clash.row, entry.payload)
                    else "fused")
            verdict = (None if resolver is None
                       else resolver.covering(record_type, entry.row, clash.row))
            if verdict is not None:
                covered = ([int(verdict["record_id"])] if verdict["record_id"]
                           else stored_family.get(verdict["dedup_base"], []))
                resolution = _resolve_clash(
                    report, record_type, pk, entry, clash, kind, verdict,
                    by_base[entry.base], seen, label, clash_label, covered,
                    resolver.settlement(verdict),
                )
                pending_narrowings.append((record_type, verdict, resolution))
                continue

            if kind == "doubled":
                # Same payload, two keys: not a dictionary fault at all - one fact
                # was filed twice, once under a pre-drift key and once under the
                # current one. Say so, because "fix the dictionary" is exactly the
                # wrong advice here (`_assert_no_key_drift` now stops new ingests
                # from reaching this state).
                message = (
                    f"{record_type}: {pk} {entry.row_id} and {pk} {clash.row_id} "
                    f"({label!r}) hold the SAME fact "
                    "under two dedup_keys - it was filed a second time by an "
                    "ingest that ran against drifted keys before this rekey. The "
                    "dictionary is fine; the data is doubled. Drop whichever row "
                    "is the degraded copy with "
                    f"`pemr record rm {record_type} {entry.row_id}` (or "
                    f"`... {clash.row_id}`) - dry run first, then --apply - and "
                    "re-run; `pemr document rm` is the whole-document option when "
                    "the re-filing document holds nothing else worth keeping; "
                    f"{record_type} was not written"
                )
            else:
                message = (
                    f"{record_type}: {pk} {entry.row_id} ({label!r}) and "
                    f"{pk} {clash.row_id} ({clash_label!r}) recompute to the same "
                    "dedup_key - the dictionary maps two distinct facts onto one "
                    f"canonical name; {record_type} was not written"
                )
            report.collisions.append(RekeyCollision(
                record_type, entry.row_id, label, clash.row_id, clash_label,
                kind, message,
            ))
            # Keep scanning: report-only mode is a survey, so the run must find
            # every collision in every table rather than stop at the first. The
            # first-seen row stays the incumbent for this key, so a third row on it
            # is reported against the same anchor instead of chaining.

    # A resolved pair in a table that *also* holds an unresolved collision is withheld
    # with the rest of that table (`writable()` is per-table, not per-row). The account
    # has to say so: the adjudication message alone would describe a write that never
    # happened.
    blocked = set(report.blocked)
    for resolution in report.resolved:
        if resolution.record_type in blocked:
            resolution.message += (
                f" - withheld: {resolution.record_type} still holds an unresolved "
                "collision, so nothing in this table was written"
            )
            # Recorded here, not in the write block: a fully blocked run has nothing
            # writable and never enters it, and the account must still say why.
            if apply:
                resolution.verdict_action = "withheld"

    writable = report.writable()
    if apply and writable:
        # One transaction: a half-rekeyed table dedups inconsistently. Two passes,
        # because `UNIQUE(dedup_key)` is enforced per statement: if two rows swap keys
        # (or one takes a key another is about to vacate) a single-pass update trips
        # the index mid-flight. Park every moving row on a unique placeholder first.
        with conn:
            for change in writable:
                conn.execute(
                    f"UPDATE {change.record_type} SET dedup_key = ? "
                    f"WHERE {change.record_type}_id = ?",
                    (f"rekey-pending-{change.record_type}-{change.row_id}",
                     change.row_id),
                )
            for change in writable:
                # dedup_base moves with the key: base and key are injective in each
                # other for a fixed occurrence, so a changed key means a changed base.
                columns = "dedup_key = ?, dedup_base = ?"
                values: list[object] = [change.new_key, change.new_base]
                if change.new_occurrence is not None:
                    # A verdict-resolved row joins the surviving family as a sibling.
                    # The occurrence is a stored column, so it has to move with the key
                    # or `_assert_no_key_drift` would read the row as drifted forever.
                    columns += ", dedup_occurrence = ?"
                    values.append(change.new_occurrence)
                values.append(change.row_id)
                conn.execute(
                    f"UPDATE {change.record_type} SET {columns} "
                    f"WHERE {change.record_type}_id = ?",
                    values,
                )
            # Same transaction as the keys they authorized: a committed rekey whose
            # authorizing verdict was left in the wrong shape is the failure this seam
            # exists to avoid. Skipped for a blocked table, whose changes were withheld
            # anyway - recorded as "withheld" so the account never implies otherwise.
            # One verdict may settle several clashes at once (two occurrences of a
            # judged family landing on two occurrences of the surviving one), so the
            # narrowing is done once per verdict and reported on every resolution it
            # authorized - narrowing twice would find its own first pass and no-op.
            done: dict[tuple[str, str, int], tuple[str, list[int]]] = {}
            for rec_type, verdict, resolution in pending_narrowings:
                if rec_type in blocked:      # already recorded as "withheld" above
                    continue
                ident = (rec_type, verdict["dedup_base"], verdict["record_id"])
                if ident not in done:
                    done[ident] = resolver.narrow(
                        conn, rec_type, verdict, resolution.covered_row_ids,
                        base_maps[rec_type],
                    )
                resolution.verdict_action, resolution.narrowed_row_ids = done[ident]
    report.applied = apply
    return report


def _resolve_clash(
    report: RekeyReport,
    record_type: str,
    pk: str,
    entry: _RekeyEntry,
    clash: _RekeyEntry,
    kind: str,
    verdict: dict,
    family: list[_RekeyEntry],
    seen: dict[str, _RekeyEntry],
    label: str,
    clash_label: str,
    covered_row_ids: list[int],
    settlement: str,
) -> RekeyResolution:
    """File a verdict-settled clash as the next free occurrence of its family.

    ``dedup_key`` is ``UNIQUE`` per table, so "both rows carry this identity" has exactly
    one representation available — occurrence numbering (migration 005). The incumbent
    keeps its occurrence and the later row takes the next free one, which is the state a
    ``--keep both`` conflict resolution leaves behind and exactly what row-scoped
    curation (issue #114) exists to annotate.

    ``settlement`` is which way the verdict settled the pair (issue #122) — ``"merged"``
    or ``"distinct"``. It selects the wording only: the write is identical either way,
    because the ``--keep both`` shape is *already* "two live siblings on one identity",
    which is what a distinct ruling asks for.

    Mutates ``entry`` and ``seen`` so a *third* row landing on this family sees the
    occupancy this one just created, and appends to ``report``.
    """
    occurrence = 1 + max(sibling.occurrence for sibling in family)
    # Defensive: the family's occupancy is derived from recomputed bases, so a stored
    # row that recomputes elsewhere could still be sitting on the key that number implies.
    while occurrence_key(entry.base, occurrence) in seen:
        occurrence += 1
    entry.occurrence = occurrence
    entry.key = occurrence_key(entry.base, occurrence)
    seen[entry.key] = entry

    scope = "row" if verdict["record_id"] else "family"
    # Present tense, not past: whether this actually lands depends on `apply` and on the
    # rest of the table coming out clean, and the caller appends that verdict.
    preamble = (
        f"{record_type}: {pk} {entry.row_id} ({label!r}) and {pk} {clash.row_id} "
        f"({clash_label!r}) recompute to the same dedup_key ({kind}), but a "
        f"{scope}-scoped '{verdict['status']}' verdict already "
    )
    if settlement == "distinct":
        message = (
            f"{preamble}rules them two distinct facts - {pk} {entry.row_id} takes "
            f"occurrence {occurrence} of {entry.base[:12]}..., the shared family, and "
            "both rows keep rendering as live, independent facts"
        )
    else:
        message = (
            f"{preamble}settles the pair - {pk} {entry.row_id} takes occurrence "
            f"{occurrence} of {entry.base[:12]}..., the surviving family"
        )
    report.changes.append(RekeyChange(
        record_type, entry.row_id, label, entry.row["dedup_key"], entry.key,
        entry.base, new_occurrence=occurrence,
    ))
    resolution = RekeyResolution(
        record_type=record_type, row_id=entry.row_id, label=label,
        clash_row_id=clash.row_id, clash_label=clash_label, kind=kind,
        status=verdict["status"], scope=scope, record_id=verdict["record_id"],
        verdict_base=verdict["dedup_base"], new_base=entry.base,
        new_occurrence=occurrence, message=message, settlement=settlement,
        covered_row_ids=sorted(covered_row_ids),
    )
    report.resolved.append(resolution)
    return resolution


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

    On a :data:`_SPARSE_TYPES` row, fields the incoming row does not state are left as
    stored rather than nulled: for a standing fact an absent field is "this document
    didn't say", so a single-field adjudication ("criticality: low, not high") must not
    also erase the reaction and noted_on the incoming document simply didn't repeat
    (issue #63). Fields it *does* state win, which is what keep-incoming means.

    When the anchor is a live attestation, writing ``document_id`` here **is** the
    conflict-flow supersession (issue #110): the human ruled for the document, so the row
    takes both its payload and its provenance, and the attestation columns stay behind as
    history. No extra code path — the assignment was already unconditional.
    """
    sparse = record_type in _SPARSE_TYPES
    assignments = ["document_id = ?"]
    values: list[object] = [document_id]
    for name in _COMPARE_FIELDS[record_type]:
        if sparse and incoming.get(name) is None:
            continue
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

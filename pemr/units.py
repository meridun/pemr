"""Display-time unit canonicalisation: a per-person canonical unit per measurement key.

Issue #136. Two clinics print two unit systems, so one household's `weight` rows arrive
as `kg` from one and `lb` from another - sometimes for the *same* person. Reading a
summary then means converting in your head, and a `trends` series that interleaves 77.6
and 171.0 is a wrong chart.

The fix is **display-only**, and that is the whole design (migration 013 carries the
argument in full):

  * ``commit-extraction`` still records the unit string exactly as extracted. No stored
    value or unit is ever mutated, and there is no corpus migration - a genuine
    unit-of-measure difference is not a spelling mistake, and rewriting a
    document-sourced row to match a preference would forge provenance.
  * A person may record one canonical **display** unit per measurement key
    (``person_unit_pref``, migration 013). ``render summary`` and ``query.trends``
    convert into it at read time and disclose every conversion; every other read path,
    and every write path, is untouched.
  * A person with no preference set sees exactly what they saw before this module
    existed.

The module has two clearly-marked halves, the :mod:`pemr.curation` precedent of pure
helpers plus ``conn``-taking readers in one file:

  * the **registry** - alias resolution, dimensions, conversion, display rounding. Pure
    arithmetic, no sqlite, and the thing that lets an *existing* row convert correctly:
    the unit's measurement system is derived from the stored unit string, so it works
    for rows committed long before this feature shipped, and needs no new column.
  * the **preference store** - the ``conn``-taking readers and writers over
    ``person_unit_pref``.

The registry is Python, deliberately **not** a ``data/dictionary.toml`` section: the
dictionary is user-grown medical *vocabulary* and an identity lever (it feeds
``dedup_key``), whereas a unit table is fixed physics and a display lever. Mixing them
would let a display edit move stored keys.

The registry holds **two populations** (issue #202). The *convertible* vitals dimensions
above - mass, length, temperature, pressure, rate, ratio - where two ids of one dimension
convert into each other. And the *single-member* ``lab:<id>`` dimensions, one dimension
per lab unit, which exist only so that two spellings of one unit compare equal: no lab
unit is ever convertible to another, because :func:`convert` returns ``None`` across
dimensions. That is a safety property, not a style: ``U/L`` equals ``IU/L`` and ``mEq/L``
equals ``mmol/L`` only for particular analytes, and ``mg/dL`` to ``mmol/L`` needs a molar
mass - none of which are registry facts. A shared "concentration" dimension with factors
would be exactly the wrong chart this module exists to prevent.

Canonical unit ids are ASCII (``degF``, never a degree sign) because they reach
CLI/render output, which must survive a cp1252/cp437 console. The alias table *accepts*
the non-ASCII spellings a document can carry, but never emits one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from . import persons
from .dedup import key_token

# --------------------------------------------------------------------------- #
# Registry (pure): aliases, dimensions, conversion
# --------------------------------------------------------------------------- #

#: Unit id -> ``(dimension, factor, offset)`` onto that dimension's base unit, where
#: ``base = value * factor + offset``. The offset is what makes temperature work: a
#: factor-only converter silently corrupts every degC/degF display.
#: Bases: mass=kg, length=cm, temperature=degC, pressure=mmHg, rate=/min, ratio=%.
UNITS: dict[str, tuple[str, float, float]] = {
    # mass (base kg)
    "kg": ("mass", 1.0, 0.0),
    "g": ("mass", 0.001, 0.0),
    "mg": ("mass", 1e-6, 0.0),
    "ug": ("mass", 1e-9, 0.0),
    "ng": ("mass", 1e-12, 0.0),
    "pg": ("mass", 1e-15, 0.0),
    "lb": ("mass", 0.45359237, 0.0),
    "oz": ("mass", 0.028349523125, 0.0),
    # length (base cm)
    "cm": ("length", 1.0, 0.0),
    "mm": ("length", 0.1, 0.0),
    "m": ("length", 100.0, 0.0),
    "in": ("length", 2.54, 0.0),
    "ft": ("length", 30.48, 0.0),
    # temperature (base degC) - affine, not a bare scale factor
    "degC": ("temperature", 1.0, 0.0),
    "degF": ("temperature", 5.0 / 9.0, -160.0 / 9.0),
    # single-member dimensions: nothing to convert *to*, but resolving them is what
    # keeps a cross-dimension preference from ever mixing scales silently.
    "mmHg": ("pressure", 1.0, 0.0),
    "/min": ("rate", 1.0, 0.0),
    "%": ("ratio", 1.0, 0.0),
    # --- lab units: single-member dimensions, never convertible (issue #202) ---
    # One dimension each, so `convert()` returns None between any two of them. These
    # exist to make two *spellings* of one unit compare equal, nothing more.
    # mass / volume
    "mg/dL": ("lab:mg/dL", 1.0, 0.0),
    "g/dL": ("lab:g/dL", 1.0, 0.0),
    "mg/L": ("lab:mg/L", 1.0, 0.0),
    "g/L": ("lab:g/L", 1.0, 0.0),
    "mg/mL": ("lab:mg/mL", 1.0, 0.0),
    "ug/dL": ("lab:ug/dL", 1.0, 0.0),
    "ug/L": ("lab:ug/L", 1.0, 0.0),
    "ug/mL": ("lab:ug/mL", 1.0, 0.0),
    "ng/dL": ("lab:ng/dL", 1.0, 0.0),
    "ng/mL": ("lab:ng/mL", 1.0, 0.0),
    "ng/L": ("lab:ng/L", 1.0, 0.0),
    "pg/mL": ("lab:pg/mL", 1.0, 0.0),
    # amount / volume
    "mmol/L": ("lab:mmol/L", 1.0, 0.0),
    "umol/L": ("lab:umol/L", 1.0, 0.0),
    "nmol/L": ("lab:nmol/L", 1.0, 0.0),
    "pmol/L": ("lab:pmol/L", 1.0, 0.0),
    "mEq/L": ("lab:mEq/L", 1.0, 0.0),
    "mOsm/kg": ("lab:mOsm/kg", 1.0, 0.0),
    # activity
    "U/L": ("lab:U/L", 1.0, 0.0),
    "U/mL": ("lab:U/mL", 1.0, 0.0),
    "IU/L": ("lab:IU/L", 1.0, 0.0),
    "IU/mL": ("lab:IU/mL", 1.0, 0.0),
    "mIU/L": ("lab:mIU/L", 1.0, 0.0),
    "mIU/mL": ("lab:mIU/mL", 1.0, 0.0),
    "uIU/mL": ("lab:uIU/mL", 1.0, 0.0),
    "mU/L": ("lab:mU/L", 1.0, 0.0),
    # counts
    "K/uL": ("lab:K/uL", 1.0, 0.0),
    "M/uL": ("lab:M/uL", 1.0, 0.0),
    "/uL": ("lab:/uL", 1.0, 0.0),
    "/HPF": ("lab:/HPF", 1.0, 0.0),
    "/LPF": ("lab:/LPF", 1.0, 0.0),
    # indices / other
    "fL": ("lab:fL", 1.0, 0.0),
    "mm/hr": ("lab:mm/hr", 1.0, 0.0),
    "mL/min": ("lab:mL/min", 1.0, 0.0),
    "mL/min/1.73m2": ("lab:mL/min/1.73m2", 1.0, 0.0),
    "mg/g": ("lab:mg/g", 1.0, 0.0),
    "sec": ("lab:sec", 1.0, 0.0),
}

#: Prefix of every single-member lab dimension. Membership of that population is read
#: off the dimension, so no second list of lab ids has to be maintained anywhere.
LAB_DIMENSION_PREFIX = "lab:"

#: Lowercased, whitespace-collapsed spelling -> canonical unit id. Seeded from the
#: spellings the corpus actually carries (``lbs``, ``F``, ``breaths/min``) plus the long
#: forms. Every canonical id is its own alias, so a stored value already in canonical
#: form resolves without a special case.
_ALIASES: dict[str, str] = {
    # mass
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "kg": "kg", "kgs": "kg", "kilogram": "kg", "kilograms": "kg",
    "g": "g", "gram": "g", "grams": "g",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    # length
    "cm": "cm", "centimeter": "cm", "centimeters": "cm", "centimetre": "cm",
    "centimetres": "cm",
    "mm": "mm", "millimeter": "mm", "millimeters": "mm",
    "m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "in": "in", "inch": "in", "inches": "in", '"': "in",
    "ft": "ft", "foot": "ft", "feet": "ft", "'": "ft",
    # temperature. The degree-sign spellings are *input* aliases only: a document may
    # print "degF" with a degree sign, and it resolves - but the canonical id it
    # resolves to is ASCII, so nothing non-ASCII can reach a cp437 console.
    "f": "degF", "degf": "degF", "deg f": "degF",
    "°f": "degF", "° f": "degF",
    "c": "degC", "degc": "degC", "deg c": "degC",
    "°c": "degC", "° c": "degC",
    # pressure
    "mmhg": "mmHg", "mm[hg]": "mmHg", "mm hg": "mmHg",
    # rate
    "/min": "/min", "per min": "/min", "per minute": "/min", "bpm": "/min",
    "breaths/min": "/min", "beats/min": "/min",
    # ratio
    "%": "%", "percent": "%", "pct": "%",
    # --- lab units (issue #202) -------------------------------------------------
    # Keys are lowercase with interior whitespace collapsed, so one entry covers a
    # whole case-only group (`mg/dL` / `mg/dl` / `MG/DL`). The micro-sign spellings
    # are *input* aliases only, the degF precedent: U+00B5 and U+03BC both resolve,
    # and the id they resolve to stays ASCII.
    #
    # NEVER aliased to each other, whatever a document implies: `U/L` and `IU/L`,
    # `mEq/L` and `mmol/L`, `uIU/mL` and `mIU/L` and `mU/L` - numerically equal for
    # some analytes and wrong for others, so an equivalence is per-analyte and not a
    # registry fact. Nor `mg/dL` and `mg/L`, `ng/dL` and `ng/mL`, `%` and `fL` -
    # different scale or different measure. A truncated string (`x10`, `10`) stays
    # unknown rather than being guessed at.
    # mass / volume
    "mg/dl": "mg/dL",
    "g/dl": "g/dL", "gm/dl": "g/dL", "gms/dl": "g/dL", "grams/dl": "g/dL",
    "mg/l": "mg/L", "g/l": "g/L", "mg/ml": "mg/mL",
    "ug/dl": "ug/dL", "µg/dl": "ug/dL", "μg/dl": "ug/dL",
    "ug/l": "ug/L", "µg/l": "ug/L", "μg/l": "ug/L",
    "ug/ml": "ug/mL", "µg/ml": "ug/mL", "μg/ml": "ug/mL",
    "ng/dl": "ng/dL", "ng/ml": "ng/mL", "ng/l": "ng/L", "pg/ml": "pg/mL",
    # amount / volume
    "mmol/l": "mmol/L",
    "umol/l": "umol/L", "µmol/l": "umol/L", "μmol/l": "umol/L",
    "nmol/l": "nmol/L", "pmol/l": "pmol/L",
    "meq/l": "mEq/L", "mosm/kg": "mOsm/kg",
    # activity
    "u/l": "U/L", "u/ml": "U/mL", "iu/l": "IU/L", "iu/ml": "IU/mL",
    "miu/l": "mIU/L", "miu/ml": "mIU/mL",
    "uiu/ml": "uIU/mL", "µiu/ml": "uIU/mL", "μiu/ml": "uIU/mL",
    "mu/l": "mU/L",
    # counts. The notation variants below are one unit written many ways - folding
    # them is the whole point of this section.
    "k/ul": "K/uL", "k/µl": "K/uL", "k/μl": "K/uL", "k/mm3": "K/uL",
    "k/cumm": "K/uL", "thousand/ul": "K/uL", "thousands/ul": "K/uL",
    "x10e3/ul": "K/uL", "10e3/ul": "K/uL", "10^3/ul": "K/uL", "10*3/ul": "K/uL",
    "10^3/mm^3": "K/uL", "10*3/mm3": "K/uL", "10e3/mm3": "K/uL",
    "m/ul": "M/uL", "m/µl": "M/uL", "m/μl": "M/uL", "mil/ul": "M/uL",
    "million/ul": "M/uL", "millions/ul": "M/uL", "x10e6/ul": "M/uL",
    "10e6/ul": "M/uL", "10^6/ul": "M/uL", "10*6/ul": "M/uL", "m/mm3": "M/uL",
    "10^6/mm^3": "M/uL",
    "/ul": "/uL", "/µl": "/uL", "/μl": "/uL", "cells/ul": "/uL", "per ul": "/uL",
    "/mm3": "/uL", "cells/mm3": "/uL", "/cumm": "/uL",
    "/hpf": "/HPF", "/lpf": "/LPF",
    # indices / other
    "fl": "fL", "cu microns": "fL", "cu_microns": "fL", "cubic microns": "fL",
    "um3": "fL", "um^3": "fL",
    "mm/hr": "mm/hr", "mm/h": "mm/hr", "mm hr": "mm/hr", "mm/hour": "mm/hr",
    "mm/1hr": "mm/hr",
    "ml/min": "mL/min",
    "ml/min/1.73": "mL/min/1.73m2", "ml/min/1.73m2": "mL/min/1.73m2",
    "ml/min/1.73 m2": "mL/min/1.73m2", "ml/min/1.73m^2": "mL/min/1.73m2",
    "ml/min/1.73sqm": "mL/min/1.73m2",
    "mg/g": "mg/g",
    "sec": "sec", "secs": "sec", "seconds": "sec",
    # the sub-gram masses the lab units above are built from, now that they are
    # spelled in the corpus (MCH is reported in pg). Genuinely convertible mass.
    "mg": "mg", "milligram": "mg", "milligrams": "mg",
    "ug": "ug", "µg": "ug", "μg": "ug", "microgram": "ug",
    "micrograms": "ug",
    "ng": "ng", "nanogram": "ng", "nanograms": "ng",
    "pg": "pg", "picogram": "pg", "picograms": "pg",
}

#: Decimal places every converted display value is rounded to, once.
DISPLAY_PLACES = 2


class UnknownUnitError(ValueError):
    """Raised when a *written* preference names a unit the registry does not know.

    Writer-side only: a **stored** row carrying an unknown unit is a fact of the record,
    not an error, and simply renders untouched.
    """


def canonical_unit(text: object) -> str | None:
    """The canonical unit id for a free-text unit string, or ``None``.

    ``None`` for empty input and for any spelling the registry does not know - the
    caller's cue to leave the value exactly as stored rather than guess at its scale.

    Letter case and whitespace are normalised (interior runs collapse to one space, so
    ``"deg  f"`` resolves like ``"deg f"``); spelling is never guessed at.
    """
    if text is None:
        return None
    return _ALIASES.get(" ".join(str(text).split()).lower())


def dimension_of(unit_id: str | None) -> str | None:
    """The dimension (``mass``, ``length``, ...) of a canonical unit id, or ``None``."""
    spec = UNITS.get(unit_id) if unit_id is not None else None
    return spec[0] if spec is not None else None


def known_units(*, include_lab: bool = True) -> tuple[str, ...]:
    """Every canonical unit id, sorted - for CLI validation and its error text.

    ``include_lab=False`` drops the single-member ``lab:<id>`` dimensions, for the one
    caller (argparse help) that wants a list short enough to read. The default keeps
    every id, so the ``UnknownUnitError`` text stays the complete list - which is where
    a user discovers a lab id in the first place.
    """
    if include_lab:
        return tuple(sorted(UNITS))
    return tuple(
        sorted(
            unit_id
            for unit_id, (dimension, _, _) in UNITS.items()
            if not dimension.startswith(LAB_DIMENSION_PREFIX)
        )
    )


def convert(value: float, from_text: object, to_unit: str | None) -> float | None:
    """``value`` (stated in ``from_text``) expressed in ``to_unit``, or ``None``.

    ``None`` - meaning "do not touch this number" - whenever either side is unknown or
    the two sides are of different dimensions. Mixing scales silently is the one failure
    this module cannot have, so every ambiguity resolves to leaving the stored value
    alone. Returns ``value`` itself when both sides resolve to the same unit id.

    Unrounded: :func:`display` applies :func:`round_display` once, at the end.
    """
    source = canonical_unit(from_text)
    if source is None or to_unit is None or to_unit not in UNITS:
        return None
    if source == to_unit:
        return value
    src_dim, src_factor, src_offset = UNITS[source]
    dst_dim, dst_factor, dst_offset = UNITS[to_unit]
    if src_dim != dst_dim:
        return None
    try:
        base = float(value) * src_factor + src_offset
    except (TypeError, ValueError):
        return None
    return (base - dst_offset) / dst_factor


def round_display(value: float) -> float:
    """The one rounding rule for a derived display number (2 dp).

    Applied once, here, so every downstream formatter keeps printing raw numbers as it
    does today. Converted numbers are never written back, so no precision loss can reach
    storage.
    """
    return round(float(value), DISPLAY_PLACES)


@dataclass(frozen=True)
class Displayed:
    """One value resolved for display: what to print, and whether it was derived.

    ``converted`` is false whenever the number printed is the stored one - no
    preference, an unknown or absent stored unit, a non-numeric value, a
    cross-dimension preference, **or** a stored unit that already resolves to the
    preferred one (relabelling a spelling is `record edit`'s job, not this feature's).
    In every one of those cases ``value``/``unit`` are the stored ones, passed straight
    through.
    """

    # `value`/`unit` are the *stored* ones whenever `converted` is false, so they carry
    # whatever the column held - a float, a None, or (defensively) a text value the
    # caller handed straight through.
    value: object
    unit: object
    converted: bool
    source_value: object
    source_unit: object


def display(
    value: object, unit_text: object, target_unit: str | None
) -> Displayed:
    """Resolve one stored value against a person's canonical unit. Never raises.

    The single entry point both read paths call, so `render summary` and `trends` cannot
    drift apart about when a number is converted.
    """
    stored = Displayed(
        value=value,
        unit=unit_text,
        converted=False,
        source_value=value,
        source_unit=unit_text,
    )
    if target_unit is None or value is None:
        return stored
    source = canonical_unit(unit_text)
    if source is None or source == target_unit:
        return stored
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return stored
    converted = convert(numeric, unit_text, target_unit)
    if converted is None:
        return stored
    return Displayed(
        value=round_display(converted),
        unit=target_unit,
        converted=True,
        source_value=value,
        source_unit=unit_text,
    )


def in_target(unit_text: object, target_unit: str | None) -> bool:
    """Is a stored unit already the person's canonical one? (no conversion needed)

    Distinct from ``Displayed.converted``, which answers "was the number changed". A
    series all stored in ``lb`` under a ``lb`` preference converts nothing yet is
    entirely in the canonical unit - the difference `trends` needs to decide whether it
    may label the whole series with that unit.
    """
    if target_unit is None:
        return False
    return canonical_unit(unit_text) == target_unit


# --------------------------------------------------------------------------- #
# Preference store (conn-taking) -- person_unit_pref, migration 013
# --------------------------------------------------------------------------- #

def has_pref_table(conn: sqlite3.Connection) -> bool:
    """Whether `person_unit_pref` exists - false for a database predating migration 013.

    The :func:`curation.has_table` / :func:`records.has_edit_table` guard: a restored
    older snapshot must read as "no preferences" rather than raise ``no such table``.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='person_unit_pref'"
    ).fetchone()
    return row is not None


def _resolve_person_id(conn: sqlite3.Connection, slug: str) -> int:
    person = persons.get_person(conn, slug)
    if person is None:
        raise persons.PersonNotFoundError(f"no person with slug '{slug}'")
    return person.person_id


def _normalize_key(key: object, dictionary: dict[str, str] | None) -> str:
    token = key_token(key, dictionary)
    if not token:
        raise ValueError("key must be non-empty")
    return token


def load_prefs(conn: sqlite3.Connection, person_id: int) -> dict[str, str]:
    """``{key_token: canonical unit id}`` for one person - the read-path entry point.

    ``{}`` for a person with no preferences and for any pre-013 database, which is the
    fast path every render and trend takes today.
    """
    if not has_pref_table(conn):
        return {}
    rows = conn.execute(
        "SELECT key, unit FROM person_unit_pref WHERE person_id = ?", (person_id,)
    ).fetchall()
    return {row["key"]: row["unit"] for row in rows}


def set_pref(
    conn: sqlite3.Connection,
    slug: str,
    key: object,
    unit: object,
    *,
    dictionary: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict:
    """Record (or replace) one person's canonical display unit for one measurement key.

    ``key`` is stored as its :func:`dedup.key_token`, so ``--key A1c`` reaches rows
    stored as ``HbA1c``; ``unit`` is stored as its canonical id, so ``--unit lbs``
    records ``lb``. Nothing is written when either is rejected.

    Raises :class:`persons.PersonNotFoundError` for an unknown slug,
    :class:`UnknownUnitError` for a unit outside :func:`known_units`, and ``ValueError``
    for an empty key.
    """
    person_id = _resolve_person_id(conn, slug)
    token = _normalize_key(key, dictionary)
    unit_id = canonical_unit(unit)
    if unit_id is None:
        raise UnknownUnitError(
            f"unknown unit '{unit}' - known units: {', '.join(known_units())}"
        )
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO person_unit_pref (person_id, key, unit, set_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (person_id, key) DO UPDATE SET unit = excluded.unit, "
            "set_at = excluded.set_at",
            (person_id, token, unit_id, stamp),
        )
    return {"key": token, "unit": unit_id, "set_at": stamp}


def clear_pref(
    conn: sqlite3.Connection,
    slug: str,
    key: object,
    *,
    dictionary: dict[str, str] | None = None,
) -> bool:
    """Drop one preference. ``False`` when there was nothing to clear (not an error -
    clearing an unset key is the state the caller asked for)."""
    person_id = _resolve_person_id(conn, slug)
    token = _normalize_key(key, dictionary)
    if not has_pref_table(conn):
        return False
    with conn:
        cur = conn.execute(
            "DELETE FROM person_unit_pref WHERE person_id = ? AND key = ?",
            (person_id, token),
        )
    return cur.rowcount > 0


def list_prefs(conn: sqlite3.Connection, slug: str) -> list[dict]:
    """``[{key, unit, set_at}]`` for one person, ordered by key."""
    person_id = _resolve_person_id(conn, slug)
    if not has_pref_table(conn):
        return []
    rows = conn.execute(
        "SELECT key, unit, set_at FROM person_unit_pref WHERE person_id = ? "
        "ORDER BY key",
        (person_id,),
    ).fetchall()
    return [dict(row) for row in rows]

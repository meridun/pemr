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
}

#: ``str(text).strip().lower()`` -> canonical unit id. Seeded from the spellings the
#: corpus actually carries (``lbs``, ``F``, ``breaths/min``) plus the obvious long
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
    """
    if text is None:
        return None
    return _ALIASES.get(str(text).strip().lower())


def dimension_of(unit_id: str | None) -> str | None:
    """The dimension (``mass``, ``length``, ...) of a canonical unit id, or ``None``."""
    spec = UNITS.get(unit_id) if unit_id is not None else None
    return spec[0] if spec is not None else None


def known_units() -> tuple[str, ...]:
    """Every canonical unit id, sorted - for CLI validation and its error text."""
    return tuple(sorted(UNITS))


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

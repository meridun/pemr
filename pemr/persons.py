"""Person CRUD — `pemr person add|list|show|edit|deactivate|reactivate|remove`."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .models import Person


class SlugExistsError(ValueError):
    """Raised when adding a person whose slug is already taken."""


class PersonNotFoundError(ValueError):
    """Raised when a slug does not resolve to a person (friendly rc=1 at the CLI)."""


class PersonHasDependentsError(ValueError):
    """Raised when a hard ``remove`` is refused because dependent records exist."""


# Fields editable via `person edit`. `slug` is deliberately excluded — it is the stable
# identity handle referenced by `--person` across ingest/query/render (see issue #26
# design). `full_name` is non-nullable (parity with `add`); the rest are nullable and
# can be cleared by passing an empty string.
_EDITABLE_FIELDS = ("full_name", "dob", "sex", "blood_type", "notes")

# Tables whose rows reference person(person_id) with ON DELETE NO ACTION (RESTRICT).
# A hard `remove` is refused if any of these hold a dependent row — the DELETE would
# otherwise fail with an IntegrityError, and destroying medical history is irreversible.
_CHILD_TABLES = (
    "document", "lab_result", "medication", "procedure",
    "appointment", "observation", "condition", "allergy", "conflict",
)


def add_person(
    conn: sqlite3.Connection,
    slug: str,
    full_name: str,
    dob: str | None = None,
    sex: str | None = None,
    blood_type: str | None = None,
    notes: str | None = None,
) -> Person:
    slug = slug.strip().lower()
    full_name = full_name.strip()
    if not slug:
        raise ValueError("slug must be non-empty")
    if not full_name:
        raise ValueError("full_name must be non-empty")
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO person (slug, full_name, dob, sex, blood_type, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (slug, full_name, dob, sex, blood_type, notes),
            )
    except sqlite3.IntegrityError as exc:
        raise SlugExistsError(f"person slug already exists: {slug}") from exc
    person = get_person(conn, slug)
    assert person is not None  # just inserted
    return person


def update_person(conn: sqlite3.Connection, slug: str, **fields: str | None) -> Person:
    """Partial field-level update of a person (``pemr person edit``).

    Only the fields passed in ``fields`` change; at least one must be given. ``slug`` is
    not editable. ``full_name`` must be non-empty when provided (parity with ``add``).
    The nullable fields (``dob``/``sex``/``blood_type``/``notes``) accept an empty string
    to clear the column to ``NULL`` — the way to undo a mistakenly-set value.

    Raises :class:`PersonNotFoundError` for an unknown slug and ``ValueError`` for an
    unknown field, an empty ``full_name``, or no fields to update.
    """
    slug = slug.strip().lower()
    unknown = set(fields) - set(_EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"cannot edit field(s): {', '.join(sorted(unknown))}")
    if not fields:
        raise ValueError("nothing to update - pass at least one field to change")

    updates: dict[str, str | None] = {}
    for name, value in fields.items():
        if name == "full_name":
            cleaned = (value or "").strip()
            if not cleaned:
                raise ValueError("full_name must be non-empty")
            updates[name] = cleaned
        else:
            # Nullable field: an explicit empty string clears the column to NULL.
            updates[name] = value if value not in (None, "") else None

    if get_person(conn, slug) is None:
        raise PersonNotFoundError(f"no person with slug '{slug}'")

    assignments = ", ".join(f"{name} = ?" for name in updates)
    values = list(updates.values())
    values.append(slug)
    with conn:
        conn.execute(f"UPDATE person SET {assignments} WHERE slug = ?", values)
    person = get_person(conn, slug)
    assert person is not None  # slug existence checked above, under the same connection
    return person


def deactivate_person(conn: sqlite3.Connection, slug: str) -> Person:
    """Soft-deactivate a person (reversible; hides them from the default ``person list``).

    Idempotent: an already-deactivated record keeps its original ``deactivated_at``.
    Raises :class:`PersonNotFoundError` for an unknown slug.
    """
    slug = slug.strip().lower()
    person = get_person(conn, slug)
    if person is None:
        raise PersonNotFoundError(f"no person with slug '{slug}'")
    if person.deactivated_at is None:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with conn:
            conn.execute(
                "UPDATE person SET deactivated_at = ? WHERE slug = ?", (ts, slug)
            )
        person = get_person(conn, slug)
        assert person is not None
    return person


def reactivate_person(conn: sqlite3.Connection, slug: str) -> Person:
    """Clear a person's deactivated flag (undo :func:`deactivate_person`).

    Raises :class:`PersonNotFoundError` for an unknown slug.
    """
    slug = slug.strip().lower()
    person = get_person(conn, slug)
    if person is None:
        raise PersonNotFoundError(f"no person with slug '{slug}'")
    if person.deactivated_at is not None:
        with conn:
            conn.execute(
                "UPDATE person SET deactivated_at = NULL WHERE slug = ?", (slug,)
            )
        person = get_person(conn, slug)
        assert person is not None
    return person


def remove_person(conn: sqlite3.Connection, slug: str) -> Person:
    """Hard-delete a person, permitted **only** when they have zero dependent rows.

    This is the childless-typo case (a roster entry with nothing ingested). If any
    child record references the person, refuse with :class:`PersonHasDependentsError`
    pointing at ``deactivate`` — deleting real medical history is irreversible and is
    not exposed behind a single flag. Returns the (now-deleted) person for reporting.

    Raises :class:`PersonNotFoundError` for an unknown slug.
    """
    slug = slug.strip().lower()
    person = get_person(conn, slug)
    if person is None:
        raise PersonNotFoundError(f"no person with slug '{slug}'")

    for table in _CHILD_TABLES:
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE person_id = ? LIMIT 1", (person.person_id,)
        ).fetchone()
        if row is not None:
            raise PersonHasDependentsError(
                f"person '{slug}' has dependent {table} record(s); hard remove would "
                "destroy medical history. Use `pemr person deactivate` instead."
            )

    try:
        with conn:
            conn.execute("DELETE FROM person WHERE slug = ?", (slug,))
    except sqlite3.IntegrityError as exc:  # backstop: an unlisted FK still referencing
        raise PersonHasDependentsError(
            f"person '{slug}' still has dependent records; use "
            "`pemr person deactivate` instead."
        ) from exc
    return person


def list_people(
    conn: sqlite3.Connection, include_inactive: bool = False
) -> list[Person]:
    """List people ordered by slug. Deactivated records are hidden unless
    ``include_inactive`` is set (``pemr person list --all``)."""
    if include_inactive:
        rows = conn.execute("SELECT * FROM person ORDER BY slug").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM person WHERE deactivated_at IS NULL ORDER BY slug"
        ).fetchall()
    return [Person.from_row(row) for row in rows]


def get_person(conn: sqlite3.Connection, slug: str) -> Person | None:
    row = conn.execute(
        "SELECT * FROM person WHERE slug = ?", (slug.strip().lower(),)
    ).fetchone()
    return Person.from_row(row) if row is not None else None

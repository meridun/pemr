"""Person CRUD — phase 1 (`pemr person add|list|show`)."""

from __future__ import annotations

import sqlite3

from .models import Person


class SlugExistsError(ValueError):
    """Raised when adding a person whose slug is already taken."""


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
            cur = conn.execute(
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


def list_people(conn: sqlite3.Connection) -> list[Person]:
    rows = conn.execute("SELECT * FROM person ORDER BY slug").fetchall()
    return [Person.from_row(row) for row in rows]


def get_person(conn: sqlite3.Connection, slug: str) -> Person | None:
    row = conn.execute(
        "SELECT * FROM person WHERE slug = ?", (slug.strip().lower(),)
    ).fetchone()
    return Person.from_row(row) if row is not None else None

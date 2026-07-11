"""Typed row shapes. Phase 1 covers Person; later phases add the record types."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Person:
    person_id: int
    slug: str
    full_name: str
    dob: str | None = None
    sex: str | None = None
    blood_type: str | None = None
    notes: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Person":
        return cls(
            person_id=row["person_id"],
            slug=row["slug"],
            full_name=row["full_name"],
            dob=row["dob"],
            sex=row["sex"],
            blood_type=row["blood_type"],
            notes=row["notes"],
        )

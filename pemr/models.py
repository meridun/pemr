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


@dataclass(frozen=True)
class Document:
    document_id: int
    sha256: str
    person_id: int | None = None
    doc_date: str | None = None
    category: str | None = None
    provider: str | None = None
    source_path: str = ""
    ocr_text: str | None = None
    ingested_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Document":
        return cls(
            document_id=row["document_id"],
            sha256=row["sha256"],
            person_id=row["person_id"],
            doc_date=row["doc_date"],
            category=row["category"],
            provider=row["provider"],
            source_path=row["source_path"],
            ocr_text=row["ocr_text"],
            ingested_at=row["ingested_at"],
        )

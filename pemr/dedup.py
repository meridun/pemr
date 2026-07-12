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
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

_WS = re.compile(r"\s+")


class ValidationError(ValueError):
    """Raised when the extraction JSON violates a record schema."""


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
    # Underscores count as separators: machine-generated keys like
    # "blood_pressure" must collapse to the same token as "Blood Pressure".
    return _WS.sub(" ", value.strip().lower().replace("_", " "))


def norm(value: object, dictionary: dict[str, str] | None = None) -> str:
    """Normalize a free-text field: lowercase, trim, collapse whitespace (underscores
    count as whitespace), then map synonyms through the dictionary. ``None`` -> ``""``
    (deterministic key part)."""
    if value is None:
        return ""
    collapsed = _collapse(str(value))
    if dictionary:
        return dictionary.get(collapsed, collapsed)
    return collapsed


def _date_only(value: object) -> str:
    """Date portion of an ISO datetime/date string ('2026-01-02T09:00' -> '2026-01-02')."""
    if value is None:
        return ""
    text = str(value).strip()
    return text.replace("T", " ").split(" ")[0]


def _round_value(value: object) -> str:
    if value is None or value == "":
        return ""
    return str(round(float(value)))


def _hash_parts(parts: list[object]) -> str:
    joined = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def dedup_key(
    record_type: str, row: dict, person_id: int, dictionary: dict[str, str] | None = None
) -> str:
    """Deterministic semantic key per Architecture.md §3. Same clinical fact from
    two different documents -> identical key -> collapses to one row."""
    def n(field_name: str) -> str:
        return norm(row.get(field_name), dictionary)

    if record_type == "lab_result":
        parts = [person_id, n("test_name"), _date_only(row.get("collected_at")),
                 _round_value(row.get("value_num"))]
    elif record_type == "medication":
        parts = [person_id, n("name"), _collapse(str(row.get("dose") or "")),
                 _date_only(row.get("started_on"))]
    elif record_type == "procedure":
        parts = [person_id, n("name"), _date_only(row.get("performed_on"))]
    elif record_type == "appointment":
        parts = [person_id, n("provider"), _date_only(row.get("scheduled_for"))]
    elif record_type == "observation":
        value = row.get("value_num")
        if value is None:
            value = _collapse(str(row.get("value_text") or ""))
        parts = [person_id, n("obs_type"), _date_only(row.get("observed_at")),
                 n("key"), value]
    else:  # pragma: no cover - guarded by validate()
        raise ValidationError(f"unknown record type: {record_type}")
    return _hash_parts(parts)


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
# so they are deliberately excluded here. The lone exception is the measured value:
# value_num feeds the lab/observation key via a lossy round(), so a corrected value
# in the same round-bucket still collides and must surface as a CONFLICT, not a
# silent duplicate — hence value_num appears below.
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


def _rows_equal(record_type: str, existing: sqlite3.Row, incoming: dict) -> bool:
    """True when two rows with a matching dedup_key are the *same fact* (a duplicate)
    rather than a conflicting one — i.e. their payload fields all agree."""
    existing_map = dict(existing)
    for name in _COMPARE_FIELDS[record_type]:
        ev = existing_map.get(name)
        iv = incoming.get(name)
        if ev is None and iv is None:
            continue
        # Numeric columns round-trip through SQLite as float; compare numerically.
        if isinstance(ev, _NUM) and isinstance(iv, _NUM):
            if float(ev) != float(iv):
                return False
        elif ev != iv:
            return False
    return True


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
        raise ValidationError(f"no document with id {document_id} — ingest it first")
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
        for row in rows:
            validate_row(record_type, row)

    summary = CommitSummary()
    detected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Pass 2: dedup + insert inside a single transaction.
    with conn:
        for record_type, rows in records.items():
            for row in rows:
                key = dedup_key(record_type, row, person_id, dictionary)
                existing = conn.execute(
                    f"SELECT * FROM {record_type} WHERE dedup_key = ?", (key,)
                ).fetchone()
                if existing is None:
                    row_id = _insert_record(
                        conn, record_type, row, person_id, document_id, key
                    )
                    summary.new.append((record_type, row_id))
                elif _rows_equal(record_type, existing, row):
                    summary.duplicate.append((record_type, key))
                else:
                    conflict_id = _stage_conflict(
                        conn, record_type, key, person_id, document_id,
                        existing, row, detected_at,
                    )
                    summary.conflict.append((record_type, conflict_id))
    return summary


def _insert_record(
    conn: sqlite3.Connection,
    record_type: str,
    row: dict,
    person_id: int,
    document_id: int,
    key: str,
) -> int:
    columns = ["person_id", "document_id", "dedup_key"]
    values: list[object] = [person_id, document_id, key]
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


def resolve_conflict(
    conn: sqlite3.Connection,
    conflict_id: int,
    keep: str,
    note: str | None = None,
) -> None:
    """Resolve a staged conflict. ``keep`` is 'existing' (drop the incoming row) or
    'incoming' (overwrite the stored record's payload fields with the incoming row).

    Overwriting touches only the payload columns (``_COMPARE_FIELDS``) whose
    disagreement defined the conflict, plus provenance ``document_id``. Identity
    fields keep their stored display form (they are equal after norm() by
    construction, but may differ in casing/spacing); the dedup_key stays put.
    """
    db.require_migrated(conn)
    if keep not in ("existing", "incoming"):
        raise ValueError("keep must be 'existing' or 'incoming'")

    row = conn.execute(
        "SELECT * FROM conflict WHERE conflict_id = ?", (conflict_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no conflict with id {conflict_id}")
    if row["status"] != "open":
        raise ValueError(f"conflict {conflict_id} is already {row['status']}")

    record_type = row["record_type"]
    resolution = f"keep-{keep}" + (f": {note}" if note else "")
    resolved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with conn:
        if keep == "incoming":
            incoming = json.loads(row["incoming_json"])
            _overwrite_record(
                conn, record_type, row["dedup_key"], row["document_id"], incoming
            )
        conn.execute(
            "UPDATE conflict SET status='resolved', resolution=?, resolved_at=? "
            "WHERE conflict_id=?",
            (resolution, resolved_at, conflict_id),
        )


def _overwrite_record(
    conn: sqlite3.Connection,
    record_type: str,
    key: str,
    document_id: int | None,
    incoming: dict,
) -> None:
    assignments = ["document_id = ?"]
    values: list[object] = [document_id]
    for name in _COMPARE_FIELDS[record_type]:
        assignments.append(f"{name} = ?")
        values.append(incoming.get(name))
    values.append(key)
    conn.execute(
        f"UPDATE {record_type} SET {', '.join(assignments)} WHERE dedup_key = ?",
        values,
    )

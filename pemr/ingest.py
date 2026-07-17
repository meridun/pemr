"""Document intake — hashing, content-addressed blob store, layer-1 dedup.

The deterministic first half of the ingestion pipeline (Architecture.md §4):

    1. sha256 the raw bytes; if the hash is already in `document` -> duplicate, stop
    2. copy the blob into sources/<sha[:2]>/<sha>.<ext> (immutable, content-addressed)
    3. insert a `document` row (category/provider left null for now)

The agent's vision/extraction step and layer-2 dedup happen later, through
`commit-extraction` (see dedup.py). This module never runs the LLM — it only lays
the deterministic rails a document travels before extraction.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .models import Document


class IngestError(RuntimeError):
    """Raised for unrecoverable ingest problems (missing file/person)."""


@dataclass(frozen=True)
class IngestResult:
    status: str  # "new" | "duplicate"
    document: Document

    @property
    def is_duplicate(self) -> bool:
        return self.status == "duplicate"

    @property
    def ocr_text_populated(self) -> bool:
        """Whether the stored document ended up with any OCR/transcription text.

        The `AGENTS.md` contract asks agents to populate `ocr_text` on every ingest
        (empty FTS otherwise); this lets a caller self-check without a follow-up read.
        """
        return bool(self.document.ocr_text)


_READ_CHUNK = 1 << 20  # 1 MiB


def hash_file(path: str | Path) -> str:
    """sha256 hex digest of a file's raw bytes, streamed (large scans safe)."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(_READ_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def blob_dest(sources_dir: str | Path, sha: str, ext: str) -> Path:
    """Content-addressed path: sources/<sha[:2]>/<sha><ext> (ext incl. leading dot)."""
    return Path(sources_dir) / sha[:2] / f"{sha}{ext}"


def _relative_source_path(sha: str, ext: str) -> str:
    """Portable, sources_dir-relative path stored in `document.source_path`.

    Absolute paths break on restore to another machine; the blob is content-
    addressed so the relative layout is reconstructable from any sources root.
    """
    return f"{sha[:2]}/{sha}{ext}"


def run_ocr(path: str | Path) -> str | None:
    """Best-effort OCR via a system `tesseract` binary. Soft dependency:

    returns None (with a stderr note) when tesseract is unavailable instead of
    failing the ingest. Only sensible for flat image scans; callers pass this
    through as `ocr_text` for the agent to work from.
    """
    if shutil.which("tesseract") is None:
        print(
            "note: --ocr requested but `tesseract` is not on PATH; "
            "storing document without ocr_text",
            file=sys.stderr,
        )
        return None
    try:
        proc = subprocess.run(
            ["tesseract", str(path), "stdout"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"note: tesseract OCR failed ({exc}); storing without ocr_text",
              file=sys.stderr)
        return None
    text = proc.stdout.strip()
    return text or None


def get_document(conn: sqlite3.Connection, sha256: str) -> Document | None:
    row = conn.execute(
        "SELECT * FROM document WHERE sha256 = ?", (sha256,)
    ).fetchone()
    return Document.from_row(row) if row is not None else None


def get_document_by_id(conn: sqlite3.Connection, document_id: int) -> Document | None:
    row = conn.execute(
        "SELECT * FROM document WHERE document_id = ?", (document_id,)
    ).fetchone()
    return Document.from_row(row) if row is not None else None


def _person_id_for_slug(conn: sqlite3.Connection, slug: str) -> int:
    row = conn.execute(
        "SELECT person_id FROM person WHERE slug = ?", (slug.strip().lower(),)
    ).fetchone()
    if row is None:
        raise IngestError(
            f"no person with slug '{slug}' — add them first: `pemr person add`"
        )
    return row["person_id"]


def ingest_document(
    conn: sqlite3.Connection,
    file_path: str | Path,
    person_slug: str,
    sources_dir: str | Path,
    *,
    doc_date: str | None = None,
    category: str | None = None,
    provider: str | None = None,
    ocr: bool = False,
    ocr_text: str | None = None,
) -> IngestResult:
    """Ingest one document: hash, layer-1 dedup, blob store, insert `document`.

    On a content-hash hit (layer 1), the existing document is returned with
    status "duplicate" and nothing is written. Otherwise the blob is copied into
    the immutable content-addressed store and a new `document` row is inserted.

    ``ocr_text`` is caller-supplied document text (the agent's own transcription —
    the `AGENTS.md` default path, which beats tesseract on messy scans). When
    provided it wins; otherwise ``ocr=True`` falls back to a best-effort tesseract
    pass. An empty/whitespace-only string is treated as absent. Populating text here
    is what makes a document findable via FTS (`find`), so it is a warning-not-error
    when it ends up empty — see :attr:`IngestResult.ocr_text_populated`.
    """
    db.require_migrated(conn)

    src = Path(file_path)
    if not src.is_file():
        raise IngestError(f"file not found: {src}")

    person_id = _person_id_for_slug(conn, person_slug)
    sha = hash_file(src)

    existing = get_document(conn, sha)
    if existing is not None:
        return IngestResult(status="duplicate", document=existing)

    ext = src.suffix.lower()
    dest = blob_dest(sources_dir, sha, ext)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # content-addressed => identical bytes, never overwrite. Track whether *this*
    # call created the blob so a failed `document` insert doesn't orphan it
    # (carry-forward advisory from #4): clean up only what we wrote.
    blob_created = not dest.exists()
    if blob_created:
        shutil.copy2(src, dest)

    supplied = ocr_text.strip() if ocr_text else None
    ocr_text = supplied or (run_ocr(dest) if ocr else None)

    try:
        with conn:
            cur = conn.execute(
                """
                INSERT INTO document
                  (sha256, person_id, doc_date, category, provider, source_path,
                   ocr_text, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sha,
                    person_id,
                    doc_date,
                    category,
                    provider,
                    _relative_source_path(sha, ext),
                    ocr_text,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
    except Exception:
        if blob_created:
            dest.unlink(missing_ok=True)
        raise
    document = get_document_by_id(conn, cur.lastrowid)
    assert document is not None  # just inserted
    return IngestResult(status="new", document=document)

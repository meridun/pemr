"""Document intake — hashing, content-addressed blob store, layer-1 dedup.

The deterministic first half of the ingestion pipeline (Architecture.md §4):

    1. sha256 the raw bytes; if the hash is already in `document` -> duplicate, stop
    2. copy the blob into sources/<sha[:2]>/<sha>.<ext> (immutable, content-addressed)
    3. insert a `document` row (category/provider left null for now)

The agent's vision/extraction step and layer-2 dedup happen later, through
`commit-extraction` (see dedup.py). This module never runs the LLM — it only lays
the deterministic rails a document travels before extraction.

Issue #61 adds a pre-write **owner check**: when document text is available, it is
scanned for the claimed person's name/DOB and the ingest is refused (pre-write, so a
refusal is a clean no-op) when the text affirmatively points somewhere else. See
:func:`check_owner`.

Issue #66 adds two intake-format guards, both stdlib-only (the engine has no runtime
dependencies): a pre-write refusal of Google Drive **pointer stubs** (see
:func:`is_pointer_stub`) and a text-extraction dispatcher (:func:`extract_text`) so
`.txt`/`.docx`/`.xlsx` and friends become findable instead of landing as opaque blobs.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sqlite3
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

from . import db
from .models import Document, Person


class IngestError(RuntimeError):
    """Raised for unrecoverable ingest problems (missing file/person)."""


class OwnerMismatchError(IngestError):
    """Raised when the document text does not look like it belongs to ``--person``.

    Carries the :class:`OwnerCheck` that triggered the refusal so a structured caller
    (MCP) can report the verdict; ``str(exc)`` is the full human-facing refusal text.
    Subclasses :class:`IngestError` so existing ``except IngestError`` handlers keep
    working.
    """

    def __init__(self, message: str, check: "OwnerCheck") -> None:
        super().__init__(message)
        self.check = check


@dataclass(frozen=True)
class IngestResult:
    status: str  # "new" | "duplicate"
    document: Document
    # None on the duplicate path: a layer-1 duplicate returns before any check runs
    # (nothing is written, so there is nothing to misfile).
    owner_check: "OwnerCheck | None" = None

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


# --------------------------------------------------------------------------- #
# Text extraction (issue #66) — `ocr=True` means "extract text by whatever route
# this file type allows", not "shell out to tesseract". `run_ocr` keeps its name and
# its tesseract semantics and becomes the image/PDF branch of the dispatcher below.
#
# Hard constraint: **zero new dependencies.** Everything here is stdlib, which is what
# draws the scope line — `.rtf`, `.msg`, `.doc` and PDF *text-layer* extraction all
# need a third-party parser and stay out, covered by the agent transcription path
# (`--ocr-text-file`) that `AGENTS.md` §3 already makes the default.
# --------------------------------------------------------------------------- #

_PLAINTEXT_SUFFIXES = frozenset({".txt", ".md", ".csv", ".tsv", ".json", ".log"})

_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# Errors an OOXML/plaintext read can legitimately produce on a malformed or truncated
# file. Extraction is best-effort: any of these degrades to "no ocr_text", never a lost
# document — the same contract `run_ocr` already has for a missing tesseract.
_EXTRACT_ERRORS = (
    OSError, ValueError, KeyError, IndexError, zipfile.BadZipFile, ET.ParseError,
)

_SHEET_NUM = re.compile(r"(\d+)")


def _xml_text(node: ET.Element, tag: str) -> str:
    """Concatenated text of every ``tag`` descendant (OOXML splits runs arbitrarily)."""
    return "".join(child.text or "" for child in node.iter(tag))


def _extract_docx(path: Path) -> str:
    """`.docx` body text: concat `w:t` runs, one line per `w:p` paragraph."""
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    return "\n".join(
        _xml_text(para, f"{_WORD_NS}t") for para in root.iter(f"{_WORD_NS}p")
    )


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    """One `.xlsx` cell: shared-string lookup, inline string, or the literal value."""
    kind = cell.get("t")
    if kind == "s":
        value = cell.find(f"{_SHEET_NS}v")
        if value is None or not (value.text or "").strip():
            return ""
        try:
            index = int(value.text)
        except ValueError:
            # One malformed shared-string index must not discard the whole workbook.
            return ""
        return shared[index] if 0 <= index < len(shared) else ""
    if kind == "inlineStr":
        return _xml_text(cell, f"{_SHEET_NS}t")
    value = cell.find(f"{_SHEET_NS}v")
    return (value.text or "") if value is not None else ""


def _extract_xlsx(path: Path) -> str:
    """`.xlsx` cell text: one line per row, tab-separated, sheets in sheet-file
    numeric order (`sheet2.xml` before `sheet10.xml`); true workbook order lives in
    `xl/workbook.xml` and is not worth a second parse for full-text purposes."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            shared = [
                _xml_text(si, f"{_SHEET_NS}t") for si in root.iter(f"{_SHEET_NS}si")
            ]
        sheets = sorted(
            (n for n in names
             if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")),
            # sheet2.xml before sheet10.xml — lexicographic order would invert them.
            key=lambda n: [int(part) for part in _SHEET_NUM.findall(n)] or [0],
        )
        lines: list[str] = []
        for name in sheets:
            root = ET.fromstring(zf.read(name))
            for row in root.iter(f"{_SHEET_NS}row"):
                lines.append("\t".join(
                    _cell_text(cell, shared) for cell in row.iter(f"{_SHEET_NS}c")
                ))
    return "\n".join(lines)


def extract_text(path: str | Path) -> str | None:
    """Best-effort document text by file type; ``None`` when nothing could be read.

    Dispatches on suffix: plaintext-ish formats are read directly and `.docx`/`.xlsx`
    are unzipped and their OOXML parsed with the stdlib. **Everything else falls
    through to :func:`run_ocr`** (tesseract) — the same thing `ocr=True` did before
    this dispatcher existed. Deliberately not a suffix allowlist: image extensions
    vary far too widely (`.jfif`, `.jpe`, extension-less scans) for one to be safe,
    and silently skipping a scan that used to OCR is the worse failure — it drops the
    document out of `find` with no signal. When tesseract declines (absent, failed, or
    no text — e.g. `.rtf`, `.msg`, `.doc`) the caller gets ``None`` plus a stderr note
    pointing at `--ocr-text-file`.

    Never raises: a malformed `.docx` must not cost you the document.
    """
    src = Path(path)
    suffix = src.suffix.lower()
    try:
        if suffix in _PLAINTEXT_SUFFIXES:
            # utf-8-sig eats a BOM; errors="replace" keeps a legacy-encoded file
            # usable rather than losing it entirely (same trade as run_ocr's decode).
            text = src.read_text(encoding="utf-8-sig", errors="replace")
        elif suffix == ".docx":
            text = _extract_docx(src)
        elif suffix == ".xlsx":
            text = _extract_xlsx(src)
        else:
            ocr_text = run_ocr(src)
            if ocr_text is None:
                # run_ocr already said *why* it declined; add what to do about it.
                print(
                    f"note: no text could be extracted from {src.name}; "
                    f"storing document without ocr_text. Transcribe it and pass "
                    f"--ocr-text-file <path>.",
                    file=sys.stderr,
                )
            return ocr_text
    except _EXTRACT_ERRORS as exc:
        print(
            f"note: text extraction failed for {src.name} ({exc}); "
            "storing without ocr_text",
            file=sys.stderr,
        )
        return None
    text = text.strip()
    return text or None


# --------------------------------------------------------------------------- #
# Google Drive pointer stubs (issue #66).
#
# A `.gsheet`/`.gdoc` in a synced Drive folder is a ~1 KB JSON link, not the document.
# Storing it produces a permanently useless blob plus a `document` row that looks
# legitimate. Detection is **conjunctive** so a real spreadsheet that merely got a
# `.gsheet` name is still ingested normally.
# --------------------------------------------------------------------------- #

_POINTER_SUFFIXES = frozenset({
    ".gdoc", ".gsheet", ".gslides", ".gdraw", ".gform", ".gsite",
    ".gtable", ".gjam", ".glink", ".gmap", ".gscript",
})
_POINTER_MAX_BYTES = 16 * 1024
_POINTER_HOSTS = frozenset({"docs.google.com", "drive.google.com"})


def is_pointer_stub(path: str | Path) -> bool:
    """Whether ``path`` is a Google Drive pointer stub rather than a document.

    All of: a Google-native suffix, ≤16 KiB, parses as a JSON **object**, and carries
    either a ``url`` on a Google Docs/Drive host or the older ``doc_id`` + ``email``
    stub shape. Never raises — an unreadable/undecodable file is simply "not a stub"
    and continues down the normal ingest path.
    """
    src = Path(path)
    if src.suffix.lower() not in _POINTER_SUFFIXES:
        return False
    try:
        if src.stat().st_size > _POINTER_MAX_BYTES:
            return False
        payload = json.loads(src.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):  # UnicodeDecodeError/JSONDecodeError ⊂ ValueError
        return False
    if not isinstance(payload, dict):
        return False
    url = payload.get("url")
    if isinstance(url, str):
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            host = ""
        if host in _POINTER_HOSTS:
            return True
    return isinstance(payload.get("doc_id"), str) and isinstance(
        payload.get("email"), str
    )


def pointer_stub_message(path: str | Path) -> str:
    """Refusal text naming the fix (export from Drive, ingest the export)."""
    name = Path(path).name
    return (
        f"`{name}` is a Google Drive pointer stub (a ~1 KB JSON link), not the "
        "document itself.\n"
        "  Export it from Drive (File > Download > PDF/XLSX) and ingest the export.\n"
        "  Nothing was ingested."
    )


# --------------------------------------------------------------------------- #
# Owner verification (issue #61) — pure, DB-free, table-testable.
#
# Exact, whole-token, normalized matching only: no fuzzy/edit-distance scoring. A
# similarity threshold tuned against a sample of one produces *confident* wrong
# answers in both directions, which is worse than the honest "unverified" verdict.
# Normalization absorbs the variation that actually occurs in scanned headers (case,
# punctuation, `LAST, FIRST` ordering, collapsed whitespace); genuine OCR corruption
# of a name lands in "suspect" for a human to adjudicate.
# --------------------------------------------------------------------------- #

# Deliberately *not* dedup.norm(): that strips parenthetical qualifiers and applies
# dictionary synonym mapping, both wrong for identity matching.
_NON_ALNUM = re.compile(r"[^0-9a-z]+")

# Name-token noise: middle initials are dropped by the length-1 rule; these are the
# suffixes/credentials that show up appended to a printed patient name.
_NAME_NOISE = frozenset({"jr", "sr", "ii", "iii", "iv", "md", "do", "phd"})

# An "identity anchor" is what turns *absence* of the claimed owner's name from
# meaningless (lab result page 3, an imaging CD label) into evidence that the document
# names somebody. Matched against the raw text, case-insensitively — `name` only counts
# with its colon, otherwise every prose use of the word would anchor.
_ANCHOR = re.compile(
    r"\bpatient\b|\bname\s*:|\bdob\b|\bdate\s+of\s+birth\b|\bbirth\s*date\b|\bmrn\b",
    re.IGNORECASE,
)

_EVIDENCE_WINDOW = 120

_MONTHS = (
    ("Jan", "January"), ("Feb", "February"), ("Mar", "March"), ("Apr", "April"),
    ("May", "May"), ("Jun", "June"), ("Jul", "July"), ("Aug", "August"),
    ("Sep", "September"), ("Oct", "October"), ("Nov", "November"), ("Dec", "December"),
)


@dataclass(frozen=True)
class OwnerCheck:
    """Verdict of matching document text against the claimed owner.

    * ``match`` — the claimed person's name or DOB is present in the text.
    * ``mismatch`` — a *different* roster person matched and the claimed one did not.
    * ``suspect`` — the text carries a patient-identity anchor but names nobody on
      the roster. This is the case that actually happened (a legible ``Patient:`` /
      ``DOB:`` header for someone who is not on the roster at all).
    * ``unverified`` — no text, or text with no identity anchor and no matches.
    """

    verdict: str  # "match" | "mismatch" | "suspect" | "unverified"
    matched_slug: str | None = None  # who the text points at, when known
    evidence: str | None = None      # ~120-char window around the identity anchor

    @property
    def blocks(self) -> bool:
        """Whether this verdict refuses the ingest (overridable with ``force``)."""
        return self.verdict in ("mismatch", "suspect")


def normalize_text(text: str) -> str:
    """Lowercase, non-alphanumerics → space, collapse runs, space-pad the ends.

    The padding lets callers test whole-token membership with a plain ``in`` against
    ``f" {token} "`` — no per-token regex compile over a page of OCR.
    """
    return f" {_NON_ALNUM.sub(' ', text.lower()).strip()} "


def name_tokens(full_name: str) -> list[str]:
    """Match-usable tokens of a person's name, or ``[]`` when the name is unusable.

    Drops single characters (middle initials) and suffix/credential noise. Returns
    ``[]`` — the "no name signal" answer — when fewer than two tokens survive or any
    survivor is under 3 characters, since short tokens collide with ordinary words.
    Such a person can still match on DOB; they can never produce a false ``suspect``
    verdict on the strength of a two-letter surname.
    """
    tokens = [
        tok for tok in _NON_ALNUM.sub(" ", full_name.lower()).split()
        if len(tok) > 1 and tok not in _NAME_NOISE
    ]
    if len(tokens) < 2 or any(len(tok) < 3 for tok in tokens):
        return []
    return tokens


def dob_candidates(dob: str | None) -> list[str]:
    """Renderings of an ISO ``YYYY-MM-DD`` DOB to look for in raw document text.

    Day-first forms (``DD/MM/YYYY``) are deliberately excluded: they collide with the
    month-first forms and would manufacture false matches. A partial-precision dob
    (``YYYY`` / ``YYYY-MM``) yields no candidates — a bare year is not evidence.
    """
    if not dob:
        return []
    try:
        parsed = date.fromisoformat(dob.strip()[:10])
    except ValueError:
        return []
    y, m, d = parsed.year, parsed.month, parsed.day
    out = [
        f"{y:04d}-{m:02d}-{d:02d}",
        f"{m:02d}/{d:02d}/{y:04d}",
        f"{m}/{d}/{y:04d}",
        f"{m:02d}-{d:02d}-{y:04d}",
    ]
    for month in _MONTHS[m - 1]:
        out.append(f"{month} {d}, {y:04d}")
        out.append(f"{d} {month} {y:04d}")
    return out


def _person_matches(person: Person, raw: str, normalized: str) -> bool:
    """Whether ``person``'s name (all tokens, order-independent) or DOB is in the text."""
    tokens = name_tokens(person.full_name)
    if tokens and all(f" {tok} " in normalized for tok in tokens):
        return True
    return any(
        # Digit boundaries, not a bare substring: without them the unpadded
        # `M/D/YYYY` rendering is a *tail* of the day-first form, so `25/4/1978`
        # would "match" a 1978-05-04 person — a silent misfile, the exact failure
        # this check exists to prevent. Same guard kills accession-number tails.
        re.search(rf"(?<![0-9]){re.escape(cand)}(?![0-9])", raw, re.IGNORECASE)
        for cand in dob_candidates(person.dob)
    )


def _evidence(raw: str) -> str | None:
    """Whitespace-collapsed ~120-char window around the first identity anchor.

    Quoted in the refusal so a human can adjudicate an OCR-mangled name without
    opening the file.
    """
    hit = _ANCHOR.search(raw)
    if hit is None:
        return None
    mid = (hit.start() + hit.end()) // 2
    half = _EVIDENCE_WINDOW // 2
    return " ".join(raw[max(0, mid - half):mid + half].split()) or None


def check_owner(
    text: str | None, claimed: Person, roster: Sequence[Person]
) -> OwnerCheck:
    """Does ``text`` look like it belongs to ``claimed``? See :class:`OwnerCheck`."""
    if not text or not text.strip():
        return OwnerCheck(verdict="unverified")

    normalized = normalize_text(text)
    if _person_matches(claimed, text, normalized):
        return OwnerCheck(
            verdict="match", matched_slug=claimed.slug, evidence=_evidence(text)
        )

    for person in roster:
        if person.slug == claimed.slug:
            continue
        if _person_matches(person, text, normalized):
            return OwnerCheck(
                verdict="mismatch",
                matched_slug=person.slug,
                evidence=_evidence(text),
            )

    evidence = _evidence(text)
    # `suspect` means "this document names somebody, and it isn't you" — a conclusion
    # only available when we could have recognised the claimed person by name in the
    # first place. With no usable name signal (a two-letter surname, a mononym), the
    # claimed person's absence is ignorance, not evidence, so blocking here would
    # refuse *every* anchored document for them and train the human to pass --force
    # reflexively. A DOB doesn't rescue it either: most documents simply don't print
    # one, so its absence proves nothing. `mismatch` above is untouched — affirmative
    # evidence pointing at another roster person still blocks.
    if evidence is not None and name_tokens(claimed.full_name):
        return OwnerCheck(verdict="suspect", evidence=evidence)
    return OwnerCheck(verdict="unverified")


def refusal_message(
    check: OwnerCheck, claimed: Person, roster: Sequence[Person]
) -> str:
    """Human-facing text for a blocking verdict (the ``OwnerMismatchError`` message)."""
    lines = [
        "owner verification failed - this document does not look like it belongs to",
        f"  '{claimed.slug}' ({claimed.full_name}).",
    ]
    if check.verdict == "mismatch":
        other = next(
            (p for p in roster if p.slug == check.matched_slug), None
        )
        named = f"'{check.matched_slug}'"
        if other is not None:
            named += f" ({other.full_name})"
        lines.append(f"  text matches a different roster person: {named}")
    if check.evidence:
        lines.append(f'  found in document text: "{check.evidence}"')
    lines.append("  Nothing was ingested. If this is correct, re-run with --force.")
    return "\n".join(lines)


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


def _person_for_slug(conn: sqlite3.Connection, slug: str) -> Person:
    row = conn.execute(
        "SELECT * FROM person WHERE slug = ?", (slug.strip().lower(),)
    ).fetchone()
    if row is None:
        raise IngestError(
            f"no person with slug '{slug}' - add them first: `pemr person add`"
        )
    return Person.from_row(row)


def _roster(conn: sqlite3.Connection) -> list[Person]:
    """Everyone on the roster, **including deactivated people** — a deactivated person
    is still a real person whose documents must not land on someone else."""
    return [
        Person.from_row(row)
        for row in conn.execute("SELECT * FROM person ORDER BY slug").fetchall()
    ]


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
    force: bool = False,
) -> IngestResult:
    """Ingest one document: hash, layer-1 dedup, owner check, blob store, insert row.

    On a content-hash hit (layer 1), the existing document is returned with
    status "duplicate" and nothing is written. Otherwise the blob is copied into
    the immutable content-addressed store and a new `document` row is inserted.

    A Google Drive pointer stub is refused up front (issue #66) — before hashing, so
    nothing is written and there is nothing to clean up. See :func:`is_pointer_stub`.

    ``ocr_text`` is caller-supplied document text (the agent's own transcription —
    the `AGENTS.md` default path, which beats tesseract on messy scans). When
    provided it wins; otherwise ``ocr=True`` runs a best-effort :func:`extract_text`
    pass (native for text/OOXML, tesseract for images and PDFs — issue #66).
    An empty/whitespace-only string is treated as absent. Populating text here
    is what makes a document findable via FTS (`find`), so it is a warning-not-error
    when it ends up empty — see :attr:`IngestResult.ocr_text_populated`.

    When text is available it is also checked against the claimed owner (issue #61).
    A blocking verdict raises :class:`OwnerMismatchError` **before** anything is
    written, so a refused ingest is a clean no-op and ``force=True`` is the whole
    recovery. The verdict rides back on :attr:`IngestResult.owner_check` so callers
    report it without a second pass.
    """
    db.require_migrated(conn)

    src = Path(file_path)
    if not src.is_file():
        raise IngestError(f"file not found: {src}")

    # Pre-hash, pre-write: a refused pointer stub leaves no blob and no row. There is
    # deliberately no --force escape hatch — the stub bytes are never the thing you
    # want in the record, and renaming the file clears the (conjunctive) guard.
    if is_pointer_stub(src):
        raise IngestError(pointer_stub_message(src))

    person = _person_for_slug(conn, person_slug)
    sha = hash_file(src)

    existing = get_document(conn, sha)
    if existing is not None:
        # Layer-1 duplicate: nothing is written, so there is nothing to misfile. A
        # duplicate already filed under the wrong owner is `pemr document reassign`'s
        # job, not a reason to fail a no-op.
        return IngestResult(status="duplicate", document=existing)

    # Resolve the text *before* the blob copy so the owner check is pre-write. OCR
    # runs on `src` rather than the copied blob — identical bytes, same result.
    supplied = ocr_text.strip() if ocr_text else None
    ocr_text = supplied or (extract_text(src) if ocr else None)

    roster = _roster(conn)
    owner_check = check_owner(ocr_text, person, roster)
    if owner_check.blocks and not force:
        raise OwnerMismatchError(
            refusal_message(owner_check, person, roster), owner_check
        )

    ext = src.suffix.lower()
    dest = blob_dest(sources_dir, sha, ext)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # content-addressed => identical bytes, never overwrite. Track whether *this*
    # call created the blob so a failed `document` insert doesn't orphan it
    # (carry-forward advisory from #4): clean up only what we wrote.
    blob_created = not dest.exists()
    if blob_created:
        shutil.copy2(src, dest)

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
                    person.person_id,
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
    return IngestResult(status="new", document=document, owner_check=owner_check)

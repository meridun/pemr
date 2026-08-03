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

Issue #70 teaches the `--ocr` path to read **PDFs**: tesseract cannot decode a PDF at
all (its Leptonica backend has no PDF reader), so every PDF ingest used to store an
empty `ocr_text`. See :func:`_ocr_pdf`.
"""

from __future__ import annotations

import hashlib
import importlib
import re
import shutil
import subprocess
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

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


# --------------------------------------------------------------------------- #
# PDF text (issue #70).
#
# `tesseract scan.pdf stdout` fails outright — "Error in pixReadStream: Pdf reading
# is not supported" — so the `--ocr` path extracted *nothing* from a PDF, searchable
# or scanned, and said so only through a generic failure note. The fix is two-part
# and per page: use the embedded text layer where there is one, rasterize and OCR
# where there isn't.
#
# The PDF backend (PyMuPDF) is an **optional extra** (`pip install pemr[ocr]`), lazily
# imported and degrading to a stderr note when absent — the same soft-dependency
# stance the tesseract integration already takes. `_load_pdf_backend` is the single
# place the backend choice lives (and the monkeypatch seam the tests use, so neither
# PyMuPDF nor tesseract is needed to run the suite).
# --------------------------------------------------------------------------- #

# 300 dpi is tesseract's documented sweet spot for small print; grayscale buys back
# most of the cost of 300-over-200 with no accuracy loss on text.
OCR_DPI = 300

# Runtime guard, not a correctness one: a 400-page bundle would otherwise spend
# minutes in tesseract at ingest. Over the cap, the first `OCR_MAX_PAGES` are read and
# a stderr note names the shortfall.
OCR_MAX_PAGES = 20

# A page needs this much text-layer text to skip OCR. Not `> 0`: scanned PDFs commonly
# carry a stray stamp or watermark character, and a couple of those must not suppress
# OCR of an otherwise-image page.
PDF_TEXT_LAYER_MIN_CHARS = 20

# tesseract's own multi-page separator. Page provenance stays recoverable by splitting,
# and FTS5's unicode61 tokenizer treats it as a separator, so — unlike a "[page 2]"
# marker — it can never produce a false `find` hit.
_PAGE_SEPARATOR = "\f"

# What a PDF backend may raise on a malformed/truncated file. PyMuPDF's own errors
# (FileDataError, FileNotFoundError) subclass RuntimeError. Extraction is best-effort:
# any of these degrades to "no ocr_text", never a lost document.
_PDF_ERRORS = (RuntimeError, ValueError, OSError, TypeError, IndexError, KeyError)


def _load_pdf_backend():
    """The PDF backend module, or ``None`` when the ``pemr[ocr]`` extra isn't installed.

    `pymupdf` is the modern import name; `fitz` is the same package on older wheels.
    Kept as a one-liner seam so swapping the backend (e.g. to `pypdfium2`, if pemr is
    ever distributed and PyMuPDF's AGPL matters) stays a local change — and so tests
    can monkeypatch a fake in.
    """
    for name in ("pymupdf", "fitz"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


def _tesseract_stderr_tail(exc: BaseException) -> str:
    """Last non-empty line of a failed tesseract run's stderr ("" when there is none).

    Worth surfacing because tesseract explains itself there and the exception's own
    `str()` does not: 501 documents in one batch stored empty `ocr_text` while the one
    line that named the cause ("Pdf reading is not supported") was captured and
    dropped.
    """
    raw = getattr(exc, "stderr", None)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if not raw:
        return ""
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _ocr_page_image(png: bytes, label: str) -> str | None:
    """OCR one rendered page. PNG bytes go in over stdin — no temp files to clean up."""
    try:
        proc = subprocess.run(
            ["tesseract", "stdin", "stdout"],
            input=png,
            capture_output=True,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        detail = _tesseract_stderr_tail(exc)
        print(
            f"note: tesseract OCR failed on {label} ({exc}"
            f"{': ' + detail if detail else ''})",
            file=sys.stderr,
        )
        return None
    # Bytes in, bytes out: decode explicitly rather than letting `text=True` pick the
    # platform codepage (issue #64 — cp1252 on Windows crashes on tesseract's UTF-8).
    return proc.stdout.decode("utf-8", errors="replace").strip() or None


def _ocr_pdf(src: Path) -> str | None:
    """Text of a PDF: embedded text layer per page, OCR of a rendered page otherwise.

    Per page rather than per document, so a scan appended to a searchable report is
    still read. Never raises — an encrypted or unparseable PDF is a stderr note and
    ``None``, the same warning-not-error contract :func:`run_ocr` already has.
    """
    backend = _load_pdf_backend()
    if backend is None:
        print(
            "note: --ocr on a PDF needs the PDF backend; install it with "
            "`pip install pemr[ocr]`. Storing document without ocr_text",
            file=sys.stderr,
        )
        return None
    try:
        doc = backend.open(str(src))
    except _PDF_ERRORS as exc:
        print(
            f"note: could not read PDF {src.name} ({exc}); storing without ocr_text",
            file=sys.stderr,
        )
        return None

    pages: list[str] = []
    # None = not looked yet. Deferred on purpose: a fully searchable PDF needs no
    # tesseract at all and must not draw a "tesseract is not on PATH" note.
    have_tesseract: bool | None = None
    try:
        if getattr(doc, "needs_pass", False):
            print(
                f"note: {src.name} is password-protected; storing without ocr_text",
                file=sys.stderr,
            )
            return None
        total = doc.page_count
        if total > OCR_MAX_PAGES:
            print(
                f"note: {src.name} has {total} pages; storing text for the first "
                f"{OCR_MAX_PAGES} only",
                file=sys.stderr,
            )
        for index in range(min(total, OCR_MAX_PAGES)):
            label = f"{src.name} page {index + 1}"
            try:
                page = doc[index]
                text = (page.get_text() or "").strip()
                if len(text) < PDF_TEXT_LAYER_MIN_CHARS:
                    if have_tesseract is None:
                        have_tesseract = shutil.which("tesseract") is not None
                        if not have_tesseract:
                            print(
                                "note: --ocr requested but `tesseract` is not on "
                                "PATH; storing document without ocr_text",
                                file=sys.stderr,
                            )
                    if have_tesseract:
                        pixmap = page.get_pixmap(
                            dpi=OCR_DPI, colorspace=backend.csGRAY
                        )
                        text = _ocr_page_image(pixmap.tobytes("png"), label) or text
            except _PDF_ERRORS as exc:
                print(
                    f"note: could not read {label} ({exc}); skipping that page",
                    file=sys.stderr,
                )
                continue
            if text:
                pages.append(text)
    finally:
        doc.close()
    return _PAGE_SEPARATOR.join(pages).strip() or None


def run_ocr(path: str | Path) -> str | None:
    """Best-effort document text for `--ocr`. Soft dependencies throughout:

    returns None (with a stderr note) when a needed tool is unavailable instead of
    failing the ingest. A PDF goes to :func:`_ocr_pdf` (text layer per page, rendered
    + OCR'd where there is none); everything else is handed to a system `tesseract`,
    which is only sensible for flat image scans. Callers pass the result through as
    `ocr_text` for the agent to work from.
    """
    src = Path(path)
    if src.suffix.lower() == ".pdf":
        # tesseract cannot decode a PDF at all, so this is a different pipeline, not
        # a tweak to the one below.
        return _ocr_pdf(src)
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
        detail = _tesseract_stderr_tail(exc)
        print(
            f"note: tesseract OCR failed ({exc}"
            f"{': ' + detail if detail else ''}); storing without ocr_text",
            file=sys.stderr,
        )
        return None
    text = proc.stdout.strip()
    return text or None


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
        # `M/D/YYYY` rendering is a *tail* of the day-first form, so `23/7/1981`
        # would "match" a 1981-03-07 person — a silent misfile, the exact failure
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

    ``ocr_text`` is caller-supplied document text (the agent's own transcription —
    the `AGENTS.md` default path, which beats tesseract on messy scans). When
    provided it wins; otherwise ``ocr=True`` falls back to a best-effort
    :func:`run_ocr` pass (tesseract on images, text layer + rendered-page OCR on
    PDFs). An empty/whitespace-only string is treated as absent. Populating text here
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
    ocr_text = supplied or (run_ocr(src) if ocr else None)

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

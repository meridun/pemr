"""Document intake — hashing, content-addressed blob store, layer-1 dedup.

The deterministic first half of the ingestion pipeline (Architecture.md §4):

    1. sha256 the raw bytes; if the hash is already in `document` -> duplicate, stop;
       if it carries a `document_tombstone` row -> tombstoned, stop (issue #80)
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
Issue #138 adds one more native route to that dispatcher, **CCDA** (C-CDA / HL7 CDA R2)
XML — what every US portal's "download my record" produces — whose narrative was
previously handed to tesseract, which cannot decode XML.

Issue #70 closes that dispatcher's last hole, **PDFs**. They fall through to
:func:`run_ocr`, and tesseract cannot decode a PDF at all (its Leptonica backend has
no PDF reader), so every PDF ingest used to store an empty `ocr_text`. See
:func:`_ocr_pdf`.

Issue #143 adds the *backwards* half of those extractor fixes: :func:`reocr_documents`
re-runs today's dispatch against a blob already in `sources/`, so a document ingested
before an extractor improved can gain the text it never got. It calls the same
:func:`extract_text_routed` `ingest` calls (so the two paths cannot drift) and writes
through the same :func:`pemr.documents.set_document_text` guard.

Issue #69 adds a second entry point, :func:`ingest_study_dir`: a DICOM study
*directory* becomes one document whose blob is a canonical zip of its slices (see
:mod:`pemr.study`). It reuses every rail above — same person lookup, same layer-1
dedup on the content hash, same content-addressed store, same insert, and the same
owner check, fed by the study's ``PatientName``/``PatientBirthDate`` header tags.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sqlite3
import sys
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
import zipfile
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Sequence
from urllib.parse import urlsplit

from . import db, study as _study, tombstones as _tombstones
from .documents import normalize_document_text, set_document_text
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
    status: str  # "new" | "duplicate" | "tombstoned"
    # None on the tombstoned path: nothing was written, and there is no document to
    # point at (the whole point of a tombstone is that the content is not on file).
    document: Document | None = None
    # None on the duplicate path: a layer-1 duplicate returns before any check runs
    # (nothing is written, so there is nothing to misfile). Also None when tombstoned.
    owner_check: "OwnerCheck | None" = None
    # The `document_tombstone` row, on the tombstoned path only (issue #80).
    tombstone: dict | None = None

    @property
    def is_duplicate(self) -> bool:
        return self.status == "duplicate"

    @property
    def is_tombstoned(self) -> bool:
        """Whether the content was refused by an intentional-removal record (issue #80).

        A benign, expected no-op with the same shape as ``duplicate`` — *not* an error.
        The bulk sweep is the common case this exists to serve, and a sweep that starts
        exiting non-zero on expected skips trains operators into `|| true`, re-burying
        the signal.
        """
        return self.status == "tombstoned"

    @property
    def ocr_text_populated(self) -> bool:
        """Whether the stored document ended up with any OCR/transcription text.

        The `AGENTS.md` contract asks agents to populate `ocr_text` on every ingest
        (empty FTS otherwise); this lets a caller self-check without a follow-up read.
        """
        return bool(self.document and self.document.ocr_text)


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
    # `normalize_document_text`, not `.strip()`: tesseract on a blank page can emit
    # nothing but zero-widths/form-feeds, which must count as "no text" on every
    # route, not just the supplied-text one (issue #87).
    return normalize_document_text(proc.stdout.decode("utf-8", errors="replace")) or None


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
                text = normalize_document_text(page.get_text())
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
    return normalize_document_text(_PAGE_SEPARATOR.join(pages)) or None


def pdf_page_count(src: Path) -> int | None:
    """How many pages a PDF has, or ``None`` when that cannot be known.

    ``None`` covers every "don't know" — the ``pemr[ocr]`` extra is absent, the file is
    not a PDF, or it is unreadable/password-protected — so a caller can only ever say
    "this document is over the cap" on positive evidence. Never raises, same contract
    as :func:`_ocr_pdf`.

    Deliberately a second, tiny open rather than a refactor of :func:`_ocr_pdf` (which
    needs the page handles it already holds): both read the one ``OCR_MAX_PAGES``
    constant, so there is nothing here to drift. Exists for `document reocr` (issue
    #143), where the operator is not looking at the source document and so cannot see
    the truncation note `_ocr_pdf` prints.
    """
    if src.suffix.lower() != ".pdf":
        return None
    backend = _load_pdf_backend()
    if backend is None:
        return None
    try:
        doc = backend.open(str(src))
    except _PDF_ERRORS:
        return None
    try:
        if getattr(doc, "needs_pass", False):
            return None
        return int(doc.page_count)
    except _PDF_ERRORS:
        return None
    finally:
        doc.close()


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
            # tesseract emits UTF-8. Without an explicit encoding, `text=True`
            # decodes with the platform default (cp1252 on Windows), which kills
            # the subprocess reader thread with UnicodeDecodeError on any byte
            # invalid in that codepage — failing the whole ingest. errors=
            # "replace" keeps a page with a stray undecodable byte usable rather
            # than losing the OCR entirely.
            encoding="utf-8",
            errors="replace",
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
    # Same predicate as every other route into `ocr_text` (issue #87): this return
    # goes straight out of `extract_text_routed` without passing its normalisation.
    text = normalize_document_text(proc.stdout)
    return text or None


# --------------------------------------------------------------------------- #
# Text extraction (issue #66) — `ocr=True` means "extract text by whatever route
# this file type allows", not "shell out to tesseract". `run_ocr` keeps its name and
# its tesseract semantics and becomes the image/PDF branch of the dispatcher below.
#
# Hard constraint: **zero new dependencies.** Everything here is stdlib (CCDA included —
# `xml.etree.ElementTree` is enough for it), which is what draws the scope line —
# `.rtf`, `.msg`, `.doc` and PDF *text-layer* extraction all
# need a third-party parser and stay out, covered by the agent transcription path
# (`--ocr-text-file`) that `AGENTS.md` §3 already makes the default.
# --------------------------------------------------------------------------- #

_PLAINTEXT_SUFFIXES = frozenset({".txt", ".md", ".csv", ".tsv", ".json", ".log"})

_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# CCDA / C-CDA (HL7 CDA R2) — issue #138. `.xml` is a *container* suffix, so unlike
# `.docx`/`.xlsx` the detection is on the parsed root element, never the extension.
_CDA_NS = "{urn:hl7-org:v3}"
_CDA_ROOT = f"{_CDA_NS}ClinicalDocument"
_CCDA_SNIFF_BYTES = 8192

# Errors an OOXML/plaintext read can legitimately produce on a malformed or truncated
# file. Extraction is best-effort: any of these degrades to "no ocr_text", never a lost
# document — the same contract `run_ocr` already has for a missing tesseract.
# `RecursionError` is in the list because element nesting depth is document-controlled
# and the stdlib's own walkers recurse: `Element.itertext()` is a recursive generator,
# so a pathologically nested narrative can exhaust the stack even though our own walk
# is iterative. It is a `RuntimeError`, so it would otherwise escape the "never raises"
# contract and cost the document (issue #138 audit).
_EXTRACT_ERRORS = (
    OSError, ValueError, KeyError, IndexError, RecursionError,
    zipfile.BadZipFile, ET.ParseError,
)

_SHEET_NUM = re.compile(r"(\d+)")

# Ceiling on how much text one file may contribute. `ocr_text` is stored in the row
# *and* mirrored into the FTS index, so an unbounded read is both a DB-size problem
# (a 106 MB log grew the database to 226 MB) and a decompression-bomb surface: a 917 KB
# `.docx` whose `word/document.xml` inflates to ~1 GB is trivial to build, and
# `MemoryError` is not something the best-effort handler below can honestly promise to
# absorb. 32 MiB is far past any real medical document and far below hurting anything.
_MAX_EXTRACT_BYTES = 32 * 1024 * 1024


def _xml_text(node: ET.Element, tag: str) -> str:
    """Concatenated text of every ``tag`` descendant (OOXML splits runs arbitrarily)."""
    return "".join(child.text or "" for child in node.iter(tag))


def _member_reader(zf: zipfile.ZipFile) -> Callable[[str], bytes]:
    """Bounded member reader sharing one uncompressed-size budget across the archive.

    `ZipInfo.file_size` is the *declared* uncompressed size, so the check costs no
    read; `ZipExtFile` never yields more than that many bytes, so a lying header can
    only under-deliver. Over budget raises `ValueError`, which the caller's
    best-effort handler turns into the usual "no ocr_text" note.
    """
    remaining = _MAX_EXTRACT_BYTES

    def read(name: str) -> bytes:
        nonlocal remaining
        size = zf.getinfo(name).file_size
        if size > remaining:
            raise ValueError(
                f"{name} expands to {size} bytes, past the "
                f"{_MAX_EXTRACT_BYTES}-byte extraction cap"
            )
        remaining -= size
        return zf.read(name)

    return read


def _extract_docx(path: Path) -> str:
    """`.docx` body text: concat `w:t` runs, one line per `w:p` paragraph."""
    with zipfile.ZipFile(path) as zf:
        read_member = _member_reader(zf)
        root = ET.fromstring(read_member("word/document.xml"))
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
        read_member = _member_reader(zf)
        names = zf.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(read_member("xl/sharedStrings.xml"))
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
            root = ET.fromstring(read_member(name))
            for row in root.iter(f"{_SHEET_NS}row"):
                lines.append("\t".join(
                    _cell_text(cell, shared) for cell in row.iter(f"{_SHEET_NS}c")
                ))
    return "\n".join(lines)


_CDA_TS_DIGITS = re.compile(r"\d+")


def _localname(tag: str) -> str:
    """Element name without its `{namespace}` prefix."""
    return tag.rpartition("}")[2]


def _ccda_flat(el: ET.Element) -> str:
    """One element → one whitespace-collapsed line.

    The single flattening primitive: it is what turns `<content>`, `<linkHtml>`,
    `<sub>`/`<sup>` and any unknown inline markup inside a narrative block into text.
    """
    return " ".join("".join(el.itertext()).split())


def _ccda_name(el: ET.Element) -> str:
    """A CDA `<name>` as one line, one space per element boundary.

    Not `_ccda_flat`: that concatenates (right for narrative, where markup splits
    *inside* a word), and `<given>Jane</given><family>Doe</family>` carries no
    whitespace of its own, so concatenating yields `JaneDoe` — a single token the
    owner check can never match against "Jane Doe".
    """
    return " ".join(
        " ".join(chunk.split()) for chunk in el.itertext() if chunk.strip()
    )


def _ccda_table_rows(table: ET.Element) -> list[str]:
    """Narrative `<table>` as one tab-delimited line per `<tr>` (`thead` and `tbody`
    alike, document order). Tab is what `_extract_xlsx` already uses for tabular text,
    and it is what keeps a header cell on the same line as its value."""
    rows: list[str] = []
    for tr in table.iter():
        if _localname(tr.tag) != "tr":
            continue
        cells = [
            _ccda_flat(cell) for cell in tr
            if _localname(cell.tag) in ("th", "td")
        ]
        if any(cells):
            rows.append("\t".join(cells))
    return rows


def _ccda_narrative(node: ET.Element) -> list[str]:
    """One section's `<text>` narrative block, rendered as lines.

    Walked with an explicit stack rather than recursion: nesting depth is whatever the
    document says it is (~1000 levels of `<content>` fits in 20 KB, far under the
    extraction cap), and a `RecursionError` here is a `RuntimeError` — it would escape
    :func:`extract_text_routed`'s "never raises" contract and cost the document.
    """
    lines: list[str] = []
    if (node.text or "").strip():
        lines.append(" ".join(node.text.split()))
    # Each frame is (remaining children, that element's tail) — the tail is emitted
    # when the frame pops, i.e. *after* its subtree, exactly as the recursion did.
    stack: list[tuple[Iterator[ET.Element], str | None]] = [(iter(node), None)]
    while stack:
        children, tail = stack[-1]
        child = next(children, None)
        if child is None:
            stack.pop()
            if (tail or "").strip():
                lines.append(" ".join(tail.split()))
            continue
        name = _localname(child.tag)
        if name == "table":
            lines.extend(_ccda_table_rows(child))
        elif name in ("paragraph", "item", "caption"):
            # Nested inline markup is already flattened by `_ccda_flat`; descending
            # would emit its text a second time.
            flat = _ccda_flat(child)
            if flat:
                lines.append(flat)
        elif name != "renderMultiMedia":   # an image reference has no text to give
            if (child.text or "").strip():
                lines.append(" ".join(child.text.split()))
            stack.append((iter(child), child.tail))
            continue                       # its tail is emitted when that frame pops
        if (child.tail or "").strip():
            lines.append(" ".join(child.tail.split()))
    return lines


def _ccda_date(value: str | None) -> str | None:
    """A CDA `TS/@value` (`"19620314000000-0600"`) as an ISO-ish date string.

    The 8-digit → `YYYY-MM-DD` reformat is load-bearing: :func:`dob_candidates`
    recognises `1962-03-14` and friends but not the raw `19620314` form, so without
    it the DOB half of the owner check could never fire on a CCDA.
    """
    digits = _CDA_TS_DIGITS.match((value or "").strip())
    if digits is None:
        return None
    raw = digits.group()
    if len(raw) >= 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    if len(raw) >= 6:
        return f"{raw[:4]}-{raw[4:6]}"
    if len(raw) >= 4:
        return raw[:4]
    return None


def _ccda_header(root: ET.Element) -> list[str]:
    """`recordTarget` identity lines, prepended so the #61 owner check has an anchor.

    A CCDA's own header is the most reliable identity in the document, and without it
    the check degrades to "filed on your say-so" — the symptom issue #138 reports.
    """
    lines: list[str] = []
    patient = root.find(
        f"{_CDA_NS}recordTarget/{_CDA_NS}patientRole/{_CDA_NS}patient"
    )
    if patient is None:
        return lines
    for name in patient.findall(f"{_CDA_NS}name"):
        flat = _ccda_name(name)   # `given`/`given`/`family` → "Jane A Doe"
        if flat:
            lines.append(f"Patient: {flat}")
    birth = patient.find(f"{_CDA_NS}birthTime")
    if birth is not None:
        dob = _ccda_date(birth.get("value"))
        if dob:
            lines.append(f"DOB: {dob}")
    return lines


class _DoctypeFound(Exception):
    """Sentinel: the prolog probe reached a ``<!DOCTYPE`` declaration."""


class _PrologOver(Exception):
    """Sentinel: the prolog probe reached the root element's start tag."""


def _xml_declares_doctype(data: bytes) -> bool:
    """True when a ``<!DOCTYPE`` sits in the XML **prolog**.

    The prolog — everything before the root element's start tag — is the only place a
    DOCTYPE may legally appear, and the internal subset it can carry is an
    entity-amplification bomb the byte cap does not bound (a 1.4 KB file rendered 1 MB
    of text, a 2 MB one 210 MB; issue #138 audit).

    The check is **encoding-agnostic by construction**: it asks expat — the same parser
    :func:`_extract_ccda` is about to hand the bytes to — rather than scanning for byte
    patterns. Two hand-rolled byte scanners were bypassed before this one: a fixed head
    window (a leading comment pads the DOCTYPE past it while the CCDA markers stay
    inside), and a whole-prolog scan for ASCII ``<!--``/``<?``/``<!doctype`` (in a
    UTF-16 document every marker is ``<\\x00!\\x00…``, so the scan mistook the first
    ``<`` for a start tag and the DOCTYPE was never seen). Establishing the encoding is
    exactly the work a scanner has to re-implement to be correct, and expat has already
    done it — from the BOM and the ``encoding=`` pseudo-attribute — by the time it
    reports either event.

    Nothing expands: expat fires ``StartDoctypeDeclHandler`` *before* it reads the
    internal subset, and the probe aborts out of the parse there. A document with no
    DOCTYPE costs only the prolog — the parse aborts at the root start tag rather than
    reading the body. A parse error means expat cannot read the file at all, so the
    :func:`ET.fromstring` below cannot either (same parser): ``False`` sends it down the
    unchanged non-CCDA route.
    """
    def _doctype(*_args: object) -> None:
        raise _DoctypeFound

    def _element(*_args: object) -> None:
        raise _PrologOver

    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = _doctype
    parser.StartElementHandler = _element
    try:
        parser.Parse(data, True)
    except _DoctypeFound:
        return True
    except (_PrologOver, expat.ExpatError):
        return False
    return False                       # no root element at all — `ET` will reject it too


def _extract_ccda(path: Path) -> str | None:
    """CCDA narrative text, or ``None`` when the file is **not** a CCDA.

    ``None`` is the caller's signal to fall through to :func:`run_ocr` unchanged:
    `.xml` is a container suffix, so an ordinary XML file must keep today's route.
    """
    size = path.stat().st_size
    if size > _MAX_EXTRACT_BYTES:
        # Deliberately a raise, not a fall-through: the honest "past the extraction
        # cap" note beats handing a 40 MB XML to tesseract to fail on.
        raise ValueError(
            f"{size} bytes, past the {_MAX_EXTRACT_BYTES}-byte extraction cap"
        )
    with path.open("rb") as fh:
        head = fh.read(_CCDA_SNIFF_BYTES)
    # Cheap negative first, so an ordinary `.xml` is never read whole or parsed. The
    # markers are matched as raw ASCII, so a CCDA in an encoding that is not
    # ASCII-compatible (UTF-16/UTF-32) sniffs as non-CCDA and keeps today's tesseract
    # route. That narrowing is deliberate: US portal exports are UTF-8, and the sniff
    # is a *negative* filter — being conservative here costs nothing beyond the status
    # quo, whereas the DOCTYPE refusal below has to be right for every encoding, which
    # is why it asks expat instead of matching bytes.
    if b"urn:hl7-org:v3" not in head or b"ClinicalDocument" not in head:
        return None
    data = path.read_bytes()           # bounded by the cap checked above
    # A conformant CCDA has no internal subset, and stdlib `ET` *does* expand internal
    # entities — so a `<!DOCTYPE` is refused here rather than parsed (billion-laughs on
    # a file whose bytes are well under the cap). Falling through costs nothing: today
    # such a file goes to tesseract anyway.
    if _xml_declares_doctype(data):
        return None
    try:
        root = ET.fromstring(data)     # the bytes already in hand, not a second read
    except ET.ParseError:
        # Malformed XML is not *detectably* a CCDA, so it takes today's path.
        return None
    if root.tag != _CDA_ROOT:
        return None

    lines = _ccda_header(root)
    body = root.find(f"{_CDA_NS}component/{_CDA_NS}structuredBody")
    if body is not None:
        # `.iter` so a nested section is rendered too; the narrative is read from each
        # section's *direct* `<text>` child, or a nested one would be emitted twice.
        for section in body.iter(f"{_CDA_NS}section"):
            block: list[str] = []
            title = section.find(f"{_CDA_NS}title")
            heading = _ccda_flat(title) if title is not None else ""
            if heading:
                block.append(heading)
            narrative = section.find(f"{_CDA_NS}text")
            if narrative is not None:
                block.extend(_ccda_narrative(narrative))
            if block:
                if lines:
                    lines.append("")
                lines.extend(block)
    # No `structuredBody` (a `nonXMLBody` CDA) still returns the header alone: strictly
    # better than today, since it restores owner verification.
    return "\n".join(lines)


def extract_text_routed(path: str | Path) -> tuple[str | None, str]:
    """:func:`extract_text` plus the **route** that produced the text.

    Dispatches on suffix: plaintext-ish formats are read directly and `.docx`/`.xlsx`
    are unzipped and their OOXML parsed with the stdlib (route ``"native"``). A CCDA
    `.xml` (issue #138) is rendered natively too — sections' narrative plus a
    `recordTarget` identity header — but on the parsed **root element**, not the
    suffix: a non-CCDA or malformed `.xml` falls through to the OCR route exactly as
    it did before that branch existed.
    **Everything else falls through to :func:`run_ocr`** (route ``"ocr"``) — the same
    thing `ocr=True` did before this dispatcher existed. Deliberately not a suffix
    allowlist: image extensions vary far too widely (`.jfif`, `.jpe`, extension-less
    scans) for one to be safe, and silently skipping a scan that used to OCR is the
    worse failure — it drops the document out of `find` with no signal. `.pdf` takes
    its own branch inside `run_ocr` (issue #70) rather than reaching tesseract, which
    cannot decode one. When that pass declines (tool absent, failed, or no text — e.g.
    `.rtf`, `.msg`, `.doc`, or a PDF with no `pemr[ocr]` extra installed) the caller
    gets ``None`` plus a stderr note pointing at `--ocr-text-file`.

    The route matters to the caller because the #61 owner check reads identity
    *anchors* (`Patient`, `DOB`, `MRN`) as "this document names somebody". That
    inference holds for a scanned or transcribed page and not for a spreadsheet,
    where those words are column labels — see ``trust_anchors`` on :func:`check_owner`.

    Never raises: a malformed `.docx` must not cost you the document.
    """
    src = Path(path)
    suffix = src.suffix.lower()
    try:
        if suffix in _PLAINTEXT_SUFFIXES:
            size = src.stat().st_size
            if size > _MAX_EXTRACT_BYTES:
                raise ValueError(
                    f"{size} bytes, past the {_MAX_EXTRACT_BYTES}-byte extraction cap"
                )
            # utf-8-sig eats a BOM; errors="replace" keeps a legacy-encoded file
            # usable rather than losing it entirely (same trade as run_ocr's decode).
            text = src.read_text(encoding="utf-8-sig", errors="replace")
        elif suffix == ".docx":
            text = _extract_docx(src)
        elif suffix == ".xlsx":
            text = _extract_xlsx(src)
        # `.xml` deliberately stays out of `_PLAINTEXT_SUFFIXES`: only a file whose
        # root element is `{urn:hl7-org:v3}ClinicalDocument` is read natively, and
        # every other `.xml` falls through to the `run_ocr` branch below unchanged.
        elif suffix == ".xml" and (ccda := _extract_ccda(src)) is not None:
            text = ccda
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
            return ocr_text, "ocr"
    except _EXTRACT_ERRORS as exc:
        print(
            f"note: text extraction failed for {src.name} ({exc}); "
            "storing without ocr_text",
            file=sys.stderr,
        )
        return None, "native"
    # Same predicate as the supplied-text route below - one emptiness notion per
    # column, so `--ocr auto` cannot store what `--ocr-text-file` rejects (issue #87).
    text = normalize_document_text(text)
    return (text or None), "native"


def extract_text(path: str | Path) -> str | None:
    """Best-effort document text by file type; ``None`` when nothing could be read.

    Route-blind convenience wrapper over :func:`extract_text_routed`, which is what
    :func:`ingest_document` calls.
    """
    return extract_text_routed(path)[0]


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
      ``DOB:`` header for someone who is not on the roster at all). Only reachable
      for transcribed/OCR'd text — see ``trust_anchors`` on :func:`check_owner`.
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
    text: str | None,
    claimed: Person,
    roster: Sequence[Person],
    *,
    trust_anchors: bool = True,
) -> OwnerCheck:
    """Does ``text`` look like it belongs to ``claimed``? See :class:`OwnerCheck`.

    ``trust_anchors=False`` says the text is **structured**, not prose — a CSV, a
    spreadsheet, a JSON export — where ``Patient``/``DOB``/``MRN`` are column labels
    and field keys rather than a printed identity header. `_ANCHOR` was tuned for
    scanned document headers and does not transfer: counting a `Patient ID,Test,Value`
    header row as "this document names somebody" refuses an ordinary lab export as
    belonging to a stranger. Only the ``suspect`` inference is suppressed;
    ``match``/``mismatch`` are affirmative name/DOB evidence and hold on every route.
    """
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
    if trust_anchors and evidence is not None and name_tokens(claimed.full_name):
        return OwnerCheck(verdict="suspect", evidence=evidence)
    return OwnerCheck(verdict="unverified")


def _strongest_check(checks: Sequence[OwnerCheck]) -> OwnerCheck:
    """The verdict to act on when more than one identity signal was checked.

    A study has two (the DICOM header tags and any caller-supplied transcription),
    and they are independent, so the ordering is by consequence: anything blocking
    wins — refusing on *any* evidence of a misfile is the whole point of the check —
    then an affirmative match, then ignorance.
    """
    for verdict in ("mismatch", "suspect", "match"):
        for check in checks:
            if check.verdict == verdict:
                return check
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


def _person_for_id(conn: sqlite3.Connection, person_id: int | None) -> Person | None:
    """The claimed owner of an already-filed document, or ``None`` when unowned.

    Sibling of :func:`_person_for_slug` for the re-run path (issue #143), where the
    owner comes from the stored row rather than a `--person` flag. Unowned is not an
    error here — there is simply nobody to check the recovered text against.
    """
    if person_id is None:
        return None
    row = conn.execute(
        "SELECT * FROM person WHERE person_id = ?", (person_id,)
    ).fetchone()
    return Person.from_row(row) if row is not None else None


def _roster(conn: sqlite3.Connection) -> list[Person]:
    """Everyone on the roster, **including deactivated people** — a deactivated person
    is still a real person whose documents must not land on someone else."""
    return [
        Person.from_row(row)
        for row in conn.execute("SELECT * FROM person ORDER BY slug").fetchall()
    ]


def _insert_document(
    conn: sqlite3.Connection,
    *,
    sha: str,
    ext: str,
    person_id: int,
    doc_date: str | None,
    category: str | None,
    provider: str | None,
    ocr_text: str | None,
) -> Document:
    """Insert the `document` row and read it back. Shared by both ingest paths.

    Blob cleanup on failure stays with the caller: the file path copies its blob in
    and the study path renames one in, so only they know what to undo.
    """
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
    document = get_document_by_id(conn, cur.lastrowid)
    assert document is not None  # just inserted
    return document


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
    tombstone_force: bool = True,
) -> IngestResult:
    """Ingest one document: hash, layer-1 dedup, owner check, blob store, insert row.

    On a content-hash hit (layer 1), the existing document is returned with
    status "duplicate" and nothing is written. Otherwise the blob is copied into
    the immutable content-addressed store and a new `document` row is inserted.

    A hash carrying a `document_tombstone` row — a removal a human deliberately recorded
    as permanent (issue #80) — returns status ``"tombstoned"`` with the row attached and
    writes nothing. That is a benign expected skip, not an error: the bulk sweep is the
    common case, and failing it would train operators to ignore the output. ``force=True``
    ingests anyway and **does not lift the tombstone** — it is a one-shot override, so the
    next sweep skips again; the row rides back on the ``"new"`` result so callers can warn.
    ``tombstone_force=False`` withholds that override from ``force`` (the MCP surface
    passes it: a tombstone *is* the human's already-recorded decision, so overriding it
    is never an agent judgment call — unlike the owner check, which is a heuristic).

    A Google Drive pointer stub is refused up front (issue #66) — before hashing, so
    nothing is written and there is nothing to clean up. See :func:`is_pointer_stub`.

    ``ocr_text`` is caller-supplied document text (the agent's own transcription —
    the `AGENTS.md` default path, which beats tesseract on messy scans). When
    provided it wins; otherwise ``ocr=True`` runs a best-effort
    :func:`extract_text_routed` pass (native for text/OOXML, issue #66; everything
    else through :func:`run_ocr` — tesseract on images, text layer + rendered-page
    OCR on PDFs, issue #70).
    An empty/whitespace-only string is treated as absent. Populating text here
    is what makes a document findable via FTS (`find`), so it is a warning-not-error
    when it ends up empty — see :attr:`IngestResult.ocr_text_populated`.

    When text is available it is also checked against the claimed owner (issue #61),
    with the identity-anchor (``suspect``) half of that check applied only to prose —
    transcribed or OCR'd text, not natively-extracted CSV/OOXML/JSON, whose
    ``Patient``/``DOB`` tokens are column labels. A blocking verdict raises
    :class:`OwnerMismatchError` **before** anything is
    written, so a refused ingest is a clean no-op and ``force=True`` is the whole
    recovery. The verdict rides back on :attr:`IngestResult.owner_check` so callers
    report it without a second pass.
    """
    db.require_migrated(conn)

    src = Path(file_path)
    if src.is_dir():
        # Naming the flag beats the old, misleading "file not found: <dir>".
        raise IngestError(
            f"{src} is a directory; ingest a study folder with "
            f"--study {_study.STUDY_KINDS[0]} (see `pemr ingest --help`)"
        )
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

    # Removal memory (issue #80). Strictly *after* the `document` lookup: a live
    # document means the content is filed, which is a different answer than "excluded",
    # and `tombstone add` refuses the both-at-once state except via `--force` below.
    tombstone = _tombstones.get_tombstone(conn, sha)
    if tombstone is not None and not (force and tombstone_force):
        return IngestResult(status="tombstoned", tombstone=tombstone)

    # Resolve the text *before* the blob copy so the owner check is pre-write. OCR
    # runs on `src` rather than the copied blob — identical bytes, same result.
    # `normalize_document_text`, not `.strip()`: a supplied text of nothing but
    # zero-width characters is empty, and must neither count as supplied nor land in
    # `ocr_text` (issue #87).
    supplied = normalize_document_text(ocr_text) or None
    if supplied:
        # An agent transcription is prose off the page: anchors mean what they say.
        ocr_text, route = supplied, "ocr"
    elif ocr:
        ocr_text, route = extract_text_routed(src)
    else:
        ocr_text, route = None, "ocr"  # no text at all; the route is moot

    roster = _roster(conn)
    # Natively-extracted text (CSV/OOXML/JSON) is structured, so a `Patient ID` column
    # header is not an identity claim — trusting anchors there refuses ordinary lab
    # exports as belonging to a stranger. `mismatch` still blocks on every route.
    owner_check = check_owner(
        ocr_text, person, roster, trust_anchors=(route != "native")
    )
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
        document = _insert_document(
            conn,
            sha=sha,
            ext=ext,
            person_id=person.person_id,
            doc_date=doc_date,
            category=category,
            provider=provider,
            ocr_text=ocr_text,
        )
    except Exception:
        if blob_created:
            dest.unlink(missing_ok=True)
        raise
    # `tombstone` is non-None here only on the `--force` override path: the row is
    # deliberately *not* lifted (see the docstring), so callers warn and name the undo.
    return IngestResult(
        status="new", document=document, owner_check=owner_check, tombstone=tombstone
    )


# --------------------------------------------------------------------------- #
# Study directories (issue #69)
# --------------------------------------------------------------------------- #

#: Blob extension for a packed study. `blob_dest`/`_relative_source_path` treat it as
#: an opaque suffix, so `sources/<sha[:2]>/<sha>.dcm.zip` keeps the content-addressed
#: invariant and `pemr verify` re-hashes it like any other blob.
STUDY_EXT = ".dcm.zip"

#: Staging area for the archive: it is packed here, hashed, then `os.replace`d into
#: the store — a same-filesystem atomic publish, so a crash can never leave a
#: truncated blob sitting under a valid content-hash name.
TMP_DIRNAME = ".tmp"

#: A study is imaging by construction, so it defaults there rather than to null; the
#: caller still wins (a disc filed under a narrower `radiology` bucket, say).
_STUDY_CATEGORY = "imaging"


def ingest_study_dir(
    conn: sqlite3.Connection,
    dir_path: str | Path,
    person_slug: str,
    sources_dir: str | Path,
    *,
    study: str = "dicom",
    allow_large: bool = False,
    doc_date: str | None = None,
    category: str | None = None,
    provider: str | None = None,
    ocr_text: str | None = None,
    force: bool = False,
    tombstone_force: bool = True,
) -> IngestResult:
    """Ingest a study *directory* as one document (issue #69).

    A burned imaging disc is clinically one document but physically ~2,000 slice
    files plus a viewer payload. The slices are packed into a canonical zip
    (:func:`pemr.study.pack_study`) and **that archive's** sha256 is the document
    hash — so the blob is still the original bytes, still content-addressed, and
    layer-1 dedup needs no special case.

    Derived defaults, all of which the caller beats:

    * ``doc_date`` ← the study's ``StudyDate`` tag,
    * ``category`` ← ``"imaging"``,
    * ``ocr_text`` ← a short derived summary (modality/date/series), so the study is
      visible to `find` instead of being an untitled row. An agent that transcribed
      the accompanying radiology report should pass that text instead.

    The issue-#61 owner check runs against two independent signals: the study's own
    ``PatientName``/``PatientBirthDate`` header tags (:func:`pemr.study.identity_text`)
    and any caller-supplied text, with the more consequential verdict winning. It is
    never run against our *derived summary*, which would be meaningless (engine
    output carries no patient identity) and worse than meaningless if a
    ``StudyDescription`` like "PATIENT POSITIONING" tripped the identity anchor —
    a spurious refusal of a study nobody could fix without ``force``. The refusal
    point is pre-write **and** pre-pack, so a refused study costs nothing.
    """
    db.require_migrated(conn)

    if study not in _study.STUDY_KINDS:
        raise IngestError(
            f"unknown study kind '{study}'; supported: "
            + ", ".join(_study.STUDY_KINDS)
        )

    src = Path(dir_path)
    if src.is_file():
        raise IngestError(
            f"--study expects a directory, but {src} is a file; "
            "ingest it without --study"
        )
    if not src.is_dir():
        raise IngestError(f"directory not found: {src}")

    person = _person_for_slug(conn, person_slug)

    scan = _study.scan_study_dir(src)
    if not scan.rel_paths:
        raise IngestError(
            f"no DICOM files found under {src} - nothing to ingest. (Files are "
            "recognised by the DICM magic at byte 128, not by extension.)"
        )
    print(
        f"study: {scan.file_count} DICOM files, "
        f"{_study.human_bytes(scan.total_bytes)} to pack",
        file=sys.stderr,
    )
    if scan.excluded_documents:
        print(
            "note: not packed (a report is its own document - ingest it separately "
            "through the normal file path): "
            + ", ".join(scan.excluded_documents),
            file=sys.stderr,
        )
    if scan.total_bytes > _study.MAX_STUDY_BYTES and not allow_large:
        raise IngestError(
            f"study is {_study.human_bytes(scan.total_bytes)}, over the "
            f"{_study.human_bytes(_study.MAX_STUDY_BYTES)} limit; sources_dir is "
            "cloud-synced by default (config.example.toml) - re-run with "
            "--allow-large to ingest it anyway"
        )

    metadata = _study.read_metadata(scan)
    # Same guard as the file path: zero-width-only text is empty, so the generated
    # study summary wins rather than an invisible character (issue #87).
    supplied = normalize_document_text(ocr_text) or None
    text = supplied or _study.summary_text(scan, metadata)

    roster = _roster(conn)
    owner_check = _strongest_check([
        check_owner(candidate, person, roster)
        for candidate in (_study.identity_text(metadata), supplied)
        if candidate
    ])
    if owner_check.blocks and not force:
        # Pre-write *and* pre-pack: a refusal costs nothing and leaves nothing.
        raise OwnerMismatchError(
            refusal_message(owner_check, person, roster), owner_check
        )

    tmp_dir = Path(sources_dir) / TMP_DIRNAME
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_blob = tmp_dir / f"{uuid.uuid4().hex}{STUDY_EXT}"
    try:
        sha = _study.pack_study(scan, tmp_blob)

        existing = get_document(conn, sha)
        if existing is not None:
            # Known cost of hashing the archive rather than a manifest: an already
            # filed disc is repacked before we can know it is a duplicate. Nothing
            # is published and no row is inserted, so the semantics match the file
            # path exactly - only the work is wasted.
            return IngestResult(status="duplicate", document=existing)

        # Same removal-memory check as the file path (issue #80) — a study's blob is a
        # content-addressed archive like any other, so a tombstoned disc must not
        # silently re-ingest either.
        tombstone = _tombstones.get_tombstone(conn, sha)
        if tombstone is not None and not (force and tombstone_force):
            return IngestResult(status="tombstoned", tombstone=tombstone)

        dest = blob_dest(sources_dir, sha, STUDY_EXT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        blob_created = not dest.exists()
        if blob_created:
            os.replace(tmp_blob, dest)
        try:
            document = _insert_document(
                conn,
                sha=sha,
                ext=STUDY_EXT,
                person_id=person.person_id,
                doc_date=doc_date or metadata.study_date,
                category=category or _STUDY_CATEGORY,
                provider=provider,
                ocr_text=text,
            )
        except Exception:
            if blob_created:
                dest.unlink(missing_ok=True)
            raise
    finally:
        tmp_blob.unlink(missing_ok=True)

    return IngestResult(
        status="new", document=document, owner_check=owner_check, tombstone=tombstone
    )


# --------------------------------------------------------------------------- #
# Re-extraction against an already-stored blob (issue #143)
# --------------------------------------------------------------------------- #
#
# `ingest` writes `ocr_text` once, from whatever the extractor could read that day.
# Every later extractor fix (#70's PDF route, #138's CCDA route) therefore only helps
# documents ingested *after* it landed; the ones already on file keep the empty column
# they got. `document set-text` cannot close that gap — it takes text derived
# out-of-band, which is a transcription path, not a re-run.
#
# `reocr_documents` is the missing verb's engine: a selector and a policy layer over
# two functions that already exist. It calls `extract_text_routed` (the one `ingest`
# calls, not a copy) and writes through `set_document_text` (the one `set-text` calls),
# so the re-run cannot drift from the ingest path and cannot bypass the overwrite guard.


@dataclass(frozen=True)
class ReocrResult:
    """What a re-extraction did — or refused to do — for one document.

    Same shape and spirit as :class:`IngestResult`: one frozen row per document, so a
    262-document sweep is a list a caller can print, count and serialise without a
    second pass over the database.

    ``status``:

    * ``written`` — the recovered text is stored.
    * ``would-write`` — ``dry_run``; ``chars`` is what *would* have been stored.
    * ``has-text`` — the column already holds visible text and ``force`` was off.
      Refused **before** extraction, so the skip is cheap.
    * ``no-text`` — extraction ran and recovered nothing. A warning, not an error.
    * ``owner-mismatch`` — the recovered text affirmatively names a *different* roster
      person (issue #61's check, re-run against text that did not exist at ingest).
    * ``missing-blob`` — ``source_path`` does not resolve under ``sources_dir``.
    * ``study-blob`` — a packed DICOM study, whose ``ocr_text`` is a derived header
      summary rather than extracted text (see :func:`ingest_study_dir`).
    """

    document_id: int
    status: str
    chars: int = 0
    previous_chars: int = 0
    route: str | None = None
    pages: int | None = None
    truncated: bool = False
    owner_check: OwnerCheck | None = None
    blob_path: str | None = None

    @property
    def wrote(self) -> bool:
        return self.status == "written"

    @property
    def refused(self) -> bool:
        """Whether the caller asked for work that was declined (drives the exit code).

        ``no-text`` is deliberately excluded: a document that genuinely has no readable
        text is an expected outcome of a sweep, and a sweep that exits non-zero on
        expected outcomes trains `|| true` — the same reasoning as
        :attr:`IngestResult.is_tombstoned`.
        """
        return self.status in ("has-text", "owner-mismatch", "missing-blob",
                               "study-blob")


def reocr_documents(
    conn: sqlite3.Connection,
    document_ids: Sequence[int],
    sources_dir: str | Path,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> list[ReocrResult]:
    """Re-derive ``ocr_text`` for each document from its stored blob (`document reocr`).

    Every id is resolved **before** any extraction runs: an unknown id is a typo, and a
    sweep must not half-run on one. After that, each document is independent — there is
    deliberately **no transaction across the sweep** (each write is
    :func:`pemr.documents.set_document_text`'s own), so an interrupted 262-document run
    leaves the finished documents committed and is safely re-runnable.

    ``force`` means both "replace existing ``ocr_text``" and "store despite an owner
    mismatch", matching what ``--force`` already means on `ingest`. ``dry_run`` reports
    what would be stored and writes nothing.

    Raises :class:`IngestError` for an unknown document id.
    """
    db.require_migrated(conn)
    root = Path(sources_dir)

    rows: list[Document] = []
    for document_id in document_ids:
        row = get_document_by_id(conn, document_id)
        if row is None:
            raise IngestError(
                f"no document with id {document_id} - see `pemr document list`"
            )
        rows.append(row)

    roster = _roster(conn)
    results: list[ReocrResult] = []
    for row in rows:
        previous = normalize_document_text(row.ocr_text)
        common = {"document_id": row.document_id, "previous_chars": len(previous)}

        if row.source_path.endswith(STUDY_EXT):
            # A study's `ocr_text` is a DICOM-header summary built from the unpacked
            # slices, not text extracted from the blob. Re-deriving it is a different
            # pipeline (see `ingest_study_dir`), so this verb reports and skips.
            results.append(ReocrResult(status="study-blob", **common))
            continue

        # Before extraction, not after: the skip has to be cheap, or a sweep across a
        # mostly-populated corpus pays for OCR it then discards. Same normalised
        # predicate `set_document_text` uses, so an invisible-characters-only row
        # (issue #87) correctly counts as empty and gets repaired.
        if previous and not force:
            results.append(ReocrResult(status="has-text", **common))
            continue

        blob = root / row.source_path
        # Recorded even when it does not resolve — a `missing-blob` result is only
        # actionable if it names the path that was looked for.
        common["blob_path"] = str(blob)
        if not blob.is_file():
            results.append(ReocrResult(status="missing-blob", **common))
            continue

        text, route = extract_text_routed(blob)
        # What `set_document_text` would actually store, so `--dry-run`'s character
        # count is the number the write reports and not one character more.
        text = normalize_document_text(text)
        pages = pdf_page_count(blob)
        common.update(
            route=route, pages=pages,
            truncated=pages is not None and pages > OCR_MAX_PAGES,
        )
        if not text:
            results.append(ReocrResult(status="no-text", **common))
            continue

        person = _person_for_id(conn, row.person_id)
        owner_check = None
        if person is not None:
            owner_check = check_owner(
                text, person, roster, trust_anchors=(route != "native")
            )
        common["owner_check"] = owner_check
        # Only `mismatch` refuses here, unlike ingest where `suspect` blocks too: this
        # document is *already filed* under that owner, so withholding the text does
        # not un-file it — it only hides the evidence and keeps the document invisible
        # to `find`, which is the bug this verb exists to close. `mismatch` is
        # affirmative evidence of a cross-owner leak, so it still earns the refusal.
        if owner_check is not None and owner_check.verdict == "mismatch" and not force:
            results.append(ReocrResult(status="owner-mismatch", **common))
            continue

        if dry_run:
            results.append(
                ReocrResult(status="would-write", chars=len(text), **common)
            )
            continue

        # `force=True` unconditionally: this call is only reached once the `has-text`
        # guard above has already applied the caller's own force policy, and re-testing
        # it here would refuse the invisible-only rows that guard deliberately admits.
        set_document_text(conn, row.document_id, text, force=True)
        results.append(ReocrResult(status="written", chars=len(text), **common))
    return results

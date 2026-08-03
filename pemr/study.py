"""Study-directory packing — a burned imaging disc as **one** document (issue #69).

A DICOM study is clinically one document (one modality, one study date, one report)
but physically a folder of ~2,000 extension-less slice files plus a Windows viewer
payload. The engine stores one `document` row per *file*, content-addressed by the
sha256 of its bytes (`Architecture.md` §1/§4), so a folder needs a single
byte-stream to stand in for it.

This module produces that byte-stream: a **canonical zip** of the study's DICOM
files. `document.sha256` is the sha256 of the archive's own bytes, so
`sources/<sha[:2]>/<sha>.dcm.zip` still means "this file's content hash is its
name", layer-1 dedup needs no special case, and `pemr verify` re-hashes the blob
exactly as it does for any other document.

Two properties carry the design:

* **Inclusion is content-based.** A file is in iff its bytes 128..132 are ``DICM``
  (the DICOM Part 10 preamble + magic). That admits ``DICOMDIR`` and the
  ``IMxxxxxx`` slices and excludes the viewer payload (`.exe`/`.dll`/HTML/
  `autorun.inf`) *by construction* — no extension denylist to keep in sync, and no
  executable retained in the archive.
* **The archive is deterministic.** Same disc, another machine, a later crawl → the
  same bytes → the same sha, or layer-1 dedup silently stops working. Every zip
  field that would otherwise vary (entry order, compression, timestamps, host
  system, file mode) is pinned below.

Metadata is read with a **minimal stdlib parser** for seven tags — five descriptive
ones that seed the derived summary, plus patient name and birth date, which are
verification input for the issue-#61 owner check and are never stored. Every length
the file declares is bounded before it is used: a scratched disc's corrupt length
field is otherwise an unbounded allocation, and an untrusted element length is
otherwise text spliced straight into `document.ocr_text`. The project has zero
runtime dependencies (`pyproject.toml`) and `pydicom` would be the first, so it is
refused rather than deferred; the reader is best-effort in exactly the way
:func:`pemr.ingest.run_ocr` is — any parse failure yields no metadata and never
fails the ingest.
"""

from __future__ import annotations

import hashlib
import shutil
import struct
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

__all__ = [
    "STUDY_KINDS",
    "MAX_STUDY_BYTES",
    "Series",
    "StudyMetadata",
    "StudyScan",
    "human_bytes",
    "identity_text",
    "is_dicom_file",
    "pack_study",
    "read_metadata",
    "scan_study_dir",
    "summary_text",
]

#: Study kinds this module can pack. A value (rather than a boolean flag) so a
#: future kind is an enum entry, not a second flag.
STUDY_KINDS = ("dicom",)

#: Refuse studies larger than this without an explicit opt-in. `sources_dir` is
#: cloud-synced (`config.example.toml`), so "is GB-per-disc acceptable?" is answered
#: per disc, by the human, at run time — not frozen into the design.
MAX_STUDY_BYTES = 4 * 1024**3  # 4 GiB

_READ_CHUNK = 1 << 20  # 1 MiB

_SIZE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")


def human_bytes(size: float) -> str:
    """Byte count in the largest unit that keeps it readable.

    The study messages exist so a human can make a storage call at run time, and
    "0.0 MiB" or "4831838208 bytes" both fail at that.
    """
    for unit in _SIZE_UNITS[:-1]:
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} {_SIZE_UNITS[-1]}"

# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #

_DICM_OFFSET = 128  # Part 10: 128-byte preamble, then the 4-byte "DICM" magic
_DICM_MAGIC = b"DICM"

#: Non-DICOM files worth *naming* when we drop them: a burned disc usually carries
#: the radiology report as a PDF/RTF beside the slices, and silently losing it would
#: be the worst outcome of this change. Reported, never packed — a report is its own
#: document, ingested through the normal file path.
_DOCUMENT_LIKE = frozenset({".pdf", ".rtf", ".doc", ".docx", ".txt", ".html", ".htm"})

#: DICOMDIR is a directory-record file, not a slice: it carries none of the study
#: tags below, so it is packed but never used as a metadata source.
_DICOMDIR = "DICOMDIR"


@dataclass(frozen=True)
class StudyScan:
    """What a study directory contains, after the inclusion filter."""

    root: Path
    #: Relative POSIX paths of the included DICOM files, sorted — this *is* the
    #: canonical archive order, so it is computed once and reused.
    rel_paths: tuple[str, ...]
    total_bytes: int
    #: Relative POSIX paths of dropped files that look like documents (see
    #: :data:`_DOCUMENT_LIKE`), for the caller to report.
    excluded_documents: tuple[str, ...]
    #: Count of everything else that was dropped (viewer payload, autorun, …).
    excluded_other: int

    @property
    def file_count(self) -> int:
        return len(self.rel_paths)

    def abs_path(self, rel: str) -> Path:
        return self.root / rel


def is_dicom_file(path: str | Path) -> bool:
    """Whether ``path`` carries the DICOM Part 10 preamble + ``DICM`` magic.

    Unreadable / too-short files are simply "not DICOM" — a scan walks whatever the
    disc happens to hold and must not die on a locked or truncated file.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(_DICM_OFFSET)
            return fh.read(4) == _DICM_MAGIC
    except OSError:
        return False


def scan_study_dir(root: str | Path) -> StudyScan:
    """Walk ``root`` recursively and split it into packed / dropped files."""
    root = Path(root)
    included: list[tuple[str, int]] = []
    documents: list[str] = []
    other = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if is_dicom_file(path):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            included.append((rel, size))
        elif path.suffix.lower() in _DOCUMENT_LIKE:
            documents.append(rel)
        else:
            other += 1
    # Bytewise on the POSIX relative path. Python's str ordering is by code point,
    # which UTF-8 preserves, so this is the same order on every platform.
    included.sort(key=lambda item: item[0])
    documents.sort()
    return StudyScan(
        root=root,
        rel_paths=tuple(rel for rel, _ in included),
        total_bytes=sum(size for _, size in included),
        excluded_documents=tuple(documents),
        excluded_other=other,
    )


# --------------------------------------------------------------------------- #
# Canonical archive
# --------------------------------------------------------------------------- #

# Every zip field that varies by host, clock, or library version, pinned. Deflate is
# excluded too: its output depends on the zlib version and level, and it buys almost
# nothing on already-compressed pixel data.
_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)  # the zip epoch; the lowest legal value
_ZIP_CREATE_SYSTEM = 0  # zipfile defaults this to 0 on Windows, 3 elsewhere
_ZIP_CREATE_VERSION = 20
_ZIP_EXTRACT_VERSION = 20
# Pinned to what `ZipFile.open(..., "w")` substitutes for a zero value (`0o600 << 16`),
# so the archive does not silently depend on that fallback staying put — and never to
# the *source* file's mode, which varies with how the disc was copied.
_ZIP_EXTERNAL_ATTR = 0o600 << 16


def pack_study(scan: StudyScan, out_path: str | Path) -> str:
    """Write ``scan``'s files to ``out_path`` as a canonical zip; return its sha256.

    Entries are written through :meth:`zipfile.ZipFile.open` from a hand-built
    :class:`zipfile.ZipInfo` — deliberately *not* :meth:`ZipFile.write`, which reads
    the source file's mtime and mode into the archive and would make the hash depend
    on how the disc happened to be copied.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", allowZip64=True) as zf:
        for rel in scan.rel_paths:
            info = zipfile.ZipInfo(filename=rel, date_time=_ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = _ZIP_CREATE_SYSTEM
            info.create_version = _ZIP_CREATE_VERSION
            info.extract_version = _ZIP_EXTRACT_VERSION
            info.external_attr = _ZIP_EXTERNAL_ATTR
            info.internal_attr = 0
            info.comment = b""
            info.extra = b""
            with open(scan.abs_path(rel), "rb") as src, zf.open(info, "w") as dst:
                shutil.copyfileobj(src, dst, _READ_CHUNK)
    digest = hashlib.sha256()
    with out_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_READ_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Minimal DICOM header reader (stdlib only — see the module docstring)
# --------------------------------------------------------------------------- #

_TAG_STUDY_DATE = (0x0008, 0x0020)
_TAG_MODALITY = (0x0008, 0x0060)
_TAG_STUDY_DESCRIPTION = (0x0008, 0x1030)
_TAG_SERIES_DESCRIPTION = (0x0008, 0x103E)
_TAG_PATIENT_NAME = (0x0010, 0x0010)
_TAG_PATIENT_BIRTH_DATE = (0x0010, 0x0030)
_TAG_STUDY_UID = (0x0020, 0x000D)
_TAG_TRANSFER_SYNTAX = (0x0002, 0x0010)

_WANTED = frozenset({
    _TAG_STUDY_DATE,
    _TAG_MODALITY,
    _TAG_STUDY_DESCRIPTION,
    _TAG_SERIES_DESCRIPTION,
    _TAG_PATIENT_NAME,
    _TAG_PATIENT_BIRTH_DATE,
    _TAG_STUDY_UID,
})

# Every wanted tag is a short-form string VR (DA/CS/LO/UI/PN), so no VR dictionary
# is needed to read them under Implicit VR — only the length encoding differs.
# Stop as soon as the element stream passes their groups: elements are in ascending
# tag order, and group 0xFFFE (item delimiters) trips the same guard.
_STOP_GROUP = 0x0021

# Explicit VR: these carry 2 reserved bytes then a 4-byte length; everything else
# uses a 2-byte length.
_LONG_FORM_VR = frozenset({
    b"OB", b"OD", b"OF", b"OL", b"OV", b"OW", b"SQ", b"SV",
    b"UC", b"UN", b"UR", b"UT", b"UV",
})

_IMPLICIT_VR_LE = "1.2.840.10008.1.2"
_EXPLICIT_VR_LE = "1.2.840.10008.1.2.1"
# The encapsulated-pixel-data syntaxes (JPEG, JPEG2000, RLE, …) all encode the
# *dataset* as Explicit VR LE — only the pixel data is compressed, and we never read
# it. Deflated (…1.2.1.99) and Explicit VR Big Endian (…1.2.2) are not supported and
# fall through to "no metadata", which is a soft failure by design.
_ENCAPSULATED_PREFIX = "1.2.840.10008.1.2.4."

#: How much of the dataset to read looking for the five tags. They live in groups
#: 0008/0020, long before pixel data; a header this large means something exotic, and
#: bailing out beats reading a gigabyte per slice.
_MAX_HEADER_SCAN = 1 << 20  # 1 MiB

#: Hard cap on a single tag value. The length prefix in the file is untrusted, and
#: every value we read has a VR maximum far below this (``DA`` 8, ``CS`` 16, ``LO``
#: 64, ``UI`` 64, ``PN`` 64 per component) — so a longer one is corruption or hostile
#: input, not data. Without the cap a file could declare a ~1 MiB
#: ``StudyDescription`` and have it spliced verbatim into `document.ocr_text`, i.e.
#: into the FTS index and into an agent's context as if it were record text.
_MAX_TAG_CHARS = 128


def _clean(raw: bytes) -> str | None:
    """DICOM string value → trimmed text (capped), or None when it carries nothing.

    Truncation happens *before* the decode, which latin-1 makes exact: it is a
    1-byte-per-character codec, so a byte cap and a character cap are the same cap.
    """
    try:
        text = raw[:_MAX_TAG_CHARS].decode("latin-1")
    except (UnicodeDecodeError, AttributeError):  # pragma: no cover - latin-1 total
        return None
    text = text.replace("\x00", "").strip()
    return text or None


def _explicit_vr_le(transfer_syntax: str | None) -> bool | None:
    """True → Explicit VR LE, False → Implicit VR LE, None → unsupported/unknown."""
    if transfer_syntax is None:
        return None
    if transfer_syntax == _IMPLICIT_VR_LE:
        return False
    if transfer_syntax == _EXPLICIT_VR_LE:
        return True
    if transfer_syntax.startswith(_ENCAPSULATED_PREFIX):
        return True
    return None


def _parse_elements(
    buf: bytes, *, explicit: bool, wanted: frozenset[tuple[int, int]]
) -> dict[tuple[int, int], str | None]:
    """Best-effort scan of a little-endian DICOM element stream for ``wanted`` tags.

    Never raises: any structure this parser does not understand (an undefined-length
    sequence, a truncated element, a value running past the buffer) ends the scan and
    yields whatever was found so far.
    """
    found: dict[tuple[int, int], str | None] = {}
    pos = 0
    end = len(buf)
    while pos + 8 <= end:
        group, element = struct.unpack_from("<HH", buf, pos)
        if group >= _STOP_GROUP:
            break
        pos += 4
        if explicit:
            vr = buf[pos:pos + 2]
            pos += 2
            if vr in _LONG_FORM_VR:
                if pos + 6 > end:
                    break
                (length,) = struct.unpack_from("<I", buf, pos + 2)
                pos += 6
            else:
                if pos + 2 > end:
                    break
                (length,) = struct.unpack_from("<H", buf, pos)
                pos += 2
        else:
            if pos + 4 > end:
                break
            (length,) = struct.unpack_from("<I", buf, pos)
            pos += 4
        if length == 0xFFFFFFFF:  # undefined-length sequence: stop rather than guess
            break
        if pos + length > end:
            break
        tag = (group, element)
        if tag in wanted:
            found[tag] = _clean(buf[pos:pos + length])
            if len(found) == len(wanted):
                break
        pos += length
    return found


def _read_tags(path: Path) -> dict[tuple[int, int], str | None]:
    """Read the wanted tags from one DICOM file. Empty dict on any problem."""
    try:
        with open(path, "rb") as fh:
            if fh.read(_DICM_OFFSET + 4)[_DICM_OFFSET:] != _DICM_MAGIC:
                return {}
            # File-meta group (0002) is always Explicit VR LE and always opens with
            # (0002,0000) UL group-length, which tells us where the dataset starts.
            head = fh.read(12)
            if len(head) < 12:
                return {}
            group, element, vr, short_len = struct.unpack_from("<HH2sH", head, 0)
            if (group, element) != (0x0002, 0x0000) or vr != b"UL" or short_len != 4:
                return {}
            (meta_length,) = struct.unpack_from("<I", head, 8)
            # Untrusted 32-bit length. `read(n)` allocates `n` up front and only
            # then shrinks, so a corrupt group length — four wrong bytes, optical
            # media's ordinary failure mode — would commit up to 4 GiB per slice
            # read, and the resulting `MemoryError` is not an `OSError`, so it
            # would escape this function and abort an ingest this module promises
            # never to fail. Bounded by the same cap as the dataset scan.
            if meta_length > _MAX_HEADER_SCAN:
                return {}
            meta = fh.read(meta_length)
            syntax = _parse_elements(
                meta, explicit=True, wanted=frozenset({_TAG_TRANSFER_SYNTAX})
            ).get(_TAG_TRANSFER_SYNTAX)
            explicit = _explicit_vr_le(syntax)
            if explicit is None:
                return {}
            return _parse_elements(
                fh.read(_MAX_HEADER_SCAN), explicit=explicit, wanted=_WANTED
            )
    except (OSError, struct.error, MemoryError):
        # `MemoryError` is deliberate and is *not* an `OSError`: the length bound
        # above is the fix, this is the backstop that keeps the "never fails the
        # ingest" contract true even if some other allocation path is found.
        return {}


def _iso_date(da: str | None) -> str | None:
    """DICOM ``DA`` (``YYYYMMDD``) → ISO ``YYYY-MM-DD``, or None if unparseable."""
    if not da or len(da) < 8:
        return None
    try:
        return datetime.strptime(da[:8], "%Y%m%d").date().isoformat()
    except ValueError:
        return None


@dataclass(frozen=True)
class Series:
    """One series of the study — in practice, one subdirectory of slices."""

    directory: str  # relative POSIX dir, "" for the study root
    description: str | None
    slice_count: int


@dataclass(frozen=True)
class StudyMetadata:
    modality: str | None = None
    study_date: str | None = None  # ISO YYYY-MM-DD
    study_description: str | None = None
    study_uid: str | None = None
    series: tuple[Series, ...] = ()
    #: Patient identity, read purely so the issue-#61 owner check has something to
    #: verify a study against (see :func:`identity_text`). **Verification input, not
    #: stored text** — :func:`summary_text` never emits these, so they do not reach
    #: `document.ocr_text`, the FTS index, or an agent's context.
    patient_name: str | None = None   # raw DICOM PN, e.g. "DOE^JANE^"
    patient_birth_date: str | None = None  # ISO YYYY-MM-DD


def _metadata_sources(scan: StudyScan) -> list[str]:
    """Included files usable as a metadata source, i.e. slices (not ``DICOMDIR``)."""
    return [
        rel for rel in scan.rel_paths
        if Path(rel).name.upper() != _DICOMDIR
    ]


def read_metadata(scan: StudyScan) -> StudyMetadata:
    """Study-level tags from the first slice + a per-series description pass.

    Best-effort throughout: an unreadable, truncated, or unsupported-transfer-syntax
    slice contributes nothing and never fails the ingest.
    """
    sources = _metadata_sources(scan)
    if not sources:
        return StudyMetadata()

    head = _read_tags(scan.abs_path(sources[0]))

    # A "series" is a slice directory. Flat discs land in one series keyed "".
    counts: dict[str, int] = {}
    first_slice: dict[str, str] = {}
    for rel in sources:
        directory = Path(rel).parent.as_posix()
        directory = "" if directory == "." else directory
        counts[directory] = counts.get(directory, 0) + 1
        first_slice.setdefault(directory, rel)

    series = []
    for directory in sorted(counts):
        rel = first_slice[directory]
        tags = head if rel == sources[0] else _read_tags(scan.abs_path(rel))
        series.append(Series(
            directory=directory,
            description=tags.get(_TAG_SERIES_DESCRIPTION),
            slice_count=counts[directory],
        ))

    return StudyMetadata(
        modality=head.get(_TAG_MODALITY),
        study_date=_iso_date(head.get(_TAG_STUDY_DATE)),
        study_description=head.get(_TAG_STUDY_DESCRIPTION),
        study_uid=head.get(_TAG_STUDY_UID),
        series=tuple(series),
        patient_name=head.get(_TAG_PATIENT_NAME),
        patient_birth_date=_iso_date(head.get(_TAG_PATIENT_BIRTH_DATE)),
    )


def identity_text(metadata: StudyMetadata) -> str | None:
    """Patient identity from the study header, for :func:`pemr.ingest.check_owner`.

    A 2,000-slice binary folder is the one document type a human cannot eyeball, so
    `pemr ingest D:\\DICOM --person jane-doe` on the *spouse's* disc is the realistic
    misfile — and without this the study path would be the only ingest door in the
    system with no owner verification at all (issue #69 audit, finding B3).

    The output is shaped for the existing check rather than a new one: the
    ``Patient:``/``DOB:`` labels are what :func:`pemr.ingest.check_owner` recognises
    as an identity anchor, ``normalize_text`` already reduces DICOM's ``DOE^JANE^``
    to ``doe jane``, and the DOB is emitted in the ISO form ``dob_candidates``
    renders. Returns None when the header carries neither tag — no signal, so the
    verdict stays ``unverified`` rather than becoming a spurious refusal.

    This is *more* reliable than the file path's input, not less: a structured tag
    beats fuzzy OCR, so the false-``suspect`` worry that shaped ``check_owner``
    barely applies here.
    """
    lines = []
    if metadata.patient_name:
        lines.append(f"Patient: {metadata.patient_name}")
    if metadata.patient_birth_date:
        lines.append(f"DOB: {metadata.patient_birth_date}")
    return "\n".join(lines) or None


#: Cap on the per-series lines in the summary: enough to characterise a study,
#: short of turning `ocr_text` into a file listing that pollutes FTS.
_MAX_SERIES_LINES = 20


def summary_text(scan: StudyScan, metadata: StudyMetadata) -> str:
    """A short derived description of the study, for ``document.ocr_text``.

    Deliberately *not* the 2,000-line file list: the point is that the study shows up
    in `find` as something recognisable instead of an untitled row. A caller-supplied
    transcription of the accompanying radiology report always wins over this.
    """
    lines = [f"DICOM study: {scan.root.name or scan.root.as_posix()}"]
    if metadata.modality:
        lines.append(f"modality: {metadata.modality}")
    if metadata.study_date:
        lines.append(f"study date: {metadata.study_date}")
    if metadata.study_description:
        lines.append(f"study description: {metadata.study_description}")
    slices = sum(s.slice_count for s in metadata.series)
    if slices:
        suffix = (
            f" in {len(metadata.series)} series" if len(metadata.series) > 1 else ""
        )
        lines.append(f"slices: {slices}{suffix}")
    # One unnamed series (a flat disc) is fully described by the slice count above.
    if len(metadata.series) > 1 or any(s.description for s in metadata.series):
        lines.append("series:")
        for item in metadata.series[:_MAX_SERIES_LINES]:
            label = item.description or item.directory or "(unnamed)"
            lines.append(f"  - {label} - {item.slice_count} slices")
        remaining = len(metadata.series) - _MAX_SERIES_LINES
        if remaining > 0:
            lines.append(f"  - ... and {remaining} more series")
    if metadata.study_uid:
        lines.append(f"study UID: {metadata.study_uid}")
    lines.append(f"files packed: {scan.file_count}")
    return "\n".join(lines)

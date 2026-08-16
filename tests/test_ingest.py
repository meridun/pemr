"""Document ingest: hashing, content-addressed blob store, layer-1 dedup."""

import json
import sqlite3
import time
import zipfile
import subprocess

import pytest

from pemr import db, ingest, persons, query
from pemr.models import Person


@pytest.fixture()
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    persons.add_person(conn, "jane-doe", "Jane Doe")
    yield conn
    conn.close()


@pytest.fixture()
def sources(tmp_path):
    return tmp_path / "sources"


def _make_file(tmp_path, name="scan.txt", content=b"lab report bytes"):
    p = tmp_path / name
    p.write_bytes(content)
    return p


def test_hash_file_is_deterministic(tmp_path):
    a = _make_file(tmp_path, "a.txt", b"same bytes")
    b = _make_file(tmp_path, "b.txt", b"same bytes")
    assert ingest.hash_file(a) == ingest.hash_file(b)
    assert ingest.hash_file(a) != ingest.hash_file(
        _make_file(tmp_path, "c.txt", b"other")
    )


def test_ingest_new_document_stores_blob_and_row(conn, tmp_path, sources):
    src = _make_file(tmp_path)
    result = ingest.ingest_document(conn, src, "jane-doe", sources)

    assert result.status == "new"
    doc = result.document
    sha = ingest.hash_file(src)
    # content-addressed blob copied under sources/<sha[:2]>/<sha>.txt
    dest = sources / sha[:2] / f"{sha}.txt"
    assert dest.is_file()
    # stored source_path is portable (relative to the sources root)
    assert doc.source_path == f"{sha[:2]}/{sha}.txt"
    assert doc.sha256 == sha
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM document"
    ).fetchone()
    assert row["n"] == 1


def test_ingest_duplicate_content_stops(conn, tmp_path, sources):
    src = _make_file(tmp_path, "scan1.txt", b"identical")
    first = ingest.ingest_document(conn, src, "jane-doe", sources)
    # a re-scan: different filename, identical bytes
    again = _make_file(tmp_path, "scan2.txt", b"identical")
    second = ingest.ingest_document(conn, again, "jane-doe", sources)

    assert first.status == "new"
    assert second.status == "duplicate"
    assert second.document.document_id == first.document.document_id
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 1


def test_ingest_unknown_person_raises(conn, tmp_path, sources):
    with pytest.raises(ingest.IngestError, match="no person"):
        ingest.ingest_document(conn, _make_file(tmp_path), "nobody", sources)


def test_ingest_missing_file_raises(conn, tmp_path, sources):
    with pytest.raises(ingest.IngestError, match="file not found"):
        ingest.ingest_document(conn, tmp_path / "nope.pdf", "jane-doe", sources)


def test_ingest_on_unmigrated_db_raises(tmp_path, sources):
    fresh = db.connect(tmp_path / "empty.db")
    try:
        with pytest.raises(db.NotMigratedError):
            ingest.ingest_document(fresh, _make_file(tmp_path), "jane-doe", sources)
    finally:
        fresh.close()


def test_failed_insert_does_not_orphan_blob(conn, tmp_path, sources):
    # carry-forward advisory from #4: if the `document` INSERT fails, the blob this
    # call copied into sources/ must be cleaned up, not left orphaned. A BEFORE INSERT
    # trigger forces the insert to abort *after* the blob has been staged.
    src = _make_file(tmp_path, "scan.txt", b"orphan check bytes")
    sha = ingest.hash_file(src)
    conn.execute(
        "CREATE TRIGGER boom BEFORE INSERT ON document "
        "BEGIN SELECT RAISE(ABORT, 'insert exploded'); END"
    )
    conn.commit()

    with pytest.raises(sqlite3.Error, match="insert exploded"):
        ingest.ingest_document(conn, src, "jane-doe", sources)
    assert not (sources / sha[:2] / f"{sha}.txt").exists()  # blob cleaned up


def test_ocr_degrades_when_tesseract_absent(conn, tmp_path, sources, monkeypatch, capsys):
    monkeypatch.setattr(ingest.shutil, "which", lambda _: None)
    # a scan (image suffix) is the tesseract branch of ingest.extract_text; text and
    # OOXML suffixes are read natively and never reach the binary.
    scan = _make_file(tmp_path, "scan.png", b"\x89PNG not really")
    result = ingest.ingest_document(conn, scan, "jane-doe", sources, ocr=True)
    assert result.status == "new"
    assert result.document.ocr_text is None
    assert "tesseract" in capsys.readouterr().err


# --- PDF OCR (issue #70) ------------------------------------------------------
#
# Every test here fakes both halves of the soft dependency — the PDF backend via the
# `_load_pdf_backend` seam and tesseract via `subprocess.run` — so the suite passes on
# a machine with neither PyMuPDF nor tesseract installed, which is the CI contract.


class _FakePixmap:
    def __init__(self, payload: str) -> None:
        self.payload = payload

    def tobytes(self, fmt: str) -> bytes:
        assert fmt == "png"
        # Carries the page's OCR text through the fake tesseract below, standing in
        # for "these pixels say this".
        return f"PNG:{self.payload}".encode()


class _FakePage:
    """A PDF page: `text` is its embedded text layer, `scanned` what OCR would read."""

    def __init__(self, text: str = "", scanned: str = "") -> None:
        self.text = text
        self.scanned = scanned
        self.pixmap_kwargs: dict | None = None

    def get_text(self) -> str:
        return self.text

    def get_pixmap(self, **kwargs):
        self.pixmap_kwargs = kwargs
        return _FakePixmap(self.scanned)


class _FakeDoc:
    def __init__(self, pages, needs_pass: bool = False) -> None:
        self.pages = list(pages)
        self.needs_pass = needs_pass
        self.closed = False

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def __getitem__(self, index: int) -> _FakePage:
        return self.pages[index]

    def close(self) -> None:
        self.closed = True


class _FakeBackend:
    csGRAY = "device-gray"

    def __init__(self, doc=None, error: Exception | None = None) -> None:
        self.doc = doc
        self.error = error

    def open(self, path: str):
        if self.error is not None:
            raise self.error
        return self.doc


def _install_backend(monkeypatch, backend) -> None:
    monkeypatch.setattr(ingest, "_load_pdf_backend", lambda: backend)


def _install_tesseract(monkeypatch, *, available: bool = True, stderr: bytes = b""):
    """Fake tesseract that echoes back whatever `_FakePixmap` encoded (or fails)."""
    monkeypatch.setattr(
        ingest.shutil, "which", lambda name: "/usr/bin/tesseract" if available else None
    )
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        assert cmd == ["tesseract", "stdin", "stdout"]
        assert "text" not in kwargs  # bytes in, bytes out (issue #64's decode policy)
        if stderr:
            raise subprocess.CalledProcessError(1, cmd, output=b"", stderr=stderr)
        payload = kwargs["input"].decode().removeprefix("PNG:")
        return subprocess.CompletedProcess(cmd, 0, stdout=payload.encode(), stderr=b"")

    monkeypatch.setattr(ingest.subprocess, "run", fake_run)
    return calls


def _pdf(tmp_path, name="scan.pdf"):
    return _make_file(tmp_path, name, b"%PDF-1.4 fake bytes")


def test_ocr_pdf_uses_text_layer_without_touching_tesseract(tmp_path, monkeypatch):
    # A searchable PDF: text layer on every page, so nothing is rasterized and
    # tesseract is never consulted (`shutil.which` unpatched would still be fine —
    # `subprocess.run` raising is the assertion that it isn't called).
    pages = [_FakePage(text="Patient: Jane Doe, HbA1c 6.1"), _FakePage(text="B" * 40)]
    _install_backend(monkeypatch, _FakeBackend(_FakeDoc(pages)))
    monkeypatch.setattr(
        ingest.subprocess, "run",
        lambda *a, **k: pytest.fail("tesseract ran on a page with a text layer"),
    )

    text = ingest.run_ocr(_pdf(tmp_path))

    assert text == "Patient: Jane Doe, HbA1c 6.1\f" + "B" * 40
    assert all(page.pixmap_kwargs is None for page in pages)


def test_ocr_pdf_rasterizes_and_ocrs_pages_with_no_text_layer(tmp_path, monkeypatch):
    # The reported bug: a scanned PDF has no text layer at all, and used to store
    # nothing because tesseract cannot decode a PDF.
    pages = [_FakePage(scanned="page one scan"), _FakePage(scanned="page two scan")]
    _install_backend(monkeypatch, _FakeBackend(_FakeDoc(pages)))
    _install_tesseract(monkeypatch)

    assert ingest.run_ocr(_pdf(tmp_path)) == "page one scan\fpage two scan"
    for page in pages:
        assert page.pixmap_kwargs == {
            "dpi": ingest.OCR_DPI, "colorspace": _FakeBackend.csGRAY
        }
    assert ingest.OCR_DPI == 300


def test_ocr_pdf_decides_per_page_not_per_document(tmp_path, monkeypatch):
    # Mixed document: a searchable cover page, then a scan carrying a stray stamp
    # character. The stray character must not suppress OCR of the whole page.
    pages = [
        _FakePage(text="Discharge summary for the visit of 2024-03-02"),
        _FakePage(text="  X ", scanned="the labs table"),
    ]
    _install_backend(monkeypatch, _FakeBackend(_FakeDoc(pages)))
    _install_tesseract(monkeypatch)

    text = ingest.run_ocr(_pdf(tmp_path))

    assert text == "Discharge summary for the visit of 2024-03-02\fthe labs table"
    assert pages[0].pixmap_kwargs is None
    assert pages[1].pixmap_kwargs is not None


def test_ocr_pdf_falls_back_to_short_text_layer_when_ocr_finds_nothing(
    tmp_path, monkeypatch
):
    # A page under the floor whose pixels yield nothing keeps its stray characters
    # rather than dropping them — some text beats none.
    pages = [_FakePage(text="Rx", scanned="")]
    _install_backend(monkeypatch, _FakeBackend(_FakeDoc(pages)))
    _install_tesseract(monkeypatch)

    assert ingest.run_ocr(_pdf(tmp_path)) == "Rx"


def test_ocr_pdf_caps_long_documents_and_says_so(tmp_path, monkeypatch, capsys):
    pages = [_FakePage(text=f"page {n} " + "z" * 30) for n in range(25)]
    _install_backend(monkeypatch, _FakeBackend(_FakeDoc(pages)))

    text = ingest.run_ocr(_pdf(tmp_path))

    assert len(text.split("\f")) == ingest.OCR_MAX_PAGES == 20
    err = capsys.readouterr().err
    assert "25 pages" in err and "first 20" in err


def test_load_pdf_backend_prefers_pymupdf_and_tolerates_absence(monkeypatch):
    # The seam itself, exercised without caring whether PyMuPDF is installed here.
    tried: list[str] = []

    def only(available):
        def import_module(name):
            tried.append(name)
            if name != available:
                raise ImportError(name)
            return f"<{name}>"
        return import_module

    monkeypatch.setattr(ingest.importlib, "import_module", only("pymupdf"))
    assert ingest._load_pdf_backend() == "<pymupdf>"
    assert tried == ["pymupdf"]  # modern name first, no needless `fitz` import

    tried.clear()
    monkeypatch.setattr(ingest.importlib, "import_module", only("fitz"))
    assert ingest._load_pdf_backend() == "<fitz>"  # older wheels

    monkeypatch.setattr(ingest.importlib, "import_module", only(None))
    assert ingest._load_pdf_backend() is None  # extra not installed


def test_ocr_pdf_without_backend_names_the_extra(tmp_path, monkeypatch, capsys):
    _install_backend(monkeypatch, None)

    assert ingest.run_ocr(_pdf(tmp_path)) is None
    assert "pemr[ocr]" in capsys.readouterr().err


def test_ocr_pdf_without_tesseract_degrades(tmp_path, monkeypatch, capsys):
    _install_backend(monkeypatch, _FakeBackend(_FakeDoc([_FakePage(scanned="x")])))
    _install_tesseract(monkeypatch, available=False)

    assert ingest.run_ocr(_pdf(tmp_path)) is None
    assert "tesseract" in capsys.readouterr().err


def test_ocr_pdf_encrypted_returns_none(tmp_path, monkeypatch, capsys):
    doc = _FakeDoc([_FakePage(scanned="secret")], needs_pass=True)
    _install_backend(monkeypatch, _FakeBackend(doc))

    assert ingest.run_ocr(_pdf(tmp_path)) is None
    assert "password-protected" in capsys.readouterr().err
    assert doc.closed


def test_ocr_pdf_unparseable_returns_none(tmp_path, monkeypatch, capsys):
    _install_backend(monkeypatch, _FakeBackend(error=RuntimeError("Failed to open")))

    assert ingest.run_ocr(_pdf(tmp_path)) is None
    assert "could not read" in capsys.readouterr().err


def test_ocr_pdf_bad_page_skips_only_that_page(tmp_path, monkeypatch, capsys):
    class _Exploding(_FakePage):
        def get_text(self):
            raise RuntimeError("mupdf: cannot parse page")

    pages = [_Exploding(), _FakePage(text="the page that still works fine")]
    _install_backend(monkeypatch, _FakeBackend(_FakeDoc(pages)))

    assert ingest.run_ocr(_pdf(tmp_path)) == "the page that still works fine"
    assert "page 1" in capsys.readouterr().err


def test_ocr_pdf_surfaces_tesseract_stderr(tmp_path, monkeypatch, capsys):
    _install_backend(monkeypatch, _FakeBackend(_FakeDoc([_FakePage(scanned="x")])))
    _install_tesseract(monkeypatch, stderr=b"Error in pixReadStream: unsupported\n")

    assert ingest.run_ocr(_pdf(tmp_path)) is None
    # The line that would have made 501 silent failures self-diagnosing.
    assert "Error in pixReadStream: unsupported" in capsys.readouterr().err


def test_run_ocr_on_an_image_is_unchanged(tmp_path, monkeypatch):
    # Regression guard for the non-PDF path: same argv, same text-mode capture as
    # before the PDF dispatch existed.
    src = _make_file(tmp_path, "scan.jpg", b"jpeg bytes")
    monkeypatch.setattr(ingest.shutil, "which", lambda _: "/usr/bin/tesseract")
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen.update(cmd=cmd, **kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="image text\n", stderr="")

    monkeypatch.setattr(ingest.subprocess, "run", fake_run)

    assert ingest.run_ocr(src) == "image text"
    assert seen["cmd"] == ["tesseract", str(src), "stdout"]
    assert seen["text"] is True


def test_run_ocr_image_failure_note_carries_tesseract_stderr(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(ingest.shutil, "which", lambda _: "/usr/bin/tesseract")

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(
            1, cmd, output="", stderr="Warning: bad dpi\nError: unsupported format\n"
        )

    monkeypatch.setattr(ingest.subprocess, "run", fake_run)

    assert ingest.run_ocr(_make_file(tmp_path, "scan.jpg", b"jpeg")) is None
    assert "Error: unsupported format" in capsys.readouterr().err


def test_ingest_pdf_with_ocr_populates_ocr_text(conn, tmp_path, sources, monkeypatch):
    pages = [_FakePage(scanned="Jane Doe cholesterol panel")]
    _install_backend(monkeypatch, _FakeBackend(_FakeDoc(pages)))
    _install_tesseract(monkeypatch)

    result = ingest.ingest_document(
        conn, _pdf(tmp_path), "jane-doe", sources, ocr=True
    )

    assert result.status == "new"
    assert result.ocr_text_populated
    assert result.document.ocr_text == "Jane Doe cholesterol panel"
# --- OCR output decoding (issue #64) ------------------------------------------
#
# Tesseract emits UTF-8. `subprocess.run(..., text=True)` with no explicit
# encoding decodes the pipe with the *platform default* codepage — cp1252 on
# Windows — inside subprocess's reader thread, so any byte invalid there
# (0x81/0x8D/0x8F/0x90/0x9D) raised UnicodeDecodeError and failed the ingest.

# 0xC3 0x8D ("Í") lands a 0x8D on the wire: undefined in cp1252, decodes fine as UTF-8.
# Names the fixture's owner so the #61 owner check passes and the round-trip test
# exercises decoding rather than refusal.
OCR_UTF8 = "Patient: Jane Doe — 20 µg/dL — MARTÍNEZ CLINIC".encode("utf-8")


def _fake_tesseract(payload: bytes, platform_default: str = "cp1252"):
    """`subprocess.run` stand-in that decodes the way CPython actually does.

    Mirrors the real contract: with `text=True` and `encoding=None` the pipe is
    wrapped with the locale's preferred encoding under strict error handling.
    """

    def _run(argv, capture_output=False, text=False, encoding=None,
             errors=None, check=False):
        assert argv[0] == "tesseract"
        if not text:
            return subprocess.CompletedProcess(argv, 0, payload, b"")
        decoded = payload.decode(encoding or platform_default, errors or "strict")
        return subprocess.CompletedProcess(argv, 0, decoded, "")

    return _run


def test_run_ocr_decodes_utf8_under_a_cp1252_platform_default(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest.shutil, "which", lambda _: "/usr/bin/tesseract")
    monkeypatch.setattr(ingest.subprocess, "run", _fake_tesseract(OCR_UTF8))
    assert ingest.run_ocr(_make_file(tmp_path)) == OCR_UTF8.decode("utf-8")


def test_run_ocr_survives_undecodable_bytes(tmp_path, monkeypatch):
    """A stray non-UTF-8 byte degrades to U+FFFD, it does not lose the page."""
    monkeypatch.setattr(ingest.shutil, "which", lambda _: "/usr/bin/tesseract")
    monkeypatch.setattr(
        ingest.subprocess, "run", _fake_tesseract(b"scan \xff noise")
    )
    assert ingest.run_ocr(_make_file(tmp_path)) == "scan � noise"


def test_ocr_text_round_trips_through_ingest(conn, tmp_path, sources, monkeypatch):
    # `.png`, not the fixture's default `.txt`: issue #66's dispatcher reads
    # plaintext natively, so only a non-native suffix still reaches tesseract —
    # which is the route whose decoding this test is about.
    monkeypatch.setattr(ingest.shutil, "which", lambda _: "/usr/bin/tesseract")
    monkeypatch.setattr(ingest.subprocess, "run", _fake_tesseract(OCR_UTF8))
    result = ingest.ingest_document(
        conn, _make_file(tmp_path, "scan.png"), "jane-doe", sources, ocr=True
    )
    assert result.document.ocr_text == OCR_UTF8.decode("utf-8")


# --- owner verification (issue #61) -------------------------------------------

JANE = Person(person_id=1, slug="jane-doe", full_name="Jane Doe", dob="1962-03-14")
BOB = Person(person_id=2, slug="bob-roe", full_name="Robert Alan Roe", dob="1955-11-02")
ROSTER = (JANE, BOB)


@pytest.mark.parametrize(
    "text, verdict, matched",
    [
        # --- match: name, in the orderings scanned headers actually use
        ("Patient: Jane Doe   MRN 44812", "match", "jane-doe"),
        ("Patient Name: DOE, JANE A", "match", "jane-doe"),
        ("PATIENT\nDOE\nJANE\nDOB 01/01/1900", "match", "jane-doe"),
        # middle initial in the text but not the roster, and vice versa
        ("Patient: Jane Q. Doe", "match", "jane-doe"),
        # every token must be present: 'Alan' missing -> Bob does *not* match either
        ("Patient: Robert Roe", "suspect", None),
        # --- match: DOB alone is high-entropy enough
        ("Patient: unreadable smudge   DOB: 03/14/1962", "match", "jane-doe"),
        ("date of birth 3/14/1962", "match", "jane-doe"),
        ("DOB 1962-03-14", "match", "jane-doe"),
        ("DOB 03-14-1962", "match", "jane-doe"),
        ("DOB Mar 14, 1962", "match", "jane-doe"),
        ("DOB March 14, 1962", "match", "jane-doe"),
        ("DOB 14 Mar 1962", "match", "jane-doe"),
        # day-first is deliberately not parsed: 14/03/1962 is not a match
        ("DOB 14/03/1962", "suspect", None),
        # --- mismatch: the text names a *different* roster person
        ("Patient: ROE, ROBERT ALAN    DOB: 11/02/1955", "mismatch", "bob-roe"),
        # --- suspect: an identity header naming nobody on the roster (the 2026-08-01
        # incident: a Piedmont summary for a non-roster patient)
        ("Patient: SMITH, JANE A    DOB: 03/14/1900", "suspect", None),
        ("MRN 99812  Name: Karen Fields", "suspect", None),
        # --- unverified: no identity anchor at all, or nothing to go on
        ("Sodium 140 mmol/L; potassium 4.1", "unverified", None),
        ("", "unverified", None),
        ("   \n\t ", "unverified", None),
        (None, "unverified", None),
        # 'name' without a colon is prose, not an anchor
        ("the brand name is atorvastatin", "unverified", None),
    ],
)
def test_check_owner_verdicts(text, verdict, matched):
    check = ingest.check_owner(text, JANE, ROSTER)
    assert check.verdict == verdict
    assert check.matched_slug == matched
    assert check.blocks is (verdict in ("mismatch", "suspect"))


@pytest.mark.parametrize(
    "full_name",
    ["Cher", "Al Wu", "J Doe"],  # <2 usable tokens, or a survivor under 3 chars
)
def test_unusable_name_never_produces_a_false_suspect(full_name):
    """A one-word or very short name carries no name signal — such a person must land
    in `unverified`, never `suspect`, or every document would refuse."""
    person = Person(person_id=9, slug="short", full_name=full_name)
    assert ingest.name_tokens(full_name) == []
    # An anchor is present and nobody matched, but we could never have recognised
    # this person by name, so their absence is ignorance rather than evidence.
    check = ingest.check_owner("Patient: Jane Doe  DOB: 03/14/1962", person, [person])
    assert check.verdict == "unverified"
    assert check.blocks is False
    # ...and with no anchor either, likewise:
    assert ingest.check_owner("routine bloodwork", person, [person]).verdict == (
        "unverified"
    )


def test_unusable_name_with_a_dob_is_not_blocked_by_a_dobless_document():
    """Most documents don't print a DOB; its absence is not evidence of a misfile.

    Regression for the "Michael Vu" case: a two-letter surname erases the name
    signal, and refusing every DOB-less document would make `ingest` unusable
    without --force for anyone with a short name.
    """
    vu = Person(person_id=9, slug="michael-vu", full_name="Michael Vu", dob="1990-09-09")
    correctly_named = ingest.check_owner(
        "Patient: VU, MICHAEL   chest x-ray, two views", vu, [vu]
    )
    assert correctly_named.verdict == "unverified"
    assert correctly_named.blocks is False
    # the DOB still carries them to a positive verdict when it *is* printed
    assert ingest.check_owner(
        "Patient: VU, MICHAEL   DOB: 09/09/1990", vu, [vu]
    ).verdict == "match"


def test_unusable_name_still_bounces_off_another_roster_person():
    """No name signal weakens `suspect`, not `mismatch`: affirmative evidence that the
    text names *someone else on the roster* still blocks."""
    vu = Person(person_id=9, slug="michael-vu", full_name="Michael Vu")
    check = ingest.check_owner("Patient: DOE, JANE A   DOB: 03/14/1962", vu, [vu, JANE])
    assert check.verdict == "mismatch"
    assert check.matched_slug == "jane-doe"


@pytest.mark.parametrize(
    "text",
    [
        "Patient: DOE, JOHN Q   DOB: 23/7/1981",   # day-first, unpadded
        "Patient: DOE, JOHN Q   DOB: 13/7/1981",
        "Patient: DOE, JOHN Q   accession 13/7/1981x",
        "Patient: DOE, JOHN Q   DOB: 03/07/19810",  # trailing digit
    ],
)
def test_dob_needs_digit_boundaries(text):
    """A candidate rendering that is merely a *substring* of a longer digit run is not
    a DOB match — otherwise a day-first `23/7/1981` silently verifies a 1981-03-07
    person and the document is misfiled with an `owner verified` line to reassure."""
    mike = Person(
        person_id=9, slug="alex-carter", full_name="Alex Carter",
        dob="1981-03-07",
    )
    check = ingest.check_owner(text, mike, [mike])
    assert check.verdict == "suspect"
    assert check.matched_slug is None


def test_partial_precision_dob_is_not_a_signal():
    person = Person(person_id=9, slug="p", full_name="Ann Zed", dob="1962")
    assert ingest.dob_candidates("1962") == []
    assert ingest.dob_candidates(None) == []
    assert ingest.check_owner("Patient: someone  DOB: 1962", person, [person]).verdict == (
        "suspect"
    )


def test_evidence_quotes_the_window_around_the_anchor():
    text = "PIEDMONT HEALTHCARE\n" * 3 + "Patient: SMITH, JANE A    DOB: 03/14/1900\n"
    check = ingest.check_owner(text, JANE, ROSTER)
    assert check.verdict == "suspect"
    assert "SMITH, JANE A" in check.evidence
    assert "\n" not in check.evidence  # whitespace collapsed for a one-line message


def test_refusal_message_names_the_other_roster_person():
    check = ingest.check_owner("Patient: ROE, ROBERT ALAN", JANE, ROSTER)
    msg = ingest.refusal_message(check, JANE, ROSTER)
    assert "'bob-roe' (Robert Alan Roe)" in msg
    assert "'jane-doe' (Jane Doe)" in msg
    assert "--force" in msg


def _seed_roster(conn):
    persons.update_person(conn, "jane-doe", dob="1962-03-14")
    persons.add_person(conn, "bob-roe", "Robert Alan Roe", dob="1955-11-02")


def test_ingest_refuses_and_writes_nothing(conn, tmp_path, sources):
    _seed_roster(conn)
    src = _make_file(tmp_path, "wrong.txt", b"scan of somebody else")
    sha = ingest.hash_file(src)

    with pytest.raises(ingest.OwnerMismatchError) as excinfo:
        ingest.ingest_document(
            conn, src, "jane-doe", sources,
            ocr_text="Patient: SMITH, KAREN    DOB: 09/09/1971",
        )
    assert excinfo.value.check.verdict == "suspect"
    # pre-write: no blob, no row -> re-running with force is the whole recovery
    assert not (sources / sha[:2] / f"{sha}.txt").exists()
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 0
    # OwnerMismatchError is an IngestError, so existing handlers still catch it
    assert isinstance(excinfo.value, ingest.IngestError)


def test_ingest_force_overrides_and_still_reports_the_verdict(conn, tmp_path, sources):
    _seed_roster(conn)
    result = ingest.ingest_document(
        conn, _make_file(tmp_path, "forced.txt", b"forced bytes"), "jane-doe", sources,
        ocr_text="Patient: ROE, ROBERT ALAN", force=True,
    )
    assert result.status == "new"
    assert result.owner_check.verdict == "mismatch"
    assert result.owner_check.matched_slug == "bob-roe"


def test_ingest_reports_a_match(conn, tmp_path, sources):
    _seed_roster(conn)
    result = ingest.ingest_document(
        conn, _make_file(tmp_path, "ok.txt", b"good bytes"), "jane-doe", sources,
        ocr_text="Patient: DOE, JANE    DOB: 03/14/1962",
    )
    assert result.owner_check.verdict == "match"
    assert result.owner_check.blocks is False


def test_deactivated_people_still_count_as_roster(conn, tmp_path, sources):
    """A deactivated person is still a real person whose documents must not land on
    someone else — the text naming them is a `mismatch`, not a `suspect`."""
    _seed_roster(conn)
    persons.deactivate_person(conn, "bob-roe")
    with pytest.raises(ingest.OwnerMismatchError) as excinfo:
        ingest.ingest_document(
            conn, _make_file(tmp_path, "d.txt", b"deact"), "jane-doe", sources,
            ocr_text="Patient: ROE, ROBERT ALAN",
        )
    assert excinfo.value.check.verdict == "mismatch"


def test_layer1_duplicate_is_unaffected_by_mismatching_text(conn, tmp_path, sources):
    _seed_roster(conn)
    src = _make_file(tmp_path, "dup.txt", b"same bytes twice")
    first = ingest.ingest_document(conn, src, "jane-doe", sources)
    again = ingest.ingest_document(
        conn, src, "jane-doe", sources, ocr_text="Patient: ROE, ROBERT ALAN"
    )
    assert again.status == "duplicate"
    assert again.document.document_id == first.document.document_id
    assert again.owner_check is None  # nothing written -> nothing checked


# --- intake formats (issue #66) -----------------------------------------------
#
# Two guards, both stdlib-only: Google Drive pointer stubs are refused pre-write, and
# `ocr=True` extracts text natively for the formats that allow it.

_STUB = {
    "url": "https://docs.google.com/spreadsheets/d/1AbC_dEf/edit?usp=drivesdk",
    "doc_id": "1AbC_dEf",
    "email": "someone@example.com",
    "resource_id": "spreadsheet:1AbC_dEf",
}


def _make_stub(tmp_path, name="budget.gsheet", payload=None):
    p = tmp_path / name
    p.write_text(json.dumps(_STUB if payload is None else payload), encoding="utf-8")
    return p


def _make_docx(
    tmp_path, name="note.docx", paragraphs=("HbA1c 5.7 percent",), doctype=""
):
    """A minimal `.docx`; `doctype` injects one into `word/document.xml`'s prolog."""
    body = "".join(
        f"<w:p><w:r><w:t>{part}</w:t></w:r></w:p>" for part in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
        f'2006/main"><w:body>{body}</w:body></w:document>'
    )
    if doctype:
        document = document.replace("?>", f"?>{doctype}", 1)
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", document)
    return p


def _make_xlsx(
    tmp_path,
    name="labs.xlsx",
    rows=(("test", "value"), ("sodium", "140")),
    doctype="",
    doctype_member="xl/sharedStrings.xml",
):
    strings: list[str] = []
    for row in rows:
        for cell in row:
            if cell not in strings:
                strings.append(cell)
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    shared = (
        f'<sst xmlns="{ns}">'
        + "".join(f"<si><t>{s}</t></si>" for s in strings)
        + "</sst>"
    )
    body = "".join(
        "<row>"
        + "".join(f'<c t="s"><v>{strings.index(cell)}</v></c>' for cell in row)
        + "</row>"
        for row in rows
    )
    sheet = f'<worksheet xmlns="{ns}"><sheetData>{body}</sheetData></worksheet>'
    members = {"xl/sharedStrings.xml": shared, "xl/worksheets/sheet1.xml": sheet}
    if doctype:
        # These fixtures carry no XML declaration, so the prolog *is* the leading
        # DOCTYPE — well-formed, and the same position the CCDA tests inject at.
        members[doctype_member] = doctype + members[doctype_member]
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        for member, content in members.items():
            zf.writestr(member, content)
    return p


_CCDA_SECTIONS = (
    "<section><title>Allergies</title><text>"
    "<paragraph>No known drug allergies</paragraph>"
    "</text></section>",
    "<section><title>Results</title><text><table>"
    "<thead><tr><th>Test</th><th>Value</th></tr></thead>"
    "<tbody><tr><td>Ferritin</td><td>201 ng/mL</td></tr></tbody>"
    "</table></text></section>",
)


def _make_ccda(
    tmp_path,
    name="DOC0001.XML",
    patient=("Jane", "Doe"),
    birth="19620314",
    sections=_CCDA_SECTIONS,
):
    given, family = patient
    birth_el = f'<birthTime value="{birth}"/>' if birth else ""
    body = (
        f"<component><structuredBody>"
        + "".join(f"<component>{s}</component>" for s in sections)
        + "</structuredBody></component>"
    ) if sections else ""
    doc = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ClinicalDocument xmlns="urn:hl7-org:v3">'
        "<recordTarget><patientRole><patient>"
        f"<name><given>{given}</given><family>{family}</family></name>"
        f"{birth_el}"
        "</patient></patientRole></recordTarget>"
        f"{body}</ClinicalDocument>"
    )
    p = tmp_path / name
    p.write_text(doc, encoding="utf-8")
    return p


def test_pointer_stub_is_refused_pre_write(conn, tmp_path, sources):
    stub = _make_stub(tmp_path)
    with pytest.raises(ingest.IngestError) as excinfo:
        ingest.ingest_document(conn, stub, "jane-doe", sources)
    message = str(excinfo.value)
    assert "pointer stub" in message
    assert "Export it from Drive" in message  # names the fix
    # pre-write: no blob and no row
    assert not sources.exists()
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 0


def test_pointer_guard_is_conjunctive_so_a_real_file_still_ingests(
    conn, tmp_path, sources
):
    # a genuine spreadsheet that merely carries a `.gsheet` name: right suffix, but
    # too big and not JSON -> not a stub.
    real = _make_file(tmp_path, "real.gsheet", b"PK\x03\x04" + b"\xff" * 20_000)
    result = ingest.ingest_document(conn, real, "jane-doe", sources)
    assert result.status == "new"


@pytest.mark.parametrize(
    "name, payload",
    [
        ("notes.txt", _STUB),                       # wrong suffix
        ("x.gsheet", {"url": "https://example.com/x"}),   # wrong host
        ("x.gsheet", {"doc_id": "abc"}),            # older shape, missing `email`
        ("x.gsheet", ["not", "an", "object"]),      # JSON, but not an object
    ],
)
def test_pointer_guard_does_not_fire(conn, tmp_path, sources, name, payload):
    src = _make_stub(tmp_path, name, payload)
    assert not ingest.is_pointer_stub(src)
    assert ingest.ingest_document(conn, src, "jane-doe", sources).status == "new"


def test_pointer_guard_ignores_unparseable_files(tmp_path):
    assert not ingest.is_pointer_stub(_make_file(tmp_path, "b.gsheet", b"\xff\xfe\x00"))


def test_extract_text_reads_plaintext_formats(conn, tmp_path, sources):
    src = _make_file(tmp_path, "labs.csv", "test,value\r\nsodium,140\n".encode("utf-8"))
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert result.ocr_text_populated
    assert "sodium,140" in result.document.ocr_text


def test_extract_text_reads_docx(conn, tmp_path, sources):
    src = _make_docx(tmp_path, paragraphs=("Visit summary", "HbA1c 5.7 percent"))
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert result.document.ocr_text == "Visit summary\nHbA1c 5.7 percent"


def test_extract_text_reads_xlsx(conn, tmp_path, sources):
    src = _make_xlsx(tmp_path)
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert result.document.ocr_text == "test\tvalue\nsodium\t140"


# --- CCDA (issue #138) ---------------------------------------------------------
# `.xml` used to reach tesseract, which cannot decode it, so every portal export
# ("download my record" produces CCDA) landed with no `ocr_text` *and* an unverified
# owner. Detection is on the parsed root element, never the suffix.


def test_extract_text_reads_ccda_xml(conn, tmp_path, sources):
    src = _make_ccda(tmp_path)
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    text = result.document.ocr_text
    assert result.ocr_text_populated
    assert text.startswith("Patient: Jane Doe\nDOB: 1962-03-14")
    assert "Allergies\nNo known drug allergies" in text
    # header and value survive on one tab-delimited line each
    assert "Test\tValue" in text
    assert "Ferritin\t201 ng/mL" in text


# The shapes a real portal export carries that `_make_ccda` only approximates: a
# multi-`<given>` legal name, a second `<name nullFlavor="UNK"/>`, a section nested
# inside another section's `<component>`, `<list>/<item>` narrative, and inline
# markup (`<content>`, `<sub>`) splitting text mid-cell.
_CCDA_VENDOR = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<ClinicalDocument xmlns="urn:hl7-org:v3">'
    "<recordTarget><patientRole><patient>"
    '<name use="L"><given>Jane</given><given>Amelia</given><family>Doe</family></name>'
    '<name use="P" nullFlavor="UNK"/>'
    '<birthTime value="19620314000000-0600"/>'
    "</patient></patientRole></recordTarget>"
    "<component><structuredBody><component><section><title>Results</title><text>"
    "<table><thead><tr><th>Test</th><th>Result</th></tr></thead>"
    "<tbody><tr><td><content ID='res1'>Ferritin</content></td>"
    "<td>11.<sub>2</sub></td></tr></tbody></table>"
    '<renderMultiMedia referencedObject="MM1"/>'
    "</text>"
    "<component><section><title>Results Addendum</title><text>"
    "<list><item>Repeat ferritin in 8 weeks.</item>"
    "<item><content ID='med27'>Ferrous sulfate 325 MG</content> - 1 tablet daily</item>"
    "</list></text></section></component>"
    "</section></component></structuredBody></component>"
    "</ClinicalDocument>"
)


def test_ccda_renders_real_vendor_export_shapes(conn, tmp_path, sources):
    src = tmp_path / "DOC0003.XML"
    src.write_text(_CCDA_VENDOR, encoding="utf-8")
    text = ingest.ingest_document(
        conn, src, "jane-doe", sources, ocr=True
    ).document.ocr_text
    # every `<given>` joins into the one name the owner check tokenises
    assert text.startswith("Patient: Jane Amelia Doe\nDOB: 1962-03-14")
    assert "Patient: \n" not in text   # the nullFlavor name contributes no line
    # inline markup is flattened, not dropped, and does not split the cell
    assert "Ferritin\t11.2" in text
    # a nested section renders under its own title, exactly once
    assert "Results Addendum" in text
    assert text.count("Repeat ferritin in 8 weeks.") == 1
    # `<list>` recurses, `<item>` flattens with its own tail text
    assert "Ferrous sulfate 325 MG - 1 tablet daily" in text


# The medication-table shape #159 rests on: the discontinue reason is inline `<content>`
# inside the same status cell as the lifecycle word. This pins the #138 extraction that
# `medication.status_reason` depends on, so a narrative regression is caught here rather
# than in a live corpus.
_CCDA_MEDS = (
    "<section><title>Medications</title><text><table>"
    "<thead><tr><th>Medication</th><th>Date</th><th>Status</th></tr></thead>"
    "<tbody>"
    "<tr><td>Amoxicillin 500 MG</td><td>08/28/2025</td><td>Discontinued</td></tr>"
    "<tr><td>Lisinopril 10 MG</td><td>11/11/2024</td>"
    "<td>Discontinued<content> (Therapy Completed)</content></td></tr>"
    "<tr><td>Levothyroxine 50 MCG</td><td>06/11/2026</td>"
    "<td>Discontinued<content> (Reorder)</content></td></tr>"
    "<tr><td>Atorvastatin 20 MG</td><td>03/02/2025</td>"
    "<td>Discontinued<content> (Patient Stopped Taking)</content></td></tr>"
    "<tr><td>Omeprazole 20 MG</td><td>05/09/2025</td>"
    "<td>Discontinued<content> (Substitution/Alternate Therapy Placed)</content></td>"
    "</tr>"
    "</tbody></table></text></section>",
)


@pytest.mark.parametrize("drug, cell", [
    ("Amoxicillin 500 MG", "Discontinued"),
    ("Lisinopril 10 MG", "Discontinued (Therapy Completed)"),
    ("Levothyroxine 50 MCG", "Discontinued (Reorder)"),
    ("Atorvastatin 20 MG", "Discontinued (Patient Stopped Taking)"),
    ("Omeprazole 20 MG", "Discontinued (Substitution/Alternate Therapy Placed)"),
])
def test_ccda_med_table_keeps_the_discontinue_reason_in_its_cell(
    conn, tmp_path, sources, drug, cell
):
    """Each status cell reaches ocr_text whole - reason attached to its own row's status
    word, never split across cells or dropped (issue #159's extraction dependency)."""
    src = _make_ccda(tmp_path, name="DOC0159.XML", sections=_CCDA_MEDS)
    text = ingest.ingest_document(
        conn, src, "jane-doe", sources, ocr=True
    ).document.ocr_text
    assert f"{drug}\t" in text
    assert text.count(cell) >= 1
    row = next(line for line in text.splitlines() if line.startswith(drug))
    assert row.endswith(cell)


def test_ccda_never_shells_out(conn, tmp_path, sources, monkeypatch):
    def boom(path):  # pragma: no cover - the assertion is that this never runs
        raise AssertionError(f"run_ocr must not be called for {path}")

    monkeypatch.setattr(ingest, "run_ocr", boom)
    src = _make_ccda(tmp_path)
    assert ingest.ingest_document(
        conn, src, "jane-doe", sources, ocr=True
    ).ocr_text_populated


@pytest.mark.parametrize(
    "name, payload",
    [
        # an IHE_XDM manifest sitting beside the documents
        ("METADATA.XML",
         '<?xml version="1.0"?><Manifest><Doc>DOC0001.XML</Doc></Manifest>'),
        ("plain.xml", "<root><a>1</a></root>"),
        # right namespace, wrong root: the parsed root tag is the authority
        ("other.xml", '<Bundle xmlns="urn:hl7-org:v3"><ClinicalDocument/></Bundle>'),
    ],
)
def test_non_ccda_xml_still_reaches_tesseract(
    conn, tmp_path, sources, monkeypatch, name, payload
):
    seen: list[str] = []

    def fake_run_ocr(path):
        seen.append(str(path))
        return "Ferritin 201 nanograms"

    monkeypatch.setattr(ingest, "run_ocr", fake_run_ocr)
    src = _make_file(tmp_path, name, payload.encode("utf-8"))
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert seen == [str(src)]
    assert result.document.ocr_text == "Ferritin 201 nanograms"


def test_malformed_ccda_xml_does_not_raise(conn, tmp_path, sources, capsys, monkeypatch):
    monkeypatch.setattr(ingest, "run_ocr", lambda _: None)   # tesseract declines
    src = _make_file(
        tmp_path, "broken.xml",
        b'<?xml version="1.0"?><ClinicalDocument xmlns="urn:hl7-org:v3">'
        b"<recordTarget><patientRole",
    )
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert result.status == "new"           # the document still lands
    assert result.document.ocr_text is None
    assert "--ocr-text-file" in capsys.readouterr().err


@pytest.mark.parametrize("wrapper", ["content", "list", "paragraph"])
def test_deeply_nested_ccda_narrative_does_not_raise(
    conn, tmp_path, sources, wrapper
):
    """Nesting depth is document-controlled and unbounded, and ~1500 levels fits in
    30 KB — far under the extraction cap. A recursive walk raised `RecursionError`
    (a `RuntimeError`, so outside `_EXTRACT_ERRORS`) straight out of
    `extract_text_routed`, breaking its "never raises" contract and losing the
    document entirely."""
    narrative = "Ferritin 201 ng/mL"
    for _ in range(1500):
        narrative = f"<{wrapper}>{narrative}</{wrapper}>"
    src = _make_ccda(
        tmp_path,
        sections=(f"<section><title>Results</title><text>{narrative}</text></section>",),
    )
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert result.status == "new"                       # the document still lands
    assert "Ferritin 201 ng/mL" in result.document.ocr_text   # ...and is readable


def test_recursion_error_degrades_like_any_other_extraction_failure(
    conn, tmp_path, sources, capsys, monkeypatch
):
    """Our own walk is iterative, but the stdlib's are not (`Element.itertext()` is a
    recursive generator), so the best-effort handler has to cover `RecursionError`
    too — it is a `RuntimeError` and would otherwise escape the contract."""
    def boom(_):
        raise RecursionError("maximum recursion depth exceeded")

    def never(path):  # pragma: no cover - the degrade is native, not a fallback to OCR
        raise AssertionError(f"run_ocr must not be called for {path}")

    monkeypatch.setattr(ingest, "_extract_ccda", boom)
    monkeypatch.setattr(ingest, "run_ocr", never)
    src = _make_ccda(tmp_path)
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert result.status == "new"                # the document still lands
    assert result.document.ocr_text is None
    assert "text extraction failed" in capsys.readouterr().err


def test_ccda_doctype_is_not_parsed_natively(conn, tmp_path, sources, monkeypatch):
    """stdlib `ET` expands internal entities, so a `<!DOCTYPE` is refused before the
    parse rather than after — a billion-laughs file is small enough to clear the cap."""
    seen: list[str] = []
    monkeypatch.setattr(ingest, "run_ocr", lambda p: seen.append(str(p)) or "scanned")
    body = _make_ccda(tmp_path, name="doctype-src.XML").read_text(encoding="utf-8")
    src = tmp_path / "doctype.xml"
    src.write_text(
        body.replace(
            "?>", '?><!DOCTYPE ClinicalDocument [<!ENTITY a "aaaa">]>', 1
        ),
        encoding="utf-8",
    )
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert seen == [str(src)]
    assert result.document.ocr_text == "scanned"


@pytest.mark.parametrize(
    "prolog",
    [
        # the markers sit in a leading comment, so the file still sniffs as a CCDA
        # while the DOCTYPE is padded past any fixed head window
        '<!-- urn:hl7-org:v3 ClinicalDocument --><!-- ' + "x" * 9000 + " -->",
        # a comment carrying something that *looks* like the root element: stopping at
        # the first `<` instead of stepping over comments would end the scan here
        '<!-- urn:hl7-org:v3 --><!-- <ClinicalDocument xmlns="urn:hl7-org:v3"> -->',
    ],
    ids=["padded-past-the-sniff-window", "comment-holding-a-fake-start-tag"],
)
def test_ccda_doctype_anywhere_in_the_prolog_is_not_parsed_natively(
    conn, tmp_path, sources, monkeypatch, prolog
):
    """The DOCTYPE refusal scans the whole prolog, not a fixed head window.

    A comment pushes the DOCTYPE past any window while the CCDA markers stay inside it,
    and the internal subset it hides amplifies far past the byte cap — expat only checks
    its ratio above 8 MiB of output, so everything under that expands silently."""
    seen: list[str] = []
    monkeypatch.setattr(ingest, "run_ocr", lambda p: seen.append(str(p)) or "scanned")
    body = _make_ccda(tmp_path, name="prolog-src.XML").read_text(encoding="utf-8")
    src = tmp_path / "prolog-doctype.xml"
    src.write_text(
        body.replace(
            "?>",
            f'?>{prolog}<!DOCTYPE ClinicalDocument [<!ENTITY a "aaaa">]>',
            1,
        ),
        encoding="utf-8",
    )
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert seen == [str(src)]
    assert result.document.ocr_text == "scanned"


def test_ccda_with_a_harmless_prolog_comment_still_reads_natively(
    conn, tmp_path, sources, monkeypatch
):
    """The other half of the guard: stepping over prolog markup must not make the
    scanner over-eager. A real CCDA behind a comment — including one quoting a start
    tag — has no DOCTYPE and is still rendered rather than shipped to tesseract."""
    def never(path):  # pragma: no cover - a CCDA never reaches the OCR route
        raise AssertionError(f"run_ocr must not be called for {path}")

    monkeypatch.setattr(ingest, "run_ocr", never)
    body = _make_ccda(tmp_path, name="comment-src.XML").read_text(encoding="utf-8")
    src = tmp_path / "commented.xml"
    src.write_text(
        body.replace("?>", "?><!-- exported <ClinicalDocument> 2026 -->", 1),
        encoding="utf-8",
    )
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert "Ferritin" in result.document.ocr_text


# --- the DOCTYPE guard has to be encoding-agnostic (issue #138 audit) ------------- #
#
# Two byte scanners were bypassed before the current expat probe. The second one
# matched raw ASCII `<!--`/`<?`/`<!doctype`, which in UTF-16 are `<\x00!\x00...`: the
# first `<` looked like a start tag, the scan declared the prolog over, and the DOCTYPE
# went unseen. The file still *sniffed* as a CCDA because ASCII marker bytes are
# trivially smuggled into a UTF-16 comment — one CJK character carries two of them —
# so the bomb took the native route and a 1.4 KB file rendered 1 MB of text.

def _utf16_smuggled(marker: str, big_endian: bool = False) -> str:
    """Characters whose UTF-16 bytes spell ``marker`` in ASCII (for the sniff)."""
    hi, lo = (0, 8) if big_endian else (8, 0)
    return "".join(
        chr((ord(marker[i]) << lo) | (ord(marker[i + 1]) << hi))
        for i in range(0, len(marker), 2)
    )


def _entity_bomb_doctype(levels=7, width=4, leaf=64, root="ClinicalDocument"):
    """A `<!DOCTYPE` whose internal subset amplifies ``&e{levels};`` ~1 MB.

    ``root`` names the declared root element so the OOXML reuses (issue #155) can
    build a bomb that is a plausible `.docx`/`.xlsx` member, not just a CCDA one."""
    chain = "".join(
        f'<!ENTITY e{n} "{f"&e{n - 1};" * width}">' for n in range(1, levels + 1)
    )
    return f'<!DOCTYPE {root} [<!ENTITY e0 "{"A" * leaf}">{chain}]>', levels


@pytest.mark.parametrize(
    "encoding, bom, big_endian",
    [
        ("utf-16-le", b"\xff\xfe", False),
        ("utf-16-be", b"\xfe\xff", True),
    ],
    ids=["utf-16-le", "utf-16-be"],
)
def test_ccda_doctype_in_a_utf16_document_is_not_parsed_natively(
    conn, tmp_path, sources, monkeypatch, encoding, bom, big_endian
):
    """The refusal must hold in an encoding whose bytes no ASCII scan can read.

    Non-vacuous by construction: the smuggled markers make the file pass the ASCII
    sniff, so it *is* a CCDA candidate and only the DOCTYPE guard stands between it and
    a parse that expands the internal subset."""
    seen: list[str] = []
    monkeypatch.setattr(ingest, "run_ocr", lambda p: seen.append(str(p)) or "scanned")
    doctype, levels = _entity_bomb_doctype()
    markers = _utf16_smuggled("urn:hl7-org:v3", big_endian) + _utf16_smuggled(
        "ClinicalDocument", big_endian
    )
    body = _make_ccda(tmp_path, name="utf16-src.XML").read_text(encoding="utf-8")
    doc = (
        body.replace('encoding="UTF-8"', 'encoding="UTF-16"', 1)
        .replace("?>", f"?><!-- {markers} -->{doctype}", 1)
        .replace("Ferritin 201 ng/mL", f"&e{levels};")
    )
    src = tmp_path / "utf16-doctype.xml"
    src.write_bytes(bom + doc.encode(encoding))

    raw = src.read_bytes()
    assert b"urn:hl7-org:v3" in raw and b"ClinicalDocument" in raw   # sniffs as CCDA
    assert ingest._xml_declares_doctype(raw) is True

    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert seen == [str(src)]                       # fell through to today's route
    assert result.document.ocr_text == "scanned"    # no amplified text stored
    assert "A" * 200 not in (result.document.ocr_text or "")


def test_utf16_ccda_without_a_doctype_keeps_the_unchanged_ocr_route(
    conn, tmp_path, sources, monkeypatch
):
    """The mirror case, pinned as a deliberate product decision rather than an accident.

    The sniff matches ASCII marker bytes, so a *genuine* UTF-16 CCDA is not detected and
    keeps today's tesseract route. Narrowing the negative filter costs nothing beyond
    the status quo (US portal exports are UTF-8); it is the DOCTYPE refusal, not the
    sniff, that has to be right for every encoding."""
    seen: list[str] = []
    monkeypatch.setattr(ingest, "run_ocr", lambda p: seen.append(str(p)) or "scanned")
    body = _make_ccda(tmp_path, name="utf16-plain-src.XML").read_text(encoding="utf-8")
    src = tmp_path / "utf16-plain.xml"
    # a well-formed UTF-16 CCDA (declaration and BOM agree), not a mangled one
    src.write_bytes(body.replace('encoding="UTF-8"', 'encoding="UTF-16"', 1)
                    .encode("utf-16"))
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert seen == [str(src)]
    assert result.document.ocr_text == "scanned"


def test_doctype_probe_refuses_before_the_internal_subset_expands(tmp_path):
    """Unit-level proof of *why* the probe is safe: expat reports the DOCTYPE before it
    reads the subset, so the amplification never happens. A 1.4 KB file that would
    render ~1 MB is refused, and refusing it does not cost the expansion."""
    doctype, levels = _entity_bomb_doctype()
    body = _make_ccda(tmp_path, name="bomb-src.XML").read_text(encoding="utf-8")
    doc = body.replace("?>", f"?>{doctype}", 1).replace(
        "Ferritin 201 ng/mL", f"&e{levels};"
    )
    src = tmp_path / "bomb.xml"
    src.write_text(doc, encoding="utf-8")
    assert src.stat().st_size < 4096            # small enough to clear the byte cap
    assert ingest._xml_declares_doctype(src.read_bytes()) is True
    assert ingest._extract_ccda(src) is None    # never rendered, so never amplified


def test_a_doctype_the_probe_cannot_reach_still_falls_through(tmp_path):
    """A parse error is the other ``False`` arm, and it is safe because it is the same
    expat: whatever the probe cannot read, the `ET.fromstring` after it cannot read
    either, so the file takes the unchanged non-CCDA route. Pinned on a lowercase
    ``<!doctype``, which is not well-formed XML at all."""
    doctype, levels = _entity_bomb_doctype()
    body = _make_ccda(tmp_path, name="lower-src.XML").read_text(encoding="utf-8")
    doc = body.replace(
        "?>", f"?>{doctype.replace('<!DOCTYPE', '<!doctype')}", 1
    ).replace("Ferritin 201 ng/mL", f"&e{levels};")
    src = tmp_path / "lowercase-doctype.xml"
    src.write_text(doc, encoding="utf-8")
    assert ingest._xml_declares_doctype(src.read_bytes()) is False
    assert ingest._extract_ccda(src) is None    # refused all the same


# --- the guard's equivalence class, not just its fixtures (issue #138 verify) ----- #
#
# Three shapes the UTF-16 pair does not cover: a 4-byte encoding, a BOM that contradicts
# the declaration, and a DOCTYPE with no internal subset at all. Each is refused, and the
# point of pinning them is that all three arrive at that refusal by a *different* arm.


def test_ccda_doctype_in_a_utf32_document_never_reaches_the_parser(
    conn, tmp_path, sources, monkeypatch
):
    """UTF-32 is refused by the *sniff*, not the DOCTYPE probe — and that is fine.

    A 4-byte encoding cannot smuggle a contiguous ASCII run (every character carries two
    zero bytes), so the marker sniff fails and the file keeps today's OCR route before
    anything is parsed. Pinned because the safety of the whole design rests on the sniff
    being a *negative* filter: it is free for it to be conservative, so a shape it cannot
    read must fall through rather than be special-cased into the native path."""
    seen: list[str] = []
    monkeypatch.setattr(ingest, "run_ocr", lambda p: seen.append(str(p)) or "scanned")
    doctype, levels = _entity_bomb_doctype()
    body = _make_ccda(tmp_path, name="utf32-src.XML").read_text(encoding="utf-8")
    doc = (
        body.replace('encoding="UTF-8"', 'encoding="UTF-32"', 1)
        .replace("?>", f"?>{doctype}", 1)
        .replace("Ferritin 201 ng/mL", f"&e{levels};")
    )
    src = tmp_path / "utf32-doctype.xml"
    src.write_bytes(b"\xff\xfe\x00\x00" + doc.encode("utf-32-le"))

    head = src.read_bytes()[:ingest._CCDA_SNIFF_BYTES]
    assert b"urn:hl7-org:v3" not in head       # the sniff is what stops it here
    assert ingest._extract_ccda(src) is None

    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert seen == [str(src)]
    assert result.document.ocr_text == "scanned"


def test_ccda_whose_bom_contradicts_its_declaration_is_not_parsed_natively(
    conn, tmp_path, sources, monkeypatch
):
    """A UTF-8 BOM under an ``encoding="UTF-16"`` declaration: the file sniffs as a CCDA
    (its bytes are ASCII-compatible) but no parser can agree with it.

    This is the ``ExpatError`` arm rather than the DOCTYPE arm, and it is safe for the
    reason that arm exists: the probe is the same expat the `ET.fromstring` below it uses,
    so a file the probe cannot read is one the parse cannot read either — the DOCTYPE it
    carries is never expanded because the document is never parsed at all."""
    seen: list[str] = []
    monkeypatch.setattr(ingest, "run_ocr", lambda p: seen.append(str(p)) or "scanned")
    doctype, levels = _entity_bomb_doctype()
    body = _make_ccda(tmp_path, name="bom-src.XML").read_text(encoding="utf-8")
    doc = (
        body.replace('encoding="UTF-8"', 'encoding="UTF-16"', 1)
        .replace("?>", f"?>{doctype}", 1)
        .replace("Ferritin 201 ng/mL", f"&e{levels};")
    )
    src = tmp_path / "bom-mismatch.xml"
    src.write_bytes(b"\xef\xbb\xbf" + doc.encode("utf-8"))   # BOM says UTF-8

    raw = src.read_bytes()
    assert b"urn:hl7-org:v3" in raw and b"ClinicalDocument" in raw   # sniffs as CCDA
    assert ingest._xml_declares_doctype(raw) is False                # the parse-error arm
    assert ingest._extract_ccda(src) is None

    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert seen == [str(src)]
    assert result.document.ocr_text == "scanned"
    assert "A" * 200 not in (result.document.ocr_text or "")


def test_an_external_doctype_is_refused_without_fetching_it(tmp_path):
    """A DOCTYPE with an external ``SYSTEM`` id and **no** internal subset.

    Two properties at once. It is still refused — the guard keys on the declaration, not
    on whether a subset follows — and refusing it costs no network: the ``SYSTEM`` id
    points at TEST-NET-1, which blackholes rather than refuses, so a resolver would stall
    for seconds instead of returning. Local-first has no exception for a schema fetch."""
    body = _make_ccda(tmp_path, name="external-src.XML").read_text(encoding="utf-8")
    src = tmp_path / "external-doctype.xml"
    src.write_text(
        body.replace(
            "?>",
            '?><!DOCTYPE ClinicalDocument SYSTEM "http://192.0.2.1/CDA.dtd">',
            1,
        ),
        encoding="utf-8",
    )
    started = time.monotonic()
    assert ingest._xml_declares_doctype(src.read_bytes()) is True
    assert ingest._extract_ccda(src) is None
    assert time.monotonic() - started < 2.0     # nothing was dereferenced


def test_ccda_owner_check_matches_record_target(conn, tmp_path, sources):
    """The point of the issue: `recordTarget` is an identity the check can use, so a
    CCDA no longer degrades to "filed on your say-so"."""
    _seed_roster(conn)
    # no `birthTime`, so the name is the only signal — `<given>`/`<family>` have no
    # whitespace of their own and must not flatten into one unmatchable token
    src = _make_ccda(tmp_path, birth="")
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert result.owner_check.verdict == "match"
    assert result.owner_check.blocks is False
    # ...and the protective half still fires on this route.
    # `pii-allow`: the roster placeholder "Robert Alan Roe" (see BOB above), split into
    # CDA's `<given>`/`<family>` — the scanner reads the given half as first+last.
    other = _make_ccda(
        tmp_path, name="DOC0002.XML",
        patient=("Robert Alan", "Roe"),  # pii-allow
        birth="",
    )
    with pytest.raises(ingest.OwnerMismatchError) as excinfo:
        ingest.ingest_document(conn, other, "jane-doe", sources, ocr=True)
    assert excinfo.value.check.verdict == "mismatch"
    assert excinfo.value.check.matched_slug == "bob-roe"


def test_ccda_birth_time_is_rendered_matchably(conn, tmp_path, sources):
    """`19620314` is not a form `dob_candidates` knows; the ISO reformat is what makes
    the DOB half of the check work at all."""
    _seed_roster(conn)
    src = _make_ccda(tmp_path, patient=("Unreadable", "Smudge"))
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert result.owner_check.verdict == "match"


def test_ccda_stranger_is_unverified_not_suspect(conn, tmp_path, sources):
    """Route-scoped anchors, same as `.docx`/`.xlsx`: a CCDA naming a non-roster
    stranger is `unverified`. No regression — it used to yield no text at all."""
    _seed_roster(conn)
    src = _make_ccda(tmp_path, patient=("Karen", "Fields"), birth="19710909")
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert result.owner_check.verdict == "unverified"
    assert result.status == "new"


def test_find_sees_ccda_narrative(conn, tmp_path, sources):
    """The headline symptom: `find` was blind to every CCDA."""
    ingest.ingest_document(conn, _make_ccda(tmp_path), "jane-doe", sources, ocr=True)
    hits = query.find(conn, "jane-doe", "ferritin")
    assert [hit["person"] for hit in hits] == ["jane-doe"]


def test_ccda_without_structured_body_still_yields_identity(conn, tmp_path, sources):
    _seed_roster(conn)
    src = _make_ccda(tmp_path, sections=())
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert result.document.ocr_text == "Patient: Jane Doe\nDOB: 1962-03-14"
    assert result.owner_check.verdict == "match"


def test_extract_text_has_no_route_for_msg(conn, tmp_path, sources, capsys, monkeypatch):
    monkeypatch.setattr(ingest, "run_ocr", lambda _: None)   # tesseract declines
    src = _make_file(tmp_path, "thread.msg", b"\xd0\xcf\x11\xe0 outlook")
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert result.status == "new"          # the document still lands
    assert result.document.ocr_text is None
    assert "--ocr-text-file" in capsys.readouterr().err


# --- routing boundary (issue #66 verify bounce) --------------------------------
# `ocr=True` used to hand *every* file to tesseract. An image-suffix allowlist silently
# dropped `.jfif`/`.jpe`/extension-less scans out of `find`, so anything without a native
# route must still reach `run_ocr`.


@pytest.mark.parametrize(
    "name",
    ["alpha.jfif", "beta.jpe", "gamma_noext", "delta.JPG", "scan.pdf", "thread.msg"],
)
def test_suffixes_without_a_native_route_still_reach_tesseract(
    conn, tmp_path, sources, monkeypatch, name
):
    seen: list[str] = []

    def fake_run_ocr(path):
        seen.append(str(path))
        return "Ferritin 201 nanograms"

    monkeypatch.setattr(ingest, "run_ocr", fake_run_ocr)
    src = _make_file(tmp_path, name, b"\x89PNG pretend scan")
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert seen == [str(src)]
    assert result.document.ocr_text == "Ferritin 201 nanograms"


@pytest.mark.parametrize("maker", ["csv", "txt", "docx", "xlsx"])
def test_natively_extracted_suffixes_never_shell_out(
    conn, tmp_path, sources, monkeypatch, maker
):
    def boom(path):  # pragma: no cover - the assertion is that this never runs
        raise AssertionError(f"run_ocr must not be called for {path}")

    monkeypatch.setattr(ingest, "run_ocr", boom)
    if maker == "docx":
        src = _make_docx(tmp_path, paragraphs=("sodium 140",))
    elif maker == "xlsx":
        src = _make_xlsx(tmp_path)
    else:
        src = _make_file(tmp_path, f"labs.{maker}", b"test,value\nsodium,140\n")
    assert ingest.ingest_document(
        conn, src, "jane-doe", sources, ocr=True
    ).ocr_text_populated


def test_xlsx_bad_shared_string_index_only_loses_that_cell(conn, tmp_path, sources):
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    src = tmp_path / "badidx.xlsx"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(
            "xl/sharedStrings.xml",
            f'<sst xmlns="{ns}"><si><t>ferritin</t></si></sst>',
        )
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{ns}"><sheetData>'
            '<row><c t="s"><v>0</v></c><c t="s"><v>notanint</v></c>'
            '<c t="s"><v>0</v></c></row>'
            "</sheetData></worksheet>",
        )
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    # bad cell blank, the rest of the workbook survives
    assert result.document.ocr_text == "ferritin\t\tferritin"


def test_malformed_docx_degrades_instead_of_losing_the_document(
    conn, tmp_path, sources, capsys
):
    src = _make_file(tmp_path, "broken.docx", b"not a zip at all")
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert result.status == "new"
    assert result.document.ocr_text is None
    assert "extraction failed" in capsys.readouterr().err


# --- OOXML DOCTYPE refusal (issue #155) ------------------------------------------
# `_member_reader`'s cap bounds a member's *declared* uncompressed size, which is not a
# bound on entity expansion: a 355-byte `.docx` expanded to 4,194,304 chars and was
# stored as `native` text. The CCDA route was guarded in #138; these pin the same guard
# on the OOXML route, all-or-nothing per file.


def _explode_on_parse(monkeypatch):
    """Make any `ET.fromstring` a test failure — refusal must precede the parse."""
    def _explode(*_args, **_kwargs):
        raise AssertionError("ET.fromstring reached a DOCTYPE-bearing member")
    monkeypatch.setattr(ingest.ET, "fromstring", _explode)


def test_docx_doctype_is_refused_before_parsing(tmp_path, monkeypatch):
    doctype, levels = _entity_bomb_doctype(root="w:document")
    src = _make_docx(
        tmp_path, name="bomb.docx", paragraphs=(f"&e{levels};",), doctype=doctype
    )
    assert src.stat().st_size < 4096            # well under the byte cap
    _explode_on_parse(monkeypatch)
    # refused, not merely empty: the parse that would amplify never runs
    assert ingest.extract_text_routed(src) == (None, "native")


def test_xlsx_doctype_in_shared_strings_is_refused(tmp_path, monkeypatch):
    doctype, levels = _entity_bomb_doctype(root="sst")
    src = _make_xlsx(tmp_path, name="bomb-shared.xlsx", doctype=doctype)
    _explode_on_parse(monkeypatch)
    assert ingest.extract_text_routed(src) == (None, "native")


def test_xlsx_doctype_in_a_worksheet_refuses_the_whole_workbook(tmp_path, capsys):
    """All-or-nothing: benign shared strings do not buy a best-effort partial result."""
    doctype, _levels = _entity_bomb_doctype(root="worksheet")
    src = _make_xlsx(
        tmp_path,
        name="bomb-sheet.xlsx",
        doctype=doctype,
        doctype_member="xl/worksheets/sheet1.xml",
    )
    assert ingest.extract_text_routed(src) == (None, "native")
    err = capsys.readouterr().err
    # refused at that member, and nothing expanded on the way there
    assert "xl/worksheets/sheet1.xml declares a DOCTYPE" in err
    assert "A" * 200 not in err


def test_ooxml_doctype_refusal_is_encoding_agnostic(tmp_path, monkeypatch):
    """A UTF-16 member with the DOCTYPE padded behind a comment — the two bypass
    classes documented against the CCDA probe must not reopen on this route."""
    doctype, levels = _entity_bomb_doctype(root="w:document")
    document = (
        '<?xml version="1.0" encoding="UTF-16" standalone="yes"?>'
        f"<!-- padding so no fixed head window sees it -->{doctype}"
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
        f'2006/main"><w:body><w:p><w:r><w:t>&e{levels};</w:t></w:r></w:p>'
        "</w:body></w:document>"
    )
    src = tmp_path / "utf16-bomb.docx"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", b"\xff\xfe" + document.encode("utf-16-le"))
    _explode_on_parse(monkeypatch)
    assert ingest.extract_text_routed(src) == (None, "native")


def test_docx_doctype_stores_the_document_without_ocr_text(
    conn, tmp_path, sources, capsys
):
    """The "never costs you the document" contract, at the level the caller sees."""
    doctype, levels = _entity_bomb_doctype(root="w:document")
    src = _make_docx(
        tmp_path, name="bomb2.docx", paragraphs=(f"&e{levels};",), doctype=doctype
    )
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert result.status == "new"                # the document itself is kept
    assert result.document.ocr_text is None
    assert not result.ocr_text_populated
    assert "extraction failed" in capsys.readouterr().err


def test_supplied_ocr_text_still_beats_extraction(conn, tmp_path, sources):
    src = _make_docx(tmp_path, paragraphs=("extracted body",))
    result = ingest.ingest_document(
        conn, src, "jane-doe", sources, ocr=True, ocr_text="agent transcription"
    )
    assert result.document.ocr_text == "agent transcription"


def test_zero_width_only_ocr_text_is_no_text_at_all(conn, tmp_path, sources):
    """`.strip()` keeps a zero-width character (it is not `.isspace()`), so a
    zero-width-only transcription used to count as supplied and report
    `ocr_text_populated` on a document carrying nothing (#87)."""
    src = _make_file(tmp_path, "scan.txt", b"a scan with no text")
    result = ingest.ingest_document(
        conn, src, "jane-doe", sources, ocr_text=chr(0x200B)
    )
    assert result.document.ocr_text is None
    assert not result.ocr_text_populated


def test_supplied_ocr_text_is_stored_without_invisible_padding(conn, tmp_path, sources):
    """Normalise what is *stored*, not just what is rejected (#87)."""
    src = _make_file(tmp_path, "scan2.txt", b"another scan")
    result = ingest.ingest_document(
        conn, src, "jane-doe", sources,
        ocr_text=chr(0xFEFF) + "  Sodium 140 mmol/L  " + chr(0x200B),
    )
    assert result.document.ocr_text == "Sodium 140 mmol/L"


# One emptiness notion per column: the *extracted* routes (`--ocr auto`) must answer
# "is this empty?" the same way the supplied-text route does, or the issue's headline
# symptom survives on an unenumerated route (#87 audit bounce).


def test_extracted_zero_width_only_text_is_no_text_at_all(conn, tmp_path, sources):
    """The `--ocr auto` native route: a file of nothing but one U+200B is empty."""
    src = _make_file(tmp_path, "zwsp.txt", chr(0x200B).encode("utf-8"))
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert result.document.ocr_text is None
    assert not result.ocr_text_populated


def test_extracted_text_is_stored_without_invisible_padding(conn, tmp_path, sources):
    src = _make_file(
        tmp_path, "padded.txt",
        (chr(0xFEFF) * 2 + " Sodium 140 mmol/L " + chr(0x200B)).encode("utf-8"),
    )
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert result.document.ocr_text == "Sodium 140 mmol/L"


def test_run_ocr_zero_width_only_tesseract_output_is_none(tmp_path, monkeypatch):
    """tesseract on a blank page can emit invisibles; that is no text, not text."""
    monkeypatch.setattr(ingest.shutil, "which", lambda _: "/usr/bin/tesseract")
    monkeypatch.setattr(
        ingest.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout=chr(0x200B) + "\n" + chr(0xFEFF), stderr=""
        ),
    )
    assert ingest.run_ocr(_make_file(tmp_path, "scan.jpg", b"jpeg")) is None


def test_ocr_pdf_zero_width_text_layer_does_not_clear_the_threshold(
    tmp_path, monkeypatch
):
    """A text layer of 60 zero-width characters used to clear
    `PDF_TEXT_LAYER_MIN_CHARS` and suppress the tesseract fallback (#87)."""
    pages = [_FakePage(text=chr(0x200B) * 60, scanned="the labs table")]
    _install_backend(monkeypatch, _FakeBackend(_FakeDoc(pages)))
    _install_tesseract(monkeypatch)

    assert ingest.run_ocr(_pdf(tmp_path)) == "the labs table"
    assert pages[0].pixmap_kwargs is not None


def test_ocr_pdf_all_invisible_pages_yield_no_text(tmp_path, monkeypatch):
    pages = [_FakePage(text=chr(0xFEFF), scanned=chr(0x200B))]
    _install_backend(monkeypatch, _FakeBackend(_FakeDoc(pages)))
    _install_tesseract(monkeypatch)

    assert ingest.run_ocr(_pdf(tmp_path)) is None


# --- structured text vs. the #61 identity anchor (issue #66 audit bounce) -------
#
# Native extraction made text available for `.csv`/`.docx`/`.xlsx`/`.json`, and that text
# flows into the owner check. `_ANCHOR` was tuned for scanned headers where `Patient:`
# precedes a printed name; in a lab export those same words are *column labels*, so
# trusting them refused files that ingested fine before this issue. The anchor→`suspect`
# inference is therefore route-scoped; `match`/`mismatch` are not.


_HEADER_ROW = "Patient ID,Test,Value,Unit\n1043,Glucose,98,mg/dL\n"


def test_check_owner_anchor_trust_is_route_scoped():
    text = "Patient: SMITH, JANE A    DOB: 03/14/1900"
    assert ingest.check_owner(text, JANE, ROSTER).verdict == "suspect"
    assert ingest.check_owner(
        text, JANE, ROSTER, trust_anchors=False
    ).verdict == "unverified"


@pytest.mark.parametrize(
    "text, verdict, matched",
    [
        ("Patient: DOE, JANE   DOB: 03/14/1962", "match", "jane-doe"),
        ("Patient: ROE, ROBERT ALAN", "mismatch", "bob-roe"),
    ],
)
def test_affirmative_verdicts_survive_untrusted_anchors(text, verdict, matched):
    """Only `suspect` is route-scoped: an actual name/DOB is evidence on any route."""
    check = ingest.check_owner(text, JANE, ROSTER, trust_anchors=False)
    assert check.verdict == verdict
    assert check.matched_slug == matched


@pytest.mark.parametrize("kind", ["csv", "docx", "xlsx", "json"])
def test_structured_headers_do_not_refuse_the_ingest(conn, tmp_path, sources, kind):
    """A `Patient ID` column (or a `patient` JSON key) is a schema, not an identity
    claim — these all ingested exit-0 before native extraction existed and must still."""
    _seed_roster(conn)
    if kind == "docx":
        src = _make_docx(tmp_path, paragraphs=("Patient chart summary", "Ferritin 201"))
    elif kind == "xlsx":
        src = _make_xlsx(tmp_path, rows=(("Patient ID", "Test"), ("1043", "Glucose")))
    elif kind == "json":
        src = _make_file(tmp_path, "export.json", b'{"patient": 1043, "dob": null}')
    else:
        src = _make_file(tmp_path, "labs.csv", _HEADER_ROW.encode("utf-8"))
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert result.status == "new"
    assert result.owner_check.verdict == "unverified"
    assert result.owner_check.blocks is False
    assert result.ocr_text_populated


@pytest.mark.parametrize("name", ["transcript.txt", "note.md", "panel.tsv", "app.log"])
def test_plaintext_suffixes_are_route_scoped_too(conn, tmp_path, sources, name):
    """The scope line is the *route*, not how prose-like the suffix is: a foreign
    identity header in a natively-read `.txt`/`.md`/`.tsv`/`.log` yields `unverified`,
    not `suspect`. Not a regression — before native extraction these went to tesseract,
    which declined, so there was no text and no check either — but it is the behavior
    `AGENTS.md` §3 documents, so pin it rather than let it drift silently."""
    _seed_roster(conn)
    src = _make_file(
        tmp_path, name, b"MERCY LABS\nPatient: SMITH, KAREN\nDOB: 09/09/1971\n"
    )
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert result.owner_check.verdict == "unverified"
    assert result.status == "new"
    # ...and the protective half still fires on the same route.
    other = _make_file(tmp_path, f"other-{name}", b"Patient: ROE, ROBERT ALAN\n")
    with pytest.raises(ingest.OwnerMismatchError) as excinfo:
        ingest.ingest_document(conn, other, "jane-doe", sources, ocr=True)
    assert excinfo.value.check.verdict == "mismatch"


def test_structured_text_still_refuses_another_roster_person(conn, tmp_path, sources):
    """The half of #61 that actually protects against a misfile is untouched: an
    affirmative match on a different roster person blocks on the native route too."""
    _seed_roster(conn)
    src = _make_docx(tmp_path, paragraphs=("Patient: ROE, ROBERT ALAN", "Ferritin 201"))
    with pytest.raises(ingest.OwnerMismatchError) as excinfo:
        ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert excinfo.value.check.verdict == "mismatch"
    assert excinfo.value.check.matched_slug == "bob-roe"
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 0


def test_ocr_route_still_produces_suspect(conn, tmp_path, sources, monkeypatch):
    """Scoping the anchor to prose must not disarm #61 on the route it was built for."""
    _seed_roster(conn)
    monkeypatch.setattr(
        ingest, "run_ocr", lambda _: "Patient: SMITH, KAREN    DOB: 09/09/1971"
    )
    src = _make_file(tmp_path, "scan.png", b"\x89PNG pretend scan")
    with pytest.raises(ingest.OwnerMismatchError) as excinfo:
        ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert excinfo.value.check.verdict == "suspect"


# --- extraction size cap (issue #66 audit) -------------------------------------
#
# `ocr_text` lands in the row *and* the FTS index, so an unbounded read is a DB-size
# problem and a decompression-bomb surface (a small `.docx` can declare a ~1 GB
# `word/document.xml`). Over the cap degrades like any other extraction failure.


@pytest.mark.parametrize("kind", ["docx", "xlsx", "txt", "ccda"])
def test_oversized_extraction_degrades_instead_of_reading_it(
    conn, tmp_path, sources, capsys, monkeypatch, kind
):
    monkeypatch.setattr(ingest, "_MAX_EXTRACT_BYTES", 16)
    if kind == "docx":
        src = _make_docx(tmp_path, paragraphs=("a" * 500,))
    elif kind == "xlsx":
        src = _make_xlsx(tmp_path)
    elif kind == "ccda":
        # over the cap degrades *native* with the cap note, rather than falling
        # through to a tesseract failure that says nothing useful
        src = _make_ccda(tmp_path)
    else:
        src = _make_file(tmp_path, "big.txt", b"x" * 500)
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert result.status == "new"           # the document still lands
    assert result.document.ocr_text is None
    assert "extraction cap" in capsys.readouterr().err


def test_extraction_cap_is_one_budget_shared_across_the_archive(
    conn, tmp_path, sources, capsys, monkeypatch
):
    """Many members, none individually over the cap, must not add up past it — the
    per-member check alone would let a 40-sheet workbook spend the budget 40 times."""
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    def sheet(text):
        return (f'<worksheet xmlns="{ns}"><sheetData><row>'
                f'<c t="inlineStr"><is><t>{text}</t></is></c>'
                "</row></sheetData></worksheet>")
    src = tmp_path / "many.xlsx"
    members = ("xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml")
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr(members[0], sheet("a" * 200))
        zf.writestr(members[1], sheet("b" * 200))
    with zipfile.ZipFile(src) as zf:
        sizes = [zf.getinfo(name).file_size for name in members]
    assert ingest.extract_text(src) is not None   # fine under the real cap

    # A cap each member clears on its own, but the pair does not.
    monkeypatch.setattr(ingest, "_MAX_EXTRACT_BYTES", max(sizes))
    result = ingest.ingest_document(conn, src, "jane-doe", sources, ocr=True)
    assert result.status == "new"
    assert result.document.ocr_text is None
    assert "sheet2.xml" in capsys.readouterr().err


# --- re-extraction against a stored blob (issue #143) --------------------------
#
# `reocr_documents` is a selector plus a policy layer over `extract_text_routed` and
# `documents.set_document_text`. These tests exercise the policy; the extraction fakes
# above stand in for PyMuPDF/tesseract, so the CI contract (neither installed) holds.


def _file_document(conn, tmp_path, sources, name="scan.txt", content=b"stored blob",
                   person="jane-doe", ocr_text=None, force=False):
    """Ingest a real blob into `sources` and return its `document` row."""
    src = _make_file(tmp_path, name, content)
    result = ingest.ingest_document(
        conn, src, person, sources, ocr_text=ocr_text, force=force
    )
    return result.document


def _stored_text(conn, document_id):
    return conn.execute(
        "SELECT ocr_text FROM document WHERE document_id = ?", (document_id,)
    ).fetchone()["ocr_text"]


def _forbid_extraction(monkeypatch):
    """Fail loudly if extraction runs at all - proves a skip happened before the work."""
    def boom(path):
        raise AssertionError(f"extraction must not run: {path}")
    monkeypatch.setattr(ingest, "extract_text_routed", boom)
    monkeypatch.setattr(ingest, "pdf_page_count", boom)


def test_reocr_fills_an_empty_document_from_its_stored_blob(conn, tmp_path, sources):
    doc = _file_document(conn, tmp_path, sources, content=b"acute pericarditis noted")
    assert doc.ocr_text is None

    (result,) = ingest.reocr_documents(conn, [doc.document_id], sources)

    assert result.status == "written"
    assert result.route == "native"
    assert result.previous_chars == 0
    assert result.chars == len("acute pericarditis noted")
    assert _stored_text(conn, doc.document_id) == "acute pericarditis noted"


def test_reocr_refuses_a_populated_document_without_force(
    conn, tmp_path, sources, monkeypatch
):
    doc = _file_document(conn, tmp_path, sources, ocr_text="human transcription")
    _forbid_extraction(monkeypatch)

    (result,) = ingest.reocr_documents(conn, [doc.document_id], sources)

    assert result.status == "has-text"
    assert result.previous_chars == len("human transcription")
    assert _stored_text(conn, doc.document_id) == "human transcription"


def test_reocr_force_replaces_existing_text(conn, tmp_path, sources):
    doc = _file_document(
        conn, tmp_path, sources, content=b"machine text", ocr_text="old transcription"
    )

    (result,) = ingest.reocr_documents(conn, [doc.document_id], sources, force=True)

    assert result.status == "written"
    assert result.previous_chars == len("old transcription")
    assert _stored_text(conn, doc.document_id) == "machine text"


def test_reocr_dry_run_reports_chars_and_writes_nothing(conn, tmp_path, sources):
    doc = _file_document(conn, tmp_path, sources, content=b"twenty four characters!!")

    (result,) = ingest.reocr_documents(conn, [doc.document_id], sources, dry_run=True)

    assert result.status == "would-write"
    assert result.chars == 24
    assert _stored_text(conn, doc.document_id) is None


def test_reocr_dry_run_names_a_pdf_over_the_page_cap(
    conn, tmp_path, sources, monkeypatch
):
    doc = _file_document(
        conn, tmp_path, sources, name="long.pdf", content=b"%PDF-1.4 fake bytes"
    )
    pages = [
        _FakePage(text="page text long enough to skip ocr entirely")
        for _ in range(21)
    ]
    _install_backend(monkeypatch, _FakeBackend(_FakeDoc(pages)))
    _install_tesseract(monkeypatch)

    (result,) = ingest.reocr_documents(conn, [doc.document_id], sources, dry_run=True)

    assert result.status == "would-write"
    assert result.pages == 21
    assert result.truncated is True
    assert _stored_text(conn, doc.document_id) is None


def test_reocr_under_the_page_cap_is_not_flagged_as_truncated(
    conn, tmp_path, sources, monkeypatch
):
    doc = _file_document(
        conn, tmp_path, sources, name="short.pdf", content=b"%PDF-1.4 fake bytes"
    )
    pages = [
        _FakePage(text="page text long enough to skip ocr entirely") for _ in range(3)
    ]
    _install_backend(monkeypatch, _FakeBackend(_FakeDoc(pages)))
    _install_tesseract(monkeypatch)

    (result,) = ingest.reocr_documents(conn, [doc.document_id], sources)

    assert result.pages == 3
    assert result.truncated is False


def test_reocr_routes_a_pdf_through_the_same_dispatch_as_ingest(
    conn, tmp_path, sources, monkeypatch
):
    """The anti-drift guarantee: re-run text is `extract_text_routed`'s text, because
    it *is* `extract_text_routed` - not a second extraction path that can rot apart."""
    doc = _file_document(
        conn, tmp_path, sources, name="mixed.pdf", content=b"%PDF-1.4 fake bytes"
    )
    pages = [
        _FakePage(text="searchable page with a real embedded text layer"),
        _FakePage(text="", scanned="rasterized page read by tesseract"),
    ]
    _install_backend(monkeypatch, _FakeBackend(_FakeDoc(pages)))
    _install_tesseract(monkeypatch)
    blob = sources / doc.source_path
    expected_text, expected_route = ingest.extract_text_routed(blob)

    (result,) = ingest.reocr_documents(conn, [doc.document_id], sources)

    assert result.route == expected_route == "ocr"
    assert _stored_text(conn, doc.document_id) == expected_text
    assert "searchable page" in expected_text and "rasterized page" in expected_text


def test_reocr_reports_an_owner_mismatch_and_writes_nothing(conn, tmp_path, sources):
    _seed_roster(conn)
    doc = _file_document(conn, tmp_path, sources, content=b"Patient: ROE, ROBERT ALAN")

    (result,) = ingest.reocr_documents(conn, [doc.document_id], sources)

    assert result.status == "owner-mismatch"
    assert result.owner_check.verdict == "mismatch"
    assert result.owner_check.matched_slug == "bob-roe"
    assert _stored_text(conn, doc.document_id) is None


def test_reocr_force_stores_despite_an_owner_mismatch(conn, tmp_path, sources):
    _seed_roster(conn)
    doc = _file_document(conn, tmp_path, sources, content=b"Patient: ROE, ROBERT ALAN")

    (result,) = ingest.reocr_documents(conn, [doc.document_id], sources, force=True)

    assert result.status == "written"
    assert result.owner_check.verdict == "mismatch"
    assert _stored_text(conn, doc.document_id) == "Patient: ROE, ROBERT ALAN"


def test_reocr_stores_and_warns_on_a_suspect_verdict(
    conn, tmp_path, sources, monkeypatch
):
    """The recorded design call: unlike ingest, a `suspect` verdict does not refuse.

    The document is already filed under that owner, so withholding the text does not
    un-file it - it only keeps the document invisible to `find`, which is the bug this
    verb exists to close. The text names nobody on the roster, so nothing leaks across
    household members either.
    """
    _seed_roster(conn)
    # `.png`, not `.txt`: `suspect` is only reachable on the OCR route, where a
    # `Patient:` header is a printed identity claim rather than a column label.
    doc = _file_document(conn, tmp_path, sources, name="scan.png", content=b"pixels")
    monkeypatch.setattr(ingest.shutil, "which", lambda _: "/usr/bin/tesseract")
    monkeypatch.setattr(
        ingest.subprocess, "run",
        _fake_tesseract(b"Patient: SMITH, KAREN  DOB: 09/09/1971"),
    )

    (result,) = ingest.reocr_documents(conn, [doc.document_id], sources)

    assert result.status == "written"
    assert result.route == "ocr"
    assert result.owner_check.verdict == "suspect"
    assert result.owner_check.evidence
    assert _stored_text(conn, doc.document_id).startswith("Patient: SMITH")


def test_reocr_native_route_does_not_trust_anchors(conn, tmp_path, sources):
    """A `Patient ID` column header is a label, not an identity claim - the same
    `trust_anchors` rule ingest applies, reached through the same route value."""
    _seed_roster(conn)
    doc = _file_document(
        conn, tmp_path, sources, name="labs.csv",
        content=b"Patient ID,Test,Value\n1,HbA1c,5.7\n",
    )

    (result,) = ingest.reocr_documents(conn, [doc.document_id], sources)

    assert result.route == "native"
    assert result.status == "written"
    assert result.owner_check.verdict == "unverified"


def test_reocr_reports_a_missing_blob_without_writing(conn, tmp_path, sources):
    doc = _file_document(conn, tmp_path, sources, content=b"about to vanish")
    (sources / doc.source_path).unlink()

    (result,) = ingest.reocr_documents(conn, [doc.document_id], sources)

    assert result.status == "missing-blob"
    assert result.blob_path and doc.sha256 in result.blob_path
    assert _stored_text(conn, doc.document_id) is None


def test_reocr_skips_a_study_blob(conn, tmp_path, sources, monkeypatch):
    """A study's ocr_text is a DICOM-header summary, not text extracted from the blob -
    re-deriving it is a different pipeline, so `reocr` reports and leaves it alone."""
    doc = _file_document(conn, tmp_path, sources, content=b"packed study stand-in")
    with conn:
        conn.execute(
            "UPDATE document SET source_path = ? WHERE document_id = ?",
            (f"aa/{doc.sha256}{ingest.STUDY_EXT}", doc.document_id),
        )
    _forbid_extraction(monkeypatch)

    (result,) = ingest.reocr_documents(conn, [doc.document_id], sources)

    assert result.status == "study-blob"
    assert _stored_text(conn, doc.document_id) is None


def test_reocr_recovering_nothing_is_a_warning_not_an_error(conn, tmp_path, sources):
    doc = _file_document(conn, tmp_path, sources, name="blank.txt", content=b"   \n")

    (result,) = ingest.reocr_documents(conn, [doc.document_id], sources)

    assert result.status == "no-text"
    assert result.refused is False       # an expected sweep outcome, not a refusal
    assert _stored_text(conn, doc.document_id) is None


def test_reocr_unknown_document_id_raises_before_any_work(conn, tmp_path, sources):
    doc = _file_document(conn, tmp_path, sources, content=b"never touched")

    with pytest.raises(ingest.IngestError) as excinfo:
        ingest.reocr_documents(conn, [doc.document_id, 999], sources)

    assert "999" in str(excinfo.value)
    # A typo in one id must not half-run the sweep.
    assert _stored_text(conn, doc.document_id) is None


def test_reocr_sweeps_several_documents_independently(conn, tmp_path, sources):
    empty = _file_document(conn, tmp_path, sources, name="a.txt", content=b"recovered")
    populated = _file_document(
        conn, tmp_path, sources, name="b.txt", content=b"other bytes",
        ocr_text="already transcribed",
    )

    results = ingest.reocr_documents(
        conn, [empty.document_id, populated.document_id], sources
    )

    assert [r.status for r in results] == ["written", "has-text"]
    # Each write is its own transaction, so a refusal later in the list cannot roll
    # back an earlier success.
    assert _stored_text(conn, empty.document_id) == "recovered"
    assert _stored_text(conn, populated.document_id) == "already transcribed"


def test_reocr_repairs_an_invisible_only_row_without_force(conn, tmp_path, sources):
    """Issue #87's invisible-character rows are part of the target population: they
    read as populated to raw truthiness and as empty to `normalize_document_text`."""
    doc = _file_document(conn, tmp_path, sources, content=b"real text at last")
    with conn:
        conn.execute(
            "UPDATE document SET ocr_text = ? WHERE document_id = ?",
            ("​﻿", doc.document_id),
        )

    (result,) = ingest.reocr_documents(conn, [doc.document_id], sources)

    assert result.status == "written"
    assert result.previous_chars == 0
    assert _stored_text(conn, doc.document_id) == "real text at last"


def test_reocr_on_an_unmigrated_db_raises(tmp_path, sources):
    raw = db.connect(tmp_path / "empty.db")
    try:
        with pytest.raises(db.NotMigratedError):
            ingest.reocr_documents(raw, [1], sources)
    finally:
        raw.close()


def test_pdf_page_count_counts_pages_and_degrades_to_none(tmp_path, monkeypatch):
    src = _pdf(tmp_path)
    _install_backend(monkeypatch, _FakeBackend(_FakeDoc([_FakePage(), _FakePage()])))
    assert ingest.pdf_page_count(src) == 2

    # Not a PDF -> not a question this function answers.
    assert ingest.pdf_page_count(_make_file(tmp_path, "notes.txt")) is None

    # Encrypted and unreadable are both "don't know", never a raise: a caller may only
    # claim truncation on positive evidence.
    _install_backend(
        monkeypatch, _FakeBackend(_FakeDoc([_FakePage()], needs_pass=True))
    )
    assert ingest.pdf_page_count(src) is None
    _install_backend(monkeypatch, _FakeBackend(error=RuntimeError("broken pdf")))
    assert ingest.pdf_page_count(src) is None

    # Backend absent (no `pemr[ocr]` extra) -> also None, no note, no crash.
    _install_backend(monkeypatch, None)
    assert ingest.pdf_page_count(src) is None

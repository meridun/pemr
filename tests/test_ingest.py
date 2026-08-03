"""Document ingest: hashing, content-addressed blob store, layer-1 dedup."""

import sqlite3
import subprocess

import pytest

from pemr import db, ingest, persons
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
    result = ingest.ingest_document(
        conn, _make_file(tmp_path), "jane-doe", sources, ocr=True
    )
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
        "Patient: DOE, JOHN Q   DOB: 25/4/1978",   # day-first, unpadded
        "Patient: DOE, JOHN Q   DOB: 15/4/1978",
        "Patient: DOE, JOHN Q   accession 15/4/1978x",
        "Patient: DOE, JOHN Q   DOB: 05/04/19780",  # trailing digit
    ],
)
def test_dob_needs_digit_boundaries(text):
    """A candidate rendering that is merely a *substring* of a longer digit run is not
    a DOB match — otherwise a day-first `25/4/1978` silently verifies a 1978-05-04
    person and the document is misfiled with an `owner verified` line to reassure."""
    mike = Person(
        person_id=9, slug="michael-dickinson", full_name="Michael Dickinson",
        dob="1978-05-04",
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

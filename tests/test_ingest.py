"""Document ingest: hashing, content-addressed blob store, layer-1 dedup."""

import json
import sqlite3
import zipfile

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
    # a scan (image suffix) is the tesseract branch of ingest.extract_text; text and
    # OOXML suffixes are read natively and never reach the binary.
    scan = _make_file(tmp_path, "scan.png", b"\x89PNG not really")
    result = ingest.ingest_document(conn, scan, "jane-doe", sources, ocr=True)
    assert result.status == "new"
    assert result.document.ocr_text is None
    assert "tesseract" in capsys.readouterr().err


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


def _make_docx(tmp_path, name="note.docx", paragraphs=("HbA1c 5.7 percent",)):
    body = "".join(
        f"<w:p><w:r><w:t>{part}</w:t></w:r></w:p>" for part in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
        f'2006/main"><w:body>{body}</w:body></w:document>'
    )
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", document)
    return p


def _make_xlsx(tmp_path, name="labs.xlsx", rows=(("test", "value"), ("sodium", "140"))):
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
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("xl/sharedStrings.xml", shared)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
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


def test_supplied_ocr_text_still_beats_extraction(conn, tmp_path, sources):
    src = _make_docx(tmp_path, paragraphs=("extracted body",))
    result = ingest.ingest_document(
        conn, src, "jane-doe", sources, ocr=True, ocr_text="agent transcription"
    )
    assert result.document.ocr_text == "agent transcription"


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


@pytest.mark.parametrize("kind", ["docx", "xlsx", "txt"])
def test_oversized_extraction_degrades_instead_of_reading_it(
    conn, tmp_path, sources, capsys, monkeypatch, kind
):
    monkeypatch.setattr(ingest, "_MAX_EXTRACT_BYTES", 16)
    if kind == "docx":
        src = _make_docx(tmp_path, paragraphs=("a" * 500,))
    elif kind == "xlsx":
        src = _make_xlsx(tmp_path)
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

"""End-to-end CLI wiring for phase 2: ingest -> commit-extraction -> review-conflicts."""

import json
import zipfile

import pytest

from pemr import cli, db


def _run(tmp_path, *argv):
    return cli.main(["--db", str(tmp_path / "cli.db"), *argv])


def _write_json(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


@pytest.fixture()
def ready(tmp_path):
    """Migrated DB + a person, ready for ingest."""
    assert _run(tmp_path, "migrate", "--create") == 0
    assert _run(tmp_path, "person", "add", "--slug", "jane-doe", "--name", "Jane Doe") == 0
    return tmp_path


def _document_id(tmp_path):
    conn = db.connect(tmp_path / "cli.db")
    try:
        return conn.execute("SELECT document_id FROM document ORDER BY document_id").fetchall()
    finally:
        conn.close()


def test_ingest_commit_review_roundtrip(ready, capsys):
    tmp_path = ready
    scan = tmp_path / "scan.txt"
    scan.write_bytes(b"hba1c 5.7 percent")
    sources = tmp_path / "sources"

    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(sources)) == 0
    out = capsys.readouterr().out
    assert "ingested document #1" in out

    labs = _write_json(tmp_path, "extract.json",
                       {"lab_result": [{"test_name": "HbA1c", "collected_at": "2026-01-02",
                                        "value_num": 5.7, "unit": "%"}]})
    assert _run(tmp_path, "commit-extraction", "--document", "1", "--json", str(labs)) == 0
    assert "1 new" in capsys.readouterr().out

    # a second document with a corrected value -> staged conflict
    scan2 = tmp_path / "scan2.txt"
    scan2.write_bytes(b"hba1c 6.2 percent")
    assert _run(tmp_path, "ingest", str(scan2), "--person", "jane-doe",
                "--sources", str(sources)) == 0
    capsys.readouterr()
    labs2 = _write_json(tmp_path, "extract2.json",
                        {"lab_result": [{"test_name": "A1c", "collected_at": "2026-01-02",
                                         "value_num": 6.2, "unit": "%"}]})
    assert _run(tmp_path, "commit-extraction", "--document", "2", "--json", str(labs2)) == 0
    assert "1 conflict" in capsys.readouterr().out

    assert _run(tmp_path, "review-conflicts") == 0
    assert "lab_result" in capsys.readouterr().out

    assert _run(tmp_path, "review-conflicts", "--resolve", "1", "--keep", "incoming") == 0
    assert "resolved conflict #1" in capsys.readouterr().out


def _stage_repeat_draw(tmp_path, capsys):
    """The issue #58 repro through the CLI: two genuine same-day draws, one document
    each (a single submission carrying both is now rejected up front)."""
    sources = tmp_path / "sources"
    for i, (value, text) in enumerate(((95, "fasting"), (148, "post-prandial")), start=1):
        scan = tmp_path / f"g{i}.txt"
        scan.write_bytes(f"glucose {value}".encode())
        assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                    "--sources", str(sources)) == 0
        payload = _write_json(tmp_path, f"g{i}.json", {"lab_result": [
            {"test_name": "glucose", "collected_at": "2024-04-01",
             "value_num": value, "value_text": text},
        ]})
        assert _run(tmp_path, "commit-extraction", "--document", str(i),
                    "--json", str(payload)) == 0
    capsys.readouterr()


def test_review_conflicts_keep_both_admits_the_repeat(ready, capsys):
    tmp_path = ready
    _stage_repeat_draw(tmp_path, capsys)

    assert _run(tmp_path, "review-conflicts", "--resolve", "1", "--keep", "both",
                "--note", "Jane confirms two draws") == 0
    out = capsys.readouterr().out
    assert "resolved conflict #1 (keep-both -> lab_result #2, occurrence 1)" in out
    assert out.isascii()

    capsys.readouterr()
    assert _run(tmp_path, "query", "labs", "--person", "jane-doe", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert [r["value_num"] for r in payload] == [95, 148]   # both queryable, in row order
    for row in payload:                                      # internals stay internal
        assert not {"dedup_key", "dedup_base", "dedup_occurrence"} & set(row)


def test_review_conflicts_listing_shows_occurrences_and_the_both_hint(ready, capsys):
    tmp_path = ready
    _stage_repeat_draw(tmp_path, capsys)
    assert _run(tmp_path, "review-conflicts") == 0
    out = capsys.readouterr().out
    assert "--keep existing|incoming|both" in out
    assert "occurrences:" not in out          # family of 1 -> nothing to say yet

    assert _run(tmp_path, "review-conflicts", "--resolve", "1", "--keep", "both") == 0
    scan = tmp_path / "g3.txt"
    scan.write_bytes(b"glucose 210")
    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources")) == 0
    payload = _write_json(tmp_path, "g3.json", {"lab_result": [
        {"test_name": "glucose", "collected_at": "2024-04-01", "value_num": 210},
    ]})
    assert _run(tmp_path, "commit-extraction", "--document", "3",
                "--json", str(payload)) == 0
    capsys.readouterr()

    assert _run(tmp_path, "review-conflicts") == 0
    out = capsys.readouterr().out
    assert "occurrences: 2 rows already stored under this key" in out
    assert out.isascii()


def test_keep_both_after_a_rekey_does_not_wedge_rekey(ready, capsys):
    """The audit repro: a dictionary edit + `rekey --apply` while a conflict is open
    leaves the conflict on a stale key. Resolving `--keep both` off that key inserted a
    row whose key no longer derives from its own columns, which made every later `rekey`
    - for every table - fail with a collision and left `document reassign` with no exit.
    """
    tmp_path = ready
    _stage_repeat_draw(tmp_path, capsys)
    dictionary = tmp_path / "dict.toml"
    dictionary.write_text('[synonyms]\n"glucose" = "glucose, plasma"\n', encoding="utf-8")

    assert _run(tmp_path, "rekey", "--apply", "--dictionary", str(dictionary)) == 0
    assert "rekeyed 1 row(s)" in capsys.readouterr().out

    assert _run(tmp_path, "review-conflicts", "--resolve", "1", "--keep", "both",
                "--dictionary", str(dictionary)) == 0
    assert "occurrence 1" in capsys.readouterr().out      # joined the live family

    assert _run(tmp_path, "rekey", "--dictionary", str(dictionary)) == 0
    assert "all dedup keys already match" in capsys.readouterr().out

    conn = db.connect(tmp_path / "cli.db")
    try:
        rows = conn.execute(
            "SELECT * FROM lab_result ORDER BY lab_result_id"
        ).fetchall()
    finally:
        conn.close()
    assert [r["value_num"] for r in rows] == [95, 148]
    assert rows[0]["dedup_base"] == rows[1]["dedup_base"]


def test_commit_extraction_rejects_an_intra_payload_collision(ready, capsys):
    tmp_path = ready
    scan = tmp_path / "g.txt"
    scan.write_bytes(b"glucose x2")
    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources")) == 0
    payload = _write_json(tmp_path, "g.json", {"lab_result": [
        {"test_name": "glucose", "collected_at": "2024-04-01", "value_num": 95},
        {"test_name": "glucose", "collected_at": "2024-04-01", "value_num": 148},
    ]})
    capsys.readouterr()
    assert _run(tmp_path, "commit-extraction", "--document", "1",
                "--json", str(payload)) == 1
    err = capsys.readouterr().err
    assert "rows 0 and 1" in err and "--keep both" in err
    assert err.isascii()


def test_ingest_duplicate_reports_cleanly(ready, capsys):
    tmp_path = ready
    scan = tmp_path / "s.txt"
    scan.write_bytes(b"same")
    sources = tmp_path / "sources"
    _run(tmp_path, "ingest", str(scan), "--person", "jane-doe", "--sources", str(sources))
    capsys.readouterr()
    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(sources)) == 0
    assert "duplicate" in capsys.readouterr().out


def test_ingest_on_unmigrated_db_is_friendly(tmp_path, capsys, unmigrated_db):
    # A DB file that exists but has no schema. Issue #55 made this distinct from "no DB
    # file at all", which the missing-database gate refuses earlier and differently.
    unmigrated_db(tmp_path / "cli.db")
    scan = tmp_path / "s.txt"
    scan.write_bytes(b"x")
    rc = _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
              "--sources", str(tmp_path / "sources"))
    assert rc == 1
    assert "migrate" in capsys.readouterr().err


# --- owner verification at ingest (issue #61) ---------------------------------

def _ocr_file(tmp_path, text):
    p = tmp_path / "ocr.txt"
    p.write_text(text, encoding="utf-8")
    return p


def test_ingest_refuses_a_document_naming_someone_else(ready, capsys):
    tmp_path = ready
    scan = tmp_path / "wrong-owner.txt"
    scan.write_bytes(b"a summary for a different patient")
    ocr = _ocr_file(tmp_path, "Patient: SMITH, KAREN A    DOB: 09/09/1971")

    rc = _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
              "--sources", str(tmp_path / "sources"), "--ocr-text-file", str(ocr))
    assert rc == 1
    err = capsys.readouterr().err
    assert "owner verification failed" in err
    assert "SMITH, KAREN A" in err  # the evidence window, for human adjudication
    assert "--force" in err
    assert not _document_id(tmp_path)  # pre-write refusal: nothing landed


def test_ingest_force_overrides_the_refusal(ready, capsys):
    tmp_path = ready
    scan = tmp_path / "forced.txt"
    scan.write_bytes(b"forced anyway")
    ocr = _ocr_file(tmp_path, "Patient: SMITH, KAREN A    DOB: 09/09/1971")

    rc = _run(tmp_path, "ingest", str(scan), "--person", "jane-doe", "--force",
              "--sources", str(tmp_path / "sources"), "--ocr-text-file", str(ocr))
    assert rc == 0
    out = capsys.readouterr()
    assert "ingested document #1" in out.out
    assert "--force" in out.err and "reassign" in out.err  # points at the undo path
    assert len(_document_id(tmp_path)) == 1


def test_ingest_reports_a_verified_owner(ready, capsys):
    tmp_path = ready
    scan = tmp_path / "hers.txt"
    scan.write_bytes(b"her own labs")
    ocr = _ocr_file(tmp_path, "Patient Name: DOE, JANE\nSodium 140 mmol/L")

    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources"),
                "--ocr-text-file", str(ocr)) == 0
    assert "owner verified" in capsys.readouterr().out


def test_ingest_without_text_notes_the_owner_was_not_verified(ready, capsys):
    tmp_path = ready
    scan = tmp_path / "untexted.txt"
    scan.write_bytes(b"no text supplied")
    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources")) == 0
    assert "owner not verified" in capsys.readouterr().err


# --- --ocr-text-file reading (issue #78) --------------------------------------
# Same helper backs `document set-text`; its half of these lives in test_documents.py.

def test_ingest_non_utf8_ocr_text_file_is_a_friendly_error(ready, capsys):
    """`UnicodeDecodeError` is a `ValueError`, not an `OSError` - a PDF or a UTF-16
    file handed to `--ocr-text-file` used to traceback out of `main` (#78)."""
    tmp_path = ready
    scan = tmp_path / "scan.txt"
    scan.write_bytes(b"some labs")
    bad = tmp_path / "scan.pdf"
    bad.write_bytes(b"%PDF-1.4\xff\xfe\x00binary junk")

    rc = _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
              "--sources", str(tmp_path / "sources"), "--ocr-text-file", str(bad))
    assert rc == 1
    err = capsys.readouterr().err
    assert "error: cannot read" in err and "scan.pdf" in err
    assert not _document_id(tmp_path)  # the read fails before anything is written


def test_ingest_bom_only_ocr_text_file_stores_no_ocr_text(ready, capsys):
    """Three bytes of BOM is an empty file, not one character of text (#78):
    U+FEFF survives `str.strip()`, so a `utf-8` read would report ocr_text populated."""
    tmp_path = ready
    scan = tmp_path / "scan.txt"
    scan.write_bytes(b"some labs")
    bom = tmp_path / "bomonly.txt"
    bom.write_bytes(b"\xef\xbb\xbf")

    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources"), "--ocr-text-file", str(bom)) == 0
    assert "no ocr_text stored" in capsys.readouterr().err
    assert _run(tmp_path, "document", "show", "1", "--text") == 0
    shown = capsys.readouterr()
    assert shown.out == "" and "has no ocr_text stored" in shown.err


def test_ingest_zero_width_only_ocr_text_file_stores_no_ocr_text(ready, capsys):
    """One U+200B is not text (#87). No encoding fixes this - `utf-8-sig` has nothing
    to strip - so `.strip()`, which only removes `.isspace()` characters, let it
    through and reported ocr_text populated on an empty document."""
    tmp_path = ready
    scan = tmp_path / "scan.txt"
    scan.write_bytes(b"some labs")
    zwsp = tmp_path / "zwsp.txt"
    zwsp.write_bytes(chr(0x200B).encode("utf-8"))

    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources"),
                "--ocr-text-file", str(zwsp)) == 0
    assert "no ocr_text stored" in capsys.readouterr().err
    assert _run(tmp_path, "document", "show", "1", "--text") == 0
    shown = capsys.readouterr()
    assert shown.out == "" and "has no ocr_text stored" in shown.err


def test_ingest_doubled_bom_ocr_text_file_stores_no_ocr_text(ready, capsys):
    """`utf-8-sig` strips one BOM; the U+FEFF survivor is not `.isspace()` (#87)."""
    tmp_path = ready
    scan = tmp_path / "scan.txt"
    scan.write_bytes(b"some labs")
    bom = tmp_path / "doublebom.txt"
    bom.write_bytes((chr(0xFEFF) * 2).encode("utf-8"))

    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources"),
                "--ocr-text-file", str(bom)) == 0
    assert "no ocr_text stored" in capsys.readouterr().err
    assert _run(tmp_path, "document", "show", "1", "--text") == 0
    shown = capsys.readouterr()
    assert shown.out == "" and "has no ocr_text stored" in shown.err


def test_ingest_strips_a_leading_bom_from_the_ocr_text_file(ready, capsys):
    """A BOM on a real transcription must not inflate the stored text (#78)."""
    tmp_path = ready
    scan = tmp_path / "hers.txt"
    scan.write_bytes(b"her own labs")
    ocr = tmp_path / "bommed.txt"
    ocr.write_bytes(b"\xef\xbb\xbfPatient Name: DOE, JANE\nSodium 140 mmol/L")

    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources"), "--ocr-text-file", str(ocr)) == 0
    capsys.readouterr()
    assert _run(tmp_path, "document", "show", "1", "--text") == 0
    assert capsys.readouterr().out == "Patient Name: DOE, JANE\nSodium 140 mmol/L\n"


def test_commit_bad_json_is_friendly(ready, capsys):
    tmp_path = ready
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert _run(tmp_path, "commit-extraction", "--document", "1", "--json", str(bad)) == 1
    assert "not valid JSON" in capsys.readouterr().err


# --- rekey (dictionary-edit maintenance) --------------------------------------

def _dict_file(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("[synonyms]\n" + body, encoding="utf-8")
    return p


def _seed_lab(ready, tmp_path, dictionary):
    scan = tmp_path / "rekey-scan.txt"
    scan.write_bytes(b"zzt 108")
    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources")) == 0
    doc = _document_id(tmp_path)[-1]["document_id"]
    payload = _write_json(tmp_path, "rekey.json", {"lab_result": [
        {"test_name": "ZZT", "collected_at": "2026-01-02", "value_num": 108},
    ]})
    assert _run(tmp_path, "commit-extraction", "--document", str(doc),
                "--json", str(payload), "--dictionary", str(dictionary)) == 0
    return payload


def test_rekey_dry_run_then_apply(ready, capsys, tmp_path):
    old = _dict_file(tmp_path, "old.toml", '"unrelated" = "unrelated"\n')
    new = _dict_file(tmp_path, "new.toml", '"zzt" = "zonulin_test"\n')
    payload = _seed_lab(ready, tmp_path, old)
    capsys.readouterr()

    assert _run(tmp_path, "rekey", "--dictionary", str(new)) == 0
    out = capsys.readouterr().out
    assert "lab_result: 1/1 key(s) change" in out
    assert "dry run: 1 row(s) would change" in out and "--apply" in out

    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--apply") == 0
    assert "rekeyed 1 row(s)" in capsys.readouterr().out

    # The point of the rekey: the same fact now dedups instead of doubling.
    doc = _document_id(tmp_path)[-1]["document_id"]
    assert _run(tmp_path, "commit-extraction", "--document", str(doc),
                "--json", str(payload), "--dictionary", str(new)) == 0
    assert "0 new, 1 duplicate" in capsys.readouterr().out

    assert _run(tmp_path, "rekey", "--dictionary", str(new)) == 0
    assert "all dedup keys already match" in capsys.readouterr().out


def test_rekey_json_output(ready, capsys, tmp_path):
    old = _dict_file(tmp_path, "old.toml", '"unrelated" = "unrelated"\n')
    new = _dict_file(tmp_path, "new.toml", '"zzt" = "zonulin_test"\n')
    _seed_lab(ready, tmp_path, old)
    capsys.readouterr()

    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False
    assert payload["scanned"]["lab_result"] == 1
    change = payload["changed"][0]
    assert change["label"] == "ZZT" and change["old_key"] != change["new_key"]


def test_rekey_refuses_a_fusing_dictionary(ready, capsys, tmp_path):
    """Exit 1 with a pointed message when the dictionary would merge two facts."""
    old = _dict_file(tmp_path, "old.toml", '"unrelated" = "unrelated"\n')
    new = _dict_file(tmp_path, "fuse.toml", '"alb" = "albumin"\n')
    scan = tmp_path / "fuse-scan.txt"
    scan.write_bytes(b"alb 4.2 / albumin 3.6")
    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources")) == 0
    doc = _document_id(tmp_path)[-1]["document_id"]
    payload = _write_json(tmp_path, "fuse.json", {"lab_result": [
        {"test_name": "ALB", "collected_at": "2026-01-02", "value_num": 4.2},
        {"test_name": "Albumin", "collected_at": "2026-01-02", "value_num": 3.6},
    ]})
    assert _run(tmp_path, "commit-extraction", "--document", str(doc),
                "--json", str(payload), "--dictionary", str(old)) == 0
    capsys.readouterr()

    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--apply") == 1
    err = capsys.readouterr().err
    assert "same dedup_key" in err and "nothing was written" in err


# --- intake formats at the CLI (issue #66) ------------------------------------

def test_ingest_refuses_a_google_drive_pointer_stub(ready, capsys):
    tmp_path = ready
    stub = tmp_path / "budget.gsheet"
    stub.write_text(json.dumps({
        "url": "https://docs.google.com/spreadsheets/d/1AbC_dEf/edit?usp=drivesdk",
        "doc_id": "1AbC_dEf",
        "email": "someone@example.com",
    }), encoding="utf-8")

    rc = _run(tmp_path, "ingest", str(stub), "--person", "jane-doe",
              "--sources", str(tmp_path / "sources"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "pointer stub" in err
    assert "Export it from Drive" in err
    assert not _document_id(tmp_path)  # pre-write refusal: nothing landed


def test_ocr_auto_makes_a_docx_findable(ready, capsys):
    tmp_path = ready
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    doc = tmp_path / "visit.docx"
    with zipfile.ZipFile(doc, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(
            "word/document.xml",
            f'<w:document xmlns:w="{ns}"><w:body>'
            "<w:p><w:r><w:t>total cholesterol 188</w:t></w:r></w:p>"
            "</w:body></w:document>",
        )

    assert _run(tmp_path, "ingest", str(doc), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources"), "--ocr", "auto") == 0
    out = capsys.readouterr()
    assert "ingested document #1" in out.out
    assert "no ocr_text stored" not in out.err

    assert _run(tmp_path, "find", "--person", "jane-doe", "cholesterol") == 0
    assert "#1" in capsys.readouterr().out


def test_ocr_tesseract_is_rejected(ready, capsys):
    """Issue #91: the misleading `tesseract` alias is gone; argparse names `auto`."""
    tmp_path = ready
    csv = tmp_path / "labs.csv"
    csv.write_text("test,value\nferritin,68\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, "ingest", str(csv), "--person", "jane-doe",
             "--sources", str(tmp_path / "sources"), "--ocr", "tesseract")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err
    assert "auto" in err


def test_ocr_help_names_only_auto_and_no_pdf_tesseract_claim(capsys):
    """Issue #91: the alias is undiscoverable and the PDF route is described honestly.

    `--ocr tesseract` was misleading in both directions: the value named after the
    tool never reached it, and the help text claimed PDFs went "via tesseract" when
    the `_ocr_pdf` branch (#70) uses the embedded text layer plus rendered-page OCR.
    """
    with pytest.raises(SystemExit):
        cli.main(["ingest", "--help"])
    # argparse wraps the help block, so compare on collapsed whitespace.
    text = " ".join(capsys.readouterr().out.split())
    assert "--ocr {auto}" in text
    assert "alias" not in text
    assert "images/PDF via tesseract" not in text
    assert "PDF page by page (text layer + rendered-page OCR)" in text


def test_unextractable_format_still_ingests_with_a_note(ready, capsys, monkeypatch):
    from pemr import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "run_ocr", lambda _: None)   # tesseract declines
    tmp_path = ready
    msg = tmp_path / "thread.msg"
    msg.write_bytes(b"\xd0\xcf\x11\xe0 outlook message")

    assert _run(tmp_path, "ingest", str(msg), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources"), "--ocr", "auto") == 0
    err = capsys.readouterr().err
    assert "no text could be extracted" in err
    assert "--ocr-text-file" in err
    assert "no ocr_text stored" in err


def test_ocr_auto_still_ocrs_an_unlisted_image_suffix(ready, capsys, monkeypatch):
    """`.jfif` has no native route and must reach tesseract, not be skipped."""
    from pemr import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "run_ocr", lambda _: "Ferritin 201 nanograms")
    tmp_path = ready
    img = tmp_path / "alpha.jfif"
    img.write_bytes(b"\xff\xd8\xff\xe0 jpeg bytes")

    assert _run(tmp_path, "ingest", str(img), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources"), "--ocr", "auto") == 0
    assert "no ocr_text stored" not in capsys.readouterr().err
    assert _run(tmp_path, "find", "--person", "jane-doe", "ferritin") == 0
    assert "#1" in capsys.readouterr().out


def test_lab_export_with_a_patient_column_is_not_refused(ready, capsys):
    """A `Patient ID` column header is a schema, not an identity claim: making the CSV
    readable must not make it unfilable (issue #66 audit bounce)."""
    tmp_path = ready
    csv = tmp_path / "labs.csv"
    csv.write_text(
        "Patient ID,Test,Value,Unit\n1043,Glucose,98,mg/dL\n", encoding="utf-8"
    )

    assert _run(tmp_path, "ingest", str(csv), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources"), "--ocr", "auto") == 0
    out = capsys.readouterr()
    assert "ingested document #1" in out.out
    assert "owner verification failed" not in out.err

    assert _run(tmp_path, "find", "--person", "jane-doe", "glucose") == 0
    assert "#1" in capsys.readouterr().out
def _stage_pre_006(tmp_path):
    """Copy every migration below 006 into a staging dir (leaving 006 pending)."""
    import shutil

    staged = tmp_path / "pre006"
    staged.mkdir()
    for path in sorted(db.DEFAULT_MIGRATIONS_DIR.glob("*.sql")):
        if path.name < "006":
            shutil.copy(path, staged / path.name)
    return staged


def test_migrate_prints_the_rekey_followup_when_006_moves_rows(tmp_path, capsys):
    """Migration 006 carries the old observation dedup keys forward (SQL cannot compute
    the new sha256 ones), so `migrate` has to name the follow-up or a re-commit of an
    already-stored allergy/condition silently forks a second row (issue #63)."""
    staged = _stage_pre_006(tmp_path)
    assert _run(tmp_path, "migrate", "--create", "--migrations-dir", str(staged)) == 0
    conn = db.connect(tmp_path / "cli.db")
    conn.execute("INSERT INTO person (slug, full_name) VALUES ('jane-doe', 'Jane')")
    conn.execute(
        "INSERT INTO observation (person_id, obs_type, key, dedup_key, dedup_base) "
        "VALUES (1, 'allergy', 'Penicillin', 'k1', 'k1')"
    )
    conn.commit()
    conn.close()
    capsys.readouterr()

    assert _run(tmp_path, "migrate") == 0
    out = capsys.readouterr().out
    assert "applied 006_condition_allergy.sql" in out
    assert "note: run `pemr rekey --apply`" in out and "1 allergy/condition row" in out
    # ... and it is not repeated on a no-op re-run.
    assert _run(tmp_path, "migrate") == 0
    assert capsys.readouterr().out.strip() == "up to date"


def test_migrate_of_a_fresh_database_has_no_rekey_followup(tmp_path, capsys):
    """Nothing moved, nothing to rekey - a note nobody must act on trains people to
    skip notes."""
    assert _run(tmp_path, "migrate", "--create") == 0
    out = capsys.readouterr().out
    assert "applied 006_condition_allergy.sql" in out
    assert "note:" not in out


def test_keep_both_no_op_line_names_the_fields_it_filled(ready, capsys):
    """A keep-both no-op on a sparse type still writes: it fills the matched sibling's
    NULL columns. The operator's line must say so - reporting a clinical write as a bare
    "no-op" is the invisible-write failure the `enriched` bucket exists to prevent
    (issue #63). The persisted resolution already names the fields; the CLI line did not.
    """
    tmp_path = ready
    scan = tmp_path / "a.txt"
    scan.write_bytes(b"allergy list")
    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources")) == 0
    for name, rows in (
        ("a1", [{"substance": "Latex", "reaction": "hives"}]),
        ("a2", [{"substance": "Latex", "reaction": "rash", "criticality": "high"}]),
        ("a3", [{"substance": "Latex", "reaction": "rash"}]),
    ):
        payload = _write_json(tmp_path, f"{name}.json", {"allergy": rows})
        assert _run(tmp_path, "commit-extraction", "--document", "1",
                    "--json", str(payload)) == 0
    capsys.readouterr()

    # Admit the thin "rash" row first, so the rich one then matches a sibling missing
    # exactly the field the promotion was about.
    assert _run(tmp_path, "review-conflicts", "--resolve", "2", "--keep", "both") == 0
    capsys.readouterr()
    assert _run(tmp_path, "review-conflicts", "--resolve", "1", "--keep", "both") == 0
    out = capsys.readouterr().out
    assert "no-op" in out and "filled criticality" in out
    assert out.isascii()

    conn = db.connect(tmp_path / "cli.db")
    try:
        stored = conn.execute(
            "SELECT reaction, criticality FROM allergy WHERE reaction = 'rash'"
        ).fetchone()
    finally:
        conn.close()
    assert (stored["reaction"], stored["criticality"]) == ("rash", "high")

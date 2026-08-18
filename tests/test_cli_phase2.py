"""End-to-end CLI wiring for phase 2: ingest -> commit-extraction -> review-conflicts."""

import hashlib
import json
import zipfile

import pytest

from pemr import cli, curation, db, dedup, render, verify


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
    assert "--keep existing|incoming|both|merge" in out
    assert "occurrences:" not in out          # family of 1 -> nothing to say yet
    assert "--field NAME=existing|incoming" in out

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


# --- keep merge through the CLI (issue #140) ---------------------------------

def _stage_med_refinement(tmp_path, capsys, stored: dict, incoming: dict):
    """The issue's shape end to end: a stored medication row, then a portal export
    that refines some fields and is silent about others -> one open conflict."""
    identity = {"name": "metformin", "dose": "500 mg", "started_on": "2024-01-05"}
    for i, payload in enumerate((stored, incoming), start=1):
        scan = tmp_path / f"m{i}.txt"
        scan.write_bytes(f"metformin {i}".encode())
        assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                    "--sources", str(tmp_path / "sources")) == 0
        doc = _write_json(tmp_path, f"m{i}.json",
                          {"medication": [identity | payload]})
        assert _run(tmp_path, "commit-extraction", "--document", str(i),
                    "--json", str(doc)) == 0
    capsys.readouterr()


def test_review_conflicts_merge_writes_and_names_the_fields(ready, capsys):
    tmp_path = ready
    _stage_med_refinement(
        tmp_path, capsys,
        {"prescriber": "Dr Who", "route": "oral"},
        {"route": "oral", "status": "active"},
    )

    assert _run(tmp_path, "review-conflicts", "--resolve", "1", "--keep", "merge",
                "--note", "Jane confirms the portal list") == 0
    out = capsys.readouterr().out
    assert "resolved conflict #1 (keep-merge -> medication #1" in out
    assert "from incoming: status" in out and "kept: prescriber" in out
    assert "Dr Who" not in out                 # field names only, never values
    assert out.isascii()

    assert _run(tmp_path, "query", "meds", "--person", "jane-doe", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1                   # merged in place, not forked
    assert payload[0]["status"] == "active"    # the refinement landed
    assert payload[0]["prescriber"] == "Dr Who"   # and the silent field survived


def test_review_conflicts_merge_collision_exits_1_and_leaves_it_open(ready, capsys):
    tmp_path = ready
    _stage_med_refinement(
        tmp_path, capsys,
        {"status": "ordered", "prescriber": "Dr Who"},
        {"status": "completed"},
    )

    assert _run(tmp_path, "review-conflicts", "--resolve", "1", "--keep", "merge") == 1
    captured = capsys.readouterr()
    assert "error: " in captured.err and "status" in captured.err
    assert "ordered" not in captured.err and "completed" not in captured.err
    assert captured.err.isascii()

    assert _run(tmp_path, "review-conflicts") == 0
    assert "[open]" in capsys.readouterr().out


def test_review_conflicts_merge_field_override_resolves(ready, capsys):
    tmp_path = ready
    _stage_med_refinement(
        tmp_path, capsys,
        {"status": "ordered", "prescriber": "Dr Who"},
        {"status": "completed"},
    )

    assert _run(tmp_path, "review-conflicts", "--resolve", "1", "--keep", "merge",
                "--field", "status=incoming") == 0
    assert "settled: status=incoming" in capsys.readouterr().out

    assert _run(tmp_path, "query", "meds", "--person", "jane-doe", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert (payload[0]["status"], payload[0]["prescriber"]) == ("completed", "Dr Who")


def test_review_conflicts_malformed_field_argument_exits_1(ready, capsys):
    tmp_path = ready
    _stage_med_refinement(
        tmp_path, capsys, {"status": "ordered"}, {"status": "completed"})

    assert _run(tmp_path, "review-conflicts", "--resolve", "1", "--keep", "merge",
                "--field", "status") == 1
    assert "NAME=existing|incoming" in capsys.readouterr().err


def test_review_conflicts_field_without_merge_exits_1(ready, capsys):
    tmp_path = ready
    _stage_med_refinement(
        tmp_path, capsys, {"status": "ordered"}, {"status": "completed"})

    assert _run(tmp_path, "review-conflicts", "--resolve", "1", "--keep", "incoming",
                "--field", "status=incoming") == 1
    assert "only apply to keep 'merge'" in capsys.readouterr().err


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


def _seed_fusing_lab_and_movable_condition(ready, capsys, tmp_path, old):
    """Two lab rows a `"alb" = "albumin"` dictionary fuses, plus a condition whose key
    merely moves under `"t2dm" = "type 2 diabetes"` — the issue-#92 shape at the CLI."""
    scan = tmp_path / "fuse-scan.txt"
    scan.write_bytes(b"alb 4.2 / albumin 3.6")
    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources")) == 0
    doc = _document_id(tmp_path)[-1]["document_id"]
    payload = _write_json(tmp_path, "fuse.json", {
        "lab_result": [
            {"test_name": "ALB", "collected_at": "2026-01-02", "value_num": 4.2},
            {"test_name": "Albumin", "collected_at": "2026-01-02", "value_num": 3.6},
        ],
        "condition": [{"name": "T2DM", "status": "active"}],
    })
    assert _run(tmp_path, "commit-extraction", "--document", str(doc),
                "--json", str(payload), "--dictionary", str(old)) == 0
    capsys.readouterr()


def test_rekey_refuses_a_fusing_dictionary(ready, capsys, tmp_path):
    """Exit 1 with a pointed message when the dictionary would merge two facts — and
    (issue #92) the fused table is skipped by name while the clean one still applies."""
    old = _dict_file(tmp_path, "old.toml", '"unrelated" = "unrelated"\n')
    new = _dict_file(tmp_path, "fuse.toml",
                     '"alb" = "albumin"\n"t2dm" = "type 2 diabetes"\n')
    _seed_fusing_lab_and_movable_condition(ready, capsys, tmp_path, old)

    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--apply") == 1
    captured = capsys.readouterr()
    assert "same dedup_key" in captured.err
    assert "lab_result was not written" in captured.err
    assert "left 1 table(s) on their stored keys: lab_result" in captured.err
    # The unaffected table is rekeyed anyway: one collision, one blocked table.
    assert "lab_result: 1/2 key(s) change (skipped: collision)" in captured.out
    assert "rekeyed 1 row(s)" in captured.out

    # Re-run: the condition is now current, so only the fused table is still outstanding.
    assert _run(tmp_path, "rekey", "--dictionary", str(new)) == 1
    out = capsys.readouterr().out
    assert "dry run: no row outside the skipped table(s) needs a new key" in out


def test_rekey_json_reports_collisions_and_skipped_tables(ready, capsys, tmp_path):
    """`--json` keeps exit-code parity with the text mode and says which tables were
    withheld, so an agent can tell a partial run from a clean one (issue #92)."""
    old = _dict_file(tmp_path, "old.toml", '"unrelated" = "unrelated"\n')
    new = _dict_file(tmp_path, "fuse.toml",
                     '"alb" = "albumin"\n"t2dm" = "type 2 diabetes"\n')
    _seed_fusing_lab_and_movable_condition(ready, capsys, tmp_path, old)

    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--json") == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] == ["lab_result"]
    assert [c["kind"] for c in payload["collisions"]] == ["fused"]
    assert [c["record_type"] for c in payload["changed"]] == ["lab_result", "condition"]


# --- collisions a recorded verdict settles (issue #116) -----------------------

def _row_ids(tmp_path, record_type):
    conn = db.connect(tmp_path / "cli.db")
    try:
        return [int(r[0]) for r in conn.execute(
            f"SELECT {record_type}_id FROM {record_type} ORDER BY {record_type}_id")]
    finally:
        conn.close()


def test_rekey_reports_a_verdict_resolved_collision_in_text_and_json(
    ready, capsys, tmp_path
):
    """AC6. A collision the operator already ruled on is a `note`, not an `error`: the
    table writes, both output modes say which pair was settled and by what, and the exit
    code is 0 because nothing is left for the operator to fix."""
    old = _dict_file(tmp_path, "old.toml", '"unrelated" = "unrelated"\n')
    new = _dict_file(tmp_path, "fuse.toml",
                     '"alb" = "albumin"\n"t2dm" = "type 2 diabetes"\n')
    _seed_fusing_lab_and_movable_condition(ready, capsys, tmp_path, old)
    alb_id, albumin_id = _row_ids(tmp_path, "lab_result")
    assert _run(tmp_path, "record", "annotate", "lab_result", str(alb_id),
                "--status", "superseded", "--note", "one assay, two labels",
                "--apply") == 0
    capsys.readouterr()

    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["collisions"] == [] and payload["skipped"] == []
    assert [(r["row_id"], r["clash_row_id"], r["kind"], r["status"], r["scope"],
             r["new_occurrence"]) for r in payload["resolved"]] == [
        (albumin_id, alb_id, "fused", "superseded", "family", 1)]

    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--apply") == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "lab_result: 2/2 key(s) change" in captured.out
    assert "(skipped: collision)" not in captured.out
    assert "note: lab_result: lab_result_id" in captured.out
    assert "family-scoped 'superseded' verdict already settles the pair" in captured.out
    assert "1 collision(s) resolved by verdict" in captured.out
    assert "rekeyed 3 row(s)" in captured.out
    assert captured.out.isascii()               # issue #23


def test_rekey_reports_a_distinct_resolved_collision_in_text_and_json(
    ready, capsys, tmp_path
):
    """Issue #122 at the CLI: a pair the operator ruled *two distinct facts* unblocks its
    table exactly as a merge-shaped ruling does, and both output modes say which of the
    two happened - `settlement` in `--json`, the wording plus the summary counts in
    text."""
    old = _dict_file(tmp_path, "old.toml", '"unrelated" = "unrelated"\n')
    new = _dict_file(tmp_path, "fuse.toml",
                     '"alb" = "albumin"\n"t2dm" = "type 2 diabetes"\n')
    _seed_fusing_lab_and_movable_condition(ready, capsys, tmp_path, old)
    alb_id, albumin_id = _row_ids(tmp_path, "lab_result")
    assert _run(tmp_path, "record", "annotate", "lab_result", str(alb_id),
                "--status", "distinct", "--note", "two assays, one generic label",
                "--apply") == 0
    capsys.readouterr()

    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["collisions"] == [] and payload["skipped"] == []
    assert [(r["row_id"], r["clash_row_id"], r["status"], r["settlement"])
            for r in payload["resolved"]] == [
        (albumin_id, alb_id, "distinct", "distinct")]

    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--apply") == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "lab_result: 2/2 key(s) change" in captured.out
    assert "(skipped: collision)" not in captured.out
    assert "rules them two distinct facts" in captured.out
    assert "both rows keep rendering as live, independent facts" in captured.out
    assert "1 collision(s) resolved by verdict (0 as one fact, 1 as distinct facts)" \
        in captured.out
    assert captured.out.isascii()               # issue #23


def test_rekey_reports_contradictory_verdicts_in_text_and_json(
    ready, capsys, tmp_path
):
    """Issue #124 end to end. One row ruled *two facts* and the other ruled *one fact*
    settles nothing, so the pair blocks like any unresolved collision: rc 1 in both modes,
    the table withheld by name, the contradiction spelled out on stderr and carried on the
    `--json` collision entry - never reported as a clean one-sided resolution."""
    old = _dict_file(tmp_path, "old.toml", '"unrelated" = "unrelated"\n')
    new = _dict_file(tmp_path, "fuse.toml",
                     '"alb" = "albumin"\n"t2dm" = "type 2 diabetes"\n')
    _seed_fusing_lab_and_movable_condition(ready, capsys, tmp_path, old)
    alb_id, albumin_id = _row_ids(tmp_path, "lab_result")
    assert _run(tmp_path, "record", "annotate", "lab_result", str(alb_id),
                "--status", "distinct", "--note", "two assays, one generic label",
                "--apply") == 0
    assert _run(tmp_path, "record", "annotate", "lab_result", str(albumin_id), "--row",
                "--status", "superseded", "--note", "no, one assay filed twice",
                "--apply") == 0
    capsys.readouterr()

    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--json") == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["resolved"] == [] and payload["skipped"] == ["lab_result"]
    collision = payload["collisions"][0]
    assert collision["kind"] == "fused"
    assert [(c["row_id"], c["status"], c["scope"], c["settlement"])
            for c in collision["contradiction"]] == [
        (albumin_id, "superseded", "row", "merged"),
        (alb_id, "distinct", "family", "distinct")]

    # Same rc in text mode, and the unaffected table is still written (the #92 quarantine
    # is per table, and a contradiction is an ordinary blocking collision).
    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--apply") == 1
    captured = capsys.readouterr()
    assert "contradictory verdicts" in captured.err
    assert "A pair ruled two opposite ways is not settled" in captured.err
    assert "lab_result was not written" in captured.err
    assert "left 1 table(s) on their stored keys: lab_result" in captured.err
    assert "lab_result: 1/2 key(s) change (skipped: collision)" in captured.out
    assert "resolved by verdict" not in captured.out
    assert "rekeyed 1 row(s)" in captured.out
    assert captured.out.isascii() and captured.err.isascii()   # issue #23


def _seed_generic_condition_pair(ready, capsys, tmp_path, old):
    """The `meridun/pemr-data#12` item 6 shape at the CLI: two real, different conditions
    that the extractor labelled with numbered placeholders, one document each. A
    `"diagnosis 2" = "diagnosis"` dictionary entry folds them onto one key - the fuse a
    dictionary edit cannot undo, because the labels are synonyms of nothing."""
    for i, (name, note) in enumerate(
        (("diagnosis", "hypertension, per cardiology"),
         ("diagnosis 2", "asthma, per pulmonology")), start=1
    ):
        scan = tmp_path / f"generic{i}.txt"
        scan.write_bytes(f"scan {i}".encode())
        assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                    "--sources", str(tmp_path / "sources")) == 0
        doc = _document_id(tmp_path)[-1]["document_id"]
        payload = _write_json(tmp_path, f"generic{i}.json", {"condition": [
            {"name": name, "status": "active", "onset_on": f"2024-0{i}-05", "note": note},
        ]})
        assert _run(tmp_path, "commit-extraction", "--document", str(doc),
                    "--json", str(payload), "--dictionary", str(old)) == 0
    capsys.readouterr()


@pytest.mark.parametrize("status, both_live", [("distinct", True),
                                               ("superseded", False)])
def test_a_distinct_resolved_pair_still_renders_as_two_live_problems(
    ready, capsys, tmp_path, status, both_live
):
    """AC2 + AC3 as one chain at the CLI, on the real-world trigger (AC6): declare the
    pair distinct, `rekey --apply` writes the table, and the rendered summary still shows
    *both* conditions with no `## Superseded / corrected` appendix at all.

    The `superseded` leg is the control that keeps the assertion honest. Both statuses
    resolve the collision and both produce the identical two-occurrence family, so the
    only thing separating them is `distinct`'s absence from
    `curation.APPENDIX_STATUSES` - and on that leg the ruled row *does* leave Active
    Problems for the appendix. Without the contrast, "no appendix" could equally mean the
    appendix never fires for conditions."""
    old = _dict_file(tmp_path, "old.toml", '"unrelated" = "unrelated"\n')
    new = _dict_file(tmp_path, "fuse.toml", '"diagnosis 2" = "diagnosis"\n')
    _seed_generic_condition_pair(ready, capsys, tmp_path, old)
    first_id, second_id = _row_ids(tmp_path, "condition")

    # Unruled, the whole table is withheld (AC5 / the #92 quarantine).
    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--apply") == 1
    assert "condition was not written" in capsys.readouterr().err

    assert _run(tmp_path, "record", "annotate", "condition", str(first_id), "--row",
                "--status", status, "--note", "two diagnoses, numbered labels",
                "--apply") == 0
    capsys.readouterr()

    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--apply") == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "condition: 1/2 key(s) change" in captured.out
    assert "(skipped: collision)" not in captured.out
    assert captured.out.isascii()               # issue #23

    conn = db.connect(tmp_path / "cli.db")
    try:
        rows = {int(r["condition_id"]): r
                for r in conn.execute("SELECT * FROM condition")}
        # Identical write either way: one family, occurrences 0 and 1.
        assert rows[first_id]["dedup_base"] == rows[second_id]["dedup_base"]
        assert (rows[first_id]["dedup_occurrence"],
                rows[second_id]["dedup_occurrence"]) == (0, 1)
        summary = render.render_summary(conn, "jane-doe")
    finally:
        conn.close()

    problems = summary.split("## Active Problems")[1].split("##")[0]
    assert "asthma, per pulmonology" in problems           # never the ruled row
    assert ("hypertension, per cardiology" in problems) is both_live
    assert ("## Superseded / corrected" not in summary) is both_live


def test_rekey_json_still_reports_a_blocking_collision(ready, capsys, tmp_path):
    """The same seed with no verdict recorded: `resolved` is empty, the table is still
    skipped by name and the exit code is still 1 (the #92 contract, unwidened)."""
    old = _dict_file(tmp_path, "old.toml", '"unrelated" = "unrelated"\n')
    new = _dict_file(tmp_path, "fuse.toml",
                     '"alb" = "albumin"\n"t2dm" = "type 2 diabetes"\n')
    _seed_fusing_lab_and_movable_condition(ready, capsys, tmp_path, old)

    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--json") == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["resolved"] == []
    assert payload["skipped"] == ["lab_result"]
    assert [c["kind"] for c in payload["collisions"]] == ["fused"]


# --- apply-time orphan reporting (issue #126) ---------------------------------
#
# `rekey` still never re-points a verdict by itself (the #109/#114/#116 design). What
# changes here is *when the operator hears about it*: at the apply that caused it, rather
# than on the next `pemr verify`.

def _first_base(tmp_path, record_type):
    conn = db.connect(tmp_path / "cli.db")
    try:
        return conn.execute(
            f"SELECT dedup_base FROM {record_type} ORDER BY {record_type}_id"
        ).fetchone()["dedup_base"]
    finally:
        conn.close()


def _seed_ruled_lab(ready, capsys, tmp_path):
    """One ZZT lab row under a verdict, plus the dictionary that will move its family."""
    old = _dict_file(tmp_path, "old.toml", '"unrelated" = "unrelated"\n')
    new = _dict_file(tmp_path, "new.toml", '"zzt" = "zonulin_test"\n')
    _seed_lab(ready, tmp_path, old)
    base = _first_base(tmp_path, "lab_result")
    assert _run(tmp_path, "record", "annotate", "lab_result", base, "--status",
                "superseded", "--note", "old label", "--apply") == 0
    capsys.readouterr()
    return base, new


def test_rekey_apply_names_the_orphans_it_produced(ready, capsys, tmp_path):
    """AC 7: the fallout is on the apply's own output, with the follow-up command."""
    base, new = _seed_ruled_lab(ready, capsys, tmp_path)

    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--apply") == 0
    captured = capsys.readouterr()
    assert "1 curation verdict(s) were orphaned by this rekey" in captured.err
    assert base[:12] in captured.err and "no-live-family" in captured.err
    assert "pemr record reaffirm --map-file" in captured.err
    # Orphaning a verdict is the documented consequence, not a failure: rc is unchanged
    # and the run still reports itself as applied.
    assert "rekeyed 1 row(s)" in captured.out
    assert captured.err.isascii()               # issue #23


def test_rekey_apply_json_carries_the_orphan_list(ready, capsys, tmp_path):
    base, new = _seed_ruled_lab(ready, capsys, tmp_path)

    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--apply", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert [o["dedup_base"] for o in payload["orphans"]] == [base]
    orphan = payload["orphans"][0]
    assert orphan["kinds"] == ["no-live-family"]
    assert orphan["successor_base"] == _first_base(tmp_path, "lab_result")
    assert orphan["successor_base"] != base
    assert orphan["successor_merge_base"] is None
    # Exit-code parity with the text mode, and the rest of the payload is untouched.
    assert payload["applied"] is True and payload["collisions"] == []


def test_rekey_dry_run_reports_no_orphans(ready, capsys, tmp_path):
    """AC 8: dry-run behaviour is unchanged — it wrote nothing, so it orphaned nothing.
    `orphans` is still present (always, so a consumer need not branch), just empty."""
    _base, new = _seed_ruled_lab(ready, capsys, tmp_path)

    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--json") == 0
    assert json.loads(capsys.readouterr().out)["orphans"] == []

    assert _run(tmp_path, "rekey", "--dictionary", str(new)) == 0
    captured = capsys.readouterr()
    assert "orphaned by this rekey" not in captured.err
    assert "dry run: 1 row(s) would change" in captured.out


def test_rekey_does_not_report_an_orphan_it_did_not_cause(ready, capsys, tmp_path):
    """The report is *this run's* fallout, scoped to the families it moved. A verdict
    orphaned earlier by `record rm` is still an orphan — `pemr verify` still says so — but
    listing it here would blame this rekey for someone else's."""
    old = _dict_file(tmp_path, "old.toml", '"unrelated" = "unrelated"\n')
    moves_condition = _dict_file(tmp_path, "cond.toml", '"t2dm" = "type 2 diabetes"\n')
    _seed_fusing_lab_and_movable_condition(ready, capsys, tmp_path, old)
    alb_id, _albumin_id = _row_ids(tmp_path, "lab_result")
    assert _run(tmp_path, "record", "annotate", "lab_result", str(alb_id), "--status",
                "superseded", "--note", "gone soon", "--apply") == 0
    assert _run(tmp_path, "record", "rm", "lab_result", str(alb_id), "--apply") == 0
    capsys.readouterr()

    assert _run(tmp_path, "rekey", "--dictionary", str(moves_condition), "--apply",
                "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert [c["record_type"] for c in payload["changed"]] == ["condition"]
    assert payload["orphans"] == []

    assert _run(tmp_path, "verify") == 0
    assert "has no live family" in capsys.readouterr().out


def test_rekey_reports_no_orphans_for_a_verdict_resolved_collision(
    ready, capsys, tmp_path
):
    """The repro's 11/11 collision-fused rows, guarded (AC 8). That path *narrows* its
    authorizing verdict to row scope in the same transaction as the keys, so the ruling is
    never orphaned and the orphan report must stay empty."""
    old = _dict_file(tmp_path, "old.toml", '"unrelated" = "unrelated"\n')
    new = _dict_file(tmp_path, "fuse.toml",
                     '"alb" = "albumin"\n"t2dm" = "type 2 diabetes"\n')
    _seed_fusing_lab_and_movable_condition(ready, capsys, tmp_path, old)
    alb_id, _albumin_id = _row_ids(tmp_path, "lab_result")
    assert _run(tmp_path, "record", "annotate", "lab_result", str(alb_id), "--status",
                "superseded", "--note", "one assay, two labels", "--apply") == 0
    capsys.readouterr()

    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--apply", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert [r["verdict_action"] for r in payload["resolved"]] == ["narrowed"]
    assert payload["orphans"] == []


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


# The five status cells a CCDA med table prints, in the shape the reason arrives in:
# inline `<content>` inside the same cell as the lifecycle word. The sixth row is the
# paired fresh prescription a reorder produces in the same export.
_CCDA_MED_ROWS = (
    ("Amoxicillin 500 MG", "08/28/2025", "Discontinued"),
    ("Lisinopril 10 MG", "11/11/2024", "Discontinued<content> (Therapy Completed)</content>"),
    ("Levothyroxine 50 MCG", "06/11/2026", "Discontinued<content> (Reorder)</content>"),
    ("Atorvastatin 20 MG", "03/02/2025",
     "Discontinued<content> (Patient Stopped Taking)</content>"),
    ("Omeprazole 20 MG", "05/09/2025",
     "Discontinued<content> (Substitution/Alternate Therapy Placed)</content>"),
    ("Levothyroxine 50 MCG", "06/12/2026", "Active"),
)


def _ccda_with_med_table(tmp_path, name="DOC0159.XML"):
    rows = "".join(
        f"<tr><td>{drug}</td><td>{date}</td><td>{cell}</td></tr>"
        for drug, date, cell in _CCDA_MED_ROWS
    )
    doc = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ClinicalDocument xmlns="urn:hl7-org:v3">'
        "<recordTarget><patientRole><patient>"
        "<name><given>Jane</given><family>Doe</family></name>"
        '<birthTime value="19620314"/>'
        "</patient></patientRole></recordTarget>"
        "<component><structuredBody><component><section>"
        "<title>Medications</title><text><table>"
        "<thead><tr><th>Medication</th><th>Date</th><th>Status</th></tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table></text></section></component></structuredBody></component>"
        "</ClinicalDocument>"
    )
    p = tmp_path / name
    p.write_text(doc, encoding="utf-8")
    return p


def test_ccda_discontinue_reason_survives_ingest_to_query_end_to_end(ready, capsys):
    """Issue #159 through the CLI, whole chain: a CCDA med table's discontinue reason
    reaches `ocr_text` (#138), lands on `medication.status_reason` at commit time, and
    changes what `query meds --active` says - a renewal stays current while a completed
    course does not. Pre-#159 every variant collapsed to a bare `discontinued` and the
    renewal read as stopped."""
    tmp_path = ready
    src = _ccda_with_med_table(tmp_path)

    assert _run(tmp_path, "ingest", str(src), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources"), "--ocr", "auto") == 0
    assert "ingested document #1" in capsys.readouterr().out

    # 1. the reason reaches ocr_text attached to its own row's status word (#138)
    conn = db.connect(tmp_path / "cli.db")
    try:
        text = conn.execute(
            "SELECT ocr_text FROM document WHERE document_id = 1"
        ).fetchone()["ocr_text"]
    finally:
        conn.close()
    for drug, _date, cell in _CCDA_MED_ROWS:
        expected = cell.replace("<content>", "").replace("</content>", "")
        assert any(line.startswith(drug) and line.endswith(expected)
                   for line in text.splitlines()), (drug, expected)

    # 2. the lifecycle word and the parenthetical split across two fields (AGENTS.md 8)
    meds = _write_json(tmp_path, "meds.json", {"medication": [
        {"name": "Amoxicillin", "dose": "500 MG", "started_on": "2025-08-01",
         "ended_on": "2025-08-28", "status": "discontinued"},
        {"name": "Lisinopril", "dose": "10 MG", "started_on": "2024-10-01",
         "ended_on": "2024-11-11", "status": "discontinued",
         "status_reason": "Therapy Completed"},
        {"name": "Levothyroxine", "dose": "50 MCG", "started_on": "2025-06-11",
         "ended_on": "2026-06-11", "status": "discontinued",
         "status_reason": "Reorder"},
        {"name": "Atorvastatin", "dose": "20 MG", "started_on": "2025-01-02",
         "ended_on": "2025-03-02", "status": "discontinued",
         "status_reason": "Patient Stopped Taking"},
        {"name": "Omeprazole", "dose": "20 MG", "started_on": "2025-02-09",
         "ended_on": "2025-05-09", "status": "discontinued",
         "status_reason": "Substitution/Alternate Therapy Placed"},
        {"name": "Levothyroxine", "dose": "50 MCG", "started_on": "2026-06-12",
         "status": "active"},
    ]})
    assert _run(tmp_path, "commit-extraction", "--document", "1",
                "--json", str(meds)) == 0
    assert "6 new" in capsys.readouterr().out

    # 3. each reason is stored verbatim and readable off the row - no ocr_text parsing
    assert _run(tmp_path, "query", "meds", "--person", "jane-doe", "--json") == 0
    stored = {(m["name"], m["started_on"]): m
              for m in json.loads(capsys.readouterr().out)}
    assert [stored[k]["status_reason"] for k in (
        ("Amoxicillin", "2025-08-01"), ("Lisinopril", "2024-10-01"),
        ("Levothyroxine", "2025-06-11"), ("Atorvastatin", "2025-01-02"),
        ("Omeprazole", "2025-02-09"), ("Levothyroxine", "2026-06-12"),
    )] == [None, "Therapy Completed", "Reorder", "Patient Stopped Taking",
           "Substitution/Alternate Therapy Placed", None]
    # ...and `status` stays lifecycle-only: the reason is never smuggled back into it
    assert {m["status"] for m in stored.values()} == {"discontinued", "active"}

    # 4. the human-readable listing shows the reason and marks the renewal
    assert _run(tmp_path, "query", "meds", "--person", "jane-doe") == 0
    out = capsys.readouterr().out
    assert out.isascii()                                        # issue #23
    levo = next(x for x in out.splitlines() if "2026-06-11" in x)
    assert "[discontinued: Reorder]" in levo
    assert "-> 2026-06-11 (renewed)" in levo
    amox = next(x for x in out.splitlines() if "Amoxicillin" in x)
    assert "[discontinued]" in amox and "(renewed)" not in amox  # bare: unchanged
    for reason in ("Therapy Completed", "Patient Stopped Taking",
                   "Substitution/Alternate Therapy Placed"):
        row = next(x for x in out.splitlines() if reason in x)
        assert f"[discontinued: {reason}]" in row
        assert "(renewed)" not in row                            # still terminal

    # 5. the verdict that matters: the renewal survives --active, the completed course
    #    does not. Both have a terminal status and a past end date.
    assert _run(tmp_path, "query", "meds", "--person", "jane-doe", "--active",
                "--json") == 0
    active = {(m["name"], m["started_on"]) for m in json.loads(capsys.readouterr().out)}
    assert ("Levothyroxine", "2025-06-11") in active     # renewed -> still current
    assert ("Levothyroxine", "2026-06-12") in active     # the paired fresh row
    assert ("Lisinopril", "2024-10-01") not in active    # therapy completed -> ended
    assert ("Atorvastatin", "2025-01-02") not in active
    assert ("Omeprazole", "2025-02-09") not in active
    assert ("Amoxicillin", "2025-08-01") not in active   # bare discontinued -> ended


def _ooxml_entity_bomb(root, levels=7, width=4, leaf=64):
    """A `<!DOCTYPE` whose internal subset amplifies ``&e{levels};`` ~1 MB."""
    chain = "".join(
        f'<!ENTITY e{n} "{f"&e{n - 1};" * width}">' for n in range(1, levels + 1)
    )
    return f'<!DOCTYPE {root} [<!ENTITY e0 "{"A" * leaf}">{chain}]>', levels


@pytest.mark.parametrize("kind", ["docx", "xlsx"])
def test_ocr_auto_refuses_a_doctype_bearing_ooxml_end_to_end(ready, capsys, kind):
    """Issue #155 through the CLI: a DOCTYPE member is refused before parsing, and
    the refusal costs a note — never the document. Pre-fix these ~800-byte files
    stored 1,048,576 chars of `AAAA…` as `native` text."""
    tmp_path = ready
    if kind == "docx":
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        doctype, levels = _ooxml_entity_bomb("w:document")
        member = "word/document.xml"
        body = (
            f'<?xml version="1.0" encoding="UTF-8"?>{doctype}'
            f'<w:document xmlns:w="{ns}"><w:body>'
            f"<w:p><w:r><w:t>&e{levels};</w:t></w:r></w:p>"
            "</w:body></w:document>"
        )
    else:
        ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        doctype, levels = _ooxml_entity_bomb("worksheet")
        member = "xl/worksheets/sheet1.xml"
        body = (
            f'{doctype}<worksheet xmlns="{ns}"><sheetData>'
            f'<row><c t="str"><v>&e{levels};</v></c></row>'
            "</sheetData></worksheet>"
        )
    src = tmp_path / f"bomb.{kind}"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(member, body)
    assert src.stat().st_size < 4096          # well under the extraction byte cap

    assert _run(tmp_path, "ingest", str(src), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources"), "--ocr", "auto",
                "--force") == 0
    out = capsys.readouterr()
    assert "ingested document #1" in out.out          # the document is kept
    assert f"{member} declares a DOCTYPE" in out.err   # named, before any parse
    assert "no ocr_text stored" in out.err
    assert "A" * 200 not in out.err

    # nothing expanded into the archive: the amplified text is unfindable
    assert _run(tmp_path, "find", "--person", "jane-doe", "AAAA") == 0
    assert "no matches" in capsys.readouterr().out


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


# --- saved HTML portal pages at the CLI (issue #173) ---------------------------
# `tests/test_ingest.py` covers the extractor and both `trust_anchors` call sites at
# the library level; these two walk the same behaviour through `pemr ingest`/`find`/
# `document reocr` — the verify-stage real run, kept as a repeatable spec.

_PORTAL_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>MyChart &mdash; Visit Summary</title>
<style>.banner { color: #003366; font-family: "Helvetica Neue", sans-serif; }</style>
<script>var portalSessionToken = "abc123"; analytics.send("visit-summary");</script>
</head><body onload="trackPageView()">
<div class="banner">Mercy Clinic &amp; Labs</div>
<p>Patient: Jane Doe<br>DOB: 01/01/1980</p>
<table><thead><tr><th>Test</th><th>Result</th></tr></thead><tbody>
<tr><td><div>Ferritin</div></td><td>201&#160;ng/mL</td></tr>
<tr><td></td><td></td></tr>
</tbody></table>
<noscript><p>Enable JavaScript for the interactive chart.</p></noscript>
</body></html>
"""

# The same lab table twice, in the two formats whose owner-check verdicts now differ.
_LAB_TABLE_CSV = "Patient ID,Test,Result\n00998877,Ferritin,201 ng/mL\n"
_LAB_TABLE_HTML = (
    "<html><body><table>"
    "<tr><th>Patient ID</th><th>Test</th></tr>"
    "<tr><td>00998877</td><td>Ferritin 201 ng/mL</td></tr>"
    "</table></body></html>"
)


def test_ocr_auto_reads_a_saved_portal_page_end_to_end(ready, capsys):
    """Issue #173 through the CLI: a saved portal page used to reach tesseract, which
    cannot decode HTML, so it stored nothing and never entered `find`. Now it is read
    natively on the `native-prose` route — tags stripped, `<script>`/`<style>` out of
    the FTS index, the identity header live, and the route named in `reocr` output."""
    from pemr import ingest as ingest_mod

    tmp_path = ready
    page = tmp_path / "portal.html"
    page.write_text(_PORTAL_PAGE, encoding="utf-8")
    sources = tmp_path / "sources"

    assert _run(tmp_path, "ingest", str(page), "--person", "jane-doe",
                "--sources", str(sources), "--ocr", "auto") == 0
    out = capsys.readouterr()
    assert "ingested document #1" in out.out
    # the anchor is armed on this route, and the header names the owner
    assert "owner verified: matched 'jane-doe' in document text" in out.out
    assert "no ocr_text stored" not in out.err

    conn = db.connect(tmp_path / "cli.db")
    try:
        text = conn.execute(
            "SELECT ocr_text FROM document WHERE document_id = 1"
        ).fetchone()["ocr_text"]
    finally:
        conn.close()
    lines = text.splitlines()
    assert "Patient: Jane Doe" in lines             # `<br>` ended the line...
    assert "DOB: 01/01/1980" in lines               # ...so the DOB is its own
    assert "Mercy Clinic & Labs" in lines           # `&amp;` decoded
    assert "Ferritin\t201 ng/mL" in lines           # `<div>` did not split the cell
    assert "\t\t" not in text and "\t" in text      # the all-empty row contributed none
    assert "<" not in text                          # every tag stripped
    assert text == ingest_mod.normalize_document_text(text)   # stored normalized

    # `ocr_text` is mirrored into FTS, so the page's code must be unfindable...
    assert _run(tmp_path, "find", "--person", "jane-doe", "ferritin") == 0
    assert "#1" in capsys.readouterr().out
    for junk in ("portalSessionToken", "Helvetica", "JavaScript"):
        assert _run(tmp_path, "find", "--person", "jane-doe", junk) == 0
        assert "no matches" in capsys.readouterr().out, junk

    # ...and `document reocr` reports the third route value on its own line (#143)
    assert _run(tmp_path, "document", "reocr", "1", "--sources", str(sources),
                "--dry-run", "--force") == 0
    assert "route native-prose" in capsys.readouterr().out


def test_html_lab_table_is_refused_where_the_csv_equivalent_is_not(ready, capsys):
    """The intended, bounded cost of arming the anchor on HTML: the *same* lab table
    files as `.csv` (structured — `Patient ID` is a column label, issue #66) and is
    refused as `suspect` when it arrives as a saved `.html` page. `--force` is the
    one-flag recovery. Pin it, so the divergence stays a decision rather than drift."""
    tmp_path = ready
    sources = tmp_path / "sources"
    csv = tmp_path / "labs.csv"
    csv.write_text(_LAB_TABLE_CSV, encoding="utf-8")
    page = tmp_path / "labs.html"
    page.write_text(_LAB_TABLE_HTML, encoding="utf-8")

    assert _run(tmp_path, "ingest", str(csv), "--person", "jane-doe",
                "--sources", str(sources), "--ocr", "auto") == 0
    assert "ingested document #1" in capsys.readouterr().out

    assert _run(tmp_path, "ingest", str(page), "--person", "jane-doe",
                "--sources", str(sources), "--ocr", "auto") == 1
    err = capsys.readouterr().err
    assert "owner verification failed" in err
    assert "re-run with --force" in err
    assert len(_document_id(tmp_path)) == 1          # pre-write refusal: nothing landed

    assert _run(tmp_path, "ingest", str(page), "--person", "jane-doe",
                "--sources", str(sources), "--ocr", "auto", "--force") == 0
    out = capsys.readouterr()
    assert "ingested document #2" in out.out
    assert "verdict: suspect" in out.err


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


def test_migrated_006_rows_rekey_per_table_when_one_table_collides(tmp_path, capsys):
    """The issue-#92 report verbatim: 006 carries the old observation keys forward, the
    follow-up `rekey --apply` hits a collision in `allergy`, and the conditions — which
    have no collision among them — must still be rekeyed rather than held hostage."""
    staged = _stage_pre_006(tmp_path)
    assert _run(tmp_path, "migrate", "--create", "--migrations-dir", str(staged)) == 0
    conn = db.connect(tmp_path / "cli.db")
    conn.execute("INSERT INTO person (slug, full_name) VALUES ('jane-doe', 'Jane')")
    for i, (sub, reaction) in enumerate((("PCN", "rash"),
                                         ("Penicillin", "anaphylaxis")), start=1):
        conn.execute(
            "INSERT INTO observation (person_id, obs_type, key, value_text, dedup_key,"
            " dedup_base) VALUES (1, 'allergy', ?, ?, ?, ?)",
            (sub, reaction, f"a{i}", f"a{i}"))
    for i, name in enumerate(("T2DM", "HTN"), start=1):
        conn.execute(
            "INSERT INTO observation (person_id, obs_type, key, dedup_key, dedup_base)"
            " VALUES (1, 'condition', ?, ?, ?)", (name, f"c{i}", f"c{i}"))
    conn.commit()
    conn.close()
    assert _run(tmp_path, "migrate") == 0
    assert "4 allergy/condition row" in capsys.readouterr().out

    new = _dict_file(tmp_path, "fuse006.toml",
                     '"pcn" = "penicillin"\n"t2dm" = "type 2 diabetes"\n')
    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--apply") == 1
    captured = capsys.readouterr()
    assert "allergy: 1/2 key(s) change (skipped: collision)" in captured.out
    assert "condition: 2/2 key(s) change" in captured.out
    assert "rekeyed 2 row(s)" in captured.out
    assert "left 1 table(s) on their stored keys: allergy" in captured.err

    conn = db.connect(tmp_path / "cli.db")
    try:
        # The carried-forward observation keys survive on the blocked table only.
        assert sorted(r["dedup_key"] for r in
                      conn.execute("SELECT dedup_key FROM allergy")) == ["a1", "a2"]
        assert not [r for r in conn.execute("SELECT dedup_key FROM condition")
                    if r["dedup_key"] in ("c1", "c2")]
    finally:
        conn.close()


def _stage_006_allergy_collision(tmp_path, capsys):
    """The real-world #116 trigger: a pre-006 database whose carried-forward
    per-document allergy keys collide onto one dictionary-derived key after 006, with
    conditions alongside that merely move. Returns the two allergy row ids."""
    staged = _stage_pre_006(tmp_path)
    assert _run(tmp_path, "migrate", "--create", "--migrations-dir", str(staged)) == 0
    conn = db.connect(tmp_path / "cli.db")
    conn.execute("INSERT INTO person (slug, full_name) VALUES ('jane-doe', 'Jane')")
    for i, (sub, reaction) in enumerate((("PCN", "rash"),
                                         ("Penicillin", "anaphylaxis")), start=1):
        conn.execute(
            "INSERT INTO observation (person_id, obs_type, key, value_text, dedup_key,"
            " dedup_base) VALUES (1, 'allergy', ?, ?, ?, ?)",
            (sub, reaction, f"a{i}", f"a{i}"))
    for i, name in enumerate(("T2DM", "HTN"), start=1):
        conn.execute(
            "INSERT INTO observation (person_id, obs_type, key, dedup_key, dedup_base)"
            " VALUES (1, 'condition', ?, ?, ?)", (name, f"c{i}", f"c{i}"))
    conn.commit()
    conn.close()
    assert _run(tmp_path, "migrate") == 0
    capsys.readouterr()
    return _row_ids(tmp_path, "allergy")


def test_migration_006_allergy_collision_clears_when_a_verdict_covers_it(
    tmp_path, capsys
):
    """AC7, the case the issue was filed from: the 006 backfill parks the allergy table
    on its old keys, and the operator has already ruled the colliding pair one allergy.
    That verdict answers the collision, so the table finally rekeys and rc drops to 0.

    And the ruling keeps exactly the extension it had: `Penicillin` was never judged, so
    running a maintenance command must not take a live, high-criticality allergy out of
    the Allergies section (the regression this issue was re-decided over)."""
    pcn_id, penicillin_id = _stage_006_allergy_collision(tmp_path, capsys)
    assert _run(tmp_path, "record", "annotate", "allergy", str(pcn_id),
                "--status", "merged-into", "--merged-into", str(penicillin_id),
                "--note", "one allergy, two spellings", "--apply") == 0
    capsys.readouterr()

    conn = db.connect(tmp_path / "cli.db")
    try:
        before = render.render_summary(conn, "jane-doe")
    finally:
        conn.close()
    assert "- Penicillin - anaphylaxis" in before
    assert "allergy: PCN" in before.split("## Superseded / corrected")[1]

    new = _dict_file(tmp_path, "fuse006.toml",
                     '"pcn" = "penicillin"\n"t2dm" = "type 2 diabetes"\n')
    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--apply") == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "allergy: 2/2 key(s) change" in captured.out
    assert "(skipped: collision)" not in captured.out
    assert "1 collision(s) resolved by verdict" in captured.out
    assert "was narrowed to row scope on allergy row 1" in captured.out
    assert "rekeyed 4 row(s)" in captured.out

    conn = db.connect(tmp_path / "cli.db")
    try:
        rows = {int(r["allergy_id"]): r
                for r in conn.execute("SELECT * FROM allergy")}
        # The carried-forward keys are gone from both rows, and the pair is one family.
        assert not {r["dedup_key"] for r in rows.values()} & {"a1", "a2"}
        assert rows[pcn_id]["dedup_base"] == rows[penicillin_id]["dedup_base"]
        assert (rows[pcn_id]["dedup_occurrence"],
                rows[penicillin_id]["dedup_occurrence"]) == (0, 1)
        # The verdict still covers PCN and only PCN: Penicillin stays where it was.
        after = render.render_summary(conn, "jane-doe")
        assert "- Penicillin - anaphylaxis" in after
        assert "allergy: PCN" in after.split("## Superseded / corrected")[1]
        # Nothing orphaned; the narrowing surfaces as a re-affirm notice instead.
        warnings = verify.verify_report(conn).warnings
        assert not any("no live family" in w for w in warnings)
        assert [w for w in warnings if "a rekey moved it" in w]
    finally:
        conn.close()
    # Idempotent: the follow-up run has nothing left to do.
    assert _run(tmp_path, "rekey", "--dictionary", str(new)) == 0
    assert "all dedup keys already match" in capsys.readouterr().out


def test_the_re_affirm_notice_from_a_narrowing_is_runnable(tmp_path, capsys):
    """The narrowing is announced so the human can re-affirm or re-rule (#116's ruling) —
    which is only true if the command the notice names actually runs. For `merged-into`,
    the dominant real-world shape, the same rekey has already moved the ruled row into
    its merge target, so the re-affirm names the row's own family by necessity.

    Continues the AC7 scenario above through that follow-up: rc 0, the notice clears, and
    the ruling keeps its status, its merge pointer and its extension."""
    pcn_id, penicillin_id = _stage_006_allergy_collision(tmp_path, capsys)
    assert _run(tmp_path, "record", "annotate", "allergy", str(pcn_id),
                "--status", "merged-into", "--merged-into", str(penicillin_id),
                "--note", "one allergy, two spellings", "--apply") == 0
    new = _dict_file(tmp_path, "fuse006.toml",
                     '"pcn" = "penicillin"\n"t2dm" = "type 2 diabetes"\n')
    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--apply") == 0
    capsys.readouterr()

    # The notice's own instruction, run as the operator would: same row, same ruling.
    assert _run(tmp_path, "record", "annotate", "allergy", str(pcn_id), "--row",
                "--status", "merged-into", "--merged-into", str(penicillin_id),
                "--note", "re-affirmed after the rekey", "--apply") == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"annotated allergy row #{pcn_id}" in captured.out

    conn = db.connect(tmp_path / "cli.db")
    try:
        verdict = curation.get_verdict(conn, "allergy", "", record_id=pcn_id)
        assert verdict["status"] == "merged-into"
        assert verdict["note"] == "re-affirmed after the rekey"
        live = conn.execute(
            "SELECT dedup_base FROM allergy WHERE allergy_id = ?", (pcn_id,)
        ).fetchone()["dedup_base"]
        assert verdict["merged_into_base"] == live
        assert verdict["dedup_base"] == live          # breadcrumb collapsed
        warnings = verify.verify_report(conn).warnings
        assert not any("a rekey moved it" in w or "no live family" in w
                       for w in warnings)
        # Extension unchanged: PCN in the appendix, Penicillin still live.
        after = render.render_summary(conn, "jane-doe")
        assert "- Penicillin - anaphylaxis" in after
        assert "allergy: PCN" in after.split("## Superseded / corrected")[1]
    finally:
        conn.close()


def test_migrated_006_collision_still_blocks_without_a_verdict(tmp_path, capsys):
    """The same staging with no verdict recorded is the unchanged #92 behaviour — the
    regression guard for the AC7 case above."""
    _stage_006_allergy_collision(tmp_path, capsys)
    new = _dict_file(tmp_path, "fuse006.toml",
                     '"pcn" = "penicillin"\n"t2dm" = "type 2 diabetes"\n')
    assert _run(tmp_path, "rekey", "--dictionary", str(new), "--apply") == 1
    captured = capsys.readouterr()
    assert "allergy: 1/2 key(s) change (skipped: collision)" in captured.out
    assert "left 1 table(s) on their stored keys: allergy" in captured.err


def test_post_006_stale_key_blocks_the_recommit_instead_of_forking_it(tmp_path, capsys):
    """Pins the post-006 upgrade paragraph in `docs/Architecture.md` §3 (issue #94).

    A pre-006 database carries its old per-document `observation` keys through 006, so
    the next `commit-extraction` that re-states one of those facts is *refused* naming
    `pemr rekey --apply` and writes nothing -- it does not silently fork a second row
    (that was the pre-guard behaviour the doc used to describe). Enforcement is narrow:
    an unrelated condition still commits while the stale key sits there, which is why
    the doc has to tell operators to run the rekey rather than wait to be stopped.
    """
    staged = _stage_pre_006(tmp_path)
    assert _run(tmp_path, "migrate", "--create", "--migrations-dir", str(staged)) == 0
    assert _run(tmp_path, "person", "add", "--slug", "jane-doe", "--name", "Jane") == 0
    conn = db.connect(tmp_path / "cli.db")
    conn.execute(
        "INSERT INTO observation (person_id, obs_type, key, observed_at, dedup_key,"
        " dedup_base) VALUES (1, 'condition', 'Hypertension', '2024-01-02', 'c1', 'c1')"
    )
    conn.commit()
    conn.close()
    assert _run(tmp_path, "migrate") == 0
    assert "1 allergy/condition row" in capsys.readouterr().out

    note = tmp_path / "visit.txt"
    note.write_bytes(b"problem list: hypertension")
    assert _run(tmp_path, "ingest", str(note), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources")) == 0
    capsys.readouterr()

    same = _write_json(tmp_path, "same.json",
                       {"condition": [{"name": "Hypertension", "status": "active"}]})
    assert _run(tmp_path, "commit-extraction", "--document", "1", "--json", str(same)) == 1
    err = capsys.readouterr().err
    assert "no longer matches the current dictionary" in err
    assert "rekey --apply" in err
    conn = db.connect(tmp_path / "cli.db")
    try:  # refused, not forked
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM condition").fetchone()["n"] == 1
    finally:
        conn.close()

    other = _write_json(tmp_path, "other.json",
                        {"condition": [{"name": "Asthma", "status": "active"}]})
    assert _run(tmp_path, "commit-extraction", "--document", "1", "--json",
                str(other)) == 0
    assert "1 new" in capsys.readouterr().out

    # ... and the remedy the message names actually clears it.
    assert _run(tmp_path, "rekey", "--apply") == 0
    capsys.readouterr()
    assert _run(tmp_path, "commit-extraction", "--document", "1", "--json", str(same)) == 0
    assert "0 new, 1 duplicate" in capsys.readouterr().out


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


# --- issue #104: the curated lab-label synonyms, walked through the real CLI ---

# (label as one document prints it, label as the other prints it) for every pair the
# #104 curation has to converge. Same person, same draw, same value, two spellings:
# the second document must land as a DUPLICATE, never as a second row. Some pairs
# carry no dictionary line at all - the parenthesized ones ride identity()'s
# redundant-alias rule and `Hemoglobin`/`hemoglobin` rides _collapse()'s casefold -
# and they are listed here precisely so "no entry needed" stays a tested claim.
_DICT_104_CLI_PAIRS: list[tuple[str, str]] = [
    ("TPro", "Total Protein"),
    ("Protein, Total (SPEP)", "Protein Electrophoresis Total Protein"),
    ("MG", "magnesium"),
    ("BMG", "Beta-2 Microglobulin"),
    ("BUN", "Urea Nitrogen (BUN)"),
    ("CO2", "CO2 (Bicarbonate)"),
    ("ALT", "ALT (SGPT)"),
    ("AST", "AST (SGOT)"),
    ("TSH", "TSH (Thyroid Stimulating Hormone)"),
    ("Kappa", "Kappa Free Light Chains, Serum"),
    ("Lambda", "Lambda Free Light Chain, Serum"),
    ("K/L Ratio", "Kappa/Lambda Free Light Chain Ratio"),
    ("LYM", "Lymphocytes (absolute)"),
    ("MONO", "Monocytes (absolute)"),
    ("EO", "Eosinophils (absolute)"),
    ("BAS", "Basophils (absolute)"),
    ("LYM%", "Lymphocytes %"),
    ("NEU%", "Neutrophils %"),
    ("MON%", "Monocytes %"),
    ("EO%", "Eosinophils %"),
    ("BAS%", "Basophils %"),
    ("IFE Interpretation, U", "IFE Interpretation:U"),
    ("Hemoglobin", "hemoglobin"),
    ("Protein,Total,Urine", "Protein, Total, Urine"),
    ("Prot, 24hr Calculated", "Prot,24hr Calculated"),
]

# The other half of the bargain: labels off the SAME draw that the curation must leave
# on keys of their own (issue #71's qualifier constraint plus the short codes #104
# deliberately excluded). Every one of these has to commit as a new row.
_DICT_104_CLI_DISTINCT: list[str] = [
    # Issue #106: SPEP renderings print bare labels, so the CMP codes and the bare
    # forms must stay apart; `Neutrophils (absolute)` is unmapped pending #107.
    "ALB",
    "Albumin",
    "NEU",
    "Neutrophils (absolute)",
    "Albumin (SPEP)",
    "Protein Electrophoresis Albumin Fraction",
    "Bicarbonate",
    "LDL cholesterol (direct)",
    "LDL cholesterol (calculated)",
    "estimated GFR (black)",
    "estimated GFR (other)",
    "M-Spike",
    "M-Spike, %",
    "Gran",
    "LY",
    "MO",
]


def test_curated_synonyms_dedup_across_documents_through_the_cli(ready, capsys):
    """#104 end to end, against the shipped dictionary: one report prints the short
    codes, the next prints the long forms, and layer-2 dedup has to see one draw
    rather than two. A fork here is exactly the symptom the curation exists to fix -
    rendered summaries repeating a row per spelling."""
    tmp_path = ready
    sources = tmp_path / "sources"
    for i in (1, 2, 3):
        scan = tmp_path / f"d{i}.txt"
        scan.write_bytes(f"lab report {i}".encode())
        assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                    "--sources", str(sources)) == 0
    capsys.readouterr()

    def _labs(names):
        # Index-derived values, so the two documents state the SAME number for each
        # pair (a duplicate, not a conflict) while every pair stays distinguishable.
        return [{"test_name": name, "collected_at": "2026-01-02",
                 "value_num": 1.0 + i, "unit": "g/dL"} for i, name in enumerate(names)]

    pairs = len(_DICT_104_CLI_PAIRS)
    short = _write_json(tmp_path, "short.json",
                        {"lab_result": _labs([a for a, _ in _DICT_104_CLI_PAIRS])})
    assert _run(tmp_path, "commit-extraction", "--document", "1",
                "--json", str(short)) == 0
    assert f"{pairs} new, 0 duplicate" in capsys.readouterr().out

    spelled = _write_json(tmp_path, "spelled.json",
                          {"lab_result": _labs([b for _, b in _DICT_104_CLI_PAIRS])})
    assert _run(tmp_path, "commit-extraction", "--document", "2",
                "--json", str(spelled)) == 0
    assert f"0 new, {pairs} duplicate" in capsys.readouterr().out

    # ...and nothing over-collapsed: the qualifier-distinct labels off that same draw
    # each still key on their own, so they commit as new rows rather than deduping
    # into their stems.
    distinct = _write_json(tmp_path, "distinct.json",
                           {"lab_result": _labs(_DICT_104_CLI_DISTINCT)})
    assert _run(tmp_path, "commit-extraction", "--document", "3",
                "--json", str(distinct)) == 0
    assert f"{len(_DICT_104_CLI_DISTINCT)} new, 0 duplicate" in capsys.readouterr().out

    conn = db.connect(tmp_path / "cli.db")
    try:
        rows, keys = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT dedup_key) FROM lab_result"
        ).fetchone()
    finally:
        conn.close()
    assert rows == keys == pairs + len(_DICT_104_CLI_DISTINCT)

    # The rows were keyed under the shipped dictionary, so `pemr rekey` (dry run) has
    # nothing to move and no collision to report.
    assert _run(tmp_path, "rekey") == 0
    assert "all dedup keys already match the current dictionary" in capsys.readouterr().out


# --- issue #117: lab_result keys on the collection date, walked through the real CLI ---

def _ingest_lab_document(tmp_path, name, text, payload):
    """Ingest a scan and commit one `lab_result` payload against it, CLI-only."""
    scan = tmp_path / name
    scan.write_bytes(text)
    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources")) == 0
    doc = _document_id(tmp_path)[-1]["document_id"]
    json_path = _write_json(tmp_path, f"{name}.json", {"lab_result": [payload]})
    return _run(tmp_path, "commit-extraction", "--document", str(doc),
                "--json", str(json_path))


def test_mixed_precision_same_draw_dedups_at_the_cli(ready, capsys):
    """The reported bug (pemr-data#9) end to end: a summary states the draw as a bare
    date, the lab report timestamps it. One fact, so one row -- and the read layer still
    sees the precision the first document gave."""
    tmp_path = ready
    payload = {"test_name": "HbA1c", "value_num": 5.7, "unit": "%",
               "ref_low": 4.0, "ref_high": 5.6}
    assert _ingest_lab_document(tmp_path, "summary.txt", b"hba1c 5.7 (no time given)",
                                {**payload, "collected_at": "2026-04-01"}) == 0
    assert "1 new, 0 duplicate" in capsys.readouterr().out
    assert _ingest_lab_document(tmp_path, "labreport.txt", b"hba1c 5.7 collected 09:15",
                                {**payload, "collected_at": "2026-04-01T09:15"}) == 0
    assert "0 new, 1 duplicate" in capsys.readouterr().out

    assert _run(tmp_path, "query", "labs", "--person", "jane-doe", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["test_name"], r["collected_at"]) for r in rows] == [("HbA1c", "2026-04-01")]


def test_same_day_distinct_draw_is_staged_and_keep_both_admits_it(ready, capsys):
    """The accepted cost of the date-only key, walked through the operator's recovery:
    a genuine second same-day draw (a GTT timepoint) collides and STAGES -- it is never
    silently doubled and never silently dropped -- and `--keep both` admits it."""
    tmp_path = ready
    common = {"test_name": "glucose", "unit": "mg/dL"}
    assert _ingest_lab_document(tmp_path, "gtt-0h.txt", b"glucose 92 fasting",
                                {**common, "collected_at": "2026-04-01T08:00",
                                 "value_num": 92}) == 0
    assert _ingest_lab_document(tmp_path, "gtt-2h.txt", b"glucose 130 two hour",
                                {**common, "collected_at": "2026-04-01T14:00",
                                 "value_num": 130}) == 0
    assert "0 new, 0 duplicate, 0 enriched, 1 conflict" in capsys.readouterr().out

    assert _run(tmp_path, "review-conflicts", "--resolve", "1", "--keep", "both",
                "--note", "GTT timepoints") == 0
    out = capsys.readouterr().out
    assert "occurrence 1" in out and out.isascii()

    assert _run(tmp_path, "query", "labs", "--person", "jane-doe", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["value_num"], r["collected_at"]) for r in rows] == [
        (92.0, "2026-04-01T08:00"), (130.0, "2026-04-01T14:00")]


def test_observation_keeps_its_full_precision_key_at_the_cli(ready, capsys):
    """The scope boundary: `observation` was deliberately left on `_norm_ts`, so the
    same mixed-precision pair still forks there -- two rows, no conflict."""
    tmp_path = ready
    for name, observed_at in (("obs1.txt", "2026-04-02"), ("obs2.txt", "2026-04-02T07:30")):
        scan = tmp_path / name
        scan.write_bytes(b"weight 180 lb")
        assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                    "--sources", str(tmp_path / "sources")) == 0
        doc = _document_id(tmp_path)[-1]["document_id"]
        payload = _write_json(tmp_path, f"{name}.json", {"observation": [
            {"obs_type": "vital", "key": "weight", "observed_at": observed_at,
             "value_num": 180, "unit": "lb"}]})
        assert _run(tmp_path, "commit-extraction", "--document", str(doc),
                    "--json", str(payload)) == 0
        assert "1 new" in capsys.readouterr().out

    conn = db.connect(tmp_path / "cli.db")
    try:
        counts = conn.execute("SELECT COUNT(*), COUNT(DISTINCT dedup_key) "
                              "FROM observation").fetchone()
    finally:
        conn.close()
    assert tuple(counts) == (2, 2)


def _seed_pre_117_mixed_precision_pair(tmp_path):
    """The state an existing database is in when this change lands: one draw stored
    twice because the two documents dated it differently, both rows on the *legacy*
    full-precision keys. Inserted directly -- `commit-extraction` would now dedup them."""
    scan = tmp_path / "legacy.txt"
    scan.write_bytes(b"hba1c 5.7 stated twice")
    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources")) == 0
    row = {"test_name": "HbA1c", "value_num": 5.7, "unit": "%"}
    conn = db.connect(tmp_path / "cli.db")
    try:
        person_id = conn.execute("SELECT person_id FROM person").fetchone()["person_id"]
        doc = conn.execute("SELECT document_id FROM document").fetchone()["document_id"]
        for collected_at in ("2026-04-01", "2026-04-01T09:15"):
            legacy = hashlib.sha256("|".join([          # pre-#117: _norm_ts(collected_at)
                str(person_id), dedup.key_token(row["test_name"]),
                collected_at.replace("T", " "),
            ]).encode("utf-8")).hexdigest()
            dedup._insert_record(conn, "lab_result",
                                 {**row, "collected_at": collected_at},
                                 person_id, doc, legacy)
        conn.commit()
    finally:
        conn.close()
    return _write_json(tmp_path, "legacy.json",
                       {"lab_result": [{**row, "collected_at": "2026-04-01"}]})


def test_rekey_is_the_migration_for_a_pre_change_database(ready, capsys):
    """The upgrade path an existing database takes, at the CLI: the drift guard refuses
    to file the fact a third time, `rekey` names the pair as `doubled` (data, not
    dictionary) and writes nothing, `record rm` clears it, and the same document then
    dedups cleanly."""
    tmp_path = ready
    payload = _seed_pre_117_mixed_precision_pair(tmp_path)
    doc = _document_id(tmp_path)[-1]["document_id"]
    capsys.readouterr()

    # 1. Nothing silent: the next commit touching that identity is refused outright.
    assert _run(tmp_path, "commit-extraction", "--document", str(doc),
                "--json", str(payload)) == 1
    err = capsys.readouterr().err
    assert "pemr rekey --apply" in err and err.isascii()

    # 2. The dry run diagnoses it as doubled DATA and quarantines only lab_result.
    assert _run(tmp_path, "rekey", "--json") == 1
    report = json.loads(capsys.readouterr().out)
    assert [c["kind"] for c in report["collisions"]] == ["doubled"]
    assert report["skipped"] == ["lab_result"]
    assert "pemr record rm" in report["collisions"][0]["message"]

    # 3. `--apply` still writes nothing for the blocked table.
    assert _run(tmp_path, "rekey", "--apply") == 1
    capsys.readouterr()
    conn = db.connect(tmp_path / "cli.db")
    try:
        assert conn.execute("SELECT COUNT(DISTINCT dedup_key) AS n "
                            "FROM lab_result").fetchone()["n"] == 2
    finally:
        conn.close()

    # 4. The documented resolution, then a clean rekey and a clean re-commit.
    assert _run(tmp_path, "record", "rm", "lab_result", "2", "--apply") == 0
    assert "removed lab_result #2" in capsys.readouterr().out
    assert _run(tmp_path, "rekey") == 0
    assert "all dedup keys already match" in capsys.readouterr().out
    assert _run(tmp_path, "commit-extraction", "--document", str(doc),
                "--json", str(payload)) == 0
    assert "0 new, 1 duplicate" in capsys.readouterr().out


# --- functional observations end to end (issue #132) --------------------------

def test_functional_observation_cli_roundtrip(ready, capsys):
    """The #132 round trip through the real CLI: a caregiver-observed functional fact
    stated by an ingested document commits, reaches `query timeline` and `render brief`
    on the generic observation path, and stays out of `render summary`'s vitals/orders
    sections and out of the problem list entirely."""
    tmp_path = ready
    register = tmp_path / "register.txt"
    register.write_bytes(b"check register transcription, jane-doe: the running balance "
                         b"column stops mid-page while checks keep being written")
    assert _run(tmp_path, "ingest", str(register), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources")) == 0
    capsys.readouterr()

    payload = _write_json(tmp_path, "functional.json", {
        "observation": [
            {"obs_type": "functional", "key": "financial_self_management",
             "observed_at": "2025-07-05",
             "value_text": "stopped carrying the running balance forward"},
            {"obs_type": "functional", "key": "meal_regularity",
             "observed_at": "2025-07-19", "value_num": 2, "unit": "meals/day"},
        ],
        "appointment": [{"scheduled_for": "2099-02-01", "provider": "Dr. Smith",
                         "specialty": "Geriatrics", "reason": "function review"}],
    })
    assert _run(tmp_path, "commit-extraction", "--document", "1",
                "--json", str(payload)) == 0
    assert "3 new" in capsys.readouterr().out

    # An undated functional row is refused at the CLI boundary, not quietly stored.
    undated = _write_json(tmp_path, "undated.json", {"observation": [
        {"obs_type": "functional", "key": "meal_regularity",
         "value_text": "skipping meals"},
    ]})
    assert _run(tmp_path, "commit-extraction", "--document", "1",
                "--json", str(undated)) == 1
    assert "missing required field 'observed_at'" in capsys.readouterr().err

    assert _run(tmp_path, "query", "timeline", "--person", "jane-doe") == 0
    timeline = capsys.readouterr().out
    assert "functional financial_self_management" in timeline
    assert "stopped carrying the running balance forward" in timeline
    assert "2025-07-05" in timeline
    assert "functional meal_regularity" in timeline and "meals/day" in timeline

    assert _run(tmp_path, "render", "brief", "--appointment", "1") == 0
    brief = capsys.readouterr().out
    observations = brief.split("## Procedures & Observations")[1].split("\n## ")[0]
    assert "functional financial_self_management" in observations
    assert "## Active Problems\n\n_none recorded_" in brief   # never a diagnosis

    assert _run(tmp_path, "render", "summary", "--person", "jane-doe") == 0
    summary = capsys.readouterr().out
    for section in ("## Latest Vitals", "## Orders & Referrals", "## Active Problems"):
        assert "functional" not in summary.split(section)[1].split("\n## ")[0]

    conn = db.connect(tmp_path / "cli.db")
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM observation "
                            "WHERE obs_type='functional'").fetchone()["n"] == 2
        assert conn.execute("SELECT COUNT(*) AS n FROM condition").fetchone()["n"] == 0
    finally:
        conn.close()


# --- a display preference must not touch dedup (issues #129 + #136) ----------

def _reingest_after_a_unit_correction(root, *, with_pref):
    """ingest -> commit -> `record edit` the unit -> (optionally set a canonical display
    unit) -> re-commit the *same* extraction off the same document.

    Returns everything the second commit did: its exit code, the conflict rows it
    staged, and the surviving observation rows.
    """
    root.mkdir(parents=True, exist_ok=True)

    def run(*argv):
        return cli.main(["--db", str(root / "cli.db"), *argv])

    assert run("migrate", "--create") == 0
    assert run("person", "add", "--slug", "jane-doe", "--name", "Jane Doe") == 0
    scan = root / "scan.txt"
    scan.write_bytes(b"weight 180 lbs")
    assert run("ingest", str(scan), "--person", "jane-doe",
               "--sources", str(root / "sources")) == 0
    payload = _write_json(root, "extract.json", {"observation": [
        {"obs_type": "vital", "key": "weight", "observed_at": "2026-01-02",
         "value_num": 180.0, "unit": "lbs"},
    ]})
    assert run("commit-extraction", "--document", "1", "--json", str(payload)) == 0
    # Issue #129's scalpel: correct the mislabelled unit in place, provenance intact.
    assert run("record", "edit", "observation", "1", "--set", "unit=lb",
               "--note", "normalise unit spelling", "--apply") == 0
    if with_pref:
        assert run("person", "unit-pref", "set", "jane-doe", "--key", "weight",
                   "--unit", "lb") == 0

    rc = run("commit-extraction", "--document", "1", "--json", str(payload))
    conn = db.connect(root / "cli.db")
    try:
        conflicts = [dict(r) for r in conn.execute(
            "SELECT record_type, dedup_key, existing_json, incoming_json, status "
            "FROM conflict ORDER BY conflict_id"
        ).fetchall()]
        rows = [dict(r) for r in conn.execute(
            "SELECT key, value_num, unit, dedup_key FROM observation "
            "ORDER BY observation_id"
        ).fetchall()]
    finally:
        conn.close()
    return rc, conflicts, rows


def test_a_display_unit_preference_cannot_change_re_ingest_dedup(tmp_path):
    """Issue #136 AC-7. `unit` is in `dedup._COMPARE_FIELDS`, so a row whose unit was
    corrected by `record edit` diverges from its own source document on re-ingest --
    deliberately and loudly (#129's "Re-ingest divergence"). #136 must not quietly
    change that either way: it is a *display* lever, so the property to prove is **no
    change at all**, with a preference set or not.
    """
    plain = _reingest_after_a_unit_correction(tmp_path / "plain", with_pref=False)
    preferred = _reingest_after_a_unit_correction(tmp_path / "preferred",
                                                  with_pref=True)
    assert plain == preferred

    rc, conflicts, rows = preferred
    # And the outcome is #129's documented one, so this cannot pass by both sides
    # silently becoming no-ops.
    assert rc == 0
    assert [c["record_type"] for c in conflicts] == ["observation"]
    assert [(r["unit"], r["value_num"]) for r in rows] == [("lb", 180.0)]

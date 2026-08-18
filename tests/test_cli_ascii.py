"""Regression for issue #23: CLI-facing output must be ASCII so it survives a
non-UTF-8 Windows console (cp1252/cp437), where a U+2014 em-dash renders as `?`
or crashes with UnicodeEncodeError. Mirrors the phase-4 lesson guarded by
``test_query.py::test_timeline_summaries_are_ascii_safe`` for query output.
"""

import json

import pytest

from pemr import cli


def _run(tmp_path, *argv):
    return cli.main(["--db", str(tmp_path / "cli.db"), *argv])


def _assert_console_safe(text: str) -> None:
    assert text.isascii(), f"non-ASCII CLI output would garble on cp437/cp1252: {text!r}"
    text.encode("cp437")  # raises UnicodeEncodeError if not console-safe


@pytest.fixture()
def ready(tmp_path):
    """Migrated DB + a person, ready for ingest."""
    assert _run(tmp_path, "migrate", "--create") == 0
    assert _run(tmp_path, "person", "add", "--slug", "jane-doe", "--name", "Jane Doe") == 0
    return tmp_path


def test_empty_person_list_is_console_safe(tmp_path, capsys):
    assert _run(tmp_path, "migrate", "--create") == 0
    capsys.readouterr()
    assert _run(tmp_path, "person", "list") == 0
    out = capsys.readouterr().out
    assert "no people yet" in out
    _assert_console_safe(out)


def test_ingest_no_ocr_note_is_console_safe(ready, capsys):
    scan = ready / "scan.txt"
    scan.write_bytes(b"hba1c 5.7 percent")
    capsys.readouterr()
    assert _run(ready, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(ready / "sources")) == 0
    captured = capsys.readouterr()
    assert "no ocr_text stored" in captured.err
    _assert_console_safe(captured.out)
    _assert_console_safe(captured.err)


def test_study_ingest_output_is_console_safe(ready, capsys):
    """Issue #69's new surface: the pack/exclusion notes and the derived summary."""
    from test_study import make_study

    capsys.readouterr()
    assert _run(ready, "ingest", str(make_study(ready / "disc")), "--person",
                "jane-doe", "--study", "dicom", "--sources", str(ready / "sources")) == 0
    captured = capsys.readouterr()
    assert "DICOM files" in captured.err
    _assert_console_safe(captured.out)
    _assert_console_safe(captured.err)

    capsys.readouterr()
    assert _run(ready, "document", "show", "1", "--text") == 0
    _assert_console_safe(capsys.readouterr().out)


def test_duplicate_output_is_console_safe(ready, capsys):
    """The exact reproduction from the issue: '... - nothing ingested'."""
    scan = ready / "s.txt"
    scan.write_bytes(b"same")
    sources = ready / "sources"
    assert _run(ready, "ingest", str(scan), "--person", "jane-doe", "--sources", str(sources)) == 0
    capsys.readouterr()
    assert _run(ready, "ingest", str(scan), "--person", "jane-doe", "--sources", str(sources)) == 0
    out = capsys.readouterr().out
    assert "duplicate" in out and "nothing ingested" in out
    _assert_console_safe(out)


def test_conflict_note_is_console_safe(ready, capsys):
    sources = ready / "sources"

    def _extract(name, obj):
        p = ready / name
        p.write_text(json.dumps(obj), encoding="utf-8")
        return str(p)

    scan1 = ready / "scan1.txt"
    scan1.write_bytes(b"hba1c 5.7 percent")
    assert _run(ready, "ingest", str(scan1), "--person", "jane-doe", "--sources", str(sources)) == 0
    labs1 = _extract("e1.json", {"lab_result": [{"test_name": "HbA1c", "collected_at": "2026-01-02",
                                                 "value_num": 5.7, "unit": "%"}]})
    assert _run(ready, "commit-extraction", "--document", "1", "--json", labs1) == 0

    scan2 = ready / "scan2.txt"
    scan2.write_bytes(b"hba1c 6.2 percent")
    assert _run(ready, "ingest", str(scan2), "--person", "jane-doe", "--sources", str(sources)) == 0
    labs2 = _extract("e2.json", {"lab_result": [{"test_name": "A1c", "collected_at": "2026-01-02",
                                                 "value_num": 6.2, "unit": "%"}]})
    capsys.readouterr()
    assert _run(ready, "commit-extraction", "--document", "2", "--json", labs2) == 0
    out = capsys.readouterr().out
    assert "conflict(s) staged" in out
    _assert_console_safe(out)


def test_unmigrated_error_is_console_safe(tmp_path, capsys, unmigrated_db):
    unmigrated_db(tmp_path / "cli.db")  # exists, no schema (issue #55 gate is earlier)
    scan = tmp_path / "s.txt"
    scan.write_bytes(b"x")
    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources")) == 1
    err = capsys.readouterr().err
    assert "not migrated" in err
    _assert_console_safe(err)


def test_missing_database_message_is_console_safe(tmp_path):
    # The issue #55 refusal is what a panicking user reads first; it must not garble.
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, "person", "list")
    _assert_console_safe(str(exc.value))
    assert "no database at" in str(exc.value)


def test_restore_and_verify_output_is_console_safe(tmp_path, capsys):
    src = tmp_path / "src"
    assert cli.main(["--db", str(src / "cli.db"), "migrate", "--create"]) == 0
    backups = tmp_path / "backups"
    assert cli.main(["--db", str(src / "cli.db"), "backup",
                     "--backup-dir", str(backups)]) == 0
    capsys.readouterr()
    assert _run(tmp_path, "restore", "latest", "--backup-dir", str(backups)) == 0
    captured = capsys.readouterr()
    _assert_console_safe(captured.out)
    _assert_console_safe(captured.err)
    capsys.readouterr()
    _run(tmp_path, "verify")
    _assert_console_safe(capsys.readouterr().out)


def test_unknown_slug_error_is_console_safe(ready, capsys):
    scan = ready / "s.txt"
    scan.write_bytes(b"x")
    capsys.readouterr()
    assert _run(ready, "ingest", str(scan), "--person", "ghost",
                "--sources", str(ready / "sources")) == 1
    err = capsys.readouterr().err
    assert "no person with slug" in err
    _assert_console_safe(err)


def test_bad_document_id_error_is_console_safe(ready, capsys):
    labs = ready / "e.json"
    labs.write_text(json.dumps({"lab_result": []}), encoding="utf-8")
    capsys.readouterr()
    assert _run(ready, "commit-extraction", "--document", "999", "--json", str(labs)) == 1
    err = capsys.readouterr().err
    assert "no document with id" in err
    _assert_console_safe(err)


def test_help_description_is_console_safe(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "Personal EMR engine" in out
    _assert_console_safe(out)


def test_record_annotate_output_is_console_safe(ready, capsys):
    """Issue #109's new CLI + render surface: the verdict block, the `--list` table,
    the `[DISPUTED: ...]` marker, the appendix heading and `verify`'s warning block."""
    scan = ready / "scan.txt"
    scan.write_bytes(b"visit note: glucose 95")
    assert _run(ready, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(ready / "sources"), "--ocr-text-file", str(scan)) == 0
    payload = ready / "extract.json"
    payload.write_text(json.dumps({"lab_result": [
        {"test_name": "Glucose", "collected_at": "2026-01-02", "value_num": 95,
         "unit": "mg/dL"},
    ]}), encoding="utf-8")
    assert _run(ready, "commit-extraction", "--document", "1", "--json",
                str(payload)) == 0

    capsys.readouterr()
    assert _run(ready, "record", "annotate", "lab_result", "1", "--status",
                "disputed", "--note", "two sources disagree", "--apply") == 0
    _assert_console_safe(capsys.readouterr().out)

    assert _run(ready, "record", "annotate", "--list") == 0
    _assert_console_safe(capsys.readouterr().out)

    assert _run(ready, "render", "summary", "--person", "jane-doe") == 0
    _assert_console_safe(capsys.readouterr().out)

    assert _run(ready, "record", "annotate", "lab_result", "1", "--status",
                "superseded", "--note", "corrected later", "--apply") == 0
    capsys.readouterr()
    assert _run(ready, "render", "summary", "--person", "jane-doe") == 0
    out = capsys.readouterr().out
    assert "Glucose" not in out              # curated out of the summary (#168)
    _assert_console_safe(out)

    # The audit trail is its own target now, and it is console-safe too (#168).
    assert _run(ready, "render", "curation", "--person", "jane-doe") == 0
    out = capsys.readouterr().out
    assert "## superseded" in out and "lab_result: Glucose" in out
    _assert_console_safe(out)

    assert _run(ready, "record", "rm", "lab_result", "1", "--apply") == 0
    capsys.readouterr()
    assert _run(ready, "verify") == 0
    captured = capsys.readouterr()
    assert "warnings" in captured.out
    _assert_console_safe(captured.out)
    _assert_console_safe(captured.err)


def test_record_reaffirm_output_is_console_safe(ready, capsys):
    """Issue #126's new surfaces: `record reaffirm`'s listing (with its `->` successor
    column) and the `rekey --apply` orphan block, both on a cp437 console."""
    with pytest.raises(SystemExit):
        _run(ready, "record", "reaffirm", "--help")
    _assert_console_safe(capsys.readouterr().out)

    scan = ready / "scan.txt"
    scan.write_bytes(b"visit note: glucose 95")
    assert _run(ready, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(ready / "sources"), "--ocr-text-file", str(scan)) == 0
    payload = ready / "extract.json"
    payload.write_text(json.dumps({"lab_result": [
        {"test_name": "Glucose", "collected_at": "2026-01-02", "value_num": 95,
         "unit": "mg/dL"},
    ]}), encoding="utf-8")
    assert _run(ready, "commit-extraction", "--document", "1", "--json",
                str(payload)) == 0
    assert _run(ready, "record", "annotate", "lab_result", "1", "--status",
                "superseded", "--note", "old label", "--apply") == 0

    dictionary = ready / "new.toml"
    dictionary.write_text('[synonyms]\n"glucose" = "blood-sugar"\n', encoding="utf-8")
    capsys.readouterr()
    assert _run(ready, "rekey", "--dictionary", str(dictionary), "--apply") == 0
    captured = capsys.readouterr()
    assert "orphaned by this rekey" in captured.err
    _assert_console_safe(captured.out)
    _assert_console_safe(captured.err)

    # And again through the map-file path, whose listing carries the `->` column.
    assert _run(ready, "rekey", "--dictionary", str(dictionary), "--apply",
                "--json") == 0
    capsys.readouterr()
    assert _run(ready, "record", "reaffirm") == 0
    _assert_console_safe(capsys.readouterr().out)


def test_record_assert_output_is_console_safe(ready, capsys):
    """Issue #110's new CLI + render surface: the report block, the `--list` table, the
    collision refusal, and the `(attested by ...)` marker in every render."""
    with pytest.raises(SystemExit):
        _run(ready, "record", "assert", "--help")
    _assert_console_safe(capsys.readouterr().out)

    # Issue #127: the subcommand's one-line help states a tool-surface exclusion,
    # not a ban on an agent invoking the verb at the CLI -- the wording
    # docs/Architecture.md and pemr/attestations.py already use. argparse renders
    # that string in the *parent* listing (`pemr record --help`), wrapped to the
    # terminal width, so collapse whitespace before matching.
    with pytest.raises(SystemExit):
        _run(ready, "record", "--help")
    out = capsys.readouterr().out
    _assert_console_safe(out)
    listing = " ".join(out.split())
    assert "never an agent write" not in listing
    assert "CLI-only, never an MCP tool" in listing

    argv = ("record", "assert", "medication", "--person", "jane-doe",
            "--attributed-to", "Mom", "--date", "2026-08-09",
            "--field", "name=Metformin", "--field", "dose=500 mg")
    capsys.readouterr()
    assert _run(ready, *argv) == 0
    _assert_console_safe(capsys.readouterr().out)

    assert _run(ready, *argv, "--apply") == 0
    _assert_console_safe(capsys.readouterr().out)

    assert _run(ready, "record", "assert", "--list") == 0
    out = capsys.readouterr().out
    assert "needs source" in out
    _assert_console_safe(out)

    assert _run(ready, "render", "summary", "--person", "jane-doe") == 0
    out = capsys.readouterr().out
    assert "no source document" in out
    _assert_console_safe(out)

    assert _run(ready, "render", "journal", "--person", "jane-doe") == 0
    _assert_console_safe(capsys.readouterr().out)

    assert _run(ready, *argv, "--field", "frequency=BID", "--apply") == 1
    err = capsys.readouterr().err
    assert "already holds this identity" in err
    _assert_console_safe(err)

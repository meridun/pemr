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
    assert _run(tmp_path, "migrate") == 0
    assert _run(tmp_path, "person", "add", "--slug", "jane-doe", "--name", "Jane Doe") == 0
    return tmp_path


def test_empty_person_list_is_console_safe(tmp_path, capsys):
    assert _run(tmp_path, "migrate") == 0
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


def test_unmigrated_error_is_console_safe(tmp_path, capsys):
    scan = tmp_path / "s.txt"
    scan.write_bytes(b"x")
    assert _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources")) == 1
    err = capsys.readouterr().err
    assert "not migrated" in err
    _assert_console_safe(err)


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

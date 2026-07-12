"""End-to-end CLI wiring for phase 2: ingest -> commit-extraction -> review-conflicts."""

import json

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
    assert _run(tmp_path, "migrate") == 0
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


def test_ingest_on_unmigrated_db_is_friendly(tmp_path, capsys):
    scan = tmp_path / "s.txt"
    scan.write_bytes(b"x")
    rc = _run(tmp_path, "ingest", str(scan), "--person", "jane-doe",
              "--sources", str(tmp_path / "sources"))
    assert rc == 1
    assert "migrate" in capsys.readouterr().err


def test_commit_bad_json_is_friendly(ready, capsys):
    tmp_path = ready
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert _run(tmp_path, "commit-extraction", "--document", "1", "--json", str(bad)) == 1
    assert "not valid JSON" in capsys.readouterr().err

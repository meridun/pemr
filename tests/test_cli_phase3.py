"""End-to-end CLI wiring for phase 3: query labs|meds|timeline, find, trends —
human + --json output, empty-result rc=0 messages, unknown-slug rc=1."""

import json

import pytest

from pemr import cli, db, dedup, persons

DICT_ARG = ["--dictionary", str(
    __import__("pathlib").Path(__file__).resolve().parent.parent
    / "data" / "dictionary.example.toml"
)]


def _run(tmp_path, *argv):
    return cli.main(["--db", str(tmp_path / "cli.db"), *argv])


def _doc(conn, slug, ocr=None, doc_date="2026-01-01"):
    pid = conn.execute(
        "SELECT person_id FROM person WHERE slug=?", (slug,)
    ).fetchone()["person_id"]
    cur = conn.execute(
        "INSERT INTO document (sha256, person_id, doc_date, source_path, ocr_text, "
        "ingested_at) VALUES (?, ?, ?, ?, ?, ?)",
        (f"sha-{conn.total_changes}", pid, doc_date, "aa/x.pdf", ocr, "2026-01-01T00:00:00"),
    )
    conn.commit()
    return cur.lastrowid


@pytest.fixture()
def ready(tmp_path):
    """Migrated DB + jane with a few labs, a med, and an OCR'd document."""
    assert _run(tmp_path, "migrate") == 0
    assert _run(tmp_path, "person", "add", "--slug", "jane-doe", "--name", "Jane Doe") == 0
    conn = db.connect(tmp_path / "cli.db")
    d = dedup.load_dictionary(DICT_ARG[1])
    doc = _doc(conn, "jane-doe", ocr="total cholesterol elevated")
    dedup.commit_extraction(conn, doc, {
        "lab_result": [
            {"test_name": "HbA1c", "collected_at": "2024-01-01", "value_num": 5.5, "unit": "%"},
            {"test_name": "A1c", "collected_at": "2026-01-01", "value_num": 6.5, "unit": "%"},
        ],
        "medication": [{"name": "Metformin", "dose": "500mg", "started_on": "2024-02-01"}],
    }, d)
    conn.close()
    return tmp_path


def test_query_labs_human_and_json(ready, capsys):
    assert _run(ready, "query", "labs", "--person", "jane-doe") == 0
    assert "HbA1c" in capsys.readouterr().out

    assert _run(ready, "query", "labs", "--person", "jane-doe", "--test", "a1c",
                *DICT_ARG, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 2
    assert "dedup_key" not in payload[0]  # internal field hidden from the contract
    assert {"test_name", "value_num", "collected_at"} <= set(payload[0])


def test_query_meds_active_json(ready, capsys):
    assert _run(ready, "query", "meds", "--person", "jane-doe", "--active", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert [m["name"] for m in payload] == ["Metformin"]


def test_query_timeline_json(ready, capsys):
    assert _run(ready, "query", "timeline", "--person", "jane-doe", "--json") == 0
    events = json.loads(capsys.readouterr().out)
    assert [e["date"] for e in events] == sorted(e["date"] for e in events)
    assert {"date", "type", "summary", "document_id"} <= set(events[0])


def test_find_human_and_json(ready, capsys):
    assert _run(ready, "find", "--person", "jane-doe", "cholesterol") == 0
    assert "cholesterol" in capsys.readouterr().out.lower()

    assert _run(ready, "find", "--person", "jane-doe", "cholesterol", "--json") == 0
    hits = json.loads(capsys.readouterr().out)
    assert hits and {"source_table", "source_id", "snippet", "document_id"} <= set(hits[0])


def test_trends_json_shape(ready, capsys):
    assert _run(ready, "trends", "--person", "jane-doe", "--test", "hba1c",
                *DICT_ARG, "--json") == 0
    t = json.loads(capsys.readouterr().out)
    assert t["count"] == 2
    assert {"test", "min", "max", "latest", "slope_per_day", "unit"} <= set(t)


def test_empty_results_are_clean_rc0(ready, capsys):
    assert _run(ready, "query", "labs", "--person", "jane-doe", "--test", "tsh",
                *DICT_ARG) == 0
    assert "no lab results" in capsys.readouterr().out
    assert _run(ready, "find", "--person", "jane-doe", "zzznotfound") == 0
    assert "no matches" in capsys.readouterr().out


def test_unknown_person_is_friendly_rc1(ready, capsys):
    assert _run(ready, "query", "labs", "--person", "ghost") == 1
    assert "ghost" in capsys.readouterr().err
    assert _run(ready, "trends", "--person", "ghost", "--test", "hba1c") == 1
    assert "ghost" in capsys.readouterr().err


def test_query_on_unmigrated_db_is_friendly(tmp_path, capsys):
    rc = _run(tmp_path, "query", "labs", "--person", "jane-doe")
    assert rc == 1
    assert "migrate" in capsys.readouterr().err

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

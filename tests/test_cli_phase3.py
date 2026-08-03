"""End-to-end CLI wiring for phase 3: query labs|meds|timeline, find, trends —
human + --json output, empty-result rc=0 messages, unknown-slug rc=1."""

import json
import shlex
import uuid

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
    assert _run(tmp_path, "migrate", "--create") == 0
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


def test_query_labs_one_sided_ref_range(ready, capsys):
    """Issue #45: a lab with only ref_high must render '(ref <= N)', never '(ref -N)'."""
    conn = db.connect(ready / "cli.db")
    d = dedup.load_dictionary(DICT_ARG[1])
    doc = conn.execute("SELECT document_id FROM document LIMIT 1").fetchone()["document_id"]
    dedup.commit_extraction(conn, doc, {
        "lab_result": [
            {"test_name": "Ferritin", "collected_at": "2026-02-01", "value_num": 15.0,
             "unit": "ng/mL", "ref_high": 20.0},                       # one-sided upper
            {"test_name": "TSH", "collected_at": "2026-02-01", "value_num": 3.0,
             "unit": "mIU/L", "ref_low": 8.0},                         # one-sided lower
        ],
    }, d)
    conn.close()

    assert _run(ready, "query", "labs", "--person", "jane-doe") == 0
    out = capsys.readouterr().out
    ferritin = next(line for line in out.splitlines() if "Ferritin" in line)
    assert "(ref <= 20.0)" in ferritin
    assert "(ref -" not in ferritin       # the old bug rendered "(ref -20.0)"
    tsh = next(line for line in out.splitlines() if "TSH" in line)
    assert "(ref >= 8.0)" in tsh


def test_query_meds_active_json(ready, capsys):
    assert _run(ready, "query", "meds", "--person", "jane-doe", "--active", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert [m["name"] for m in payload] == ["Metformin"]


def test_query_meds_terminal_status_renders_ended_not_current(ready, capsys):
    """Issue #21: a completed med with no ended_on must not render '(current)'."""
    conn = db.connect(ready / "cli.db")
    d = dedup.load_dictionary(DICT_ARG[1])
    doc = conn.execute("SELECT document_id FROM document LIMIT 1").fetchone()["document_id"]
    dedup.commit_extraction(conn, doc, {
        "medication": [{"name": "Amoxicillin", "dose": "250mg", "frequency": "BID",
                        "started_on": "2026-05-20", "status": "completed"}],
    }, d)
    conn.close()

    assert _run(ready, "query", "meds", "--person", "jane-doe") == 0
    out = capsys.readouterr().out
    amox = next(line for line in out.splitlines() if "Amoxicillin" in line)
    assert "(current)" not in amox
    assert "(ended)" in amox
    assert "[completed]" in amox
    # the still-current med is unaffected
    metformin = next(line for line in out.splitlines() if "Metformin" in line)
    assert "(current)" in metformin


def test_query_meds_active_drops_expired_course_labelled_active(ready, capsys):
    """Issue #57 repro: a 2024 ten-day course carrying status='active' must not come
    back from `query meds --active`; a future end date under the same status must."""
    conn = db.connect(ready / "cli.db")
    d = dedup.load_dictionary(DICT_ARG[1])
    doc = conn.execute("SELECT document_id FROM document LIMIT 1").fetchone()["document_id"]
    dedup.commit_extraction(conn, doc, {
        "medication": [
            {"name": "Amoxicillin", "dose": "500mg", "frequency": "TID",
             "started_on": "2024-01-01", "ended_on": "2024-01-10", "status": "active"},
            {"name": "Skyrizi", "dose": "150mg", "frequency": "q8w",
             "started_on": "2025-09-04", "ended_on": "2099-09-04", "status": "active"},
        ],
    }, d)
    conn.close()

    assert _run(ready, "query", "meds", "--person", "jane-doe", "--active", "--json") == 0
    names = [m["name"] for m in json.loads(capsys.readouterr().out)]
    assert "Amoxicillin" not in names
    assert "Skyrizi" in names


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
    assert {"test", "min", "max", "latest", "slope_per_day", "unit",
            "latest_tie"} <= set(t)


def _seed_lab(tmp_path, slug, **cols):
    """Insert a lab_result row directly (bypassing dedup) to reach the #20
    same-timestamp duplicate state, then return."""
    conn = db.connect(tmp_path / "cli.db")
    pid = conn.execute(
        "SELECT person_id FROM person WHERE slug=?", (slug,)
    ).fetchone()["person_id"]
    cols.setdefault("dedup_key", f"k-{uuid.uuid4()}")
    keys = ["person_id", *cols]
    vals = [pid, *cols.values()]
    placeholders = ", ".join("?" for _ in keys)
    conn.execute(
        f"INSERT INTO lab_result ({', '.join(keys)}) VALUES ({placeholders})", vals
    )
    conn.commit()
    conn.close()


def test_trends_slope_na_message_reworded(ready, capsys):
    """Issue #29 part A: the n/a message names distinct dates, not 'dated points'."""
    # Single point -> slope n/a.
    _seed_lab(ready, "jane-doe", test_name="LDL", value_num=100, unit="mg/dL",
              collected_at="2026-01-01T08:00:00")
    assert _run(ready, "trends", "--person", "jane-doe", "--test", "ldl", *DICT_ARG) == 0
    out = capsys.readouterr().out
    assert "n/a (need >=2 distinct dates)" in out
    assert "dated points" not in out

    # Two points that share one calendar date -> still n/a, same reworded message.
    _seed_lab(ready, "jane-doe", test_name="Glucose", value_num=5.0, unit="mmol/L",
              collected_at="2026-03-01T08:00:00")
    _seed_lab(ready, "jane-doe", test_name="Glucose", value_num=5.5, unit="mmol/L",
              collected_at="2026-03-01T20:00:00")
    assert _run(ready, "trends", "--person", "jane-doe", "--test", "glucose", *DICT_ARG) == 0
    assert "n/a (need >=2 distinct dates)" in capsys.readouterr().out


def test_trends_same_timestamp_tie_disclosed(ready, capsys):
    """Issue #29 part B: same exact timestamp with differing values is disclosed."""
    _seed_lab(ready, "jane-doe", test_name="Glucose", value_num=5.0, unit="mmol/L",
              collected_at="2026-07-17T09:00:00")
    _seed_lab(ready, "jane-doe", test_name="Glucose", value_num=5.2, unit="mmol/L",
              collected_at="2026-07-17T09:00:00")
    assert _run(ready, "trends", "--person", "jane-doe", "--test", "glucose", *DICT_ARG) == 0
    out = capsys.readouterr().out
    latest = next(line for line in out.splitlines() if "latest" in line)
    assert "5.2" in latest  # deterministic higher-id pick
    assert "(1 of 2 at this timestamp)" in latest


def test_trends_other_assay_note_pastes_back_and_finds_the_rows(ready, capsys):
    """Issue #71 regression: the disclosure note prints a `--test` token as a command
    to paste, but for any analyte whose *canonical* value carries underscores
    (`vitamin_d_25oh`) that token could not be re-derived -- pasting it returned zero
    rows AND zero disclosure, walking the user into a silent dead end for exactly the
    rows the note exists to keep findable.

    Driven through argv (split with shlex, the way a shell would split the printed
    command) so both halves are covered: the quoting and the re-derivation."""
    for value, collected in ((31.0, "2025-01-01"), (28.0, "2026-01-01")):
        _seed_lab(ready, "jane-doe", test_name="Vitamin D", value_num=value,
                  unit="ng/mL", collected_at=collected)
    _seed_lab(ready, "jane-doe", test_name="Vitamin D (25-OH)", value_num=44.0,
              unit="ng/mL", collected_at="2025-01-01")

    assert _run(ready, "trends", "--person", "jane-doe", "--test", "vitamin d",
                *DICT_ARG) == 0
    note = next(l for l in capsys.readouterr().out.splitlines() if "another assay" in l)
    assert "1 more row(s)" in note
    # Exactly what the user would paste, split the way their shell splits it.
    pasted = shlex.split(note.split("another assay: ", 1)[1])
    assert pasted[0] == "--test" and len(pasted) == 2

    assert _run(ready, "trends", "--person", "jane-doe", *pasted, *DICT_ARG) == 0
    out = capsys.readouterr().out
    assert "(1 point(s))" in out and "44" in out
    assert "2 more row(s)" in out          # the reciprocal disclosure still works


def test_other_assay_note_quotes_an_injectable_token(capsys):
    """The token descends from `test_name`, i.e. untrusted document text, and is
    printed as a command the user is invited to run. A `\"` in it must not close the
    quote and leave the remainder of the name live in their shell."""
    hostile = 'albumin (spep" ; rm -rf ~ #)'
    cli._print_other_assays({"other_assays": [hostile], "other_assay_count": 1})
    note = capsys.readouterr().out.strip()
    assert shlex.split(note.split("another assay: ", 1)[1]) == ["--test", hostile]


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


def test_query_on_unmigrated_db_is_friendly(tmp_path, capsys, unmigrated_db):
    # Schema-less DB file, not an absent one - see issue #55's missing-database gate.
    unmigrated_db(tmp_path / "cli.db")
    rc = _run(tmp_path, "query", "labs", "--person", "jane-doe")
    assert rc == 1
    assert "migrate" in capsys.readouterr().err

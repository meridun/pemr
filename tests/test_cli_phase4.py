"""End-to-end CLI wiring for phase 4: render summary|brief|journal -- Markdown to
stdout (the §5 redirect contract), --out file convenience, unknown-slug/appointment
rc=1, and unmigrated-DB friendliness."""

import json
from pathlib import Path

import pytest

from pemr import cli, db, dedup, persons

DICT_PATH = str(
    Path(__file__).resolve().parent.parent / "data" / "dictionary.example.toml"
)


def _run(tmp_path, *argv):
    return cli.main(["--db", str(tmp_path / "cli.db"), *argv])


@pytest.fixture()
def ready(tmp_path):
    """Migrated DB + jane with a med, an abnormal lab and one upcoming appointment."""
    assert _run(tmp_path, "migrate", "--create") == 0
    assert _run(tmp_path, "person", "add", "--slug", "jane-doe",
                "--name", "Jane Doe", "--dob", "1980-01-01") == 0
    conn = db.connect(tmp_path / "cli.db")
    d = dedup.load_dictionary(DICT_PATH)
    pid = conn.execute("SELECT person_id FROM person WHERE slug='jane-doe'").fetchone()["person_id"]
    cur = conn.execute(
        "INSERT INTO document (sha256, person_id, source_path, ingested_at) "
        "VALUES ('sha1', ?, 'aa/x.pdf', '2026-01-01T00:00:00')", (pid,)
    )
    conn.commit()
    dedup.commit_extraction(conn, cur.lastrowid, {
        "lab_result": [{"test_name": "LDL", "collected_at": "2026-01-01",
                        "value_num": 160, "unit": "mg/dL", "flag": "H"}],
        "medication": [{"name": "Metformin", "started_on": "2024-02-01"}],
        "appointment": [{"scheduled_for": "2099-02-01", "provider": "Dr. Smith",
                         "specialty": "Endocrinology", "reason": "follow-up"}],
    }, d)
    aid = conn.execute(
        "SELECT appointment_id FROM appointment WHERE provider='Dr. Smith'"
    ).fetchone()["appointment_id"]
    conn.close()
    return tmp_path, aid


def test_render_summary_to_stdout(ready, capsys):
    tmp_path, _ = ready
    assert _run(tmp_path, "render", "summary", "--person", "jane-doe") == 0
    out = capsys.readouterr().out
    assert "# Master Summary: Jane Doe" in out
    assert "Metformin" in out and "LDL" in out


def test_render_summary_to_out_file(ready, capsys):
    tmp_path, _ = ready
    dest = tmp_path / "summary.md"
    assert _run(tmp_path, "render", "summary", "--person", "jane-doe",
                "--out", str(dest)) == 0
    assert f"wrote {dest}" in capsys.readouterr().out
    assert "# Master Summary: Jane Doe" in dest.read_text(encoding="utf-8")


def test_render_brief_to_stdout(ready, capsys):
    tmp_path, aid = ready
    assert _run(tmp_path, "render", "brief", "--appointment", str(aid)) == 0
    out = capsys.readouterr().out
    assert "# Appointment Brief: Jane Doe" in out
    assert "Dr. Smith" in out


def test_render_journal_to_stdout(ready, capsys):
    tmp_path, _ = ready
    assert _run(tmp_path, "render", "journal", "--person", "jane-doe") == 0
    assert "# Journal: Jane Doe" in capsys.readouterr().out


def test_render_unknown_person_is_friendly_rc1(ready, capsys):
    tmp_path, _ = ready
    assert _run(tmp_path, "render", "summary", "--person", "ghost") == 1
    assert "ghost" in capsys.readouterr().err


def test_render_unknown_appointment_is_friendly_rc1(ready, capsys):
    tmp_path, _ = ready
    assert _run(tmp_path, "render", "brief", "--appointment", "99999") == 1
    assert "99999" in capsys.readouterr().err


def _commit_orders(tmp_path, sha, rows):
    """Insert one document and commit `obs_type='order'` rows into it *through the CLI*
    (`commit-extraction --json`), i.e. the way a real ingest session arrives."""
    conn = db.connect(tmp_path / "cli.db")
    pid = conn.execute(
        "SELECT person_id FROM person WHERE slug='jane-doe'"
    ).fetchone()["person_id"]
    cur = conn.execute(
        "INSERT INTO document (sha256, person_id, source_path, ingested_at) "
        "VALUES (?, ?, 'aa/x.pdf', '2026-01-01T00:00:00')", (sha, pid)
    )
    conn.commit()
    doc = cur.lastrowid
    conn.close()
    payload = tmp_path / f"extract-{sha}.json"
    payload.write_text(json.dumps(
        {"observation": [dict(r, obs_type="order") for r in rows]}
    ), encoding="utf-8")
    assert _run(tmp_path, "commit-extraction", "--document", str(doc),
                "--json", str(payload)) == 0


def test_render_summary_groups_repeated_orders_end_to_end(ready, capsys):
    """Issue #93, walked through the real CLI: an order restated by three documents is
    one bullet carrying the latest detail plus the `+N earlier` disclosure; distinct and
    qualifier-bearing items stay their own bullets; a timestamped row shows a bare date;
    an undated row shows none; newest first, undated last. (Keyless rows are covered in
    `tests/test_render.py` -- two undated ones collide on the storage dedup key, so that
    case cannot be seeded through the commit path.)"""
    tmp_path, _ = ready
    _commit_orders(tmp_path, "sha-o1", [
        {"key": "cervical collar", "value_text": "Dr. Smith, ortho",
         "observed_at": "2026-01-05"},
        {"key": "outpatient physical therapy", "observed_at": "2026-01-05"},
    ])
    _commit_orders(tmp_path, "sha-o2", [
        {"key": "cervical collar", "value_text": "Dr. Adams, ortho",
         "observed_at": "2026-02-01"},
        {"key": "sleep study", "value_text": "home study",
         "observed_at": "2026-02-01T09:30:00"},          # timestamp -> bare date
    ])
    _commit_orders(tmp_path, "sha-o3", [
        {"key": "cervical collar", "value_text": "Dr. Jones, ortho",
         "observed_at": "2026-06-14"},
        {"key": "outpatient physical therapy (aquatic)", "observed_at": "2026-06-14"},
        {"key": "wheelchair evaluation", "value_text": "seating clinic"},   # undated
    ])
    capsys.readouterr()

    assert _run(tmp_path, "render", "summary", "--person", "jane-doe") == 0
    out = capsys.readouterr().out
    section = out.split("## Orders & Referrals")[1].split("\n## ")[0]
    lines = [ln for ln in section.splitlines() if ln.startswith("- ")]
    assert lines == [
        "- cervical collar - Dr. Jones, ortho  (ordered 2026-06-14; +2 earlier, "
        "first 2026-01-05)",
        "- outpatient physical therapy (aquatic)  (ordered 2026-06-14)",
        "- sleep study - home study  (ordered 2026-02-01)",
        "- outpatient physical therapy  (ordered 2026-01-05)",
        "- wheelchair evaluation - seating clinic",
    ]
    # the collapse is render-only: every stored row survives, keys untouched
    conn = db.connect(tmp_path / "cli.db")
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM observation WHERE obs_type='order'"
    ).fetchone()["n"] == 7
    conn.close()
    assert out.isascii()          # cp1252/cp437 console contract


def test_render_on_unmigrated_db_is_friendly(tmp_path, capsys, unmigrated_db):
    # Schema-less DB file, not an absent one - see issue #55's missing-database gate.
    unmigrated_db(tmp_path / "cli.db")
    rc = _run(tmp_path, "render", "summary", "--person", "jane-doe")
    assert rc == 1
    assert "migrate" in capsys.readouterr().err

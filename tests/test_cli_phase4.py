"""End-to-end CLI wiring for phase 4: render summary|brief|journal -- Markdown to
stdout (the §5 redirect contract), --out file convenience, unknown-slug/appointment
rc=1, and unmigrated-DB friendliness."""

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


def test_render_on_unmigrated_db_is_friendly(tmp_path, capsys, unmigrated_db):
    # Schema-less DB file, not an absent one - see issue #55's missing-database gate.
    unmigrated_db(tmp_path / "cli.db")
    rc = _run(tmp_path, "render", "summary", "--person", "jane-doe")
    assert rc == 1
    assert "migrate" in capsys.readouterr().err

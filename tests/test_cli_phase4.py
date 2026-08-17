"""End-to-end CLI wiring for phase 4: render summary|brief|journal -- Markdown to
stdout (the §5 redirect contract), --out file convenience, unknown-slug/appointment
rc=1, and unmigrated-DB friendliness."""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from pemr import cli, db, dedup, persons, render

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


def _commit_labs(tmp_path, sha, rows):
    """`_commit_orders`'s sibling for `lab_result` rows, committed the same way."""
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
    payload.write_text(json.dumps({"lab_result": rows}), encoding="utf-8")
    assert _run(tmp_path, "commit-extraction", "--document", str(doc),
                "--json", str(payload)) == 0


def _commit_procedures(tmp_path, sha, rows):
    """`_commit_orders`'s sibling for `procedure` rows, committed the same way."""
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
    payload.write_text(json.dumps({"procedure": rows}), encoding="utf-8")
    assert _run(tmp_path, "commit-extraction", "--document", str(doc),
                "--json", str(payload)) == 0


def test_render_summary_routine_procedures_come_from_the_dictionary(ready, capsys):
    """Issue #166 at the command boundary: `--dictionary` selects one file and the
    summary reads *both* tables from it -- the synonym map and the render-only
    `[procedures].routine` list. This pins the wiring, not the filter logic (that lives
    in `tests/test_render.py`); without it the arg could go unpassed and every summary
    would silently render unfiltered."""
    tmp_path, _ = ready
    _commit_procedures(tmp_path, "sha-proc", [
        {"name": "Office Visit, Established Patient", "performed_on": "2026-02-01"},
        {"name": "Total knee arthroplasty", "performed_on": "2026-01-15"},
    ])
    d = tmp_path / "dict.toml"
    d.write_text(
        '[synonyms]\n"a1c" = "hba1c"\n\n'
        '[procedures]\nroutine = ["office visit"]\n',
        encoding="utf-8",
    )
    capsys.readouterr()

    assert _run(tmp_path, "render", "summary", "--person", "jane-doe",
                "--dictionary", str(d)) == 0
    out = capsys.readouterr().out
    section = out.split("## Procedures")[1].split("\n## ")[0]
    assert "- 2026-01-15  Total knee arthroplasty" in section
    assert "Office Visit" not in section
    assert "_1 routine procedure not shown" in section
    # render-only: the hidden row is untouched in the DB
    conn = db.connect(tmp_path / "cli.db")
    assert conn.execute("SELECT COUNT(*) AS n FROM procedure").fetchone()["n"] == 2
    conn.close()
    assert out.isascii()          # cp1252/cp437 console contract


def test_render_summary_default_dictionary_applies_the_starter_list(ready, capsys):
    """With no `--dictionary` the CLI falls back to the shipped
    `data/dictionary.example.toml`, exactly as it already does for `[synonyms]` -- so the
    starter routine list is live out of the box, and the run that suppresses discloses it
    while an unlisted procedure still renders (default-show)."""
    tmp_path, _ = ready
    _commit_procedures(tmp_path, "sha-proc2", [
        {"name": "Office Visit, Established Patient", "performed_on": "2026-02-01"},
        {"name": "Total knee arthroplasty", "performed_on": "2026-01-15"},
    ])
    capsys.readouterr()
    assert _run(tmp_path, "render", "summary", "--person", "jane-doe") == 0
    section = capsys.readouterr().out.split("## Procedures")[1].split("\n## ")[0]
    assert "Office Visit" not in section
    assert "- 2026-01-15  Total knee arthroplasty" in section
    assert "_1 routine procedure not shown" in section


def test_render_summary_empty_routine_list_hides_nothing(ready, capsys):
    """The default-show rule survives the front door: a dictionary with an empty list
    suppresses nothing and discloses nothing."""
    tmp_path, _ = ready
    _commit_procedures(tmp_path, "sha-proc3", [
        {"name": "Office Visit, Established Patient", "performed_on": "2026-02-01"},
    ])
    d = tmp_path / "empty.toml"
    d.write_text("[procedures]\nroutine = []\n", encoding="utf-8")
    capsys.readouterr()
    assert _run(tmp_path, "render", "summary", "--person", "jane-doe",
                "--dictionary", str(d)) == 0
    section = capsys.readouterr().out.split("## Procedures")[1].split("\n## ")[0]
    assert "Office Visit, Established Patient" in section
    assert "not shown" not in section


def test_render_summary_groups_repeated_orders_end_to_end(ready, capsys):
    """Issue #93, walked through the real CLI: an order restated by three documents is
    one bullet carrying the latest detail plus the `+N earlier` disclosure; distinct and
    qualifier-bearing items stay their own bullets; a timestamped row shows a bare date;
    an undated row shows none; oldest first, undated last. (Keyless rows are covered in
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
        "- outpatient physical therapy  (ordered 2026-01-05)",
        "- sleep study - home study  (ordered 2026-02-01)",
        "- cervical collar - Dr. Jones, ortho  (ordered 2026-06-14; +2 earlier, "
        "first 2026-01-05)",
        "- outpatient physical therapy (aquatic)  (ordered 2026-06-14)",
        "- wheelchair evaluation - seating clinic",
    ]
    # the collapse is render-only: every stored row survives, keys untouched
    conn = db.connect(tmp_path / "cli.db")
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM observation WHERE obs_type='order'"
    ).fetchone()["n"] == 7
    conn.close()
    assert out.isascii()          # cp1252/cp437 console contract


def test_render_summary_hides_resulted_order_end_to_end(ready, capsys):
    """Issue #128 through the real CLI (the render helpers are private, so the seam only
    stays honest if the behaviour is pinned at the command boundary): the `ready` fixture
    already holds an LDL result collected 2026-01-01, so an LDL order placed that day is
    gone from the section while the referral beside it stays."""
    tmp_path, _ = ready
    _commit_orders(tmp_path, "sha-r1", [
        {"key": "LDL", "value_text": "fasting", "observed_at": "2026-01-01"},
        {"key": "cervical collar", "observed_at": "2026-01-01"},   # no result: still open
    ])
    capsys.readouterr()

    assert _run(tmp_path, "render", "summary", "--person", "jane-doe") == 0
    out = capsys.readouterr().out
    section = out.split("## Orders & Referrals")[1].split("\n## ")[0]
    assert "LDL" not in section
    assert "- cervical collar  (ordered 2026-01-01)" in section
    # render-only: the suppressed order row is untouched in the DB
    conn = db.connect(tmp_path / "cli.db")
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM observation WHERE obs_type='order' AND key='LDL'"
    ).fetchone()["n"] == 1
    conn.close()


def test_render_summary_hides_fully_resulted_panel_order_end_to_end(ready, capsys):
    """Issue #145 through the real CLI, for the same reason #128 is pinned here: the
    decomposition helpers are private, so the only honest proof that a *panel* order
    leaves the section is the command boundary. Rendered **without** `--dictionary`,
    which is also the path where a separator-bearing single analyte (`Ferritin, Serum`)
    can only be rescued by the whole-key arm of the match, never by the dictionary."""
    tmp_path, _ = ready
    _commit_labs(tmp_path, "sha-p-labs", [
        {"test_name": "Sodium", "collected_at": "2026-03-05",
         "value_num": 140, "unit": "mmol/L"},
        {"test_name": "Potassium", "collected_at": "2026-03-05",
         "value_num": 4.1, "unit": "mmol/L"},
        {"test_name": "Chloride", "collected_at": "2026-03-05",
         "value_num": 101, "unit": "mmol/L"},
        {"test_name": "Calcium", "collected_at": "2026-03-05",
         "value_num": 9.4, "unit": "mg/dL"},
        {"test_name": "Ferritin, Serum", "collected_at": "2026-03-05",
         "value_num": 120, "unit": "ng/mL"},
    ])
    _commit_orders(tmp_path, "sha-p-orders", [
        # every analyte resulted in window -> gone (issue #145)
        {"key": "Sodium, Potassium, Chloride", "observed_at": "2026-03-01"},
        # only Calcium resulted -> all-or-nothing keeps the panel outstanding
        {"key": "Calcium, Phosphorus", "observed_at": "2026-03-01"},
        # a comma is part of *this* analyte's name, not structure: #128's whole-key
        # match still reaches it, with no dictionary to declare it
        {"key": "Ferritin, Serum", "observed_at": "2026-03-01"},
        # no separator, so no decomposition: a Sodium result must not close it
        {"key": "Sodium Chloride Infusion", "observed_at": "2026-03-01"},
        {"key": "cervical collar", "observed_at": "2026-03-01"},   # referral: still open
    ])
    capsys.readouterr()

    assert _run(tmp_path, "render", "summary", "--person", "jane-doe") == 0
    out = capsys.readouterr().out
    section = out.split("## Orders & Referrals")[1].split("\n## ")[0]
    assert [ln for ln in section.splitlines() if ln.startswith("- ")] == [
        "- Calcium, Phosphorus  (ordered 2026-03-01)",
        "- cervical collar  (ordered 2026-03-01)",
        "- Sodium Chloride Infusion  (ordered 2026-03-01)",
    ]
    # render-only: every order row, suppressed or not, is untouched in the DB
    conn = db.connect(tmp_path / "cli.db")
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM observation WHERE obs_type='order'"
    ).fetchone()["n"] == 5
    conn.close()
    assert out.isascii()          # cp1252/cp437 console contract


def test_render_summary_keeps_panel_order_with_unreadable_component_end_to_end(
    ready, capsys
):
    """Issue #145's fail-closed rule, pinned at the same command boundary: a compound key
    with one component this layer cannot read (`Extensive Panel` is all noise words; `()`
    normalizes away) is voided whole, so the analytes it *can* read never close it. The
    `Sodium, CBC` control keeps the fix honest -- decomposition still suppresses a panel
    whose every component is both readable and resulted, so this is a narrowing, not a
    disabling."""
    tmp_path, _ = ready
    _commit_labs(tmp_path, "sha-u-labs", [
        {"test_name": "CBC", "collected_at": "2026-03-05", "value_num": 1, "unit": "x"},
        {"test_name": "Sodium", "collected_at": "2026-03-05",
         "value_num": 140, "unit": "mmol/L"},
    ])
    _commit_orders(tmp_path, "sha-u-orders", [
        # a lone CBC result must not suppress an order still naming something unread
        {"key": "CBC, Extensive Panel", "observed_at": "2026-03-01"},
        # both components readable and resulted -> still suppressed
        {"key": "Sodium, CBC", "observed_at": "2026-03-01"},
        # component that normalizes to nothing, same rule
        {"key": "Sodium, ()", "observed_at": "2026-03-02"},
    ])
    capsys.readouterr()

    assert _run(tmp_path, "render", "summary", "--person", "jane-doe") == 0
    out = capsys.readouterr().out
    section = out.split("## Orders & Referrals")[1].split("\n## ")[0]
    assert [ln for ln in section.splitlines() if ln.startswith("- ")] == [
        "- CBC, Extensive Panel  (ordered 2026-03-01)",
        "- Sodium, ()  (ordered 2026-03-02)",
    ]
    conn = db.connect(tmp_path / "cli.db")
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM observation WHERE obs_type='order'"
    ).fetchone()["n"] == 3
    conn.close()
    assert out.isascii()


def test_render_summary_abnormal_labs_are_age_bounded_end_to_end(tmp_path, capsys):
    """Issue #165 walked through the real command. The CLI injects no `now`, so this is
    the one place the section is exercised against the wall clock the way a real record
    ages -- rows are dated relative to today, not to a pinned datetime.

    All four selection rules at once: an in-window draw renders, a stale draw of an
    analyte that also has a recent one does not, and an analyte whose *only* abnormal
    draw is years old still renders -- the keep-latest guard, without which a person on
    a slow draw cadence reads as "nothing flagged" while the marker is live.

    The boundary rows sit a day inside and two days outside the cutoff rather than on it:
    the exact inclusive edge is pinned in `test_summary_abnormal_labs_window_edges_are_exact`,
    which can pin `now`, and this test must stay green across a midnight crossing between
    its own `date.today()` and the render's.
    """
    assert _run(tmp_path, "migrate", "--create") == 0
    assert _run(tmp_path, "person", "add", "--slug", "jane-doe",
                "--name", "Jane Doe", "--dob", "1980-01-01") == 0
    today = date.today()
    cutoff = render._months_before(today, render._ABNORMAL_LABS_WINDOW_MONTHS)
    recent, edge = today - timedelta(days=10), cutoff + timedelta(days=1)
    stale, ancient = cutoff - timedelta(days=2), cutoff - timedelta(days=300)
    _commit_labs(tmp_path, "sha-window-labs", [
        # LDL: a current draw plus the decade-old history the section used to dump
        {"test_name": "LDL", "collected_at": recent.isoformat(),
         "value_num": 161, "unit": "mg/dL", "flag": "H"},
        {"test_name": "LDL", "collected_at": ancient.isoformat(),
         "value_num": 158, "unit": "mg/dL", "flag": "H"},
        # Glucose: a recent draw holds the guard open, so the two older rows test the
        # window itself rather than being retained as the analyte's latest
        {"test_name": "Glucose", "collected_at": edge.isoformat(),
         "value_num": 210, "unit": "mg/dL", "flag": "H"},
        {"test_name": "Glucose", "collected_at": stale.isoformat(),
         "value_num": 205, "unit": "mg/dL", "flag": "H"},
        # TSH: slow cadence, only abnormal draw is out of window -> keep-latest
        {"test_name": "TSH", "collected_at": ancient.isoformat(),
         "value_num": 9.4, "unit": "uIU/mL", "flag": "H"},
        # normal and in window: never in this section at any window
        {"test_name": "HbA1c", "collected_at": (today - timedelta(days=5)).isoformat(),
         "value_num": 5.4, "unit": "%", "ref_low": 4.0, "ref_high": 5.7},
    ])
    capsys.readouterr()

    assert _run(tmp_path, "render", "summary", "--person", "jane-doe") == 0
    out = capsys.readouterr().out
    # heading built from the same constant that drives the filter -- they cannot desync
    heading = f"## Abnormal Labs (last {render._ABNORMAL_LABS_WINDOW_MONTHS} months)"
    assert heading in out
    assert "## Abnormal Labs (last 12 months)" in out
    section = out.split(heading)[1].split("\n## ")[0]
    bullets = [ln for ln in section.splitlines() if ln.startswith("- ")]
    assert [ln.split()[1:3] for ln in bullets] == [
        [recent.isoformat(), "LDL"],           # in window
        [edge.isoformat(), "Glucose"],         # in window, a day inside the cutoff
        [ancient.isoformat(), "TSH"],          # keep-latest: not a false-empty
    ]
    assert stale.isoformat() not in section    # aged out
    assert "HbA1c" not in section              # normal, window is not what excludes it
    assert out.isascii()                       # cp1252/cp437 console contract
    # render-only: every aged-out row is still in the database
    conn = db.connect(tmp_path / "cli.db")
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 6
    conn.close()


def test_render_on_unmigrated_db_is_friendly(tmp_path, capsys, unmigrated_db):
    # Schema-less DB file, not an absent one - see issue #55's missing-database gate.
    unmigrated_db(tmp_path / "cli.db")
    rc = _run(tmp_path, "render", "summary", "--person", "jane-doe")
    assert rc == 1
    assert "migrate" in capsys.readouterr().err

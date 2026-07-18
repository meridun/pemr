"""Phase 5 MCP wrapper (pemr/mcp_server.py).

Two concerns, per issue #12 §3:

* **Tool round-trips** — each tool, called against a seeded scratch DB, returns the same
  structured payload the CLI's engine functions produce; write tools are refused on an
  un-migrated DB with a friendly `ToolError`; read-only tools leave the DB byte-stable.
* **Contract lint** — `AGENTS.md` exists, references every exposed tool, and carries the
  four MUST-rule sections (B1–B4).

The tests import the plain tool functions directly (connection-injected), so the suite
needs neither the `mcp` SDK nor a running stdio server.
"""

import hashlib
from pathlib import Path

import pytest

from pemr import db, dedup, mcp_server, persons

REPO = Path(__file__).resolve().parent.parent
DICT_PATH = REPO / "data" / "dictionary.example.toml"
AGENTS = REPO / "AGENTS.md"


def _doc(conn, slug, ocr=None, doc_date="2026-01-01"):
    pid = conn.execute(
        "SELECT person_id FROM person WHERE slug=?", (slug,)
    ).fetchone()["person_id"]
    cur = conn.execute(
        "INSERT INTO document (sha256, person_id, doc_date, source_path, ocr_text, "
        "ingested_at) VALUES (?, ?, ?, ?, ?, ?)",
        (f"sha-{slug}-{conn.total_changes}", pid, doc_date, "aa/x.pdf", ocr,
         "2026-01-01T00:00:00"),
    )
    conn.commit()
    return cur.lastrowid


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "m.db"


@pytest.fixture()
def seeded(db_path):
    """Migrated DB: one person with labs (incl. a synonym-collapsed HbA1c series),
    a med, and an appointment — enough to exercise every read tool."""
    conn = db.connect(db_path)
    db.migrate(conn)
    persons.add_person(conn, "jane-doe", "Jane Doe")
    d = dedup.load_dictionary(DICT_PATH)
    doc = _doc(conn, "jane-doe", ocr="fasting glucose panel and cholesterol")
    dedup.commit_extraction(conn, doc, {
        "lab_result": [
            {"test_name": "HbA1c", "collected_at": "2024-01-01", "value_num": 5.5, "unit": "%"},
            {"test_name": "A1c", "collected_at": "2026-01-01", "value_num": 6.5, "unit": "%"},
        ],
        "medication": [{"name": "Metformin", "dose": "500mg", "started_on": "2024-02-01"}],
        "appointment": [{"scheduled_for": "2026-02-01", "provider": "Dr. Smith",
                         "specialty": "Endocrinology", "reason": "diabetes follow-up"}],
    }, d)
    yield conn
    conn.close()


def _appt_id(conn):
    return conn.execute("SELECT appointment_id FROM appointment").fetchone()[0]


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# Read-tool round-trips
# --------------------------------------------------------------------------- #

def test_person_list_and_show(seeded):
    people = mcp_server.person_list(seeded)
    assert [p["slug"] for p in people] == ["jane-doe"]
    assert mcp_server.person_show(seeded, slug="jane-doe")["full_name"] == "Jane Doe"


def test_person_show_unknown_raises(seeded):
    with pytest.raises(mcp_server.ToolError, match="no person"):
        mcp_server.person_show(seeded, slug="nobody")


def test_query_kinds(seeded):
    labs = mcp_server.query(seeded, kind="labs", person="jane-doe")
    assert len(labs) == 2
    assert "dedup_key" not in labs[0]  # hidden field stripped, like the CLI
    assert mcp_server.query(seeded, kind="meds", person="jane-doe")[0]["name"] == "Metformin"
    assert mcp_server.query(seeded, kind="timeline", person="jane-doe")


def test_query_unknown_kind_raises(seeded):
    with pytest.raises(mcp_server.ToolError, match="unknown query kind"):
        mcp_server.query(seeded, kind="bogus", person="jane-doe")


def test_find_and_trends(seeded):
    hits = mcp_server.find(seeded, person="jane-doe", query_text="glucose")
    assert hits  # ocr_text was populated, so FTS sees it
    tr = mcp_server.trends(seeded, person="jane-doe", test="a1c")  # dictionary-normalized
    assert tr["count"] == 2
    assert tr["latest"] == 6.5


def test_renderers_return_markdown(seeded):
    assert mcp_server.render_summary(seeded, person="jane-doe")["markdown"].startswith("#")
    brief = mcp_server.render_brief(seeded, appointment=_appt_id(seeded))["markdown"]
    assert "Medication Interaction Review" in brief  # the placeholder AGENTS.md fills
    assert mcp_server.render_journal(seeded, person="jane-doe")["markdown"].startswith("#")


def test_read_tools_leave_db_byte_stable(seeded, db_path):
    seeded.commit()
    before = _sha(db_path)
    mcp_server.person_list(seeded)
    mcp_server.query(seeded, kind="labs", person="jane-doe")
    mcp_server.find(seeded, person="jane-doe", query_text="glucose")
    mcp_server.trends(seeded, person="jane-doe", test="a1c")
    mcp_server.render_summary(seeded, person="jane-doe")
    mcp_server.render_journal(seeded, person="jane-doe")
    assert _sha(db_path) == before


# --------------------------------------------------------------------------- #
# Write-tool round-trips + conflict sign-off
# --------------------------------------------------------------------------- #

def test_person_add_writes(seeded):
    out = mcp_server.person_add(seeded, slug="john-doe", full_name="John Doe")
    assert out["slug"] == "john-doe"
    assert {p["slug"] for p in mcp_server.person_list(seeded)} == {"jane-doe", "john-doe"}


def test_person_edit_writes(seeded):
    out = mcp_server.person_edit(seeded, slug="jane-doe", dob="1981-02-03")
    assert out["dob"] == "1981-02-03"
    assert out["full_name"] == "Jane Doe"  # untouched partial update


def test_person_edit_clears_nullable_with_empty_string(seeded):
    mcp_server.person_edit(seeded, slug="jane-doe", notes="typo")
    out = mcp_server.person_edit(seeded, slug="jane-doe", notes="")
    assert out["notes"] is None


def test_person_edit_unknown_slug_raises(seeded):
    with pytest.raises(mcp_server.ToolError, match="no person"):
        mcp_server.person_edit(seeded, slug="nobody", dob="2000-01-01")


def test_person_edit_no_fields_raises(seeded):
    with pytest.raises(mcp_server.ToolError, match="nothing to update"):
        mcp_server.person_edit(seeded, slug="jane-doe")


def test_ingest_with_agent_ocr_text(seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("PEMR_SOURCES", str(tmp_path / "sources"))
    scan = tmp_path / "scan.txt"
    scan.write_bytes(b"a fresh lab report")
    out = mcp_server.ingest_document(
        seeded, file=str(scan), person="jane-doe",
        ocr_text="Sodium 140 mmol/L; potassium 4.1",
    )
    assert out["status"] == "new"
    assert out["ocr_text_populated"] is True
    assert out["document"]["ocr_text"].startswith("Sodium")


def test_commit_extraction_via_wrapper(seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("PEMR_SOURCES", str(tmp_path / "sources"))
    scan = tmp_path / "s2.txt"
    scan.write_bytes(b"another report")
    doc = mcp_server.ingest_document(seeded, file=str(scan), person="jane-doe",
                                     ocr_text="ldl panel")["document"]["document_id"]
    out = mcp_server.commit_extraction(seeded, document_id=doc, records={
        "lab_result": [{"test_name": "LDL", "collected_at": "2026-03-01",
                        "value_num": 120, "unit": "mg/dL"}],
    })
    assert out["counts"]["new"] == 1


def test_review_conflicts_lists_and_requires_signoff(seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("PEMR_SOURCES", str(tmp_path / "sources"))
    # Stage a conflict: same lab key (person+test+date+rounded value) with a differing unit.
    scan = tmp_path / "c.txt"
    scan.write_bytes(b"conflicting report")
    doc = mcp_server.ingest_document(seeded, file=str(scan), person="jane-doe",
                                     ocr_text="a1c recheck")["document"]["document_id"]
    mcp_server.commit_extraction(seeded, document_id=doc, records={
        "lab_result": [{"test_name": "A1c", "collected_at": "2026-01-01",
                        "value_num": 6.5, "unit": "mmol/mol"}],  # unit differs -> conflict
    })
    conflicts = mcp_server.review_conflicts(seeded)
    assert len(conflicts) == 1
    cid = conflicts[0]["conflict_id"]

    # Resolution without sign-off is refused.
    with pytest.raises(mcp_server.ToolError, match="sign-off"):
        mcp_server.review_conflicts(seeded, resolve=cid)

    # With verbatim sign-off it resolves, and the sign-off is persisted.
    res = mcp_server.review_conflicts(
        seeded, resolve=cid, keep="incoming",
        signoff="Jane said keep the mmol/mol value",
    )
    assert res["resolved"] == cid
    row = seeded.execute(
        "SELECT status, resolution FROM conflict WHERE conflict_id=?", (cid,)
    ).fetchone()
    assert row["status"] == "resolved"
    assert "Jane said keep" in row["resolution"]


def test_write_tools_refused_on_unmigrated_db(tmp_path):
    fresh = db.connect(tmp_path / "empty.db")
    try:
        with pytest.raises(mcp_server.ToolError):
            mcp_server.commit_extraction(fresh, document_id=1, records={})
        with pytest.raises(mcp_server.ToolError):
            mcp_server.ingest_document(fresh, file=str(tmp_path / "x"), person="jane-doe")
    finally:
        fresh.close()


# --------------------------------------------------------------------------- #
# Contract lint
# --------------------------------------------------------------------------- #

def test_agents_md_exists_and_covers_every_tool():
    assert AGENTS.is_file(), "AGENTS.md must exist"
    text = AGENTS.read_text(encoding="utf-8")
    missing = [name for name in mcp_server.TOOL_NAMES if name not in text]
    assert not missing, f"AGENTS.md does not reference: {missing}"


def test_agents_md_has_the_four_must_sections():
    text = AGENTS.read_text(encoding="utf-8")
    for heading in (
        "Extraction naming",
        "OCR text at ingest",
        "Conflict discipline",
        "Medication-interaction section",
    ):
        assert heading in text, f"AGENTS.md missing MUST section: {heading}"


# --------------------------------------------------------------------------- #
# Wire surface — the *registered* tool names must equal TOOL_NAMES (the contract
# lint above only sees the documented strings; this asserts the real MCP surface,
# so a rename can't silently ship names AGENTS.md never mentions). Requires the
# optional `mcp` SDK; skipped when it isn't installed.
# --------------------------------------------------------------------------- #

def test_registered_tool_names_equal_contract():
    pytest.importorskip("mcp")
    server = mcp_server.build_server()
    registered = {t.name for t in server._tool_manager.list_tools()}
    assert registered == set(mcp_server.TOOL_NAMES)

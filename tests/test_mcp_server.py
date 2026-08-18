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

from pemr import (
    __version__, curation, db, dedup, ingest, mcp_server, persons, tombstones,
)

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


def test_query_carries_the_verdict_and_suppresses_nothing(seeded):
    """Issue #131: the structured payload mirrors the CLI's `--json`, not its human
    view — it discloses the verdict and lets the caller decide."""
    base = seeded.execute(
        "SELECT dedup_base FROM medication WHERE name = 'Metformin'"
    ).fetchone()["dedup_base"]
    curation.annotate_record(seeded, "medication", base, status="superseded",
                             note="stopped", apply=True)

    meds = mcp_server.query(seeded, kind="meds", person="jane-doe")
    assert [m["name"] for m in meds] == ["Metformin"]        # nothing hidden
    assert meds[0]["_curation"]["status"] == "superseded"

    labs = mcp_server.query(seeded, kind="labs", person="jane-doe")
    assert all("_curation" not in r for r in labs)           # additive: no verdict, no key

    events = mcp_server.query(seeded, kind="timeline", person="jane-doe")
    assert any(e.get("_curation", {}).get("status") == "superseded" for e in events)
    # `with_identity=True` scaffolding must not reach the payload.
    for key in ("record_type", "dedup_base", "record_id"):
        assert all(key not in e for e in events)


def test_query_with_no_verdicts_carries_no_curation_key(seeded):
    for kind in ("labs", "meds", "timeline"):
        rows = mcp_server.query(seeded, kind=kind, person="jane-doe")
        assert rows and all("_curation" not in r for r in rows)


def test_find_and_trends(seeded):
    hits = mcp_server.find(seeded, person="jane-doe", query_text="glucose")
    assert hits  # ocr_text was populated, so FTS sees it
    assert all(h["person"] == "jane-doe" for h in hits)
    tr = mcp_server.trends(seeded, person="jane-doe", test="a1c")  # dictionary-normalized
    assert tr["count"] == 2
    assert tr["latest"] == 6.5


def test_find_household_wide_omits_person(seeded):
    # person omitted -> whole-household search; each hit attributes its owner
    hits = mcp_server.find(seeded, query_text="glucose")
    assert hits and all(h["person"] == "jane-doe" for h in hits)
    assert mcp_server.find(seeded, query_text="nonesuch-xyz") == []


def test_renderers_return_markdown(seeded):
    summary = mcp_server.render_summary(seeded, person="jane-doe")["markdown"]
    assert summary.startswith("#")
    # Issue #166: the wrapper passes the routine-procedure list, so the section exists
    # and the extra kwarg cannot silently drift into a TypeError.
    assert "## Procedures" in summary
    brief = mcp_server.render_brief(seeded, appointment=_appt_id(seeded))["markdown"]
    assert "Medication Interaction Review" in brief  # the placeholder AGENTS.md fills
    assert mcp_server.render_journal(seeded, person="jane-doe")["markdown"].startswith("#")


def test_render_curation_mirrors_the_cli_target(seeded):
    """Issue #168: the appendix left the three clinical documents, so without this tool
    its content would vanish from every MCP-visible surface. Empty Markdown for an
    uncurated person is the documented state, not a failure."""
    assert mcp_server.render_curation(seeded, person="jane-doe")["markdown"] == ""

    base = seeded.execute(
        "SELECT dedup_base FROM medication WHERE name = 'Metformin'"
    ).fetchone()["dedup_base"]
    curation.annotate_record(seeded, "medication", base, status="superseded",
                             note="duplicate portal import", apply=True)

    md = mcp_server.render_curation(seeded, person="jane-doe")["markdown"]
    assert md.startswith("# Curation Record: Jane Doe")
    assert "medication: Metformin" in md and "duplicate portal import" in md
    # ... and it really is the content the summary no longer carries.
    assert "Metformin" not in mcp_server.render_summary(seeded, person="jane-doe")["markdown"]


def test_render_curation_unknown_person_is_a_friendly_tool_error(seeded):
    with pytest.raises(mcp_server.ToolError):
        mcp_server.render_curation(seeded, person="ghost")


def test_render_summary_narrows_procedures_like_the_cli(seeded):
    """Issue #166, verify pass: the MCP front door must *apply* the routine list, not
    merely accept the kwarg. `test_renderers_return_markdown` asserts only that the
    section exists, which a `_routine_procedures()` stuck at `()` would also satisfy --
    so the two front doors could silently disagree. `_ARGS.dictionary` is unset here,
    exactly as a real server starts, so this resolves the shipped
    `data/dictionary.example.toml` and its starter patterns: the same file and the same
    fallback `pemr render summary` uses."""
    doc = _doc(seeded, "jane-doe")
    dedup.commit_extraction(seeded, doc, {
        "procedure": [
            {"name": "Office Visit, Established Patient", "performed_on": "2026-02-01"},
            {"name": "Total knee arthroplasty", "performed_on": "2026-01-15"},
        ],
    }, {})
    section = mcp_server.render_summary(
        seeded, person="jane-doe"
    )["markdown"].split("## Procedures")[1].split("\n## ")[0]

    assert "- 2026-01-15  Total knee arthroplasty" in section   # default-show
    assert "Office Visit" not in section                        # suppressed
    assert "_1 routine procedure not shown" in section          # and disclosed
    # Render-only: the suppressed row is still in the journal both front doors serve.
    journal = mcp_server.render_journal(seeded, person="jane-doe")["markdown"]
    assert "Office Visit, Established Patient" in journal


def test_read_tools_leave_db_byte_stable(seeded, db_path):
    seeded.commit()
    before = _sha(db_path)
    mcp_server.person_list(seeded)
    mcp_server.query(seeded, kind="labs", person="jane-doe")
    mcp_server.find(seeded, person="jane-doe", query_text="glucose")
    mcp_server.trends(seeded, person="jane-doe", test="a1c")
    mcp_server.render_summary(seeded, person="jane-doe")
    mcp_server.render_journal(seeded, person="jane-doe")
    mcp_server.render_curation(seeded, person="jane-doe")
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
    # issue #61: the owner verdict rides back on every ingest. No identity anchor in a
    # bare lab line -> "unverified", which proceeds.
    assert out["owner_check"]["verdict"] == "unverified"


def test_ingest_refuses_a_mismatched_owner(seeded, tmp_path, monkeypatch):
    """Issue #61 via MCP: a blocking verdict surfaces as a ToolError, and `force`
    is the documented (human-signed-off) override."""
    monkeypatch.setenv("PEMR_SOURCES", str(tmp_path / "sources"))
    scan = tmp_path / "someone-else.txt"
    scan.write_bytes(b"not her record")
    header = "Patient: SMITH, KAREN A    DOB: 09/09/1971"

    with pytest.raises(mcp_server.ToolError, match="owner verification failed"):
        mcp_server.ingest_document(seeded, file=str(scan), person="jane-doe",
                                   ocr_text=header)
    assert seeded.execute(
        "SELECT COUNT(*) FROM document WHERE source_path != 'aa/x.pdf'"
    ).fetchone()[0] == 0

    out = mcp_server.ingest_document(seeded, file=str(scan), person="jane-doe",
                                     ocr_text=header, force=True)
    assert out["status"] == "new"
    assert out["owner_check"]["verdict"] == "suspect"
    assert "SMITH, KAREN A" in out["owner_check"]["evidence"]


def test_ingest_of_tombstoned_content_is_structured_not_an_error(
    seeded, tmp_path, monkeypatch
):
    """Issue #80 via MCP: a tombstone hit is a structured, non-exceptional answer."""
    monkeypatch.setenv("PEMR_SOURCES", str(tmp_path / "sources"))
    scan = tmp_path / "excluded.txt"
    scan.write_bytes(b"a document carrying identifiers")
    tombstones.add_tombstone(
        seeded, ingest.hash_file(scan), reason="identifiers", note="insurance card"
    )

    out = mcp_server.ingest_document(seeded, file=str(scan), person="jane-doe")
    assert out["status"] == "tombstoned"
    assert out["is_tombstoned"] is True
    assert out["document"] is None
    assert out["ocr_text_populated"] is False
    assert out["tombstone"]["reason"] == "identifiers"


def test_agent_cannot_force_past_a_tombstone(seeded, tmp_path, monkeypatch):
    """Deliberately stricter than the owner check: that verdict is a heuristic a human
    may reasonably ask an agent to override, a tombstone *is* the human's decision."""
    monkeypatch.setenv("PEMR_SOURCES", str(tmp_path / "sources"))
    scan = tmp_path / "excluded2.txt"
    scan.write_bytes(b"still excluded")
    sha = ingest.hash_file(scan)
    tombstones.add_tombstone(seeded, sha, reason="identifiers")

    with pytest.raises(mcp_server.ToolError, match="does not override a tombstone"):
        mcp_server.ingest_document(seeded, file=str(scan), person="jane-doe",
                                   force=True)
    assert seeded.execute(
        "SELECT COUNT(*) FROM document WHERE sha256 = ?", (sha,)
    ).fetchone()[0] == 0


def test_ingest_study_directory(seeded, tmp_path, monkeypatch):
    """Issue #69 through MCP: `study` makes `file` a directory, packed as one document."""
    from test_study import make_study  # same synthesized fixtures, one definition

    monkeypatch.setenv("PEMR_SOURCES", str(tmp_path / "sources"))
    out = mcp_server.ingest_document(
        seeded, file=str(make_study(tmp_path / "disc")), person="jane-doe",
        study="dicom",
    )
    assert out["status"] == "new"
    assert out["ocr_text_populated"] is True
    assert out["document"]["source_path"].endswith(".dcm.zip")
    assert out["document"]["category"] == "imaging"
    assert out["document"]["doc_date"] == "2019-04-12"


def test_ingest_study_unknown_kind_is_a_tool_error(seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("PEMR_SOURCES", str(tmp_path / "sources"))
    (tmp_path / "disc").mkdir()
    with pytest.raises(mcp_server.ToolError, match="unknown study kind"):
        mcp_server.ingest_document(seeded, file=str(tmp_path / "disc"),
                                   person="jane-doe", study="ct-raw")


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


# --- document_set_text (ingest completion, issue #62) -----------------------


def test_document_set_text_fills_an_empty_ocr_text(seeded):
    doc = _doc(seeded, "jane-doe", ocr=None)
    out = mcp_server.document_set_text(
        seeded, document_id=doc, text="thyroid panel within range"
    )
    assert out["has_ocr_text"] is True
    assert out["ocr_text_chars"] == len("thyroid panel within range")
    assert "ocr_text" not in out
    # FTS is trigger-maintained, so `find` sees it with no reindex.
    assert mcp_server.find(seeded, query_text="thyroid", person="jane-doe")


def test_document_set_text_refuses_a_populated_document(seeded):
    doc = _doc(seeded, "jane-doe", ocr="the original transcription")
    with pytest.raises(mcp_server.ToolError, match="already has ocr_text"):
        mcp_server.document_set_text(seeded, document_id=doc, text="replacement")
    assert seeded.execute(
        "SELECT ocr_text FROM document WHERE document_id = ?", (doc,)
    ).fetchone()["ocr_text"] == "the original transcription"


def test_document_set_text_exposes_no_force_parameter():
    """Replacing an existing transcription is a human-at-the-CLI action; the tool must
    not hand an agent that lever (AGENTS.md write-tool list)."""
    import inspect

    assert "force" not in inspect.signature(mcp_server.document_set_text).parameters


def test_document_set_text_unknown_id_and_empty_text_raise(seeded):
    with pytest.raises(mcp_server.ToolError, match="no document with id"):
        mcp_server.document_set_text(seeded, document_id=999, text="text")
    doc = _doc(seeded, "jane-doe", ocr=None)
    with pytest.raises(mcp_server.ToolError, match="empty"):
        mcp_server.document_set_text(seeded, document_id=doc, text="   ")


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


def _stage_repeat_draw(seeded, tmp_path, monkeypatch) -> int:
    """One open conflict from two genuine same-day draws (issue #58); returns its id."""
    monkeypatch.setenv("PEMR_SOURCES", str(tmp_path / "sources"))
    draw = {"test_name": "glucose", "collected_at": "2024-04-01"}
    for name, value_text, value_num in (("g1.txt", "fasting", 95),
                                        ("g2.txt", "post-prandial", 148)):
        scan = tmp_path / name
        scan.write_bytes(name.encode())
        doc = mcp_server.ingest_document(seeded, file=str(scan), person="jane-doe",
                                         ocr_text="glucose")["document"]["document_id"]
        mcp_server.commit_extraction(seeded, document_id=doc, records={
            "lab_result": [draw | {"value_num": value_num, "value_text": value_text}],
        })
    return mcp_server.review_conflicts(seeded)[0]["conflict_id"]


def test_review_conflicts_keep_both_requires_signoff(seeded, tmp_path, monkeypatch):
    """`both` admits a row rather than choosing one, so it goes through the *same*
    sign-off gate - no new bypass (AGENTS.md conflict discipline)."""
    cid = _stage_repeat_draw(seeded, tmp_path, monkeypatch)
    with pytest.raises(mcp_server.ToolError, match="sign-off"):
        mcp_server.review_conflicts(seeded, resolve=cid, keep="both")
    assert seeded.execute(
        "SELECT status FROM conflict WHERE conflict_id=?", (cid,)
    ).fetchone()["status"] == "open"


def test_review_conflicts_keep_both_reports_the_admitted_row(seeded, tmp_path,
                                                             monkeypatch):
    cid = _stage_repeat_draw(seeded, tmp_path, monkeypatch)
    res = mcp_server.review_conflicts(
        seeded, resolve=cid, keep="both",
        signoff="Jane confirmed both draws are real",
    )
    assert res["keep"] == "both" and res["record_type"] == "lab_result"
    assert res["occurrence"] == 1 and res["no_op"] is False
    row = seeded.execute(
        "SELECT * FROM lab_result WHERE lab_result_id = ?", (res["row_id"],)
    ).fetchone()
    assert row["value_num"] == 148.0
    assert "Jane confirmed" in seeded.execute(
        "SELECT resolution FROM conflict WHERE conflict_id=?", (cid,)
    ).fetchone()["resolution"]


def test_review_conflicts_listing_reports_family_size(seeded, tmp_path, monkeypatch):
    cid = _stage_repeat_draw(seeded, tmp_path, monkeypatch)
    assert mcp_server.review_conflicts(seeded)[0]["occurrences"] == 1
    mcp_server.review_conflicts(seeded, resolve=cid, keep="both",
                                signoff="Jane confirmed both draws are real")
    # A later conflict on that identity now shows the reviewer that a repeat was admitted.
    scan = tmp_path / "g3.txt"
    scan.write_bytes(b"g3")
    doc = mcp_server.ingest_document(seeded, file=str(scan), person="jane-doe",
                                     ocr_text="glucose")["document"]["document_id"]
    mcp_server.commit_extraction(seeded, document_id=doc, records={
        "lab_result": [{"test_name": "glucose", "collected_at": "2024-04-01",
                        "value_num": 210, "value_text": "third"}],
    })
    assert mcp_server.review_conflicts(seeded)[0]["occurrences"] == 2


def _stage_med_conflict(seeded, tmp_path, monkeypatch, stored, incoming) -> int:
    """One open medication conflict on a single identity (issue #140's shape: a portal
    export refining some fields and silent about others); returns its conflict id."""
    monkeypatch.setenv("PEMR_SOURCES", str(tmp_path / "sources"))
    identity = {"name": "metformin", "dose": "500 mg", "started_on": "2024-01-05"}
    for name, payload in (("m1.txt", stored), ("m2.txt", incoming)):
        scan = tmp_path / name
        scan.write_bytes(name.encode())
        doc = mcp_server.ingest_document(
            seeded, file=str(scan), person="jane-doe", ocr_text="metformin",
        )["document"]["document_id"]
        mcp_server.commit_extraction(seeded, document_id=doc, records={
            "medication": [identity | payload],
        })
    return mcp_server.review_conflicts(seeded)[0]["conflict_id"]


def test_review_conflicts_merge_requires_signoff_and_reports_fields(
    seeded, tmp_path, monkeypatch
):
    """`merge` writes, so it goes through the *same* sign-off gate - no new bypass -
    and its payload names the fields per side without echoing any value."""
    cid = _stage_med_conflict(
        seeded, tmp_path, monkeypatch,
        {"prescriber": "Dr Who", "route": "oral"},
        {"route": "oral", "status": "active"},
    )
    with pytest.raises(mcp_server.ToolError, match="sign-off"):
        mcp_server.review_conflicts(seeded, resolve=cid, keep="merge")
    assert seeded.execute(
        "SELECT status FROM conflict WHERE conflict_id=?", (cid,)
    ).fetchone()["status"] == "open"

    res = mcp_server.review_conflicts(
        seeded, resolve=cid, keep="merge",
        signoff="Jane said take the portal status and keep the prescriber",
    )
    assert res["keep"] == "merge" and res["record_type"] == "medication"
    assert (res["taken"], res["preserved"], res["settled"]) == (
        ["status"], ["prescriber"], {})
    assert "Dr Who" not in str(res)
    row = seeded.execute(
        "SELECT * FROM medication WHERE medication_id = ?", (res["row_id"],)
    ).fetchone()
    assert (row["status"], row["prescriber"]) == ("active", "Dr Who")


def test_review_conflicts_merge_collision_refuses_until_a_field_is_settled(
    seeded, tmp_path, monkeypatch
):
    cid = _stage_med_conflict(
        seeded, tmp_path, monkeypatch,
        {"status": "ordered", "prescriber": "Dr Who"},
        {"status": "completed"},
    )
    with pytest.raises(mcp_server.ToolError, match="status"):
        mcp_server.review_conflicts(seeded, resolve=cid, keep="merge",
                                    signoff="Jane said merge them")
    assert seeded.execute(
        "SELECT status FROM conflict WHERE conflict_id=?", (cid,)
    ).fetchone()["status"] == "open"

    res = mcp_server.review_conflicts(
        seeded, resolve=cid, keep="merge", fields={"status": "incoming"},
        signoff="Jane said the portal status wins",
    )
    assert res["settled"] == {"status": "incoming"}
    row = seeded.execute(
        "SELECT * FROM medication WHERE medication_id = ?", (res["row_id"],)
    ).fetchone()
    assert (row["status"], row["prescriber"]) == ("completed", "Dr Who")


def test_review_conflicts_rejects_an_unknown_keep(seeded, tmp_path, monkeypatch):
    cid = _stage_repeat_draw(seeded, tmp_path, monkeypatch)
    with pytest.raises(mcp_server.ToolError, match="keep must be"):
        mcp_server.review_conflicts(seeded, resolve=cid, keep="neither",
                                    signoff="Jane said neither")


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


def test_server_info_advertises_pemr_version():
    """`serverInfo` must report pemr's version, not the `mcp` SDK's (#60).

    Asserted on the initialize options the server actually puts on the wire, so this
    also catches an SDK change to how the version is resolved.
    """
    pytest.importorskip("mcp")
    opts = mcp_server.build_server()._mcp_server.create_initialization_options()
    assert opts.server_name == "pemr"
    assert opts.server_version == __version__


def test_curation_verbs_are_not_on_the_mcp_surface():
    """Trust boundary (issue #109, the `document rm` / `record rm` precedent): a
    human's clinical verdict is CLI-only. AGENTS.md's blessed write set is
    commit_extraction/person_add/person_edit/ingest/document_set_text, and
    `record annotate` is deliberately not in it - read or write.

    `render_curation` (issue #168) is the one tool that may say "curation" at all, and it
    is a *renderer*: it reads the verdict table the same way `render_summary` reads the
    clinical tables. Writing a verdict is still unreachable from here, which is the
    property this test exists for - so the write surface may never say it."""
    surface = " ".join(mcp_server.TOOL_NAMES).lower()
    for spelling in ("annotate", "record_rm", "record_annotate"):
        assert spelling not in surface, spelling
    writes = " ".join(mcp_server.WRITE_TOOLS).lower()
    for spelling in ("annotate", "curation", "record_rm", "record_annotate"):
        assert spelling not in writes, spelling
    assert [t for t in mcp_server.TOOL_NAMES if "curation" in t] == ["render_curation"]


def test_record_assert_is_not_on_the_mcp_surface():
    """Trust boundary (issue #110): `record assert` is the one path that can put a fact
    in the record with no external source, so it is CLI-only - the same standing as
    `document rm` / `record rm` / `record annotate`. AGENTS.md's blessed write set is
    commit_extraction/person_add/person_edit/ingest/document_set_text."""
    surface = " ".join(mcp_server.TOOL_NAMES).lower()
    for spelling in ("assert", "attest", "record_assert", "attestation"):
        assert spelling not in surface, spelling
    assert "record_assert" not in mcp_server.WRITE_TOOLS


def test_record_edit_is_not_on_the_mcp_surface():
    """Trust boundary (issue #129): `record edit` mutates a stored clinical value with no
    new source behind the change, so the row stops matching what its document says on one
    person's say-so. Same standing as `record assert` — CLI-only, and not in AGENTS.md's
    blessed write set (commit_extraction/person_add/person_edit/ingest/
    document_set_text)."""
    surface = " ".join(mcp_server.TOOL_NAMES).lower()
    for spelling in ("record_edit", "edit_record", "record_edits"):
        assert spelling not in surface, spelling
    assert "record_edit" not in mcp_server.WRITE_TOOLS


# --------------------------------------------------------------------------- #
# self-reported lanes: CLI/MCP verb parity (issue #167)
# --------------------------------------------------------------------------- #

def _attest_self_reports(conn, slug="jane-doe"):
    from pemr import attestations

    for row in (
        {"obs_type": "symptom", "key": "right foot ache",
         "observed_at": "2026-08-16T09:00", "value_num": 3},
        {"obs_type": "activity", "key": "morning walk",
         "observed_at": "2026-08-16T07:30", "value_num": 40, "unit": "min"},
    ):
        attestations.assert_record(
            conn, "observation", slug, row, attributed_to="Jane Doe",
            attested_on="2026-08-18", apply=True,
        )


def test_render_journal_tool_mirrors_the_cli_default(seeded):
    """The MCP front door applies the same default-off filter the CLI does -- the two
    verbs must not disagree about what the journal contains."""
    _attest_self_reports(seeded)
    md = mcp_server.render_journal(seeded, person="jane-doe")["markdown"]
    assert "right foot ache" not in md and "morning walk" not in md


def test_render_journal_tool_forwards_the_opt_in(seeded):
    _attest_self_reports(seeded)
    md = mcp_server.render_journal(
        seeded, person="jane-doe", include_self_reported=True
    )["markdown"]
    assert "symptom right foot ache" in md
    assert "activity morning walk" in md


def test_the_self_reported_lanes_add_no_tool(seeded):
    """`record assert` is CLI-only and stays that way: the lanes are entered through an
    existing verb, and the MCP surface gains a parameter, never a name."""
    assert "record_assert" not in mcp_server.TOOL_NAMES
    assert set(mcp_server.TOOL_NAMES) == set(
        mcp_server.READ_ONLY_TOOLS + mcp_server.WRITE_TOOLS
    )
    assert "render_journal" in mcp_server.READ_ONLY_TOOLS


def test_the_query_tool_still_returns_self_reports(seeded):
    """Only the journal filters. `query timeline` -- CLI and MCP alike -- is the
    complete record it filters from."""
    _attest_self_reports(seeded)
    events = mcp_server.query(seeded, kind="timeline", person="jane-doe")
    assert any("morning walk" in e["summary"] for e in events)
    assert any("right foot ache" in e["summary"] for e in events)

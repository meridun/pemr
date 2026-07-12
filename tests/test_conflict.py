"""Conflict staging + `review-conflicts` resolution."""

import pytest

from pemr import db, dedup, persons


@pytest.fixture()
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    persons.add_person(conn, "jane-doe", "Jane Doe")
    yield conn
    conn.close()


def _doc(conn, sha):
    pid = conn.execute("SELECT person_id FROM person WHERE slug='jane-doe'").fetchone()[0]
    cur = conn.execute(
        "INSERT INTO document (sha256, person_id, source_path, ingested_at) "
        "VALUES (?, ?, ?, ?)",
        (sha, pid, "aa/x.pdf", "2026-01-01T00:00:00"),
    )
    conn.commit()
    return cur.lastrowid


def _lab(value):
    return {"test_name": "hba1c", "collected_at": "2026-01-02", "value_num": value}


@pytest.fixture()
def staged(conn):
    """A single open conflict: stored 5.7, incoming 6.2 (same key bucket)."""
    dedup.commit_extraction(conn, _doc(conn, "doc-a"), {"lab_result": [_lab(5.7)]})
    dedup.commit_extraction(conn, _doc(conn, "doc-b"), {"lab_result": [_lab(6.2)]})
    conflicts = dedup.list_conflicts(conn)
    assert len(conflicts) == 1
    return conflicts[0]["conflict_id"]


def test_list_only_open_by_default(conn, staged):
    assert len(dedup.list_conflicts(conn)) == 1
    assert len(dedup.list_conflicts(conn, status="resolved")) == 0


def test_resolve_keep_existing_leaves_row(conn, staged):
    dedup.resolve_conflict(conn, staged, keep="existing")
    assert conn.execute("SELECT value_num FROM lab_result").fetchone()["value_num"] == 5.7
    row = conn.execute("SELECT * FROM conflict WHERE conflict_id=?", (staged,)).fetchone()
    assert row["status"] == "resolved" and row["resolution"] == "keep-existing"
    assert row["resolved_at"] is not None


def test_resolve_keep_incoming_overwrites_row(conn, staged):
    dedup.resolve_conflict(conn, staged, keep="incoming", note="lab issued correction")
    assert conn.execute("SELECT value_num FROM lab_result").fetchone()["value_num"] == 6.2
    # still exactly one row (overwrite, not insert)
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 1
    row = conn.execute("SELECT * FROM conflict WHERE conflict_id=?", (staged,)).fetchone()
    assert row["status"] == "resolved"
    assert "keep-incoming" in row["resolution"] and "correction" in row["resolution"]


def test_resolve_keep_incoming_preserves_identity_fields(conn):
    # Same fact, but the second scan's OCR differs in identity-field casing AND
    # a payload value. keep-incoming must update the payload + provenance while
    # leaving the stored identity display form ("hba1c") alone.
    doc_a = _doc(conn, "doc-c")
    doc_b = _doc(conn, "doc-d")
    dedup.commit_extraction(conn, doc_a, {"lab_result": [
        {"test_name": "hba1c", "collected_at": "2026-01-02", "value_num": 5.7}]})
    summary = dedup.commit_extraction(conn, doc_b, {"lab_result": [
        {"test_name": "HBA1C", "collected_at": "2026-01-02", "value_num": 6.2}]})
    assert summary.counts["conflict"] == 1
    conflict_id = dedup.list_conflicts(conn)[-1]["conflict_id"]
    dedup.resolve_conflict(conn, conflict_id, keep="incoming")
    row = conn.execute("SELECT * FROM lab_result").fetchone()
    assert row["test_name"] == "hba1c"      # identity display form preserved
    assert row["value_num"] == 6.2          # payload overwritten
    assert row["document_id"] == doc_b      # provenance points at the winner


def test_resolve_unknown_id_raises(conn):
    with pytest.raises(ValueError, match="no conflict"):
        dedup.resolve_conflict(conn, 999, keep="existing")


def test_resolve_already_resolved_raises(conn, staged):
    dedup.resolve_conflict(conn, staged, keep="existing")
    with pytest.raises(ValueError, match="already resolved"):
        dedup.resolve_conflict(conn, staged, keep="existing")


def test_resolve_bad_keep_raises(conn, staged):
    with pytest.raises(ValueError, match="keep must be"):
        dedup.resolve_conflict(conn, staged, keep="whatever")

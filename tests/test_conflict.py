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


# --- keep both: admitting a genuine repeat (issue #58) ------------------------

def _glucose(value, text):
    """The issue's exact repro: two legitimate same-day draws on a date-only report."""
    return {"test_name": "glucose", "collected_at": "2024-04-01",
            "value_num": value, "value_text": text}


@pytest.fixture()
def repeat_draw(conn):
    """One open conflict from two genuine same-day draws, submitted separately."""
    dedup.commit_extraction(conn, _doc(conn, "draw-1"),
                            {"lab_result": [_glucose(95, "fasting draw")]})
    summary = dedup.commit_extraction(
        conn, _doc(conn, "draw-2"),
        {"lab_result": [_glucose(148, "2-hour post-prandial draw")]},
    )
    assert summary.counts == {"new": 0, "duplicate": 0, "conflict": 1}
    return dedup.list_conflicts(conn)[0]["conflict_id"]


def test_keep_both_admits_the_second_draw(conn, repeat_draw):
    result = dedup.resolve_conflict(conn, repeat_draw, keep="both")

    rows = conn.execute(
        "SELECT * FROM lab_result ORDER BY lab_result_id"
    ).fetchall()
    assert [r["value_num"] for r in rows] == [95.0, 148.0]   # both queryable
    assert [r["dedup_occurrence"] for r in rows] == [0, 1]
    assert rows[0]["dedup_base"] == rows[1]["dedup_base"]    # one identity family
    assert rows[0]["dedup_key"] != rows[1]["dedup_key"]      # distinct keys (UNIQUE)
    assert rows[1]["dedup_key"] == dedup.occurrence_key(rows[1]["dedup_base"], 1)

    assert (result.kept, result.record_type) == ("both", "lab_result")
    assert (result.row_id, result.occurrence) == (rows[1]["lab_result_id"], 1)
    assert result.no_op is False


def test_keep_both_records_an_auditable_resolution(conn, repeat_draw):
    dedup.resolve_conflict(conn, repeat_draw, keep="both", note="both draws are real")
    row = conn.execute(
        "SELECT * FROM conflict WHERE conflict_id=?", (repeat_draw,)
    ).fetchone()
    assert row["status"] == "resolved" and row["resolved_at"] is not None
    resolution = row["resolution"]
    assert resolution.startswith("keep-both -> lab_result #2 occurrence=1 key=")
    assert "both draws are real" in resolution
    assert resolution.isascii()      # stored *and* printed; cp1252 console (issue #23)


def test_keep_both_carries_provenance_from_the_conflict(conn, repeat_draw):
    """The admitted row belongs to the document that submitted it, not to the one that
    produced the sibling."""
    dedup.resolve_conflict(conn, repeat_draw, keep="both")
    conflict = conn.execute(
        "SELECT * FROM conflict WHERE conflict_id=?", (repeat_draw,)
    ).fetchone()
    admitted = conn.execute(
        "SELECT * FROM lab_result WHERE dedup_occurrence = 1"
    ).fetchone()
    assert admitted["document_id"] == conflict["document_id"]
    assert admitted["person_id"] == conflict["person_id"]


def test_recommit_of_an_admitted_draw_dedups_instead_of_forking(conn, repeat_draw):
    """The point of family-aware matching: a third commit of the already-admitted
    payload must report `duplicate`, not fork a new occurrence or re-stage a conflict."""
    dedup.resolve_conflict(conn, repeat_draw, keep="both")
    summary = dedup.commit_extraction(
        conn, _doc(conn, "draw-3"),
        {"lab_result": [_glucose(148, "2-hour post-prandial draw")]},
    )
    assert summary.counts == {"new": 0, "duplicate": 1, "conflict": 0}
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 2
    # It deduped against the sibling, not against occurrence 0.
    sibling_key = conn.execute(
        "SELECT dedup_key FROM lab_result WHERE dedup_occurrence = 1"
    ).fetchone()["dedup_key"]
    assert summary.duplicate == [("lab_result", sibling_key)]


def test_a_changed_value_on_an_admitted_identity_restages_a_conflict(conn, repeat_draw):
    """Family matching must not swallow genuinely new facts: a third *different* value
    on that identity is still a conflict for a human to adjudicate."""
    dedup.resolve_conflict(conn, repeat_draw, keep="both")
    summary = dedup.commit_extraction(
        conn, _doc(conn, "draw-4"),
        {"lab_result": [_glucose(210, "third draw")]},
    )
    assert summary.counts == {"new": 0, "duplicate": 0, "conflict": 1}
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 2


def test_keep_both_numbers_a_third_occurrence(conn, repeat_draw):
    dedup.resolve_conflict(conn, repeat_draw, keep="both")
    dedup.commit_extraction(conn, _doc(conn, "draw-4"),
                            {"lab_result": [_glucose(210, "third draw")]})
    third = dedup.list_conflicts(conn)[0]["conflict_id"]
    result = dedup.resolve_conflict(conn, third, keep="both")
    assert result.occurrence == 2
    assert [r["dedup_occurrence"] for r in conn.execute(
        "SELECT dedup_occurrence FROM lab_result ORDER BY lab_result_id"
    )] == [0, 1, 2]


def test_keep_both_is_idempotent_across_conflicts_from_one_payload(conn):
    """Two documents each staging the same repeat: resolving both `keep both` must not
    produce twin rows."""
    dedup.commit_extraction(conn, _doc(conn, "d1"),
                            {"lab_result": [_glucose(95, "fasting draw")]})
    payload = {"lab_result": [_glucose(148, "2-hour post-prandial draw")]}
    dedup.commit_extraction(conn, _doc(conn, "d2"), payload)
    dedup.commit_extraction(conn, _doc(conn, "d3"), payload)
    first, second = [c["conflict_id"] for c in dedup.list_conflicts(conn)]

    dedup.resolve_conflict(conn, first, keep="both")
    result = dedup.resolve_conflict(conn, second, keep="both")

    assert result.no_op is True
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 2
    resolution = conn.execute(
        "SELECT resolution FROM conflict WHERE conflict_id=?", (second,)
    ).fetchone()["resolution"]
    assert resolution == "keep-both (no-op: matches lab_result #2)"


def test_keep_both_revalidates_the_staged_payload(conn, repeat_draw):
    """The staged JSON becomes a row, so it is re-validated at resolution time — a
    conflict hand-edited to something unschematic must not land."""
    conn.execute(
        "UPDATE conflict SET incoming_json = ? WHERE conflict_id = ?",
        ('{"test_name": "glucose", "collected_at": "not-a-date"}', repeat_draw),
    )
    conn.commit()
    with pytest.raises(dedup.ValidationError, match="collected_at"):
        dedup.resolve_conflict(conn, repeat_draw, keep="both")
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 1
    assert conn.execute(
        "SELECT status FROM conflict WHERE conflict_id=?", (repeat_draw,)
    ).fetchone()["status"] == "open"


def test_keep_both_works_for_observation_rows(conn):
    """#56-era screening/immunization rows carry the same date-only exposure."""
    obs = {"obs_type": "screening", "key": "mammogram", "observed_at": "2024-04-01"}
    dedup.commit_extraction(conn, _doc(conn, "o1"),
                            {"observation": [obs | {"value_text": "left"}]})
    dedup.commit_extraction(conn, _doc(conn, "o2"),
                            {"observation": [obs | {"value_text": "right"}]})
    conflict_id = dedup.list_conflicts(conn)[0]["conflict_id"]
    result = dedup.resolve_conflict(conn, conflict_id, keep="both")
    assert result.occurrence == 1
    assert [r["value_text"] for r in conn.execute(
        "SELECT value_text FROM observation ORDER BY observation_id"
    )] == ["left", "right"]


def test_keep_existing_and_incoming_still_return_a_result(conn, staged):
    """Source compatibility: the return type changed from None, but the older
    resolutions still add no row."""
    result = dedup.resolve_conflict(conn, staged, keep="existing")
    assert (result.kept, result.row_id, result.occurrence) == ("existing", None, None)
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 1

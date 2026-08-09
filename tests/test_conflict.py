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
    assert summary.counts == {"new": 0, "duplicate": 0, "enriched": 0, "conflict": 1, "promoted": 0}
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
    assert summary.counts == {"new": 0, "duplicate": 1, "enriched": 0, "conflict": 0, "promoted": 0}
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
    assert summary.counts == {"new": 0, "duplicate": 0, "enriched": 0, "conflict": 1, "promoted": 0}
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


# --- orphaned families: the conflict key is the base, not the anchor's key ----

def _drop_occurrence(conn, occurrence):
    """Remove one sibling the way `document rm --apply` would (its owning document
    went away), leaving a hole in the family."""
    conn.execute("DELETE FROM lab_result WHERE dedup_occurrence = ?", (occurrence,))
    conn.commit()


@pytest.fixture()
def orphaned_family(conn, repeat_draw):
    """A family whose occurrence 0 is gone, with a fresh conflict staged against the
    surviving occurrence-1 sibling.

    The conflict's ``dedup_key`` is the family *base*; the anchor row's own key is
    ``hash(base|1)``. Any resolution addressing the row by the conflict's key matches
    nothing here.
    """
    dedup.resolve_conflict(conn, repeat_draw, keep="both")
    _drop_occurrence(conn, 0)
    summary = dedup.commit_extraction(
        conn, _doc(conn, "draw-5"), {"lab_result": [_glucose(210, "third draw")]}
    )
    assert summary.counts == {"new": 0, "duplicate": 0, "enriched": 0, "conflict": 1, "promoted": 0}
    conflict = dedup.list_conflicts(conn)[0]
    survivor = conn.execute("SELECT * FROM lab_result").fetchone()
    # The premise of the regression: key-targeted writes cannot find this row.
    assert conflict["dedup_key"] == survivor["dedup_base"] != survivor["dedup_key"]
    return conflict["conflict_id"]


def test_keep_incoming_overwrites_the_anchor_when_occurrence_zero_is_gone(
    conn, orphaned_family
):
    """Regression: keep-incoming used to UPDATE ... WHERE dedup_key = <conflict key>,
    which matches zero rows once occurrence 0 is gone - the conflict was stamped
    `resolved` and the incoming value silently vanished."""
    result = dedup.resolve_conflict(conn, orphaned_family, keep="incoming")

    rows = conn.execute("SELECT * FROM lab_result").fetchall()
    assert len(rows) == 1                       # overwrite, not insert
    assert rows[0]["value_num"] == 210.0        # the staged value actually landed
    assert rows[0]["dedup_occurrence"] == 1     # on the surviving sibling
    assert (result.kept, result.row_id) == ("incoming", rows[0]["lab_result_id"])
    assert (result.occurrence, result.dedup_key) == (1, rows[0]["dedup_key"])
    assert conn.execute(
        "SELECT status FROM conflict WHERE conflict_id=?", (orphaned_family,)
    ).fetchone()["status"] == "resolved"


def test_keep_both_still_admits_into_an_orphaned_family(conn, orphaned_family):
    """The other half of the shape: max(occurrence)+1 already handled the hole, and
    must keep doing so."""
    result = dedup.resolve_conflict(conn, orphaned_family, keep="both")
    assert result.occurrence == 2
    assert [(r["dedup_occurrence"], r["value_num"]) for r in conn.execute(
        "SELECT * FROM lab_result ORDER BY lab_result_id"
    )] == [(1, 148.0), (2, 210.0)]


def test_keep_incoming_refuses_when_the_family_is_empty(conn, orphaned_family):
    """Nothing left to overwrite is a refusal, never a reported success: resolving
    would otherwise discard the staged value with rc 0."""
    _drop_occurrence(conn, 1)
    with pytest.raises(ValueError, match="no stored lab_result row left"):
        dedup.resolve_conflict(conn, orphaned_family, keep="incoming")
    row = conn.execute(
        "SELECT * FROM conflict WHERE conflict_id=?", (orphaned_family,)
    ).fetchone()
    assert (row["status"], row["resolved_at"]) == ("open", None)


def test_empty_family_refusal_names_the_keep_both_recovery(conn, orphaned_family):
    """The refusal is a dead end unless it points somewhere: keep-both admits the row
    at occurrence 0 and is the operator's way out."""
    _drop_occurrence(conn, 1)
    with pytest.raises(ValueError) as exc:
        dedup.resolve_conflict(conn, orphaned_family, keep="incoming")
    assert "keep 'both'" in str(exc.value)
    assert str(exc.value).isascii()          # printed by the CLI (issue #23)

    result = dedup.resolve_conflict(conn, orphaned_family, keep="both")
    assert (result.occurrence, result.no_op) == (0, False)
    assert conn.execute("SELECT value_num FROM lab_result").fetchone()["value_num"] == 210.0


def test_keep_existing_still_resolves_an_empty_family(conn, orphaned_family):
    """keep-existing drops the incoming row by design, so it stays a legal resolution
    even with nothing stored - it writes nothing either way."""
    _drop_occurrence(conn, 1)
    result = dedup.resolve_conflict(conn, orphaned_family, keep="existing")
    assert result.kept == "existing"
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 0


# --- dictionary drift: a conflict's stored key is not the live base -----------
#
# `rekey` rewrites dedup_key/dedup_base on the record tables and does not touch the
# `conflict` table, so a dictionary edit made while a conflict is open strands it on a
# key no row carries. Resolutions must re-derive the base from the payload: numbering a
# keep-both row on the stale key inserts a row whose key is not derivable from its own
# columns, which wedges `rekey` (and therefore `document reassign`) for the whole DB.

_RENAMED = {"glucose": "glucose, plasma"}


@pytest.fixture()
def drifted(conn, repeat_draw):
    """The `repeat_draw` conflict, with the dictionary changed and `rekey` applied
    underneath it: the stored row moved to a new base, the conflict did not."""
    report = dedup.rekey(conn, _RENAMED, apply=True)
    assert report.applied and len(report.changes) == 1
    conflict = dedup.list_conflicts(conn)[0]
    stored = conn.execute("SELECT * FROM lab_result").fetchone()
    # The premise: the conflict's key is stale, nothing carries it any more.
    assert conflict["dedup_key"] != stored["dedup_base"]
    return conflict["conflict_id"]


def test_keep_both_after_rekey_joins_the_live_family(conn, drifted):
    """Regression: the admitted row used to land at the stale base as occurrence 0,
    forking the identity and making both rows recompute to one key."""
    result = dedup.resolve_conflict(conn, drifted, keep="both", dictionary=_RENAMED)

    rows = conn.execute("SELECT * FROM lab_result ORDER BY lab_result_id").fetchall()
    assert [r["value_num"] for r in rows] == [95.0, 148.0]
    assert [r["dedup_occurrence"] for r in rows] == [0, 1]
    assert rows[0]["dedup_base"] == rows[1]["dedup_base"]     # one family, not two
    assert (result.occurrence, result.dedup_key) == (1, rows[1]["dedup_key"])


def test_rekey_still_works_after_a_drifted_keep_both(conn, drifted):
    """The wedge itself: an underivable key made `rekey` raise a collision on every
    later dictionary edit, for every table, with no CLI way out."""
    dedup.resolve_conflict(conn, drifted, keep="both", dictionary=_RENAMED)

    assert dedup.rekey(conn, _RENAMED).changes == []          # already canonical
    later = dedup.rekey(conn, {"glucose": "glu"}, apply=True)  # a further edit
    assert len(later.changes) == 2                             # family moves together
    rows = conn.execute("SELECT * FROM lab_result ORDER BY lab_result_id").fetchall()
    assert rows[0]["dedup_base"] == rows[1]["dedup_base"]
    assert rows[0]["dedup_key"] != rows[1]["dedup_key"]


def test_keep_incoming_after_rekey_overwrites_the_live_row(conn, drifted):
    """`keep incoming` refused a drifted conflict outright (empty family at the stale
    key) and pointed the operator at the broken keep-both path."""
    result = dedup.resolve_conflict(conn, drifted, keep="incoming", dictionary=_RENAMED)

    rows = conn.execute("SELECT * FROM lab_result").fetchall()
    assert len(rows) == 1 and rows[0]["value_num"] == 148.0
    assert result.row_id == rows[0]["lab_result_id"]


def test_conflict_occurrences_counts_the_live_family(conn, drifted):
    """The CLI/MCP listing hint read 0 occurrences for a family that exists."""
    conflict = dedup.list_conflicts(conn)[0]
    assert dedup.conflict_occurrences(conn, conflict, _RENAMED) == 1
    dedup.resolve_conflict(conn, drifted, keep="both", dictionary=_RENAMED)
    staged_again = dedup.commit_extraction(
        conn, _doc(conn, "draw-6"),
        {"lab_result": [_glucose(210, "third draw")]}, _RENAMED,
    )
    assert staged_again.counts["conflict"] == 1
    assert dedup.conflict_occurrences(
        conn, dedup.list_conflicts(conn)[0], _RENAMED
    ) == 2


def test_keep_both_before_rekey_stays_with_the_stored_family(conn, repeat_draw):
    """Dictionary edited but `rekey` not yet run: the stored rows still carry the
    staged key, so the admitted sibling must join *them* — splitting it off onto the
    freshly derived base would collide the two the moment `rekey` runs."""
    result = dedup.resolve_conflict(
        conn, repeat_draw, keep="both", dictionary=_RENAMED
    )
    assert result.occurrence == 1

    rows = conn.execute("SELECT * FROM lab_result ORDER BY lab_result_id").fetchall()
    assert rows[0]["dedup_base"] == rows[1]["dedup_base"]
    report = dedup.rekey(conn, _RENAMED, apply=True)           # no collision
    assert len(report.changes) == 2


# --- fusing drift: the derived base holds someone *else's* family --------------
#
# A dictionary edit that maps two distinct analytes onto one canonical name puts an
# unrelated, pre-existing family on the base a conflict re-derives to. `rekey` refuses
# to run in that state, so the database stays there. The conflict's own staged key must
# win whenever it still has rows, or a resolution acts on the wrong record.

_FUSED = {"a1c": "hba1c"}


def _named(test_name, value):
    return {"test_name": test_name, "collected_at": "2026-01-02", "value_num": value}


@pytest.fixture()
def fused(conn):
    """Two distinct identities — `a1c` (one row) and `hba1c` (two occurrences) — plus
    an open conflict staged against the *a1c* row, under a dictionary that fuses the
    two names. The families are deliberately different sizes so every assertion below
    names which one was read."""
    dedup.commit_extraction(conn, _doc(conn, "fuse-a"),
                            {"lab_result": [_named("a1c", 5.7)]})
    dedup.commit_extraction(conn, _doc(conn, "fuse-b"),
                            {"lab_result": [_named("hba1c", 9.9)]})
    dedup.commit_extraction(conn, _doc(conn, "fuse-c"),
                            {"lab_result": [_named("hba1c", 10.4)]})
    dedup.resolve_conflict(conn, dedup.list_conflicts(conn)[0]["conflict_id"],
                           keep="both")               # hba1c family: occurrences 0, 1
    summary = dedup.commit_extraction(conn, _doc(conn, "fuse-d"),
                                      {"lab_result": [_named("a1c", 6.2)]})
    assert summary.counts["conflict"] == 1

    conflict = dedup.list_conflicts(conn)[0]
    a1c, hba1c = conn.execute(
        "SELECT * FROM lab_result ORDER BY lab_result_id"
    ).fetchall()[:2]
    # The premise: under _FUSED the conflict re-derives onto the *hba1c* family's base,
    # while its own staged key still names the a1c row.
    assert conflict["dedup_key"] == a1c["dedup_base"] != hba1c["dedup_base"]
    assert dedup._derive_base(conflict, _FUSED) == hba1c["dedup_base"]
    # The state itself: rekey cannot clear it — lab_result collides, so that table is
    # skipped and stays on its stored keys.
    assert dedup.rekey(conn, _FUSED).blocked == ["lab_result"]
    return conflict["conflict_id"]


def test_keep_incoming_under_fusing_drift_overwrites_the_conflicts_own_row(conn, fused):
    """Regression: preferring the re-derived base overwrote an unrelated `hba1c`
    result — destroying a real value and reprovenancing it — while the row the operator
    asked to overwrite stayed untouched, all at rc 0."""
    result = dedup.resolve_conflict(conn, fused, keep="incoming", dictionary=_FUSED)

    rows = conn.execute("SELECT * FROM lab_result ORDER BY lab_result_id").fetchall()
    assert [(r["test_name"], r["value_num"]) for r in rows] == [
        ("a1c", 6.2), ("hba1c", 9.9), ("hba1c", 10.4),
    ]
    assert result.row_id == rows[0]["lab_result_id"]
    assert [r["document_id"] for r in rows[1:]] == [2, 3]   # bystanders' provenance


def test_keep_both_under_fusing_drift_joins_the_staged_family(conn, fused):
    """The admitted repeat belongs to the identity the conflict was staged against,
    not to the unrelated family the fused name points at."""
    result = dedup.resolve_conflict(conn, fused, keep="both", dictionary=_FUSED)

    rows = conn.execute("SELECT * FROM lab_result ORDER BY lab_result_id").fetchall()
    assert len(rows) == 4
    a1c, hba1c, admitted = rows[0], rows[1], rows[3]
    assert admitted["dedup_base"] == a1c["dedup_base"] != hba1c["dedup_base"]
    assert (result.occurrence, admitted["dedup_occurrence"]) == (1, 1)
    assert [r["value_num"] for r in rows[1:3]] == [9.9, 10.4]     # untouched


def test_conflict_occurrences_under_fusing_drift_counts_the_staged_family(conn, fused):
    """The listing hint counted the unrelated family (2) instead of the conflict's
    own (1)."""
    conflict = dedup.list_conflicts(conn)[0]
    assert dedup.conflict_occurrences(conn, conflict, _FUSED) == 1


def test_keep_existing_and_incoming_still_return_a_result(conn, staged):
    """Source compatibility: the return type changed from None, but the older
    resolutions still add no row."""
    result = dedup.resolve_conflict(conn, staged, keep="existing")
    assert (result.kept, result.row_id, result.occurrence) == ("existing", None, None)
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 1

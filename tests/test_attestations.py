"""Human-attested records: `pemr record assert` (issue #110).

Covers the module surface (pemr/attestations.py) and the CLI wiring, in the shape
test_records.py uses for `record rm`. The two things this feature cannot get wrong get
their own tests: an attested row must key exactly like a document-sourced one, and it must
never be indistinguishable from a sourced fact.
"""

import json

import pytest

from pemr import attestations, cli, curation, db, dedup, persons, query, records

MED = {"name": "Metformin", "dose": "500 mg", "started_on": "2025-06-01"}


@pytest.fixture()
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def jane(conn):
    return persons.add_person(conn, "jane-doe", "Jane Doe")


def _document(conn, person_id, sha="aa11bb22"):
    cur = conn.execute(
        "INSERT INTO document (sha256, person_id, source_path, ingested_at) "
        "VALUES (?, ?, ?, '2026-01-01T00:00:00')",
        (sha, person_id, f"{sha[:2]}/{sha}.pdf"),
    )
    conn.commit()
    return int(cur.lastrowid)


def _assert_med(conn, payload=None, **kwargs):
    return attestations.assert_record(
        conn, "medication", "jane-doe", dict(payload or MED),
        attributed_to=kwargs.pop("attributed_to", "Mom"),
        attested_on=kwargs.pop("attested_on", "2026-08-09"),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# assert_record: dry run, write, and the derived identity
# --------------------------------------------------------------------------- #

def test_dry_run_writes_nothing_and_reports_the_branch(conn, jane):
    report = _assert_med(conn)
    assert report.outcome == "new"
    assert report.applied is False
    assert report.row_id is None
    assert report.label == "Metformin"
    assert report.fields == MED
    assert report.dedup_key == report.dedup_base
    assert conn.execute("SELECT COUNT(*) AS n FROM medication").fetchone()["n"] == 0


def test_apply_inserts_one_unsourced_row_with_the_attestation(conn, jane):
    report = _assert_med(conn, apply=True)
    assert report.applied is True
    row = conn.execute("SELECT * FROM medication").fetchone()
    assert int(row["medication_id"]) == report.row_id
    assert row["document_id"] is None
    assert row["attested_by"] == "Mom"
    assert row["attested_on"] == "2026-08-09"
    assert row["attested_at"] == report.attested_at
    assert row["name"] == "Metformin"
    assert dedup.attestation_state(row) == "attested"
    assert dedup.is_attested(row) is True


def test_attested_row_keys_exactly_like_a_document_sourced_one(conn, jane):
    """The AC that makes "attested rows dedup normally" mean something: the key is
    derived from the payload and the person, and provenance is not part of it."""
    report = _assert_med(conn, apply=True)
    expected = dedup.dedup_key("medication", MED, jane.person_id)
    row = conn.execute("SELECT * FROM medication").fetchone()
    assert row["dedup_key"] == expected
    assert row["dedup_base"] == expected
    assert report.dedup_base == expected
    assert int(row["dedup_occurrence"]) == 0


def test_attested_row_is_indexed_for_find_like_any_other(conn, jane):
    """The FTS triggers are provenance-blind, so an unsourced fact is still findable."""
    _assert_med(conn, apply=True)
    hits = query.find(conn, "jane-doe", "Metformin")
    assert [h["source_table"] for h in hits] == ["medication"]
    assert hits[0]["document_id"] is None


# --------------------------------------------------------------------------- #
# Validation — every refusal writes nothing
# --------------------------------------------------------------------------- #

def _nothing_written(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM medication").fetchone()["n"] == 0


def test_unknown_record_type_is_refused(conn, jane):
    with pytest.raises(ValueError, match="unknown record type"):
        attestations.assert_record(
            conn, "family_history", "jane-doe", {}, attributed_to="Mom",
            attested_on="2026-08-09", apply=True,
        )


def test_unknown_person_is_refused(conn, jane):
    with pytest.raises(query.PersonNotFoundError):
        attestations.assert_record(
            conn, "medication", "nobody", dict(MED), attributed_to="Mom",
            attested_on="2026-08-09", apply=True,
        )
    assert _nothing_written(conn)


def test_unknown_field_is_refused(conn, jane):
    with pytest.raises(dedup.ValidationError, match="unknown field"):
        _assert_med(conn, payload=MED | {"nope": "x"}, apply=True)
    assert _nothing_written(conn)


def test_missing_required_field_is_refused(conn, jane):
    with pytest.raises(dedup.ValidationError, match="missing required field"):
        _assert_med(conn, payload={"dose": "500 mg"}, apply=True)
    assert _nothing_written(conn)


def test_bad_payload_type_is_refused(conn, jane):
    with pytest.raises(dedup.ValidationError, match="expected str"):
        attestations.assert_record(
            conn, "lab_result", "jane-doe",
            {"test_name": "HbA1c", "collected_at": "2026-01-02", "unit": 5},
            attributed_to="Mom", attested_on="2026-08-09", apply=True,
        )


def test_non_iso_attestation_date_is_refused(conn, jane):
    with pytest.raises(ValueError, match="--date"):
        _assert_med(conn, attested_on="08/09/2026", apply=True)
    assert _nothing_written(conn)


def test_empty_attribution_is_refused(conn, jane):
    with pytest.raises(ValueError, match="attributed-to"):
        _assert_med(conn, attributed_to="   ", apply=True)
    assert _nothing_written(conn)


def test_key_drift_still_refuses_an_assert(conn, jane):
    """`_assert_no_key_drift` is one of the three gates an assert shares with an ingest:
    a stored row on a stale key would be forked rather than collided with."""
    doc = _document(conn, jane.person_id)
    dedup.commit_extraction(conn, doc, {"medication": [MED]})
    conn.execute("UPDATE medication SET dedup_key = 'stale', dedup_base = 'stale'")
    conn.commit()
    with pytest.raises(dedup.DictionaryDriftError, match="pemr rekey"):
        _assert_med(conn, apply=True)


# --------------------------------------------------------------------------- #
# Collision — refused, never staged
# --------------------------------------------------------------------------- #

def test_asserting_a_fact_already_on_record_reports_duplicate(conn, jane):
    doc = _document(conn, jane.person_id)
    dedup.commit_extraction(conn, doc, {"medication": [MED]})
    report = _assert_med(conn, apply=True)
    assert report.outcome == "duplicate"
    assert report.existing_provenance == f"document #{doc}"
    assert report.row_id == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM medication").fetchone()["n"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM medication WHERE attested_by IS NOT NULL"
    ).fetchone()["n"] == 0


def test_asserting_a_differing_payload_is_refused_not_staged(conn, jane):
    doc = _document(conn, jane.person_id)
    dedup.commit_extraction(conn, doc, {"medication": [MED | {"frequency": "BID"}]})
    with pytest.raises(attestations.AttestationCollisionError) as exc:
        _assert_med(conn, payload=MED | {"frequency": "daily"}, apply=True)
    assert "medication 1" in str(exc.value)
    assert "document #1" in str(exc.value)
    # Refused, not staged: no conflict row, and the stored value is untouched.
    assert conn.execute("SELECT COUNT(*) AS n FROM conflict").fetchone()["n"] == 0
    assert conn.execute(
        "SELECT frequency FROM medication"
    ).fetchone()["frequency"] == "BID"


def test_the_collision_message_names_an_attestation_as_such(conn, jane):
    _assert_med(conn, apply=True)
    with pytest.raises(attestations.AttestationCollisionError, match="attestation"):
        _assert_med(conn, payload=MED | {"frequency": "daily"}, apply=True)


# --------------------------------------------------------------------------- #
# The collision check consults the curation overlay (issue #133)
#
# Before this, `record assert` resolved a collision from `dedup.load_family`'s raw SQL,
# which never sees the `curation` table: a row the human had already ruled `superseded`
# still blocked, and the error's own suggested remedy (`record annotate`) provably could
# not work. Released rows now stop blocking; `disputed` and unverdicted ones do not.
# --------------------------------------------------------------------------- #

def _rule(conn, target, status="superseded", *, row=False, **kwargs):
    """Record one verdict the way an operator would — `record annotate`'s API."""
    return curation.annotate_record(
        conn, "medication", str(target), status=status,
        note="ruled on during the test", row=row, apply=True, **kwargs,
    )


def _base(conn, jane):
    return dedup.dedup_key("medication", MED, jane.person_id)


def _stored_row_id(conn, occurrence=0):
    return int(conn.execute(
        "SELECT medication_id FROM medication WHERE dedup_occurrence = ?",
        (occurrence,),
    ).fetchone()["medication_id"])


def test_a_released_row_no_longer_blocks_a_differing_attestation(conn, jane):
    """The defect, inverted: annotating the colliding row now does what the error said."""
    _assert_med(conn, apply=True)
    _rule(conn, _stored_row_id(conn), row=True)
    report = _assert_med(conn, payload=MED | {"frequency": "daily"}, apply=True)
    assert report.outcome == "new"
    assert report.applied is True
    assert conn.execute("SELECT COUNT(*) AS n FROM medication").fetchone()["n"] == 2


@pytest.mark.parametrize("status", curation.APPENDIX_STATUSES)
def test_every_appendix_status_releases_the_identity(conn, jane, status):
    """The vocabulary is `APPENDIX_STATUSES`, not three literals — a status added there
    must release here too, or the two halves of "leaves the live view" have drifted."""
    _assert_med(conn, apply=True)
    extra = (
        {"merged_into_base": _base(conn, jane)} if status == "merged-into" else {}
    )
    _rule(conn, _stored_row_id(conn), status=status, row=True, **extra)
    assert _assert_med(
        conn, payload=MED | {"frequency": "daily"}, apply=True
    ).outcome == "new"


def test_a_family_scoped_release_also_unblocks(conn, jane):
    _assert_med(conn, apply=True)
    _rule(conn, _base(conn, jane))
    assert _assert_med(
        conn, payload=MED | {"frequency": "daily"}, apply=True
    ).outcome == "new"


@pytest.mark.parametrize("scope_row", [True, False])
def test_a_disputed_row_still_blocks(conn, jane, scope_row):
    """`disputed` is deliberately not in APPENDIX_STATUSES: the row still holds the
    identity, it is merely flagged."""
    _assert_med(conn, apply=True)
    target = _stored_row_id(conn) if scope_row else _base(conn, jane)
    _rule(conn, target, status="disputed", row=scope_row)
    with pytest.raises(attestations.AttestationCollisionError):
        _assert_med(conn, payload=MED | {"frequency": "daily"}, apply=True)


def test_an_unverdicted_row_still_blocks(conn, jane):
    _assert_med(conn, apply=True)
    with pytest.raises(attestations.AttestationCollisionError):
        _assert_med(conn, payload=MED | {"frequency": "daily"}, apply=True)


def test_one_unverdicted_sibling_still_blocks_a_released_family(conn, jane):
    """A released row must not mask an unresolved one — and must not be the row the
    error tells you to annotate."""
    _assert_med(conn, apply=True)
    first = _stored_row_id(conn)
    _rule(conn, first, row=True)
    _assert_med(conn, payload=MED | {"frequency": "daily"}, apply=True)
    second = _stored_row_id(conn, occurrence=1)

    with pytest.raises(attestations.AttestationCollisionError) as exc:
        _assert_med(conn, payload=MED | {"frequency": "TID"}, apply=True)
    message = str(exc.value)
    assert f"medication {second}" in message
    assert f"medication {first}" not in message
    assert "1 other row(s) in this family already carry a releasing verdict" in message


def test_row_scope_beats_family_scope_in_the_collision_check(conn, jane):
    """Both directions, through `VerdictMap.for_row` — no second precedence rule."""
    _assert_med(conn, apply=True)
    row_id, base = _stored_row_id(conn), _base(conn, jane)

    _rule(conn, base, status="superseded")
    _rule(conn, row_id, status="disputed", row=True)
    with pytest.raises(attestations.AttestationCollisionError):
        _assert_med(conn, payload=MED | {"frequency": "daily"}, apply=True)

    _rule(conn, base, status="disputed")
    _rule(conn, row_id, status="superseded", row=True)
    assert _assert_med(
        conn, payload=MED | {"frequency": "daily"}, apply=True
    ).outcome == "new"


def test_the_collision_message_stays_honest(conn, jane):
    """The bug this issue is about: never recommend a remedy that cannot work."""
    _assert_med(conn, apply=True)
    row_id = _stored_row_id(conn)

    # Unverdicted: today's advice, which does work.
    with pytest.raises(attestations.AttestationCollisionError) as bare:
        _assert_med(conn, payload=MED | {"frequency": "daily"}, apply=True)
    assert f"pemr record rm medication {row_id}" in str(bare.value)
    assert f"pemr record annotate medication {row_id} --row" in str(bare.value)

    # Verdicted but not releasing: say which verdict it carries, and which ones release.
    _rule(conn, row_id, status="disputed", row=True)
    with pytest.raises(attestations.AttestationCollisionError) as ruled:
        _assert_med(conn, payload=MED | {"frequency": "daily"}, apply=True)
    message = str(ruled.value)
    assert "already carries the verdict 'disputed'" in message
    for status in curation.APPENDIX_STATUSES:
        assert f"`{status}`" in message


def test_a_released_row_matching_the_payload_still_reports_duplicate(conn, jane):
    """Twin semantics are untouched: matching stays `dedup._rows_equal`'s call."""
    _assert_med(conn, apply=True)
    _rule(conn, _stored_row_id(conn), row=True)
    report = _assert_med(conn, apply=True)
    assert report.outcome == "duplicate"
    assert conn.execute("SELECT COUNT(*) AS n FROM medication").fetchone()["n"] == 1


def test_the_unblocked_write_takes_the_next_free_occurrence(conn, jane):
    """`dedup_key` is UNIQUE: an unblocked write at occurrence 0 would raise
    IntegrityError against its released sibling instead of landing."""
    _assert_med(conn, apply=True)
    _rule(conn, _stored_row_id(conn), row=True)
    _assert_med(conn, payload=MED | {"frequency": "daily"}, apply=True)
    _rule(conn, _stored_row_id(conn, occurrence=1), row=True)
    base = _base(conn, jane)

    dry = _assert_med(conn, payload=MED | {"frequency": "TID"})
    assert dry.dedup_occurrence == 2
    assert dry.dedup_key == dedup.occurrence_key(base, 2)
    assert conn.execute("SELECT COUNT(*) AS n FROM medication").fetchone()["n"] == 2

    written = _assert_med(conn, payload=MED | {"frequency": "TID"}, apply=True)
    assert written.dedup_occurrence == 2
    assert written.dedup_key == dedup.occurrence_key(base, 2)
    row = conn.execute(
        "SELECT * FROM medication WHERE medication_id = ?", (written.row_id,)
    ).fetchone()
    assert row["dedup_key"] == dedup.occurrence_key(base, 2)
    assert row["dedup_base"] == base
    assert len(dedup.load_family(conn, "medication", base)) == 3


def test_an_unblocked_write_steps_over_a_hole_left_by_record_rm(conn, jane):
    """The occupied-occurrence seam: `record rm` on a middle sibling leaves a hole, and
    the next unblocked write must take `max + 1` over what is still stored — not the
    hole, whose key belongs to no live row, and not a number a live sibling already
    holds (that would be `UNIQUE(dedup_key)` instead of a landing). Same rule, same
    idiom, as `dedup._resolve_keep_both`."""
    base = _base(conn, jane)
    for occurrence, frequency in enumerate((None, "daily", "TID")):
        payload = MED if frequency is None else MED | {"frequency": frequency}
        _assert_med(conn, payload=payload, apply=True)
        _rule(conn, _stored_row_id(conn, occurrence=occurrence), row=True)

    records.remove_record(
        conn, "medication", _stored_row_id(conn, occurrence=1), apply=True
    )
    written = _assert_med(conn, payload=MED | {"frequency": "QID"}, apply=True)
    assert written.dedup_occurrence == 3
    assert written.dedup_key == dedup.occurrence_key(base, 3)
    stored = {
        int(row["dedup_occurrence"])
        for row in dedup.load_family(conn, "medication", base)
    }
    assert stored == {0, 2, 3}


def test_an_unverdicted_database_is_byte_identical(conn, jane):
    """The additive-only AC: with no verdicts at all, nothing about this path moved."""
    report = _assert_med(conn, apply=True)
    assert report.dedup_occurrence == 0
    assert report.dedup_key == report.dedup_base
    row = conn.execute("SELECT * FROM medication").fetchone()
    assert row["dedup_key"] == row["dedup_base"]
    assert int(row["dedup_occurrence"]) == 0
    with pytest.raises(attestations.AttestationCollisionError) as exc:
        _assert_med(conn, payload=MED | {"frequency": "daily"}, apply=True)
    assert "`pemr record rm medication 1`" in str(exc.value)
    assert "already carries the verdict" not in str(exc.value)


# --------------------------------------------------------------------------- #
# A `distinct` verdict also releases the identity (issue #186)
#
# The stopgap for #152's episode model: a genuine second occurrence of a recurring fact
# collides with the stored one, and every remedy #133 left was wrong for it — `record rm`
# deletes a sourced row, an appendix status pulls a live fact out of its clinical section,
# and editing the date overwrites what a document said. `distinct` (#122) is the verdict
# that means exactly "two real facts, one coincidental key collision", so it releases here
# too — while staying out of APPENDIX_STATUSES, which is what keeps both rows rendering.
# --------------------------------------------------------------------------- #

def test_a_distinct_row_no_longer_blocks_a_differing_attestation(conn, jane):
    """The motivating case: the second occurrence lands beside the first, not over it."""
    _assert_med(conn, apply=True)
    first = _stored_row_id(conn)
    _rule(conn, first, status="distinct", row=True)
    report = _assert_med(conn, payload=MED | {"frequency": "daily"}, apply=True)
    assert report.outcome == "new"
    assert report.applied is True
    assert report.dedup_occurrence == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM medication").fetchone()["n"] == 2
    # The row ruled `distinct` is still there, untouched - nothing was replaced.
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM medication WHERE medication_id = ?", (first,)
    ).fetchone()["n"] == 1


def test_a_distinct_release_dry_runs_without_writing(conn, jane):
    """The dry-run-by-default invariant reaches the newly unblocked path too: it must
    report the write it *would* make, and make none."""
    _assert_med(conn, apply=True)
    _rule(conn, _stored_row_id(conn), status="distinct", row=True)
    report = _assert_med(conn, payload=MED | {"frequency": "daily"})
    assert report.outcome == "new"
    assert report.applied is False
    assert report.dedup_occurrence == 1
    assert report.dedup_key == dedup.occurrence_key(_base(conn, jane), 1)
    assert conn.execute("SELECT COUNT(*) AS n FROM medication").fetchone()["n"] == 1


def test_a_family_scoped_distinct_also_unblocks(conn, jane):
    """Row-beats-family (#114) needed no second rule for `distinct`: both scopes work
    because `_blocking_rows` reads what `annotate_rows` stamped."""
    _assert_med(conn, apply=True)
    _rule(conn, _base(conn, jane), status="distinct")
    assert _assert_med(
        conn, payload=MED | {"frequency": "daily"}, apply=True
    ).outcome == "new"


def test_distinct_composes_with_an_appendix_release(conn, jane):
    """`distinct` is a second, independent check - it adds to the appendix release
    rather than replacing it, so a mixed family unblocks."""
    _assert_med(conn, apply=True)
    _rule(conn, _stored_row_id(conn), status="distinct", row=True)
    _assert_med(conn, payload=MED | {"frequency": "daily"}, apply=True)
    _rule(conn, _stored_row_id(conn, occurrence=1), status="superseded", row=True)

    report = _assert_med(conn, payload=MED | {"frequency": "TID"}, apply=True)
    assert report.outcome == "new"
    assert report.dedup_occurrence == 2
    assert len(dedup.load_family(conn, "medication", _base(conn, jane))) == 3


@pytest.mark.parametrize("status", ["disputed", "confirmed"])
@pytest.mark.parametrize("scope_row", [True, False])
def test_a_non_releasing_verdict_still_blocks(conn, jane, status, scope_row):
    """Widened, not removed. `confirmed` in particular sounds settling and is neither an
    appendix status nor `distinct` - it affirms the row, so the row keeps the identity."""
    _assert_med(conn, apply=True)
    target = _stored_row_id(conn) if scope_row else _base(conn, jane)
    _rule(conn, target, status=status, row=scope_row)
    with pytest.raises(attestations.AttestationCollisionError):
        _assert_med(conn, payload=MED | {"frequency": "daily"}, apply=True)


def test_one_unverdicted_sibling_still_blocks_a_distinct_released_family(conn, jane):
    """The disclosure arithmetic counts distinct-released siblings, and the message
    still names the row that actually blocks - never the released one."""
    _assert_med(conn, apply=True)
    first = _stored_row_id(conn)
    _rule(conn, first, status="distinct", row=True)
    _assert_med(conn, payload=MED | {"frequency": "daily"}, apply=True)
    second = _stored_row_id(conn, occurrence=1)

    with pytest.raises(attestations.AttestationCollisionError) as exc:
        _assert_med(conn, payload=MED | {"frequency": "TID"}, apply=True)
    message = str(exc.value)
    assert f"medication {second}" in message
    assert f"medication {first}" not in message
    assert "1 other row(s) in this family already carry a releasing verdict" in message


def test_the_collision_message_offers_the_distinct_path(conn, jane):
    """AC4: both branches must name `distinct` as a live remedy, and must keep naming
    the appendix statuses - the vocabulary is interpolated, never hand-typed."""
    _assert_med(conn, apply=True)
    row_id = _stored_row_id(conn)

    with pytest.raises(attestations.AttestationCollisionError) as bare:
        _assert_med(conn, payload=MED | {"frequency": "daily"}, apply=True)
    unverdicted = str(bare.value)

    _rule(conn, row_id, status="disputed", row=True)
    with pytest.raises(attestations.AttestationCollisionError) as ruled:
        _assert_med(conn, payload=MED | {"frequency": "daily"}, apply=True)
    verdicted = str(ruled.value)

    for message in (unverdicted, verdicted):
        assert "`distinct`" in message
        assert (
            f"pemr record annotate medication {row_id} --row --status distinct" in message
        )
    # Still honest about the older remedies.
    assert f"pemr record rm medication {row_id}" in unverdicted
    assert "already carries the verdict 'disputed'" in verdicted
    for status in curation.APPENDIX_STATUSES:
        assert f"`{status}`" in verdicted


def test_a_distinct_row_matching_the_payload_still_reports_duplicate(conn, jane):
    """Twin semantics are untouched: a released row is still `dedup._rows_equal`'s call,
    so an *equal* payload reports duplicate rather than filing a second copy."""
    _assert_med(conn, apply=True)
    _rule(conn, _stored_row_id(conn), status="distinct", row=True)
    report = _assert_med(conn, apply=True)
    assert report.outcome == "duplicate"
    assert conn.execute("SELECT COUNT(*) AS n FROM medication").fetchone()["n"] == 1


# --------------------------------------------------------------------------- #
# list_attested — the "needs source" queue
# --------------------------------------------------------------------------- #

def test_list_attested_reports_only_live_attestations_by_default(conn, jane):
    doc = _document(conn, jane.person_id)
    dedup.commit_extraction(conn, doc, {
        "lab_result": [{"test_name": "HbA1c", "collected_at": "2026-01-02"}]
    })
    _assert_med(conn, apply=True)
    rows = attestations.list_attested(conn)
    assert [(r["record_type"], r["label"], r["needs_source"]) for r in rows] == [
        ("medication", "Metformin", True)
    ]
    assert rows[0]["person"] == "jane-doe"
    assert rows[0]["attested_by"] == "Mom"
    assert rows[0]["document_id"] is None


def test_list_attested_can_include_superseded_rows(conn, jane):
    _assert_med(conn, apply=True)
    doc = _document(conn, jane.person_id)
    dedup.commit_extraction(conn, doc, {"medication": [MED]})
    assert attestations.list_attested(conn) == []
    rows = attestations.list_attested(conn, include_superseded=True)
    assert [(r["needs_source"], r["document_id"]) for r in rows] == [(False, doc)]


def test_list_attested_filters_by_record_type(conn, jane):
    _assert_med(conn, apply=True)
    attestations.assert_record(
        conn, "allergy", "jane-doe", {"substance": "Penicillin"},
        attributed_to="Mom", attested_on="2026-08-09", apply=True,
    )
    assert [r["record_type"] for r in attestations.list_attested(conn, "allergy")] == [
        "allergy"
    ]
    with pytest.raises(ValueError, match="unknown record type"):
        attestations.list_attested(conn, "family_history")


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #

def _run(tmp_path, *argv):
    return cli.main(["--db", str(tmp_path / "cli.db"), *argv])


@pytest.fixture()
def cli_ready(tmp_path):
    assert _run(tmp_path, "migrate", "--create") == 0
    assert _run(tmp_path, "person", "add", "--slug", "jane-doe", "--name", "Jane") == 0
    return tmp_path


_ASSERT_ARGV = (
    "record", "assert", "medication", "--person", "jane-doe",
    "--attributed-to", "Mom", "--date", "2026-08-09",
    "--field", "name=Metformin", "--field", "dose=500 mg",
    "--field", "started_on=2025-06-01",
)


def test_cli_assert_dry_run(cli_ready, capsys):
    assert _run(cli_ready, *_ASSERT_ARGV) == 0
    out = capsys.readouterr().out
    assert "attested by Mom on 2026-08-09 (no source document)" in out
    assert "dry run: nothing was written" in out
    assert _run(cli_ready, "record", "assert", "--list") == 0
    assert "no attested records" in capsys.readouterr().out


def test_cli_assert_apply_then_list(cli_ready, capsys):
    assert _run(cli_ready, *_ASSERT_ARGV, "--apply") == 0
    assert "wrote medication #1" in capsys.readouterr().out
    assert _run(cli_ready, "record", "assert", "--list") == 0
    out = capsys.readouterr().out
    assert "Metformin" in out and "needs source" in out and "Mom" in out


def test_cli_assert_json_shape_is_stable(cli_ready, capsys):
    assert _run(cli_ready, *_ASSERT_ARGV, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "record_type", "person", "person_id", "attributed_to", "attested_on",
        "attested_at", "label", "fields", "dedup_key", "dedup_base",
        "dedup_occurrence", "outcome", "row_id", "existing_provenance", "applied",
    }
    assert payload["outcome"] == "new"
    assert payload["applied"] is False
    assert payload["fields"]["name"] == "Metformin"

    assert _run(cli_ready, *_ASSERT_ARGV, "--apply", "--json") == 0
    written = json.loads(capsys.readouterr().out)
    assert written["applied"] is True and written["row_id"] == 1

    assert _run(cli_ready, "record", "assert", "--list", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert set(rows[0]) == {
        "record_type", "row_id", "person", "label", "attested_by", "attested_on",
        "attested_at", "document_id", "needs_source", "dedup_base",
    }


def test_cli_assert_numeric_fields_are_coerced(cli_ready, capsys):
    assert _run(
        cli_ready, "record", "assert", "lab_result", "--person", "jane-doe",
        "--attributed-to", "Mom", "--date", "2026-08-09",
        "--field", "test_name=HbA1c", "--field", "collected_at=2026-01-02",
        "--field", "value_num=5.7", "--field", "unit=%", "--apply", "--json",
    ) == 0
    assert json.loads(capsys.readouterr().out)["fields"]["value_num"] == 5.7


def test_cli_assert_unknown_field_is_argparse_misuse(cli_ready):
    with pytest.raises(SystemExit) as exc:
        _run(cli_ready, "record", "assert", "medication", "--person", "jane-doe",
             "--attributed-to", "Mom", "--date", "2026-08-09", "--field", "nope=1")
    assert exc.value.code == 2


def test_cli_assert_requires_a_payload(cli_ready):
    with pytest.raises(SystemExit) as exc:
        _run(cli_ready, "record", "assert", "medication", "--person", "jane-doe",
             "--attributed-to", "Mom", "--date", "2026-08-09")
    assert exc.value.code == 2


def test_cli_assert_requires_attribution(cli_ready):
    with pytest.raises(SystemExit) as exc:
        _run(cli_ready, "record", "assert", "medication", "--person", "jane-doe",
             "--date", "2026-08-09", "--field", "name=Metformin")
    assert exc.value.code == 2


def test_cli_assert_bad_date_fails_friendly(cli_ready, capsys):
    assert _run(
        cli_ready, "record", "assert", "medication", "--person", "jane-doe",
        "--attributed-to", "Mom", "--date", "08/09/2026",
        "--field", "name=Metformin", "--apply",
    ) == 1
    assert "error:" in capsys.readouterr().err


def test_cli_assert_collision_fails_friendly(cli_ready, capsys):
    assert _run(cli_ready, *_ASSERT_ARGV, "--apply") == 0
    capsys.readouterr()
    assert _run(cli_ready, *_ASSERT_ARGV, "--field", "frequency=BID", "--apply") == 1
    assert "already holds this identity" in capsys.readouterr().err


def _seed_cli_family(path, count):
    """`count` document-sourced rows on one identity — the #130 repro's shape."""
    conn = db.connect(path / "cli.db")
    person_id = int(
        conn.execute("SELECT person_id FROM person").fetchone()["person_id"]
    )
    doc = _document(conn, person_id, "cc33dd44")
    base = dedup.dedup_key("medication", MED, person_id)
    ids = [
        dedup._insert_record(
            conn, "medication", MED | {"frequency": f"{occ + 1}x daily"},
            person_id, doc, base, occ,
        )
        for occ in range(count)
    ]
    conn.commit()
    conn.close()
    return ids


def test_cli_annotating_every_colliding_row_unblocks_the_assert(cli_ready, capsys):
    """The #130 repro end to end: four stored rows hold one identity, the assert is
    refused, the error's own remedy is run on all four, and the identical assert lands.
    Before the fix the second assert refused byte-identically to the first."""
    ids = _seed_cli_family(cli_ready, 4)
    argv = (*_ASSERT_ARGV, "--field", "status=discontinued")
    assert _run(cli_ready, *argv, "--apply") == 1
    assert "already holds this identity" in capsys.readouterr().err

    for row_id in ids:
        assert _run(
            cli_ready, "record", "annotate", "medication", str(row_id), "--row",
            "--status", "superseded", "--note", "replaced by the current order",
            "--apply",
        ) == 0
    capsys.readouterr()

    assert _run(cli_ready, *argv, "--apply") == 0
    assert "wrote medication #5" in capsys.readouterr().out


def test_cli_annotating_distinct_unblocks_the_assert(cli_ready, capsys):
    """The #186 operator flow end to end, through verbs that already existed: the assert
    is refused, the error names the `distinct` path, the operator rules the stored row
    `distinct`, and the retry lands a second occurrence beside a sourced row nothing
    deleted or edited. No new verb or flag was needed (`--status distinct` is #122's)."""
    ids = _seed_cli_family(cli_ready, 1)
    argv = (*_ASSERT_ARGV, "--field", "status=discontinued")
    assert _run(cli_ready, *argv, "--apply") == 1
    err = capsys.readouterr().err
    assert "already holds this identity" in err
    assert "--row --status distinct" in err

    assert _run(
        cli_ready, "record", "annotate", "medication", str(ids[0]), "--row",
        "--status", "distinct", "--note", "a second, unrelated occurrence", "--apply",
    ) == 0
    capsys.readouterr()

    assert _run(cli_ready, *argv, "--apply") == 0
    assert "wrote medication #2" in capsys.readouterr().out
    conn = db.connect(cli_ready / "cli.db")
    try:
        rows = conn.execute(
            "SELECT dedup_occurrence FROM medication ORDER BY dedup_occurrence"
        ).fetchall()
    finally:
        conn.close()
    assert [int(row["dedup_occurrence"]) for row in rows] == [0, 1]


def test_cli_assert_discloses_a_family_scoped_release(cli_ready, capsys):
    """The one surprising outcome of the fix: a *family* verdict covers the row just
    written, so it must be disclosed rather than reported as a plain success."""
    ids = _seed_cli_family(cli_ready, 1)
    assert _run(
        cli_ready, "record", "annotate", "medication", str(ids[0]),
        "--status", "superseded", "--note", "family ruling", "--apply",
    ) == 0
    capsys.readouterr()
    argv = (*_ASSERT_ARGV, "--field", "status=discontinued")
    assert _run(cli_ready, *argv, "--apply") == 0
    out = capsys.readouterr().out
    assert "family-scoped 'superseded' verdict" in out
    assert "wrote medication #2" in out

    # A row-scoped release gets the plain occurrence note instead, not this one: re-scope
    # the family ruling onto the two rows it meant, then lift it.
    for row_id in (str(ids[0]), "2"):
        assert _run(
            cli_ready, "record", "annotate", "medication", row_id, "--row",
            "--status", "superseded", "--note", "row ruling", "--apply",
        ) == 0
    assert _run(
        cli_ready, "record", "annotate", "medication", str(ids[0]), "--clear", "--apply"
    ) == 0
    capsys.readouterr()
    assert _run(cli_ready, *argv, "--field", "route=oral", "--apply") == 0
    out = capsys.readouterr().out
    assert "family-scoped" not in out
    assert "takes occurrence 2 of the identity" in out

    # The --json contract is unchanged on this path (the #110 key set).
    assert _run(cli_ready, *argv, "--field", "route=oral", "--json") == 0
    assert set(json.loads(capsys.readouterr().out)) == {
        "record_type", "person", "person_id", "attributed_to", "attested_on",
        "attested_at", "label", "fields", "dedup_key", "dedup_base",
        "dedup_occurrence", "outcome", "row_id", "existing_provenance", "applied",
    }


def test_cli_walk_from_attestation_to_a_source_document(cli_ready, capsys):
    """The whole point, end to end: attest a fact with no document, see it flagged as
    needing a source, then ingest the document that proves it and watch the row be
    promoted rather than duplicated."""
    assert _run(cli_ready, *_ASSERT_ARGV, "--apply") == 0
    capsys.readouterr()

    scan = cli_ready / "note.txt"
    scan.write_bytes(b"metformin 500 mg daily")
    assert _run(cli_ready, "ingest", str(scan), "--person", "jane-doe",
                "--sources", str(cli_ready / "sources")) == 0
    payload = cli_ready / "extract.json"
    payload.write_text(json.dumps({"medication": [MED]}), encoding="utf-8")
    capsys.readouterr()
    assert _run(cli_ready, "commit-extraction", "--document", "1",
                "--json", str(payload)) == 0
    out = capsys.readouterr().out
    assert "0 new, 0 duplicate" in out
    assert "now backed by this document" in out

    # Off the needs-source queue, still on record as an attestation.
    assert _run(cli_ready, "record", "assert", "--list") == 0
    assert "no attested records" in capsys.readouterr().out
    assert _run(cli_ready, "record", "assert", "--list", "--all", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["needs_source"], r["document_id"], r["attested_by"]) for r in rows] == [
        (False, 1, "Mom")
    ]


def test_cli_assert_on_an_unmigrated_db_is_friendly(tmp_path, capsys, unmigrated_db):
    unmigrated_db(tmp_path / "cli.db")
    assert _run(tmp_path, *_ASSERT_ARGV, "--apply") == 1
    assert "error:" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# One read contract, two front doors
# --------------------------------------------------------------------------- #

def test_public_row_keeps_an_unattested_payload_byte_identical(conn, jane):
    """The additive-only AC for the read contract: three NULL columns must not appear on
    every row of every query just because the schema grew."""
    doc = _document(conn, jane.person_id)
    dedup.commit_extraction(conn, doc, {"medication": [MED]})
    row = conn.execute("SELECT * FROM medication").fetchone()
    payload = dedup.public_row(row)
    assert not set(payload) & set(dedup.ATTESTATION_COLUMNS)
    assert not set(payload) & set(dedup.INTERNAL_COLUMNS)
    # The correction mark (migration 016, issue #134) obeys the same rule.
    assert not set(payload) & set(dedup.EDIT_MARK_COLUMNS)
    assert payload["document_id"] == doc


def test_public_row_discloses_provenance_on_an_attested_row(conn, jane):
    _assert_med(conn, apply=True)
    payload = dedup.public_row(conn.execute("SELECT * FROM medication").fetchone())
    assert payload["attested_by"] == "Mom"
    assert payload["attested_on"] == "2026-08-09"
    assert payload["document_id"] is None


def test_the_mcp_read_payload_uses_the_same_rule(conn, jane):
    """A payload that discloses provenance at the CLI but not over MCP would be the one
    drift this feature cannot afford."""
    from pemr import mcp_server

    _assert_med(conn, apply=True)
    over_mcp = mcp_server.query(conn, kind="meds", person="jane-doe")
    from pemr import cli as _cli

    at_cli = [_cli._clean(r) for r in query.query_meds(conn, "jane-doe")]
    assert over_mcp == at_cli
    assert over_mcp[0]["attested_by"] == "Mom"


def test_cli_assert_a_functional_observation_without_a_document(cli_ready, capsys):
    """Issue #132: AGENTS.md §2 names `record assert` as the home for a functional fact
    known only from family knowledge, so that path has to honour the family's rules —
    the row lands unsourced, and an undated one is refused like any other."""
    argv = (
        "record", "assert", "observation", "--person", "jane-doe",
        "--attributed-to", "Daughter", "--date", "2026-08-09",
        "--field", "obs_type=functional", "--field", "key=meal_regularity",
        "--field", "observed_at=2026-08-01",
        "--field", "value_text=one meal most days",
    )
    assert _run(cli_ready, *argv, "--apply") == 0
    assert "wrote observation #1" in capsys.readouterr().out
    assert _run(cli_ready, "record", "assert", "--list") == 0
    assert "needs source" in capsys.readouterr().out

    assert _run(
        cli_ready, "record", "assert", "observation", "--person", "jane-doe",
        "--attributed-to", "Daughter", "--date", "2026-08-09",
        "--field", "obs_type=functional", "--field", "key=meal_regularity", "--apply",
    ) == 1
    assert "missing required field 'observed_at'" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# self-reported symptom / activity lanes (issue #167)
# --------------------------------------------------------------------------- #

SYMPTOM = {"obs_type": "symptom", "key": "right foot ache",
           "observed_at": "2026-08-16T09:00", "value_num": 3,
           "value_text": "achy after the walk"}


def _assert_obs(conn, payload, **kwargs):
    return attestations.assert_record(
        conn, "observation", "jane-doe", dict(payload),
        attributed_to=kwargs.pop("attributed_to", "Jane Doe"),
        attested_on=kwargs.pop("attested_on", "2026-08-18"),
        **kwargs,
    )


def test_a_symptom_asserts_through_the_existing_path(conn, jane):
    """T4: no new CLI verb and no migration -- `record assert` already carries exactly
    the right shape (a fact attributed to a person, dated, with no source document)."""
    report = _assert_obs(conn, SYMPTOM, apply=True)
    assert report.applied is True
    row = conn.execute("SELECT * FROM observation").fetchone()
    assert row["obs_type"] == "symptom"
    assert row["key"] == "right foot ache"
    assert row["value_num"] == 3
    assert row["value_text"] == "achy after the walk"
    assert row["document_id"] is None
    assert dedup.attestation_state(row) == "attested"


def test_an_activity_asserts_through_the_existing_path(conn, jane):
    _assert_obs(conn, {"obs_type": "activity", "key": "morning walk",
                       "observed_at": "2026-08-16T07:30", "value_num": 40,
                       "unit": "min"}, apply=True)
    row = conn.execute("SELECT * FROM observation").fetchone()
    assert row["obs_type"] == "activity" and row["key"] == "morning walk"


def test_asserting_a_self_report_never_touches_the_problem_list(conn, jane):
    """The load-bearing invariant: these lanes live entirely inside the `observation`
    catch-all and bypass the curation-verdict pipeline that governs `condition`."""
    _assert_obs(conn, SYMPTOM, apply=True)
    _assert_obs(conn, {"obs_type": "activity", "key": "morning walk",
                       "observed_at": "2026-08-16T07:30"}, apply=True)
    assert conn.execute("SELECT COUNT(*) AS n FROM condition").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM allergy").fetchone()["n"] == 0


def test_a_keyless_symptom_assert_is_refused(conn, jane):
    with pytest.raises(dedup.ValidationError, match="missing required field 'key'"):
        _assert_obs(conn, {"obs_type": "symptom", "observed_at": "2026-08-16T09:00"},
                    apply=True)
    assert conn.execute("SELECT COUNT(*) AS n FROM observation").fetchone()["n"] == 0


def test_a_date_only_symptom_assert_is_refused(conn, jane):
    """The precision rule reaches the real write path, not just `validate_row`: without
    it two reports of one complaint on one day would collapse into a single row."""
    with pytest.raises(dedup.ValidationError, match="requires a time of day"):
        _assert_obs(conn, dict(SYMPTOM, observed_at="2026-08-16"), apply=True)
    assert conn.execute("SELECT COUNT(*) AS n FROM observation").fetchone()["n"] == 0


def test_two_same_day_symptom_asserts_land_as_two_rows(conn, jane):
    """The AC the whole D1 decision exists for."""
    morning = _assert_obs(conn, SYMPTOM, apply=True)
    evening = _assert_obs(
        conn, dict(SYMPTOM, observed_at="2026-08-16T21:00", value_num=6), apply=True
    )
    assert evening.outcome == "new"
    assert morning.dedup_key != evening.dedup_key
    assert conn.execute("SELECT COUNT(*) AS n FROM observation").fetchone()["n"] == 2

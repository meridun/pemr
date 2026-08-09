"""Human-attested records: `pemr record assert` (issue #110).

Covers the module surface (pemr/attestations.py) and the CLI wiring, in the shape
test_records.py uses for `record rm`. The two things this feature cannot get wrong get
their own tests: an attested row must key exactly like a document-sourced one, and it must
never be indistinguishable from a sourced fact.
"""

import json

import pytest

from pemr import attestations, cli, db, dedup, persons, query

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

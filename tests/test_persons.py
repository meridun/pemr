"""Person CRUD + `pemr person` CLI surface."""

import pytest

from pemr import cli, db, persons


@pytest.fixture()
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    yield conn
    conn.close()


def test_add_and_get_person(conn):
    person = persons.add_person(
        conn, "jane-doe", "Jane Doe", dob="1980-01-01", blood_type="O+"
    )
    assert person.person_id == 1
    fetched = persons.get_person(conn, "jane-doe")
    assert fetched == person
    assert fetched.dob == "1980-01-01"


def test_slug_normalized_lowercase(conn):
    persons.add_person(conn, "  Jane-Doe ", "Jane Doe")
    assert persons.get_person(conn, "JANE-DOE").slug == "jane-doe"


def test_duplicate_slug_rejected(conn):
    persons.add_person(conn, "jane-doe", "Jane Doe")
    with pytest.raises(persons.SlugExistsError):
        persons.add_person(conn, "jane-doe", "Someone Else")


@pytest.mark.parametrize("slug,name", [("", "Jane"), ("jane", "  ")])
def test_empty_fields_rejected(conn, slug, name):
    with pytest.raises(ValueError):
        persons.add_person(conn, slug, name)


def test_list_people_sorted_by_slug(conn):
    persons.add_person(conn, "zoe", "Zoe")
    persons.add_person(conn, "amy", "Amy")
    assert [p.slug for p in persons.list_people(conn)] == ["amy", "zoe"]


def test_get_missing_person_returns_none(conn):
    assert persons.get_person(conn, "nobody") is None


# --- edit (partial field-level update) ---


def test_update_person_partial(conn):
    persons.add_person(conn, "jane-doe", "Jane Doe", dob="1980-01-01", blood_type="O+")
    updated = persons.update_person(conn, "jane-doe", dob="1981-02-03")
    assert updated.dob == "1981-02-03"
    assert updated.full_name == "Jane Doe"       # untouched
    assert updated.blood_type == "O+"            # untouched


def test_update_person_clears_nullable_with_empty_string(conn):
    persons.add_person(conn, "jane-doe", "Jane Doe", dob="1980-01-01", notes="typo")
    updated = persons.update_person(conn, "jane-doe", dob="", notes="")
    assert updated.dob is None
    assert updated.notes is None


def test_update_person_rejects_empty_name(conn):
    persons.add_person(conn, "jane-doe", "Jane Doe")
    with pytest.raises(ValueError):
        persons.update_person(conn, "jane-doe", full_name="  ")


def test_update_person_requires_a_field(conn):
    persons.add_person(conn, "jane-doe", "Jane Doe")
    with pytest.raises(ValueError):
        persons.update_person(conn, "jane-doe")


def test_update_person_rejects_unknown_field(conn):
    persons.add_person(conn, "jane-doe", "Jane Doe")
    with pytest.raises(ValueError):
        persons.update_person(conn, "jane-doe", nickname="Janey")


def test_update_unknown_slug_raises(conn):
    with pytest.raises(persons.PersonNotFoundError):
        persons.update_person(conn, "nobody", dob="2000-01-01")


# --- deactivate / reactivate (soft, reversible) ---


def test_deactivate_hides_from_default_list_and_all_shows(conn):
    persons.add_person(conn, "jane-doe", "Jane Doe")
    persons.add_person(conn, "john-doe", "John Doe")
    persons.deactivate_person(conn, "john-doe")
    assert [p.slug for p in persons.list_people(conn)] == ["jane-doe"]
    assert [p.slug for p in persons.list_people(conn, include_inactive=True)] == [
        "jane-doe", "john-doe",
    ]
    assert persons.get_person(conn, "john-doe").deactivated_at is not None


def test_deactivate_is_idempotent(conn):
    persons.add_person(conn, "jane-doe", "Jane Doe")
    first = persons.deactivate_person(conn, "jane-doe").deactivated_at
    second = persons.deactivate_person(conn, "jane-doe").deactivated_at
    assert first == second  # timestamp preserved


def test_reactivate_restores(conn):
    persons.add_person(conn, "jane-doe", "Jane Doe")
    persons.deactivate_person(conn, "jane-doe")
    restored = persons.reactivate_person(conn, "jane-doe")
    assert restored.deactivated_at is None
    assert [p.slug for p in persons.list_people(conn)] == ["jane-doe"]


def test_deactivate_unknown_slug_raises(conn):
    with pytest.raises(persons.PersonNotFoundError):
        persons.deactivate_person(conn, "nobody")


# --- remove (hard delete, childless-only) ---


def test_remove_childless_person(conn):
    persons.add_person(conn, "typo", "Typo Person")
    persons.remove_person(conn, "typo")
    assert persons.get_person(conn, "typo") is None


def test_remove_with_dependents_refused(conn):
    person = persons.add_person(conn, "jane-doe", "Jane Doe")
    conn.execute(
        "INSERT INTO document (sha256, person_id, source_path, ingested_at) "
        "VALUES (?, ?, ?, ?)",
        ("sha-1", person.person_id, "aa/x.pdf", "2026-01-01T00:00:00"),
    )
    conn.commit()
    with pytest.raises(persons.PersonHasDependentsError):
        persons.remove_person(conn, "jane-doe")
    assert persons.get_person(conn, "jane-doe") is not None  # not deleted


def test_remove_unknown_slug_raises(conn):
    with pytest.raises(persons.PersonNotFoundError):
        persons.remove_person(conn, "nobody")


# --- CLI surface (argparse wiring end-to-end against a temp DB) ---


def _run(tmp_path, *argv):
    return cli.main(["--db", str(tmp_path / "cli.db"), *argv])


def test_cli_migrate_then_person_roundtrip(tmp_path, capsys):
    assert _run(tmp_path, "migrate") == 0
    assert _run(
        tmp_path, "person", "add", "--slug", "jane-doe", "--name", "Jane Doe"
    ) == 0
    assert _run(tmp_path, "person", "list") == 0
    assert _run(tmp_path, "person", "show", "jane-doe") == 0
    out = capsys.readouterr().out
    assert "applied 001_init.sql" in out
    assert "jane-doe" in out


def test_cli_show_unknown_slug_fails(tmp_path, capsys):
    assert _run(tmp_path, "migrate") == 0
    assert _run(tmp_path, "person", "show", "nobody") == 1
    assert "no person" in capsys.readouterr().err


def test_cli_duplicate_add_fails_cleanly(tmp_path, capsys):
    _run(tmp_path, "migrate")
    _run(tmp_path, "person", "add", "--slug", "j", "--name", "J")
    assert _run(tmp_path, "person", "add", "--slug", "j", "--name", "J2") == 1
    assert "already exists" in capsys.readouterr().err


def test_cli_person_edit_updates_field(tmp_path, capsys):
    _run(tmp_path, "migrate")
    _run(tmp_path, "person", "add", "--slug", "jane", "--name", "Jane",
         "--dob", "1980-01-01")
    assert _run(tmp_path, "person", "edit", "jane", "--dob", "1981-02-03") == 0
    out = capsys.readouterr().out
    assert "1981-02-03" in out


def test_cli_person_edit_clears_nullable(tmp_path, capsys):
    _run(tmp_path, "migrate")
    _run(tmp_path, "person", "add", "--slug", "jane", "--name", "Jane",
         "--dob", "1980-01-01")
    assert _run(tmp_path, "person", "edit", "jane", "--dob", "") == 0
    assert _run(tmp_path, "person", "show", "jane") == 0
    out = capsys.readouterr().out
    assert "1980-01-01" not in out


def test_cli_person_edit_no_fields_fails(tmp_path, capsys):
    _run(tmp_path, "migrate")
    _run(tmp_path, "person", "add", "--slug", "jane", "--name", "Jane")
    assert _run(tmp_path, "person", "edit", "jane") == 1
    assert "nothing to update" in capsys.readouterr().err


def test_cli_person_edit_unknown_slug_fails(tmp_path, capsys):
    _run(tmp_path, "migrate")
    assert _run(tmp_path, "person", "edit", "nobody", "--name", "X") == 1
    assert "no person" in capsys.readouterr().err


def test_cli_person_deactivate_reactivate_and_list_all(tmp_path, capsys):
    _run(tmp_path, "migrate")
    _run(tmp_path, "person", "add", "--slug", "jane", "--name", "Jane")
    assert _run(tmp_path, "person", "deactivate", "jane") == 0
    capsys.readouterr()  # drain output from the setup commands above
    # default list hides the deactivated person
    _run(tmp_path, "person", "list")
    default_out = capsys.readouterr().out
    assert "jane" not in default_out
    # --all reveals them, marked inactive
    _run(tmp_path, "person", "list", "--all")
    all_out = capsys.readouterr().out
    assert "jane" in all_out and "inactive" in all_out
    # reactivate restores to the default list
    assert _run(tmp_path, "person", "reactivate", "jane") == 0
    capsys.readouterr()  # drain the reactivate confirmation
    _run(tmp_path, "person", "list")
    assert "jane" in capsys.readouterr().out


def test_cli_person_remove_childless(tmp_path, capsys):
    _run(tmp_path, "migrate")
    _run(tmp_path, "person", "add", "--slug", "typo", "--name", "Typo")
    assert _run(tmp_path, "person", "remove", "typo") == 0
    assert "removed person" in capsys.readouterr().out
    assert _run(tmp_path, "person", "show", "typo") == 1

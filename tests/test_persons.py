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

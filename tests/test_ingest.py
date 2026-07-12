"""Document ingest: hashing, content-addressed blob store, layer-1 dedup."""

import sqlite3

import pytest

from pemr import db, ingest, persons


@pytest.fixture()
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    persons.add_person(conn, "jane-doe", "Jane Doe")
    yield conn
    conn.close()


@pytest.fixture()
def sources(tmp_path):
    return tmp_path / "sources"


def _make_file(tmp_path, name="scan.txt", content=b"lab report bytes"):
    p = tmp_path / name
    p.write_bytes(content)
    return p


def test_hash_file_is_deterministic(tmp_path):
    a = _make_file(tmp_path, "a.txt", b"same bytes")
    b = _make_file(tmp_path, "b.txt", b"same bytes")
    assert ingest.hash_file(a) == ingest.hash_file(b)
    assert ingest.hash_file(a) != ingest.hash_file(
        _make_file(tmp_path, "c.txt", b"other")
    )


def test_ingest_new_document_stores_blob_and_row(conn, tmp_path, sources):
    src = _make_file(tmp_path)
    result = ingest.ingest_document(conn, src, "jane-doe", sources)

    assert result.status == "new"
    doc = result.document
    sha = ingest.hash_file(src)
    # content-addressed blob copied under sources/<sha[:2]>/<sha>.txt
    dest = sources / sha[:2] / f"{sha}.txt"
    assert dest.is_file()
    # stored source_path is portable (relative to the sources root)
    assert doc.source_path == f"{sha[:2]}/{sha}.txt"
    assert doc.sha256 == sha
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM document"
    ).fetchone()
    assert row["n"] == 1


def test_ingest_duplicate_content_stops(conn, tmp_path, sources):
    src = _make_file(tmp_path, "scan1.txt", b"identical")
    first = ingest.ingest_document(conn, src, "jane-doe", sources)
    # a re-scan: different filename, identical bytes
    again = _make_file(tmp_path, "scan2.txt", b"identical")
    second = ingest.ingest_document(conn, again, "jane-doe", sources)

    assert first.status == "new"
    assert second.status == "duplicate"
    assert second.document.document_id == first.document.document_id
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 1


def test_ingest_unknown_person_raises(conn, tmp_path, sources):
    with pytest.raises(ingest.IngestError, match="no person"):
        ingest.ingest_document(conn, _make_file(tmp_path), "nobody", sources)


def test_ingest_missing_file_raises(conn, tmp_path, sources):
    with pytest.raises(ingest.IngestError, match="file not found"):
        ingest.ingest_document(conn, tmp_path / "nope.pdf", "jane-doe", sources)


def test_ingest_on_unmigrated_db_raises(tmp_path, sources):
    fresh = db.connect(tmp_path / "empty.db")
    try:
        with pytest.raises(db.NotMigratedError):
            ingest.ingest_document(fresh, _make_file(tmp_path), "jane-doe", sources)
    finally:
        fresh.close()


def test_failed_insert_does_not_orphan_blob(conn, tmp_path, sources):
    # carry-forward advisory from #4: if the `document` INSERT fails, the blob this
    # call copied into sources/ must be cleaned up, not left orphaned. A BEFORE INSERT
    # trigger forces the insert to abort *after* the blob has been staged.
    src = _make_file(tmp_path, "scan.txt", b"orphan check bytes")
    sha = ingest.hash_file(src)
    conn.execute(
        "CREATE TRIGGER boom BEFORE INSERT ON document "
        "BEGIN SELECT RAISE(ABORT, 'insert exploded'); END"
    )
    conn.commit()

    with pytest.raises(sqlite3.Error, match="insert exploded"):
        ingest.ingest_document(conn, src, "jane-doe", sources)
    assert not (sources / sha[:2] / f"{sha}.txt").exists()  # blob cleaned up


def test_ocr_degrades_when_tesseract_absent(conn, tmp_path, sources, monkeypatch, capsys):
    monkeypatch.setattr(ingest.shutil, "which", lambda _: None)
    result = ingest.ingest_document(
        conn, _make_file(tmp_path), "jane-doe", sources, ocr=True
    )
    assert result.status == "new"
    assert result.document.ocr_text is None
    assert "tesseract" in capsys.readouterr().err

"""Regression for issue #46: `pemr find` echoes *dynamic* document content, which
can legitimately contain non-ASCII codepoints (em-dashes, smart quotes, accents)
from the source medical document. Unlike the static CLI-facing literals governed
by issue #23 (``test_cli_ascii.py``), this data must pass through **verbatim** —
never ASCII-normalized or replaced. ``main`` reconfigures stdout/stderr to UTF-8
so a legacy Windows console (cp1252/cp437) prints it correctly instead of ``?``.

The cp437 console path itself is Windows-runtime and can't be exercised
cross-platform; these tests guard the two things that *are* portable: that the
code does not strip/normalize the data, and that ``main`` reconfigures a
reconfigurable stream.
"""

import io

from pemr import cli, db, persons


# A snippet drawn straight from stored OCR text: an em-dash (U+2014) and an
# accented character, both of which cp437/cp1252 would garble.
_NON_ASCII_OCR = "diagnosis — severe migraña with aura"


def _seed(tmp_path):
    conn = db.connect(tmp_path / "find.db")
    db.migrate(conn)
    persons.add_person(conn, "jane-doe", "Jane Doe")
    pid = conn.execute(
        "SELECT person_id FROM person WHERE slug=?", ("jane-doe",)
    ).fetchone()["person_id"]
    conn.execute(
        "INSERT INTO document (sha256, person_id, doc_date, source_path, ocr_text, "
        "ingested_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("sha-jane-1", pid, "2026-01-01", "aa/x.pdf", _NON_ASCII_OCR,
         "2026-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()
    return tmp_path


def test_find_snippet_preserves_non_ascii_data(tmp_path, capsys):
    """The stored non-ASCII content survives the human-readable ``find`` path
    unmodified — no ``?`` substitution, no ASCII-normalization."""
    _seed(tmp_path)
    capsys.readouterr()
    rc = cli.main(["--db", str(tmp_path / "find.db"), "find", "diagnosis",
                   "--person", "jane-doe"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "—" in out, f"em-dash was dropped/replaced: {out!r}"
    assert "migraña" in out, f"accented data was normalized: {out!r}"
    assert "?" not in out


def test_main_reconfigures_streams_to_utf8(tmp_path, monkeypatch):
    """On a reconfigurable stream, ``main`` forces UTF-8 with ``errors='replace'``
    so a legacy console codepage can't garble or crash on stored data."""
    _seed(tmp_path)
    calls: list[dict] = []

    class _Reconfigurable(io.StringIO):
        def reconfigure(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr("sys.stdout", _Reconfigurable())
    monkeypatch.setattr("sys.stderr", _Reconfigurable())
    rc = cli.main(["--db", str(tmp_path / "find.db"), "find", "diagnosis",
                   "--person", "jane-doe"])
    assert rc == 0
    assert calls == [
        {"encoding": "utf-8", "errors": "replace"},
        {"encoding": "utf-8", "errors": "replace"},
    ]


def test_main_tolerates_streams_without_reconfigure(tmp_path, monkeypatch):
    """A stream lacking ``reconfigure`` (already-wrapped, or a capture buffer) is
    left untouched — the guard degrades to a no-op instead of raising."""
    _seed(tmp_path)

    class _Plain(io.StringIO):
        reconfigure = None  # attribute present but not callable -> skipped

    monkeypatch.setattr("sys.stdout", _Plain())
    monkeypatch.setattr("sys.stderr", _Plain())
    rc = cli.main(["--db", str(tmp_path / "find.db"), "find", "diagnosis",
                   "--person", "jane-doe"])
    assert rc == 0

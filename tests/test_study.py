"""Study-directory packing (issue #69): canonical zip, DICM filter, header reader.

Fixtures are synthesized here rather than checked in — a minimal but *real* DICOM
Part 10 byte stream is a few lines of `struct`, and binary blobs in the repo are
neither reviewable nor greppable.
"""

import json
import struct
import tracemalloc
import zipfile

import pytest

from pemr import cli, db, ingest, persons, study


# --------------------------------------------------------------------------- #
# Fixture synthesis: minimal DICOM Part 10 files
# --------------------------------------------------------------------------- #

IMPLICIT_VR_LE = "1.2.840.10008.1.2"
EXPLICIT_VR_LE = "1.2.840.10008.1.2.1"
EXPLICIT_VR_BE = "1.2.840.10008.1.2.2"  # unsupported on purpose


def _pad(value: str, pad: str = " ") -> bytes:
    """DICOM values are even-length; text VRs pad with a space, UI with NUL."""
    raw = value.encode("ascii")
    return raw + pad.encode("ascii") if len(raw) % 2 else raw


def _explicit(group, element, vr, value: bytes) -> bytes:
    return struct.pack("<HH2sH", group, element, vr, len(value)) + value


def _implicit(group, element, value: bytes) -> bytes:
    return struct.pack("<HHI", group, element, len(value)) + value


def dicom_bytes(
    *,
    transfer_syntax: str = EXPLICIT_VR_LE,
    study_date: str | None = "20190412",
    modality: str | None = "CT",
    study_description: str | None = "CT ABDOMEN PELVIS",
    series_description: str | None = "AXIAL 2.0",
    patient_name: str | None = None,
    patient_birth_date: str | None = None,
    study_uid: str | None = "1.2.840.113619.2.55.3.1",
    meta_length: int | None = None,
) -> bytes:
    """A minimal valid Part 10 file carrying (only) the tags this engine reads.

    ``meta_length`` overrides the ``(0002,0000)`` group-length element with a value
    that does not describe the file — the corruption the reader must survive without
    allocating on it (issue #69 audit B1).
    """
    meta = _explicit(0x0002, 0x0010, b"UI", _pad(transfer_syntax, "\x00"))
    explicit = transfer_syntax != IMPLICIT_VR_LE

    def element(group, elem, vr, value, pad=" "):
        if value is None:
            return b""
        raw = _pad(value, pad)
        return _explicit(group, elem, vr, raw) if explicit else _implicit(group, elem, raw)

    dataset = b"".join([
        element(0x0008, 0x0020, b"DA", study_date),
        element(0x0008, 0x0060, b"CS", modality),
        element(0x0008, 0x1030, b"LO", study_description),
        element(0x0008, 0x103E, b"LO", series_description),
        element(0x0010, 0x0010, b"PN", patient_name),
        element(0x0010, 0x0030, b"DA", patient_birth_date),
        element(0x0020, 0x000D, b"UI", study_uid, "\x00"),
        # A blob of pixel data, so the "stop before the payload" path is exercised.
        (_explicit(0x7FE0, 0x0010, b"OW", b"\x00" * 16) if explicit
         else _implicit(0x7FE0, 0x0010, b"\x00" * 16)),
    ])
    declared = len(meta) if meta_length is None else meta_length
    return (
        b"\x00" * 128
        + b"DICM"
        + _explicit(0x0002, 0x0000, b"UL", struct.pack("<I", declared))
        + meta
        + dataset
    )


def make_study(root, *, series=("SER1",), slices=2, viewer=True, report=True, **tags):
    """A burned-disc-shaped tree: DICOMDIR + slice dirs + a Windows viewer payload."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "DICOMDIR").write_bytes(dicom_bytes(series_description=None, **tags))
    for name in series:
        directory = root / name
        directory.mkdir(exist_ok=True)
        for slice_no in range(slices):
            (directory / f"IM{slice_no:06d}").write_bytes(
                dicom_bytes(series_description=f"{name} SERIES", **tags)
            )
    if viewer:
        (root / "VIEWER.EXE").write_bytes(b"MZ" + b"\x90" * 64)
        (root / "viewer.dll").write_bytes(b"MZ" + b"\x00" * 64)
        (root / "autorun.inf").write_bytes(b"[autorun]\r\n")
        (root / "index.html").write_text("<html>viewer</html>")
    if report:
        (root / "report.pdf").write_bytes(b"%PDF-1.4 radiology report")
    return root


# --------------------------------------------------------------------------- #
# Scanning / inclusion filter
# --------------------------------------------------------------------------- #

def test_scan_includes_dicom_by_magic_and_drops_viewer_payload(tmp_path):
    scan = study.scan_study_dir(make_study(tmp_path / "disc"))

    assert scan.rel_paths == ("DICOMDIR", "SER1/IM000000", "SER1/IM000001")
    # index.html is dropped as a viewer file, but it *is* document-like, so it is
    # named rather than counted; the executables are only counted.
    assert scan.excluded_documents == ("index.html", "report.pdf")
    assert scan.excluded_other == 3  # VIEWER.EXE, viewer.dll, autorun.inf
    assert scan.total_bytes == sum(
        (tmp_path / "disc" / rel).stat().st_size for rel in scan.rel_paths
    )


def test_is_dicom_file_tolerates_short_and_missing_files(tmp_path):
    short = tmp_path / "short.bin"
    short.write_bytes(b"\x00" * 40)
    assert study.is_dicom_file(short) is False
    assert study.is_dicom_file(tmp_path / "nope.bin") is False
    assert study.is_dicom_file(tmp_path) is False


@pytest.mark.parametrize("size,expected", [
    (0, "0 B"), (999, "999 B"), (1536, "1.5 KiB"), (5 * 1024**2, "5.0 MiB"),
    (4 * 1024**3, "4.0 GiB"), (3 * 1024**4, "3.0 TiB"),
])
def test_human_bytes(size, expected):
    assert study.human_bytes(size) == expected


def test_scan_empty_dir_yields_no_files(tmp_path):
    (tmp_path / "empty").mkdir()
    assert study.scan_study_dir(tmp_path / "empty").rel_paths == ()


# --------------------------------------------------------------------------- #
# Canonical archive — the determinism the content hash rests on
# --------------------------------------------------------------------------- #

def test_pack_is_deterministic_across_trees_and_creation_order(tmp_path):
    first = make_study(tmp_path / "a", series=("S1", "S2"))
    # Same content, created in the opposite order, at a different path, later.
    second = tmp_path / "b"
    second.mkdir()
    (second / "S2").mkdir()
    (second / "S1").mkdir()
    for rel in reversed(study.scan_study_dir(first).rel_paths):
        (second / rel).write_bytes((first / rel).read_bytes())
    make_study(second, series=(), viewer=True, report=True)

    sha_a = study.pack_study(study.scan_study_dir(first), tmp_path / "a.zip")
    sha_b = study.pack_study(study.scan_study_dir(second), tmp_path / "b.zip")
    assert sha_a == sha_b
    # ...and stable across repeated packs of the same tree.
    assert sha_a == study.pack_study(study.scan_study_dir(first), tmp_path / "a2.zip")


def test_pack_golden_hash(tmp_path):
    """A pinned hash over a fixed fixture: catches cross-version `zipfile` drift.

    If this fails after a Python upgrade, every previously ingested study would
    re-ingest as a *new* document instead of deduping — that is exactly the
    regression worth failing loudly on.
    """
    root = tmp_path / "golden"
    root.mkdir()
    (root / "IM000001").write_bytes(b"\x00" * 128 + b"DICM" + b"payload-a")
    (root / "IM000002").write_bytes(b"\x00" * 128 + b"DICM" + b"payload-b")
    sha = study.pack_study(study.scan_study_dir(root), tmp_path / "golden.zip")
    assert sha == "07368b18d1294cc67eafa48e25c6a7798281db45ac0dd77acea8a336c4d6b2b9"


def test_pack_pins_every_varying_zip_field(tmp_path):
    scan = study.scan_study_dir(make_study(tmp_path / "disc"))
    out = tmp_path / "study.zip"
    study.pack_study(scan, out)

    with zipfile.ZipFile(out) as zf:
        assert [i.filename for i in zf.infolist()] == list(scan.rel_paths)
        for info in zf.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.compress_type == zipfile.ZIP_STORED
            assert info.create_system == 0
            # never the source file's mode — see `_ZIP_EXTERNAL_ATTR`
            assert info.external_attr == 0o600 << 16
            assert info.internal_attr == 0
            assert info.comment == b""
            assert info.extra == b""
        # ...and the archive still round-trips the original bytes.
        assert zf.read("SER1/IM000000") == (
            scan.abs_path("SER1/IM000000").read_bytes()
        )


# --------------------------------------------------------------------------- #
# Header reader
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("syntax", [EXPLICIT_VR_LE, IMPLICIT_VR_LE])
def test_read_metadata_both_transfer_syntaxes(tmp_path, syntax):
    root = tmp_path / "disc"
    root.mkdir()
    (root / "IM000001").write_bytes(dicom_bytes(transfer_syntax=syntax))
    meta = study.read_metadata(study.scan_study_dir(root))

    assert meta.modality == "CT"
    assert meta.study_date == "2019-04-12"
    assert meta.study_description == "CT ABDOMEN PELVIS"
    assert meta.study_uid == "1.2.840.113619.2.55.3.1"
    assert [s.description for s in meta.series] == ["AXIAL 2.0"]


@pytest.mark.parametrize(
    "content",
    [
        b"\x00" * 128 + b"DICM",                       # truncated after the magic
        b"\x00" * 128 + b"DICM" + b"\x02\x00\x00\x00", # truncated meta element
        dicom_bytes(transfer_syntax=EXPLICIT_VR_BE),   # unsupported syntax
        dicom_bytes(study_date="not-a-date", modality=None),
    ],
    ids=["truncated", "truncated-meta", "big-endian", "junk-date"],
)
def test_read_metadata_never_raises_on_bad_input(tmp_path, content):
    root = tmp_path / "disc"
    root.mkdir()
    (root / "IM000001").write_bytes(content)
    meta = study.read_metadata(study.scan_study_dir(root))
    assert meta.study_date is None  # junk/unsupported => no metadata, no exception


def test_read_metadata_ignores_a_bogus_meta_group_length(tmp_path):
    """Issue #69 audit B1: the file's declared meta length must not size a read.

    `BufferedReader.read(n)` allocates `n` up front, so an unbounded 32-bit length —
    four corrupt bytes, which is exactly how optical media fails — would commit up to
    4 GiB per slice, and the `MemoryError` that follows on a machine that cannot
    satisfy it is *not* an `OSError`, so it would escape the reader's "never fails
    the ingest" contract.
    """
    root = tmp_path / "disc"
    root.mkdir()
    (root / "IM000001").write_bytes(dicom_bytes(meta_length=0xFFFFFFFF))

    tracemalloc.start()
    try:
        meta = study.read_metadata(study.scan_study_dir(root))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert meta == study.StudyMetadata(series=(study.Series("", None, 1),))
    # Without the bound this is ~4 GiB; the bound makes it a few KiB.
    assert peak < 8 * 1024**2


def test_read_metadata_caps_an_overlong_tag_value(tmp_path):
    """Issue #69 audit B2: an untrusted element length must not reach `ocr_text`.

    `StudyDescription` is VR `LO` (64 chars); a file declaring 60,000 would otherwise
    be spliced verbatim into the FTS index and into an agent's context as if it were
    clinical record text.
    """
    root = tmp_path / "disc"
    root.mkdir()
    (root / "IM000001").write_bytes(dicom_bytes(study_description="X" * 60_000))
    scan = study.scan_study_dir(root)
    meta = study.read_metadata(scan)

    assert meta.study_description is not None
    assert len(meta.study_description) == 128
    assert len(study.summary_text(scan, meta)) < 512
    # Truncation is of the stored *value* only — the element stream still advances by
    # the declared length, so later tags are read as usual.
    assert [s.description for s in meta.series] == ["AXIAL 2.0"]


def test_identity_text_is_shaped_for_the_owner_check(tmp_path):
    root = tmp_path / "disc"
    root.mkdir()
    (root / "IM000001").write_bytes(
        dicom_bytes(patient_name="DOE^JANE^", patient_birth_date="19780504")
    )
    meta = study.read_metadata(study.scan_study_dir(root))

    assert meta.patient_name == "DOE^JANE^"
    assert meta.patient_birth_date == "1978-05-04"  # ISO, as `dob_candidates` renders
    text = study.identity_text(meta)
    # The labels are what `check_owner`'s identity anchor recognises, and
    # `normalize_text` reduces the DICOM caret form to plain name tokens.
    assert text == "Patient: DOE^JANE^\nDOB: 1978-05-04"
    assert " doe jane " in ingest.normalize_text(text)


def test_identity_text_is_none_without_the_tags(tmp_path):
    """No signal must stay `unverified`, not become a spurious refusal."""
    root = tmp_path / "disc"
    root.mkdir()
    (root / "IM000001").write_bytes(dicom_bytes())
    assert study.identity_text(study.read_metadata(study.scan_study_dir(root))) is None


def test_metadata_ignores_dicomdir_as_the_tag_source(tmp_path):
    """DICOMDIR is packed but carries directory records, not study tags."""
    root = tmp_path / "disc"
    root.mkdir()
    (root / "DICOMDIR").write_bytes(dicom_bytes(modality=None, study_date=None))
    (root / "IM000001").write_bytes(dicom_bytes())
    meta = study.read_metadata(study.scan_study_dir(root))
    assert meta.modality == "CT"


def test_summary_text_is_a_digest_not_a_file_listing(tmp_path):
    scan = study.scan_study_dir(make_study(tmp_path / "disc", series=("S1", "S2")))
    text = study.summary_text(scan, study.read_metadata(scan))

    assert "DICOM study: disc" in text
    assert "modality: CT" in text
    assert "study date: 2019-04-12" in text
    assert "slices: 4 in 2 series" in text
    assert "S1 SERIES - 2 slices" in text
    assert "IM000000" not in text  # never the per-slice listing


# --------------------------------------------------------------------------- #
# Ingest integration
# --------------------------------------------------------------------------- #

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


def test_ingest_study_dir_stores_one_document(conn, tmp_path, sources):
    root = make_study(tmp_path / "disc")
    result = ingest.ingest_study_dir(conn, root, "jane-doe", sources)

    assert result.status == "new"
    doc = result.document
    sha = doc.sha256
    assert doc.source_path == f"{sha[:2]}/{sha}.dcm.zip"
    assert (sources / sha[:2] / f"{sha}.dcm.zip").is_file()
    # derived defaults
    assert doc.doc_date == "2019-04-12"
    assert doc.category == "imaging"
    assert result.ocr_text_populated
    assert "DICOM study: disc" in doc.ocr_text
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 1
    # the stored blob's bytes really are the content hash (what `pemr verify` checks)
    assert ingest.hash_file(sources / doc.source_path) == sha


def test_ingest_study_dir_reingest_is_a_duplicate_no_op(conn, tmp_path, sources):
    root = make_study(tmp_path / "disc")
    first = ingest.ingest_study_dir(conn, root, "jane-doe", sources)
    second = ingest.ingest_study_dir(conn, root, "jane-doe", sources)

    assert second.status == "duplicate"
    assert second.document.document_id == first.document.document_id
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 1
    assert len(list((sources / first.document.sha256[:2]).iterdir())) == 1
    # the staging copy is unlinked on every path, duplicate included
    assert list((sources / ingest.TMP_DIRNAME).iterdir()) == []


def test_ingest_study_dir_caller_values_win(conn, tmp_path, sources):
    root = make_study(tmp_path / "disc")
    result = ingest.ingest_study_dir(
        conn, root, "jane-doe", sources,
        doc_date="2020-02-02", category="radiology", provider="Mercy Imaging",
        ocr_text="  IMPRESSION: no acute findings.  ",
    )
    doc = result.document
    assert doc.doc_date == "2020-02-02"
    assert doc.category == "radiology"
    assert doc.provider == "Mercy Imaging"
    assert doc.ocr_text == "IMPRESSION: no acute findings."


def test_ingest_study_dir_reports_excluded_documents(conn, tmp_path, sources, capsys):
    ingest.ingest_study_dir(conn, make_study(tmp_path / "disc"), "jane-doe", sources)
    err = capsys.readouterr().err
    assert "3 DICOM files" in err
    assert "report.pdf" in err


def test_ingest_study_dir_size_guard(conn, tmp_path, sources, monkeypatch):
    root = make_study(tmp_path / "disc")
    monkeypatch.setattr(study, "MAX_STUDY_BYTES", 10)

    with pytest.raises(ingest.IngestError, match="over the 10 B limit.*--allow-large"):
        ingest.ingest_study_dir(conn, root, "jane-doe", sources)
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 0
    assert not sources.exists()  # refused pre-write: nothing staged, nothing stored

    result = ingest.ingest_study_dir(conn, root, "jane-doe", sources, allow_large=True)
    assert result.status == "new"


def test_ingest_study_dir_rejects_a_file(conn, tmp_path, sources):
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.4")
    with pytest.raises(ingest.IngestError, match="is a file"):
        ingest.ingest_study_dir(conn, path, "jane-doe", sources)


def test_ingest_study_dir_unknown_kind(conn, tmp_path, sources):
    with pytest.raises(ingest.IngestError, match="unknown study kind"):
        ingest.ingest_study_dir(
            conn, make_study(tmp_path / "disc"), "jane-doe", sources, study="mri-raw"
        )


def test_ingest_study_dir_missing_dir(conn, tmp_path, sources):
    with pytest.raises(ingest.IngestError, match="directory not found"):
        ingest.ingest_study_dir(conn, tmp_path / "nope", "jane-doe", sources)


def test_ingest_study_dir_with_no_dicom_files(conn, tmp_path, sources):
    root = tmp_path / "disc"
    root.mkdir()
    (root / "readme.txt").write_text("nothing here")
    with pytest.raises(ingest.IngestError, match="no DICOM files"):
        ingest.ingest_study_dir(conn, root, "jane-doe", sources)
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 0


def test_ingest_document_points_a_directory_at_the_study_flag(conn, tmp_path, sources):
    root = make_study(tmp_path / "disc")
    with pytest.raises(ingest.IngestError, match="--study dicom"):
        ingest.ingest_document(conn, root, "jane-doe", sources)
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 0


def test_ingest_study_dir_owner_check_runs_on_supplied_text(conn, tmp_path, sources):
    persons.add_person(conn, "john-roe", "John Roe")
    root = make_study(tmp_path / "disc")

    with pytest.raises(ingest.OwnerMismatchError) as excinfo:
        ingest.ingest_study_dir(
            conn, root, "jane-doe", sources,
            ocr_text="Patient: John Roe\nIMPRESSION: normal study.",
        )
    assert excinfo.value.check.verdict == "mismatch"
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 0
    assert not sources.exists()  # pre-write *and* pre-pack

    # The derived summary is engine output, not document text: it is never checked.
    result = ingest.ingest_study_dir(conn, root, "jane-doe", sources)
    assert result.owner_check.verdict == "unverified"


def test_ingest_study_dir_owner_check_reads_the_dicom_header(conn, tmp_path, sources):
    """Issue #69 audit B3: the study path must not opt out of the #61 misfile rail.

    A 2,000-slice binary folder is the one document type a human cannot eyeball, so
    pointing `--person jane-doe` at the spouse's disc is the realistic slip — and the
    identity is sitting in the header.
    """
    persons.add_person(conn, "john-roe", "John Roe")
    root = make_study(tmp_path / "disc", patient_name="ROE^JOHN^")

    with pytest.raises(ingest.OwnerMismatchError) as excinfo:
        ingest.ingest_study_dir(conn, root, "jane-doe", sources)
    assert excinfo.value.check.verdict == "mismatch"
    assert excinfo.value.check.matched_slug == "john-roe"
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 0
    assert not sources.exists()  # refused pre-write *and* pre-pack

    # ...and `--force` is still the whole recovery.
    result = ingest.ingest_study_dir(conn, root, "jane-doe", sources, force=True)
    assert result.status == "new"
    assert result.owner_check.verdict == "mismatch"


def test_ingest_study_dir_header_identity_verifies_and_is_never_stored(
    conn, tmp_path, sources
):
    root = make_study(tmp_path / "disc", patient_name="DOE^JANE^")
    result = ingest.ingest_study_dir(conn, root, "jane-doe", sources)

    assert result.owner_check.verdict == "match"
    assert result.owner_check.matched_slug == "jane-doe"
    # Verification input, not stored text: the identity tags never reach `ocr_text`,
    # so they never reach the FTS index or an agent's context.
    assert "JANE" not in result.document.ocr_text
    assert "DOE" not in result.document.ocr_text


def test_ingest_study_dir_header_dob_alone_verifies(conn, tmp_path, sources):
    persons.add_person(conn, "ann-poe", "Ann Poe", dob="1978-05-04")
    root = make_study(tmp_path / "disc", patient_birth_date="19780504")

    result = ingest.ingest_study_dir(conn, root, "ann-poe", sources)
    assert result.owner_check.verdict == "match"


def test_ingest_study_dir_unknown_patient_is_suspect(conn, tmp_path, sources):
    """Nobody on the roster: the header names *someone*, and it isn't the claimant."""
    root = make_study(tmp_path / "disc", patient_name="STRANGER^SAM^")

    with pytest.raises(ingest.OwnerMismatchError) as excinfo:
        ingest.ingest_study_dir(conn, root, "jane-doe", sources)
    assert excinfo.value.check.verdict == "suspect"


@pytest.mark.parametrize(
    "verdicts, expected",
    [
        (("match", "mismatch"), "mismatch"),   # blocking beats affirmative
        (("suspect", "match"), "suspect"),
        (("unverified", "match"), "match"),    # affirmative beats ignorance
        (("unverified", "unverified"), "unverified"),
        ((), "unverified"),
    ],
)
def test_strongest_check_orders_by_consequence(verdicts, expected):
    checks = [ingest.OwnerCheck(verdict=v) for v in verdicts]
    assert ingest._strongest_check(checks).verdict == expected


# --------------------------------------------------------------------------- #
# CLI + MCP wiring
# --------------------------------------------------------------------------- #

def _cli(tmp_path, *argv):
    return cli.main(["--db", str(tmp_path / "cli.db"), *argv])


@pytest.fixture()
def cli_ready(tmp_path):
    assert _cli(tmp_path, "migrate", "--create") == 0
    assert _cli(tmp_path, "person", "add", "--slug", "jane-doe",
                "--name", "Jane Doe") == 0
    return tmp_path


def test_cli_ingest_study_roundtrip(cli_ready, capsys):
    tmp_path = cli_ready
    root = make_study(tmp_path / "disc")
    sources = tmp_path / "sources"

    assert _cli(tmp_path, "ingest", str(root), "--person", "jane-doe",
                "--study", "dicom", "--sources", str(sources)) == 0
    captured = capsys.readouterr()
    assert "ingested document #1" in captured.out
    assert ".dcm.zip" in captured.out
    # a study is searchable off the derived summary, so no "no ocr_text" warning
    assert "no ocr_text stored" not in captured.err

    assert _cli(tmp_path, "document", "show", "1", "--text") == 0
    assert "DICOM study: disc" in capsys.readouterr().out

    # re-run is a no-op duplicate, per layer-1 dedup on the archive's hash
    assert _cli(tmp_path, "ingest", str(root), "--person", "jane-doe",
                "--study", "dicom", "--sources", str(sources)) == 0
    assert "duplicate: already filed as document #1" in capsys.readouterr().out


def test_cli_ingest_study_says_it_ignores_ocr(cli_ready, capsys):
    tmp_path = cli_ready
    assert _cli(tmp_path, "ingest", str(make_study(tmp_path / "disc")),
                "--person", "jane-doe", "--study", "dicom", "--ocr", "tesseract",
                "--sources", str(tmp_path / "sources")) == 0
    assert "--ocr is ignored for --study dicom" in capsys.readouterr().err


def test_cli_ingest_directory_without_study_flag_errors(cli_ready, capsys):
    tmp_path = cli_ready
    root = make_study(tmp_path / "disc")
    assert _cli(tmp_path, "ingest", str(root), "--person", "jane-doe",
                "--sources", str(tmp_path / "sources")) == 1
    assert "--study dicom" in capsys.readouterr().err


def test_cli_ingest_study_flag_with_a_file_errors(cli_ready, capsys):
    tmp_path = cli_ready
    scan = tmp_path / "scan.txt"
    scan.write_bytes(b"a flat scan")
    assert _cli(tmp_path, "ingest", str(scan), "--person", "jane-doe",
                "--study", "dicom", "--sources", str(tmp_path / "sources")) == 1
    assert "is a file" in capsys.readouterr().err


def test_cli_study_verify_resolves_the_packed_blob(cli_ready, capsys):
    """`pemr verify` re-hashes `sources/<source_path>` — the archive must satisfy it."""
    tmp_path = cli_ready
    sources = tmp_path / "sources"
    assert _cli(tmp_path, "ingest", str(make_study(tmp_path / "disc")),
                "--person", "jane-doe", "--study", "dicom",
                "--sources", str(sources)) == 0
    capsys.readouterr()
    assert _cli(tmp_path, "verify", "--sources", str(sources), "--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["blobs"] == {
        "checked": 1, "ok": 1, "missing": 0, "mismatched": 0, "skipped": None
    }

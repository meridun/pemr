"""`pemr` command-line entry point (phase 1: migrate, person add|list|show).

DB path resolution order: --db flag > PEMR_DB env var > config.toml
[paths].data_dir + /pemr.db. Config path resolution: --config flag >
PEMR_CONFIG env var > ./config.toml.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tomllib
from dataclasses import asdict
from pathlib import Path

from . import (
    __version__, backup, db, dedup, documents, ingest, persons, query, render,
    restore, study, verify,
)

# Shipped starter analyte/name dictionary (framework, not user data — see .gitignore).
_DEFAULT_DICTIONARY = Path(__file__).resolve().parent.parent / "data" / "dictionary.example.toml"


def _load_config(args: argparse.Namespace) -> dict:
    config_path = Path(
        args.config or os.environ.get("PEMR_CONFIG") or "config.toml"
    )
    if config_path.is_file():
        with config_path.open("rb") as fh:
            return tomllib.load(fh)
    return {}


def _resolve_db_path(args: argparse.Namespace) -> Path:
    if args.db:
        return Path(args.db)
    env_db = os.environ.get("PEMR_DB")
    if env_db:
        return Path(env_db)
    config_path = Path(
        args.config or os.environ.get("PEMR_CONFIG") or "config.toml"
    )
    if config_path.is_file():
        with config_path.open("rb") as fh:
            config = tomllib.load(fh)
        data_dir = config.get("paths", {}).get("data_dir")
        if data_dir:
            return Path(data_dir) / "pemr.db"
    raise SystemExit(
        "error: no database path - pass --db, set PEMR_DB, or set "
        f"[paths].data_dir in {config_path} (see config.example.toml)"
    )


def _no_database_message(db_path: Path) -> str:
    """The refusal shown when a command needs a database and there isn't one.

    Issue #55: the old advice here was `pemr migrate`, which happily created a brand-new
    empty database and reported success - manufacturing a convincing empty archive for
    the exact user whose data just vanished (and then feeding it to the next `pemr
    backup`, whose rotation could prune the real snapshots). ASCII only (issue #23).
    """
    return (
        f"error: no database at {db_path}\n"
        "nothing was created - pemr will not conjure an empty archive over a missing "
        "database.\n"
        "  - restoring after data loss?  pemr restore latest\n"
        "  - starting a new archive?     pemr migrate --create"
    )


def _connect_db(args: argparse.Namespace):
    """Resolve the DB path, refuse if there is no database there, then connect.

    Every CLI command that reads or writes the archive goes through here; the MCP
    wrapper applies the same gate (:func:`pemr.mcp_server._connect`). `pemr migrate
    --create` is the one documented way past it.
    """
    db_path = _resolve_db_path(args)
    if not db.database_exists(db_path):
        raise SystemExit(_no_database_message(db_path))
    return db.connect(db_path)


def _resolve_sources_dir(args: argparse.Namespace) -> Path:
    sources_dir = _resolve_sources_dir_optional(args)
    if sources_dir is None:
        raise SystemExit(
            "error: no sources dir - pass --sources, set PEMR_SOURCES, or set "
            "[paths].sources_dir in config.toml (see config.example.toml)"
        )
    return sources_dir


def _resolve_sources_dir_optional(args: argparse.Namespace) -> Path | None:
    """As :func:`_resolve_sources_dir`, but None instead of exiting.

    `restore`/`verify` must still report on the database when `sources/` is
    unconfigured - the blob pass is skipped with a note, not fatal.
    """
    override = getattr(args, "sources", None)
    if override:
        return Path(override)
    env = os.environ.get("PEMR_SOURCES")
    if env:
        return Path(env)
    sources_dir = _load_config(args).get("paths", {}).get("sources_dir")
    if sources_dir:
        return Path(sources_dir)
    return None


def _resolve_backup_dir_optional(args: argparse.Namespace) -> Path | None:
    """Backup dir if configured, else None (`restore <path>` does not need one)."""
    override = getattr(args, "backup_dir", None)
    if override:
        return Path(override)
    env = os.environ.get("PEMR_BACKUP_DIR")
    if env:
        return Path(env)
    backup_dir = _load_config(args).get("paths", {}).get("backup_dir")
    if backup_dir:
        return Path(backup_dir)
    return None


def _resolve_backup_dir(args: argparse.Namespace) -> Path:
    backup_dir = _resolve_backup_dir_optional(args)
    if backup_dir is None:
        raise SystemExit(
            "error: no backup dir — pass --backup-dir, set PEMR_BACKUP_DIR, or set "
            "[paths].backup_dir in config.toml (see config.example.toml)"
        )
    return backup_dir


def _resolve_retention(args: argparse.Namespace) -> tuple[int, int]:
    """Retention counts, layered flag > [backup] config > hardcoded default."""
    cfg = _load_config(args).get("backup", {})
    keep_daily = (
        args.keep_daily
        if args.keep_daily is not None
        else cfg.get("keep_daily", backup.DEFAULT_KEEP_DAILY)
    )
    keep_weekly = (
        args.keep_weekly
        if args.keep_weekly is not None
        else cfg.get("keep_weekly", backup.DEFAULT_KEEP_WEEKLY)
    )
    keep_daily, keep_weekly = int(keep_daily), int(keep_weekly)
    if keep_daily < 0 or keep_weekly < 0:
        raise SystemExit(
            "error: retention counts cannot be negative "
            f"(keep_daily={keep_daily}, keep_weekly={keep_weekly}); "
            "use --no-rotate to keep every snapshot"
        )
    return keep_daily, keep_weekly


def _resolve_dictionary_path(args: argparse.Namespace) -> Path | None:
    override = getattr(args, "dictionary", None)
    if override:
        return Path(override)
    env = os.environ.get("PEMR_DICTIONARY")
    if env:
        return Path(env)
    configured = _load_config(args).get("paths", {}).get("dictionary_file")
    if configured and Path(configured).is_file():
        return Path(configured)
    return _DEFAULT_DICTIONARY if _DEFAULT_DICTIONARY.is_file() else None


class _OcrTextFileError(Exception):
    """`--ocr-text-file` could not be read; the message is already user-facing."""


def _read_ocr_text_file(path: str) -> str:
    """Read an `--ocr-text-file` for the two commands that accept one.

    One helper for `ingest` and `document set-text` so their read paths cannot
    diverge again (issue #78); #62 deliberately mirrored `ingest`'s read
    byte-for-byte, which duplicated both of its defects:

    * `utf-8-sig`, not `utf-8`: a UTF-8 BOM survives `str.strip()`, so a BOM-only
      "empty" file (Notepad, PowerShell 5.1 - three bytes, `EF BB BF`) sails past
      the emptiness guard and stores one invisible character as `ocr_text`, which
      then reports `ocr_text_populated: true` while carrying no text. A BOM on a
      real transcription silently inflates the stored text by one character.
    * `UnicodeDecodeError` is a `ValueError`, not an `OSError`, and `main()` has no
      catch-all: handing in a PDF/UTF-16/latin-1 file is an ordinary user mistake
      and must read as the friendly error, not a traceback.
    """
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise _OcrTextFileError(f"cannot read {path}: {exc}") from exc


def _migration_followups(conn: sqlite3.Connection, applied: list[str]) -> list[str]:
    """Manual follow-ups the just-applied migrations leave behind.

    006 moves allergy/condition rows out of `observation` carrying their OLD dedup keys
    (the new ones are sha256 over dictionary-normalized fields, which SQL cannot compute),
    so until `rekey` re-derives them a re-commit of an already-stored allergy/condition
    inserts a second row instead of deduping. Saying so here makes it impossible to miss
    (issue #63) -- but only when rows actually moved: on a fresh `--create` there is
    nothing to rekey, and a note nobody has to act on trains people to skip notes.
    """
    notes: list[str] = []
    if "006_condition_allergy.sql" in applied:
        moved = conn.execute(
            "SELECT (SELECT COUNT(*) FROM allergy) + (SELECT COUNT(*) FROM condition) "
            "AS n"
        ).fetchone()["n"]
        if moved:
            notes.append(
                f"run `pemr rekey --apply` to re-derive dedup keys for the {moved} "
                "allergy/condition row(s) migrated out of `observation`"
            )
    return notes


def _cmd_migrate(args: argparse.Namespace) -> int:
    # The one command allowed to create a database - and only with --create. Without it
    # `migrate` applies migrations to an existing database and nothing else (issue #55).
    db_path = _resolve_db_path(args)
    if not db.database_exists(db_path) and not args.create:
        raise SystemExit(_no_database_message(db_path))
    conn = db.connect(db_path)
    try:
        applied = db.migrate(conn, args.migrations_dir or db.DEFAULT_MIGRATIONS_DIR)
        followups = _migration_followups(conn, applied)
    finally:
        conn.close()
    if applied:
        for name in applied:
            print(f"applied {name}")
        for note in followups:
            print(f"note: {note}")
    else:
        print("up to date")
    return 0


def _cmd_person_add(args: argparse.Namespace) -> int:
    conn = _connect_db(args)
    try:
        try:
            person = persons.add_person(
                conn,
                slug=args.slug,
                full_name=args.name,
                dob=args.dob,
                sex=args.sex,
                blood_type=args.blood_type,
                notes=args.notes,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()
    print(f"added person #{person.person_id}: {person.slug} ({person.full_name})")
    return 0


def _cmd_person_list(args: argparse.Namespace) -> int:
    conn = _connect_db(args)
    try:
        people = persons.list_people(conn, include_inactive=args.all_people)
    finally:
        conn.close()
    if not people:
        print("no people yet - `pemr person add --slug <slug> --name <name>`")
        return 0
    for p in people:
        dob = f"  dob={p.dob}" if p.dob else ""
        status = "  [inactive]" if p.deactivated_at else ""
        print(f"#{p.person_id}  {p.slug}  {p.full_name}{dob}{status}")
    return 0


def _print_person(person) -> None:
    """Print one person record as aligned key/value lines (the `person show` shape,
    reused by `person edit`/`deactivate`/`reactivate` so they echo the updated record)."""
    for key, value in asdict(person).items():
        print(f"{key:14} {value if value is not None else ''}")


def _cmd_person_show(args: argparse.Namespace) -> int:
    conn = _connect_db(args)
    try:
        person = persons.get_person(conn, args.slug)
    finally:
        conn.close()
    if person is None:
        print(f"error: no person with slug '{args.slug}'", file=sys.stderr)
        return 1
    _print_person(person)
    return 0


def _cmd_person_edit(args: argparse.Namespace) -> int:
    # A flag left unset is None -> not part of the update; an explicit empty string
    # (e.g. --dob "") is passed through and clears that nullable column (persons.py).
    fields: dict[str, str] = {}
    if args.name is not None:
        fields["full_name"] = args.name
    if args.dob is not None:
        fields["dob"] = args.dob
    if args.sex is not None:
        fields["sex"] = args.sex
    if args.blood_type is not None:
        fields["blood_type"] = args.blood_type
    if args.notes is not None:
        fields["notes"] = args.notes

    conn = _connect_db(args)
    try:
        try:
            person = persons.update_person(conn, args.slug, **fields)
        except persons.PersonNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()
    _print_person(person)
    return 0


def _cmd_person_deactivate(args: argparse.Namespace) -> int:
    conn = _connect_db(args)
    try:
        try:
            person = persons.deactivate_person(conn, args.slug)
        except persons.PersonNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()
    print(f"deactivated {person.slug} (as of {person.deactivated_at})")
    return 0


def _cmd_person_reactivate(args: argparse.Namespace) -> int:
    conn = _connect_db(args)
    try:
        try:
            person = persons.reactivate_person(conn, args.slug)
        except persons.PersonNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()
    print(f"reactivated {person.slug}")
    return 0


def _cmd_person_remove(args: argparse.Namespace) -> int:
    conn = _connect_db(args)
    try:
        try:
            person = persons.remove_person(conn, args.slug)
        except persons.PersonNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except persons.PersonHasDependentsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()
    print(f"removed person #{person.person_id}: {person.slug}")
    return 0


# --------------------------------------------------------------------------- #
# document recovery (list / edit / reassign / rm) - issue #54
# plus per-document detail + post-ingest text (show / set-text) - issue #62
# --------------------------------------------------------------------------- #

def _with_document_conn(args: argparse.Namespace, work):
    """Open the DB, run ``work(conn)``, translating every friendly `document`
    failure mode (un-migrated DB, unknown id/slug, refusal) into rc=1 on stderr.

    Goes through the :func:`_connect_db` gate like every other read/write command
    (issue #55): a missing database is refused here too, rather than being created
    on connect. `db.NotMigratedError` below still covers the distinct case of a
    database file that exists but has no schema.
    """
    conn = _connect_db(args)
    try:
        try:
            return work(conn)
        except db.NotMigratedError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except (
            documents.DocumentNotFoundError,
            documents.OpenConflictsError,
            documents.DictionaryDriftError,
            documents.ReassignCollisionError,
            documents.OcrTextPresentError,
            persons.PersonNotFoundError,
        ) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()


def _cmd_document_list(args: argparse.Namespace) -> int:
    def work(conn):
        docs = documents.list_documents(conn, args.person)
        if args.json:
            _print_json(docs)
            return 0
        if not docs:
            print("no documents yet - `pemr ingest <file> --person <slug>`")
            return 0
        for d in docs:
            print(
                f"#{d['document_id']:<5} {_fmt(d['doc_date']):11} "
                f"{_fmt(d['category']):12} {_fmt(d['provider']):22} "
                f"{_fmt(d['person']):16} {d['sha256'][:12]}  {d['record_count']} rec"
            )
        return 0

    return _with_document_conn(args, work)


def _print_document_show(doc: dict) -> None:
    """The aligned key/value detail block shared by `document show` and `set-text`.

    The per-type `records` breakdown elides zeros for a human; ``--json`` keeps the full
    stable ``_record_counts`` key set (`documents.py` commits to that for machine
    consumers). Only static ASCII here — issue #23's console-safety convention.
    """
    counts = doc["records"]
    breakdown = ", ".join(f"{name} {n}" for name, n in counts.items() if n)
    records = f"{doc['record_count']}" + (f"  ({breakdown})" if breakdown else "")
    chars = doc["ocr_text_chars"]
    conflicts = (
        ", ".join(f"#{cid}" for cid in doc["conflicts_open"])
        + " open - resolve with `pemr review-conflicts`"
        if doc["conflicts_open"]
        else "none"
    )
    fields = [
        ("document_id", doc["document_id"]),
        ("person", _fmt(doc["person"])),
        ("doc_date", _fmt(doc["doc_date"])),
        ("category", _fmt(doc["category"])),
        ("provider", _fmt(doc["provider"])),
        ("sha256", f"{doc['sha256'][:12]}..."),
        ("source_path", _fmt(doc["source_path"])),
        ("ingested_at", _fmt(doc["ingested_at"])),
        ("has_ocr_text", f"yes ({chars} chars)" if doc["has_ocr_text"] else "no"),
        ("records", records),
        ("conflicts", conflicts),
    ]
    for key, value in fields:
        print(f"{key:14} {value}")


def _cmd_document_show(args: argparse.Namespace) -> int:
    def work(conn):
        if args.text:
            # Raw dump, nothing else, so `document show 7 --text > doc.txt` works like the
            # `render ... > exports/...` redirect contract. This is the only CLI read path
            # for ocr_text, which is what makes `set-text --force` reviewable.
            text = documents.get_document_text(conn, args.document_id)
            if not text:
                print(
                    f"note: document #{args.document_id} has no ocr_text stored",
                    file=sys.stderr,
                )
                return 0
            print(text)
            return 0
        doc = documents.get_document_view(conn, args.document_id)
        if args.json:
            _print_json(doc)
            return 0
        _print_document_show(doc)
        return 0

    return _with_document_conn(args, work)


def _cmd_document_set_text(args: argparse.Namespace) -> int:
    try:
        text = _read_ocr_text_file(args.ocr_text_file)
    except _OcrTextFileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    def work(conn):
        # Read first: the before-size feeds the status line, and resolving the id here
        # means an unknown id outranks an empty file in the error the human sees.
        before = documents.get_document_view(conn, args.document_id)
        if not text.strip():
            # documents.set_document_text guards this too (so MCP gets it); repeated here
            # only to name the offending path, which the engine never sees.
            raise ValueError(
                f"{args.ocr_text_file} is empty - nothing to store; ocr_text unchanged"
            )
        doc = documents.set_document_text(
            conn, args.document_id, text, force=args.force
        )
        if args.json:
            _print_json(doc)
            return 0
        was = (
            f"replaced {before['ocr_text_chars']} chars"
            if before["has_ocr_text"]
            else "was empty"
        )
        print(
            f"set ocr_text on document #{doc['document_id']}: "
            f"{doc['ocr_text_chars']} chars ({was})"
        )
        _print_document_show(doc)
        return 0

    return _with_document_conn(args, work)


def _cmd_document_edit(args: argparse.Namespace) -> int:
    # A flag left unset is None -> not part of the update; an explicit empty string
    # (e.g. --category "") clears that column (documents.py), same as `person edit`.
    fields: dict[str, str] = {}
    if args.doc_date is not None:
        fields["doc_date"] = args.doc_date
    if args.category is not None:
        fields["category"] = args.category
    if args.provider is not None:
        fields["provider"] = args.provider

    def work(conn):
        doc = documents.edit_document(conn, args.document_id, **fields)
        if args.json:
            _print_json(doc)
            return 0
        for key in ("document_id", "person", "doc_date", "category", "provider",
                    "source_path", "ingested_at", "record_count"):
            print(f"{key:14} {_fmt(doc[key])}")
        return 0

    return _with_document_conn(args, work)


def _cmd_document_reassign(args: argparse.Namespace) -> int:
    def work(conn):
        dictionary = dedup.load_dictionary(_resolve_dictionary_path(args))
        report = documents.reassign_document(
            conn, args.document_id, args.person, dictionary, apply=args.apply
        )
        if args.json:
            _print_json({
                "document_id": report.document_id,
                "from": report.from_slug,
                "to": report.to_slug,
                "applied": report.applied,
                "unchanged": report.unchanged,
                "target_inactive": report.target_inactive,
                "counts": report.counts,
                "moved": [
                    {
                        "record_type": c.record_type,
                        "row_id": c.row_id,
                        "label": c.label,
                        "old_key": c.old_key,
                        "new_key": c.new_key,
                    }
                    for c in report.changes
                ],
            })
            return 0
        if report.unchanged:
            print(
                f"document #{report.document_id} is already owned by "
                f"'{report.to_slug}' - nothing to do"
            )
            return 0
        print(
            f"document #{report.document_id}: {_fmt(report.from_slug) or '(none)'} "
            f"-> {report.to_slug}"
        )
        for c in report.changes:
            print(f"  {c.record_type} #{c.row_id}  {c.label}")
        if report.target_inactive:
            print(f"note: '{report.to_slug}' is deactivated (records still move)")
        if report.applied:
            print(f"reassigned {len(report.changes)} record(s)")
        else:
            print(
                f"dry run: {len(report.changes)} record(s) would move - "
                "re-run with --apply (back up first: `pemr backup`)"
            )
        return 0

    return _with_document_conn(args, work)


def _cmd_document_rm(args: argparse.Namespace) -> int:
    # The sources dir is only needed to delete the blob; keeping it (the default)
    # must not require configured paths.
    sources_dir = _resolve_sources_dir(args) if args.purge_blob else None

    def work(conn):
        report = documents.remove_document(
            conn, args.document_id, sources_dir=sources_dir,
            purge_blob=args.purge_blob, apply=args.apply,
        )
        if args.json:
            _print_json({
                "document_id": report.document_id,
                "person": report.person_slug,
                "sha256": report.sha256,
                "applied": report.applied,
                "records": report.records,
                "record_count": report.record_count,
                "conflicts_deleted": report.conflicts_deleted,
                "conflicts_anchored": report.conflicts_anchored,
                "conflicts_detached": report.conflicts_detached,
                "blob_path": report.blob_path,
                "blob_purged": report.blob_purged,
            })
            return 0
        print(
            f"document #{report.document_id}  {_fmt(report.person_slug)}  "
            f"{report.sha256[:12]}"
        )
        for record_type, count in report.records.items():
            if count:
                print(f"  {record_type}: {count} row(s)")
        print(f"  records: {report.record_count} total")
        if report.conflicts_deleted:
            print(f"  conflicts deleted (open): {report.conflicts_deleted}")
        if report.conflicts_anchored:
            print(
                "  conflicts deleted (staged against these records): "
                f"{report.conflicts_anchored}"
            )
        if report.conflicts_detached:
            print(f"  conflicts detached (resolved): {report.conflicts_detached}")
        if not args.purge_blob:
            print(f"  blob kept: {report.blob_path}")
        elif report.blob_purged:
            print(f"  blob deleted: {report.blob_path}")
        elif report.applied:
            print(f"  blob already gone: {report.blob_path}")
        else:
            print(f"  blob would be deleted: {report.blob_path}")
        if report.applied:
            print(f"removed document #{report.document_id}")
        else:
            print(
                "dry run: nothing was deleted - re-run with --apply "
                "(back up first: `pemr backup`)"
            )
        return 0

    return _with_document_conn(args, work)


def _report_owner_check(
    check: "ingest.OwnerCheck | None", person_slug: str, document_id: int
) -> None:
    """Print the issue-#61 owner-verification verdict for a successful ingest.

    A blocking verdict only reaches here when the human passed ``--force`` (otherwise
    `ingest_document` raised pre-write), so that branch points at the undo path.
    """
    if check is None:  # duplicate path: nothing written, nothing checked
        return
    if check.verdict == "match":
        print(f"owner verified: matched '{person_slug}' in document text")
    elif check.blocks:
        print(
            f"warning: owner verification was overridden with --force "
            f"(verdict: {check.verdict}). Filed under '{person_slug}' anyway; undo "
            f"with `pemr document reassign {document_id} --person <slug> --apply`.",
            file=sys.stderr,
        )
    else:
        print(
            "note: owner not verified - no document text available (or no patient "
            f"identity found in it). Filed under '{person_slug}' on your say-so.",
            file=sys.stderr,
        )


def _cmd_ingest(args: argparse.Namespace) -> int:
    ocr_text = None
    if getattr(args, "ocr_text_file", None):
        try:
            ocr_text = _read_ocr_text_file(args.ocr_text_file)
        except _OcrTextFileError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    conn = _connect_db(args)
    try:
        try:
            if args.study:
                # A study *directory* is one document (issue #69): the blob is a
                # canonical zip of its slices, hashed like any other file.
                if args.ocr:
                    print(
                        f"note: --ocr is ignored for --study {args.study} (there is "
                        "no flat image to OCR); the study's ocr_text is a derived "
                        "summary unless you pass --ocr-text-file",
                        file=sys.stderr,
                    )
                result = ingest.ingest_study_dir(
                    conn,
                    args.file,
                    person_slug=args.person,
                    sources_dir=_resolve_sources_dir(args),
                    study=args.study,
                    allow_large=args.allow_large,
                    doc_date=args.doc_date,
                    category=args.category,
                    provider=args.provider,
                    ocr_text=ocr_text,
                    force=args.force,
                )
            else:
                result = ingest.ingest_document(
                    conn,
                    args.file,
                    person_slug=args.person,
                    sources_dir=_resolve_sources_dir(args),
                    doc_date=args.doc_date,
                    category=args.category,
                    provider=args.provider,
                    # `tesseract` is a retained alias for `auto`: both mean "extract
                    # by whatever route this file type allows" (ingest.extract_text).
                    ocr=(args.ocr is not None),
                    ocr_text=ocr_text,
                    force=args.force,
                )
        except db.NotMigratedError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except ingest.IngestError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()

    doc = result.document
    if result.is_duplicate:
        print(
            f"duplicate: already filed as document #{doc.document_id} "
            f"(sha256 {doc.sha256[:12]}...) - nothing ingested"
        )
        return 0
    print(
        f"ingested document #{doc.document_id} (sha256 {doc.sha256[:12]}...) "
        f"-> sources/{doc.source_path}"
    )
    _report_owner_check(result.owner_check, args.person, doc.document_id)
    if not result.ocr_text_populated:
        print(
            "note: no ocr_text stored - `find` (full-text search) will not see this "
            "document. Supply --ocr-text-file <path> or --ocr auto.",
            file=sys.stderr,
        )
    print(f"next: extract, then `pemr commit-extraction --document {doc.document_id} --json <file>`")
    return 0


def _cmd_commit_extraction(args: argparse.Namespace) -> int:
    try:
        with open(args.json, "rb") as fh:
            records = json.load(fh)
    except OSError as exc:
        print(f"error: cannot read {args.json}: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"error: {args.json} is not valid JSON: {exc}", file=sys.stderr)
        return 1

    conn = _connect_db(args)
    try:
        dictionary = dedup.load_dictionary(_resolve_dictionary_path(args))
        try:
            summary = dedup.commit_extraction(
                conn, args.document, records, dictionary
            )
        except db.NotMigratedError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except dedup.ValidationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()

    c = summary.counts
    print(
        f"committed: {c['new']} new, {c['duplicate']} duplicate, "
        f"{c['enriched']} enriched, {c['conflict']} conflict"
    )
    if summary.conflict:
        print(
            f"note: {c['conflict']} conflict(s) staged - resolve with "
            "`pemr review-conflicts`"
        )
    return 0


def _cmd_rekey(args: argparse.Namespace) -> int:
    conn = _connect_db(args)
    try:
        dictionary = dedup.load_dictionary(_resolve_dictionary_path(args))
        try:
            report = dedup.rekey(conn, dictionary, apply=args.apply)
        except db.NotMigratedError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except dedup.RekeyCollisionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()

    if args.json:
        _print_json({
            "applied": report.applied,
            "scanned": report.scanned,
            "changed": [
                {
                    "record_type": c.record_type,
                    "row_id": c.row_id,
                    "label": c.label,
                    "old_key": c.old_key,
                    "new_key": c.new_key,
                }
                for c in report.changes
            ],
        })
        return 0

    for record_type, count in report.scanned.items():
        changed = sum(1 for c in report.changes if c.record_type == record_type)
        print(f"{record_type}: {changed}/{count} key(s) change")
    for c in report.changes:
        print(f"  {c.record_type} #{c.row_id}  {c.label}")
    if not report.changes:
        print("all dedup keys already match the current dictionary")
        return 0
    if report.applied:
        print(f"rekeyed {len(report.changes)} row(s)")
    else:
        print(
            f"dry run: {len(report.changes)} row(s) would change - "
            "re-run with --apply (back up first: `pemr backup`)"
        )
    return 0


def _cmd_review_conflicts(args: argparse.Namespace) -> int:
    conn = _connect_db(args)
    try:
        try:
            # Resolutions re-derive the conflict's identity family under the current
            # dictionary; a conflict staged before a dictionary edit carries a key no
            # row still holds (issue #58).
            dictionary = dedup.load_dictionary(_resolve_dictionary_path(args))
            if args.resolve is not None:
                result = dedup.resolve_conflict(
                    conn, args.resolve, keep=args.keep, note=args.note,
                    dictionary=dictionary,
                )
                print(f"resolved conflict #{args.resolve} ({_resolved_as(result)})")
                return 0
            conflicts = dedup.list_conflicts(
                conn, status=None if args.all else "open"
            )
            # Family size per conflict, read before the connection closes: it is the
            # minimum a reviewer needs to tell "re-commit of an already-admitted draw"
            # from "genuine third draw" (richer rendering is issue #59).
            occurrences = {
                row["conflict_id"]: dedup.conflict_occurrences(conn, row, dictionary)
                for row in conflicts
            }
        except db.NotMigratedError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:   # includes ValidationError from a keep-both payload
            print(f"error: {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()

    if not conflicts:
        print("no open conflicts")
        return 0
    for row in conflicts:
        print(
            f"#{row['conflict_id']}  {row['record_type']}  [{row['status']}]  "
            f"key={row['dedup_key'][:12]}..."
        )
        print(f"    existing: {row['existing_json']}")
        print(f"    incoming: {row['incoming_json']}")
        count = occurrences.get(row["conflict_id"], 0)
        if count > 1:
            print(f"    occurrences: {count} rows already stored under this key")
    print(
        "\nresolve: `pemr review-conflicts --resolve <id> --keep "
        "existing|incoming|both [--note ...]`"
    )
    return 0


def _resolved_as(result: dedup.ResolveResult) -> str:
    """How a resolution reports itself on the success line.

    A keep-both no-op on a sparse type is only a no-op about the *row count*: it still
    fills the matched sibling's NULL columns from the staged payload (issue #63). Naming
    those fields keeps the operator's line honest about the write, matching what
    `dedup._resolution_text` persists on the conflict. Field names only, never values --
    the resolution text is an audit trail, not a place to echo clinical data.
    """
    if result.kept != "both":
        return f"keep-{result.kept}"
    if result.no_op:
        filled = (
            f", filled {', '.join(sorted(result.gains))}" if result.gains else ""
        )
        return (
            f"keep-both, no-op: already stored as {result.record_type} "
            f"#{result.row_id}{filled}"
        )
    return (
        f"keep-both -> {result.record_type} #{result.row_id}, "
        f"occurrence {result.occurrence}"
    )


# --------------------------------------------------------------------------- #
# Phase 3: query / find / trends
# --------------------------------------------------------------------------- #

# Internal columns never emitted in --json (unstable / not part of the contract).
_HIDDEN_FIELDS = dedup.INTERNAL_COLUMNS


def _clean(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in _HIDDEN_FIELDS}


def _print_json(payload: object) -> None:
    # ensure_ascii keeps output cp1252-console-safe (phase 2 lesson).
    print(json.dumps(payload, indent=2, ensure_ascii=True, default=str))


def _fmt(value: object) -> str:
    return "" if value is None else str(value)


def _with_conn_person(args: argparse.Namespace, work):
    """Open the DB, run ``work(conn)``, translating the two friendly failure modes
    (un-migrated DB, unknown person slug) into an rc=1 stderr message."""
    conn = _connect_db(args)
    try:
        try:
            return work(conn)
        except db.NotMigratedError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except query.PersonNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()


def _cmd_query_labs(args: argparse.Namespace) -> int:
    def work(conn):
        dictionary = dedup.load_dictionary(_resolve_dictionary_path(args))
        rows = query.query_labs(
            conn, args.person, test=args.test, since=args.since, dictionary=dictionary
        )
        if args.json:
            _print_json([_clean(r) for r in rows])
            return 0
        if not rows:
            print("no lab results")
            return 0
        for r in rows:
            value = r["value_num"] if r["value_num"] is not None else r["value_text"]
            unit = f" {r['unit']}" if r["unit"] else ""
            flag = f"  [{r['flag']}]" if r["flag"] else ""
            # Reuse render's shared helper so one-sided ranges (issue #45) render
            # as "(ref <= 20)" / "(ref >= 8)" instead of a bogus "(ref -20.0)".
            ref = render._ref_range(r)
            print(f"{_fmt(r['collected_at']):19}  {r['test_name']:20}  "
                  f"{_fmt(value)}{unit}{flag}{ref}")
        return 0

    return _with_conn_person(args, work)


def _cmd_query_meds(args: argparse.Namespace) -> int:
    def work(conn):
        rows = query.query_meds(conn, args.person, active=args.active)
        if args.json:
            _print_json([_clean(r) for r in rows])
            return 0
        if not rows:
            print("no active medications" if args.active else "no medications")
            return 0
        for r in rows:
            dose = f"  {r['dose']}" if r["dose"] else ""
            freq = f"  {r['frequency']}" if r["frequency"] else ""
            if r["ended_on"]:
                end = f" -> {r['ended_on']}"
            elif query.med_is_current(r):
                end = " -> (current)"
            else:  # terminal status but no explicit end date (issue #21)
                end = " -> (ended)"
            span = _fmt(r["started_on"]) + end
            status = f"  [{r['status']}]" if r["status"] else ""
            print(f"{r['name']:24}{dose}{freq}  {span}{status}")
        return 0

    return _with_conn_person(args, work)


def _cmd_query_timeline(args: argparse.Namespace) -> int:
    def work(conn):
        events = query.query_timeline(conn, args.person, since=args.since)
        if args.json:
            _print_json(events)
            return 0
        if not events:
            print("no events")
            return 0
        for e in events:
            prov = f"  (doc #{e['document_id']})" if e["document_id"] is not None else ""
            print(f"{e['date']:10}  {e['type']:12}  {e['summary']}{prov}")
        return 0

    return _with_conn_person(args, work)


def _cmd_find(args: argparse.Namespace) -> int:
    def work(conn):
        hits = query.find(conn, args.person, args.query)
        if args.json:
            _print_json(hits)
            return 0
        if not hits:
            print("no matches")
            return 0
        for h in hits:
            prov = f"  (doc #{h['document_id']})" if h["document_id"] is not None else ""
            who = "" if args.person is not None else f"{h['person']}  "
            print(f"{who}{h['source_table']}#{h['source_id']}{prov}: {h['snippet']}")
        return 0

    return _with_conn_person(args, work)


def _cmd_trends(args: argparse.Namespace) -> int:
    def work(conn):
        dictionary = dedup.load_dictionary(_resolve_dictionary_path(args))
        result = query.trends(conn, args.person, args.test, dictionary=dictionary)
        if args.json:
            _print_json(result)
            return 0
        if result["count"] == 0:
            print(f"no numeric results for '{result['test']}'")
            return 0
        unit = f" {result['unit']}" if result["unit"] else ""
        print(f"{result['test']}  ({result['count']} point(s))")
        print(f"  min    {_fmt(result['min'])}{unit}")
        print(f"  max    {_fmt(result['max'])}{unit}")
        tie = result.get("latest_tie", 0)
        tie_note = f"  (1 of {tie} at this timestamp)" if tie > 1 else ""
        print(
            f"  latest {_fmt(result['latest'])}{unit}"
            f"  @ {_fmt(result['latest_at'])}{tie_note}"
        )
        if result["slope_per_day"] is None:
            print("  slope  n/a (need >=2 distinct dates)")
        else:
            print(f"  slope  {result['slope_per_day']:+.4g}{unit}/day")
        return 0

    return _with_conn_person(args, work)


# --------------------------------------------------------------------------- #
# Phase 4: render (summary / brief / journal)
# --------------------------------------------------------------------------- #

def _emit_markdown(markdown: str, out: str | None) -> int:
    """Write rendered Markdown to ``--out`` or stdout (the §5 shell-redirect default)."""
    if out:
        try:
            with open(out, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(markdown)
        except OSError as exc:
            print(f"error: cannot write {out}: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {out}")
        return 0
    # Default stdout keeps the documented `> exports/...` redirect contract. Output is
    # ASCII-only by construction (render layer), so a cp1252/cp437 console is safe.
    print(markdown, end="" if markdown.endswith("\n") else "\n")
    return 0


def _render_with_conn(args: argparse.Namespace, work) -> int:
    """Open the DB, run ``work(conn)``, translating render's friendly failures
    (un-migrated DB, unknown person slug, unknown appointment id) into rc=1 stderr."""
    conn = _connect_db(args)
    try:
        try:
            return work(conn)
        except db.NotMigratedError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except (query.PersonNotFoundError, render.AppointmentNotFoundError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()


def _cmd_render_summary(args: argparse.Namespace) -> int:
    def work(conn):
        dictionary = dedup.load_dictionary(_resolve_dictionary_path(args))
        markdown = render.render_summary(conn, args.person, dictionary=dictionary)
        return _emit_markdown(markdown, args.out)

    return _render_with_conn(args, work)


def _cmd_render_brief(args: argparse.Namespace) -> int:
    def work(conn):
        dictionary = dedup.load_dictionary(_resolve_dictionary_path(args))
        markdown = render.render_brief(conn, args.appointment, dictionary=dictionary)
        return _emit_markdown(markdown, args.out)

    return _render_with_conn(args, work)


def _cmd_render_journal(args: argparse.Namespace) -> int:
    def work(conn):
        markdown = render.render_journal(conn, args.person, since=args.since)
        return _emit_markdown(markdown, args.out)

    return _render_with_conn(args, work)


# --------------------------------------------------------------------------- #
# Phase 6: backup (VACUUM INTO snapshot + rotation)
# --------------------------------------------------------------------------- #

def _backup_source_problem(db_path: Path) -> str | None:
    """None if ``db_path`` is a real archive, else the refusal to print.

    Returns text rather than raising so `backup` keeps its established rc=1 shape.

    Issue #55: `backup.snapshot` only checks that the file *exists*, so a zero-byte or
    schema-less `pemr.db` used to produce a structurally valid (and therefore
    integrity-clean) 4 KB snapshot at rc=0 - which then became a legitimate rotation
    candidate and could prune the real snapshots. That is the exact chain the issue's
    drill describes: an empty archive becomes a valid backup source and eats the
    backups. The `migrate` gate closed the route that manufactured the empty database;
    this closes every other route into it.

    Deliberately not inside `backup.snapshot`: `pemr restore --force` snapshots the
    live database as a rescue copy precisely when that database may be damaged, and
    banking a damaged database is still better than discarding it.
    """
    if not db.database_exists(db_path):
        return _no_database_message(db_path)
    # A bare sqlite3 connection, not db.connect: this must not flip journal_mode on a
    # database it is about to refuse, and a file that will not read at all is not this
    # function's story to tell - backup.snapshot reports the real sqlite error.
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error:
        return None
    try:
        migrated = db.is_migrated(conn)
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()
    if migrated:
        return None
    return (
        f"error: {db_path} has no pemr schema - refusing to snapshot it\n"
        "a snapshot of an empty database is worthless, and rotation could prune your "
        "real snapshots to keep it.\n"
        "  - restoring after data loss?  pemr restore latest\n"
        "  - starting a new archive?     pemr migrate --create"
    )


def _cmd_backup(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args)
    backup_dir = _resolve_backup_dir(args)
    problem = _backup_source_problem(db_path)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 1
    try:
        snap, size = backup.snapshot(db_path, backup_dir)
    except backup.BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    kept: list[Path] = []
    pruned: list[Path] = []
    rotated = not args.no_rotate
    if rotated:
        keep_daily, keep_weekly = _resolve_retention(args)
        try:
            result = backup.rotate(backup_dir, keep_daily, keep_weekly, protect=snap)
        except OSError as exc:
            print(f"error: rotation failed: {exc}", file=sys.stderr)
            return 1
        kept, pruned = result.kept, result.pruned

    if args.json:
        _print_json({
            "snapshot": str(snap),
            "bytes": size,
            "kept": [str(p) for p in kept],
            "pruned": [str(p) for p in pruned],
        })
        return 0

    print(f"wrote {snap} ({size} bytes)")
    if rotated:
        print(f"rotated: kept {len(kept)}, pruned {len(pruned)}")
    else:
        print("rotation skipped (--no-rotate)")
    return 0


# --------------------------------------------------------------------------- #
# Issue #55: restore (the other direction) + verify (DB + blob health)
# --------------------------------------------------------------------------- #

def _cmd_restore(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args)
    try:
        result = restore.restore(
            args.snapshot,
            db_path,
            backup_dir=_resolve_backup_dir_optional(args),
            sources_dir=_resolve_sources_dir_optional(args),
            migrations_dir=args.migrations_dir,
            force=args.force,
        )
    except restore.RestoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    report = result.report
    if args.json:
        _print_json({
            "snapshot": str(result.snapshot),
            "database": str(result.db_path),
            "rescue": str(result.rescue) if result.rescue else None,
            "cleared_sidecars": [str(p) for p in result.cleared_sidecars],
            "applied_migrations": result.applied_migrations,
            "report": report.as_dict() if report else None,
        })
        return 0

    if result.rescue:
        print(f"rescue copy of the previous database: {result.rescue}")
    for sidecar in result.cleared_sidecars:
        print(f"cleared stale sidecar {sidecar.name}")
    print(f"restored {result.db_path} from {result.snapshot}")
    if result.applied_migrations:
        for name in result.applied_migrations:
            print(f"applied {name}")
    else:
        print("migrations up to date")
    if report is not None:
        for line in verify.format_report(report):
            print(line)
        if not report.ok:
            # Blob problems are a warning, not a failure: the database restore
            # genuinely succeeded, and `sources/` may simply be mid-sync.
            print(
                "warning: the database restored, but the checks above found problems",
                file=sys.stderr,
            )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    conn = _connect_db(args)
    try:
        report = verify.verify_report(conn, _resolve_sources_dir_optional(args))
    finally:
        conn.close()
    # Both modes share the exit code: `--json` is what a monitoring cron picks, and a
    # verification command that reports success on a failed verification is worse than
    # useless there (the `ok` field is not what scripts check).
    if args.json:
        _print_json(report.as_dict())
    else:
        for line in verify.format_report(report):
            print(line)
    return 0 if report.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pemr", description="Personal EMR engine - SQLite is truth."
    )
    parser.add_argument("--version", action="version", version=f"pemr {__version__}")
    parser.add_argument("--db", help="path to pemr.db (overrides PEMR_DB/config)")
    parser.add_argument("--config", help="path to config.toml (default ./config.toml)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_migrate = sub.add_parser(
        "migrate", help="apply pending migrations to an existing database"
    )
    p_migrate.add_argument(
        "--migrations-dir", help="override migrations directory (mainly for tests)"
    )
    p_migrate.add_argument(
        "--create", action="store_true",
        help="bootstrap a brand-new empty database (required when none exists)",
    )
    p_migrate.set_defaults(func=_cmd_migrate)

    p_person = sub.add_parser("person", help="manage the people roster")
    person_sub = p_person.add_subparsers(dest="person_command", required=True)

    p_add = person_sub.add_parser("add", help="add a person")
    p_add.add_argument("--slug", required=True, help="unique slug, e.g. jane-doe")
    p_add.add_argument("--name", required=True, help="full name")
    p_add.add_argument("--dob", help="ISO date, e.g. 1980-01-01")
    p_add.add_argument("--sex")
    p_add.add_argument("--blood-type", dest="blood_type")
    p_add.add_argument("--notes")
    p_add.set_defaults(func=_cmd_person_add)

    p_list = person_sub.add_parser("list", help="list people")
    p_list.add_argument(
        "--all", dest="all_people", action="store_true",
        help="include deactivated people",
    )
    p_list.set_defaults(func=_cmd_person_list)

    p_show = person_sub.add_parser("show", help="show one person")
    p_show.add_argument("slug")
    p_show.set_defaults(func=_cmd_person_show)

    p_edit = person_sub.add_parser(
        "edit", help="update a person's fields (partial; slug is not editable)"
    )
    p_edit.add_argument("slug")
    p_edit.add_argument("--name", help="full name (non-empty)")
    p_edit.add_argument("--dob", help='ISO date; pass "" to clear')
    p_edit.add_argument("--sex", help='pass "" to clear')
    p_edit.add_argument("--blood-type", dest="blood_type", help='pass "" to clear')
    p_edit.add_argument("--notes", help='pass "" to clear')
    p_edit.set_defaults(func=_cmd_person_edit)

    p_deactivate = person_sub.add_parser(
        "deactivate", help="soft-deactivate a person (reversible; hides from list)"
    )
    p_deactivate.add_argument("slug")
    p_deactivate.set_defaults(func=_cmd_person_deactivate)

    p_reactivate = person_sub.add_parser(
        "reactivate", help="undo deactivate (restore to the default list)"
    )
    p_reactivate.add_argument("slug")
    p_reactivate.set_defaults(func=_cmd_person_reactivate)

    p_remove = person_sub.add_parser(
        "remove", help="hard-delete a person (only if they have no records)"
    )
    p_remove.add_argument("slug")
    p_remove.set_defaults(func=_cmd_person_remove)

    # --- document recovery (misfiled document escape hatch, issue #54) ----
    p_document = sub.add_parser(
        "document", help="inspect and recover misfiled documents"
    )
    document_sub = p_document.add_subparsers(dest="document_command", required=True)

    d_list = document_sub.add_parser(
        "list", help="list ingested documents, newest first"
    )
    d_list.add_argument("--person", help="owner slug; omit to list every person's")
    d_list.add_argument("--json", action="store_true", help="machine-readable output")
    d_list.set_defaults(func=_cmd_document_list)

    # --- per-document detail + post-ingest text (issue #62) ---------------
    d_show = document_sub.add_parser(
        "show", help="one document's metadata, record counts and open conflicts"
    )
    d_show.add_argument("document_id", type=int, metavar="ID")
    # Mutually exclusive: --json is the metadata view, --text is the raw transcription.
    d_show_out = d_show.add_mutually_exclusive_group()
    d_show_out.add_argument(
        "--json", action="store_true", help="machine-readable output"
    )
    d_show_out.add_argument(
        "--text", action="store_true",
        help="dump the stored ocr_text to stdout and nothing else",
    )
    d_show.set_defaults(func=_cmd_document_show)

    d_set_text = document_sub.add_parser(
        "set-text", help="attach or replace a document's ocr_text after ingest"
    )
    d_set_text.add_argument("document_id", type=int, metavar="ID")
    d_set_text.add_argument(
        "--ocr-text-file", dest="ocr_text_file", required=True,
        help="file of document text to store as ocr_text (same flag as `ingest`)",
    )
    d_set_text.add_argument(
        "--force", action="store_true",
        help="replace existing ocr_text (refused without this)",
    )
    d_set_text.add_argument("--json", action="store_true", help="machine-readable output")
    d_set_text.set_defaults(func=_cmd_document_set_text)

    d_edit = document_sub.add_parser(
        "edit", help="correct a document's date/category/provider (partial)"
    )
    d_edit.add_argument("document_id", type=int, metavar="ID")
    d_edit.add_argument("--doc-date", dest="doc_date", help='pass "" to clear')
    d_edit.add_argument("--category", help='pass "" to clear')
    d_edit.add_argument("--provider", help='pass "" to clear')
    d_edit.add_argument("--json", action="store_true", help="machine-readable output")
    d_edit.set_defaults(func=_cmd_document_edit)

    d_reassign = document_sub.add_parser(
        "reassign",
        help="move a document and its records to another person (dry run by default)",
    )
    d_reassign.add_argument("document_id", type=int, metavar="ID")
    d_reassign.add_argument("--person", required=True, help="new owner slug")
    d_reassign.add_argument(
        "--apply", action="store_true", help="write the move (default: report only)"
    )
    d_reassign.add_argument(
        "--dictionary", help="synonym dictionary TOML (overrides default)"
    )
    d_reassign.add_argument("--json", action="store_true", help="machine-readable output")
    d_reassign.set_defaults(func=_cmd_document_reassign)

    d_rm = document_sub.add_parser(
        "rm",
        help="delete a document and its records (dry run by default)",
    )
    d_rm.add_argument("document_id", type=int, metavar="ID")
    d_rm.add_argument(
        "--apply", action="store_true", help="write the delete (default: report only)"
    )
    d_rm.add_argument(
        "--purge-blob", dest="purge_blob", action="store_true",
        help="also delete the stored scan (irreversible; kept by default)",
    )
    d_rm.add_argument("--sources", help="sources blob dir (overrides config)")
    d_rm.add_argument("--json", action="store_true", help="machine-readable output")
    d_rm.set_defaults(func=_cmd_document_rm)

    p_ingest = sub.add_parser(
        "ingest", help="ingest a document (hash, blob store, layer-1 dedup)"
    )
    p_ingest.add_argument(
        "file", help="path to the document, or study directory with --study"
    )
    p_ingest.add_argument("--person", required=True, help="owner slug, e.g. jane-doe")
    p_ingest.add_argument(
        "--study",
        choices=list(study.STUDY_KINDS),
        help="treat the path as a study directory: pack it into one document",
    )
    p_ingest.add_argument(
        "--allow-large",
        dest="allow_large",
        action="store_true",
        help="ingest a study over the size limit (sources_dir may be cloud-synced)",
    )
    p_ingest.add_argument("--sources", help="sources blob dir (overrides config)")
    p_ingest.add_argument("--doc-date", dest="doc_date", help="date the doc pertains to")
    p_ingest.add_argument("--category", help="labs|imaging|visit-note|rx|vaccine|...")
    p_ingest.add_argument("--provider")
    p_ingest.add_argument(
        "--ocr",
        choices=["auto", "tesseract"],
        help="pre-fill ocr_text: text/.docx/.xlsx read natively, images/PDF via "
             "tesseract (soft dependency). 'tesseract' is an alias for 'auto'",
    )
    p_ingest.add_argument(
        "--ocr-text-file",
        dest="ocr_text_file",
        help="file of agent-supplied document text to store as ocr_text (wins over --ocr)",
    )
    p_ingest.add_argument(
        "--force",
        action="store_true",
        help="ingest even if the document text does not name --person",
    )
    p_ingest.set_defaults(func=_cmd_ingest)

    p_commit = sub.add_parser(
        "commit-extraction",
        help="validate + dedup + commit extracted rows for a document",
    )
    p_commit.add_argument(
        "--document", type=int, required=True, help="document id from `pemr ingest`"
    )
    p_commit.add_argument(
        "--json", required=True, help="extraction JSON: {type: [rows]}"
    )
    p_commit.add_argument("--dictionary", help="synonym dictionary TOML (overrides default)")
    p_commit.set_defaults(func=_cmd_commit_extraction)

    p_rekey = sub.add_parser(
        "rekey",
        help="recompute stored dedup keys after a dictionary edit (dry run by default)",
    )
    p_rekey.add_argument(
        "--apply", action="store_true",
        help="write the recomputed keys (default: report only)",
    )
    p_rekey.add_argument("--dictionary", help="synonym dictionary TOML (overrides default)")
    p_rekey.add_argument("--json", action="store_true", help="machine-readable output")
    p_rekey.set_defaults(func=_cmd_rekey)

    p_review = sub.add_parser(
        "review-conflicts", help="list or resolve staged dedup conflicts"
    )
    p_review.add_argument(
        "--resolve", type=int, metavar="CONFLICT_ID", help="resolve one conflict"
    )
    p_review.add_argument(
        "--keep", choices=list(dedup.KEEP_CHOICES), default="existing",
        help="on --resolve: keep the stored row, overwrite it with the incoming one, "
             "or 'both' = admit the incoming row alongside the stored one as a new "
             "occurrence of the same identity (use for a genuine repeat the source "
             "cannot timestamp). Default existing",
    )
    p_review.add_argument("--note", help="optional resolution note")
    p_review.add_argument(
        "--dictionary",
        help="synonym dictionary TOML (overrides default); a resolution re-derives "
             "the conflict's identity through it",
    )
    p_review.add_argument(
        "--all", action="store_true", help="list resolved conflicts too"
    )
    p_review.set_defaults(func=_cmd_review_conflicts)

    # --- phase 3: query / find / trends -----------------------------------
    p_query = sub.add_parser("query", help="structured reads over the record tables")
    query_sub = p_query.add_subparsers(dest="query_command", required=True)

    q_labs = query_sub.add_parser("labs", help="lab results for a person")
    q_labs.add_argument("--person", required=True, help="owner slug")
    q_labs.add_argument("--test", help="analyte name (dictionary-normalized)")
    q_labs.add_argument("--since", help="ISO date; keep rows on/after this date")
    q_labs.add_argument("--dictionary", help="synonym dictionary TOML (overrides default)")
    q_labs.add_argument("--json", action="store_true", help="machine-readable output")
    q_labs.set_defaults(func=_cmd_query_labs)

    q_meds = query_sub.add_parser("meds", help="medications for a person")
    q_meds.add_argument("--person", required=True, help="owner slug")
    q_meds.add_argument("--active", action="store_true", help="current meds only")
    q_meds.add_argument("--json", action="store_true", help="machine-readable output")
    q_meds.set_defaults(func=_cmd_query_meds)

    q_timeline = query_sub.add_parser(
        "timeline", help="merged chronological event stream"
    )
    q_timeline.add_argument("--person", required=True, help="owner slug")
    q_timeline.add_argument("--since", help="ISO date; keep events on/after this date")
    q_timeline.add_argument("--json", action="store_true", help="machine-readable output")
    q_timeline.set_defaults(func=_cmd_query_timeline)

    p_find = sub.add_parser("find", help="full-text search over OCR text + record fields")
    p_find.add_argument("--person", help="owner slug; omit to search all people")
    p_find.add_argument("query", help="search text")
    p_find.add_argument("--json", action="store_true", help="machine-readable output")
    p_find.set_defaults(func=_cmd_find)

    p_trends = sub.add_parser(
        "trends", help="min/max/latest/slope for one analyte over time"
    )
    p_trends.add_argument("--person", required=True, help="owner slug")
    p_trends.add_argument("--test", required=True, help="analyte name (dictionary-normalized)")
    p_trends.add_argument("--dictionary", help="synonym dictionary TOML (overrides default)")
    p_trends.add_argument("--json", action="store_true", help="machine-readable output")
    p_trends.set_defaults(func=_cmd_trends)

    # --- phase 4: render (summary / brief / journal) ----------------------
    p_render = sub.add_parser(
        "render", help="generate Markdown documents from DB state (read-only)"
    )
    render_sub = p_render.add_subparsers(dest="render_command", required=True)

    r_summary = render_sub.add_parser(
        "summary", help="master summary for a person -> Markdown on stdout"
    )
    r_summary.add_argument("--person", required=True, help="owner slug")
    r_summary.add_argument("--dictionary", help="synonym dictionary TOML (overrides default)")
    r_summary.add_argument("--out", help="write to file instead of stdout")
    r_summary.set_defaults(func=_cmd_render_summary)

    r_brief = render_sub.add_parser(
        "brief", help="walk-in brief for one appointment -> Markdown on stdout"
    )
    r_brief.add_argument(
        "--appointment", type=int, required=True, help="appointment id"
    )
    r_brief.add_argument("--dictionary", help="synonym dictionary TOML (overrides default)")
    r_brief.add_argument("--out", help="write to file instead of stdout")
    r_brief.set_defaults(func=_cmd_render_brief)

    r_journal = render_sub.add_parser(
        "journal", help="narrative chronology for a person -> Markdown on stdout"
    )
    r_journal.add_argument("--person", required=True, help="owner slug")
    r_journal.add_argument("--since", help="ISO date; keep events on/after this date")
    r_journal.add_argument("--out", help="write to file instead of stdout")
    r_journal.set_defaults(func=_cmd_render_journal)

    # --- phase 6: backup (VACUUM INTO snapshot + rotation) ----------------
    p_backup = sub.add_parser(
        "backup", help="VACUUM INTO timestamped snapshot + rotate old snapshots"
    )
    p_backup.add_argument(
        "--backup-dir", dest="backup_dir",
        help="output dir (overrides PEMR_BACKUP_DIR / [paths].backup_dir)",
    )
    p_backup.add_argument(
        "--keep-daily", dest="keep_daily", type=int,
        help="retain newest snapshot for this many recent days "
             "(default [backup].keep_daily or 7)",
    )
    p_backup.add_argument(
        "--keep-weekly", dest="keep_weekly", type=int,
        help="retain newest snapshot for this many recent ISO weeks "
             "(default [backup].keep_weekly or 4)",
    )
    p_backup.add_argument(
        "--no-rotate", dest="no_rotate", action="store_true",
        help="take the snapshot, skip pruning entirely",
    )
    p_backup.add_argument("--json", action="store_true", help="machine-readable output")
    p_backup.set_defaults(func=_cmd_backup)

    # --- issue #55: restore + verify --------------------------------------
    p_restore = sub.add_parser(
        "restore",
        help="install a backup snapshot over the live database (then migrate + verify)",
    )
    p_restore.add_argument(
        "snapshot",
        help="snapshot path, a bare filename inside the backup dir, or 'latest'",
    )
    p_restore.add_argument(
        "--force", action="store_true",
        help="allow replacing an existing database (a pemr-prerestore-*.sqlite "
             "rescue copy is taken first)",
    )
    p_restore.add_argument(
        "--backup-dir", dest="backup_dir",
        help="where snapshots live (overrides PEMR_BACKUP_DIR / [paths].backup_dir)",
    )
    p_restore.add_argument("--sources", help="sources blob dir (overrides config)")
    p_restore.add_argument(
        "--migrations-dir", help="override migrations directory (mainly for tests)"
    )
    p_restore.add_argument("--json", action="store_true", help="machine-readable output")
    p_restore.set_defaults(func=_cmd_restore)

    p_verify = sub.add_parser(
        "verify",
        help="integrity + migrations + row counts + source blob resolution (read-only)",
    )
    p_verify.add_argument("--sources", help="sources blob dir (overrides config)")
    p_verify.add_argument("--json", action="store_true", help="machine-readable output")
    p_verify.set_defaults(func=_cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Force UTF-8 stdout/stderr so stored document content (accents, em-dashes,
    # smart quotes) prints verbatim instead of `?` on a legacy Windows console
    # codepage (cp1252/cp437). Guarded: streams without ``reconfigure`` (already
    # wrapped, or pytest capture) are left untouched. Orthogonal to issue #23's
    # ASCII-literal convention — that governs static messages, this governs the
    # dynamic data echoed by ``find`` (issue #46).
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

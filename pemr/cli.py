"""`pemr` command-line entry point (phase 1: migrate, person add|list|show).

DB path resolution order: --db flag > PEMR_DB env var > config.toml
[paths].data_dir + /pemr.db. Config path resolution: --config flag >
PEMR_CONFIG env var > ./config.toml.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from . import (
    __version__, attestations, backup, curation, db, dedup, documents, ingest, persons,
    query, records, render, restore, study, tombstones, verify,
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
            records.RecordNotFoundError,
            records.AnchoredConflictError,
            curation.CurationNotFoundError,
            curation.FamilyNotFoundError,
            curation.RowNotFoundError,
            attestations.AttestationCollisionError,
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
        if not documents.normalize_document_text(text):
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


def _warn_purge_without_tombstone(
    report: "documents.RemoveReport", args: argparse.Namespace
) -> None:
    """`--purge-blob` over-promises without a tombstone (issue #80) — say so.

    Purging deletes the stored scan, which reads final, but layer-1 identity is scoped
    to the live `document` table: the next sweep over the unchanged source folder
    re-ingests the file as new. The two flags stay **orthogonal** rather than
    `--purge-blob` implying `--tombstone` — purging is disk hygiene ("this 400 MB scan
    is junk"), tombstoning is content policy ("never file this again"), and conflating
    them would make every space-reclaiming purge a permanent silent block, which is the
    blanket ignore-list failure the design rejects. So: a warning, not a behaviour change.

    Emitted on the **dry run too**: a warning that only appears after the irreversible
    run is useless. Suppressed when a tombstone was asked for or already exists (no
    nagging on a re-run). The git-history caveat is unconditional on `--tombstone` — the
    blob may still be in a data repo's history, which no flag here can fix.
    """
    if not args.purge_blob:
        return
    if not (report.tombstoned or report.tombstone_existed):
        print(
            "warning: --purge-blob deletes the stored scan but does not prevent "
            "re-ingest. The next\n"
            "  sweep over the source folder will re-ingest this file as new. Add "
            "--tombstone to\n"
            "  record that the removal was intentional.",
            file=sys.stderr,
        )
    if report.blob_purged:
        print(
            "note: if this blob was ever committed to a data repo, purging it here "
            "does not\n  remove it from that repo's git history.",
            file=sys.stderr,
        )


def _cmd_document_rm(args: argparse.Namespace) -> int:
    # The sources dir is only needed to delete the blob; keeping it (the default)
    # must not require configured paths.
    sources_dir = _resolve_sources_dir(args) if args.purge_blob else None
    if (args.reason is not None or args.note is not None) and not args.tombstone:
        # An argparse error (usage + rc=2), not a silent ignore: `--reason identifiers`
        # without `--tombstone` reads as "I recorded why", and it would record nothing.
        # The owning subparser is carried on the namespace so the usage line names
        # `pemr document rm` rather than the top-level parser.
        args.parser.error(
            "--reason/--note only apply with --tombstone; nothing was written"
        )

    def work(conn):
        report = documents.remove_document(
            conn, args.document_id, sources_dir=sources_dir,
            purge_blob=args.purge_blob, tombstone=args.tombstone,
            reason=args.reason, note=args.note, apply=args.apply,
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
                # Appended, never inserted: the --json key set is a contract.
                "curation_retired": report.curation_retired,
                "blob_path": report.blob_path,
                "blob_purged": report.blob_purged,
                "tombstoned": report.tombstoned,
                "tombstone_reason": report.tombstone_reason,
                "tombstone_note": report.tombstone_note,
                "tombstone_existed": report.tombstone_existed,
            })
            _warn_purge_without_tombstone(report, args)
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
        for verdict in report.curation_retired:
            # Row-scoped verdicts on the doomed rows (issue #114) - named rather than
            # counted, for the same reason `record rm` names its one.
            lifted = "lifted" if report.applied else "would be lifted"
            print(
                f"  curation verdict (row scope) {lifted}: {verdict['record_type']} "
                f"#{verdict['record_id']}  {curation.describe(verdict)}"
            )
        if not args.purge_blob:
            print(f"  blob kept: {report.blob_path}")
        elif report.blob_purged:
            print(f"  blob deleted: {report.blob_path}")
        elif report.applied:
            print(f"  blob already gone: {report.blob_path}")
        else:
            print(f"  blob would be deleted: {report.blob_path}")
        if report.tombstoned:
            verb = "tombstone updated" if report.tombstone_existed else "tombstone recorded"
            if not report.applied:
                verb = (
                    "would update tombstone" if report.tombstone_existed
                    else "would record tombstone"
                )
            detail = f" (reason: {report.tombstone_reason})" if report.tombstone_reason else ""
            print(f"  {verb}{detail}")
        if report.applied:
            print(f"removed document #{report.document_id}")
        else:
            print(
                "dry run: nothing was deleted - re-run with --apply "
                "(back up first: `pemr backup`)"
            )
        _warn_purge_without_tombstone(report, args)
        return 0

    return _with_document_conn(args, work)


# --- tombstones (intentional-removal memory, issue #80) ---------------------

def _tombstone_row_line(row: dict) -> str:
    """One `tombstone list` line. Truncated hash (full one is a `--json` concern)."""
    live = row.get("live_document_id")
    note = row["note"] or ""
    if live:
        marker = f"(live as document #{live})"
        note = f"{note} {marker}".strip() if note else marker
    return (
        f"{row['sha256'][:12]}  {(row['removed_at'] or '')[:10]:10}  "
        f"{('#' + str(row['document_id'])) if row['document_id'] else '-':5} "
        f"{_fmt(row['reason']):12} {note}"
    ).rstrip()


def _cmd_document_tombstone_list(args: argparse.Namespace) -> int:
    def work(conn):
        rows = tombstones.list_tombstones(conn)
        if args.json:
            _print_json(rows)
            return 0
        if not rows:
            print("no tombstones recorded")
            return 0
        print(f"{'sha256':12}  {'removed':10}  {'doc':5} {'reason':12} note")
        for row in rows:
            print(_tombstone_row_line(row))
        return 0

    return _with_document_conn(args, work)


def _cmd_document_tombstone_add(args: argparse.Namespace) -> int:
    """Pre-emptive exclusion: record a hash without ingesting the content first.

    Without this verb the only way to exclude a never-ingested file would be to ingest
    it and then `rm --tombstone` — which copies the blob into `sources/` and inserts a
    row, i.e. puts precisely the content the operator wants kept out of the store *into*
    the store first. ``--file`` is the primary form and hashes the bytes here rather
    than asking anyone to transcribe 64 hex characters (a typo'd hash is a tombstone
    that silently matches nothing); it reads the file and does nothing else — no blob
    copy, no `document` row, no owner check. ``--sha256`` is the forensic escape hatch
    for when the file itself is gone.
    """
    if args.file is not None:
        try:
            sha = ingest.hash_file(args.file)
        except OSError as exc:
            print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
            return 1
    else:
        try:
            sha = tombstones.normalize_sha256(args.sha256)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    def work(conn):
        row = tombstones.add_tombstone(
            conn, sha, reason=args.reason, note=args.note
        )
        if args.json:
            _print_json(row)
            return 0
        print(
            f"tombstoned {row['sha256'][:12]} - {tombstones.describe(row)}. "
            f"`pemr ingest` will refuse this content; lift it with "
            f"`pemr document tombstone rm {row['sha256']}`"
        )
        return 0

    return _with_document_conn(args, work)


def _cmd_document_tombstone_rm(args: argparse.Namespace) -> int:
    try:
        sha = tombstones.normalize_sha256(args.sha256)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    def work(conn):
        row = tombstones.remove_tombstone(conn, sha)
        print(f"lifted tombstone {row['sha256'][:12]} - {tombstones.describe(row)}")
        return 0

    return _with_document_conn(args, work)


# --------------------------------------------------------------------------- #
# row-level record repair (`record rm`) - issue #107
# --------------------------------------------------------------------------- #

def _cmd_record_rm(args: argparse.Namespace) -> int:
    dictionary = dedup.load_dictionary(_resolve_dictionary_path(args))

    def work(conn):
        report = records.remove_record(
            conn, args.table, args.row_id, dictionary, apply=args.apply
        )
        if args.json:
            _print_json({
                "record_type": report.record_type,
                "row_id": report.row_id,
                "person": report.person_slug,
                "document_id": report.document_id,
                "label": report.label,
                "fields": report.fields,
                "dedup_key": report.dedup_key,
                "dedup_base": report.dedup_base,
                "dedup_occurrence": report.dedup_occurrence,
                "family_size": report.family_size,
                "family_remaining": report.family_remaining,
                "conflicts_reanchored": report.conflicts_reanchored,
                # Appended, never inserted: the --json key set is a contract.
                "curation_retired": report.curation_retired,
                "applied": report.applied,
            })
            return 0
        print(
            f"{report.record_type} #{report.row_id}  {_fmt(report.person_slug)}  "
            f"{report.label}"
        )
        for name, value in report.fields.items():
            if value is not None:
                print(f"  {name}: {value}")
        owner = (
            f"#{report.document_id}" if report.document_id is not None else "none"
        )
        print(f"  document: {owner} (kept, with its other records)")
        remain = "remain" if report.applied else "would remain"
        print(
            f"  identity: occurrence {report.dedup_occurrence} of "
            f"{report.family_size} in its family ({report.family_remaining} "
            f"{remain})"
        )
        if report.conflicts_reanchored:
            ids = ", ".join(f"#{cid}" for cid in report.conflicts_reanchored)
            print(
                f"  open conflict(s) {ids} re-anchor to the lowest surviving "
                "occurrence"
            )
        for verdict in report.curation_retired:
            # Named, not counted: a row-scoped verdict is a recorded human ruling, and
            # the dry run is the only chance to notice it is going away (issue #114).
            lifted = "lifted" if report.applied else "would be lifted"
            print(
                f"  curation verdict (row scope) {lifted}: "
                f"{curation.describe(verdict)}"
            )
        if report.applied:
            print(f"removed {report.record_type} #{report.row_id}")
        else:
            print(
                "dry run: nothing was deleted - re-run with --apply "
                "(back up first: `pemr backup`)"
            )
        return 0

    return _with_document_conn(args, work)


# --------------------------------------------------------------------------- #
# recorded human verdicts (`record annotate`) - issue #109
# --------------------------------------------------------------------------- #

def _curation_scope(row: dict) -> str:
    """`family` or `row #<id>` — the scope column shared by `--list` and the report."""
    record_id = row.get("record_id") or 0
    return f"row #{record_id}" if record_id else curation.SCOPE_FAMILY


def _curation_row_line(row: dict) -> str:
    """One `--list` line: identity, scope, verdict, and whether the target still exists.

    The scope column is not cosmetic (issue #114): a family verdict and a row verdict on
    the same family are two different rulings, and `--clear` needs `--row` for exactly
    one of them.
    """
    if not row["family_size"]:
        orphan = (
            "  (orphaned: no live row)" if row.get("record_id")
            else "  (orphaned: no live family)"
        )
    else:
        orphan = ""
    return (
        f"{row['record_type']:12}  {str(row['dedup_base'])[:12]}  "
        f"{_curation_scope(row):10}  {(row['created_at'] or '')[:10]:10}  "
        f"{row['label']}  [{curation.describe(row)}]{orphan}"
    )


def _print_curation_report(report: "curation.CurationReport") -> None:
    """The human block shared by annotate and clear, shaped like `_cmd_record_rm`'s."""
    if report.family_size:
        family = f"{report.family_size} row(s)"
    elif report.record_id:
        family = "no live row (orphaned verdict)"
    else:
        family = "no live rows (orphaned verdict)"
    gone = "(no live row)" if report.record_id else "(no live family)"
    print(
        f"{report.record_type}  {report.label or gone}  "
        f"base {report.dedup_base[:12]}  ({family})"
    )
    print(f"  scope: {_curation_scope(report.as_dict())}")
    print(f"  status: {report.status}")
    print(f"  note: {report.note}")
    if report.attributed_to:
        print(f"  attributed to: {report.attributed_to}")
    if report.merged_into_base:
        print(f"  merged into: {report.merged_into_base[:12]}")
    if report.previous is not None and report.action != "clear":
        print(f"  replaces: {curation.describe(report.previous)}")


def _cmd_record_annotate(args: argparse.Namespace) -> int:
    """`record annotate` — list, clear, or record one verdict (family- or row-scoped).

    Three modes on one subparser rather than three verbs: the positionals differ only
    in whether they are present, and `--list`/`--clear` read as flags on the noun the
    operator already has in hand. The handler owns the "table and target are required
    unless --list" rule, because argparse cannot express it across `nargs='?'`.

    `--row` (issue #114) narrows both the write and the clear to one row; without it
    every path behaves exactly as it did under #109, which is why row scope is opt-in
    rather than inferred from a digit-shaped target.
    """
    parser = args.annotate_parser
    if args.list:
        if args.target is not None:
            parser.error("--list takes an optional table, not a target")
        if args.row:
            parser.error("--row scopes a verdict to one row; it has no meaning with --list")

        def work(conn):
            rows = curation.list_curation(conn, args.table)
            if args.json:
                _print_json(rows)
                return 0
            if not rows:
                print("no curation verdicts recorded")
                return 0
            print(
                f"{'type':12}  {'base':12}  {'scope':10}  {'recorded':10}  "
                "label  [verdict]"
            )
            for row in rows:
                print(_curation_row_line(row))
            return 0

        return _with_document_conn(args, work)

    if args.table is None or args.target is None:
        parser.error("table and target are required unless --list is given")

    if args.clear:
        def work(conn):
            report = curation.clear_curation(
                conn, args.table, args.target, row=args.row, apply=args.apply
            )
            if args.json:
                _print_json(report.as_dict())
                return 0
            _print_curation_report(report)
            if report.applied:
                target = (
                    f"row #{report.record_id}" if report.record_id
                    else report.dedup_base[:12]
                )
                print(
                    f"cleared curation verdict for {report.record_type} {target}"
                )
            else:
                print("dry run: nothing was written - re-run with --apply")
            return 0

        return _with_document_conn(args, work)

    if args.status is None or args.note is None:
        parser.error("--status and --note are required to record a verdict")

    def work(conn):
        report = curation.annotate_record(
            conn,
            args.table,
            args.target,
            status=args.status,
            note=args.note,
            attributed_to=args.attributed_to,
            merged_into_base=args.merged_into,
            row=args.row,
            apply=args.apply,
        )
        if args.json:
            _print_json(report.as_dict())
            return 0
        _print_curation_report(report)
        if report.applied:
            target = (
                f"row #{report.record_id}" if report.record_id
                else report.dedup_base[:12]
            )
            print(
                f"annotated {report.record_type} {target} ({report.action})"
            )
        else:
            print("dry run: nothing was written - re-run with --apply")
        return 0

    return _with_document_conn(args, work)


# --------------------------------------------------------------------------- #
# bulk re-affirm of orphaned verdicts (`record reaffirm`) - issue #126
# --------------------------------------------------------------------------- #

@dataclass
class _ReaffirmAction:
    """What `record reaffirm` plans to do about one orphaned verdict.

    Also the ``--json`` payload shape (``as_dict`` widens
    :meth:`curation.OrphanVerdict.as_dict`), so these key names are a contract.

    ``action`` is one of:

    * ``"list"`` — no disposition was supplied; this is the pure listing mode.
    * ``"reaffirm"`` — re-record the same ruling against ``to_base`` (a family
      ``dedup_base``, or the row id at row scope).
    * ``"clear"`` — lift the verdict (``--clear``).
    * ``"skip"`` — refused, with ``reason``: nothing in the map names it, no successor
      family for a kind that needs one, or the successor already carries its own ruling.
    * ``"already-handled"`` — a map entry that matches no live orphan, so an earlier run
      already dealt with it. Not an error; re-running the same file is a clean no-op.
    """

    orphan: "curation.OrphanVerdict"
    action: str
    to_base: str | None = None
    to_merge_base: str | None = None
    target_label: str = ""
    target_family_size: int = 0
    reason: str = ""
    applied: bool = False

    def as_dict(self) -> dict:
        return {
            **self.orphan.as_dict(),
            "action": self.action,
            "to_base": self.to_base,
            "to_merge_base": self.to_merge_base,
            "target_label": self.target_label,
            "target_family_size": self.target_family_size,
            "reason": self.reason,
            "applied": self.applied,
        }


#: The `record reaffirm` actions that refuse an orphan, and therefore set rc=1: the
#: operator asked for a batch and did not get all of it. ``already-handled`` is
#: deliberately absent - re-running the same map file is idempotent, not a failure.
_REAFFIRM_REFUSALS = ("skip",)


def _load_reaffirm_map(path: str) -> list[dict]:
    """Read a ``--map-file`` into the list of orphan entries it carries.

    Accepts either the object `pemr rekey --apply --json` emits (its ``orphans`` array is
    taken) or a bare array of the same entries, so the composed pipeline is one
    redirection: ``pemr rekey --apply --json > f`` then
    ``pemr record reaffirm --map-file f``. Unknown keys are ignored — the rekey payload
    carries much more than this command needs, and a consumer must not have to trim it.
    """
    with open(path, "rb") as fh:
        payload = json.load(fh)
    if isinstance(payload, dict):
        payload = payload.get("orphans", [])
    if not isinstance(payload, list):
        raise ValueError(
            f"{path} is not a rekey report: expected an object with an 'orphans' array, "
            "or a bare array of orphan entries"
        )
    return [entry for entry in payload if isinstance(entry, dict)]


def _reaffirm_map_key(record_type: object, record_id: object, base: object) -> tuple:
    """The identity a map entry and a live orphan are matched on.

    Row id when there is one, ``dedup_base`` otherwise — the same split every other
    curation path uses, because a row-scoped verdict resolves by id alone and its stored
    base may be a stale breadcrumb.
    """
    rid = int(record_id or 0)
    return (str(record_type), rid) if rid else (str(record_type), str(base))


def _orphan_from_entry(entry: dict) -> "curation.OrphanVerdict":
    """A map-file entry read back as an :class:`curation.OrphanVerdict`.

    Only for reporting an ``already-handled`` entry: the live table is always the authority
    on what is orphaned, so a stored entry is never trusted as one.
    """
    return curation.OrphanVerdict(
        record_type=str(entry.get("record_type") or ""),
        dedup_base=str(entry.get("dedup_base") or ""),
        record_id=int(entry.get("record_id") or 0),
        scope=str(entry.get("scope") or ""),
        status=str(entry.get("status") or ""),
        note=str(entry.get("note") or ""),
        attributed_to=entry.get("attributed_to"),
        merged_into_base=entry.get("merged_into_base"),
        created_at=str(entry.get("created_at") or ""),
        kinds=[str(k) for k in (entry.get("kinds") or [])],
    )


def _reaffirm_target(
    conn, orphan: "curation.OrphanVerdict", to_base: str
) -> tuple[str, int]:
    """``(label, family_size)`` for where a re-affirm would land — what a reviewer reads.

    Load-bearing, not decoration (issue #126): the successor family is often *larger* than
    the one the human ruled on, and seeing its size is how a reviewer catches a ruling that
    would widen over rows nobody judged before approving the batch.
    """
    if orphan.record_id:
        label, _live_base, size = curation.row_label(
            conn, orphan.record_type, orphan.record_id
        )
        return label, size
    return curation.family_label(conn, orphan.record_type, to_base)


def _plan_reaffirm(
    conn,
    orphans: list["curation.OrphanVerdict"],
    mapping: list[dict] | None,
    clear: bool,
) -> list[_ReaffirmAction]:
    """Decide what to do about every live orphan — the whole batch, before any write.

    ``mapping=None`` and ``clear=False`` is the pure listing mode: no disposition was
    supplied, so nothing is planned and nothing is refused.

    Nothing is ever *silently* re-pointed here (the #109/#114/#116 design this issue
    preserves): a re-point needs an explicit map entry naming the successor family, the
    successor's label and size are reported for review, and a successor that already
    carries its own family-scoped verdict is **skipped** — a second human's ruling is never
    overwritten.
    """
    listing = mapping is None and not clear
    by_key: dict[tuple, dict] = {}
    for entry in mapping or []:
        by_key[_reaffirm_map_key(
            entry.get("record_type"), entry.get("record_id"), entry.get("dedup_base")
        )] = entry

    plan: list[_ReaffirmAction] = []
    matched: set[tuple] = set()
    claimed: set[tuple[str, str]] = set()
    for orphan in orphans:
        key = _reaffirm_map_key(orphan.record_type, orphan.record_id, orphan.dedup_base)
        if listing:
            plan.append(_ReaffirmAction(orphan=orphan, action="list"))
            continue
        if clear:
            plan.append(_ReaffirmAction(orphan=orphan, action="clear"))
            continue
        entry = by_key.get(key)
        if entry is None:
            plan.append(_ReaffirmAction(
                orphan=orphan, action="skip",
                reason="not named by the map file - re-run `pemr rekey --apply --json` "
                       "for the run that orphaned it, or lift it with --clear",
            ))
            continue
        matched.add(key)
        action = _plan_one_reaffirm(conn, orphan, entry)
        if action.action == "reaffirm" and not orphan.record_id:
            # One family verdict per family, and `annotate_record` is an upsert: two
            # orphans re-pointed onto the same successor would leave the second silently
            # overwriting the first. The whole batch is planned before any write, so the
            # per-row `get_verdict` check above cannot see an earlier row's claim - this
            # does.
            claim = (orphan.record_type, str(action.to_base))
            if claim in claimed:
                action = _ReaffirmAction(
                    orphan=orphan, action="skip",
                    reason="another verdict in this batch is already being re-pointed "
                           "onto that family - reconcile the two by hand",
                )
            else:
                claimed.add(claim)
        plan.append(action)

    # A map entry with no live orphan behind it: an earlier run already handled it (or a
    # human did, by hand). Reported, never an error - re-running the same file must be a
    # clean no-op, which is what makes the composed pipeline safe to retry.
    for key, entry in by_key.items():
        if key in matched:
            continue
        plan.append(_ReaffirmAction(
            orphan=_orphan_from_entry(entry), action="already-handled",
            reason="no longer orphaned",
        ))
    return plan


def _plan_one_reaffirm(
    conn, orphan: "curation.OrphanVerdict", entry: dict
) -> _ReaffirmAction:
    """Plan the re-affirm of one mapped orphan, or refuse it with a reason."""
    successor = entry.get("successor_base") or None
    successor_merge = entry.get("successor_merge_base") or None

    def skip(reason: str) -> _ReaffirmAction:
        return _ReaffirmAction(orphan=orphan, action="skip", reason=reason)

    # Where the ruling itself goes. Only the family-scoped `no-live-family` class moves;
    # a row-scoped verdict is re-affirmed against its own row (rekey never renumbers ids),
    # and a family whose only fault is a dangling merge target is still live.
    if curation.ORPHAN_NO_FAMILY in orphan.kinds:
        if not successor:
            return skip(
                "the map names no successor family for this base - it was removed rather "
                "than moved; lift the verdict with --clear"
            )
        to_base = str(successor)
        if not dedup.load_family(conn, orphan.record_type, to_base):
            return skip(
                f"successor family {to_base[:12]}... is not live either - the map is "
                "stale; re-run `pemr rekey --apply --json`"
            )
        if curation.get_verdict(conn, orphan.record_type, to_base) is not None:
            return skip(
                f"successor family {to_base[:12]}... already carries its own verdict - "
                "refusing to overwrite a second ruling; reconcile the two by hand"
            )
    elif orphan.record_id:
        # A row-scoped verdict is re-affirmed against its own row - `rekey` never renumbers
        # a row id. Only reachable here for the dangling-merge class, and only while the row
        # is still live: a removed row's remedy is `--clear --row`, and re-pointing it would
        # just fail inside `resolve_row`.
        to_base = str(orphan.record_id)
        if not curation.row_label(conn, orphan.record_type, orphan.record_id)[2]:
            return skip(
                "the annotated row is gone - lift the verdict with --clear instead"
            )
    else:
        to_base = orphan.dedup_base

    # And where its merge pointer goes. `merged_into_base` is only legal on a
    # `merged-into` verdict (:func:`curation.annotate_record` enforces that on the way in),
    # so a dangling target on any other status can only be a hand-edited row - refused
    # rather than reshaped.
    to_merge_base: str | None = None
    if orphan.merged_into_base:
        if orphan.status != "merged-into":
            return skip(
                f"status '{orphan.status}' carries a merge target (hand-edited?) - "
                "`--merged-into` is only meaningful with 'merged-into'; lift it with "
                "--clear"
            )
        if curation.ORPHAN_DANGLING_MERGE in orphan.kinds:
            if not successor_merge:
                return skip(
                    "the map names no successor for the merge target - it was removed "
                    "rather than moved; re-rule the verdict with `pemr record annotate`"
                )
            to_merge_base = str(successor_merge)
        else:
            to_merge_base = orphan.merged_into_base
        if not dedup.load_family(conn, orphan.record_type, to_merge_base):
            return skip(
                f"merge target {str(to_merge_base)[:12]}... is not live - the map is "
                "stale; re-run `pemr rekey --apply --json`"
            )
        if to_merge_base == to_base and not orphan.record_id:
            # This rekey fused the ruled family into its own merge target. A family merged
            # into itself renders nowhere at all (`annotate_record` refuses it at family
            # scope), so this needs a human, not a re-point.
            return skip(
                "this rekey merged the family into its own merge target - a family "
                "merged into itself would render nowhere; re-rule it with "
                "`pemr record annotate`"
            )

    label, size = _reaffirm_target(conn, orphan, to_base)
    return _ReaffirmAction(
        orphan=orphan, action="reaffirm", to_base=to_base,
        to_merge_base=to_merge_base, target_label=label, target_family_size=size,
    )


def _apply_reaffirm(conn, plan: list[_ReaffirmAction]) -> str:
    """Write the planned batch, per row, through the existing curation write paths.

    Returns ``""`` on a clean batch, or the failure that stopped it. The plan is fully
    validated before this is called, so a failure here is unexpected — and the batch stops
    at it rather than pressing on, with :attr:`_ReaffirmAction.applied` naming exactly what
    landed.

    **Annotate before clear, never the reverse.** :func:`curation.annotate_record` and
    :func:`curation.clear_curation` each own their own transaction (the requirements ask
    for per-row reuse of those paths, not a new batch state machine), so a failure between
    the two is possible: this order leaves the ruling *duplicated* on the old and new base,
    which the next run reconciles, instead of destroying a human's verdict.
    """
    for action in plan:
        orphan = action.orphan
        row = orphan.scope == curation.SCOPE_ROW
        token = str(orphan.record_id) if row else orphan.dedup_base
        try:
            if action.action == "clear":
                curation.clear_curation(
                    conn, orphan.record_type, token, row=row, apply=True
                )
            elif action.action == "reaffirm":
                curation.annotate_record(
                    conn, orphan.record_type, str(action.to_base),
                    status=orphan.status, note=orphan.note,
                    attributed_to=orphan.attributed_to,
                    merged_into_base=action.to_merge_base,
                    row=row, apply=True,
                )
                if not row and action.to_base != orphan.dedup_base:
                    curation.clear_curation(
                        conn, orphan.record_type, orphan.dedup_base, apply=True
                    )
            else:
                continue
        except (ValueError, curation.CurationNotFoundError) as exc:
            action.reason = f"write failed: {exc}"
            return str(exc)
        action.applied = True
    return ""


def _reaffirm_line(action: _ReaffirmAction) -> str:
    """One listing line: which verdict, why it is orphaned, and what happens to it."""
    orphan = action.orphan
    ident = (
        f"row #{orphan.record_id}" if orphan.record_id
        else str(orphan.dedup_base)[:12]
    )
    if action.action == "reaffirm":
        target = f" -> {str(action.to_base)[:12]}"
        if action.target_label:
            target += f" ({action.target_label}, {action.target_family_size} row(s))"
    elif action.reason:
        target = f" ({action.reason})"
    else:
        target = ""
    return (
        f"{orphan.record_type:12}  {ident:12}  {orphan.scope:6}  "
        f"{action.action:15}  {','.join(orphan.kinds):22}  "
        f"[{curation.describe(orphan.as_dict())}]{target}"
    )


def _cmd_record_reaffirm(args: argparse.Namespace) -> int:
    """`record reaffirm` — list the orphaned verdicts, then re-point or lift them in bulk.

    The bulk remedy for the state `pemr rekey` deliberately leaves behind (issue #126):
    `rekey` never re-points a verdict by itself, and the per-row `record annotate` remedy
    does not scale to the 57-row orphan batch one dictionary edit produced. This is a
    deterministic control, not an interactive tool: dry run by default (the
    `annotate`/`clear` convention), `--json` so an agent can read and summarize it, and
    exactly one explicit `--apply` as the human-approval gate. Nothing prompts.

    A separate subcommand rather than a fourth `record annotate` mode: that handler already
    hand-rolls cross-flag validation for three modes on one subparser, and a bulk verb with
    its own disposition flags belongs on its own usage line.
    """
    mapping: list[dict] | None = None
    if args.map_file:
        try:
            mapping = _load_reaffirm_map(args.map_file)
        except OSError as exc:
            print(f"error: cannot read {args.map_file}: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:      # includes json.JSONDecodeError
            print(f"error: {exc}", file=sys.stderr)
            return 1

    def work(conn):
        plan = _plan_reaffirm(
            conn, curation.list_orphans(conn, args.table), mapping, args.clear
        )
        failure = ""
        if args.apply and any(a.action in ("reaffirm", "clear") for a in plan):
            failure = _apply_reaffirm(conn, plan)
        refused = [a for a in plan if a.action in _REAFFIRM_REFUSALS]
        rc = 1 if (refused or failure) else 0

        if args.json:
            _print_json([a.as_dict() for a in plan])
            return rc

        if not plan:
            print("no orphaned curation verdicts")
            return rc
        print(
            f"{'type':12}  {'target':12}  {'scope':6}  {'action':15}  "
            f"{'orphaned by':22}  [verdict]"
        )
        for action in plan:
            print(_reaffirm_line(action))

        written = sum(1 for a in plan if a.applied)
        if failure:
            print(
                f"error: the batch stopped after {written} write(s): {failure}",
                file=sys.stderr,
            )
        elif args.apply:
            print(f"re-annotated {written} verdict(s)")
        elif mapping is None and not args.clear:
            print(
                "listing only: pass --map-file <file> (from `pemr rekey --apply --json`) "
                "to re-point these,\n  or --clear to lift them - then --apply"
            )
        else:
            print("dry run: nothing was written - re-run with --apply")
        if refused:
            print(
                f"warning: {len(refused)} verdict(s) were not handled - see the reasons "
                "above",
                file=sys.stderr,
            )
        return rc

    return _with_document_conn(args, work)


# --------------------------------------------------------------------------- #
# human-attested records (`record assert`) - issue #110
# --------------------------------------------------------------------------- #

def _parse_field_args(
    parser: argparse.ArgumentParser, table: str, raw: list[str] | None
) -> dict:
    """``--field NAME=VALUE`` repeats -> a payload dict for :mod:`dedup`.

    Splits on the **first** ``=`` so a value may contain more (``--field
    value_text=ratio=1.2``). Unknown and duplicated names are argparse misuse (rc=2 with
    the usage line) rather than engine errors: they are typos in the command, and the
    operator needs the field list right there. Numeric-spec fields are coerced, because
    argparse hands everything over as text and ``value_num`` must not land as a string; a
    value that will not coerce is passed through untouched so
    :func:`dedup.validate_row` produces the type message instead of a worse one here.
    """
    spec = dedup.FIELD_SPECS[table]
    payload: dict = {}
    for item in raw or []:
        name, sep, value = item.partition("=")
        name = name.strip()
        if not sep or not name:
            parser.error(f"--field expects NAME=VALUE, got {item!r}")
        if name not in spec:
            parser.error(
                f"unknown {table} field '{name}' - known fields: "
                f"{', '.join(spec)}"
            )
        if name in payload:
            parser.error(f"--field {name} given twice")
        types = spec[name][0]
        if isinstance(types, tuple) and int in types:
            try:
                payload[name] = int(value)
            except ValueError:
                try:
                    payload[name] = float(value)
                except ValueError:
                    payload[name] = value
        else:
            payload[name] = value
    return payload


def _attested_row_line(row: dict) -> str:
    """One `--list` line: what was attested, by whom, and whether a source arrived."""
    state = "needs source" if row["needs_source"] else f"document #{row['document_id']}"
    return (
        f"{row['record_type']:12}  #{row['row_id']:<6}  {_fmt(row['person']):12}  "
        f"{(row['attested_on'] or '')[:10]:10}  {row['label']}  "
        f"[{row['attested_by']}]  ({state})"
    )


def _print_attest_report(report: "attestations.AttestReport") -> None:
    """The human block, shaped like `_cmd_record_rm`'s: the fact, then its provenance."""
    label = report.label or report.record_type
    print(f"{report.record_type}  {_fmt(report.person_slug)}  {label}")
    for name, value in report.fields.items():
        print(f"  {name}: {value}")
    print(
        f"  attested by {report.attributed_to} on {report.attested_on} "
        "(no source document)"
    )
    if report.outcome == "duplicate":
        print(
            f"  already recorded: {report.record_type} #{report.row_id} "
            f"({report.existing_provenance}) holds this exact fact"
        )


def _cmd_record_assert(args: argparse.Namespace) -> int:
    """`record assert` — list attested rows, or commit one attested fact.

    Two modes on one subparser, the `_cmd_record_annotate` shape: `--list` takes an
    optional table and no payload; otherwise the table and at least one `--field` are
    required. The handler owns the rules argparse cannot express across ``nargs='?'``.
    """
    parser = args.assert_parser
    if args.list:
        def work(conn):
            rows = attestations.list_attested(
                conn, args.table, include_superseded=args.all
            )
            if args.json:
                _print_json(rows)
                return 0
            if not rows:
                print("no attested records")
                return 0
            print(
                f"{'type':12}  {'row':7}  {'person':12}  {'attested':10}  label  "
                "[who]  (source)"
            )
            for row in rows:
                print(_attested_row_line(row))
            return 0

        return _with_document_conn(args, work)

    if args.table is None:
        parser.error("a table is required unless --list is given")
    if not args.field:
        parser.error(
            "at least one --field NAME=VALUE is required - the fact being attested"
        )
    if not args.person:
        parser.error("--person is required")
    if not args.attributed_to:
        parser.error("--attributed-to is required: who is attesting this fact")
    if not args.date:
        parser.error("--date is required: when the attestation was made")

    payload = _parse_field_args(parser, args.table, args.field)
    dictionary = dedup.load_dictionary(_resolve_dictionary_path(args))

    def work(conn):
        report = attestations.assert_record(
            conn,
            args.table,
            args.person,
            payload,
            attributed_to=args.attributed_to,
            attested_on=args.date,
            dictionary=dictionary,
            apply=args.apply,
        )
        if args.json:
            _print_json(report.as_dict())
            return 0
        _print_attest_report(report)
        if report.outcome == "duplicate":
            print("nothing to do: the fact is already on record")
        elif report.applied:
            print(f"wrote {report.record_type} #{report.row_id}")
        else:
            print("dry run: nothing was written - re-run with --apply")
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
                    # `--ocr auto` means "extract by whatever route this file type
                    # allows" (ingest.extract_text).
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

    if result.is_tombstoned:
        # rc=0 and no `next:` line: an expected, benign skip with the same shape as
        # `duplicate` (issue #80). Deliberately worded so a sweep log tells the two
        # apart at a glance - that reporting gap is what the issue is about.
        ts = result.tombstone or {}
        print(
            f"skipped: tombstoned {tombstones.describe(ts)} - nothing ingested"
        )
        if ts.get("note"):
            print(f"  note: {ts['note']}")
        print(
            f"  sha256 {ts.get('sha256', '')[:12]}...  Lift with "
            f"`pemr document tombstone rm {ts.get('sha256', '')}`,\n"
            "  or ingest anyway with --force."
        )
        return 0

    doc = result.document
    if result.is_duplicate:
        print(
            f"duplicate: already filed as document #{doc.document_id} "
            f"(sha256 {doc.sha256[:12]}...) - nothing ingested"
        )
        return 0
    if result.tombstone is not None:
        print(
            "warning: this content is tombstoned "
            f"({tombstones.describe(result.tombstone)}) and was ingested anyway with "
            "--force.\n  The tombstone was NOT lifted, so the next sweep will skip "
            f"this file again. Undo with\n  `pemr document rm {result.document.document_id}"
            " --apply`.",
            file=sys.stderr,
        )
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
        except (dedup.ValidationError, dedup.DictionaryDriftError) as exc:
            # Drift is a refusal, not a crash: the message names the rekey to run.
            print(f"error: {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()

    c = summary.counts
    print(
        f"committed: {c['new']} new, {c['duplicate']} duplicate, "
        f"{c['enriched']} enriched, {c['conflict']} conflict"
    )
    # Only when it happened, so an unattested commit's output is byte-identical to what
    # it was before issue #110 (the additive-only rule).
    if summary.promoted:
        print(
            f"note: {c['promoted']} attested record(s) now backed by this document - "
            "the attestation is kept as history"
        )
    if summary.conflict:
        print(
            f"note: {c['conflict']} conflict(s) staged - resolve with "
            "`pemr review-conflicts`"
        )
    return 0


def _rekey_orphans(
    conn, report: "dedup.RekeyReport"
) -> list[tuple["curation.OrphanVerdict", str | None, str | None]]:
    """The curation verdicts *this* rekey run orphaned, each with its successor family.

    Issue #126. `rekey` deliberately never re-points a verdict by itself (the #109/#114/#116
    design), so a run that moves a family's `dedup_base` leaves that family's verdict inert.
    Until now the operator only learned which ones on the *next* `pemr verify`; this is the
    same detection (:func:`curation.list_orphans`) read at apply time and **scoped to what
    this run moved**, so the report is this run's fallout rather than every orphan in the
    database.

    Assembled here rather than in `dedup` on purpose: `dedup` must never import `curation`
    (the :class:`dedup.CollisionResolver` injected-seam rule), and this module imports both.

    Returns ``(orphan, successor_base, successor_merge_base)`` per entry — the successors
    being where the moved family and the moved merge target went, or ``None`` when this run
    did not move that one. A table in :attr:`dedup.RekeyReport.blocked` is excluded by
    construction: its changes were withheld, so it orphaned nothing.
    """
    blocked = set(report.blocked)
    moved: dict[tuple[str, str], str] = {
        (record_type, old): new
        for record_type, base_map in report.base_maps.items()
        if record_type not in blocked
        for old, new in base_map.items()
        if old != new
    }
    if not moved:
        return []
    out: list[tuple["curation.OrphanVerdict", str | None, str | None]] = []
    for orphan in curation.list_orphans(conn):
        successor = moved.get((orphan.record_type, orphan.dedup_base))
        successor_merge = (
            moved.get((orphan.record_type, orphan.merged_into_base))
            if orphan.merged_into_base else None
        )
        if successor is None and successor_merge is None:
            # A pre-existing orphan from an earlier run or an unrelated `record rm`:
            # real, but not this run's doing, and `pemr verify` already names it.
            continue
        out.append((orphan, successor, successor_merge))
    return out


def _print_rekey_orphans(
    orphans: list[tuple["curation.OrphanVerdict", str | None, str | None]]
) -> None:
    """The apply-time orphan block: what this rekey knocked loose, and how to fix it.

    A `warning:`, not an `error:` — orphaning a verdict is the documented consequence of
    the warn-and-reannotate design, not a failure, so `rekey`'s exit code is unchanged
    (issue #126).
    """
    if not orphans:
        return
    print(
        f"warning: {len(orphans)} curation verdict(s) were orphaned by this rekey",
        file=sys.stderr,
    )
    for orphan, successor, successor_merge in orphans:
        ident = (
            f"row #{orphan.record_id}" if orphan.record_id
            else f"{str(orphan.dedup_base)[:12]}..."
        )
        # The two successors are reported apart: one is where the family the verdict rules
        # on went, the other where its merge target went, and a verdict can be flagged for
        # either or both.
        arrow = f" -> {str(successor)[:12]}..." if successor else ""
        if successor_merge:
            arrow += f" (merges into {str(successor_merge)[:12]}...)"
        print(
            f"  {orphan.record_type} {ident}  {','.join(orphan.kinds)}{arrow}  "
            f"[{curation.describe(orphan.as_dict())}]",
            file=sys.stderr,
        )
    print(
        "  re-point them in one reviewed batch with\n"
        "  `pemr record reaffirm --map-file <file>` (dry run), then --apply - where <file>\n"
        "  is this run's own report: `pemr rekey --apply --json > <file>`. A dedup_base is\n"
        "  overwritten in place, so the old->new mapping exists nowhere else.\n"
        "  `pemr record reaffirm` alone lists them; `--clear` lifts them instead.",
        file=sys.stderr,
    )


def _cmd_rekey(args: argparse.Namespace) -> int:
    conn = _connect_db(args)
    try:
        dictionary = dedup.load_dictionary(_resolve_dictionary_path(args))
        try:
            # The curation overlay is injected, never imported by `dedup` (issue #116):
            # a collision the human already ruled on with a merged-into/superseded
            # verdict is settled, not blocking.
            report = dedup.rekey(
                conn, dictionary, apply=args.apply,
                resolver=curation.collision_resolver(conn),
            )
        except db.NotMigratedError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        # Read *after* the write, from live state: the orphan set is what the next `pemr
        # verify` would say about this run, and a dry run wrote nothing to have fallout
        # from (predicting it would mean simulating the post-write database - out of
        # scope, issue #126).
        orphans = _rekey_orphans(conn, report) if args.apply else []
    finally:
        conn.close()

    # A collision blocks its own table only (issue #92), so it is a partial failure:
    # the clean tables are reported (and, under --apply, written) and the exit code
    # still says something was left undone. Same rc in --json mode as in text mode.
    # `collisions` is now the *blocking* ones only, so a run whose every collision was
    # verdict-resolved exits 0 - there is nothing left for the operator to fix.
    writable = report.writable()
    rc = 1 if report.collisions else 0

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
            # `changed` is every key that moves; `skipped` names the tables whose
            # changes were withheld, so a consumer can tell written from withheld.
            "skipped": report.blocked,
            "collisions": [
                {
                    "record_type": c.record_type,
                    "row_id": c.row_id,
                    "label": c.label,
                    "clash_row_id": c.clash_row_id,
                    "clash_label": c.clash_label,
                    "kind": c.kind,
                    "message": c.message,
                    # Appended (issue #124): the two rulings that disagree, when *that*
                    # is why the pair blocks. Empty on every ordinary collision, so a
                    # consumer can branch on it without a second lookup - and a
                    # contradiction is never reported as a clean one-sided resolution.
                    "contradiction": [
                        {
                            "row_id": v.row_id,
                            "status": v.status,
                            "scope": v.scope,
                            "verdict_base": v.verdict_base,
                            "settlement": v.settlement,
                        }
                        for v in c.contradiction
                    ],
                }
                for c in report.collisions
            ],
            # Appended, never inserted (the additive-only contract rule): the collisions
            # a recorded verdict settled, which `collisions` and `skipped` deliberately
            # no longer carry.
            "resolved": [
                {
                    "record_type": r.record_type,
                    "row_id": r.row_id,
                    "label": r.label,
                    "clash_row_id": r.clash_row_id,
                    "clash_label": r.clash_label,
                    "kind": r.kind,
                    "status": r.status,
                    "scope": r.scope,
                    "record_id": r.record_id,
                    "new_base": r.new_base,
                    "new_occurrence": r.new_occurrence,
                    "covered_row_ids": r.covered_row_ids,
                    "verdict_action": r.verdict_action,
                    "narrowed_row_ids": r.narrowed_row_ids,
                    "message": r.message,
                    # Appended (issue #122): which question the verdict answered -
                    # "merged" (the pair is one fact) or "distinct" (two facts that share
                    # a recomputed key). `status` carries the raw vocabulary; this is the
                    # two-valued contract a consumer can branch on.
                    "settlement": r.settlement,
                }
                for r in report.resolved
            ],
            # Appended (issue #126): the curation verdicts *this* run orphaned, so an
            # agent sees the fallout here instead of on the next `pemr verify` - and can
            # feed this very payload straight back as
            # `pemr record reaffirm --map-file <file>`. Always present, `[]` on a dry run
            # (which writes nothing, so it orphans nothing).
            "orphans": [
                {
                    **orphan.as_dict(),
                    "successor_base": successor,
                    "successor_merge_base": successor_merge,
                }
                for orphan, successor, successor_merge in orphans
            ],
        })
        return rc

    for record_type, count in report.scanned.items():
        changed = sum(1 for c in report.changes if c.record_type == record_type)
        blocked = " (skipped: collision)" if record_type in report.blocked else ""
        print(f"{record_type}: {changed}/{count} key(s) change{blocked}")
    for c in report.changes:
        print(f"  {c.record_type} #{c.row_id}  {c.label}")

    # A note, not an error: the operator has nothing to fix here, but they do need to
    # see why a table that holds a collision was written anyway - and, when a ruling's
    # scope was narrowed to keep it off a row it never judged, they need to be told
    # loudly enough to re-affirm or re-rule it.
    for resolution in report.resolved:
        print(f"note: {resolution.message}")
        if resolution.verdict_action == "narrowed":
            rows = ", ".join(str(r) for r in resolution.narrowed_row_ids)
            print(
                f"note: the family verdict on {resolution.verdict_base[:12]}... was "
                f"narrowed to row scope on {resolution.record_type} "
                f"{'rows' if len(resolution.narrowed_row_ids) > 1 else 'row'} {rows} - "
                "the rows it already covered - so it does not extend over the rest of "
                f"{resolution.new_base[:12]}...; re-affirm or re-rule it with "
                f"`pemr record annotate --row {resolution.record_type} <id>`"
            )
    if report.resolved:
        # Both counts, always: "resolved by verdict" alone hides the difference between
        # "a human folded these into one fact" and "a human ruled them two facts that
        # both keep rendering" (issue #122).
        merged = sum(1 for r in report.resolved if r.settlement != "distinct")
        distinct = len(report.resolved) - merged
        print(
            f"{len(report.resolved)} collision(s) resolved by verdict "
            f"({merged} as one fact, {distinct} as distinct facts)"
        )

    _print_rekey_orphans(orphans)

    for collision in report.collisions:
        print(f"error: {collision.message}", file=sys.stderr)
    if report.collisions:
        print(
            f"error: {len(report.collisions)} collision(s) left "
            f"{len(report.blocked)} table(s) on their stored keys: "
            f"{', '.join(report.blocked)} - fix the collisions above and re-run",
            file=sys.stderr,
        )

    if not report.changes and not report.collisions and not report.resolved:
        print("all dedup keys already match the current dictionary")
    elif report.applied:
        print(f"rekeyed {len(writable)} row(s)")
    elif writable:
        print(
            f"dry run: {len(writable)} row(s) would change - "
            "re-run with --apply (back up first: `pemr backup`)"
        )
    else:
        print("dry run: no row outside the skipped table(s) needs a new key")
    return rc


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
    """Strip a record row down to its ``--json`` contract (:func:`dedup.public_row`).

    Kept as a local alias because it reads at every call site; the rule itself lives in
    `dedup` so the MCP read payload cannot drift from this one (issue #110).
    """
    return dedup.public_row(row)


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


def _print_other_assays(result: dict) -> None:
    """Disclose same-analyte rows `trends` excluded as a different assay (issue #71).

    Without this the split is invisible: an SPEP albumin series would just be missing
    from a CMP albumin trend with no hint it exists. Each token is printed ready to
    paste back as ``--test``.

    Quoted with :func:`shlex.quote`, not an f-string's own ``"``: the token descends
    from ``test_name``, i.e. untrusted document text, and this is the one place in the
    read path that renders such text as a command the user is invited to run. A name
    carrying a ``"`` would otherwise close the quote and leave the remainder live."""
    others = result.get("other_assays") or []
    if not others:
        return
    count = result.get("other_assay_count", 0)
    tokens = "  ".join(f"--test {shlex.quote(t)}" for t in others)
    print(f"  note   {count} more row(s) of this analyte under another assay: {tokens}")


def _cmd_trends(args: argparse.Namespace) -> int:
    def work(conn):
        dictionary = dedup.load_dictionary(_resolve_dictionary_path(args))
        result = query.trends(conn, args.person, args.test, dictionary=dictionary)
        if args.json:
            _print_json(result)
            return 0
        if result["count"] == 0:
            print(f"no numeric results for '{result['test']}'")
            _print_other_assays(result)
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
        _print_other_assays(result)
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

def _report_lost_tombstones(result: "restore.RestoreResult") -> None:
    """Name the tombstones this restore drops (issue #80), and how to put them back.

    A snapshot older than a tombstone loses it along with everything else recorded
    since — correct, but the *consequence* is issue #80's bug resurrected: the next
    sweep silently re-ingests a document a human deliberately excluded. Reported on
    stderr because it is a caveat about a restore that otherwise succeeded, and capped
    like every other problem list (`verify.PROBLEM_DISPLAY_LIMIT`).
    """
    lost = result.lost_tombstones
    if not lost:
        return
    lines = [
        f"note: {len(lost)} tombstone(s) in the current database are not in this "
        "snapshot and will be lost:"
    ]
    for row in lost[:verify.PROBLEM_DISPLAY_LIMIT]:
        lines.append(f"  {row['sha256'][:12]}... {tombstones.describe(row)}")
    hidden = len(lost) - verify.PROBLEM_DISPLAY_LIMIT
    if hidden > 0:
        lines.append(f"  ... and {hidden} more (use --json for the full list)")
    if result.rescue is not None:
        lines.append(f"  They are preserved in the rescue copy {result.rescue.name}.")
    lines.append(
        "  Re-apply with `pemr document tombstone add --sha256 <hash> --reason <slug>`."
    )
    print("\n".join(lines), file=sys.stderr)


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
            "lost_tombstones": result.lost_tombstones,
            "report": report.as_dict() if report else None,
        })
        return 0

    if result.rescue:
        print(f"rescue copy of the previous database: {result.rescue}")
    _report_lost_tombstones(result)
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
    d_rm.add_argument(
        "--tombstone", action="store_true",
        help="record that this removal is permanent, so a later sweep refuses to "
             "re-ingest the file (off by default: most removals are corrections)",
    )
    d_rm.add_argument(
        "--reason",
        help="short free-text slug for the tombstone, e.g. identifiers, not-medical, "
             "wrong-household (needs --tombstone; not validated - no taxonomy)",
    )
    d_rm.add_argument(
        "--note",
        help="free text for the human, e.g. \"dad's 2019 insurance card\" - the only "
             "recognisable label a tombstone carries (needs --tombstone)",
    )
    d_rm.add_argument("--json", action="store_true", help="machine-readable output")
    # `parser` rides along so the flag-combination check in the handler can raise a
    # real argparse error against *this* subparser (usage line included).
    d_rm.set_defaults(func=_cmd_document_rm, parser=d_rm)

    # --- intentional-removal memory (issue #80) ---------------------------
    d_tombstone = document_sub.add_parser(
        "tombstone",
        help="inspect, record and lift intentional-removal memory (layer-1 dedup)",
    )
    tombstone_sub = d_tombstone.add_subparsers(
        dest="tombstone_command", required=True
    )

    t_list = tombstone_sub.add_parser(
        "list", help="recorded tombstones, newest first"
    )
    t_list.add_argument("--json", action="store_true", help="machine-readable output")
    t_list.set_defaults(func=_cmd_document_tombstone_list)

    t_add = tombstone_sub.add_parser(
        "add",
        help="tombstone content without ingesting it (pre-emptive exclusion)",
    )
    # Exactly one: --file hashes the bytes here (no transcription, no typo'd hash);
    # --sha256 is the forensic path for when the file itself is gone.
    t_add_what = t_add.add_mutually_exclusive_group(required=True)
    t_add_what.add_argument(
        "--file", help="hash this file and tombstone it (nothing is ingested or copied)"
    )
    t_add_what.add_argument(
        "--sha256", help="tombstone a bare content hash (64 hex characters)"
    )
    t_add.add_argument("--reason", help="short free-text slug, e.g. identifiers")
    t_add.add_argument("--note", help="free text for the human")
    t_add.add_argument("--json", action="store_true", help="machine-readable output")
    t_add.set_defaults(func=_cmd_document_tombstone_add)

    t_rm = tombstone_sub.add_parser("rm", help="lift a tombstone (full hash only)")
    t_rm.add_argument("sha256", metavar="SHA256")
    t_rm.set_defaults(func=_cmd_document_tombstone_rm)

    # --- row-level record repair (doubled-fact escape hatch, issue #107) ---
    p_record = sub.add_parser(
        "record", help="inspect and repair individual records"
    )
    record_sub = p_record.add_subparsers(dest="record_command", required=True)

    r_rm = record_sub.add_parser(
        "rm",
        help="delete one record row, leaving its document and every other record "
             "it produced intact (dry run by default)",
    )
    r_rm.add_argument("table", choices=list(dedup.KNOWN_TYPES))
    r_rm.add_argument("row_id", type=int, metavar="ID")
    r_rm.add_argument(
        "--apply", action="store_true", help="write the delete (default: report only)"
    )
    r_rm.add_argument(
        "--dictionary", help="synonym dictionary TOML (overrides default)"
    )
    r_rm.add_argument("--json", action="store_true", help="machine-readable output")
    r_rm.set_defaults(func=_cmd_record_rm)

    # --- recorded human verdicts (curation overlay, issue #109) ---
    r_annotate = record_sub.add_parser(
        "annotate",
        help="record a human verdict over a record family (dry run by default); "
             "source rows are never mutated",
    )
    # Both optional so `--list` can take an optional table and no target; the handler
    # enforces "table and target unless --list" with the usage line.
    r_annotate.add_argument(
        "table", nargs="?", choices=list(dedup.KNOWN_TYPES)
    )
    r_annotate.add_argument("target", nargs="?", metavar="BASE-OR-ID")
    r_annotate.add_argument(
        "--status", choices=list(curation.STATUSES), help="the verdict"
    )
    r_annotate.add_argument(
        "--note", help="required: why the verdict was made, and who said so"
    )
    r_annotate.add_argument("--attributed-to", help="who made the call")
    r_annotate.add_argument(
        "--merged-into", metavar="BASE-OR-ID",
        help="target family, required with --status merged-into",
    )
    r_annotate.add_argument(
        "--list", action="store_true", help="list current verdicts and exit"
    )
    r_annotate.add_argument(
        "--clear", action="store_true", help="lift the verdict on this target"
    )
    r_annotate.add_argument(
        "--row", action="store_true",
        help="scope the verdict to this ROW only, not its whole dedup family "
             "(target must be a row id); also selects a row verdict for --clear",
    )
    r_annotate.add_argument(
        "--apply", action="store_true", help="write the verdict (default: report only)"
    )
    r_annotate.add_argument("--json", action="store_true", help="machine-readable output")
    r_annotate.set_defaults(func=_cmd_record_annotate, annotate_parser=r_annotate)

    # --- bulk remedy for orphaned verdicts (issue #126) ---
    r_reaffirm = record_sub.add_parser(
        "reaffirm",
        help="re-point or lift the curation verdicts a rekey orphaned, in one reviewed "
             "batch (dry run by default)",
    )
    r_reaffirm.add_argument(
        "table", nargs="?", choices=list(dedup.KNOWN_TYPES),
        help="limit to one record type (default: every type)",
    )
    # A verdict is either re-pointed or lifted, never both: `--clear` ignores the map
    # entirely, so accepting the pair would silently discard one of them. With neither,
    # the command is a pure listing.
    r_reaffirm_how = r_reaffirm.add_mutually_exclusive_group()
    r_reaffirm_how.add_argument(
        "--map-file", dest="map_file", metavar="FILE",
        help="old->new base mapping from `pemr rekey --apply --json` (that payload, or a "
             "bare array of its 'orphans' entries); required to re-point anything, since "
             "the mapping cannot be re-derived once the rekey is over",
    )
    r_reaffirm_how.add_argument(
        "--clear", action="store_true",
        help="lift the listed verdicts instead of re-pointing them",
    )
    r_reaffirm.add_argument(
        "--apply", action="store_true",
        help="write the batch (default: report only)",
    )
    r_reaffirm.add_argument("--json", action="store_true", help="machine-readable output")
    r_reaffirm.set_defaults(func=_cmd_record_reaffirm, reaffirm_parser=r_reaffirm)

    # --- human-attested records (issue #110) ---
    r_assert = record_sub.add_parser(
        "assert",
        help="record a fact attested by a person, with no source document yet "
             "(dry run by default); CLI-only, never an agent write",
    )
    # Optional so `--list` can take an optional table and no payload; the handler
    # enforces "table and --field unless --list" with the usage line.
    r_assert.add_argument("table", nargs="?", choices=list(dedup.KNOWN_TYPES))
    r_assert.add_argument("--person", help="owner slug, e.g. jane-doe")
    r_assert.add_argument(
        "--attributed-to", help="required: who is attesting this fact"
    )
    r_assert.add_argument(
        "--date", help="required: when the attestation was made (ISO date)"
    )
    r_assert.add_argument(
        "--field", action="append", metavar="NAME=VALUE",
        help="one payload field; repeat for each (e.g. --field name=Metformin)",
    )
    r_assert.add_argument(
        "--list", action="store_true",
        help="list attested rows still needing a source document, and exit",
    )
    r_assert.add_argument(
        "--all", action="store_true",
        help="with --list: include attestations a document has since backed",
    )
    r_assert.add_argument(
        "--apply", action="store_true", help="write the row (default: report only)"
    )
    r_assert.add_argument(
        "--dictionary", help="synonym dictionary TOML (overrides default)"
    )
    r_assert.add_argument("--json", action="store_true", help="machine-readable output")
    r_assert.set_defaults(func=_cmd_record_assert, assert_parser=r_assert)

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
        choices=["auto"],
        help="pre-fill ocr_text: text/.docx/.xlsx read natively, PDF page by page "
             "(text layer + rendered-page OCR), other files via tesseract "
             "(soft dependency)",
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

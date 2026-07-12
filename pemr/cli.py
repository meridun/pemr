"""`pemr` command-line entry point (phase 1: migrate, person add|list|show).

DB path resolution order: --db flag > PEMR_DB env var > config.toml
[paths].data_dir + /pemr.db. Config path resolution: --config flag >
PEMR_CONFIG env var > ./config.toml.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from dataclasses import asdict
from pathlib import Path

from . import __version__, db, dedup, ingest, persons

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
        "error: no database path — pass --db, set PEMR_DB, or set "
        f"[paths].data_dir in {config_path} (see config.example.toml)"
    )


def _resolve_sources_dir(args: argparse.Namespace) -> Path:
    override = getattr(args, "sources", None)
    if override:
        return Path(override)
    env = os.environ.get("PEMR_SOURCES")
    if env:
        return Path(env)
    sources_dir = _load_config(args).get("paths", {}).get("sources_dir")
    if sources_dir:
        return Path(sources_dir)
    raise SystemExit(
        "error: no sources dir — pass --sources, set PEMR_SOURCES, or set "
        "[paths].sources_dir in config.toml (see config.example.toml)"
    )


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


def _cmd_migrate(args: argparse.Namespace) -> int:
    conn = db.connect(_resolve_db_path(args))
    try:
        applied = db.migrate(conn, args.migrations_dir or db.DEFAULT_MIGRATIONS_DIR)
    finally:
        conn.close()
    if applied:
        for name in applied:
            print(f"applied {name}")
    else:
        print("up to date")
    return 0


def _cmd_person_add(args: argparse.Namespace) -> int:
    conn = db.connect(_resolve_db_path(args))
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
    conn = db.connect(_resolve_db_path(args))
    try:
        people = persons.list_people(conn)
    finally:
        conn.close()
    if not people:
        print("no people yet — `pemr person add --slug <slug> --name <name>`")
        return 0
    for p in people:
        dob = f"  dob={p.dob}" if p.dob else ""
        print(f"#{p.person_id}  {p.slug}  {p.full_name}{dob}")
    return 0


def _cmd_person_show(args: argparse.Namespace) -> int:
    conn = db.connect(_resolve_db_path(args))
    try:
        person = persons.get_person(conn, args.slug)
    finally:
        conn.close()
    if person is None:
        print(f"error: no person with slug '{args.slug}'", file=sys.stderr)
        return 1
    for key, value in asdict(person).items():
        print(f"{key:12} {value if value is not None else ''}")
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    conn = db.connect(_resolve_db_path(args))
    try:
        try:
            result = ingest.ingest_document(
                conn,
                args.file,
                person_slug=args.person,
                sources_dir=_resolve_sources_dir(args),
                doc_date=args.doc_date,
                category=args.category,
                provider=args.provider,
                ocr=(args.ocr == "tesseract"),
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
            f"(sha256 {doc.sha256[:12]}...) — nothing ingested"
        )
        return 0
    print(
        f"ingested document #{doc.document_id} (sha256 {doc.sha256[:12]}...) "
        f"-> sources/{doc.source_path}"
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

    conn = db.connect(_resolve_db_path(args))
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
        f"{c['conflict']} conflict"
    )
    if summary.conflict:
        print(
            f"note: {c['conflict']} conflict(s) staged — resolve with "
            "`pemr review-conflicts`"
        )
    return 0


def _cmd_review_conflicts(args: argparse.Namespace) -> int:
    conn = db.connect(_resolve_db_path(args))
    try:
        try:
            if args.resolve is not None:
                dedup.resolve_conflict(
                    conn, args.resolve, keep=args.keep, note=args.note
                )
                print(f"resolved conflict #{args.resolve} (keep-{args.keep})")
                return 0
            conflicts = dedup.list_conflicts(
                conn, status=None if args.all else "open"
            )
        except db.NotMigratedError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
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
    print(
        "\nresolve: `pemr review-conflicts --resolve <id> --keep existing|incoming "
        "[--note ...]`"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pemr", description="Personal EMR engine — SQLite is truth."
    )
    parser.add_argument("--version", action="version", version=f"pemr {__version__}")
    parser.add_argument("--db", help="path to pemr.db (overrides PEMR_DB/config)")
    parser.add_argument("--config", help="path to config.toml (default ./config.toml)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_migrate = sub.add_parser("migrate", help="apply pending migrations")
    p_migrate.add_argument(
        "--migrations-dir", help="override migrations directory (mainly for tests)"
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
    p_list.set_defaults(func=_cmd_person_list)

    p_show = person_sub.add_parser("show", help="show one person")
    p_show.add_argument("slug")
    p_show.set_defaults(func=_cmd_person_show)

    p_ingest = sub.add_parser(
        "ingest", help="ingest a document (hash, blob store, layer-1 dedup)"
    )
    p_ingest.add_argument("file", help="path to the document to ingest")
    p_ingest.add_argument("--person", required=True, help="owner slug, e.g. jane-doe")
    p_ingest.add_argument("--sources", help="sources blob dir (overrides config)")
    p_ingest.add_argument("--doc-date", dest="doc_date", help="date the doc pertains to")
    p_ingest.add_argument("--category", help="labs|imaging|visit-note|rx|vaccine|...")
    p_ingest.add_argument("--provider")
    p_ingest.add_argument(
        "--ocr", choices=["tesseract"], help="pre-fill ocr_text (soft dependency)"
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

    p_review = sub.add_parser(
        "review-conflicts", help="list or resolve staged dedup conflicts"
    )
    p_review.add_argument(
        "--resolve", type=int, metavar="CONFLICT_ID", help="resolve one conflict"
    )
    p_review.add_argument(
        "--keep", choices=["existing", "incoming"], default="existing",
        help="on --resolve: keep stored row or overwrite with incoming (default existing)",
    )
    p_review.add_argument("--note", help="optional resolution note")
    p_review.add_argument(
        "--all", action="store_true", help="list resolved conflicts too"
    )
    p_review.set_defaults(func=_cmd_review_conflicts)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

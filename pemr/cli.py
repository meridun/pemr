"""`pemr` command-line entry point (phase 1: migrate, person add|list|show).

DB path resolution order: --db flag > PEMR_DB env var > config.toml
[paths].data_dir + /pemr.db. Config path resolution: --config flag >
PEMR_CONFIG env var > ./config.toml.
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from dataclasses import asdict
from pathlib import Path

from . import __version__, db, persons


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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

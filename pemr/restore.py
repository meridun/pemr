"""`pemr restore` — install a `pemr backup` snapshot back over the live database.

Issue #55: the backup half was tested, the half that matters on the day the DB is gone
never had been. A drill proved the *copy* works, so what this module actually buys is
the operator-error protection the drill exposed, in a form that is testable:

* stale ``-wal`` / ``-shm`` sidecars next to the destination are cleared, so SQLite
  cannot replay a stale WAL over the freshly restored file;
* ``migrate`` runs afterwards, because a snapshot older than the code is the normal
  case and remembering that step is exactly the trap;
* the source is validated (opens, ``integrity_check``, has a schema) **before**
  anything on disk is touched, so a corrupt snapshot cannot destroy a live database;
* restoring over an existing database requires ``--force`` *and* first banks a rescue
  copy of that database, which makes restore non-destructive by construction.

**The rescue copy's off-pattern name is load-bearing.** :data:`pemr.backup._SNAPSHOT_RE`
matches only ``pemr-<8 digits>-<4|6 digits>.sqlite``, and the backup module's contract
guarantees anything else is never parsed and never pruned. So
``pemr-prerestore-YYYYMMDD-HHMMSS.sqlite`` is immune to rotation by construction --
including the same-calendar-day collapse that would otherwise eat it.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import backup, db, verify

# Rescue-copy filename: deliberately outside backup._SNAPSHOT_RE, so rotation is
# structurally incapable of pruning it (see the module docstring).
RESCUE_PREFIX = "pemr-prerestore-"

LATEST = "latest"

_SIDECAR_SUFFIXES = ("-wal", "-shm")


class RestoreError(RuntimeError):
    """A restore could not be performed. The live database is left as it was."""


@dataclass
class RestoreResult:
    snapshot: Path
    db_path: Path
    rescue: Path | None = None
    applied_migrations: list[str] = field(default_factory=list)
    cleared_sidecars: list[Path] = field(default_factory=list)
    report: verify.VerifyReport | None = None


def latest_snapshot(backup_dir: str | Path) -> Path | None:
    """Newest snapshot in ``backup_dir`` by *filename* timestamp, or None.

    Filename, never mtime — the backup module's contract, and the only ordering that
    survives a cloud-sync round trip.
    """
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return None
    dated: list[tuple[datetime, Path]] = []
    for path in backup_dir.glob("pemr-*.sqlite"):
        ts = backup._parse_ts(path.name)
        if ts is not None:
            dated.append((ts, path))
    if not dated:
        return None
    dated.sort(key=lambda e: e[0], reverse=True)
    return dated[0][1]


def resolve_snapshot(spec: str, backup_dir: str | Path | None) -> Path:
    """Resolve the positional arg: an existing path, a bare name, or ``latest``."""
    if spec == LATEST:
        if backup_dir is None:
            raise RestoreError(
                "`restore latest` needs a backup dir - pass --backup-dir, set "
                "PEMR_BACKUP_DIR, or set [paths].backup_dir in config.toml"
            )
        found = latest_snapshot(backup_dir)
        if found is None:
            raise RestoreError(f"no snapshots found in {Path(backup_dir)}")
        return found

    direct = Path(spec)
    if direct.is_file():
        return direct
    if backup_dir is not None:
        in_dir = Path(backup_dir) / spec
        if in_dir.is_file():
            return in_dir
    raise RestoreError(f"no such snapshot: {spec}")


def _validate_snapshot(snapshot: Path) -> list[str]:
    """Integrity + schema checks on the source. Returns its applied migrations."""
    result = backup.integrity_check(snapshot)
    if result != "ok":
        raise RestoreError(f"snapshot failed integrity_check: {result}")
    conn = sqlite3.connect(snapshot)
    try:
        conn.row_factory = sqlite3.Row
        if not db.is_migrated(conn):
            raise RestoreError(
                f"{snapshot} has no pemr schema - it is not a pemr snapshot"
            )
        return sorted(db.applied_versions(conn))
    except sqlite3.Error as exc:
        raise RestoreError(f"cannot read {snapshot}: {exc}") from exc
    finally:
        conn.close()


def _live_row_total(db_path: Path) -> int | None:
    """Total rows across the counted tables, for the refuse-without---force message."""
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error:
        return None
    try:
        return sum(verify.row_counts(conn).values())
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _sidecars(db_path: Path) -> list[Path]:
    return [db_path.with_name(db_path.name + suffix) for suffix in _SIDECAR_SUFFIXES]


def restore(
    snapshot_spec: str,
    db_path: str | Path,
    *,
    backup_dir: str | Path | None = None,
    sources_dir: str | Path | None = None,
    migrations_dir: str | Path | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> RestoreResult:
    """Install ``snapshot_spec`` over ``db_path``. See the module docstring for order.

    Every abort path leaves the live database exactly as it was. Blob problems are
    *reported*, never fatal: the database restore genuinely succeeded, and failing
    there would be wrong when `sources/` is merely mid-sync.
    """
    db_path = Path(db_path)
    snapshot = resolve_snapshot(snapshot_spec, backup_dir)

    # 1. Validate the source before touching anything.
    if db_path.exists() and snapshot.resolve() == db_path.resolve():
        raise RestoreError(f"{snapshot} is the live database - nothing to restore from")
    _validate_snapshot(snapshot)

    result = RestoreResult(snapshot=snapshot, db_path=db_path)

    # 2/3. Guard the destination, and bank a rescue copy before overwriting it.
    if db.database_exists(db_path):
        if not force:
            total = _live_row_total(db_path)
            rows = "unreadable" if total is None else f"{total} row(s)"
            raise RestoreError(
                f"{db_path} already exists ({rows}) - restoring would replace it. "
                "Re-run with --force (a pemr-prerestore-*.sqlite rescue copy of the "
                "current database is taken first)."
            )
        # Rescue copies land next to the other snapshots; if no backup dir is
        # configured, the snapshot's own directory is the obvious fallback.
        rescue_dir = Path(backup_dir) if backup_dir is not None else snapshot.parent
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        try:
            rescue, _ = backup.snapshot(
                db_path, rescue_dir, name=f"{RESCUE_PREFIX}{stamp}.sqlite"
            )
        except backup.BackupError as exc:
            raise RestoreError(
                f"aborted: could not take a rescue copy of {db_path}: {exc}"
            ) from exc
        result.rescue = rescue

    # 4/5. Stage the copy first, then clear sidecars and swap it in atomically.
    # Staging before any deletion means a failed copy destroys nothing; os.replace on
    # the same filesystem means a crash mid-install never leaves a half-written DB.
    tmp = db_path.with_name(db_path.name + ".restore-tmp")
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if tmp.exists():
            tmp.unlink()
        shutil.copyfile(snapshot, tmp)
    except OSError as exc:
        raise RestoreError(f"cannot stage {snapshot} next to {db_path}: {exc}") from exc

    try:
        for sidecar in _sidecars(db_path):
            if sidecar.exists():
                sidecar.unlink()
                result.cleared_sidecars.append(sidecar)
        os.replace(tmp, db_path)
    except OSError as exc:
        backup._unlink_quietly(tmp)
        raise RestoreError(f"cannot install {snapshot} as {db_path}: {exc}") from exc

    # 6/7. Migrate forward (a snapshot older than the code is the normal case), then
    # report on what landed.
    conn = db.connect(db_path)
    try:
        result.applied_migrations = db.migrate(
            conn, migrations_dir or db.DEFAULT_MIGRATIONS_DIR
        )
        result.report = verify.verify_report(conn, sources_dir)
    except (sqlite3.Error, RuntimeError) as exc:
        raise RestoreError(
            f"restored {db_path} but migrate failed: {exc}"
        ) from exc
    finally:
        conn.close()
    return result

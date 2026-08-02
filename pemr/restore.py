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
    """A restore could not be performed.

    Every failure path up to and including the install leaves the live database
    exactly as it was. The single exception is a stale sidecar that will not delete
    *after* a successful install; that message says so explicitly and names the file.
    """


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


def _rescue_name(rescue_dir: Path, now: datetime) -> str:
    """A free rescue filename, still outside :data:`backup._SNAPSHOT_RE`.

    ``backup.snapshot(name=...)`` deliberately has no collision fallback, so two
    ``restore --force`` runs inside one wall-clock second would otherwise abort with
    "could not take a rescue copy" - fail-safe, but mid-incident that reads as the
    restore mechanism itself being broken. The ``-2``/``-3`` suffix keeps the name off
    the rotation pattern just as the second-precision stamp does.
    """
    stamp = now.strftime("%Y%m%d-%H%M%S")
    candidate = f"{RESCUE_PREFIX}{stamp}.sqlite"
    counter = 2
    while (rescue_dir / candidate).exists():
        candidate = f"{RESCUE_PREFIX}{stamp}-{counter}.sqlite"
        counter += 1
    return candidate


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
        try:
            rescue, _ = backup.snapshot(
                db_path, rescue_dir, name=_rescue_name(rescue_dir, now or datetime.now())
            )
        except backup.BackupError as exc:
            raise RestoreError(
                f"aborted before touching {db_path}: no rescue copy of it could be "
                f"written to {rescue_dir}: {exc}. The live database is unchanged - "
                "move it aside and re-run (restoring onto an absent database needs no "
                "--force)."
            ) from exc
        result.rescue = rescue

    # 4. Stage the copy first: a failed copy then destroys nothing (under the reverse
    # order a copy failure would leave the operator with no live database at all).
    tmp = db_path.with_name(db_path.name + ".restore-tmp")
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if tmp.exists():
            tmp.unlink()
        shutil.copyfile(snapshot, tmp)
    except OSError as exc:
        raise RestoreError(f"cannot stage {snapshot} next to {db_path}: {exc}") from exc

    # 5. Install atomically - same filesystem, so a crash mid-install can never leave
    # a half-written database in place.
    try:
        os.replace(tmp, db_path)
    except OSError as exc:
        backup._unlink_quietly(tmp)
        raise RestoreError(f"cannot install {snapshot} as {db_path}: {exc}") from exc

    # 6. Only now clear the sidecars. A stale `-wal` holds committed-but-uncheckpointed
    # transactions, so deleting it on a path that then fails to install would be the
    # one way this code loses data; doing it after the swap makes "every abort leaves
    # the live database as it was" true by construction. Nothing opens the database in
    # between, so SQLite never sees the restored file next to a foreign WAL.
    for sidecar in _sidecars(db_path):
        if not sidecar.exists():
            continue
        try:
            sidecar.unlink()
        except OSError as exc:
            raise RestoreError(
                f"restored {db_path} from {snapshot}, but the stale sidecar "
                f"{sidecar.name} could not be removed: {exc}. Delete it by hand before "
                "opening the database - SQLite may otherwise replay it over the restore."
            ) from exc
        result.cleared_sidecars.append(sidecar)

    # 7/8. Migrate forward (a snapshot older than the code is the normal case), then
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

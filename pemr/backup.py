"""Phase 6: `pemr backup` — VACUUM INTO snapshot + calendar-bucketed rotation.

Architecture.md §8: the live `pemr.db` is local-only (WAL `-wal`/`-shm` sidecars
corrupt under cloud sync); `pemr backup` writes a consistent single-file
``VACUUM INTO`` snapshot into the cloud-synced ``backup_dir``, then rotates older
snapshots by a calendar daily + weekly retention policy.

Because rotation prunes *irreplaceable-data* snapshots, its safety invariants are
part of the contract, not incidental:

* A run can never prune its own output. The just-written snapshot lands in the newest
  daily bucket under any ``keep_daily >= 1``; ``rotate(..., protect=<snapshot>)`` makes
  this hold *unconditionally* — even for degenerate ``keep_daily=0, keep_weekly=0`` the
  protected snapshot is always retained.
* Prune only ever deletes files whose names match the ``pemr-*.sqlite`` pattern this
  command creates — anything else in the directory is out of scope by construction.
* Every snapshot is read back and ``PRAGMA integrity_check``-ed *before*
  :func:`snapshot` returns, so a corrupt snapshot can never reach :func:`rotate` and
  prune good snapshots to make room for a worthless one (issue #55). An unreadable
  backup discovered at restore time is the classic backup failure; this is the check
  that makes it discovered at write time instead.

Timestamps are **local time** (single-machine personal tool; Task Scheduler fires on
wall-clock and "daily/weekly" should track the user's calendar) and are parsed from
the *filename*, never from mtime (authoritative and cloud-sync-stable).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

DEFAULT_KEEP_DAILY = 7
DEFAULT_KEEP_WEEKLY = 4

# pemr-YYYYMMDD-HHMM.sqlite, plus the -HHMMSS same-minute collision variant.
# Anything not matching this exact shape is never parsed and never pruned.
_SNAPSHOT_RE = re.compile(r"^pemr-(\d{8})-(\d{6}|\d{4})\.sqlite$")


class BackupError(RuntimeError):
    """A snapshot could not be taken (missing DB, sqlite/OS failure)."""


@dataclass
class RotationResult:
    kept: list[Path]
    pruned: list[Path]


def _snapshot_name(now: datetime, *, seconds: bool) -> str:
    fmt = "pemr-%Y%m%d-%H%M%S.sqlite" if seconds else "pemr-%Y%m%d-%H%M.sqlite"
    return now.strftime(fmt)


def _parse_ts(name: str) -> datetime | None:
    """Timestamp encoded in a snapshot filename, or None if it isn't one of ours."""
    m = _SNAPSHOT_RE.match(name)
    if m is None:
        return None
    day, clock = m.group(1), m.group(2)
    fmt = "%Y%m%d%H%M%S" if len(clock) == 6 else "%Y%m%d%H%M"
    try:
        return datetime.strptime(day + clock, fmt)
    except ValueError:
        return None


def integrity_check(path: str | Path) -> str:
    """``PRAGMA integrity_check`` on ``path``; returns ``"ok"`` or the failure text.

    A file that will not even open as SQLite reports the sqlite error text, so callers
    have a single string to test against ``"ok"``.

    A path that does not exist is a failure, not ``"ok"``: ``sqlite3.connect`` creates
    eagerly and an empty database passes ``integrity_check``, so without this guard the
    function would answer "healthy" *and* leave behind the zero-byte debris that
    :func:`pemr.db.database_exists` exists to reject.
    """
    if not Path(path).is_file():
        return f"no such file: {path}"
    try:
        conn = sqlite3.connect(path)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"cannot open as sqlite: {exc}"
    if not row or not row[0]:
        return "integrity_check returned nothing"
    return str(row[0])


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:  # already gone / locked - nothing useful to do here
        pass


def snapshot(
    db_path: str | Path,
    backup_dir: str | Path,
    now: datetime | None = None,
    name: str | None = None,
) -> tuple[Path, int]:
    """Write a ``VACUUM INTO`` snapshot of ``db_path`` into ``backup_dir``.

    Returns ``(snapshot_path, size_bytes)``. Local-time, minute-precision filename;
    on a same-minute collision falls back to seconds precision. ``VACUUM INTO``
    refuses to overwrite an existing target — that safety is preserved, so if even
    the seconds-precision name exists the underlying error propagates as
    :class:`BackupError` rather than clobbering a file.

    ``name`` overrides the generated filename verbatim (no timestamp, no collision
    fallback), keeping the refuse-to-overwrite behavior. :mod:`pemr.restore` uses it
    for the ``pemr-prerestore-*.sqlite`` rescue copy: that name deliberately does not
    match :data:`_SNAPSHOT_RE`, so by the module contract above rotation can never
    parse or prune it.

    The written snapshot is verified (:func:`integrity_check`) before returning; a
    snapshot that fails is unlinked and raises :class:`BackupError`, so a corrupt
    file never survives to be rotated against.

    Raises :class:`BackupError` if the DB file is missing, the vacuum fails, or the
    result does not verify.
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        raise BackupError(f"no database at {db_path}")

    backup_dir = Path(backup_dir)
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"cannot create backup dir {backup_dir}: {exc}") from exc

    now = now or datetime.now()
    if name is not None:
        target = backup_dir / name
    else:
        target = backup_dir / _snapshot_name(now, seconds=False)
        if target.exists():
            target = backup_dir / _snapshot_name(now, seconds=True)

    # Whether the target predates this call decides whether a failure may clean it up:
    # never unlink a file we did not write (that is the never-overwrite contract).
    pre_existing = target.exists()

    conn = sqlite3.connect(db_path)
    try:
        # VACUUM INTO runs in autocommit (Python's sqlite3 only auto-BEGINs for
        # INSERT/UPDATE/DELETE/REPLACE) and refuses to overwrite an existing target.
        conn.execute("VACUUM INTO ?", (str(target),))
    except sqlite3.Error as exc:
        if not pre_existing and target.exists():
            _unlink_quietly(target)  # partial write from a failed vacuum
        raise BackupError(f"snapshot failed: {exc}") from exc
    finally:
        conn.close()

    result = integrity_check(target)
    if result != "ok":
        _unlink_quietly(target)
        raise BackupError(f"snapshot failed verification: {result}")

    return target, target.stat().st_size


def rotate(
    backup_dir: str | Path,
    keep_daily: int = DEFAULT_KEEP_DAILY,
    keep_weekly: int = DEFAULT_KEEP_WEEKLY,
    protect: str | Path | None = None,
) -> RotationResult:
    """Prune snapshots in ``backup_dir`` down to the daily + weekly retention set.

    Deterministic, filename-driven (§8 "prune older"):

    1. Glob ``pemr-*.sqlite`` and keep only names matching the exact snapshot
       pattern; parse the local-time timestamp from the filename.
    2. Sort newest → oldest.
    3. **Daily tier** — for each of the most recent ``keep_daily`` distinct calendar
       days, retain that day's newest snapshot.
    4. **Weekly tier** — of the snapshots not already retained, for each of the most
       recent ``keep_weekly`` distinct ISO weeks (``date.isocalendar()`` ``(year,
       week)``), retain that week's newest snapshot.
    5. Everything retained by neither tier is deleted. This also collapses redundant
       same-day / same-week snapshots.

    ``protect`` — a snapshot that must always be retained regardless of the daily and
    weekly counts. The caller passes the path it just wrote so that the contract
    invariant "a run can never prune its own output" holds *unconditionally*, even for
    degenerate retention such as ``keep_daily=0, keep_weekly=0``. Matched by filename
    (all snapshots live in ``backup_dir``).

    Returns the kept and pruned paths, each newest-first.
    """
    backup_dir = Path(backup_dir)
    protect_name = Path(protect).name if protect is not None else None
    entries: list[tuple[datetime, Path]] = []
    for path in backup_dir.glob("pemr-*.sqlite"):
        ts = _parse_ts(path.name)
        if ts is not None:
            entries.append((ts, path))
    entries.sort(key=lambda e: e[0], reverse=True)  # newest first

    kept: set[Path] = set()

    # Always retain the just-written snapshot: makes the "never prune own output"
    # invariant hold even when the daily/weekly buckets are empty (keep_* == 0).
    if protect_name is not None:
        for _, path in entries:
            if path.name == protect_name:
                kept.add(path)

    # Daily tier: newest snapshot for each of the most recent keep_daily distinct
    # calendar days. Same-day entries are contiguous (sorted by full timestamp), so
    # the first time a day is seen is its newest snapshot.
    daily_days: list[date] = []
    for ts, path in entries:
        day = ts.date()
        if day in daily_days:
            continue
        if len(daily_days) >= keep_daily:
            break
        daily_days.append(day)
        kept.add(path)

    # Weekly tier: of the snapshots not already daily-kept, newest per ISO week for
    # the most recent keep_weekly distinct weeks.
    weekly_weeks: list[tuple[int, int]] = []
    for ts, path in entries:
        if path in kept:
            continue
        week = ts.isocalendar()[:2]  # (iso_year, iso_week)
        if week in weekly_weeks:
            continue
        if len(weekly_weeks) >= keep_weekly:
            break
        weekly_weeks.append(week)
        kept.add(path)

    kept_paths = [path for _, path in entries if path in kept]
    pruned_paths = [path for _, path in entries if path not in kept]
    for path in pruned_paths:
        path.unlink()
    return RotationResult(kept=kept_paths, pruned=pruned_paths)

"""Phase 6: `pemr backup` — VACUUM INTO snapshot + calendar-bucketed rotation.

Covers snapshot round-trip/validity, local-time filename + same-minute collision,
the daily/weekly rotation algorithm (newest-per-day/week, same-day/same-week
collapse, older-than-window pruning, ISO-week + year-rollover edges, non-matching
filenames left untouched, fresh snapshot always survives — including the
``protect=`` guard that keeps the just-written snapshot under degenerate keep 0/0),
and the CLI wiring (--no-rotate, --json, missing-DB and unresolvable-backup-dir rc=1,
negative retention rejected).
"""

from datetime import datetime
from pathlib import Path

import pytest

from pemr import backup, cli, db


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _seed_db(path: Path) -> None:
    """A migrated DB with one person, so snapshots have real content to read back."""
    conn = db.connect(path)
    try:
        db.migrate(conn)
        conn.execute(
            "INSERT INTO person (slug, full_name, dob) VALUES (?, ?, ?)",
            ("jane-doe", "Jane Doe", "1980-01-01"),
        )
        conn.commit()
    finally:
        conn.close()


def _touch_snapshots(backup_dir: Path, names: list[str]) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (backup_dir / name).write_bytes(b"stub")


def _snap(day: str, clock: str = "1200") -> str:
    return f"pemr-{day}-{clock}.sqlite"


# --------------------------------------------------------------------------- #
# snapshot
# --------------------------------------------------------------------------- #

def test_snapshot_is_valid_queryable_sqlite(tmp_path):
    src = tmp_path / "pemr.db"
    _seed_db(src)
    out = tmp_path / "backups"
    snap, size = backup.snapshot(src, out)

    assert snap.parent == out
    assert snap.name.startswith("pemr-") and snap.suffix == ".sqlite"
    assert size == snap.stat().st_size > 0

    # Round-trip: the snapshot opens and the seeded row reads back.
    conn = db.connect(snap)
    try:
        assert db.is_migrated(conn)
        row = conn.execute("SELECT full_name FROM person WHERE slug='jane-doe'").fetchone()
        assert row["full_name"] == "Jane Doe"
    finally:
        conn.close()


def test_snapshot_filename_is_local_time_minute_precision(tmp_path):
    src = tmp_path / "pemr.db"
    _seed_db(src)
    now = datetime(2026, 3, 4, 9, 7)  # naive local time
    snap, _ = backup.snapshot(src, tmp_path / "backups", now=now)
    assert snap.name == "pemr-20260304-0907.sqlite"


def test_snapshot_same_minute_collision_falls_back_to_seconds(tmp_path):
    src = tmp_path / "pemr.db"
    _seed_db(src)
    out = tmp_path / "backups"
    now = datetime(2026, 3, 4, 9, 7, 42)
    first, _ = backup.snapshot(src, out, now=now)
    second, _ = backup.snapshot(src, out, now=now)

    assert first.name == "pemr-20260304-0907.sqlite"
    assert second.name == "pemr-20260304-090742.sqlite"
    assert first.exists() and second.exists()  # neither overwritten


def test_snapshot_never_overwrites_even_seconds_target(tmp_path):
    src = tmp_path / "pemr.db"
    _seed_db(src)
    out = tmp_path / "backups"
    now = datetime(2026, 3, 4, 9, 7, 42)
    backup.snapshot(src, out, now=now)  # minute
    backup.snapshot(src, out, now=now)  # seconds fallback
    # A third same-second run has no free name → VACUUM INTO refuses → BackupError.
    with pytest.raises(backup.BackupError):
        backup.snapshot(src, out, now=now)


def test_snapshot_missing_db_raises(tmp_path):
    with pytest.raises(backup.BackupError, match="no database at"):
        backup.snapshot(tmp_path / "absent.db", tmp_path / "backups")


def test_snapshot_creates_backup_dir(tmp_path):
    src = tmp_path / "pemr.db"
    _seed_db(src)
    nested = tmp_path / "a" / "b" / "backups"
    snap, _ = backup.snapshot(src, nested)
    assert snap.parent == nested and nested.is_dir()


# --------------------------------------------------------------------------- #
# rotation
# --------------------------------------------------------------------------- #

def test_rotate_daily_keeps_newest_per_day(tmp_path):
    out = tmp_path / "backups"
    # Three consecutive days, two snapshots each; keep_daily=7 keeps newest per day.
    names = [
        _snap("20260701", "0800"), _snap("20260701", "2000"),
        _snap("20260702", "0800"), _snap("20260702", "2000"),
        _snap("20260703", "0800"), _snap("20260703", "2000"),
    ]
    _touch_snapshots(out, names)
    # keep_weekly=0 isolates the daily tier (all three days share one ISO week, so a
    # nonzero weekly tier would legitimately retain the newest leftover too).
    result = backup.rotate(out, keep_daily=7, keep_weekly=0)

    kept = {p.name for p in result.kept}
    assert kept == {
        _snap("20260701", "2000"),
        _snap("20260702", "2000"),
        _snap("20260703", "2000"),
    }
    # Same-day older snapshots pruned (collapse).
    assert {p.name for p in result.pruned} == {
        _snap("20260701", "0800"),
        _snap("20260702", "0800"),
        _snap("20260703", "0800"),
    }
    for p in result.pruned:
        assert not p.exists()
    for p in result.kept:
        assert p.exists()


def test_rotate_weekly_tier_and_older_pruned(tmp_path):
    out = tmp_path / "backups"
    # 10 distinct days spanning ~2 weeks. keep_daily=3 keeps the 3 newest days;
    # keep_weekly=1 keeps the newest not-daily-kept snapshot in the most recent
    # remaining ISO week; everything else prunes.
    days = [
        "20260706", "20260707", "20260708", "20260709", "20260710",  # ISO week 28
        "20260713", "20260714", "20260715", "20260716", "20260717",  # ISO week 29
    ]
    _touch_snapshots(out, [_snap(d) for d in days])
    result = backup.rotate(out, keep_daily=3, keep_weekly=1)
    kept = {p.name for p in result.kept}

    # Daily: 3 newest calendar days.
    assert _snap("20260717") in kept
    assert _snap("20260716") in kept
    assert _snap("20260715") in kept
    # Weekly: newest remaining is 20260714 (week 29, since 15-17 are daily-kept).
    assert _snap("20260714") in kept
    assert len(kept) == 4
    # Everything older pruned.
    assert _snap("20260713") not in kept
    assert _snap("20260706") not in kept


def test_rotate_weekly_collapses_same_week(tmp_path):
    out = tmp_path / "backups"
    # Two snapshots in one older ISO week; only the newest survives the weekly tier.
    _touch_snapshots(out, [
        _snap("20260717"),                 # daily-kept (most recent day)
        _snap("20260707"), _snap("20260709"),  # same ISO week 28, weekly tier
    ])
    result = backup.rotate(out, keep_daily=1, keep_weekly=1)
    kept = {p.name for p in result.kept}
    assert kept == {_snap("20260717"), _snap("20260709")}
    assert {p.name for p in result.pruned} == {_snap("20260707")}


def test_rotate_year_rollover_distinct_iso_weeks(tmp_path):
    out = tmp_path / "backups"
    # 2025-12-29 is ISO week 1 of 2026; 2025-12-22 is ISO week 52 of 2025 — distinct
    # week keys across the year boundary, so with keep_weekly=2 both weeklies survive.
    _touch_snapshots(out, [
        _snap("20260105"),   # daily-kept anchor (most recent)
        _snap("20251229"),   # ISO (2026, 1)
        _snap("20251222"),   # ISO (2025, 52)
    ])
    result = backup.rotate(out, keep_daily=1, keep_weekly=2)
    kept = {p.name for p in result.kept}
    assert kept == {_snap("20260105"), _snap("20251229"), _snap("20251222")}
    assert result.pruned == []


def test_rotate_ignores_non_matching_filenames(tmp_path):
    out = tmp_path / "backups"
    _touch_snapshots(out, [_snap("20260701"), _snap("20260601")])
    # Foreign / hand-named files that must never be touched.
    foreign = [
        "notes.txt",
        "pemr-backup.sqlite",         # no timestamp
        "pemr-2026070.sqlite",        # too few digits
        "pemr-20260701.sqlite",       # missing -HHMM
        "pemr-20260701-12.sqlite",    # wrong clock width
        "backup-20260701-1200.sqlite",  # wrong prefix
    ]
    for f in foreign:
        (out / f).write_bytes(b"keepme")

    backup.rotate(out, keep_daily=1, keep_weekly=0)
    for f in foreign:
        assert (out / f).exists(), f"rotation must not touch {f}"


def test_rotate_seconds_variant_is_recognized(tmp_path):
    out = tmp_path / "backups"
    _touch_snapshots(out, [
        _snap("20260701", "120045"),  # seconds-precision same-minute variant (12:00:45)
        _snap("20260701", "1200"),    # minute-precision (12:00:00)
    ])
    result = backup.rotate(out, keep_daily=1, keep_weekly=0)
    # Both parse as day 2026-07-01; the seconds variant is genuinely newer → kept,
    # the minute one collapsed. Proves the -HHMMSS filename form is recognized.
    assert {p.name for p in result.kept} == {_snap("20260701", "120045")}
    assert {p.name for p in result.pruned} == {_snap("20260701", "1200")}


def test_rotate_empty_dir_is_noop(tmp_path):
    out = tmp_path / "backups"
    out.mkdir()
    result = backup.rotate(out)
    assert result.kept == [] and result.pruned == []


def test_rotate_protect_survives_zero_retention(tmp_path):
    # Contract invariant made unconditional: with keep_daily=0 and keep_weekly=0
    # (empty buckets), the protected just-written snapshot is still retained.
    out = tmp_path / "backups"
    fresh = _snap("20260701", "1200")
    _touch_snapshots(out, [fresh, _snap("20260630"), _snap("20260620")])
    result = backup.rotate(out, keep_daily=0, keep_weekly=0, protect=out / fresh)
    assert {p.name for p in result.kept} == {fresh}
    assert fresh not in {p.name for p in result.pruned}
    assert (out / fresh).exists()


def test_rotate_without_protect_zero_retention_prunes_everything(tmp_path):
    # Documents the boundary: absent `protect`, keep 0/0 is a full purge — which is
    # exactly why the CLI always passes protect and rejects negative counts.
    out = tmp_path / "backups"
    _touch_snapshots(out, [_snap("20260701"), _snap("20260630")])
    result = backup.rotate(out, keep_daily=0, keep_weekly=0)
    assert result.kept == []
    assert len(result.pruned) == 2


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #

def _run(tmp_path, *argv):
    return cli.main(["--db", str(tmp_path / "pemr.db"), *argv])


def test_cli_backup_writes_snapshot_and_rotates(tmp_path, capsys):
    _seed_db(tmp_path / "pemr.db")
    out = tmp_path / "backups"
    rc = _run(tmp_path, "backup", "--backup-dir", str(out))
    assert rc == 0
    printed = capsys.readouterr().out
    assert "wrote " in printed and "rotated: kept 1, pruned 0" in printed
    snaps = list(out.glob("pemr-*.sqlite"))
    assert len(snaps) == 1


def test_cli_backup_json(tmp_path, capsys):
    import json as _json
    _seed_db(tmp_path / "pemr.db")
    out = tmp_path / "backups"
    rc = _run(tmp_path, "backup", "--backup-dir", str(out), "--json")
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert Path(payload["snapshot"]).exists()
    assert payload["bytes"] > 0
    assert len(payload["kept"]) == 1 and payload["pruned"] == []


def test_cli_backup_fresh_snapshot_survives_rotation(tmp_path, capsys):
    _seed_db(tmp_path / "pemr.db")
    out = tmp_path / "backups"
    # Pre-populate with an old snapshot that would otherwise be outside the window.
    _touch_snapshots(out, [_snap("20200101")])
    rc = _run(tmp_path, "backup", "--backup-dir", str(out),
              "--keep-daily", "1", "--keep-weekly", "0")
    assert rc == 0
    remaining = {p.name for p in out.glob("pemr-*.sqlite")}
    # Today's fresh snapshot is the newest day → kept; the 2020 one pruned.
    assert _snap("20200101") not in remaining
    assert len(remaining) == 1  # only the just-written snapshot


def test_cli_backup_no_rotate_prunes_nothing(tmp_path, capsys):
    _seed_db(tmp_path / "pemr.db")
    out = tmp_path / "backups"
    _touch_snapshots(out, [_snap("20200101")])
    rc = _run(tmp_path, "backup", "--backup-dir", str(out), "--no-rotate")
    assert rc == 0
    assert "rotation skipped (--no-rotate)" in capsys.readouterr().out
    # Old snapshot untouched; new one added.
    assert (out / _snap("20200101")).exists()
    assert len(list(out.glob("pemr-*.sqlite"))) == 2


def test_cli_backup_missing_db_rc1(tmp_path, capsys):
    # No DB created.
    rc = _run(tmp_path, "backup", "--backup-dir", str(tmp_path / "backups"))
    assert rc == 1
    assert "no database at" in capsys.readouterr().err


def test_cli_backup_unresolvable_backup_dir_exits(tmp_path, monkeypatch):
    _seed_db(tmp_path / "pemr.db")
    monkeypatch.delenv("PEMR_BACKUP_DIR", raising=False)
    # No --backup-dir, no env, no config → resolver raises SystemExit (rc=1 shape).
    with pytest.raises(SystemExit) as exc:
        cli.main(["--db", str(tmp_path / "pemr.db"), "--config",
                  str(tmp_path / "absent.toml"), "backup"])
    assert "no backup dir" in str(exc.value)


def test_cli_backup_dir_env_resolution(tmp_path, capsys, monkeypatch):
    _seed_db(tmp_path / "pemr.db")
    out = tmp_path / "env-backups"
    monkeypatch.setenv("PEMR_BACKUP_DIR", str(out))
    rc = _run(tmp_path, "backup")
    assert rc == 0
    assert len(list(out.glob("pemr-*.sqlite"))) == 1


def test_cli_backup_retention_from_config(tmp_path, capsys, monkeypatch):
    _seed_db(tmp_path / "pemr.db")
    out = tmp_path / "backups"
    _touch_snapshots(out, [_snap("20200101")])
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"[paths]\nbackup_dir = {out.as_posix()!r}\n"
        "[backup]\nkeep_daily = 1\nkeep_weekly = 0\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("PEMR_BACKUP_DIR", raising=False)
    rc = cli.main(["--db", str(tmp_path / "pemr.db"), "--config", str(cfg), "backup"])
    assert rc == 0
    # config keep_daily=1/keep_weekly=0 prunes the old 2020 snapshot.
    assert not (out / _snap("20200101")).exists()


def test_cli_backup_zero_retention_keeps_fresh_snapshot(tmp_path, capsys):
    # keep_daily=0 keep_weekly=0 must still leave the just-written snapshot on disk
    # (the audit finding: a run must never prune its own output). rc=0, kept >= 1.
    _seed_db(tmp_path / "pemr.db")
    out = tmp_path / "backups"
    rc = _run(tmp_path, "backup", "--backup-dir", str(out), "--json",
              "--keep-daily", "0", "--keep-weekly", "0")
    import json as _json
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert Path(payload["snapshot"]).exists()
    assert len(payload["kept"]) == 1
    assert payload["pruned"] == []
    assert len(list(out.glob("pemr-*.sqlite"))) == 1


def test_cli_backup_negative_retention_rejected(tmp_path, capsys):
    # Negative retention has no policy meaning and previously mapped to "delete all";
    # it is now rejected before any pruning (rc=1), and the snapshot still survives.
    _seed_db(tmp_path / "pemr.db")
    out = tmp_path / "backups"
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, "backup", "--backup-dir", str(out), "--keep-daily", "-3")
    assert "cannot be negative" in str(exc.value)
    # The snapshot was written before rotation; the failure leaves it intact.
    assert len(list(out.glob("pemr-*.sqlite"))) == 1

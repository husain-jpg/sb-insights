"""
Database backup utility
=======================

Creates timestamped, gzipped copies of the SQLite DB file for disaster recovery.

Retention policy:
  - Last 30 daily backups (one per day)
  - Last 13 weekly backups (Sunday backups kept for ~3 months)
  - Older backups deleted automatically

Usage:
  - Nightly auto-backup runs in a background thread (started by api/main.py).
  - Manual backup: POST /api/system/backup-now

Honest scope notes:
  - Local backups only — protects against accidental deletion / corruption,
    not against drive failure. Add cloud-side backup at launch.
  - Uses sqlite3.Connection.backup() (the proper online backup API), so
    backups are safe even if the DB is being written to during the backup.
  - Single SQLite file — Postgres backups will need a different approach
    (pg_dump etc.) when we migrate at launch.
"""
from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path


def _backup_dir() -> Path:
    """Where backups live. Created if missing."""
    d = Path(__file__).parent.parent / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _db_path() -> Path:
    """Path to the live database file."""
    # Match the env variable used by api/main.py
    p = os.environ.get("DB_PATH")
    if p:
        return Path(p)
    return Path(__file__).parent.parent / "terroir.db"


def create_backup() -> dict:
    """Create a single gzipped backup of the live DB.

    Returns metadata about the new backup (path, size, timestamp).
    Safe to call concurrent with the live system writing — uses SQLite's
    online backup API rather than copying the file blindly.
    """
    src = _db_path()
    if not src.exists():
        raise FileNotFoundError(f"No DB file at {src}")

    today_iso = date.today().isoformat()
    out = _backup_dir() / f"terroir-{today_iso}.db.gz"

    # Step 1: stream a consistent snapshot to a temp .db file using SQLite's
    # backup API. This is safe even with the live system writing.
    tmp = out.with_suffix(".tmp.db")
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(tmp))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    # Step 2: gzip the snapshot, then remove the temp.
    with open(tmp, "rb") as fin, gzip.open(out, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout)
    tmp.unlink()

    return {
        "path": str(out.relative_to(_backup_dir().parent)),
        "size_bytes": out.stat().st_size,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def list_backups() -> list[dict]:
    """List all backup files with metadata, newest first."""
    bdir = _backup_dir()
    items = []
    for f in sorted(bdir.glob("terroir-*.db.gz"), reverse=True):
        stat = f.stat()
        items.append({
            "filename": f.name,
            "size_bytes": stat.st_size,
            "size_human": _human_size(stat.st_size),
            "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            "path": str(f.relative_to(_backup_dir().parent)),
        })
    return items


def prune_backups() -> dict:
    """Apply retention policy. Keep:
      - last 30 daily backups (newest 30 by filename date)
      - older backups: only Sunday ones kept, then only 13 of those
      - everything else deleted

    Returns count of pruned files.
    """
    bdir = _backup_dir()
    files = sorted(bdir.glob("terroir-*.db.gz"))
    if not files:
        return {"pruned": 0, "kept": 0}

    today = date.today()
    kept = []
    pruned = []

    # Parse filename → date
    def _file_date(p: Path) -> date | None:
        # filename like 'terroir-2026-05-08.db.gz'
        try:
            stem = p.name.replace("terroir-", "").replace(".db.gz", "")
            return date.fromisoformat(stem)
        except (ValueError, IndexError):
            return None

    dated = [(p, _file_date(p)) for p in files]
    dated = [(p, d) for p, d in dated if d is not None]

    cutoff_30d = today - timedelta(days=30)

    # Keep all backups within last 30 days
    recent = [(p, d) for p, d in dated if d >= cutoff_30d]
    older = [(p, d) for p, d in dated if d < cutoff_30d]

    # Of the older set, keep up to 13 Sundays (weekday=6 in Python)
    older_sundays = [(p, d) for p, d in older if d.weekday() == 6]
    # Keep newest 13 Sundays
    older_sundays_sorted = sorted(older_sundays, key=lambda x: x[1], reverse=True)
    keep_sundays = set(p for p, _ in older_sundays_sorted[:13])

    for p, d in dated:
        if d >= cutoff_30d or p in keep_sundays:
            kept.append(p)
        else:
            try:
                p.unlink()
                pruned.append(p.name)
            except OSError:
                pass

    return {
        "pruned": len(pruned),
        "kept": len(kept),
        "pruned_files": pruned,
    }


def get_db_stats() -> dict:
    """File size + row counts per table. Used by System Health UI."""
    src = _db_path()
    if not src.exists():
        return {"error": "DB file not found", "path": str(src)}
    stats = {
        "db_path": str(src),
        "db_size_bytes": src.stat().st_size,
        "db_size_human": _human_size(src.stat().st_size),
        "modified_at": datetime.fromtimestamp(src.stat().st_mtime).isoformat(timespec="seconds"),
    }
    conn = sqlite3.connect(str(src))
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [r[0] for r in cur.fetchall() if not r[0].startswith("sqlite_")]
        counts = {}
        for t in tables:
            try:
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                counts[t] = cur.fetchone()[0]
            except sqlite3.OperationalError:
                counts[t] = None
        stats["table_counts"] = counts
        stats["total_rows"] = sum(c for c in counts.values() if c is not None)
    finally:
        conn.close()
    return stats


def _human_size(n: int) -> str:
    """Format bytes as KB/MB/GB."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} TB"

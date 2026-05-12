"""
Backup and export utilities for SB Insights.

Two modes:
  1. backup  — copies the SQLite file to a timestamped .db file. Fastest, smallest,
               restorable on any machine with SQLite. Recommended for local backups.
  2. export  — writes every table to a single .json file. Slower and larger, but
               portable across DB engines (will be used to migrate to Postgres later).

Usage:
    python jobs/export_data.py backup
    python jobs/export_data.py backup --output backups/sbinsights_2026-04-24.db

    python jobs/export_data.py export
    python jobs/export_data.py export --output exports/sbinsights_2026-04-24.json
    python jobs/export_data.py export --tables sales_daily,invoices,invoice_lines

Both write to ./backups/ or ./exports/ by default if no path given.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Default DB path (matches what the API uses)
DEFAULT_DB = os.environ.get("TERROIR_DB", "db/sbinsights.db")


def list_tables(conn) -> list[str]:
    """Return all user tables (skip sqlite internal tables)."""
    cur = conn.cursor()
    cur.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
    """)
    return [r[0] for r in cur.fetchall()]


def table_summary(conn, table: str) -> dict:
    """Row count + size estimate for a table."""
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return {"table": table, "rows": cur.fetchone()[0]}


def cmd_backup(args) -> None:
    """Copy the SQLite file to a timestamped backup."""
    src = Path(args.db)
    if not src.exists():
        print(f"ERROR: database not found at {src}")
        sys.exit(1)

    if args.output:
        dest = Path(args.output)
    else:
        backup_dir = Path("backups")
        backup_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        dest = backup_dir / f"sbinsights_{ts}.db"

    dest.parent.mkdir(parents=True, exist_ok=True)

    # Use SQLite's online backup API so we don't corrupt anything if the DB
    # is being written to (the API server might be running). Falls back to
    # a regular file copy if that fails.
    try:
        with sqlite3.connect(src) as src_conn, sqlite3.connect(dest) as dest_conn:
            src_conn.backup(dest_conn)
        method = "online backup (safe with running server)"
    except Exception as e:
        # Fall back to file copy
        shutil.copy2(src, dest)
        method = f"file copy (fallback after: {e})"

    size_mb = dest.stat().st_size / 1024 / 1024
    print(f"✓ Backup written to {dest}")
    print(f"  Method: {method}")
    print(f"  Size:   {size_mb:.2f} MB")


def cmd_export(args) -> None:
    """Dump tables to JSON for cross-engine portability."""
    src = Path(args.db)
    if not src.exists():
        print(f"ERROR: database not found at {src}")
        sys.exit(1)

    if args.output:
        dest = Path(args.output)
    else:
        export_dir = Path("exports")
        export_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        dest = export_dir / f"sbinsights_{ts}.json"

    dest.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(src)
    conn.row_factory = sqlite3.Row

    all_tables = list_tables(conn)
    if args.tables:
        wanted = {t.strip() for t in args.tables.split(",")}
        tables = [t for t in all_tables if t in wanted]
        missing = wanted - set(all_tables)
        if missing:
            print(f"WARNING: tables not found: {sorted(missing)}")
    else:
        tables = all_tables

    payload = {
        "schema_version": 1,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "source_db": str(src.resolve()),
        "tables": {},
    }

    total_rows = 0
    for t in tables:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {t}")
        rows = [dict(r) for r in cur.fetchall()]
        payload["tables"][t] = {
            "row_count": len(rows),
            "rows": rows,
        }
        total_rows += len(rows)
        print(f"  exported {len(rows):>7} rows from {t}")

    with open(dest, "w", encoding="utf-8") as f:
        # default=str handles dates/decimals/etc that aren't JSON-native
        json.dump(payload, f, indent=2, default=str, ensure_ascii=False)

    size_mb = dest.stat().st_size / 1024 / 1024
    print()
    print(f"✓ Export written to {dest}")
    print(f"  Tables: {len(tables)}")
    print(f"  Total rows: {total_rows:,}")
    print(f"  Size:   {size_mb:.2f} MB")
    print()
    print("This file can be used to migrate to a different database engine later.")


def cmd_status(args) -> None:
    """Show a quick overview of what's in the database."""
    src = Path(args.db)
    if not src.exists():
        print(f"ERROR: database not found at {src}")
        sys.exit(1)

    size_mb = src.stat().st_size / 1024 / 1024
    print(f"Database: {src}")
    print(f"Size:     {size_mb:.2f} MB")
    print()
    conn = sqlite3.connect(src)
    print(f"{'Table':<24} {'Rows':>10}")
    print("-" * 36)
    total = 0
    for t in list_tables(conn):
        s = table_summary(conn, t)
        print(f"{s['table']:<24} {s['rows']:>10,}")
        total += s["rows"]
    print("-" * 36)
    print(f"{'TOTAL':<24} {total:>10,}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backup and export SB Insights data."
    )
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"path to SQLite database (default: {DEFAULT_DB})")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_backup = sub.add_parser("backup",
                              help="Copy DB to a timestamped .db file (recommended for local)")
    p_backup.add_argument("--output", help="Output path (default: backups/<timestamp>.db)")
    p_backup.set_defaults(func=cmd_backup)

    p_export = sub.add_parser("export",
                              help="Dump tables to JSON (use this for cross-engine migration)")
    p_export.add_argument("--output", help="Output path (default: exports/<timestamp>.json)")
    p_export.add_argument("--tables", help="Comma-separated table names to export (default: all)")
    p_export.set_defaults(func=cmd_export)

    p_status = sub.add_parser("status",
                              help="Show row counts per table")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

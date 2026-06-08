"""One-time cleanup: remove duplicate sale_lines rows from the 5/11-5/21 era.

Root cause: during early May 2026 we ran historical bulk imports on top of
the daily Cova exports. The daily exports were already in sale_lines; the
bulk imports added MORE rows for the same invoices but with different
line_no assignments. Result: same (invoice_no, sku, units, subtotal)
appearing 2x (or more) in sale_lines for those dates only.

sales_daily was unaffected (UNIQUE constraint on sku/loc/date — re-imports
just overwrite). discount_lines was unaffected for the same reason.

Symptom this fixes: Financial tab "Gross Sales" / "Net Sales" inflated by
~$5K per store on those dates. Post-dedup, sale_lines aggregates match Cova
"Sales by Location" within 1%.

Scope: only deletes WITHIN 5/11-5/21, only when (invoice_no, sku, units,
sold_price, subtotal) is identical. Keeps the lowest rowid per dup group.
Legit double-scans of the same item with same price (which Cova reports as
two line items) on OTHER dates are not touched.

Idempotent: safe to re-run; second run deletes 0 rows.

Run as terroir user (or root with sudo -u terroir) from /opt/terroir-ops:
    sudo -u terroir .venv/bin/python deploy/dedup_sale_lines.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB_PATH = REPO / "terroir.db"

# The known-bad date range from the historical-import incident.
START_DATE = "2026-05-11"
END_DATE = "2026-05-21"


def main() -> int:
    if not DB_PATH.exists():
        print(f"DB not found at {DB_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    cur = conn.cursor()

    # Pre-state
    pre_rows = cur.execute(
        "SELECT COUNT(*) FROM sale_lines WHERE sale_date >= ? AND sale_date <= ?",
        (START_DATE, END_DATE),
    ).fetchone()[0]
    pre_revenue = cur.execute(
        "SELECT COALESCE(SUM(subtotal),0) FROM sale_lines WHERE sale_date >= ? AND sale_date <= ?",
        (START_DATE, END_DATE),
    ).fetchone()[0]
    print(f"PRE  : {pre_rows:,} rows  ${pre_revenue:,.2f} subtotal "
          f"({START_DATE} to {END_DATE})")

    # Find candidate dup count
    extras = cur.execute(
        """SELECT COALESCE(SUM(c)-COUNT(*), 0) FROM (
               SELECT COUNT(*) c FROM sale_lines
               WHERE sale_date >= ? AND sale_date <= ?
               GROUP BY invoice_no, sku, units, sold_price, subtotal
               HAVING c > 1
           )""",
        (START_DATE, END_DATE),
    ).fetchone()[0]
    print(f"     : {extras:,} duplicate rows identified for removal")

    if extras == 0:
        print("Nothing to do — already deduped.")
        conn.close()
        return 0

    # The DELETE itself — keep MIN(rowid) per dup group.
    deleted = cur.execute(
        """DELETE FROM sale_lines
           WHERE sale_date >= ? AND sale_date <= ?
             AND rowid NOT IN (
                 SELECT MIN(rowid) FROM sale_lines
                 WHERE sale_date >= ? AND sale_date <= ?
                 GROUP BY invoice_no, sku, units, sold_price, subtotal
             )""",
        (START_DATE, END_DATE, START_DATE, END_DATE),
    ).rowcount
    conn.commit()
    print(f"DEL  : {deleted:,} rows deleted")

    # Post-state
    post_rows = cur.execute(
        "SELECT COUNT(*) FROM sale_lines WHERE sale_date >= ? AND sale_date <= ?",
        (START_DATE, END_DATE),
    ).fetchone()[0]
    post_revenue = cur.execute(
        "SELECT COALESCE(SUM(subtotal),0) FROM sale_lines WHERE sale_date >= ? AND sale_date <= ?",
        (START_DATE, END_DATE),
    ).fetchone()[0]
    print(f"POST : {post_rows:,} rows  ${post_revenue:,.2f} subtotal")
    print(f"     : removed ${pre_revenue - post_revenue:,.2f} of duplicate revenue")

    # Confidence check: residual duplicate count should be 0
    residual = cur.execute(
        """SELECT COALESCE(SUM(c)-COUNT(*), 0) FROM (
               SELECT COUNT(*) c FROM sale_lines
               WHERE sale_date >= ? AND sale_date <= ?
               GROUP BY invoice_no, sku, units, sold_price, subtotal
               HAVING c > 1
           )""",
        (START_DATE, END_DATE),
    ).fetchone()[0]
    print(f"CHECK: {residual} residual duplicates (should be 0)")
    conn.close()
    return 0 if residual == 0 else 2


if __name__ == "__main__":
    sys.exit(main())

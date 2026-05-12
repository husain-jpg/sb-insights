"""
Parse Cova Discounts report (.xlsx). Format:
- Sheet 'Parameters' has the date range parameters (we ignore these for now)
- Sheet 'Discounts' has the line-level data
- One row per (invoice, line, sku) where a discount was applied
- Has fields the Itemized Sales export does NOT: discount_reason, discount_type,
  authorizing employee — making this the ground-truth source for discount analysis.

Idempotent — re-importing same file replaces. Safe to drop daily exports.

Usage:
    from jobs.import_discounts import import_discounts_file
    result = import_discounts_file(conn, Path('Discounts_*.xlsx'))
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

# Filename pattern for Cova Discounts exports — accepts both .xlsx and .csv
DISCOUNTS_FILENAME_RE = re.compile(r"^Discounts.*\.(xlsx?|csv)$", re.IGNORECASE)

# Required columns the Discounts report must have. Used to validate
# files after metadata rows are skipped.
DISCOUNTS_REQUIRED_COLS = {
    "Discount Reason", "Discount Type", "Location", "Employee",
    "Invoice #", "Date", "SKU", "Product Discount Amount",
}


def is_discounts_file(file_path: Path) -> bool:
    """Quick filename-based detection, then verify by checking content.

    Discounts CSV exports from Cova have ~13 metadata rows above the real
    header (Parameters, Entities, Employee, etc.). Use _read_any() which
    auto-detects the header row by scanning for known Cova field names.
    """
    if not DISCOUNTS_FILENAME_RE.search(file_path.name):
        return False
    try:
        if file_path.suffix.lower() == ".csv":
            from jobs.import_cova_exports import _read_any
            df_head = _read_any(file_path, nrows=0)
            cols = set(df_head.columns)
            return DISCOUNTS_REQUIRED_COLS.issubset(cols)
        else:
            xl = pd.ExcelFile(file_path)
            return "Discounts" in xl.sheet_names
    except Exception:
        return False


def _read_discounts_dataframe(file_path: Path) -> pd.DataFrame:
    """Read the Discounts data into a DataFrame, regardless of format.

    For CSVs: uses _read_any() which auto-skips Cova's metadata header rows.
    For xlsx: reads the 'Discounts' sheet directly.
    """
    if file_path.suffix.lower() == ".csv":
        from jobs.import_cova_exports import _read_any
        return _read_any(file_path)
    else:
        return pd.read_excel(file_path, sheet_name="Discounts")


def import_discounts_file(conn, file_path: Path) -> dict:
    """Parse and persist a Cova Discounts export. Accepts .xlsx or .csv."""
    log.info("Reading discounts file: %s", file_path.name)
    df = _read_discounts_dataframe(file_path)
    log.info("  %d discount rows loaded", len(df))
    if len(df) == 0:
        log.info("  (empty discounts file)")
        return {"type": "discounts", "file_name": file_path.name,
                "rows": 0, "date_range": (None, None)}

    # Normalize types
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df["Quantity"] = pd.to_numeric(df["Quantity"], errors="coerce")
    df["Product Discount Amount"] = pd.to_numeric(
        df["Product Discount Amount"], errors="coerce"
    ).abs()  # store as positive (Cova stores as negative)

    # Map Cova location names to internal location_id (use existing helper)
    from jobs.import_cova_exports import ensure_location
    cova_locations = df["Location"].dropna().unique()
    location_map = {name: ensure_location(conn, name) for name in cova_locations}
    df["location_id"] = df["Location"].map(location_map)

    # Cova doesn't give us a per-invoice line_no for discounts. Synthesize one
    # from the row order within each invoice.
    df["line_no"] = df.groupby("Invoice #").cumcount() + 1

    # Date strings
    df["sale_date"] = df["Date"].dt.strftime("%Y-%m-%d")
    df["sale_datetime_str"] = df["Date"].dt.strftime("%Y-%m-%d %H:%M:%S")

    # Drop rows that can't be inserted (no invoice number)
    valid = df[df["Invoice #"].notna()].copy()

    rows = list(zip(
        valid["Invoice #"].astype(str),
        valid["line_no"].astype(int),
        valid["sale_date"],
        valid["sale_datetime_str"].where(valid["sale_datetime_str"].notna(), None),
        valid["location_id"].fillna(""),
        valid["SKU"].where(valid["SKU"].notna(), None),
        valid["Product"].where(valid["Product"].notna(), None),
        valid["Quantity"].where(valid["Quantity"].notna(), None),
        valid["Discount Type"].where(valid["Discount Type"].notna(), None),
        valid["Discount Reason"].where(valid["Discount Reason"].notna(), None),
        valid["Product Discount Amount"].fillna(0).astype(float),
        valid["Employee"].where(valid["Employee"].notna(), None),
        valid["Customer"].where(valid["Customer"].notna(), None),
    ))

    insert_sql = """
        INSERT INTO discount_lines (
            invoice_no, line_no, sale_date, sale_datetime, location_id,
            sku, product_name, quantity, discount_type, discount_reason,
            discount_amount, cashier, customer_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (invoice_no, line_no, sku) DO UPDATE SET
            sale_date = excluded.sale_date,
            sale_datetime = excluded.sale_datetime,
            location_id = excluded.location_id,
            product_name = excluded.product_name,
            quantity = excluded.quantity,
            discount_type = excluded.discount_type,
            discount_reason = excluded.discount_reason,
            discount_amount = excluded.discount_amount,
            cashier = excluded.cashier,
            customer_name = excluded.customer_name
    """

    cur = conn.cursor()
    chunk_size = 5000
    n = 0
    for i in range(0, len(rows), chunk_size):
        cur.executemany(insert_sql, rows[i:i+chunk_size])
        n += len(rows[i:i+chunk_size])
        conn.commit()

    return {
        "type": "discounts",
        "file_name": file_path.name,
        "rows": n,
        "date_range": (valid["Date"].min().date().isoformat() if n else None,
                       valid["Date"].max().date().isoformat() if n else None),
        "locations": list(location_map.values()),
    }

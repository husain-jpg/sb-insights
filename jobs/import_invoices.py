"""
Parse OCS delivery invoices (Sales Order / Customer Invoice .xlsx files).

OCS sends one .xlsx per delivery shipment. A single weekly order may produce
multiple invoices on the same delivery date if shipments are split. Each
invoice has a 7-row metadata block followed by line items.

Standard layout (verified across SO and CI prefixes):
    Row 5: "Customer account C00001696"  | "Invoice <NUMBER>"
    Row 6: "<Company name>"              | "<YYYY-MM-DD>"  (invoice date)
    Row 7: "<Street address>"
    Row 8: "<City, Province Postal>"     | "Payment by ..."
    Row 9: "<Fiscal period>"             | "<Total with tax>"
    Row 13: column headers (ITEM, DESCRIPTION, VARIANT, QUANTITY, ...)
    Row 14+: line items (variant ends with `_<size>___`, quantity in EA)

Idempotent: re-importing the same invoice replaces it. Safe to drop the
same .xlsx into imports/ multiple times.

Usage:
    from jobs.import_invoices import import_invoice_file
    result = import_invoice_file(conn, Path('Invoice_SO123.xlsx'))
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

# Filename pattern for invoices we recognize:
#   Invoice_SO006228059.xlsx, Invoice_CI006082724.xlsx
INVOICE_FILENAME_RE = re.compile(r"Invoice_[A-Z]{2}\d+\.xlsx?$", re.IGNORECASE)


def is_invoice_file(file_path: Path) -> bool:
    """Quick filename-based detection for the import dispatcher."""
    return bool(INVOICE_FILENAME_RE.search(file_path.name))


def _parse_quantity(q) -> Optional[int]:
    """Convert '12.00 EA' -> 12. Returns None if unparseable."""
    if pd.isna(q):
        return None
    s = str(q).replace("EA", "").strip()
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _parse_money(v) -> Optional[float]:
    """Handle '$1,234.56' style. Returns None if unparseable."""
    if pd.isna(v):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _resolve_location(conn, address_line: str) -> Optional[str]:
    """
    Match the invoice's city/province line against locations.name.
    OCS uses store-city names like 'Amherstview' or 'Toronto' which should
    be substrings of the location.name the user has set in the DB.

    Returns location_id (e.g. 'S1') or None if no match.
    """
    if not address_line:
        return None
    # OCS address format: "Amherstview, ON K7N1A6"
    city = address_line.split(",")[0].strip().lower() if "," in address_line else address_line.strip().lower()
    if not city:
        return None

    cur = conn.cursor()
    cur.execute("SELECT id, name FROM locations")
    candidates = [(r[0], (r[1] or "").lower()) for r in cur.fetchall()]
    # Prefer exact substring match — city should appear in the location name
    for loc_id, name in candidates:
        if city in name:
            return loc_id
    return None


def parse_invoice(file_path: Path) -> dict:
    """
    Read an OCS invoice .xlsx and extract metadata + line items.
    Returns a dict — does not touch the database.
    """
    df = pd.read_excel(file_path, sheet_name="Sheet1", header=None)
    if df.shape[0] < 14:
        raise ValueError(f"{file_path.name}: too short to be a valid OCS invoice")

    # Metadata block — assume standard layout, defensive on each cell
    def cell(r, c):
        try:
            v = df.iat[r, c]
            return None if pd.isna(v) else str(v).strip()
        except (IndexError, KeyError):
            return None

    invoice_no_raw = cell(5, 2) or ""
    invoice_no = invoice_no_raw.replace("Invoice", "").strip()
    if not invoice_no:
        raise ValueError(f"{file_path.name}: invoice number not found at row 5 col 2")

    customer_account_raw = cell(5, 0) or ""
    customer_account = customer_account_raw.replace("Customer account", "").strip()

    invoice_date_raw = cell(6, 2)
    if not invoice_date_raw:
        raise ValueError(f"{file_path.name}: invoice date not found at row 6 col 2")
    # Normalize to YYYY-MM-DD
    try:
        # Could be a Timestamp string or ISO date
        invoice_date = pd.to_datetime(invoice_date_raw).strftime("%Y-%m-%d")
    except Exception:
        invoice_date = invoice_date_raw[:10]  # best effort

    address_line = cell(8, 0)  # "Amherstview, ON K7N1A6"
    fiscal_period = cell(9, 0)
    total_with_tax = _parse_money(cell(9, 2))

    # Line items — header is at row 13, data from row 14
    line_df = df.iloc[14:].copy()
    line_df.columns = ["ITEM", "DESCRIPTION", "VARIANT", "QUANTITY", "UNIT_PRICE", "_skip", "AMOUNT"]
    # Filter out blank rows and the trailing "HST #" footer
    line_df = line_df[line_df["VARIANT"].notna() & line_df["ITEM"].notna()]
    # The HST footer row has ITEM like "HST # 770238319" — exclude
    line_df = line_df[~line_df["ITEM"].astype(str).str.startswith("HST", na=False)]

    lines = []
    units_total = 0
    subtotal = 0.0
    for _, row in line_df.iterrows():
        units = _parse_quantity(row["QUANTITY"])
        if units is None:
            continue
        unit_price = _parse_money(row["UNIT_PRICE"])
        amt = _parse_money(row["AMOUNT"])
        variant = str(row["VARIANT"]).strip()
        desc = str(row["DESCRIPTION"]).strip() if pd.notna(row["DESCRIPTION"]) else None
        lines.append({
            "ocs_variant": variant,
            "description": desc,
            "units_delivered": units,
            "unit_price": unit_price,
            "line_total": amt or 0.0,
        })
        units_total += units
        subtotal += (amt or 0.0)

    return {
        "invoice_no": invoice_no,
        "invoice_date": invoice_date,
        "customer_account": customer_account,
        "address_line": address_line,
        "fiscal_period": fiscal_period,
        "subtotal": round(subtotal, 2),
        "total_with_tax": total_with_tax,
        "line_count": len(lines),
        "units_total": units_total,
        "lines": lines,
    }


def import_invoice_file(conn, file_path: Path) -> dict:
    """
    Parse and persist an invoice. Idempotent — re-importing replaces the
    invoice and its lines.

    Returns a result dict with stats. If the invoice's address can't be
    matched to a known location, the invoice is still saved with
    location_id=NULL and `unmatched_location` is set in the result.
    """
    parsed = parse_invoice(file_path)
    location_id = _resolve_location(conn, parsed["address_line"] or "")

    cur = conn.cursor()
    # Upsert the invoice header
    cur.execute("""
        INSERT INTO invoices (
            invoice_no, location_id, invoice_date, customer_account,
            fiscal_period, subtotal, total_with_tax, line_count, units_total,
            source_filename
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (invoice_no) DO UPDATE SET
            location_id = excluded.location_id,
            invoice_date = excluded.invoice_date,
            customer_account = excluded.customer_account,
            fiscal_period = excluded.fiscal_period,
            subtotal = excluded.subtotal,
            total_with_tax = excluded.total_with_tax,
            line_count = excluded.line_count,
            units_total = excluded.units_total,
            source_filename = excluded.source_filename,
            imported_at = CURRENT_TIMESTAMP
    """, (
        parsed["invoice_no"], location_id, parsed["invoice_date"],
        parsed["customer_account"], parsed["fiscal_period"],
        parsed["subtotal"], parsed["total_with_tax"],
        parsed["line_count"], parsed["units_total"],
        file_path.name,
    ))

    # Replace lines (delete + insert is simpler than per-row upsert here)
    cur.execute("DELETE FROM invoice_lines WHERE invoice_no = ?", (parsed["invoice_no"],))
    cur.executemany("""
        INSERT INTO invoice_lines (invoice_no, ocs_variant, description,
                                   units_delivered, unit_price, line_total)
        VALUES (?, ?, ?, ?, ?, ?)
    """, [
        (parsed["invoice_no"], l["ocs_variant"], l["description"],
         l["units_delivered"], l["unit_price"], l["line_total"])
        for l in parsed["lines"]
    ])
    conn.commit()

    return {
        "type": "invoice",
        "file_name": file_path.name,
        "invoice_no": parsed["invoice_no"],
        "invoice_date": parsed["invoice_date"],
        "location_id": location_id,
        "address_line": parsed["address_line"],
        "subtotal": parsed["subtotal"],
        "total_with_tax": parsed["total_with_tax"],
        "lines": parsed["line_count"],
        "units": parsed["units_total"],
        "unmatched_location": location_id is None,
    }

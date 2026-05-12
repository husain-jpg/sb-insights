"""
Parse data partner buysheets (currently: Canna Collective monthly buysheets).

Format observed (Canna Collective):
- Sheet 'Sheet1' with metadata in rows 0-7 and column headers in row 8
- Row 5 col 3 contains the period label (e.g., "May 1st - May 31st")
- Filename also typically contains the period (e.g., "May_1_-_May_31")
- Data columns: Licensed Producer Name, Sub Category, SKU, Item Name, Brand, Data Fee %, *
- The "*" column has free-text flags (Gold Tier / High Data Fee / New Sku / etc.)

Behavior:
- Auto-creates "Canna Collective" brand_partner if missing
- Creates one data_revenue_deals row per buysheet SKU
- Period dates derived from filename or sheet header
- Idempotent: re-importing same period replaces all deals for that period
- SKU values from buysheet are OCS variant numbers (e.g., "322017_355ml___"),
  which match the ocs_variant_number field in our products table

Usage:
    from jobs.import_buysheet import import_buysheet_file
    result = import_buysheet_file(conn, Path('Canna_Collective_-_Buy_Sheet_..._May_1_-_May_31...xlsx'))
"""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

# Filename pattern: "Canna_Collective" or other partner buysheet
BUYSHEET_FILENAME_RE = re.compile(
    r"(canna_collective|buysheet|buy_sheet|buy.sheet)",
    re.IGNORECASE,
)

# Period pattern: matches "May 1st - May 31st", "May_1_-_May_31", etc.
_MONTHS = (
    "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|"
    "January|February|March|April|June|July|August|September|"
    "October|November|December"
)
PERIOD_RE = re.compile(
    rf"({_MONTHS})\s*_?(\d+)(?:st|nd|rd|th)?\s*_?-_?\s*({_MONTHS})\s*_?(\d+)(?:st|nd|rd|th)?",
    re.IGNORECASE,
)

_MONTH_NUM = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "september": 9, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def is_buysheet_file(file_path: Path) -> bool:
    """Quick filename detection. Sheet structure verified at import time."""
    return bool(BUYSHEET_FILENAME_RE.search(file_path.name))


def _extract_period(file_path: Path, df_raw: pd.DataFrame) -> tuple[date, date] | None:
    """
    Try to extract (start_date, end_date) from filename first, then from
    the metadata rows of the sheet. Returns None if nothing parses cleanly.
    Year is inferred from current year unless the period seems to be in the past
    by more than 6 months, in which case it's pushed to next year.
    """
    candidates = [file_path.name]
    # Scan metadata rows of the sheet (first 10 rows, all columns)
    for i in range(min(10, len(df_raw))):
        for c in range(len(df_raw.columns)):
            v = df_raw.iat[i, c]
            if pd.notna(v):
                candidates.append(str(v))

    for text in candidates:
        m = PERIOD_RE.search(text)
        if not m:
            continue
        start_month_str, start_day_str, end_month_str, end_day_str = m.groups()
        start_month = _MONTH_NUM.get(start_month_str.lower())
        end_month = _MONTH_NUM.get(end_month_str.lower())
        if not (start_month and end_month):
            continue

        # Year inference: assume current year. If the resulting end date is
        # more than 6 months in the past, bump year forward.
        today = date.today()
        year = today.year
        try:
            start = date(year, start_month, int(start_day_str))
            end = date(year, end_month, int(end_day_str))
        except ValueError:
            continue
        # Period crosses year boundary (e.g., Dec 15 - Jan 14)
        if end < start:
            end = date(year + 1, end_month, int(end_day_str))
        # Bump year if everything's in the past
        if (today - end).days > 180:
            try:
                start = date(year + 1, start_month, int(start_day_str))
                end_year = year + 1 if end_month >= start_month else year + 2
                end = date(end_year, end_month, int(end_day_str))
            except ValueError:
                pass
        return start, end

    return None


def _ensure_brand_partner(conn, name: str) -> int:
    """Get or create a brand_partner. Returns id."""
    cur = conn.cursor()
    cur.execute("SELECT id FROM brand_partners WHERE brand_name = ?", (name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO brand_partners (brand_name, notes, is_active) VALUES (?, ?, 1)",
        (name, "Auto-created from buysheet import"),
    )
    conn.commit()
    return cur.lastrowid


def import_buysheet_file(conn, file_path: Path) -> dict:
    """
    Parse and persist a Canna Collective (or similar) buysheet.

    Returns dict with stats. Raises ValueError on unrecoverable format issues
    (which the dispatcher catches and logs).
    """
    log.info("Reading buysheet: %s", file_path.name)

    # Read with no header to extract metadata, then re-read with proper header
    df_raw = pd.read_excel(file_path, sheet_name=0, header=None)
    period = _extract_period(file_path, df_raw)
    if not period:
        raise ValueError(
            f"Could not extract period dates from {file_path.name}. "
            f"Expected something like 'May 1 - May 31' in filename or sheet."
        )
    start_date, end_date = period

    # Read with header at row 8 (0-indexed)
    df = pd.read_excel(file_path, sheet_name=0, skiprows=8)
    df.columns = [str(c).strip() for c in df.columns]

    # Validate required columns
    required = ["SKU", "Item Name", "Brand", "Data Fee %"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(
            f"Buysheet {file_path.name} missing required columns: {missing}. "
            f"Got: {list(df.columns)}"
        )

    # Drop rows with missing critical fields
    df = df.dropna(subset=["SKU", "Data Fee %"]).copy()
    df["Data Fee %"] = pd.to_numeric(df["Data Fee %"], errors="coerce")
    df = df[df["Data Fee %"].notna()]

    # Convert fee from decimal (0.06) to percentage (6.0) — our schema stores percentages
    df["fee_pct"] = df["Data Fee %"] * 100

    if len(df) == 0:
        raise ValueError(f"No valid SKU rows in {file_path.name}")

    # Get or create the partner
    partner_name = "Canna Collective"
    brand_id = _ensure_brand_partner(conn, partner_name)

    # Idempotency: clear existing deals for this partner + period before re-inserting.
    # Use exact period match — if the user re-imports the same buysheet, we replace.
    cur = conn.cursor()
    cur.execute("""
        DELETE FROM data_revenue_deals
        WHERE brand_id = ? AND start_date = ? AND end_date = ?
    """, (brand_id, start_date.isoformat(), end_date.isoformat()))
    deleted = cur.rowcount

    # Build notes column from item name + flag column if present
    flag_col = "*" if "*" in df.columns else None
    has_lp_col = "Licensed Producer Name" in df.columns

    inserted = 0
    lp_updates = 0
    for _, r in df.iterrows():
        sku = str(r["SKU"]).strip()
        fee = float(r["fee_pct"])
        item_name = str(r["Item Name"]).strip()
        brand = str(r["Brand"]).strip() if pd.notna(r.get("Brand")) else ""
        lp = str(r["Licensed Producer Name"]).strip() if has_lp_col and pd.notna(r.get("Licensed Producer Name")) else ""
        flag = str(r[flag_col]).strip() if flag_col and pd.notna(r.get(flag_col)) else ""

        # Compose notes: item name, brand, lp, flag
        note_parts = [item_name]
        if brand:
            note_parts.append(f"Brand: {brand}")
        if lp:
            note_parts.append(f"LP: {lp}")
        if flag:
            note_parts.append(f"Flag: {flag}")
        notes = " | ".join(note_parts)

        cur.execute("""
            INSERT INTO data_revenue_deals
                (brand_id, start_date, end_date, percentage, basis, sku_filter, notes)
            VALUES (?, ?, ?, ?, 'retail_sales', ?, ?)
        """, (
            brand_id,
            start_date.isoformat(),
            end_date.isoformat(),
            fee,
            sku,  # one SKU per row in sku_filter (per-SKU deal)
            notes,
        ))
        inserted += 1

        # Update LP on products if SKU exists and LP not yet set
        if lp:
            cur.execute("""
                UPDATE products SET lp = ?
                WHERE ocs_variant_number = ? AND (lp IS NULL OR lp = '')
            """, (lp, sku))
            if cur.rowcount > 0:
                lp_updates += 1

    conn.commit()

    return {
        "type": "buysheet",
        "file_name": file_path.name,
        "partner": partner_name,
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "deals_replaced": deleted,
        "deals_inserted": inserted,
        "lp_updates": lp_updates,
        "skus_total": len(df),
    }

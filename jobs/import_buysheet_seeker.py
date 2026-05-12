"""
Parse Seeker monthly buysheet for Ontario.

File shape observed (Seeker__April_ON.xlsx, Seeker__May_ON.xlsx, etc.):
- Single sheet 'Sheet1'
- Header at row 0, data starts row 1
- Key columns:
    Status (Active/ACTIVE), Seeker Code, Seeker Base Program Eligible,
    Seeker Base SKU Code, Province, Sub-Category, OCSSKU, Brand, Product,
    THC Min, THC Max, Wholesale, Case Pack
- Rate is encoded INSIDE the Seeker Code:
    'DC2.6_1SPL_ON' → 2.6% (digits after 'DC' before '_')
- Per user's direction: use the active 'Seeker Code' rate.
  The 'Seeker Base SKU Code' represents a fallback rate; we surface in notes
  but don't use it for active-rate computation.
- Basis is wholesale cost.
- Status filter: only 'Active' / 'ACTIVE'.
- Province filter: only 'ON' for our use.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

PARTNER_NAME = "Seeker"

SEEKER_FILENAME_RE = re.compile(r"seeker.*\.xlsx?$", re.IGNORECASE)

# Seeker code: e.g. 'DC2.6_1SPL_ON', 'DC10_FUEG_ON', 'DC.7_BIG_ON' (=0.7), 'DC2._WEED_ON' (=2)
SEEKER_RATE_RE = re.compile(r"DC(\d*\.?\d*)_", re.IGNORECASE)


def _parse_seeker_rate(code: str) -> float | None:
    """Extract rate from Seeker Code, handling edge cases like DC.7 and DC2."""
    m = SEEKER_RATE_RE.search(code)
    if not m:
        return None
    digits = m.group(1)
    # Normalize: '.7' → '0.7', '2.' → '2', '' → None
    if not digits or digits == '.':
        return None
    if digits.startswith('.'):
        digits = '0' + digits
    if digits.endswith('.'):
        digits = digits[:-1]
    try:
        return float(digits)
    except ValueError:
        return None

# Filename month detection: 'Seeker__April_ON.xlsx', 'Seeker__May_ON.xlsx'
SEEKER_MONTH_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|"
    r"September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)",
    re.IGNORECASE,
)
_MONTH_NUM = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "september": 9, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def is_seeker_file(file_path: Path) -> bool:
    # Discriminate from the rates-registry file by checking content later;
    # the per-month buysheet has 'Seeker Code' column, the rates registry has 'Data Code'.
    if not SEEKER_FILENAME_RE.search(file_path.name):
        return False
    # Heuristic: require a month name in the filename for buysheets specifically
    # (rates registry filename is 'Seeker_Brand_Partner_and_Rates_April.xlsx' which
    # also has 'April' — so we'll need column-based discrimination)
    return True


def _is_buysheet_columns(df_cols: list[str]) -> bool:
    """The buysheet has 'Seeker Code' and 'OCSSKU'; the rates registry doesn't."""
    return "Seeker Code" in df_cols and "OCSSKU" in df_cols


def _ensure_brand_partner(conn, name: str) -> int:
    cur = conn.cursor()
    cur.execute("SELECT id FROM brand_partners WHERE brand_name = ?", (name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        """INSERT INTO brand_partners
           (brand_name, partner_type, is_direct_deal, notes, is_active)
           VALUES (?, 'brand', 0, ?, 1)""",
        (name, "Auto-created from Seeker buysheet import"),
    )
    conn.commit()
    return cur.lastrowid


def _detect_period(file_path: Path) -> tuple[date, date] | None:
    m = SEEKER_MONTH_RE.search(file_path.stem)
    if not m:
        return None
    month_num = _MONTH_NUM.get(m.group(1).lower())
    if not month_num:
        return None
    today = date.today()
    year = today.year
    # Year inference: same logic as buysheet — assume current year, bump if past
    start = date(year, month_num, 1)
    if month_num == 12:
        end = date(year, 12, 31)
    else:
        from datetime import timedelta
        end = date(year, month_num + 1, 1) - timedelta(days=1)
    if (today - end).days > 180:
        start = date(year + 1, month_num, 1)
        if month_num == 12:
            end = date(year + 1, 12, 31)
        else:
            from datetime import timedelta
            end = date(year + 1, month_num + 1, 1) - timedelta(days=1)
    return start, end


def import_seeker_file(conn, file_path: Path) -> dict:
    """Parse and persist a Seeker monthly buysheet."""
    log.info("Reading Seeker buysheet: %s", file_path.name)
    df = pd.read_excel(file_path, sheet_name=0)
    df.columns = [str(c).strip() for c in df.columns]

    if not _is_buysheet_columns(list(df.columns)):
        raise ValueError(
            f"{file_path.name} doesn't look like a Seeker buysheet "
            f"(missing 'Seeker Code' or 'OCSSKU'). Got: {list(df.columns)[:10]}"
        )

    period = _detect_period(file_path)
    if not period:
        raise ValueError(
            f"Could not detect month from filename {file_path.name}. "
            f"Expected something like 'Seeker__April_ON.xlsx'."
        )
    start_date, end_date = period

    # Filter to ON + Active rows
    df = df[df["Province"].astype(str).str.upper() == "ON"]
    df = df[df["Status"].astype(str).str.upper() == "ACTIVE"]
    df = df.dropna(subset=["Seeker Code", "OCSSKU"]).copy()

    # Extract rate from Seeker Code
    df["rate_pct"] = df["Seeker Code"].astype(str).apply(_parse_seeker_rate)
    parse_fail = df["rate_pct"].isna().sum()
    df = df[df["rate_pct"].notna()].copy()

    if parse_fail > 0:
        log.warning("  skipped %d rows with un-parseable Seeker Codes", int(parse_fail))

    if len(df) == 0:
        raise ValueError(f"No valid rows after filtering in {file_path.name}")

    brand_id = _ensure_brand_partner(conn, PARTNER_NAME)

    # Idempotency
    cur = conn.cursor()
    cur.execute("""
        DELETE FROM data_revenue_deals
        WHERE brand_id = ? AND start_date = ? AND end_date = ?
    """, (brand_id, start_date.isoformat(), end_date.isoformat()))
    deleted = cur.rowcount

    # Insert deals
    inserted = 0
    lp_updates = 0
    has_base_col = "Seeker Base SKU Code" in df.columns
    for _, r in df.iterrows():
        sku = str(r["OCSSKU"]).strip()
        rate = float(r["rate_pct"])
        brand = str(r["Brand"]).strip() if pd.notna(r.get("Brand")) else None
        product = str(r["Product"]).strip() if pd.notna(r.get("Product")) else ""

        notes_parts = [product]
        if brand: notes_parts.append(f"Brand: {brand}")
        notes_parts.append(f"Code: {r['Seeker Code']}")
        if has_base_col and pd.notna(r.get("Seeker Base SKU Code")):
            notes_parts.append(f"BaseCode: {r['Seeker Base SKU Code']}")
        notes = " | ".join(notes_parts)

        cur.execute("""
            INSERT INTO data_revenue_deals
                (brand_id, start_date, end_date, percentage, basis, sku_filter, notes)
            VALUES (?, ?, ?, ?, 'wholesale_cost', ?, ?)
        """, (brand_id, start_date.isoformat(), end_date.isoformat(),
              rate, sku, notes))
        inserted += 1
        # Note: Seeker file doesn't have an LP column directly — LPs come from IRC/CC

    conn.commit()

    return {
        "type": "buysheet_seeker",
        "file_name": file_path.name,
        "partner": PARTNER_NAME,
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "deals_replaced": deleted,
        "deals_inserted": inserted,
        "lp_updates": lp_updates,
        "skipped_parse_fail": int(parse_fail),
    }

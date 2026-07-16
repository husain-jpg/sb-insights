"""
Parse IRC (Inner Spirit / IRCC) monthly master buysheet for Ontario.

File shape observed (ON_Master_ALL_2026.xlsx):
- Multi-sheet workbook with one tab per month: 'May 2026 General Listings Works',
  'April 2026 General Listings Works', etc., plus 'Bundles' and 'Holiday Campaign'.
- For now we only ingest General Listings Works for the target month.
- Column header row is row 9 (0-indexed), data starts at row 10.
- Key columns:
    LP, Brand, Product, Data Quality Index (DQI), Unit Cost (excl. HST),
    Category, Province, Provincial SKU (= OCS variant number), GTIN, ...
- Rate is encoded INSIDE the DQI string:
    'Avi_010825_10BUC_712' → 10% (rate is the digits between underscore and the 4-letter code)
    'Avi_010825_7.00LIL_341' → 7%
    'See Bundles' → no rate (deal lives in Bundles tab; we skip)
- Basis is wholesale cost (% of unit cost).
- LP is captured and persisted to products.lp for hierarchy resolution.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

PARTNER_NAME = "IRCC"

IRC_FILENAME_RE = re.compile(r"(ircc?|inner.?spirit|on[\s_-]?master)", re.IGNORECASE)

# DQI rate extraction. Format: <Brand3>_<DDMMYY>_<RATE><CODE>_<NUMBER>
# Two-step approach handles edge cases cleanly:
#   1. Extract the chunk between the 2nd and 3rd underscores
#   2. Pull leading digits as rate; if followed by an ordinal (ST/ND/RD/TH),
#      the last digit belongs to the ordinal not the rate.
# Examples:
#   'Avi_010825_10BUC_712' → 10
#   'Avi_010825_7.00LIL_341' → 7
#   'Avi_010825_101ST_511' → 10  (rate 10, code "1ST")
#   'Avi_010825_104TH_858' → 10  (rate 10, code "4TH")
#   'Ele_010526_10LA _145' → 10  (trailing space in code)
DQI_CHUNK_RE = re.compile(r"^[A-Za-z]+_\d+_([^_]+)_\d", re.IGNORECASE)
DQI_NUM_RE = re.compile(r"^(\d+(?:\.\d+)?)([A-Z].*)$", re.IGNORECASE)
DQI_ORDINAL_RE = re.compile(r"^(ST|ND|RD|TH)([A-Z]|$)", re.IGNORECASE)


def parse_dqi_rate(dqi: str) -> float | None:
    """Extract rebate percentage from a DQI code, or None if unparseable.

    Defensive guard: any rate >= 100% is treated as a parse failure. IRC
    rebates are always in 0-99 range; values above that come from chunks
    like '0410X' where the regex grabs '0410' as a 4-digit numeric prefix
    that isn't really a rate. Better to skip + log than to silently load
    a 410% discount.
    """
    m = DQI_CHUNK_RE.search(dqi)
    if not m:
        return None
    chunk = m.group(1).strip()
    m2 = DQI_NUM_RE.match(chunk)
    if not m2:
        return None
    rate_str, rest = m2.group(1), m2.group(2)
    # Ordinal-suffix rule: if rest is an ordinal (ST/ND/RD/TH followed by a
    # letter or end-of-string), the last digit of rate_str belongs to the
    # ordinal. Don't strip if rate contains a decimal (those are unambiguous).
    if DQI_ORDINAL_RE.match(rest) and len(rate_str) > 1 and '.' not in rate_str:
        rate_str = rate_str[:-1]
    try:
        rate = float(rate_str)
    except ValueError:
        return None
    # Sanity guard — reject obviously-malformed extractions
    if rate >= 100:
        return None
    return rate


def is_irc_file(file_path: Path) -> bool:
    return bool(IRC_FILENAME_RE.search(file_path.name))


_MONTH_NAMES = {
    1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
    7: "July", 8: "August", 9: "September", 10: "October", 11: "November", 12: "December",
}
_MONTH_LOOKUP = {name.lower(): num for num, name in _MONTH_NAMES.items()}

# Sheet name like "May 2026 General Listings Works" or truncated "May 2026 General Listings Wor"
SHEET_MONTH_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{4})\s+General\s+Listings\s+Wor",
    re.IGNORECASE,
)


def _parse_sheet_period(sheet_name: str) -> tuple[int, int] | None:
    """Extract (month, year) from a sheet name like 'May 2026 General Listings Works'."""
    m = SHEET_MONTH_RE.match(sheet_name.strip())
    if not m:
        return None
    month = _MONTH_LOOKUP.get(m.group(1).lower())
    year = int(m.group(2))
    return (month, year) if month else None


def _select_latest_sheet(xl: pd.ExcelFile) -> tuple[str, int, int] | None:
    """Pick the MOST RECENT General Listings Works sheet by parsed date.

    Returns (sheet_name, month, year) or None.

    Period stamping does NOT come from the sheet — each row has explicit
    Offer Start / Offer End columns. The sheet picker just chooses the
    freshest snapshot of the IRC deal catalogue.
    """
    candidates: list[tuple[int, int, str]] = []  # (year, month, sheet_name)
    for sheet in xl.sheet_names:
        parsed = _parse_sheet_period(sheet)
        if parsed is None:
            continue
        month, year = parsed
        candidates.append((year, month, sheet))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    year, month, sheet = candidates[0]
    return (sheet, month, year)


def _coerce_offer_date(value) -> tuple[str | None, bool]:
    """Parse a cell from the Offer Start / Offer End columns.

    Returns (iso_date_or_None, is_ongoing).
    - Datetime / date / pandas Timestamp → ISO string, is_ongoing=False
    - 'Ongoing' (any case) or blank → (None, True if 'ongoing' else False)
    - Anything else → (None, False)
    """
    if value is None:
        return (None, False)
    # pandas-style NaN
    try:
        if pd.isna(value):
            return (None, False)
    except (TypeError, ValueError):
        pass
    # String 'Ongoing'
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("ongoing", "open", "indefinite", ""):
            return (None, s == "ongoing" or s in ("open", "indefinite"))
        # Try parsing as a date string
        try:
            ts = pd.to_datetime(value, errors="coerce")
            if pd.isna(ts):
                return (None, False)
            return (ts.date().isoformat(), False)
        except Exception:
            return (None, False)
    # datetime / date / Timestamp
    try:
        if hasattr(value, "date"):
            return (value.date().isoformat(), False)
        return (str(value)[:10], False)
    except Exception:
        return (None, False)


def _ensure_brand_partner(conn, name: str) -> int:
    """Get or create a brand_partners row. Returns id."""
    cur = conn.cursor()
    cur.execute("SELECT id FROM brand_partners WHERE brand_name = ?", (name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        """INSERT INTO brand_partners
           (brand_name, partner_type, is_direct_deal, notes, is_active)
           VALUES (?, 'brand', 0, ?, 1)""",
        (name, "Auto-created from IRC buysheet import"),
    )
    conn.commit()
    return cur.lastrowid


def _next_month_period(target_month: int, target_year: int) -> tuple[date, date]:
    """First and last day of the target month."""
    start = date(target_year, target_month, 1)
    if target_month == 12:
        end = date(target_year, 12, 31)
    else:
        next_first = date(target_year, target_month + 1, 1)
        end = date(next_first.year, next_first.month, next_first.day - 1) \
              if next_first.day > 1 else next_first  # safety
        # simpler: subtract 1 day from next month's 1st
        from datetime import timedelta
        end = next_first - timedelta(days=1)
    return start, end


def import_irc_file(conn, file_path: Path,
                    target_month: int | None = None,
                    target_year: int | None = None) -> dict:
    """Import IRC General Listings — each row's Offer Start / Offer End columns
    drive the deal validity window. The sheet picker only chooses the freshest
    snapshot of the catalogue; per-row dates are authoritative.

    Each upload is treated as the canonical IRCC snapshot — all existing IRCC
    deals are deleted and replaced. To target a non-latest sheet for testing,
    pass target_month/target_year (otherwise the most recent sheet is used).

    Returns stats dict.
    """
    import warnings
    warnings.filterwarnings("ignore", category=UserWarning)  # openpyxl noise on this file

    log.info("Reading IRC buysheet: %s", file_path.name)

    xl = pd.ExcelFile(file_path)

    # Pick which sheet to load
    if target_month is not None and target_year is not None:
        # Caller-specified — used for testing or one-off retro imports
        target_label = f"{_MONTH_NAMES[target_month]} {target_year}".lower()
        sheet_name = None
        for sheet in xl.sheet_names:
            sl = sheet.lower()
            if target_label in sl and "general listings" in sl and "works" in sl:
                sheet_name = sheet
                break
        if not sheet_name:
            raise ValueError(
                f"No {_MONTH_NAMES[target_month]} {target_year} General Listings "
                f"Works sheet in {file_path.name}. Sheets: {xl.sheet_names}"
            )
        sheet_month, sheet_year = target_month, target_year
        log.info("  using sheet (caller-specified): %s", sheet_name)
    else:
        selection = _select_latest_sheet(xl)
        if not selection:
            raise ValueError(
                f"No General Listings Works sheet found in {file_path.name}. "
                f"Sheets: {xl.sheet_names}"
            )
        sheet_name, sheet_month, sheet_year = selection
        log.info("  using latest available sheet: %s (%s %d)",
                 sheet_name, _MONTH_NAMES[sheet_month], sheet_year)

    # Header at row 9
    df = pd.read_excel(file_path, sheet_name=sheet_name, header=9)
    df.columns = [str(c).strip() for c in df.columns]

    # Required columns — now including per-row date columns
    required = ["LP", "Brand", "Product", "Data Quality Index (DQI)",
                "Offer Start", "Offer End"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(
            f"IRC buysheet missing required columns: {missing}. "
            f"Got: {list(df.columns)[:18]}..."
        )

    # SKU column varies — try common names
    sku_col = None
    for candidate in ("Provincial SKU", "OCSSKU", "SKU"):
        if candidate in df.columns:
            sku_col = candidate
            break
    if not sku_col:
        raise ValueError(f"IRC buysheet has no recognized SKU column. Got: {list(df.columns)[:15]}")

    # Drop rows missing critical fields (Offer Start is required; Offer End may be 'Ongoing'/blank)
    df = df.dropna(subset=["LP", "Brand", "Data Quality Index (DQI)", sku_col, "Offer Start"]).copy()

    # Extract rate from DQI
    df["dqi_str"] = df["Data Quality Index (DQI)"].astype(str).str.strip()
    df["rate_pct"] = df["dqi_str"].apply(parse_dqi_rate)

    # Skip rows with no parseable rate (e.g., "See Bundles", empty, malformed)
    bundles_count = (df["dqi_str"].str.lower() == "see bundles").sum()
    parse_fail = df["rate_pct"].isna().sum() - bundles_count
    df = df[df["rate_pct"].notna()].copy()

    if bundles_count:
        log.info("  skipped %d 'See Bundles' rows (bundles tab not yet supported)", bundles_count)
    if parse_fail > 0:
        log.warning("  skipped %d rows with un-parseable DQI codes", int(parse_fail))

    if len(df) == 0:
        raise ValueError(f"No valid rows after parsing rates from {file_path.name}")

    # Get partner id
    brand_id = _ensure_brand_partner(conn, PARTNER_NAME)

    # Idempotency: this upload is the authoritative IRC snapshot — clear ALL
    # existing IRCC deals before re-inserting from this file.
    cur = conn.cursor()
    from jobs.deal_archive import archive_deals
    archive_deals(conn, "d.brand_id = ?", (brand_id,), file_path.name)
    cur.execute("DELETE FROM data_revenue_deals WHERE brand_id = ?", (brand_id,))
    deleted = cur.rowcount

    # Insert deals (with per-row dates) + capture LP info on products
    inserted = 0
    ongoing_count = 0
    bad_start_count = 0
    lp_updates = 0
    seen_starts: set[str] = set()
    seen_ends: set[str] = set()

    for _, r in df.iterrows():
        sku = str(r[sku_col]).strip()
        rate = float(r["rate_pct"])
        lp = str(r["LP"]).strip() if pd.notna(r.get("LP")) else None
        brand = str(r["Brand"]).strip() if pd.notna(r.get("Brand")) else None
        product = str(r["Product"]).strip() if pd.notna(r.get("Product")) else ""

        # Per-row dates — Offer Start MUST parse, Offer End may be NULL ('Ongoing')
        start_iso, _ = _coerce_offer_date(r["Offer Start"])
        if start_iso is None:
            bad_start_count += 1
            continue
        end_iso, is_ongoing = _coerce_offer_date(r["Offer End"])
        if is_ongoing:
            ongoing_count += 1
        # else: end_iso may be None (blank/unparseable) — treat as no expiry too

        seen_starts.add(start_iso)
        if end_iso:
            seen_ends.add(end_iso)

        notes_parts = [product]
        if brand: notes_parts.append(f"Brand: {brand}")
        if lp:    notes_parts.append(f"LP: {lp}")
        notes_parts.append(f"DQI: {r['dqi_str']}")
        notes = " | ".join(notes_parts)

        cur.execute("""
            INSERT INTO data_revenue_deals
                (brand_id, start_date, end_date, percentage, basis, sku_filter, notes)
            VALUES (?, ?, ?, ?, 'wholesale_cost', ?, ?)
        """, (brand_id, start_iso, end_iso, rate, sku, notes))
        inserted += 1

        # Update LP on products table if SKU exists
        if lp:
            cur.execute("""
                UPDATE products SET lp = ?
                WHERE ocs_variant_number = ? AND (lp IS NULL OR lp = '')
            """, (lp, sku))
            if cur.rowcount > 0:
                lp_updates += 1

    if bad_start_count:
        log.warning("  skipped %d rows with unparseable Offer Start", bad_start_count)

    conn.commit()

    log.info(
        "  IRCC import: deleted %d prior, inserted %d new (%d ongoing); "
        "Offer Start range: %s ... %s; Offer End range: %s ... %s",
        deleted, inserted, ongoing_count,
        min(seen_starts) if seen_starts else "?",
        max(seen_starts) if seen_starts else "?",
        min(seen_ends) if seen_ends else "?",
        max(seen_ends) if seen_ends else "(ongoing only)",
    )

    return {
        "type": "buysheet_irc",
        "file_name": file_path.name,
        "partner": PARTNER_NAME,
        "sheet": sheet_name,
        "sheet_month": sheet_month,
        "sheet_year": sheet_year,
        # period_start/period_end: the span of the imported deals' per-row dates.
        # Named to match the Seeker/Canna importers so run_import's logging and
        # the buysheet-upload endpoint can read them uniformly (IRC uses per-row
        # Offer Start/End, so this is a range, not a single month).
        "period_start": min(seen_starts) if seen_starts else None,
        "period_end": max(seen_ends) if seen_ends else None,
        "deals_replaced": deleted,
        "deals_inserted": inserted,
        "ongoing_deals": ongoing_count,
        "skipped_bad_start_date": bad_start_count,
        # Range of per-row dates actually used (for sanity check)
        "earliest_start": min(seen_starts) if seen_starts else None,
        "latest_start":   max(seen_starts) if seen_starts else None,
        "earliest_end":   min(seen_ends) if seen_ends else None,
        "latest_end":     max(seen_ends) if seen_ends else None,
        "lp_updates": lp_updates,
        "skipped_bundles": int(bundles_count),
        "skipped_parse_fail": int(parse_fail),
    }

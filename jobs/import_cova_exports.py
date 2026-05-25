"""
Import Cova Excel exports into the local database.

Two file types are supported:

1. Itemized Sales export (e.g. `Itemized_Sales_-_YYYYMMDD.xlsx`)
   - One row per line-item on a receipt
   - Used to populate `products` and `sales_daily` (aggregated to per-day)

2. Inventory On Hand by Product export (e.g. `Inventory_On_Hand_by_Product_-_YYYYMMDD.xlsx`)
   - One row per SKU per location
   - Used to populate `products`, `inventory_snapshots`, and `prices`

The importer auto-detects which type a file is from its column layout, so
you can drop both into the same folder and run one command.

Usage:
    python jobs/import_cova_exports.py /path/to/imports/folder

Or import a single file:
    python jobs/import_cova_exports.py /path/to/file.xlsx
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import sqlite_schema as schema

log = logging.getLogger(__name__)

# The column names Cova uses. If Cova ever renames these, update here.
SALES_SIGNATURE = {"Invoice #", "Date (Local)", "SKU", "Product", "Quantity", "Subtotal"}
INVENTORY_SIGNATURE = {"Location", "SKU", "In Stock Qty", "Regular Price", "Classification"}
# Historical inventory exports ("Inventory by Product as of") omit Regular Price
# but include In Stock Cost and Product State. Use those as the signature.
INVENTORY_HISTORICAL_SIGNATURE = {"Location", "SKU", "In Stock Qty", "Classification", "In Stock Cost", "Product State"}
OCS_CATALOG_SIGNATURE = {"OCS Variant Number", "OCS Item Number", "Product Name", "Stock Status", "Pack Size"}


def _find_csv_header_row(file_path: Path) -> int:
    """
    Cova CSV exports sometimes start with metadata rows (Parameters, date range,
    filters) before the actual column header line. Scan the first 30 rows and
    find the one that looks like real column headers — lots of comma-separated
    values, several of which match known Cova field names.
    """
    known_fields = {
        "Invoice #", "SKU", "Product", "Quantity", "Subtotal",
        "Location", "In Stock Qty", "Regular Price", "Classification",
        "OCS Variant Number", "Product Name", "Stock Status", "Pack Size",
        "Discount Reason", "Discount Type", "Product Discount Amount",
    }
    with open(file_path, "r", encoding="utf-8-sig") as f:
        for row_num, line in enumerate(f):
            if row_num > 30:
                break
            cells = {c.strip().strip('"') for c in line.strip().split(",")}
            if len(cells & known_fields) >= 3:
                return row_num
    return 0  # fallback: treat first row as header


def _read_any(file_path: Path, **kwargs):
    """Read an Excel or CSV file, whichever it is."""
    suffix = file_path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(file_path, **kwargs)
    if suffix == ".csv":
        # Cova CSVs are UTF-8 with BOM, and often have metadata rows at the top.
        # Skip them automatically so the user doesn't have to hand-edit the file.
        header_row = _find_csv_header_row(file_path)
        return pd.read_csv(
            file_path,
            encoding="utf-8-sig",
            skiprows=header_row,
            low_memory=False,  # avoid dtype warnings on mixed-type columns
            **kwargs,
        )
    raise ValueError(f"Unsupported file type: {suffix}")


def extract_historical_as_of(file_path: Path) -> datetime | None:
    """
    Cova's "Inventory by Product Historical" export includes a Parameters
    sheet with an 'As Of' row. If we find it, parse and return the date.
    Regular "Inventory On Hand" exports don't have this sheet — return None.
    """
    if file_path.suffix.lower() not in (".xlsx", ".xls"):
        return None
    try:
        params = pd.read_excel(file_path, sheet_name="Parameters")
    except (ValueError, KeyError):
        return None  # no Parameters sheet

    # The sheet is shaped like:
    #   Entities         | <StoreName>
    #   Rooms            | All
    #   Classifications  | All
    #   As Of            | 2026-03-01 20:00:00
    if "Entities" not in params.columns or params.shape[1] < 2:
        return None

    value_col = params.columns[1]
    for _, row in params.iterrows():
        key = str(row["Entities"]).strip().lower()
        if key == "as of":
            val = row[value_col]
            if isinstance(val, (datetime, pd.Timestamp)):
                # Normalize to UTC
                ts = pd.Timestamp(val)
                if ts.tz is None:
                    ts = ts.tz_localize("UTC")
                return ts.to_pydatetime()
            try:
                return pd.to_datetime(val, utc=True).to_pydatetime()
            except Exception:
                return None
    return None


def detect_file_type(file_path: Path) -> str:
    """Peek at the column headers to figure out what kind of export this is."""
    df = _read_any(file_path, nrows=0)  # headers only, no rows
    cols = set(df.columns)
    if SALES_SIGNATURE.issubset(cols):
        return "sales"
    if INVENTORY_SIGNATURE.issubset(cols):
        return "inventory"
    if INVENTORY_HISTORICAL_SIGNATURE.issubset(cols):
        return "inventory"
    if OCS_CATALOG_SIGNATURE.issubset(cols):
        return "ocs_catalog"
    raise ValueError(
        f"Could not determine file type for {file_path.name}. "
        f"Expected a Cova Itemized Sales, Inventory On Hand, or OCS Catalogue export."
    )


# ---------------------------------------------------------------------------
# Location management — map Cova location names to our short codes (S1, S2…)
# ---------------------------------------------------------------------------

def ensure_location(conn, cova_location_name: str) -> str:
    """
    Get or create a location row for this Cova location name.
    Returns the short code (e.g. 'S1') that everything else uses as location_id.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM locations WHERE cova_location_id = ?",
        (cova_location_name,),
    )
    row = cur.fetchone()
    if row:
        return row[0]

    # Assign next short code
    cur.execute("SELECT COUNT(*) FROM locations")
    n = cur.fetchone()[0]
    short_code = f"S{n + 1}"

    cur.execute(
        """
        INSERT INTO locations (id, cova_location_id, name, city, region)
        VALUES (?, ?, ?, NULL, NULL)
        """,
        (short_code, cova_location_name, cova_location_name),
    )
    conn.commit()
    log.info("Registered new location: %s → %s", cova_location_name, short_code)
    return short_code


# ---------------------------------------------------------------------------
# Category path derivation
# ---------------------------------------------------------------------------

# Per SB Cannabis product tree:
#   "Cannabis" covers Flower, Edibles, Extracts, Topicals, Vapes, Nicotine, Seeds
#   "Accessories" covers Cleaning, Devices, Glassware, Grinders, Lighters, etc.
#   "Other" is a bucket for Fees, Donations, Gift Cards, LP Payments — not real products.
# Nicotine lives under Cannabis in Cova (your decision to keep it there).
_OTHER_CATEGORIES = {
    "fees - og", "integrated gift card - og", "donation", "lp payments",
    "fees", "gift card", "gift cards",
}

# Fallback map: leaf classification -> top_level. Used when Category Path is
# missing (e.g. inventory-only exports). Built from the SB Cannabis product tree.
_CLASSIFICATION_TO_TOP_LEVEL = {
    # ------- Cannabis -------
    # Flower
    "dried flower": "Cannabis", "milled flower": "Cannabis", "pre-rolls": "Cannabis",
    "infused milled flower": "Cannabis", "infused pre-rolls": "Cannabis",
    # Edibles
    "baked goods": "Cannabis", "dissolvable powder": "Cannabis", "drinks": "Cannabis",
    "hot drinks": "Cannabis", "shots": "Cannabis", "chocolate": "Cannabis",
    "cooking & baking": "Cannabis", "hard edibles": "Cannabis", "soft chews": "Cannabis",
    "frozen treats": "Cannabis",
    # Extracts
    "capsules": "Cannabis", "oils": "Cannabis", "sublingual strips": "Cannabis",
    "distillates": "Cannabis", "hash": "Cannabis", "isolates": "Cannabis",
    "kief": "Cannabis", "resin": "Cannabis", "rosin": "Cannabis",
    "shatter": "Cannabis", "wax": "Cannabis",
    # Seeds
    "seeds": "Cannabis",
    # Topicals
    "bath & shower": "Cannabis", "creams & lotions": "Cannabis",
    "topical oils": "Cannabis", "transdermal": "Cannabis",
    # Vapes
    "510 thread": "Cannabis", "disposables": "Cannabis", "pods": "Cannabis",
    "vape kits": "Cannabis", "gio cartridge": "Cannabis", "vapes - clean": "Cannabis",
    # Nicotine (per user preference, grouped under Cannabis)
    "nicotine disp": "Cannabis", "nicotine disp - inactive": "Cannabis", "juice": "Cannabis",

    # ------- Accessories -------
    # Cleaning
    "ashtrays": "Accessories", "cleaning tools": "Accessories", "mats": "Accessories",
    "odour control": "Accessories", "solutions": "Accessories",
    # Devices
    "batteries": "Accessories", "device auxiliary": "Accessories",
    "infusers & decarboxylators": "Accessories", "scales": "Accessories",
    "vaporizers": "Accessories",
    # Glassware
    "bongs": "Accessories", "dab rigs": "Accessories", "glassware auxiliary": "Accessories",
    "micro-dose": "Accessories", "pipes": "Accessories", "spoons": "Accessories",
    "stones": "Accessories", "dugouts": "Accessories",
    # Grinders
    "grinders": "Accessories",
    # Lighters
    "covers": "Accessories", "electric": "Accessories", "fuel": "Accessories",
    "sparkwheels": "Accessories", "torches": "Accessories", "wicks": "Accessories",
    # Misc
    "books": "Accessories", "gift kits": "Accessories", "grow kits": "Accessories",
    "misc": "Accessories", "molds": "Accessories",
    # Rolling
    "cones": "Accessories", "filters": "Accessories", "machines": "Accessories",
    "papers": "Accessories", "trays": "Accessories", "wraps": "Accessories",
    # Storage
    "climate control": "Accessories", "glass storage": "Accessories",
    "herb storage": "Accessories", "rolling storage": "Accessories",
    "concentrate storage": "Accessories",
    # Apparel
    "hats": "Accessories", "hoodies": "Accessories", "long sleeves": "Accessories",
    "raglans": "Accessories", "t-shirt": "Accessories", "stickers": "Accessories",
    "pins": "Accessories", "gift bags": "Accessories",
}


def derive_top_level(category_path: str | None, classification: str | None) -> str | None:
    """
    Given a Category Path like "Cannabis > Flower > Dried Flower" and a
    Classification like "Dried Flower", return the top-level group:
    'Cannabis', 'Accessories', or 'Other'. Returns None if we really can't tell.
    """
    # Check "Other" bucket first — categorize by leaf classification
    if classification and classification.strip().lower() in _OTHER_CATEGORIES:
        return "Other"

    # Preferred: first segment of the Category Path
    if category_path and isinstance(category_path, str):
        first = category_path.split(">")[0].strip()
        if first.lower() == "starbuds":
            parts = [p.strip() for p in category_path.split(">")]
            if len(parts) >= 2:
                first = parts[1]
        if first in ("Cannabis", "Accessories", "Other"):
            return first
        low = first.lower()
        if low == "cannabis": return "Cannabis"
        if low == "accessories": return "Accessories"

    # Fallback: leaf classification (used when inventory file lacks Category Path)
    if classification:
        mapped = _CLASSIFICATION_TO_TOP_LEVEL.get(classification.strip().lower())
        if mapped:
            return mapped

    return None


def backfill_top_levels(conn) -> int:
    """One-shot repair: classify any products where top_level is still NULL
    using the leaf classification fallback. Returns number updated."""
    cur = conn.cursor()
    cur.execute("""
        SELECT sku, category FROM products
        WHERE top_level IS NULL AND category IS NOT NULL
    """)
    rows = cur.fetchall()
    updates = []
    for sku, cat in rows:
        top = _CLASSIFICATION_TO_TOP_LEVEL.get((cat or "").strip().lower())
        if top is None and (cat or "").strip().lower() in _OTHER_CATEGORIES:
            top = "Other"
        if top:
            updates.append((top, sku))
    if updates:
        cur.executemany(
            "UPDATE products SET top_level = ? WHERE sku = ?",
            updates,
        )
        conn.commit()
    return len(updates)


# ---------------------------------------------------------------------------
# Sales importer
# ---------------------------------------------------------------------------

def import_sales(conn, file_path: Path) -> dict:
    """
    Import an Itemized Sales export. Populates THREE tables:
      - products       (master record, upsert)
      - sales_daily    (per-SKU per-day aggregate, fast queries for reorder engine)
      - sale_lines     (line-level detail, used for cashier/discount/basket analytics)

    Both sales tables are populated in the same pass for efficiency.
    All upserts are idempotent — re-importing the same file replaces existing rows.
    """
    log.info("Reading sales file: %s", file_path.name)
    df = _read_any(file_path)
    log.info("  %d line items loaded", len(df))

    # Drop rows missing critical fields
    df = df.dropna(subset=["SKU", "Date (Local)"])
    df["Date (Local)"] = pd.to_datetime(df["Date (Local)"])

    # Map Cova location names to our internal location_id
    cova_locations = df["Location"].unique()
    location_map = {name: ensure_location(conn, name) for name in cova_locations}
    df["location_id"] = df["Location"].map(location_map)

    # ------- Upsert product master -------
    products = (
        df.groupby("SKU")
        .agg(
            name=("Product", "first"),
            brand=("Brand", "first"),
            category=("Classification", "first"),
            category_path=("Category Path", "first"),
            thc=("THC %", "first"),
            cbd=("CBD %", "first"),
        )
        .reset_index()
    )
    products["top_level"] = products.apply(
        lambda r: derive_top_level(r.get("category_path"), r.get("category")),
        axis=1,
    )
    _upsert_products(conn, products)
    log.info("  upserted %d product rows", len(products))

    # ------- Aggregate sales to daily (existing behavior, unchanged) -------
    df["sale_date"] = df["Date (Local)"].dt.strftime("%Y-%m-%d")
    daily = (
        df.groupby(["SKU", "location_id", "sale_date"])
        .agg(
            units_sold=("Quantity", "sum"),
            gross_revenue=("Subtotal", "sum"),
        )
        .reset_index()
    )

    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO sales_daily (sku, location_id, sale_date, units_sold, gross_revenue)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (sku, location_id, sale_date) DO UPDATE SET
            units_sold = excluded.units_sold,
            gross_revenue = excluded.gross_revenue
        """,
        [
            (r["SKU"], r["location_id"], r["sale_date"],
             int(r["units_sold"]), float(r["gross_revenue"]))
            for _, r in daily.iterrows()
        ],
    )
    log.info("  upserted %d daily sales rows", len(daily))

    # ------- Populate sale_lines (new — line-level detail) -------
    # Each row in df is one line item. We need to assign a per-invoice line_no
    # because Cova exports don't provide one explicitly.
    df["line_no"] = df.groupby("Invoice #").cumcount() + 1

    # Coerce numerics defensively (CSV vs xlsx behave differently)
    for col in ("Quantity", "Regular Price", "Sold At Price",
                "Product Discount", "Subtotal"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Online flag — Cova's "Online" column is "Yes"/"No"
    df["is_online"] = (df.get("Online", "").astype(str).str.lower() == "yes").astype(int)

    # Datetime — combine if available
    if "Date Time (Local)" in df.columns:
        df["sale_datetime_str"] = pd.to_datetime(
            df["Date Time (Local)"], errors="coerce"
        ).dt.strftime("%Y-%m-%d %H:%M:%S")
    else:
        df["sale_datetime_str"] = None

    # Build the sale_lines DataFrame using vectorized operations.
    # iterrows() is ~50x slower; for 480K rows it can take minutes vs seconds.
    log.info("  preparing %d sale_lines rows...", len(df))

    # Drop rows with NULL invoice — those can't be inserted with our PK
    sl_df = df[df["Invoice #"].notna()].copy()

    # Build the column-aligned tuples list using zip() over numpy arrays —
    # this is the fastest approach short of writing C extensions.
    rows = list(zip(
        sl_df["Invoice #"].astype(str),
        sl_df["line_no"].astype(int),
        sl_df["sale_date"],
        sl_df["sale_datetime_str"].where(sl_df["sale_datetime_str"].notna(), None),
        sl_df["location_id"],
        sl_df["SKU"].astype(str),
        sl_df["Quantity"].fillna(0).astype(float),
        sl_df["Regular Price"].where(sl_df["Regular Price"].notna(), None),
        sl_df["Sold At Price"].where(sl_df["Sold At Price"].notna(), None),
        # Cova stores discount as a negative deduction. Flip sign so
        # discount_amount > 0 means "we gave away X dollars" — easier to read
        # and makes discount-rate math: rate = SUM(discount) / SUM(regular_price * units).
        sl_df["Product Discount"].fillna(0).astype(float).abs(),
        sl_df["Subtotal"].where(sl_df["Subtotal"].notna(), None),
        sl_df.get("Tendered By", pd.Series([None]*len(sl_df))).where(
            sl_df.get("Tendered By", pd.Series([None]*len(sl_df))).notna(), None),
        sl_df.get("Created By", pd.Series([None]*len(sl_df))).where(
            sl_df.get("Created By", pd.Series([None]*len(sl_df))).notna(), None),
        sl_df["is_online"].astype(int),
        sl_df.get("Customer", pd.Series([None]*len(sl_df))).where(
            sl_df.get("Customer", pd.Series([None]*len(sl_df))).notna(), None),
    ))

    # Chunked bulk insert with periodic commit to bound memory
    insert_sql = """
        INSERT INTO sale_lines (
            invoice_no, line_no, sale_date, sale_datetime, location_id,
            sku, units, regular_price, sold_price, discount_amount,
            subtotal, cashier, created_by, is_online, customer_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (invoice_no, line_no, sku) DO UPDATE SET
            sale_date = excluded.sale_date,
            sale_datetime = excluded.sale_datetime,
            location_id = excluded.location_id,
            units = excluded.units,
            regular_price = excluded.regular_price,
            sold_price = excluded.sold_price,
            discount_amount = excluded.discount_amount,
            subtotal = excluded.subtotal,
            cashier = excluded.cashier,
            created_by = excluded.created_by,
            is_online = excluded.is_online,
            customer_name = excluded.customer_name
    """

    chunk_size = 10000
    line_count = 0
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i:i+chunk_size]
        cur.executemany(insert_sql, chunk)
        line_count += len(chunk)
        conn.commit()
        if line_count % 100000 == 0 or line_count == len(rows):
            log.info("    sale_lines: %d / %d", line_count, len(rows))
    log.info("  upserted %d sale_lines rows", line_count)

    return {
        "type": "sales",
        "line_items": len(df),
        "products": len(products),
        "daily_rows": len(daily),
        "sale_lines_rows": line_count,
        "locations": list(location_map.values()),
        "date_range": (df["Date (Local)"].min().date().isoformat(),
                       df["Date (Local)"].max().date().isoformat()),
    }


# ---------------------------------------------------------------------------
# Inventory importer
# ---------------------------------------------------------------------------

def import_inventory(conn, file_path: Path, as_of: datetime | None = None) -> dict:
    """
    Import an Inventory On Hand by Product export. Creates a time-stamped
    snapshot per (sku, location). Also upserts products and prices.
    """
    as_of = as_of or datetime.now(timezone.utc)
    as_of_iso = as_of.strftime("%Y-%m-%d %H:%M:%S")
    log.info("Reading inventory file: %s", file_path.name)

    df = _read_any(file_path)
    log.info("  %d inventory rows loaded", len(df))

    df = df.dropna(subset=["SKU", "Location"])

    # Register locations
    cova_locations = df["Location"].unique()
    location_map = {name: ensure_location(conn, name) for name in cova_locations}
    df["location_id"] = df["Location"].map(location_map)

    # ------- Upsert product master (inventory file has them too) -------
    # Also extract the OCS variant from the "Supplier SKU" field so we can
    # later join to the OCS catalogue for pack sizes and wholesale prices.
    def extract_ocs_variant(s):
        if not isinstance(s, str): return None
        for part in s.split(','):
            part = part.strip()
            if '_' in part and part:
                return part
        return None

    df['ocs_variant'] = df.get('Supplier SKU', '').apply(extract_ocs_variant) if 'Supplier SKU' in df.columns else None

    # Some columns are optional depending on export type:
    #   Regular "Inventory On Hand" export  -> has Brand, Regular Price, Category Path
    #   "Inventory by Product Historical"   -> no Brand, no Regular Price, no Category Path
    has_brand = "Brand" in df.columns
    has_category_path = "Category Path" in df.columns

    agg_spec = dict(
        name=("Product", "first"),
        category=("Classification", "first"),
        ocs_variant=("ocs_variant", "first"),
    )
    if has_brand:
        agg_spec["brand"] = ("Brand", "first")
    if has_category_path:
        agg_spec["category_path"] = ("Category Path", "first")

    products = df.groupby("SKU").agg(**agg_spec).reset_index()
    if not has_brand:
        products["brand"] = None
    if not has_category_path:
        products["category_path"] = None

    products["top_level"] = products.apply(
        lambda r: derive_top_level(r.get("category_path"), r.get("category")),
        axis=1,
    )
    _upsert_products(conn, products)

    # ------- Insert inventory snapshots -------
    # Capture First/Last Received Date and Days Since Last Sold straight from
    # the Cova export when present (Inventory On Hand exports include them;
    # the Inventory Historical export doesn't).
    has_first_recv = "First Received Date" in df.columns
    has_last_recv = "Last Received Date" in df.columns
    has_days_since = "Days Since Last Sold" in df.columns

    def _date_iso(val):
        """Cova ships dates as 'YYYY-MM-DD HH:MM:SS' or pandas Timestamps.
        Empty values come through as NaT/NaN. Return ISO date or None."""
        if val is None:
            return None
        try:
            import pandas as _pd
            if _pd.isna(val):
                return None
        except Exception:
            pass
        try:
            ts = pd.to_datetime(val, errors="coerce")
            if pd.isna(ts):
                return None
            return ts.strftime("%Y-%m-%d")
        except Exception:
            return None

    def _int_or_none(val):
        try:
            import pandas as _pd
            if _pd.isna(val):
                return None
        except Exception:
            pass
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    snapshots = []
    for _, r in df.iterrows():
        snapshots.append((
            r["SKU"], r["location_id"], int(r["In Stock Qty"]), 0,
            _date_iso(r["First Received Date"]) if has_first_recv else None,
            _date_iso(r["Last Received Date"]) if has_last_recv else None,
            _int_or_none(r["Days Since Last Sold"]) if has_days_since else None,
            as_of_iso,
        ))
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO inventory_snapshots
            (sku, location_id, on_hand, reserved,
             first_received_date, last_received_date, days_since_last_sold, as_of)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (sku, location_id, as_of) DO UPDATE SET
            on_hand = excluded.on_hand,
            first_received_date = COALESCE(excluded.first_received_date, inventory_snapshots.first_received_date),
            last_received_date = COALESCE(excluded.last_received_date, inventory_snapshots.last_received_date),
            days_since_last_sold = COALESCE(excluded.days_since_last_sold, inventory_snapshots.days_since_last_sold)
        """,
        snapshots,
    )

    # ------- Upsert prices -------
    # Historical exports don't include Regular Price; skip price upsert there.
    price_rows: list[tuple] = []
    if "Regular Price" in df.columns:
        prices_df = df[df["Regular Price"].notna() & (df["Regular Price"] > 0)]
        price_rows = [
            (r["SKU"], r["location_id"], float(r["Regular Price"]), None, "CAD", as_of_iso)
            for _, r in prices_df.iterrows()
        ]
        cur.executemany(
            """
            INSERT INTO prices (sku, location_id, regular_price, sale_price, currency, as_of)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (sku, location_id) DO UPDATE SET
                regular_price = excluded.regular_price,
                sale_price = excluded.sale_price,
                as_of = excluded.as_of
            """,
            price_rows,
        )
    conn.commit()
    log.info("  inserted %d snapshots, %d prices", len(snapshots), len(price_rows))

    # Refresh the materialized latest-on-hand table so the Reorder Report
    # doesn't re-derive it from the full snapshot history on every load.
    current_rows = refresh_current_inventory(conn)

    return {
        "type": "inventory",
        "snapshots": len(snapshots),
        "prices": len(price_rows),
        "current_inventory_rows": current_rows,
        "locations": list(location_map.values()),
        "as_of": as_of_iso,
    }


def refresh_current_inventory(conn) -> int:
    """Rebuild the materialized current_inventory table from inventory_snapshots.

    current_inventory holds exactly one row per (sku, location) — the most
    recent snapshot. Deriving this at query time required a window-function
    scan over all ~2.9M snapshot rows on every Reorder Report load. We now
    precompute it here, once per inventory import, using the same
    latest-row-per-(sku, location) pattern the reorder engine used to inline.

    Full DELETE + INSERT (the table is a derived snapshot, not authoritative).
    Returns the number of rows written. Caller is responsible for the commit
    boundary — we commit here so the refresh is durable even if a later step
    in the import fails.
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM current_inventory")
    cur.execute(
        """
        INSERT INTO current_inventory (sku, location_id, on_hand, last_received_date, as_of)
        SELECT sku, location_id, on_hand, last_received_date, as_of FROM (
            SELECT sku, location_id, on_hand, last_received_date, as_of,
                   ROW_NUMBER() OVER (
                       PARTITION BY sku, location_id ORDER BY as_of DESC
                   ) AS rn
            FROM inventory_snapshots
        ) t WHERE rn = 1
        """
    )
    rows = cur.rowcount
    conn.commit()
    log.info("  refreshed current_inventory: %d rows", rows)
    return rows


# ---------------------------------------------------------------------------
# OCS Catalog importer
# ---------------------------------------------------------------------------

def import_ocs_catalog(conn, file_path: Path) -> dict:
    """
    Import the OCS B2B portal catalogue export.
    Populates the ocs_catalog table with pack size, unit price, stock status.
    """
    log.info("Reading OCS catalogue: %s", file_path.name)
    df = _read_any(file_path)
    log.info("  %d catalogue rows loaded", len(df))

    # Normalize the variant key to lowercase so joins are case-insensitive
    df = df.dropna(subset=["OCS Variant Number"])
    df['variant_norm'] = df['OCS Variant Number'].str.lower()

    rows = []
    for _, r in df.iterrows():
        rows.append((
            r['variant_norm'],
            str(r['OCS Item Number']) if pd.notna(r.get('OCS Item Number')) else None,
            str(r['GTIN']) if pd.notna(r.get('GTIN')) else None,
            str(r['Product Name']) if pd.notna(r.get('Product Name')) else '—',
            str(r['Brand']) if pd.notna(r.get('Brand')) else None,
            str(r['Supplier Name']) if pd.notna(r.get('Supplier Name')) else None,
            str(r['Category']) if pd.notna(r.get('Category')) else None,
            str(r['Sub-Category']) if pd.notna(r.get('Sub-Category')) else None,
            str(r['Size']) if pd.notna(r.get('Size')) else None,
            str(r['Stock Status']) if pd.notna(r.get('Stock Status')) else None,
            float(r['Unit Price']) if pd.notna(r.get('Unit Price')) else None,
            int(r['Pack Size']) if pd.notna(r.get('Pack Size')) else None,
            float(r['Minimum THC Content (%)']) if pd.notna(r.get('Minimum THC Content (%)')) else None,
            float(r['Maximum THC Content (%)']) if pd.notna(r.get('Maximum THC Content (%)')) else None,
            float(r['Minimum CBD Content (%)']) if pd.notna(r.get('Minimum CBD Content (%)')) else None,
            float(r['Maximum CBD Content (%)']) if pd.notna(r.get('Maximum CBD Content (%)')) else None,
        ))

    cur = conn.cursor()
    # Wipe and reload — the OCS catalog is a full snapshot each time
    cur.execute("DELETE FROM ocs_catalog")
    cur.executemany(
        """
        INSERT INTO ocs_catalog (
            ocs_variant_number, ocs_item_number, gtin, product_name, brand,
            supplier, category, subcategory, size, stock_status, unit_price,
            pack_size, thc_min, thc_max, cbd_min, cbd_max
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    log.info("  upserted %d OCS catalog rows", len(rows))

    # Report match rate
    cur.execute("""
        SELECT COUNT(*) FROM products
        WHERE ocs_variant_number IN (SELECT ocs_variant_number FROM ocs_catalog)
    """)
    matched = cur.fetchone()[0]
    log.info("  %d products now matched to OCS catalog", matched)

    return {
        "type": "ocs_catalog",
        "catalog_rows": len(rows),
        "matched_products": matched,
        "orderable_at_ocs": int(df[df['Stock Status'] == 'YES'].shape[0]),
        "out_at_ocs": int(df[df['Stock Status'] == 'NO'].shape[0]),
    }


def import_cova_catalog(conn, file_path: Path | None = None) -> dict:
    """
    Import the Cova product catalogue export (the "Products" sheet of the
    per-store Cova catalogue dump). Populates the cova_catalog table, which
    successor detection reads for manufacturer / size / Date Added / UPC —
    fields not present in products or ocs_catalog.

    Full replace each time (mirrors import_ocs_catalog). If file_path is None,
    uses the most recent cova-catalog-*.xlsx in <repo>/cova-catalog/.
    """
    if file_path is None:
        cdir = Path(__file__).resolve().parent.parent / "cova-catalog"
        candidates = sorted(cdir.glob("cova-catalog-*.xlsx"))
        if not candidates:
            raise FileNotFoundError(f"No cova-catalog-*.xlsx found in {cdir}")
        file_path = candidates[-1]
    file_path = Path(file_path)

    log.info("Reading Cova catalogue: %s", file_path.name)
    df = pd.read_excel(file_path, sheet_name="Products", dtype=str)
    log.info("  %d catalogue rows loaded", len(df))

    def _s(v):
        return str(v).strip() if pd.notna(v) and str(v).strip() != "" else None

    def _ci(v):
        try:
            return int(float(v)) if pd.notna(v) and str(v).strip() != "" else None
        except (ValueError, TypeError):
            return None

    # Keyed by catalog_sku to dedupe against the PRIMARY KEY (last row wins).
    rows: dict[str, tuple] = {}
    for _, r in df.iterrows():
        sku = _s(r.get("Catalog SKU"))
        if not sku:
            continue
        rows[sku] = (
            sku,
            _s(r.get("Product Name *")),
            _s(r.get("Brand")),
            _s(r.get("Vendor SKU")),
            _s(r.get("UPC")),
            _s(r.get("Size")),
            _s(r.get("Manufacturer")),
            _s(r.get("Net Weight")),
            _ci(r.get("Case Qty")),
            _s(r.get("Date Added (UTC)")),
            _s(r.get("Date Updated (UTC)")),
            _s(r.get("Product Status")),
        )

    cur = conn.cursor()
    cur.execute("DELETE FROM cova_catalog")
    cur.executemany(
        """
        INSERT INTO cova_catalog (
            catalog_sku, product_name, brand, vendor_sku, upc, size,
            manufacturer, net_weight, case_qty, date_added, date_updated, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        list(rows.values()),
    )
    conn.commit()
    log.info("  loaded %d Cova catalog rows", len(rows))

    return {
        "type": "cova_catalog",
        "catalog_rows": len(rows),
        "source": file_path.name,
        "with_upc": int(df["UPC"].notna().sum()),
    }


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _upsert_products(conn, products_df) -> None:
    """Upsert a DataFrame of product master records."""
    rows = []
    for _, r in products_df.iterrows():
        ocs_variant = r.get("ocs_variant") if "ocs_variant" in r else None
        if ocs_variant is not None and not pd.notna(ocs_variant):
            ocs_variant = None
        category_path = r.get("category_path") if "category_path" in r else None
        if category_path is not None and not pd.notna(category_path):
            category_path = None
        top_level = r.get("top_level") if "top_level" in r else None
        if top_level is not None and not pd.notna(top_level):
            top_level = None
        rows.append((
            str(r["SKU"]),
            str(r["SKU"]),
            str(ocs_variant).lower() if ocs_variant else None,
            str(r.get("name", "")) if pd.notna(r.get("name")) else "—",
            str(r.get("brand", "")) if pd.notna(r.get("brand")) else None,
            str(r.get("category", "")) if pd.notna(r.get("category")) else None,
            str(category_path) if category_path else None,
            str(top_level) if top_level else None,
            float(r["thc"]) if "thc" in r and pd.notna(r["thc"]) else None,
            float(r["cbd"]) if "cbd" in r and pd.notna(r["cbd"]) else None,
        ))
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO products (
            sku, cova_catalog_item_id, ocs_variant_number, name, brand,
            category, category_path, top_level, thc, cbd, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')
        ON CONFLICT (sku) DO UPDATE SET
            name = excluded.name,
            ocs_variant_number = COALESCE(excluded.ocs_variant_number, products.ocs_variant_number),
            brand = COALESCE(excluded.brand, products.brand),
            category = COALESCE(excluded.category, products.category),
            category_path = COALESCE(excluded.category_path, products.category_path),
            top_level = COALESCE(excluded.top_level, products.top_level),
            thc = COALESCE(excluded.thc, products.thc),
            cbd = COALESCE(excluded.cbd, products.cbd),
            last_updated_at = CURRENT_TIMESTAMP
        """,
        rows,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_import(path: Path, db_path: str) -> list[dict]:
    """Import a single file or every file in a directory."""
    if path.is_dir():
        # Auto-extract any .zip files in the import folder.
        # OCS bulk-download invoices are delivered as zips containing many .xlsx files.
        import zipfile
        zips = list(path.glob("*.zip"))
        for z in zips:
            try:
                with zipfile.ZipFile(z) as zf:
                    extracted = 0
                    for member in zf.namelist():
                        # Only extract spreadsheets/CSVs we care about; skip junk
                        if not member.lower().endswith((".xlsx", ".xls", ".csv")):
                            continue
                        # Strip any directory prefixes — flatten into imports/
                        target_name = Path(member).name
                        if not target_name:
                            continue
                        target = path / target_name
                        if target.exists():
                            continue  # don't clobber existing files
                        with zf.open(member) as src, open(target, "wb") as dst:
                            dst.write(src.read())
                        extracted += 1
                    log.info("Extracted %d files from %s", extracted, z.name)
                # Move processed zip aside so it isn't re-processed next run
                processed_dir = path / "processed"
                processed_dir.mkdir(exist_ok=True)
                z.rename(processed_dir / z.name)
            except zipfile.BadZipFile:
                log.warning("Skipping %s: not a valid zip file", z.name)

        files = sorted(
            list(path.glob("*.xlsx")) + list(path.glob("*.xls")) + list(path.glob("*.csv"))
        )
        if not files:
            log.warning("No Excel files found in %s", path)
            return []
    else:
        files = [path]

    results = []
    with sqlite3.connect(db_path) as conn:
        schema.init_schema(conn)

        # Process in a specific order:
        # 1. OCS catalog first (so products table can link to it)
        # 2. Inventory (creates products + links to OCS via ocs_variant_number)
        # 3. Sales last (aggregates sales data)
        # 4. Invoices anywhere — they don't depend on other imports
        typed_files = []
        for f in files:
            # Invoice and Discounts filename patterns are unambiguous; check those first
            # (signature-based detection would raise ValueError for them)
            from jobs.import_invoices import is_invoice_file
            from jobs.import_discounts import is_discounts_file
            from jobs.import_buysheet_irc import is_irc_file
            from jobs.import_buysheet_seeker import is_seeker_file
            from jobs.import_buysheet import is_buysheet_file
            from jobs.import_order_fill import is_order_fill_file
            if is_invoice_file(f):
                typed_files.append((f, "invoice"))
                continue
            if is_discounts_file(f):
                typed_files.append((f, "discounts"))
                continue
            if is_order_fill_file(f):
                typed_files.append((f, "order_fill"))
                continue
            # IRC and Seeker must come BEFORE the generic CC buysheet check
            # because CC's filename regex is broad. Check most specific first.
            if is_irc_file(f):
                typed_files.append((f, "buysheet_irc"))
                continue
            if is_seeker_file(f):
                typed_files.append((f, "buysheet_seeker"))
                continue
            if is_buysheet_file(f):
                typed_files.append((f, "buysheet"))
                continue
            try:
                file_type = detect_file_type(f)
                typed_files.append((f, file_type))
            except ValueError as e:
                log.warning("Skipping %s: %s", f.name, e)

        order = {"ocs_catalog": 0, "inventory": 1, "sales": 2, "invoice": 3,
                 "discounts": 4, "order_fill": 5,
                 "buysheet": 6, "buysheet_irc": 6, "buysheet_seeker": 6}
        typed_files.sort(key=lambda x: order.get(x[1], 99))

        # Count invoices upfront for progress display
        invoice_count = sum(1 for _, t in typed_files if t == "invoice")
        invoice_index = 0
        invoice_unmatched = 0
        bulk_invoices = invoice_count >= 5  # quieter logging when bulk

        for f, file_type in typed_files:
            if not (file_type == "invoice" and bulk_invoices):
                log.info("=" * 70)
                log.info("Processing %s (detected as %s)", f.name, file_type)
                log.info("=" * 70)

            if file_type == "sales":
                result = import_sales(conn, f)
            elif file_type == "inventory":
                # Detect Historical exports (multi-sheet xlsx with Parameters).
                # If found, use the embedded As Of date. Otherwise default = now.
                historical_as_of = extract_historical_as_of(f)
                if historical_as_of:
                    log.info("  detected Historical export, as_of=%s",
                             historical_as_of.strftime("%Y-%m-%d"))
                result = import_inventory(conn, f, as_of=historical_as_of)
            elif file_type == "ocs_catalog":
                result = import_ocs_catalog(conn, f)
            elif file_type == "invoice":
                from jobs.import_invoices import import_invoice_file
                result = import_invoice_file(conn, f)
                invoice_index += 1
                if result.get("unmatched_location"):
                    invoice_unmatched += 1
                    if not bulk_invoices:
                        log.warning("  ⚠ Invoice %s could not be matched to a known location "
                                    "(address: %s). Saved with location_id=NULL.",
                                    result["invoice_no"], result.get("address_line"))
                elif not bulk_invoices:
                    log.info("  invoice %s · %s · %s · %d lines · $%s",
                             result["invoice_no"], result["invoice_date"],
                             result["location_id"], result["lines"], result["total_with_tax"])
                # Periodic progress every 25 invoices in bulk mode
                if bulk_invoices and (invoice_index % 25 == 0 or invoice_index == invoice_count):
                    log.info("  invoices: %d/%d processed (%d unmatched so far)",
                             invoice_index, invoice_count, invoice_unmatched)
            elif file_type == "discounts":
                from jobs.import_discounts import import_discounts_file
                result = import_discounts_file(conn, f)
                log.info("  discounts: %d rows · %s to %s",
                         result["rows"], result["date_range"][0], result["date_range"][1])
            elif file_type == "order_fill":
                from jobs.import_order_fill import import_order_fill_file
                try:
                    result = import_order_fill_file(conn, f)
                    lt = result["lead_times"]
                    log.info("  Order Fill: %d SKUs · CTB=%sd, FT-Exp=%sd, FT-Std=%sd · %d back in stock, %d new",
                             result["rows"], lt["click_to_buy"], lt["flow_thru_expedited"],
                             lt["flow_thru_standard"], result["flags"]["back_in_stock"],
                             result["flags"]["new_arrival"])
                except Exception as e:
                    log.warning("  ⚠ Order Fill failed: %s", e)
                    continue
            elif file_type == "buysheet":
                from jobs.import_buysheet import import_buysheet_file
                try:
                    result = import_buysheet_file(conn, f)
                    log.info("  buysheet: %s · period %s to %s · %d deals (%d replaced, %d LP updates)",
                             result["partner"], result["period_start"], result["period_end"],
                             result["deals_inserted"], result["deals_replaced"],
                             result.get("lp_updates", 0))
                except ValueError as e:
                    log.warning("  ⚠ Buysheet failed: %s", e)
                    continue
            elif file_type == "buysheet_irc":
                from jobs.import_buysheet_irc import import_irc_file
                try:
                    result = import_irc_file(conn, f)
                    log.info("  IRC buysheet: %s · period %s to %s · %d deals (%d replaced, %d LP updates, %d bundles skipped)",
                             result["partner"], result["period_start"], result["period_end"],
                             result["deals_inserted"], result["deals_replaced"],
                             result.get("lp_updates", 0), result.get("skipped_bundles", 0))
                except ValueError as e:
                    log.warning("  ⚠ IRC buysheet failed: %s", e)
                    continue
            elif file_type == "buysheet_seeker":
                from jobs.import_buysheet_seeker import import_seeker_file
                try:
                    result = import_seeker_file(conn, f)
                    log.info("  Seeker buysheet: %s · period %s to %s · %d deals (%d replaced)",
                             result["partner"], result["period_start"], result["period_end"],
                             result["deals_inserted"], result["deals_replaced"])
                except ValueError as e:
                    log.warning("  ⚠ Seeker buysheet failed: %s", e)
                    continue
            else:
                continue

            result["file_name"] = f.name
            results.append(result)

        if bulk_invoices and invoice_unmatched:
            log.warning("=" * 70)
            log.warning("⚠ %d of %d invoices had unmatched locations (saved with location_id=NULL)",
                        invoice_unmatched, invoice_count)
            log.warning("Review unmatched invoices: SELECT * FROM invoices WHERE location_id IS NULL")
            log.warning("=" * 70)

        # After all imports: backfill any NULL top_level values using the
        # classification fallback map. This handles products that came in
        # only through inventory (no Category Path) or from older imports.
        backfilled = backfill_top_levels(conn)
        if backfilled:
            log.info("=" * 70)
            log.info("Backfilled top_level on %d products from classification map", backfilled)
            log.info("=" * 70)

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Import Cova Excel exports into the local database."
    )
    parser.add_argument(
        "path",
        type=Path,
        help="Path to an Excel file OR a folder containing Excel files.",
    )
    parser.add_argument(
        "--db",
        default="terroir.db",
        help="Path to the SQLite database (default: terroir.db)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.path.exists():
        log.error("Path does not exist: %s", args.path)
        sys.exit(1)

    results = run_import(args.path, args.db)

    print("\n" + "=" * 70)
    print("IMPORT SUMMARY")
    print("=" * 70)
    print(f"Database: {args.db}")
    print(f"Files processed: {len(results)}")
    for r in results:
        print(f"\n  {r['file_name']} ({r['type']})")
        for k, v in r.items():
            if k not in ("file_name", "type"):
                print(f"    {k}: {v}")


if __name__ == "__main__":
    main()

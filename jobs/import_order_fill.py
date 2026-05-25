"""
Order Fill parser — ingest OCS weekly order template into the database.

The OCS Order Fill is a per-order-cycle xlsx (single 'MasterCatalogue' sheet)
listing every SKU available to order, with key per-SKU metadata:
  - Flow Thru (YES/NO): NO=Click-to-Buy (fast direct from OCS warehouse)
                        YES=Flow Through (coordinated direct from LP)
  - Delivery Tier: 'Standard' or 'Expedited' for Flow Through items only
  - Estimated Delivery Date: when OCS commits to delivery
  - Back In Stock flag: 'X' if recently restored
  - New Arrival flag: 'X' if newly listed
  - Available Quantity: how many OCS has in stock
  - MaxQty: per-store order limit
  - Price Change: INCREASE/DECREASE if changed since last fill
  - Plus full product info (brand, name, size, etc.)

We persist this for:
  1. Visibility badges in the Reorder tab (back-in-stock, FT tier)
  2. Lead time calculations (delivery_date - generation_date)
  3. Future: avoid recommending more than Available Quantity allows

Filename format: 'OrderExport_{DD}_{Mon}_{YYYY}_{HMM}AM_Packs.xlsx'
e.g. 'OrderExport_01_May_2026_703AM_Packs.xlsx'

Idempotent — re-importing the same source_file replaces the run.
We don't auto-archive old runs; that's a future maintenance task.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from pathlib import Path
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

# Filename detection
# Allow an optional store prefix (e.g. "S5__OrderExport_…") so per-store files
# saved by the upload endpoint / connector still detect as Order Fills.
ORDER_FILL_FILENAME_RE = re.compile(
    r"(?:^|_)OrderExport_\d{1,2}_[A-Za-z]{3,9}_\d{4}.*\.xlsx?$",
    re.IGNORECASE,
)

# Required columns the Order Fill must have
ORDER_FILL_REQUIRED_COLS = {
    "Flow Thru", "Delivery Tier", "Estimated Delivery Date",
    "SKU", "Brand", "ItemName", "Available Quantity",
}


def is_order_fill_file(file_path: Path) -> bool:
    """Detect by filename + content. Filename pattern is fairly specific."""
    if not ORDER_FILL_FILENAME_RE.search(file_path.name):
        return False
    try:
        xl = pd.ExcelFile(file_path)
        if "MasterCatalogue" not in xl.sheet_names:
            return False
        df_head = pd.read_excel(file_path, sheet_name="MasterCatalogue", nrows=0)
        return ORDER_FILL_REQUIRED_COLS.issubset(set(df_head.columns))
    except Exception:
        return False


def _extract_generated_at(file_path: Path) -> Optional[str]:
    """Parse generation date from filename like 'OrderExport_01_May_2026_703AM_Packs.xlsx'.
    Returns ISO date string or None if unparseable."""
    m = re.search(r"OrderExport_(\d{1,2})_([A-Za-z]+)_(\d{4})", file_path.name)
    if not m:
        return None
    day, month_str, year = m.groups()
    months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
    mn = months.get(month_str[:3].lower())
    if not mn:
        return None
    try:
        return f"{int(year):04d}-{mn:02d}-{int(day):02d}"
    except ValueError:
        return None


def _parse_delivery_date(s) -> Optional[str]:
    """Parse 'May 6th, 2026' / 'May 13th, 2026' / etc. → '2026-05-06'."""
    if pd.isna(s) or not isinstance(s, str):
        return None
    # Strip ordinal suffix (1st, 2nd, 3rd, 4th, ...)
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", s)
    try:
        ts = pd.to_datetime(cleaned, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.strftime("%Y-%m-%d")
    except Exception:
        return None


def _x_flag(val) -> int:
    """Convert 'X' / 'x' / NaN → 1/0."""
    if pd.isna(val):
        return 0
    s = str(val).strip().upper()
    return 1 if s == "X" else 0


def _safe_int(val) -> Optional[int]:
    if pd.isna(val):
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def _safe_float(val) -> Optional[float]:
    if pd.isna(val):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def import_order_fill_file(conn, file_path: Path, location_id: str | None = None) -> dict:
    """Parse and persist an OCS Order Fill xlsx.

    location_id tags the run with the store it was pulled for — Order Fill is
    per-store because each store's order window (and thus availability/delivery)
    differs. None = a legacy chain-wide run. Idempotent per source_file, so the
    connector should give each store's file a store-unique name.

    Returns a summary dict including computed lead times per delivery tier.
    """
    log.info("Reading Order Fill: %s", file_path.name)

    df = pd.read_excel(file_path, sheet_name="MasterCatalogue")
    log.info("  %d Order Fill rows loaded", len(df))

    if len(df) == 0:
        return {"type": "order_fill", "file_name": file_path.name,
                "rows": 0, "lead_times": {}}

    generated_at = _extract_generated_at(file_path)

    # Compute lead times by delivery tier.
    # For each tier, take the most common delivery date and compute lead days.
    # (Multiple dates can appear if delivery is split across days; mode is robust.)
    def _lead_days(delivery_date_iso: Optional[str]) -> Optional[int]:
        if not delivery_date_iso or not generated_at:
            return None
        try:
            return (pd.Timestamp(delivery_date_iso) - pd.Timestamp(generated_at)).days
        except Exception:
            return None

    # Group by Flow Thru + Delivery Tier; find the most common delivery date in each
    df["_flow_thru_norm"] = df["Flow Thru"].astype(str).str.upper().str.strip()
    df["_delivery_tier_norm"] = df["Delivery Tier"].astype(str).where(
        df["Delivery Tier"].notna(), None
    )
    df["_delivery_date_iso"] = df["Estimated Delivery Date"].apply(_parse_delivery_date)

    def _modal_delivery(filter_mask) -> Optional[str]:
        subset = df.loc[filter_mask, "_delivery_date_iso"].dropna()
        if subset.empty:
            return None
        return Counter(subset).most_common(1)[0][0]

    ctb_mask = df["_flow_thru_norm"] == "NO"
    ft_exp_mask = (df["_flow_thru_norm"] == "YES") & (df["Delivery Tier"] == "Expedited")
    ft_std_mask = (df["_flow_thru_norm"] == "YES") & (df["Delivery Tier"] == "Standard")

    ctb_delivery = _modal_delivery(ctb_mask)
    ft_exp_delivery = _modal_delivery(ft_exp_mask)
    ft_std_delivery = _modal_delivery(ft_std_mask)

    ctb_lead = _lead_days(ctb_delivery)
    ft_exp_lead = _lead_days(ft_exp_delivery)
    ft_std_lead = _lead_days(ft_std_delivery)

    log.info("  Lead times: CTB=%s days, FT-Exp=%s days, FT-Std=%s days",
             ctb_lead, ft_exp_lead, ft_std_lead)

    cur = conn.cursor()

    # Idempotent insert by source_file
    cur.execute("""
        INSERT INTO order_fill_runs (
            source_file, generated_at, location_id,
            click_to_buy_lead_days, flow_thru_expedited_lead_days, flow_thru_standard_lead_days,
            click_to_buy_delivery, flow_thru_expedited_delivery, flow_thru_standard_delivery,
            sku_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source_file) DO UPDATE SET
            generated_at = excluded.generated_at,
            location_id = excluded.location_id,
            click_to_buy_lead_days = excluded.click_to_buy_lead_days,
            flow_thru_expedited_lead_days = excluded.flow_thru_expedited_lead_days,
            flow_thru_standard_lead_days = excluded.flow_thru_standard_lead_days,
            click_to_buy_delivery = excluded.click_to_buy_delivery,
            flow_thru_expedited_delivery = excluded.flow_thru_expedited_delivery,
            flow_thru_standard_delivery = excluded.flow_thru_standard_delivery,
            sku_count = excluded.sku_count,
            imported_at = CURRENT_TIMESTAMP
        RETURNING id
    """, (
        file_path.name, generated_at, location_id,
        ctb_lead, ft_exp_lead, ft_std_lead,
        ctb_delivery, ft_exp_delivery, ft_std_delivery,
        len(df),
    ))
    run_id = cur.fetchone()[0]

    # Clear and re-load SKU rows for this run
    cur.execute("DELETE FROM order_fill_skus WHERE run_id = ?", (run_id,))

    rows_to_insert = []
    for _, r in df.iterrows():
        ocs_variant = r.get("SKU")
        if not isinstance(ocs_variant, str) or not ocs_variant.strip():
            continue
        flow_thru = 1 if str(r.get("Flow Thru", "")).strip().upper() == "YES" else 0
        delivery_tier = r.get("Delivery Tier") if pd.notna(r.get("Delivery Tier")) else None
        rows_to_insert.append((
            run_id,
            ocs_variant.strip(),
            flow_thru,
            delivery_tier,
            r["_delivery_date_iso"],
            _x_flag(r.get("Back In Stock")),
            _x_flag(r.get("New Arrival")),
            _x_flag(r.get("Favourite")),
            _safe_float(r.get("ItemPrice")),
            _safe_float(r.get("UnitPrice")),
            _safe_int(r.get("PackSize")),
            _safe_int(r.get("MaxQty")),
            _safe_int(r.get("Available Quantity")),
            (str(r.get("Price Change")).strip()
             if pd.notna(r.get("Price Change")) else None),
            _safe_float(r.get("Price Change %")),
            r.get("Brand") if pd.notna(r.get("Brand")) else None,
            r.get("ItemName") if pd.notna(r.get("ItemName")) else None,
            r.get("Sub Category") if pd.notna(r.get("Sub Category")) else None,
        ))

    cur.executemany("""
        INSERT INTO order_fill_skus (
            run_id, ocs_variant_number,
            flow_thru, delivery_tier, estimated_delivery_date,
            back_in_stock, new_arrival, favourite,
            item_price, unit_price, pack_size, max_qty, available_quantity,
            price_change, price_change_pct,
            brand, item_name, sub_category
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows_to_insert)
    conn.commit()

    log.info("  inserted %d Order Fill SKU rows (run_id=%d)", len(rows_to_insert), run_id)

    return {
        "type": "order_fill",
        "file_name": file_path.name,
        "location_id": location_id,
        "run_id": run_id,
        "generated_at": generated_at,
        "rows": len(rows_to_insert),
        "lead_times": {
            "click_to_buy": ctb_lead,
            "flow_thru_expedited": ft_exp_lead,
            "flow_thru_standard": ft_std_lead,
        },
        "delivery_dates": {
            "click_to_buy": ctb_delivery,
            "flow_thru_expedited": ft_exp_delivery,
            "flow_thru_standard": ft_std_delivery,
        },
        "flags": {
            "back_in_stock": int(df.apply(lambda r: _x_flag(r.get("Back In Stock")), axis=1).sum()),
            "new_arrival": int(df.apply(lambda r: _x_flag(r.get("New Arrival")), axis=1).sum()),
        },
    }


def get_latest_order_fill_skus(conn) -> dict:
    """Return Order Fill availability keyed by ``(location_id, variant_lower)``,
    using the latest run *per store*.

    Order Fill is per-store, so we take the most recent run for each location_id
    independently. A ``location_id`` of None is a legacy chain-wide run; the
    reorder lookup tries the store-specific key first, then falls back to the
    ``(None, variant)`` key, so pre-migration data keeps working until per-store
    runs arrive. Variant is lower-cased so the join is case-insensitive
    (products/ocs_catalog store it lower-cased; the Order Fill file may not).
    """
    cur = conn.cursor()
    # Latest run id per location (NULLs sort last in DESC, so dated runs win).
    cur.execute("SELECT id, location_id FROM order_fill_runs ORDER BY generated_at DESC, id DESC")
    latest_run_by_loc: dict = {}
    for run_id, loc in cur.fetchall():
        if loc not in latest_run_by_loc:   # first seen per loc = newest
            latest_run_by_loc[loc] = run_id
    if not latest_run_by_loc:
        return {}

    loc_by_run = {rid: loc for loc, rid in latest_run_by_loc.items()}
    run_ids = list(loc_by_run)
    placeholders = ",".join("?" * len(run_ids))
    cur.execute(f"""
        SELECT run_id, ocs_variant_number, flow_thru, delivery_tier, estimated_delivery_date,
               back_in_stock, new_arrival, available_quantity, max_qty,
               price_change, price_change_pct
        FROM order_fill_skus WHERE run_id IN ({placeholders})
    """, run_ids)
    cols = [d[0] for d in cur.description]
    out: dict = {}
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        loc = loc_by_run[d["run_id"]]
        out[(loc, (d["ocs_variant_number"] or "").lower())] = d
    return out

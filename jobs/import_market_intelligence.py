"""
Market Intelligence importer
============================

Parses OCS's per-store municipality reports and persists them to
market_intelligence_imports + market_intelligence_data tables.

Expected file layout:
    imports/market_intelligence/{YYYY-MM-DD}/{StoreName}/
        3_2_Average_Sales_Units_per_Store_by_Municipality.xlsx
        2_2_Sales_Velocity_-_Average_Daily_Sales_Units_per_Store_by_Municipality.xlsx

Both files share the same SKU set; we join them on SKU during import.

Honest design notes:
- The "Your Store(s)" / "Your Municipality" columns are AVERAGES per store,
  not raw totals. We store both as-is and let the UI compute ratios.
- Period is inferred from folder date when not stated explicitly. User should
  pass period_start / period_end as kwargs when known.
- For Innisfil (or any store without municipality data), the 'Your Municipality'
  column may be entirely null. We still import — just flag has_municipality=0.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd


# OCS export filenames vary by user — some have underscores (3_2_Average...),
# some have dots (3.2 Average...), some have hyphens. Match permissively on
# the numbered prefix + key phrase.
AVG_UNITS_FILENAME_PATTERN = re.compile(
    r"3[._]2[._\s].*average.*sales.*units.*\.xlsx?$",
    re.IGNORECASE,
)
VELOCITY_FILENAME_PATTERN = re.compile(
    r"2[._]2[._\s].*sales.*velocity.*\.xlsx?$",
    re.IGNORECASE,
)


def _detect_files(folder: Path) -> tuple[Path | None, Path | None]:
    """Find the average-units and velocity files in a folder. Either may be
    absent — caller decides what to do."""
    avg = None
    vel = None
    for f in folder.iterdir():
        if not f.is_file():
            continue
        if AVG_UNITS_FILENAME_PATTERN.search(f.name):
            avg = f
        elif VELOCITY_FILENAME_PATTERN.search(f.name):
            vel = f
    return avg, vel


def _read_export(path: Path) -> pd.DataFrame:
    """Read the OCS Export sheet. Both report types use a single 'Export' sheet."""
    df = pd.read_excel(path, sheet_name="Export")
    # Normalize column names to lowercase keys we'll use internally
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _resolve_location_id(conn, store_name: str) -> str | None:
    """Map a store folder name (e.g. 'Bradford', 'Amherstview') to a location_id.
    Tries exact match on locations.name first, then case-insensitive partial.
    Returns None if no match — caller decides whether to create or fail."""
    cur = conn.cursor()
    cur.execute("SELECT id FROM locations WHERE name = ? OR LOWER(name) = LOWER(?)",
                (store_name, store_name))
    row = cur.fetchone()
    if row:
        return row[0]
    # Try LIKE match — store names sometimes have suffixes like "(Livingstone)"
    cur.execute("SELECT id FROM locations WHERE LOWER(name) LIKE LOWER(?)",
                (f"%{store_name}%",))
    row = cur.fetchone()
    return row[0] if row else None


def import_market_intelligence(
    conn,
    folder: Path,
    *,
    location_id: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
) -> dict:
    """Import a single store's market intelligence files.

    folder: path to the per-store folder (e.g. .../2026-04-30/Bradford/)
    location_id: optional override; default = inferred from folder name
    period_start, period_end: optional; default = inferred from parent folder name
                              if it's a date, otherwise unset
    """
    folder = Path(folder)
    if not folder.exists() or not folder.is_dir():
        raise FileNotFoundError(f"folder not found: {folder}")

    # Resolve location from folder name
    store_name = folder.name
    if location_id is None:
        location_id = _resolve_location_id(conn, store_name)
    if location_id is None:
        raise ValueError(f"Could not resolve store '{store_name}' to a location_id. "
                         f"Pass location_id explicitly.")

    # Detect period from grandparent folder name if it looks like a date
    if period_end is None:
        parent = folder.parent.name
        # Match YYYY-MM-DD or YYYY-MM
        m_full = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", parent)
        m_month = re.match(r"^(\d{4})-(\d{2})$", parent)
        if m_full:
            period_end = parent
        elif m_month:
            # Default to last day of month if only month given
            y, mo = int(m_month.group(1)), int(m_month.group(2))
            from calendar import monthrange
            period_end = f"{y:04d}-{mo:02d}-{monthrange(y, mo)[1]:02d}"

    avg_path, vel_path = _detect_files(folder)
    if not avg_path and not vel_path:
        raise FileNotFoundError(
            f"No market intelligence files found in {folder}. "
            f"Expected '3_2_Average_Sales_Units...xlsx' and/or '2_2_Sales_Velocity...xlsx'."
        )

    # Build the unified SKU dataset by joining the two files
    rows: dict[str, dict] = {}  # keyed by sku

    if avg_path:
        df = _read_export(avg_path)
        for _, r in df.iterrows():
            sku = str(r.get("SKU") or "").strip()
            if not sku:
                continue
            rows.setdefault(sku, {"sku": sku})
            rows[sku].update({
                "item_name": r.get("Item Name"),
                "brand": r.get("Brand"),
                "supplier": r.get("LP/Supplier"),
                "subcategory": r.get("Subcategory"),
                "size": r.get("Size"),
                "your_units": r.get("Your Store(s)"),
                "municipality_units": r.get("Your Municipality"),
            })

    if vel_path:
        df = _read_export(vel_path)
        for _, r in df.iterrows():
            sku = str(r.get("SKU") or "").strip()
            if not sku:
                continue
            rows.setdefault(sku, {"sku": sku})
            # Don't overwrite item info if already set from avg file
            if "item_name" not in rows[sku]:
                rows[sku].update({
                    "item_name": r.get("Item Name"),
                    "brand": r.get("Brand"),
                    "supplier": r.get("LP/Supplier"),
                    "subcategory": r.get("Subcategory"),
                    "size": r.get("Size"),
                })
            rows[sku].update({
                "your_velocity": r.get("Your Store(s)"),
                "municipality_velocity": r.get("Your Municipality"),
                "sales_days": r.get("Sales Days"),
            })

    # Determine if we have municipality data at all
    muni_count = sum(1 for v in rows.values()
                     if pd.notna(v.get("municipality_units")) or pd.notna(v.get("municipality_velocity")))
    has_muni = 1 if muni_count > 0 else 0

    # Compute period_days
    period_days = None
    if period_start and period_end:
        try:
            d1 = date.fromisoformat(period_start)
            d2 = date.fromisoformat(period_end)
            period_days = (d2 - d1).days + 1
        except ValueError:
            pass

    cur = conn.cursor()
    cur.execute("""
        INSERT INTO market_intelligence_imports
            (location_id, period_start, period_end, period_days, sku_count,
             has_municipality, source_files)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        location_id, period_start, period_end, period_days,
        len(rows), has_muni,
        json.dumps([f.name for f in [avg_path, vel_path] if f]),
    ))
    import_id = cur.lastrowid

    def _val(x):
        """Pandas NaN -> None for SQLite; otherwise pass through."""
        if x is None:
            return None
        try:
            if pd.isna(x):
                return None
        except (TypeError, ValueError):
            pass
        return x

    payload = []
    for sku, r in rows.items():
        payload.append((
            import_id, sku,
            _val(r.get("item_name")), _val(r.get("brand")),
            _val(r.get("supplier")), _val(r.get("subcategory")),
            _val(r.get("size")),
            _val(r.get("your_units")), _val(r.get("municipality_units")),
            _val(r.get("your_velocity")), _val(r.get("municipality_velocity")),
            _val(r.get("sales_days")),
        ))
    cur.executemany("""
        INSERT INTO market_intelligence_data
            (import_id, sku, item_name, brand, supplier, subcategory, size,
             your_units, municipality_units, your_velocity, municipality_velocity,
             sales_days)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, payload)
    conn.commit()

    return {
        "import_id": import_id,
        "location_id": location_id,
        "store_name": store_name,
        "period_start": period_start,
        "period_end": period_end,
        "period_days": period_days,
        "sku_count": len(rows),
        "has_municipality": bool(has_muni),
        "files_found": {
            "average_units": avg_path.name if avg_path else None,
            "velocity": vel_path.name if vel_path else None,
        },
    }

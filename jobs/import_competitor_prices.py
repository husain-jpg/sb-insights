"""
Import competitor price CSVs into the competitor_prices table.

Usage — from terroir-ops root:
    python jobs/import_competitor_prices.py

Scans imports/ for files matching:
    cannacabana_menu_*.csv
    hibuddy_*.csv
    competitor_*.csv

and loads any new rows into the database. Moves processed files to
imports/processed/ so they don't get re-imported on the next run.

Idempotent: the competitor_prices primary key is
    (competitor_name, variant_id, price_tier, collected_at)
so re-loading the same CSV only produces INSERT OR IGNORE no-ops.
"""
from __future__ import annotations

import csv
import os
import sqlite3
import sys
from pathlib import Path

# Let `from db.sqlite_schema import ...` work when launched via
# `python jobs/import_competitor_prices.py` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = os.environ.get("TERROIR_DB", "db/sbinsights.db")

# Columns in the CSVs, in the order the scraper writes them.
# This mapping is permissive — missing columns become NULL.
CSV_TO_DB = {
    "competitor_name": "competitor_name",
    "sb_competes_with": "sb_competes_with",
    "collection": "collection",
    "collection_label": "collection_label",
    "product_id": "product_id",
    "product_handle": "product_handle",
    "product_title": "product_title",
    "vendor": "vendor",
    "product_type": "product_type",
    "variant_id": "variant_id",
    "variant_sku": "variant_sku",
    "variant_title": "variant_title",
    "variant_size": "variant_size",
    "price": "price",
    "compare_at_price": "compare_at_price",
    "available": "available",
    "tags": "tags",
    "scraped_at": "collected_at",   # scraped_at in CSV → collected_at in DB
}


def _normalize_row(row: dict, source: str) -> dict:
    """Coerce types and ensure required fields."""
    out = {}
    for csv_col, db_col in CSV_TO_DB.items():
        v = row.get(csv_col)
        if v == "" or v is None:
            out[db_col] = None
        else:
            out[db_col] = v

    # Casts
    for num_col in ("price", "compare_at_price"):
        if out.get(num_col) is not None:
            try:
                out[num_col] = float(out[num_col])
            except (TypeError, ValueError):
                out[num_col] = None
    if out.get("available") is not None:
        out["available"] = 1 if str(out["available"]).lower() in ("true", "1") else 0

    out["price_tier"] = "market"  # current scrapers capture Market only
    out["source"] = source

    # Must-haves — drop row if missing
    if not out.get("competitor_name") or not out.get("variant_id") or not out.get("collected_at"):
        return None
    if not out.get("product_title"):
        out["product_title"] = "(untitled)"
    return out


def _source_for(filename: str) -> str:
    name = filename.lower()
    if "cannacabana" in name:
        return "cannacabana"
    if "hibuddy" in name:
        return "hibuddy"
    return "competitor"


def import_file(conn: sqlite3.Connection, path: Path) -> tuple[int, int]:
    """Import one CSV. Returns (inserted, skipped_or_invalid)."""
    source = _source_for(path.name)
    inserted = 0
    skipped = 0
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        cur = conn.cursor()
        for raw in reader:
            norm = _normalize_row(raw, source)
            if norm is None:
                skipped += 1
                continue
            cols = list(norm.keys())
            placeholders = ",".join(["?"] * len(cols))
            colnames = ",".join(cols)
            sql = (
                f"INSERT OR IGNORE INTO competitor_prices ({colnames}) "
                f"VALUES ({placeholders})"
            )
            try:
                cur.execute(sql, [norm[c] for c in cols])
                if cur.rowcount == 1:
                    inserted += 1
                else:
                    skipped += 1
            except sqlite3.Error as e:
                print(f"    ! row skipped: {e}")
                skipped += 1
        conn.commit()
    return inserted, skipped


def main():
    imports = Path("imports")
    processed = imports / "processed"
    processed.mkdir(exist_ok=True, parents=True)

    patterns = ["cannacabana_menu_*.csv", "hibuddy_*.csv", "competitor_*.csv"]
    files: list[Path] = []
    for pat in patterns:
        files.extend(sorted(imports.glob(pat)))

    if not files:
        print("No competitor price CSVs to import.")
        print("Drop files matching cannacabana_menu_*.csv in ./imports/ and re-run.")
        return

    print(f"Found {len(files)} competitor file(s) to import.")
    print()

    # Ensure schema exists
    from db.sqlite_schema import init_schema
    Path(DB_PATH).parent.mkdir(exist_ok=True, parents=True)
    conn = sqlite3.connect(DB_PATH)
    init_schema(conn)

    total_inserted = 0
    total_skipped = 0
    for path in files:
        print(f"→ {path.name}")
        try:
            inserted, skipped = import_file(conn, path)
        except Exception as e:
            print(f"    ✗ FAILED: {e}")
            continue
        total_inserted += inserted
        total_skipped += skipped
        print(f"    inserted {inserted}, skipped {skipped}")

        # Move to processed
        dest = processed / path.name
        i = 1
        while dest.exists():
            dest = processed / f"{path.stem}_{i}{path.suffix}"
            i += 1
        path.rename(dest)

    conn.close()
    print()
    print(f"Done. Total inserted: {total_inserted:,}  skipped: {total_skipped:,}")


if __name__ == "__main__":
    main()

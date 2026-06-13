"""
Auto-fill the OCS weekly order template using engine recommendations.

The OCS sends a per-store .xlsx every Wednesday at 7pm. It has 4,300+ rows,
all with Quantity=0. The store has until 12:30pm Thursday to fill in
quantities and upload it back.

This module takes that template and the engine's reorder recommendations
for the target store, and produces a filled version with:
  - Quantity column populated for SKUs the engine wants
  - Quantity in CASES (PackSize multiples), since that's what OCS expects
  - Clamped to OCS Available Quantity and MaxQty
  - Untouched (=0) for SKUs the engine has no opinion on

Returns both a Python preview structure (for UI) and the filled DataFrame
(for download).

The OCS Quantity field appears to be in CASE units (you order N "packs"
of PackSize each). reorder_qty from the engine is in UNITS, so we convert.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import date
from io import BytesIO
from pathlib import Path

import pandas as pd

from jobs.reorder_engine import compute_all_reorders


@dataclass
class OrderLine:
    """A single matched line: OCS row + engine recommendation."""
    ocs_row_index: int           # row number in the original template (0-indexed)
    sku: str                     # OCS variant number
    product_name: str
    brand: str | None
    sub_category: str | None
    pack_size: int               # units per case
    unit_price: float            # per-unit wholesale
    item_price: float            # full case price (unit_price * pack_size)
    available_quantity: int | None  # OCS-side stock cap (None = no cap shown)
    max_quantity: int | None     # OCS-side per-order cap
    is_back_in_stock: bool
    is_new_arrival: bool
    is_favourite: bool
    # Engine fields:
    is_top_sku: bool             # anchor flag
    on_hand: int                 # current stock at this store
    daily_velocity: float
    days_supply: float | None
    urgency: str | None          # stockout/critical/high/medium
    engine_reorder_units: int    # raw engine recommendation in UNITS
    engine_reorder_cases: int    # rounded up to full cases
    suggested_quantity: int      # final cases to order (after clamping)
    line_total: float            # cases * item_price
    notes: str                   # human-readable explanation of clamping/sourcing


def fill_template(
    conn,
    template_path: Path | BytesIO,
    location_id: str,
    *,
    ceiling_days: int | None = None,
    min_velocity: float | None = None,
) -> tuple[list[OrderLine], pd.DataFrame, dict]:
    """
    Read the OCS template, compute engine recs for the store, fill quantities.

    Returns:
        (order_lines, filled_dataframe, summary)

    summary has totals like total_cases, total_cost, anchor_count, etc.
    """
    df = pd.read_excel(template_path, sheet_name="MasterCatalogue")

    # Make sure required columns exist — fail loudly if format changed
    required = {"SKU", "PackSize", "UnitPrice", "ItemPrice", "Quantity",
                "Available Quantity", "ItemName", "Brand", "Sub Category"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"OCS template missing expected columns: {missing}")

    # Reset Quantity to integers, in case the template had any pre-fill
    df["Quantity"] = 0

    # Pull engine recommendations for this store. Anchor to the latest sale
    # date — same as the Reorder Report — so the filled quantities match what
    # the manager saw on screen (compute_all_reorders otherwise defaults
    # as_of to today, which drifts from the data and changes velocities).
    _row = conn.execute("SELECT MAX(sale_date) FROM sales_daily").fetchone()
    as_of_date = date.fromisoformat(_row[0]) if _row and _row[0] else None
    kw = {"as_of_date": as_of_date}
    if ceiling_days is not None:
        kw["ceiling_days"] = ceiling_days
    if min_velocity is not None:
        kw["min_velocity"] = min_velocity
    recs = compute_all_reorders(conn, location_id=location_id, **kw)

    # Map engine recs by ocs_variant for fast lookup
    recs_by_variant: dict[str, object] = {}
    for r in recs:
        if r.ocs_variant:
            recs_by_variant[r.ocs_variant] = r

    # Walk every OCS template row, attach engine rec if any, decide quantity
    order_lines: list[OrderLine] = []
    for idx, row in df.iterrows():
        sku = str(row["SKU"]) if pd.notna(row["SKU"]) else ""
        if not sku:
            continue

        pack_size = int(row["PackSize"]) if pd.notna(row["PackSize"]) else 1
        unit_price = float(row["UnitPrice"]) if pd.notna(row["UnitPrice"]) else 0.0
        item_price = float(row["ItemPrice"]) if pd.notna(row["ItemPrice"]) else unit_price * pack_size
        # Available Quantity: a NUMBER is OCS's stock cap; BLANK means no cap
        # shown (orderable, unconstrained) — NOT zero. The old code coerced
        # blank → 0, which clamped every unconstrained SKU down to 0 cases and
        # left the whole template empty. None here = "don't clamp on supply".
        available = int(row["Available Quantity"]) if pd.notna(row["Available Quantity"]) else None
        max_qty = int(row["MaxQty"]) if pd.notna(row.get("MaxQty")) else None

        rec = recs_by_variant.get(sku)
        if rec is None:
            # Engine has no opinion — leave Quantity at 0, skip preview row
            # (we don't want a preview screen with 4,300 zero rows)
            continue

        engine_units = int(rec.reorder_qty or 0)
        engine_cases = int(rec.reorder_cases or 0) if rec.reorder_cases else math.ceil(engine_units / pack_size) if engine_units else 0

        # Clamping logic:
        # 1. Don't order more than OCS has (only when a cap is given)
        # 2. Don't exceed MaxQty if set
        # 3. Cases stay ≥ 0
        suggested = max(0, engine_cases)
        notes_parts = []
        if available is not None and suggested * pack_size > available:
            old = suggested
            suggested = available // pack_size  # floor — only what's on hand
            if old > suggested:
                notes_parts.append(f"clamped to OCS available ({available} units = {suggested} cases)")
        if max_qty is not None and suggested > max_qty:
            old = suggested
            suggested = max_qty
            notes_parts.append(f"clamped to MaxQty ({max_qty})")

        notes = "; ".join(notes_parts) if notes_parts else ""

        # Write back into the dataframe for download
        df.at[idx, "Quantity"] = suggested

        order_lines.append(OrderLine(
            ocs_row_index=int(idx),
            sku=sku,
            product_name=str(row["ItemName"]),
            brand=str(row["Brand"]) if pd.notna(row["Brand"]) else None,
            sub_category=str(row["Sub Category"]) if pd.notna(row["Sub Category"]) else None,
            pack_size=pack_size,
            unit_price=unit_price,
            item_price=item_price,
            available_quantity=available,
            max_quantity=max_qty,
            is_back_in_stock=pd.notna(row.get("Back In Stock")),
            is_new_arrival=pd.notna(row.get("New Arrival")),
            is_favourite=pd.notna(row.get("Favourite")),
            is_top_sku=bool(rec.is_top_sku),
            on_hand=int(rec.on_hand or 0),
            daily_velocity=float(rec.daily_velocity or 0),
            days_supply=float(rec.days_supply) if rec.days_supply is not None else None,
            urgency=rec.urgency,
            engine_reorder_units=engine_units,
            engine_reorder_cases=engine_cases,
            suggested_quantity=suggested,
            line_total=round(suggested * item_price, 2),
            notes=notes,
        ))

    # Also surface engine recs that AREN'T in the OCS template — managers
    # need to see these. They're items the engine wants to reorder but OCS
    # isn't offering this week. (Could be Click-To-Buy gaps, supplier issues, etc.)
    matched_skus = {ol.sku for ol in order_lines}
    unmet_recs = []
    for r in recs:
        if r.reorder_qty > 0 and r.ocs_variant and r.ocs_variant not in matched_skus:
            unmet_recs.append({
                "sku": r.sku,
                "ocs_variant": r.ocs_variant,
                "product_name": r.product_name,
                "category": r.category,
                "is_top_sku": r.is_top_sku,
                "urgency": r.urgency,
                "on_hand": r.on_hand,
                "engine_reorder_units": r.reorder_qty,
                "reason": "Not in this week's OCS template",
            })

    # Summary
    suggested_lines = [ol for ol in order_lines if ol.suggested_quantity > 0]
    summary = {
        "location_id": location_id,
        "template_rows": int(len(df)),
        "engine_recs_total": len(recs),
        "matched_lines": len(order_lines),
        "filled_lines": len(suggested_lines),
        "anchor_filled_lines": sum(1 for ol in suggested_lines if ol.is_top_sku),
        "total_cases": sum(ol.suggested_quantity for ol in suggested_lines),
        "total_cost": round(sum(ol.line_total for ol in suggested_lines), 2),
        "clamped_lines": sum(1 for ol in suggested_lines if ol.notes),
        "unmet_engine_recs": unmet_recs,
        "unmet_count": len(unmet_recs),
    }

    return order_lines, df, summary


def write_filled_template(df: pd.DataFrame, out_path: Path | BytesIO) -> None:
    """Write the filled DataFrame back to .xlsx in the OCS-expected format."""
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="MasterCatalogue", index=False)

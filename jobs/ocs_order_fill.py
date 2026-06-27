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
    extra_order_units: dict[str, int] | None = None,
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

    # Trial additions ('Added' in Suggested Additions): the store has no
    # history for these so the engine won't produce them. Inject synthetic
    # recs (units already trial-sized) so they fill into the template like
    # any other order line. Don't clobber a real engine rec if one exists.
    from types import SimpleNamespace
    for variant, units in (extra_order_units or {}).items():
        if variant not in recs_by_variant:
            recs_by_variant[variant] = SimpleNamespace(
                sku=variant, product_name="", ocs_variant=variant,
                reorder_qty=int(units), reorder_cases=0,
                is_top_sku=False, on_hand=0, daily_velocity=0.0,
                days_supply=None, urgency="trial_add", category=None,
            )

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


@dataclass
class OrderCompareLine:
    """One SKU's manager-ordered vs engine-recommended comparison."""
    sku: str                      # OCS variant number
    product_name: str
    brand: str | None
    sub_category: str | None
    pack_size: int                # units per case
    unit_price: float             # per-unit wholesale
    item_price: float             # full case price (unit_price * pack_size)
    # What the manager actually ordered (from the CartExport Quantity column):
    ordered_cases: int
    ordered_units: int
    ordered_cost: float
    # What the engine would recommend for this store:
    engine_cases: int
    engine_units: int
    engine_cost: float
    # Variance (manager - engine), in cases and dollars:
    delta_cases: int
    delta_cost: float
    bucket: str                   # 'on_target' | 'over' | 'under' | 'missed'
    engine_zero: bool             # engine recommended nothing for this SKU
    in_template: bool             # True if the SKU is a row in the uploaded cart file
    # Engine context (None when the SKU has no engine rec — pure manager add):
    on_hand: int | None
    daily_velocity: float | None
    days_supply: float | None
    urgency: str | None
    is_top_sku: bool
    available_quantity: int | None
    max_quantity: int | None
    notes: str


def _engine_cases_for(rec, pack_size: int) -> int:
    """Engine recommended cases, mirroring fill_template's derivation."""
    engine_units = int(getattr(rec, "reorder_qty", 0) or 0)
    rc = getattr(rec, "reorder_cases", None)
    if rc:
        return int(rc)
    if engine_units and pack_size > 0:
        return math.ceil(engine_units / pack_size)
    return 0


def compare_order(
    conn,
    cart_path: Path | BytesIO,
    location_id: str,
    *,
    ceiling_days: int | None = None,
    min_velocity: float | None = None,
    on_target_tolerance_cases: int = 0,
) -> tuple[list[OrderCompareLine], dict]:
    """
    Compare a manager's *built* OCS order (a filled CartExport/OrderExport
    .xlsx, Quantity column populated in CASES) against the engine's reorder
    recommendation for the same store.

    This is the inverse of fill_template: instead of writing engine quantities
    INTO the template, we read the manager's quantities OUT and diff them.

    Buckets (variance = ordered_cases - engine_cases):
      on_target  |variance| <= on_target_tolerance_cases (both may be 0 only if
                 the SKU was ordered; pure 0/0 SKUs are dropped entirely)
      over       ordered more than the engine wanted (incl. engine wanted 0)
      under      ordered less than the engine wanted, but ordered something
      missed     engine wanted some, manager ordered 0

    Returns (lines, summary). Stateless — nothing is persisted.
    """
    df = pd.read_excel(cart_path, sheet_name="MasterCatalogue")

    required = {"SKU", "PackSize", "UnitPrice", "ItemPrice", "Quantity",
                "ItemName", "Brand", "Sub Category", "Available Quantity"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Order file missing expected columns: {missing}")

    # Engine recs, anchored to the latest sale date so the comparison matches
    # what the manager saw on the Reorder Report (same as fill_template).
    _row = conn.execute("SELECT MAX(sale_date) FROM sales_daily").fetchone()
    as_of_date = date.fromisoformat(_row[0]) if _row and _row[0] else None
    kw = {"as_of_date": as_of_date}
    if ceiling_days is not None:
        kw["ceiling_days"] = ceiling_days
    if min_velocity is not None:
        kw["min_velocity"] = min_velocity
    recs = compute_all_reorders(conn, location_id=location_id, **kw)

    recs_by_variant: dict[str, object] = {}
    for r in recs:
        if r.ocs_variant:
            recs_by_variant[r.ocs_variant] = r

    lines: list[OrderCompareLine] = []
    matched_variants: set[str] = set()

    for _, row in df.iterrows():
        sku = str(row["SKU"]).strip() if pd.notna(row["SKU"]) else ""
        if not sku:
            continue

        ordered_cases = _safe_int_cell(row.get("Quantity"))
        rec = recs_by_variant.get(sku)
        pack_size = _safe_int_cell(row.get("PackSize")) or 1
        engine_cases = _engine_cases_for(rec, pack_size) if rec is not None else 0

        # Drop SKUs nobody touched (the file is the full 4,300-row catalogue).
        if ordered_cases == 0 and engine_cases == 0:
            continue

        matched_variants.add(sku)
        unit_price = _safe_float_cell(row.get("UnitPrice"))
        item_price = _safe_float_cell(row.get("ItemPrice")) or (unit_price * pack_size)
        available = _safe_int_cell(row.get("Available Quantity"), default=None)
        max_qty = _safe_int_cell(row.get("MaxQty"), default=None)

        lines.append(_build_compare_line(
            sku=sku, row=row, rec=rec, pack_size=pack_size,
            unit_price=unit_price, item_price=item_price,
            ordered_cases=ordered_cases, engine_cases=engine_cases,
            available=available, max_qty=max_qty, in_template=True,
            on_target_tolerance_cases=on_target_tolerance_cases,
        ))

    # Engine wants it, but it isn't an orderable row in this file (not offered
    # this cycle / Click-to-Buy gap). Surface as a 'missed' line so the manager
    # sees demand the cart can't satisfy. Mirrors fill_template's unmet_recs.
    for r in recs:
        if r.ocs_variant and r.ocs_variant not in matched_variants and (r.reorder_qty or 0) > 0:
            pack_size = int(r.ocs_pack_size) if r.ocs_pack_size else 1
            engine_cases = _engine_cases_for(r, pack_size)
            if engine_cases <= 0:
                continue
            item_price = float(r.ocs_unit_price) * pack_size if r.ocs_unit_price else 0.0
            lines.append(_build_compare_line(
                sku=r.ocs_variant, row=None, rec=r, pack_size=pack_size,
                unit_price=float(r.ocs_unit_price or 0.0), item_price=item_price,
                ordered_cases=0, engine_cases=engine_cases,
                available=None, max_qty=None, in_template=False,
                on_target_tolerance_cases=on_target_tolerance_cases,
            ))

    # Sort: biggest disagreements first, missed/over/under above on_target.
    bucket_rank = {"missed": 0, "under": 1, "over": 2, "on_target": 3}
    lines.sort(key=lambda l: (bucket_rank.get(l.bucket, 9), -abs(l.delta_cost)))

    summary = _summarize_compare(lines, location_id, as_of_date)
    return lines, summary


def _build_compare_line(*, sku, row, rec, pack_size, unit_price, item_price,
                        ordered_cases, engine_cases, available, max_qty,
                        in_template, on_target_tolerance_cases) -> OrderCompareLine:
    ordered_units = ordered_cases * pack_size
    engine_units = engine_cases * pack_size
    ordered_cost = round(ordered_cases * item_price, 2)
    engine_cost = round(engine_cases * item_price, 2)
    delta_cases = ordered_cases - engine_cases
    delta_cost = round(ordered_cost - engine_cost, 2)
    engine_zero = engine_cases == 0

    if abs(delta_cases) <= on_target_tolerance_cases:
        bucket = "on_target"
    elif ordered_cases == 0:
        bucket = "missed"
    elif delta_cases > 0:
        bucket = "over"
    else:
        bucket = "under"

    urgency = getattr(rec, "urgency", None) if rec is not None else None
    notes_parts: list[str] = []
    if bucket == "missed" and urgency in ("stockout", "critical"):
        notes_parts.append(f"{urgency.upper()} — engine wanted {engine_cases} case(s), none ordered")
    if not in_template:
        notes_parts.append("not offered in this order file")
    if available is not None and ordered_units > available:
        notes_parts.append(f"ordered {ordered_units}u exceeds OCS available ({available}u)")

    # Product descriptors: prefer the file row, fall back to the engine rec.
    if row is not None:
        product_name = str(row.get("ItemName")) if pd.notna(row.get("ItemName")) else sku
        brand = str(row.get("Brand")) if pd.notna(row.get("Brand")) else None
        sub_category = str(row.get("Sub Category")) if pd.notna(row.get("Sub Category")) else None
    else:
        product_name = getattr(rec, "product_name", None) or sku
        brand = getattr(rec, "brand", None)
        sub_category = getattr(rec, "category", None)

    return OrderCompareLine(
        sku=sku,
        product_name=product_name,
        brand=brand,
        sub_category=sub_category,
        pack_size=pack_size,
        unit_price=round(unit_price, 2),
        item_price=round(item_price, 2),
        ordered_cases=ordered_cases,
        ordered_units=ordered_units,
        ordered_cost=ordered_cost,
        engine_cases=engine_cases,
        engine_units=engine_units,
        engine_cost=engine_cost,
        delta_cases=delta_cases,
        delta_cost=delta_cost,
        bucket=bucket,
        engine_zero=engine_zero,
        in_template=in_template,
        on_hand=int(getattr(rec, "on_hand", 0)) if rec is not None else None,
        daily_velocity=float(getattr(rec, "daily_velocity", 0.0)) if rec is not None else None,
        days_supply=(float(rec.days_supply) if rec is not None and rec.days_supply is not None else None),
        urgency=urgency,
        is_top_sku=bool(getattr(rec, "is_top_sku", False)) if rec is not None else False,
        available_quantity=available,
        max_quantity=max_qty,
        notes="; ".join(notes_parts),
    )


def _summarize_compare(lines: list[OrderCompareLine], location_id: str,
                       as_of_date) -> dict:
    by_bucket: dict[str, int] = {"on_target": 0, "over": 0, "under": 0, "missed": 0}
    for l in lines:
        by_bucket[l.bucket] = by_bucket.get(l.bucket, 0) + 1
    ordered_lines = [l for l in lines if l.ordered_cases > 0]
    missed_critical = [
        l for l in lines
        if l.bucket == "missed" and l.urgency in ("stockout", "critical")
    ]
    return {
        "location_id": location_id,
        "as_of_date": as_of_date.isoformat() if as_of_date else None,
        "compared_lines": len(lines),
        "buckets": by_bucket,
        "ordered_lines": len(ordered_lines),
        "ordered_cases_total": sum(l.ordered_cases for l in lines),
        "ordered_cost_total": round(sum(l.ordered_cost for l in lines), 2),
        "engine_lines": sum(1 for l in lines if l.engine_cases > 0),
        "engine_cases_total": sum(l.engine_cases for l in lines),
        "engine_cost_total": round(sum(l.engine_cost for l in lines), 2),
        "delta_cost_total": round(sum(l.delta_cost for l in lines), 2),
        "missed_critical_count": len(missed_critical),
        "over_spend": round(sum(l.delta_cost for l in lines if l.delta_cost > 0), 2),
        "under_spend": round(-sum(l.delta_cost for l in lines if l.delta_cost < 0), 2),
    }


def _safe_int_cell(val, default: int | None = 0) -> int | None:
    if val is None or pd.isna(val):
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def _safe_float_cell(val, default: float = 0.0) -> float:
    if val is None or pd.isna(val):
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default

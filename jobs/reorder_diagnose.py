"""
Reorder diagnostic
==================

Given a (location, sku) pair, returns a structured trace explaining what the
engine would see and which filter (if any) caused it to be excluded from the
reorder report.

This is READ-ONLY introspection. It does not mutate anything or run the full
engine — it reads the same inputs the engine reads and checks the same filters
in the same order.

The reasons are returned in the order the engine evaluates them, so the FIRST
reason marked "exclude" is the one that caused exclusion. If no exclusions
fire, the SKU should be in the report.

Honest caveat: if I change the engine logic later, I have to update this file
too to stay in sync. There's no DRY way to do this without significant refactor
of the engine to make filters first-class.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta


VELOCITY_WINDOW_DAYS = 30
MIN_VELOCITY_BASIS_DAYS = 7  # keep in sync with jobs/reorder_engine.py


def diagnose_sku(conn: sqlite3.Connection,
                 sku: str,
                 location_id: str,
                 as_of: date | None = None) -> dict:
    """Return a structured diagnostic for one (sku, store) combination.

    Returns:
        {
          "sku": "...",
          "location_id": "...",
          "found": bool,        # was the SKU even seen in our system?
          "inputs": {           # what data the engine has access to
            "on_hand": float,
            "velocity_30d": float,
            "pack_size": int,
            ...
          },
          "checks": [           # ordered list of each filter check
            {"name": "...", "passed": bool, "explanation": "...", "values": {...}}
          ],
          "verdict": "included" | "excluded" | "no_data",
          "excluded_reason": "..." | None,
        }
    """
    if as_of is None:
        as_of = date.today()
    as_of_iso = as_of.isoformat()
    # 30 complete days ENDING ON as_of, inclusive — must match the engine.
    window_start_d = as_of - timedelta(days=VELOCITY_WINDOW_DAYS - 1)
    window_start_iso = window_start_d.isoformat()

    cur = conn.cursor()

    # Load settings (same as engine does)
    cur.execute("SELECT key, value FROM app_settings")
    settings = {k: v for k, v in cur.fetchall()}

    hero_ceiling = int(float(settings.get("reorder.hero_ceiling_days", 14)))
    regular_ceiling = int(float(settings.get("reorder.regular_ceiling_days", 7)))
    min_velocity = float(settings.get("reorder.min_velocity", 0.0))
    pack_min_raw = float(settings.get("reorder.pack_size_min_fraction", 50))
    pack_min_fraction = pack_min_raw / 100.0 if pack_min_raw > 1.0 else pack_min_raw
    overstock_days = int(float(settings.get("reorder.overstock_days", 50)))
    coverage_days = (int(float(settings.get("reorder.order_cycle_days", 7)))
                     + int(float(settings.get("reorder.lead_time_days", 4))))
    stockout_days = float(settings.get("reorder.stockout_imminent_days", 3.0))
    stockout_min_vel = float(settings.get("reorder.stockout_min_velocity", 0.3))

    # ----- Gather inputs -----
    # Product record
    cur.execute("""
        SELECT p.sku, p.name, p.brand, p.lp, p.ocs_variant_number, p.top_level
        FROM products p WHERE p.sku = ?
    """, (sku,))
    prod = cur.fetchone()
    if not prod:
        return {
            "sku": sku, "location_id": location_id, "found": False,
            "verdict": "no_data",
            "excluded_reason": "SKU not found in products table",
            "inputs": {}, "checks": [],
        }

    _sku, name, brand, lp, ocs_variant, top_level = prod

    # Inventory on hand (latest snapshot at this location)
    cur.execute("""
        SELECT on_hand, as_of FROM inventory_snapshots
        WHERE location_id = ? AND sku = ?
        ORDER BY as_of DESC LIMIT 1
    """, (location_id, sku))
    inv_row = cur.fetchone()
    on_hand = float(inv_row[0]) if inv_row else 0.0
    inv_as_of = inv_row[1] if inv_row else None

    # 30-day velocity — stockout-adjusted, exactly like the engine: a SKU at
    # zero on-hand has its velocity measured over the days it could actually
    # sell (window start → last sale), not over the dead tail after it ran dry.
    cur.execute("""
        SELECT COALESCE(SUM(units_sold), 0), MAX(sale_date) FROM sales_daily
        WHERE location_id = ? AND sku = ? AND sale_date >= ? AND sale_date <= ?
    """, (location_id, sku, window_start_iso, as_of_iso))
    vrow = cur.fetchone()
    units_30d = float(vrow[0] or 0)
    last_sale = vrow[1]
    basis_days = VELOCITY_WINDOW_DAYS
    if on_hand <= 0 and units_30d > 0 and last_sale:
        sellable = (date.fromisoformat(last_sale) - window_start_d).days + 1
        basis_days = max(MIN_VELOCITY_BASIS_DAYS, min(VELOCITY_WINDOW_DAYS, sellable))
    velocity = units_30d / basis_days

    # OCS catalog data
    cur.execute("""
        SELECT pack_size, stock_status, unit_price, category, subcategory
        FROM ocs_catalog WHERE ocs_variant_number = ?
    """, (ocs_variant or "",))
    ocs = cur.fetchone()
    pack_size = int(ocs[0]) if ocs and ocs[0] else 1
    ocs_stock = ocs[1] if ocs else None
    ocs_price = float(ocs[2]) if ocs and ocs[2] else None
    category = ocs[3] if ocs else None
    subcategory = ocs[4] if ocs else None

    # Hero (anchor) override, if any. NOTE: the previous version queried
    # min_qty/max_qty columns that don't exist in anchor_overrides — that (and
    # SUM(units) vs units_sold) made every diagnose call crash since launch.
    cur.execute("""
        SELECT mode FROM anchor_overrides
        WHERE sku = ? AND location_id = ?
    """, (sku, location_id))
    anchor = cur.fetchone()
    anchor_override = anchor[0] if anchor else None  # 'include' | 'exclude' | None

    # Hero membership — same (cached) ranking the engine uses, so the ceiling
    # in the math below matches what the report actually ran.
    from jobs.reorder_engine import compute_top_skus_per_store
    try:
        is_hero = sku in compute_top_skus_per_store(conn, as_of_date=as_of).get(location_id, set())
    except Exception:
        is_hero = False
    ceiling_used = hero_ceiling if is_hero else regular_ceiling

    inputs = {
        "name": name,
        "brand": brand,
        "lp": lp,
        "ocs_variant_number": ocs_variant,
        "top_level_classification": top_level,
        "category": category,
        "subcategory": subcategory,
        "on_hand": on_hand,
        "inventory_as_of": inv_as_of,
        "units_sold_30d": units_30d,
        "velocity_per_day": round(velocity, 3),
        "velocity_basis_days": basis_days,
        "pack_size": pack_size,
        "ocs_stock_status": ocs_stock,
        "ocs_unit_price": ocs_price,
        "anchor_override": anchor_override,
        "is_hero": is_hero,
        "settings_applied": {
            "hero_ceiling_days": hero_ceiling,
            "regular_ceiling_days": regular_ceiling,
            "ceiling_used": ceiling_used,
            "coverage_days": coverage_days,
            "min_velocity": min_velocity,
            "pack_size_min_pct": pack_min_raw,
            "overstock_days": overstock_days,
            "stockout_imminent_days": stockout_days,
            "stockout_min_velocity": stockout_min_vel,
        }
    }

    # Days of cover
    days_of_cover = on_hand / velocity if velocity > 0 else float("inf")
    inputs["days_of_cover"] = (round(days_of_cover, 1)
                                if days_of_cover != float("inf") else "infinity")

    # ----- Run checks in engine order -----
    checks: list[dict] = []

    # CHECK 1: top_level filter (default: Cannabis only)
    if top_level and top_level not in ("Cannabis", "Accessories"):
        checks.append({
            "name": "top_level_filter",
            "passed": False,
            "explanation": f"SKU classified as '{top_level}', not Cannabis. Reorder report shows Cannabis by default.",
            "values": {"top_level": top_level},
        })
    else:
        checks.append({
            "name": "top_level_filter",
            "passed": True,
            "explanation": f"Classification = '{top_level}' (Cannabis path)",
            "values": {"top_level": top_level},
        })

    # CHECK 2: OCS stock status
    if ocs_stock == "NO":
        checks.append({
            "name": "ocs_stock_status",
            "passed": False,
            "explanation": "OCS says this SKU is currently out of stock. Filtered unless 'Include OCS out-of-stock' toggle is on.",
            "values": {"ocs_stock_status": ocs_stock},
        })
    else:
        checks.append({
            "name": "ocs_stock_status",
            "passed": True,
            "explanation": f"OCS stock = '{ocs_stock or 'unknown'}'",
            "values": {"ocs_stock_status": ocs_stock},
        })

    # CHECK 3: Minimum velocity floor
    if velocity < min_velocity:
        checks.append({
            "name": "min_velocity_floor",
            "passed": False,
            "explanation": f"Velocity {velocity:.3f}/day is below minimum floor {min_velocity}/day.",
            "values": {"velocity": velocity, "min_velocity": min_velocity},
        })
    else:
        checks.append({
            "name": "min_velocity_floor",
            "passed": True,
            "explanation": f"Velocity {velocity:.3f}/day ≥ floor {min_velocity}",
            "values": {"velocity": velocity, "min_velocity": min_velocity},
        })

    # CHECK 4: Overstock (days_of_cover > overstock_days threshold)
    if velocity > 0 and days_of_cover > overstock_days:
        checks.append({
            "name": "overstock_check",
            "passed": False,
            "explanation": f"Already have {days_of_cover:.0f} days of cover, exceeds overstock threshold of {overstock_days} days. No reorder needed.",
            "values": {"days_of_cover": days_of_cover, "overstock_days": overstock_days},
        })
    else:
        checks.append({
            "name": "overstock_check",
            "passed": True,
            "explanation": (f"Days of cover {days_of_cover:.1f} < overstock threshold {overstock_days}"
                            if days_of_cover != float("inf")
                            else "Zero velocity — overstock check N/A"),
            "values": {"days_of_cover": days_of_cover if days_of_cover != float("inf") else None,
                       "overstock_days": overstock_days},
        })

    # CHECK 5: Reorder trigger (min-max). The engine only orders when days of
    # cover has dropped to the trigger (order_cycle + lead_time). Above it,
    # reorder_qty = 0 even though urgency may read "ok": not at trigger yet.
    days_cover_num = on_hand / velocity if velocity > 0 else 0.0
    if velocity > 0 and days_cover_num > coverage_days:
        checks.append({
            "name": "reorder_trigger",
            "passed": False,
            "explanation": (f"{days_cover_num:.1f} days of cover is above the reorder "
                            f"trigger of {coverage_days} days (order cycle + lead time). "
                            f"Not due for reorder yet."),
            "values": {"days_of_cover": round(days_cover_num, 1), "coverage_days": coverage_days},
        })
    else:
        checks.append({
            "name": "reorder_trigger",
            "passed": True,
            "explanation": (f"{days_cover_num:.1f} days of cover ≤ trigger of "
                            f"{coverage_days} days — due for reorder"
                            if velocity > 0 else "Zero velocity — trigger check N/A"),
            "values": {"days_of_cover": round(days_cover_num, 1), "coverage_days": coverage_days},
        })

    # CHECK 6: Pack-size threshold (uses the Hero ceiling when the SKU is a
    # Hero at this store — same as the engine — and mirrors the engine's
    # stockout-imminent override: an active seller about to hit zero gets one
    # full case even when demand is below the case threshold).
    import math as _math
    target = velocity * ceiling_used
    shortfall = max(0, _math.ceil(target) - on_hand)
    cases_needed_raw = shortfall / pack_size if pack_size > 0 else 0
    override_fires = (days_cover_num < stockout_days and velocity >= stockout_min_vel
                      and shortfall > 0)
    if (velocity > 0 and shortfall > 0 and cases_needed_raw < pack_min_fraction
            and not override_fires):
        checks.append({
            "name": "pack_size_threshold",
            "passed": False,
            "explanation": (f"Engine would want {shortfall:.0f} units; that's only "
                            f"{cases_needed_raw:.2f} of a {pack_size}-pack case "
                            f"({(cases_needed_raw*100):.0f}%), below the {(pack_min_fraction*100):.0f}% threshold. "
                            f"Skipped to avoid ordering a near-empty case. (Stockout-imminent "
                            f"override needs < {stockout_days:.0f} days cover AND velocity ≥ "
                            f"{stockout_min_vel}/day; velocity here is {velocity:.2f}.)"),
            "values": {
                "shortfall_units": round(shortfall, 2),
                "pack_size": pack_size,
                "case_fraction_needed": round(cases_needed_raw, 3),
                "pack_min_fraction": pack_min_fraction,
                "ceiling_used": ceiling_used,
            },
        })
    else:
        if velocity == 0:
            note = "Zero velocity — pack-size check N/A"
        elif shortfall <= 0:
            note = "No shortfall — already have enough"
        elif override_fires and cases_needed_raw < pack_min_fraction:
            note = (f"Below case threshold, but stockout-imminent override fires "
                    f"({days_cover_num:.1f} days cover < {stockout_days:.0f} and velocity "
                    f"{velocity:.2f} ≥ {stockout_min_vel}/day) → one full case")
        else:
            note = (f"Engine wants {shortfall:.0f} units = {cases_needed_raw:.2f} "
                    f"of a {pack_size}-pack ≥ {(pack_min_fraction*100):.0f}% threshold")
        checks.append({
            "name": "pack_size_threshold",
            "passed": True,
            "explanation": note,
            "values": {
                "shortfall_units": round(shortfall, 2),
                "pack_size": pack_size,
                "case_fraction_needed": round(cases_needed_raw, 3) if velocity > 0 else None,
                "ceiling_used": ceiling_used,
            },
        })

    # ----- Verdict -----
    excluded = [c for c in checks if not c["passed"]]
    if excluded:
        verdict = "excluded"
        excluded_reason = excluded[0]["name"] + ": " + excluded[0]["explanation"]
    else:
        verdict = "included"
        excluded_reason = None

    return {
        "sku": sku,
        "location_id": location_id,
        "found": True,
        "inputs": inputs,
        "checks": checks,
        "verdict": verdict,
        "excluded_reason": excluded_reason,
    }

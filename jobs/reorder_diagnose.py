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
    window_start_iso = (as_of - timedelta(days=VELOCITY_WINDOW_DAYS)).isoformat()

    cur = conn.cursor()

    # Load settings (same as engine does)
    cur.execute("SELECT key, value FROM app_settings")
    settings = {k: v for k, v in cur.fetchall()}

    hero_ceiling = int(settings.get("reorder.hero_ceiling_days", 14))
    regular_ceiling = int(settings.get("reorder.regular_ceiling_days", 7))
    min_velocity = float(settings.get("reorder.min_velocity", 0.0))
    pack_min_raw = float(settings.get("reorder.pack_size_min_fraction", 50))
    pack_min_fraction = pack_min_raw / 100.0 if pack_min_raw > 1.0 else pack_min_raw
    overstock_days = int(settings.get("reorder.overstock_days", 60))

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

    # 30-day velocity
    cur.execute("""
        SELECT COALESCE(SUM(units), 0) FROM sales_daily
        WHERE location_id = ? AND sku = ? AND sale_date >= ? AND sale_date <= ?
    """, (location_id, sku, window_start_iso, as_of_iso))
    units_30d = float(cur.fetchone()[0] or 0)
    velocity = units_30d / VELOCITY_WINDOW_DAYS

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

    # Min/max overrides if any
    cur.execute("""
        SELECT min_qty, max_qty FROM anchor_overrides
        WHERE sku = ? AND (location_id = ? OR location_id IS NULL)
        ORDER BY location_id DESC LIMIT 1
    """, (sku, location_id))
    anchor = cur.fetchone()
    min_qty = int(anchor[0]) if anchor and anchor[0] is not None else None
    max_qty = int(anchor[1]) if anchor and anchor[1] is not None else None

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
        "pack_size": pack_size,
        "ocs_stock_status": ocs_stock,
        "ocs_unit_price": ocs_price,
        "min_qty_anchor": min_qty,
        "max_qty_anchor": max_qty,
        "settings_applied": {
            "hero_ceiling_days": hero_ceiling,
            "regular_ceiling_days": regular_ceiling,
            "min_velocity": min_velocity,
            "pack_size_min_pct": pack_min_raw,
            "overstock_days": overstock_days,
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

    # CHECK 5: Pack-size threshold
    # The engine computes: target = velocity * ceiling, shortfall = target - on_hand
    # then asks: shortfall / pack_size >= pack_min_fraction?
    # We use regular_ceiling here unless we have strong reason to think it's a Hero
    # (which we'd need top-SKU data for; we use regular as a safe default in diagnostic)
    target = velocity * regular_ceiling
    shortfall = max(0, target - on_hand)
    cases_needed_raw = shortfall / pack_size if pack_size > 0 else 0
    if velocity > 0 and shortfall > 0 and cases_needed_raw < pack_min_fraction:
        checks.append({
            "name": "pack_size_threshold",
            "passed": False,
            "explanation": (f"Engine would want {shortfall:.2f} units; that's only "
                            f"{cases_needed_raw:.2f} of a {pack_size}-pack case "
                            f"({(cases_needed_raw*100):.0f}%), below the {(pack_min_fraction*100):.0f}% threshold. "
                            f"Skipped to avoid ordering a near-empty case."),
            "values": {
                "shortfall_units": round(shortfall, 2),
                "pack_size": pack_size,
                "case_fraction_needed": round(cases_needed_raw, 3),
                "pack_min_fraction": pack_min_fraction,
            },
        })
    else:
        if velocity == 0:
            note = "Zero velocity — pack-size check N/A"
        elif shortfall <= 0:
            note = "No shortfall — already have enough"
        else:
            note = (f"Engine wants {shortfall:.2f} units = {cases_needed_raw:.2f} "
                    f"of a {pack_size}-pack ≥ {(pack_min_fraction*100):.0f}% threshold")
        checks.append({
            "name": "pack_size_threshold",
            "passed": True,
            "explanation": note,
            "values": {
                "shortfall_units": round(shortfall, 2),
                "pack_size": pack_size,
                "case_fraction_needed": round(cases_needed_raw, 3) if velocity > 0 else None,
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

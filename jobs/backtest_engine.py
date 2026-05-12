"""
Backtest the reorder engine against historical data.

Given a historical inventory snapshot (e.g., Grove as of 2026-03-01), run the
same reorder logic the engine would have produced on that day — using only
sales velocity data from BEFORE that date — then measure what actually
happened in the days AFTER.

Key measurements:
  * Stockout detection: of items engine said "reorder N", how many actually
    stocked out in the window before engine recommendation would have arrived?
  * Dead stock: of items engine said "skip, low velocity", did they stay
    dead, or did demand appear?
  * Quantity sizing: for items engine recommended, was it too much, not
    enough, or roughly right based on actual post-date sales?

What we CAN'T measure:
  * Whether the store actually ordered what the engine suggested (they
    didn't — tool didn't exist yet). So "ordered vs received" isn't testable.
  * What WOULD have happened if the order arrived. We assume a constant
    4-day lead time.

Usage:
    python jobs/backtest_engine.py --as-of 2026-03-01 --location S1
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3

# Engine constants — must match reorder_engine.py
ORDER_CYCLE_DAYS = 7
LEAD_TIME_DAYS = 4
VELOCITY_WINDOW_DAYS = 30
COVERAGE_DAYS = ORDER_CYCLE_DAYS + LEAD_TIME_DAYS  # 11
CEILING_DAYS_DEFAULT = 21
MIN_VELOCITY_DEFAULT = 0.5  # aligned with reorder_engine's new default
# Top-SKU tier (must match reorder_engine.py)
TOP_SKU_COUNT = 50
TOP_SKU_CEILING_DAYS = 28
TOP_SKU_TRAILING_DAYS = 90
TOP_SKU_MIN_VELOCITY = 0.0


def compute_top_skus_for_location(
    conn, location_id: str, as_of_date: date,
    n: int = TOP_SKU_COUNT, trailing_days: int = TOP_SKU_TRAILING_DAYS,
) -> set[str]:
    """Top N SKUs at this location by revenue over the trailing window
    ending at as_of_date. Used by backtest to mirror the live engine's tier."""
    start = (as_of_date - timedelta(days=trailing_days)).isoformat()
    end = as_of_date.isoformat()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT sku FROM (
            SELECT sku, SUM(gross_revenue) AS rev
            FROM sales_daily
            WHERE location_id = ?
              AND sale_date >= ? AND sale_date < ?
            GROUP BY sku
        )
        WHERE rev > 0
        ORDER BY rev DESC
        LIMIT ?
        """,
        (location_id, start, end, n),
    )
    return {r[0] for r in cur.fetchall()}


def find_snapshot_date(conn, location_id: str, target_date: str) -> str | None:
    """Return the inventory_snapshots.as_of value at or closest before target_date."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT as_of FROM inventory_snapshots
        WHERE location_id = ? AND as_of <= ?
        ORDER BY as_of DESC LIMIT 1
        """,
        (location_id, target_date + " 23:59:59"),
    )
    r = cur.fetchone()
    return r[0] if r else None


def run_engine_as_of(
    conn,
    location_id: str,
    as_of_date: date,
    ceiling_days: int = CEILING_DAYS_DEFAULT,
    min_velocity: float = MIN_VELOCITY_DEFAULT,
    use_top_sku_tier: bool = True,
) -> list[dict]:
    """
    Run the reorder engine "as of" the given historical date.

    Uses:
      - Inventory snapshot closest to (but not after) as_of_date
      - Sales velocity from the 30 days BEFORE as_of_date
      - OCS catalog as we currently have it (limitation: we can't time-travel
        the catalog since we only have the current snapshot)

    Returns a list of dicts, one per SKU with stock or recent sales.
    """
    snap_as_of = find_snapshot_date(conn, location_id, as_of_date.isoformat())
    if not snap_as_of:
        raise ValueError(
            f"No inventory snapshot at or before {as_of_date} for location {location_id}. "
            "Import a Historical inventory export for this date first."
        )

    window_start = (as_of_date - timedelta(days=VELOCITY_WINDOW_DAYS)).isoformat()
    window_end = as_of_date.isoformat()

    # Custom SQL: inventory AT a specific as_of, velocity BEFORE that as_of.
    sql = """
    WITH frozen_inventory AS (
        SELECT sku, location_id, on_hand
        FROM inventory_snapshots
        WHERE location_id = ? AND as_of = ?
    ),
    velocity AS (
        SELECT sku, location_id,
               SUM(units_sold) AS units_window,
               SUM(gross_revenue) AS revenue_window
        FROM sales_daily
        WHERE location_id = ?
          AND sale_date >= ? AND sale_date < ?
        GROUP BY sku, location_id
    ),
    universe AS (
        SELECT sku, location_id FROM frozen_inventory
        UNION
        SELECT sku, location_id FROM velocity
    )
    SELECT
        u.sku,
        u.location_id,
        COALESCE(p.name, u.sku) AS product_name,
        p.category,
        p.top_level,
        p.brand,
        COALESCE(fi.on_hand, 0) AS on_hand,
        COALESCE(v.units_window, 0) AS units_window,
        COALESCE(v.revenue_window, 0.0) AS revenue_window,
        oc.pack_size AS ocs_pack_size,
        oc.unit_price AS ocs_unit_price,
        oc.stock_status AS ocs_stock_status
    FROM universe u
    LEFT JOIN frozen_inventory fi USING (sku, location_id)
    LEFT JOIN velocity v USING (sku, location_id)
    LEFT JOIN products p ON p.sku = u.sku
    LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = p.ocs_variant_number
    WHERE u.location_id = ?
    """
    cur = conn.cursor()
    cur.execute(sql, (
        location_id, snap_as_of,
        location_id, window_start, window_end,
        location_id,
    ))

    # Compute Top-N anchor SKUs for this location as of this date. Mirrors the
    # live engine. Passing use_top_sku_tier=False disables it (for A/B testing).
    top_skus: set[str] = set()
    if use_top_sku_tier:
        top_skus = compute_top_skus_for_location(conn, location_id, as_of_date)

    rows = []
    for r in cur.fetchall():
        (sku, loc, name, category, top_level, brand, on_hand,
         units_window, revenue_window, pack_size, unit_price, stock_status) = r
        velocity = units_window / VELOCITY_WINDOW_DAYS
        days_supply = (on_hand / velocity) if velocity > 0 else None

        # Tiered parameters
        is_top = sku in top_skus
        sku_ceiling = max(ceiling_days, TOP_SKU_CEILING_DAYS) if is_top else ceiling_days
        sku_min_velocity = TOP_SKU_MIN_VELOCITY if is_top else min_velocity

        # Engine logic — mirrors reorder_engine.compute_reorder_qty
        if velocity < sku_min_velocity:
            qty = 0
            decision = "skip_low_velocity"
        elif days_supply is not None and days_supply > COVERAGE_DAYS:
            qty = 0
            decision = "skip_sufficient_stock"
        else:
            target = math.ceil(sku_ceiling * velocity)
            qty = max(0, target - on_hand)
            decision = "reorder" if qty > 0 else "skip_at_ceiling"

        rows.append({
            "sku": sku,
            "product_name": name,
            "category": category,
            "top_level": top_level,
            "brand": brand,
            "location_id": loc,
            "is_top_sku": is_top,
            "on_hand_as_of": on_hand,
            "velocity_before": round(velocity, 3),
            "days_supply": round(days_supply, 1) if days_supply else None,
            "reorder_qty": qty,
            "decision": decision,
            "revenue_window": round(revenue_window, 2),
            "ocs_pack_size": pack_size,
            "ocs_unit_price": unit_price,
            "ocs_stock_status": stock_status,
        })
    return rows


def measure_outcomes(
    conn, rows: list[dict], as_of_date: date, lookforward_days: int = 30,
) -> list[dict]:
    """
    For each SKU the engine evaluated, look at what ACTUALLY happened in the
    days after as_of_date. Annotate each row with outcome data:

      - units_after: total units sold in the window after as_of_date
      - velocity_after: actual post-date velocity
      - would_have_stocked_out: True if on_hand / velocity_after < lead_time
      - outcome: classification of how engine did
    """
    end_date = as_of_date + timedelta(days=lookforward_days)
    window_start = as_of_date.isoformat()
    window_end = end_date.isoformat()

    loc = rows[0]["location_id"] if rows else None

    cur = conn.cursor()
    cur.execute(
        """
        SELECT sku, SUM(units_sold), SUM(gross_revenue)
        FROM sales_daily
        WHERE location_id = ? AND sale_date >= ? AND sale_date < ?
        GROUP BY sku
        """,
        (loc, window_start, window_end),
    )
    sales_after = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    results = []
    for row in rows:
        units_after, revenue_after = sales_after.get(row["sku"], (0, 0.0))
        velocity_after = units_after / lookforward_days

        # Would this SKU have run out before a new order arrived?
        # on_hand would cover demand for (on_hand / velocity_after) days;
        # a new order takes LEAD_TIME_DAYS to arrive.
        on_hand = row["on_hand_as_of"]
        if velocity_after > 0:
            days_until_empty = on_hand / velocity_after
            stocked_out = days_until_empty < LEAD_TIME_DAYS
        else:
            days_until_empty = None
            stocked_out = False

        # Outcome classification
        if row["decision"] == "reorder":
            if stocked_out:
                # Engine said reorder, SKU would have stocked out — correct call
                outcome = "correct_reorder_prevented_stockout"
            elif units_after == 0:
                outcome = "wasted_reorder_no_demand"
            elif velocity_after > 0 and row["reorder_qty"] > units_after * 2:
                outcome = "oversized_reorder"
            elif velocity_after > 0 and row["reorder_qty"] < units_after * 0.5:
                outcome = "undersized_reorder"
            else:
                outcome = "sized_reasonably"
        elif row["decision"] == "skip_low_velocity":
            if stocked_out:
                outcome = "missed_stockout_low_velocity"  # we wrongly skipped
            elif units_after > 0:
                outcome = "skipped_but_some_demand"
            else:
                outcome = "correct_skip_dead_stock"
        elif row["decision"] == "skip_sufficient_stock":
            if stocked_out:
                outcome = "missed_stockout_sufficient"  # we thought stock was fine
            else:
                outcome = "correct_skip_sufficient"
        elif row["decision"] == "skip_at_ceiling":
            outcome = "correct_skip_at_ceiling"
        else:
            outcome = "unknown"

        results.append({
            **row,
            "units_after": units_after,
            "velocity_after": round(velocity_after, 3),
            "revenue_after": round(revenue_after, 2),
            "days_until_empty": round(days_until_empty, 1) if days_until_empty else None,
            "stocked_out_in_window": stocked_out,
            "outcome": outcome,
        })
    return results


def summarize(results: list[dict], as_of_date: date, lookforward_days: int) -> dict:
    """Aggregate results into a human-readable summary."""
    by_decision = {}
    by_outcome = {}
    by_category_outcome = {}

    for r in results:
        by_decision[r["decision"]] = by_decision.get(r["decision"], 0) + 1
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
        cat = r["category"] or "(uncategorized)"
        key = (cat, r["outcome"])
        by_category_outcome[key] = by_category_outcome.get(key, 0) + 1

    total_reorders = sum(1 for r in results if r["decision"] == "reorder")
    correct_reorders = sum(1 for r in results
                          if r["outcome"] == "correct_reorder_prevented_stockout")
    # A reorder is "good" if it prevented a stockout OR was sized reasonably
    # (kept stock at sensible level, not dramatic miss either way).
    good_reorders = sum(1 for r in results
                       if r["outcome"] in ("correct_reorder_prevented_stockout",
                                           "sized_reasonably"))
    wasted_reorders = sum(1 for r in results
                         if r["outcome"] == "wasted_reorder_no_demand")
    missed_stockouts = sum(1 for r in results
                          if r["outcome"] in ("missed_stockout_low_velocity",
                                              "missed_stockout_sufficient"))

    total_skips = sum(1 for r in results if r["decision"].startswith("skip"))
    correct_skips = sum(1 for r in results
                       if r["outcome"].startswith("correct_skip"))

    return {
        "as_of_date": as_of_date.isoformat(),
        "lookforward_days": lookforward_days,
        "total_skus_evaluated": len(results),
        "by_decision": by_decision,
        "by_outcome": by_outcome,
        "reorders_recommended": total_reorders,
        "reorders_that_prevented_stockouts": correct_reorders,
        "reorders_good": good_reorders,
        "reorders_wasted_no_demand": wasted_reorders,
        "stockouts_missed": missed_stockouts,
        "skips_total": total_skips,
        "skips_correct": correct_skips,
        "by_category_outcome": {f"{cat}__{out}": n
                               for (cat, out), n in sorted(by_category_outcome.items())},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", required=True, help="Date (YYYY-MM-DD)")
    parser.add_argument("--location", required=True, help="Location ID (e.g., S1)")
    parser.add_argument("--lookforward", type=int, default=30)
    parser.add_argument("--db", default="db/sbinsights.db")
    parser.add_argument("--csv", help="Optional: write detailed results to this CSV")
    args = parser.parse_args()

    as_of_date = datetime.fromisoformat(args.as_of).date()

    conn = sqlite3.connect(args.db)
    print(f"Running backtest for {args.location} as of {as_of_date}...")
    print(f"Lookforward window: {args.lookforward} days")
    print()

    rows = run_engine_as_of(conn, args.location, as_of_date)
    print(f"Engine evaluated {len(rows)} SKUs.")

    results = measure_outcomes(conn, rows, as_of_date, args.lookforward)
    summary = summarize(results, as_of_date, args.lookforward)

    # Pretty-print summary
    print()
    print("=" * 70)
    print(f"BACKTEST SUMMARY — {args.location} as of {as_of_date}")
    print("=" * 70)
    print(f"SKUs evaluated: {summary['total_skus_evaluated']}")
    print()
    print("Engine decisions:")
    for k, v in sorted(summary["by_decision"].items()):
        print(f"  {k:<30} {v:>6}")
    print()
    print("Outcomes:")
    for k, v in sorted(summary["by_outcome"].items(), key=lambda x: -x[1]):
        print(f"  {k:<40} {v:>6}")
    print()
    print("Key metrics:")
    if summary["reorders_recommended"] > 0:
        pct_correct = 100 * summary["reorders_that_prevented_stockouts"] / summary["reorders_recommended"]
        print(f"  Reorder precision:  {summary['reorders_that_prevented_stockouts']}/{summary['reorders_recommended']} "
              f"= {pct_correct:.1f}% prevented a stockout")
    if summary["skips_total"] > 0:
        pct_skip = 100 * summary["skips_correct"] / summary["skips_total"]
        print(f"  Skip precision:     {summary['skips_correct']}/{summary['skips_total']} "
              f"= {pct_skip:.1f}% were correct skips")
    print(f"  Stockouts missed:   {summary['stockouts_missed']}  (skips that shouldn't have been skips)")
    print()

    if args.csv:
        import csv as csvmod
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            if results:
                writer = csvmod.DictWriter(f, fieldnames=list(results[0].keys()))
                writer.writeheader()
                writer.writerows(results)
        print(f"→ Wrote detailed results to {args.csv}")


if __name__ == "__main__":
    main()

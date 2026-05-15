"""
Sale flag detector
==================

Determines whether each SKU at each store is:
  - Currently on sale (active LTO / ongoing promo)
  - Recently ended (sale activity in last 30 days but tapered off)
  - Not on sale

Honest design notes:
- Uses discount_lines (from Cova Discounts CSV) as ground truth — not the ltos
  table, which is mostly empty since you haven't been manually entering LTOs.
- "Real" promotion = discount_pct >= 10%. Filters out staff/member small discounts.
- "Significant day" = >= 30% of units sold that day had a real discount applied.
- "Active sale" = 3+ significant days in last 7
- "Recently ended" = significant activity in last 30, but <3 days in last 7
- "Not on sale" = nothing recent

These thresholds are heuristics — adjust if false-positive rate is high in
practice.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, timedelta


# How much of a discount counts as a real promotional sale (vs. small
# member/staff discounts). 10% is a sane minimum for "real" sales.
MIN_DISCOUNT_PCT = 10.0

# What fraction of units on a given day must have been discounted for that
# day to count as a "real promo day." Tunable if the heuristic feels off.
MIN_DISCOUNTED_UNITS_FRAC = 0.30

# Sliding windows for classification
ACTIVE_WINDOW_DAYS = 7
RECENT_WINDOW_DAYS = 30

# A SKU needs at least this many "real promo days" in the active window to
# be flagged as Active Sale
MIN_ACTIVE_DAYS = 3


def compute_sale_flags(conn: sqlite3.Connection,
                       as_of: date | None = None) -> dict:
    """Return a per-(location_id, sku) dict of sale flag info.

    Returns:
        {
          (location_id, sku): {
            "status": "active" | "recent" | "none",
            "active_days_in_7": int,
            "days_in_30": int,
            "avg_discount_pct": float,
            "last_promo_date": "YYYY-MM-DD" | None,
            "days_since_last_promo": int | None,
          }
        }
    """
    if as_of is None:
        as_of = date.today()
    window_30_start = (as_of - timedelta(days=RECENT_WINDOW_DAYS)).isoformat()
    window_7_start = (as_of - timedelta(days=ACTIVE_WINDOW_DAYS)).isoformat()

    cur = conn.cursor()

    # First, aggregate discount activity per (location, sku, day):
    #   - total units sold that day
    #   - units with a "real" discount (>= MIN_DISCOUNT_PCT)
    #   - avg discount % on discounted units
    #
    # Note: discount_lines table schema differs slightly between deployments.
    # We use defensive column access. Fields expected:
    #   location_id, sku, sale_date, qty, discount_pct
    try:
        cur.execute(f"""
            SELECT location_id, sku, sale_date,
                   SUM(qty) AS total_qty,
                   SUM(CASE WHEN discount_pct >= ? THEN qty ELSE 0 END) AS discounted_qty,
                   AVG(CASE WHEN discount_pct >= ? THEN discount_pct ELSE NULL END) AS avg_pct
            FROM discount_lines
            WHERE sale_date >= ?
            GROUP BY location_id, sku, sale_date
        """, (MIN_DISCOUNT_PCT, MIN_DISCOUNT_PCT, window_30_start))
    except sqlite3.OperationalError as e:
        # Table might not exist on fresh installs; return empty
        return {}

    # Bucket by (location, sku); per-day counts
    per_sku: dict[tuple[str, str], dict] = defaultdict(lambda: {
        "promo_days_7": 0,
        "promo_days_30": 0,
        "discount_pcts": [],
        "last_promo_date": None,
    })

    for loc_id, sku, sale_date, total_qty, disc_qty, avg_pct in cur.fetchall():
        if not total_qty or total_qty <= 0:
            continue
        # Is this a "real promo day"? Requires the discounted-units fraction
        # to exceed our threshold.
        frac = (disc_qty or 0) / total_qty
        if frac < MIN_DISCOUNTED_UNITS_FRAC:
            continue  # not enough units discounted to count as a real promo

        key = (loc_id, sku)
        bucket = per_sku[key]
        bucket["promo_days_30"] += 1
        if sale_date >= window_7_start:
            bucket["promo_days_7"] += 1
        if avg_pct is not None:
            bucket["discount_pcts"].append(avg_pct)
        # Track the latest promo date
        if bucket["last_promo_date"] is None or sale_date > bucket["last_promo_date"]:
            bucket["last_promo_date"] = sale_date

    # Now classify each SKU
    out: dict[tuple[str, str], dict] = {}
    for (loc_id, sku), bucket in per_sku.items():
        days_30 = bucket["promo_days_30"]
        days_7 = bucket["promo_days_7"]
        last_dt = bucket["last_promo_date"]
        days_since = None
        if last_dt:
            try:
                days_since = (as_of - date.fromisoformat(last_dt)).days
            except ValueError:
                days_since = None

        if days_7 >= MIN_ACTIVE_DAYS:
            status = "active"
        elif days_30 > 0:
            # Activity in last 30 days but not enough in last 7 → recently ended
            status = "recent"
        else:
            continue  # no recent activity, skip

        avg_pct = (sum(bucket["discount_pcts"]) / len(bucket["discount_pcts"])
                   if bucket["discount_pcts"] else 0.0)

        out[(loc_id, sku)] = {
            "status": status,
            "active_days_in_7": days_7,
            "days_in_30": days_30,
            "avg_discount_pct": round(avg_pct, 1),
            "last_promo_date": last_dt,
            "days_since_last_promo": days_since,
        }

    return out

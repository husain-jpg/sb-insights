"""
Run backtests across multiple historical dates and compare results.

Given a DB that has multiple inventory_snapshots for a location (one per
historical export you've imported), this runs the backtest as of each
available snapshot date and produces a cross-date summary.

Usage:
    python jobs/backtest_all.py --location S1 --db db/sbinsights.db

    # Optional: restrict to specific dates
    python jobs/backtest_all.py --location S1 --dates 2026-01-15,2026-02-01,2026-03-01,2026-04-01
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs.backtest_engine import run_engine_as_of, measure_outcomes, summarize


def list_available_snapshot_dates(conn, location_id: str) -> list[str]:
    """Distinct snapshot dates available for this location, chronological."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT DATE(as_of) AS day
        FROM inventory_snapshots
        WHERE location_id = ?
        ORDER BY day
        """,
        (location_id,),
    )
    return [r[0] for r in cur.fetchall()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--location", required=True)
    parser.add_argument("--db", default="db/sbinsights.db")
    parser.add_argument(
        "--dates",
        help="Comma-separated dates (YYYY-MM-DD). If omitted, uses all available snapshot dates.",
    )
    parser.add_argument("--lookforward", type=int, default=30)
    parser.add_argument("--min-velocity", type=float, default=0.3,
                        help="Tune min_velocity floor (default 0.3)")
    parser.add_argument("--ceiling-days", type=int, default=21,
                        help="Tune ceiling_days (default 21)")
    parser.add_argument("--no-top-tier", action="store_true",
                        help="Disable the Top-50 anchor tier (for A/B comparison)")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)

    if args.dates:
        dates = [d.strip() for d in args.dates.split(",")]
    else:
        dates = list_available_snapshot_dates(conn, args.location)

    # Drop today's auto-generated snapshot — it's not useful for backtesting
    # since we have no future data to compare against.
    today_str = datetime.today().date().isoformat()
    dates = [d for d in dates if d < today_str and
             (datetime.today().date() - datetime.fromisoformat(d).date()).days >= 14]

    if not dates:
        print(f"No usable snapshot dates found for {args.location}.")
        print("Need to import at least one Historical export dated 14+ days ago.")
        return

    print(f"Backtesting {args.location} across {len(dates)} dates: {', '.join(dates)}")
    print(f"Lookforward: {args.lookforward} days  ·  min_velocity: {args.min_velocity}  ·  ceiling: {args.ceiling_days}")
    print()

    all_summaries = []
    for d in dates:
        as_of = datetime.fromisoformat(d).date()
        # Cap lookforward at (today - as_of) so we don't count sales we haven't observed
        days_available = (datetime.today().date() - as_of).days
        lookforward = min(args.lookforward, days_available)

        try:
            rows = run_engine_as_of(
                conn, args.location, as_of,
                ceiling_days=args.ceiling_days,
                min_velocity=args.min_velocity,
                use_top_sku_tier=not args.no_top_tier,
            )
        except ValueError as e:
            print(f"  [{d}] SKIPPED: {e}")
            continue

        results = measure_outcomes(conn, rows, as_of, lookforward)
        summary = summarize(results, as_of, lookforward)
        summary["lookforward_actual"] = lookforward
        all_summaries.append(summary)

    if not all_summaries:
        print("No backtests ran successfully.")
        return

    # ---- Cross-date comparison table ----
    print("=" * 90)
    print(f"CROSS-DATE COMPARISON — {args.location}")
    print("=" * 90)
    print(f"  min_velocity={args.min_velocity}  ceiling_days={args.ceiling_days}")
    print()

    header = f"{'Date':<12} {'SKUs':>5} {'Reord':>6} {'Hit%':>5} {'Good%':>6} {'Skip':>5} {'SkipOK%':>7} {'Missed':>7} {'Wasted':>7} {'Look':>5}"
    print(header)
    print("-" * len(header))
    for s in all_summaries:
        reorder_total = s.get("reorders_recommended", 0)
        reorder_hit = s.get("reorders_that_prevented_stockouts", 0)
        reorder_good = s.get("reorders_good", 0)
        skip_total = s.get("skips_total", 0)
        skip_ok = s.get("skips_correct", 0)
        hit_pct = (100 * reorder_hit / reorder_total) if reorder_total else 0
        good_pct = (100 * reorder_good / reorder_total) if reorder_total else 0
        skip_pct = (100 * skip_ok / skip_total) if skip_total else 0
        print(
            f"{s['as_of_date']:<12} "
            f"{s['total_skus_evaluated']:>5} "
            f"{reorder_total:>6} "
            f"{hit_pct:>4.0f}% "
            f"{good_pct:>5.0f}% "
            f"{skip_total:>5} "
            f"{skip_pct:>6.0f}% "
            f"{s['stockouts_missed']:>7} "
            f"{s.get('reorders_wasted_no_demand', 0):>7} "
            f"{s.get('lookforward_actual', args.lookforward):>3}d"
        )

    print()
    print("Columns:")
    print("  Reord   = reorder recommendations issued")
    print("  Hit%    = of reorders, % that prevented a real stockout (narrow win)")
    print("  Good%   = of reorders, % that were good (prevented stockout OR sized reasonably)")
    print("  Skip    = skip recommendations issued")
    print("  SkipOK% = of skips, % that were correct (SKU had no/low demand)")
    print("  Missed  = SKUs skipped that actually needed reordering")
    print("  Wasted  = reorders with zero post-demand (bought for nothing)")
    print("  Look    = lookforward window in days")

    # ---- Category-level drift ----
    print()
    print("=" * 90)
    print("WORST CATEGORIES (by stockouts missed, summed across all dates)")
    print("=" * 90)
    cat_outcomes: dict[str, dict[str, int]] = {}
    for s in all_summaries:
        for key, n in s["by_category_outcome"].items():
            cat, outcome = key.split("__", 1)
            cat_outcomes.setdefault(cat, {"missed": 0, "wasted": 0, "correct_reorder": 0, "total": 0})
            cat_outcomes[cat]["total"] += n
            if "missed_stockout" in outcome:
                cat_outcomes[cat]["missed"] += n
            elif "wasted_reorder" in outcome:
                cat_outcomes[cat]["wasted"] += n
            elif "correct_reorder" in outcome:
                cat_outcomes[cat]["correct_reorder"] += n

    sorted_cats = sorted(cat_outcomes.items(), key=lambda kv: -kv[1]["missed"])
    print(f"{'Category':<25} {'SKUs':>5} {'Missed':>7} {'Wasted':>7} {'Correct':>8}")
    print("-" * 60)
    for cat, counts in sorted_cats[:15]:
        print(f"{cat[:24]:<25} {counts['total']:>5} {counts['missed']:>7} "
              f"{counts['wasted']:>7} {counts['correct_reorder']:>8}")


if __name__ == "__main__":
    main()

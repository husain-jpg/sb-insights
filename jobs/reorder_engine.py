"""
Reorder forecasting engine — v3 (min-max model).

Tuned for this retailer's actual operation:
- Weekly OCS orders (7-day cycle)
- 3-4 day lead time (using 4)
- Target: 3-week ceiling of sales-days on hand
- Ignore long-tail SKUs (< 2 units/week) — case-pack makes them over-buy

Formula (min-max):
    trigger_days  = ORDER_CYCLE_DAYS + LEAD_TIME_DAYS = 11
    ceiling_days  = 21                                           (3 weeks)
    min_velocity  = 0.3 units/day                                (~2/week)

    If on-hand days_supply > trigger_days:   skip (don't reorder)
    If velocity < min_velocity:              skip (long-tail, decide manually)
    Otherwise:
        raw_qty = max(0, ceil(ceiling_days × velocity) − on_hand)
        qty     = round_up_to_case_pack(raw_qty)

Urgency classification (unchanged from v2):
    CRITICAL    < LEAD_TIME_DAYS
    HIGH        < ORDER_CYCLE_DAYS
    MEDIUM      < COVERAGE_DAYS
    OK          ≥ above
    OVERSTOCK   > OVERSTOCK_DAYS
    DEAD_STOCK  stock > 0 but zero sales in window
    STOCKOUT    sales > 0 but zero stock

This module is database-agnostic: works with SQLite (manual-import phase)
and Postgres (Cova API phase) because it only uses ANSI SQL.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import date, timedelta

# These are DEFAULT values. The actual live values come from the app_settings
# table at runtime via _load_settings(conn). The defaults below are what gets
# seeded into the DB on first init.
#
# IMPORTANT: User has explicitly directed tighter ceilings to avoid overstocking
# in cannabis retail (rapid trend shifts, shelf-life sensitive). Risk accepted:
# more frequent stockouts in exchange for less obsolete inventory.
ORDER_CYCLE_DAYS = 7
LEAD_TIME_DAYS = 4
VELOCITY_WINDOW_DAYS = 30
COVERAGE_DAYS = ORDER_CYCLE_DAYS + LEAD_TIME_DAYS  # legacy global, recomputed per call
CEILING_DAYS_DEFAULT = 10
MIN_VELOCITY_DEFAULT = 0.0
OVERSTOCK_DAYS = 50

TOP_SKU_COUNT = 50                  # legacy global cap (only used if per_format=False)
TOP_SKU_PER_FORMAT_N = 5
TOP_SKU_CEILING_DAYS = 7
TOP_SKU_TRAILING_DAYS = 90
TOP_SKU_MIN_VELOCITY = 0.0

PACK_SIZE_MIN_FRACTION = 0.5


def _load_settings(conn) -> dict:
    """Pull current values for engine settings from app_settings table.
    Falls back to module defaults if a setting is missing. Cheap query — runs
    once per /api/reorder call.

    Returns a dict with all tunable engine knobs as keys."""
    defaults = {
        "reorder.order_cycle_days": ORDER_CYCLE_DAYS,
        "reorder.lead_time_days": LEAD_TIME_DAYS,
        "reorder.hero_ceiling_days": TOP_SKU_CEILING_DAYS,
        "reorder.regular_ceiling_days": CEILING_DAYS_DEFAULT,
        "reorder.min_velocity": MIN_VELOCITY_DEFAULT,
        "reorder.pack_size_min_fraction": PACK_SIZE_MIN_FRACTION,
        "reorder.overstock_days": OVERSTOCK_DAYS,
        "hero.top_n_per_format": TOP_SKU_PER_FORMAT_N,
        "hero.lookback_days": TOP_SKU_TRAILING_DAYS,
    }
    try:
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM app_settings")
        for k, v in cur.fetchall():
            if k in defaults:
                defaults[k] = float(v)
    except Exception:
        # Table doesn't exist yet (e.g. fresh DB before init) — fall back.
        pass
    return defaults


@dataclass
class ReorderRec:
    sku: str
    product_name: str
    category: str | None
    category_path: str | None
    top_level: str | None  # 'Cannabis' | 'Accessories' | 'Other' | None
    brand: str | None
    location_id: str
    on_hand: int
    daily_velocity: float
    days_supply: float | None  # None means "infinite" (no sales)
    reorder_qty: int
    urgency: str
    revenue_30d: float
    wholesale_cost_est: float | None = None
    # OCS catalog fields — None when SKU has no OCS match (discontinued/accessory)
    ocs_variant: str | None = None
    ocs_pack_size: int | None = None
    ocs_unit_price: float | None = None  # wholesale per EACH unit
    ocs_stock_status: str | None = None  # 'YES' | 'NO' | None
    reorder_cases: int | None = None  # reorder_qty rounded up to full cases
    mix_multiplier: float = 1.0  # 1.0 when mix-aware reorder is off
    is_top_sku: bool = False  # True if SKU is in per-store Top N by trailing revenue

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def classify_urgency(on_hand: int, velocity: float, days_supply: float | None,
                     *,
                     coverage_days: int | None = None,
                     overstock_days: int | None = None,
                     lead_time_days: int | None = None,
                     order_cycle_days: int | None = None) -> str:
    """Map inventory + velocity to the urgency bucket."""
    effective_on_hand = max(0, on_hand)

    if velocity == 0 and effective_on_hand == 0:
        return "inactive"
    if velocity == 0 and effective_on_hand > 0:
        return "dead_stock"
    if effective_on_hand == 0 and velocity > 0:
        return "stockout"
    assert days_supply is not None
    _coverage = coverage_days if coverage_days is not None else COVERAGE_DAYS
    _overstock = overstock_days if overstock_days is not None else OVERSTOCK_DAYS
    _lead = lead_time_days if lead_time_days is not None else LEAD_TIME_DAYS
    _cycle = order_cycle_days if order_cycle_days is not None else ORDER_CYCLE_DAYS
    if days_supply < _lead:
        return "critical"
    if days_supply < _cycle:
        return "high"
    if days_supply < _coverage:
        return "medium"
    if days_supply > _overstock:
        return "overstock"
    return "ok"


def compute_reorder_qty(
    on_hand: int,
    velocity: float,
    *,
    ceiling_days: int = CEILING_DAYS_DEFAULT,
    min_velocity: float = MIN_VELOCITY_DEFAULT,
    coverage_days: int | None = None,
) -> int:
    """
    Min-max: only reorder if on-hand days_supply has dropped to the trigger
    (coverage_days). Then buy enough to bring us up to ceiling_days.
    SKUs below min_velocity are skipped entirely (long-tail / case-pack bloat).
    """
    if velocity < min_velocity:
        return 0
    effective_on_hand = max(0, on_hand)
    days_supply = effective_on_hand / velocity if velocity > 0 else 0
    # Only order when stock has dropped to trigger
    _coverage = coverage_days if coverage_days is not None else COVERAGE_DAYS
    if days_supply > _coverage:
        return 0
    target_units = math.ceil(ceiling_days * velocity)
    return max(0, target_units - effective_on_hand)


def round_to_pack(raw_qty: int, pack_size: int | None,
                  *, min_fraction: float | None = None) -> tuple[int, int | None]:
    """Round qty up to nearest full case, OR skip entirely if it's below
    min_fraction of a case (default = module PACK_SIZE_MIN_FRACTION)."""
    if not pack_size or pack_size <= 1:
        return raw_qty, None
    if raw_qty <= 0:
        return 0, 0
    fraction = raw_qty / pack_size
    threshold = min_fraction if min_fraction is not None else PACK_SIZE_MIN_FRACTION
    if fraction < threshold:
        return 0, 0
    cases = math.ceil(raw_qty / pack_size)
    return cases * pack_size, cases


# Mix-aware reorder constants — keep these dampened to avoid over-correction.
# Tuning notes in comments above the compute_mix_multipliers function.
MIX_MULT_MAX = 1.25          # cap: don't order more than 25% above normal
MIX_MULT_MIN = 0.70          # floor: don't order less than 70% of normal
MIX_MIN_INV_PCT = 3.0        # only apply multiplier if category has ≥3% inventory share
MIX_MIN_SALES_PCT = 3.0      # AND ≥3% sales share (filters tiny/noisy categories)


def compute_mix_multipliers(
    conn,
    *,
    location_id: str | None = None,
    top_level: str = "Cannabis",
    days: int = 90,
    as_of_date: date | None = None,
    inflation_cap_pct: float = 15.0,
) -> dict[str, float]:
    """
    Compute per-category order-quantity multipliers based on mix drift.

    Returns a dict: {category_name: multiplier}
    - Underweight categories (drift < -3pts) get multipliers > 1.0 (order more)
    - Overweight categories (drift > +3pts) get multipliers < 1.0 (order less)
    - Balanced categories or small-share categories get 1.0 (no change)

    Drift = (share of current inventory cost) − (share of recent sales revenue).

    The goal is to nudge the mix toward the sales shape over a few order cycles,
    not to balance spend in a single week. Expect a modest total increase when
    mix-aware is active — that reflects genuine under-buying that's accumulated.

    Safety: if the raw multipliers would inflate total reorder spend by more
    than `inflation_cap_pct` (default 15%), the underweight multipliers are
    uniformly scaled down until the cap is respected. Protects against
    pathological cases where many categories are underweight at once.
    """
    as_of = (as_of_date or date.today()).isoformat()

    # Inventory cost by category
    inv_sql = """
        WITH latest_inv AS (
            SELECT sku, location_id, on_hand FROM (
                SELECT sku, location_id, on_hand,
                       ROW_NUMBER() OVER (PARTITION BY sku, location_id ORDER BY as_of DESC) rn
                FROM inventory_snapshots
            ) t WHERE rn = 1 AND on_hand > 0
        )
        SELECT p.category,
               SUM(i.on_hand * COALESCE(oc.unit_price, pr.regular_price * 0.60, 0)) AS inv_cost
        FROM latest_inv i
        JOIN products p ON p.sku = i.sku
        LEFT JOIN prices pr ON pr.sku = i.sku AND pr.location_id = i.location_id
        LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = p.ocs_variant_number
        WHERE p.top_level = ? AND p.category IS NOT NULL
    """
    inv_params: list = [top_level]
    if location_id:
        inv_sql += " AND i.location_id = ?"
        inv_params.append(location_id)
    inv_sql += " GROUP BY p.category"

    # Sales revenue by category
    sales_sql = """
        SELECT p.category, SUM(s.gross_revenue) AS sales
        FROM sales_daily s
        JOIN products p ON p.sku = s.sku
        WHERE s.sale_date >= date(?, '-' || ? || ' days')
          AND s.sale_date <= ?
          AND p.top_level = ?
          AND p.category IS NOT NULL
    """
    sales_params: list = [as_of, days, as_of, top_level]
    if location_id:
        sales_sql += " AND s.location_id = ?"
        sales_params.append(location_id)
    sales_sql += " GROUP BY p.category"

    cur = conn.cursor()
    cur.execute(inv_sql, inv_params)
    inv_by_cat = {r[0]: float(r[1] or 0) for r in cur.fetchall()}
    cur.execute(sales_sql, sales_params)
    sales_by_cat = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

    total_inv = sum(inv_by_cat.values()) or 1
    total_sales = sum(sales_by_cat.values()) or 1

    # --- Step 1: raw drift-based multipliers (gentler than v1) ---
    raw: dict[str, float] = {}
    for cat in set(inv_by_cat) | set(sales_by_cat):
        inv_pct = (inv_by_cat.get(cat, 0) / total_inv) * 100
        sales_pct = (sales_by_cat.get(cat, 0) / total_sales) * 100

        # Skip small categories — too noisy to multiplier-adjust
        if inv_pct < MIX_MIN_INV_PCT and sales_pct < MIX_MIN_SALES_PCT:
            raw[cat] = 1.0
            continue

        drift = inv_pct - sales_pct

        if abs(drift) < 1.5:
            mult = 1.0
        elif drift <= -5:
            mult = 1.20  # was 1.25 — dampened
        elif drift <= -3:
            mult = 1.12  # was 1.15 — dampened
        elif drift >= 5:
            mult = 0.75  # was 0.70 — dampened
        elif drift >= 3:
            mult = 0.88  # was 0.85 — dampened
        else:
            mult = 1.0  # |drift| between 1.5 and 3: nudge zone, don't adjust yet

        raw[cat] = max(MIX_MULT_MIN, min(MIX_MULT_MAX, mult))

    # --- Step 2: inflation safety cap ---
    # Estimate how much total reorder spend the raw multipliers would inflate
    # (sales-weighted proxy). If > inflation_cap_pct, scale underweight
    # multipliers down uniformly until the cap is satisfied.
    up_weight = 0.0
    down_weight = 0.0
    base_weight = 0.0
    for cat, m in raw.items():
        w = sales_by_cat.get(cat, 0)
        base_weight += w
        if m > 1.0:
            up_weight += (m - 1.0) * w
        elif m < 1.0:
            down_weight += (1.0 - m) * w

    if base_weight > 0:
        net_inflation_pct = ((up_weight - down_weight) / base_weight) * 100
        if net_inflation_pct > inflation_cap_pct:
            # Need to dampen up-side. Target up-side = cap + down-side.
            target_up = (inflation_cap_pct / 100) * base_weight + down_weight
            if up_weight > 0:
                up_scale = max(0.0, target_up / up_weight)
                for cat, m in raw.items():
                    if m > 1.0:
                        new_boost = (m - 1.0) * up_scale
                        raw[cat] = round(1.0 + new_boost, 3)

    return raw


def compute_top_skus_per_store(
    conn,
    *,
    as_of_date: date | None = None,
    trailing_days: int | None = None,
    n: int = TOP_SKU_COUNT,
    per_format: bool = True,
    per_format_top_n: int | None = None,
) -> dict[str, set[str]]:
    """
    Return {location_id: set_of_top_skus} — Heroes for each store.

    Two ranking modes:

    GLOBAL (per_format=False, the legacy behavior):
      Top-N SKUs per store by trailing revenue, regardless of category/size.
      Tends to be dominated by fast-format SKUs (3.5g flower, 3-pack pre-rolls).

    PER-FORMAT (per_format=True, the default now):
      For each (category, size) format bucket, take the top per_format_top_n
      SKUs by trailing revenue. This ensures slower-format products like
      28g flower or concentrates also get Hero protection within their
      own lane, instead of being crowded out by 3.5g volume.

      Guardrail: bucket Hero count = min(per_format_top_n, ceil(bucket_skus_with_sales / 3))
      so tiny buckets don't end up with 5 of 8 SKUs flagged Hero.

    Manager overrides (anchor_overrides table) still apply on top of either mode.
    """
    # Resolve from settings if caller didn't override
    settings = _load_settings(conn)
    if trailing_days is None:
        trailing_days = int(settings["hero.lookback_days"])
    if per_format_top_n is None:
        per_format_top_n = int(settings["hero.top_n_per_format"])

    end = as_of_date or date.today()
    start = end - timedelta(days=trailing_days)
    cur = conn.cursor()

    if not per_format:
        # Legacy global top-N
        cur.execute("""
            WITH ranked AS (
                SELECT sku, location_id,
                       SUM(gross_revenue) AS rev,
                       ROW_NUMBER() OVER (
                           PARTITION BY location_id
                           ORDER BY SUM(gross_revenue) DESC
                       ) AS rn
                FROM sales_daily
                WHERE sale_date >= ? AND sale_date < ?
                GROUP BY sku, location_id
            )
            SELECT sku, location_id FROM ranked WHERE rn <= ? AND rev > 0
        """, (start.isoformat(), end.isoformat(), n))
        by_loc: dict[str, set[str]] = {}
        for sku, loc in cur.fetchall():
            by_loc.setdefault(loc, set()).add(sku)
    else:
        # Per-format ranking. Format key = (category, size).
        # Step 1: pull per-(sku, store) revenue + product format info.
        cur.execute("""
            SELECT s.sku, s.location_id,
                   COALESCE(p.category, 'Unknown') AS category,
                   COALESCE(p.size, '') AS size,
                   SUM(s.gross_revenue) AS rev
            FROM sales_daily s
            LEFT JOIN products p ON p.sku = s.sku
            WHERE s.sale_date >= ? AND s.sale_date < ?
              AND p.top_level = 'Cannabis'  -- Only Cannabis gets Hero treatment
            GROUP BY s.sku, s.location_id, p.category, p.size
            HAVING SUM(s.gross_revenue) > 0
        """, (start.isoformat(), end.isoformat()))

        # Group by (location, category, size) and rank within each bucket.
        # bucket_rows[(loc, cat, size)] = [(sku, revenue), ...] sorted desc
        bucket_rows: dict[tuple[str, str, str], list[tuple[str, float]]] = {}
        for sku, loc, cat, size, rev in cur.fetchall():
            key = (loc, cat, size)
            bucket_rows.setdefault(key, []).append((sku, float(rev)))

        # Apply per-format top-N with guardrail
        by_loc = {}
        for (loc, cat, size), skus in bucket_rows.items():
            skus.sort(key=lambda x: x[1], reverse=True)
            # Guardrail: don't let a small bucket Hero-flag too many of its SKUs
            cap = min(per_format_top_n,
                      max(1, math.ceil(len(skus) / 3)))
            heroes_in_bucket = {s[0] for s in skus[:cap]}
            by_loc.setdefault(loc, set()).update(heroes_in_bucket)

    # Apply manager overrides: include forces a SKU in, exclude removes it.
    try:
        cur.execute("SELECT sku, location_id, mode FROM anchor_overrides")
        for sku, loc, mode in cur.fetchall():
            if mode == "include":
                by_loc.setdefault(loc, set()).add(sku)
            elif mode == "exclude":
                if loc in by_loc:
                    by_loc[loc].discard(sku)
    except Exception:
        pass  # table missing — ignore, no overrides

    return by_loc


def compute_all_reorders(
    conn,
    *,
    location_id: str | None = None,
    as_of_date: date | None = None,
    ceiling_days: int | None = None,
    min_velocity: float | None = None,
    mix_multipliers: dict[str, float] | None = None,
    use_top_sku_tier: bool = True,
) -> list[ReorderRec]:
    """
    Compute reorder recs for every (SKU, location) with stock or recent sales.

    All numeric parameters default to None, in which case live values from
    app_settings are used. Pass explicit values to override (useful for
    backtesting or scenario analysis).
    """
    # Resolve runtime knobs from app_settings (with kwargs taking priority)
    settings = _load_settings(conn)
    if ceiling_days is None:
        ceiling_days = int(settings["reorder.regular_ceiling_days"])
    if min_velocity is None:
        min_velocity = float(settings["reorder.min_velocity"])
    hero_ceiling = int(settings["reorder.hero_ceiling_days"])
    pack_min_fraction = float(settings["reorder.pack_size_min_fraction"])
    coverage_days = int(settings["reorder.order_cycle_days"]) + int(settings["reorder.lead_time_days"])
    overstock_days = int(settings["reorder.overstock_days"])

    as_of = (as_of_date or date.today()).isoformat()
    window_start = ((as_of_date or date.today()) - timedelta(days=VELOCITY_WINDOW_DAYS)).isoformat()

    top_skus_by_loc: dict[str, set[str]] = {}
    if use_top_sku_tier:
        top_skus_by_loc = compute_top_skus_per_store(conn, as_of_date=as_of_date)

    sql = """
    WITH latest_inventory AS (
        SELECT sku, location_id, on_hand FROM (
            SELECT sku, location_id, on_hand, as_of,
                   ROW_NUMBER() OVER (PARTITION BY sku, location_id ORDER BY as_of DESC) AS rn
            FROM inventory_snapshots
        ) t WHERE rn = 1
    ),
    velocity_30d AS (
        SELECT sku, location_id,
               SUM(units_sold) AS units_30d,
               SUM(gross_revenue) AS revenue_30d
        FROM sales_daily
        WHERE sale_date >= ? AND sale_date < ?
        GROUP BY sku, location_id
    ),
    universe AS (
        SELECT sku, location_id FROM latest_inventory
        UNION
        SELECT sku, location_id FROM velocity_30d
    )
    SELECT
        u.sku,
        u.location_id,
        COALESCE(p.name, u.sku) AS product_name,
        p.category,
        p.category_path,
        p.top_level,
        p.brand,
        COALESCE(li.on_hand, 0) AS on_hand,
        COALESCE(v.units_30d, 0) AS units_30d,
        COALESCE(v.revenue_30d, 0.0) AS revenue_30d,
        p.ocs_variant_number,
        oc.pack_size AS ocs_pack_size,
        oc.unit_price AS ocs_unit_price,
        oc.stock_status AS ocs_stock_status
    FROM universe u
    LEFT JOIN latest_inventory li USING (sku, location_id)
    LEFT JOIN velocity_30d v USING (sku, location_id)
    LEFT JOIN products p USING (sku)
    LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = p.ocs_variant_number
    """

    params: list = [window_start, as_of]
    if location_id:
        sql += " WHERE u.location_id = ?"
        params.append(location_id)

    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()

    recs: list[ReorderRec] = []
    for row in rows:
        (sku, loc_id, name, category, category_path, top_level, brand,
         on_hand, units_30d, revenue_30d,
         ocs_variant, ocs_pack_size, ocs_unit_price, ocs_stock_status) = row

        on_hand = int(on_hand or 0)
        effective_on_hand = max(0, on_hand)
        velocity = float(units_30d or 0) / VELOCITY_WINDOW_DAYS

        days_supply = effective_on_hand / velocity if velocity > 0 else None
        urgency = classify_urgency(
            on_hand, velocity, days_supply,
            coverage_days=coverage_days,
            overstock_days=overstock_days,
            lead_time_days=int(settings["reorder.lead_time_days"]),
            order_cycle_days=int(settings["reorder.order_cycle_days"]),
        )

        if urgency == "inactive":
            continue

        # Defaults; per-SKU adjustments below
        sku_ceiling = ceiling_days
        sku_min_velocity = min_velocity
        mix_mult = 1.0  # default, overridden below for non-Heroes when mix-aware is on

        # Top-SKU tier: Heroes use a FIXED tight ceiling regardless of mix-aware
        # multipliers. The user's stated preference is "rather stock out than
        # overstock" — Hero status already protects these SKUs from being dropped
        # from the reorder list, but inflation via mix-aware would defeat the
        # tight-ceiling intent.
        is_top = sku in top_skus_by_loc.get(loc_id, set())
        if is_top:
            sku_ceiling = hero_ceiling  # live from settings
            sku_min_velocity = TOP_SKU_MIN_VELOCITY
        else:
            # Non-Hero SKUs: apply mix-aware multiplier if provided.
            # (Mix-aware can usefully bump up underweight categories.)
            if mix_multipliers and category:
                mix_mult = mix_multipliers.get(category, 1.0)
                sku_ceiling = max(1, int(round(ceiling_days * mix_mult)))

        raw_qty = compute_reorder_qty(
            effective_on_hand, velocity,
            ceiling_days=sku_ceiling, min_velocity=sku_min_velocity,
            coverage_days=coverage_days,
        )
        pack_size = int(ocs_pack_size) if ocs_pack_size else None
        rounded_qty, cases = round_to_pack(raw_qty, pack_size, min_fraction=pack_min_fraction)

        wholesale = float(ocs_unit_price) if ocs_unit_price else None

        recs.append(ReorderRec(
            sku=sku,
            product_name=name or sku,
            category=category,
            category_path=category_path,
            top_level=top_level,
            brand=brand,
            location_id=loc_id,
            on_hand=on_hand,
            daily_velocity=round(velocity, 2),
            days_supply=round(days_supply, 1) if days_supply is not None else None,
            reorder_qty=rounded_qty,
            reorder_cases=cases,
            urgency=urgency,
            revenue_30d=round(float(revenue_30d or 0), 2),
            wholesale_cost_est=wholesale,
            ocs_variant=ocs_variant,
            ocs_pack_size=pack_size,
            ocs_unit_price=wholesale,
            ocs_stock_status=ocs_stock_status,
            mix_multiplier=round(mix_mult, 2),
            is_top_sku=is_top,
        ))

    urgency_order = {
        "stockout": 0, "critical": 1, "high": 2, "medium": 3,
        "ok": 4, "overstock": 5, "dead_stock": 6,
    }
    recs.sort(key=lambda r: (urgency_order.get(r.urgency, 99), -r.revenue_30d))
    return recs

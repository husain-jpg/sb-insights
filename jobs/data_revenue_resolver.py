"""
Data Revenue Resolver — central logic for determining the active rebate deal
for any SKU, given the partner hierarchy:

    Brand-direct or LP-direct deal  >  IRCC  >  Canna Collective  >  Seeker

Rules locked in with user:
- A direct deal at the BRAND or LP level excludes that brand/LP from ALL
  collective programs, even if the direct deal is 0% or has no rate set.
- Within collectives, IRCC always beats Canna Collective always beats Seeker —
  even if the lower-priority collective has a higher rate for that SKU.
- Resolver runs at QUERY time. Storage is unfiltered — we keep the raw deal
  rows from each source, then pick the winner per SKU on demand.

This module is called from:
- /api/reorder (Reorder tab badge)
- Future: /api/reports/data-revenue (monthly reimbursement reports)
- Future: Order Builder margin-aware view
"""
from __future__ import annotations

from datetime import date
from typing import Iterable

# Lower number = higher priority. "direct" wins over everything.
COLLECTIVE_PRIORITY = {
    "IRCC": 1,
    "Canna Collective": 2,
    "Seeker": 3,
}


def get_active_deals_for_skus(conn, skus: Iterable[str], today: date | None = None) -> dict:
    """
    For each SKU in `skus`, return the active deal that applies (or None).

    Returns:
        {sku: {
            "partner": str,            # "IRCC" / "Canna Collective" / "Seeker" / "Brand Direct" / "LP Direct"
            "percentage": float,       # rebate %
            "basis": str,              # "retail_sales" / "wholesale_cost" / etc
            "is_direct": bool,         # True if from a direct brand/LP deal
            "source_brand": str|None,  # for direct deals, the brand_partner name
        } | None}

    SKUs in the input but not in the output mapping have no active deal.

    The resolver does ONE pass over relevant tables for efficiency. Don't call
    this in a per-SKU loop — pass all SKUs at once.
    """
    today = today or date.today()
    today_iso = today.isoformat()
    sku_set = set(skus)
    if not sku_set:
        return {}

    cur = conn.cursor()

    # 1) Build a brand→is_direct + lp→is_direct lookup from brand_partners
    cur.execute("""
        SELECT brand_name, partner_type
        FROM brand_partners
        WHERE is_direct_deal = 1 AND is_active = 1
    """)
    direct_brands = set()
    direct_lps = set()
    for name, ptype in cur.fetchall():
        if not name:
            continue
        if (ptype or 'brand').lower() == 'lp':
            direct_lps.add(name.strip().lower())
        else:
            direct_brands.add(name.strip().lower())

    # 2) Get the brand and LP for each SKU we care about.
    # Match products via Cova SKU OR OCS variant number (deals use OCS variant
    # in sku_filter, but inventory/sales use Cova SKU; products is the bridge).
    #
    # SQLite has a hard limit of 999 parameters per query (sometimes 32K on
    # newer builds). With thousands of SKUs in the inventory endpoint, naive
    # IN (?, ?, ..., ?) blows past that. We chunk into batches of 400 (×2 for
    # the OR-on-ocs_variant doubles param count → max 800 < 999 floor).
    CHUNK_SIZE = 400
    sku_list = list(sku_set)

    def _chunked(seq, n):
        for i in range(0, len(seq), n):
            yield seq[i:i + n]

    # Build dual lookup: cova_sku → (brand, lp), ocs_var → (brand, lp)
    by_cova: dict[str, tuple[str|None, str|None]] = {}
    by_ocs: dict[str, tuple[str|None, str|None]] = {}

    for batch in _chunked(sku_list, CHUNK_SIZE):
        placeholders = ",".join(["?"] * len(batch))
        cur.execute(f"""
            SELECT sku, ocs_variant_number, brand, lp
            FROM products
            WHERE sku IN ({placeholders}) OR ocs_variant_number IN ({placeholders})
        """, list(batch) + list(batch))
        for cova_sku, ocs_var, brand, lp in cur.fetchall():
            if cova_sku and cova_sku in sku_set:
                by_cova[cova_sku] = (brand, lp)
            if ocs_var and ocs_var in sku_set:
                by_ocs[ocs_var] = (brand, lp)

    # 3) For each SKU, check direct deal exclusion FIRST
    direct_excluded: dict[str, dict] = {}  # sku → "we have a direct deal, no collective applies"
    for sku in sku_set:
        brand, lp = by_cova.get(sku) or by_ocs.get(sku) or (None, None)
        b_norm = brand.strip().lower() if brand else None
        l_norm = lp.strip().lower() if lp else None
        if b_norm in direct_brands:
            direct_excluded[sku] = {
                "partner": "Brand Direct",
                "percentage": 0.0,
                "basis": "n/a",
                "is_direct": True,
                "source_brand": brand,
            }
        elif l_norm in direct_lps:
            direct_excluded[sku] = {
                "partner": "LP Direct",
                "percentage": 0.0,
                "basis": "n/a",
                "is_direct": True,
                "source_brand": lp,
            }

    # 4) Get all active collective deals matching our SKUs.
    # Same chunking strategy — split SKU list into batches to stay under
    # SQLite's parameter limit.
    deals_by_sku: dict[str, dict] = {}
    for batch in _chunked(sku_list, CHUNK_SIZE):
        placeholders = ",".join(["?"] * len(batch))
        cur.execute(f"""
            SELECT d.sku_filter, b.brand_name, d.percentage, d.basis
            FROM data_revenue_deals d
            JOIN brand_partners b ON b.id = d.brand_id
            WHERE d.start_date <= ?
              AND (d.end_date IS NULL OR d.end_date >= ?)
              AND d.sku_filter IS NOT NULL AND d.sku_filter != ''
              AND (d.sku_filter IN ({placeholders}) OR d.sku_filter IN ({placeholders}))
        """, [today_iso, today_iso] + list(batch) + list(batch))

        # Bucket deals by SKU; pick highest-priority collective per SKU
        for sku_filter, partner_name, pct, basis in cur.fetchall():
            sku_key = sku_filter.strip()
            # Score by priority — lower is better (1 = IRCC wins)
            priority = COLLECTIVE_PRIORITY.get(partner_name, 99)
            existing = deals_by_sku.get(sku_key)
            if existing is None or priority < existing["_priority"]:
                deals_by_sku[sku_key] = {
                    "partner": partner_name,
                    "percentage": float(pct),
                    "basis": basis,
                    "is_direct": False,
                    "source_brand": None,
                    "_priority": priority,
                }

    # 5) Compose the result: direct deals override collective deals
    result: dict[str, dict] = {}
    for sku in sku_set:
        if sku in direct_excluded:
            result[sku] = direct_excluded[sku]
        elif sku in deals_by_sku:
            d = deals_by_sku[sku].copy()
            d.pop("_priority", None)
            result[sku] = d

    return result


def get_active_deal_for_sku(conn, sku: str, today: date | None = None) -> dict | None:
    """Convenience wrapper for single-SKU lookup."""
    out = get_active_deals_for_skus(conn, [sku], today=today)
    return out.get(sku)

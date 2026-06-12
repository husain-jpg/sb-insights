"""
Data Revenue Resolver — central logic for determining the active rebate deal
for any SKU, given the partner hierarchy:

    NEW HIERARCHY (May 2026 rewrite):

        LTO + Data LP Partner   (these STACK with each other)
                  >
        IRCC > Canna Collective > Seeker
                  (within collectives)

Rules locked in with user (replaces prior "direct deal" model):

1. LTOs and Data LP Partner agreements TRUMP collectives. If a SKU has
   either of these active, no collective rebate applies — even if the
   collective has a higher rate.

2. LTOs and Data LP Partners STACK with each other. A SKU can simultaneously
   benefit from both, and both contribute to the effective margin.

3. Within collectives only, the priority is unchanged: IRCC > CC > Seeker.
   Only ONE collective applies per SKU at a time.

4. The old `is_direct_deal` flag on brand_partners is replaced by entries
   in `data_lp_partner_agreements`. A schema migration moves legacy data.

5. Resolver runs at QUERY time. We return a structured result that may
   include 0, 1, or 2 active rates per SKU (collective alone, LTO alone,
   Partner alone, or LTO + Partner together — but never collective + LTO).

This module is called from:
- /api/reorder (Reorder tab badge)
- /api/suggested-promos (margin-with-rebates math)
- Future: /api/reports/data-revenue (monthly reimbursement reports)
"""
from __future__ import annotations

from datetime import date
from typing import Iterable

# Lower number = higher priority within collectives.
COLLECTIVE_PRIORITY = {
    "IRCC": 1,
    "Canna Collective": 2,
    "Seeker": 3,
}


def get_active_deals_for_skus(conn, skus: Iterable[str], today: date | None = None,
                              extra_meta: dict | None = None) -> dict:
    """
    For each SKU in `skus`, return the active deal(s) that apply.

    extra_meta: optional {sku: {"brand":, "lp":, "category":, "subcategory":}}
    fallback used for SKUs missing from the products table (e.g. Suggested
    Additions — SKUs no store carries yet). Without meta, brand/LP-scoped
    deals (Data LP Partners, LTOs) can't match those SKUs; SKU-scoped
    collective deals always match either way.

    Returns (preserving backward-compat as much as possible):
        {sku: {
            "partner": str,            # primary partner label (for badge display)
            "percentage": float,       # primary rebate %
            "basis": str,              # 'retail_sales' | 'wholesale_cost' | 'gross_profit' | 'units_sold' | 'flat_per_month' | 'flat_per_unit'
            "is_direct": bool,         # True if from an LTO or Data Partner (not a collective)
            "source_brand": str|None,  # for direct deals, the brand/LP name
            # NEW fields (May 2026):
            "components": [            # all stacked rebates contributing to this SKU
                {"kind": "lto"|"partner"|"collective", "label": str, "rate": float,
                 "rate_type": str, "rate_value_display": str},
                ...
            ],
            "has_lto": bool,
            "has_partner": bool,
            "blocks_collective": bool,  # True if LTO or Partner present
        } | None}

    SKUs in the input but not in the output mapping have no active deal.
    """
    today = today or date.today()
    today_iso = today.isoformat()
    sku_set = set(skus)
    if not sku_set:
        return {}

    cur = conn.cursor()

    # 1) Load product info for all requested SKUs (brand, lp, category, subcategory).
    #    Match products via Cova SKU OR OCS variant number.
    CHUNK_SIZE = 400
    sku_list = list(sku_set)

    def _chunked(seq, n):
        for i in range(0, len(seq), n):
            yield seq[i:i + n]

    # sku → (brand, lp, category, subcategory). We need category for LTO scope matching.
    sku_meta: dict[str, dict] = {}
    for batch in _chunked(sku_list, CHUNK_SIZE):
        placeholders = ",".join(["?"] * len(batch))
        try:
            cur.execute(f"""
                SELECT p.sku, p.ocs_variant_number, p.brand, p.lp,
                       oc.category, oc.subcategory
                FROM products p
                LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = p.ocs_variant_number
                WHERE p.sku IN ({placeholders}) OR p.ocs_variant_number IN ({placeholders})
            """, list(batch) + list(batch))
        except Exception:
            continue
        for cova_sku, ocs_var, brand, lp, cat, subcat in cur.fetchall():
            meta = {"brand": brand, "lp": lp, "category": cat, "subcategory": subcat}
            if cova_sku and cova_sku in sku_set:
                sku_meta[cova_sku] = meta
            if ocs_var and ocs_var in sku_set:
                sku_meta[ocs_var] = meta

    # Caller-provided fallback meta for SKUs the products table doesn't know.
    if extra_meta:
        for k, m in extra_meta.items():
            if k in sku_set and k not in sku_meta:
                sku_meta[k] = m

    # 2) Load active Data LP Partner agreements. These are LP/brand-scoped,
    #    not SKU-scoped. We'll match each SKU against the partner list by
    #    brand/lp name.
    partner_agreements: list[dict] = []
    try:
        cur.execute("""
            SELECT id, partner_name, scope_type, scope_value,
                   rate_type, rate_value, start_date, end_date
            FROM data_lp_partner_agreements
            WHERE is_active = 1
              AND start_date <= ?
              AND (end_date IS NULL OR end_date >= ?)
        """, (today_iso, today_iso))
        for row in cur.fetchall():
            partner_agreements.append({
                "id": row[0], "partner_name": row[1],
                "scope_type": row[2], "scope_value": (row[3] or "").strip().lower(),
                "rate_type": row[4], "rate_value": float(row[5] or 0),
                "start_date": row[6], "end_date": row[7],
            })
    except Exception:
        # Table might not exist on very old installs; treat as no partners
        partner_agreements = []

    # 3) Load active LTOs (with both junction-table and scope-rule matching).
    active_ltos: list[dict] = []
    try:
        cur.execute("""
            SELECT l.id, l.name, l.discount_pct, l.rate_percentage, l.rate_basis,
                   l.applies_to_brand, l.applies_to_category, l.applies_to_subcategory,
                   l.lto_type
            FROM ltos l
            WHERE l.is_active = 1
              AND l.start_date <= ?
              AND l.end_date >= ?
        """, (today_iso, today_iso))
        for row in cur.fetchall():
            active_ltos.append({
                "id": row[0], "name": row[1],
                "discount_pct": row[2], "rate_percentage": row[3], "rate_basis": row[4],
                "applies_to_brand": (row[5] or "").strip().lower() if row[5] else None,
                "applies_to_category": (row[6] or "").strip().lower() if row[6] else None,
                "applies_to_subcategory": (row[7] or "").strip().lower() if row[7] else None,
                "lto_type": row[8],
            })
    except Exception:
        active_ltos = []

    # Map LTO id → set of SKUs from junction table
    lto_skus_map: dict[int, set] = {}
    if active_ltos:
        lto_ids = [l["id"] for l in active_ltos]
        for batch in _chunked(lto_ids, CHUNK_SIZE):
            placeholders = ",".join(["?"] * len(batch))
            try:
                cur.execute(f"""
                    SELECT lto_id, sku FROM lto_skus WHERE lto_id IN ({placeholders})
                """, batch)
                for lto_id, sku in cur.fetchall():
                    lto_skus_map.setdefault(lto_id, set()).add(sku)
            except Exception:
                pass

    # Helper: does LTO l match SKU?
    def _lto_matches(l: dict, sku: str, meta: dict) -> bool:
        # If LTO has explicit SKU rows, only those match
        if l["id"] in lto_skus_map and lto_skus_map[l["id"]]:
            return sku in lto_skus_map[l["id"]]
        # Otherwise use scope rules. If no scope rules either, LTO matches nothing
        # (avoids unintentional chain-wide application).
        if not l["applies_to_brand"]:
            return False
        brand_norm = (meta.get("brand") or "").strip().lower()
        if brand_norm != l["applies_to_brand"]:
            return False
        if l["applies_to_category"]:
            cat_norm = (meta.get("category") or "").strip().lower()
            if cat_norm != l["applies_to_category"]:
                return False
        if l["applies_to_subcategory"]:
            sub_norm = (meta.get("subcategory") or "").strip().lower()
            if sub_norm != l["applies_to_subcategory"]:
                return False
        return True

    # Helper: does Data Partner agreement match SKU?
    def _partner_matches(a: dict, meta: dict) -> bool:
        if a["scope_type"] == "brand":
            return (meta.get("brand") or "").strip().lower() == a["scope_value"]
        if a["scope_type"] == "lp":
            return (meta.get("lp") or "").strip().lower() == a["scope_value"]
        return False

    # 4) Load active collective deals. Same chunked pattern as before.
    collective_deals_by_sku: dict[str, dict] = {}
    for batch in _chunked(sku_list, CHUNK_SIZE):
        placeholders = ",".join(["?"] * len(batch))
        try:
            cur.execute(f"""
                SELECT d.sku_filter, b.brand_name, d.percentage, d.basis
                FROM data_revenue_deals d
                JOIN brand_partners b ON b.id = d.brand_id
                WHERE d.start_date <= ?
                  AND (d.end_date IS NULL OR d.end_date >= ?)
                  AND d.sku_filter IS NOT NULL AND d.sku_filter != ''
                  AND (d.sku_filter IN ({placeholders}) OR d.sku_filter IN ({placeholders}))
            """, [today_iso, today_iso] + list(batch) + list(batch))
        except Exception:
            continue
        for sku_filter, partner_name, pct, basis in cur.fetchall():
            sku_key = sku_filter.strip()
            priority = COLLECTIVE_PRIORITY.get(partner_name, 99)
            existing = collective_deals_by_sku.get(sku_key)
            if existing is None or priority < existing["_priority"]:
                collective_deals_by_sku[sku_key] = {
                    "partner": partner_name,
                    "percentage": float(pct or 0),
                    "basis": basis,
                    "_priority": priority,
                }

    # 5) Apply the hierarchy per SKU.
    result: dict[str, dict] = {}
    for sku in sku_set:
        meta = sku_meta.get(sku, {})
        components: list[dict] = []

        # Check LTOs first
        for lto in active_ltos:
            if _lto_matches(lto, sku, meta):
                # An LTO's "rate" for stacking purposes is its rebate %
                # (rate_percentage). discount_pct is a discount-off-retail,
                # which doesn't add to margin — it reduces revenue. We
                # track both.
                components.append({
                    "kind": "lto",
                    "label": f"LTO: {lto['name']}",
                    "rate": float(lto["rate_percentage"] or 0),
                    "rate_type": lto["rate_basis"] or "retail_sales",
                    "rate_value_display": f"{lto['rate_percentage'] or 0:.1f}% {lto['rate_basis'] or 'retail'}",
                    "discount_pct": float(lto["discount_pct"] or 0),
                    "lto_id": lto["id"],
                    "lto_type": lto["lto_type"],
                })

        # Check Data LP Partners
        for partner in partner_agreements:
            if _partner_matches(partner, meta):
                # Translate the partner's rate_type into a stacking-compatible
                # display. We don't try to coerce flat-rate $/month into a
                # per-SKU percentage — those are tracked separately.
                rt = partner["rate_type"]
                if rt == "pct_wholesale":
                    rt_basis = "wholesale_cost"
                elif rt == "pct_retail":
                    rt_basis = "retail_sales"
                elif rt == "pct_gross_margin":
                    rt_basis = "gross_profit"
                else:
                    rt_basis = rt  # flat_per_month or flat_per_unit — UI handles separately
                components.append({
                    "kind": "partner",
                    "label": f"Partner: {partner['partner_name']}",
                    "rate": partner["rate_value"] if rt.startswith("pct_") else 0.0,
                    "rate_type": rt_basis,
                    "rate_value_display": _format_partner_rate(partner),
                    "scope_type": partner["scope_type"],
                    "scope_value": partner["scope_value"],
                    "agreement_id": partner["id"],
                    "flat_rate_type": rt if not rt.startswith("pct_") else None,
                    "flat_rate_value": partner["rate_value"] if not rt.startswith("pct_") else None,
                })

        has_lto = any(c["kind"] == "lto" for c in components)
        has_partner = any(c["kind"] == "partner" for c in components)
        blocks_collective = has_lto or has_partner

        # If we have LTO or Partner, collective is blocked even if eligible
        if not blocks_collective:
            cd = collective_deals_by_sku.get(sku)
            if cd:
                components.append({
                    "kind": "collective",
                    "label": cd["partner"],
                    "rate": cd["percentage"],
                    "rate_type": cd["basis"],
                    "rate_value_display": f"{cd['percentage']:.1f}% {cd['basis']}",
                })

        if not components:
            continue  # SKU has no active deal

        # Choose a "primary" component for legacy fields (badge display).
        # Priority: LTO first, then Partner, then Collective.
        primary = (
            next((c for c in components if c["kind"] == "lto"), None) or
            next((c for c in components if c["kind"] == "partner"), None) or
            next((c for c in components if c["kind"] == "collective"), None)
        )

        result[sku] = {
            "partner": primary["label"],
            "percentage": primary["rate"],
            "basis": primary["rate_type"],
            "is_direct": primary["kind"] != "collective",
            "source_brand": meta.get("brand") or meta.get("lp"),
            # New richer fields
            "components": components,
            "has_lto": has_lto,
            "has_partner": has_partner,
            "blocks_collective": blocks_collective,
        }

    return result


def _format_partner_rate(partner: dict) -> str:
    """Human-readable rate display for a Data Partner agreement."""
    rt = partner["rate_type"]
    val = partner["rate_value"]
    if rt == "pct_wholesale":
        return f"{val:.1f}% of wholesale"
    if rt == "pct_retail":
        return f"{val:.1f}% of retail"
    if rt == "pct_gross_margin":
        return f"{val:.1f}% of gross margin"
    if rt == "flat_per_month":
        return f"${val:,.2f} / month"
    if rt == "flat_per_unit":
        return f"${val:,.4f} / unit"
    return f"{val} ({rt})"


def get_active_deal_for_sku(conn, sku: str, today: date | None = None) -> dict | None:
    """Convenience wrapper for single-SKU lookup."""
    out = get_active_deals_for_skus(conn, [sku], today=today)
    return out.get(sku)

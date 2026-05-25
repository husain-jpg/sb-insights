"""
Successor detection for the Reorder Report.

Background (see SESSION_NOTES_2026-05-21.md): when OCS refreshes its catalog it
RE-LISTS products as entirely new entries — new OCS variant number AND new GTIN —
and Cova mirrors this with a new Catalog SKU, usually tagged "*New*" in the name.
The retailer's old SKU keeps its (now-dead) variant number and becomes an
"orphan" that no longer joins to ocs_catalog. The product still exists; only its
identifiers changed.

This module finds, for an orphaned predecessor SKU, the best *successor*
candidate from the Cova catalog using weighted signals. It does NOT mutate any
product mapping — detection is meant to run at report-generation time, and a
manager separately confirms/dismisses candidates (persisted in
successor_dismissals / sku_flags).

DATA SOURCE NOTE
----------------
Three of the seven signals — size, manufacturer, and Date Added — are not in the
SQLite database today (products has no manufacturer/date_added, and products.size
is null for these rows). They live only in the Cova catalog export
(cova-catalog/cova-catalog-*.xlsx). `_load_cova_catalog()` is the single seam
that reads it; when a `cova_catalog` DB table (or the Cova API) lands, swap that
one function and `detect_successor()` is unchanged.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime
from difflib import SequenceMatcher

# ---------------------------------------------------------------------------
# Signal weights (sum to 1.0). Name similarity is scaled by its ratio (0..1);
# the rest are all-or-nothing on an exact/boolean match.
# ---------------------------------------------------------------------------
W_NAME = 0.40          # SequenceMatcher ratio on normalized names, scaled
W_BRAND = 0.15         # brand exact match
W_SIZE = 0.15          # size exact match
W_MANUFACTURER = 0.10  # manufacturer exact match
W_NEW_TAG = 0.10       # candidate name carries the "*New*" tag
W_RECENT = 0.05        # candidate Date Added within RECENT_DAYS
W_IN_OCS = 0.05        # candidate's variant exists in current ocs_catalog

RECENT_DAYS = 90

# Confidence tier cutoffs (inclusive lower bounds).
TIER_HIGH = 0.90       # HIGH   : 90%+
TIER_MEDIUM = 0.75     # MEDIUM : 75-89%
TIER_LOW = 0.50        # LOW    : 50-74%  (below 0.50 -> return None)

# Name-similarity gates (Phase 2). The non-name signals
# (brand+size+manufacturer+*New*+recent+in_ocs) sum to ~0.60 of "free" weight
# for any same-line *New* product, so a weak name match could otherwise ride
# that floor into MEDIUM/HIGH (e.g. a wrong Jeeter flavor). These gates cap the
# tier by name_sim so only genuinely name-matched candidates climb:
#   HIGH   requires confidence >= TIER_HIGH   AND name_sim >= NAME_GATE_HIGH
#   MEDIUM requires confidence >= TIER_MEDIUM AND name_sim >= NAME_GATE_MEDIUM
#   LOW    any candidate reaching TIER_LOW by weights (no name gate)
NAME_GATE_HIGH = 0.85
NAME_GATE_MEDIUM = 0.75


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------
def _normalize_name(name: str | None) -> str:
    """Lowercased alnum core of a product name: drop the 'Brand | ' prefix,
    the '[size]' bracket, the '*New*' tag, and all non-alphanumerics."""
    if not name:
        return ""
    s = str(name)
    s = s.split("|")[-1]                  # drop leading "Brand | "
    s = re.sub(r"\*new\*", "", s, flags=re.I)
    s = re.sub(r"\[.*?\]", "", s)         # drop "[5x0.5g]" etc.
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _has_new_tag(name: str | None) -> bool:
    return bool(name) and "*new*" in str(name).lower()


# ---------------------------------------------------------------------------
# Cova catalog access — reads the cova_catalog DB table (populated by
# jobs.import_cova_exports.import_cova_catalog). This used to read the xlsx
# directly; moving it to the DB keeps report-time detection off the filesystem
# and ready for Postgres / the Cova API. detect_successor() is unchanged.
# ---------------------------------------------------------------------------
_COVA_CACHE: dict | None = None


def _load_cova_catalog(conn, force: bool = False) -> dict:
    """Return cached Cova catalog (from the cova_catalog table) as:
        {
          "by_sku":   {catalog_sku: row_dict},
          "by_brand": {brand_lower: [row_dict, ...]},
        }
    Each row_dict: sku, name, brand, manufacturer, size, vendor_sku (variant),
    date_added (date|None), name_core, is_new.
    """
    global _COVA_CACHE
    if _COVA_CACHE is not None and not force:
        return _COVA_CACHE

    by_sku: dict = {}
    by_brand: dict = {}
    cur = conn.execute("""
        SELECT catalog_sku, product_name, brand, vendor_sku, size,
               manufacturer, date_added
        FROM cova_catalog
    """)
    for sku, name, brand, vendor_sku, size, manufacturer, date_added in cur.fetchall():
        sku = (sku or "").strip()
        if not sku:
            continue
        name = name or ""
        brand = brand or ""
        da = None
        if date_added:
            try:
                da = datetime.strptime(str(date_added)[:10], "%Y-%m-%d").date()
            except ValueError:
                da = None
        row = {
            "sku": sku,
            "name": name,
            "brand": brand,
            "manufacturer": manufacturer or "",
            "size": size or "",
            "vendor_sku": vendor_sku or "",
            "date_added": da,
            "name_core": _normalize_name(name),
            "is_new": _has_new_tag(name),
        }
        by_sku[sku] = row
        by_brand.setdefault(brand.lower(), []).append(row)

    _COVA_CACHE = {"by_sku": by_sku, "by_brand": by_brand, "source": "cova_catalog (DB)"}
    return _COVA_CACHE


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
# Module-level OCS stock cache. detect_successor() is called per orphan during
# report generation; without this, _ocs_status would issue one query per
# candidate (observed ~12 min over the full orphan set). Built once per process
# from the cova/ocs data; like _COVA_CACHE it goes stale after a fresh OCS
# import until restart. Call reset_caches() to force a reload.
_OCS_CACHE: dict | None = None

# Precomputed orphan -> best-successor map (Phase C). Built at server startup
# and after a Cova catalog import (build_successor_map). Stores the best
# candidate IGNORING dismissals — dismissals are applied at LOOKUP time in
# detect_successor so a manager dismissal takes effect immediately, without a
# rebuild. None means "not built yet" -> detect_successor computes live.
_SUCCESSOR_MAP: dict | None = None


def reset_caches() -> None:
    """Drop the in-memory Cova/OCS caches (call after a catalog re-import).
    Does NOT clear the successor map — that is rebuilt explicitly by
    build_successor_map()."""
    global _COVA_CACHE, _OCS_CACHE
    _COVA_CACHE = None
    _OCS_CACHE = None


def _ocs_status(conn, variant: str | None) -> tuple[bool, bool]:
    """(in_ocs, in_stock) for a given OCS variant number (case-insensitive)."""
    global _OCS_CACHE
    if _OCS_CACHE is None:
        _OCS_CACHE = {}
        for v, st in conn.execute("SELECT ocs_variant_number, stock_status FROM ocs_catalog"):
            if v is not None:
                _OCS_CACHE[v.lower()] = (str(st).strip().upper() == "YES")
    if not variant:
        return (False, False)
    hit = _OCS_CACHE.get(variant.lower())
    if hit is None:
        return (False, False)
    return (True, hit)


def _dismissed(conn, predecessor_sku: str) -> tuple[bool, set]:
    """Return (dismiss_all, {dismissed_successor_skus}) for a predecessor.
    Tolerates the successor_dismissals table not existing yet (Phase 1)."""
    try:
        rows = conn.execute(
            "SELECT rejected_successor_sku FROM successor_dismissals WHERE predecessor_sku=?",
            (predecessor_sku,),
        ).fetchall()
    except sqlite3.OperationalError:
        return (False, set())
    dismiss_all = any(r[0] is None or str(r[0]).strip() == "" for r in rows)
    specific = {str(r[0]) for r in rows if r[0] is not None and str(r[0]).strip() != ""}
    return (dismiss_all, specific)


def _predecessor_info(conn, sku: str, cova: dict) -> dict | None:
    """Predecessor attributes, preferring Cova catalog, falling back to products
    (deriving size from the name bracket / variant suffix). None if unusable."""
    row = cova["by_sku"].get(sku)
    if row:
        return row
    pr = conn.execute(
        "SELECT name, brand, ocs_variant_number FROM products WHERE sku=?", (sku,)
    ).fetchone()
    if not pr:
        return None
    name, brand, variant = pr
    size = ""
    m = re.search(r"\[([^\]]+)\]", name or "")
    if m:
        size = m.group(1).strip()
    elif variant and "_" in variant:
        size = variant.split("_", 1)[1].strip("_")
    return {
        "sku": sku, "name": name or "", "brand": brand or "",
        "manufacturer": "", "size": size, "vendor_sku": variant or "",
        "date_added": None, "name_core": _normalize_name(name), "is_new": _has_new_tag(name),
    }


def _tier(confidence: float, name_sim: float) -> str | None:
    if confidence < TIER_LOW:
        return None
    if confidence >= TIER_HIGH and name_sim >= NAME_GATE_HIGH:
        return "HIGH"
    if confidence >= TIER_MEDIUM and name_sim >= NAME_GATE_MEDIUM:
        return "MEDIUM"
    return "LOW"  # reached TIER_LOW by weights but failed the higher name gates


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def _compute_best_successor(predecessor_sku: str, conn,
                            *, exclude_skus: frozenset | set = frozenset()) -> dict | None:
    """Core successor scoring for a predecessor SKU. Returns the best candidate
    dict (with confidence/tier/signals) or None.

    This is dismissal-AGNOSTIC: it only skips the candidate SKUs explicitly
    passed in `exclude_skus`. The successor map is built with exclude_skus
    empty (so dismissals can be applied later at lookup time); the live lookup
    path passes the dismissed set. See module docstring for the data seam.
    """
    # Reorder Report scope is cannabis-only. Accessories (papers, wraps, vapes,
    # trays, etc.) are handled in a separate supplier workflow, and their
    # flavor-in-bracket names (e.g. Juicy Jay's "[Tropical]" vs "[Cherry Pie]")
    # are a known successor-detection false-positive class. Skip them up front.
    # Cheap lookup via the ix_products_top index on products(top_level).
    row = conn.execute(
        "SELECT top_level FROM products WHERE sku=?", (predecessor_sku,)
    ).fetchone()
    if not row or (row[0] or "") != "Cannabis":
        return None

    cova = _load_cova_catalog(conn)
    pred = _predecessor_info(conn, predecessor_sku, cova)
    if pred is None or not pred["brand"]:
        return None  # can't pool candidates without a brand

    today = date.today()
    pred_core = pred["name_core"]
    pred_size = pred["size"].lower()
    pred_manu = pred["manufacturer"].lower()
    pred_variant = (pred["vendor_sku"] or "").lower()

    best = None
    for cand in cova["by_brand"].get(pred["brand"].lower(), []):
        if cand["sku"] == predecessor_sku or cand["sku"] in exclude_skus:
            continue
        # Skip the predecessor's own (dead) listing if it appears under the same variant.
        if pred_variant and cand["vendor_sku"].lower() == pred_variant:
            continue

        name_ratio = SequenceMatcher(None, pred_core, cand["name_core"]).ratio()
        brand_ok = True  # pooled by brand
        size_ok = bool(cand["size"]) and cand["size"].lower() == pred_size and bool(pred_size)
        manu_ok = bool(cand["manufacturer"]) and cand["manufacturer"].lower() == pred_manu and bool(pred_manu)
        new_ok = cand["is_new"]
        recent_ok = cand["date_added"] is not None and 0 <= (today - cand["date_added"]).days <= RECENT_DAYS
        in_ocs, in_stock = _ocs_status(conn, cand["vendor_sku"])

        confidence = (
            name_ratio * W_NAME
            + (W_BRAND if brand_ok else 0.0)
            + (W_SIZE if size_ok else 0.0)
            + (W_MANUFACTURER if manu_ok else 0.0)
            + (W_NEW_TAG if new_ok else 0.0)
            + (W_RECENT if recent_ok else 0.0)
            + (W_IN_OCS if in_ocs else 0.0)
        )

        if best is None or confidence > best["confidence"]:
            best = {
                "successor_sku": cand["sku"],
                "successor_variant": cand["vendor_sku"],
                "successor_name": cand["name"],
                "confidence": confidence,
                "successor_in_ocs": in_ocs,
                "successor_in_stock": in_stock,
                "signals": {
                    "name_similarity": {"ratio": round(name_ratio, 3),
                                        "weight": W_NAME, "contribution": round(name_ratio * W_NAME, 3)},
                    "brand_match": {"matched": brand_ok, "weight": W_BRAND,
                                    "contribution": W_BRAND if brand_ok else 0.0},
                    "size_match": {"matched": size_ok, "weight": W_SIZE,
                                   "contribution": W_SIZE if size_ok else 0.0},
                    "manufacturer_match": {"matched": manu_ok, "weight": W_MANUFACTURER,
                                           "contribution": W_MANUFACTURER if manu_ok else 0.0},
                    "new_tag": {"matched": new_ok, "weight": W_NEW_TAG,
                                "contribution": W_NEW_TAG if new_ok else 0.0},
                    "recent_date_added": {"matched": recent_ok, "weight": W_RECENT,
                                          "contribution": W_RECENT if recent_ok else 0.0},
                    "successor_in_ocs": {"matched": in_ocs, "weight": W_IN_OCS,
                                         "contribution": W_IN_OCS if in_ocs else 0.0},
                },
            }

    if best is None:
        return None
    tier = _tier(best["confidence"], best["signals"]["name_similarity"]["ratio"])
    if tier is None:
        return None
    best["confidence"] = round(best["confidence"], 4)
    best["tier"] = tier
    return best


def detect_successor(predecessor_sku: str, conn) -> dict | None:
    """Find the best successor candidate for an orphaned predecessor SKU.

    Returns None if no candidate scores >= 0.50 (or the predecessor is
    unusable / fully dismissed). Otherwise returns a dict with the chosen
    successor, its confidence (0..1), tier, the per-signal breakdown, and
    whether it's live/in-stock at OCS.

    Phase C: when the precomputed successor map is available
    (build_successor_map ran at startup / after a catalog import) this is an
    O(1) dict lookup. Dismissals are ALWAYS evaluated here, at lookup time, so
    a manager dismissing a candidate takes effect on the very next call without
    rebuilding the map:
      - dismiss-all for the predecessor       -> None
      - the cached winner is the dismissed one -> recompute live excluding all
                                                  dismissed candidates (next best)
      - otherwise                              -> return the cached winner
    If the map isn't built (or this predecessor isn't in it, e.g. during tests
    or for a brand-new orphan), fall back to a live computation.
    """
    dismiss_all, dismissed = _dismissed(conn, predecessor_sku)
    if dismiss_all:
        return None

    if _SUCCESSOR_MAP is not None and predecessor_sku in _SUCCESSOR_MAP:
        cand = _SUCCESSOR_MAP[predecessor_sku]
        if cand is None:
            return None
        if cand["successor_sku"] not in dismissed:
            return cand
        # The cached best was dismissed — recompute live to find the next best
        # candidate that isn't dismissed (rare path; dismissals are manual).

    return _compute_best_successor(predecessor_sku, conn, exclude_skus=dismissed)


def build_successor_map(conn) -> int:
    """Precompute orphan -> best-successor for every current cannabis orphan.

    An orphan is a cannabis product whose ocs_variant_number no longer matches
    ocs_catalog (OCS re-listed it under a new variant). We score each one once
    here instead of per-report-load. Results IGNORE dismissals — those are
    applied at lookup time in detect_successor (so a dismissal needs no rebuild).

    Resets the Cova/OCS caches first so the map is built against current catalog
    data (important when called right after a catalog import). Assigns the
    finished map atomically, so concurrent readers see either the old map or the
    complete new one — never a half-built one. Returns the number of orphans.
    """
    global _SUCCESSOR_MAP
    reset_caches()
    _load_cova_catalog(conn)  # warm the cache once up front
    orphans = conn.execute(
        """
        SELECT p.sku FROM products p
        WHERE p.top_level = 'Cannabis'
          AND p.ocs_variant_number IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM ocs_catalog oc
              WHERE oc.ocs_variant_number = p.ocs_variant_number
          )
        """
    ).fetchall()
    new_map: dict = {}
    for (sku,) in orphans:
        new_map[sku] = _compute_best_successor(sku, conn)
    _SUCCESSOR_MAP = new_map  # atomic swap
    return len(new_map)

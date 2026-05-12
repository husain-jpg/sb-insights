"""
Canna Cabana competitor price scraper.

Target: cannacabana.com — Canna Cabana is a Shopify-backed retailer that
exposes a standard /collections/<handle>/products.json endpoint. This scraper
walks each category collection, paginates via Shopify's ?page=N&limit=250
protocol, and writes a flat CSV of all products to imports/.

Province-wide pricing only (Ontario, Canada). No store-specific inventory.
Not a Playwright-style browser scraper — pure HTTP against a documented
Shopify endpoint.

USAGE:
    python jobs/scrape_cannacabana.py

Pilot scope: Canna Cabana is competing geographically with SB's Livingstone
store. Tag in the output accordingly.
"""
from __future__ import annotations

import csv
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing dependency. Run:  pip install requests")
    sys.exit(1)


# ------ Config ------
BASE = "https://cannacabana.com"
COMPETITOR_NAME = "Canna Cabana Cundles"
SB_COMPETES_WITH = "S2"  # Livingstone

# Collections worth scraping — these map roughly to SB's product tree.
# Ordered from most-to-least important for pricing comparison.
COLLECTIONS = [
    ("weed-online", "Dried Flower"),
    ("pre-rolls", "Pre-Rolls"),
    ("vape-cartridges", "Vapes"),
    ("concentrates", "Concentrates"),
    ("edibles", "Edibles"),
    ("beverages", "Beverages"),
    ("oils-capsules", "Oils & Capsules"),
    ("topicals", "Topicals"),
    ("cannabis-accessories", "Accessories"),
]

PAGE_SIZE = 250          # Shopify caps this at 250
REQUEST_DELAY_SEC = 1.0  # polite throttle between requests
MAX_PAGES_PER_COLLECTION = 20  # safety circuit breaker

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-CA,en;q=0.9",
}

OUTPUT_DIR = Path("imports")
OUTPUT_DIR.mkdir(exist_ok=True)


@dataclass
class Row:
    competitor_name: str
    sb_competes_with: str
    collection: str               # the collection handle we scraped
    collection_label: str         # human-readable category
    product_id: int
    product_handle: str
    product_title: str
    vendor: str | None            # = brand in Shopify-speak
    product_type: str | None      # = category
    variant_id: int
    variant_sku: str | None       # Shopify variant SKU (may match OCS variant #)
    variant_title: str | None
    variant_size: str | None      # option1 usually
    price: float | None
    compare_at_price: float | None  # "was" price if on sale
    available: bool | None
    tags: str | None
    scraped_at: str


def parse_products(items: list[dict], collection_handle: str, collection_label: str) -> list[Row]:
    """Flatten Shopify product+variant JSON into one row per variant."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: list[Row] = []
    for p in items:
        variants = p.get("variants") or []
        tags = ",".join(p.get("tags") or [])[:500] if p.get("tags") else None
        for v in variants:
            try:
                price = float(v.get("price")) if v.get("price") is not None else None
            except (TypeError, ValueError):
                price = None
            try:
                compare_at = float(v.get("compare_at_price")) if v.get("compare_at_price") else None
            except (TypeError, ValueError):
                compare_at = None
            rows.append(Row(
                competitor_name=COMPETITOR_NAME,
                sb_competes_with=SB_COMPETES_WITH,
                collection=collection_handle,
                collection_label=collection_label,
                product_id=p.get("id"),
                product_handle=p.get("handle"),
                product_title=p.get("title"),
                vendor=p.get("vendor"),
                product_type=p.get("product_type"),
                variant_id=v.get("id"),
                variant_sku=v.get("sku"),
                variant_title=v.get("title"),
                variant_size=v.get("option1"),
                price=price,
                compare_at_price=compare_at,
                available=v.get("available"),
                tags=tags,
                scraped_at=now,
            ))
    return rows


def scrape_collection(session: requests.Session, handle: str, label: str) -> list[Row]:
    """Walk all pages of one collection."""
    all_rows: list[Row] = []
    for page in range(1, MAX_PAGES_PER_COLLECTION + 1):
        url = f"{BASE}/collections/{handle}/products.json?limit={PAGE_SIZE}&page={page}"
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
        except requests.exceptions.RequestException as e:
            print(f"    ✗ Network error on page {page}: {e}")
            break

        if r.status_code == 404:
            print(f"    ! Collection not found: {handle}")
            break
        if r.status_code != 200:
            print(f"    ! Unexpected status {r.status_code} on page {page}")
            break

        try:
            data = r.json()
        except ValueError:
            print(f"    ! Page {page}: non-JSON response ({len(r.text)} bytes)")
            break

        products = data.get("products") or []
        if not products:
            # Empty page = we've paginated past the end
            break

        rows = parse_products(products, handle, label)
        all_rows.extend(rows)
        print(f"    page {page}: {len(products)} products → {len(rows)} variants (running total: {len(all_rows)})")

        # If this page was less than full, it's the last page
        if len(products) < PAGE_SIZE:
            break

        time.sleep(REQUEST_DELAY_SEC)

    return all_rows


def main():
    print("=" * 60)
    print("Canna Cabana Scraper (pilot)")
    print(f"Base: {BASE}")
    print(f"Competitor: {COMPETITOR_NAME}")
    print(f"Competes with SB: {SB_COMPETES_WITH}")
    print("=" * 60)
    print()

    session = requests.Session()
    all_rows: list[Row] = []
    timings: dict[str, tuple[int, float]] = {}

    t0_total = time.time()
    for handle, label in COLLECTIONS:
        print(f"  [{label}] /{handle}")
        t0 = time.time()
        rows = scrape_collection(session, handle, label)
        elapsed = time.time() - t0
        all_rows.extend(rows)
        timings[label] = (len(rows), elapsed)
        print(f"    = {len(rows)} rows in {elapsed:.1f}s")
        print()
        time.sleep(REQUEST_DELAY_SEC)

    total_elapsed = time.time() - t0_total

    if not all_rows:
        print("✗ No rows scraped. Something is wrong.")
        sys.exit(1)

    # Write CSV
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = OUTPUT_DIR / f"cannacabana_menu_{ts}.csv"
    with out_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(all_rows[0]).keys()))
        writer.writeheader()
        for row in all_rows:
            writer.writerow(asdict(row))

    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total variants scraped: {len(all_rows):,}")
    print(f"Total time: {total_elapsed:.1f}s")
    print()
    print("By category:")
    for label, (n, secs) in timings.items():
        print(f"  {label:<20} {n:>5} variants  ({secs:>4.1f}s)")
    print()
    print(f"→ Wrote {out_file}")
    print()

    # Quick price sanity check
    priced = [r for r in all_rows if r.price is not None]
    if priced:
        prices = [r.price for r in priced]
        print(f"Prices present on {len(priced):,} of {len(all_rows):,} rows")
        print(f"  Range: ${min(prices):.2f} – ${max(prices):.2f}")
        print(f"  Median: ${sorted(prices)[len(prices)//2]:.2f}")
    on_sale = [r for r in all_rows if r.compare_at_price and r.price and r.compare_at_price > r.price]
    print(f"Items on sale: {len(on_sale):,}")
    print()
    print("=== Sample rows ===")
    for r in all_rows[:5]:
        tag = "SALE" if (r.compare_at_price and r.price and r.compare_at_price > r.price) else "    "
        price_str = f"${r.price:>6.2f}" if r.price else "   ---"
        was_str = f"was ${r.compare_at_price:.2f}" if r.compare_at_price else ""
        print(f"  {tag} [{r.collection_label:<12}] {(r.vendor or '?'):<20} {r.product_title[:30]:<32} {(r.variant_size or '?'):<10} {price_str} {was_str}")


if __name__ == "__main__":
    main()

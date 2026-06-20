"""
Scrape the Beaverton Cannabis — BRADFORD menu for competitive pricing.

Bradford is the downtown competitor for SB's Bradford store (S3). The site is a
React SPA (TechPOS) at beavertoncannabis.ca/shop/bradford-3, backed by a JSON
API at dceapi.techpos.ca. The menu's product grid is served by
  POST https://dceapi.techpos.ca/website/api/products/filterProducts
which returns clean product JSON (brand, name, category, price, size) and the
true total record count — so we call that API directly with a large PageSize
instead of scraping the DOM pager. The API only needs a `domain` header to pick
the tenant; no auth/cookies, so no browser is required.

Output is a competitor_*.csv that import_competitor_prices.py loads into
competitor_prices, tagged sb_competes_with = "S3".

    python jobs/scrape_beaverton_bradford.py            # full menu -> CSV
    python jobs/scrape_beaverton_bradford.py --debug    # print totals + samples
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://beavertoncannabis.ca"
BRANCH = "bradford-3"                       # = TechPOS branchId 3
BRANCH_ID = "3"
COMPETITOR_NAME = "Beaverton Cannabis (Bradford)"
SB_COMPETES_WITH = "S3"                     # SB Bradford

API_URL = "https://dceapi.techpos.ca/website/api/products/filterProducts"
API_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "domain": "beavertoncannabis.ca",       # tenant selector — required, else 500
    "timezone_offset": "240",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
}
PAGE_SIZE = 250                             # 1000 is rejected (500); 250 is fine

OUTPUT_DIR = Path("imports")

_SIZE_RE = re.compile(
    r"(\d+\.?\d*)\s?(g|mg|ml|pk|pack|seeds?|caps?|capsules?)\b", re.I)


@dataclass
class Row:
    competitor_name: str
    sb_competes_with: str
    collection: str           # category id
    collection_label: str     # category name
    product_id: str
    product_handle: str
    product_title: str
    vendor: str | None        # brand
    product_type: str | None  # category
    variant_id: str
    variant_sku: str | None
    variant_title: str | None
    variant_size: str | None
    price: float | None
    compare_at_price: float | None
    available: bool | None
    tags: str | None
    scraped_at: str


def _body(page: int, page_size: int = PAGE_SIZE) -> dict:
    """The exact filterProducts payload the site sends; ProductGroupId=null pulls
    the whole menu (every category) in one paginated stream."""
    return {
        "ProductGroupId": None, "Category": "", "SortId": 1,
        "Page": page, "PageSize": page_size, "SearchText": "",
        "Brand": [], "Weight": [], "Species": [], "BranchId": BRANCH_ID,
        "Terpene": "", "Mood": "", "SubCategory": "",
        "THCMAX": 100, "THCMIN": 0, "CBDMAX": 100, "CBDMIN": 0,
        "FromExpressCheckout": False, "OnSale": False,
        "ShowOnlyOutOfStock": False, "CategoryId": None, "CampaignId": 0,
    }


def _fetch_page(session, page: int) -> dict:
    """POST one page; return the `data` object ({products, totalRecords, ...})."""
    last = None
    for attempt in range(3):
        try:
            r = session.post(API_URL, headers=API_HEADERS, json=_body(page), timeout=30)
            r.raise_for_status()
            j = r.json()
            if not j.get("success", True) and j.get("errors"):
                raise RuntimeError(j["errors"])
            return j.get("data") or {}
        except Exception as e:           # transient network/5xx — back off and retry
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"filterProducts page {page} failed: {last}")


def _size_from_name(name: str) -> str | None:
    """The consumer-facing pack size lives in the product name (e.g. '… - 3.5g',
    '510 Thread Cartridge - 1g'); take the last weight/count token. This matches
    how our own product names are sized, so the comparison matcher lines up."""
    m = _SIZE_RE.findall(name or "")
    if not m:
        return None
    amt, unit = m[-1]
    unit = unit.lower()
    unit = {"pack": "pk", "capsule": "caps", "capsules": "caps",
            "seed": "seeds"}.get(unit, unit)
    return f"{amt}{unit}"


def parse_product(p: dict, ts: str) -> Row | None:
    name = (p.get("name") or "").replace(" ", " ").strip()
    if not name:
        return None
    brand = (p.get("brand") or "").strip() or None
    pr = p.get("price") or {}
    reg = pr.get("price")
    sell = pr.get("discountedPrice")
    if sell is None:
        sell = reg
    if sell is None:
        return None
    # on sale → discountedPrice is the shelf price, regular price is the strike-through
    compare_at = reg if (reg is not None and sell is not None and reg > sell) else None

    cat = (p.get("category") or "").strip() or None
    size = _size_from_name(name)
    if not size and p.get("weightPerUnit"):
        wpu = p["weightPerUnit"]
        size = f"{wpu:g}g" if p.get("measurementType") == 2 else str(wpu)

    q = p.get("quantity")
    available = (q is None) or (q > 0)
    vid = hashlib.sha1(f"{BRANCH}|{brand}|{name}|{size}".encode()).hexdigest()[:16]
    return Row(
        competitor_name=COMPETITOR_NAME, sb_competes_with=SB_COMPETES_WITH,
        collection=str(p.get("categoryId") or ""), collection_label=cat or "Menu",
        product_id=hashlib.sha1(f"{BRANCH}|{brand}|{name}".encode()).hexdigest()[:16],
        product_handle=str(p.get("id") or ""), product_title=name, vendor=brand,
        product_type=cat, variant_id=vid, variant_sku=(p.get("sku") or None),
        variant_title=size, variant_size=size,
        price=float(sell), compare_at_price=(float(compare_at) if compare_at else None),
        available=available, tags=None, scraped_at=ts)


def fetch_all() -> list[Row]:
    """Page through filterProducts until every record is collected."""
    import requests
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    session = requests.Session()
    rows: dict[str, Row] = {}
    page = 1
    total = None
    while True:
        data = _fetch_page(session, page)
        prods = data.get("products") or []
        if total is None:
            total = data.get("totalRecords") or 0
            print(f"Beaverton Bradford: {total} products to fetch "
                  f"({-(-total // PAGE_SIZE)} pages of {PAGE_SIZE})")
        for p in prods:
            r = parse_product(p, ts)
            if r and r.variant_id not in rows:
                rows[r.variant_id] = r
        print(f"   page {page}: +{len(prods)} (have {len(rows)} unique)")
        if not prods or len(rows) >= (total or 0) or page > 60:
            break
        page += 1
    return list(rows.values())


def cmd_scrape() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    rows = fetch_all()
    if not rows:
        print(">>> No products returned — check the `domain` header / API status.")
        return
    out = OUTPUT_DIR / ("competitor_beaverton_bradford_"
                        + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()))
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))
    cats: dict[str, int] = {}
    for r in rows:
        cats[r.product_type or "?"] = cats.get(r.product_type or "?", 0) + 1
    print(f"\nSaved {len(rows)} products -> {out}")
    print("  by category:", ", ".join(f"{k} {v}" for k, v in sorted(cats.items())))
    print("  Next: python jobs/import_competitor_prices.py")


def cmd_debug() -> None:
    rows = fetch_all()
    print(f"\n=== {len(rows)} products; first 8 ===")
    for r in rows[:8]:
        print(f"  [{r.product_type}] {r.vendor} | {r.product_title[:48]} "
              f"| {r.variant_size} | ${r.price}"
              + (f" (was ${r.compare_at_price})" if r.compare_at_price else ""))


def main() -> None:
    for _st in (sys.stdout, sys.stderr):  # UTF-8 so redirected/scheduled runs don't crash
        try:
            _st.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Scrape Beaverton Cannabis (Bradford) menu")
    ap.add_argument("--debug", action="store_true", help="print totals + sample rows, no CSV")
    args = ap.parse_args()
    try:
        import requests  # noqa: F401
    except ImportError:
        print("Need: pip install requests")
        sys.exit(1)
    if args.debug:
        cmd_debug()
    else:
        cmd_scrape()


if __name__ == "__main__":
    main()

"""
HiBuddy competitor price scraper — pilot version for one store.

Target: Canna Cabana at 201 Cundles Rd E Unit 105A, Barrie
  https://hibuddy.ca/store/8941fc54437332558c61d8dc33494d91/canna-cabana

USAGE:
    python jobs/scrape_hibuddy.py

What it does:
  1. Fetches the HiBuddy store page with a realistic browser user-agent
  2. Parses the initial 12 menu items from the rendered HTML
  3. Tries known Next.js pagination patterns to get the full 675
  4. Writes results to imports/hibuddy_canna_cabana_cundles.csv

If pagination fails, it will still produce a CSV with the 12 visible items
so you can verify the data structure and we'll debug pagination separately.

Run this manually — not on a schedule — for the first few cycles.
"""
from __future__ import annotations

import csv
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Run:  pip install requests beautifulsoup4 lxml")
    sys.exit(1)


# ------ Config ------
STORE_URL = "https://hibuddy.ca/store/8941fc54437332558c61d8dc33494d91/canna-cabana"
STORE_ID = "8941fc54437332558c61d8dc33494d91"
STORE_NAME = "Canna Cabana Cundles"
SB_STORE_ID = "S2"  # Livingstone (the SB store this competes with)

# Browser-realistic headers. Not being sneaky — HiBuddy's page requires
# legit User-Agent or it returns 403.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
}

OUTPUT_DIR = Path("imports")
OUTPUT_DIR.mkdir(exist_ok=True)


@dataclass
class MenuItem:
    store_id: str           # HiBuddy's store hash
    store_name: str         # Human-readable
    sb_competes_with: str   # Which SB store this competes with
    product_id: str         # HiBuddy's product hash
    product_name: str
    brand: str | None
    category: str | None
    size: str | None
    price_from: float | None        # "From $X" price
    typical_nearby_price: float | None  # "Typical nearby $Y"
    deal_percent_off: int | None    # e.g. 23 for "23% OFF"
    scraped_at: str


# --- Parse initial HTML ---
def parse_items_from_html(html: str, store_id: str) -> list[MenuItem]:
    """Parse menu items from the initial HTML response.

    Strategy: each product is a distinct block anchored by an <a> link to
    /product/<hash>. We need to find the tightest enclosing container that
    holds ONLY this product's info (brand, name, size, prices). The markup
    varies, so we walk up and STOP at the first ancestor whose text content
    grows past this single product's data (a heuristic: once we see more than
    one "From$" price, we've gone too far).
    """
    soup = BeautifulSoup(html, "lxml")
    items: list[MenuItem] = []
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    product_links = soup.select(f'a[href*="/product/"][href*="dealStoreId={store_id}"]')
    seen = set()

    for link in product_links:
        href = link.get("href", "")
        m = re.search(r"/product/([a-f0-9]+)", href)
        if not m:
            continue
        product_id = m.group(1)
        if product_id in seen:
            continue
        seen.add(product_id)

        # Walk up, stopping when we find the SMALLEST container that has both
        # brand/category marker AND a price. Any further up and we'd grab
        # neighboring cards too.
        card = link.parent
        best_card = None
        for _ in range(8):
            if card is None:
                break
            text = card.get_text(" ", strip=True)
            # Check how many "From$" prices are in this container
            price_count = len(re.findall(r"From\$\d", text))
            has_brand_cat = bool(re.search(r"•(Edibles|Extracts|Flower|Pre-Rolls|Topicals|Vapes)", text))
            if price_count == 1 and has_brand_cat:
                best_card = card
                break
            if price_count > 1:
                # Went too far — last single-price parent was the right one
                break
            card = card.parent

        if best_card is None:
            # Fallback: use the link itself, which has at least name+href
            best_card = link.parent or link

        card_text = best_card.get_text(" ", strip=True)

        # Brand + category from "BRAND•Category" pattern
        brand, category = None, None
        cat_match = re.search(r"•(Edibles|Extracts|Flower|Pre-Rolls|Topicals|Vapes)", card_text)
        if cat_match:
            category = cat_match.group(1)
            before_bullet = card_text[: cat_match.start()]
            before_bullet = re.sub(r"^.*?(?:%\s*OFF|\bOFF\b)\s*", "", before_bullet)
            brand = before_bullet.strip() or None

        # Product name — prefer the h3/h2 text within the card.
        name = None
        h = best_card.find(["h3", "h2"])
        if h:
            name = h.get_text(strip=True)
        if not name:
            name = link.get_text(strip=True)
        if name and category and "•" + category in name:
            name = name.split("•" + category, 1)[-1].strip()
        if name and brand and name.startswith(brand):
            name = name[len(brand):].strip()

        # Size — after the product name, before "From$"
        # Try to pick up size tokens like "30ml", "3x0.5g", "5 pack", "14g"
        size = None
        after_cat = card_text
        if category:
            after_cat = card_text.split(category, 1)[-1]
        before_price = after_cat.split("From$", 1)[0]
        size_patterns = [
            r"\b(\d+(?:\.\d+)?x\d+(?:\.\d+)?\s*(?:g|mg|ml))\b",     # 3x0.5g, 5x10mg
            r"\b(\d+(?:\.\d+)?\s*(?:ml|mg|g)\b)",                    # 30ml, 14g
            r"\b(\d+\s*(?:pack|Pack|pc|caps|count))\b",              # 5 pack, 30 pack
        ]
        for pat in size_patterns:
            m = re.search(pat, before_price)
            if m:
                size = re.sub(r"\s+", " ", m.group(1)).strip()
                break

        # Prices
        price_from = None
        pm = re.search(r"From\$(\d+(?:\.\d+)?)", card_text)
        if pm:
            price_from = float(pm.group(1))

        typical = None
        tm = re.search(r"Typical nearby\s*\$?\s*(\d+(?:\.\d+)?)", card_text)
        if tm:
            typical = float(tm.group(1))

        # Deal %
        deal = None
        dm = re.search(r"(\d+)%\s*OFF", card_text)
        if dm:
            deal = int(dm.group(1))

        items.append(MenuItem(
            store_id=store_id,
            store_name=STORE_NAME,
            sb_competes_with=SB_STORE_ID,
            product_id=product_id,
            product_name=name,
            brand=brand,
            category=category,
            size=size,
            price_from=price_from,
            typical_nearby_price=typical,
            deal_percent_off=deal,
            scraped_at=now,
        ))

    return items


# --- Discover pagination ---
def try_fetch_all_items(session: requests.Session, store_id: str) -> tuple[list[MenuItem], str]:
    """Attempt to fetch the full menu, trying several pagination patterns.

    Returns (items, mechanism_used_or_error).
    """
    # First: get the initial page
    print(f"→ Fetching {STORE_URL} ...")
    r = session.get(STORE_URL, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        return [], f"HTTP {r.status_code} on initial page fetch"

    items = parse_items_from_html(r.text, store_id)
    print(f"→ Parsed {len(items)} items from initial HTML")

    if len(items) == 0:
        return [], "Parsed 0 items — HTML structure may have changed"

    # Look for the total item count so we know if pagination is needed
    total_match = re.search(r"of\s+(\d+)\s+menu items", r.text)
    total = int(total_match.group(1)) if total_match else len(items)
    print(f"→ Store has {total} total items; got {len(items)} so far")

    if len(items) >= total:
        return items, "complete-in-initial-html"

    # Try pagination pattern 1: Next.js server-side pagination via URL
    # Common pattern: ?page=2, ?offset=12, or appending /page/2
    for suffix, label in [
        ("?page=2", "?page= URL param"),
        ("?offset=12", "?offset= URL param"),
    ]:
        try:
            url = STORE_URL + suffix
            print(f"→ Trying {label}: {url}")
            r2 = session.get(url, headers=HEADERS, timeout=30)
            if r2.status_code == 200:
                new_items = parse_items_from_html(r2.text, store_id)
                # If pagination worked, page 2 returns DIFFERENT items
                initial_ids = {i.product_id for i in items}
                new_ids = {i.product_id for i in new_items} - initial_ids
                if len(new_ids) > 0:
                    # Keep paginating
                    items.extend([i for i in new_items if i.product_id in new_ids])
                    print(f"  → Got {len(new_ids)} new items, continuing...")
                    # Continue paginating with this pattern
                    page = 3
                    while len(items) < total and page < 100:
                        time.sleep(0.6)  # polite throttle
                        purl = STORE_URL + suffix.replace("=2", f"={page}").replace("=12", f"={(page-1)*12}")
                        r3 = session.get(purl, headers=HEADERS, timeout=30)
                        if r3.status_code != 200:
                            break
                        more = parse_items_from_html(r3.text, store_id)
                        existing = {i.product_id for i in items}
                        net_new = [i for i in more if i.product_id not in existing]
                        if not net_new:
                            break
                        items.extend(net_new)
                        print(f"  → page {page}: {len(net_new)} new items (total {len(items)})")
                        page += 1
                    return items, f"paginated via {label}"
        except Exception as e:
            print(f"  → {label} failed: {e}")

    # Try pagination pattern 2: Discover the Next.js _next/data JSON endpoint
    # Next.js pages often have a __NEXT_DATA__ script tag pointing to a data URL
    next_data_match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', r.text, re.DOTALL)
    if next_data_match:
        try:
            payload = json.loads(next_data_match.group(1))
            build_id = payload.get("buildId")
            if build_id:
                data_url = f"https://hibuddy.ca/_next/data/{build_id}/store/{store_id}.json"
                print(f"→ Trying Next.js data endpoint: {data_url}")
                r_data = session.get(data_url, headers=HEADERS, timeout=30)
                if r_data.status_code == 200:
                    data = r_data.json()
                    # Walk the JSON looking for a list of items
                    print(f"  → Got {len(r_data.text)} bytes of JSON — inspect structure manually")
                    # Save for inspection
                    (OUTPUT_DIR / "hibuddy_raw_nextjs_data.json").write_text(r_data.text)
                    print(f"  → Saved raw JSON to imports/hibuddy_raw_nextjs_data.json for debugging")
        except Exception as e:
            print(f"  → Next.js data discovery failed: {e}")

    # Fallback: return whatever we got
    return items, f"partial ({len(items)} of {total}) — pagination mechanism unknown"


def main():
    print("=" * 60)
    print("HiBuddy Scraper — Canna Cabana Cundles pilot")
    print("=" * 60)
    print()

    session = requests.Session()

    try:
        items, mechanism = try_fetch_all_items(session, STORE_ID)
    except requests.exceptions.RequestException as e:
        print(f"✗ Network error: {e}")
        sys.exit(1)

    if not items:
        print(f"✗ No items scraped. Mechanism result: {mechanism}")
        sys.exit(1)

    print()
    print(f"✓ Scraped {len(items)} items. Mechanism: {mechanism}")
    print()

    # Write CSV
    out_file = OUTPUT_DIR / f"hibuddy_canna_cabana_cundles_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with out_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(items[0]).keys()))
        writer.writeheader()
        for item in items:
            writer.writerow(asdict(item))

    print(f"→ Wrote {out_file}")
    print()

    # Show first 10 as preview
    print("=== Sample (first 10 items) ===")
    for i, item in enumerate(items[:10], 1):
        print(f"  {i:>2}. [{item.category or '?':<10}] {item.brand or '?':<20} {item.product_name[:40]:<42} {item.size or '?':<8}  ${item.price_from or 0:>6.2f}  (typical ${item.typical_nearby_price or 0:>6.2f})")

    print()
    print("=== Coverage summary ===")
    by_cat = {}
    for item in items:
        by_cat.setdefault(item.category or "Unknown", []).append(item)
    for cat, lst in sorted(by_cat.items()):
        print(f"  {cat:<15} {len(lst)} items")


if __name__ == "__main__":
    main()

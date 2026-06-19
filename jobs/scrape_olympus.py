"""
Scrape the Olympus Cannabis menu for competitive pricing — the other downtown
Bradford competitor (tagged sb_competes_with = "S3").

Olympus runs a Buddi/Nuxt store at olympuscannabis.ca; products are server-
rendered into the page (no product API), so we scrape the DOM. Categories are
/category/{name}; each category paginates with a "Load More" button. Each card:
  species, BRAND (caps), name, THC:, CBD:, size(s), $price, ADD TO CART
Output is competitor_*.csv for import_competitor_prices.py.

Usage (from terroir-ops root):
    python jobs/scrape_olympus.py            # all categories -> CSV
    python jobs/scrape_olympus.py --debug flower
    python jobs/scrape_olympus.py --watch
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://olympuscannabis.ca"
SHOP_URL = f"{BASE}/shop"
COMPETITOR_NAME = "Olympus Cannabis"
SB_COMPETES_WITH = "S3"
OUTPUT_DIR = Path("imports")
SHOT_DIR = Path("powerbi_shots")

# One row per product card: climb from each ADD TO CART button to the card
# ancestor (has an <img> and a $price), return its text lines + image alt.
EXTRACT_JS = r"""
() => {
  const norm = s => (s||'').replace(/ /g,' ').replace(/[ \t]+/g,' ').trim();
  const priceRe = /\$\d+(?:\.\d{2})?/;
  const cards = new Set();
  document.querySelectorAll('button, a').forEach(btn => {
    if (norm(btn.textContent).toLowerCase().includes('add to cart')) {
      let n = btn;
      for (let i=0;i<8 && n.parentElement;i++){ n=n.parentElement;
        if (n.querySelector('img') && priceRe.test(n.textContent||'')) { cards.add(n); break; } }
    }
  });
  const arr=[...cards]; const leaves=arr.filter(c=>!arr.some(o=>o!==c && c.contains(o)));
  return leaves.map(card => {
    const img=card.querySelector('img');
    const lines=(card.innerText||'').split('\n').map(norm).filter(Boolean);
    const prices=(card.innerText||'').match(/\$\d+(?:\.\d{2})?/g) || [];
    return { lines, prices, img: img?(img.getAttribute('alt')||''):'' };
  });
}
"""

LABELS = {"add to cart", "view cart", "select", "out of stock", "sold out"}
SIZE_RE = re.compile(r"^\d+\.?\d*\s?(g|gram|caps?|capsule|mg|ml|pk|pack|seeds?|each|x.*)$", re.I)


@dataclass
class Row:
    competitor_name: str
    sb_competes_with: str
    collection: str
    collection_label: str
    product_id: str
    product_handle: str
    product_title: str
    vendor: str | None
    product_type: str | None
    variant_id: str
    variant_sku: str | None
    variant_title: str | None
    variant_size: str | None
    price: float | None
    compare_at_price: float | None
    available: bool | None
    tags: str | None
    scraped_at: str


def _ctx(headless: bool):
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    b = pw.chromium.launch(headless=headless,
                           args=["--no-sandbox", "--disable-dev-shm-usage"])
    pg = b.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/124.0.0.0 Safari/537.36",
                    viewport={"width": 1500, "height": 1400})
    return pw, b, pg


def get_categories(pg) -> list[str]:
    pg.goto(SHOP_URL, wait_until="networkidle", timeout=60_000)
    pg.wait_for_timeout(3000)
    hrefs = pg.eval_on_selector_all("a", "els => els.map(e => e.getAttribute('href'))")
    cats = sorted({h.split("/category/")[1] for h in hrefs
                   if h and re.fullmatch(r"/category/[a-z0-9-]+", h)})
    return cats


def _money(s: str) -> float | None:
    m = re.search(r"\d+(?:\.\d{2})?", s or "")
    return float(m.group()) if m else None


def _load_all(pg) -> None:
    """Click the 'Load More' button until it's gone. A JS click is required — the
    button sits under a sticky element so Playwright's actionable click times out."""
    for _ in range(80):  # safety ceiling
        pg.mouse.wheel(0, 8000)
        pg.wait_for_timeout(400)
        clicked = pg.evaluate(r"""() => {
          const b=[...document.querySelectorAll('button')].find(
            e => /^load more$/i.test((e.textContent||'').trim()) && e.offsetParent!==null);
          if (b) { b.scrollIntoView({block:'center'}); b.click(); return true; }
          return false;
        }""")
        if not clicked:
            break
        pg.wait_for_timeout(1500)


def parse_card(card: dict, cat: str, ts: str) -> Row | None:
    lines = card.get("lines", [])
    prices = card.get("prices", [])
    if not prices:
        return None
    price = _money(prices[0])
    if price is None:
        return None
    # brand = first ALL-CAPS line that isn't a label/THC/CBD/size; name = next line.
    brand, bi = None, -1
    for i, ln in enumerate(lines):
        low = ln.lower()
        if low in LABELS or ln.endswith(":") or SIZE_RE.match(ln) or "$" in ln:
            continue
        letters = [ch for ch in ln if ch.isalpha()]
        if letters and ln == ln.upper() and len(ln) >= 2 and "mg/g" not in low:
            brand, bi = ln, i
            break
    name = card.get("img") or ""
    if bi >= 0 and bi + 1 < len(lines):
        nxt = lines[bi + 1]
        if "$" not in nxt and not nxt.endswith(":"):
            name = nxt
    if not name:
        return None
    # size(s): lines matching a size pattern; >1 distinct => multi-variant
    sizes = [ln for ln in lines if SIZE_RE.match(ln)]
    multi = len(set(sizes)) > 1
    size = "multi" if multi else (sizes[0] if sizes else None)
    vid = hashlib.sha1(f"olympus|{brand}|{name}|{size}".encode()).hexdigest()[:16]
    return Row(
        competitor_name=COMPETITOR_NAME, sb_competes_with=SB_COMPETES_WITH,
        collection=cat, collection_label=cat.replace("-", " ").title(),
        product_id=hashlib.sha1(f"olympus|{brand}|{name}".encode()).hexdigest()[:16],
        product_handle="", product_title=name, vendor=brand,
        product_type=cat.replace("-", " ").title(),
        variant_id=vid, variant_sku=None, variant_title=size, variant_size=size,
        price=price, compare_at_price=None,
        available=True, tags=None, scraped_at=ts)


def scrape_category(pg, cat: str, ts: str) -> list[Row]:
    pg.goto(f"{BASE}/category/{cat}", wait_until="networkidle", timeout=60_000)
    pg.wait_for_timeout(3000)
    _load_all(pg)
    rows: dict[str, Row] = {}
    for c in pg.evaluate(EXTRACT_JS):
        r = parse_card(c, cat, ts)
        if r and r.variant_id not in rows:
            rows[r.variant_id] = r
    print(f"   {cat}: {len(rows)} rows")
    return list(rows.values())


def cmd_debug(cat: str, headless: bool) -> None:
    pw, b, pg = _ctx(headless)
    try:
        pg.goto(f"{BASE}/category/{cat}", wait_until="networkidle", timeout=60_000)
        pg.wait_for_timeout(3500)
        _load_all(pg)
        cards = pg.evaluate(EXTRACT_JS)
        print(f"=== {cat}: {len(cards)} cards ===")
        for c in cards[:6]:
            print("\nlines:", c["lines"])
            print("prices:", c["prices"], "img:", c["img"][:40])
    finally:
        b.close(); pw.stop()


def cmd_scrape(headless: bool) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pw, b, pg = _ctx(headless)
    rows: list[Row] = []
    try:
        cats = get_categories(pg)
        print(f"Olympus categories: {cats}")
        for cat in cats:
            try:
                rows.extend(scrape_category(pg, cat, ts))
            except Exception as e:
                print(f"   ! {cat} failed: {e}")
    finally:
        b.close(); pw.stop()
    if not rows:
        print(">>> No rows — run with --debug <category> to inspect markup.")
        return
    out = OUTPUT_DIR / ("competitor_olympus_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()))
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))
    print(f"\nSaved {len(rows)} products -> {out}")


def main() -> None:
    for _st in (sys.stdout, sys.stderr):
        try:
            _st.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Scrape Olympus Cannabis menu")
    ap.add_argument("--debug", metavar="CATEGORY")
    ap.add_argument("--watch", action="store_true")
    args = ap.parse_args()
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("Need: pip install playwright; playwright install chromium")
        sys.exit(1)
    if args.debug:
        cmd_debug(args.debug, headless=not args.watch)
    else:
        cmd_scrape(headless=not args.watch)


if __name__ == "__main__":
    main()

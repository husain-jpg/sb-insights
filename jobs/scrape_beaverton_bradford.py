"""
Scrape the Beaverton Cannabis — BRADFORD menu for competitive pricing.

Bradford is the downtown competitor for SB's Bradford store (S3). The menu is a
React SPA (TechPOS) at beavertoncannabis.ca/shop/bradford-3 — products render in
the DOM but the markup uses hashed CSS-module class names, so we scrape by
structure (each product card has an image, a price, and an Add-to-cart button)
rather than by class. Output is a competitor_*.csv that import_competitor_prices.py
loads into competitor_prices, tagged sb_competes_with = "S3".

Usage (from terroir-ops root, needs: pip install playwright; playwright install chromium):
    python jobs/scrape_beaverton_bradford.py            # scrape all groups -> CSV
    python jobs/scrape_beaverton_bradford.py --debug 20 # dump one group's raw cards
    python jobs/scrape_beaverton_bradford.py --watch     # visible browser
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
COMPETITOR_NAME = "Beaverton Cannabis (Bradford)"
SB_COMPETES_WITH = "S3"                     # SB Bradford
HOME_URL = f"{BASE}/shop/{BRANCH}/homepage"
GROUP_URL = f"{BASE}/shop/{BRANCH}/menu?productgroupid={{gid}}"

OUTPUT_DIR = Path("imports")
SHOT_DIR = Path("powerbi_shots")           # reuse the gitignored diagnostics dir

# JS run in the page to pull one row per product card. Resilient to hashed class
# names: find each Add-to-cart/Select-size button, climb to the card ancestor
# (the first ancestor that has an <img> AND a price), and return its text + image.
EXTRACT_JS = r"""
() => {
  const norm = s => (s || '').replace(/ /g,' ').replace(/[ \t]+/g,' ').trim();
  const priceRe = /\$\s?\d+(?:\.\d{2})?/;
  const cards = new Set();
  document.querySelectorAll('button, a').forEach(btn => {
    const t = norm(btn.textContent).toLowerCase();
    if (t.includes('add to cart') || t.includes('select size') || t.includes('sold out')) {
      let n = btn;
      for (let i = 0; i < 8 && n.parentElement; i++) {
        n = n.parentElement;
        if (n.querySelector('img') && priceRe.test(n.textContent || '')) { cards.add(n); break; }
      }
    }
  });
  // Drop the grid/false-positives: keep only leaf cards (a card that does not
  // contain another candidate card).
  const arr = [...cards];
  const leaves = arr.filter(c => !arr.some(o => o !== c && c.contains(o)));
  return leaves.map(card => {
    const img = card.querySelector('img');
    const lines = (card.innerText || '').split('\n').map(norm).filter(Boolean);
    const prices = (card.innerText || '').match(/\$\s?\d+(?:\.\d{2})?(?:\s*\/\s*[^\s,]+)?/g) || [];
    return { lines, prices, img: img ? (img.getAttribute('alt') || '') : '', raw: card.innerText };
  });
}
"""


@dataclass
class Row:
    competitor_name: str
    sb_competes_with: str
    collection: str           # product group id
    collection_label: str     # product group / category name
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


def _ctx(headless: bool):
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    b = pw.chromium.launch(headless=headless,
                           args=["--no-sandbox", "--disable-dev-shm-usage"])
    pg = b.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/124.0.0.0 Safari/537.36",
                    viewport={"width": 1500, "height": 1200})
    return pw, b, pg


def get_groups(pg) -> list[tuple[str, str]]:
    """Return [(group_id, label)] from the Bradford homepage's category links."""
    pg.goto(HOME_URL, wait_until="networkidle", timeout=60_000)
    pg.wait_for_timeout(3000)
    pairs = pg.eval_on_selector_all(
        "a[href*='productgroupid=']",
        "els => els.map(e => [ (e.getAttribute('href').match(/productgroupid=(\\d+)/)||[])[1],"
        "                       (e.innerText||'').trim() ])")
    out, seen = [], set()
    for gid, label in pairs:
        if gid and gid not in seen:
            seen.add(gid)
            label = re.sub(r"^\s*Shop\s*", "", label or "").strip() or f"group {gid}"
            out.append((gid, label))
    return out


def _scroll_all(pg) -> None:
    """Lazy-load: scroll to the bottom until the height stops growing."""
    last = -1
    for _ in range(40):
        h = pg.evaluate("document.body.scrollHeight")
        if h == last:
            break
        last = h
        pg.mouse.wheel(0, 6000)
        pg.wait_for_timeout(900)


def _money(s: str) -> float | None:
    m = re.search(r"\d+(?:\.\d{2})?", s or "")
    return float(m.group()) if m else None


def parse_card(card: dict, gid: str, label: str, ts: str) -> Row | None:
    """Turn one extracted card into a Row. From the live markup, each card holds:
      species badge, THC/CBD badges, "<Brand> - <Name>", "<Brand>",
      "$<pack price>", "/<pack size>", "$<per-gram> /g", "Excl. Tax", button
    The image alt = the clean product name; the pack price (size like 3.5g/7g) is
    the real price — NOT the per-gram (/g) figure."""
    lines = card.get("lines", [])
    raw = card.get("raw", "")
    name = (card.get("img") or "").strip()
    if not name:  # fallback: the line containing "Brand - Name"
        name = next((ln for ln in lines if " - " in ln), "")
    if not name:
        return None

    # Brand = the short line right after the name line (e.g. "Redecan").
    brand = None
    if name in lines:
        i = lines.index(name)
        for ln in lines[i + 1:]:
            low = ln.lower()
            if re.search(r"\$\s?\d", ln) or low in ("excl. tax", "add to cart", "select size"):
                break
            brand = ln
            break

    # Separate pack prices (size starts with a digit, e.g. /3.5g, /7g, /28g) from
    # the per-gram price (/g). The pack price is what the store charges.
    pack = []          # (amount, size)
    for p in card.get("prices", []):
        amt = _money(p)
        if amt is None:
            continue
        msz = re.search(r"/\s*(\d[\w.]*)", p)        # /7g, /3.5g, /1g …
        if msz:
            pack.append((amt, msz.group(1)))
    multi = "select size" in raw.lower()
    if pack:
        pack.sort(key=lambda x: x[0])
        price, size = pack[0]
        compare_at = pack[-1][0] if len(pack) > 1 and pack[-1][0] > price else None
    elif multi:
        # multi-variant card shows "from $X / g" — capture as a from-price marker.
        price = _money(next((p for p in card.get("prices", [])), ""))
        size, compare_at = "multi", None
    else:
        # No sized price (edibles/beverages/accessories): the FIRST price listed is
        # the product price; any smaller one after it is a per-unit figure.
        amts = [a for a in (_money(p) for p in card.get("prices", [])) if a is not None]
        if not amts:
            return None
        price, size, compare_at = amts[0], None, None
    vid = hashlib.sha1(f"{BRANCH}|{brand}|{name}|{size}".encode()).hexdigest()[:16]
    return Row(
        competitor_name=COMPETITOR_NAME, sb_competes_with=SB_COMPETES_WITH,
        collection=gid, collection_label=label,
        product_id=hashlib.sha1(f"{BRANCH}|{brand}|{name}".encode()).hexdigest()[:16],
        product_handle="", product_title=name, vendor=brand, product_type=label,
        variant_id=vid, variant_sku=None, variant_title=size, variant_size=size,
        price=price, compare_at_price=compare_at,
        available=("sold out" not in card.get("raw", "").lower()),
        tags=None, scraped_at=ts)


def _click_pager(pg, page_no: int) -> bool:
    """Click the numbered pager via a JS click. Playwright's actionable .click()
    is unreliable here (the pager sits under an overlay and times out), which
    silently truncated several categories to one page. A JS el.click() works."""
    try:
        return bool(pg.evaluate(
            """(n) => {
                const el = [...document.querySelectorAll('button,a,li,span,div')].find(
                    e => e.children.length === 0 &&
                         (e.textContent||'').trim() === String(n) &&
                         e.offsetParent !== null);
                if (el) { el.scrollIntoView({block:'center'}); el.click(); return true; }
                return false;
            }""", page_no))
    except Exception:
        return False


def scrape_group(pg, gid: str, label: str, ts: str) -> list[Row]:
    pg.goto(GROUP_URL.format(gid=gid), wait_until="networkidle", timeout=60_000)
    pg.wait_for_timeout(3500)
    _scroll_all(pg)
    rows: dict[str, Row] = {}
    page_no = 1
    while page_no <= 60:  # safety ceiling
        for c in pg.evaluate(EXTRACT_JS):
            r = parse_card(c, gid, label, ts)
            if r and r.variant_id not in rows:
                rows[r.variant_id] = r
        before = len(rows)
        if not _click_pager(pg, page_no + 1):
            break                      # no next page button
        page_no += 1
        try:
            pg.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        pg.wait_for_timeout(1800)
        _scroll_all(pg)
        # extract here so we can tell if the new page actually added anything
        for c in pg.evaluate(EXTRACT_JS):
            r = parse_card(c, gid, label, ts)
            if r and r.variant_id not in rows:
                rows[r.variant_id] = r
        if len(rows) == before:        # page added nothing new → stop
            break
    print(f"   group {gid} '{label}': {page_no} page(s) -> {len(rows)} rows")
    return list(rows.values())


def cmd_debug(group_id: str, headless: bool) -> None:
    pw, b, pg = _ctx(headless)
    try:
        SHOT_DIR.mkdir(exist_ok=True)
        pg.goto(GROUP_URL.format(gid=group_id), wait_until="networkidle", timeout=60_000)
        pg.wait_for_timeout(4000)
        _scroll_all(pg)
        cards = pg.evaluate(EXTRACT_JS)
        pg.screenshot(path=str(SHOT_DIR / f"_bv_group_{group_id}.png"), full_page=True)
        print(f"=== group {group_id}: {len(cards)} cards ===")
        for c in cards[:8]:
            print("\n--- card ---")
            print("lines:", c["lines"])
            print("prices:", c["prices"], "img-alt:", c["img"][:50])
        print(f"\n(screenshot: powerbi_shots/_bv_group_{group_id}.png)")
    finally:
        b.close(); pw.stop()


def cmd_scrape(headless: bool) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pw, b, pg = _ctx(headless)
    all_rows: list[Row] = []
    try:
        groups = get_groups(pg)
        print(f"Bradford product groups: {[(g, l) for g, l in groups]}")
        for gid, label in groups:
            try:
                all_rows.extend(scrape_group(pg, gid, label, ts))
            except Exception as e:
                print(f"   ! group {gid} failed: {e}")
    finally:
        b.close(); pw.stop()
    if not all_rows:
        print(">>> No rows scraped — run with `--debug <groupid>` to inspect card markup.")
        return
    out = OUTPUT_DIR / ("competitor_beaverton_bradford_"
                        + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(all_rows[0]).keys()))
        w.writeheader()
        for r in all_rows:
            w.writerow(asdict(r))
    print(f"\nSaved {len(all_rows)} products -> {out}")
    print("  Next: python jobs/import_competitor_prices.py")


def main() -> None:
    for _st in (sys.stdout, sys.stderr):  # UTF-8 so redirected/scheduled runs don't crash
        try:
            _st.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Scrape Beaverton Cannabis (Bradford) menu")
    ap.add_argument("--debug", metavar="GROUPID", help="dump one group's raw cards and exit")
    ap.add_argument("--watch", action="store_true", help="visible browser")
    args = ap.parse_args()
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("Playwright not installed:\n  pip install playwright\n  playwright install chromium")
        sys.exit(1)
    if args.debug:
        cmd_debug(args.debug, headless=not args.watch)
    else:
        cmd_scrape(headless=not args.watch)


if __name__ == "__main__":
    main()

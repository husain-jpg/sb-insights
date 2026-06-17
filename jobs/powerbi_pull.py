"""
Power BI auto-pull for OCS Market Intelligence  —  EXPERIMENTAL / laptop-run.

The OCS municipality data lives in a Power BI report in OCS's tenant that we
have *guest* access to. There's no API/file URL we can hit directly, and the
account uses MFA, so the only reliable automation is to drive a real browser:

    1) Log in ONCE interactively (you complete MFA). The session is saved into
       a local profile folder.
    2) A scheduled run reuses that saved session — no password, no MFA prompt —
       opens the report, exports the two Excel files, and uploads them straight
       into the dashboard via /api/market-intelligence/upload.

This is the approach that survives MFA. Refresh the login every so often if the
session ever expires (just re-run `login`).

------------------------------------------------------------------------------
SETUP (on the Windows laptop that does the manual pull today):
    pip install playwright requests
    playwright install chromium

USAGE:
    # one-time (a browser opens — sign in + complete MFA, then press Enter):
    python jobs/powerbi_pull.py login

    # prove the saved session still works unattended (no login prompt):
    python jobs/powerbi_pull.py probe

    # the real thing (export both reports for a store + upload):
    python jobs/powerbi_pull.py pull --store S4 --window 30

Config below: set REPORT_URL to the OCS Power BI link, and your app login.
------------------------------------------------------------------------------
NOTE: the export step (clicking 'Export data' on the two visuals) is the one
part that depends on this specific report's layout. `probe` screenshots the
report so we can see its structure and finish the export clicks precisely.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

# The OCS Power BI report (the share link you sent).
REPORT_URL = (
    "https://app.powerbi.com/Redirect?action=OpenReport"
    "&appId=6f22a862-f102-4094-a846-b1ad1890bac2"
    "&reportObjectId=b4bf4bf2-bc42-419c-9160-f9b2e08102d1"
    "&ctid=6e9d22f8-d9e5-4479-bfef-33ab60f4da24"
    "&reportPage=ReportSection594b52ef4ce7ff2c8279"
    "&pbi_source=appShareLink"
)

def _env_from_dotenv(key: str, default: str = "") -> str:
    """Read a value from the environment, falling back to a local .env file next
    to the project (the connector runs standalone, so it won't have the app's env
    loaded). Lets the dashboard upload creds live in C:\\terroir-ops\\.env instead
    of being hardcoded."""
    import os
    if os.environ.get(key):
        return os.environ[key]
    env_path = Path(__file__).resolve().parent.parent / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return default


# Where the SB Insights dashboard lives + a login that can upload. Creds come from
# C:\terroir-ops\.env (POWERBI_UPLOAD_EMAIL / POWERBI_UPLOAD_PASSWORD) so they're
# not committed; if unset, the pull still exports locally and just skips upload.
APP_BASE = "https://app.sbinsights.co"
APP_EMAIL = _env_from_dotenv("POWERBI_UPLOAD_EMAIL")
APP_PASSWORD = _env_from_dotenv("POWERBI_UPLOAD_PASSWORD")

# Local folder that holds the saved Power BI browser session. Keep it private.
PROFILE_DIR = str(Path.home() / ".sbinsights_powerbi_profile")
DOWNLOAD_DIR = str(Path.home() / ".sbinsights_powerbi_downloads")
# Diagnostic screenshots go straight into the project (gitignored) so they can
# be inspected without copying them out of the hidden downloads folder.
SHOT_DIR = str(Path(__file__).resolve().parent.parent / "powerbi_shots")


def _ctx(headless: bool):
    """Open a persistent browser context so the login session is reused."""
    from playwright.sync_api import sync_playwright
    Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(
        PROFILE_DIR, headless=headless, accept_downloads=True,
        viewport={"width": 1600, "height": 1000},
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"),
        args=["--disable-blink-features=AutomationControlled"],
    )
    return pw, ctx


def _wait_logged_in(page, timeout: int = 120) -> bool:
    """Wait for the report to actually render. Returns True once report visuals
    appear, False if we land on a real password/email login form. Tolerates the
    brief Microsoft silent-auth redirect (which is NOT a real logout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        # A genuine login form means the session is dead.
        try:
            if page.locator("input[type=password], input[name=loginfmt]").count() > 0 \
               and page.locator("input[type=password], input[name=loginfmt]").first.is_visible():
                return False
        except Exception:
            pass
        # Report rendered = logged in.
        try:
            if page.locator("visual-container-modern, visual-container, .visualContainer").count() > 0:
                return True
        except Exception:
            pass
        time.sleep(2)
    # Timed out — assume not loaded.
    return page.locator("visual-container-modern, visual-container, .visualContainer").count() > 0


def cmd_login() -> None:
    """Open a real window; you sign in + complete MFA, then press Enter."""
    pw, ctx = _ctx(headless=False)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(REPORT_URL, wait_until="load", timeout=120_000)
    print("\n" + "=" * 70)
    print(">>> A browser window opened. Sign in to Power BI + complete MFA.")
    print(">>> CRITICAL: when it asks 'Stay signed in?' click YES — that's what")
    print(">>>           keeps the session alive for automation (days, not 1 hour).")
    print(">>> When the REPORT is fully visible, come back here and press Enter.")
    print("=" * 70)
    input()
    # Verify we're actually logged in (re-navigate in this same session).
    page.goto(REPORT_URL, wait_until="load", timeout=120_000)
    time.sleep(8)
    if "login" in page.url.lower() or "signin" in page.url.lower():
        print("\n>>> WARNING: still on a login page — sign-in didn't stick. "
              "Re-run `login` and be sure to accept 'Stay signed in?'.")
    else:
        print("\n>>> Logged in and session saved to:", PROFILE_DIR)
    ctx.close(); pw.stop()


def cmd_probe() -> None:
    """Headless: reuse the saved session, reach the report, screenshot it.
    This is the make-or-break test — if it lands on the report (not a login
    page), unattended automation works and we can wire up the export."""
    pw, ctx = _ctx(headless=True)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(REPORT_URL, wait_until="load", timeout=120_000)
    _wait_logged_in(page)  # tolerate the silent-auth redirect
    Path(SHOT_DIR).mkdir(parents=True, exist_ok=True)
    shot = str(Path(SHOT_DIR) / "powerbi_probe.png")
    page.screenshot(path=shot, full_page=True)
    title = page.title()
    url = page.url
    print("Landed on:", url)
    print("Page title:", title)
    print("Screenshot:", shot)
    if "login" in url.lower() or "signin" in url.lower():
        print("\n>>> RESULT: hit a login page — the saved session expired. Re-run `login`.")
    else:
        print("\n>>> RESULT: reached the report unattended. Automation is viable —"
              " send me the screenshot and I'll finish the export clicks.")
    ctx.close(); pw.stop()


# The two report pages to export, mapped to the filename the importer expects
# (it recognizes the report by these original-style names).
PAGES = [
    ("2.2 Sales Velocity Your City",
     "2.2 Sales Velocity - Average Daily Sales Units per Store by Municipality.xlsx"),
    ("3.2 Sales Units Your City",
     "3.2 Average Sales Units per Store by Municipality.xlsx"),
]

# Rolling lookback windows (days) pulled each run. Each is end=latest-available
# data date, start=end-(window-1). 14 = fast-mover signal … 180 = stable baseline;
# comparing windows gives momentum without storing history.
WINDOWS = [14, 30, 90, 180]

# The on-canvas Date "between" slicer's two text inputs (aria-labels are stable
# prefixes; the rest of the label is the live available range).
START_INPUT = 'input[aria-label^="Start date"]'
END_INPUT = 'input[aria-label^="End date"]'

# All 8 Star Buds stores, keyed by their OCS CRSA (the value in the report's
# on-canvas store slicer). `name` is a distinctive token that must appear in the
# report header when that store is selected AND must match the dashboard's
# location name (GET /api/locations) so we can resolve the upload location_id.
# The location_id (S1..S8) is NOT hardcoded — it's assigned dynamically in the
# DB, so we resolve it by name at runtime to avoid silent drift.
STORES = [
    {"crsa": "CRSA1208505", "name": "Grove"},
    {"crsa": "CRSA1207212", "name": "Amherstview"},
    {"crsa": "CRSA1201213", "name": "Wasaga"},
    {"crsa": "CRSA1199779", "name": "Angus"},
    {"crsa": "CRSA1188481", "name": "Innisfil"},
    {"crsa": "CRSA1172630", "name": "Bradford"},
    {"crsa": "CRSA1172589", "name": "Livingstone"},
    {"crsa": "CRSA1172574", "name": "Huronia"},
]


def _shot(page, name: str) -> None:
    try:
        Path(SHOT_DIR).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOT_DIR) / f"step_{name}.png"))
        print("   (screenshot: powerbi_shots/step_%s.png)" % name)
    except Exception:
        pass


def _go_to_page(page, page_name: str) -> None:
    """Click a report page in the left navigator by its visible name."""
    print(f" → opening page '{page_name}'")
    # Dismiss any menu/dialog left open by the previous page's export, which
    # otherwise sits on top of the nav and swallows the click (the usual reason
    # the *second* page fails after the first exported fine). Escape only — do
    # NOT click a corner; (5,5) lands on the Microsoft 365 app-launcher waffle
    # and opens a flyout that covers the whole left navigator.
    try:
        page.keyboard.press("Escape")
        time.sleep(0.3)
        page.keyboard.press("Escape")
        time.sleep(1)
    except Exception:
        pass
    # The left nav renders page names as clickable text. Try a few locators.
    for loc in (page.get_by_role("button", name=page_name),
                page.get_by_text(page_name, exact=True),
                page.get_by_text(page_name)):
        try:
            loc.first.click(timeout=8000)
            time.sleep(10)  # let the page's visuals render
            return
        except Exception:
            continue
    raise RuntimeError(f"could not find left-nav page '{page_name}'")


def _dump_date_slicer(page) -> None:
    """Capture the on-canvas Date slicer's DOM + any date <input> fields, so the
    date range can be driven precisely (it doesn't default — must be set each run)."""
    try:
        Path(SHOT_DIR).mkdir(parents=True, exist_ok=True)
        out = []
        visuals = page.locator("visual-container-modern, visual-container, .visualContainer")
        for i in range(min(visuals.count(), 40)):
            v = visuals.nth(i)
            try:
                t = v.inner_text(timeout=400) or ""
                b = v.bounding_box()
            except Exception:
                continue
            if "Date" in t and b and b["width"] < 360 and b["height"] < 220:
                out.append(f"[{i}] box={b} text={t!r}")
                try:
                    out.append("HTML: " + v.inner_html(timeout=1500)[:6000])
                except Exception as e:
                    out.append(f"HTML err {e}")
        inputs = page.locator("input")
        out.append(f"\n# {inputs.count()} <input> elements:")
        for i in range(min(inputs.count(), 30)):
            try:
                out.append(f"  input[{i}] type={inputs.nth(i).get_attribute('type')!r} "
                           f"aria={inputs.nth(i).get_attribute('aria-label')!r} "
                           f"val={inputs.nth(i).input_value(timeout=300)!r}")
            except Exception:
                continue
        (Path(SHOT_DIR) / "date_slicer.txt").write_text("\n".join(out), encoding="utf-8")
        print("   (date slicer dump: powerbi_shots/date_slicer.txt)")
    except Exception as e:
        print(f"   (date slicer dump failed: {e})")


def _dump_buttons(page, name: str) -> None:
    """List every button's aria-label + bounding box — used right after hovering
    the table to see whether/where the visual's '…' (More options) button is."""
    try:
        Path(SHOT_DIR).mkdir(parents=True, exist_ok=True)
        btns = page.get_by_role("button")
        lines = [f"# {btns.count()} buttons after hover"]
        for i in range(min(btns.count(), 120)):
            try:
                lbl = btns.nth(i).get_attribute("aria-label")
                b = btns.nth(i).bounding_box()
            except Exception:
                lbl, b = "<err>", None
            if lbl and ("more option" in lbl.lower() or "export" in lbl.lower()
                        or "focus" in lbl.lower()):
                lines.append(f"[{i}] {lbl!r} box={b}")
        (Path(SHOT_DIR) / f"buttons_{name}.txt").write_text("\n".join(lines), encoding="utf-8")
        print(f"   (button dump: powerbi_shots/buttons_{name}.txt)")
    except Exception as e:
        print(f"   (button dump failed: {e})")


def _click_table_more_options(page, box) -> bool:
    """Click the data table's own 'More options' (…) — the one nearest the table's
    top-right corner. A page-wide match also hits the TOP TOOLBAR's '…', which
    opens 'Get insights' instead of 'Export data', so we must disambiguate by
    position."""
    if not box:
        return False
    tx, ty = box["x"] + box["width"], box["y"]  # table top-right corner
    btns = page.get_by_role("button", name="More options")
    best_d, best_i = 1e9, -1
    for i in range(btns.count()):
        try:
            b = btns.nth(i).bounding_box()
        except Exception:
            continue
        if not b:
            continue
        d = ((b["x"] - tx) ** 2 + (b["y"] - ty) ** 2) ** 0.5
        if d < best_d:
            best_d, best_i = d, i
    if best_i >= 0 and best_d < 250:  # the visual header '…' sits at this corner
        try:
            btns.nth(best_i).click(timeout=5000)
            return True
        except Exception:
            return False
    return False


def _export_table(page, out_path: Path, tag: str = "") -> Path:
    """Export the page's data table to .xlsx via the visual's More-options menu.

    `tag` (e.g. "2.2") prefixes the step screenshots so each page's export is
    captured separately instead of overwriting the previous page's shots.
    """
    pre = (tag + "_") if tag else ""
    # Reveal the visual header: hover the largest visual container on the page.
    visuals = page.locator("visual-container-modern, visual-container, .visualContainer")
    n = visuals.count()
    print(f"   found {n} visual container(s); hovering the table")
    target, box, best = None, None, -1
    # Pick the largest visual by area, ignoring slivers/phantoms (width > 200).
    for i in range(min(n, 16)):
        try:
            b = visuals.nth(i).bounding_box()
            if b and b["width"] > 200 and b["width"] * b["height"] > best:
                best = b["width"] * b["height"]; target = visuals.nth(i); box = b
        except Exception:
            continue
    target = target or visuals.first
    # Reveal the visual header (where the '…' lives). A synthetic hover alone
    # doesn't reliably surface it, so: (1) click the TITLE strip at the very top
    # of the visual to SELECT it — selection keeps the header persistent and the
    # title isn't a data point so it won't cross-filter — then (2) hover the body
    # with a gradual mouse move to trigger the header's :hover as a backup.
    if box:
        try:
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + 6)
            time.sleep(1)
        except Exception:
            pass
        try:
            page.mouse.move(box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2, steps=10)
        except Exception:
            pass
    else:
        try:
            target.hover()
        except Exception:
            pass
    time.sleep(1.5)
    _shot(page, pre + "hover")
    _dump_buttons(page, tag or "x")  # capture the '…' button position while hovered
    # Click the TABLE's own '…' (nearest its top-right corner), keeping the mouse
    # over the visual so the header stays visible. Fall back to the visual-scoped
    # button if the position match misses.
    if not _click_table_more_options(page, box):
        try:
            target.get_by_role("button", name="More options").last.click(timeout=5000)
        except Exception:
            pass
    time.sleep(1)
    _shot(page, pre + "menu")
    # Guard: make sure we opened the visual's menu (which has 'Export data'),
    # not the toolbar's 'Get insights' menu.
    if page.get_by_text("Export data", exact=False).count() == 0:
        _dump_visuals(page, f"wrongmenu_{tag}")
        raise RuntimeError("visual 'More options' menu didn't open (no 'Export "
                           "data' entry) — dumped visuals")
    # "Export data" entry — prefer the real menu item over any tooltip/label.
    for loc in (page.get_by_role("menuitem", name="Export data"),
                page.get_by_text("Export data", exact=False)):
        try:
            loc.first.click(timeout=8000); break
        except Exception:
            continue
    time.sleep(2)
    _shot(page, pre + "dialog")
    # Export dialog: accept defaults and download.
    with page.expect_download(timeout=120_000) as dl:
        for loc in (page.get_by_role("button", name="Export"),
                    page.get_by_role("button", name="Export data")):
            try:
                loc.last.click(timeout=5000); break
            except Exception:
                continue
    download = dl.value
    download.save_as(str(out_path))
    print(f"   ✓ exported -> {out_path.name}")
    return out_path


# The on-canvas CRSA dropdown slicer. Its menu is a role=combobox with
# aria-label 'CRSA'; the selected value lives in '.slicer-restatement'. ':visible'
# skips the phantom 0x0 duplicate Power BI renders alongside it.
CRSA_DROPDOWN = 'div.slicer-dropdown-menu[aria-label="CRSA"]:visible'


def _dump_visuals(page, name: str) -> None:
    """Dump each visual container's bbox + text snippet, plus full HTML of any
    that mention CRSA — to identify and drive the on-canvas CRSA slicer."""
    try:
        Path(SHOT_DIR).mkdir(parents=True, exist_ok=True)
        visuals = page.locator("visual-container-modern, visual-container, .visualContainer")
        lines = [f"# {visuals.count()} visual containers"]
        for i in range(min(visuals.count(), 40)):
            v = visuals.nth(i)
            try:
                b = v.bounding_box()
                t = (v.inner_text(timeout=500) or "").replace("\n", " ")[:90]
            except Exception:
                b, t = None, "<err>"
            lines.append(f"[{i}] box={b} text={t!r}")
            if t and "CRSA" in t:
                try:
                    lines.append("    HTML: " + v.inner_html(timeout=1500)[:5000])
                except Exception as e:
                    lines.append(f"    HTML err {e}")
        (Path(SHOT_DIR) / f"visuals_{name}.txt").write_text("\n".join(lines), encoding="utf-8")
        print(f"   (visual dump: powerbi_shots/visuals_{name}.txt)")
    except Exception as e:
        print(f"   (visual dump failed: {e})")


def _dump_dom(page, name: str) -> None:
    """Write the Filters-pane structure + a roster of clickable roles so selectors
    can be fixed from real DOM instead of guesswork."""
    try:
        Path(SHOT_DIR).mkdir(parents=True, exist_ok=True)
        out = []
        for sel in ("div[aria-label='Filters']", "[class*='filterPane']",
                    "[class*='FilterPane']", ".filterContainer", "explore-filter-panel"):
            loc = page.locator(sel)
            if loc.count():
                try:
                    html = loc.first.inner_html(timeout=4000)
                    out.append(f"<!-- {sel} ({loc.count()}) -->\n{html[:20000]}")
                except Exception as e:
                    out.append(f"<!-- {sel}: {e} -->")
        for role in ("radio", "checkbox", "listitem", "button"):
            try:
                r = page.get_by_role(role)
                labels = [r.nth(i).get_attribute("aria-label")
                          for i in range(min(r.count(), 80))]
                crsa_only = [l for l in labels if l and "CRSA" in l]
                out.append(f"<!-- role={role} count={r.count()} "
                           f"CRSA-labels={crsa_only} all={labels} -->")
            except Exception as e:
                out.append(f"<!-- role={role}: {e} -->")
        (Path(SHOT_DIR) / f"dom_{name}.txt").write_text("\n\n".join(out), encoding="utf-8")
        print(f"   (DOM dump: powerbi_shots/dom_{name}.txt)")
    except Exception as e:
        print(f"   (DOM dump failed: {e})")


def _set_crsa(page, crsa: str) -> None:
    """Set the store via the on-canvas CRSA dropdown slicer: open the dropdown
    (role=combobox, aria-label 'CRSA'), click the target CRSA option, close it.
    Single-select replaces the prior store and the table requeries. Caller
    verifies via _crsa_selected()."""
    dd = page.locator(CRSA_DROPDOWN).first
    if dd.count() == 0:
        _shot(page, f"setcrsa_noslicer_{crsa}")
        _dump_visuals(page, f"noslicer_{crsa}")
        raise RuntimeError("CRSA dropdown slicer not found (dumped visuals)")
    # Open the dropdown if it isn't already (clicking the menu toggles it).
    try:
        if (dd.get_attribute("aria-expanded") or "false") != "true":
            dd.click(timeout=5000)
            time.sleep(2)
    except Exception:
        dd.click(timeout=5000)
        time.sleep(2)
    _shot(page, f"setcrsa_open_{crsa}")
    # The opened listbox renders the options; click the exact CRSA value.
    opt = page.get_by_role("option", name=re.compile(re.escape(crsa)))
    if opt.count() == 0:
        opt = page.get_by_text(crsa, exact=True)  # fallback: plain option text
    for _ in range(10):
        if opt.count() > 0:
            break
        time.sleep(1)
    if opt.count() == 0:
        _shot(page, f"setcrsa_noval_{crsa}")
        _dump_visuals(page, f"opened_{crsa}")
        raise RuntimeError(f"CRSA value '{crsa}' not found in opened slicer "
                           f"(dumped visuals)")
    opt.first.scroll_into_view_if_needed(timeout=5000)
    opt.first.click(timeout=5000)
    time.sleep(6)  # let the table requery for the new store
    # Close the dropdown + its backdrop, which otherwise blocks clicks on the
    # date inputs and the table.
    _dismiss_overlays(page)


def _crsa_selected(page, crsa: str) -> bool:
    """True iff the CRSA dropdown slicer's restatement now reads exactly this
    store — the real anti-mislabel guard. Reads '.slicer-restatement' (e.g.
    'CRSA1208505')."""
    try:
        rest = page.locator(f'{CRSA_DROPDOWN} .slicer-restatement').first
        txt = rest.inner_text(timeout=4000)
    except Exception:
        return False
    found = re.findall(r"CRSA\d+", txt)
    return found == [crsa]


def _dismiss_overlays(page) -> None:
    """Close any open dropdown overlay. Power BI/Angular put a full-screen
    transparent '.cdk-overlay-backdrop' behind dropdowns (e.g. the CRSA slicer);
    it intercepts pointer events on every other control until dismissed."""
    for _ in range(5):
        bd = page.locator(".cdk-overlay-backdrop")
        if bd.count() == 0:
            return
        try:
            bd.first.click(timeout=2000)  # clicking the backdrop closes the overlay
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
        time.sleep(0.6)


def _fmt_date(d) -> str:
    """M/d/yyyy with no zero-padding (the format the slicer expects)."""
    return f"{d.month}/{d.day}/{d.year}"


def _parse_date(s):
    from datetime import datetime
    m = re.search(r"\d{1,2}/\d{1,2}/\d{4}", s or "")
    return datetime.strptime(m.group(), "%m/%d/%Y").date() if m else None


def _latest_available_date(page):
    """The newest date the report has data for = the End input's available-range
    MAX (from its aria-label, e.g. '… 5/12/2026 to 6/14/2026'). Avoids guessing
    the data lag."""
    aria = page.locator(END_INPUT).first.get_attribute("aria-label") or ""
    dates = re.findall(r"\d{1,2}/\d{1,2}/\d{4}", aria)
    if not dates:
        raise RuntimeError(f"couldn't read latest data date from End input ('{aria}')")
    return _parse_date(dates[-1])


def _fill_date(page, selector: str, value: str) -> None:
    # Filling one date box opens a calendar popup whose backdrop blocks the OTHER
    # box — so clear any open overlay before AND after each field.
    _dismiss_overlays(page)
    inp = page.locator(selector).first
    inp.click(timeout=5000)
    inp.press("Control+a")
    inp.press("Delete")
    inp.type(value, delay=25)
    inp.press("Enter")
    time.sleep(1.5)
    _dismiss_overlays(page)


def _set_date_window(page, window_days: int):
    """Set the date slicer to [latest-(window-1), latest]. Set END first (to the
    latest data date) so START's allowed range is maximized, then START. Returns
    (start, end) dates; raises if the read-back doesn't match."""
    from datetime import timedelta
    _dismiss_overlays(page)  # clear the CRSA dropdown's backdrop first
    end = _latest_available_date(page)
    start = end - timedelta(days=window_days - 1)
    _fill_date(page, END_INPUT, _fmt_date(end))
    _fill_date(page, START_INPUT, _fmt_date(start))
    _dismiss_overlays(page)  # a typed date can leave a calendar popup open
    sv = _parse_date(page.locator(START_INPUT).first.input_value())
    ev = _parse_date(page.locator(END_INPUT).first.input_value())
    if sv != start or ev != end:
        _shot(page, f"setdate_fail_{window_days}d")
        raise RuntimeError(f"date window not set: wanted {_fmt_date(start)}..{_fmt_date(end)}, "
                           f"got {sv}..{ev}")
    return start, end


def cmd_pull(store: str | None, windows: list, headless: bool = True) -> None:
    """Reuse the saved session; for each requested store × each lookback window,
    set CRSA + date range, export both report pages, and upload. `store` =
    None/"all" does every store; `windows` is the list of lookback-day windows."""
    targets = _select_targets(store)
    if not targets:
        print(f">>> No store matched '{store}'. Known: "
              + ", ".join(s["name"] for s in STORES))
        return

    locations = _resolve_locations()  # name -> location_id, via the app

    pw, ctx = _ctx(headless=headless)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(REPORT_URL, wait_until="load", timeout=120_000)
    print("Waiting for the report to load…")
    if not _wait_logged_in(page):
        _shot(page, "not_loaded")
        print(">>> Couldn't reach the report ('step_not_loaded.png'). If it shows a "
              "login page, run `login` again; otherwise send me the shot.")
        ctx.close(); pw.stop(); return
    print("Report loaded.")
    time.sleep(5)
    _dump_date_slicer(page)  # one-time: capture the date control's DOM

    succeeded, failed = [], []
    for store_cfg in targets:
        name, crsa = store_cfg["name"], store_cfg["crsa"]
        # location_id is only needed for upload; resolve it but don't block the
        # export on it (so switch+export can be tested before creds are set).
        loc_id = _match_location(locations, name) if locations else None
        print(f"\n=== {name} ({crsa}) → location_id={loc_id or '(upload skipped)'} ===")
        for win in windows:
            label = f"{name} {win}d"
            try:
                files = []
                end_date = None
                for page_name, filename in PAGES:
                    tag = f"{crsa}_{win}d_{page_name.split()[0]}"  # CRSA…_30d_2.2
                    _go_to_page(page, page_name)
                    # CRSA + date are page-level on-canvas slicers — set + verify
                    # both on THIS page before exporting (the data table is the
                    # only thing that changes, so read-backs are the guard).
                    print(f"   [{win}d] CRSA → {crsa}")
                    _set_crsa(page, crsa)
                    if not _crsa_selected(page, crsa):
                        raise RuntimeError(f"CRSA not confirmed as {crsa} on '{page_name}'")
                    start_d, end_date = _set_date_window(page, win)
                    print(f"   [{win}d] dates {_fmt_date(start_d)}..{_fmt_date(end_date)}")
                    out = Path(DOWNLOAD_DIR) / crsa / f"{win}d" / filename
                    out.parent.mkdir(parents=True, exist_ok=True)
                    files.append(_export_table(page, out, tag=tag))
                if len(files) != 2:
                    raise RuntimeError(f"only {len(files)}/2 files exported")
                if loc_id:
                    _upload(files, loc_id, win, end_date)
                elif locations:
                    print(f"   ! no dashboard location matched '{name}'; saved "
                          f"locally, NOT uploaded")
                else:
                    print(f"   (no creds — saved to {Path(DOWNLOAD_DIR)/crsa/f'{win}d'})")
                succeeded.append(label)
            except Exception as e:
                print(f"   ✗ {label} failed: {e}")
                _shot(page, f"fail_{crsa}_{win}d")
                failed.append((label, str(e)))

    total = len(targets) * len(windows)
    print("\n" + "=" * 60)
    print(f"DONE: {len(succeeded)}/{total} store-windows pulled.")
    if succeeded:
        print("  ✓ " + ", ".join(succeeded))
    if failed:
        print("  ✗ FAILED:")
        for lbl, why in failed:
            print(f"      - {lbl}: {why}")
    ctx.close(); pw.stop()


def _select_targets(store: str | None) -> list[dict]:
    """Resolve the --store argument to a list of STORES entries."""
    if not store or store.lower() == "all":
        return list(STORES)
    s = store.strip().lower()
    return [st for st in STORES
            if s in st["name"].lower() or s == st["crsa"].lower()]


def _resolve_locations() -> list[dict]:
    """Fetch the dashboard's locations (id + name) so we can map each store to
    its location_id. Returns [] if we can't reach the app (callers then skip)."""
    if not (APP_EMAIL and APP_PASSWORD):
        return []
    try:
        import requests
        s = requests.Session()
        s.post(f"{APP_BASE}/api/auth/login",
               json={"email": APP_EMAIL, "password": APP_PASSWORD}, timeout=30
               ).raise_for_status()
        r = s.get(f"{APP_BASE}/api/locations", timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f">>> Couldn't fetch /api/locations ({e}); store→id mapping unavailable.")
        return []


def _match_location(locations: list[dict], name: str) -> str | None:
    """Match a store name token (e.g. 'Livingstone') to a dashboard location_id,
    tolerating dashboard names like 'North (Livingstone)'. Returns None if no
    single unambiguous match."""
    n = name.lower()
    hits = [loc for loc in locations
            if n in (loc.get("name", "") + " " + loc.get("short_name", "")).lower()]
    if len(hits) == 1:
        return hits[0]["id"]
    return None


def _upload(files: list[Path], location_id: str, window: int, end_date) -> None:
    """Log into the dashboard and upload the two Excel files under the given
    location_id. `end_date` is the report's latest-data date (the window's
    period_end) — NOT today, so the recorded period matches the actual data."""
    import requests
    s = requests.Session()
    r = s.post(f"{APP_BASE}/api/auth/login",
               json={"email": APP_EMAIL, "password": APP_PASSWORD}, timeout=30)
    r.raise_for_status()
    multipart = [("files", (f.name, open(f, "rb").read())) for f in files]
    r = s.post(f"{APP_BASE}/api/market-intelligence/upload",
               data={"location_id": location_id, "date": end_date.isoformat(),
                     "window_days": str(window), "replace": "true"},
               files=multipart, timeout=120)
    print("   upload:", r.status_code, r.text[:200])
    r.raise_for_status()


def main() -> None:
    ap = argparse.ArgumentParser(description="Power BI auto-pull for OCS Market Intelligence")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    sub.add_parser("probe")
    p_pull = sub.add_parser("pull")
    p_pull.add_argument("--store", default="all",
                        help="store name (e.g. Livingstone) or CRSA; default 'all' = every store")
    p_pull.add_argument("--window", type=int, default=None,
                        help="pull ONLY this lookback window (days), e.g. 30 for a quick "
                             "test; omit to pull all of " + str(WINDOWS))
    p_pull.add_argument("--watch", action="store_true",
                        help="run with a visible browser to watch the export")
    args = ap.parse_args()

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("Playwright not installed. Run:\n  pip install playwright requests\n"
              "  playwright install chromium")
        sys.exit(1)

    if args.cmd == "login":
        cmd_login()
    elif args.cmd == "probe":
        cmd_probe()
    elif args.cmd == "pull":
        windows = [args.window] if args.window else WINDOWS
        cmd_pull(args.store, windows, headless=not args.watch)


if __name__ == "__main__":
    main()

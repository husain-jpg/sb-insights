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

# Where the SB Insights dashboard lives + a login that can upload.
APP_BASE = "https://app.sbinsights.co"
APP_EMAIL = ""        # fill in (an account on the dashboard)
APP_PASSWORD = ""     # fill in

# Local folder that holds the saved Power BI browser session. Keep it private.
PROFILE_DIR = str(Path.home() / ".sbinsights_powerbi_profile")
DOWNLOAD_DIR = str(Path.home() / ".sbinsights_powerbi_downloads")


def _ctx(headless: bool):
    """Open a persistent browser context so the login session is reused."""
    from playwright.sync_api import sync_playwright
    Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(
        PROFILE_DIR, headless=headless, accept_downloads=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    return pw, ctx


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
    time.sleep(20)  # let the report render
    shot = str(Path(DOWNLOAD_DIR) / "powerbi_probe.png")
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


def _shot(page, name: str) -> None:
    try:
        page.screenshot(path=str(Path(DOWNLOAD_DIR) / f"step_{name}.png"))
        print("   (screenshot: step_%s.png)" % name)
    except Exception:
        pass


def _go_to_page(page, page_name: str) -> None:
    """Click a report page in the left navigator by its visible name."""
    print(f" → opening page '{page_name}'")
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


def _export_table(page, out_path: Path) -> Path:
    """Export the page's data table to .xlsx via the visual's More-options menu."""
    # Reveal the visual header: hover the largest visual container on the page.
    visuals = page.locator("visual-container-modern, visual-container, .visualContainer")
    n = visuals.count()
    print(f"   found {n} visual container(s); hovering the table")
    target = None
    # Pick the widest visual (the data table dominates the page).
    best_w = -1
    for i in range(min(n, 12)):
        try:
            box = visuals.nth(i).bounding_box()
            if box and box["width"] > best_w:
                best_w = box["width"]; target = visuals.nth(i)
        except Exception:
            continue
    target = target or visuals.first
    target.hover()
    time.sleep(1)
    _shot(page, "hover")
    # The "More options" (…) button on the visual header.
    for loc in (target.get_by_role("button", name="More options"),
                page.get_by_role("button", name="More options"),
                target.locator("[aria-label='More options']")):
        try:
            loc.last.click(timeout=5000); break
        except Exception:
            continue
    time.sleep(1)
    _shot(page, "menu")
    # "Export data" entry.
    page.get_by_text("Export data", exact=False).first.click(timeout=8000)
    time.sleep(2)
    _shot(page, "dialog")
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


def cmd_pull(store: str, window: int, headless: bool = True) -> None:
    """Reuse the saved session, export both report pages, upload to dashboard."""
    pw, ctx = _ctx(headless=headless)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(REPORT_URL, wait_until="load", timeout=120_000)
    time.sleep(20)
    if "login" in page.url.lower() or "signin" in page.url.lower():
        print(">>> Session expired — run `login` again."); ctx.close(); pw.stop(); return

    files: list[Path] = []
    for page_name, filename in PAGES:
        try:
            _go_to_page(page, page_name)
            out = Path(DOWNLOAD_DIR) / filename
            files.append(_export_table(page, out))
        except Exception as e:
            print(f"   ✗ failed on '{page_name}': {e}")
            _shot(page, "fail_" + page_name.split()[0])
    print(f"\nExported {len(files)} file(s) to {DOWNLOAD_DIR}")
    if len(files) == 2 and APP_EMAIL and APP_PASSWORD:
        _upload(files, store, window)
    elif len(files) == 2:
        print("Set APP_EMAIL / APP_PASSWORD in the script to auto-upload; "
              "for now the files are saved locally.")
    ctx.close(); pw.stop()


def _upload(files: list[Path], store: str, window: int) -> None:
    """Log into the dashboard and upload the two Excel files for import."""
    import requests
    from datetime import date
    s = requests.Session()
    r = s.post(f"{APP_BASE}/api/auth/login",
               json={"email": APP_EMAIL, "password": APP_PASSWORD}, timeout=30)
    r.raise_for_status()
    multipart = [("files", (f.name, open(f, "rb").read())) for f in files]
    r = s.post(f"{APP_BASE}/api/market-intelligence/upload",
               data={"location_id": store, "date": date.today().isoformat(),
                     "window_days": str(window)},
               files=multipart, timeout=120)
    print("Upload:", r.status_code, r.text[:300])


def main() -> None:
    ap = argparse.ArgumentParser(description="Power BI auto-pull for OCS Market Intelligence")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    sub.add_parser("probe")
    p_pull = sub.add_parser("pull")
    p_pull.add_argument("--store", required=True)
    p_pull.add_argument("--window", type=int, default=30)
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
        cmd_pull(args.store, args.window, headless=not args.watch)


if __name__ == "__main__":
    main()

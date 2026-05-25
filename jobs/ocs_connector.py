"""
OCS B2B portal connector — auto-pull the OCS catalogue + Order Fill (OrderExport).

STATUS: scaffolding. The orchestration (save downloaded files to imports/ and
hand them to run_import) is complete and stable. The three site-specific calls
— login(), fetch_catalogue(), fetch_order_fill() — are STUBS to be filled from a
HAR capture of a manual session (login → click Export → select format →
download), since OCS has no API. Until those are implemented and an ocs_account
row is marked is_active=1, nothing here runs (no scheduler is wired yet).

Design (see also the email scraper, jobs/email_scraper.py, which this mirrors):
  - Plain-password login (no MFA today); hold the session cookie.
  - Each report is an "Export" action that takes a product-format parameter
    (e.g. the OrderExport filename encodes "Packs"). Likely either a single
    request that returns the .xlsx, or a generate-then-poll flow — the HAR
    decides which, and that logic goes in fetch_*().
  - Downloaded files are written to imports/ with their native names, then
    run_import() routes them to import_ocs_catalog / import_order_fill_file
    (which also rebuilds the successor map and refreshes current_inventory).
  - Credentials live in the ocs_account table, encrypted with encrypt_secret.

MFA resilience: keep auth isolated in login()/_make_session so that if OCS adds
MFA later we can swap in an app-password / refreshable-cookie strategy without
touching fetch/parse. The manual upload button (POST /api/order-fill/import)
remains the permanent fallback.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

IMPORTS_DIR = Path(__file__).resolve().parent.parent / "imports"


@dataclass
class OcsAccount:
    id: int
    base_url: str
    username: str
    password: str          # decrypted
    catalogue_format: Optional[str]
    order_fill_format: Optional[str]
    is_active: bool


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def load_account(conn) -> Optional[OcsAccount]:
    """Load + decrypt the single OCS account row (most recent). None if unset."""
    from jobs.auth import decrypt_secret
    row = conn.execute(
        """SELECT id, base_url, username, password_enc, catalogue_format,
                  order_fill_format, is_active
           FROM ocs_account ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    if not row:
        return None
    return OcsAccount(
        id=row[0], base_url=row[1], username=row[2],
        password=decrypt_secret(row[3]),
        catalogue_format=row[4], order_fill_format=row[5],
        is_active=bool(row[6]),
    )


def _make_session():
    """A requests session with a browser-ish User-Agent. Imported lazily so the
    module stays importable even where requests isn't installed."""
    import requests
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    })
    return s


# ---------------------------------------------------------------------------
# Site-specific calls — first cut from the HAR (ASP.NET MVC portal on Icescape).
# UNVALIDATED against the live site (the HAR had response bodies stripped):
# login-success detection, the SelectStore page's retailer-list HTML, and
# whether the export GETs stream the file vs redirect to a blob all need
# confirming on the first authenticated run. Endpoints are known:
#   POST /Admin/Login            (Email, Password, RememberMe, + hidden fields)
#   POST /Admin/SelectStore      (retailerID, redirect)
#   GET  /Sales/GenerateOrderExportFile?packType=<n>&exportOrderTemplate=true
#   GET  /sales/GenerateCatelogue
# ---------------------------------------------------------------------------
import re as _re

_HIDDEN_INPUT_RE = _re.compile(
    r'<input[^>]*\btype=["\']hidden["\'][^>]*>', _re.IGNORECASE)
_ATTR_RE = _re.compile(r'\b(name|value)=["\']([^"\']*)["\']', _re.IGNORECASE)


def _hidden_fields(html: str) -> dict:
    """Extract hidden <input name=value> pairs from a form page so we resubmit
    them verbatim (ReturnUrl, CallbackUrl, anti-forgery token if present, …)."""
    out: dict = {}
    for tag in _HIDDEN_INPUT_RE.findall(html or ""):
        attrs = dict((k.lower(), v) for k, v in _ATTR_RE.findall(tag))
        if attrs.get("name"):
            out[attrs["name"]] = attrs.get("value", "")
    return out


def _filename_from_response(resp, default: str) -> str:
    cd = resp.headers.get("content-disposition", "") or ""
    m = _re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';\r\n]+)', cd, _re.IGNORECASE)
    return (m.group(1).strip() if m else default)


def login(session, account: OcsAccount) -> None:
    """Authenticate the session. GET the sign-in page to pick up hidden form
    fields, then POST /Admin/Login. Confirms a session by checking we no longer
    bounce to the sign-in page. Raises on failure.

    NEEDS LIVE VALIDATION: the success signal (the Login response is small JSON)
    and the exact sign-in page path may need adjusting once we see real bodies.
    """
    base = account.base_url.rstrip("/")
    session.get(f"{base}/Admin/LoginPreReq", timeout=30)  # mirror the browser precheck
    signin = session.get(f"{base}/Admin/Signin", timeout=30)
    form = _hidden_fields(signin.text)
    form.update({
        "Email": account.username,
        "Password": account.password,
        "RememberMe": "true",
    })
    resp = session.post(f"{base}/Admin/Login", data=form, timeout=30,
                        headers={"Referer": f"{base}/Admin/Signin"})
    # Heuristic success check until we confirm the JSON shape live.
    ok = resp.ok and "Admin/Signin" not in (resp.url or "")
    body = (resp.text or "").lower()
    if '"success":false' in body or "invalid" in body and "password" in body:
        ok = False
    if not ok:
        raise RuntimeError(f"OCS login failed (status {resp.status_code})")


def select_store(session, account: OcsAccount, retailer_id: str) -> None:
    """Set the active store context (POST /Admin/SelectStore)."""
    base = account.base_url.rstrip("/")
    session.post(f"{base}/Admin/SelectStore",
                 data={"retailerID": retailer_id, "redirect": "/"}, timeout=30,
                 headers={"Referer": f"{base}/Admin/SelectStore"})


def _parse_store_blocks(html: str) -> list[dict]:
    """Parse the SelectStore page's per-store ``storediv`` blocks.

    Each store carries data-retailer-name, a hidden hdnStoreNumber (e.g. 6001),
    and a hidden hdnERetailerID token (URL-encoded; this is what SelectStore
    posts and may be session-bound, so it's re-read each run). Returns
    [{"store_number", "name", "token"}, …].
    """
    out: list[dict] = []
    # Split on each store block's name attribute; chunk[i>0] holds one store.
    parts = _re.split(r'data-retailer-name=', html or "")
    for chunk in parts[1:]:
        nm = _re.match(r'["\']([^"\']*)["\']', chunk)
        addr = _re.search(r'data-retailer-address=["\']([^"\']*)["\']', chunk)
        sn = _re.search(r'hdnStoreNumber["\']\s+value=["\']([^"\']+)["\']', chunk)
        tok = _re.search(r'hdnERetailerID["\']\s+value=["\']([^"\']+)["\']', chunk)
        store_number = sn.group(1).strip() if sn else ""
        token = tok.group(1).strip() if tok else ""
        if store_number or token:
            out.append({
                "store_number": store_number,
                "name": (nm.group(1).strip() if nm else ""),
                "address": (addr.group(1).strip() if addr else ""),
                "token": token,
            })
    return out


def list_retailers(session, account: OcsAccount) -> list[dict]:
    """Fetch + parse the SelectStore retailer list for the store-mapping UI.
    Returns [{"retailer_id", "store_number", "name"}, …] where retailer_id is
    the (stable) store number used as the mapping key."""
    base = account.base_url.rstrip("/")
    html = session.get(f"{base}/Admin/SelectStore", timeout=30).text or ""
    return [{"retailer_id": b["store_number"], "store_number": b["store_number"],
             "name": b["name"], "address": b["address"]}
            for b in _parse_store_blocks(html)]


def _store_tokens(session, account: OcsAccount) -> dict:
    """Map {store_number: current hdnERetailerID token} from a fresh SelectStore
    page (tokens may be session-bound, so resolve them at run time)."""
    base = account.base_url.rstrip("/")
    html = session.get(f"{base}/Admin/SelectStore", timeout=30).text or ""
    return {b["store_number"]: b["token"] for b in _parse_store_blocks(html) if b["store_number"]}


def fetch_catalogue(session, account: OcsAccount) -> tuple[bytes, str]:
    """Download the OCS catalogue export (chain-wide; same for all stores)."""
    base = account.base_url.rstrip("/")
    resp = session.get(f"{base}/sales/GenerateCatelogue", timeout=120, allow_redirects=True)
    resp.raise_for_status()
    fname = _filename_from_response(resp, "OCS_Catalogue.xlsx")
    return resp.content, fname


def fetch_order_fill(session, account: OcsAccount, pack_type: int = 1) -> tuple[bytes, str] | None:
    """Download the OrderExport for the currently-selected store.

    Returns (bytes, filename), or None if no order form is available for this
    store right now (each store's form only exists ~7pm the night before its
    order day until that day's deadline). pack_type 1 = 'Packs'.
    """
    base = account.base_url.rstrip("/")
    resp = session.get(f"{base}/Sales/GenerateOrderExportFile",
                       params={"packType": pack_type, "exportOrderTemplate": "true"},
                       timeout=120, allow_redirects=True)
    ct = (resp.headers.get("content-type") or "").lower()
    # No form available → portal returns HTML/JSON/empty rather than a spreadsheet.
    if not resp.ok or not resp.content or "spreadsheet" not in ct and "octet-stream" not in ct \
            and "excel" not in ct and ".xls" not in (resp.headers.get("content-disposition", "").lower()):
        return None
    fname = _filename_from_response(resp, "OrderExport.xlsx")
    return resp.content, fname


# ---------------------------------------------------------------------------
# Orchestration (complete + stable; independent of the site specifics above)
# ---------------------------------------------------------------------------
def _save_to_imports(content: bytes, filename: str) -> Path:
    IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    safe = Path(filename).name or "ocs_download.xlsx"
    dest = IMPORTS_DIR / safe
    with open(dest, "wb") as f:
        f.write(content)
    return dest


def sync_ocs(conn, db_path: str) -> dict:
    """Pull the catalogue + Order Fill and import them. Records run status on
    the ocs_account row. No-op (returns skipped) if the account is missing or
    inactive — so this is safe to call before login() is implemented.

    `conn` is used for account load + status writes; `db_path` is passed to
    run_import, which opens its own connection (matching the email scraper).
    """
    from jobs.import_cova_exports import run_import

    account = load_account(conn)
    if account is None or not account.is_active:
        return {"status": "skipped", "reason": "no active OCS account"}

    from jobs.import_order_fill import import_order_fill_file

    imported: list[str] = []
    try:
        session = _make_session()
        login(session, account)

        # 1) Catalogue — chain-wide (identical for all stores), one fetch.
        content, filename = fetch_catalogue(session, account)
        cat_path = _save_to_imports(content, filename)
        run_import(cat_path, db_path)
        imported.append(cat_path.name)

        # 2) OrderExport — per store. The mapping stores OCS store numbers; the
        # SelectStore call needs the encrypted retailer token, which is resolved
        # fresh from the SelectStore page (tokens are session-bound). Poll every
        # mapped store; only the store(s) inside their order window return a file
        # (else None → skip, don't clobber). Files are saved store-prefixed so
        # import_order_fill_file tags a distinct per-store run.
        tokens = _store_tokens(session, account)  # {store_number: token}
        for store_number, location_id in store_mappings(conn):
            token = tokens.get(str(store_number))
            if not token:
                continue  # mapped store not present on the portal
            select_store(session, account, token)
            of = fetch_order_fill(session, account, pack_type=1)  # 1 = Packs
            if not of:
                continue  # no form available for this store right now
            content, filename = of
            path = _save_to_imports(content, f"{location_id}__{filename}")
            import_order_fill_file(conn, path, location_id=location_id)
            imported.append(path.name)

        _record_status(conn, account.id, "ok", None)
        log.info("OCS sync imported %d file(s): %s", len(imported), imported)
        return {"status": "ok", "imported": imported}
    except Exception as e:  # noqa: BLE001 — record then re-raise to caller's log
        _record_status(conn, account.id, "error", str(e))
        log.warning("OCS sync failed: %s", e)
        return {"status": "error", "error": str(e), "imported": imported}


def store_mappings(conn) -> list[tuple[str, str]]:
    """Return [(ocs_retailer_id, our_location_id), …] from the ocs_store_map
    table. Empty until configured during the first live run (we'll parse the
    retailer list off the SelectStore page and map each to S1–S8). When empty,
    sync_ocs simply imports the catalogue and skips the per-store OrderExport.
    """
    try:
        rows = conn.execute(
            "SELECT ocs_retailer_id, location_id FROM ocs_store_map WHERE is_active = 1"
        ).fetchall()
        return [(str(r[0]), str(r[1])) for r in rows]
    except Exception:
        return []


def _record_status(conn, account_id: int, status: str, error: Optional[str]) -> None:
    conn.execute(
        "UPDATE ocs_account SET last_run_at = ?, last_status = ?, last_error = ? WHERE id = ?",
        (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), status, error, account_id),
    )
    conn.commit()

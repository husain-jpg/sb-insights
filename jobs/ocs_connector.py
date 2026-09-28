"""
OCS B2B portal connector — auto-pull the OCS catalogue + Order Fill (OrderExport).

STATUS: scaffolding. The orchestration (save downloaded files to imports/ and
hand them to run_import) is complete and stable. The three site-specific calls
— login(), fetch_catalogue(), fetch_order_fill() — are STUBS to be filled from a
HAR capture of a manual session (login → click Export → select format →
download), since OCS has no API. Until those are implemented and an ocs_account
row is marked is_active=1, nothing here runs (no scheduler is wired yet).

Design (see also the email scraper, jobs/email_scraper.py, which this mirrors):
  - Password login plus a saved MFA trusted-device cookie (OCS made SMS MFA
    mandatory in Sept 2026; see "Cookie persistence" below).
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
    last_status: Optional[str] = None   # 'ok' | 'error' | AUTH_FAILED_STATUS | MFA_REQUIRED_STATUS
    last_error: Optional[str] = None
    cookies: Optional[str] = None       # decrypted JSON cookie jar (holds the MFA trusted-device cookie)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
# last_status value that pauses automatic retries (see the lockout guard in
# sync_ocs). Kept as a constant so the API layer can clear it on config save.
AUTH_FAILED_STATUS = "auth_error"
# last_status when the password was accepted but OCS wants an SMS code, i.e. the
# trusted-device cookie is missing or expired. Also pauses automatic runs: only
# a person with the phone can clear it, via Verify in Settings.
MFA_REQUIRED_STATUS = "mfa_required"


def ensure_columns(conn) -> None:
    """Add the MFA cookie columns if missing. init_schema also adds them, but it
    only runs during imports, and the connector can be used before the next one."""
    from db.sqlite_schema import _ensure_column
    _ensure_column(conn, "ocs_account", "session_cookies_enc", "TEXT")
    _ensure_column(conn, "ocs_account", "trusted_until", "TEXT")
    conn.commit()


def load_account(conn) -> Optional[OcsAccount]:
    """Load + decrypt the single OCS account row (most recent). None if unset."""
    from jobs.auth import decrypt_secret
    ensure_columns(conn)
    row = conn.execute(
        """SELECT id, base_url, username, password_enc, catalogue_format,
                  order_fill_format, is_active, last_status, last_error,
                  session_cookies_enc
           FROM ocs_account ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    if not row:
        return None
    return OcsAccount(
        id=row[0], base_url=row[1], username=row[2],
        password=decrypt_secret(row[3]),
        catalogue_format=row[4], order_fill_format=row[5],
        is_active=bool(row[6]),
        last_status=row[7], last_error=row[8],
        cookies=(decrypt_secret(row[9]) if row[9] else None),
    )


def _make_session(account: Optional[OcsAccount] = None):
    """A requests session with a browser-ish User-Agent, preloaded with the
    account's saved cookies (the MFA trusted-device cookie lives there).
    Imported lazily so the module stays importable without requests.

    Keep the User-Agent constant: OCS remembers a trusted device per browser,
    so changing it may void the trust and force a new SMS code."""
    import requests
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    })
    if account is not None and account.cookies:
        _import_cookies(s, account.cookies)
    return s


# ---------------------------------------------------------------------------
# Cookie persistence: how the connector survives OCS's SMS-only MFA.
# ---------------------------------------------------------------------------
# OCS made MFA mandatory in Sept 2026 with SMS / voice codes only (no
# authenticator app), so the connector cannot answer it alone. What OCS offers
# instead is "remember this device for 30 days": after one verified sign-in,
# /Admin/RegisterTrustedDevice sets a persistent cookie that lets later
# password logins skip MFA. We keep that cookie jar encrypted on the account
# row and replay it on every run; an admin re-verifies about once a month.
#
# Only persistent (expiring) cookies are kept. Session cookies belong to one
# sign-in, and replaying a stale ASP.NET session could confuse the login.
def _export_cookies(session) -> str:
    import json as _json
    import time as _t
    now = _t.time()
    jar = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
            "expires": c.expires, "secure": bool(c.secure)}
           for c in session.cookies
           if c.expires is not None and c.expires > now]
    return _json.dumps(jar)


def _import_cookies(session, blob: str) -> None:
    import json as _json
    import time as _t
    try:
        jar = _json.loads(blob)
    except (ValueError, TypeError):
        return
    now = _t.time()
    for c in jar or []:
        if c.get("expires") and c["expires"] <= now:
            continue
        session.cookies.set(c["name"], c["value"], domain=c.get("domain"),
                            path=c.get("path") or "/", expires=c.get("expires"),
                            secure=bool(c.get("secure")))


def save_session_cookies(conn, account_id: int, session,
                         trusted_until: Optional[str] = None) -> None:
    """Persist the session's cookie jar, encrypted. Called after every
    successful login so rotated cookies are kept; trusted_until is set only by
    the MFA verify flow."""
    from jobs.auth import encrypt_secret
    enc = encrypt_secret(_export_cookies(session))
    if trusted_until:
        conn.execute(
            "UPDATE ocs_account SET session_cookies_enc = ?, trusted_until = ? WHERE id = ?",
            (enc, trusted_until, account_id))
    else:
        conn.execute("UPDATE ocs_account SET session_cookies_enc = ? WHERE id = ?",
                     (enc, account_id))
    conn.commit()


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


def _looks_like_maintenance(text: str) -> bool:
    return "maintenance" in (text or "").lower()


class OcsAuthError(RuntimeError):
    """Login was rejected by the portal.

    Distinct from a transport/parse failure because the portal enforces a
    5-attempt lockout: the scheduler must STOP retrying on this, not back off
    and try again tomorrow. `attempts_remaining` is parsed from the portal's
    own message when it offers one.
    """

    def __init__(self, message: str, attempts_remaining: int | None = None):
        super().__init__(message)
        self.attempts_remaining = attempts_remaining


class OcsMfaRequired(OcsAuthError):
    """The password was accepted but OCS wants an SMS code (no trusted-device
    cookie, or it expired). Retrying can't help; only someone with the phone
    can clear it."""


class OcsMfaError(RuntimeError):
    """A step of the interactive MFA flow failed (wrong or expired code, MFA
    locked). `locked` means an OCS admin must reset MFA on the account."""

    def __init__(self, message: str, locked: bool = False):
        super().__init__(message)
        self.locked = locked


_ATTEMPTS_RE = _re.compile(r"(\d+)\s*/\s*(\d+)\s*attempts?\s*remaining", _re.I)


def _login_rejection(resp) -> tuple[str, int | None] | None:
    """Return (message, attempts_remaining) if the portal rejected this login.

    /Admin/Login answers with HTTP 200 + JSON in every case, e.g.
        {"enable_Warning_Message":"Incorrect E-mail/Password. Please try
          again. 3/5 Attempts remaining.","redirect":9}
    so status codes and generic substring sniffing are both useless. Trust the
    portal's own warning field — it names the failure and counts down to
    lockout. Anything unparseable is treated as NOT a rejection; the
    authenticated-page probe is the backstop.
    """
    body = resp.text or ""
    warning = None
    try:
        import json as _json
        data = _json.loads(body)
        if isinstance(data, dict):
            warning = (data.get("enable_Warning_Message")
                       or data.get("Enable_Warning_Message") or "").strip()
    except (ValueError, TypeError):
        # Not JSON — fall back to the legacy HTML-ish checks so a portal
        # change back to form posts still surfaces a failure.
        low = body.lower()
        if '"success":false' in low or ("incorrect" in low and "password" in low)                 or ("invalid" in low and "password" in low):
            warning = "login rejected (non-JSON response)"
    if not warning:
        return None
    m = _ATTEMPTS_RE.search(warning)
    return warning, (int(m.group(1)) if m else None)


def _assert_authenticated(session, account: OcsAccount) -> None:
    """Definitive post-login probe. GET the SelectStore page and require the
    per-store blocks that _parse_store_blocks needs — only an authenticated
    session sees those. Raises with a precise reason otherwise.

    This exists because the portal answers EVERYTHING with HTTP 200: a failed
    login, a maintenance page, a sign-in bounce. Without this probe, runs
    reported 'ok' for days while saving HTML pages as .xlsx (June 7-12 2026)."""
    base = account.base_url.rstrip("/")
    resp = session.get(f"{base}/Admin/SelectStore", timeout=30)
    body = (resp.text or "").lower()
    if _looks_like_maintenance(body):
        raise RuntimeError("OCS portal is in maintenance mode — try again later")
    # Probe on PARSED store blocks, not on the words "storediv"/"hdnstorenumber".
    # The portal now serves the store-picker shell to anonymous sessions too —
    # those strings appear in its markup/JS whether or not you are logged in, so
    # the old substring test passed for an unauthenticated session and handed
    # back "ok" while the catalogue fetch was really downloading the login page.
    # Verified 2026-09-21: anonymous GET returns 200/28KB containing "storediv"
    # but zero parseable store blocks. Real store blocks require a session.
    stores = _parse_store_blocks(resp.text or "")
    if not stores:
        raise OcsAuthError(
            "OCS login did not reach the store picker (no store blocks parsed) — "
            "wrong credentials, MFA now required, or the portal layout changed")


def _post_login(session, account: OcsAccount) -> dict:
    """GET the sign-in page for its hidden fields, POST /Admin/Login, and
    return the portal's JSON verdict. Raises OcsAuthError if the password was
    rejected. Does NOT handle MFA or probe for a real session; callers do."""
    base = account.base_url.rstrip("/")
    session.get(f"{base}/Admin/LoginPreReq", timeout=30)  # mirror the browser precheck
    signin = session.get(f"{base}/Admin/Signin", timeout=30)
    if _looks_like_maintenance(signin.text):
        raise RuntimeError("OCS portal is in maintenance mode — try again later")
    form = _hidden_fields(signin.text)
    form.update({
        "Email": account.username,
        "Password": account.password,
        "RememberMe": "true",
    })
    resp = session.post(f"{base}/Admin/Login", data=form, timeout=30,
                        headers={"Referer": f"{base}/Admin/Signin"})
    if not resp.ok:
        raise RuntimeError(f"OCS login failed (status {resp.status_code})")
    rejected = _login_rejection(resp)
    if rejected:
        msg, remaining = rejected
        raise OcsAuthError(f"OCS rejected the login: {msg}", attempts_remaining=remaining)
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if data.get("mfaLocked"):
        raise OcsAuthError("OCS has locked MFA on this account — an OCS admin must reset it")
    return data


def _mfa_pending(data: dict) -> bool:
    """Mirrors the portal's own sign-in script: a code is demanded when the
    verdict has mfaEnabled and isn't an error (redirect 9). A trusted device
    comes back with mfaEnabled false and goes straight through."""
    return bool(data.get("mfaEnabled")) and data.get("redirect") != 9


def login(session, account: OcsAccount) -> None:
    """Authenticate the session for an unattended run. Relies on the saved
    trusted-device cookie (see _make_session) to get past MFA; if OCS asks for
    a code anyway, raises OcsMfaRequired instead of sending one, since no one
    is there to type it. Success is verified by probing an authenticated-only
    page (see _assert_authenticated)."""
    data = _post_login(session, account)
    if _mfa_pending(data):
        raise OcsMfaRequired(
            "OCS is asking for an SMS verification code — the remembered-device "
            "sign-in has expired or was never set up. An admin needs to use "
            "Verify in Settings → OCS Connector.")
    _assert_authenticated(session, account)


# ---------------------------------------------------------------------------
# Interactive MFA (Settings → OCS Connector → Verify). Endpoints and statusId
# codes come from the portal's /js/mfaverify.js; each is a jQuery-style AJAX
# POST answering JSON.
# ---------------------------------------------------------------------------
TRUSTED_DEVICE_DAYS = 30  # window.mfaConfig.trustedDeviceDurationDays on the sign-in page
_MFA_LOCKED_MSG = "OCS has locked MFA on this account — an OCS admin must reset it"


def _ajax(session, account: OcsAccount, path: str, data: Optional[dict] = None):
    base = account.base_url.rstrip("/")
    resp = session.post(f"{base}{path}", data=data or {}, timeout=30,
                        headers={"X-Requested-With": "XMLHttpRequest",
                                 "Referer": f"{base}/Admin/Signin"})
    if not resp.ok:
        raise OcsMfaError(f"OCS {path} failed (status {resp.status_code})")
    try:
        return resp.json()
    except ValueError:
        raise OcsMfaError(f"OCS {path} did not return JSON")


def mfa_start(session, account: OcsAccount) -> dict:
    """Log in with the password and, if OCS wants a code, text one to the
    account's default enrolled phone. Returns {"needed": False} when the saved
    trusted-device cookie already gets us in, else
    {"needed": True, "req_id", "phone"}."""
    data = _post_login(session, account)
    if not _mfa_pending(data):
        _assert_authenticated(session, account)
        return {"needed": False}
    if not data.get("mfaRegistered"):
        raise OcsMfaError("This OCS account has no phone enrolled for MFA — "
                          "sign in once on the OCS website to enrol one")
    phones = _ajax(session, account, "/Admin/GetEnrolledPhones")
    if not phones:
        raise OcsMfaError("OCS returned no enrolled phone numbers")
    phone = next((p for p in phones if p.get("isDefault")), phones[0])
    res = _ajax(session, account, "/Admin/SendLoginMFACode",
                {"enrolledPhoneId": phone.get("id"), "method": "SMS"})
    if res.get("statusId") == -1:
        raise OcsMfaError(_MFA_LOCKED_MSG, locked=True)
    if res.get("statusId") != 1:
        raise OcsMfaError(res.get("message") or "OCS could not send a verification code")
    masked = f"{phone.get('areaCode') or ''} {phone.get('maskedPhoneNumber') or ''}".strip()
    return {"needed": True, "req_id": res.get("value"),
            "phone": res.get("phoneNumber") or masked}


def mfa_resend(session, account: OcsAccount, req_id) -> dict:
    """Text a fresh code (the previous one stops working). Returns the new req_id."""
    res = _ajax(session, account, "/Admin/ResendLoginVerifyMFACode", {"reqId": req_id})
    if res.get("statusId") == -1:
        raise OcsMfaError(_MFA_LOCKED_MSG, locked=True)
    if res.get("statusId") != 1:
        raise OcsMfaError(res.get("message") or "OCS could not resend the code")
    return {"req_id": res.get("value")}


def mfa_verify(session, account: OcsAccount, req_id, code: str) -> str:
    """Submit the SMS code, then register this server as a trusted device so
    unattended logins skip MFA. Returns trusted_until (UTC 'YYYY-MM-DD HH:MM:SS')."""
    from datetime import timedelta
    res = _ajax(session, account, "/Admin/VerifyLoginMFACode", {"reqId": req_id, "otp": code})
    sid = res.get("statusId")
    if sid == -3:
        raise OcsMfaError("Too many wrong codes. " + _MFA_LOCKED_MSG, locked=True)
    if sid == -2:
        raise OcsMfaError("That code was replaced by a newer one — use Resend code")
    if sid == -4:
        raise OcsMfaError("Incorrect code, and OCS has cancelled it — use Resend code for a new one")
    if sid != 1:
        raise OcsMfaError("Incorrect code — check the text message and try again")
    base = account.base_url.rstrip("/")
    trust = session.post(
        f"{base}/Admin/RegisterTrustedDevice",
        data={"deviceType": "Desktop", "operatingSystem": "Linux - SB Insights server",
              "browserType": "Chrome", "browserVersion": "124.0"},
        timeout=30,
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{base}/Admin/Signin"})
    if not trust.ok:
        raise OcsMfaError(f"Code accepted, but OCS refused to remember this device "
                          f"(status {trust.status_code})")
    _assert_authenticated(session, account)
    until = datetime.now(timezone.utc) + timedelta(days=TRUSTED_DEVICE_DAYS)
    return until.strftime("%Y-%m-%d %H:%M:%S")


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


# Excel magic bytes: .xlsx is a zip (PK\x03\x04); legacy .xls is OLE2.
_EXCEL_MAGICS = (b"PK\x03\x04", b"\xd0\xcf\x11\xe0")


def fetch_catalogue(session, account: OcsAccount) -> tuple[bytes, str]:
    """Download the OCS catalogue export (chain-wide; same for all stores).
    Validates the payload is actually an Excel file — the portal returns
    HTML (sign-in / maintenance page) with HTTP 200 when not authenticated,
    which previously got saved as OCS_Catalogue.xlsx and silently skipped
    by the importer."""
    base = account.base_url.rstrip("/")
    resp = session.get(f"{base}/sales/GenerateCatelogue", timeout=120, allow_redirects=True)
    resp.raise_for_status()
    if not resp.content or not resp.content.startswith(_EXCEL_MAGICS):
        if _looks_like_maintenance(resp.text if "html" in (resp.headers.get("content-type") or "") else ""):
            raise RuntimeError("OCS portal is in maintenance mode — catalogue download returned the maintenance page")
        # Dump the page for diagnosis — when the portal changes its export
        # flow, this is the only way to see what it served instead.
        debug_path = IMPORTS_DIR / "_ocs_debug_last_response.html"
        try:
            IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
            debug_path.write_bytes(resp.content)
        except OSError:
            pass
        raise RuntimeError(
            f"OCS catalogue download is not an Excel file "
            f"(content-type {resp.headers.get('content-type')!r}, {len(resp.content)} bytes) — "
            f"page saved to {debug_path.name} for diagnosis")
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
    # Belt-and-braces: even with a spreadsheet content-type, require real
    # Excel magic bytes so an HTML error page can never be saved as .xlsx.
    if not resp.content.startswith(_EXCEL_MAGICS):
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


def _move_to_processed(path: Path) -> None:
    """Move an imported download into imports/processed/ so it isn't re-imported
    on the next startup (collision-safe). Mirrors run.py's behavior."""
    proc = IMPORTS_DIR / "processed"
    proc.mkdir(parents=True, exist_ok=True)
    dest = proc / path.name
    i = 1
    while dest.exists():
        dest = proc / f"{path.stem}_{i}{path.suffix}"
        i += 1
    try:
        path.rename(dest)
    except OSError:
        pass


def sync_ocs(conn, db_path: str, force: bool = False) -> dict:
    """Pull the catalogue + Order Fill and import them. Records run status on
    the ocs_account row.

    force=False (the scheduler) skips when the account is inactive. force=True
    (a manual "Run now") runs regardless of is_active, as long as an account
    exists. `conn` loads the account + writes status; `db_path` is passed to
    run_import, which opens its own connection (matching the email scraper).
    """
    from jobs.import_cova_exports import run_import

    account = load_account(conn)
    if account is None:
        return {"status": "skipped", "reason": "no OCS account configured"}
    if not force and not account.is_active:
        return {"status": "skipped", "reason": "OCS account inactive"}
    # Lockout guard. The portal locks the account after 5 consecutive failed
    # logins and counts them across days, so a nightly retry on a bad password
    # walks straight into a lockout — which would take the connector down for
    # everyone, not just the catalogue. Once a login is REJECTED (as opposed to
    # a network/parse error) we stop attempting until someone re-enters the
    # credentials; saving the config clears this. A manual "Run now"
    # (force=True) is the deliberate retry after fixing the password.
    if not force and account.last_status == MFA_REQUIRED_STATUS:
        return {"status": "skipped",
                "reason": ("OCS needs an SMS verification code — use Verify in "
                           "Settings → OCS Connector.")}
    if not force and account.last_status == AUTH_FAILED_STATUS:
        return {"status": "skipped",
                "reason": ("OCS login was rejected on the last attempt and the portal "
                           "locks the account after 5 failures — not retrying. "
                           "Re-enter the OCS password in Settings, then use Run now. "
                           f"Last error: {account.last_error or 'n/a'}")}

    from jobs.import_order_fill import import_order_fill_file

    imported: list[str] = []
    try:
        session = _make_session(account)
        login(session, account)
        save_session_cookies(conn, account.id, session)  # keep rotated cookies

        # 1) Catalogue — chain-wide (identical for all stores), one fetch.
        content, filename = fetch_catalogue(session, account)
        cat_path = _save_to_imports(content, filename)
        run_import(cat_path, db_path)
        _move_to_processed(cat_path)
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
            _move_to_processed(path)
            imported.append(path.name)

        _record_status(conn, account.id, "ok", None)
        log.info("OCS sync imported %d file(s): %s", len(imported), imported)
        return {"status": "ok", "imported": imported}
    except OcsAuthError as e:
        # Distinct status so the scheduler stops instead of burning attempts.
        status = MFA_REQUIRED_STATUS if isinstance(e, OcsMfaRequired) else AUTH_FAILED_STATUS
        remaining = getattr(e, "attempts_remaining", None)
        detail = str(e)
        if remaining is not None:
            detail += (f"  [{remaining} login attempt(s) left before OCS locks "
                       f"the account — automatic retries are now paused]")
        _record_status(conn, account.id, status, detail)
        log.error("OCS sync failed authentication: %s", detail)
        return {"status": status, "error": detail, "imported": imported}
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

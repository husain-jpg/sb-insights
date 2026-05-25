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
# Site-specific calls — FILL FROM HAR (see module docstring)
# ---------------------------------------------------------------------------
def login(session, account: OcsAccount) -> None:
    """Authenticate the session against the OCS portal.

    TODO(HAR): GET the login page, parse any CSRF/anti-forgery token, POST the
    credential form, and confirm the session cookie is set. Raise on failure.
    """
    raise NotImplementedError("OCS login not implemented — fill from HAR capture")


def fetch_catalogue(session, account: OcsAccount) -> tuple[bytes, str]:
    """Return (file_bytes, filename) for the OCS catalogue export.

    TODO(HAR): replicate the Export action with account.catalogue_format. If the
    portal generates asynchronously, poll until ready, then download.
    """
    raise NotImplementedError("OCS catalogue fetch not implemented — fill from HAR")


def fetch_order_fill(session, account: OcsAccount) -> tuple[bytes, str]:
    """Return (file_bytes, filename) for the OrderExport (Order Fill).

    TODO(HAR): replicate the Export action with account.order_fill_format
    (e.g. 'Packs'). Handle generate-then-poll if that's the flow.
    """
    raise NotImplementedError("OCS Order Fill fetch not implemented — fill from HAR")


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

    imported: list[str] = []
    try:
        session = _make_session()
        login(session, account)
        for fetch in (fetch_catalogue, fetch_order_fill):
            content, filename = fetch(session, account)
            path = _save_to_imports(content, filename)
            run_import(path, db_path)
            imported.append(path.name)
        _record_status(conn, account.id, "ok", None)
        log.info("OCS sync imported %d file(s): %s", len(imported), imported)
        return {"status": "ok", "imported": imported}
    except Exception as e:  # noqa: BLE001 — record then re-raise to caller's log
        _record_status(conn, account.id, "error", str(e))
        log.warning("OCS sync failed: %s", e)
        return {"status": "error", "error": str(e), "imported": imported}


def _record_status(conn, account_id: int, status: str, error: Optional[str]) -> None:
    conn.execute(
        "UPDATE ocs_account SET last_run_at = ?, last_status = ?, last_error = ? WHERE id = ?",
        (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), status, error, account_id),
    )
    conn.commit()

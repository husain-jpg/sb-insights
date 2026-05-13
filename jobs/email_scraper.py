"""
Email scraper — Cova link-based reports
=======================================

Cova sends daily scheduled reports via email. Each email contains a single
pre-signed Azure Blob Storage download link. The scraper:

1. Polls an IMAP inbox
2. Finds emails from noreply@covasoft.com
3. Extracts the Azure Blob URL
4. Downloads the file (links are ~7-day valid, no auth needed)
5. Saves to imports/ for the existing auto-importer to ingest

Email format Cova uses today (May 2026):
  Sender:  noreply@covasoft.com
  Subject: "<Report Name> - Daily (scheduled report)"
  Body:    HTML with one link to https://covareportserviceprod.blob.core.windows.net/...
  Attachments: none

Reports today (each contains all 8 stores in one file):
  - Inventory on Hand by Product
  - Itemized Sales
  - Discount Report

The existing imports/ auto-importer handles multi-store files (reads the
store column inside each row), so we just dump everything in imports/ and
move on.

Design notes:
- We mark emails as "seen" via the email_scraper_log (UID-based dedupe).
- Downloaded filenames preserve the original Cova naming
  ("Inventory On Hand by Product - 20260511-233125998.xlsx") with a
  timestamp prefix to avoid collisions if Cova reuses names.
- Errors are logged but don't crash the whole poll; if one email fails,
  the next one still gets processed.
"""
from __future__ import annotations

import email
import imaplib
import re
import sqlite3
from datetime import datetime
from email.header import decode_header
from pathlib import Path
from urllib.parse import urlparse, unquote, urlunparse, quote

import requests

from jobs.auth import decrypt_secret


# Match Cova's Azure Blob Storage URLs. The hostname is specific enough
# that this is safe — no risk of accidentally downloading random URLs.
# Important: we match up to the closing HTML attribute char ('"', "'", '<', '>')
# rather than whitespace, because Cova sends URLs with literal spaces in the
# filename (e.g., "Inventory On Hand by Product - ..."). Technically the HTML
# is malformed (browsers tolerate it) but our regex must handle reality.
COVA_BLOB_URL_PATTERN = re.compile(
    r"https://covareportserviceprod\.blob\.core\.windows\.net/[^\"'<>\r\n]+",
    re.IGNORECASE,
)

# Recognized Cova sender (single source of truth for filtering)
COVA_SENDER = "noreply@covasoft.com"

# Where downloaded files go. Existing auto-importer scans this folder.
IMPORTS_DIR = Path(__file__).parent.parent / "imports"

# Reasonable per-download timeout
DOWNLOAD_TIMEOUT_S = 60


def _decode_str(raw) -> str:
    """Decode a possibly RFC2047-encoded header."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return ""
    parts = decode_header(str(raw))
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", errors="replace"))
            except Exception:
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _is_cova_email(sender: str) -> bool:
    """Check if an email is from Cova. Permissive match — Cova's exact
    sender format may include display names like 'Cova <noreply@covasoft.com>'."""
    if not sender:
        return False
    return COVA_SENDER.lower() in sender.lower()


def _extract_message_body(msg) -> str:
    """Walk message parts and return concatenated text/html bodies.
    Cova links are usually in HTML, but we check text too for safety."""
    bodies = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                bodies.append(payload.decode(charset, errors="replace"))
        except Exception:
            continue
    return "\n".join(bodies)


def _find_cova_links(body: str) -> list[str]:
    """Find all Cova Azure Blob URLs in a message body. Deduplicates."""
    if not body:
        return []
    matches = COVA_BLOB_URL_PATTERN.findall(body)
    # Some emails repeat the same link multiple times (e.g., a button + a
    # text fallback). Dedupe but preserve order.
    seen: set[str] = set()
    out = []
    for url in matches:
        # Clean trailing HTML entities or punctuation that might have been captured
        url = url.rstrip(".,;)")
        # Common HTML entities to decode
        url = url.replace("&amp;", "&")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _extract_filename_from_url(url: str) -> str:
    """Pull the filename out of an Azure Blob URL.
    Example: .../191809/Inventory%20On%20Hand%20by%20Product%20-%2020260511-233125998.xlsx?sv=...
    Returns: 'Inventory On Hand by Product - 20260511-233125998.xlsx'"""
    parsed = urlparse(url)
    # Path is /tenant_id/filename.xlsx — we want the last segment, URL-decoded
    segments = [s for s in parsed.path.split("/") if s]
    if not segments:
        return "cova-download.bin"
    return unquote(segments[-1])


def _download_link(url: str) -> tuple[bytes, str]:
    """Download a file from a Cova URL. Returns (content, filename).
    Raises an exception on failure.

    Cova sometimes sends URLs with literal spaces in the path (technically
    invalid HTML but tolerated by browsers). We percent-encode the path
    before fetching, since the requests library won't tolerate raw spaces."""
    parsed = urlparse(url)
    # Encode path portion. Preserve characters that are already-encoded (%XX)
    # and other URL-safe special chars.
    safe_chars = "/!$&'()*+,;=:@%-._~"
    fixed_path = quote(parsed.path, safe=safe_chars)
    fixed_url = urlunparse((
        parsed.scheme, parsed.netloc, fixed_path,
        parsed.params, parsed.query, parsed.fragment,
    ))
    response = requests.get(fixed_url, timeout=DOWNLOAD_TIMEOUT_S, stream=True)
    response.raise_for_status()
    content = response.content
    filename = _extract_filename_from_url(fixed_url)
    return content, filename


def _save_download(content: bytes, original_filename: str) -> Path:
    """Save downloaded content to imports/ with a timestamp prefix to
    avoid collisions if Cova reuses filenames."""
    IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    # Sanitize: strip any path traversal, keep just the filename
    safe = Path(original_filename).name
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_name = f"{ts}_{safe}"
    out_path = IMPORTS_DIR / out_name
    with open(out_path, "wb") as f:
        f.write(content)
    return out_path


def poll_account(conn: sqlite3.Connection, account_id: int,
                 max_messages: int = 100) -> dict:
    """Poll a single email account for Cova report emails.

    Strategy:
      - Connect via IMAP-SSL
      - For each UID we haven't seen before:
          - If sender is Cova → extract link → download → save → log 'imported'
          - Otherwise → log 'skipped' (so we don't re-check on next poll)
      - Errors per-message logged with action='error' but don't abort the batch

    Returns a summary dict with counts.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT label, host, port, username, password_enc, folder, is_active
        FROM email_scraper_accounts WHERE id = ?
    """, (account_id,))
    row = cur.fetchone()
    if not row:
        return {"ok": False, "error": f"Account {account_id} not found"}
    label, host, port, username, password_enc, folder, is_active = row
    if not is_active:
        return {"ok": False, "error": f"Account '{label}' is disabled"}

    password = decrypt_secret(password_enc)
    if not password:
        return {"ok": False, "error": "Could not decrypt password — SECRET_KEY may have changed"}

    summary = {
        "ok": True, "account_id": account_id, "account_label": label,
        "polled_at": datetime.utcnow().isoformat(),
        "messages_checked": 0, "imported": 0, "skipped": 0, "errors": 0,
        "files_saved": [],
    }

    try:
        imap = imaplib.IMAP4_SSL(host, port)
        try:
            imap.login(username, password)
            imap.select(folder)

            typ, data = imap.uid("search", None, "ALL")
            if typ != "OK" or not data or not data[0]:
                return summary
            uids = data[0].split()
            # Newest first; cap to avoid runaway batches
            uids = list(reversed(uids))[:max_messages]

            for uid_bytes in uids:
                uid = uid_bytes.decode("ascii")

                # Skip if already processed
                cur.execute("""
                    SELECT 1 FROM email_scraper_log
                    WHERE account_id = ? AND message_uid = ?
                """, (account_id, uid))
                if cur.fetchone():
                    continue

                summary["messages_checked"] += 1

                # Fetch the message
                typ, fetch_data = imap.uid("fetch", uid, "(RFC822)")
                if typ != "OK" or not fetch_data:
                    _log_message(conn, account_id, uid, None, None, None, 0,
                                 "error", "Could not fetch message from IMAP")
                    summary["errors"] += 1
                    continue

                raw_msg = fetch_data[0][1]
                msg = email.message_from_bytes(raw_msg)
                sender = _decode_str(msg.get("From"))
                subject = _decode_str(msg.get("Subject"))
                received_raw = msg.get("Date")

                # Filter to Cova emails only
                if not _is_cova_email(sender):
                    _log_message(conn, account_id, uid, received_raw, sender, subject,
                                 0, "skipped", "Not from Cova")
                    summary["skipped"] += 1
                    continue

                # Find Cova download links in the body
                body = _extract_message_body(msg)
                links = _find_cova_links(body)

                if not links:
                    _log_message(conn, account_id, uid, received_raw, sender, subject,
                                 0, "skipped", "No Cova download links found in body")
                    summary["skipped"] += 1
                    continue

                # Download each link (usually 1 per email)
                downloaded = []
                errors_this_msg = []
                for link in links:
                    try:
                        content, filename = _download_link(link)
                        out_path = _save_download(content, filename)
                        downloaded.append(out_path.name)
                        summary["files_saved"].append(out_path.name)
                    except requests.HTTPError as e:
                        status = e.response.status_code if e.response is not None else "?"
                        errors_this_msg.append(f"HTTP {status}")
                    except requests.RequestException as e:
                        errors_this_msg.append(f"Network: {type(e).__name__}")
                    except Exception as e:
                        errors_this_msg.append(f"{type(e).__name__}: {e}")

                if downloaded and not errors_this_msg:
                    _log_message(conn, account_id, uid, received_raw, sender, subject,
                                 len(downloaded), "imported", "; ".join(downloaded))
                    summary["imported"] += 1
                elif downloaded and errors_this_msg:
                    # Partial success — count as imported but flag the issue
                    _log_message(conn, account_id, uid, received_raw, sender, subject,
                                 len(downloaded), "imported",
                                 f"Saved: {'; '.join(downloaded)}; Failed: {'; '.join(errors_this_msg)}")
                    summary["imported"] += 1
                    summary["errors"] += len(errors_this_msg)
                else:
                    _log_message(conn, account_id, uid, received_raw, sender, subject,
                                 0, "error", "; ".join(errors_this_msg) or "Unknown error")
                    summary["errors"] += 1

            imap.close()
        finally:
            try:
                imap.logout()
            except Exception:
                pass

        # Update last_polled_at + clear last_error on successful poll
        cur.execute("""
            UPDATE email_scraper_accounts
            SET last_polled_at = CURRENT_TIMESTAMP, last_error = NULL
            WHERE id = ?
        """, (account_id,))
        conn.commit()

    except imaplib.IMAP4.error as e:
        cur.execute("UPDATE email_scraper_accounts SET last_error = ? WHERE id = ?",
                    (f"IMAP error: {e}", account_id))
        conn.commit()
        summary["ok"] = False
        summary["error"] = f"IMAP error: {e}"
    except Exception as e:
        cur.execute("UPDATE email_scraper_accounts SET last_error = ? WHERE id = ?",
                    (f"Error: {e}", account_id))
        conn.commit()
        summary["ok"] = False
        summary["error"] = f"{type(e).__name__}: {e}"

    return summary


def _log_message(conn: sqlite3.Connection, account_id: int, uid: str,
                 received: str | None, sender: str | None, subject: str | None,
                 attachment_count: int, action: str, detail: str) -> None:
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO email_scraper_log
            (account_id, message_uid, received_at, sender, subject,
             attachment_count, action, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (account_id, uid, received, sender, subject, attachment_count, action, detail))
    conn.commit()


def poll_all_active_accounts(conn: sqlite3.Connection) -> list[dict]:
    """Poll every active account. Used by the background scheduler."""
    cur = conn.cursor()
    cur.execute("SELECT id FROM email_scraper_accounts WHERE is_active = 1")
    account_ids = [r[0] for r in cur.fetchall()]
    return [poll_account(conn, aid) for aid in account_ids]

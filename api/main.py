"""
SB Insights — local API backend.

Endpoints:
  GET /                        -> the dashboard HTML
  GET /api/health              -> liveness check
  GET /api/locations           -> store list
  GET /api/kpis                -> overview headline numbers
  GET /api/inventory-summary   -> total stock value, SKU counts
  GET /api/store-performance   -> WoW + MTD-vs-PY-MTD per store
  GET /api/sales-trend         -> daily sales by category
  GET /api/reorder             -> reorder recommendations
  GET /api/stockouts           -> active stockouts with 7d/14d/30d velocities
  GET /api/dead-stock          -> zero-sales stock
  GET /api/overstock           -> >50d supply SKUs
  GET /api/transfers           -> transfer recs (single-store aware)
  GET /api/inventory           -> full on-hand list
"""

from __future__ import annotations

import os
import math
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Body, UploadFile, File, Form, Cookie, Request, Response as FastAPIResponse, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, Response, StreamingResponse, RedirectResponse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jobs.reorder_engine import (
    compute_all_reorders, compute_mix_multipliers,
    CEILING_DAYS_DEFAULT, MIN_VELOCITY_DEFAULT,
    _load_settings as load_engine_settings,
)
from jobs.auth import (
    hash_password, verify_password,
    create_session, get_session_user, delete_session, delete_user_sessions,
    prune_expired_sessions,
    encrypt_secret, decrypt_secret,
    SESSION_COOKIE_NAME, SESSION_LIFETIME_DAYS,
)
from api.excel_export import build_workbook


def _load_dotenv():
    """Minimal .env loader (no dependency): KEY=VALUE lines, '#' comments.
    Real environment variables take precedence (setdefault). Runs before any
    config is read so SECRET_KEY/TERROIR_DB/TZ etc. are available."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv()

DB_PATH = os.environ.get("TERROIR_DB", "terroir.db")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(title="SB Insights", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    # Allow same-origin (browser visiting the dashboard) + localhost in dev.
    # Production lives on app.sbinsights.co — keep .co root + www in case
    # we later host a marketing landing on the apex.
    allow_origins=[
        "http://127.0.0.1:8000", "http://localhost:8000",
        "https://app.sbinsights.co",
        "https://sbinsights.co", "https://www.sbinsights.co",
    ],
    allow_credentials=True,  # required to send cookies
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["*"],
)


# ============================================================================
# Tiny in-memory TTL cache for Overview endpoints
# ============================================================================
# Overview tab fires several heavy queries on every page load — reorder engine
# (kpis), store-performance, inventory-summary. These results change at most
# every few minutes (after a sales/inventory import). A 60s TTL means at worst
# managers see 60s-stale numbers, but repeat loads inside that window are
# essentially free instead of 5-10s.
#
# Cache is per-process — with our single uvicorn worker that's the whole app.
# Cleared on any successful data import via reset_overview_cache().

import time as _time
from threading import Lock as _Lock

_overview_cache: dict[str, tuple[float, object]] = {}
_overview_cache_lock = _Lock()
_OVERVIEW_TTL_SECONDS = 60


def overview_cache_get(key: str):
    with _overview_cache_lock:
        item = _overview_cache.get(key)
        if item is None:
            return None
        ts, val = item
        if _time.time() - ts > _OVERVIEW_TTL_SECONDS:
            _overview_cache.pop(key, None)
            return None
        return val


def overview_cache_set(key: str, val) -> None:
    with _overview_cache_lock:
        _overview_cache[key] = (_time.time(), val)


def reset_overview_cache() -> None:
    """Call after any data import to invalidate stale Overview numbers."""
    with _overview_cache_lock:
        _overview_cache.clear()


# ============================================================================
# Background backup scheduler
# ============================================================================
# Runs a daily DB backup in a background thread. Backup happens once per
# calendar day, regardless of when the server starts. If the server runs
# continuously, backup fires at ~3am local. If the server is restarted,
# we still do at most one backup per day.
#
# Honest scope notes:
#   - Only runs while the server is alive. If you don't run the server for
#     3 days, backup is also 3 days old. The "Backup now" button in the
#     System Health UI is the escape valve.
#   - Errors are logged but never raised — backup failures shouldn't crash
#     the API. Check System Health → recent backups to see if it's working.

import threading
import time as _time
from datetime import datetime as _dt

_backup_thread_started = False
_last_backup_date: str | None = None

# Background schedulers (backup, email scraper, OCS connector, successor map)
# start per-process via startup hooks. With multiple web workers that means N
# nightly backups, N OCS pulls (hammering OCS + duplicate imports), etc. Gate
# them to a single instance via an exclusive lock file so exactly one process
# runs them — regardless of worker count. Set TERROIR_NO_SCHEDULERS=1 on web
# workers when running a dedicated scheduler process.
_scheduler_lock_fh = None
_run_schedulers_cached: bool | None = None


def _should_run_schedulers() -> bool:
    """True only in the one process that should run background schedulers.
    Acquires a process-lifetime exclusive lock; other workers get False. On
    non-POSIX (Windows dev / single process) there's no contention, so True."""
    global _run_schedulers_cached, _scheduler_lock_fh
    if _run_schedulers_cached is not None:
        return _run_schedulers_cached
    if os.environ.get("TERROIR_NO_SCHEDULERS") == "1":
        _run_schedulers_cached = False
        return False
    try:
        import fcntl  # POSIX only
    except Exception:
        _run_schedulers_cached = True   # Windows/dev: single process
        return True
    try:
        lock_path = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)) or ".",
                                 ".terroir-schedulers.lock")
        fh = open(lock_path, "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _scheduler_lock_fh = fh  # held for the process lifetime
        _run_schedulers_cached = True
    except OSError:
        _run_schedulers_cached = False  # another worker holds it
    return _run_schedulers_cached


def _backup_loop():
    """Runs in a daemon thread, sleeps until 3am, runs backup, repeats."""
    global _last_backup_date
    from jobs.backup import create_backup, prune_backups
    while True:
        try:
            now = _dt.now()
            today_iso = now.date().isoformat()
            # Trigger if we haven't backed up today AND it's past 3am
            # (or the server just started and we're past 3am)
            if _last_backup_date != today_iso and now.hour >= 3:
                try:
                    result = create_backup()
                    _last_backup_date = today_iso
                    print(f"[backup] Created: {result['path']} ({result['size_bytes']} bytes)")
                    prune_result = prune_backups()
                    if prune_result["pruned"]:
                        print(f"[backup] Pruned {prune_result['pruned']} old backups")
                except Exception as e:
                    print(f"[backup] FAILED: {e}")
            # Sleep for an hour, then check again
            _time.sleep(3600)
        except Exception as e:
            # Catch-all so the thread never dies silently
            print(f"[backup] Loop error: {e}")
            _time.sleep(3600)


@app.on_event("startup")
def _start_backup_thread():
    """Launch the background backup thread on first request."""
    global _backup_thread_started
    if _backup_thread_started:
        return
    if os.environ.get("TERROIR_DISABLE_BACKUPS") == "1":
        print("[backup] Disabled via TERROIR_DISABLE_BACKUPS")
        return
    if not _should_run_schedulers():
        print("[backup] Skipped — another worker owns the schedulers")
        return
    t = threading.Thread(target=_backup_loop, daemon=True, name="backup-scheduler")
    t.start()
    _backup_thread_started = True
    print("[backup] Scheduler thread started (nightly @ 3am)")


_scraper_thread_started = False


def _scraper_loop():
    """Daemon thread: poll active email accounts for new Cova exports, then
    import the newly-downloaded files. Polling alone only saves files to
    imports/; this loop also loads them so the DB stays current without a
    server restart. Imports ONLY the files just downloaded (not the whole
    imports/ backlog) to keep each cycle cheap."""
    from jobs.email_scraper import poll_all_active_accounts
    from jobs.import_cova_exports import run_import
    from pathlib import Path as _Path
    while True:
        interval_s = 900
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("PRAGMA busy_timeout = 10000")
                row = conn.execute(
                    "SELECT MIN(poll_interval_min) FROM email_scraper_accounts WHERE is_active = 1"
                ).fetchone()
                if not row or row[0] is None:
                    _time.sleep(900)
                    continue  # no active accounts — idle and recheck later
                interval_s = max(300, int(row[0]) * 60)  # honor config, floor at 5 min
                results = poll_all_active_accounts(conn)
            imports_dir = _Path("imports")
            processed = imports_dir / "processed"
            processed.mkdir(parents=True, exist_ok=True)
            saved = [f for r in results for f in (r.get("files_saved") or [])]
            for name in saved:
                fp = imports_dir / name
                if not fp.exists():
                    continue
                try:
                    run_import(fp, DB_PATH)
                    dest = processed / fp.name
                    i = 1
                    while dest.exists():
                        dest = processed / f"{fp.stem}_{i}{fp.suffix}"
                        i += 1
                    fp.rename(dest)
                except Exception as e:
                    print(f"[scraper] import failed for {name}: {e}")
            if saved:
                print(f"[scraper] polled + imported {len(saved)} new file(s)")
        except Exception as e:
            print(f"[scraper] Loop error: {e}")
        _time.sleep(interval_s)


@app.on_event("startup")
def _start_scraper_thread():
    """Launch the auto-poll+import thread (opt out via TERROIR_DISABLE_SCRAPER=1)."""
    global _scraper_thread_started
    if _scraper_thread_started:
        return
    if os.environ.get("TERROIR_DISABLE_SCRAPER") == "1":
        print("[scraper] Disabled via TERROIR_DISABLE_SCRAPER")
        return
    if not _should_run_schedulers():
        print("[scraper] Skipped — another worker owns the schedulers")
        return
    t = threading.Thread(target=_scraper_loop, daemon=True, name="email-scraper")
    t.start()
    _scraper_thread_started = True
    print("[scraper] Scheduler thread started (auto-poll + import)")


# ============================================================================
# OCS connector scheduler — nightly auto-pull of catalogue + per-store Order Fill
# ============================================================================
# Runs sync_ocs once each evening. OCS order forms appear ~7pm the night before
# a store's order day and vanish after that day's deadline, so an evening run
# catches whichever stores currently have a live form (the rest are skipped, no
# clobber). Gated by ocs_account.is_active (sync_ocs(force=False) self-skips when
# inactive). Mirrors the backup thread.
#
# Timezone note: uses the host's LOCAL clock (like _backup_loop). The on-prem box
# is in Eastern, so "after 8pm local" == after 8pm ET, DST-correct via the OS. On
# a UTC cloud host, switch this to zoneinfo("America/Toronto").
_ocs_thread_started = False
_ocs_last_run_date: str | None = None
OCS_RUN_AFTER_HOUR = 20  # 8pm local


def _ocs_connector_loop():
    global _ocs_last_run_date
    from jobs.ocs_connector import sync_ocs
    while True:
        try:
            now = _dt.now()
            today = now.date().isoformat()
            if _ocs_last_run_date != today and now.hour >= OCS_RUN_AFTER_HOUR:
                try:
                    with sqlite3.connect(DB_PATH) as conn:
                        conn.execute("PRAGMA busy_timeout = 30000")
                        res = sync_ocs(conn, DB_PATH, force=False)
                    _ocs_last_run_date = today  # one attempt per day
                    if res.get("status") != "skipped":
                        print(f"[ocs] nightly sync {res.get('status')}: "
                              f"{res.get('imported') or res.get('error')}")
                except Exception as e:
                    print(f"[ocs] nightly sync failed: {e}")
            _time.sleep(1800)  # re-check every 30 min
        except Exception as e:
            print(f"[ocs] Loop error: {e}")
            _time.sleep(1800)


@app.on_event("startup")
def _start_ocs_connector_thread():
    """Launch the nightly OCS connector thread (opt out via
    TERROIR_DISABLE_OCS_CONNECTOR=1). Only actually syncs when the OCS account
    is marked active."""
    global _ocs_thread_started
    if _ocs_thread_started:
        return
    if os.environ.get("TERROIR_DISABLE_OCS_CONNECTOR") == "1":
        print("[ocs] Connector scheduler disabled via TERROIR_DISABLE_OCS_CONNECTOR")
        return
    if not _should_run_schedulers():
        print("[ocs] Scheduler skipped — another worker owns the schedulers")
        return
    t = threading.Thread(target=_ocs_connector_loop, daemon=True, name="ocs-connector")
    t.start()
    _ocs_thread_started = True
    print(f"[ocs] Connector scheduler started (nightly after {OCS_RUN_AFTER_HOUR}:00 if active)")


_successor_map_built = False


def _build_successor_map_bg():
    """Build the orphan->successor map in the background so it's ready for the
    first Reorder Report load. Until it finishes, detect_successor falls back to
    live computation (correct, just slower), so this never blocks startup."""
    try:
        from jobs.successor_detection import build_successor_map
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("PRAGMA busy_timeout = 10000")
            n = build_successor_map(conn)
        print(f"[successor] Built successor map: {n} orphans")
    except Exception as e:
        print(f"[successor] map build failed (will compute live): {e}")


@app.on_event("startup")
def _start_successor_map_build():
    """Precompute the successor map on startup (Phase C). Runs in a daemon
    thread to avoid delaying server readiness; opt out via
    TERROIR_DISABLE_SUCCESSOR_MAP=1."""
    global _successor_map_built
    if _successor_map_built:
        return
    if os.environ.get("TERROIR_DISABLE_SUCCESSOR_MAP") == "1":
        print("[successor] Map build disabled via TERROIR_DISABLE_SUCCESSOR_MAP")
        return
    if not os.path.exists(DB_PATH):
        return  # nothing to build against yet
    t = threading.Thread(target=_build_successor_map_bg, daemon=True, name="successor-map")
    t.start()
    _successor_map_built = True
    print("[successor] Map build thread started")


@app.get("/", response_class=HTMLResponse)
def dashboard_page(request: Request):
    # If users exist and you're not logged in → bounce to login
    if os.path.exists(DB_PATH):
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("PRAGMA busy_timeout = 10000")
                cur = conn.cursor()
                cur.execute("SELECT 1 FROM users WHERE is_active = 1 LIMIT 1")
                has_users = cur.fetchone() is not None
                if has_users:
                    token = request.cookies.get(SESSION_COOKIE_NAME)
                    user = get_session_user(conn, token) if token else None
                    if not user:
                        return RedirectResponse(url="/login", status_code=302)
        except sqlite3.OperationalError:
            # users table doesn't exist yet (first run); fall through to dashboard
            pass
    return FileResponse(os.path.join(STATIC_DIR, "dashboard.html"))


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


@contextmanager
def db():
    if not os.path.exists(DB_PATH):
        raise HTTPException(
            status_code=503,
            detail=f"Database not found at {DB_PATH}. Run the import command first.",
        )
    conn = sqlite3.connect(DB_PATH)
    # SQLite is single-writer. During the nightly backup window or any other
    # concurrent write, naive requests fail with "database is locked". Set a
    # busy_timeout so SQLite WAITS for the lock instead of immediately erroring.
    # 10 seconds is generous — covers the worst-case backup window for our DB
    # size while still failing fast enough that hung requests don't pile up.
    # Real fix is Postgres at cloud launch; this is the right SQLite mitigation.
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        yield conn
    finally:
        conn.close()


# ============================================================================
# Auth dependencies
# ============================================================================
# require_user: enforces "you must be logged in" for protected endpoints.
# require_admin: enforces "you must be an admin" for sensitive endpoints.
#
# Day-1 behavior: if no users exist yet (fresh system), all endpoints work
# without auth so you can do initial setup. Once at least one admin exists,
# auth becomes mandatory. This avoids a chicken-and-egg lockout.

def _has_any_users(conn) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM users WHERE is_active = 1 LIMIT 1")
    return cur.fetchone() is not None


def get_current_user(request: Request) -> dict | None:
    """Look up the logged-in user from the session cookie. Returns None if
    not logged in or session expired. Does NOT raise — caller decides."""
    if request is None:  # direct Python call (tests/exports) — no session
        return None
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    with db() as conn:
        return get_session_user(conn, token)


def require_user(request: Request) -> dict:
    """FastAPI dependency. Raises 401 if not logged in. Use on user-only routes."""
    with db() as conn:
        if not _has_any_users(conn):
            # Bootstrap mode — no auth required yet
            return {"id": 0, "email": "bootstrap", "name": "Bootstrap", "role": "admin",
                    "must_change_password": False}
        user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(request: Request) -> dict:
    """FastAPI dependency. Raises 403 if not an admin."""
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


# ============================================================================
# Auth endpoints
# ============================================================================

@app.post("/api/auth/login")
def login(payload: dict = Body(...), request: Request = None) -> dict:
    """Email + password login. Sets a session cookie on success.

    Payload:
      email (required)
      password (required)
    """
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password required")

    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, password_hash, role, is_active, must_change_password, name
            FROM users WHERE LOWER(email) = ?
        """, (email,))
        row = cur.fetchone()
        # Same error for missing user vs wrong password — don't leak which is wrong
        if not row:
            # Slight timing protection: still hash a dummy password to even out
            # response time. Not perfect but discourages timing attacks.
            verify_password(password, "$2b$12$abcdefghijklmnopqrstuv")
            raise HTTPException(status_code=401, detail="Invalid email or password")
        user_id, password_hash, role, is_active, must_change, name = row
        if not is_active:
            raise HTTPException(status_code=401, detail="Account disabled")
        if not verify_password(password, password_hash):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        # Create session
        ua = request.headers.get("user-agent") if request else None
        token, expires_at = create_session(conn, user_id, user_agent=ua)
        # Update last_login_at
        cur.execute("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?", (user_id,))
        conn.commit()

    response = FastAPIResponse(
        content=f'{{"ok": true, "must_change_password": {str(bool(must_change)).lower()}, "email": "{email}", "name": "{name or ""}", "role": "{role}"}}',
        media_type="application/json",
    )
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_LIFETIME_DAYS * 86400,
        httponly=True,         # JS can't read it (XSS protection)
        secure=False,          # CHANGE TO TRUE FOR PRODUCTION (HTTPS)
        samesite="lax",
    )
    return response


@app.post("/api/auth/logout")
def logout(request: Request) -> dict:
    """End the current session."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        with db() as conn:
            delete_session(conn, token)
    response = FastAPIResponse(content='{"ok": true}', media_type="application/json")
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/api/auth/me")
def get_me(request: Request) -> dict:
    """Return the current user, or {logged_in: false} if not logged in.
    Used by the dashboard to know who's logged in and what role they have."""
    with db() as conn:
        bootstrap = not _has_any_users(conn)
    if bootstrap:
        return {
            "logged_in": True, "bootstrap_mode": True,
            "email": "bootstrap", "name": "Bootstrap", "role": "admin",
            "must_change_password": False,
        }
    user = get_current_user(request)
    if not user:
        return {"logged_in": False}
    return {"logged_in": True, "bootstrap_mode": False, **user}


@app.post("/api/auth/change-password")
def change_password(payload: dict = Body(...), request: Request = None) -> dict:
    """Change your own password. Requires current password unless must_change
    flag is set (i.e., admin gave you a temp password)."""
    user = require_user(request)
    if user["id"] == 0:
        raise HTTPException(status_code=400, detail="Bootstrap user cannot change password — create a real admin first")

    current_password = payload.get("current_password") or ""
    new_password = payload.get("new_password") or ""
    if not new_password or len(new_password) < 12:
        raise HTTPException(status_code=400, detail="New password must be at least 12 characters")

    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT password_hash, must_change_password FROM users WHERE id = ?", (user["id"],))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        password_hash, must_change = row

        # If must_change is set, skip current-password check (admin set a temp)
        if not must_change:
            if not current_password:
                raise HTTPException(status_code=400, detail="Current password required")
            if not verify_password(current_password, password_hash):
                raise HTTPException(status_code=401, detail="Current password is incorrect")

        new_hash = hash_password(new_password)
        cur.execute("""
            UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?
        """, (new_hash, user["id"]))
        # Invalidate ALL their other sessions (force re-login elsewhere)
        cur.execute("DELETE FROM user_sessions WHERE user_id = ? AND session_token != ?",
                    (user["id"], request.cookies.get(SESSION_COOKIE_NAME, "")))
        conn.commit()

    return {"ok": True}


# ============================================================================
# User management endpoints — admin-only
# ============================================================================

@app.get("/api/users")
def list_users(request: Request) -> dict:
    """List all users (admin only)."""
    user = require_admin(request)
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, email, name, role, is_active, must_change_password,
                   created_at, created_by, last_login_at
            FROM users ORDER BY created_at DESC
        """)
        cols = [d[0] for d in cur.description]
        items = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {"count": len(items), "items": items}


@app.post("/api/users")
def create_user(payload: dict = Body(...), request: Request = None) -> dict:
    """Create a new user (admin only). Sets must_change_password=1 so they
    have to change the temp password on first login."""
    actor = require_admin(request)
    email = (payload.get("email") or "").strip().lower()
    name = (payload.get("name") or "").strip() or None
    temp_password = payload.get("temp_password") or ""
    role = (payload.get("role") or "regular").strip()

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required")
    if len(temp_password) < 12:
        raise HTTPException(status_code=400, detail="Temp password must be at least 12 characters")
    if role not in ("admin", "regular"):
        raise HTTPException(status_code=400, detail="Role must be admin or regular")

    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE LOWER(email) = ?", (email,))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="User with that email already exists")
        password_hash = hash_password(temp_password)
        cur.execute("""
            INSERT INTO users (email, name, password_hash, role, must_change_password, created_by)
            VALUES (?, ?, ?, ?, 1, ?)
        """, (email, name, password_hash, role, actor.get("email")))
        conn.commit()
        new_id = cur.lastrowid

    return {"ok": True, "id": new_id, "email": email,
            "instructions": "Share the email + temp password with the user. They'll be forced to change the password on first login."}


@app.post("/api/users/{user_id}/reset-password")
def reset_user_password(user_id: int, payload: dict = Body(...), request: Request = None) -> dict:
    """Admin resets a user's password to a temp value. Forces change on next login.
    Also invalidates all their existing sessions."""
    require_admin(request)
    temp_password = payload.get("temp_password") or ""
    if len(temp_password) < 12:
        raise HTTPException(status_code=400, detail="Temp password must be at least 12 characters")

    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE id = ?", (user_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="User not found")
        new_hash = hash_password(temp_password)
        cur.execute("""
            UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?
        """, (new_hash, user_id))
        # Kill all their existing sessions
        delete_user_sessions(conn, user_id)
        conn.commit()
    return {"ok": True, "instructions": "Share the new temp password with the user."}


@app.post("/api/users/{user_id}/deactivate")
def deactivate_user(user_id: int, request: Request = None) -> dict:
    """Deactivate a user (admin only). Kills their sessions and disables login."""
    actor = require_admin(request)
    if actor["id"] == user_id:
        raise HTTPException(status_code=400, detail="Can't deactivate yourself")
    with db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="User not found")
        delete_user_sessions(conn, user_id)
        conn.commit()
    return {"ok": True}


@app.post("/api/users/{user_id}/reactivate")
def reactivate_user(user_id: int, request: Request = None) -> dict:
    """Re-enable a previously-deactivated user (admin only)."""
    require_admin(request)
    with db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET is_active = 1 WHERE id = ?", (user_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="User not found")
        conn.commit()
    return {"ok": True}


@app.post("/api/users/{user_id}/role")
def change_user_role(user_id: int, payload: dict = Body(...), request: Request = None) -> dict:
    """Change a user's role (admin only). 'regular' users keep full dashboard
    access and can VIEW System Settings, but can't change settings or reach
    the admin pages (Users, Email Scraper, OCS Connector).

    Lockout guard: refuses to demote the last active admin — there must
    always be at least one account able to administer the system."""
    require_admin(request)
    role = (payload.get("role") or "").strip()
    if role not in ("admin", "regular"):
        raise HTTPException(status_code=400, detail="Role must be admin or regular")
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT role, is_active FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        current_role, is_active = row
        if current_role == role:
            return {"ok": True, "unchanged": True, "role": role}
        if current_role == "admin" and role != "admin":
            cur.execute("""
                SELECT COUNT(*) FROM users
                WHERE role = 'admin' AND is_active = 1 AND id != ?
            """, (user_id,))
            if (cur.fetchone()[0] or 0) == 0:
                raise HTTPException(status_code=400,
                                    detail="Can't demote the last active admin — promote someone else first")
        cur.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        conn.commit()
    # No session invalidation needed: role is read live from the users table
    # on every request (session lookup joins users), so it applies immediately.
    return {"ok": True, "role": role}


def get_latest_sale_date(conn) -> date:
    cur = conn.cursor()
    cur.execute("SELECT MAX(sale_date) FROM sales_daily")
    row = cur.fetchone()
    if row and row[0]:
        return date.fromisoformat(row[0])
    return date.today()


def get_price_map(conn) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT sku, location_id, regular_price FROM prices")
    return {(r[0], r[1]): r[2] for r in cur.fetchall()}


def filter_by_top_level(recs, top_level: str | None, *, default_exclude_other: bool = True):
    """
    Filter reorder recs by top-level classification.

    If `top_level` is set ('Cannabis', 'Accessories', 'Other', 'All'), honor that.
    If not specified and default_exclude_other=True, exclude 'Other' (fees, donations, gift cards)
    since those aren't real products and pollute money views.
    """
    if top_level and top_level != "All":
        return [r for r in recs if (r.top_level or "") == top_level]
    if default_exclude_other:
        return [r for r in recs if (r.top_level or "") != "Other"]
    return recs


# Slow/dead stock recency cutoffs (days since last sold). Slow = SLOW..DEAD-1,
# Dead = DEAD+. Kept here so the API and any future report share one definition.
SLOW_STOCK_DAYS = 60
DEAD_STOCK_DAYS = 90


def compute_reorder_kpis(conn, *, store: str | None, top_level: str, as_of: date) -> dict:
    """Summary KPIs for the Reorder Report header, scoped to the same store +
    top_level universe as the recommendations.

    - days_on_hand: on-hand value (at cost) / daily burn, where daily burn is
      trailing-30-day COGS / 30 (matches the engine's velocity window).
    - weekly_burn_cost: daily burn x 7.
    - slow/dead stock: on-hand value whose Cova 'days since last sold' is in
      [SLOW_STOCK_DAYS, DEAD_STOCK_DAYS) (slow) or >= DEAD_STOCK_DAYS (dead).

    Cost basis mirrors the report: OCS wholesale unit price, else retail x 0.60.
    """
    cur = conn.cursor()
    # top_level scoping: a specific level filters to it; 'All' (or None) keeps
    # everything except 'Other' (fees/gift cards), matching filter_by_top_level.
    tl_clause, tl_params = "", []
    if top_level and top_level != "All":
        tl_clause = "AND p.top_level = ?"
        tl_params = [top_level]
    else:
        tl_clause = "AND COALESCE(p.top_level, '') != 'Other'"
    loc_clause = "AND x.location_id = ?" if store else ""
    loc_param = [store] if store else []

    # Cost basis: actual Cova landed cost (avg_unit_cost) first — what the
    # inventory really cost (reconciles to Cova's "In Stock Cost") — then OCS
    # wholesale, then 60%-of-retail as a last resort.
    #   COST_OH:   for on-hand rows (current_inventory x → x.avg_unit_cost)
    #   COST_COGS: for sold rows (no cost on sales_daily; join current_inventory)
    COST_OH = "COALESCE(x.avg_unit_cost, oc.unit_price, pr.regular_price * 0.60, 0)"
    COST_COGS = "COALESCE(ci.avg_unit_cost, oc.unit_price, pr.regular_price * 0.60, 0)"

    # On-hand value (at cost) + units, from the materialized current_inventory.
    cur.execute(f"""
        SELECT COALESCE(SUM(x.on_hand * {COST_OH}), 0), COALESCE(SUM(x.on_hand), 0)
        FROM current_inventory x
        JOIN products p ON p.sku = x.sku
        LEFT JOIN prices pr ON pr.sku = x.sku AND pr.location_id = x.location_id
        LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = p.ocs_variant_number
        WHERE 1=1 {tl_clause} {loc_clause}
    """, tl_params + loc_param)
    on_hand_value, on_hand_units = cur.fetchone()

    # Trailing-30-day COGS (cost of product sold). Value sold units at the SKU's
    # current landed cost where known (join current_inventory).
    win_start = (as_of - timedelta(days=29)).isoformat()
    cur.execute(f"""
        SELECT COALESCE(SUM(x.units_sold * {COST_COGS}), 0)
        FROM sales_daily x
        JOIN products p ON p.sku = x.sku
        LEFT JOIN current_inventory ci ON ci.sku = x.sku AND ci.location_id = x.location_id
        LEFT JOIN prices pr ON pr.sku = x.sku AND pr.location_id = x.location_id
        LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = p.ocs_variant_number
        WHERE x.sale_date >= ? AND x.sale_date <= ? {tl_clause} {loc_clause}
    """, [win_start, as_of.isoformat()] + tl_params + loc_param)
    cogs_30d = cur.fetchone()[0] or 0.0

    # Slow / dead stock by recency of last sale (on-hand only).
    cur.execute(f"""
        SELECT
            COALESCE(SUM(CASE WHEN x.days_since_last_sold >= ? AND x.days_since_last_sold < ?
                              THEN x.on_hand * {COST_OH} ELSE 0 END), 0) AS slow_val,
            COALESCE(SUM(CASE WHEN x.days_since_last_sold >= ?
                              THEN x.on_hand * {COST_OH} ELSE 0 END), 0) AS dead_val,
            COALESCE(SUM(CASE WHEN x.days_since_last_sold >= ? AND x.days_since_last_sold < ?
                              THEN 1 ELSE 0 END), 0) AS slow_skus,
            COALESCE(SUM(CASE WHEN x.days_since_last_sold >= ?
                              THEN 1 ELSE 0 END), 0) AS dead_skus
        FROM current_inventory x
        JOIN products p ON p.sku = x.sku
        LEFT JOIN prices pr ON pr.sku = x.sku AND pr.location_id = x.location_id
        LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = p.ocs_variant_number
        WHERE x.on_hand > 0 {tl_clause} {loc_clause}
    """, [SLOW_STOCK_DAYS, DEAD_STOCK_DAYS, DEAD_STOCK_DAYS,
          SLOW_STOCK_DAYS, DEAD_STOCK_DAYS, DEAD_STOCK_DAYS] + tl_params + loc_param)
    slow_val, dead_val, slow_skus, dead_skus = cur.fetchone()

    # As-of date of the on-hand data (latest snapshot feeding current_inventory).
    cur.execute(f"""
        SELECT MAX(x.as_of) FROM current_inventory x
        JOIN products p ON p.sku = x.sku
        WHERE 1=1 {tl_clause} {loc_clause}
    """, tl_params + loc_param)
    row = cur.fetchone()
    inventory_as_of = (row[0][:10] if row and row[0] else None)

    daily_burn = (cogs_30d / 30.0) if cogs_30d else 0.0
    return {
        "inventory_as_of": inventory_as_of,
        "on_hand_value": round(float(on_hand_value), 2),
        "on_hand_units": int(on_hand_units or 0),
        "cogs_30d": round(float(cogs_30d), 2),
        "daily_burn_cost": round(daily_burn, 2),
        "weekly_burn_cost": round(daily_burn * 7, 2),
        "days_on_hand": round(float(on_hand_value) / daily_burn, 1) if daily_burn > 0 else None,
        "slow_stock_value": round(float(slow_val), 2),
        "slow_stock_skus": int(slow_skus or 0),
        "dead_stock_value": round(float(dead_val), 2),
        "dead_stock_skus": int(dead_skus or 0),
        "slow_dead_total_value": round(float(slow_val) + float(dead_val), 2),
        "slow_days": SLOW_STOCK_DAYS,
        "dead_days": DEAD_STOCK_DAYS,
    }


# ---------------------------------------------------------------------------
# /api/locations
# ---------------------------------------------------------------------------

import re

def _short_store_name(cova_name: str | None) -> str:
    """
    Cova names stores like 'North (Livingstone)'. Extract the part in
    parentheses — that's the street name, which is what the user actually
    thinks of each store by. Falls back to the first word if no parens.
    """
    if not cova_name:
        return ""
    m = re.search(r'\(([^)]+)\)', cova_name)
    if m:
        return m.group(1).strip()
    return cova_name.split(' ')[0] or cova_name


# ---------------------------------------------------------------------------
# /api/locations
# ---------------------------------------------------------------------------

@app.get("/api/locations")
def list_locations() -> list[dict]:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, name, city, region FROM locations
            WHERE is_active = 1 ORDER BY id
        """)
        return [
            {
                "id": r[0],
                "name": r[1],
                "short_name": _short_store_name(r[1]),
                "city": r[2],
                "region": r[3],
            }
            for r in cur.fetchall()
        ]


# ---------------------------------------------------------------------------
# /api/reorder
# ---------------------------------------------------------------------------

@app.get("/api/reorder/diagnose")
def diagnose_reorder_sku(sku: str, location_id: str) -> dict:
    """Diagnostic — explain why a SKU is or isn't on the reorder report.

    Returns a structured trace of every filter the engine evaluates, in order.
    The FIRST failing check is what caused exclusion. If all checks pass, the
    SKU should be in the report.

    Query params:
      sku — the Cova SKU
      location_id — store id (e.g. 'S3' for Bradford)
    """
    from jobs.reorder_diagnose import diagnose_sku
    with db() as conn:
        as_of = get_latest_sale_date(conn)
        result = diagnose_sku(conn, sku, location_id, as_of=as_of)
    return result


@app.get("/api/reorder")
def get_reorder(
    store: str | None = None,
    top_level: str | None = None,
    urgency: str | None = None,              # stockout | critical | high | medium | all
    include_ocs_out: bool = False,
    # None = use the live values from app_settings (Settings tab). Passing a
    # value here overrides settings for this request only. These MUST default
    # to None: hardcoded defaults silently overrode the Settings tab, so the
    # report ran different math than the configured (and diagnosed) values.
    ceiling_days: int | None = None,
    min_velocity: float | None = None,
    mix_aware: bool = False,                 # NEW: apply category mix multipliers
    mix_days: int = 90,                      # window for mix signal (default 90d)
) -> dict:
    # Resolve top_level default (Cannabis) before anything else
    tl = top_level if top_level is not None else "Cannabis"

    with db() as conn:
        as_of = get_latest_sale_date(conn)

        # If mix-aware, compute category multipliers first (scoped to store + top_level)
        mix_multipliers = None
        if mix_aware:
            # compute multipliers only for the top_level we'll actually show
            mix_tl = tl if tl in ("Cannabis", "Accessories") else "Cannabis"
            mix_multipliers = compute_mix_multipliers(
                conn,
                location_id=store,
                top_level=mix_tl,
                days=mix_days,
                as_of_date=as_of,
            )

        recs = compute_all_reorders(
            conn,
            location_id=store,
            as_of_date=as_of,
            ceiling_days=ceiling_days,
            min_velocity=min_velocity,
            mix_multipliers=mix_multipliers,
            apply_successors=True,  # Phase 3: resolve re-listed orphans to successors
        )
        price_map = get_price_map(conn)

        # Resolve active data revenue deals using the hierarchy:
        # Brand-Direct or LP-Direct > IRCC > Canna Collective > Seeker.
        # Pass both Cova SKUs and OCS variants — resolver handles either.
        from jobs.data_revenue_resolver import get_active_deals_for_skus
        sku_set = set()
        for r in recs:
            sku_set.add(r.sku)
            if getattr(r, "ocs_variant", None):
                sku_set.add(r.ocs_variant)
        deal_map = get_active_deals_for_skus(conn, sku_set)

        # Order Fill data — back-in-stock + Flow Through tier (visibility only)
        from jobs.import_order_fill import get_latest_order_fill_skus
        order_fill_map = get_latest_order_fill_skus(conn)

        # Sale flags — uses discount_lines to identify SKUs currently on sale
        # or with recently ended sales. Pure visibility — no math change.
        from jobs.sale_flags import compute_sale_flags
        sale_flag_map = compute_sale_flags(conn, as_of=as_of)

        # Ratings + comments — show alongside each rec so managers can see
        # peer feedback without leaving the Reorder Report.
        rating_map: dict[str, dict] = {}
        cur = conn.cursor()
        # Aggregate ratings by SKU (across all stores — ratings are
        # store-agnostic in our data model).
        try:
            cur.execute("""
                SELECT sku,
                       AVG(rating) AS avg_rating,
                       COUNT(*) AS rating_count
                FROM product_ratings
                GROUP BY sku
            """)
            for sku, avg_r, cnt in cur.fetchall():
                rating_map[sku] = {
                    "avg_rating": round(float(avg_r), 1) if avg_r is not None else None,
                    "rating_count": int(cnt or 0),
                    "comment_count": 0,
                }
        except sqlite3.OperationalError:
            pass  # table missing on fresh install

        # Comment counts per SKU
        try:
            cur.execute("""
                SELECT sku, COUNT(*) FROM product_comments GROUP BY sku
            """)
            for sku, cnt in cur.fetchall():
                if sku in rating_map:
                    rating_map[sku]["comment_count"] = int(cnt or 0)
                else:
                    rating_map[sku] = {
                        "avg_rating": None,
                        "rating_count": 0,
                        "comment_count": int(cnt or 0),
                    }
        except sqlite3.OperationalError:
            pass

        # Header KPIs (days-on-hand, weekly burn, slow/dead stock) scoped to the
        # same store + top_level universe as the recs below. Computed inside the
        # connection block; reorder totals + stockout count are added at return.
        kpis_inv = compute_reorder_kpis(conn, store=store, top_level=tl, as_of=as_of)

        # Resolve the effective engine knobs for the settings echo, so the UI
        # reports the values the engine actually ran with (settings or override).
        eng = load_engine_settings(conn)
        resolved_ceiling = (ceiling_days if ceiling_days is not None
                            else int(eng["reorder.regular_ceiling_days"]))
        resolved_min_velocity = (min_velocity if min_velocity is not None
                                 else float(eng["reorder.min_velocity"]))

    recs = filter_by_top_level(recs, tl, default_exclude_other=True)

    payload = []
    for r in recs:
        d = r.to_dict()
        if r.ocs_unit_price:
            est_cost = round(r.ocs_unit_price, 2)
        else:
            retail = price_map.get((r.sku, r.location_id), 0)
            est_cost = round(retail * 0.60, 2) if retail else None
        d["wholesale_cost_est"] = est_cost
        d["line_total"] = round((est_cost or 0) * r.reorder_qty, 2)

        # Attach active data fee — look up by Cova SKU first, then OCS variant
        deal = deal_map.get(r.sku)
        if not deal and getattr(r, "ocs_variant", None):
            deal = deal_map.get(r.ocs_variant)
        if deal:
            d["data_fee_pct"] = round(deal["percentage"], 2)
            d["data_fee_partner"] = deal["partner"]
            d["data_fee_basis"] = deal["basis"]
            d["data_fee_is_direct"] = deal["is_direct"]
        else:
            d["data_fee_pct"] = None
            d["data_fee_partner"] = None
            d["data_fee_basis"] = None
            d["data_fee_is_direct"] = False

        # Attach Order Fill metadata (visibility only — does not affect reorder math).
        # Look up via OCS variant number, which is how Order Fill keys SKUs.
        of = None
        if getattr(r, "ocs_variant", None):
            v = r.ocs_variant.lower()
            # Order Fill is per-store: prefer this store's run, fall back to a
            # legacy chain-wide (None) run so pre-migration data still resolves.
            of = order_fill_map.get((r.location_id, v)) or order_fill_map.get((None, v))
        if of:
            d["order_fill_back_in_stock"] = bool(of["back_in_stock"])
            d["order_fill_new_arrival"] = bool(of["new_arrival"])
            d["order_fill_flow_thru"] = bool(of["flow_thru"])
            d["order_fill_delivery_tier"] = of["delivery_tier"]   # 'Standard' | 'Expedited' | None
            d["order_fill_estimated_delivery"] = of["estimated_delivery_date"]
            d["order_fill_available_qty"] = of["available_quantity"]
            d["order_fill_price_change"] = of["price_change"]
            d["order_fill_price_change_pct"] = of["price_change_pct"]
        else:
            d["order_fill_back_in_stock"] = False
            d["order_fill_new_arrival"] = False
            d["order_fill_flow_thru"] = None  # None = not in latest Order Fill (vs. False)
            d["order_fill_delivery_tier"] = None
            d["order_fill_estimated_delivery"] = None
            d["order_fill_available_qty"] = None
            d["order_fill_price_change"] = None
            d["order_fill_price_change_pct"] = None

        # Sale flag — visual only, no math change. Tells the manager that
        # the velocity number they're looking at may be inflated by a current
        # or recently-ended promotion.
        sf = sale_flag_map.get((r.location_id, r.sku))
        if sf:
            d["sale_flag"] = sf["status"]  # 'active' | 'recent'
            d["sale_flag_detail"] = {
                "active_days_in_7": sf["active_days_in_7"],
                "days_in_30": sf["days_in_30"],
                "avg_discount_pct": sf["avg_discount_pct"],
                "last_promo_date": sf["last_promo_date"],
                "days_since_last_promo": sf["days_since_last_promo"],
            }
        else:
            d["sale_flag"] = None
            d["sale_flag_detail"] = None

        # Ratings + comments from manager feedback (synced from OCS Catalogue tab)
        rinfo = rating_map.get(r.sku) or {"avg_rating": None, "rating_count": 0, "comment_count": 0}
        d["avg_rating"] = rinfo["avg_rating"]
        d["rating_count"] = rinfo["rating_count"]
        d["comment_count"] = rinfo["comment_count"]

        payload.append(d)

    # Actionable = has a reorder qty > 0 AND urgency is not "ok"/"overstock"/etc.
    # With the new min-max model, recs below min_velocity or above COVERAGE_DAYS
    # already have reorder_qty=0, so this naturally filters them out.
    #
    # EXCEPTION — stockouts stay visible even at qty 0. A selling SKU at zero
    # on-hand whose demand rounds below the case threshold used to vanish from
    # the report entirely (while the Active Stockouts KPI still counted it),
    # so it could never be reordered until someone noticed by hand. Keep the
    # row, flag it needs_review, and let the manager decide.
    actionable = [p for p in payload
                  if p["urgency"] in ("stockout", "critical", "high", "medium")
                  and (p["reorder_qty"] > 0 or p["urgency"] == "stockout")]
    for p in actionable:
        p["needs_review"] = (p["urgency"] == "stockout" and p["reorder_qty"] == 0)
        if p["needs_review"]:
            p["notes"] = "Manual review — stocked out, demand below case threshold"

    # Exclude OCS-out-of-stock items unless explicitly requested. Two exemptions
    # keep genuinely-orderable items from being hidden by a stale catalogue NO:
    #   1. Successor recs — an OOS successor still appears (flagged via
    #      successor_in_stock) so the re-listed product isn't lost silently.
    #   2. Order Fill availability — the OCS Order Fill is the authoritative
    #      "what OCS will deliver this cycle" feed. A flow-through item ships from
    #      the LP, so it correctly reads NO in the warehouse-stock catalogue yet
    #      is still orderable. If it's in the latest Order Fill (flow-through,
    #      back-in-stock, or available qty > 0), keep it. Display/math unchanged.
    def _orderable_via_order_fill(p: dict) -> bool:
        return (p.get("order_fill_flow_thru") is not None     # present in latest Order Fill
                or bool(p.get("order_fill_back_in_stock"))
                or (p.get("order_fill_available_qty") or 0) > 0)

    if not include_ocs_out:
        actionable = [p for p in actionable
                      if p.get("successor_predecessor_sku") is not None
                      or p.get("ocs_stock_status") != "NO"
                      or _orderable_via_order_fill(p)]

    # Urgency filter (optional)
    if urgency and urgency != "all":
        actionable = [p for p in actionable if p["urgency"] == urgency]

    # Rebate-aware re-sort: within MEDIUM and HIGH urgency tiers, promote
    # SKUs with active rebates to the top. Stockouts/critical aren't reshuffled
    # — those are urgent regardless of rebate. The user's intent: "for low-ish
    # or mediocre velocity, drift more towards SKUs that have rebates."
    URGENCY_RANK = {"stockout": 0, "critical": 1, "high": 2, "medium": 3}
    REBATE_PROMOTE_TIERS = {"medium", "high"}
    def _sort_key(p):
        u = URGENCY_RANK.get(p.get("urgency", "medium"), 99)
        # In the rebate-promote tiers, has-rebate ranks BEFORE no-rebate
        rebate_bump = 0
        if p.get("urgency") in REBATE_PROMOTE_TIERS:
            has_rebate = bool(p.get("data_fee_pct") or p.get("data_fee_is_direct"))
            rebate_bump = 0 if has_rebate else 1
        # Zero-qty needs_review stockouts sort after ordered stockouts
        review_bump = 1 if p.get("needs_review") else 0
        return (u, review_bump, rebate_bump, -(p.get("line_total") or 0))
    actionable.sort(key=_sort_key)

    # Assemble header KPIs: inventory-derived metrics + this week's reorder
    # totals + active stockout count (selling SKUs at 0 on-hand).
    kpis = {
        **kpis_inv,
        "reorder_cost": round(sum(p["line_total"] for p in actionable), 2),
        "reorder_units": sum(p["reorder_qty"] for p in actionable),
        # Count only rows with an actual order — needs_review rows (qty 0) are
        # visible in the table but aren't "SKUs being reordered".
        "reorder_skus": sum(1 for p in actionable if p["reorder_qty"] > 0),
        "needs_review_skus": sum(1 for p in actionable if p.get("needs_review")),
        "active_stockouts": sum(1 for r in recs if r.urgency == "stockout"),
        # Cost share of the order that's just filling stockouts (genuine missed
        # demand). Used by Highlights to tell "you're over-ordering" apart from
        # "you're heavy on the wrong stock — most of this order is refilling
        # sold-out best-sellers."
        "stockout_value": round(sum(p["line_total"] or 0 for p in actionable
                                    if p["urgency"] == "stockout"), 2),
    }

    return {
        "count": len(actionable),
        "total_wholesale_cost": round(sum(p["line_total"] for p in actionable), 2),
        "total_units": sum(p["reorder_qty"] for p in actionable),
        "recommendations": actionable,
        "all_recs_count": len(payload),
        "kpis": kpis,
        "top_level": tl,
        "settings": {
            "ceiling_days": resolved_ceiling,
            "min_velocity": resolved_min_velocity,
            "include_ocs_out": include_ocs_out,
            "mix_aware": mix_aware,
            "mix_days": mix_days if mix_aware else None,
        },
        "mix_multipliers": mix_multipliers if mix_aware else None,
    }


# ---------------------------------------------------------------------------
# /api/kpis
# ---------------------------------------------------------------------------

@app.get("/api/kpis")
def get_kpis(store: str | None = None) -> dict:
    cache_key = f"kpis:{store or ''}"
    cached = overview_cache_get(cache_key)
    if cached is not None:
        return cached
    with db() as conn:
        as_of = get_latest_sale_date(conn)
        recs = compute_all_reorders(conn, location_id=store, as_of_date=as_of)
        price_map = get_price_map(conn)

    # Exclude Other (fees, donations, gift cards) from money metrics
    recs = filter_by_top_level(recs, None, default_exclude_other=True)

    total_reorder_cost = 0.0
    critical = 0
    stockouts = 0
    overstock_value = 0.0
    dead_value = 0.0

    for r in recs:
        retail = price_map.get((r.sku, r.location_id), 0)
        # Prefer real OCS unit price; fall back to 60% retail proxy
        cost = r.ocs_unit_price if r.ocs_unit_price else (retail * 0.60 if retail else 0)
        stock_value = cost * r.on_hand

        if r.urgency == "critical": critical += 1
        elif r.urgency == "stockout": stockouts += 1

        if r.urgency in ("stockout", "critical", "high", "medium"):
            total_reorder_cost += cost * r.reorder_qty

        if r.urgency == "overstock": overstock_value += stock_value
        if r.urgency == "dead_stock": dead_value += stock_value

    result = {
        "reorder_value_pending": round(total_reorder_cost, 2),
        "reorder_sku_count": sum(1 for r in recs if r.urgency in ("stockout", "critical", "high", "medium") and r.reorder_qty > 0),
        "critical_stockouts": critical + stockouts,
        "active_stockouts": stockouts,
        "dead_stock_value": round(dead_value, 2),
        "overstock_value": round(overstock_value, 2),
        "as_of": as_of.isoformat(),
    }
    overview_cache_set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# /api/inventory-summary — new
# ---------------------------------------------------------------------------

@app.get("/api/inventory-summary")
def inventory_summary(store: str | None = None) -> dict:
    """Headline inventory numbers. Store filter optional.
    Excludes 'Other' category by default."""
    cache_key = f"invsum:{store or ''}"
    cached = overview_cache_get(cache_key)
    if cached is not None:
        return cached
    # Use the materialized current_inventory table (~40k rows) instead of
    # the window-scan over inventory_snapshots (~470k rows even post-cleanup).
    # Cost prefers the per-row avg_unit_cost from Cova (actual landed cost),
    # falling back to OCS wholesale price, then 60% of retail.
    store_clause = "AND ci.location_id = ?" if store else ""
    params = [store] if store else []
    with db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT
                COUNT(*) AS total_skus,
                SUM(CASE WHEN ci.on_hand > 0 THEN 1 ELSE 0 END) AS skus_in_stock,
                SUM(CASE WHEN ci.on_hand > 0 THEN ci.on_hand ELSE 0 END) AS total_units,
                SUM(CASE WHEN ci.on_hand > 0 THEN ci.on_hand * COALESCE(pr.regular_price, 0) ELSE 0 END) AS retail_value,
                SUM(CASE WHEN ci.on_hand > 0 THEN ci.on_hand *
                    COALESCE(ci.avg_unit_cost, oc.unit_price, pr.regular_price * 0.60, 0)
                ELSE 0 END) AS cost_value
            FROM current_inventory ci
            LEFT JOIN products p ON p.sku = ci.sku
            LEFT JOIN prices pr ON pr.sku = ci.sku AND pr.location_id = ci.location_id
            LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = p.ocs_variant_number
            WHERE COALESCE(p.top_level, '') != 'Other' {store_clause}
        """, params)
        row = cur.fetchone()
        total_skus, skus_in_stock, total_units, retail_value, cost_value = row
        retail_value = float(retail_value or 0)
        cost_value = float(cost_value or 0)

        # Breakdown by top level — same source so Cannabis vs Accessories
        # totals reconcile exactly with the headline numbers above. Now also
        # carries total_units so weeks_of_inventory can be computed per-type.
        cur.execute(f"""
            SELECT
                COALESCE(p.top_level, 'Unknown') AS top_level,
                SUM(CASE WHEN ci.on_hand > 0 THEN 1 ELSE 0 END) AS skus_in_stock,
                SUM(CASE WHEN ci.on_hand > 0 THEN ci.on_hand ELSE 0 END) AS units,
                SUM(CASE WHEN ci.on_hand > 0 THEN ci.on_hand * COALESCE(pr.regular_price, 0) ELSE 0 END) AS retail_value,
                SUM(CASE WHEN ci.on_hand > 0 THEN ci.on_hand *
                    COALESCE(ci.avg_unit_cost, oc.unit_price, pr.regular_price * 0.60, 0)
                ELSE 0 END) AS cost_value
            FROM current_inventory ci
            LEFT JOIN products p ON p.sku = ci.sku
            LEFT JOIN prices pr ON pr.sku = ci.sku AND pr.location_id = ci.location_id
            LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = p.ocs_variant_number
            WHERE COALESCE(p.top_level, '') != 'Other' {store_clause}
            GROUP BY top_level
        """, params)
        by_top = {}
        for top, skus, units, rev, cost in cur.fetchall():
            by_top[top or "Unknown"] = {
                "skus_in_stock": skus or 0,
                "units": int(units or 0),
                "retail_value": round(float(rev or 0), 2),
                "cost_value": round(float(cost or 0), 2),
            }

        # Weeks of inventory: total_units ÷ avg weekly units sold over last 30d
        # Computed TWICE — once combined (back-compat) and once per top_level
        # so Cannabis WOI isn't diluted by slow-moving Accessories (which can
        # have ~50 weeks of cover while cannabis runs at ~4-6).
        from datetime import datetime, timedelta as _td
        win_end = datetime.now().date()
        win_start = win_end - _td(days=30)
        woi_store_clause = " AND s.location_id = ?" if store else ""
        woi_params = [win_start.isoformat(), win_end.isoformat()]
        if store:
            woi_params.append(store)

        # Per-top-level units sold in last 30 days
        cur.execute(f"""
            SELECT COALESCE(p.top_level, 'Unknown') AS top_level,
                   COALESCE(SUM(s.units_sold), 0) AS units_30d
            FROM sales_daily s
            LEFT JOIN products p ON p.sku = s.sku
            WHERE s.sale_date >= ? AND s.sale_date <= ?
              AND COALESCE(p.top_level, '') != 'Other'
              {woi_store_clause}
            GROUP BY top_level
        """, woi_params)
        units_30d_by_top = {row[0]: float(row[1] or 0) for row in cur.fetchall()}
        units_30d = sum(units_30d_by_top.values())

        # Combined weeks_of_inventory (back-compat for any caller)
        weekly_units = units_30d / (30.0 / 7.0)  # = units_30d × 7/30
        weeks_of_inventory = (round(total_units / weekly_units, 1)
                              if weekly_units > 0 and total_units else None)

        # Per-top-level weeks_of_inventory + attach into the by_top dict
        weeks_by_top: dict[str, float | None] = {}
        for top, info in by_top.items():
            u30 = units_30d_by_top.get(top, 0.0)
            wk = u30 / (30.0 / 7.0)
            woi = round(info["units"] / wk, 1) if wk > 0 and info["units"] else None
            info["weeks_of_inventory"] = woi
            info["units_sold_30d"] = int(u30)
            weeks_by_top[top] = woi

    result = {
        "total_skus": total_skus or 0,
        "skus_in_stock": skus_in_stock or 0,
        "total_units": total_units or 0,
        "total_stock_value_retail": round(retail_value, 2),
        "total_stock_value_cost": round(cost_value, 2),
        "by_top_level": by_top,
        "weeks_of_inventory": weeks_of_inventory,            # combined (legacy)
        "weeks_of_inventory_by_top": weeks_by_top,           # new — per type
        "units_sold_30d": int(units_30d),
    }
    overview_cache_set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# /api/store-performance — extended
# ---------------------------------------------------------------------------

@app.get("/api/store-performance")
def store_performance(store: str | None = None) -> list[dict]:
    """For each store: last 7d revenue, prior 7d, MTD, prior-year MTD, last 30d.
    Can be filtered to a single store."""
    cache_key = f"storeperf:{store or ''}"
    cached = overview_cache_get(cache_key)
    if cached is not None:
        return cached
    with db() as conn:
        as_of = get_latest_sale_date(conn)
        as_of_iso = as_of.isoformat()

        mtd_start = as_of.replace(day=1).isoformat()
        py_start = as_of.replace(year=as_of.year - 1).replace(day=1).isoformat()
        py_end = as_of.replace(year=as_of.year - 1).isoformat()

        # Unbounded JOIN — scans the full sales_daily history (~906k rows) so
        # any future analytics extension can look back across all 2.5 years.
        # First load is ~4-5s but the 60s in-memory cache makes every refresh
        # inside that window <50ms, so the real user experience is fast.
        sql = """
            SELECT
                l.id, l.name, l.city,
                COALESCE(SUM(CASE
                    WHEN s.sale_date >= date(?, '-7 days') AND s.sale_date <= ?
                    THEN s.gross_revenue ELSE 0 END), 0) AS rev_7d,
                COALESCE(SUM(CASE
                    WHEN s.sale_date >= date(?, '-14 days') AND s.sale_date < date(?, '-7 days')
                    THEN s.gross_revenue ELSE 0 END), 0) AS rev_prior_7d,
                COALESCE(SUM(CASE
                    WHEN s.sale_date >= date(?, '-30 days') AND s.sale_date <= ?
                    THEN s.gross_revenue ELSE 0 END), 0) AS rev_30d,
                COALESCE(SUM(CASE
                    WHEN s.sale_date >= ? AND s.sale_date <= ?
                    THEN s.gross_revenue ELSE 0 END), 0) AS rev_mtd,
                COALESCE(SUM(CASE
                    WHEN s.sale_date >= ? AND s.sale_date <= ?
                    THEN s.gross_revenue ELSE 0 END), 0) AS rev_mtd_py
            FROM locations l
            LEFT JOIN sales_daily s ON s.location_id = l.id
            WHERE l.is_active = 1
        """
        params = [
            as_of_iso, as_of_iso, as_of_iso, as_of_iso,
            as_of_iso, as_of_iso, mtd_start, as_of_iso,
            py_start, py_end,
        ]
        if store:
            sql += " AND l.id = ?"
            params.append(store)
        sql += " GROUP BY l.id, l.name, l.city ORDER BY l.id"

        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()

    result = []
    for sid, name, city, r7, r_prior_7, r30, r_mtd, r_mtd_py in rows:
        wow = None
        if r_prior_7 and r_prior_7 > 0:
            wow = round((r7 - r_prior_7) / r_prior_7 * 100, 1)
        yoy = None
        if r_mtd_py and r_mtd_py > 0:
            yoy = round((r_mtd - r_mtd_py) / r_mtd_py * 100, 1)

        result.append({
            "id": sid, "name": name, "short_name": _short_store_name(name), "city": city,
            "revenue_7d": float(r7 or 0),
            "revenue_prior_7d": float(r_prior_7 or 0),
            "wow_delta_pct": wow,
            "revenue_30d": float(r30 or 0),
            "revenue_mtd": float(r_mtd or 0),
            "revenue_mtd_prior_year": float(r_mtd_py or 0),
            "mtd_yoy_delta_pct": yoy,
        })
    overview_cache_set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# /api/sales-trend
# ---------------------------------------------------------------------------

@app.get("/api/sales-trend")
def sales_trend(days: int = Query(7, ge=1, le=90)) -> list[dict]:
    with db() as conn:
        as_of = get_latest_sale_date(conn)
        cur = conn.cursor()
        cur.execute("""
            SELECT s.sale_date, COALESCE(p.category, 'other') AS category, SUM(s.gross_revenue) AS revenue
            FROM sales_daily s
            LEFT JOIN products p ON p.sku = s.sku
            WHERE s.sale_date >= date(?, ?) AND s.sale_date <= ?
            GROUP BY s.sale_date, category
            ORDER BY s.sale_date
        """, (as_of.isoformat(), f"-{days} days", as_of.isoformat()))
        rows = cur.fetchall()

    by_day = {}
    for sale_date, category, revenue in rows:
        d = date.fromisoformat(sale_date)
        key = d.strftime("%b %d")  # Cross-platform (no %-d)
        if key not in by_day:
            by_day[key] = {"day": key}
        slug = (category or "other").lower().replace(" ", "_").replace("-", "_")
        by_day[key][slug] = float(revenue or 0)
    return list(by_day.values())


# ---------------------------------------------------------------------------
# /api/stockouts — extended with 7d/14d columns
# ---------------------------------------------------------------------------

@app.get("/api/stockouts")
def get_stockouts(top_level: str | None = None, store: str | None = None) -> dict:
    with db() as conn:
        as_of = get_latest_sale_date(conn)
        recs = compute_all_reorders(conn, location_id=store, as_of_date=as_of)
        recs = filter_by_top_level(recs, top_level, default_exclude_other=True)
        stockout_recs = [r for r in recs if r.urgency == "stockout"]
        stockout_keys = [(r.sku, r.location_id) for r in stockout_recs]

        velocities_7d, velocities_14d = {}, {}
        if stockout_keys:
            cur = conn.cursor()
            # 7d
            cur.execute(f"""
                SELECT sku, location_id, SUM(units_sold) AS u
                FROM sales_daily
                WHERE sale_date >= date(?, '-7 days') AND sale_date <= ?
                GROUP BY sku, location_id
            """, (as_of.isoformat(), as_of.isoformat()))
            for sku, loc, u in cur.fetchall():
                velocities_7d[(sku, loc)] = int(u or 0)
            # 14d
            cur.execute(f"""
                SELECT sku, location_id, SUM(units_sold) AS u
                FROM sales_daily
                WHERE sale_date >= date(?, '-14 days') AND sale_date <= ?
                GROUP BY sku, location_id
            """, (as_of.isoformat(), as_of.isoformat()))
            for sku, loc, u in cur.fetchall():
                velocities_14d[(sku, loc)] = int(u or 0)

    items = []
    for r in stockout_recs:
        d = r.to_dict()
        u7 = velocities_7d.get((r.sku, r.location_id), 0)
        u14 = velocities_14d.get((r.sku, r.location_id), 0)
        u30 = int(round(r.daily_velocity * 30))
        d["units_7d"] = u7
        d["units_14d"] = u14
        d["units_30d"] = u30
        d["velocity_7d"] = round(u7 / 7, 2)
        d["velocity_14d"] = round(u14 / 14, 2)
        # OCS out-of-stock indicator (already in d via to_dict, but explicit flag for UI)
        d["ocs_out_of_stock"] = (d.get("ocs_stock_status") == "NO")
        items.append(d)

    # Pull any user-saved notes for these stockouts
    if items:
        with db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT sku, location_id, note FROM stockout_notes")
            note_map = {(sku, loc): note for sku, loc, note in cur.fetchall()}
            for it in items:
                it["note"] = note_map.get((it["sku"], it["location_id"]), "")

            # Rebate badge enrichment — same hierarchy as Reorder tab
            from jobs.data_revenue_resolver import get_active_deals_for_skus
            sku_set = set()
            for it in items:
                sku_set.add(it["sku"])
                if it.get("ocs_variant"):
                    sku_set.add(it["ocs_variant"])
            deal_map = get_active_deals_for_skus(conn, sku_set)
            for it in items:
                deal = deal_map.get(it["sku"]) or deal_map.get(it.get("ocs_variant", ""))
                if deal:
                    it["data_fee_pct"] = round(deal["percentage"], 2)
                    it["data_fee_partner"] = deal["partner"]
                    it["data_fee_basis"] = deal["basis"]
                    it["data_fee_is_direct"] = deal["is_direct"]
                else:
                    it["data_fee_pct"] = None
                    it["data_fee_partner"] = None
                    it["data_fee_basis"] = None
                    it["data_fee_is_direct"] = False

    return {
        "count": len(items),
        "revenue_at_risk": round(sum(s["revenue_30d"] for s in items), 2),
        "items": items,
    }


@app.post("/api/stockouts/note")
def save_stockout_note(payload: dict = Body(...)) -> dict:
    """Save or update a free-text note explaining why a SKU is stocked out.
    Pass empty string to clear."""
    sku = (payload.get("sku") or "").strip()
    location_id = (payload.get("location_id") or "").strip()
    note = (payload.get("note") or "").strip()
    if not sku or not location_id:
        raise HTTPException(status_code=400, detail="sku and location_id required")
    with db() as conn:
        cur = conn.cursor()
        if note:
            cur.execute("""
                INSERT INTO stockout_notes (sku, location_id, note, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT (sku, location_id) DO UPDATE SET
                  note = excluded.note,
                  updated_at = CURRENT_TIMESTAMP
            """, (sku, location_id, note))
        else:
            cur.execute("DELETE FROM stockout_notes WHERE sku = ? AND location_id = ?",
                        (sku, location_id))
        conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# /api/dead-stock
# ---------------------------------------------------------------------------

@app.get("/api/dead-stock")
def get_dead_stock(
    top_level: str | None = None,
    store: str | None = None,
    window_days: int = 30,
    max_velocity: float | None = None,
) -> dict:
    """
    Items with stock on-hand but very low demand.

    window_days   -- how many days of sales history to consider (14/30/60/90).
                     Shorter = less tolerant. Longer = stricter definition of dead.
    max_velocity  -- optional: also include "nearly dead" items selling below
                     this rate (units/day). None or 0 = only true-zero-sales items.
    """
    window_days = max(1, min(365, int(window_days)))

    with db() as conn:
        cur = conn.cursor()
        # Find latest sale date for the relevant location (or all)
        if store:
            cur.execute(
                "SELECT MAX(sale_date) FROM sales_daily WHERE location_id = ?",
                (store,),
            )
        else:
            cur.execute("SELECT MAX(sale_date) FROM sales_daily")
        row = cur.fetchone()
        as_of = date.fromisoformat(row[0]) if row and row[0] else date.today()
        window_start = (as_of - timedelta(days=window_days)).isoformat()

        # Compute velocity over the requested window for every (sku, location)
        # with current stock. Then apply filter.
        loc_filter = "AND inv.location_id = ?" if store else ""
        params: list = [window_start, as_of.isoformat()]
        if store:
            params.append(store)

        sql = f"""
        WITH latest_inventory AS (
            SELECT sku, location_id, on_hand, MAX(as_of) AS last_snap
            FROM inventory_snapshots
            GROUP BY sku, location_id
        ),
        window_sales AS (
            SELECT sku, location_id,
                   COALESCE(SUM(units_sold), 0) AS units_window
            FROM sales_daily
            WHERE sale_date >= ? AND sale_date <= ?
            GROUP BY sku, location_id
        )
        SELECT inv.sku, inv.location_id, inv.on_hand,
               COALESCE(ws.units_window, 0) AS units_window,
               p.name, p.category, p.top_level, p.brand
        FROM latest_inventory inv
        LEFT JOIN window_sales ws ON ws.sku = inv.sku AND ws.location_id = inv.location_id
        LEFT JOIN products p ON p.sku = inv.sku
        WHERE inv.on_hand > 0 {loc_filter}
        """
        cur.execute(sql, params)
        rows = cur.fetchall()

        # Apply filters (top_level + velocity threshold)
        price_map = get_price_map(conn)
        # Retail price: look up per-location via price_map; cost: via products->ocs
        cur.execute("""
            SELECT p.sku, oc.unit_price
            FROM products p
            LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = p.ocs_variant_number
        """)
        ocs_cost = {r[0]: r[1] for r in cur.fetchall() if r[1] is not None}

    items = []
    total_cost = 0.0
    for r in rows:
        sku, loc_id, on_hand, units_window, name, category, top_level_row, brand = r
        velocity = units_window / window_days

        # Default: only truly dead (0 sales in window)
        # With max_velocity: include items selling below threshold
        if max_velocity is None or max_velocity <= 0:
            if units_window > 0:
                continue
        else:
            if velocity > max_velocity:
                continue

        # Top-level filter (Cannabis/Accessories)
        if top_level:
            if top_level == "Cannabis" and top_level_row != "Cannabis":
                continue
            if top_level == "Accessories" and top_level_row != "Accessories":
                continue

        retail = price_map.get((sku, loc_id), 0) or 0
        cost = ocs_cost.get(sku) or (retail * 0.60 if retail else 0)
        tied_up = round(cost * on_hand, 2)
        total_cost += tied_up

        items.append({
            "sku": sku,
            "product_name": name or sku,
            "category": category,
            "top_level": top_level_row,
            "brand": brand,
            "location_id": loc_id,
            "on_hand": on_hand,
            "units_window": units_window,
            "daily_velocity": round(velocity, 3),
            "cost_tied_up": tied_up,
            "retail_value": round(retail * on_hand, 2),
        })

    items.sort(key=lambda x: x["cost_tied_up"], reverse=True)
    return {
        "count": len(items),
        "capital_tied_up": round(total_cost, 2),
        "window_days": window_days,
        "max_velocity": max_velocity,
        "items": items,
    }


# ---------------------------------------------------------------------------
# /api/overstock
# ---------------------------------------------------------------------------

@app.get("/api/overstock")
def get_overstock(top_level: str | None = None, store: str | None = None) -> dict:
    with db() as conn:
        as_of = get_latest_sale_date(conn)
        recs = compute_all_reorders(conn, location_id=store, as_of_date=as_of)
        recs = filter_by_top_level(recs, top_level, default_exclude_other=True)
    items = [r.to_dict() for r in recs if r.urgency == "overstock"]
    return {"count": len(items), "items": items}


# ---------------------------------------------------------------------------
# /api/dead-stock-overstock — unified slow-movers + overstock view
# ---------------------------------------------------------------------------
# This endpoint replaces separate Dead Stock and Overstock tabs.
# Returns both buckets with a `bucket` field ('dead' | 'overstock') and
# additional columns the UI needs:
#   - last_received_date (most recent invoice line for this SKU at this location)
#   - days_since_last_sold (latest sale_date for this SKU+location)
#   - days_on_hand (= on_hand / daily_velocity, or null if velocity=0)

@app.get("/api/dead-stock-overstock")
def get_dead_stock_overstock(
    top_level: str | None = None,
    store: str | None = None,
    bucket: str | None = None,         # 'dead' | 'overstock' | None (both)
    window_days: int = 30,             # for dead-stock velocity window
    nearly_dead: bool = False,         # include items selling slowly (not just zero)
) -> dict:
    """
    Unified view: items that are either DEAD (capital tied up, not selling)
    or OVERSTOCKED (selling, but holding way more than days_supply target).

    The two buckets answer different questions:
      DEAD       → "should we liquidate / discount this?"
      OVERSTOCK  → "should we transfer some elsewhere?"

    Both are 'capital-tied-up' problems but call for different actions.
    """
    window_days = max(1, min(365, int(window_days)))

    with db() as conn:
        cur = conn.cursor()
        as_of = get_latest_sale_date(conn)
        window_start = (as_of - timedelta(days=window_days)).isoformat()

        # Step 1: get latest inventory per (sku, location) with stock on hand
        loc_filter = "AND inv.location_id = ?" if store else ""
        params: list = []
        if store:
            params.append(store)

        cur.execute(f"""
            WITH latest_inv AS (
                SELECT sku, location_id, on_hand,
                       last_received_date
                FROM (
                    SELECT sku, location_id, on_hand, last_received_date,
                           ROW_NUMBER() OVER (PARTITION BY sku, location_id
                                              ORDER BY as_of DESC) AS rn
                    FROM inventory_snapshots
                )
                WHERE rn = 1
            )
            SELECT inv.sku, inv.location_id, inv.on_hand,
                   p.name, p.category, p.top_level, p.brand, p.ocs_variant_number,
                   inv.last_received_date
            FROM latest_inv inv
            LEFT JOIN products p ON p.sku = inv.sku
            WHERE inv.on_hand > 0 {loc_filter}
        """, params)
        inv_rows = cur.fetchall()

        if not inv_rows:
            return {"count": 0, "items": [], "totals": {"dead_value": 0, "overstock_value": 0}}

        # Step 2: window-window sales velocity per (sku, location)
        cur.execute("""
            SELECT sku, location_id,
                   COALESCE(SUM(units_sold), 0) AS units_window,
                   MAX(sale_date) AS last_sold
            FROM sales_daily
            WHERE sale_date >= ? AND sale_date <= ?
            GROUP BY sku, location_id
        """, (window_start, as_of.isoformat()))
        window_sales = {(r[0], r[1]): (r[2], r[3]) for r in cur.fetchall()}

        # Last sold (lifetime, for dead-stock "days since last sold")
        cur.execute("""
            SELECT sku, location_id, MAX(sale_date) AS last_sold
            FROM sales_daily GROUP BY sku, location_id
        """)
        lifetime_last_sold = {(r[0], r[1]): r[2] for r in cur.fetchall()}

        # Step 4: pricing
        price_map = get_price_map(conn)
        cur.execute("""
            SELECT p.sku, oc.unit_price
            FROM products p
            LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = p.ocs_variant_number
        """)
        ocs_cost = {r[0]: r[1] for r in cur.fetchall() if r[1] is not None}

    # Thresholds (kept consistent with existing engine logic)
    DEAD_VELOCITY_FLOOR = 0.05    # nearly_dead = velocity below this
    OVERSTOCK_DAYS_THRESHOLD = 50

    items = []
    dead_value = 0.0
    overstock_value = 0.0

    for r in inv_rows:
        (sku, loc_id, on_hand, name, category, top_level_row, brand, ocs_variant,
         last_received) = r

        # Top-level filter
        if top_level == "Cannabis" and top_level_row != "Cannabis":
            continue
        if top_level == "Accessories" and top_level_row != "Accessories":
            continue
        # Always exclude 'Other' (fees, donations, gift cards) from money metrics
        if top_level_row == "Other":
            continue

        units_window, _ = window_sales.get((sku, loc_id), (0, None))
        velocity = units_window / window_days
        last_sold = lifetime_last_sold.get((sku, loc_id))

        # Days since last sold
        days_since_sold = None
        if last_sold:
            try:
                days_since_sold = (as_of - date.fromisoformat(last_sold)).days
            except ValueError:
                pass

        # Days on hand
        days_on_hand = round(on_hand / velocity, 1) if velocity > 0 else None

        # last_received is now passed straight from the snapshot — no lookup needed

        # Bucket assignment
        is_dead = False
        is_overstock = False
        if units_window == 0:
            is_dead = True
        elif nearly_dead and velocity < DEAD_VELOCITY_FLOOR:
            is_dead = True
        elif days_on_hand is not None and days_on_hand > OVERSTOCK_DAYS_THRESHOLD:
            is_overstock = True

        if not (is_dead or is_overstock):
            continue
        item_bucket = "dead" if is_dead else "overstock"
        if bucket and bucket != item_bucket:
            continue

        retail = price_map.get((sku, loc_id), 0) or 0
        cost = ocs_cost.get(sku) or (retail * 0.60 if retail else 0)
        tied_up = round(cost * on_hand, 2)
        if is_dead:
            dead_value += tied_up
        else:
            overstock_value += tied_up

        items.append({
            "sku": sku,
            "ocs_variant": ocs_variant,
            "product_name": name or sku,
            "category": category,
            "top_level": top_level_row,
            "brand": brand,
            "location_id": loc_id,
            "bucket": item_bucket,
            "on_hand": on_hand,
            "units_window": units_window,
            "daily_velocity": round(velocity, 3),
            "days_on_hand": days_on_hand,
            "days_since_last_sold": days_since_sold,
            "last_received_date": last_received,
            "last_sold_date": last_sold,
            "cost_tied_up": tied_up,
            "retail_value": round(retail * on_hand, 2),
        })

    items.sort(key=lambda x: x["cost_tied_up"], reverse=True)

    # Rebate badge enrichment — same hierarchy as Reorder tab
    if items:
        with db() as conn:
            from jobs.data_revenue_resolver import get_active_deals_for_skus
            sku_set = set()
            for it in items:
                sku_set.add(it["sku"])
                if it.get("ocs_variant"):
                    sku_set.add(it["ocs_variant"])
            deal_map = get_active_deals_for_skus(conn, sku_set)
            for it in items:
                deal = deal_map.get(it["sku"]) or deal_map.get(it.get("ocs_variant", ""))
                if deal:
                    it["data_fee_pct"] = round(deal["percentage"], 2)
                    it["data_fee_partner"] = deal["partner"]
                    it["data_fee_basis"] = deal["basis"]
                    it["data_fee_is_direct"] = deal["is_direct"]
                else:
                    it["data_fee_pct"] = None
                    it["data_fee_partner"] = None
                    it["data_fee_basis"] = None
                    it["data_fee_is_direct"] = False

    return {
        "count": len(items),
        "items": items,
        "totals": {
            "dead_value": round(dead_value, 2),
            "overstock_value": round(overstock_value, 2),
            "combined_value": round(dead_value + overstock_value, 2),
        },
        "window_days": window_days,
        "as_of": as_of.isoformat(),
    }


# ---------------------------------------------------------------------------
# /api/cross-store-opportunities
# ---------------------------------------------------------------------------
# "Wasaga sells this product really well; Angus doesn't carry it. Should Angus
# try it?" — surface SKUs that are productive at one or more stores but absent
# at a target store.
#
# A "candidate" for store X is a SKU that:
#   - Has 0 current on-hand at X (and ideally 0 historic sales at X)
#   - Has meaningful velocity at one or more OTHER stores in the trailing window
#   - Is currently available in OCS catalog (so X could actually order it)
#
# Ranking: total units sold across other stores in the window. Higher = stronger
# signal that this is a customer-favorite worth trying.

@app.get("/api/cross-store-opportunities")
def get_cross_store_opportunities(
    target_store: str | None = None,
    window_days: int = 60,
    top_level: str | None = None,
    min_other_stores: int = 1,
    min_velocity_other: float = 0.5,
    limit: int = 100,
) -> dict:
    """
    Surface SKUs selling well elsewhere but missing at target_store.

    target_store     -- the store we're looking for opportunities AT.
                        If None: returns top opportunities across all stores.
    window_days      -- how recent the velocity at other stores must be (default 60).
    min_other_stores -- minimum number of other stores where the SKU is selling well.
                        1 = "selling at any other store". 3+ = "broadly proven".
    min_velocity_other -- threshold (units/day) for "selling well" at the other store.
    limit            -- max candidates returned per store.
    """
    window_days = max(7, min(365, int(window_days)))

    with db() as conn:
        cur = conn.cursor()
        as_of = get_latest_sale_date(conn)
        window_start = (as_of - timedelta(days=window_days)).isoformat()

        # Get all active stores (so we can compute "other stores" per target)
        cur.execute("""
            SELECT id, name FROM locations WHERE is_active = 1
        """)
        all_stores = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]

        # If only 1 active store, no cross-store comparisons possible
        if len(all_stores) < 2:
            return {
                "single_store": True,
                "items": [],
                "count": 0,
                "as_of": as_of.isoformat(),
            }

        # Per-(sku, store) trailing-window units + revenue
        cur.execute("""
            SELECT sku, location_id,
                   COALESCE(SUM(units_sold), 0) AS units,
                   COALESCE(SUM(gross_revenue), 0) AS revenue
            FROM sales_daily
            WHERE sale_date >= ? AND sale_date <= ?
            GROUP BY sku, location_id
        """, (window_start, as_of.isoformat()))
        sales_by_sku_store: dict[tuple, dict] = {}
        for sku, loc, units, revenue in cur.fetchall():
            sales_by_sku_store[(sku, loc)] = {
                "units": int(units or 0),
                "velocity": (units or 0) / window_days,
                "revenue": float(revenue or 0),
            }

        # Current inventory per (sku, store)
        cur.execute("""
            SELECT sku, location_id, on_hand
            FROM (
                SELECT sku, location_id, on_hand,
                       ROW_NUMBER() OVER (PARTITION BY sku, location_id
                                          ORDER BY as_of DESC) AS rn
                FROM inventory_snapshots
            )
            WHERE rn = 1
        """)
        inv_by_sku_store: dict[tuple, int] = {(r[0], r[1]): r[2] for r in cur.fetchall()}

        # Lifetime sales — to verify "never sold" claim
        cur.execute("""
            SELECT sku, location_id, MAX(sale_date) AS last_sold
            FROM sales_daily GROUP BY sku, location_id
        """)
        lifetime_last_sold: dict[tuple, str] = {(r[0], r[1]): r[2] for r in cur.fetchall()}

        # Product info
        cur.execute("""
            SELECT sku, name, category, top_level, brand
            FROM products
        """)
        product_info: dict[str, dict] = {
            r[0]: {"name": r[1], "category": r[2], "top_level": r[3], "brand": r[4]}
            for r in cur.fetchall()
        }

        # OCS catalog availability — gate candidates by what's currently orderable
        cur.execute("""
            SELECT p.sku, COALESCE(oc.stock_status, 'UNKNOWN') AS stock_status
            FROM products p
            LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = p.ocs_variant_number
        """)
        ocs_status: dict[str, str] = {r[0]: r[1] for r in cur.fetchall()}

    # Determine which stores to evaluate
    stores_to_check = [target_store] if target_store else [s["id"] for s in all_stores]

    items = []
    for store_id in stores_to_check:
        # For each SKU, see if this is an opportunity at this store
        # Build sku → list of (other_store_id, velocity, units, revenue) where it sells well
        sku_to_other_perf: dict[str, list] = {}
        for (sku, other_loc), perf in sales_by_sku_store.items():
            if other_loc == store_id:
                continue
            if perf["velocity"] < min_velocity_other:
                continue
            sku_to_other_perf.setdefault(sku, []).append({
                "store_id": other_loc,
                "velocity": round(perf["velocity"], 2),
                "units": perf["units"],
                "revenue": round(perf["revenue"], 2),
            })

        for sku, perf_list in sku_to_other_perf.items():
            if len(perf_list) < min_other_stores:
                continue
            # Filter: must NOT be selling well or stocked at target store
            on_hand = inv_by_sku_store.get((sku, store_id), 0)
            if on_hand and on_hand > 0:
                continue
            # Lifetime gate: if it sold here recently (last 90 days), skip — they have it
            last_sold_here = lifetime_last_sold.get((sku, store_id))
            if last_sold_here:
                try:
                    days_ago = (as_of - date.fromisoformat(last_sold_here)).days
                    if days_ago < 90:
                        continue
                except ValueError:
                    pass
            # Top-level filter
            info = product_info.get(sku, {})
            if top_level == "Cannabis" and info.get("top_level") != "Cannabis":
                continue
            if top_level == "Accessories" and info.get("top_level") != "Accessories":
                continue
            if info.get("top_level") == "Other":
                continue
            # OCS availability — must be orderable
            stock = ocs_status.get(sku, "UNKNOWN")
            if stock == "NO":
                continue  # OCS doesn't currently carry it

            total_units = sum(p["units"] for p in perf_list)
            total_revenue = sum(p["revenue"] for p in perf_list)
            best = max(perf_list, key=lambda p: p["velocity"])
            items.append({
                "sku": sku,
                "product_name": info.get("name") or sku,
                "category": info.get("category"),
                "brand": info.get("brand"),
                "target_store": store_id,
                "on_hand_at_target": on_hand,
                "last_sold_at_target": last_sold_here,
                "selling_at_n_stores": len(perf_list),
                "best_store_id": best["store_id"],
                "best_store_velocity": best["velocity"],
                "total_units_other_stores": total_units,
                "total_revenue_other_stores": round(total_revenue, 2),
                "ocs_stock_status": stock,
                "other_stores_detail": perf_list,
            })

    # Sort by total revenue across other stores, then number of stores selling
    items.sort(key=lambda x: (x["total_revenue_other_stores"], x["selling_at_n_stores"]),
               reverse=True)

    # If target_store specified, limit to top N
    if target_store:
        items = items[:limit]

    return {
        "count": len(items),
        "items": items,
        "as_of": as_of.isoformat(),
        "window_days": window_days,
        "single_store": False,
        "total_stores": len(all_stores),
    }


# ---------------------------------------------------------------------------
# /api/suggested-promos
# ---------------------------------------------------------------------------
# For Cannabis SKUs (mainly Dried Flower + Pre-Rolls — shelf-life sensitive),
# surface aging slow-movers and recommend a discount tier based on age.
#
# Cascade by months since last received:
#   3 months → 5%
#   4 months → 10%
#   5 months → 15%
#   6+ months → 20%
#
# A SKU only enters this list if it's:
#   1. Currently on hand (has stock to discount)
#   2. Not selling fast — at most 0.3 units/day in the trailing 30 days
#      (Hero/fast-movers don't need promos; they need to keep moving naturally)
#   3. At least 90 days since last received OR no clear receive date
#
# We surface ALL THREE age signals (not just last received) so the user
# can apply judgment:
#   - days_since_last_received → "how stale on shelf" (drives the tier)
#   - days_since_first_received → "how long it's been stocked"
#   - days_since_last_sold → "is this still moving at all"

@app.get("/api/suggested-promos")
def get_suggested_promos(
    store: str | None = None,
    top_level: str | None = "Cannabis",
    categories: str | None = None,        # comma-separated
    min_age_days: int = 90,
    max_velocity: float = 0.3,
    limit: int = 200,
) -> dict:
    """
    Suggest promotional discounts for aging slow-movers.

    Defaults are tuned for Cannabis (shelf-life sensitive). Override
    `top_level=` or `categories=` for other product types.
    """
    with db() as conn:
        cur = conn.cursor()
        as_of = get_latest_sale_date(conn)
        thirty_ago = (as_of - timedelta(days=30)).isoformat()

        # Latest inventory per (sku, store) — pull received dates straight
        # from the snapshot, which captures the columns Cova's IOH export
        # already contains. No more deriving from invoice history.
        loc_filter = "AND inv.location_id = ?" if store else ""
        params: list = []
        if store:
            params.append(store)

        cur.execute(f"""
            WITH latest_inv AS (
                SELECT sku, location_id, on_hand,
                       first_received_date, last_received_date, days_since_last_sold
                FROM (
                    SELECT sku, location_id, on_hand,
                           first_received_date, last_received_date, days_since_last_sold,
                           ROW_NUMBER() OVER (PARTITION BY sku, location_id
                                              ORDER BY as_of DESC) AS rn
                    FROM inventory_snapshots
                )
                WHERE rn = 1
            )
            SELECT inv.sku, inv.location_id, inv.on_hand,
                   p.name, p.category, p.top_level, p.brand, p.ocs_variant_number,
                   inv.first_received_date, inv.last_received_date, inv.days_since_last_sold
            FROM latest_inv inv
            LEFT JOIN products p ON p.sku = inv.sku
            WHERE inv.on_hand > 0 {loc_filter}
        """, params)
        inv_rows = cur.fetchall()

        # 30-day velocity per (sku, location)
        cur.execute("""
            SELECT sku, location_id,
                   COALESCE(SUM(units_sold), 0) AS units
            FROM sales_daily
            WHERE sale_date >= ? AND sale_date <= ?
            GROUP BY sku, location_id
        """, (thirty_ago, as_of.isoformat()))
        vel_30d = {(r[0], r[1]): r[2] for r in cur.fetchall()}

        # Lifetime last sold per (sku, location) — sales-derived, used for the
        # "Since sold" column. Cova's days_since_last_sold from the snapshot is
        # more accurate (uses a snapshot's reference date), but this is current.
        cur.execute("""
            SELECT sku, location_id, MAX(sale_date) FROM sales_daily GROUP BY sku, location_id
        """)
        last_sold = {(r[0], r[1]): r[2] for r in cur.fetchall()}

        # Pricing
        price_map = get_price_map(conn)
        cur.execute("""
            SELECT p.sku, oc.unit_price
            FROM products p
            LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = p.ocs_variant_number
        """)
        cost_map = {r[0]: r[1] for r in cur.fetchall() if r[1] is not None}

    # Tier cascade by months-since-last-received
    def _suggest_tier(days_since_last_received):
        if days_since_last_received is None:
            return None
        months = days_since_last_received / 30.0
        if months >= 6:
            return {"discount_pct": 20, "label": "6+ months"}
        if months >= 5:
            return {"discount_pct": 15, "label": "5 months"}
        if months >= 4:
            return {"discount_pct": 10, "label": "4 months"}
        if months >= 3:
            return {"discount_pct": 5, "label": "3 months"}
        return None

    cat_filter = None
    if categories:
        cat_filter = {c.strip() for c in categories.split(",") if c.strip()}

    items = []
    for r in inv_rows:
        (sku, loc_id, on_hand, name, category, top_level_row, brand, ocs_variant,
         snap_first_received, snap_last_received, snap_days_since_sold) = r

        # Top-level filter
        if top_level == "Cannabis" and top_level_row != "Cannabis":
            continue
        if top_level == "Accessories" and top_level_row != "Accessories":
            continue
        if top_level_row == "Other":
            continue
        if cat_filter and category not in cat_filter:
            continue

        # Velocity gate
        units_30 = vel_30d.get((sku, loc_id), 0)
        velocity = units_30 / 30.0
        if velocity > max_velocity:
            continue

        # Receive dates come straight from the inventory snapshot (which
        # captures Cova's First/Last Received Date for each SKU at each store).
        first_received = snap_first_received
        last_received = snap_last_received

        # Compute day deltas
        def _days(d):
            if not d:
                return None
            try:
                return (as_of - date.fromisoformat(d)).days
            except ValueError:
                return None

        days_since_first_received = _days(first_received)
        days_since_last_received = _days(last_received)
        # Prefer Cova's days_since_last_sold from the snapshot (it's referenced
        # to the snapshot date, not today, so won't drift). Fall back to deriving
        # from sales_daily if missing.
        if snap_days_since_sold is not None:
            days_since_last_sold = snap_days_since_sold
        else:
            days_since_last_sold = _days(last_sold.get((sku, loc_id)))

        # Age gate
        if days_since_last_received is None:
            # No received-date data — skip (we can't compute a tier without it)
            if min_age_days > 0:
                continue
        elif days_since_last_received < min_age_days:
            continue

        suggested = _suggest_tier(days_since_last_received)

        retail = price_map.get((sku, loc_id), 0) or 0
        cost = cost_map.get(sku) or (retail * 0.60 if retail else 0)
        tied_up = round(cost * on_hand, 2)
        retail_value = round(retail * on_hand, 2)

        # Estimated discount $ if applied
        discount_amt = None
        if suggested and retail:
            discount_amt = round(retail * (suggested["discount_pct"] / 100.0) * on_hand, 2)

        items.append({
            "sku": sku,
            "ocs_variant": ocs_variant,
            "product_name": name or sku,
            "category": category,
            "brand": brand,
            "top_level": top_level_row,
            "location_id": loc_id,
            "on_hand": on_hand,
            "units_30d": units_30,
            "daily_velocity": round(velocity, 3),
            "first_received_date": first_received,
            "last_received_date": last_received,
            "last_sold_date": last_sold.get((sku, loc_id)),
            "days_since_first_received": days_since_first_received,
            "days_since_last_received": days_since_last_received,
            "days_since_last_sold": days_since_last_sold,
            "regular_price": retail,
            "unit_cost": round(cost, 2) if cost else None,  # per-unit cost — needed for margin math in UI
            "cost_tied_up": tied_up,
            "retail_value": retail_value,
            "suggested_discount_pct": suggested["discount_pct"] if suggested else None,
            "suggested_tier_label": suggested["label"] if suggested else None,
            "estimated_discount_total": discount_amt,
        })

    # Sort by retail value descending — biggest financial exposure first
    items.sort(key=lambda x: x["retail_value"], reverse=True)
    items = items[:limit]

    # Rebate badge enrichment — same hierarchy as Reorder tab
    if items:
        with db() as conn:
            from jobs.data_revenue_resolver import get_active_deals_for_skus
            sku_set = set()
            for it in items:
                sku_set.add(it["sku"])
                if it.get("ocs_variant"):
                    sku_set.add(it["ocs_variant"])
            deal_map = get_active_deals_for_skus(conn, sku_set)
            for it in items:
                deal = deal_map.get(it["sku"]) or deal_map.get(it.get("ocs_variant", ""))
                if deal:
                    it["data_fee_pct"] = round(deal["percentage"], 2)
                    it["data_fee_partner"] = deal["partner"]
                    it["data_fee_basis"] = deal["basis"]
                    it["data_fee_is_direct"] = deal["is_direct"]
                else:
                    it["data_fee_pct"] = None
                    it["data_fee_partner"] = None
                    it["data_fee_basis"] = None
                    it["data_fee_is_direct"] = False

    return {
        "count": len(items),
        "items": items,
        "as_of": as_of.isoformat(),
        "totals": {
            "retail_value_at_risk": round(sum(i["retail_value"] for i in items), 2),
            "cost_tied_up": round(sum(i["cost_tied_up"] for i in items), 2),
            "estimated_discount_total": round(
                sum(i["estimated_discount_total"] or 0 for i in items), 2),
        },
        "thresholds": {
            "min_age_days": min_age_days,
            "max_velocity": max_velocity,
        },
    }


# ---------------------------------------------------------------------------
# /api/transfers — new (single-store aware)
# ---------------------------------------------------------------------------

@app.get("/api/transfers")
def get_transfers(from_store: str | None = None, to_store: str | None = None) -> dict:
    """
    Match stores that have EXCESS stock (above target) with stores that are
    stockout or critical for the same SKU.

    "Excess" is a looser bar than "overstock":
        overstock           = days supply > 50  (what the Overstock tab shows)
        transfer-source     = days supply > 35  (willing to share some)

    Transfer quantity is capped by how much the source can spare WITHOUT
    dropping below its own 21-day target:
        source_excess_units = on_hand - (21 × velocity)
    And by destination need:
        dest_need_units     = 14 × dest_velocity

    Takes the smaller of the two (so we don't under-supply the dest or
    strip the source).

    Only meaningful with 2+ stores.
    """
    # Thresholds — tuned so Transfers surfaces more pairs than Overstock does.
    TRANSFER_SOURCE_DAYS = 35    # source store must have > this many days supply
    SOURCE_TARGET_DAYS = 21      # source keeps this much after giving
    DEST_TARGET_DAYS = 14        # destination is filled to this level

    with db() as conn:
        as_of = get_latest_sale_date(conn)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(DISTINCT id) FROM locations WHERE is_active = 1")
        n_stores = cur.fetchone()[0]
        cur.execute("SELECT name FROM locations WHERE is_active = 1 LIMIT 1")
        first_store = cur.fetchone()
        store_name = first_store[0] if first_store else None

        if n_stores < 2:
            return {
                "single_store": True,
                "store_name": store_name,
                "items": [],
                "total_benefit": 0,
                "total_units": 0,
            }

        recs = compute_all_reorders(conn, as_of_date=as_of)
        price_map = get_price_map(conn)

    # Group by SKU
    by_sku: dict[str, list] = {}
    for r in recs:
        by_sku.setdefault(r.sku, []).append(r)

    transfers = []
    for sku, rows in by_sku.items():
        # Sources: any store with days_supply above the transfer threshold,
        # AND with enough stock that they could spare at least 1 unit.
        sources = [
            r for r in rows
            if r.days_supply is not None
            and r.days_supply > TRANSFER_SOURCE_DAYS
            and r.on_hand > 0
        ]
        dests = [r for r in rows if r.urgency in ("stockout", "critical")]
        if not sources or not dests:
            continue

        for dest in dests:
            if not sources:
                break
            # Pick the source with the most on-hand (most room to spare)
            sources.sort(key=lambda x: x.on_hand, reverse=True)
            src = sources[0]

            # How much can this source spare without dropping below their
            # own 21-day target?
            source_keep = max(0, int(round(SOURCE_TARGET_DAYS * src.daily_velocity)))
            source_spare = max(0, src.on_hand - source_keep)

            # How much does the destination need to reach 14d target?
            dest_need = max(1, int(round(DEST_TARGET_DAYS * dest.daily_velocity)))

            transfer_qty = min(source_spare, dest_need)
            if transfer_qty < 1:
                # Source can't spare enough; skip to next source
                sources.pop(0)
                continue

            # Benefit: destination retail - wholesale × units.
            retail = price_map.get((sku, dest.location_id)) or 0
            wholesale = dest.ocs_unit_price or src.ocs_unit_price or 0
            if retail > 0 and wholesale > 0:
                gross_profit_per_unit = retail - wholesale
                benefit_method = "exact"
            elif retail > 0:
                gross_profit_per_unit = retail * 0.33
                benefit_method = "retail × 33% margin (no wholesale)"
            else:
                gross_profit_per_unit = 0
                benefit_method = "no pricing available"
            benefit = round(transfer_qty * gross_profit_per_unit, 2)

            transfers.append({
                "sku": sku,
                "product_name": dest.product_name,
                "category": dest.category,
                "from_store": src.location_id,
                "from_on_hand": src.on_hand,
                "from_velocity": src.daily_velocity,
                "from_days_supply": round(src.days_supply, 1) if src.days_supply else None,
                "to_store": dest.location_id,
                "to_on_hand": dest.on_hand,
                "to_velocity": dest.daily_velocity,
                "transfer_qty": transfer_qty,
                "retail_price": round(retail, 2) if retail else None,
                "wholesale_cost": round(wholesale, 2) if wholesale else None,
                "gross_profit_per_unit": round(gross_profit_per_unit, 2),
                "net_benefit": benefit,
                "benefit_method": benefit_method,
            })
            # Decrement source's on-hand for subsequent destinations on this SKU
            src.on_hand -= transfer_qty
            # Recompute days_supply so the threshold check would still pass next iter
            if src.daily_velocity > 0:
                src.days_supply = src.on_hand / src.daily_velocity
            if (src.days_supply is None or src.days_supply <= TRANSFER_SOURCE_DAYS
                or src.on_hand < 2):
                sources.pop(0)

    transfers.sort(key=lambda x: x["net_benefit"], reverse=True)

    # Apply optional from_store / to_store filters
    if from_store:
        transfers = [t for t in transfers if t["from_store"] == from_store]
    if to_store:
        transfers = [t for t in transfers if t["to_store"] == to_store]

    total_benefit = round(sum(t["net_benefit"] for t in transfers), 2)
    total_units = sum(t["transfer_qty"] for t in transfers)
    exact_count = sum(1 for t in transfers if t["benefit_method"] == "exact")

    return {
        "single_store": False,
        "items": transfers,
        "count": len(transfers),
        "total_benefit": total_benefit,
        "total_units": total_units,
        "exact_priced_count": exact_count,
    }


# ---------------------------------------------------------------------------
# /api/inventory — full on-hand list
# ---------------------------------------------------------------------------

@app.get("/api/inventory")
def get_inventory() -> dict:
    """All SKUs with current on-hand, 30d velocity, retail + cost values.
    Cost uses OCS wholesale when available, falls back to 60% of retail
    (marked as 'estimated' so the UI can show users which is which)."""
    with db() as conn:
        as_of = get_latest_sale_date(conn)
        as_of_iso = as_of.isoformat()

        cur = conn.cursor()
        cur.execute("""
            WITH latest AS (
                SELECT sku, location_id, on_hand,
                       first_received_date, last_received_date, days_since_last_sold
                FROM (
                    SELECT sku, location_id, on_hand,
                           first_received_date, last_received_date, days_since_last_sold,
                           ROW_NUMBER() OVER (PARTITION BY sku, location_id ORDER BY as_of DESC) rn
                    FROM inventory_snapshots
                ) WHERE rn = 1
            ),
            v30 AS (
                SELECT sku, location_id, SUM(units_sold) AS units_30d
                FROM sales_daily
                WHERE sale_date >= date(?, '-30 days') AND sale_date <= ?
                GROUP BY sku, location_id
            )
            SELECT
                l.sku, l.location_id, l.on_hand,
                p.name, p.brand, p.category, p.category_path, p.top_level,
                p.ocs_variant_number,
                pr.regular_price,
                oc.unit_price AS ocs_unit_price,
                COALESCE(v.units_30d, 0) AS units_30d,
                l.first_received_date, l.last_received_date, l.days_since_last_sold
            FROM latest l
            LEFT JOIN products p ON p.sku = l.sku
            LEFT JOIN prices pr ON pr.sku = l.sku AND pr.location_id = l.location_id
            LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = p.ocs_variant_number
            LEFT JOIN v30 v ON v.sku = l.sku AND v.location_id = l.location_id
        """, (as_of_iso, as_of_iso))

        items = []
        for (sku, loc, on_hand, name, brand, category, category_path, top_level,
             ocs_variant,
             price, ocs_unit_price, units_30d,
             first_received, last_received, days_since_sold) in cur.fetchall():
            on_hand = int(on_hand or 0)
            velocity = float(units_30d or 0) / 30 if units_30d else 0
            days_supply = (on_hand / velocity) if velocity > 0 else None
            price = float(price) if price else None

            # Unit cost: OCS wholesale when we have it, 60% of retail otherwise
            if ocs_unit_price:
                unit_cost = round(float(ocs_unit_price), 2)
                cost_source = "ocs"
            elif price:
                unit_cost = round(price * 0.60, 2)
                cost_source = "estimated"
            else:
                unit_cost = None
                cost_source = None

            total_cost = round((unit_cost or 0) * on_hand, 2) if unit_cost else 0

            items.append({
                "sku": sku,
                "ocs_variant": ocs_variant,
                "location_id": loc,
                "on_hand": on_hand,
                "product_name": name or sku,
                "brand": brand,
                "category": category,
                "category_path": category_path,
                "top_level": top_level,
                "regular_price": price,
                "unit_cost": unit_cost,
                "cost_source": cost_source,
                "daily_velocity": round(velocity, 2),
                "days_supply": round(days_supply, 1) if days_supply is not None else None,
                "retail_value": round((price or 0) * on_hand, 2),
                "total_cost": total_cost,
                "first_received_date": first_received,
                "last_received_date": last_received,
                "days_since_last_sold": days_since_sold,
            })

        # Rebate badge enrichment — same hierarchy as Reorder/Stockouts/etc.
        # We only need this for items the user will see (with on_hand > 0 is
        # the typical filter, but apply to all for export consistency).
        if items:
            from jobs.data_revenue_resolver import get_active_deals_for_skus
            sku_set = set()
            for it in items:
                sku_set.add(it["sku"])
                if it.get("ocs_variant"):
                    sku_set.add(it["ocs_variant"])
            deal_map = get_active_deals_for_skus(conn, sku_set)
            for it in items:
                deal = deal_map.get(it["sku"]) or deal_map.get(it.get("ocs_variant", ""))
                if deal:
                    it["data_fee_pct"] = round(deal["percentage"], 2)
                    it["data_fee_partner"] = deal["partner"]
                    it["data_fee_basis"] = deal["basis"]
                    it["data_fee_is_direct"] = deal["is_direct"]
                else:
                    it["data_fee_pct"] = None
                    it["data_fee_partner"] = None
                    it["data_fee_basis"] = None
                    it["data_fee_is_direct"] = False

    # Chain totals for the summary bar (on-hand > 0 only — empty rows pollute)
    in_stock = [i for i in items if i["on_hand"] > 0]
    totals = {
        "total_retail": round(sum(i["retail_value"] for i in in_stock), 2),
        "total_cost": round(sum(i["total_cost"] for i in in_stock), 2),
        "total_units": sum(i["on_hand"] for i in in_stock),
        "skus_in_stock": len(in_stock),
    }

    return {"count": len(items), "items": items, "totals": totals}


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------
# /api/mix-analysis — compare current inventory mix to recent sales mix
# ---------------------------------------------------------------------------

@app.get("/api/mix-analysis")
def mix_analysis(
    store: str | None = None,
    top_level: str = "Cannabis",
    days: int = 90,
) -> dict:
    """
    For each product classification, compare:
      - share of current inventory cost
      - share of recent sales revenue (default 90-day window)

    Drift = inventory % − sales %.
    Positive drift means 'overweight' (holding too much relative to sales).
    Negative drift means 'underweight' (holding too little relative to sales).

    Signal buckets: OVER / over / ≈ / under / UNDER based on drift magnitude.
    """
    with db() as conn:
        as_of = get_latest_sale_date(conn)
        as_of_iso = as_of.isoformat()
        cur = conn.cursor()

        # Inventory cost by classification (current snapshot)
        inv_sql = """
            WITH latest_inv AS (
                SELECT sku, location_id, on_hand FROM (
                    SELECT sku, location_id, on_hand,
                           ROW_NUMBER() OVER (PARTITION BY sku, location_id ORDER BY as_of DESC) rn
                    FROM inventory_snapshots
                ) WHERE rn = 1 AND on_hand > 0
            )
            SELECT p.category,
                   SUM(i.on_hand) AS units,
                   SUM(i.on_hand * COALESCE(oc.unit_price, pr.regular_price * 0.60, 0)) AS inv_cost
            FROM latest_inv i
            JOIN products p ON p.sku = i.sku
            LEFT JOIN prices pr ON pr.sku = i.sku AND pr.location_id = i.location_id
            LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = p.ocs_variant_number
            WHERE p.top_level = ? AND p.category IS NOT NULL
        """
        inv_params = [top_level]
        if store:
            inv_sql += " AND i.location_id = ?"
            inv_params.append(store)
        inv_sql += " GROUP BY p.category"
        cur.execute(inv_sql, inv_params)
        inv_rows = {r[0]: (r[1] or 0, float(r[2] or 0)) for r in cur.fetchall()}

        # Sales revenue by classification (last N days)
        sales_sql = """
            SELECT p.category,
                   SUM(s.units_sold) AS units,
                   SUM(s.gross_revenue) AS sales
            FROM sales_daily s
            JOIN products p ON p.sku = s.sku
            WHERE s.sale_date >= date(?, '-' || ? || ' days')
              AND s.sale_date <= ?
              AND p.top_level = ?
              AND p.category IS NOT NULL
        """
        sales_params = [as_of_iso, days, as_of_iso, top_level]
        if store:
            sales_sql += " AND s.location_id = ?"
            sales_params.append(store)
        sales_sql += " GROUP BY p.category"
        cur.execute(sales_sql, sales_params)
        sales_rows = {r[0]: (r[1] or 0, float(r[2] or 0)) for r in cur.fetchall()}

    # Union of categories
    all_cats = set(inv_rows.keys()) | set(sales_rows.keys())
    total_inv = sum(v[1] for v in inv_rows.values())
    total_sales = sum(v[1] for v in sales_rows.values())

    items = []
    for cat in all_cats:
        inv_units, inv_cost = inv_rows.get(cat, (0, 0.0))
        sales_units, sales_rev = sales_rows.get(cat, (0, 0.0))
        inv_pct = (inv_cost / total_inv * 100) if total_inv else 0
        sales_pct = (sales_rev / total_sales * 100) if total_sales else 0
        drift = inv_pct - sales_pct

        # Signal thresholds
        adrift = abs(drift)
        if adrift < 1.5:
            signal = "balanced"
        elif drift >= 3:
            signal = "overweight_major"
        elif drift >= 1.5:
            signal = "overweight_minor"
        elif drift <= -3:
            signal = "underweight_major"
        else:
            signal = "underweight_minor"

        items.append({
            "category": cat,
            "inv_units": inv_units,
            "inv_cost": round(inv_cost, 2),
            "inv_pct": round(inv_pct, 1),
            "sales_units": sales_units,
            "sales_revenue": round(sales_rev, 2),
            "sales_pct": round(sales_pct, 1),
            "drift": round(drift, 1),
            "signal": signal,
        })

    # Sort by absolute drift, biggest signals first
    items.sort(key=lambda x: abs(x["drift"]), reverse=True)

    return {
        "as_of": as_of.isoformat(),
        "window_days": days,
        "store": store,
        "top_level": top_level,
        "totals": {
            "inv_cost": round(total_inv, 2),
            "sales_revenue": round(total_sales, 2),
        },
        "items": items,
    }


# ---------------------------------------------------------------------------
# /api/competitor-prices — current competitor pricing (latest snapshot per SKU)
# ---------------------------------------------------------------------------

@app.get("/api/competitor-prices")
def competitor_prices(
    competitor: str | None = None,
    sb_store: str | None = None,
    product_type: str | None = None,
    tier: str = "market",
    search: str | None = None,
    limit: int = 2000,
) -> dict:
    """
    Latest competitor price snapshot — one row per (competitor, variant, tier).
    Uses window function to pick the most recent collected_at per variant.

    Filters: competitor name, SB store being compared, product_type, price tier.
    """
    with db() as conn:
        cur = conn.cursor()

        # Any data at all?
        cur.execute("SELECT COUNT(*) FROM competitor_prices")
        total = cur.fetchone()[0]
        if total == 0:
            return {
                "count": 0,
                "total_rows_in_db": 0,
                "items": [],
                "competitors": [],
                "product_types": [],
                "hint": "No competitor data loaded. Run jobs/scrape_cannacabana.py then jobs/import_competitor_prices.py",
            }

        # Competitor list (for filter UI)
        cur.execute(
            "SELECT DISTINCT competitor_name, sb_competes_with "
            "FROM competitor_prices ORDER BY competitor_name"
        )
        competitors = [{"name": r[0], "sb_store": r[1]} for r in cur.fetchall()]

        # Product type list
        cur.execute(
            "SELECT DISTINCT product_type FROM competitor_prices "
            "WHERE product_type IS NOT NULL ORDER BY product_type"
        )
        product_types = [r[0] for r in cur.fetchall()]

        # Main query — latest snapshot per (competitor, variant, tier)
        sql = """
            WITH latest AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY competitor_name, variant_id, price_tier
                    ORDER BY collected_at DESC
                ) AS rn
                FROM competitor_prices
                WHERE price_tier = ?
            )
            SELECT
                competitor_name, sb_competes_with, product_title, vendor,
                product_type, collection_label, variant_sku, variant_size,
                price, compare_at_price, available, collected_at, price_tier
            FROM latest
            WHERE rn = 1
        """
        params: list = [tier]
        if competitor:
            sql += " AND competitor_name = ?"
            params.append(competitor)
        if sb_store:
            sql += " AND sb_competes_with = ?"
            params.append(sb_store)
        if product_type:
            sql += " AND product_type = ?"
            params.append(product_type)
        if search:
            sql += " AND (LOWER(product_title) LIKE ? OR LOWER(vendor) LIKE ?)"
            q = f"%{search.lower()}%"
            params.extend([q, q])
        sql += " ORDER BY price DESC"
        sql += " LIMIT ?"
        params.append(limit)

        cur.execute(sql, params)
        items = []
        for row in cur.fetchall():
            (competitor_name, sb_competes_with, product_title, vendor,
             product_type_v, collection_label, variant_sku, variant_size,
             price, compare_at_price, available, collected_at, pt) = row
            on_sale = bool(compare_at_price and price and compare_at_price > price)
            items.append({
                "competitor_name": competitor_name,
                "sb_competes_with": sb_competes_with,
                "product_title": product_title,
                "vendor": vendor,
                "product_type": product_type_v,
                "collection_label": collection_label,
                "variant_sku": variant_sku,
                "variant_size": variant_size,
                "price": round(price, 2) if price is not None else None,
                "compare_at_price": round(compare_at_price, 2) if compare_at_price else None,
                "available": bool(available) if available is not None else None,
                "on_sale": on_sale,
                "discount_pct": round(100 * (1 - price / compare_at_price), 1) if on_sale else None,
                "collected_at": collected_at,
                "price_tier": pt,
            })

        # Summary stats
        prices_list = [i["price"] for i in items if i["price"]]

    return {
        "count": len(items),
        "total_rows_in_db": total,
        "items": items,
        "competitors": competitors,
        "product_types": product_types,
        "summary": {
            "min_price": min(prices_list) if prices_list else None,
            "max_price": max(prices_list) if prices_list else None,
            "median_price": sorted(prices_list)[len(prices_list) // 2] if prices_list else None,
            "on_sale_count": sum(1 for i in items if i["on_sale"]),
        },
    }


# ---------------------------------------------------------------------------
# /api/export/* — Excel export endpoints
# ---------------------------------------------------------------------------
#
# Design: each export endpoint calls the same underlying data endpoint as the
# dashboard (reuses filter logic), then maps the response through a columns
# spec to build_workbook(). Response is a streaming .xlsx download.
#
# The `filter_*` query params mirror the dashboard's current filters so the
# Excel file reflects what the user is looking at on screen.

def _xlsx_response(content: bytes, filename: str) -> Response:
    """Wrap openpyxl bytes as a downloadable xlsx response."""
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _today_str() -> str:
    from datetime import date as _d
    return _d.today().isoformat()


def _filter_suffix(parts: dict) -> str:
    """Build a readable filename suffix from non-empty filter params."""
    bits = []
    for k, v in parts.items():
        if v:
            bits.append(str(v).replace(" ", ""))
    return "_".join(bits) if bits else "all"


@app.get("/api/export/reorder")
def export_reorder(
    store: str | None = None,
    top_level: str | None = None,
    urgency: str | None = None,
    include_ocs_out: bool = False,
    mix_aware: bool = False,
) -> Response:
    """Download Reorder queue as Excel, respecting current filters."""
    data = get_reorder(
        store=store, top_level=top_level, urgency=urgency,
        include_ocs_out=include_ocs_out, mix_aware=mix_aware,
    )
    recs = data["recommendations"]

    # Resolve store label
    store_label = ""
    if store:
        with db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT name FROM locations WHERE id = ?", (store,))
            r = cur.fetchone()
            store_label = _short_store_name(r[0]) if r else store

    subtitle = f"As of {data.get('as_of') or _today_str()}"
    if store_label: subtitle += f"  ·  Store: {store_label}"
    subtitle += f"  ·  Type: {data.get('top_level') or 'Cannabis'}"
    if urgency and urgency != "all": subtitle += f"  ·  Urgency: {urgency}"
    if mix_aware: subtitle += "  ·  Mix-aware: ON"

    columns = [
        {"key": "urgency", "label": "Status"},
        {"key": "is_top_sku_label", "label": "Hero"},
        {"key": "ocs_variant", "label": "OCS Variant #"},
        {"key": "product_name", "label": "Product"},
        {"key": "category", "label": "Category"},
        {"key": "brand", "label": "Brand"},
        {"key": "sku", "label": "Cova SKU"},
        {"key": "location_id_label", "label": "Store"},
        {"key": "on_hand", "label": "On Hand", "format": "int"},
        {"key": "daily_velocity", "label": "Units/Day", "format": "decimal"},
        {"key": "days_supply", "label": "Days Supply", "format": "decimal"},
        {"key": "ocs_pack_size", "label": "Pack Size", "format": "int"},
        {"key": "reorder_cases", "label": "Cases to Order", "format": "int"},
        {"key": "reorder_qty", "label": "Units to Order", "format": "int"},
        {"key": "ocs_unit_price", "label": "Unit Cost", "format": "currency"},
        {"key": "line_total", "label": "Line Total", "format": "currency"},
        {"key": "ocs_stock_status", "label": "OCS Stock"},
        {"key": "mix_multiplier", "label": "Mix ×", "format": "decimal"},
        {"key": "notes", "label": "Notes"},
    ]

    # Pre-resolve store labels on rows so the sheet reads nicely
    loc_map = _locations_short_map()
    for r in recs:
        r["location_id_label"] = loc_map.get(r.get("location_id"), r.get("location_id"))
        r["is_top_sku_label"] = "★" if r.get("is_top_sku") else ""
        r.setdefault("notes", "")

    totals = {
        "product_name": f"Total ({len(recs)} SKUs)",
        "reorder_qty": data["total_units"],
        "line_total": data["total_wholesale_cost"],
    }

    xlsx = build_workbook(
        sheet_name="Reorder",
        title="SB Insights — Reorder Queue",
        subtitle=subtitle,
        columns=columns,
        rows=recs,
        totals=totals,
    )
    fname = f"Reorder_{_filter_suffix(dict(store=store_label, type=top_level, urgency=urgency))}_{_today_str()}.xlsx"
    return _xlsx_response(xlsx, fname)


@app.get("/api/export/transfers")
def export_transfers(
    from_store: str | None = None,
    to_store: str | None = None,
) -> Response:
    data = get_transfers(from_store=from_store, to_store=to_store)
    if data.get("single_store"):
        raise HTTPException(status_code=400, detail="No transfers available (single-store mode)")
    items = data["items"]
    loc_map = _locations_short_map()
    for r in items:
        r["from_store_label"] = loc_map.get(r["from_store"], r["from_store"])
        r["to_store_label"] = loc_map.get(r["to_store"], r["to_store"])
        r.setdefault("notes", "")

    subtitle_bits = [f"As of {_today_str()}"]
    if from_store: subtitle_bits.append(f"From: {loc_map.get(from_store, from_store)}")
    if to_store: subtitle_bits.append(f"To: {loc_map.get(to_store, to_store)}")

    columns = [
        {"key": "product_name", "label": "Product"},
        {"key": "sku", "label": "Cova SKU"},
        {"key": "category", "label": "Category"},
        {"key": "from_store_label", "label": "From Store"},
        {"key": "from_on_hand", "label": "From On Hand", "format": "int"},
        {"key": "from_velocity", "label": "From Units/Day", "format": "decimal"},
        {"key": "to_store_label", "label": "To Store"},
        {"key": "to_on_hand", "label": "To On Hand", "format": "int"},
        {"key": "to_velocity", "label": "To Units/Day", "format": "decimal"},
        {"key": "transfer_qty", "label": "Transfer Qty", "format": "int"},
        {"key": "retail_price", "label": "Retail (dest)", "format": "currency"},
        {"key": "wholesale_cost", "label": "Wholesale", "format": "currency"},
        {"key": "gross_profit_per_unit", "label": "GP/unit", "format": "currency"},
        {"key": "net_benefit", "label": "Est. Benefit", "format": "currency"},
        {"key": "benefit_method", "label": "Benefit Method"},
        {"key": "notes", "label": "Notes"},
    ]

    totals = {
        "product_name": f"Total ({len(items)} transfers)",
        "transfer_qty": sum(r["transfer_qty"] for r in items),
        "net_benefit": round(sum(r["net_benefit"] for r in items), 2),
    }

    xlsx = build_workbook(
        sheet_name="Transfers",
        title="SB Insights — Inter-Store Transfers",
        subtitle=" · ".join(subtitle_bits),
        columns=columns,
        rows=items,
        totals=totals,
    )
    fname = f"Transfers_{_filter_suffix(dict(from_=loc_map.get(from_store,''), to=loc_map.get(to_store,'')))}_{_today_str()}.xlsx"
    return _xlsx_response(xlsx, fname)


@app.get("/api/export/stockouts")
def export_stockouts(
    top_level: str | None = None,
    store: str | None = None,
) -> Response:
    data = get_stockouts(top_level=top_level, store=store)
    items = data["items"]
    loc_map = _locations_short_map()
    for r in items:
        r["location_id_label"] = loc_map.get(r.get("location_id"), r.get("location_id"))

    columns = [
        {"key": "product_name", "label": "Product"},
        {"key": "sku", "label": "Cova SKU"},
        {"key": "category", "label": "Category"},
        {"key": "location_id_label", "label": "Store"},
        {"key": "units_7d", "label": "Units 7d", "format": "int"},
        {"key": "units_14d", "label": "Units 14d", "format": "int"},
        {"key": "units_30d", "label": "Units 30d", "format": "int"},
        {"key": "velocity_7d", "label": "Vel. 7d", "format": "decimal"},
        {"key": "velocity_14d", "label": "Vel. 14d", "format": "decimal"},
        {"key": "daily_velocity", "label": "Vel. 30d", "format": "decimal"},
        {"key": "revenue_30d", "label": "Revenue 30d", "format": "currency"},
        {"key": "reorder_qty", "label": "Order Qty", "format": "int"},
    ]

    totals = {
        "product_name": f"Total ({len(items)} stockouts)",
        "revenue_30d": round(sum((r.get("revenue_30d") or 0) for r in items), 2),
        "reorder_qty": sum((r.get("reorder_qty") or 0) for r in items),
    }

    subtitle = f"As of {_today_str()}  ·  ${data.get('revenue_at_risk', 0):,.0f}/mo at risk"

    xlsx = build_workbook(
        sheet_name="Stockouts", title="SB Insights — Active Stockouts",
        subtitle=subtitle, columns=columns, rows=items, totals=totals,
    )
    fname = f"Stockouts_{_filter_suffix(dict(store=loc_map.get(store,''), type=top_level))}_{_today_str()}.xlsx"
    return _xlsx_response(xlsx, fname)


@app.get("/api/export/dead-stock")
def export_dead_stock(
    top_level: str | None = None,
    store: str | None = None,
    window_days: int = 30,
    max_velocity: float | None = None,
) -> Response:
    data = get_dead_stock(
        top_level=top_level, store=store,
        window_days=window_days, max_velocity=max_velocity,
    )
    items = data["items"]
    loc_map = _locations_short_map()
    for r in items:
        r["location_id_label"] = loc_map.get(r.get("location_id"), r.get("location_id"))

    columns = [
        {"key": "product_name", "label": "Product"},
        {"key": "sku", "label": "Cova SKU"},
        {"key": "category", "label": "Category"},
        {"key": "location_id_label", "label": "Store"},
        {"key": "on_hand", "label": "On Hand", "format": "int"},
        {"key": "units_window", "label": f"Units sold ({window_days}d)", "format": "int"},
        {"key": "daily_velocity", "label": "Units/day", "format": "decimal"},
        {"key": "cost_tied_up", "label": "Cost Value", "format": "currency"},
        {"key": "retail_value", "label": "Retail Value", "format": "currency"},
    ]

    totals = {
        "product_name": f"Total ({len(items)} SKUs)",
        "on_hand": sum(r["on_hand"] for r in items),
        "cost_tied_up": round(sum(r.get("cost_tied_up", 0) for r in items), 2),
        "retail_value": round(sum(r.get("retail_value", 0) for r in items), 2),
    }

    xlsx = build_workbook(
        sheet_name="Dead Stock", title="SB Insights — Dead Stock",
        subtitle=f"As of {_today_str()}  ·  ${data.get('capital_tied_up', 0):,.0f} tied up",
        columns=columns, rows=items, totals=totals,
    )
    fname = f"DeadStock_{_filter_suffix(dict(store=loc_map.get(store,''), type=top_level))}_{_today_str()}.xlsx"
    return _xlsx_response(xlsx, fname)


@app.get("/api/export/overstock")
def export_overstock(
    top_level: str | None = None,
    store: str | None = None,
) -> Response:
    data = get_overstock(top_level=top_level, store=store)
    items = data["items"]
    loc_map = _locations_short_map()
    for r in items:
        r["location_id_label"] = loc_map.get(r.get("location_id"), r.get("location_id"))

    columns = [
        {"key": "product_name", "label": "Product"},
        {"key": "sku", "label": "Cova SKU"},
        {"key": "category", "label": "Category"},
        {"key": "location_id_label", "label": "Store"},
        {"key": "on_hand", "label": "On Hand", "format": "int"},
        {"key": "daily_velocity", "label": "Units/Day", "format": "decimal"},
        {"key": "days_supply", "label": "Days Supply", "format": "decimal"},
    ]

    xlsx = build_workbook(
        sheet_name="Overstock", title="SB Insights — Overstock",
        subtitle=f"As of {_today_str()}  ·  {data['count']} SKUs",
        columns=columns, rows=items, totals=None,
    )
    fname = f"Overstock_{_filter_suffix(dict(store=loc_map.get(store,''), type=top_level))}_{_today_str()}.xlsx"
    return _xlsx_response(xlsx, fname)


@app.get("/api/export/inventory")
def export_inventory(
    store: str | None = None,
    top_level: str | None = None,
    category: str | None = None,
    in_stock_only: bool = True,
) -> Response:
    data = get_inventory()  # returns all items + totals; we filter client-side
    items = data["items"]
    loc_map = _locations_short_map()

    # Apply filters (matches dashboard behavior)
    if in_stock_only:
        items = [i for i in items if i.get("on_hand", 0) > 0]
    if top_level:
        items = [i for i in items if i.get("top_level") == top_level]
    if store:
        items = [i for i in items if i.get("location_id") == store]
    if category:
        items = [i for i in items if i.get("category") == category]

    for r in items:
        r["location_id_label"] = loc_map.get(r.get("location_id"), r.get("location_id"))

    columns = [
        {"key": "product_name", "label": "Product"},
        {"key": "sku", "label": "Cova SKU"},
        {"key": "top_level", "label": "Type"},
        {"key": "category", "label": "Category"},
        {"key": "brand", "label": "Brand"},
        {"key": "location_id_label", "label": "Store"},
        {"key": "on_hand", "label": "On Hand", "format": "int"},
        {"key": "daily_velocity", "label": "Units/Day", "format": "decimal"},
        {"key": "days_supply", "label": "Days Supply", "format": "decimal"},
        {"key": "regular_price", "label": "Retail Price", "format": "currency"},
        {"key": "unit_cost", "label": "Cost/Unit", "format": "currency"},
        {"key": "total_cost", "label": "Total Cost", "format": "currency"},
        {"key": "retail_value", "label": "Retail Value", "format": "currency"},
    ]

    total_cost = sum(r.get("total_cost", 0) for r in items)
    total_retail = sum(r.get("retail_value", 0) for r in items)

    totals = {
        "product_name": f"Total ({len(items)} SKUs)",
        "on_hand": sum(r.get("on_hand", 0) for r in items),
        "total_cost": round(total_cost, 2),
        "retail_value": round(total_retail, 2),
    }

    subtitle = f"As of {_today_str()}"
    if store: subtitle += f"  ·  {loc_map.get(store, store)}"
    if top_level: subtitle += f"  ·  {top_level}"
    if category: subtitle += f"  ·  {category}"

    xlsx = build_workbook(
        sheet_name="Inventory", title="SB Insights — Current Inventory",
        subtitle=subtitle, columns=columns, rows=items, totals=totals,
    )
    fname = f"Inventory_{_filter_suffix(dict(store=loc_map.get(store,''), type=top_level, cat=category))}_{_today_str()}.xlsx"
    return _xlsx_response(xlsx, fname)


@app.get("/api/export/mix-analysis")
def export_mix_analysis(
    store: str | None = None,
    top_level: str = "Cannabis",
    days: int = 90,
) -> Response:
    data = mix_analysis(store=store, top_level=top_level, days=days)
    items = data["items"]
    # Normalize signal for readability in Excel
    signal_pretty = {
        "overweight_major": "OVERWEIGHT",
        "overweight_minor": "over",
        "balanced": "balanced",
        "underweight_minor": "under",
        "underweight_major": "UNDERWEIGHT",
    }
    for r in items:
        r["signal_pretty"] = signal_pretty.get(r.get("signal", ""), r.get("signal", ""))

    loc_map = _locations_short_map()
    subtitle = f"As of {data.get('as_of')}  ·  {days}-day window  ·  {top_level}"
    if store: subtitle += f"  ·  {loc_map.get(store, store)}"

    columns = [
        {"key": "signal_pretty", "label": "Signal"},
        {"key": "category", "label": "Category"},
        {"key": "inv_cost", "label": "Inv. Cost", "format": "currency"},
        {"key": "inv_pct", "label": "Inv. %", "format": "decimal"},
        {"key": "sales_revenue", "label": "Sales", "format": "currency"},
        {"key": "sales_pct", "label": "Sales %", "format": "decimal"},
        {"key": "drift", "label": "Drift (pts)", "format": "decimal"},
        {"key": "inv_units", "label": "Inv. Units", "format": "int"},
        {"key": "sales_units", "label": "Units Sold", "format": "int"},
    ]

    totals = {
        "category": f"Total ({len(items)} categories)",
        "inv_cost": round(data["totals"]["inv_cost"], 2),
        "sales_revenue": round(data["totals"]["sales_revenue"], 2),
    }

    xlsx = build_workbook(
        sheet_name="Mix Analysis", title="SB Insights — Mix Analysis",
        subtitle=subtitle, columns=columns, rows=items, totals=totals,
    )
    fname = f"MixAnalysis_{_filter_suffix(dict(store=loc_map.get(store,''), type=top_level, days=days))}_{_today_str()}.xlsx"
    return _xlsx_response(xlsx, fname)


@app.get("/api/export/competitor-prices")
def export_competitor_prices(
    competitor: str | None = None,
    sb_store: str | None = None,
    product_type: str | None = None,
    tier: str = "market",
    search: str | None = None,
) -> Response:
    data = competitor_prices(
        competitor=competitor, sb_store=sb_store, product_type=product_type,
        tier=tier, search=search, limit=10000,
    )
    items = data["items"]

    # Readable on-sale column
    for r in items:
        r["sale_display"] = f"-{r['discount_pct']}%" if r.get("on_sale") else ""
        r["stock_display"] = "OOS" if r.get("available") is False else ("in stock" if r.get("available") else "")

    columns = [
        {"key": "competitor_name", "label": "Competitor"},
        {"key": "vendor", "label": "Brand"},
        {"key": "product_title", "label": "Product"},
        {"key": "product_type", "label": "Category"},
        {"key": "variant_size", "label": "Size"},
        {"key": "variant_sku", "label": "SKU"},
        {"key": "price", "label": "Price", "format": "currency"},
        {"key": "compare_at_price", "label": "Was", "format": "currency"},
        {"key": "sale_display", "label": "Sale %"},
        {"key": "stock_display", "label": "Stock"},
        {"key": "price_tier", "label": "Tier"},
        {"key": "collected_at", "label": "Scraped"},
    ]

    filters_for_fname = {
        "competitor": (competitor or "").replace(" ", ""),
        "type": product_type or "",
        "search": search or "",
    }
    subtitle_parts = [f"Tier: {tier}"]
    if competitor: subtitle_parts.append(competitor)
    if product_type: subtitle_parts.append(product_type)
    if search: subtitle_parts.append(f'search="{search}"')
    subtitle = "  ·  ".join(subtitle_parts) + f"  ·  {len(items)} rows"

    xlsx = build_workbook(
        sheet_name="Competitor Prices",
        title="SB Insights — Competitor Pricing",
        subtitle=subtitle, columns=columns, rows=items,
    )
    fname = f"CompetitorPrices_{_filter_suffix(filters_for_fname)}_{_today_str()}.xlsx"
    return _xlsx_response(xlsx, fname)


def _locations_short_map() -> dict:
    """Cached {store_id: short_name} lookup for enriching exports."""
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM locations WHERE is_active = 1")
        return {r[0]: _short_store_name(r[1]) for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------
# Managers, ratings, comments, anchor overrides
# ---------------------------------------------------------------------------

@app.get("/api/managers")
def list_managers(active_only: bool = True) -> list[dict]:
    """Returns list of managers for the 'Who are you?' dropdown."""
    with db() as conn:
        cur = conn.cursor()
        where = "WHERE is_active = 1" if active_only else ""
        cur.execute(f"""
            SELECT id, name, location_id, is_admin, is_active, created_at
            FROM managers {where} ORDER BY name
        """)
        return [
            {"id": r[0], "name": r[1], "location_id": r[2],
             "is_admin": bool(r[3]), "is_active": bool(r[4]), "created_at": r[5]}
            for r in cur.fetchall()
        ]


@app.post("/api/managers")
def create_manager(payload: dict = Body(...)) -> dict:
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    location_id = payload.get("location_id")
    is_admin = 1 if payload.get("is_admin") else 0
    with db() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO managers (name, location_id, is_admin) VALUES (?, ?, ?)",
                (name, location_id, is_admin),
            )
            conn.commit()
            return {"id": cur.lastrowid, "name": name,
                    "location_id": location_id, "is_admin": bool(is_admin)}
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"manager '{name}' already exists")


@app.put("/api/managers/{manager_id}")
def update_manager(manager_id: int, payload: dict = Body(...)) -> dict:
    fields = []
    params = []
    for k in ("name", "location_id", "is_admin", "is_active"):
        if k in payload:
            fields.append(f"{k} = ?")
            val = payload[k]
            if k in ("is_admin", "is_active"):
                val = 1 if val else 0
            params.append(val)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    params.append(manager_id)
    with db() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE managers SET {', '.join(fields)} WHERE id = ?", params)
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="manager not found")
        conn.commit()
    return {"ok": True, "id": manager_id}


# ---- Ratings ---------------------------------------------------------------

@app.get("/api/ratings")
def list_ratings(sku: str | None = None, location_id: str | None = None) -> list[dict]:
    """List ratings. Filter by sku, location, or both."""
    where = []
    params: list = []
    if sku:
        where.append("sku = ?"); params.append(sku)
    if location_id:
        where.append("location_id = ?"); params.append(location_id)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT sku, location_id, author_name, rating, notes, updated_at
            FROM product_ratings {where_sql}
            ORDER BY updated_at DESC
        """, params)
        return [
            {"sku": r[0], "location_id": r[1], "author_name": r[2],
             "rating": r[3], "notes": r[4], "updated_at": r[5]}
            for r in cur.fetchall()
        ]


@app.post("/api/ratings")
def upsert_rating(payload: dict = Body(...)) -> dict:
    """Create or overwrite a rating. Primary key is (sku, location, author)."""
    sku = payload.get("sku")
    location_id = payload.get("location_id")
    author_name = (payload.get("author_name") or "").strip()
    rating = payload.get("rating")
    if not (sku and location_id and author_name):
        raise HTTPException(status_code=400, detail="sku, location_id, author_name required")
    if not isinstance(rating, int) or rating < 0 or rating > 10:
        raise HTTPException(status_code=400, detail="rating must be integer 0..10")
    notes = payload.get("notes")
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO product_ratings (sku, location_id, author_name, rating, notes)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (sku, location_id, author_name) DO UPDATE SET
                rating = excluded.rating,
                notes = excluded.notes,
                updated_at = CURRENT_TIMESTAMP
        """, (sku, location_id, author_name, rating, notes))
        conn.commit()
    return {"ok": True, "sku": sku, "location_id": location_id,
            "author_name": author_name, "rating": rating}


@app.delete("/api/ratings")
def delete_rating(sku: str, location_id: str, author_name: str) -> dict:
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM product_ratings WHERE sku = ? AND location_id = ? AND author_name = ?",
            (sku, location_id, author_name),
        )
        conn.commit()
    return {"ok": True, "deleted": cur.rowcount}


# ---- Comments --------------------------------------------------------------

@app.get("/api/comments")
def list_comments(sku: str, location_id: str | None = None) -> list[dict]:
    """List comments for a SKU. location_id filter is additive (includes chain-wide)."""
    params: list = [sku]
    loc_filter = ""
    if location_id:
        loc_filter = "AND (location_id = ? OR location_id IS NULL)"
        params.append(location_id)
    with db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT id, sku, location_id, author_name, body, created_at
            FROM product_comments
            WHERE sku = ? {loc_filter}
            ORDER BY created_at DESC
        """, params)
        return [
            {"id": r[0], "sku": r[1], "location_id": r[2],
             "author_name": r[3], "body": r[4], "created_at": r[5]}
            for r in cur.fetchall()
        ]


@app.post("/api/comments")
def create_comment(payload: dict = Body(...)) -> dict:
    sku = payload.get("sku")
    author_name = (payload.get("author_name") or "").strip()
    body = (payload.get("body") or "").strip()
    location_id = payload.get("location_id")  # may be None for chain-wide
    if not (sku and author_name and body):
        raise HTTPException(status_code=400, detail="sku, author_name, body required")
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO product_comments (sku, location_id, author_name, body)
            VALUES (?, ?, ?, ?)
        """, (sku, location_id, author_name, body))
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}


@app.delete("/api/comments/{comment_id}")
def delete_comment(comment_id: int) -> dict:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM product_comments WHERE id = ?", (comment_id,))
        conn.commit()
    return {"ok": True, "deleted": cur.rowcount}


# ---- Anchor overrides ------------------------------------------------------

@app.get("/api/anchor-overrides")
def list_anchor_overrides(location_id: str | None = None) -> list[dict]:
    where = "WHERE location_id = ?" if location_id else ""
    params = (location_id,) if location_id else ()
    with db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT sku, location_id, mode, reason, author_name, created_at
            FROM anchor_overrides {where}
            ORDER BY created_at DESC
        """, params)
        # Join in product name so the UI can show something human-readable
        rows = cur.fetchall()
        if not rows:
            return []
        skus = tuple({r[0] for r in rows})
        placeholders = ",".join("?" * len(skus))
        cur.execute(f"SELECT sku, name FROM products WHERE sku IN ({placeholders})", skus)
        names = dict(cur.fetchall())
    return [
        {"sku": r[0], "location_id": r[1], "mode": r[2],
         "reason": r[3], "author_name": r[4], "created_at": r[5],
         "product_name": names.get(r[0], r[0])}
        for r in rows
    ]


@app.post("/api/anchor-overrides")
def upsert_anchor_override(payload: dict = Body(...)) -> dict:
    sku = payload.get("sku")
    location_id = payload.get("location_id")
    mode = payload.get("mode")
    if not (sku and location_id and mode in ("include", "exclude")):
        raise HTTPException(status_code=400, detail="sku, location_id, mode=(include|exclude) required")
    reason = payload.get("reason")
    author_name = payload.get("author_name")
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO anchor_overrides (sku, location_id, mode, reason, author_name)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (sku, location_id) DO UPDATE SET
                mode = excluded.mode,
                reason = excluded.reason,
                author_name = excluded.author_name,
                created_at = CURRENT_TIMESTAMP
        """, (sku, location_id, mode, reason, author_name))
        conn.commit()
    # Anchor overrides feed the Hero ranking but aren't in the hero cache key —
    # invalidate so the change shows on the next Reorder Report load.
    from jobs.reorder_engine import reset_hero_cache
    reset_hero_cache()
    return {"ok": True, "sku": sku, "location_id": location_id, "mode": mode}


@app.delete("/api/anchor-overrides")
def delete_anchor_override(sku: str, location_id: str) -> dict:
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM anchor_overrides WHERE sku = ? AND location_id = ?",
            (sku, location_id),
        )
        conn.commit()
    from jobs.reorder_engine import reset_hero_cache
    reset_hero_cache()
    return {"ok": True, "deleted": cur.rowcount}


# ---------------------------------------------------------------------------
# OCS order template auto-fill
# ---------------------------------------------------------------------------

@app.post("/api/order-fill")
async def order_fill(
    template: UploadFile = File(...),
    location_id: str = Form(...),
    ceiling_days: int | None = Form(None),
    min_velocity: float | None = Form(None),
) -> dict:
    """
    Upload an OCS weekly order template (.xlsx), get back a preview of what
    the engine would fill in. The filled .xlsx is returned base64-encoded
    so the browser can offer it as a download without a second round-trip.
    """
    import base64
    from io import BytesIO
    from jobs.ocs_order_fill import fill_template, write_filled_template

    contents = await template.read()
    if len(contents) > 25_000_000:  # 25MB safety net
        raise HTTPException(status_code=413, detail="template too large")

    buf = BytesIO(contents)
    try:
        with db() as conn:
            kw = {}
            if ceiling_days is not None: kw["ceiling_days"] = ceiling_days
            if min_velocity is not None: kw["min_velocity"] = min_velocity
            lines, filled_df, summary = fill_template(
                conn, buf, location_id=location_id, **kw,
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Serialize filled xlsx for inline download
    out = BytesIO()
    write_filled_template(filled_df, out)
    out.seek(0)
    filled_b64 = base64.b64encode(out.read()).decode("ascii")

    # Convert dataclasses to dicts for JSON
    from dataclasses import asdict
    return {
        "summary": summary,
        "lines": [asdict(l) for l in lines if l.suggested_quantity > 0],
        "all_lines": [asdict(l) for l in lines],   # includes 0-qty matches for "what got skipped"
        "filled_xlsx_base64": filled_b64,
        "original_filename": template.filename,
    }


@app.post("/api/order-fill/import")
async def import_order_fill_upload(
    file: UploadFile = File(...),
    location_id: str | None = Form(None),
    _admin: dict = Depends(require_admin),
) -> dict:
    """Upload an OCS Order Fill (OrderExport_*.xlsx) for a store and import it.

    This is the manual route for the OCS Order Fill, which doesn't arrive via
    the Cova email scraper. Once imported, the Reorder Report reflects that
    store's flow-through / back-in-stock availability (an item OCS will deliver
    this cycle is no longer hidden just because the warehouse-stock catalogue
    says NO). Admin only. Order Fill is per-store, so a store must be chosen;
    the saved filename is store-prefixed to keep each store's run distinct.
    Idempotent — re-importing the same store's file replaces its run.
    """
    from pathlib import Path as _Path
    from jobs.import_order_fill import is_order_fill_file, import_order_fill_file

    if not location_id:
        raise HTTPException(status_code=400, detail="Select the store this Order Fill is for")

    contents = await file.read()
    if len(contents) > 25_000_000:  # 25MB safety net
        raise HTTPException(status_code=413, detail="file too large")
    name = _Path(file.filename or "").name
    if not name.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Expected an .xlsx Order Fill export")

    imports_dir = _Path("imports")
    imports_dir.mkdir(exist_ok=True)
    # Store-prefix so each store's OrderExport is a distinct source_file
    # (the OCS filename encodes date/time but not store).
    dest = imports_dir / f"{location_id}__{name}"
    with open(dest, "wb") as f:
        f.write(contents)

    # Validate it's actually an Order Fill before importing (filename pattern +
    # MasterCatalogue sheet + required columns).
    if not is_order_fill_file(dest):
        try:
            dest.unlink()
        except OSError:
            pass
        raise HTTPException(
            status_code=400,
            detail="Not a recognized OCS Order Fill — expected OrderExport_DD_Mon_YYYY*.xlsx "
                   "with a 'MasterCatalogue' sheet.",
        )

    try:
        with db() as conn:
            result = import_order_fill_file(conn, dest, location_id=location_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Import failed: {e}")

    return {"ok": True, "location_id": location_id, **result}


@app.get("/api/reorder/export-ocs-template")
def export_ocs_template(store: str | None = None):
    """Fill the store's latest auto-pulled OrderExport with the engine's reorder
    quantities and return it ready to upload to OCS — no manual upload needed.

    Single-store only (an order is placed per store). 404 if we don't have an
    Order Fill for that store yet (run the OCS Connector, or import one).
    """
    from io import BytesIO
    from pathlib import Path as _Path
    from jobs.ocs_order_fill import fill_template, write_filled_template

    if not store:
        raise HTTPException(status_code=400,
                            detail="Select a single store to export an OCS order template.")
    with db() as conn:
        row = conn.execute(
            """SELECT source_file, generated_at FROM order_fill_runs
               WHERE location_id = ? ORDER BY generated_at DESC, id DESC LIMIT 1""",
            (store,)).fetchone()
        if not row:
            raise HTTPException(status_code=404,
                detail=f"No OCS Order Fill imported for {store} yet — run the OCS Connector "
                       f"(Settings → OCS Connector) or import one there.")
        source_file = row[0]
        # Locate the stored OrderExport file (connector/upload saved it).
        path = _Path("imports") / "processed" / source_file
        if not path.exists():
            path = _Path("imports") / source_file
        if not path.exists():
            raise HTTPException(status_code=404,
                detail=f"Order Fill file for {store} is no longer on disk ({source_file}); "
                       f"re-run the OCS Connector to refresh it.")
        try:
            _lines, filled_df, _summary = fill_template(conn, path, location_id=store)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        out = BytesIO()
        write_filled_template(filled_df, out)
        out.seek(0)

    fname = f"OCS_Order_{store}_{date.today().isoformat()}.xlsx"
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ---------------------------------------------------------------------------
# OCS connector — config + live-validation harness (admin only)
# ---------------------------------------------------------------------------
# Credentials are encrypted at rest (encrypt_secret). The connector itself
# stays gated by ocs_account.is_active; these endpoints let an admin configure
# it, test the login, see the retailer list to build the store mapping, and run
# a manual sync. The auto-scheduler is intentionally NOT wired until the
# connector is validated live.

@app.get("/api/ocs/config")
def ocs_get_config(_admin: dict = Depends(require_admin)) -> dict:
    with db() as conn:
        row = conn.execute(
            """SELECT base_url, username, order_fill_format, is_active,
                      last_run_at, last_status, last_error
               FROM ocs_account ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        maps = conn.execute(
            "SELECT ocs_retailer_id, location_id, ocs_store_number, label, is_active FROM ocs_store_map"
        ).fetchall()
    store_map = [dict(zip(["ocs_retailer_id", "location_id", "ocs_store_number", "label", "is_active"], m))
                 for m in maps]
    if not row:
        return {"configured": False, "store_map": store_map}
    return {
        "configured": True, "base_url": row[0], "username": row[1],
        "order_fill_format": row[2], "is_active": bool(row[3]),
        "last_run_at": row[4], "last_status": row[5], "last_error": row[6],
        "store_map": store_map,
    }


@app.post("/api/ocs/config")
def ocs_save_config(payload: dict = Body(...), _admin: dict = Depends(require_admin)) -> dict:
    base_url = (payload.get("base_url") or "").strip()
    username = (payload.get("username") or "").strip()
    password = payload.get("password")  # optional on update (keep existing if blank)
    if not base_url or not username:
        raise HTTPException(status_code=400, detail="base_url and username are required")
    is_active = 1 if payload.get("is_active") else 0
    order_fill_format = payload.get("order_fill_format") or "Packs"
    from jobs.auth import encrypt_secret
    with db() as conn:
        existing = conn.execute(
            "SELECT id, password_enc FROM ocs_account ORDER BY id DESC LIMIT 1"
        ).fetchone()
        pw_enc = encrypt_secret(password) if password else (existing[1] if existing else None)
        if pw_enc is None:
            raise HTTPException(status_code=400, detail="password required for first setup")
        if existing:
            conn.execute(
                """UPDATE ocs_account SET base_url=?, username=?, password_enc=?,
                          order_fill_format=?, is_active=? WHERE id=?""",
                (base_url, username, pw_enc, order_fill_format, is_active, existing[0]))
        else:
            conn.execute(
                """INSERT INTO ocs_account (label, base_url, username, password_enc, order_fill_format, is_active)
                   VALUES ('OCS B2B', ?, ?, ?, ?, ?)""",
                (base_url, username, pw_enc, order_fill_format, is_active))
        conn.commit()
    return {"ok": True}


@app.post("/api/ocs/test-login")
def ocs_test_login(_admin: dict = Depends(require_admin)) -> dict:
    from jobs.ocs_connector import load_account, _make_session, login
    with db() as conn:
        acct = load_account(conn)
    if not acct:
        raise HTTPException(status_code=400, detail="Configure OCS credentials first")
    try:
        login(_make_session(), acct)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/ocs/retailers")
def ocs_list_retailers(_admin: dict = Depends(require_admin)) -> dict:
    """Log in and parse the SelectStore retailer list (for building the store
    mapping). Live call — used during validation."""
    from jobs.ocs_connector import load_account, _make_session, login, list_retailers
    with db() as conn:
        acct = load_account(conn)
    if not acct:
        raise HTTPException(status_code=400, detail="Configure OCS credentials first")
    try:
        s = _make_session()
        login(s, acct)
        return {"ok": True, "retailers": list_retailers(s, acct)}
    except Exception as e:
        return {"ok": False, "error": str(e), "retailers": []}


@app.post("/api/ocs/store-map")
def ocs_save_store_map(payload: dict = Body(...), _admin: dict = Depends(require_admin)) -> dict:
    """Replace the OCS-retailer → our-store mapping. payload: {"mappings": [...]}"""
    rows = payload.get("mappings", [])
    with db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM ocs_store_map")
        n = 0
        for m in rows:
            rid, loc = m.get("ocs_retailer_id"), m.get("location_id")
            if not rid or not loc:
                continue
            cur.execute(
                """INSERT INTO ocs_store_map (ocs_retailer_id, location_id, ocs_store_number, label, is_active)
                   VALUES (?, ?, ?, ?, 1)""",
                (str(rid), str(loc), m.get("ocs_store_number"), m.get("label")))
            n += 1
        conn.commit()
    return {"ok": True, "count": n}


@app.post("/api/ocs/run-now")
def ocs_run_now(_admin: dict = Depends(require_admin)) -> dict:
    """Trigger a one-off connector sync (catalogue + per-store OrderExports).
    Manual run — forces a sync even if the account isn't marked active yet."""
    from jobs.ocs_connector import sync_ocs
    with db() as conn:
        return sync_ocs(conn, DB_PATH, force=True)


# ---------------------------------------------------------------------------
# Order Outcome Analysis — sell-through tracking against historical invoices
# ---------------------------------------------------------------------------

# Engine "would have recommended" heuristic (cheap stand-in for re-running
# the engine at delivery date). True if either: trailing 30d velocity ≥ 0.5,
# OR was a top-50 anchor at the time. Mirrors current engine defaults.
_ENGINE_VEL_FLOOR = 0.5
_TOP_N_ANCHORS = 50
_ANCHOR_WINDOW_DAYS = 90


def _build_engine_decision_lookup(conn, location_id: str, as_of_date: date) -> dict:
    """
    Returns dict keyed by ocs_variant_number → True if the engine WOULD
    have recommended ordering this SKU at as_of_date for this location.
    Implementation is a heuristic — see _ENGINE_VEL_FLOOR.
    """
    cur = conn.cursor()
    pre_start = (as_of_date - timedelta(days=30)).isoformat()
    pre_end = as_of_date.isoformat()

    # Pre-delivery 30d velocity per OCS variant
    cur.execute("""
        SELECT p.ocs_variant_number, SUM(sd.units_sold) / 30.0 AS vel
        FROM sales_daily sd
        JOIN products p ON p.sku = sd.sku
        WHERE sd.location_id = ? AND sd.sale_date >= ? AND sd.sale_date < ?
          AND p.ocs_variant_number IS NOT NULL
        GROUP BY p.ocs_variant_number
    """, (location_id, pre_start, pre_end))
    velocity_by_variant = {r[0]: (r[1] or 0) for r in cur.fetchall()}

    # Top-50 anchors at as_of_date
    anchor_start = (as_of_date - timedelta(days=_ANCHOR_WINDOW_DAYS)).isoformat()
    cur.execute("""
        SELECT p.ocs_variant_number FROM (
            SELECT sku, SUM(gross_revenue) AS rev
            FROM sales_daily
            WHERE location_id = ? AND sale_date >= ? AND sale_date < ?
            GROUP BY sku
        ) t
        JOIN products p ON p.sku = t.sku
        WHERE t.rev > 0 AND p.ocs_variant_number IS NOT NULL
        ORDER BY t.rev DESC LIMIT ?
    """, (location_id, anchor_start, pre_end, _TOP_N_ANCHORS))
    anchor_variants = {r[0] for r in cur.fetchall()}

    # Combine: engine recommends if anchor OR vel >= floor
    all_variants = set(velocity_by_variant) | anchor_variants
    return {
        v: (v in anchor_variants) or (velocity_by_variant.get(v, 0) >= _ENGINE_VEL_FLOOR)
        for v in all_variants
    }


def _outcome_sql(window_days: int) -> str:
    """SQL template for per-line outcome analysis. Parameterized by window."""
    return f"""
    WITH post_sales AS (
        SELECT p.ocs_variant_number AS ocs_var,
               SUM(sd.units_sold) AS units_after,
               SUM(sd.gross_revenue) AS revenue_after
        FROM sales_daily sd
        JOIN products p ON p.sku = sd.sku
        WHERE sd.location_id = ?
          AND sd.sale_date >= ? AND sd.sale_date < ?
          AND p.ocs_variant_number IS NOT NULL
        GROUP BY p.ocs_variant_number
    )
    SELECT
        il.ocs_variant,
        il.description,
        il.units_delivered,
        il.unit_price,
        il.line_total,
        p.sku AS cova_sku,
        p.category,
        p.brand,
        COALESCE(ps.units_after, 0) AS units_sold_after,
        COALESCE(ps.revenue_after, 0) AS revenue_after
    FROM invoice_lines il
    LEFT JOIN products p ON p.ocs_variant_number = il.ocs_variant
    LEFT JOIN post_sales ps ON ps.ocs_var = il.ocs_variant
    WHERE il.invoice_no = ?
    """


# ---------------------------------------------------------------------------
# Brand Partners, Data Revenue Deals, LTOs
# ---------------------------------------------------------------------------

# ---- Brand partners ----

@app.get("/api/brand-partners")
def list_brand_partners(active_only: bool = False) -> list[dict]:
    """All brand partners. Optionally filter to active only."""
    where = "WHERE is_active = 1" if active_only else ""
    with db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT id, brand_name, contact_name, contact_email, notes, is_active, created_at
            FROM brand_partners {where} ORDER BY brand_name
        """)
        return [
            {"id": r[0], "brand_name": r[1], "contact_name": r[2],
             "contact_email": r[3], "notes": r[4],
             "is_active": bool(r[5]), "created_at": r[6]}
            for r in cur.fetchall()
        ]


@app.post("/api/brand-partners")
def create_brand_partner(payload: dict = Body(...)) -> dict:
    name = (payload.get("brand_name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="brand_name is required")
    with db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO brand_partners (brand_name, contact_name, contact_email, notes, is_active)
                VALUES (?, ?, ?, ?, ?)
            """, (
                name,
                payload.get("contact_name"),
                payload.get("contact_email"),
                payload.get("notes"),
                1 if payload.get("is_active", True) else 0,
            ))
            conn.commit()
            return {"id": cur.lastrowid, "brand_name": name}
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"brand '{name}' already exists")


@app.put("/api/brand-partners/{brand_id}")
def update_brand_partner(brand_id: int, payload: dict = Body(...)) -> dict:
    fields, params = [], []
    for k in ("brand_name", "contact_name", "contact_email", "notes", "is_active"):
        if k in payload:
            v = payload[k]
            if k == "is_active":
                v = 1 if v else 0
            fields.append(f"{k} = ?")
            params.append(v)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    params.append(brand_id)
    with db() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE brand_partners SET {', '.join(fields)} WHERE id = ?", params)
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="brand not found")
        conn.commit()
    return {"ok": True, "id": brand_id}


@app.delete("/api/brand-partners/{brand_id}")
def delete_brand_partner(brand_id: int) -> dict:
    """Soft-delete by setting is_active=0. Hard delete is intentionally not exposed —
    deals and LTOs can reference it."""
    with db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE brand_partners SET is_active = 0 WHERE id = ?", (brand_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="brand not found")
        conn.commit()
    return {"ok": True, "id": brand_id, "soft_deleted": True}


# ============================================================================
# OCS Catalogue — searchable product list with manager ratings + reviews
# ============================================================================
# This tab is the "second brain" for ordering decisions. Managers can:
#   - Browse the OCS catalog (~5,400 SKUs)
#   - Search by name, brand, category, size
#   - Rate products 1-10 (their personal score)
#   - See aggregated ratings across all managers
#   - Add notes/comments visible to other managers
#
# The tables product_ratings and product_comments already exist.
# These endpoints expose them.

@app.get("/api/ocs-catalogue")
def get_ocs_catalogue(
    search: str | None = None,
    category: str | None = None,
    brand: str | None = None,
    in_stock_only: bool = False,
    has_ratings: bool | None = None,
    sort: str = "name",
    limit: int = 500,
) -> dict:
    """List OCS catalog SKUs with aggregate rating info.

    Joins to product_ratings to compute average rating + count, and to
    products (via ocs_variant_number) to get the Cova SKU when available
    (so we can link to inventory + reviews from the catalog view).
    """
    sort = sort if sort in {"name", "brand", "category", "rating", "rating_count"} else "name"

    with db() as conn:
        cur = conn.cursor()

        # Base query: pull catalog rows, plus aggregated ratings/comments and
        # the Cova SKU (left-join via ocs_variant_number).
        sql = """
            SELECT oc.ocs_variant_number, oc.ocs_item_number, oc.product_name,
                   oc.brand, oc.supplier, oc.category, oc.subcategory, oc.size,
                   oc.stock_status, oc.unit_price, oc.pack_size,
                   oc.thc_min, oc.thc_max, oc.cbd_min, oc.cbd_max,
                   p.sku AS cova_sku,
                   COALESCE((
                       SELECT AVG(rating) FROM product_ratings pr
                       WHERE pr.sku = p.sku
                   ), 0) AS avg_rating,
                   COALESCE((
                       SELECT COUNT(*) FROM product_ratings pr
                       WHERE pr.sku = p.sku
                   ), 0) AS rating_count,
                   COALESCE((
                       SELECT COUNT(*) FROM product_comments pc
                       WHERE pc.sku = p.sku
                   ), 0) AS comment_count
            FROM ocs_catalog oc
            LEFT JOIN products p ON p.ocs_variant_number = oc.ocs_variant_number
            WHERE 1=1
        """
        params: list = []

        if search:
            sql += " AND (LOWER(oc.product_name) LIKE ? OR LOWER(oc.brand) LIKE ? OR oc.ocs_variant_number LIKE ?)"
            q = f"%{search.lower()}%"
            params.extend([q, q, f"%{search}%"])
        if category:
            sql += " AND oc.category = ?"
            params.append(category)
        if brand:
            sql += " AND oc.brand = ?"
            params.append(brand)
        if in_stock_only:
            sql += " AND oc.stock_status = 'YES'"
        if has_ratings is True:
            sql += " AND rating_count > 0"
        elif has_ratings is False:
            sql += " AND rating_count = 0"

        # ORDER BY uses subquery aliases — restate them at end
        order_map = {
            "name": "oc.product_name",
            "brand": "oc.brand, oc.product_name",
            "category": "oc.category, oc.subcategory, oc.product_name",
            "rating": "avg_rating DESC, rating_count DESC",
            "rating_count": "rating_count DESC, avg_rating DESC",
        }
        sql += f" ORDER BY {order_map[sort]} LIMIT ?"
        params.append(min(2000, max(1, int(limit))))

        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        items = [dict(zip(cols, row)) for row in cur.fetchall()]

        # Order Fill availability — OCS's stock_status='NO' often misses
        # flow-through SKUs (ships from LP, not OCS warehouse). Aggregate
        # across stores: if any store sees this variant as flow_thru or
        # back_in_stock in the latest Order Fill run, mark it available.
        # Same data source as the Reorder Report's "OCS Avail." column for
        # consistency.
        try:
            from jobs.import_order_fill import get_latest_order_fill_skus
            of_map = get_latest_order_fill_skus(conn)  # keyed by (loc, variant_lower)
            # Aggregate per variant (drop the location dimension for the
            # catalogue's chain-wide view).
            of_by_variant: dict = {}
            for (loc, variant), info in of_map.items():
                if not variant:
                    continue
                agg = of_by_variant.setdefault(variant, {
                    "flow_thru": False, "back_in_stock": False,
                    "available_quantity": 0, "stores_seen": 0,
                })
                if info.get("flow_thru"):     agg["flow_thru"] = True
                if info.get("back_in_stock"): agg["back_in_stock"] = True
                aq = info.get("available_quantity") or 0
                if aq > agg["available_quantity"]:
                    agg["available_quantity"] = aq
                agg["stores_seen"] += 1
            for it in items:
                v = (it.get("ocs_variant_number") or "").lower()
                agg = of_by_variant.get(v)
                if agg:
                    it["order_fill_flow_thru"]     = bool(agg["flow_thru"])
                    it["order_fill_back_in_stock"] = bool(agg["back_in_stock"])
                    it["order_fill_avail_qty"]     = int(agg["available_quantity"])
                else:
                    it["order_fill_flow_thru"]     = None
                    it["order_fill_back_in_stock"] = False
                    it["order_fill_avail_qty"]     = 0
        except Exception:
            for it in items:
                it.setdefault("order_fill_flow_thru", None)
                it.setdefault("order_fill_back_in_stock", False)
                it.setdefault("order_fill_avail_qty", 0)

        # Attach data revenue / rebate info via the resolver so the OCS
        # Catalogue tab shows the same badges as the Reorder Report.
        # Match on both Cova SKU and OCS variant — the resolver accepts either.
        try:
            from jobs.data_revenue_resolver import get_active_deals_for_skus
            sku_set = set()
            for it in items:
                if it.get("cova_sku"):
                    sku_set.add(it["cova_sku"])
                if it.get("ocs_variant_number"):
                    sku_set.add(it["ocs_variant_number"])
            deal_map = get_active_deals_for_skus(conn, sku_set) if sku_set else {}
            for it in items:
                # Prefer Cova-SKU match if available, fall back to OCS variant
                deal = deal_map.get(it.get("cova_sku")) or deal_map.get(it.get("ocs_variant_number"))
                if deal:
                    it["data_fee_pct"] = round(deal["percentage"], 2)
                    it["data_fee_partner"] = deal["partner"]
                    it["data_fee_basis"] = deal["basis"]
                    it["data_fee_is_direct"] = deal["is_direct"]
                else:
                    it["data_fee_pct"] = None
                    it["data_fee_partner"] = None
                    it["data_fee_basis"] = None
                    it["data_fee_is_direct"] = False
        except Exception:
            # Resolver fails shouldn't kill the whole catalogue listing
            for it in items:
                it.setdefault("data_fee_pct", None)
                it.setdefault("data_fee_partner", None)
                it.setdefault("data_fee_basis", None)
                it.setdefault("data_fee_is_direct", False)

        # Distinct categories + brands for filter dropdowns
        cur.execute("SELECT DISTINCT category FROM ocs_catalog WHERE category IS NOT NULL ORDER BY category")
        categories = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT brand FROM ocs_catalog WHERE brand IS NOT NULL ORDER BY brand")
        brands = [r[0] for r in cur.fetchall()]

        # Latest catalogue refresh — drives the "as of" header on the page.
        cur.execute("SELECT MAX(as_of) FROM ocs_catalog")
        catalogue_as_of = cur.fetchone()[0]

    return {
        "count": len(items),
        "items": items,
        "filters": {"categories": categories, "brands": brands},
        "catalogue_as_of": catalogue_as_of,
    }


@app.get("/api/ocs-catalogue/{sku}")
def get_ocs_catalogue_item(sku: str) -> dict:
    """Detail view for one product. Returns all per-manager ratings + comments
    plus aggregate. SKU here can be a Cova SKU or an OCS variant number — we
    resolve to the Cova SKU since that's what the rating tables key on."""
    with db() as conn:
        cur = conn.cursor()

        # Resolve sku → cova_sku (might already be one, might be ocs_variant)
        cur.execute("""
            SELECT sku FROM products WHERE sku = ? OR ocs_variant_number = ? LIMIT 1
        """, (sku, sku))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Product not found")
        cova_sku = row[0]

        # Catalog info via products → ocs_catalog
        cur.execute("""
            SELECT p.sku, p.name, p.brand, p.category, p.size, p.ocs_variant_number,
                   oc.product_name, oc.subcategory, oc.stock_status, oc.unit_price,
                   oc.pack_size, oc.thc_min, oc.thc_max, oc.cbd_min, oc.cbd_max
            FROM products p
            LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = p.ocs_variant_number
            WHERE p.sku = ?
        """, (cova_sku,))
        cat_row = cur.fetchone()
        cat_cols = [d[0] for d in cur.description]
        product = dict(zip(cat_cols, cat_row)) if cat_row else {}

        # All ratings for this SKU
        cur.execute("""
            SELECT location_id, author_name, rating, notes, updated_at
            FROM product_ratings WHERE sku = ?
            ORDER BY updated_at DESC
        """, (cova_sku,))
        rcols = [d[0] for d in cur.description]
        ratings = [dict(zip(rcols, row)) for row in cur.fetchall()]

        # All comments
        cur.execute("""
            SELECT id, location_id, author_name, body, created_at
            FROM product_comments WHERE sku = ?
            ORDER BY created_at DESC
        """, (cova_sku,))
        ccols = [d[0] for d in cur.description]
        comments = [dict(zip(ccols, row)) for row in cur.fetchall()]

        # Aggregate
        avg_rating = sum(r["rating"] for r in ratings) / len(ratings) if ratings else 0
        return {
            "product": product,
            "ratings": ratings,
            "comments": comments,
            "aggregate": {
                "avg_rating": round(avg_rating, 1),
                "rating_count": len(ratings),
                "comment_count": len(comments),
            },
        }


@app.post("/api/ocs-catalogue/{sku}/rating")
def upsert_product_rating(sku: str, body: dict = Body(...)) -> dict:
    """Set or update a manager's rating for a product. Replaces any existing
    rating from the same author at the same location."""
    author = (body.get("author_name") or "").strip()
    if not author:
        raise HTTPException(status_code=400, detail="author_name required")
    rating = body.get("rating")
    try:
        rating = int(rating)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="rating must be an integer")
    if not 1 <= rating <= 10:
        raise HTTPException(status_code=400, detail="rating must be between 1 and 10")
    location_id = body.get("location_id") or "_global"
    notes = body.get("notes") or ""

    with db() as conn:
        cur = conn.cursor()
        # Resolve to Cova SKU
        cur.execute("SELECT sku FROM products WHERE sku = ? OR ocs_variant_number = ? LIMIT 1",
                    (sku, sku))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Product not found")
        cova_sku = row[0]

        cur.execute("""
            INSERT INTO product_ratings (sku, location_id, author_name, rating, notes)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (sku, location_id, author_name) DO UPDATE SET
                rating = excluded.rating,
                notes = excluded.notes,
                updated_at = CURRENT_TIMESTAMP
        """, (cova_sku, location_id, author, rating, notes))
        conn.commit()
    return {"ok": True}


@app.post("/api/ocs-catalogue/{sku}/comment")
def add_product_comment(sku: str, body: dict = Body(...)) -> dict:
    """Add a free-text comment from a manager. Append-only; comments stay in
    the historical record. Use rating/notes for the editable per-manager view."""
    author = (body.get("author_name") or "").strip()
    if not author:
        raise HTTPException(status_code=400, detail="author_name required")
    text = (body.get("body") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="body required")
    location_id = body.get("location_id")

    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT sku FROM products WHERE sku = ? OR ocs_variant_number = ? LIMIT 1",
                    (sku, sku))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Product not found")
        cova_sku = row[0]

        cur.execute("""
            INSERT INTO product_comments (sku, location_id, author_name, body)
            VALUES (?, ?, ?, ?)
        """, (cova_sku, location_id, author, text))
        cid = cur.lastrowid
        conn.commit()
    return {"ok": True, "id": cid}


@app.delete("/api/ocs-catalogue/comment/{comment_id}")
def delete_product_comment(comment_id: int) -> dict:
    """Delete a single comment by id. We don't soft-delete because comments
    are low-stakes and managers should be able to remove their own typos."""
    with db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM product_comments WHERE id = ?", (comment_id,))
        conn.commit()
    return {"ok": True, "deleted": cur.rowcount}


# ============================================================================
# Monthly Reports — package Cova exports for collective partners
# ============================================================================
# Each month, we send each data collective (IRCC, Canna Collective, Seeker)
# the source Cova exports per store. Today: download manually, rename, zip,
# email. With this tool: drop files into imports/monthly_reports/, click
# "Package", get a zip ready to email.
#
# Folder convention:
#   imports/monthly_reports/
#       2026-04/                         <-- year-month
#           Amherstview/                 <-- store name (matches Cova export filename)
#               2026_04_DiagnosticReport_Amherstview.csv
#               2026_04_SalesByProduct_Amherstview.xlsx
#           Bradford/
#               ...
#
# The "Package" endpoint zips a selected month's files — either all stores
# at once, or a single store. Per-collective filtering will come once we
# know what each one actually wants (waiting on user's historical examples).

import re as _re
import zipfile as _zipfile
import io as _io

MONTHLY_REPORTS_DIR = Path(__file__).parent.parent / "imports" / "monthly_reports"


def _ensure_reports_dir():
    """Create the monthly_reports root folder if missing. No-op if exists."""
    MONTHLY_REPORTS_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/api/monthly-reports/months")
def list_report_months() -> dict:
    """List year-months with at least one file present. Used by the UI to
    populate the 'Pick a month' dropdown."""
    _ensure_reports_dir()
    months = []
    for child in sorted(MONTHLY_REPORTS_DIR.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        # Only show folders matching YYYY-MM
        if not _re.match(r"^\d{4}-\d{2}$", child.name):
            continue
        # Count files to give the user a hint
        total_files = sum(1 for _ in child.rglob("*") if _.is_file())
        store_count = sum(1 for s in child.iterdir() if s.is_dir())
        months.append({
            "month": child.name,
            "store_count": store_count,
            "file_count": total_files,
        })
    return {"months": months}


@app.get("/api/monthly-reports/{month}")
def list_report_month_details(month: str) -> dict:
    """List the stores + files present for a given year-month.
    Used to show the user what's there before they click Package."""
    if not _re.match(r"^\d{4}-\d{2}$", month):
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")
    _ensure_reports_dir()
    month_dir = MONTHLY_REPORTS_DIR / month
    if not month_dir.exists():
        return {"month": month, "stores": []}

    stores = []
    for store_dir in sorted(month_dir.iterdir()):
        if not store_dir.is_dir():
            continue
        files = []
        for f in sorted(store_dir.iterdir()):
            if not f.is_file():
                continue
            files.append({
                "name": f.name,
                "size_bytes": f.stat().st_size,
                # Classify so the UI can show DR vs SBP vs other
                "report_type": _classify_report_file(f.name),
            })
        stores.append({"store": store_dir.name, "files": files})
    return {"month": month, "stores": stores}


def _classify_report_file(filename: str) -> str:
    """Guess what kind of report this file is from the filename.
    Used for UI display only — doesn't affect packaging."""
    lower = filename.lower()
    if "diagnostic" in lower:
        return "diagnostic"
    if "sales" in lower and ("product" in lower or "byproduct" in lower):
        return "sales_by_product"
    if "inventory" in lower or "onhand" in lower:
        return "inventory"
    return "other"


@app.get("/api/monthly-reports/{month}/package")
def package_month_zip(
    month: str,
    store: str | None = None,
    collective: str | None = None,
) -> StreamingResponse:
    """Build a zip of the month's report files.

    store  - optional, single store name. If omitted: all stores included.
    collective - optional label for the zip filename (purely cosmetic for now;
                 per-collective filtering will be added once we know what
                 each wants).

    The zip preserves the per-store folder structure inside.
    """
    if not _re.match(r"^\d{4}-\d{2}$", month):
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")
    _ensure_reports_dir()
    month_dir = MONTHLY_REPORTS_DIR / month
    if not month_dir.exists():
        raise HTTPException(status_code=404,
                            detail=f"No files for {month}. Drop files in imports/monthly_reports/{month}/")

    # Build zip in memory (file sizes are small enough — sub-MB per file)
    buf = _io.BytesIO()
    file_count = 0
    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
        if store:
            store_dir = month_dir / store
            if not store_dir.exists():
                raise HTTPException(status_code=404,
                                    detail=f"Store '{store}' has no files for {month}")
            for f in sorted(store_dir.rglob("*")):
                if f.is_file():
                    arcname = f"{store}/{f.name}"
                    zf.write(f, arcname)
                    file_count += 1
        else:
            for store_dir in sorted(month_dir.iterdir()):
                if not store_dir.is_dir():
                    continue
                for f in sorted(store_dir.iterdir()):
                    if f.is_file():
                        arcname = f"{store_dir.name}/{f.name}"
                        zf.write(f, arcname)
                        file_count += 1

    if file_count == 0:
        raise HTTPException(status_code=404, detail="No files matched")

    buf.seek(0)
    # Filename convention: SBInsights_{collective}_{month}_{scope}.zip
    scope = store or "AllStores"
    collective_part = f"_{collective}" if collective else ""
    fname = f"SBInsights{collective_part}_{month}_{scope}.zip"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ============================================================================
# Market Intelligence — OCS regional/municipal benchmarks
# ============================================================================
# Backs the Market Intelligence tab. Provides:
#   - List of imports (per store, per period)
#   - Trigger import from imports/market_intelligence/ folder
#   - Gap Report (top municipality SKUs you don't carry)
#   - Performance Comparison (SKUs you carry vs municipality average)
#
# Data is store-scoped. Always pass location_id to scope queries.

MARKET_INTEL_DIR = Path(__file__).parent.parent / "imports" / "market_intelligence"


def _ensure_market_intel_dir():
    MARKET_INTEL_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/api/market-intelligence/imports")
def list_market_intelligence_imports(location_id: str | None = None) -> dict:
    """List all imports, optionally filtered to a single store.
    Used to populate the 'pick an import' dropdown."""
    with db() as conn:
        cur = conn.cursor()
        sql = """
            SELECT mi.id, mi.location_id, l.name AS store_name,
                   mi.period_start, mi.period_end, mi.period_days,
                   mi.sku_count, mi.has_municipality, mi.imported_at,
                   mi.source_files
            FROM market_intelligence_imports mi
            LEFT JOIN locations l ON l.id = mi.location_id
        """
        params: list = []
        if location_id:
            sql += " WHERE mi.location_id = ?"
            params.append(location_id)
        sql += " ORDER BY mi.imported_at DESC"
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        items = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {"count": len(items), "items": items}


@app.get("/api/market-intelligence/folders")
def list_market_intelligence_folders() -> dict:
    """List folders available for import from imports/market_intelligence/.
    Returns the {date}/{store} hierarchy so the UI can show import candidates."""
    _ensure_market_intel_dir()
    folders = []
    for date_dir in sorted(MARKET_INTEL_DIR.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        for store_dir in sorted(date_dir.iterdir()):
            if not store_dir.is_dir():
                continue
            files = [f.name for f in store_dir.iterdir() if f.is_file()]
            folders.append({
                "date": date_dir.name,
                "store_name": store_dir.name,
                "path": str(store_dir.relative_to(Path(__file__).parent.parent)),
                "files": files,
                "file_count": len(files),
            })
    return {"folders": folders}


@app.post("/api/market-intelligence/import")
def trigger_market_intelligence_import(payload: dict = Body(...)) -> dict:
    """Import a specific folder. Payload:
        store_name (required) — e.g. 'Bradford'
        date (required) — folder date 'YYYY-MM-DD'
        period_start, period_end (optional) — overrides
    """
    from jobs.import_market_intelligence import import_market_intelligence
    store_name = (payload.get("store_name") or "").strip()
    date_str = (payload.get("date") or "").strip()
    if not store_name or not date_str:
        raise HTTPException(status_code=400, detail="store_name and date required")

    folder = MARKET_INTEL_DIR / date_str / store_name
    if not folder.exists():
        raise HTTPException(status_code=404, detail=f"folder not found: {folder}")

    try:
        with db() as conn:
            result = import_market_intelligence(
                conn, folder,
                period_start=payload.get("period_start"),
                period_end=payload.get("period_end") or date_str,
            )
        return result
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/market-intelligence/import-all")
def trigger_bulk_market_intelligence_import(payload: dict = Body(default={})) -> dict:
    """Import every folder under imports/market_intelligence/ that has files.

    Returns a per-folder result list so the UI can show what succeeded and what
    failed without one bad folder blocking the others.

    Payload (optional):
      date (optional) — limit to a single year-month-date (YYYY-MM-DD)
                        to skip historical folders. If omitted, imports all.
    """
    from jobs.import_market_intelligence import import_market_intelligence
    _ensure_market_intel_dir()

    date_filter = (payload.get("date") or "").strip() or None
    period_start = payload.get("period_start")

    results = []
    success_count = 0
    fail_count = 0
    skip_count = 0

    for date_dir in sorted(MARKET_INTEL_DIR.iterdir()):
        if not date_dir.is_dir():
            continue
        if date_filter and date_dir.name != date_filter:
            continue
        for store_dir in sorted(date_dir.iterdir()):
            if not store_dir.is_dir():
                continue
            files = [f for f in store_dir.iterdir() if f.is_file()]
            if not files:
                results.append({
                    "date": date_dir.name, "store_name": store_dir.name,
                    "status": "skipped", "reason": "empty folder",
                })
                skip_count += 1
                continue
            try:
                with db() as conn:
                    result = import_market_intelligence(
                        conn, store_dir,
                        period_start=period_start,
                        period_end=date_dir.name,
                    )
                results.append({
                    "date": date_dir.name, "store_name": store_dir.name,
                    "status": "imported", "sku_count": result["sku_count"],
                    "has_municipality": result["has_municipality"],
                })
                success_count += 1
            except Exception as e:
                results.append({
                    "date": date_dir.name, "store_name": store_dir.name,
                    "status": "error", "error": str(e),
                })
                fail_count += 1

    return {
        "results": results,
        "success_count": success_count,
        "fail_count": fail_count,
        "skip_count": skip_count,
        "total": len(results),
    }


@app.get("/api/market-intelligence/gap-report")
def get_gap_report(
    location_id: str,
    import_id: int | None = None,
    limit: int = 100,
    subcategory: str | None = None,
    in_ocs_catalog_only: bool = True,
) -> dict:
    """Top municipality SKUs you DON'T carry.

    location_id is required (this is per-store).
    import_id defaults to the most recent import for the location.
    in_ocs_catalog_only filters to SKUs currently orderable from OCS.
    """
    with db() as conn:
        cur = conn.cursor()

        # Resolve import_id
        if import_id is None:
            cur.execute("""
                SELECT id FROM market_intelligence_imports
                WHERE location_id = ? ORDER BY imported_at DESC LIMIT 1
            """, (location_id,))
            row = cur.fetchone()
            if not row:
                return {"count": 0, "items": [], "warning": "No imports found for this store"}
            import_id = row[0]

        # Pull SKUs you don't carry, joined with OCS catalog for orderability
        sql = """
            SELECT mi.sku, mi.item_name, mi.brand, mi.supplier, mi.subcategory, mi.size,
                   mi.municipality_units, mi.municipality_velocity, mi.sales_days,
                   oc.unit_price, oc.pack_size, oc.stock_status, oc.thc_min, oc.thc_max
            FROM market_intelligence_data mi
            LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = mi.sku
            WHERE mi.import_id = ?
              AND mi.your_units IS NULL
              AND mi.municipality_units IS NOT NULL
        """
        params: list = [import_id]
        if subcategory:
            sql += " AND mi.subcategory = ?"
            params.append(subcategory)
        if in_ocs_catalog_only:
            sql += " AND oc.ocs_variant_number IS NOT NULL"
        sql += " ORDER BY mi.municipality_units DESC LIMIT ?"
        params.append(min(2000, max(1, int(limit))))
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        items = [dict(zip(cols, r)) for r in cur.fetchall()]

        # Get import metadata for context
        cur.execute("""
            SELECT period_start, period_end, period_days, imported_at
            FROM market_intelligence_imports WHERE id = ?
        """, (import_id,))
        meta = cur.fetchone()

    return {
        "count": len(items),
        "items": items,
        "import_id": import_id,
        "period_start": meta[0] if meta else None,
        "period_end": meta[1] if meta else None,
        "period_days": meta[2] if meta else None,
        "imported_at": meta[3] if meta else None,
    }


@app.get("/api/market-intelligence/performance-comparison")
def get_performance_comparison(
    location_id: str,
    import_id: int | None = None,
    limit: int = 200,
    subcategory: str | None = None,
) -> dict:
    """SKUs you DO carry, with your velocity vs municipality velocity.

    Useful for finding 'underweight' SKUs (you carry but stock too few) and
    'outperforming' SKUs (you carry and outsell peers).
    """
    with db() as conn:
        cur = conn.cursor()

        if import_id is None:
            cur.execute("""
                SELECT id FROM market_intelligence_imports
                WHERE location_id = ? ORDER BY imported_at DESC LIMIT 1
            """, (location_id,))
            row = cur.fetchone()
            if not row:
                return {"count": 0, "items": [], "warning": "No imports found for this store"}
            import_id = row[0]

        sql = """
            SELECT mi.sku, mi.item_name, mi.brand, mi.supplier, mi.subcategory, mi.size,
                   mi.your_units, mi.your_velocity,
                   mi.municipality_units, mi.municipality_velocity,
                   mi.sales_days,
                   oc.stock_status, oc.unit_price
            FROM market_intelligence_data mi
            LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = mi.sku
            WHERE mi.import_id = ?
              AND mi.your_units IS NOT NULL
              AND mi.municipality_units IS NOT NULL
        """
        params: list = [import_id]
        if subcategory:
            sql += " AND mi.subcategory = ?"
            params.append(subcategory)
        sql += " ORDER BY mi.municipality_units DESC LIMIT ?"
        params.append(min(2000, max(1, int(limit))))
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        items = [dict(zip(cols, r)) for r in cur.fetchall()]

        # Compute ratio + flag
        for it in items:
            yu = it.get("your_units") or 0
            mu = it.get("municipality_units") or 0
            if mu > 0:
                ratio = yu / mu
                it["units_ratio"] = round(ratio, 2)
                if ratio < 0.5:
                    it["performance_flag"] = "underweight"
                elif ratio > 1.5:
                    it["performance_flag"] = "outperforming"
                else:
                    it["performance_flag"] = "in_line"
            else:
                it["units_ratio"] = None
                it["performance_flag"] = None

        cur.execute("""
            SELECT period_start, period_end, period_days, imported_at
            FROM market_intelligence_imports WHERE id = ?
        """, (import_id,))
        meta = cur.fetchone()

    return {
        "count": len(items),
        "items": items,
        "import_id": import_id,
        "period_start": meta[0] if meta else None,
        "period_end": meta[1] if meta else None,
        "period_days": meta[2] if meta else None,
        "imported_at": meta[3] if meta else None,
    }


# ----------------------------------------------------------------------------
# Suggested Additions — peer-popular SKUs you don't carry, surfaced in Reorder
# ----------------------------------------------------------------------------
# This is the heart of the gap-to-reorder integration. Each store has a list of
# SKUs that:
#   - Sell well at peer stores in the municipality (peer velocity ≥ floor)
#   - You don't currently carry (no on-hand, no recent sales)
#   - Aren't snoozed/dismissed by user action
#
# The Reorder Report displays the top N of these as "Suggested Additions"
# alongside the existing reorder recs, with a one-click snooze/dismiss/add.
#
# Honest design notes:
#   - Initial order qty = min(1 case, 7 days × peer velocity rounded to case)
#   - Conservative-by-default: never suggest more than 1 case unless peer
#     velocity is high enough that 7 days = 2+ cases.
#   - User actions (snooze/dismiss/add) per (location, sku) — Bradford might
#     dismiss a SKU that Amherstview wants to consider.

@app.get("/api/reorder/suggested-additions")
def get_suggested_additions(
    location_id: str,
    limit: int = 20,
    min_peer_velocity: float = 0.5,
    include_snoozed: bool = False,
) -> dict:
    """Top peer-popular SKUs the store doesn't carry.

    Filters:
      - Only includes SKUs where peer velocity >= min_peer_velocity (default 0.5/day)
      - Only includes SKUs in OCS catalog (orderable now)
      - Excludes snoozed/dismissed unless include_snoozed=True
      - Excludes SKUs the store has any sales activity for in the last 90 days
        (treated as "we tried this, just stocked out — handled by regular reorder")
    """
    today = date.today()
    today_iso = today.isoformat()
    ninety_ago = (today - timedelta(days=90)).isoformat()

    with db() as conn:
        cur = conn.cursor()

        # Find latest market intel import for this store
        cur.execute("""
            SELECT id, period_start, period_end, period_days, imported_at, has_municipality
            FROM market_intelligence_imports
            WHERE location_id = ?
            ORDER BY imported_at DESC LIMIT 1
        """, (location_id,))
        imp = cur.fetchone()
        if not imp:
            return {
                "count": 0, "items": [],
                "warning": "No market intelligence data imported for this store. "
                           "Drop OCS exports into imports/market_intelligence/ and import.",
            }
        import_id, p_start, p_end, p_days, imported_at, has_muni = imp
        if not has_muni:
            return {
                "count": 0, "items": [],
                "warning": "This store has no municipality benchmark data "
                           "(too few peer stores to anonymize).",
                "imported_at": imported_at,
            }

        # Pull candidates: in market intel, not carried by us, in OCS catalog
        # Also join the user-action status table (gap_suggestions_status) and
        # the recent sales check (anything we sold in last 90 days = we DO carry it).
        sql = """
            WITH recent_sold AS (
                SELECT DISTINCT sku FROM sales_daily
                WHERE location_id = ? AND sale_date >= ?
            ),
            current_stock AS (
                SELECT sku FROM inventory_snapshots WHERE location_id = ? AND on_hand > 0
                GROUP BY sku
            )
            SELECT mi.sku, mi.item_name, mi.brand, mi.supplier, mi.subcategory, mi.size,
                   mi.municipality_units, mi.municipality_velocity, mi.sales_days,
                   oc.unit_price, oc.pack_size, oc.stock_status, oc.thc_min, oc.thc_max,
                   gs.status, gs.snoozed_until, gs.note AS status_note,
                   gs.author_name AS status_author, gs.updated_at AS status_updated
            FROM market_intelligence_data mi
            INNER JOIN ocs_catalog oc ON oc.ocs_variant_number = mi.sku
            LEFT JOIN gap_suggestions_status gs
                ON gs.location_id = ? AND gs.sku = mi.sku
            LEFT JOIN products p ON p.ocs_variant_number = mi.sku
            LEFT JOIN recent_sold rs ON rs.sku = p.sku
            LEFT JOIN current_stock cs ON cs.sku = p.sku
            WHERE mi.import_id = ?
              AND mi.your_units IS NULL
              AND mi.municipality_velocity IS NOT NULL
              AND mi.municipality_velocity >= ?
              AND rs.sku IS NULL
              AND cs.sku IS NULL
        """
        params: list = [location_id, ninety_ago, location_id, location_id,
                        import_id, float(min_peer_velocity)]

        if not include_snoozed:
            # Hide dismissed entirely; hide snoozed unless snooze has expired
            sql += """
              AND (gs.status IS NULL
                   OR gs.status = 'added'
                   OR (gs.status = 'snoozed' AND gs.snoozed_until <= ?))
            """
            params.append(today_iso)

        sql += " ORDER BY mi.municipality_units DESC LIMIT ?"
        params.append(min(200, max(1, int(limit))))

        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        items = [dict(zip(cols, r)) for r in cur.fetchall()]

        # Compute suggested initial order qty per item.
        # Conservative trial-order policy:
        #   - Default: 1 case (commits to OCS minimum)
        #   - For pack=1 SKUs (singles): suggest min(7, ceil(peer_vel × 7)) units
        #     so we don't over-commit on high-velocity singles
        #   - Hard cap: never suggest more than 14 units OR 2 cases for an
        #     untested SKU (whichever is larger, since case sizes vary)
        # Once the SKU is stocked and you have your own velocity data,
        # the regular reorder engine takes over.
        for it in items:
            pack_size = it.get("pack_size") or 1
            peer_vel = it.get("municipality_velocity") or 0
            unit_price = it.get("unit_price") or 0

            if pack_size <= 1:
                # Singles: cap at 7 units OR ceil(peer × 7) — whichever is smaller
                seven_day_demand = math.ceil(peer_vel * 7)
                suggested_units = min(7, max(1, seven_day_demand))
                suggested_cases = suggested_units
            else:
                # Multi-unit cases: 1 case is the standard trial order.
                # Only go to 2 cases if peer velocity is very high (would cover < 3 days).
                cases_for_three_day = math.ceil((peer_vel * 3) / pack_size) if pack_size > 0 else 1
                suggested_cases = max(1, min(2, cases_for_three_day))
                suggested_units = suggested_cases * pack_size

            it["suggested_cases"] = suggested_cases
            it["suggested_units"] = suggested_units
            it["suggested_cost"] = round(unit_price * suggested_units, 2)

        # Rebate qualification — same resolver + field names as the reorder
        # rows, so the UI badge logic carries over. These SKUs usually aren't
        # in the products table (no store carries them), so pass the market-
        # intel brand/supplier as fallback meta — without it, brand-scoped
        # direct deals can't match and a blocked collective could show as
        # active.
        from jobs.data_revenue_resolver import get_active_deals_for_skus
        extra_meta = {
            it["sku"]: {"brand": it.get("brand"), "lp": it.get("supplier"),
                        "category": None, "subcategory": it.get("subcategory")}
            for it in items
        }
        deal_map = get_active_deals_for_skus(conn, set(extra_meta), extra_meta=extra_meta)
        for it in items:
            deal = deal_map.get(it["sku"])
            it["data_fee_pct"] = round(deal["percentage"], 2) if deal else None
            it["data_fee_partner"] = deal["partner"] if deal else None
            it["data_fee_basis"] = deal["basis"] if deal else None
            it["data_fee_is_direct"] = bool(deal and deal["is_direct"])

    return {
        "count": len(items),
        "items": items,
        "import_id": import_id,
        "period_start": p_start,
        "period_end": p_end,
        "period_days": p_days,
        "imported_at": imported_at,
        "min_peer_velocity": min_peer_velocity,
    }


@app.post("/api/reorder/suggested-additions/{sku}/action")
def update_gap_status(sku: str, payload: dict = Body(...)) -> dict:
    """Snooze, dismiss, or mark-as-added a suggested addition.

    Payload:
      location_id (required) — which store this decision applies to
      action (required) — 'snooze' | 'dismiss' | 'added' | 'reset'
      snooze_days (optional, default 90) — for 'snooze' only
      author_name (optional) — for audit
      note (optional)
    """
    location_id = (payload.get("location_id") or "").strip()
    action = (payload.get("action") or "").strip()
    if not location_id:
        raise HTTPException(status_code=400, detail="location_id required")
    if action not in {"snooze", "dismiss", "added", "reset"}:
        raise HTTPException(status_code=400,
                            detail="action must be snooze | dismiss | added | reset")

    author = (payload.get("author_name") or "").strip() or None
    note = payload.get("note") or None

    with db() as conn:
        cur = conn.cursor()
        if action == "reset":
            cur.execute("""
                DELETE FROM gap_suggestions_status WHERE location_id = ? AND sku = ?
            """, (location_id, sku))
            conn.commit()
            return {"ok": True, "action": "reset"}

        snoozed_until = None
        if action == "snooze":
            days = int(payload.get("snooze_days") or 90)
            snoozed_until = (date.today() + timedelta(days=days)).isoformat()

        # Map action to status value
        status_value = {"snooze": "snoozed", "dismiss": "dismissed", "added": "added"}[action]

        cur.execute("""
            INSERT INTO gap_suggestions_status
                (location_id, sku, status, snoozed_until, note, author_name)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (location_id, sku) DO UPDATE SET
                status = excluded.status,
                snoozed_until = excluded.snoozed_until,
                note = excluded.note,
                author_name = excluded.author_name,
                updated_at = CURRENT_TIMESTAMP
        """, (location_id, sku, status_value, snoozed_until, note, author))
        conn.commit()

    return {"ok": True, "action": action, "snoozed_until": snoozed_until}


@app.get("/api/reorder/suggested-additions/_history")
def get_gap_history(location_id: str, status: str | None = None) -> dict:
    """List previously-actioned gap suggestions (snoozed/dismissed/added).
    Useful for the 'Reviewed gaps' panel."""
    with db() as conn:
        cur = conn.cursor()
        sql = """
            SELECT gs.location_id, gs.sku, gs.status, gs.snoozed_until,
                   gs.note, gs.author_name, gs.updated_at,
                   oc.product_name AS item_name, oc.brand,
                   oc.subcategory, oc.unit_price, oc.pack_size
            FROM gap_suggestions_status gs
            LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = gs.sku
            WHERE gs.location_id = ?
        """
        params: list = [location_id]
        if status:
            sql += " AND gs.status = ?"
            params.append(status)
        sql += " ORDER BY gs.updated_at DESC"
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        items = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {"count": len(items), "items": items}


# ============================================================================
# App Settings — engine tuning UI backend
# ============================================================================
# These endpoints back the Admin → Settings tab. Settings are stored in
# `app_settings` and changes are logged to `app_settings_history` for audit.
#
# Validation:
#   - Each setting has min_value / max_value bounds. Writes outside the range
#     are rejected with 400.
#   - Type coercion: 'int' settings round to whole numbers, 'percent' is stored
#     as raw % (e.g. 50.0 not 0.5).
#
# Identity:
#   - Author name comes from the X-Author header (set by the dashboard from
#     localStorage). Falls back to "anonymous" for direct API calls.

@app.get("/api/settings")
def get_all_settings(request: Request = None) -> dict:
    """List all tunable settings, grouped by section, with current + default
    values. Any logged-in user may VIEW settings; only admins may change them
    (see update_setting / reset_setting)."""
    require_user(request)
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT key, value, default_value, min_value, max_value,
                   value_type, section, label, description, sort_order, updated_at
            FROM app_settings
            ORDER BY section, sort_order, label
        """)
        rows = []
        for r in cur.fetchall():
            rows.append({
                "key": r[0], "value": r[1], "default_value": r[2],
                "min_value": r[3], "max_value": r[4],
                "value_type": r[5], "section": r[6],
                "label": r[7], "description": r[8],
                "sort_order": r[9], "updated_at": r[10],
                "is_modified": r[1] != r[2],
            })
    # Group by section
    sections: dict[str, list] = {}
    for row in rows:
        sections.setdefault(row["section"], []).append(row)
    return {"settings": rows, "sections": sections}


@app.put("/api/settings/{key:path}")
def update_setting(key: str, payload: dict = Body(...), request: Request = None) -> dict:
    """Update one setting (ADMIN ONLY — non-admins get read-only access via
    GET). Validates against the row's min/max bounds. Logs the change to
    app_settings_history."""
    admin = require_admin(request)
    new_value = payload.get("value")
    if new_value is None:
        raise HTTPException(status_code=400, detail="value required")
    try:
        new_value = float(new_value)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="value must be a number")
    # Audit identity comes from the authenticated session, not the payload —
    # the old client-typed changed_by is kept only as a fallback label.
    actor = admin.get("email") or (payload.get("changed_by") or "").strip() or "anonymous"
    note = payload.get("note") or None

    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT value, min_value, max_value, value_type
            FROM app_settings WHERE key = ?
        """, (key,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"unknown setting '{key}'")
        old_value, min_v, max_v, vtype = row

        # Type-coerce
        if vtype == "int" or vtype == "days":
            new_value = float(int(round(new_value)))

        # Bounds check
        if min_v is not None and new_value < min_v:
            raise HTTPException(status_code=400,
                                detail=f"value {new_value} below min {min_v}")
        if max_v is not None and new_value > max_v:
            raise HTTPException(status_code=400,
                                detail=f"value {new_value} above max {max_v}")

        # Skip writes that don't change anything
        if old_value == new_value:
            return {"ok": True, "unchanged": True, "value": new_value}

        cur.execute("""
            UPDATE app_settings SET value = ?, updated_at = CURRENT_TIMESTAMP
            WHERE key = ?
        """, (new_value, key))
        cur.execute("""
            INSERT INTO app_settings_history (key, old_value, new_value, changed_by, note)
            VALUES (?, ?, ?, ?, ?)
        """, (key, old_value, new_value, actor, note))
        conn.commit()
    return {"ok": True, "value": new_value, "old_value": old_value}


@app.post("/api/settings/{key:path}/reset")
def reset_setting(key: str, payload: dict = Body(default={}), request: Request = None) -> dict:
    """Reset a setting back to its default value (ADMIN ONLY). Logged to history."""
    admin = require_admin(request)
    actor = admin.get("email") or (payload.get("changed_by") or "").strip() or "anonymous"
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value, default_value FROM app_settings WHERE key = ?", (key,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"unknown setting '{key}'")
        old_value, default_value = row
        if old_value == default_value:
            return {"ok": True, "unchanged": True, "value": default_value}
        cur.execute("""
            UPDATE app_settings SET value = ?, updated_at = CURRENT_TIMESTAMP
            WHERE key = ?
        """, (default_value, key))
        cur.execute("""
            INSERT INTO app_settings_history (key, old_value, new_value, changed_by, note)
            VALUES (?, ?, ?, ?, ?)
        """, (key, old_value, default_value, actor, "reset to default"))
        conn.commit()
    return {"ok": True, "value": default_value, "old_value": old_value}


@app.get("/api/settings/_history")
def get_settings_history(key: str | None = None, limit: int = 100,
                         request: Request = None) -> dict:
    """Audit trail of recent settings changes. Pass ?key=foo.bar to filter.
    Viewable by any logged-in user (read-only data)."""
    require_user(request)
    with db() as conn:
        cur = conn.cursor()
        if key:
            cur.execute("""
                SELECT id, key, old_value, new_value, changed_by, changed_at, note
                FROM app_settings_history WHERE key = ?
                ORDER BY changed_at DESC LIMIT ?
            """, (key, min(500, max(1, int(limit)))))
        else:
            cur.execute("""
                SELECT id, key, old_value, new_value, changed_by, changed_at, note
                FROM app_settings_history
                ORDER BY changed_at DESC LIMIT ?
            """, (min(500, max(1, int(limit))),))
        cols = [d[0] for d in cur.description]
        items = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {"count": len(items), "items": items}


# ---- Data revenue deals ----

@app.get("/api/data-revenue-deals")
def list_data_revenue_deals(brand_id: int | None = None, active_on: str | None = None,
                            include_archived: bool = False) -> list[dict]:
    """
    List data revenue deals. Optional filters:
        brand_id  -- only deals for this brand
        active_on -- only deals active on this date (YYYY-MM-DD)
        include_archived -- if False (default), hide deals where end_date < today
    """
    from datetime import date as _date
    today_iso = _date.today().isoformat()
    where = []
    params = []
    if brand_id is not None:
        where.append("d.brand_id = ?"); params.append(brand_id)
    if active_on:
        where.append("d.start_date <= ? AND (d.end_date IS NULL OR d.end_date >= ?)")
        params.extend([active_on, active_on])
    if not include_archived:
        # Hide deals whose end_date is in the past. NULL end_date = open-ended, keep visible.
        where.append("(d.end_date IS NULL OR d.end_date >= ?)")
        params.append(today_iso)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with db() as conn:
        cur = conn.cursor()
        # Category resolved via the SKU filter — OCS catalogue first (76%
        # coverage on current deals), fall back to products.category. NULL
        # when neither maps. Keeps the join in SQL so the frontend doesn't
        # need a second round-trip.
        cur.execute(f"""
            SELECT d.id, d.brand_id, b.brand_name, d.start_date, d.end_date,
                   d.percentage, d.basis, d.sku_filter, d.notes, d.created_at,
                   COALESCE(oc.category, p.category) AS category
            FROM data_revenue_deals d
            LEFT JOIN brand_partners b ON b.id = d.brand_id
            LEFT JOIN ocs_catalog   oc ON oc.ocs_variant_number = d.sku_filter
            LEFT JOIN products      p  ON p.ocs_variant_number  = d.sku_filter
            {where_sql}
            ORDER BY d.start_date DESC, d.id DESC
        """, params)
        out = []
        for r in cur.fetchall():
            is_archived = (r[4] is not None) and (r[4] < today_iso)
            out.append({
                "id": r[0], "brand_id": r[1], "brand_name": r[2],
                "start_date": r[3], "end_date": r[4],
                "percentage": r[5], "basis": r[6],
                "sku_filter": r[7], "notes": r[8], "created_at": r[9],
                "category": r[10],
                "is_archived": is_archived,
            })
        return out


@app.post("/api/data-revenue-deals")
def create_data_revenue_deal(payload: dict = Body(...),
                             _admin: dict = Depends(require_admin)) -> dict:
    brand_id = payload.get("brand_id")
    start_date = payload.get("start_date")
    percentage = payload.get("percentage")
    basis = payload.get("basis")
    if not (brand_id and start_date and percentage is not None and basis):
        raise HTTPException(status_code=400,
                            detail="brand_id, start_date, percentage, basis are required")
    if basis not in ("retail_sales", "wholesale_cost", "gross_profit", "units_sold"):
        raise HTTPException(status_code=400,
                            detail="basis must be one of: retail_sales, wholesale_cost, gross_profit, units_sold")
    if not (0 <= float(percentage) <= 100):
        raise HTTPException(status_code=400, detail="percentage must be 0..100")
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO data_revenue_deals (brand_id, start_date, end_date, percentage, basis, sku_filter, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            brand_id, start_date, payload.get("end_date"),
            percentage, basis,
            payload.get("sku_filter"), payload.get("notes"),
        ))
        conn.commit()
        return {"id": cur.lastrowid}


@app.put("/api/data-revenue-deals/{deal_id}")
def update_data_revenue_deal(deal_id: int, payload: dict = Body(...),
                             _admin: dict = Depends(require_admin)) -> dict:
    fields, params = [], []
    valid = ("brand_id", "start_date", "end_date", "percentage", "basis", "sku_filter", "notes")
    for k in valid:
        if k in payload:
            fields.append(f"{k} = ?"); params.append(payload[k])
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    params.append(deal_id)
    with db() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE data_revenue_deals SET {', '.join(fields)} WHERE id = ?", params)
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="deal not found")
        conn.commit()
    return {"ok": True, "id": deal_id}


@app.delete("/api/data-revenue-deals/{deal_id}")
def delete_data_revenue_deal(deal_id: int,
                             _admin: dict = Depends(require_admin)) -> dict:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM data_revenue_deals WHERE id = ?", (deal_id,))
        conn.commit()
    return {"ok": True, "deleted": cur.rowcount}


# ---- LTOs ----

@app.get("/api/ltos")
def list_ltos(brand_id: int | None = None, lp_id: int | None = None,
              active_on: str | None = None,
              include_archived: bool = False,
              include_skus: bool = True) -> list[dict]:
    """
    List LTOs. Filters:
      - brand_id: filter to LTOs tied to this brand_partner
      - lp_id: filter to LTOs tied to this LP (licensed producer)
      - active_on: date — return LTOs active on that date
      - include_archived: if False (default), hide LTOs with end_date < today
    When include_skus is True, each LTO has an `applicable_skus` list (empty
    list = applies via brand/category scope rules, not SKU junction).
    """
    from datetime import date as _date
    today_iso = _date.today().isoformat()
    where = []
    params = []
    if brand_id is not None:
        where.append("l.brand_id = ?"); params.append(brand_id)
    if lp_id is not None:
        where.append("l.lp_id = ?"); params.append(lp_id)
    if active_on:
        where.append("l.start_date <= ? AND l.end_date >= ?")
        params.extend([active_on, active_on])
    if not include_archived:
        # Hide LTOs whose end_date is in the past (auto-archived)
        where.append("l.end_date >= ?")
        params.append(today_iso)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT l.id, l.name, l.brand_id, b.brand_name, l.start_date, l.end_date,
                   l.lto_type, l.discount_per_unit, l.rebate_threshold,
                   l.rebate_percentage, l.notes, l.is_active, l.created_at,
                   l.lp_id, l.rate_basis, l.applies_to_brand,
                   l.applies_to_category, l.applies_to_subcategory, l.discount_pct
            FROM ltos l
            LEFT JOIN brand_partners b ON b.id = l.brand_id
            {where_sql}
            ORDER BY l.start_date DESC, l.id DESC
        """, params)
        ltos = []
        for r in cur.fetchall():
            is_archived = (r[5] or "") < today_iso  # end_date < today
            ltos.append({
                "id": r[0], "name": r[1], "brand_id": r[2], "brand_name": r[3],
                "start_date": r[4], "end_date": r[5], "lto_type": r[6],
                "discount_per_unit": r[7], "rebate_threshold": r[8],
                "rebate_percentage": r[9], "notes": r[10],
                "is_active": bool(r[11]), "created_at": r[12],
                "lp_id": r[13], "rate_basis": r[14],
                "applies_to_brand": r[15], "applies_to_category": r[16],
                "applies_to_subcategory": r[17], "discount_pct": r[18],
                "is_archived": is_archived,
            })
        if include_skus and ltos:
            ids = [str(l["id"]) for l in ltos]
            cur.execute(f"""
                SELECT lto_id, sku FROM lto_skus
                WHERE lto_id IN ({",".join("?" * len(ids))})
            """, ids)
            sku_map = {}
            for lid, sku in cur.fetchall():
                sku_map.setdefault(lid, []).append(sku)
            for l in ltos:
                l["applicable_skus"] = sku_map.get(l["id"], [])
    return ltos


@app.post("/api/ltos")
def create_lto(payload: dict = Body(...)) -> dict:
    name = (payload.get("name") or "").strip()
    start_date = payload.get("start_date")
    end_date = payload.get("end_date")
    lto_type = payload.get("lto_type") or "wholesale_discount"
    if not (name and start_date and end_date):
        raise HTTPException(status_code=400,
                            detail="name, start_date, end_date are required")
    if lto_type not in ("wholesale_discount", "volume_rebate", "promo_credit", "feature_flag"):
        raise HTTPException(status_code=400, detail="invalid lto_type")
    skus = payload.get("applicable_skus", []) or []
    if not isinstance(skus, list):
        raise HTTPException(status_code=400, detail="applicable_skus must be a list of SKU strings")
    # New scope fields. At least one of (skus list, applies_to_brand) should be set.
    applies_to_brand = (payload.get("applies_to_brand") or "").strip() or None
    applies_to_category = (payload.get("applies_to_category") or "").strip() or None
    applies_to_subcategory = (payload.get("applies_to_subcategory") or "").strip() or None
    if not skus and not applies_to_brand:
        raise HTTPException(status_code=400,
                            detail="Either applicable_skus or applies_to_brand must be specified — an LTO with no scope would never match")
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO ltos (name, brand_id, lp_id, start_date, end_date, lto_type,
                              discount_per_unit, rebate_threshold, rebate_percentage,
                              rate_basis, rate_percentage, discount_pct,
                              applies_to_brand, applies_to_category, applies_to_subcategory,
                              notes, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            name, payload.get("brand_id"), payload.get("lp_id"),
            start_date, end_date, lto_type,
            payload.get("discount_per_unit"),
            payload.get("rebate_threshold"),
            payload.get("rebate_percentage"),
            payload.get("rate_basis"),
            payload.get("rate_percentage"),
            payload.get("discount_pct"),
            applies_to_brand, applies_to_category, applies_to_subcategory,
            payload.get("notes"),
            1 if payload.get("is_active", True) else 0,
        ))
        lto_id = cur.lastrowid
        if skus:
            cur.executemany("INSERT INTO lto_skus (lto_id, sku) VALUES (?, ?)",
                            [(lto_id, s) for s in skus])
        conn.commit()
        return {"id": lto_id, "applicable_skus": skus}


@app.put("/api/ltos/{lto_id}")
def update_lto(lto_id: int, payload: dict = Body(...)) -> dict:
    fields, params = [], []
    valid = ("name", "brand_id", "lp_id", "start_date", "end_date", "lto_type",
             "discount_per_unit", "rebate_threshold", "rebate_percentage",
             "rate_basis", "rate_percentage", "discount_pct",
             "applies_to_brand", "applies_to_category", "applies_to_subcategory",
             "notes", "is_active")
    for k in valid:
        if k in payload:
            v = payload[k]
            if k == "is_active":
                v = 1 if v else 0
            fields.append(f"{k} = ?"); params.append(v)
    with db() as conn:
        cur = conn.cursor()
        if fields:
            params.append(lto_id)
            cur.execute(f"UPDATE ltos SET {', '.join(fields)} WHERE id = ?", params)
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="LTO not found")
        # If the payload has applicable_skus, replace the linked SKUs
        if "applicable_skus" in payload:
            skus = payload.get("applicable_skus") or []
            cur.execute("DELETE FROM lto_skus WHERE lto_id = ?", (lto_id,))
            if skus:
                cur.executemany("INSERT INTO lto_skus (lto_id, sku) VALUES (?, ?)",
                                [(lto_id, s) for s in skus])
        conn.commit()
    return {"ok": True, "id": lto_id}


@app.delete("/api/ltos/{lto_id}")
def delete_lto(lto_id: int) -> dict:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM ltos WHERE id = ?", (lto_id,))
        conn.commit()
    return {"ok": True, "deleted": cur.rowcount}


# ============================================================================
# Data LP Partners — ongoing rebate agreements (replaces is_direct_deal flag)
# ============================================================================
# Distinct from collective deals (data_revenue_deals) and from LTOs.
# A Data LP Partner is an ongoing arrangement: "this brand/LP pays us X% of
# Y on an ongoing basis." Multiple rate types supported.

@app.get("/api/data-lp-partners")
def list_data_lp_partners(include_inactive: bool = False) -> dict:
    """List all Data LP Partner agreements."""
    where = []
    if not include_inactive:
        where.append("is_active = 1")
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT id, partner_name, scope_type, scope_value, rate_type,
                   rate_value, start_date, end_date, notes, is_active, created_at
            FROM data_lp_partner_agreements
            {where_sql}
            ORDER BY partner_name, start_date DESC
        """)
        cols = [d[0] for d in cur.description]
        items = [dict(zip(cols, r)) for r in cur.fetchall()]
        for it in items:
            it["is_active"] = bool(it["is_active"])
    return {"count": len(items), "items": items}


@app.post("/api/data-lp-partners")
def create_data_lp_partner(payload: dict = Body(...)) -> dict:
    """Create a new Data LP Partner agreement."""
    partner_name = (payload.get("partner_name") or "").strip()
    scope_type = (payload.get("scope_type") or "brand").strip()
    scope_value = (payload.get("scope_value") or "").strip()
    rate_type = (payload.get("rate_type") or "").strip()
    rate_value = payload.get("rate_value")
    start_date = payload.get("start_date")
    if not (partner_name and scope_value and rate_type and start_date and rate_value is not None):
        raise HTTPException(status_code=400,
                            detail="partner_name, scope_value, rate_type, rate_value, start_date required")
    if scope_type not in ("brand", "lp"):
        raise HTTPException(status_code=400, detail="scope_type must be 'brand' or 'lp'")
    if rate_type not in ("pct_wholesale", "pct_retail", "pct_gross_margin",
                         "flat_per_month", "flat_per_unit"):
        raise HTTPException(status_code=400, detail="invalid rate_type")
    try:
        rate_value = float(rate_value)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="rate_value must be numeric")
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO data_lp_partner_agreements
                (partner_name, scope_type, scope_value, rate_type, rate_value,
                 start_date, end_date, notes, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            partner_name, scope_type, scope_value, rate_type, rate_value,
            start_date, payload.get("end_date"),
            payload.get("notes"),
            1 if payload.get("is_active", True) else 0,
        ))
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}


@app.put("/api/data-lp-partners/{agreement_id}")
def update_data_lp_partner(agreement_id: int, payload: dict = Body(...)) -> dict:
    """Update a Data LP Partner agreement."""
    valid = ("partner_name", "scope_type", "scope_value", "rate_type",
             "rate_value", "start_date", "end_date", "notes", "is_active")
    fields, params = [], []
    for k in valid:
        if k in payload:
            v = payload[k]
            if k == "is_active":
                v = 1 if v else 0
            fields.append(f"{k} = ?"); params.append(v)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    params.append(agreement_id)
    with db() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE data_lp_partner_agreements SET {', '.join(fields)} WHERE id = ?", params)
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Agreement not found")
        conn.commit()
    return {"ok": True, "id": agreement_id}


@app.delete("/api/data-lp-partners/{agreement_id}")
def delete_data_lp_partner(agreement_id: int) -> dict:
    """Delete a Data LP Partner agreement."""
    with db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM data_lp_partner_agreements WHERE id = ?", (agreement_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Agreement not found")
        conn.commit()
    return {"ok": True}


@app.get("/api/order-outcomes/invoices")
def list_invoices_for_outcomes(
    store: str | None = None,
    limit: int = 50,
) -> dict:
    """
    List recent invoices (most recent first), suitable for the Order Outcome
    UI's invoice picker. Includes basic stats per invoice for quick scanning.
    """
    where_parts = []
    params: list = []
    if store:
        where_parts.append("location_id = ?")
        params.append(store)
    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    with db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT invoice_no, location_id, invoice_date, line_count,
                   units_total, subtotal, total_with_tax
            FROM invoices {where_sql}
            ORDER BY invoice_date DESC, invoice_no
            LIMIT ?
        """, params + [int(limit)])
        rows = [
            {"invoice_no": r[0], "location_id": r[1], "invoice_date": r[2],
             "line_count": r[3], "units_total": r[4],
             "subtotal": r[5], "total_with_tax": r[6]}
            for r in cur.fetchall()
        ]
    return {"count": len(rows), "invoices": rows}


@app.get("/api/order-outcomes/invoice/{invoice_no}")
def invoice_outcome(invoice_no: str, window_days: int = 30) -> dict:
    """
    Per-invoice sell-through analysis.
    For each line: how many units sold in the N days after delivery,
    annotated with whether the engine would have recommended this SKU
    at the time. Aggregated stats split engine-recommended vs manager-only.
    """
    if window_days < 1 or window_days > 365:
        raise HTTPException(status_code=400, detail="window_days must be 1..365")

    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT invoice_no, location_id, invoice_date, line_count,
                   units_total, subtotal, total_with_tax
            FROM invoices WHERE invoice_no = ?
        """, (invoice_no,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"invoice {invoice_no} not found")
        invoice_meta = {
            "invoice_no": row[0], "location_id": row[1], "invoice_date": row[2],
            "line_count": row[3], "units_total": row[4],
            "subtotal": row[5], "total_with_tax": row[6],
        }
        if not invoice_meta["location_id"]:
            raise HTTPException(
                status_code=400,
                detail="invoice has no location_id; cannot run outcome analysis"
            )

        inv_date = date.fromisoformat(invoice_meta["invoice_date"])
        window_end = (inv_date + timedelta(days=window_days)).isoformat()
        # Cap at today's actual data so we don't claim 30 days of post-data we don't have
        latest = get_latest_sale_date(conn)
        if latest and window_end > latest.isoformat():
            window_end = latest.isoformat()
        actual_window_days = (date.fromisoformat(window_end) - inv_date).days

        cur.execute(_outcome_sql(window_days), (
            invoice_meta["location_id"], inv_date.isoformat(), window_end, invoice_no
        ))
        raw_lines = cur.fetchall()

        engine_decisions = _build_engine_decision_lookup(
            conn, invoice_meta["location_id"], inv_date
        )

    lines = []
    for r in raw_lines:
        (variant, desc, units_del, unit_price, line_total, cova_sku,
         category, brand, units_after, revenue_after) = r
        units_after = int(units_after or 0)
        units_del = int(units_del or 0)
        engine_rec = engine_decisions.get(variant, False)
        sell_through = (100.0 * units_after / units_del) if units_del else 0
        lines.append({
            "ocs_variant": variant,
            "description": desc,
            "category": category,
            "brand": brand,
            "cova_sku": cova_sku,
            "units_delivered": units_del,
            "units_sold_after": units_after,
            "sell_through_pct": round(sell_through, 1),
            "revenue_after": round(revenue_after or 0, 2),
            "line_total": round(line_total or 0, 2),
            "dead_at_window": units_after == 0,
            "engine_would_recommend": engine_rec,
        })

    # Bucketed summary
    def bucket_stats(rows: list) -> dict:
        if not rows:
            return {"lines": 0, "units_ordered": 0, "units_sold": 0,
                    "sell_through_pct": 0, "dead_lines": 0, "cost": 0}
        units_ord = sum(r["units_delivered"] for r in rows)
        units_sld = sum(r["units_sold_after"] for r in rows)
        return {
            "lines": len(rows),
            "units_ordered": units_ord,
            "units_sold": units_sld,
            "sell_through_pct": round(100 * units_sld / units_ord, 1) if units_ord else 0,
            "dead_lines": sum(1 for r in rows if r["dead_at_window"]),
            "cost": round(sum(r["line_total"] for r in rows), 2),
        }

    engine_recs = [l for l in lines if l["engine_would_recommend"]]
    manager_only = [l for l in lines if not l["engine_would_recommend"]]

    return {
        "invoice": invoice_meta,
        "window_days_requested": window_days,
        "window_days_actual": actual_window_days,
        "summary": {
            "all": bucket_stats(lines),
            "engine_recommended": bucket_stats(engine_recs),
            "manager_only": bucket_stats(manager_only),
        },
        "lines": lines,
    }


@app.get("/api/order-outcomes/sku-rolling")
def sku_rolling_outcomes(
    store: str | None = None,
    window_days: int = 30,
    min_deliveries: int = 2,
    limit: int = 200,
) -> dict:
    """
    Per-SKU rolling sell-through across all imported invoices.
    Identifies products that consistently underperform OR overperform.
    A SKU appears here if it's been delivered at least min_deliveries times.
    """
    if window_days < 1 or window_days > 365:
        raise HTTPException(status_code=400, detail="window_days must be 1..365")

    with db() as conn:
        cur = conn.cursor()
        # Get all invoice lines with the location and date
        where = "WHERE i.location_id IS NOT NULL"
        params: list = []
        if store:
            where += " AND i.location_id = ?"
            params.append(store)
        cur.execute(f"""
            SELECT il.ocs_variant, il.description, il.units_delivered,
                   il.line_total, i.invoice_date, i.location_id, i.invoice_no
            FROM invoice_lines il
            JOIN invoices i ON i.invoice_no = il.invoice_no
            {where}
            ORDER BY il.ocs_variant, i.invoice_date
        """, params)
        all_lines = cur.fetchall()

        latest = get_latest_sale_date(conn)
        latest_iso = latest.isoformat() if latest else date.today().isoformat()

        # For efficiency: pull post-delivery sales in one pass per (variant, location)
        # Variants are reused across stores so we key by both.
        variant_loc_pairs = {(r[0], r[5]) for r in all_lines}
        # Pull all sales joined to products → variant for the relevant locations
        if not variant_loc_pairs:
            return {"count": 0, "items": []}

        # Group lines by (variant, location)
        from collections import defaultdict
        deliveries = defaultdict(list)  # (variant, loc) -> list of {date, units, cost}
        for variant, desc, units, line_total, inv_date, loc, inv_no in all_lines:
            deliveries[(variant, loc)].append({
                "date": inv_date,
                "units": int(units or 0),
                "cost": float(line_total or 0),
                "desc": desc,
                "invoice_no": inv_no,
            })

        # For each (variant, loc), compute total units sold in the union of all
        # post-delivery windows. To keep it accurate we compute per-delivery
        # then aggregate.
        results = []
        # Pre-fetch sales by (sku, location, date) — this could be heavy on
        # very large invoice sets, but acceptable for typical scales.
        cur.execute("""
            SELECT p.ocs_variant_number, sd.location_id, sd.sale_date, sd.units_sold
            FROM sales_daily sd
            JOIN products p ON p.sku = sd.sku
            WHERE p.ocs_variant_number IS NOT NULL
        """)
        sales_index = defaultdict(list)  # (variant, loc) -> [(date, units), ...]
        for variant, loc, sdate, units in cur.fetchall():
            sales_index[(variant, loc)].append((sdate, units))

    for (variant, loc), delivs in deliveries.items():
        if len(delivs) < min_deliveries:
            continue
        sales_for_pair = sales_index.get((variant, loc), [])

        # For each delivery, compute units sold in the window after it
        total_delivered = 0
        total_sold_after = 0
        total_cost = 0.0
        delivs_dead_count = 0
        for d in delivs:
            d_date = date.fromisoformat(d["date"])
            window_end_date = min(d_date + timedelta(days=window_days),
                                  date.fromisoformat(latest_iso))
            window_end_iso = window_end_date.isoformat()
            # Sum sales in window
            sold = sum(units for sdate, units in sales_for_pair
                       if d["date"] <= sdate < window_end_iso)
            d["units_sold_after"] = sold
            d["sell_through_pct"] = (100 * sold / d["units"]) if d["units"] else 0
            total_delivered += d["units"]
            total_sold_after += sold
            total_cost += d["cost"]
            if sold == 0 and d["units"] > 0:
                delivs_dead_count += 1

        results.append({
            "ocs_variant": variant,
            "location_id": loc,
            "description": delivs[0]["desc"],
            "deliveries": len(delivs),
            "total_units_delivered": total_delivered,
            "total_units_sold": total_sold_after,
            "rolling_sell_through_pct": round(100 * total_sold_after / total_delivered, 1)
                                         if total_delivered else 0,
            "dead_deliveries": delivs_dead_count,
            "total_cost": round(total_cost, 2),
            "first_delivery": delivs[0]["date"],
            "last_delivery": delivs[-1]["date"],
        })

    # Sort by total cost descending — biggest dollars first
    results.sort(key=lambda x: x["total_cost"], reverse=True)
    return {
        "count": len(results),
        "window_days": window_days,
        "min_deliveries": min_deliveries,
        "items": results[:limit],
    }


# ---------------------------------------------------------------------------
# Analytics — sales performance with date range + YoY comparison
# ---------------------------------------------------------------------------

def _resolve_date_range(preset: str, custom_start: str | None,
                       custom_end: str | None, today: date) -> tuple[date, date]:
    """Convert a preset string (or 'custom') into (start, end) inclusive dates."""
    if preset == "custom":
        if not custom_start or not custom_end:
            raise HTTPException(status_code=400, detail="custom range requires start and end")
        return date.fromisoformat(custom_start), date.fromisoformat(custom_end)

    # Common business shortcuts
    if preset == "today":
        return today, today
    if preset == "yesterday":
        d = today - timedelta(days=1); return d, d
    if preset == "wtd":  # week-to-date, Monday-based
        start = today - timedelta(days=today.weekday())
        return start, today
    if preset == "last7":
        return today - timedelta(days=6), today
    if preset == "last30":
        return today - timedelta(days=29), today
    if preset == "mtd":
        return today.replace(day=1), today
    if preset == "last_month":
        first_of_this = today.replace(day=1)
        last_of_prev = first_of_this - timedelta(days=1)
        first_of_prev = last_of_prev.replace(day=1)
        return first_of_prev, last_of_prev
    if preset == "qtd":
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        return date(today.year, q_start_month, 1), today
    if preset == "last_quarter":
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        first_of_this_q = date(today.year, q_start_month, 1)
        last_of_prev_q = first_of_this_q - timedelta(days=1)
        prev_q_start_month = ((last_of_prev_q.month - 1) // 3) * 3 + 1
        return date(last_of_prev_q.year, prev_q_start_month, 1), last_of_prev_q
    if preset == "ytd":
        return date(today.year, 1, 1), today
    if preset == "last12":
        return today - timedelta(days=365), today
    raise HTTPException(status_code=400, detail=f"unknown preset: {preset}")


def _shift_period_back_one_year(start: date, end: date) -> tuple[date, date]:
    """Return same date range one year earlier. Handles Feb 29 by clamping."""
    def safe_subtract(d: date) -> date:
        try:
            return d.replace(year=d.year - 1)
        except ValueError:
            # Feb 29 in a non-leap year — clamp to Feb 28
            return d.replace(year=d.year - 1, day=28)
    return safe_subtract(start), safe_subtract(end)


def _query_period_metrics(conn, start: date, end: date,
                          store: str | None) -> dict:
    """
    Aggregate sale_lines metrics for the given period, grouped by location.
    Returns {location_id: metrics_dict, '_total': metrics_dict}.

    Joins to ocs_catalog for wholesale cost (margin computation).
    Coverage caveat: not all SKUs have OCS variant linkage, so margin
    is computed only on the subset where it's available — we report coverage.
    """
    where = ["sl.sale_date >= ?", "sl.sale_date <= ?"]
    params: list = [start.isoformat(), end.isoformat()]
    if store:
        where.append("sl.location_id = ?"); params.append(store)
    where_sql = " AND ".join(where)

    cur = conn.cursor()
    cur.execute(f"""
        WITH agg AS (
            SELECT
                sl.location_id,
                COUNT(DISTINCT sl.invoice_no)                AS transactions,
                SUM(sl.units)                                AS units,
                SUM(sl.subtotal)                             AS revenue,
                SUM(sl.units * COALESCE(sl.regular_price, sl.sold_price, 0)) AS regular_total,
                SUM(sl.discount_amount)                      AS discount_total,
                SUM(CASE WHEN oc.unit_price IS NOT NULL
                         THEN sl.subtotal ELSE 0 END)        AS revenue_with_cost,
                SUM(CASE WHEN oc.unit_price IS NOT NULL
                         THEN sl.units * oc.unit_price ELSE 0 END) AS cost_total
            FROM sale_lines sl
            LEFT JOIN products p ON p.sku = sl.sku
            LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = p.ocs_variant_number
            WHERE {where_sql}
            GROUP BY sl.location_id
        )
        SELECT
            location_id, transactions, units, revenue, regular_total,
            discount_total, revenue_with_cost, cost_total
        FROM agg
    """, params)

    by_loc: dict[str, dict] = {}
    totals = {
        "transactions": 0, "units": 0.0, "revenue": 0.0,
        "regular_total": 0.0, "discount_total": 0.0,
        "revenue_with_cost": 0.0, "cost_total": 0.0,
    }
    for r in cur.fetchall():
        loc, tx, units, rev, reg, disc, rev_wc, cost = r
        m = {
            "transactions": int(tx or 0),
            "units": float(units or 0),
            "revenue": float(rev or 0),
            "regular_total": float(reg or 0),
            "discount_total": float(disc or 0),
            "revenue_with_cost": float(rev_wc or 0),
            "cost_total": float(cost or 0),
        }
        by_loc[loc] = m
        for k in totals: totals[k] += m[k]
    by_loc["_total"] = totals
    return by_loc


def _derive_metrics(raw: dict) -> dict:
    """Add derived metrics: avg_transaction, margin_dollars, margin_pct, discount_pct."""
    out = dict(raw)
    # Explicit revenue names: Gross Sales = pre-discount (units x regular price);
    # Net Sales = Gross less discounts = Cova Subtotal (what we store as revenue).
    out["gross_sales"] = raw["regular_total"]
    out["net_sales"] = raw["revenue"]
    out["avg_transaction"] = (raw["revenue"] / raw["transactions"]) if raw["transactions"] else 0
    out["margin_dollars"] = raw["revenue_with_cost"] - raw["cost_total"]
    out["margin_pct"] = (
        (out["margin_dollars"] / raw["revenue_with_cost"] * 100)
        if raw["revenue_with_cost"] else 0
    )
    out["margin_coverage_pct"] = (
        (raw["revenue_with_cost"] / raw["revenue"] * 100)
        if raw["revenue"] else 0
    )
    out["discount_pct"] = (
        (raw["discount_total"] / raw["regular_total"] * 100)
        if raw["regular_total"] else 0
    )
    return out


def _diff_pair(curr: float, prev: float) -> dict:
    """Return $ and % difference between current and prior values."""
    diff = curr - prev
    pct = ((curr - prev) / prev * 100) if prev else None
    return {"diff": diff, "pct": pct}


@app.get("/api/analytics/sales-performance")
def sales_performance(
    preset: str = "last30",
    start: str | None = None,
    end: str | None = None,
    store: str | None = None,
    yoy: bool = True,
) -> dict:
    """
    Sales performance metrics by store with optional YoY comparison.

    Presets: today, yesterday, wtd, last7, last30, mtd, last_month, qtd,
             last_quarter, ytd, last12, custom (requires start + end)

    Returns:
      - period: {start, end, label}
      - prior_period: {start, end} (if yoy=true)
      - rows: list of per-store metrics with current + prior + diffs
    """
    today = date.today()
    p_start, p_end = _resolve_date_range(preset, start, end, today)
    if p_start > p_end:
        raise HTTPException(status_code=400, detail="start must be <= end")

    with db() as conn:
        # Get latest available sale_date — clamp the period to it
        latest = get_latest_sale_date(conn)
        actual_end = min(p_end, latest) if latest else p_end

        current = _query_period_metrics(conn, p_start, actual_end, store)

        prior_data = None
        if yoy:
            prev_start, prev_end = _shift_period_back_one_year(p_start, actual_end)
            prior_data = _query_period_metrics(conn, prev_start, prev_end, store)

        # Get location names for nicer display
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM locations")
        loc_names = {r[0]: r[1] for r in cur.fetchall()}

    # Build per-store rows + a chain-wide total row
    rows = []
    all_loc_ids = sorted(set(current.keys()) - {"_total"})
    if prior_data:
        all_loc_ids = sorted(set(all_loc_ids) | (set(prior_data.keys()) - {"_total"}))

    for loc_id in all_loc_ids + ["_total"]:
        curr_raw = current.get(loc_id, {
            "transactions": 0, "units": 0, "revenue": 0,
            "regular_total": 0, "discount_total": 0,
            "revenue_with_cost": 0, "cost_total": 0,
        })
        curr = _derive_metrics(curr_raw)
        row = {
            "location_id": loc_id,
            "location_name": "Chain Total" if loc_id == "_total" else loc_names.get(loc_id, loc_id),
            "is_total": loc_id == "_total",
            "current": curr,
        }
        if prior_data:
            prev_raw = prior_data.get(loc_id, {
                "transactions": 0, "units": 0, "revenue": 0,
                "regular_total": 0, "discount_total": 0,
                "revenue_with_cost": 0, "cost_total": 0,
            })
            prev = _derive_metrics(prev_raw)
            row["prior"] = prev
            row["delta"] = {
                "revenue": _diff_pair(curr["revenue"], prev["revenue"]),
                "gross_sales": _diff_pair(curr["gross_sales"], prev["gross_sales"]),
                "net_sales": _diff_pair(curr["net_sales"], prev["net_sales"]),
                "units": _diff_pair(curr["units"], prev["units"]),
                "transactions": _diff_pair(curr["transactions"], prev["transactions"]),
                "avg_transaction": _diff_pair(curr["avg_transaction"], prev["avg_transaction"]),
                "margin_dollars": _diff_pair(curr["margin_dollars"], prev["margin_dollars"]),
                "margin_pct": {"diff": curr["margin_pct"] - prev["margin_pct"], "pct": None},
                "discount_pct": {"diff": curr["discount_pct"] - prev["discount_pct"], "pct": None},
            }
        rows.append(row)

    response = {
        "period": {
            "start": p_start.isoformat(),
            "end": actual_end.isoformat(),
            "preset": preset,
            "clamped": actual_end < p_end,
        },
        "rows": rows,
    }
    if yoy and prior_data:
        prev_start, prev_end = _shift_period_back_one_year(p_start, actual_end)
        response["prior_period"] = {
            "start": prev_start.isoformat(),
            "end": prev_end.isoformat(),
        }
    return response


# ---------------------------------------------------------------------------
# /api/lps — Licensed Producers (Data Partners tab)
# ---------------------------------------------------------------------------
# CRUD for the licensed_producers table + nested lp_contacts.
# Pre-seeded from OCS catalog + master list via jobs.seed_lps.

@app.get("/api/lps")
def list_lps(
    active_only: bool = False,
    has_agreement: bool | None = None,
) -> dict:
    """List all LPs with summary status. Used for the Data Partners tab list view."""
    with db() as conn:
        cur = conn.cursor()
        where_clauses = []
        params: list = []
        if active_only:
            where_clauses.append("is_active = 1")
        if has_agreement is True:
            where_clauses.append("agreement_signed = 1")
        elif has_agreement is False:
            where_clauses.append("agreement_signed = 0")
        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
        cur.execute(f"""
            SELECT lp.id, lp.name, lp.terms_summary, lp.payment_terms,
                   lp.agreement_sent, lp.agreement_signed, lp.data_sent,
                   lp.report_sent, lp.invoice_sent, lp.is_active,
                   lp.lto_active, lp.name_in_ocs,
                   (SELECT COUNT(*) FROM lp_contacts c WHERE c.lp_id = lp.id) AS contact_count,
                   (SELECT name FROM lp_contacts c WHERE c.lp_id = lp.id
                    ORDER BY c.is_primary DESC, c.sort_order LIMIT 1) AS primary_contact
            FROM licensed_producers lp
            {where_sql}
            ORDER BY lp.name
        """, params)
        rows = []
        for r in cur.fetchall():
            rows.append({
                "id": r[0], "name": r[1],
                "terms_summary": r[2], "payment_terms": r[3],
                "agreement_sent": bool(r[4]), "agreement_signed": bool(r[5]),
                "data_sent": bool(r[6]), "report_sent": bool(r[7]),
                "invoice_sent": bool(r[8]), "is_active": bool(r[9]),
                "lto_active": bool(r[10]), "name_in_ocs": r[11],
                "contact_count": r[12], "primary_contact": r[13],
            })
    return {"count": len(rows), "lps": rows}


@app.get("/api/lps/{lp_id}")
def get_lp(lp_id: int) -> dict:
    """Get full detail for one LP including all contacts."""
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM licensed_producers WHERE id = ?", (lp_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"LP {lp_id} not found")
        cols = [d[0] for d in cur.description]
        lp = dict(zip(cols, row))
        # Coerce booleans
        for k in ("agreement_sent", "agreement_signed", "data_sent", "report_sent",
                 "invoice_sent", "lto_active", "lto_report_sent", "lto_invoice_sent",
                 "is_active"):
            if k in lp:
                lp[k] = bool(lp[k])

        cur.execute("""
            SELECT id, name, email, phone, role, notes, is_primary, sort_order
            FROM lp_contacts WHERE lp_id = ? ORDER BY sort_order, id
        """, (lp_id,))
        contacts = []
        for c in cur.fetchall():
            contacts.append({
                "id": c[0], "name": c[1], "email": c[2], "phone": c[3],
                "role": c[4], "notes": c[5],
                "is_primary": bool(c[6]), "sort_order": c[7],
            })
        lp["contacts"] = contacts
    return lp


@app.put("/api/lps/{lp_id}")
def update_lp(lp_id: int, payload: dict) -> dict:
    """Update an LP. Accepts any subset of editable fields."""
    editable = {
        "name", "terms_summary", "payment_terms", "notes",
        "agreement_sent", "agreement_signed", "data_sent",
        "report_sent", "invoice_sent",
        "lto_active", "lto_offer", "lto_applicable_skus",
        "lto_report_sent", "lto_invoice_sent", "lto_payment_terms",
        "is_active",
    }
    updates = {k: v for k, v in payload.items() if k in editable}
    if not updates:
        raise HTTPException(status_code=400, detail="no editable fields provided")
    # Coerce booleans
    bool_fields = {"agreement_sent", "agreement_signed", "data_sent",
                   "report_sent", "invoice_sent", "lto_active",
                   "lto_report_sent", "lto_invoice_sent", "is_active"}
    set_parts = []
    params: list = []
    for k, v in updates.items():
        if k in bool_fields:
            v = 1 if v else 0
        set_parts.append(f"{k} = ?")
        params.append(v)
    set_parts.append("updated_at = CURRENT_TIMESTAMP")
    params.append(lp_id)
    with db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            UPDATE licensed_producers SET {', '.join(set_parts)} WHERE id = ?
        """, params)
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"LP {lp_id} not found")
        conn.commit()
    return {"ok": True, "lp_id": lp_id, "updated_fields": list(updates.keys())}


@app.post("/api/lps")
def create_lp(payload: dict) -> dict:
    """Create a new LP. Only `name` is required."""
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    with db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO licensed_producers (name, terms_summary, payment_terms, notes, is_active)
                VALUES (?, ?, ?, ?, 1)
            """, (name, payload.get("terms_summary"),
                  payload.get("payment_terms"), payload.get("notes")))
            conn.commit()
            return {"ok": True, "lp_id": cur.lastrowid, "name": name}
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"LP '{name}' already exists")


@app.delete("/api/lps/{lp_id}")
def delete_lp(lp_id: int) -> dict:
    """Soft-delete an LP (sets is_active=0). Real deletion would orphan
    historical data — better to keep the record and mark inactive."""
    with db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE licensed_producers SET is_active = 0 WHERE id = ?", (lp_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"LP {lp_id} not found")
        conn.commit()
    return {"ok": True, "lp_id": lp_id, "soft_deleted": True}


# ---------------------------------------------------------------------------
# Contacts within an LP

@app.post("/api/lps/{lp_id}/contacts")
def add_contact(lp_id: int, payload: dict) -> dict:
    """Add a contact to an LP."""
    name = (payload.get("name") or "").strip() or None
    email = (payload.get("email") or "").strip() or None
    phone = (payload.get("phone") or "").strip() or None
    if not (name or email):
        raise HTTPException(status_code=400, detail="provide at least name or email")
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM licensed_producers WHERE id = ?", (lp_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"LP {lp_id} not found")
        cur.execute("SELECT MAX(sort_order) FROM lp_contacts WHERE lp_id = ?", (lp_id,))
        max_sort = cur.fetchone()[0] or 0
        cur.execute("""
            INSERT INTO lp_contacts (lp_id, name, email, phone, role, notes, is_primary, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (lp_id, name, email, phone, payload.get("role"), payload.get("notes"),
              1 if payload.get("is_primary") else 0, max_sort + 1))
        conn.commit()
        return {"ok": True, "contact_id": cur.lastrowid}


@app.put("/api/lps/{lp_id}/contacts/{contact_id}")
def update_contact(lp_id: int, contact_id: int, payload: dict) -> dict:
    """Update a contact."""
    editable = {"name", "email", "phone", "role", "notes", "is_primary", "sort_order"}
    updates = {k: v for k, v in payload.items() if k in editable}
    if not updates:
        raise HTTPException(status_code=400, detail="no editable fields provided")
    if "is_primary" in updates:
        updates["is_primary"] = 1 if updates["is_primary"] else 0
    set_parts = [f"{k} = ?" for k in updates]
    params = list(updates.values()) + [contact_id, lp_id]
    with db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            UPDATE lp_contacts SET {', '.join(set_parts)}
            WHERE id = ? AND lp_id = ?
        """, params)
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="contact not found")
        conn.commit()
    return {"ok": True}


@app.delete("/api/lps/{lp_id}/contacts/{contact_id}")
def delete_contact(lp_id: int, contact_id: int) -> dict:
    """Permanently delete a contact (no soft-delete here — they're easy to re-add)."""
    with db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM lp_contacts WHERE id = ? AND lp_id = ?", (contact_id, lp_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="contact not found")
        conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Seeding endpoint — called from CLI or admin UI

@app.post("/api/lps/_seed")
def seed_lps_endpoint() -> dict:
    """Run the LP seeder. Idempotent — preserves existing user edits.
    Pulls LP universe from ocs_catalog.supplier and merges with master list."""
    from jobs.seed_lps import seed_lps_from_master_list
    with db() as conn:
        result = seed_lps_from_master_list(conn)
    return result


# ---------------------------------------------------------------------------
# OCS-catalog-driven LP list — superset of `licensed_producers` table
# ---------------------------------------------------------------------------
# The user's vision: see EVERY LP that appears in the OCS catalog, regardless
# of whether we have a partnership record. This endpoint joins live from
# ocs_catalog.supplier so newly listed LPs show up automatically.
#
# For each supplier:
#   - sku_count: how many SKUs this LP has in the OCS catalog
#   - in_stock_count: how many are currently orderable
#   - has_lp_record: do we have a row in licensed_producers for them
#   - lp_id, agreement_signed, contact_count, etc. (when has_lp_record)
#
# This lets the user click into any supplier (even "new" ones) and create
# a partnership record on the fly.

@app.get("/api/lps-from-catalog")
def list_lps_from_catalog(
    has_agreement: bool | None = None,
    has_record: bool | None = None,
    search: str | None = None,
) -> dict:
    """List the union of OCS catalog suppliers + licensed_producers entries.

    Filters:
      has_agreement -- True/False to filter by partnership agreement status
      has_record    -- True = only show LPs we have a record for; False = only
                       show LPs in OCS catalog without a record yet
      search        -- substring match on supplier name (case-insensitive)
    """
    with db() as conn:
        cur = conn.cursor()

        # Step 1: pull all suppliers from OCS catalog with sku counts.
        cur.execute("""
            SELECT supplier,
                   COUNT(*) AS sku_count,
                   SUM(CASE WHEN stock_status = 'YES' THEN 1 ELSE 0 END) AS in_stock_count
            FROM ocs_catalog
            WHERE supplier IS NOT NULL AND TRIM(supplier) <> ''
            GROUP BY supplier
            ORDER BY supplier
        """)
        catalog_suppliers = {r[0]: {"sku_count": r[1], "in_stock_count": r[2]}
                             for r in cur.fetchall()}

        # Step 2: pull all licensed_producers records, keyed by name AND name_in_ocs
        # (the latter is the OCS-catalog-supplier-name equivalent if different)
        cur.execute("""
            SELECT lp.id, lp.name, lp.name_in_ocs, lp.terms_summary, lp.payment_terms,
                   lp.agreement_sent, lp.agreement_signed, lp.data_sent,
                   lp.report_sent, lp.invoice_sent, lp.is_active, lp.lto_active,
                   (SELECT COUNT(*) FROM lp_contacts c WHERE c.lp_id = lp.id) AS contact_count,
                   (SELECT COUNT(*) FROM ltos lt WHERE lt.lp_id = lp.id AND lt.is_active = 1) AS active_lto_count
            FROM licensed_producers lp
        """)
        lp_records: dict[str, dict] = {}  # key: lowercase supplier name -> lp record
        for r in cur.fetchall():
            rec = {
                "lp_id": r[0], "lp_name": r[1], "name_in_ocs": r[2],
                "terms_summary": r[3], "payment_terms": r[4],
                "agreement_sent": bool(r[5]), "agreement_signed": bool(r[6]),
                "data_sent": bool(r[7]), "report_sent": bool(r[8]),
                "invoice_sent": bool(r[9]), "is_active": bool(r[10]),
                "lto_active": bool(r[11]),
                "contact_count": r[12], "active_lto_count": r[13],
            }
            for key in (r[1], r[2]):  # match on either lp.name or lp.name_in_ocs
                if key:
                    lp_records[key.lower()] = rec

        # Step 3: build the unified view
        items = []
        seen_lps = set()
        for supplier, stats in catalog_suppliers.items():
            rec = lp_records.get(supplier.lower())
            item = {
                "supplier": supplier,
                "sku_count": stats["sku_count"],
                "in_stock_count": stats["in_stock_count"],
                "has_lp_record": bool(rec),
            }
            if rec:
                item.update(rec)
                seen_lps.add(rec["lp_id"])
            items.append(item)

        # Step 4: also include LPs that have a record but aren't (or aren't anymore)
        # in the OCS catalog — these are still partners worth tracking.
        for key, rec in lp_records.items():
            if rec["lp_id"] in seen_lps:
                continue
            seen_lps.add(rec["lp_id"])
            items.append({
                "supplier": rec["lp_name"],
                "sku_count": 0,
                "in_stock_count": 0,
                "has_lp_record": True,
                **rec,
            })

        # Apply filters
        if search:
            q = search.lower().strip()
            items = [x for x in items if q in x["supplier"].lower()]
        if has_agreement is True:
            items = [x for x in items if x.get("agreement_signed")]
        elif has_agreement is False:
            items = [x for x in items if not x.get("agreement_signed")]
        if has_record is True:
            items = [x for x in items if x["has_lp_record"]]
        elif has_record is False:
            items = [x for x in items if not x["has_lp_record"]]

        items.sort(key=lambda x: x["supplier"].lower())

    return {"count": len(items), "items": items}


@app.post("/api/lps/from-supplier")
def create_lp_from_supplier(payload: dict = Body(...)) -> dict:
    """Create a licensed_producers record for a supplier seen in OCS catalog
    but not yet tracked. Idempotent — returns existing record if name matches."""
    supplier = (payload.get("supplier") or "").strip()
    if not supplier:
        raise HTTPException(status_code=400, detail="supplier required")

    with db() as conn:
        cur = conn.cursor()
        # Check existing
        cur.execute("""
            SELECT id FROM licensed_producers
            WHERE LOWER(name) = LOWER(?) OR LOWER(name_in_ocs) = LOWER(?)
            LIMIT 1
        """, (supplier, supplier))
        existing = cur.fetchone()
        if existing:
            return {"id": existing[0], "created": False}
        cur.execute("""
            INSERT INTO licensed_producers (name, name_in_ocs, is_active)
            VALUES (?, ?, 1)
        """, (supplier, supplier))
        new_id = cur.lastrowid
        conn.commit()
    return {"id": new_id, "created": True}


# ---------------------------------------------------------------------------
# OCS catalog SKU search — used by LTO creator
# ---------------------------------------------------------------------------
# Autocomplete-style search for products to attach to an LTO. Returns
# enough info for the UI to display + identify each SKU uniquely.

@app.get("/api/ocs-catalog/search")
def search_ocs_catalog(
    q: str = "",
    supplier: str | None = None,
    limit: int = 25,
) -> dict:
    """Fuzzy SKU search. Used by the LTO creator UI.

    Searches across product_name, brand, and ocs_variant_number.
    If supplier is provided, filters to that LP's catalog only.
    """
    if not q or len(q.strip()) < 2:
        return {"items": []}

    q_lower = q.lower().strip()

    with db() as conn:
        cur = conn.cursor()
        sql = """
            SELECT ocs_variant_number, product_name, brand, supplier,
                   category, size, stock_status, unit_price, pack_size
            FROM ocs_catalog
            WHERE (LOWER(product_name) LIKE ?
                   OR LOWER(brand) LIKE ?
                   OR ocs_variant_number LIKE ?)
        """
        like_q = f"%{q_lower}%"
        params: list = [like_q, like_q, f"%{q}%"]

        if supplier:
            sql += " AND LOWER(supplier) = LOWER(?)"
            params.append(supplier)

        sql += " ORDER BY product_name LIMIT ?"
        params.append(min(100, max(1, int(limit))))

        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        items = [dict(zip(cols, r)) for r in cur.fetchall()]

    return {"count": len(items), "items": items}


# ---------------------------------------------------------------------------
# LP-scoped LTO endpoints — separate from brand-scoped LTOs
# ---------------------------------------------------------------------------
# LTOs created from the Data Partners tab attach to an LP via lp_id.
# These coexist with brand-scoped LTOs (lp_id NULL, brand_id set).

@app.get("/api/lps/{lp_id}/ltos")
def list_ltos_for_lp(lp_id: int, active_only: bool = False,
                     include_archived: bool = False) -> dict:
    """List LTOs scoped to an LP.

    By default, auto-hides LTOs whose end_date is in the past (archived).
    Pass include_archived=True to see them.
    """
    from datetime import date as _date
    today_iso = _date.today().isoformat()
    with db() as conn:
        cur = conn.cursor()
        sql = """
            SELECT lt.id, lt.name, lt.start_date, lt.end_date,
                   lt.rate_percentage, lt.rate_basis, lt.is_active,
                   lt.notes, lt.created_at,
                   lt.applies_to_brand, lt.applies_to_category,
                   lt.applies_to_subcategory, lt.discount_pct,
                   (SELECT COUNT(*) FROM lto_skus s WHERE s.lto_id = lt.id) AS sku_count
            FROM ltos lt
            WHERE lt.lp_id = ?
        """
        params: list = [lp_id]
        if active_only:
            sql += " AND lt.is_active = 1"
        if not include_archived:
            sql += " AND lt.end_date >= ?"
            params.append(today_iso)
        sql += " ORDER BY lt.start_date DESC"
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        items = [dict(zip(cols, r)) for r in cur.fetchall()]

        # Tag each item with is_archived (computed) for the UI
        for it in items:
            it["is_archived"] = (it.get("end_date") or "") < today_iso

        # Attach SKU lists for each LTO
        for it in items:
            cur.execute("""
                SELECT s.sku, oc.product_name, oc.brand, oc.size
                FROM lto_skus s
                LEFT JOIN ocs_catalog oc ON oc.ocs_variant_number = s.sku
                WHERE s.lto_id = ?
            """, (it["id"],))
            it["skus"] = [dict(zip([d[0] for d in cur.description], r))
                          for r in cur.fetchall()]
    return {"count": len(items), "items": items}


@app.post("/api/lps/{lp_id}/ltos")
def create_lp_lto(lp_id: int, payload: dict = Body(...)) -> dict:
    """Create an LTO scoped to a specific LP. The LTO captures:
      - name (e.g. 'Spring 2026 OG Kush rebate')
      - start_date / end_date (active period)
      - rate_percentage (data revenue %)
      - rate_basis (retail_sales | wholesale_cost | gross_profit)
      - discount_pct (% off retail for the promo, separate from rate_percentage)
      - applies_to_brand / applies_to_category / applies_to_subcategory (scope)
      - applicable_skus (list of OCS variant numbers — alternative to scope rules)
    """
    name = (payload.get("name") or "").strip()
    start_date = payload.get("start_date")
    end_date = payload.get("end_date")
    rate = payload.get("rate_percentage")
    basis = payload.get("rate_basis", "retail_sales")
    if not (name and start_date and end_date and rate is not None):
        raise HTTPException(status_code=400,
                            detail="name, start_date, end_date, rate_percentage required")
    if basis not in ("retail_sales", "wholesale_cost", "gross_profit"):
        raise HTTPException(status_code=400, detail="invalid rate_basis")
    try:
        rate_f = float(rate)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="rate_percentage must be a number")
    if not 0 <= rate_f <= 100:
        raise HTTPException(status_code=400, detail="rate_percentage must be 0-100")
    skus = payload.get("applicable_skus", []) or []
    if not isinstance(skus, list):
        raise HTTPException(status_code=400, detail="applicable_skus must be a list")
    # New scope fields. If user specifies a brand-level scope, no SKU list needed.
    applies_to_brand = (payload.get("applies_to_brand") or "").strip() or None
    applies_to_category = (payload.get("applies_to_category") or "").strip() or None
    applies_to_subcategory = (payload.get("applies_to_subcategory") or "").strip() or None
    discount_pct = payload.get("discount_pct")
    try:
        discount_pct = float(discount_pct) if discount_pct is not None else None
    except (ValueError, TypeError):
        discount_pct = None
    if not skus and not applies_to_brand:
        raise HTTPException(status_code=400,
                            detail="Either applicable_skus or applies_to_brand must be specified")

    with db() as conn:
        cur = conn.cursor()
        # Verify LP exists
        cur.execute("SELECT id FROM licensed_producers WHERE id = ?", (lp_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"LP {lp_id} not found")

        cur.execute("""
            INSERT INTO ltos (lp_id, name, start_date, end_date, lto_type,
                              rate_percentage, rate_basis, discount_pct,
                              applies_to_brand, applies_to_category, applies_to_subcategory,
                              notes, is_active)
            VALUES (?, ?, ?, ?, 'volume_rebate', ?, ?, ?, ?, ?, ?, ?, 1)
        """, (lp_id, name, start_date, end_date, rate_f, basis, discount_pct,
              applies_to_brand, applies_to_category, applies_to_subcategory,
              payload.get("notes")))
        lto_id = cur.lastrowid
        if skus:
            cur.executemany("INSERT INTO lto_skus (lto_id, sku) VALUES (?, ?)",
                            [(lto_id, s) for s in skus])
        conn.commit()
    return {"id": lto_id, "applicable_skus": skus}


# ============================================================================
# Email scraper — auto-import Cova exports from a dedicated inbox
# ============================================================================
# Polls IMAP inboxes, recognizes Cova attachments, drops them into imports/
# for the existing auto-importer to pick up. See jobs/email_scraper.py.
#
# Foundation features (this version):
#   - Configure 1+ accounts via UI
#   - Manual "Poll now" button per account
#   - View recent processing log
# Not yet:
#   - Background scheduler running on a timer
#   - OAuth2 (uses app passwords only for now)
#   - OCS-specific routing

@app.get("/api/email-scraper/accounts")
def list_email_accounts(request: Request) -> dict:
    """List all configured email accounts. Admin-only."""
    require_admin(request)
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, label, provider, host, port, username, folder,
                   is_active, poll_interval_min, last_polled_at, last_error,
                   created_at
            FROM email_scraper_accounts ORDER BY created_at DESC
        """)
        cols = [d[0] for d in cur.description]
        items = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {"count": len(items), "items": items}


@app.post("/api/email-scraper/accounts")
def create_email_account(payload: dict = Body(...), request: Request = None) -> dict:
    """Create a new email scraper account. Admin-only.

    Payload:
      label (required) — e.g. 'Cova exports'
      host (required) — e.g. 'imap.gmail.com'
      port — default 993
      username (required) — usually the email address
      password (required) — app password from the email provider
      folder — default 'INBOX'
    """
    require_admin(request)
    label = (payload.get("label") or "").strip()
    host = (payload.get("host") or "").strip()
    port = int(payload.get("port") or 993)
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    folder = (payload.get("folder") or "INBOX").strip()
    poll_interval = int(payload.get("poll_interval_min") or 5)

    if not all([label, host, username, password]):
        raise HTTPException(status_code=400, detail="label, host, username, password are required")

    password_enc = encrypt_secret(password)

    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO email_scraper_accounts
                (label, host, port, username, password_enc, folder,
                 poll_interval_min, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        """, (label, host, port, username, password_enc, folder, poll_interval))
        conn.commit()
        new_id = cur.lastrowid

    return {"ok": True, "id": new_id}


@app.delete("/api/email-scraper/accounts/{account_id}")
def delete_email_account(account_id: int, request: Request) -> dict:
    """Remove an email scraper account. Doesn't delete the log."""
    require_admin(request)
    with db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM email_scraper_accounts WHERE id = ?", (account_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Account not found")
        conn.commit()
    return {"ok": True}


@app.post("/api/email-scraper/accounts/{account_id}/poll")
def poll_email_account(account_id: int, request: Request) -> dict:
    """Trigger a manual poll for one account. Returns summary of what
    happened (messages checked, imported, errors)."""
    require_admin(request)
    from jobs.email_scraper import poll_account
    with db() as conn:
        result = poll_account(conn, account_id)
    return result


@app.get("/api/email-scraper/log")
def get_email_log(
    account_id: int | None = None, limit: int = 50,
    request: Request = None,
) -> dict:
    """Recent processing log entries. Admin-only."""
    require_admin(request)
    with db() as conn:
        cur = conn.cursor()
        sql = """
            SELECT esl.id, esl.account_id, esa.label AS account_label,
                   esl.message_uid, esl.received_at, esl.processed_at,
                   esl.sender, esl.subject, esl.attachment_count,
                   esl.action, esl.detail
            FROM email_scraper_log esl
            LEFT JOIN email_scraper_accounts esa ON esa.id = esl.account_id
        """
        params: list = []
        if account_id:
            sql += " WHERE esl.account_id = ?"
            params.append(account_id)
        sql += " ORDER BY esl.processed_at DESC LIMIT ?"
        params.append(min(500, max(1, int(limit))))
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        items = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {"count": len(items), "items": items}


# ============================================================================
# System Health — operational visibility for the Admin UI
# ============================================================================
# Backs the Admin → System Health tab. Surfaces:
#   - Data freshness per source (latest import dates, age in days)
#   - Per-store coverage (which stores have current IOH + sales)
#   - DB stats (size, row counts per table)
#   - Recent activity (last 20 events across all sources)
#   - Backup status (last backup, file count, total size)
#
# Read-only — no writes. All queries are cheap (COUNT/MAX) so this is safe to
# poll frequently from the UI.

@app.get("/api/system/health")
def get_system_health() -> dict:
    """Comprehensive health snapshot. Returns everything the UI needs in one
    call to avoid the N+1 round-trip pattern."""
    today = date.today()

    def _age_days(d_iso: str | None) -> int | None:
        if not d_iso:
            return None
        try:
            # Parse out just the date part (handles 'YYYY-MM-DD' and 'YYYY-MM-DD HH:MM:SS')
            d_part = d_iso.split(" ")[0].split("T")[0]
            return (today - date.fromisoformat(d_part)).days
        except (ValueError, AttributeError):
            return None

    sources = []
    per_store = []
    recent_activity = []

    if os.path.exists(DB_PATH):
        with db() as conn:
            cur = conn.cursor()

            # ----- Data freshness per source -----
            # Each source: latest record date, age in days, row count

            # Cova IOH
            cur.execute("SELECT MAX(as_of), COUNT(DISTINCT as_of) FROM inventory_snapshots")
            r = cur.fetchone()
            sources.append({
                "key": "cova_inventory", "label": "Cova Inventory On Hand",
                "last_update": r[0], "age_days": _age_days(r[0]),
                "snapshot_count": r[1] or 0,
                "expected_cadence_days": 1, "stale_after_days": 3,
            })

            # Cova sales
            cur.execute("SELECT MAX(sale_date), COUNT(*) FROM sales_daily")
            r = cur.fetchone()
            sources.append({
                "key": "cova_sales", "label": "Cova Sales",
                "last_update": r[0], "age_days": _age_days(r[0]),
                "row_count": r[1] or 0,
                "expected_cadence_days": 1, "stale_after_days": 2,
            })

            # OCS catalog
            cur.execute("SELECT COUNT(*) FROM ocs_catalog")
            ocs_rows = cur.fetchone()[0]
            sources.append({
                "key": "ocs_catalog", "label": "OCS Catalog",
                "row_count": ocs_rows,
                "expected_cadence_days": 14, "stale_after_days": 30,
                "note": "No timestamp tracking yet; reseed when product changes are visible",
            })

            # OCS Invoices
            cur.execute("SELECT MAX(invoice_date), COUNT(*) FROM invoices")
            r = cur.fetchone()
            sources.append({
                "key": "ocs_invoices", "label": "OCS Invoices",
                "last_update": r[0], "age_days": _age_days(r[0]),
                "row_count": r[1] or 0,
                "expected_cadence_days": 14, "stale_after_days": 30,
            })

            # Discounts
            cur.execute("SELECT MAX(sale_date), COUNT(*) FROM discount_lines")
            r = cur.fetchone()
            sources.append({
                "key": "discounts", "label": "Cova Discounts",
                "last_update": r[0], "age_days": _age_days(r[0]),
                "row_count": r[1] or 0,
                "expected_cadence_days": 30, "stale_after_days": 60,
            })

            # Market Intelligence
            cur.execute("""
                SELECT MAX(imported_at), COUNT(*) FROM market_intelligence_imports
            """)
            r = cur.fetchone()
            sources.append({
                "key": "market_intelligence", "label": "OCS Market Intelligence",
                "last_update": r[0], "age_days": _age_days(r[0]),
                "row_count": r[1] or 0,
                "expected_cadence_days": 14, "stale_after_days": 21,
            })

            # ----- Per-store coverage -----
            cur.execute("""
                SELECT l.id, l.name,
                       (SELECT MAX(as_of) FROM inventory_snapshots WHERE location_id = l.id) AS last_ioh,
                       (SELECT MAX(sale_date) FROM sales_daily WHERE location_id = l.id) AS last_sales,
                       (SELECT COUNT(*) FROM market_intelligence_imports WHERE location_id = l.id) AS mi_imports,
                       (SELECT MAX(imported_at) FROM market_intelligence_imports WHERE location_id = l.id) AS last_mi
                FROM locations l
                WHERE l.is_active = 1
                ORDER BY l.name
            """)
            for row in cur.fetchall():
                loc_id, name, last_ioh, last_sales, mi_count, last_mi = row
                per_store.append({
                    "location_id": loc_id, "store_name": name,
                    "last_ioh": last_ioh, "ioh_age_days": _age_days(last_ioh),
                    "last_sales": last_sales, "sales_age_days": _age_days(last_sales),
                    "mi_imports": mi_count, "last_mi": last_mi,
                    "mi_age_days": _age_days(last_mi),
                })

            # ----- Recent activity (merged event stream) -----
            # Pull last N from each source, then merge + sort + truncate.
            events = []
            try:
                cur.execute("""
                    SELECT 'mi_import' AS kind, imported_at AS ts,
                           location_id AS detail_a, sku_count AS detail_b
                    FROM market_intelligence_imports
                    ORDER BY imported_at DESC LIMIT 10
                """)
                for r in cur.fetchall():
                    events.append({
                        "kind": "mi_import", "timestamp": r[1],
                        "summary": f"Market intelligence import: {r[2]} ({r[3]} SKUs)",
                    })
            except sqlite3.OperationalError:
                pass

            try:
                cur.execute("""
                    SELECT 'settings_change' AS kind, changed_at AS ts,
                           key, new_value, changed_by
                    FROM app_settings_history
                    ORDER BY changed_at DESC LIMIT 10
                """)
                for r in cur.fetchall():
                    events.append({
                        "kind": "settings_change", "timestamp": r[1],
                        "summary": f"Setting changed: {r[2]} → {r[3]} (by {r[4] or 'anonymous'})",
                    })
            except sqlite3.OperationalError:
                pass

            try:
                cur.execute("""
                    SELECT 'gap_action' AS kind, updated_at AS ts,
                           location_id, sku, status, author_name
                    FROM gap_suggestions_status
                    ORDER BY updated_at DESC LIMIT 10
                """)
                for r in cur.fetchall():
                    events.append({
                        "kind": "gap_action", "timestamp": r[1],
                        "summary": f"Gap suggestion {r[4]}: {r[3]} at {r[2]} (by {r[5] or 'anonymous'})",
                    })
            except sqlite3.OperationalError:
                pass

            # Sort merged events by timestamp desc, take 20
            events.sort(key=lambda e: e["timestamp"] or "", reverse=True)
            recent_activity = events[:20]

    # ----- DB stats + backups -----
    db_stats = None
    backup_summary = None
    try:
        from jobs.backup import get_db_stats, list_backups
        db_stats = get_db_stats()
        backups = list_backups()
        backup_summary = {
            "count": len(backups),
            "latest": backups[0] if backups else None,
            "total_size_bytes": sum(b["size_bytes"] for b in backups),
        }
    except Exception as e:
        db_stats = {"error": str(e)}

    return {
        "sources": sources,
        "per_store": per_store,
        "recent_activity": recent_activity,
        "db_stats": db_stats,
        "backup_summary": backup_summary,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }


@app.post("/api/system/backup-now")
def trigger_manual_backup() -> dict:
    """Run a backup right now. Returns metadata about the new file."""
    try:
        from jobs.backup import create_backup, prune_backups
        result = create_backup()
        prune = prune_backups()
        result["pruned"] = prune["pruned"]
        return result
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup failed: {e}")


@app.get("/api/system/backups")
def list_db_backups() -> dict:
    """List all current backups with metadata."""
    try:
        from jobs.backup import list_backups
        return {"backups": list_backups()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict:
    """Trivial liveness probe for load balancers / uptime monitors — no DB, no
    auth, always fast. Use this for health checks, not /api/health (which counts
    rows on large tables and touches the DB)."""
    return {"status": "ok"}


@app.get("/api/health")
def health() -> dict:
    if not os.path.exists(DB_PATH):
        raise HTTPException(status_code=503, detail=f"db not found at {DB_PATH}")
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM products"); products = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM sales_daily"); sales = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM inventory_snapshots"); inv = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM locations"); locs = cur.fetchone()[0]
    return {
        "status": "ok", "db_path": DB_PATH,
        "locations": locs, "products": products,
        "sales_rows": sales, "inventory_snapshots": inv,
    }

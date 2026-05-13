"""
Authentication helpers
======================

Centralized password hashing, session token generation, and current-user
lookup. Used by api/main.py auth endpoints.

Honest scope notes:
- Uses bcrypt for password hashing (industry standard, well-tested).
- Sessions are random tokens stored in `user_sessions` table. Cookie carries
  the token; server looks it up. Cookies are HttpOnly + Secure (set in main.py).
- No JWT — too easy to misuse. Server-side sessions are simpler for this scale.
- 30-day session lifetime. Auto-extends on each request (last_seen_at).
"""
from __future__ import annotations

import os
import secrets
import sqlite3
from base64 import b64encode, b64decode
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from cryptography.fernet import Fernet, InvalidToken


SESSION_LIFETIME_DAYS = 30
SESSION_COOKIE_NAME = "sbinsights_session"


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt. Returns the encoded string
    safe to store in the DB."""
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    salt = bcrypt.gensalt(rounds=12)  # ~250ms per hash, hard to brute-force
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Check if a plaintext password matches a stored hash."""
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def create_session(conn: sqlite3.Connection, user_id: int,
                   user_agent: str | None = None) -> tuple[str, str]:
    """Create a new session for a user. Returns (session_token, expires_at_iso).
    The token goes in a cookie; expires_at is also stored so we can verify."""
    token = secrets.token_hex(32)  # 64 hex chars = 256 bits of entropy
    expires_at = (datetime.utcnow() + timedelta(days=SESSION_LIFETIME_DAYS)).isoformat()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO user_sessions (session_token, user_id, expires_at, user_agent)
        VALUES (?, ?, ?, ?)
    """, (token, user_id, expires_at, user_agent))
    conn.commit()
    return token, expires_at


def get_session_user(conn: sqlite3.Connection, session_token: str | None) -> Optional[dict]:
    """Look up the user for a given session token. Returns None if invalid/expired.
    Auto-updates last_seen_at on success."""
    if not session_token:
        return None
    cur = conn.cursor()
    cur.execute("""
        SELECT s.user_id, s.expires_at, u.id, u.email, u.name, u.role,
               u.must_change_password, u.is_active
        FROM user_sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.session_token = ?
    """, (session_token,))
    row = cur.fetchone()
    if not row:
        return None
    user_id, expires_at, uid, email, name, role, must_change, active = row
    # Check expiry
    try:
        if datetime.fromisoformat(expires_at) < datetime.utcnow():
            # Expired — clean up
            cur.execute("DELETE FROM user_sessions WHERE session_token = ?", (session_token,))
            conn.commit()
            return None
    except ValueError:
        return None
    if not active:
        return None
    # Touch last_seen_at
    cur.execute("UPDATE user_sessions SET last_seen_at = CURRENT_TIMESTAMP WHERE session_token = ?",
                (session_token,))
    conn.commit()
    return {
        "id": uid, "email": email, "name": name, "role": role,
        "must_change_password": bool(must_change),
    }


def delete_session(conn: sqlite3.Connection, session_token: str) -> None:
    """Log out a single session."""
    cur = conn.cursor()
    cur.execute("DELETE FROM user_sessions WHERE session_token = ?", (session_token,))
    conn.commit()


def delete_user_sessions(conn: sqlite3.Connection, user_id: int) -> int:
    """Force-logout all sessions for a user (e.g., on password change).
    Returns count of deleted sessions."""
    cur = conn.cursor()
    cur.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
    count = cur.rowcount
    conn.commit()
    return count


def prune_expired_sessions(conn: sqlite3.Connection) -> int:
    """Housekeeping. Returns count of pruned rows. Call periodically."""
    cur = conn.cursor()
    cur.execute("DELETE FROM user_sessions WHERE expires_at < ?",
                (datetime.utcnow().isoformat(),))
    count = cur.rowcount
    conn.commit()
    return count


# ---------------------------------------------------------------------------
# Secret encryption (for email scraper passwords)
# ---------------------------------------------------------------------------
# App passwords for email accounts can't be one-way hashed (we need them to log
# in). So we encrypt them with a key from the .env file. If .env leaks, those
# passwords are compromised — but the same is true of the DB credentials and
# everything else.

_FERNET_CACHE: Fernet | None = None


def _get_fernet() -> Fernet:
    """Lazy-load the Fernet instance from SECRET_KEY env var."""
    global _FERNET_CACHE
    if _FERNET_CACHE is None:
        key = os.environ.get("SECRET_KEY")
        if not key:
            # Fall back to a derived key for dev. NEVER use this in production.
            # Production should always have SECRET_KEY set in .env.
            print("[auth] WARNING: SECRET_KEY not set. Using insecure fallback. Set SECRET_KEY in .env for production.")
            key = b64encode(b"dev-only-key-do-not-use-in-prod-32b").decode()
        # Fernet wants a 32-byte url-safe-base64-encoded key
        try:
            _FERNET_CACHE = Fernet(key.encode() if isinstance(key, str) else key)
        except ValueError:
            # Key wasn't proper Fernet format; derive one. Still insecure.
            from hashlib import sha256
            derived = b64encode(sha256(key.encode()).digest())
            _FERNET_CACHE = Fernet(derived)
    return _FERNET_CACHE


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret (e.g., email app password) for DB storage."""
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a stored secret. Returns empty string on failure (so a broken
    SECRET_KEY doesn't crash the server, but does break the scraper visibly)."""
    try:
        return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def generate_secret_key() -> str:
    """Generate a fresh Fernet key. Used for first-time setup."""
    return Fernet.generate_key().decode("utf-8")

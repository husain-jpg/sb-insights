"""Create the initial GM admin accounts on a fresh production database.

Bypasses the API (which requires an existing admin to create more users)
by inserting directly into the `users` table. Run ONCE on the droplet
after the DB is migrated and the service is up.

Usage on droplet (as terroir user, from /opt/terroir-ops):
    .venv/bin/python deploy/bootstrap_admins.py
"""
from __future__ import annotations

import os
import secrets
import string
import sqlite3
import sys
from pathlib import Path

# Ensure repo root is importable
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Use the project's own auth helper so the hash format matches login()
from jobs.auth import hash_password


def _read_env(path: Path) -> dict[str, str]:
    """Minimal .env reader — same shape as the project's _load_dotenv,
    avoids the python-dotenv dependency for this one-off bootstrap."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


# Bootstrap roster — edit if more GMs need accounts.
ACCOUNTS = [
    {"email": "husain@starbuds.co",     "name": "Husain",     "role": "admin"},
    {"email": "eric.dawes@starbuds.co", "name": "Eric Dawes", "role": "admin"},
    {"email": "sophia@starbuds.co",     "name": "Sophia",     "role": "admin"},
]


def gen_temp_password(length: int = 16) -> str:
    """Url-safe enough, mixed-case + digits. Forced change on first login."""
    alphabet = string.ascii_letters + string.digits + "!@#$%&*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        # ensure mix
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw)):
            return pw


def main() -> int:
    env = _read_env(REPO_ROOT / ".env")
    db_path = env.get("TERROIR_DB") or os.environ.get("TERROIR_DB") or "terroir.db"
    print(f"DB: {db_path}")

    conn = sqlite3.connect(db_path, timeout=30)
    cur = conn.cursor()

    created: list[tuple[str, str]] = []
    skipped: list[str] = []

    for acct in ACCOUNTS:
        email = acct["email"].strip().lower()
        cur.execute("SELECT id FROM users WHERE LOWER(email) = ?", (email,))
        if cur.fetchone():
            skipped.append(email)
            continue
        temp_pw = gen_temp_password()
        cur.execute(
            """INSERT INTO users (email, name, password_hash, role,
                                  must_change_password, created_by, is_active)
               VALUES (?, ?, ?, ?, 1, 'bootstrap', 1)""",
            (email, acct["name"], hash_password(temp_pw), acct["role"]),
        )
        created.append((email, temp_pw))

    conn.commit()
    conn.close()

    print()
    if created:
        print("=" * 60)
        print("CREATED ACCOUNTS — SHARE THESE TEMP PASSWORDS SECURELY")
        print("(Each user is forced to change password on first login)")
        print("=" * 60)
        for email, pw in created:
            print(f"  {email:35}  temp_password: {pw}")
        print()
    if skipped:
        print(f"Skipped (already existed): {', '.join(skipped)}")
    if not created and not skipped:
        print("No accounts in ACCOUNTS list. Edit deploy/bootstrap_admins.py.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

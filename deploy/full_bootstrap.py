"""Bootstrap / reset admin accounts.

Idempotent — for each account:
  - If exists → resets password + sets must_change_password=1 + role=admin
  - If not exists → creates with a fresh password + must_change_password=1
Prints temp passwords once. Capture immediately.
"""
from __future__ import annotations
import secrets, string, sqlite3, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from jobs.auth import hash_password

ACCOUNTS = [
    ("husain@starbuds.co", "Husain"),
    ("eric.dawes@starbuds.co", "Eric Dawes"),
    ("sophia@starbuds.co", "Sophia"),
    ("stew@starbuds.co", "Stew"),
]

DB_PATH = REPO / "terroir.db"


def gen_pw(n: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(n))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw)):
            return pw


def main() -> int:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    cur = conn.cursor()
    print()
    print("=" * 60)
    print("Admin accounts — temp passwords (save to Bitwarden NOW):")
    print("=" * 60)
    for email, name in ACCOUNTS:
        pw = gen_pw()
        cur.execute("SELECT id FROM users WHERE LOWER(email) = ?", (email.lower(),))
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE users SET password_hash = ?, must_change_password = 1, "
                "role = 'admin', is_active = 1 WHERE id = ?",
                (hash_password(pw), row[0]),
            )
            tag = "RESET  "
        else:
            cur.execute(
                "INSERT INTO users (email, name, password_hash, role, "
                "must_change_password, created_by, is_active) "
                "VALUES (?, ?, ?, 'admin', 1, 'bootstrap', 1)",
                (email, name, hash_password(pw)),
            )
            tag = "CREATE "
        print(f"  {tag}  {email:30}  {pw}")
    conn.commit()
    conn.close()
    print()
    print("Done. Each user is forced to change password on first login.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

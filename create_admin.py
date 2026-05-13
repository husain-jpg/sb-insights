"""
create_admin.py — Bootstrap the first admin user.

Usage:
    py create_admin.py

Interactive prompts for email + name + password. Validates inputs, refuses
to overwrite an existing user, prints a confirmation.

This is the only way to create the FIRST admin. Once you have one admin
logged in, subsequent users are created through the Admin → Users UI.
"""
from __future__ import annotations

import getpass
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from db.sqlite_schema import init_schema
from jobs.auth import hash_password


def main():
    db_path = os.environ.get("TERROIR_DB", "terroir.db")
    if not Path(db_path).exists():
        print(f"⚠ Database not found at {db_path}.")
        print("  Run `py run.py` once to create it, then re-run this script.")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    init_schema(conn)  # idempotent; ensures users table exists

    print()
    print("=" * 60)
    print("  Create first admin user — SB Insights")
    print("=" * 60)
    print()

    # Check if any admin already exists
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1")
    existing_admin_count = cur.fetchone()[0]
    if existing_admin_count > 0:
        print(f"⚠ There is already an admin user in the system.")
        cur.execute("SELECT email FROM users WHERE role = 'admin' AND is_active = 1 LIMIT 5")
        for (email,) in cur.fetchall():
            print(f"    - {email}")
        print()
        choice = input("Create another admin anyway? (y/N): ").strip().lower()
        if choice != "y":
            print("Aborted.")
            sys.exit(0)
        print()

    # Email
    while True:
        email = input("Email: ").strip().lower()
        if not email or "@" not in email or "." not in email:
            print("  ⚠ Invalid email. Try again.")
            continue
        cur.execute("SELECT id FROM users WHERE LOWER(email) = ?", (email,))
        if cur.fetchone():
            print(f"  ⚠ A user with that email already exists. Use a different one.")
            continue
        break

    # Name
    name = input("Display name (optional, just used for UI): ").strip() or None

    # Password
    while True:
        password = getpass.getpass("Password (min 12 chars): ")
        if len(password) < 12:
            print("  ⚠ Password too short. Use at least 12 characters.")
            continue
        password2 = getpass.getpass("Confirm password: ")
        if password != password2:
            print("  ⚠ Passwords don't match. Try again.")
            continue
        break

    # Insert
    password_hash = hash_password(password)
    cur.execute("""
        INSERT INTO users (email, name, password_hash, role, must_change_password, created_by)
        VALUES (?, ?, ?, 'admin', 0, 'create_admin.py')
    """, (email, name, password_hash))
    conn.commit()
    user_id = cur.lastrowid

    print()
    print(f"✓ Admin created (id={user_id}, email={email}).")
    print()
    print("Next steps:")
    print("  1. Start the server: py run.py")
    print("  2. Visit http://127.0.0.1:8000/login")
    print(f"  3. Log in with {email} + your password")
    print()


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
# Step 3: Generate the production .env file on the droplet with a secure
# SECRET_KEY. CRITICAL: this must run BEFORE you migrate the DB or save any
# new OCS/email credentials. SECRET_KEY encrypts those — changing it later
# makes them unrecoverable.
#
# Run on the droplet as root.

set -euo pipefail

APP_DIR="/opt/terroir-ops"
APP_USER="terroir"
ENV_FILE="$APP_DIR/.env"

if [ -f "$ENV_FILE" ]; then
    echo "==> $ENV_FILE already exists. Refusing to overwrite (it has your SECRET_KEY)."
    echo "    If you really want to regenerate, delete it first AND understand that"
    echo "    encrypted credentials in the DB will be lost. Aborting."
    exit 1
fi

echo "==> Generating SECRET_KEY"
SECRET_KEY=$(sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

cat > "$ENV_FILE" <<EOF
# SB Insights — production environment
# Generated $(date -Iseconds) on $(hostname)
# DO NOT COMMIT. DO NOT SHARE. Chmod 600.

# Fernet key used for session signing AND encrypting stored OCS/email
# credentials. Generated at deploy time. NEVER rotate without re-entering
# all stored credentials.
SECRET_KEY=${SECRET_KEY}

# DB path. SQLite for now; will switch to managed Postgres later if locking
# contention becomes a real problem in practice.
TERROIR_DB=${APP_DIR}/terroir.db

# Timezone for schedulers (backup 3am, OCS connector 8pm).
TZ=America/Toronto

# Public URL where this is served (used in error pages, links, etc.)
PUBLIC_URL=https://app.sbinsights.co
EOF

chown "$APP_USER:$APP_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"

echo "==> .env created at $ENV_FILE (mode 600, owned by $APP_USER)"
echo
echo "SECRET_KEY (save in a password manager — required for disaster recovery):"
echo "    $SECRET_KEY"
echo
echo "If you lose this and need to restore from backup, you'll have to wipe"
echo "stored OCS/email creds and re-enter them."

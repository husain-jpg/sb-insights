#!/usr/bin/env bash
# Step 4: Migrate the local SQLite DB to the droplet, enable WAL mode, run
# schema migrations. Run from your LAPTOP.
#
# Usage:
#   DROPLET_IP=<ip>  bash deploy/04_migrate_db.sh
#
# What this does:
#   1. Stops the (not-yet-running) service on the droplet just in case
#   2. Takes a clean snapshot of the local DB using sqlite3 .backup
#      (safer than copying a live DB file — handles WAL/journal correctly)
#   3. gzip + scp to the droplet
#   4. Decompresses, enables WAL mode, runs the schema migration
#   5. Sanity checks: row counts on the key tables

set -euo pipefail

: "${DROPLET_IP:?Set DROPLET_IP=<your droplet IP> before running}"
APP_DIR="/opt/terroir-ops"
APP_USER="terroir"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL_DB="${REPO_ROOT}/terroir.db"
SNAPSHOT="/tmp/terroir-migrate-$(date +%Y%m%d-%H%M%S).db"

[ -f "$LOCAL_DB" ] || { echo "Local DB $LOCAL_DB not found"; exit 1; }
LOCAL_BYTES=$(stat -c%s "$LOCAL_DB" 2>/dev/null || stat -f%z "$LOCAL_DB")
echo "==> Local DB: $LOCAL_DB ($((LOCAL_BYTES/1024/1024)) MB)"

echo "==> [1/5] Stopping remote service (if running)"
ssh "root@${DROPLET_IP}" "systemctl stop terroir-ops 2>/dev/null || true"

echo "==> [2/5] Taking clean snapshot via sqlite3 .backup"
sqlite3 "$LOCAL_DB" ".backup '$SNAPSHOT'"
echo "    snapshot at $SNAPSHOT ($((LOCAL_BYTES/1024/1024)) MB)"

echo "==> [3/5] Compress + scp"
gzip -1 "$SNAPSHOT"
SNAPSHOT_GZ="${SNAPSHOT}.gz"
GZ_BYTES=$(stat -c%s "$SNAPSHOT_GZ" 2>/dev/null || stat -f%z "$SNAPSHOT_GZ")
echo "    compressed: $((GZ_BYTES/1024/1024)) MB"
scp "$SNAPSHOT_GZ" "root@${DROPLET_IP}:/tmp/terroir-migrate.db.gz"
rm -f "$SNAPSHOT_GZ"

echo "==> [4/5] Decompress on droplet, install, enable WAL, run migrations"
ssh "root@${DROPLET_IP}" "bash -s" <<'REMOTE'
set -euo pipefail
APP_DIR=/opt/terroir-ops
APP_USER=terroir
gunzip -f /tmp/terroir-migrate.db.gz
chown ${APP_USER}:${APP_USER} /tmp/terroir-migrate.db
# Move into place (replace any existing DB)
if [ -f ${APP_DIR}/terroir.db ]; then
    mv ${APP_DIR}/terroir.db ${APP_DIR}/terroir.db.replaced-$(date +%Y%m%d-%H%M%S)
fi
mv /tmp/terroir-migrate.db ${APP_DIR}/terroir.db
chown ${APP_USER}:${APP_USER} ${APP_DIR}/terroir.db
chmod 640 ${APP_DIR}/terroir.db

echo "    enabling WAL mode (better read/write concurrency than rollback journal)"
sudo -u ${APP_USER} sqlite3 ${APP_DIR}/terroir.db "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;" | tee /tmp/wal_result
grep -q wal /tmp/wal_result || { echo "WAL mode not active — abort"; exit 1; }

echo "    running schema migration (creates any missing tables/columns)"
cd ${APP_DIR}
sudo -u ${APP_USER} .venv/bin/python -c "
from db.sqlite_schema import ensure_schema
import sqlite3
conn = sqlite3.connect('terroir.db', timeout=60)
ensure_schema(conn)
conn.close()
print('schema OK')
"
REMOTE

echo "==> [5/5] Sanity check row counts"
ssh "root@${DROPLET_IP}" "
    sudo -u ${APP_USER} sqlite3 ${APP_DIR}/terroir.db <<-EOF
        .headers on
        .mode column
        SELECT 'sales_daily' tbl, COUNT(*) rows, MAX(sale_date) latest FROM sales_daily
        UNION ALL SELECT 'discount_lines', COUNT(*), MAX(sale_date) FROM discount_lines
        UNION ALL SELECT 'inventory_snapshots', COUNT(*), substr(MAX(as_of),1,10) FROM inventory_snapshots
        UNION ALL SELECT 'ocs_catalog', COUNT(*), MAX(as_of) FROM ocs_catalog
        UNION ALL SELECT 'data_revenue_deals', COUNT(*), MAX(start_date) FROM data_revenue_deals
        UNION ALL SELECT 'products', COUNT(*), '' FROM products
        UNION ALL SELECT 'users', COUNT(*), '' FROM users;
EOF
"

echo "==> Migration done. WAL active, schema current, counts above should match your local."
echo "    NOTE: stored OCS/email credentials will NOT decrypt on the new box (different"
echo "          SECRET_KEY). You'll re-enter them via the dashboard after first login."

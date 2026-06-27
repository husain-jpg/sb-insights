#!/usr/bin/env bash
# Delivery-date watcher — runs ON THE DROPLET on a schedule. Probes each store's
# real OCS order form (exportOrderTemplate=false) to detect when it carries
# committed delivery dates, which only appear during a store's order window.
# READ-ONLY against the portal — it never imports or writes to the DB, so it
# cannot clobber connector data.
#
# Sources .env so the stored OCS creds decrypt under the real prod SECRET_KEY.
# Running outside systemd otherwise falls back to the dev key and decryption
# fails (see the SECRET_KEY notes).
#
# Manual run:  sudo -u terroir /opt/terroir-ops/deploy/run_delivery_probe.sh
# Cron (twice daily in terroir's crontab — ~evening + ~late-morning Toronto):
#   0 1  * * * /opt/terroir-ops/deploy/run_delivery_probe.sh
#   0 14 * * * /opt/terroir-ops/deploy/run_delivery_probe.sh
set -u
cd /opt/terroir-ops || exit 1
set -a
. /opt/terroir-ops/.env
set +a
export TERROIR_DB=/opt/terroir-ops/terroir.db
export PYTHONUTF8=1
mkdir -p logs
echo "===== delivery-date probe $(date -u +%FT%TZ) =====" >> logs/delivery_date_probe.cron.log
/opt/terroir-ops/.venv/bin/python -m jobs.delivery_date_probe >> logs/delivery_date_probe.cron.log 2>&1

#!/usr/bin/env bash
# Weekly competitor-menu refresh — runs ON THE DROPLET (Playwright installed there).
# Scrapes Olympus + Beaverton (Bradford) and imports into the production DB so the
# Competitor "Discount opportunities" panel stays current. Run as the `terroir`
# user (its venv + DB ownership). Scrapers run sequentially so only one headless
# Chromium is alive at a time (keeps memory in check on the 2GB box).
#
# Manual run:   sudo -u terroir /opt/terroir-ops/deploy/scrape_competitors.sh
# Cron (weekly, Mondays 4am) — in terroir's crontab:
#   0 4 * * 1 /opt/terroir-ops/deploy/scrape_competitors.sh
set -u
cd /opt/terroir-ops || exit 1
export TERROIR_DB=/opt/terroir-ops/terroir.db
export PYTHONUTF8=1
PY=/opt/terroir-ops/.venv/bin/python
mkdir -p logs
LOG=/opt/terroir-ops/logs/competitors.log

echo "===== competitor refresh $(date -u +%FT%TZ) =====" >> "$LOG"
"$PY" jobs/scrape_olympus.py            >> "$LOG" 2>&1
"$PY" jobs/scrape_beaverton_bradford.py >> "$LOG" 2>&1
"$PY" jobs/import_competitor_prices.py  >> "$LOG" 2>&1
echo "===== done $(date -u +%FT%TZ) =====" >> "$LOG"

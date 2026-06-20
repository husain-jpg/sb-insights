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

# Prune stale rows: keep only each competitor's freshest scrape batch. Needed
# because a scraper's variant_id formula can change between versions (so old rows
# don't supersede new ones) and delisted products would otherwise linger forever.
# Guarded: only prune a competitor whose newest batch has >= 50 rows, so a failed
# or partial scrape can never wipe the last good data.
"$PY" - >> "$LOG" 2>&1 <<'PYEOF'
import os, sqlite3
c = sqlite3.connect(os.environ.get("TERROIR_DB", "db/sbinsights.db"))
for (comp,) in c.execute("SELECT DISTINCT competitor_name FROM competitor_prices").fetchall():
    m = c.execute("SELECT MAX(collected_at) FROM competitor_prices WHERE competitor_name=?", (comp,)).fetchone()[0]
    fresh = c.execute("SELECT COUNT(*) FROM competitor_prices WHERE competitor_name=? AND collected_at=?", (comp, m)).fetchone()[0]
    if fresh < 50:
        print(f"prune: SKIP {comp} (newest batch only {fresh} rows)"); continue
    cur = c.execute("DELETE FROM competitor_prices WHERE competitor_name=? AND collected_at<?", (comp, m))
    print(f"prune: {comp} kept {fresh} @ {m}, deleted {cur.rowcount} stale")
c.commit(); c.close()
PYEOF
echo "===== done $(date -u +%FT%TZ) =====" >> "$LOG"

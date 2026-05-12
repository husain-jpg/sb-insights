# SB Insights — Operational Runbook

This document is for whoever is keeping the system running day-to-day. Written
for non-developers — if you can edit a spreadsheet and use a terminal, you can
follow this.

## What this system is

SB Insights is a local web dashboard for SB Cannabis. It pulls data from Cova
POS and OCS, computes reorder suggestions, tracks dead stock, surfaces gaps in
your assortment vs. peer stores, and packages monthly reports for collectives.

It runs entirely on your laptop (Windows). No cloud yet. Database is a single
SQLite file at `C:\terroir-ops\terroir.db`.

---

## Daily checklist

Once a day (ideally morning):

- [ ] Open dashboard: `cd C:\terroir-ops` then `python run.py`
- [ ] Browser to `http://127.0.0.1:8000`
- [ ] Click **Admin → System Health**
- [ ] Look at "Per-store coverage" — every active store should have IOH age ≤ 2
      days and Sales age ≤ 2 days. Anything red means a Cova export didn't run
      for that store.
- [ ] Look at "Backups" — should show a backup from the previous calendar day.
      If not, click "Backup now" to force one.

If everything green: done. Move on.

If something is red: see "Common problems" below.

---

## Bi-weekly checklist (every 2 weeks)

- [ ] Pull OCS Market Intelligence reports for each active store.
      Use a **trailing 30-day window** in the OCS export date picker.
      Two files per store:
      - `3_2_Average_Sales_Units_per_Store_by_Municipality.xlsx`
      - `2_2_Sales_Velocity_-_Average_Daily_Sales_Units_per_Store_by_Municipality.xlsx`
- [ ] Drop them into `imports\market_intelligence\YYYY-MM-DD\StoreName\`
      where `YYYY-MM-DD` is today and `StoreName` matches the store
      (Bradford, Amherstview, etc.)
- [ ] Open dashboard → Inventory Reports → Market Intelligence → Imports
- [ ] Click "⚡ Import all"
- [ ] Review the new Suggested Additions section at the top of Reorder Report

---

## Monthly checklist

- [ ] On the 1st of the month, send Monthly Reports to data collectives
- [ ] Drop Cova exports (Diagnostic Report + Sales by Product) into
      `imports\monthly_reports\YYYY-MM\StoreName\` for each store
- [ ] Dashboard → Admin → Monthly Reports → click Package All Stores for the
      relevant collective
- [ ] Email the resulting zip to the collective contact

---

## Common problems and fixes

### "Page won't load" / "Connection refused"

The server isn't running. Open a Command Prompt:

```
cd C:\terroir-ops
python run.py
```

Leave the window open while you work. Closing it stops the server.

### "Dashboard says my Cova IOH data is X days old"

Cova auto-export has stopped firing. Two paths:

**Quick fix:** Manually export Inventory On Hand from Cova for each affected
store and drop the .xlsx files into `imports\` (root). The next dashboard
refresh will pick them up.

**Long-term fix:** Log into Cova admin → Reports → Scheduled Exports → confirm
the IOH export schedule still exists and is enabled.

### "Sales data is stale"

Same idea but for sales. Cova Itemized Sales auto-export is misfiring. Manual
export from Cova → drop into `imports\` → done.

### "Reorder Report shows a 500 error"

Something in the engine broke. Check the terminal window where the server is
running — it'll have a Python traceback.

If you can't make sense of it: stop the server (Ctrl+C), copy the error,
restart with `python run.py`, and forward the error to whoever maintains this.

### "Settings I changed don't seem to be taking effect"

Settings should apply on the next /api/reorder call (i.e., next time you load
the Reorder Report). If they're not:

1. Admin → Settings → check the value displayed there matches what you wanted
2. Admin → Settings → check the audit trail at the bottom shows your change
3. Hard-refresh the browser (Ctrl+Shift+R)
4. Reload the Reorder Report

If still not working: there's a bug. Stop tweaking settings and report it.

---

## How to restore from backup

Backups live in `C:\terroir-ops\backups\` as gzipped files named
`terroir-YYYY-MM-DD.db.gz`.

To restore:

1. **Stop the server** (Ctrl+C in the terminal window)
2. **Rename the current DB** as a safety net:
   ```
   ren terroir.db terroir-broken-YYYY-MM-DD.db
   ```
3. **Pick the backup you want**, e.g. `terroir-2026-05-08.db.gz`
4. **Decompress it.** PowerShell can do this:
   ```
   Expand-Archive -LiteralPath backups\terroir-2026-05-08.db.gz -DestinationPath .
   ```
   Or use 7-Zip / WinRAR to extract the `.gz` file.
5. **Move/rename** the resulting `terroir-2026-05-08.db` to `terroir.db` in
   the project root.
6. **Restart** the server: `python run.py`

The dashboard now reflects the data as of that backup's date. Any sales /
inventory imports that happened after the backup will need to be re-run.

---

## How to re-import data sources

If a data source is corrupted or out of date and you need to refresh:

### Cova IOH (Inventory On Hand)

Drop the .xlsx file into `C:\terroir-ops\imports\` — the dashboard will
auto-detect on next refresh and import. Old snapshots aren't deleted; new ones
are added.

### Cova Itemized Sales

Same as IOH — drop the .csv into `imports\`.

### OCS Catalog

If the catalog seems out of date (missing newly listed products), manually
re-import from the OCS portal export. Dashboard → Admin → Data Partners →
Re-seed (preserves your manual edits).

### OCS Market Intelligence

Re-export from OCS portal, drop in `imports\market_intelligence\YYYY-MM-DD\StoreName\`,
import via dashboard. New imports don't overwrite old ones — the dashboard uses
the latest by default.

### Monthly Reports

These are passthrough — drop Cova exports into the matching folder, package via
the UI when you need to send to a collective. Re-importing means re-running the
Cova export.

---

## Where everything lives

```
C:\terroir-ops\
├── api\                    # Server code (FastAPI app + dashboard HTML)
├── jobs\                   # Importers (Cova exports, OCS, market intel, etc.)
├── db\                     # Database schema + migrations
├── imports\                # Drop new data files here
│   ├── market_intelligence\YYYY-MM-DD\StoreName\
│   ├── monthly_reports\YYYY-MM\StoreName\
│   └── (raw Cova exports go here, in the root)
├── backups\                # Auto-created nightly DB backups
├── run.py                  # Entry point — `python run.py` starts the server
└── terroir.db              # The database file (single file, contains everything)
```

---

## Who to contact

- Cova issues (data not flowing, export errors): Cova support
- OCS portal issues (missing exports, login problems): OCS B2B account manager
- Dashboard bugs / unexpected behavior: whoever maintains this system

---

## What this runbook does NOT cover

- Setting up a new computer (clone the project, install Python, install
  dependencies — separate setup doc needed)
- Migrating to cloud hosting
- Adding new users or authentication
- Major schema changes

For any of those, you'll need a developer.

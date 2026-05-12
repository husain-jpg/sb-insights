# SB Insights — Launch Checklist

**Status**: Locked plan, Day 10-14 timeline, slip risk to Day 14-17.

---

## Launch Configuration (Locked)

- **Hosting**: DigitalOcean Toronto droplet, Basic 4GB RAM / 2 CPU / 80GB SSD ($24/mo)
- **Backups**: DigitalOcean weekly snapshots ($4.80/mo)
- **Security**: Cloudflare Tunnel — droplet IP hidden, free SSL, DDoS protection
- **Domain**: `sbinsights.ca` ($15/yr, register at any registrar)
- **URL**: `https://sbinsights.ca`
- **Database**: Postgres (migrating from local SQLite during launch)
- **Day 1 users**: 3 (user + GM + systems manager)
- **Auth**: Pre-created accounts with bcrypt passwords. No signup, no password reset flow Day 1.
- **Total monthly cost**: ~$30/mo + $15/yr domain

---

## YOUR TASKS (this week)

### Data tasks (do these in any order)

- [ ] **Re-import Cova Itemized Sales** with the new schema
  - All 8 stores
  - Jan 1, 2024 → today
  - **Format: CSV** (much faster than xlsx for big files; xlsx hits row limits at this scale)
  - All 41 columns required (don't use any "minimal" export option)
  - If Cova caps export size, split by quarter — multiple files in `imports/` is fine
  - Run `python run.py` after dropping in `imports/`
  - Verify with `python jobs/export_data.py status` — should show ~1.5-3M rows in `sale_lines`

- [ ] **Current Inventory On Hand per store**
  - One `Inventory_On_Hand_by_Product_*.xlsx` per store
  - This is the single point-in-time snapshot the reorder engine reads
  - Drop into `imports/`, run `python run.py`

- [ ] **Bulk OCS invoice upload** (in progress)
  - Drop OCS invoice zip files directly into `imports/`
  - Importer auto-extracts and processes
  - All 8 stores, back to Jan 2024 ideally

- [ ] **Optional: Historical Inventory snapshots**
  - 1-2 historical exports per store (e.g., 6 months ago, 12 months ago)
  - Cova's "Inventory by Product Historical" report
  - Enables backtesting and order-outcome analysis at higher quality
  - Not required for launch, can add later

- [ ] **Optional: Historical Cova Discounts pull**
  - All Discounts reports back to Jan 2024
  - Enables year-over-year discount trending in the new Analytics tab
  - If the bulk pull is hard, daily auto-export (below) accumulates forward-looking data

### Cova auto-exports (set up once, run daily forever)

- [ ] **Set up dedicated email inbox** (e.g., `imports@sbcannabis.ca`)
  - Google Workspace alias works fine
  - Don't use your main inbox — keep this isolated
  - Used as the destination for all Cova scheduled exports

- [ ] **Configure Cova scheduled exports** to that inbox:
  - **Itemized Sales** — daily at ~3 AM, previous day only
  - **Inventory On Hand by Product** — daily at ~4 AM, current snapshot
  - **Inventory by Product Historical** — weekly (Monday morning), current snapshot
  - **Discounts** — daily at ~3 AM, previous day only
  - All 8 stores in each export
  - Test ONE export first before scheduling all four

### Cloud setup (do these but don't provision yet)

- [ ] **Create DigitalOcean account** with billing set up
  - Don't provision a droplet yet — wait for me to give exact spec

- [ ] **Create Cloudflare account** (free tier)
  - Don't add the domain yet — wait until launch prep

- [ ] **Buy sbinsights.ca**
  - Any registrar works (Cloudflare Registrar, Namecheap, Google Domains, etc.)
  - Cloudflare Registrar is slightly cheaper if you plan to use Cloudflare anyway
  - Don't configure DNS yet — wait until launch prep

### OCS automation (separate track, gather info)

- [ ] **Email OCS account manager** about API/automation:
  > "We're building internal tools that need fresh sales/inventory data daily. Two questions:
  > 1. Do you support scheduled exports (daily) of catalog and invoice data — delivered via email, SFTP, or cloud storage?
  > 2. If not, do you have an API for retrieving these reports programmatically?"
  - Their answer determines OCS automation approach (post-launch work)

---

## MY TASKS (in order)

### Pre-launch (before Week 2 starts)

- [x] Brand Partners CRUD APIs and tables
- [x] Order Outcomes tab (per-invoice + per-SKU rolling)
- [x] Backup/export utility (`jobs/export_data.py`)
- [x] Auto-zip extraction in importer
- [x] `sale_lines` schema + line-level sales import
- [x] Partner Deals UI (brands, deals, LTOs)
- [x] Discounts report parser + ingestion
- [x] Analytics tab UI (date presets/custom + YoY + Excel export)
- [x] Canna Collective buysheet importer + Reorder tab data fee badge
- [x] IRC + Seeker buysheet importers (May 2026 General Listings + monthly Seeker)
- [x] Hierarchy resolver: Brand/LP-Direct > IRCC > Canna Collective > Seeker
- [x] LP capture from CC + IRC buysheets, persisted to products.lp

### Week 2 — launch prep

- [ ] Cloud server provisioning script (Postgres, nginx, systemd, cloudflared)
- [ ] SQLite → Postgres migration tool
- [ ] Email watcher service (poll inbox, drop attachments into imports/, run importer)
- [ ] Auth scaffolding (bcrypt, sessions, 3 hardcoded accounts)
- [ ] Health check endpoint + basic monitoring

### Launch day

- [ ] Provision droplet
- [ ] Migrate data SQLite → Postgres
- [ ] Configure Cloudflare Tunnel + DNS
- [ ] Cut over
- [ ] Parallel run with laptop version for 4-5 days
- [ ] Decommission laptop version

### Post-launch (Week 3+, in priority order)

- [ ] Brand report generator (per-brand sales reports)
- [ ] Pricing strategy script (ad-hoc, methodology locked)
- [ ] Suggested promotions tab (needs scoping)
- [ ] Discounts tab (when ~3 weeks of Cova Discounts data accumulated)
- [ ] OCS portal automation (depends on OCS rep response)
- [ ] Discounted Cannabis Barrie scraper (depends on provider)
- [ ] Per-category engine tuning
- [ ] Full UI for ratings/comments/anchor overrides (currently API-only)
- [ ] Phase 2: full team rollout, role-based access, real auth flow

---

## Realistic Risks That Could Slip Timeline

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Bulk invoice upload format quirks | Medium | 1 day | Importer logs warnings, doesn't crash |
| Sales re-import surfaces data quirks | Medium | 1-2 days | Idempotent — re-run safe after fix |
| Cova auto-exports first-day issues | Medium | 0 days | Manual download still works while debugging |
| Postgres migration type coercions | Low-Medium | 1 day | Migration script logs and continues |
| DNS / SSL / hosting setup delays | Low | 0-1 day | Do early in week 2 |

If everything's clean: ready Day 9-10. With normal hiccups: Day 10-14. Worst realistic case: Day 14-17.

---

## Operating Constraints Day 1

- **3 user accounts only**, pre-created, no signup
- **All 3 see everything** — no role-based restrictions yet
- **Bugs are tolerable** — internal trusted users, not customer-facing
- **Use any network** — Cloudflare Tunnel HTTPS means no plain-text data even on hotel wifi

---

*This file lives in the project package. It updates whenever I ship a new build.*

# SB Insights — Production Deploy Runbook

Step-by-step to launch `app.sbinsights.co` on a DigitalOcean Droplet so Eric, Sophia, and you can hit it from any laptop.

**Total time:** ~60-90 minutes if everything cooperates. DNS propagation is usually the longest single wait.

---

## Pre-flight (do this first, on your laptop)

You'll need:
- DigitalOcean account (you have one)
- Domain `sbinsights.co` registered + DNS access (Cloudflare, Namecheap, etc.)
- SSH key on your laptop — DO will add this to the droplet automatically on creation
- This repo checked out at `C:\terroir-ops`
- The current `terroir.db` file (your local DB)

---

## Step 1 — Create the droplet (5 min)

In DigitalOcean control panel:

1. **Create → Droplets**
2. **Image:** Ubuntu 24.04 LTS
3. **Plan:** Basic, Regular (Premium AMD/Intel = nicer), **$12/mo (2 GB RAM / 1 CPU / 50 GB SSD)** — that's the minimum that comfortably holds the 1.35 GB DB + Python + nginx with headroom
4. **Datacenter:** Toronto (TOR1) — closest to your stores, lowest latency
5. **Authentication:** SSH Key (add your laptop's public key if not already saved)
6. **Hostname:** `terroir-prod-1`
7. **Tags (optional):** `prod`, `sb-insights`
8. **Create**

Note the public IP that gets assigned. You'll use it everywhere as `$DROPLET_IP`.

---

## Step 2 — Point DNS at the droplet (5 min + propagation wait)

In your DNS provider's panel (Cloudflare/Namecheap/etc.):

- Add an **A record**:
  - **Name:** `app`
  - **Type:** `A`
  - **Value:** the droplet IP
  - **TTL:** 300 (5 min) for the launch — bump to 3600 later
  - **Proxy:** OFF if using Cloudflare (Let's Encrypt needs to see the real droplet IP for cert validation)

Verify from your laptop:
```bash
dig app.sbinsights.co +short
```
Should return the droplet IP. If it doesn't, wait 5-15 min and try again — don't proceed until this resolves.

---

## Step 3 — Bootstrap the droplet (10 min)

SSH in:
```bash
ssh root@$DROPLET_IP
```

Pull and run the setup script. Two options:

**Option A — clone from GitHub directly** (only if your repo is public or you've added a deploy key):
```bash
cd /tmp
git clone https://github.com/husain-jpg/sb-insights.git /opt/terroir-ops
bash /opt/terroir-ops/deploy/01_setup_droplet.sh
```

**Option B — push code from laptop first** (works for private repos):

From your laptop:
```bash
# Set the IP once for this terminal session
export DROPLET_IP=<droplet-ip>

# Push the repo over rsync
bash deploy/02_push_code.sh
```

Then on the droplet:
```bash
bash /opt/terroir-ops/deploy/01_setup_droplet.sh
```

The script installs Python 3.12, nginx, certbot, sqlite3, creates a `terroir` system user, installs Python deps in a venv, sets TZ=America/Toronto, opens firewall ports 22/80/443.

---

## Step 4 — Generate the production .env (1 min)

On the droplet:
```bash
bash /opt/terroir-ops/deploy/03_seed_env.sh
```

This generates a fresh `SECRET_KEY` (Fernet key, used for session signing AND for encrypting stored OCS/email credentials). **Save the printed SECRET_KEY in your password manager** — it's required for disaster recovery, and you can't rotate it without losing encrypted creds.

---

## Step 5 — Migrate the local DB to the droplet (5-15 min depending on connection)

From your laptop:
```bash
export DROPLET_IP=<droplet-ip>
bash deploy/04_migrate_db.sh
```

What this does:
1. Takes a clean snapshot via `sqlite3 .backup` (safer than copying a live DB file)
2. Compresses + scp's it to the droplet (1.35 GB → ~250-400 MB gzipped)
3. Decompresses, enables WAL mode, runs schema migrations
4. Prints row counts for sanity check — compare against your local

**Expected:** sales_daily, discount_lines, inventory_snapshots, ocs_catalog, products, users counts should match your local DB exactly.

---

## Step 6 — Install nginx config + HTTPS cert (5 min)

On the droplet:
```bash
bash /opt/terroir-ops/deploy/05_nginx_https.sh
```

This:
- Verifies DNS for `app.sbinsights.co` resolves to this droplet (aborts otherwise)
- Installs the nginx site config
- Runs `certbot --nginx` to get a Let's Encrypt cert + auto-redirect HTTP→HTTPS
- Enables auto-renewal via the certbot.timer systemd unit

After this, https://app.sbinsights.co will respond with a 502 (no upstream yet) — that's expected. Step 7 brings the app up.

---

## Step 7 — Install the systemd service + start the app (1 min)

On the droplet:
```bash
bash /opt/terroir-ops/deploy/06_install_service.sh
```

Probes `/healthz` at the end. If you see `HTTP 200`, you're live.

Tail logs to watch the startup:
```bash
journalctl -u terroir-ops -f
```

You should see:
- `Application startup complete`
- Background backup thread starting
- Scheduled scraper thread starting
- OCS connector thread starting (initially inactive — needs creds re-entered)
- Successor map building in the background

---

## Step 8 — Bootstrap GM admin accounts (1 min)

On the droplet:
```bash
bash /opt/terroir-ops/deploy/07_bootstrap_admins.sh
```

This creates:
- `husain@starbuds.co`
- `eric.dawes@starbuds.co`
- `sophia@starbuds.co`

All as **admin** role with `must_change_password=1` (they're forced to set a new password on first login). The temp passwords are printed to stdout — **save them in a password manager** and share with each GM via Signal, in person, or another secure channel. Do NOT email them.

---

## Step 9 — Re-enter OCS + email scraper credentials (5 min)

The credentials in the migrated DB were encrypted with your local `SECRET_KEY` (which was the "insecure fallback" since none was set). On the cloud box with a different real `SECRET_KEY`, they won't decrypt. You need to re-enter them via the UI:

1. Log in as `husain@starbuds.co`
2. Settings → **Email Scraper** → re-enter Gmail App Password + verify
3. Settings → **OCS Connector** → re-enter OCS B2B login → Validate → Save
4. Settings → OCS Connector → toggle **Active = ON**
5. Click "Run Now" to do a one-off sync, verify it pulls catalog + order exports

---

## Step 10 — Smoke test as a GM (5 min)

1. Hit https://app.sbinsights.co in a fresh browser (or incognito)
2. Log in as one of the GMs with their temp password
3. Set a new password
4. Walk through:
   - **Overview** — KPIs + store performance
   - **Reorder Report** — pick a store, confirm SKUs load with rebate badges
   - **Promotions** — filters work, KPI cards populate
   - **OCS Catalogue** — search + filters work, "Latest catalogue update" shows in header
   - **Financial / Analytics** — date ranges + YoY toggle work

If any tab shows an error, tail the journal: `journalctl -u terroir-ops -n 100 | grep -i error`

---

## Step 11 — Set up offsite backups (10 min) — DO THIS WITHIN THE FIRST WEEK

You have nightly local backups already (the in-process scheduler at 3am). For production, push them to DO Spaces:

1. In DO control panel: **Spaces → Create**, name `sb-insights-backups`, Toronto (TOR1)
2. Generate a Spaces Access Key
3. On droplet:
   ```bash
   apt-get install -y s3cmd
   s3cmd --configure  # paste the access key + secret
   ```
4. Cron entry (`crontab -e -u terroir`):
   ```
   30 3 * * * cd /opt/terroir-ops && /opt/terroir-ops/.venv/bin/python -c "from jobs.backup import push_to_spaces; push_to_spaces()"
   ```

I haven't written `push_to_spaces()` yet — flag me when you're at this step and I'll add it. For the launch itself, the local nightly backups + DO snapshots are enough.

---

## What you give the GMs

Email them this once, on launch day:

```
Subject: SB Insights is live

Hey Eric / Sophia,

The dashboard is live at:
    https://app.sbinsights.co

Your login: <their email>
Temp password: <send via Signal, not email>

You'll be asked to set a new password on first login. Use something strong.

A few things to know upfront:
- Rebate $ figures on the Promotions tab are currently OVERSTATED for IRCC
  while we finish redesigning the receive-date qualification logic.
  Treat IRCC rebate $ as an upper bound for now.
- Everything else (sales, inventory, reorder suggestions, promo tiers,
  catalogue lookup) is current and accurate.
- Bug reports / feature requests welcome — just text me.

Husain
```

---

## Disaster recovery cheatsheet

**App won't start:**
- `journalctl -u terroir-ops -n 50` — look for tracebacks
- `systemctl status terroir-ops`
- Most common cause: bad `.env` syntax or missing dependency. Fix and `systemctl restart terroir-ops`

**Cert expired / HTTPS broken:**
- `certbot renew --dry-run` — test renewal
- `certbot renew --force-renewal` — force a renewal
- `systemctl reload nginx` after

**DB corrupted or rolled back:**
- Local backups live at `/opt/terroir-ops/backups/` — `ls -lt` shows newest
- Stop service, `cp <backup>.db.gz /opt/terroir-ops/terroir.db.gz && gunzip`, start service

**Locked out of admin:**
- SSH in, run `bash /opt/terroir-ops/deploy/07_bootstrap_admins.sh` again — it'll skip existing users and only create missing ones. To reset a password, edit the script's password reset path (or call me).

**Need to push a code update later:**
- From laptop: `DROPLET_IP=<ip> bash deploy/02_push_code.sh`
- On droplet: `systemctl restart terroir-ops`

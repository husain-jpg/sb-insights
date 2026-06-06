#!/usr/bin/env bash
# Step 1: Bootstrap a fresh DigitalOcean Droplet (Ubuntu 24.04 LTS) for SB Insights.
# Run as root on the droplet. Safe to re-run — every step is idempotent.
#
# Prerequisites on the droplet:
#   - SSH access as root (DO sets this up on creation)
#   - DNS A record for app.sbinsights.co pointed at the droplet IP
#
# Usage on droplet:
#   curl -fsSL https://raw.githubusercontent.com/<owner>/sb-insights/feat/ocs-connector/deploy/01_setup_droplet.sh | bash
# OR copy this file across, chmod +x, run.

set -euo pipefail

DOMAIN="app.sbinsights.co"
APP_USER="terroir"
APP_DIR="/opt/terroir-ops"
PYTHON_VERSION="3.12"     # Ubuntu 24.04 ships 3.12; matches your dev env close enough

echo "==> [1/8] System update"
apt-get update -y
apt-get upgrade -y

echo "==> [2/8] Install required packages"
apt-get install -y \
    git curl ufw fail2ban \
    python${PYTHON_VERSION} python${PYTHON_VERSION}-venv python3-pip \
    nginx \
    certbot python3-certbot-nginx \
    sqlite3

echo "==> [3/8] Create non-root app user"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "$APP_USER"
    echo "  created user $APP_USER"
else
    echo "  user $APP_USER already exists"
fi

echo "==> [4/8] Create app directory and clone repo"
mkdir -p "$APP_DIR"
chown "$APP_USER:$APP_USER" "$APP_DIR"
if [ ! -d "$APP_DIR/.git" ]; then
    echo "  cloning repo (you'll need to authenticate or pre-load via 02_push_code.sh)"
    sudo -u "$APP_USER" git clone https://github.com/husain-jpg/sb-insights.git "$APP_DIR" || {
        echo "  >>> Repo not cloneable as anonymous. Either:"
        echo "  >>> (a) Add a deploy key under your GitHub repo settings, OR"
        echo "  >>> (b) Run 02_push_code.sh from your laptop to scp the code over."
        echo "  >>> Skipping clone — you'll need to populate $APP_DIR manually."
    }
fi

echo "==> [5/8] Set up Python venv + install dependencies"
sudo -u "$APP_USER" bash -c "
    cd $APP_DIR
    python${PYTHON_VERSION} -m venv .venv
    .venv/bin/pip install --upgrade pip
    if [ -f requirements.txt ]; then
        .venv/bin/pip install -r requirements.txt
    else
        echo '  no requirements.txt yet — install pkgs manually after scp'
    fi
"

echo "==> [6/8] UFW firewall"
ufw allow OpenSSH
ufw allow 'Nginx Full'   # 80 + 443
yes | ufw enable || true
ufw status

echo "==> [7/8] Set timezone to America/Toronto"
timedatectl set-timezone America/Toronto
echo "  timedatectl: $(timedatectl | grep 'Time zone')"

echo "==> [8/8] Done with base setup"
echo
echo "NEXT STEPS:"
echo "  1. Push the code if step 4 didn't clone successfully (use 02_push_code.sh)."
echo "  2. Copy the production .env to $APP_DIR/.env (use 03_seed_env.sh)."
echo "  3. Migrate the database (use 04_migrate_db.sh from your laptop)."
echo "  4. Install nginx config + get HTTPS cert (use 05_nginx_https.sh)."
echo "  5. Install systemd unit + start service (use 06_install_service.sh)."
echo "  6. Bootstrap admin accounts (use 07_bootstrap_admins.sh)."

#!/usr/bin/env bash
# Step 2: Push code from your LAPTOP to the droplet (alternative to git clone).
# Use this if the droplet can't clone the GitHub repo directly.
#
# Usage on YOUR LAPTOP:
#   DROPLET_IP=<ip>  bash deploy/02_push_code.sh
#
# Assumes you can SSH to the droplet as root with key auth (default for DO).

set -euo pipefail

: "${DROPLET_IP:?Set DROPLET_IP=<your droplet IP> before running}"
APP_DIR="/opt/terroir-ops"
APP_USER="terroir"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> rsyncing repo to root@$DROPLET_IP:$APP_DIR"
# Exclude the local DB (huge + we migrate it separately), venv, processed/, secrets
rsync -azv --delete \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude 'imports/' \
    --exclude 'imports/processed/' \
    --exclude 'backups/' \
    --exclude 'terroir.db*' \
    --exclude '.env' \
    --exclude '.git/' \
    --exclude 'SESSION_NOTES_*' \
    ./ "root@${DROPLET_IP}:${APP_DIR}/"

echo "==> chown to $APP_USER"
ssh "root@${DROPLET_IP}" "chown -R ${APP_USER}:${APP_USER} ${APP_DIR}"

echo "==> install/update python deps"
ssh "root@${DROPLET_IP}" "
    cd ${APP_DIR}
    sudo -u ${APP_USER} .venv/bin/pip install --upgrade pip
    sudo -u ${APP_USER} .venv/bin/pip install -r requirements.txt
"

echo "==> done. Code is on the droplet."

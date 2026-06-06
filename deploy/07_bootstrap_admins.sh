#!/usr/bin/env bash
# Step 7: Create the GM admin accounts. Run on droplet as root.
# Prints temp passwords to stdout — copy them somewhere private (e.g.
# 1Password) and share with each GM out-of-band (Signal, in person, etc.).

set -euo pipefail

APP_DIR="/opt/terroir-ops"
APP_USER="terroir"

echo "==> Running bootstrap_admins.py"
cd "$APP_DIR"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" "$APP_DIR/deploy/bootstrap_admins.py"

echo
echo "Done. Test login flow:"
echo "  1. Open https://app.sbinsights.co"
echo "  2. Log in with one of the credentials above"
echo "  3. You'll be prompted to set a new password (must_change_password)"
echo "  4. Confirm the dashboard loads"

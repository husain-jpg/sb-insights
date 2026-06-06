#!/usr/bin/env bash
# Step 6: Install the systemd unit + start the service. Run on droplet as root.

set -euo pipefail

APP_DIR="/opt/terroir-ops"

echo "==> Install systemd unit"
cp "$APP_DIR/deploy/terroir-ops.service" "/etc/systemd/system/terroir-ops.service"
systemctl daemon-reload
systemctl enable terroir-ops
systemctl restart terroir-ops

echo "==> Sleep 4s for boot"
sleep 4

echo "==> Service status"
systemctl --no-pager status terroir-ops | head -20

echo "==> Probe /healthz via nginx"
curl -fsSL -o /dev/null -w "  HTTP %{http_code} from https://app.sbinsights.co/healthz\n" \
    https://app.sbinsights.co/healthz

echo
echo "Done. Service is running and exposed via HTTPS."
echo "Tail logs with:  journalctl -u terroir-ops -f"
echo "Next: 07_bootstrap_admins.sh"

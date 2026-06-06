#!/usr/bin/env bash
# Step 5: Install nginx config + obtain Let's Encrypt cert. Run on droplet as root.
# Prerequisite: DNS A record for app.sbinsights.co MUST already resolve to this
# droplet's IP. Verify with: dig app.sbinsights.co +short

set -euo pipefail

DOMAIN="app.sbinsights.co"
APP_DIR="/opt/terroir-ops"

echo "==> [1/4] Sanity-check DNS"
RESOLVED=$(dig +short "$DOMAIN" | tail -n1)
DROPLET_IP=$(curl -s4 https://ifconfig.io)
echo "  $DOMAIN resolves to: ${RESOLVED:-(empty)}"
echo "  this droplet's public IP: $DROPLET_IP"
if [ -z "$RESOLVED" ]; then
    echo "  DNS not propagated yet. Set the A record for $DOMAIN to $DROPLET_IP and wait a few minutes."
    exit 1
fi
if [ "$RESOLVED" != "$DROPLET_IP" ]; then
    echo "  DNS points to $RESOLVED, not to this droplet ($DROPLET_IP). Fix DNS before continuing."
    exit 1
fi
echo "  OK"

echo "==> [2/4] Install nginx site config"
cp "$APP_DIR/deploy/nginx.conf" "/etc/nginx/sites-available/$DOMAIN"
ln -sf "/etc/nginx/sites-available/$DOMAIN" "/etc/nginx/sites-enabled/$DOMAIN"
# Disable default landing if it exists
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo "==> [3/4] Obtain Let's Encrypt cert + add TLS server block"
# --redirect: force HTTP -> HTTPS
# --agree-tos: agree to LE TOS
# --no-eff-email: skip the EFF newsletter signup
certbot --nginx -d "$DOMAIN" \
    --non-interactive --agree-tos --no-eff-email \
    --email "husain@starbuds.co" \
    --redirect

echo "==> [4/4] Verify cert renewal timer is active"
systemctl list-timers | grep -i certbot || systemctl enable --now certbot.timer

echo
echo "Done. https://$DOMAIN should now serve via nginx -> uvicorn (once service is up)."
echo "Next: 06_install_service.sh"

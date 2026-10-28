#!/usr/bin/env bash
# Installs or updates codec399 on Ubuntu 22.04 / 24.04. Run as root from a checkout of the repo:
#   sudo bash deploy/install.sh codec.example.com
# Safe to re-run: it re-syncs the code, reinstalls requirements and restarts the service.
set -euo pipefail

DOMAIN="${1:-_}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/codec399
DATA=/var/lib/codec399
ENV=/etc/codec399.env

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip ffmpeg nginx rsync
ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libvpx-vp9 || {
    echo "This ffmpeg has no libvpx-vp9 encoder; install one that does and set FFMPEG in $ENV." >&2
    exit 1
}

id -u codec399 >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin codec399
mkdir -p "$APP" "$DATA"

rsync -a --delete --exclude .git --exclude jobs --exclude .venv --exclude __pycache__ --exclude .claude \
    "$SRC/" "$APP/"
[ -x "$APP/.venv/bin/python" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --quiet --upgrade pip
"$APP/.venv/bin/pip" install --quiet -r "$APP/deploy/requirements-server.txt"
chown -R codec399:codec399 "$APP" "$DATA"

if [ ! -f "$ENV" ]; then
    PASS="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
    sed "s|^CODEC399_PASSWORD=.*|CODEC399_PASSWORD=$PASS|" "$APP/deploy/codec399.env.example" > "$ENV"
    chmod 600 "$ENV"
    echo "Wrote $ENV with a generated password."
    echo "  user: codec399"
    echo "  password: $PASS"
fi

install -m 644 "$APP/deploy/codec399.service" /etc/systemd/system/codec399.service
sed "s|server_name _;|server_name $DOMAIN;|" "$APP/deploy/nginx.conf" > /etc/nginx/sites-available/codec399
ln -sf /etc/nginx/sites-available/codec399 /etc/nginx/sites-enabled/codec399
rm -f /etc/nginx/sites-enabled/default
nginx -t

systemctl daemon-reload
systemctl enable codec399 >/dev/null
systemctl restart codec399
systemctl reload nginx

echo
echo "codec399 is running behind nginx on port 80 (server_name $DOMAIN)."
echo "Check:  systemctl status codec399   and   journalctl -u codec399 -f"
echo "HTTPS:  apt-get install -y certbot python3-certbot-nginx && certbot --nginx -d $DOMAIN"

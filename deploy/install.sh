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
ffmpeg -hide_banner -encoders 2>/dev/null | grep libvpx-vp9 >/dev/null || {
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
    install -m 600 "$APP/deploy/codec399.env.example" "$ENV"
    echo "Wrote $ENV."
fi

install -m 644 "$APP/deploy/codec399.service" /etc/systemd/system/codec399.service
install -m 644 "$APP/deploy/codec399-bot.service" /etc/systemd/system/codec399-bot.service
SITE=/etc/nginx/sites-available/codec399
if [ -f "$SITE" ] && grep -q "managed by Certbot" "$SITE"; then
    # certbot --nginx has added the HTTPS server block and redirect to this file; rewriting it
    # from the template would drop them, so an update leaves the site as certbot left it.
    echo "Keeping $SITE (it carries certbot's HTTPS configuration)."
else
    sed "s|server_name _;|server_name $DOMAIN;|" "$APP/deploy/nginx.conf" > "$SITE"
fi
ln -sf "$SITE" /etc/nginx/sites-enabled/codec399
rm -f /etc/nginx/sites-enabled/default
nginx -t

systemctl daemon-reload
systemctl enable codec399 >/dev/null
systemctl restart codec399
systemctl reload nginx
# The Discord bot runs only once its token is in the env file (see deploy/PAYMENTS.md).
if grep -Eq '^DISCORD_BOT_TOKEN=.+' "$ENV"; then
    systemctl enable codec399-bot >/dev/null
    systemctl restart codec399-bot
    echo "Discord bot restarted; check: journalctl -u codec399-bot -f"
else
    systemctl disable --now codec399-bot >/dev/null 2>&1 || true
    echo "DISCORD_BOT_TOKEN is not set in $ENV, so the Discord bot is not running (deploy/PAYMENTS.md)."
fi

echo
echo "codec399 is running behind nginx on port 80 (server_name $DOMAIN)."
echo "Check:  systemctl status codec399   and   journalctl -u codec399 -f"
echo "HTTPS:  apt-get install -y certbot python3-certbot-nginx && certbot --nginx -d $DOMAIN"
if [ ! -f "$DATA/users.json" ]; then
    echo
    echo "No accounts yet. Create the admin account (you will be asked for the password):"
    echo "  cd $APP && sudo -u codec399 CODEC399_JOBS_DIR=$DATA .venv/bin/python manage.py add-user you@example.com --role admin"
fi

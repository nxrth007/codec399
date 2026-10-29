# Deploying codec399 to a VPS

Target: a Hostinger KVM VPS (or any Ubuntu 22.04 / 24.04 box) with the app behind nginx.
Encodes are long and use every core, so this is a dedicated-machine deployment, not a PaaS one.

## What is in this folder

| File | Purpose |
|---|---|
| `install.sh` | One-shot installer and updater: apt packages (ffmpeg with libvpx-vp9, nginx), system user, venv, env file, systemd units, nginx site |
| `codec399.service` | systemd unit running gunicorn with one worker and eight threads on 127.0.0.1:5399 |
| `codec399-bot.service` | systemd unit for the Discord bot (`discord_bot.py`); started only when `DISCORD_BOT_TOKEN` is set |
| `PAYMENTS.md` | Step-by-step setup of the Whop webhook, API key and the Discord bot |
| `nginx.conf` | Reverse proxy with a 16 GB upload limit, streaming uploads and downloads, hour-long timeouts |
| `codec399.env.example` | Settings the service reads from `/etc/codec399.env` |
| `requirements-server.txt` | App requirements plus gunicorn |

The app itself reads these environment variables:

| Variable | Effect |
|---|---|
| `CODEC399_USERS_FILE` | Accounts file, default `users.json` in the jobs directory (see Accounts below) |
| `CODEC399_SECRET_KEY` | Session-cookie signing key, default kept in `secret.key` next to the accounts file |
| `CODEC399_JOBS_DIR` | Where uploads and outputs live, default `jobs/` next to the code |
| `CODEC399_BEHIND_PROXY` | `1` to trust one reverse proxy's forwarded headers |
| `CODEC399_MAX_QUEUE` | Jobs queued or running before new uploads get a 429, default 40 |
| `CODEC399_CONCURRENT_JOBS` | Encodes running at the same time, default 2 (each uses about 1.5 cores) |
| `CODEC399_AUTO_FAST_SECONDS` | User uploads longer than this use Fast encode automatically, default 210 (3:30) |
| `CODEC399_USER_MAX_SECONDS` | User uploads longer than this are refused, default 360 (6:00); admins have no cap |
| `CODEC399_USER_MAX_BATCH` | Videos a user may upload per batch, default 8 |
| `CODEC399_WHOP_WEBHOOK_SECRET` | Secret of the Whop webhook that posts to `/api/whop/webhook`; unset, the endpoint answers 503 |
| `CODEC399_BOT_TOKEN` | Shared secret the Discord bot presents to `/api/bot/account`; unset, that endpoint answers 503 |
| `DISCORD_NOTIFY_WEBHOOK` | Optional Discord channel webhook; every Whop payment is announced there |
| `WHOP_*`, `DISCORD_*`, `CODEC399_PUBLIC_URL` | Checkout creation and the bot; see [PAYMENTS.md](PAYMENTS.md) |

The last four are starting values: admins change them on the Settings tab of the encoder screen,
and the saved values in `/var/lib/codec399/settings.json` win over the environment.
| `HOST`, `PORT` | Bind address for `python app.py`; gunicorn uses its own `--bind` |

Two jobs run at a time by default (a Settings-tab value), admin jobs first; a faithful encode uses
about one and a half cores. Faithful mode on a 1080x1920 source runs at about 12 slots per second,
so a two minute clip takes around an hour on four cores.

## Steps

1. Create the VPS with Ubuntu 24.04, add your SSH key, and open ports 22, 80 and 443 in the
   Hostinger firewall.
2. Point a DNS A record (for example `codec.yourdomain.com`) at the VPS IP.
3. On the VPS, as root:

   ```bash
   apt-get install -y git
   git clone <your repo url> /root/codec399
   bash /root/codec399/deploy/install.sh codec.yourdomain.com
   ```

   On the first run the installer ends by printing the command that creates the admin account;
   see Accounts below.
4. HTTPS, once DNS resolves:

   ```bash
   apt-get install -y certbot python3-certbot-nginx
   certbot --nginx -d codec.yourdomain.com
   ```

5. Open `https://codec.yourdomain.com`, sign in with the admin account, and upload a
   short clip with "Fast encode" ticked to confirm ffmpeg works end to end.

## Accounts

Every page needs a signed-in account. Accounts live in `/var/lib/codec399/users.json` with hashed
passwords and a role: `admin` gets the encoder screen with every job, `media_buyer` gets the
encoder screen's Upload, Processing and Finished tabs with only their own jobs, and `user` gets an
upload-only dashboard. Run `manage.py` as the service user so the file keeps the right owner:

```bash
cd /opt/codec399
sudo -u codec399 CODEC399_JOBS_DIR=/var/lib/codec399 .venv/bin/python manage.py add-user you@example.com --role admin
sudo -u codec399 CODEC399_JOBS_DIR=/var/lib/codec399 .venv/bin/python manage.py add-user someone@example.com
sudo -u codec399 CODEC399_JOBS_DIR=/var/lib/codec399 .venv/bin/python manage.py list-users
sudo -u codec399 CODEC399_JOBS_DIR=/var/lib/codec399 .venv/bin/python manage.py set-password you@example.com
```

`add-user` and `set-password` ask for the password without echo; `--password-stdin` reads it from
the first line of stdin instead. Changes take effect on the next request, no restart needed.

People can also register themselves from the sign-in page with an email, a password and their
Business Portfolio ID. That creates a `pending` account that cannot sign in until an admin accepts
it on the Approval tab of the encoder screen as a user or a media buyer (or with
`manage.py approve EMAIL [--role media_buyer]`); declining removes the request. The same tab
switches existing non-admin accounts between the two roles (`manage.py set-role`), and can revoke an
account: it can no longer sign in, open sessions end, and the addresses it was seen from (kept in
`blocked_ips.json`) are refused at registration until the account is restored (`manage.py revoke`
and `restore`). Addresses are read from nginx's forwarded header, so keep `CODEC399_BEHIND_PROXY=1`.

## Payments and upload credits

Every non-admin account has a balance of upload credits (one per uploaded video) or Gold
(unlimited). A paid Whop order credits the account: the Discord bot (`discord_bot.py`, set up in
[PAYMENTS.md](PAYMENTS.md)) posts the pricing in every ticket, checks that the email is registered
(`/api/bot/account`), and creates a Whop checkout with
metadata `{"email": <registered email>, "ads": N}` or `{"email": ..., "gold": true}`, Whop
sends `payment.succeeded` to `https://<domain>/api/whop/webhook`, and `whop.py` verifies the
signature, approves the account if it is still pending, and adds the credits. Gold is monthly: each
Gold payment extends `gold_until` by 31 days, and a membership event that says the subscription
ended drops it; an admin's Gold toggle has no expiry. Deliveries are
logged to `/var/lib/codec399/whop_events.log`; duplicates are ignored by webhook id. Admins can
also add credits or toggle Gold on the Approval tab (or `manage.py credits` / `manage.py gold`).

## Updating

```bash
cd /root/codec399 && git pull && bash deploy/install.sh codec.yourdomain.com
```

A restart cancels any encode in progress; finished outputs in `/var/lib/codec399` survive.

## Operating

```bash
systemctl status codec399
journalctl -u codec399 -f
du -sh /var/lib/codec399          # outputs accumulate until deleted from the Finished tab
nano /etc/codec399.env && systemctl restart codec399
```

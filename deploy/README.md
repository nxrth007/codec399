# Deploying codec399 to a VPS

Target: a Hostinger KVM VPS (or any Ubuntu 22.04 / 24.04 box) with the app behind nginx.
Encodes are long and use every core, so this is a dedicated-machine deployment, not a PaaS one.

## What is in this folder

| File | Purpose |
|---|---|
| `install.sh` | One-shot installer and updater: apt packages (ffmpeg with libvpx-vp9, nginx), system user, venv, env file, systemd unit, nginx site |
| `codec399.service` | systemd unit running gunicorn with one worker and eight threads on 127.0.0.1:5399 |
| `nginx.conf` | Reverse proxy with a 16 GB upload limit, streaming uploads and downloads, hour-long timeouts |
| `codec399.env.example` | Settings the service reads from `/etc/codec399.env` |
| `requirements-server.txt` | App requirements plus gunicorn |

The app itself reads these environment variables:

| Variable | Effect |
|---|---|
| `CODEC399_PASSWORD` | When set, every request needs HTTP Basic auth. Unset, the app is open (local use). |
| `CODEC399_USER` | Basic auth user name, default `codec399` |
| `CODEC399_JOBS_DIR` | Where uploads and outputs live, default `jobs/` next to the code |
| `CODEC399_BEHIND_PROXY` | `1` to trust one reverse proxy's forwarded headers |
| `CODEC399_MAX_QUEUE` | Jobs queued or running before new uploads get a 429, default 10 |
| `HOST`, `PORT` | Bind address for `python app.py`; gunicorn uses its own `--bind` |

Jobs run one at a time. Faithful mode on a 1080x1920 source runs at about 12 slots per second,
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

   The installer prints the generated password once. It is kept in `/etc/codec399.env`.
4. HTTPS, once DNS resolves:

   ```bash
   apt-get install -y certbot python3-certbot-nginx
   certbot --nginx -d codec.yourdomain.com
   ```

5. Open `https://codec.yourdomain.com`, sign in with user `codec399` and the password, and upload a
   short clip with "Fast encode" ticked to confirm ffmpeg works end to end.

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

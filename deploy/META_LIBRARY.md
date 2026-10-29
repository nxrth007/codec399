# Clients' videos in the Meta Media Library

Every finished video is copied into the client's own folder in the Ghost business's Media Library
(Business Manager → Media Library), and that folder is shared with the Business Portfolio ID the
client gave when they registered. The client then sees their edited ads inside their own Business
Manager, ready to pick in Ads Manager, without downloading anything.

```
registration approved ──► folder "<client email>" created in our Media Library
                           │  shared with the client's Business Portfolio ID
                           │  (they accept once: Business Settings → Requests)
                           ▼
encode finishes ──► output uploaded into that folder, named like the download
                           │  retried for up to a day if Meta is unreachable
                           ▼
Gallery shows "In their Meta Media Library folder"; the folder tile shows the share state
```

It runs inside the app, server-side, with Meta's Graph API. Nothing has to stay open anywhere.

## 1. What Meta needs from you (once)

All of this happens in the **andalus trading co** Business Manager, signed in as an admin of it.

1. **Business id.** Business Settings → Business info → the **Business Portfolio ID** (a long
   number). That is `META_BUSINESS_ID`.
2. **A Meta app.** Business Settings → Accounts → Apps → Add → Create a new app ID (type
   *Business*). Any name. Under the app's **App settings → Basic**, note nothing is needed; the
   app only exists so a System User token can be issued through it.
3. **A System User token.** Business Settings → Users → System users → Add → name it
   `ghostrus`, role **Admin** (admin is required for the creative folder endpoints). Then:
   - **Add assets** → Apps → your new app → *Manage app*.
   - **Generate new token** → pick the app, token expiry *Never*, and tick these permissions:
     `business_management`, `business_creative_management`, `ads_management`.
   - Copy the token straight into `/etc/codec399.env` as `META_ACCESS_TOKEN`. It is shown once.
4. Put both values in `/etc/codec399.env` and restart the app:

   ```
   META_BUSINESS_ID=123456789012345
   META_ACCESS_TOKEN=EAAB...
   ```

   ```bash
   cd /root/codec399 && bash deploy/install.sh ghostrus.xyz
   ```

5. Prove it works, from the server:

   ```bash
   set -a; . /etc/codec399.env; set +a; cd /opt/codec399 && .venv/bin/python manage.py meta-check
   ```

   It prints the business name and the folders already in the Media Library. If it prints an
   error mentioning permissions or capability, see *If Meta refuses* below.

## 2. What the client does (once)

The first time a folder is shared with a Business Portfolio, that business has to accept it:
**Business Settings → Requests → Received** → Approve. After that, further videos and folders
need nothing. Until they accept, their folder tile in the Gallery says *waiting for them to
accept*; the app checks again every time it uploads a video for them and on **Sync to Meta**.

If a client typed the wrong Business Portfolio ID at registration, correct it in `users.json`
(`portfolio_id`), then run `manage.py meta-sync EMAIL`; the folder is shared again with the new id.

## 3. Day to day

| Where | What |
|---|---|
| Approval tab → Approve | creates and shares the folder in the background |
| Encode finishes | the video is uploaded into the owner's folder; the Gallery card shows *Uploading to Meta…*, then *In their Meta Media Library folder*, or the error |
| Gallery → folder | *Open in Meta Media Library* link; the tile shows *shared* / *waiting for them to accept* / *sharing failed* |
| Gallery → **Sync to Meta** | creates any missing folders, re-checks pending shares, and queues every finished video that is not uploaded yet or failed. Use it once after setting the token to push the existing Gallery up |
| `manage.py meta-sync [EMAIL]` | the same from the shell (videos are queued through the app when `CODEC399_INTERNAL_URL` and `CODEC399_BOT_TOKEN` are in the environment) |
| `/var/lib/codec399/meta_library.log` | one line per folder created, share, upload, retry and failure |

Only videos whose owner is an active account **with a Business Portfolio ID** go up; admin and
account-less uploads stay in the Gallery only. A failed upload is retried after 30 s, 2 min,
10 min, 30 min and 1 h, then marked failed on the card until the next Sync. Uploads interrupted by
a restart resume on start-up. Nothing in this path can fail or delay an encode.

Videos removed from the Gallery are **not** removed from the Media Library; delete those in
Business Manager if needed.

## If Meta refuses

Meta documents the creative-folder endpoints as *"available to select partners only"*, and the
`business_creative_management` permission is not in the public permissions list. In practice a
System User token from an admin of the business, generated through the business's own app, is the
way to try it; `manage.py meta-check` tells you in one call whether this business has access. If
it answers with a permissions or capability error, the options are to ask your Meta rep to enable
Creative Asset Management for the business's app, or to share videos another way (for example an
ad account the client shares with you, uploaded through `/act_<id>/advideos`, which every
advertiser can use). Say which and the upload step can be switched; everything else stays.

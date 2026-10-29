# Hand-off to grokbot: putting finished videos in the Meta Media Library by browser

The site keeps a queue of finished videos that still have to reach their client's folder in the
**andalus trading co** Media Library, serves the files, and records what the agent did, so the
Gallery always shows which videos are in Meta and which are not. The agent does the Meta side in
a browser; the site never touches Meta itself.

Two ways in, both fine:

- **HTTP**: a bearer token (`CODEC399_EXPORT_TOKEN` in `/etc/codec399.env`) on the endpoints below.
- **Browser**: sign in to https://ghostrus.xyz as an admin account made for the agent
  (`manage.py add-user grokbot@ghostrus.xyz --role admin`); the same endpoints work with that
  session, and the Gallery has the same controls as buttons.

## The loop

```
every few minutes:
  GET /api/export/queue                       → videos not yet in Meta, oldest first
  for each item:
    if item.share_status != "shared":         → make sure the client's folder exists and is shared
        in Business Manager → Media Library: folder named item.folder (the client's email)
        Share → partner business id item.portfolio_id → send
        POST /api/export/accounts/<email> {"folder_url": "<link>", "share_status": "pending" or "shared"}
    GET item.file                             → the video, saved under item.filename
    upload it into that folder in the Media Library
    POST /api/export/<job_id> {"status": "uploaded", "by": "grokbot"}
    (or {"status": "error", "error": "<what went wrong>"} to leave it in the queue with a note)
```

The folder for a client is named after their **email**, and the Business Portfolio ID to share it
with is the one they registered with (`portfolio_id`). A share only needs accepting once per
client, on their side (Business Settings → Requests); until then record `"pending"`, and switch
it to `"shared"` when their business shows as a partner on the folder.

## Endpoints

All need `Authorization: Bearer <CODEC399_EXPORT_TOKEN>` or an admin session.

| | |
|---|---|
| `GET /api/export/queue` | `{"count": n, "items": [...]}`; add `?all=1` to include videos already marked uploaded |
| `GET /api/export/accounts` | every active client with a Business Portfolio ID: `email`, `portfolio_id`, `folder`, `folder_url`, `share_status`, `videos_pending` |
| `GET /api/export/<job_id>/file` | the MP4, with `Content-Disposition` naming it like the download |
| `POST /api/export/<job_id>` | `{"status": "uploaded"\|"error"\|"pending", "video_id"?: "...", "error"?: "...", "by"?: "grokbot"}` |
| `POST /api/export/accounts/<email>` | `{"folder_url"?: "...", "folder_id"?: "...", "share_status"?: "shared"\|"pending"\|"error"\|"none", "share_error"?: "..."}` |

A queue item:

```json
{
  "job_id": "3f9c…", "email": "client@example.com", "portfolio_id": "1234567890",
  "folder": "client@example.com", "folder_url": null, "share_status": "none",
  "filename": "promo_codecghost.mp4", "bytes": 48213344, "finished": 1761736000.0,
  "status": "pending", "error": null, "by": null, "file": "/api/export/3f9c…/file"
}
```

`status` is `pending` (never attempted), `error` (last attempt failed; `error` says why) or, with
`?all=1`, `uploaded`. Only videos whose owner is an active account with a Business Portfolio ID
are offered; admin uploads are not.

## What the Gallery shows

Each folder tile: *Meta folder shared* / *waiting for them to accept* / *sharing failed*, and in
the folder view an *Open in Meta Media Library* link plus a line to set the link and share state
by hand. Each finished video: *In their Meta Media Library folder* or the error, with **Mark in
Meta** / **Unmark Meta** buttons for corrections.

## Set-up

```
# /etc/codec399.env
CODEC399_EXPORT_TOKEN=<openssl rand -hex 32>
```

then `bash deploy/install.sh ghostrus.xyz`. Give the token to the agent (never commit it).

```bash
curl -s -H "Authorization: Bearer $CODEC399_EXPORT_TOKEN" https://ghostrus.xyz/api/export/queue
```

The built-in API sync (`deploy/META_LIBRARY.md`) writes the very same records, so the two can
coexist or either can be switched off; the queue only ever lists what is not marked uploaded.

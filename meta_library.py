"""Mirror finished videos into Meta's Media Library.

Every approved account gets a folder of its own in the Ghost business's Media Library (Business
Manager > Media Library), shared with the Business Portfolio ID the person registered with.  Each
video is uploaded into that folder as soon as its encode finishes, so the person finds it in their
own Business Manager, ready to use in ads.

Configuration (environment):

    META_ACCESS_TOKEN   a System User token from the Ghost business with business_management,
                        business_creative_management and ads_management
    META_BUSINESS_ID    the Ghost business (portfolio) id that owns the Media Library
    META_API_VERSION    Graph API version, default v25.0
    META_SHARE_TASKS    tasks granted on the shared folder, default MANAGE_CONTENT

Meta's side of this is the Business Creative Asset Management API: `POST /{business}/creative_folders`
creates a folder, `POST /{folder}/agencies` shares it with a partner business (who accepts it once,
in Business Settings > Requests), and `POST /{business}/videos` with `creative_folder_id` uploads a
video into it, in one request for small files and in chunks for large ones.

The account's folder record ({folder_id, folder_url, share_status, ...}) lives on the account in
users.json; a job's upload record ({status, video_id, error, ...}) lives in its meta.json under
"meta".  Everything is logged to `meta_library.log` next to the accounts file.  Nothing here can
fail an encode: an upload that fails is retried with growing waits and finally marked "error" so
an admin can push it again from the Gallery.
"""
import io
import json
import mimetypes
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import auth

ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "")
BUSINESS_ID = os.environ.get("META_BUSINESS_ID", "")
API_VERSION = os.environ.get("META_API_VERSION", "v25.0")
SHARE_TASKS = [t.strip() for t in (os.environ.get("META_SHARE_TASKS") or "MANAGE_CONTENT").split(",") if t.strip()]

GRAPH = "https://graph.facebook.com"
GRAPH_VIDEO = "https://graph-video.facebook.com"
TIMEOUT = 180
SINGLE_UPLOAD_MAX = 50 * 1024 * 1024     # larger files go up in chunks
CHUNK_RETRIES = 3
MAX_ATTEMPTS = 6                         # per video, across retries
BACKOFF = (30, 120, 600, 1800, 3600)     # seconds before attempt 2, 3, ...

DATA_DIR = os.path.dirname(auth.USERS_FILE) or "."
LOG_FILE = os.path.join(DATA_DIR, "meta_library.log")


class MetaError(RuntimeError):
    """A failed Graph API call; str() is Meta's message with its error code."""


def configured():
    """Names of the environment variables still missing; empty when the sync can run."""
    return [name for name, value in (("META_ACCESS_TOKEN", ACCESS_TOKEN), ("META_BUSINESS_ID", BUSINESS_ID)) if not value]


def _log(line):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")


# ---------------------------------------------------------------- Graph API

def _multipart(fields, files):
    boundary = "codec399" + uuid.uuid4().hex
    body = io.BytesIO()
    for name, value in fields.items():
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, (filename, data) in files.items():
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                   f"Content-Type: {ctype}\r\n\r\n".encode())
        body.write(data)
        body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())
    return body.getvalue(), f"multipart/form-data; boundary={boundary}"


def _request(method, path, params=None, files=None, host=GRAPH):
    """One Graph API call.  `params` go in the query string for GET and in the body otherwise;
    `files` is {field: (filename, bytes)} and makes the body multipart.  Returns the decoded JSON
    and raises MetaError for an API error or a connection problem.  The token never reaches the log."""
    params = dict(params or {})
    params["access_token"] = ACCESS_TOKEN
    url = f"{host}/{API_VERSION}{path}"
    headers = {"User-Agent": "codec399/1.0"}
    data = None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(params)
    elif files:
        data, headers["Content-Type"] = _multipart(params, files)
    else:
        data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            err = json.loads(raw).get("error") or {}
        except ValueError:
            err = {}
        message = err.get("error_user_msg") or err.get("message") or f"HTTP {exc.code}"
        code = err.get("code")
        raise MetaError(f"{message} (code {code})" if code is not None else message) from None
    except (urllib.error.URLError, OSError) as exc:
        raise MetaError(f"could not reach Meta: {exc}") from None
    try:
        return json.loads(raw)
    except ValueError:
        raise MetaError("Meta answered with something that is not JSON") from None


def check():
    """Prove the token and business id work: the business's name and how many folders it has."""
    missing = configured()
    if missing:
        raise MetaError("not configured; set " + ", ".join(missing))
    business = _request("GET", f"/{BUSINESS_ID}", {"fields": "id,name"})
    folders = _request("GET", f"/{BUSINESS_ID}/creative_folders", {"fields": "id,name", "limit": 500})
    return {"business_id": business.get("id"), "business_name": business.get("name"),
            "folders": [{"id": f.get("id"), "name": f.get("name")} for f in folders.get("data") or []]}


# ---------------------------------------------------------------- folders

def folder_name(account):
    return account["email"]


def _partner_has_access(folder_id, partner_id):
    listing = _request("GET", f"/{folder_id}/agencies", {"fields": "id,name", "limit": 100})
    return any(str(b.get("id")) == str(partner_id) for b in listing.get("data") or [])


def _share(email, meta, partner_id):
    """Share the folder with the person's Business Portfolio.  Meta answers "In Progress" until the
    partner accepts the request in their Business Settings; we remember that and look again later."""
    if meta.get("shared_with") == partner_id and meta.get("share_status") == "shared":
        return meta
    if meta.get("shared_with") == partner_id and meta.get("share_status") == "pending":
        if _partner_has_access(meta["folder_id"], partner_id):
            meta.update(share_status="shared", shared_at=time.time(), share_error=None)
            _log(f"{email}: portfolio {partner_id} accepted folder {meta['folder_id']}")
        return meta
    reply = _request("POST", f"/{meta['folder_id']}/agencies",
                     {"business": partner_id, "permitted_tasks": json.dumps(SHARE_TASKS)})
    pending = bool(reply.get("share_status"))
    meta.update(shared_with=partner_id, share_requested=time.time(), share_error=None,
                share_status="pending" if pending else "shared", shared_at=None if pending else time.time())
    _log(f"{email}: folder {meta['folder_id']} shared with portfolio {partner_id}"
         + (" (waiting for them to accept)" if pending else ""))
    return meta


def ensure_folder(email):
    """Create the account's Media Library folder once, then share it with the person's Business
    Portfolio.  Returns the account's folder record.  A sharing failure is recorded on the account
    (share_status "error") but does not raise: uploads into our own folder still work."""
    missing = configured()
    if missing:
        raise MetaError("not configured; set " + ", ".join(missing))
    account = auth.account(email)
    if account is None:
        raise MetaError(f"{email} has no account")
    meta = dict(account.get("meta") or {})
    if not meta.get("folder_id"):
        created = _request("POST", f"/{BUSINESS_ID}/creative_folders", {"name": folder_name(account)})
        meta.update(folder_id=str(created["id"]), folder_created=time.time(), folder_name=folder_name(account))
        try:
            meta["folder_url"] = _request("GET", f"/{created['id']}", {"fields": "media_library_url"}).get("media_library_url")
        except MetaError as exc:
            _log(f"{email}: no media_library_url for folder {created['id']}: {exc}")
        auth.set_meta(email, **meta)
        _log(f"{email}: folder {created['id']} created")
    partner = (account.get("portfolio_id") or "").strip()
    if partner:
        try:
            meta = _share(email, meta, partner)
        except MetaError as exc:
            meta.update(shared_with=partner, share_status="error", share_error=str(exc))
            _log(f"{email}: sharing folder {meta['folder_id']} with portfolio {partner} failed: {exc}")
    auth.set_meta(email, **meta)
    return meta


def prepare_in_background(email):
    """Create and share the folder on a thread; used when a registration is approved."""
    if configured():
        return False

    def run():
        try:
            ensure_folder(email)
        except Exception as exc:  # noqa: BLE001 - logged, never raised into the request
            _log(f"{email}: preparing the folder failed: {exc}")

    threading.Thread(target=run, name="meta-folder", daemon=True).start()
    return True


# ---------------------------------------------------------------- videos

def upload_video(folder_id, path, filename):
    """Upload the file into the folder under `filename`; returns Meta's business video id."""
    size = os.path.getsize(path)
    title = os.path.splitext(filename)[0]
    if size <= SINGLE_UPLOAD_MAX:
        with open(path, "rb") as fh:
            data = fh.read()
        reply = _request("POST", f"/{BUSINESS_ID}/videos", {"name": title, "creative_folder_id": folder_id},
                         files={"source": (filename, data)}, host=GRAPH_VIDEO)
        return str(reply.get("business_video_id") or reply.get("id"))

    start = _request("POST", f"/{BUSINESS_ID}/videos",
                     {"upload_phase": "start", "file_size": size, "title": title, "creative_folder_id": folder_id},
                     host=GRAPH_VIDEO)
    session_id = start["upload_session_id"]
    lo, hi = int(start["start_offset"]), int(start["end_offset"])
    with open(path, "rb") as fh:
        while lo < hi:
            fh.seek(lo)
            chunk = fh.read(hi - lo)
            for attempt in range(1, CHUNK_RETRIES + 1):
                try:
                    reply = _request("POST", f"/{BUSINESS_ID}/videos",
                                     {"upload_phase": "transfer", "upload_session_id": session_id, "start_offset": lo,
                                      "creative_folder_id": folder_id},
                                     files={"video_file_chunk": (filename, chunk)}, host=GRAPH_VIDEO)
                    break
                except MetaError as exc:
                    if attempt == CHUNK_RETRIES:
                        raise
                    _log(f"chunk at {lo} of {filename} failed ({exc}); retrying")
                    time.sleep(5 * attempt)
            lo, hi = int(reply["start_offset"]), int(reply["end_offset"])
    _request("POST", f"/{BUSINESS_ID}/videos",
             {"upload_phase": "finish", "upload_session_id": session_id, "title": title, "creative_folder_id": folder_id},
             host=GRAPH_VIDEO)
    return str(start.get("business_video_id") or start.get("video_id"))


def sync_one(email, path, filename):
    """Folder first, then the video.  Returns the upload record to keep on the job."""
    meta = ensure_folder(email)
    video_id = upload_video(meta["folder_id"], path, filename)
    _log(f"{email}: uploaded {filename} as video {video_id} into folder {meta['folder_id']}")
    return {"video_id": video_id, "folder_id": meta["folder_id"], "at": time.time()}


# ---------------------------------------------------------------- background worker

_queue = queue.Queue()
_worker = None
_worker_lock = threading.Lock()


def _start_worker():
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="meta-library", daemon=True)
            _worker.start()


def _later(seconds, item):
    timer = threading.Timer(seconds, _queue.put, [item])
    timer.daemon = True
    timer.start()


def _run():
    while True:
        item = _queue.get()
        try:
            result = sync_one(item["email"], item["path"], item["filename"])
        except Exception as exc:  # noqa: BLE001 - any failure is retried, then reported
            item["attempt"] += 1
            error = str(exc)
            if item["attempt"] < MAX_ATTEMPTS:
                wait = BACKOFF[min(item["attempt"] - 1, len(BACKOFF) - 1)]
                _log(f"job {item['job_id']}: {error}; attempt {item['attempt']} of {MAX_ATTEMPTS}, next in {wait}s")
                item["on_result"]("retrying", {"error": error, "attempt": item["attempt"]})
                _later(wait, item)
            else:
                _log(f"job {item['job_id']}: giving up after {item['attempt']} attempts: {error}")
                item["on_result"]("error", {"error": error, "attempt": item["attempt"]})
        else:
            item["on_result"]("uploaded", result)
        finally:
            _queue.task_done()


def sync_in_background(job_id, email, path, filename, on_result):
    """Queue one finished video.  `on_result(status, info)` runs on the worker thread with status
    "uploaded" ({video_id, folder_id, at}), "retrying" ({error, attempt}) or "error" ({error, attempt})."""
    _start_worker()
    _queue.put({"job_id": job_id, "email": email, "path": path, "filename": filename,
                "on_result": on_result, "attempt": 0})


def pending():
    """Uploads waiting in the queue right now (retries waiting on a timer are not counted)."""
    return _queue.qsize()

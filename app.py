"""codec399 web app: upload a video, run the methodology, download the result."""
import hmac
import itertools
import json
import os
import queue
import shutil
import threading
import time
import uuid

from flask import Flask, abort, g, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

import auth
import pipeline
import settings
import whop

BASE = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.environ.get("CODEC399_JOBS_DIR") or os.path.join(BASE, "jobs")
os.makedirs(JOBS_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 ** 3
if os.environ.get("CODEC399_BEHIND_PROXY") == "1":
    # Trust one reverse proxy (nginx) for the client address and scheme.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Every page needs a signed-in account (see auth.py and manage.py).  The session cookie is only
# sent over HTTPS when the app runs behind the proxy, which is where HTTPS is enforced.
app.secret_key = auth.secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("CODEC399_BEHIND_PROXY") == "1",
                  PERMANENT_SESSION_LIFETIME=14 * 24 * 3600)
# Jobs queued or running at once before new uploads are refused.
MAX_QUEUE = int(os.environ.get("CODEC399_MAX_QUEUE", "40"))
# Admins may send this many videos in one batch from the encoder screen.
ADMIN_MAX_BATCH = 10
# The user upload length cap and the automatic Fast cutoff live in settings.py: they start from
# CODEC399_USER_MAX_SECONDS / CODEC399_AUTO_FAST_SECONDS and are editable on the Settings tab.

jobs = {}
lock = threading.Lock()
# Waiting jobs, admin uploads first, then by arrival; the dispatcher starts them as slots free up.
work = queue.PriorityQueue()
_seq = itertools.count()
running = set()


def _mmss(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _public(job):
    keys = ("id", "name", "status", "stage", "progress", "created", "started", "finished",
            "error", "summary", "fast", "fast_auto", "encode_audio", "input_bytes", "output_name", "owner", "credits_left")
    return {k: job.get(k) for k in keys}


def _save_meta(job):
    """Write the job's record next to its files.  Written when the job is created and at every status
    change, so a restart can pick queued and half-done encodes up again (see _load_existing)."""
    record = _public(job)
    record.update(ext=job.get("ext"), owner_role=job.get("owner_role"))
    path = os.path.join(JOBS_DIR, job["id"], "meta.json")
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(record, fh)
    os.replace(path + ".tmp", path)


def _draining():
    """While `drain` exists in the jobs directory no new encode is started; the installer creates it so
    it can wait for running encodes to finish and restart the service without losing anything."""
    return os.path.exists(os.path.join(JOBS_DIR, "drain"))


def _load_existing():
    """Reload finished jobs, and put back in the queue every job that was queued or encoding when the
    service last stopped.  Their uploads are still on disk (an upload is only deleted once its job
    ends), so a restart costs the time already spent on the encode, never the job."""
    revived = 0
    for jid in sorted(os.listdir(JOBS_DIR)):
        meta = os.path.join(JOBS_DIR, jid, "meta.json")
        if not os.path.isfile(meta):
            continue
        try:
            with open(meta, encoding="utf-8") as fh:
                job = json.load(fh)
        except (OSError, ValueError):
            continue
        status = job.get("status")
        if status == "done" and os.path.isfile(os.path.join(JOBS_DIR, jid, "output.mp4")):
            jobs[jid] = job
        elif status in ("queued", "processing"):
            src = os.path.join(JOBS_DIR, jid, "input" + (job.get("ext") or ".mp4"))
            if not os.path.isfile(src):
                job.update(status="error", stage="error", error="The upload was lost in a restart.", finished=time.time())
                _save_meta(job)
                jobs[jid] = job
                continue
            job.update(status="queued", stage="queued", progress=0.0, started=None, finished=None, error=None,
                       cancel=threading.Event(), restarted=int(job.get("restarted") or 0) + 1)
            job.setdefault("created", time.time())
            jobs[jid] = job
            _save_meta(job)
            _enqueue(job)
            revived += 1
    if revived:
        print(f"codec399: {revived} job(s) put back in the queue after the restart", flush=True)


def _run(job):
    jid = job["id"]
    src = os.path.join(JOBS_DIR, jid, "input" + job["ext"])
    dst = os.path.join(JOBS_DIR, jid, "output.mp4")

    def on_progress(state):
        with lock:
            job["stage"] = state["stage"]
            job["progress"] = state["progress"]

    with lock:
        job["status"] = "processing"
        job["started"] = time.time()
    _save_meta(job)
    try:
        summary = pipeline.process(src, dst, on_progress, fast=job["fast"], cancel=job["cancel"],
                                   encode_audio=bool(job.get("encode_audio")))
        with lock:
            job.update(status="done", stage="done", progress=1.0, summary=summary, finished=time.time())
    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        with lock:
            job.update(status="error", stage="error", error=str(exc), finished=time.time())
    _save_meta(job)
    for fn in (src, dst if job["status"] != "done" else None):
        if fn:
            try:
                os.remove(fn)
            except OSError:
                pass


def _enqueue(job):
    priority = 0 if job.get("owner_role") == "admin" else 1
    work.put((priority, job["created"], next(_seq), job))


def _run_and_release(job):
    try:
        if not job["cancel"].is_set():
            _run(job)
    finally:
        with lock:
            running.discard(job["id"])


def _dispatcher():
    """Keep up to `concurrent_jobs` (a live setting) encodes running; admin jobs start first."""
    while True:
        with lock:
            free = len(running) < max(1, int(settings.get("concurrent_jobs")))
        if not free or _draining():
            time.sleep(0.5)
            continue
        try:
            _priority, _created, _n, job = work.get(timeout=0.5)
        except queue.Empty:
            continue
        if job["cancel"].is_set():
            continue
        with lock:
            running.add(job["id"])
        threading.Thread(target=_run_and_release, args=(job,), daemon=True).start()


PUBLIC_ENDPOINTS = {"login", "register", "static", "whop_webhook", "bot_account"}
# Shared secret the Discord bot sends as a Bearer token to look accounts up before selling credits.
BOT_TOKEN = os.environ.get("CODEC399_BOT_TOKEN", "")
DISCORD_INVITE = "https://discord.gg/peEY87MCu"


@app.before_request
def _require_login():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    user = auth.get_user(session.get("email")) if session.get("email") else None
    if user is None:
        session.clear()
        if request.path.startswith("/api/"):
            return jsonify({"error": "Sign in required."}), 401
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    g.user = user
    return None


def _admin_only():
    if g.user["role"] != "admin":
        abort(403)


def _staff_only():
    """Admins and media buyers share the encoder screen; media buyers see only their own jobs."""
    if g.user["role"] not in ("admin", "media_buyer"):
        abort(403)


def _mine(job):
    return g.user["role"] == "admin" or job.get("owner") == g.user["email"]


def _download_name(name):
    """The file name a finished job downloads as: the upload's name with a suffix, and every "399"
    (including the one in the suffix) spelled "ghost"."""
    base = os.path.splitext(name)[0] + "_codec399"
    return base.replace("399", "ghost") + ".mp4"


def _safe_next(target):
    return target if target and target.startswith("/") and not target.startswith("//") else "/"


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("email") and auth.get_user(session["email"]):
        return redirect(_safe_next(request.args.get("next")))
    error = None
    email = ""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        user = auth.verify(email, request.form.get("password") or "")
        if user is None:
            time.sleep(0.5)  # blunt password guessing a little
            error = "That email and password don't match."
        elif user["status"] == "revoked":
            error = "Your access has been revoked."
        elif user["status"] != "active":
            error = "Your registration is still waiting for approval."
        else:
            session.clear()
            session["email"] = user["email"]
            session.permanent = True
            auth.record_ip(user["email"], request.remote_addr)
            return redirect(_safe_next(request.args.get("next")))
    status = 401 if error else 200
    return render_template("login.html", error=error, email=email, next=request.args.get("next") or ""), status


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("email") and auth.get_user(session["email"]):
        return redirect(url_for("index"))
    form = {"email": "", "portfolio_id": ""}
    error = None
    if request.method == "POST":
        form = {"email": (request.form.get("email") or "").strip(),
                "portfolio_id": (request.form.get("portfolio_id") or "").strip()}
        try:
            auth.register(form["email"], request.form.get("password") or "", form["portfolio_id"], request.remote_addr)
        except ValueError as exc:
            error = str(exc)
        else:
            return render_template("register.html", done=True, email=form["email"].lower())
    return render_template("register.html", error=error, **form), (400 if error else 200)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    if g.user["role"] not in ("admin", "media_buyer"):
        return redirect(url_for("dashboard"))
    gold, credits = auth.entitlement(g.user["email"])
    return render_template("index.html", user=g.user, is_admin=g.user["role"] == "admin", max_batch=ADMIN_MAX_BATCH,
                           gold=gold, credits=credits, discord=DISCORD_INVITE)


@app.get("/dashboard")
def dashboard():
    limits = settings.load()
    gold, credits = auth.entitlement(g.user["email"])
    return render_template("dashboard.html", user=g.user, max_seconds=int(round(limits["user_max_seconds"])),
                           max_batch=int(limits["user_max_batch"]), gold=gold, credits=credits, discord=DISCORD_INVITE)


@app.get("/api/registrations")
def list_registrations():
    _admin_only()
    return jsonify(auth.pending())


@app.post("/api/registrations/<path:email>/<decision>")
def decide_registration(email, decision):
    _admin_only()
    body = request.get_json(silent=True) or {}
    try:
        if decision == "approve":
            auth.approve(email, body.get("role") or "user")
        elif decision == "decline":
            auth.decline(email)
        else:
            abort(404)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"email": email.strip().lower(), "decision": decision})


@app.get("/api/accounts")
def list_accounts():
    """Active and revoked non-admin accounts, for the Approval tab's Accounts section."""
    _admin_only()
    return jsonify([a for a in auth.accounts() if a["status"] in ("active", "revoked") and a["role"] != "admin"])


@app.get("/api/me")
def me():
    gold, credits = auth.entitlement(g.user["email"])
    return jsonify({"email": g.user["email"], "role": g.user["role"], "gold": gold, "credits": credits})


@app.post("/api/accounts/<path:email>/credits")
def account_credits(email):
    """Body: {"add": 5} adds five upload credits; negative removes."""
    _admin_only()
    body = request.get_json(silent=True) or {}
    try:
        n = int(body.get("add"))
    except (TypeError, ValueError):
        return jsonify({"error": "Enter a whole number of uploads."}), 400
    if n == 0 or abs(n) > 10000:
        return jsonify({"error": "Enter a whole number between -10000 and 10000, not 0."}), 400
    try:
        return jsonify({"email": email.strip().lower(), "credits": auth.add_credits(email, n, note=f"admin {g.user['email']}")})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/accounts/<path:email>/gold")
def account_gold(email):
    """Body: {"gold": true|false}."""
    _admin_only()
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(auth.set_gold(email, bool(body.get("gold")), note=f"admin {g.user['email']}"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/whop/webhook")
def whop_webhook():
    """Whop calls this on payment.succeeded; see whop.py.  Public, authenticated by signature."""
    status, message = whop.handle(request.get_data(), {k.lower(): v for k, v in request.headers.items()})
    return jsonify({"message": message}), status


@app.get("/api/bot/account")
def bot_account():
    """The Discord bot asks whether an email is registered before it hands out a checkout link.
    Public, authenticated by CODEC399_BOT_TOKEN; answers 404 for an unknown email."""
    if not BOT_TOKEN:
        return jsonify({"error": "CODEC399_BOT_TOKEN is not configured."}), 503
    header = request.headers.get("Authorization", "")
    if not hmac.compare_digest(header, f"Bearer {BOT_TOKEN}"):
        return jsonify({"error": "Bad token."}), 401
    email = (request.args.get("email") or "").strip().lower()
    user = auth.load_users().get(email) if email else None
    if user is None:
        return jsonify({"error": "No account with that email."}), 404
    return jsonify({"email": email, "role": user.get("role", "user"), "status": user.get("status", "active"),
                    "credits": int(user.get("credits") or 0), "gold": auth.is_gold(user)})


@app.post("/api/accounts/<path:email>/revoke")
def revoke_account(email):
    _admin_only()
    try:
        return jsonify(auth.revoke(email))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/accounts/<path:email>/restore")
def restore_account(email):
    _admin_only()
    try:
        return jsonify(auth.restore(email))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/accounts/<path:email>/role")
def change_role(email):
    _admin_only()
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(auth.set_role(email, body.get("role")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/settings")
def get_settings():
    _admin_only()
    return jsonify(settings.load())


@app.post("/api/settings")
def set_settings():
    """Body: {"user_max_minutes": 6, "auto_fast_minutes": 3.5, "user_max_batch": 8, "concurrent_jobs": 2}; any key may be omitted."""
    _admin_only()
    body = request.get_json(silent=True) or {}
    changes = {}
    for field, key in (("user_max_minutes", "user_max_seconds"), ("auto_fast_minutes", "auto_fast_seconds")):
        if field in body:
            try:
                changes[key] = float(body[field]) * 60
            except (TypeError, ValueError):
                return jsonify({"error": "Enter a number of minutes."}), 400
    for key in ("user_max_batch", "concurrent_jobs"):
        if key in body:
            changes[key] = body[key]
    if not changes:
        return jsonify({"error": "Nothing to set."}), 400
    try:
        return jsonify(settings.update(changes))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/gallery")
def gallery():
    """Finished videos grouped by uploader: one folder per active user account, plus a folder
    for any other uploader (admins, or jobs from before accounts existed)."""
    _admin_only()
    folders = {}
    for acct in auth.accounts():
        if acct["status"] == "active" and acct["role"] in ("user", "media_buyer"):
            folders[acct["email"]] = {"email": acct["email"], "portfolio_id": acct["portfolio_id"],
                                      "role": "user", "videos": [], "in_progress": 0}
    known = {a["email"]: a for a in auth.accounts()}
    with lock:
        snapshot = [_public(j) for j in jobs.values()]
    for j in snapshot:
        owner = j.get("owner") or ""
        if owner not in folders:
            acct = known.get(owner)
            folders[owner] = {"email": owner, "portfolio_id": acct["portfolio_id"] if acct else None,
                              "role": acct["role"] if acct else None, "videos": [], "in_progress": 0}
        if j["status"] == "done":
            folders[owner]["videos"].append(j)
        elif j["status"] in ("queued", "processing"):
            folders[owner]["in_progress"] += 1
    for f in folders.values():
        f["videos"].sort(key=lambda j: j["created"], reverse=True)
    return jsonify(sorted(folders.values(), key=lambda f: (f["email"] == "", f["email"])))


@app.get("/api/jobs")
def list_jobs():
    _staff_only()
    with lock:
        items = sorted((_public(j) for j in jobs.values() if _mine(j)), key=lambda j: j["created"], reverse=True)
    return jsonify(items)


@app.post("/api/jobs")
def create_job():
    with lock:
        pending = sum(1 for j in jobs.values() if j["status"] in ("queued", "processing"))
    if pending >= MAX_QUEUE:
        return jsonify({"error": f"The queue is full ({MAX_QUEUE} jobs). Try again later."}), 429
    file = request.files.get("video")
    if file is None or not file.filename:
        return jsonify({"error": "No file was uploaded."}), 400
    name = secure_filename(file.filename) or "video.mp4"
    ext = os.path.splitext(name)[1].lower() or ".mp4"
    jid = uuid.uuid4().hex[:12]
    jdir = os.path.join(JOBS_DIR, jid)
    os.makedirs(jdir)
    path = os.path.join(jdir, "input" + ext)
    file.save(path)
    fast = request.form.get("fast") in ("1", "true", "on")
    fast_auto = False
    if g.user["role"] != "admin":
        limits = settings.load()
        try:
            duration = pipeline.probe(path)["duration"]
        except Exception:  # noqa: BLE001 - an unreadable file fails properly once the job runs
            duration = None
        if duration is not None and duration > limits["user_max_seconds"]:
            shutil.rmtree(jdir, ignore_errors=True)
            return jsonify({"error": f"You can't upload a video longer than {_mmss(limits['user_max_seconds'])}; "
                                     f"this one is {_mmss(duration)}."}), 413
        if duration is not None and not fast:
            fast_auto = duration > limits["auto_fast_seconds"]
            fast = fast_auto
    job = {
        "id": jid, "name": name, "ext": ext, "status": "queued", "stage": "queued", "progress": 0.0,
        "created": time.time(), "started": None, "finished": None, "error": None, "summary": None,
        "fast": fast, "fast_auto": fast_auto,
        "encode_audio": request.form.get("encode_audio") in ("1", "true", "on"),
        "input_bytes": os.path.getsize(path),
        "owner": g.user["email"], "owner_role": g.user["role"],
        "output_name": _download_name(name),
        "cancel": threading.Event(),
    }
    if g.user["role"] != "admin":
        try:
            left = auth.consume_credit(g.user["email"])
        except ValueError:
            shutil.rmtree(jdir, ignore_errors=True)
            return jsonify({"error": "You have no uploads left. Open a ticket in the Discord to buy more.", "credits": 0}), 402
        job["credits_left"] = left
    with lock:
        jobs[jid] = job
    _save_meta(job)
    _enqueue(job)
    return jsonify(_public(job)), 201


@app.get("/api/jobs/<jid>/download")
def download(jid):
    _staff_only()
    with lock:
        job = jobs.get(jid)
    if job is None or job["status"] != "done" or not _mine(job):
        abort(404)
    path = os.path.join(JOBS_DIR, jid, "output.mp4")
    # Jobs finished before the rename keep their stored name; apply the spelling on the way out too.
    return send_file(path, as_attachment=True, download_name=job["output_name"].replace("399", "ghost"),
                     mimetype="video/mp4")


@app.delete("/api/jobs/<jid>")
def delete_job(jid):
    _staff_only()
    with lock:
        job = jobs.get(jid)
        if job is None or not _mine(job):
            abort(404)
        if job["status"] == "processing":
            job["cancel"].set()
            return jsonify({"status": "cancelling"}), 202
        if job["status"] == "queued":
            job["cancel"].set()
            job.update(status="error", stage="error", error="Cancelled.", finished=time.time())
            return jsonify({"status": "cancelled"}), 202
        del jobs[jid]
    jdir = os.path.join(JOBS_DIR, jid)
    for fn in os.listdir(jdir):
        try:
            os.remove(os.path.join(jdir, fn))
        except OSError:
            pass
    try:
        os.rmdir(jdir)
    except OSError:
        pass
    return "", 204


_load_existing()
threading.Thread(target=_dispatcher, daemon=True).start()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5399"))
    print(f"codec399 running at http://{host}:{port}")
    app.run(host=host, port=port, threaded=True)

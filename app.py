"""codec399 web app: upload a video, run the methodology, download the result."""
import json
import os
import queue
import threading
import time
import uuid

from flask import Flask, abort, g, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

import auth
import pipeline

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
MAX_QUEUE = int(os.environ.get("CODEC399_MAX_QUEUE", "10"))

jobs = {}
lock = threading.Lock()
work = queue.Queue()


def _public(job):
    keys = ("id", "name", "status", "stage", "progress", "created", "started", "finished",
            "error", "summary", "fast", "encode_audio", "input_bytes", "output_name", "owner")
    return {k: job.get(k) for k in keys}


def _save_meta(job):
    with open(os.path.join(JOBS_DIR, job["id"], "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(_public(job), fh)


def _load_existing():
    for jid in os.listdir(JOBS_DIR):
        meta = os.path.join(JOBS_DIR, jid, "meta.json")
        if not os.path.isfile(meta):
            continue
        try:
            with open(meta, encoding="utf-8") as fh:
                job = json.load(fh)
        except (OSError, ValueError):
            continue
        if job.get("status") == "done" and os.path.isfile(os.path.join(JOBS_DIR, jid, "output.mp4")):
            jobs[jid] = job


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


def _worker():
    while True:
        job = work.get()
        try:
            if not job["cancel"].is_set():
                _run(job)
        finally:
            work.task_done()


PUBLIC_ENDPOINTS = {"login", "register", "static"}


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
        elif user["status"] != "active":
            error = "Your registration is still waiting for approval."
        else:
            session.clear()
            session["email"] = user["email"]
            session.permanent = True
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
            auth.register(form["email"], request.form.get("password") or "", form["portfolio_id"])
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
    if g.user["role"] != "admin":
        return redirect(url_for("dashboard"))
    return render_template("index.html", user=g.user)


@app.get("/dashboard")
def dashboard():
    return render_template("dashboard.html", user=g.user)


@app.get("/api/registrations")
def list_registrations():
    _admin_only()
    return jsonify(auth.pending())


@app.post("/api/registrations/<path:email>/<decision>")
def decide_registration(email, decision):
    _admin_only()
    try:
        if decision == "approve":
            auth.approve(email)
        elif decision == "decline":
            auth.decline(email)
        else:
            abort(404)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"email": email.strip().lower(), "decision": decision})


@app.get("/api/jobs")
def list_jobs():
    _admin_only()
    with lock:
        items = sorted((_public(j) for j in jobs.values()), key=lambda j: j["created"], reverse=True)
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
    job = {
        "id": jid, "name": name, "ext": ext, "status": "queued", "stage": "queued", "progress": 0.0,
        "created": time.time(), "started": None, "finished": None, "error": None, "summary": None,
        "fast": request.form.get("fast") in ("1", "true", "on"),
        "encode_audio": request.form.get("encode_audio") in ("1", "true", "on"),
        "input_bytes": os.path.getsize(path),
        "owner": g.user["email"],
        "output_name": os.path.splitext(name)[0] + "_codec399.mp4",
        "cancel": threading.Event(),
    }
    with lock:
        jobs[jid] = job
    work.put(job)
    return jsonify(_public(job)), 201


@app.get("/api/jobs/<jid>/download")
def download(jid):
    _admin_only()
    with lock:
        job = jobs.get(jid)
    if job is None or job["status"] != "done":
        abort(404)
    path = os.path.join(JOBS_DIR, jid, "output.mp4")
    return send_file(path, as_attachment=True, download_name=job["output_name"], mimetype="video/mp4")


@app.delete("/api/jobs/<jid>")
def delete_job(jid):
    _admin_only()
    with lock:
        job = jobs.get(jid)
        if job is None:
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
threading.Thread(target=_worker, daemon=True).start()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5399"))
    print(f"codec399 running at http://{host}:{port}")
    app.run(host=host, port=port, threaded=True)

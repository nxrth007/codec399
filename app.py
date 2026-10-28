"""codec399 web app: upload a video, run the methodology, download the result."""
import json
import os
import queue
import threading
import time
import uuid

from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

import pipeline

BASE = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BASE, "jobs")
os.makedirs(JOBS_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 ** 3

jobs = {}
lock = threading.Lock()
work = queue.Queue()


def _public(job):
    keys = ("id", "name", "status", "stage", "progress", "created", "started", "finished",
            "error", "summary", "fast", "input_bytes", "output_name")
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
        summary = pipeline.process(src, dst, on_progress, fast=job["fast"])
        with lock:
            job.update(status="done", stage="done", progress=1.0, summary=summary, finished=time.time())
    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        with lock:
            job.update(status="error", stage="error", error=str(exc), finished=time.time())
    _save_meta(job)
    try:
        os.remove(src)
    except OSError:
        pass


def _worker():
    while True:
        job = work.get()
        try:
            _run(job)
        finally:
            work.task_done()


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/jobs")
def list_jobs():
    with lock:
        items = sorted((_public(j) for j in jobs.values()), key=lambda j: j["created"], reverse=True)
    return jsonify(items)


@app.post("/api/jobs")
def create_job():
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
        "input_bytes": os.path.getsize(path),
        "output_name": os.path.splitext(name)[0] + "_codec399.mp4",
    }
    with lock:
        jobs[jid] = job
    work.put(job)
    return jsonify(_public(job)), 201


@app.get("/api/jobs/<jid>/download")
def download(jid):
    with lock:
        job = jobs.get(jid)
    if job is None or job["status"] != "done":
        abort(404)
    path = os.path.join(JOBS_DIR, jid, "output.mp4")
    return send_file(path, as_attachment=True, download_name=job["output_name"], mimetype="video/mp4")


@app.delete("/api/jobs/<jid>")
def delete_job(jid):
    with lock:
        job = jobs.get(jid)
        if job is None:
            abort(404)
        if job["status"] in ("queued", "processing"):
            return jsonify({"error": "This job is still running."}), 409
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
    port = int(os.environ.get("PORT", "5399"))
    print(f"codec399 running at http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, threaded=True)

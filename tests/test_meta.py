"""Meta Media Library sync: a folder per account shared with their Business Portfolio, videos
uploaded into it (in one go or in chunks), retries, and the app's hooks.  Meta itself is replaced
by a fake `_request` that records every call.

    python -m unittest tests.test_meta
"""
import json
import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("CODEC399_JOBS_DIR", tempfile.mkdtemp(prefix="codec399-meta-"))

import auth  # noqa: E402
import meta_library  # noqa: E402
import app as webapp  # noqa: E402

meta_library.ACCESS_TOKEN = "test-token"
meta_library.BUSINESS_ID = "biz1"
webapp.BOT_TOKEN = "bot-test"


class FakeGraph:
    """Answers the calls meta_library makes; `calls` is [(method, path, params, files)]."""

    def __init__(self, share_pending=True, partner_accepted=False, fail_first=0):
        self.calls = []
        self.share_pending = share_pending
        self.partner_accepted = partner_accepted
        self.fail_first = fail_first
        self.chunks = {}
        self.next_folder = 100

    def __call__(self, method, path, params=None, files=None, host=meta_library.GRAPH):
        params = dict(params or {})
        self.calls.append((method, path, params, files))
        if self.fail_first:
            self.fail_first -= 1
            raise meta_library.MetaError("Meta is down (code 1)")
        if path == "/biz1/creative_folders" and method == "POST":
            self.next_folder += 1
            return {"id": str(self.next_folder)}
        if method == "GET" and params.get("fields") == "media_library_url":
            return {"media_library_url": f"https://business.facebook.com/latest/media_library?folder={path[1:]}"}
        if path.endswith("/agencies") and method == "POST":
            return {"success": True, "share_status": "In Progress"} if self.share_pending else {"success": True}
        if path.endswith("/agencies") and method == "GET":
            return {"data": [{"id": "pf123", "name": "Client"}] if self.partner_accepted else []}
        if path == "/biz1/videos":
            phase = params.get("upload_phase")
            if phase is None:
                self.assertion = files["source"]
                return {"success": True, "business_video_id": "vid-single"}
            if phase == "start":
                size = int(params["file_size"])
                self.size = size
                return {"upload_session_id": "sess", "business_video_id": "vid-chunked", "video_id": "v",
                        "start_offset": "0", "end_offset": str(min(10, size))}
            if phase == "transfer":
                start = int(params["start_offset"])
                self.chunks[start] = files["video_file_chunk"][1]
                end = start + len(self.chunks[start])
                return {"start_offset": str(end), "end_offset": str(min(end + 10, self.size))}
            if phase == "finish":
                return {"success": True}
        if path == "/biz1" and method == "GET":
            return {"id": "biz1", "name": "Ghost"}
        raise AssertionError(f"unexpected call {method} {path}")


class MetaFolders(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGraph()
        self.saved = meta_library._request
        meta_library._request = self.fake
        self.email = f"meta-{id(self)}@example.com"
        auth.register(self.email, "Str0ngPassw0rd!", "pf123", "10.0.0.1")
        auth.approve(self.email)

    def tearDown(self):
        meta_library._request = self.saved
        auth.remove_user(self.email)

    def test_folder_created_once_and_shared_with_portfolio(self):
        meta = meta_library.ensure_folder(self.email)
        self.assertEqual(meta["folder_id"], "101")
        self.assertEqual(meta["share_status"], "pending")
        self.assertEqual(meta["shared_with"], "pf123")
        self.assertIn("media_library?folder=101", meta["folder_url"])
        create = [c for c in self.fake.calls if c[1] == "/biz1/creative_folders"]
        self.assertEqual(create[0][2]["name"], self.email)
        share = [c for c in self.fake.calls if c[1] == "/101/agencies" and c[0] == "POST"]
        self.assertEqual(share[0][2]["business"], "pf123")
        self.assertEqual(json.loads(share[0][2]["permitted_tasks"]), meta_library.SHARE_TASKS)
        self.assertEqual(auth.account(self.email)["meta"]["folder_id"], "101")

        # Second time: no new folder, no new share request; the pending share is looked at again.
        self.fake.partner_accepted = True
        meta = meta_library.ensure_folder(self.email)
        self.assertEqual(meta["folder_id"], "101")
        self.assertEqual(meta["share_status"], "shared")
        self.assertEqual(len([c for c in self.fake.calls if c[1] == "/biz1/creative_folders"]), 1)
        self.assertEqual(len([c for c in self.fake.calls if c[1] == "/101/agencies" and c[0] == "POST"]), 1)

    def test_sharing_failure_is_recorded_not_raised(self):
        def refuse(method, path, params=None, files=None, host=None):
            if path.endswith("/agencies"):
                raise meta_library.MetaError("Not a partner (code 100)")
            return self.fake(method, path, params, files)
        meta_library._request = refuse
        meta = meta_library.ensure_folder(self.email)
        self.assertEqual(meta["folder_id"], "101")
        self.assertEqual(meta["share_status"], "error")
        self.assertIn("Not a partner", meta["share_error"])

    def test_unknown_account_or_missing_config(self):
        with self.assertRaises(meta_library.MetaError):
            meta_library.ensure_folder("nobody@example.com")
        meta_library.ACCESS_TOKEN = ""
        try:
            self.assertEqual(meta_library.configured(), ["META_ACCESS_TOKEN"])
            with self.assertRaises(meta_library.MetaError):
                meta_library.ensure_folder(self.email)
        finally:
            meta_library.ACCESS_TOKEN = "test-token"


class MetaUploads(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGraph()
        self.saved = (meta_library._request, meta_library.SINGLE_UPLOAD_MAX)
        meta_library._request = self.fake
        self.dir = tempfile.mkdtemp(prefix="codec399-meta-up-")
        self.path = os.path.join(self.dir, "output.mp4")
        with open(self.path, "wb") as fh:
            fh.write(bytes(range(25)))

    def tearDown(self):
        meta_library._request, meta_library.SINGLE_UPLOAD_MAX = self.saved

    def test_small_file_goes_in_one_request(self):
        vid = meta_library.upload_video("101", self.path, "clip_codecghost.mp4")
        self.assertEqual(vid, "vid-single")
        (method, path, params, files), = [c for c in self.fake.calls if c[1] == "/biz1/videos"]
        self.assertEqual(params["creative_folder_id"], "101")
        self.assertEqual(params["name"], "clip_codecghost")
        self.assertEqual(files["source"], ("clip_codecghost.mp4", bytes(range(25))))

    def test_large_file_goes_in_chunks(self):
        meta_library.SINGLE_UPLOAD_MAX = 10
        vid = meta_library.upload_video("101", self.path, "clip_codecghost.mp4")
        self.assertEqual(vid, "vid-chunked")
        phases = [c[2].get("upload_phase") for c in self.fake.calls if c[1] == "/biz1/videos"]
        self.assertEqual(phases, ["start", "transfer", "transfer", "transfer", "finish"])
        self.assertEqual(b"".join(self.fake.chunks[k] for k in sorted(self.fake.chunks)), bytes(range(25)))
        self.assertTrue(all(c[2]["creative_folder_id"] == "101" for c in self.fake.calls if c[1] == "/biz1/videos"))


class MetaWorker(unittest.TestCase):
    def setUp(self):
        self.saved = (meta_library._request, meta_library.BACKOFF)
        meta_library.BACKOFF = (0.05,)
        self.email = f"worker-{id(self)}@example.com"
        auth.register(self.email, "Str0ngPassw0rd!", "pf123", "10.0.0.2")
        auth.approve(self.email)
        self.dir = tempfile.mkdtemp(prefix="codec399-meta-w-")
        self.path = os.path.join(self.dir, "output.mp4")
        with open(self.path, "wb") as fh:
            fh.write(b"\0" * 8)

    def tearDown(self):
        meta_library._request, meta_library.BACKOFF = self.saved
        auth.remove_user(self.email)

    def _run(self, fake):
        meta_library._request = fake
        results, done = [], threading.Event()

        def on_result(status, info):
            results.append((status, info))
            if status in ("uploaded", "error"):
                done.set()
        meta_library.sync_in_background("job1", self.email, self.path, "clip.mp4", on_result)
        self.assertTrue(done.wait(5), "the worker did not finish")
        return results

    def test_uploads_and_reports(self):
        results = self._run(FakeGraph())
        self.assertEqual(results[-1][0], "uploaded")
        self.assertEqual(results[-1][1]["video_id"], "vid-single")
        self.assertEqual(results[-1][1]["folder_id"], "101")

    def test_retries_after_a_failure(self):
        results = self._run(FakeGraph(fail_first=1))
        self.assertEqual([r[0] for r in results], ["retrying", "uploaded"])
        self.assertIn("Meta is down", results[0][1]["error"])
        self.assertEqual(results[0][1]["attempt"], 1)


class AppHooks(unittest.TestCase):
    def setUp(self):
        self.email = f"hook-{id(self)}@example.com"
        auth.register(self.email, "Str0ngPassw0rd!", "pf123", "10.0.0.3")
        auth.approve(self.email)
        self.jid = f"job-{id(self)}"
        os.makedirs(os.path.join(webapp.JOBS_DIR, self.jid), exist_ok=True)
        with open(os.path.join(webapp.JOBS_DIR, self.jid, "output.mp4"), "wb") as fh:
            fh.write(b"\0" * 4)
        self.job = {"id": self.jid, "name": "clip.mp4", "ext": ".mp4", "status": "done", "stage": "done", "progress": 1.0,
                    "created": 1.0, "started": 2.0, "finished": 3.0, "error": None, "summary": {}, "fast": False,
                    "fast_auto": False, "encode_audio": False, "input_bytes": 4, "owner": self.email,
                    "owner_role": "user", "output_name": "clip_codec399.mp4", "credits_left": None}
        self.calls = []
        self.saved = meta_library.sync_in_background
        self.saved_token = webapp.BOT_TOKEN
        webapp.BOT_TOKEN = "bot-test"

        def fake_sync(job_id, email, path, filename, on_result):
            self.calls.append((job_id, email, path, filename))
            on_result("uploaded", {"video_id": "vid-1", "folder_id": "101", "at": 9.0})
        meta_library.sync_in_background = fake_sync

    def tearDown(self):
        meta_library.sync_in_background = self.saved
        webapp.BOT_TOKEN = self.saved_token
        auth.remove_user(self.email)
        with webapp.lock:
            webapp.jobs.pop(self.jid, None)

    def test_finished_video_is_queued_and_recorded(self):
        self.assertTrue(webapp._meta_sync(self.job))
        self.assertEqual(self.calls[0][1], self.email)
        self.assertEqual(self.calls[0][3], "clip_codecghost.mp4")
        self.assertEqual(self.job["meta"]["status"], "uploaded")
        with open(os.path.join(webapp.JOBS_DIR, self.jid, "meta.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["meta"]["video_id"], "vid-1")
        self.assertFalse(webapp._meta_sync(self.job))   # already up: nothing queued again
        self.assertEqual(len(self.calls), 1)

    def test_owner_without_portfolio_is_skipped(self):
        auth.load_users()[self.email]  # exists
        users = auth.load_users()
        users[self.email]["portfolio_id"] = ""
        auth._save_users(users)
        self.assertFalse(webapp._meta_sync(self.job))
        self.assertEqual(self.calls, [])

    def test_sync_endpoint_needs_admin_or_bot_token(self):
        client = webapp.app.test_client()
        self.assertEqual(client.post("/api/meta/sync").status_code, 401)      # no session, no token
        r = client.post("/api/meta/sync", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)
        with webapp.lock:
            webapp.jobs[self.jid] = self.job
        r = client.post("/api/meta/sync", headers={"Authorization": "Bearer bot-test"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertGreaterEqual(r.get_json()["folders"], 1)
        self.assertEqual(r.get_json()["videos_queued"], 1)
        self.assertEqual(self.job["meta"]["status"], "uploaded")

    def test_sync_endpoint_reports_missing_config(self):
        meta_library.ACCESS_TOKEN = ""
        try:
            r = webapp.app.test_client().post("/api/meta/sync", headers={"Authorization": "Bearer bot-test"})
            self.assertEqual(r.status_code, 503)
            self.assertIn("META_ACCESS_TOKEN", r.get_json()["error"])
        finally:
            meta_library.ACCESS_TOKEN = "test-token"


if __name__ == "__main__":
    unittest.main()

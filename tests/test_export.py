"""The hand-off for an outside agent: the export queue, the file, and marking videos and folders.

    python -m unittest tests.test_export
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("CODEC399_JOBS_DIR", tempfile.mkdtemp(prefix="codec399-export-"))

import auth  # noqa: E402
import app as webapp  # noqa: E402

TOKEN = {"Authorization": "Bearer export-test"}


class ExportHandoff(unittest.TestCase):
    def setUp(self):
        self.saved_token = webapp.EXPORT_TOKEN
        webapp.EXPORT_TOKEN = "export-test"
        self.email = f"export-{id(self)}@example.com"
        auth.register(self.email, "Str0ngPassw0rd!", "pf-777", "10.0.1.1")
        auth.approve(self.email)
        self.jid = f"exp-{id(self)}"
        os.makedirs(os.path.join(webapp.JOBS_DIR, self.jid), exist_ok=True)
        with open(os.path.join(webapp.JOBS_DIR, self.jid, "output.mp4"), "wb") as fh:
            fh.write(b"MP4!")
        self.job = {"id": self.jid, "name": "promo.mp4", "ext": ".mp4", "status": "done", "stage": "done", "progress": 1.0,
                    "created": 1.0, "started": 2.0, "finished": 3.0, "error": None, "summary": {"output_bytes": 4},
                    "fast": False, "fast_auto": False, "encode_audio": False, "input_bytes": 4, "owner": self.email,
                    "owner_role": "user", "output_name": "promo_codec399.mp4", "credits_left": None}
        with webapp.lock:
            webapp.jobs[self.jid] = self.job
        self.client = webapp.app.test_client()

    def tearDown(self):
        webapp.EXPORT_TOKEN = self.saved_token
        with webapp.lock:
            webapp.jobs.pop(self.jid, None)
        auth.remove_user(self.email)

    def test_token_or_admin_only(self):
        self.assertEqual(self.client.get("/api/export/queue").status_code, 401)
        self.assertEqual(self.client.get("/api/export/queue", headers={"Authorization": "Bearer nope"}).status_code, 401)
        self.assertEqual(self.client.get(f"/api/export/{self.jid}/file").status_code, 401)
        self.assertEqual(self.client.post(f"/api/export/{self.jid}", json={"status": "uploaded"}).status_code, 401)
        webapp.EXPORT_TOKEN = ""
        self.assertEqual(self.client.get("/api/export/queue", headers=TOKEN).status_code, 401)   # no token configured

    def test_queue_lists_the_video_with_where_it_goes(self):
        r = self.client.get("/api/export/queue", headers=TOKEN)
        self.assertEqual(r.status_code, 200)
        items = [i for i in r.get_json()["items"] if i["job_id"] == self.jid]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["email"], self.email)
        self.assertEqual(item["portfolio_id"], "pf-777")
        self.assertEqual(item["folder"], self.email)
        self.assertEqual(item["filename"], "promo_codecghost.mp4")
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["share_status"], "none")
        self.assertEqual(item["file"], f"/api/export/{self.jid}/file")
        self.assertEqual(item["bytes"], 4)

        accounts = self.client.get("/api/export/accounts", headers=TOKEN).get_json()
        mine = [a for a in accounts if a["email"] == self.email][0]
        self.assertEqual(mine["portfolio_id"], "pf-777")
        self.assertEqual(mine["videos_pending"], 1)

    def test_file_download(self):
        r = self.client.get(f"/api/export/{self.jid}/file", headers=TOKEN)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"MP4!")
        self.assertIn("promo_codecghost.mp4", r.headers["Content-Disposition"])
        self.assertEqual(self.client.get("/api/export/nope/file", headers=TOKEN).status_code, 404)

    def test_marking_removes_it_from_the_queue_and_persists(self):
        r = self.client.post(f"/api/export/{self.jid}", headers=TOKEN, json={"status": "uploaded", "video_id": "v9", "by": "grokbot"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["meta"]["by"], "grokbot")
        self.assertEqual(self.job["meta"]["status"], "uploaded")
        with open(os.path.join(webapp.JOBS_DIR, self.jid, "meta.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["meta"]["video_id"], "v9")
        ids = [i["job_id"] for i in self.client.get("/api/export/queue", headers=TOKEN).get_json()["items"]]
        self.assertNotIn(self.jid, ids)
        ids = [i["job_id"] for i in self.client.get("/api/export/queue?all=1", headers=TOKEN).get_json()["items"]]
        self.assertIn(self.jid, ids)

        # An error keeps it in the queue with the message; "pending" clears the record.
        r = self.client.post(f"/api/export/{self.jid}", headers=TOKEN, json={"status": "error", "error": "upload button missing"})
        self.assertEqual(r.status_code, 200)
        item = [i for i in self.client.get("/api/export/queue", headers=TOKEN).get_json()["items"] if i["job_id"] == self.jid][0]
        self.assertEqual(item["status"], "error")
        self.assertEqual(item["error"], "upload button missing")
        self.client.post(f"/api/export/{self.jid}", headers=TOKEN, json={"status": "pending"})
        self.assertIsNone(self.job["meta"])
        self.assertEqual(self.client.post(f"/api/export/{self.jid}", headers=TOKEN, json={"status": "weird"}).status_code, 400)

    def test_folder_record_on_the_account(self):
        r = self.client.post(f"/api/export/accounts/{self.email}", headers=TOKEN,
                             json={"folder_url": "https://business.facebook.com/latest/media_library?x=1", "share_status": "shared"})
        self.assertEqual(r.status_code, 200)
        meta = auth.account(self.email)["meta"]
        self.assertEqual(meta["share_status"], "shared")
        self.assertIn("media_library", meta["folder_url"])
        self.assertTrue(meta["shared_at"])
        item = [i for i in self.client.get("/api/export/queue", headers=TOKEN).get_json()["items"] if i["job_id"] == self.jid][0]
        self.assertEqual(item["share_status"], "shared")
        self.assertEqual(self.client.post("/api/export/accounts/nobody@example.com", headers=TOKEN, json={"share_status": "shared"}).status_code, 404)
        self.assertEqual(self.client.post(f"/api/export/accounts/{self.email}", headers=TOKEN, json={}).status_code, 400)

    def test_videos_without_a_portfolio_are_not_offered(self):
        users = auth.load_users()
        users[self.email]["portfolio_id"] = ""
        auth._save_users(users)
        ids = [i["job_id"] for i in self.client.get("/api/export/queue", headers=TOKEN).get_json()["items"]]
        self.assertNotIn(self.jid, ids)


if __name__ == "__main__":
    unittest.main()

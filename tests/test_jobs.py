"""Jobs survive a restart: queued and interrupted encodes are put back in the queue from their
on-disk records, and a record whose upload is gone becomes an error instead of vanishing.

    python -m unittest tests.test_jobs
"""
import json
import os
import queue
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="codec399-jobs-")
os.environ.setdefault("CODEC399_JOBS_DIR", TMP)

import app as webapp  # noqa: E402


def _job_dir(jobs_dir, jid, status, with_input=True, ext=".mp4"):
    d = os.path.join(jobs_dir, jid)
    os.makedirs(d, exist_ok=True)
    if with_input:
        with open(os.path.join(d, "input" + ext), "wb") as fh:
            fh.write(b"\0" * 16)
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"id": jid, "name": jid + ext, "ext": ext, "status": status, "stage": status, "progress": 0.4,
                   "created": 1000.0 + len(jid), "started": 1001.0, "owner": "buyer@example.com",
                   "owner_role": "media_buyer", "output_name": jid + "_codecghost.mp4"}, fh)
    return d


class RestartRecovery(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="codec399-restart-")
        # Point the app at a fresh directory with `drain` present so the dispatcher thread, which is
        # already running, leaves the revived jobs in the queue for us to inspect.
        self.saved = (webapp.JOBS_DIR, webapp.jobs, webapp.work)
        webapp.JOBS_DIR = self.dir
        webapp.jobs = {}
        webapp.work = queue.PriorityQueue()
        open(os.path.join(self.dir, "drain"), "w").close()

    def tearDown(self):
        webapp.JOBS_DIR, webapp.jobs, webapp.work = self.saved

    def test_queued_and_processing_jobs_are_requeued(self):
        _job_dir(self.dir, "aaa", "processing")
        _job_dir(self.dir, "bbb", "queued")
        _job_dir(self.dir, "ccc", "done", with_input=False)   # no output.mp4 either: not reloaded
        webapp._load_existing()
        self.assertEqual(sorted(webapp.jobs), ["aaa", "bbb"])
        for jid in ("aaa", "bbb"):
            job = webapp.jobs[jid]
            self.assertEqual(job["status"], "queued")
            self.assertEqual(job["progress"], 0.0)
            self.assertIsNone(job["started"])
            self.assertEqual(job["restarted"], 1)
            self.assertFalse(job["cancel"].is_set())
            with open(os.path.join(self.dir, jid, "meta.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["status"], "queued")
        self.assertEqual(webapp.work.qsize(), 2)
        self.assertTrue(webapp._draining())

    def test_record_without_upload_becomes_an_error(self):
        _job_dir(self.dir, "ddd", "processing", with_input=False)
        webapp._load_existing()
        job = webapp.jobs["ddd"]
        self.assertEqual(job["status"], "error")
        self.assertIn("lost", job["error"])
        self.assertEqual(webapp.work.qsize(), 0)

    def test_new_job_is_recorded_before_it_runs(self):
        job = {"id": "eee", "name": "clip.mp4", "ext": ".mp4", "status": "queued", "stage": "queued", "progress": 0.0,
               "created": 5.0, "started": None, "finished": None, "error": None, "summary": None, "fast": False,
               "fast_auto": False, "encode_audio": False, "input_bytes": 1, "owner": "x@y.z", "owner_role": "user",
               "output_name": "clip_codecghost.mp4"}
        os.makedirs(os.path.join(self.dir, "eee"))
        webapp._save_meta(job)
        with open(os.path.join(self.dir, "eee", "meta.json"), encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["ext"], ".mp4")
        self.assertEqual(saved["owner_role"], "user")


if __name__ == "__main__":
    unittest.main()

"""Public pages: /start needs no sign-in and points at the Discord invite.

    python -m unittest tests.test_pages
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("CODEC399_JOBS_DIR", tempfile.mkdtemp(prefix="codec399-pages-"))

import app as webapp  # noqa: E402


class StartPage(unittest.TestCase):
    def setUp(self):
        self.client = webapp.app.test_client()

    def test_start_is_public_and_links_to_discord(self):
        r = self.client.get("/start")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn(f'href="{webapp.DISCORD_INVITE}"', html)
        self.assertIn(f'<link rel="canonical" href="{webapp.PUBLIC_URL}/start">', html)
        self.assertIn("/static/logo.webp", html)
        self.assertIn(">Start</a>", html)
        # The Meta Pixel: the loader, the init with the pixel id, and the noscript fallback.
        self.assertIn("connect.facebook.net/en_US/fbevents.js", html)
        self.assertIn("fbq('init', '1626220608533770')", html)
        self.assertIn("facebook.com/tr?id=1626220608533770&ev=PageView&noscript=1", html)
        self.assertNotIn("4413644252232262", html)
        self.assertIn("fbq('track', 'Lead'", html)

    def test_other_pages_still_need_sign_in(self):
        r = self.client.get("/dashboard")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])


if __name__ == "__main__":
    unittest.main()

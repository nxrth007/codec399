"""Deleting accounts from the Approval tab: admins only, never themselves or another admin.

    python -m unittest tests.test_accounts
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("CODEC399_JOBS_DIR", tempfile.mkdtemp(prefix="codec399-accounts-"))

import auth  # noqa: E402
import app as webapp  # noqa: E402


def _make(email, role):
    auth.register(email, "Str0ngPassw0rd!", "pf-1", "10.0.2.1")
    auth.approve(email, "user")
    if role != "user":
        users = auth.load_users()
        users[email]["role"] = role
        auth._save_users(users)


class DeleteAccount(unittest.TestCase):
    def setUp(self):
        tag = id(self)
        self.admin = f"admin-{tag}@example.com"
        self.other_admin = f"admin2-{tag}@example.com"
        self.buyer = f"buyer-{tag}@example.com"
        self.victim = f"victim-{tag}@example.com"
        _make(self.admin, "admin")
        _make(self.other_admin, "admin")
        _make(self.buyer, "media_buyer")
        _make(self.victim, "user")
        self.client = webapp.app.test_client()

    def tearDown(self):
        for e in (self.admin, self.other_admin, self.buyer, self.victim):
            try:
                auth.remove_user(e)
            except ValueError:
                pass

    def _as(self, email):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["email"] = email

    def test_admin_deletes_an_account(self):
        self._as(self.admin)
        r = self.client.delete(f"/api/accounts/{self.victim}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["deleted"])
        self.assertIsNone(auth.account(self.victim))
        self.assertEqual(self.client.delete(f"/api/accounts/{self.victim}").status_code, 404)

    def test_revoked_accounts_can_be_deleted_too(self):
        auth.revoke(self.victim)
        self._as(self.admin)
        self.assertEqual(self.client.delete(f"/api/accounts/{self.victim}").status_code, 200)
        self.assertIsNone(auth.account(self.victim))

    def test_not_yourself_and_not_admins(self):
        self._as(self.admin)
        self.assertEqual(self.client.delete(f"/api/accounts/{self.admin}").status_code, 400)
        self.assertEqual(self.client.delete(f"/api/accounts/{self.other_admin}").status_code, 400)
        self.assertIsNotNone(auth.account(self.admin))
        self.assertIsNotNone(auth.account(self.other_admin))

    def test_media_buyers_may_not_delete(self):
        self._as(self.buyer)
        self.assertEqual(self.client.delete(f"/api/accounts/{self.victim}").status_code, 403)
        self.assertIsNotNone(auth.account(self.victim))


if __name__ == "__main__":
    unittest.main()

"""Tests for the Whop checkout bodies, the bot's account endpoint and the webhook credit path.

    python -m unittest tests.test_payments
"""
import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="codec399-test-")
os.environ["CODEC399_JOBS_DIR"] = TMP
os.environ["CODEC399_WHOP_WEBHOOK_SECRET"] = "ws_testsecret"
os.environ["CODEC399_BOT_TOKEN"] = "bot-secret"
os.environ["WHOP_COMPANY_ID"] = "biz_test"
os.environ["WHOP_PRODUCT_ID"] = "prod_test"
os.environ["WHOP_API_KEY"] = "key_test"
os.environ["CODEC399_PUBLIC_URL"] = "https://codec.example.com"

import auth  # noqa: E402
import whop  # noqa: E402
import whop_api  # noqa: E402
import app as webapp  # noqa: E402


class CheckoutBodies(unittest.TestCase):
    def test_ads_variant_body(self):
        body = whop_api.ads_variant_body(4)
        self.assertEqual(body["billing_type"], "one_time")
        self.assertEqual(body["initial_price"], 100.0)
        self.assertEqual(body["account_id"], "biz_test")
        self.assertEqual(body["product_id"], "prod_test")
        self.assertEqual(body["visibility"], "hidden")
        self.assertEqual(body["metadata"], {"codec399_ads": 4})
        self.assertEqual(body["title"], "4 ad uploads")

    def test_ads_variant_body_rejects_bad_count(self):
        with self.assertRaises(ValueError):
            whop_api.ads_variant_body(0)
        with self.assertRaises(ValueError):
            whop_api.ads_variant_body(whop_api.MAX_ADS + 1)

    def test_gold_variant_body(self):
        body = whop_api.gold_variant_body()
        self.assertEqual(body["billing_type"], "renewal")
        self.assertEqual(body["billing_period"], 30)
        self.assertEqual(body["initial_price"], 0)
        self.assertEqual(body["renewal_price"], 1500.0)
        self.assertEqual(body["metadata"], {"codec399_gold": True})

    def test_gold_matcher_rejects_setup_fee(self):
        meta = {"codec399_gold": True}
        self.assertFalse(whop_api._matches({"metadata": meta, "renewal_price": 1500.0, "initial_price": 1500.0}, "gold"))
        self.assertTrue(whop_api._matches({"metadata": meta, "renewal_price": 1500.0, "initial_price": 0.0}, "gold"))

    def test_checkout_body(self):
        body = whop_api.checkout_body("plan_x", "Buyer@Example.com", ads=4, discord_id=42)
        self.assertEqual(body["mode"], "payment")
        self.assertEqual(body["variant_id"], "plan_x")
        self.assertEqual(body["metadata"], {"email": "buyer@example.com", "ads": 4, "discord_id": "42"})
        self.assertEqual(body["redirect_url"], "https://codec.example.com/dashboard?paid=1")
        gold = whop_api.checkout_body("plan_g", "buyer@example.com", gold=True)
        self.assertEqual(gold["metadata"], {"email": "buyer@example.com", "gold": True})
        with self.assertRaises(ValueError):
            whop_api.checkout_body("plan_x", "not-an-email", ads=2)

    def test_variant_cache_hit_skips_the_api(self):
        whop_api._save_cache({"ads:2": {"id": "plan_cached", "price": 50.0}})
        self.assertEqual(whop_api.variant_for("ads", 2), "plan_cached")
        whop_api._save_cache({})

    def test_configured(self):
        self.assertEqual(whop_api.configured(), [])


class BotAccountEndpoint(unittest.TestCase):
    def setUp(self):
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()
        for email in list(auth.load_users()):
            auth.remove_user(email)

    def test_requires_token(self):
        r = self.client.get("/api/bot/account?email=x@y.z")
        self.assertEqual(r.status_code, 401)
        r = self.client.get("/api/bot/account?email=x@y.z", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)

    def test_unknown_and_known(self):
        hdr = {"Authorization": "Bearer bot-secret"}
        r = self.client.get("/api/bot/account?email=nobody@example.com", headers=hdr)
        self.assertEqual(r.status_code, 404)
        auth.register("Buyer@example.com", "password123", "PF-1")
        r = self.client.get("/api/bot/account?email=buyer@example.com", headers=hdr)
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["credits"], 0)
        self.assertFalse(data["gold"])


class WebhookCredits(unittest.TestCase):
    def setUp(self):
        for email in list(auth.load_users()):
            auth.remove_user(email)
        self.client = webapp.app.test_client()

    def _deliver(self, event, msg_id):
        raw = json.dumps(event).encode()
        headers = whop.sign(raw, "ws_testsecret", msg_id, int(time.time()))
        return self.client.post("/api/whop/webhook", data=raw, headers=headers)

    def test_payment_approves_and_credits(self):
        auth.register("buyer@example.com", "password123", "PF-1")
        meta = whop_api.checkout_body("plan_x", "buyer@example.com", ads=3)["metadata"]
        event = {"id": "evt_1", "type": "payment.succeeded",
                 "data": {"id": "pay_1", "final_amount": 75, "currency": "usd", "metadata": meta}}
        r = self._deliver(event, "msg_1")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["message"], "Credited.")
        user = auth.get_user("buyer@example.com")
        self.assertEqual(user["status"], "active")
        self.assertEqual(user["credits"], 3)
        # The same delivery again changes nothing.
        r = self._deliver(event, "msg_1")
        self.assertEqual(r.get_json()["message"], "Duplicate delivery ignored.")
        self.assertEqual(auth.get_user("buyer@example.com")["credits"], 3)

    def test_gold_payment(self):
        auth.add_user("gold@example.com", "password123")
        meta = whop_api.checkout_body("plan_g", "gold@example.com", gold=True)["metadata"]
        event = {"id": "evt_2", "type": "payment.succeeded",
                 "data": {"id": "pay_2", "final_amount": 1500, "currency": "usd", "metadata": meta}}
        self.assertEqual(self._deliver(event, "msg_2").get_json()["message"], "Credited.")
        self.assertTrue(auth.get_user("gold@example.com")["gold"])

    def test_unregistered_email_is_not_credited(self):
        meta = whop_api.checkout_body("plan_x", "ghost@example.com", ads=2)["metadata"]
        event = {"id": "evt_3", "type": "payment.succeeded",
                 "data": {"id": "pay_3", "final_amount": 50, "currency": "usd", "metadata": meta}}
        r = self._deliver(event, "msg_3")
        self.assertEqual(r.status_code, 200)
        self.assertIn("no account is registered", r.get_json()["message"])


if __name__ == "__main__":
    unittest.main()

"""Whop payments: webhook verification and turning a paid order into upload credits.

Flow: the Discord bot creates a Whop checkout with metadata {"email": ..., "ads": N} or
{"email": ..., "gold": true}.  Whop copies that metadata onto the payment and sends
`payment.succeeded` here.  We verify the signature (Standard Webhooks: HMAC-SHA256 over
"{id}.{timestamp}.{body}", base64, header `webhook-signature: v1,<sig>`), drop duplicates by
`webhook-id`, then approve the account if it is still pending and add the credits.

Set CODEC399_WHOP_WEBHOOK_SECRET to the secret Whop shows for the webhook (`ws_...`).  Until it is
set the endpoint answers 503 and nothing is credited.  Every event is appended to whop_events.log
next to the accounts file so an unexpected payload can be inspected.  With DISCORD_NOTIFY_WEBHOOK set
to a Discord channel webhook URL, every credited payment (and every one that could not be matched) is
posted there so staff see it in the server.
"""
import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.request

import auth

SECRET = os.environ.get("CODEC399_WHOP_WEBHOOK_SECRET", "")
DATA_DIR = os.path.dirname(auth.USERS_FILE) or "."
SEEN_FILE = os.path.join(DATA_DIR, "whop_seen.json")
LOG_FILE = os.path.join(DATA_DIR, "whop_events.log")
NOTIFY_WEBHOOK = os.environ.get("DISCORD_NOTIFY_WEBHOOK", "")
TOLERANCE = 5 * 60
_lock = threading.Lock()


class WebhookError(ValueError):
    pass


def verify(raw_body, headers, secret=None, now=None):
    """Raise WebhookError unless the Standard Webhooks signature checks out."""
    secret = secret if secret is not None else SECRET
    if not secret:
        raise WebhookError("Webhook secret is not configured.")
    msg_id = headers.get("webhook-id") or ""
    ts = headers.get("webhook-timestamp") or ""
    sig_header = headers.get("webhook-signature") or ""
    if not (msg_id and ts and sig_header):
        raise WebhookError("Missing signature headers.")
    try:
        ts_val = int(ts)
    except ValueError:
        raise WebhookError("Bad timestamp.")
    if abs((now if now is not None else time.time()) - ts_val) > TOLERANCE:
        raise WebhookError("Timestamp outside tolerance.")
    key = secret.encode()
    # Standard Webhooks secrets are usually "whsec_<base64>"; Whop documents using its ws_ value as is.
    if secret.startswith("whsec_"):
        try:
            key = base64.b64decode(secret[len("whsec_"):])
        except ValueError:
            key = secret.encode()
    signed = f"{msg_id}.{ts}.".encode() + (raw_body if isinstance(raw_body, bytes) else raw_body.encode())
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for part in sig_header.split():
        version, _, sig = part.partition(",")
        if version == "v1" and hmac.compare_digest(sig, expected):
            return msg_id
    raise WebhookError("Signature mismatch.")


def sign(raw_body, secret, msg_id, ts):
    """Produce the headers Whop would send; used by tests."""
    key = secret.encode()
    if secret.startswith("whsec_"):
        key = base64.b64decode(secret[len("whsec_"):])
    signed = f"{msg_id}.{ts}.".encode() + raw_body
    sig = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return {"webhook-id": msg_id, "webhook-timestamp": str(ts), "webhook-signature": "v1," + sig}


def _seen(msg_id):
    try:
        with open(SEEN_FILE, encoding="utf-8") as fh:
            seen = json.load(fh)
    except (FileNotFoundError, ValueError):
        seen = {}
    if msg_id in seen:
        return True
    seen[msg_id] = time.time()
    if len(seen) > 5000:
        for k in sorted(seen, key=seen.get)[: len(seen) - 5000]:
            del seen[k]
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SEEN_FILE, "w", encoding="utf-8") as fh:
        json.dump(seen, fh)
    return False


def _log(line):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")


def _notify(text):
    """Post a line to the staff Discord channel, in the background; failures only go to the log."""
    if not NOTIFY_WEBHOOK:
        return

    def post():
        req = urllib.request.Request(NOTIFY_WEBHOOK, data=json.dumps({"content": text[:1900],
                                     "allowed_mentions": {"parse": []}}).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "codec399/1.0"})
        try:
            urllib.request.urlopen(req, timeout=10).close()
        except Exception as exc:  # noqa: BLE001 - never let a notification break the webhook
            _log(f"discord notify failed: {exc}")

    threading.Thread(target=post, daemon=True).start()


def _find(d, *keys):
    """First present value for any of `keys`, looking one level into nested dicts too."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    for v in (d or {}).values() if isinstance(d, dict) else []:
        if isinstance(v, dict):
            for k in keys:
                if v.get(k) not in (None, ""):
                    return v[k]
    return None


def extract_order(event):
    """Pull what we need out of a payment.succeeded event.  Returns a dict with email, ads, gold,
    amount, currency, payment_id; email may be None if neither metadata nor the buyer carry one."""
    data = event.get("data") or {}
    meta = _find(data, "metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    email = (meta.get("email") or _find(data, "email", "user_email", "customer_email") or "")
    email = str(email).strip().lower() or None
    ads = meta.get("ads")
    try:
        ads = int(ads) if ads is not None else 0
    except (TypeError, ValueError):
        ads = 0
    gold = str(meta.get("gold", "")).lower() in ("1", "true", "yes")
    amount = _find(data, "final_amount", "total", "amount", "subtotal")
    return {
        "email": email, "ads": max(0, ads), "gold": gold, "amount": amount,
        "currency": _find(data, "currency"), "payment_id": data.get("id") or event.get("id"),
        "metadata": meta,
    }


ENDED = ("expired", "canceled", "cancelled", "completed", "unpaid", "past_due", "inactive")


def _membership_update(msg_id, event):
    """A Gold subscription that lapses on Whop: when the membership is no longer valid, end Gold.
    Renewals are handled by payment.succeeded, which extends the paid period."""
    data = event.get("data") or {}
    meta = _find(data, "metadata") or {}
    if not isinstance(meta, dict) or str(meta.get("gold", "")).lower() not in ("1", "true", "yes"):
        return 200, "Membership event without Gold metadata; ignored."
    status = str(_find(data, "status") or "").lower()
    valid = _find(data, "valid")
    ended = status in ENDED or valid is False
    email = str(meta.get("email") or _find(data, "email", "user_email") or "").strip().lower()
    if not ended or not email:
        return 200, f"Membership {status or 'update'} noted; nothing changed."
    try:
        auth.end_gold(email, note=f"whop membership {status}")
    except ValueError as exc:
        _log(f"{msg_id}: {exc}")
        return 200, str(exc)
    _log(f"{msg_id}: gold ended for {email} ({status})")
    return 200, "Gold ended."


def handle(raw_body, headers):
    """Verify, de-duplicate and apply one webhook delivery.  Returns (status, message)."""
    try:
        msg_id = verify(raw_body, headers)
    except WebhookError as exc:
        _log(f"rejected: {exc}")
        return 400 if "secret" not in str(exc).lower() else 503, str(exc)
    try:
        event = json.loads(raw_body)
    except ValueError:
        _log(f"{msg_id}: body is not JSON")
        return 400, "Body is not JSON."
    etype = event.get("type")
    with _lock:
        if _seen(msg_id):
            return 200, "Duplicate delivery ignored."
        _log(f"{msg_id}: {etype} " + json.dumps(event)[:2000])
        if etype in ("membership.updated", "membership.deactivated", "membership.canceled", "membership.cancelled"):
            return _membership_update(msg_id, event)
        if etype != "payment.succeeded":
            return 200, f"Ignored {etype}."
        order = extract_order(event)
        if not order["email"]:
            _log(f"{msg_id}: no email in metadata or payment; nothing credited")
            _notify(f"⚠️ Whop payment {order['payment_id']} ({order['amount']} {order['currency']}) carried no email; nothing credited.")
            return 200, "No email on the payment; nothing credited."
        if not order["ads"] and not order["gold"]:
            _log(f"{msg_id}: no ads/gold in metadata; nothing credited")
            return 200, "No order details in metadata; nothing credited."
        try:
            result = auth.apply_purchase(order["email"], order["ads"], order["gold"],
                                         note=f"whop {order['payment_id']} {order['amount']} {order['currency']}")
        except ValueError as exc:
            _log(f"{msg_id}: {exc}")
            _notify(f"⚠️ Whop payment {order['payment_id']} ({order['amount']} {order['currency']}): {exc}")
            return 200, str(exc)
        _log(f"{msg_id}: credited {order['email']} ads={order['ads']} gold={order['gold']} -> {result}")
        what = "Gold (monthly)" if order["gold"] else f"{order['ads']} ad upload{'s' if order['ads'] != 1 else ''}"
        who = f" <@{order['metadata'].get('discord_id')}>" if order["metadata"].get("discord_id") else ""
        _notify(f"✅ Paid: {what} for `{order['email']}`{who} ({order['amount']} {order['currency']}). "
                f"Account {result['status']}, {'Gold' if result['gold'] else str(result['credits']) + ' uploads left'}.")
        return 200, "Credited."

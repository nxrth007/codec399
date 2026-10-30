"""Accounts and sessions for codec399.

Accounts live in a JSON file outside the repo, by default `users.json` in the jobs directory
(`CODEC399_USERS_FILE` overrides the path).  Passwords are stored as werkzeug hashes.  Each
account has a role (`admin` sees the encoder screen and every job; `media_buyer` sees the encoder
screen but only their own jobs; `user` only gets the upload dashboard) and a status: `active`,
`pending` for a self-registration an admin has not yet accepted, or `revoked` for an account an admin
has shut out.  Only active accounts can sign in.  Revoking also blocks the addresses the account was
seen from (`blocked_ips.json` next to the users file) so the person cannot simply register again.  Create and change accounts with manage.py; approve
registrations from the admin screen's Approval tab or with manage.py.

The Flask session cookie is signed with a key kept in `secret.key` next to the users file, so
logins survive a restart (`CODEC399_SECRET_KEY` overrides it).
"""
import json
import os
import secrets
import threading
import time

from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("CODEC399_JOBS_DIR") or os.path.join(BASE, "jobs")
USERS_FILE = os.environ.get("CODEC399_USERS_FILE") or os.path.join(DATA_DIR, "users.json")
ROLES = ("admin", "media_buyer", "user")
ROLE_LABELS = {"admin": "admin", "media_buyer": "media buyer", "user": "user"}
STATUSES = ("active", "pending", "revoked")
BLOCKED_IPS_FILE = os.path.join(os.path.dirname(USERS_FILE) or ".", "blocked_ips.json")
MAX_IPS = 10
GOLD_PERIOD = 31 * 24 * 3600   # a monthly Gold payment covers this long; each renewal extends it

_lock = threading.Lock()


def _normalise(email):
    return (email or "").strip().lower()


def _check_email(email):
    if not email or "@" not in email or " " in email:
        raise ValueError("Enter a valid email address.")


def _check_password(password):
    if len(password or "") < 8:
        raise ValueError("The password must be at least 8 characters.")


def load_users():
    try:
        with open(USERS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    return data.get("users", {})


def _save_users(users):
    os.makedirs(os.path.dirname(USERS_FILE) or ".", exist_ok=True)
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"users": users}, fh, indent=2, sort_keys=True)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, USERS_FILE)


def _public(email, user):
    return {
        "email": email,
        "role": user.get("role", "user"),
        "status": user.get("status", "active"),
        "portfolio_id": user.get("portfolio_id"),
        "created": user.get("created"),
        "revoked": user.get("revoked"),
        "ips": list(user.get("ips") or []),
        "credits": int(user.get("credits") or 0),
        "gold": is_gold(user),
        "gold_manual": bool(user.get("gold")),
        "gold_until": user.get("gold_until"),
        "purchases": list(user.get("purchases") or []),
        "meta": dict(user.get("meta") or {}),
    }


def is_gold(user):
    """Gold is either switched on by an admin (no expiry) or paid monthly (gold_until in the future)."""
    if user.get("gold"):
        return True
    until = user.get("gold_until")
    return bool(until) and float(until) > time.time()


def entitlement(email):
    """(gold, credits) for an account; admins are treated as unlimited."""
    user = load_users().get(_normalise(email)) or {}
    if user.get("role") == "admin":
        return True, 0
    return is_gold(user), int(user.get("credits") or 0)


def add_credits(email, n, note=""):
    """Add (or with a negative n, remove) upload credits.  Returns the new balance."""
    email = _normalise(email)
    n = int(n)
    with _lock:
        users = load_users()
        user = users.get(email)
        if user is None:
            raise ValueError(f"{email} does not exist.")
        user["credits"] = max(0, int(user.get("credits") or 0) + n)
        user.setdefault("purchases", []).append({"at": time.time(), "ads": n, "note": note})
        user["purchases"] = user["purchases"][-50:]
        _save_users(users)
        return user["credits"]


def set_gold(email, on, note=""):
    email = _normalise(email)
    with _lock:
        users = load_users()
        user = users.get(email)
        if user is None:
            raise ValueError(f"{email} does not exist.")
        user["gold"] = bool(on)
        user.setdefault("purchases", []).append({"at": time.time(), "gold": bool(on), "note": note})
        user["purchases"] = user["purchases"][-50:]
        _save_users(users)
        return _public(email, user)


def consume_credit(email):
    """Spend one upload credit; returns the remaining balance.  Gold and admins spend nothing."""
    email = _normalise(email)
    with _lock:
        users = load_users()
        user = users.get(email)
        if user is None:
            raise ValueError(f"{email} does not exist.")
        if user.get("role") == "admin" or is_gold(user):
            return None
        credits = int(user.get("credits") or 0)
        if credits <= 0:
            raise ValueError("No uploads left.")
        user["credits"] = credits - 1
        _save_users(users)
        return user["credits"]


def apply_purchase(email, ads=0, gold=False, note=""):
    """A paid Whop order: approve the account if it is still pending, then credit it."""
    email = _normalise(email)
    with _lock:
        users = load_users()
        user = users.get(email)
        if user is None:
            raise ValueError(f"Paid, but no account is registered as {email}; credit it by hand once they register.")
        if user.get("status") == "pending":
            user["status"] = "active"
            user["approved"] = time.time()
        if user.get("status") == "revoked":
            raise ValueError(f"Paid, but {email} is revoked; reinstate the account to credit it.")
        if gold:
            # Monthly Gold: a payment covers one period from now, or from the current expiry if
            # it renews early, so consecutive renewals chain without a gap.
            base = max(time.time(), float(user.get("gold_until") or 0))
            user["gold_until"] = base + GOLD_PERIOD
        if ads:
            user["credits"] = int(user.get("credits") or 0) + int(ads)
        user.setdefault("purchases", []).append({"at": time.time(), "ads": int(ads), "gold": bool(gold), "note": note})
        user["purchases"] = user["purchases"][-50:]
        _save_users(users)
        return {"status": user["status"], "credits": int(user.get("credits") or 0), "gold": is_gold(user), "gold_until": user.get("gold_until")}


def end_gold(email, note=""):
    """A Gold subscription ended on Whop: drop the paid expiry (an admin-set Gold flag is untouched)."""
    email = _normalise(email)
    with _lock:
        users = load_users()
        user = users.get(email)
        if user is None:
            raise ValueError(f"{email} does not exist.")
        user["gold_until"] = 0
        user.setdefault("purchases", []).append({"at": time.time(), "gold_ended": True, "note": note})
        user["purchases"] = user["purchases"][-50:]
        _save_users(users)
        return _public(email, user)


def _load_blocked():
    try:
        with open(BLOCKED_IPS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}


def _save_blocked(blocked):
    os.makedirs(os.path.dirname(BLOCKED_IPS_FILE) or ".", exist_ok=True)
    tmp = BLOCKED_IPS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(blocked, fh, indent=2, sort_keys=True)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, BLOCKED_IPS_FILE)


def ip_blocked(ip):
    return bool(ip) and ip in _load_blocked()


def record_ip(email, ip):
    """Remember the most recent addresses an account signed in or registered from."""
    email = _normalise(email)
    if not ip:
        return
    with _lock:
        users = load_users()
        user = users.get(email)
        if user is None:
            return
        ips = [x for x in (user.get("ips") or []) if x != ip]
        ips.append(ip)
        user["ips"] = ips[-MAX_IPS:]
        _save_users(users)


def add_user(email, password, role="user"):
    """Create an active account straight away (admin tooling)."""
    email = _normalise(email)
    _check_email(email)
    if role not in ROLES:
        raise ValueError(f"Role must be one of {', '.join(ROLES)}.")
    _check_password(password)
    with _lock:
        users = load_users()
        if email in users:
            raise ValueError(f"{email} already exists.")
        users[email] = {"hash": generate_password_hash(password), "role": role, "status": "active",
                        "created": time.time()}
        _save_users(users)


# Upload credits every new registration starts with, so a new member can try one video before
# buying more in the Discord.  Existing accounts are not touched when this changes.
WELCOME_CREDITS = 1


def register(email, password, portfolio_id, ip=None):
    """Self-registration from the Register page: a pending `user` account awaiting approval, with
    WELCOME_CREDITS uploads to start."""
    email = _normalise(email)
    _check_email(email)
    _check_password(password)
    portfolio_id = (portfolio_id or "").strip()
    if not portfolio_id:
        raise ValueError("Enter your Business Portfolio ID.")
    if len(portfolio_id) > 64:
        raise ValueError("The Business Portfolio ID is too long.")
    if ip_blocked(ip):
        raise ValueError("Registration is not available from your network.")
    with _lock:
        users = load_users()
        if email in users:
            raise ValueError("An account with that email already exists.")
        users[email] = {"hash": generate_password_hash(password), "role": "user", "status": "pending",
                        "portfolio_id": portfolio_id, "created": time.time(), "ips": [ip] if ip else [],
                        "credits": WELCOME_CREDITS}
        _save_users(users)


def account(email):
    """One account, whatever its status, or None."""
    email = _normalise(email)
    user = load_users().get(email)
    return None if user is None else _public(email, user)


def set_meta(email, **fields):
    """Merge fields into the account's Meta Media Library record (folder id, share status, ...)."""
    email = _normalise(email)
    with _lock:
        users = load_users()
        user = users.get(email)
        if user is None:
            raise ValueError(f"No account for {email}.")
        meta = dict(user.get("meta") or {})
        meta.update(fields)
        user["meta"] = meta
        _save_users(users)
        return meta


def accounts():
    """Every account, without password hashes, sorted by email."""
    users = load_users()
    return sorted((_public(e, u) for e, u in users.items()), key=lambda r: r["email"])


def pending():
    """Registrations waiting for a decision, oldest first."""
    users = load_users()
    rows = [_public(e, u) for e, u in users.items() if u.get("status") == "pending"]
    return sorted(rows, key=lambda r: r["created"] or 0)


def approve(email, role="user"):
    email = _normalise(email)
    if role not in ("user", "media_buyer"):
        raise ValueError("A registration can be accepted as a user or a media buyer.")
    with _lock:
        users = load_users()
        user = users.get(email)
        if user is None or user.get("status") != "pending":
            raise ValueError(f"{email} has no pending registration.")
        user["status"] = "active"
        user["role"] = role
        user["approved"] = time.time()
        _save_users(users)
        return _public(email, user)


def decline(email):
    """Remove a pending registration; the person may register again later."""
    email = _normalise(email)
    with _lock:
        users = load_users()
        user = users.get(email)
        if user is None or user.get("status") != "pending":
            raise ValueError(f"{email} has no pending registration.")
        del users[email]
        _save_users(users)


def revoke(email):
    """Shut an account out: it can no longer sign in, its sessions stop working on their next
    request, and the addresses it was seen from are blocked from registering."""
    email = _normalise(email)
    with _lock:
        users = load_users()
        user = users.get(email)
        if user is None:
            raise ValueError(f"{email} does not exist.")
        if user.get("role") == "admin":
            raise ValueError("Admin accounts cannot be revoked from the Approval tab.")
        if user.get("status") == "revoked":
            raise ValueError(f"{email} is already revoked.")
        user["status"] = "revoked"
        user["revoked"] = time.time()
        _save_users(users)
        blocked = _load_blocked()
        for ip in user.get("ips") or []:
            blocked.setdefault(ip, {"email": email, "at": time.time()})
        _save_blocked(blocked)
        return _public(email, user)


def restore(email):
    """Undo a revoke: the account is active again and its address blocks are lifted."""
    email = _normalise(email)
    with _lock:
        users = load_users()
        user = users.get(email)
        if user is None:
            raise ValueError(f"{email} does not exist.")
        if user.get("status") != "revoked":
            raise ValueError(f"{email} is not revoked.")
        user["status"] = "active"
        user.pop("revoked", None)
        _save_users(users)
        blocked = {ip: info for ip, info in _load_blocked().items() if info.get("email") != email}
        _save_blocked(blocked)
        return _public(email, user)


def set_role(email, role):
    """Switch an active, non-admin account between user and media buyer."""
    email = _normalise(email)
    if role not in ("user", "media_buyer"):
        raise ValueError("Role must be user or media_buyer.")
    with _lock:
        users = load_users()
        user = users.get(email)
        if user is None:
            raise ValueError(f"{email} does not exist.")
        if user.get("role") == "admin":
            raise ValueError("Admin accounts are managed with manage.py, not from the Approval tab.")
        if user.get("status", "active") != "active":
            raise ValueError(f"{email} is still waiting for approval.")
        user["role"] = role
        _save_users(users)
        return _public(email, user)


def set_password(email, password):
    email = _normalise(email)
    _check_password(password)
    with _lock:
        users = load_users()
        if email not in users:
            raise ValueError(f"{email} does not exist.")
        users[email]["hash"] = generate_password_hash(password)
        _save_users(users)


def remove_user(email):
    email = _normalise(email)
    with _lock:
        users = load_users()
        if email not in users:
            raise ValueError(f"{email} does not exist.")
        del users[email]
        _save_users(users)


def verify(email, password):
    """Return the account (with email, role and status) when the credentials match, else None.
    A pending account still verifies, so the login page can say it is awaiting approval."""
    email = _normalise(email)
    user = load_users().get(email)
    if user is None:
        # Burn comparable time so a missing account is not distinguishable by latency.
        check_password_hash(generate_password_hash("x" * 12), "y")
        return None
    if not check_password_hash(user["hash"], password or ""):
        return None
    return _public(email, user)


def get_user(email):
    """Active account for an email already in the session, or None if removed or not yet approved."""
    email = _normalise(email)
    user = load_users().get(email)
    if user is None or user.get("status", "active") != "active":
        return None
    return _public(email, user)


def secret_key():
    key = os.environ.get("CODEC399_SECRET_KEY")
    if key:
        return key
    path = os.path.join(os.path.dirname(USERS_FILE) or ".", "secret.key")
    try:
        with open(path, encoding="utf-8") as fh:
            key = fh.read().strip()
            if key:
                return key
    except FileNotFoundError:
        pass
    key = secrets.token_urlsafe(48)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key

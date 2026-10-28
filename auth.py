"""Accounts and sessions for codec399.

Accounts live in a JSON file outside the repo, by default `users.json` in the jobs directory
(`CODEC399_USERS_FILE` overrides the path).  Passwords are stored as werkzeug hashes.  Each
account has a role (`admin` sees the encoder screen and every job; `user` only gets the upload
dashboard) and a status: `active`, or `pending` for a self-registration an admin has not yet
accepted.  Pending accounts cannot sign in.  Create and change accounts with manage.py; approve
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
ROLES = ("admin", "user")
STATUSES = ("active", "pending")

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
    }


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


def register(email, password, portfolio_id):
    """Self-registration from the Register page: a pending `user` account awaiting approval."""
    email = _normalise(email)
    _check_email(email)
    _check_password(password)
    portfolio_id = (portfolio_id or "").strip()
    if not portfolio_id:
        raise ValueError("Enter your Business Portfolio ID.")
    if len(portfolio_id) > 64:
        raise ValueError("The Business Portfolio ID is too long.")
    with _lock:
        users = load_users()
        if email in users:
            raise ValueError("An account with that email already exists.")
        users[email] = {"hash": generate_password_hash(password), "role": "user", "status": "pending",
                        "portfolio_id": portfolio_id, "created": time.time()}
        _save_users(users)


def accounts():
    """Every account, without password hashes, sorted by email."""
    users = load_users()
    return sorted((_public(e, u) for e, u in users.items()), key=lambda r: r["email"])


def pending():
    """Registrations waiting for a decision, oldest first."""
    users = load_users()
    rows = [_public(e, u) for e, u in users.items() if u.get("status") == "pending"]
    return sorted(rows, key=lambda r: r["created"] or 0)


def approve(email):
    email = _normalise(email)
    with _lock:
        users = load_users()
        user = users.get(email)
        if user is None or user.get("status") != "pending":
            raise ValueError(f"{email} has no pending registration.")
        user["status"] = "active"
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

"""Account administration for codec399.

    python manage.py add-user EMAIL [--role admin|media_buyer|user] [--password-stdin]
    python manage.py set-password EMAIL [--password-stdin]
    python manage.py remove-user EMAIL
    python manage.py set-role EMAIL user|media_buyer
    python manage.py revoke EMAIL
    python manage.py restore EMAIL
    python manage.py credits EMAIL N          # add N upload credits (negative removes)
    python manage.py gold EMAIL on|off
    python manage.py approve EMAIL [--role user|media_buyer]
    python manage.py decline EMAIL
    python manage.py list-users
    python manage.py meta-check               # prove the Meta token works; list Media Library folders
    python manage.py meta-sync [EMAIL]        # create/share Meta folders; queue missing uploads via the app

Without --password-stdin the password is asked for twice without echo.  Set CODEC399_JOBS_DIR
(or CODEC399_USERS_FILE) the same way the service does so the account lands in the file the
app reads; on a server that is `/var/lib/codec399/users.json`.
"""
import argparse
import getpass
import json
import os
import sys
import time
import urllib.request

import auth
import meta_library


def _password(args):
    if args.password_stdin:
        pw = sys.stdin.readline().rstrip("\r\n")
    else:
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Again: "):
            sys.exit("The passwords did not match.")
    return pw


def _meta_sync(email=None):
    """Folders synchronously (this process talks to Meta), then ask the running app to queue the
    videos it has not uploaded yet; that part needs CODEC399_INTERNAL_URL and CODEC399_BOT_TOKEN."""
    missing = meta_library.configured()
    if missing:
        sys.exit("Meta is not configured: set " + ", ".join(missing))
    if email:
        targets = [email.strip().lower()]
    else:
        targets = [a["email"] for a in auth.accounts()
                   if a["status"] == "active" and a["role"] != "admin" and a.get("portfolio_id")]
    for e in targets:
        meta = meta_library.ensure_folder(e)
        state = meta.get("share_status") or "not shared"
        print(f"{e}: folder {meta['folder_id']} ({state}" + (f": {meta['share_error']}" if meta.get("share_error") else "") + ")")
    url, token = os.environ.get("CODEC399_INTERNAL_URL", ""), os.environ.get("CODEC399_BOT_TOKEN", "")
    if not (url and token):
        print("Videos: press Sync to Meta in the Gallery (CODEC399_INTERNAL_URL / CODEC399_BOT_TOKEN are not set here).")
        return
    req = urllib.request.Request(url.rstrip("/") + "/api/meta/sync", method="POST",
                                 headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            reply = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001 - report and leave the folders done
        sys.exit(f"Folders done; the app could not queue videos: {exc}")
    print(f"Videos: {reply.get('videos_queued', 0)} queued for upload by the app.")


def main(argv=None):
    p = argparse.ArgumentParser(description="Manage codec399 accounts.")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-user", help="create an active account")
    a.add_argument("email")
    a.add_argument("--role", choices=auth.ROLES, default="user")
    a.add_argument("--password-stdin", action="store_true", help="read the password from the first line of stdin")
    s = sub.add_parser("set-password", help="change an account's password")
    s.add_argument("email")
    s.add_argument("--password-stdin", action="store_true")
    r = sub.add_parser("remove-user", help="delete an account")
    r.add_argument("email")
    ap = sub.add_parser("approve", help="accept a pending registration")
    ap.add_argument("email")
    ap.add_argument("--role", choices=("user", "media_buyer"), default="user")
    sr = sub.add_parser("set-role", help="switch a non-admin account between user and media_buyer")
    sr.add_argument("email")
    sr.add_argument("role", choices=("user", "media_buyer"))
    sub.add_parser("decline", help="remove a pending registration").add_argument("email")
    sub.add_parser("revoke", help="shut an account out and block its addresses from registering").add_argument("email")
    sub.add_parser("restore", help="undo a revoke").add_argument("email")
    cr = sub.add_parser("credits", help="add upload credits (negative removes)")
    cr.add_argument("email")
    cr.add_argument("n", type=int)
    gd = sub.add_parser("gold", help="turn unlimited uploads on or off")
    gd.add_argument("email")
    gd.add_argument("state", choices=("on", "off"))
    sub.add_parser("list-users", help="show every account with its role and status")
    sub.add_parser("meta-check", help="prove the Meta token and business id work")
    ms = sub.add_parser("meta-sync", help="create and share Meta Media Library folders, then queue missing uploads")
    ms.add_argument("email", nargs="?", help="one account; default: every active account with a Business Portfolio ID")
    args = p.parse_args(argv)

    try:
        if args.cmd == "add-user":
            auth.add_user(args.email, _password(args), args.role)
            print(f"Created {args.role} account {args.email.strip().lower()} in {auth.USERS_FILE}")
        elif args.cmd == "set-password":
            auth.set_password(args.email, _password(args))
            print(f"Password updated for {args.email.strip().lower()}")
        elif args.cmd == "remove-user":
            auth.remove_user(args.email)
            print(f"Removed {args.email.strip().lower()}")
        elif args.cmd == "approve":
            auth.approve(args.email, args.role)
            print(f"Approved {args.email.strip().lower()} as {auth.ROLE_LABELS[args.role]}")
        elif args.cmd == "set-role":
            auth.set_role(args.email, args.role)
            print(f"{args.email.strip().lower()} is now a {auth.ROLE_LABELS[args.role]}")
        elif args.cmd == "revoke":
            auth.revoke(args.email)
            print(f"Revoked {args.email.strip().lower()}")
        elif args.cmd == "restore":
            auth.restore(args.email)
            print(f"Restored {args.email.strip().lower()}")
        elif args.cmd == "credits":
            print(f"{args.email.strip().lower()} now has {auth.add_credits(args.email, args.n, note='manage.py')} uploads left")
        elif args.cmd == "gold":
            auth.set_gold(args.email, args.state == "on", note="manage.py")
            print(f"Gold {args.state} for {args.email.strip().lower()}")
        elif args.cmd == "decline":
            auth.decline(args.email)
            print(f"Declined and removed {args.email.strip().lower()}")
        elif args.cmd == "list-users":
            users = auth.load_users()
            if not users:
                print(f"No accounts in {auth.USERS_FILE}")
            for email, u in sorted(users.items()):
                created = time.strftime("%Y-%m-%d", time.gmtime(u.get("created", 0)))
                extra = f"  portfolio {u['portfolio_id']}" if u.get("portfolio_id") else ""
                if u.get("role") != "admin":
                    extra += "  GOLD" if u.get("gold") else f"  credits {int(u.get('credits') or 0)}"
                print(f"{u.get('role', 'user'):11} {u.get('status', 'active'):8} {email}  (created {created}){extra}")
        elif args.cmd == "meta-check":
            info = meta_library.check()
            print(f"Meta business: {info['business_name']} ({info['business_id']}); {len(info['folders'])} folder(s) in the Media Library")
            for f in info["folders"]:
                print(f"  {f['id']}  {f['name']}")
        elif args.cmd == "meta-sync":
            _meta_sync(args.email)
    except (ValueError, meta_library.MetaError) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()

"""Account administration for codec399.

    python manage.py add-user EMAIL [--role admin|user] [--password-stdin]
    python manage.py set-password EMAIL [--password-stdin]
    python manage.py remove-user EMAIL
    python manage.py list-users

Without --password-stdin the password is asked for twice without echo.  Set CODEC399_JOBS_DIR
(or CODEC399_USERS_FILE) the same way the service does so the account lands in the file the
app reads; on a server that is `/var/lib/codec399/users.json`.
"""
import argparse
import getpass
import sys
import time

import auth


def _password(args):
    if args.password_stdin:
        pw = sys.stdin.readline().rstrip("\r\n")
    else:
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Again: "):
            sys.exit("The passwords did not match.")
    return pw


def main(argv=None):
    p = argparse.ArgumentParser(description="Manage codec399 accounts.")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-user", help="create an account")
    a.add_argument("email")
    a.add_argument("--role", choices=auth.ROLES, default="user")
    a.add_argument("--password-stdin", action="store_true", help="read the password from the first line of stdin")
    s = sub.add_parser("set-password", help="change an account's password")
    s.add_argument("email")
    s.add_argument("--password-stdin", action="store_true")
    r = sub.add_parser("remove-user", help="delete an account")
    r.add_argument("email")
    sub.add_parser("list-users", help="show every account and its role")
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
        elif args.cmd == "list-users":
            users = auth.load_users()
            if not users:
                print(f"No accounts in {auth.USERS_FILE}")
            for email, u in sorted(users.items()):
                created = time.strftime("%Y-%m-%d", time.gmtime(u.get("created", 0)))
                print(f"{u.get('role', 'user'):6}  {email}  (created {created})")
    except ValueError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()

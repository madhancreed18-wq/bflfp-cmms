"""Copy the live data into the test copy, so you practise on realistic records.

    python refresh_dev.py              # show what would be copied
    python refresh_dev.py --apply      # overwrite data-dev with a copy of data

Copies the database and, unless you say --db-only, the uploaded photos and
signatures. The live folder is only ever READ — this script cannot damage it.

It always runs one safety pass over the copy: every account's password is reset
to `1234`, and web-push subscriptions are cleared, so a test system can never
notify a real technician's phone or be signed into with a production password.
Use --keep-passwords if you would rather sign in with the real ones.
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(HERE, "data")
DEV = os.path.join(HERE, "data-dev")


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n/1:,.1f} {unit}"
        n /= 1024.0


def tree_size(path):
    total = files = 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
                files += 1
            except OSError:
                pass
    return files, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually copy (default: dry run)")
    ap.add_argument("--db-only", action="store_true", help="skip the uploads folder")
    ap.add_argument("--keep-passwords", action="store_true",
                    help="do not reset passwords in the copy")
    ap.add_argument("--live", default=LIVE)
    ap.add_argument("--dev", default=DEV)
    args = ap.parse_args()

    live_db = os.path.join(args.live, "cmms.db")
    dev_db = os.path.join(args.dev, "cmms.db")
    if not os.path.exists(live_db):
        sys.exit(f"! no live database at {live_db}")

    print(f"  from : {args.live}")
    print(f"  to   : {args.dev}\n")
    print(f"  cmms.db            {os.path.getsize(live_db)/1024/1024:,.1f} MB")
    live_up = os.path.join(args.live, "uploads")
    if not args.db_only and os.path.isdir(live_up):
        n, sz = tree_size(live_up)
        print(f"  uploads            {n:,} file(s), {sz/1024/1024:,.1f} MB")
    if os.path.exists(dev_db):
        print(f"\n  ! the existing test database will be OVERWRITTEN "
              f"({os.path.getsize(dev_db)/1024/1024:,.1f} MB)")
    if not args.keep_passwords:
        print("\n  in the copy: every password reset to 1234, push subscriptions cleared")

    if not args.apply:
        print("\nDry run — nothing copied. Re-run with --apply.")
        return

    os.makedirs(args.dev, exist_ok=True)
    if os.path.exists(dev_db):
        keep = f"{dev_db}.bak-{datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(dev_db, keep)
        print(f"\nOld test database kept as {keep}")
    shutil.copy2(live_db, dev_db)
    print(f"Copied the database.")

    if not args.db_only and os.path.isdir(live_up):
        dev_up = os.path.join(args.dev, "uploads")
        if os.path.isdir(dev_up):
            shutil.rmtree(dev_up)
        shutil.copytree(live_up, dev_up)
        n, _ = tree_size(dev_up)
        print(f"Copied {n:,} uploaded file(s).")

    if not args.keep_passwords:
        sys.path.insert(0, HERE)
        from server.db import hash_pw                      # same hashing the app uses
        c = sqlite3.connect(dev_db)
        pw = hash_pw("1234")
        n = c.execute("UPDATE users SET password=?", (pw,)).rowcount
        cleared = 0
        for tbl in ("push_subs", "subscriptions"):
            try:
                cleared += c.execute(f"DELETE FROM {tbl}").rowcount
            except sqlite3.OperationalError:
                pass
        c.commit()
        c.close()
        print(f"Reset {n} password(s) to 1234 and cleared {cleared} push subscription(s).")

    print("\nDone. Start the test copy with:  Start DEV.bat   ->  http://localhost:8001")


if __name__ == "__main__":
    main()

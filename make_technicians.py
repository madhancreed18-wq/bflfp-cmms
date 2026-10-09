"""One-off: give every technician LOGIN a matching technician PERSON.

Since v129 the app separates two things:

  * a login account  — a credential (username/password). May be shared by a crew.
  * a technician     — a real person. This is what the planner assigns work to,
                       and what carries a photo and a department.

Older databases only have the logins, so the Technicians column and the
job-assignment popup come up empty. This script creates one person per active
technician login, links them (`users.login_id`) so that person's work is visible
when that login signs in, and moves currently-open jobs from the login onto the
person so nothing is orphaned.

The names are deliberately placeholders — "Technician 1", "Technician 2" … —
distinct from the login names so you can tell them apart. Rename them in
Manage → Users → Technicians → Edit.

Safe to run more than once: a login that already has a person is skipped.
Run it with the app stopped if you can; SQLite copes either way.

    python make_technicians.py            # do it
    python make_technicians.py --dry-run  # just show what would happen
"""
import os
import sys
import secrets
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server.db import db, now, hash_pw          # noqa: E402

DRY = "--dry-run" in sys.argv
OPEN_STATUSES = ("Done", "Rejected", "Cancelled")   # everything else counts as open


def main():
    c = db()
    made = moved = 0
    try:
        logins = c.execute(
            "SELECT id,username,name,factory_id,department FROM users"
            " WHERE role='technician' AND active=1 AND COALESCE(can_login,1)=1"
            " ORDER BY id").fetchall()
        if not logins:
            print("No technician login accounts found — nothing to do.")
            return

        print(f"{len(logins)} technician login(s) found\n")
        for i, lg in enumerate(logins, 1):
            existing = c.execute(
                "SELECT id,name FROM users WHERE login_id=? AND COALESCE(can_login,1)=0",
                (lg["id"],)).fetchone()
            if existing:
                print(f"  {lg['username']:<12} already has a person: {existing['name']} — skipped")
                continue

            person_name = f"Technician {i}"
            print(f"  {lg['username']:<12} → creating person '{person_name}'")
            if not DRY:
                pid = c.insert_id(
                    """INSERT INTO users(username,password,name,role,active,factory_id,
                       department,photo,can_login,login_id)
                       VALUES(?,?,?,'technician',1,?,?,'',0,?)""",
                    ("t" + secrets.token_hex(4), hash_pw(secrets.token_hex(16)),
                     person_name, lg["factory_id"], lg["department"] or "", lg["id"]))
                made += 1

                # open work follows the person; finished jobs keep their original history
                qs = ",".join("?" * len(OPEN_STATUSES))
                n = c.execute(
                    f"UPDATE jobs SET lead_tech=? WHERE lead_tech=? AND status NOT IN ({qs})",
                    (pid, lg["id"], *OPEN_STATUSES)).rowcount
                moved += n or 0
                if n:
                    print(f"               moved {n} open job(s) onto them")
        if not DRY:
            c.commit()
    finally:
        c.close()

    print()
    if DRY:
        print("Dry run — nothing was written.")
    else:
        print(f"Done: {made} technician(s) created, {moved} open job(s) re-pointed.")
        print("They now appear in Manage → Users → Technicians and in the")
        print("job-assignment popup. Rename them there, add photos and departments.")


if __name__ == "__main__":
    main()

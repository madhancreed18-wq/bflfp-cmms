"""Give the four accounts that have no department one, and list anyone still without.

Five user records were left with a blank department when the department codes came in
(item 114). Blank is not harmless from 1 October: a CM job raised by an account with no
department is numbered PRD-... — "production" — whoever actually reported it. Four of the
five are engineering people and are set here; manager1 is deliberately left alone until
somebody says what it is.

    planner1   BFLFP engineering planner   → ENG
    tech1      BFLFP crew handset 1        → ENG
    tech2      BFLFP crew handset 2        → ENG
    admin1     admin, all three plants     → ENG
    manager1   NOT TOUCHED — still to be confirmed

Only planner1 and admin1 have ever raised a CM job, so those two are the ones that
actually change anything; the handsets are set so the list is clean.

    python set_departments.py                       # dry run, changes nothing
    python set_departments.py --write                # apply
    python set_departments.py --db "D:\\bflfp-cmms\\data\\cmms.db" --write

Safe to run twice: an account that already has a department is left exactly as it is and
reported as skipped, so this can never quietly overwrite a choice somebody made in Manage.
The department a person is in is only the DEFAULT offered on the report form — whoever
raises a job still picks the department the fault actually belongs to.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

# username → department code. Keyed by username rather than by row id because the ids
# are whatever the seed happened to assign and differ between a dev copy and live.
TARGETS = {
    "planner1": "ENG",
    "tech1": "ENG",
    "tech2": "ENG",
    "admin1": "ENG",
}
LEAVE_ALONE = {"manager1": "waiting on a decision — not set by this script"}


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(here / "data" / "cmms.db"))
    ap.add_argument("--write", action="store_true", help="apply (default: dry run)")
    ap.add_argument("--force", action="store_true",
                    help="also overwrite a department that is already set")
    a = ap.parse_args()

    if not Path(a.db).exists():
        sys.exit(f"no database at {a.db}")
    c = sqlite3.connect(a.db)
    c.row_factory = sqlite3.Row

    # The codes have to exist before anything is filed under them, and they are created
    # by the app at startup. Running this against a database the new build has never
    # opened would file people under a department that is not on the list.
    try:
        codes = {r["code"] for r in c.execute("SELECT code FROM departments WHERE active=1")}
    except sqlite3.OperationalError:
        sys.exit("this database has no departments table yet — start the app once on the\n"
                 "new build first, which creates it and maps the existing user records")
    missing = {v for v in TARGETS.values()} - codes
    if missing:
        sys.exit(f"these codes are not in the departments table: {', '.join(sorted(missing))}")

    print(f"\n{a.db}\n{'WRITING' if a.write else 'DRY RUN — nothing will be changed'}\n")
    done = skipped = absent = 0
    for username, code in TARGETS.items():
        row = c.execute("SELECT id, name, COALESCE(department,'') dep FROM users"
                        " WHERE username=?", (username,)).fetchone()
        if not row:
            print(f"   {username:<10} NOT IN THIS DATABASE")
            absent += 1
            continue
        who = f"{username:<10} {row['name']}"
        if row["dep"] and not a.force:
            print(f"   {who}\n     already set to {row['dep']} — left alone")
            skipped += 1
            continue
        print(f"   {who}\n     {row['dep'] or '(blank)'} → {code}")
        if a.write:
            c.execute("UPDATE users SET department=? WHERE id=?", (code, row["id"]))
        done += 1
    for username, why in LEAVE_ALONE.items():
        row = c.execute("SELECT name FROM users WHERE username=?", (username,)).fetchone()
        if row:
            print(f"   {username:<10} {row['name']}\n     {why}")
    if a.write:
        c.commit()

    # Anyone else still blank. The four above were the known list; this is what makes the
    # script worth running again in a month rather than a one-off.
    rest = [dict(r) for r in c.execute(
        "SELECT u.username, u.name, u.role, f.code fac FROM users u"
        " LEFT JOIN factories f ON f.id=u.factory_id"
        " WHERE COALESCE(u.department,'')='' AND COALESCE(u.active,1)=1"
        " ORDER BY f.code, u.role, u.name")]
    print(f"\n── {'set' if a.write else 'would set'}: {done} · already had one: {skipped}"
          + (f" · not in this database: {absent}" if absent else ""))
    if rest:
        print(f"\n── STILL WITHOUT A DEPARTMENT: {len(rest)}"
              "   (a CM they raise on or after 1 Oct will be numbered PRD-…)")
        for r in rest:
            print(f"   {r['username'] or '(no login)':<12} {r['name']:<28}"
                  f" {r['role']:<11} {r['fac'] or 'all plants'}")
    else:
        print("\n── every active user has a department")

    # Anything still sitting as FREE TEXT. The startup migration turns every value it
    # recognises into a code, so whatever is left did not map — and a department that is
    # not a code is invisible to the new numbering: the job still comes out PRD-…, and
    # the value does not appear in the picker on the report form. Worth seeing, because
    # the answer is usually either "add that code" or "it was a typo".
    odd = [dict(r) for r in c.execute(
        "SELECT COALESCE(u.department,'') dep, COUNT(*) n,"
        "       GROUP_CONCAT(COALESCE(NULLIF(u.username,''), u.name), ', ') who"
        " FROM users u WHERE COALESCE(u.department,'')<>'' AND COALESCE(u.active,1)=1"
        " GROUP BY u.department ORDER BY n DESC, u.department")]
    odd = [r for r in odd if r["dep"] not in codes]
    if odd:
        print(f"\n── FREE TEXT THAT IS NOT A DEPARTMENT CODE: {len(odd)}"
              "\n   (not in the picker, and a CM raised by these people is still PRD-…)")
        for r in odd:
            print(f"   {r['dep']:<24} {r['n']:>3}   {r['who'][:70]}")
        print("   → either add the code to DEPARTMENTS in server/db.py, or set these"
              "\n     people to an existing one in Manage → Users")
    if not a.write:
        print("\nrun again with --write to apply")
    c.close()


if __name__ == "__main__":
    main()

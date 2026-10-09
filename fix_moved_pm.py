"""fix_moved_pm.py — put PM work orders back on the day they were planned for.

Up to b328, planning one day and then opening and saving the NEXT day re-dated every
unfinished job of the first day onto the second (fixed in b329). A PM work order
remembers the day it was made for in `due_date`, so the ones this moved are easy to
find: still unfinished, due on a day that has not passed yet, but now planned for a
LATER day than that.

    .venv\\Scripts\\python.exe fix_moved_pm.py            preview only, changes nothing
    .venv\\Scripts\\python.exe fix_moved_pm.py --apply    preview, ask, then fix

With --apply it lists everything first and changes nothing unless you type YES. Before
writing it takes a full backup of the database using SQLite's own backup, which is safe
while the app is running. Only planned_date is changed; the technician, the crew and
the status are left exactly as they are.

A planner who DELIBERATELY moved a PM to a later day will also appear in the list —
read it before typing YES, and answer NO if anything on it was moved on purpose.
"""
import datetime
import os
import sqlite3
import sys

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cmms.db")
if not os.path.exists(DB):
    DB = r"D:\bflfp-cmms\data\cmms.db"
TODAY = datetime.date.today().isoformat()
APPLY = "--apply" in sys.argv
def _opt(name):
    a = sys.argv
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else None
PLANT = (_opt("--plant") or "").upper()      # e.g. --plant PC   : one plant only
TO = _opt("--to")                             # e.g. --to 2026-09-22 : put them on this day
if TO:
    datetime.date.fromisoformat(TO)           # refuse a date that is not a date
    if TO < TODAY:
        sys.exit("--to must be today or later")

c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row
W = "COALESCE((SELECT m.factory_id FROM machines m WHERE m.id=j.machine_id), j.factory_id)"
rows = c.execute(f"""
    SELECT j.id, j.jobid, j.status, j.planned_date, j.due_date, j.lead_tech,
           SUBSTR(COALESCE(j.planned_at,''),1,16) planned_at, m.code mcode,
           (SELECT code FROM factories WHERE id={W}) plant,
           (SELECT name FROM users WHERE id=j.lead_tech) tech
      FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
     WHERE j.jobtype='PM'
       AND COALESCE(j.due_date,'')<>'' AND COALESCE(j.planned_date,'')<>''
       AND j.due_date >= ? AND j.planned_date > j.due_date
       AND j.status NOT IN ('Done','Cancelled','Rejected','ServiceCompleted')
       AND j.started_at IS NULL
     ORDER BY plant, j.due_date, j.jobid""", (TODAY,)).fetchall()
if PLANT:
    rows = [r for r in rows if (r["plant"] or "").upper() == PLANT]

print("database :", DB)
print("today    :", TODAY)
print()
if not rows:
    print("Nothing to fix - no unfinished PM sits on a later day than it was planned for.")
    sys.exit(0)

plant = None
for r in rows:
    if r["plant"] != plant:
        plant = r["plant"]
        print(f"=== {plant} ===")
    print("  %-14s %-10s due %s  now on %s   tech %-10s  %s" % (
        r["jobid"], r["mcode"] or "", r["due_date"], r["planned_date"],
        r["tech"] or "-", r["status"]))
print(f"\n{len(rows)} PM work order(s) would move to "
      + (TO if TO else "their due day") + (f"  (plant {PLANT} only)" if PLANT else ""))

if not APPLY:
    print("\nPreview only - nothing was changed. Run again with --apply to fix.")
    sys.exit(0)

ans = input("\nMove ALL of these to " + (TO or "their due day") + "? Type YES to continue: ").strip()
if ans != "YES":
    print("Nothing changed.")
    sys.exit(0)

bak = DB + ".bak-before-pm-fix-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
with sqlite3.connect(bak) as dst:
    c.backup(dst)                       # consistent even while the app is writing
print("backed up ->", os.path.basename(bak))

n = 0
for r in rows:
    c.execute("UPDATE jobs SET planned_date=? WHERE id=?", (TO or r["due_date"], r["id"]))
    n += 1
c.commit()

left = c.execute(f"""SELECT COUNT(*) n FROM jobs j WHERE j.jobtype='PM'
                     AND j.due_date >= ? AND j.planned_date > j.due_date
                     AND j.status NOT IN ('Done','Cancelled','Rejected','ServiceCompleted')
                     AND j.started_at IS NULL""", (TODAY,)).fetchone()["n"]
print(f"moved {n} job(s) to {TO or 'their due day'}.")
print("Refresh the Planning page - they are on their own day again.")

"""fix_redated_done_pm.py — put FINISHED PM work orders back on their own day (b379).

28 Sep 2026, BFLFP: last week's 21 Filling PMs (made for 21 Sep, finished 21-23 Sep)
had been moved onto 28 Sep. Each of those machines then "had a PM on the 28th", so
this week's PM was never offered: the calendar showed 21 done, the assign board PM 0.
b379 stops a finished job being moved at all; this puts back the ones already moved.

It finds PM work orders that are finished (Done / ServiceCompleted), were finished
BEFORE the day they now sit on, and sit later than the day they were made for
(`due_date`). Each goes back to its `due_date`. Only planned_date changes; status,
crew, times and signatures are untouched, and a line is added to each job's history.

    .venv\\Scripts\\python.exe fix_redated_done_pm.py                 preview, changes nothing
    .venv\\Scripts\\python.exe fix_redated_done_pm.py --plant FP      one plant only
    .venv\\Scripts\\python.exe fix_redated_done_pm.py --day 2026-09-28  only jobs now on that day
    .venv\\Scripts\\python.exe fix_redated_done_pm.py --apply         preview, ask YES, back up, fix

Safe while the app is running (SQLite's own backup is taken first). After it, refresh
the Plan calendar: the machines' PM for the day comes back as PM due.
"""
import datetime
import os
import sqlite3
import sys

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cmms.db")
if not os.path.exists(DB):
    DB = r"D:\bflfp-cmms\data\cmms.db"
APPLY = "--apply" in sys.argv
a = sys.argv
PLANT = (a[a.index("--plant") + 1] if "--plant" in a and a.index("--plant") + 1 < len(a) else "").upper()
if PLANT in ("BFLFP",):
    PLANT = "FP"
if PLANT in ("BFLPC",):
    PLANT = "PC"
DAY = a[a.index("--day") + 1] if "--day" in a and a.index("--day") + 1 < len(a) else ""
if DAY:
    datetime.date.fromisoformat(DAY)          # refuse a date that is not a date

c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row
W = "COALESCE((SELECT m.factory_id FROM machines m WHERE m.id=j.machine_id), j.factory_id)"
rows = c.execute(f"""
    SELECT j.id, j.jobid, j.status, j.planned_date, j.due_date,
           SUBSTR(COALESCE(j.done_at,''),1,16) done_at, m.code mcode,
           (SELECT code FROM factories WHERE id={W}) plant
      FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
     WHERE j.jobtype='PM' AND j.status IN ('Done','ServiceCompleted')
       AND COALESCE(j.due_date,'')<>'' AND COALESCE(j.planned_date,'')<>''
       AND j.planned_date > j.due_date
       AND COALESCE(j.done_at,'')<>'' AND SUBSTR(j.done_at,1,10) < j.planned_date
     ORDER BY plant, j.planned_date, j.jobid""").fetchall()
if PLANT:
    rows = [r for r in rows if (r["plant"] or "").upper() == PLANT]
if DAY:
    rows = [r for r in rows if r["planned_date"][:10] == DAY]

print("database :", DB)
if not rows:
    print("Nothing to fix - no finished PM sits on a later day than it was finished.")
    sys.exit(0)
plant = None
for r in rows:
    if r["plant"] != plant:
        plant = r["plant"]
        print(f"=== {plant} ===")
    print("  %-14s %-10s %-16s made for %s  finished %s  now on %s  -> back to %s" % (
        r["jobid"], r["mcode"] or "", r["status"], r["due_date"], r["done_at"],
        r["planned_date"], r["due_date"]))
print(f"\n{len(rows)} finished PM work order(s) would go back to their own day"
      + (f"  (plant {PLANT} only)" if PLANT else "") + (f"  (now on {DAY} only)" if DAY else ""))
if not APPLY:
    print("\nPreview only - nothing was changed. Run again with --apply to fix.")
    sys.exit(0)

if input("\nMove ALL of these back? Type YES to continue: ").strip() != "YES":
    print("Nothing changed.")
    sys.exit(0)
bak = DB + ".bak-before-done-pm-fix-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
with sqlite3.connect(bak) as dst:
    c.backup(dst)
print("backed up ->", os.path.basename(bak))
stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
for r in rows:
    c.execute("UPDATE jobs SET planned_date=? WHERE id=?", (r["due_date"], r["id"]))
    c.execute("INSERT INTO messages(job_id,author,text,kind,created_at) VALUES(?,?,?,'system',?)",
              (r["id"], None, f"📅 คืนวันเดิม {r['planned_date']} → {r['due_date']} (งานปิดแล้วถูกย้ายวัน) /"
                              f" put back from {r['planned_date']} to {r['due_date']} (finished job had been moved)",
               stamp))
c.commit()
print(f"moved {len(rows)} job(s) back. Refresh the Plan calendar.")

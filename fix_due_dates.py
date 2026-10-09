"""One-off repair: due dates that fall before the day the work is planned for.

`ensure_pm_jobs()` used to stamp `due_date = the day it noticed the PM was due` and
leave `planned_date` empty. Once a planner scheduled one of those jobs for a later
day, the row was left saying "planned 10 Aug, due 5 Aug" — a deadline five days
before the work was meant to start, so the job counted as late from birth.

The app no longer accepts such a pair (the Re-plan dialog pulls the due date along
with the plan, and the server rejects it), but rows written before that still carry
it. This script pulls each one's due date up to its planned date.

    python fix_due_dates.py              # show what would change, touch nothing
    python fix_due_dates.py --apply      # take a backup, then write

Safe to run twice: the second run finds nothing.
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "cmms.db")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--db", default=DB, help=f"database file (default: {DB})")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"! no database at {args.db}")

    c = sqlite3.connect(args.db)
    c.row_factory = sqlite3.Row
    rows = c.execute("""SELECT j.id, j.jobid, j.jobtype, j.status, j.planned_date, j.due_date,
                               j.jobsource, m.code mcode
                        FROM jobs j LEFT JOIN machines m ON m.id = j.machine_id
                        WHERE j.planned_date IS NOT NULL AND j.planned_date <> ''
                          AND j.due_date    IS NOT NULL AND j.due_date    <> ''
                          AND j.due_date < j.planned_date
                        ORDER BY j.id""").fetchall()

    if not rows:
        print("Nothing to fix — every due date is on or after its planned date.")
        return

    print(f"{len(rows)} job(s) have a due date before their planned date:\n")
    print(f"  {'Job ID':<16} {'Asset':<10} {'Type':<5} {'Status':<18} {'planned':<12} "
          f"{'due (now)':<12} -> {'due (new)'}")
    for r in rows:
        print(f"  {r['jobid']:<16} {r['mcode'] or '—':<10} {r['jobtype'] or '':<5} "
              f"{r['status']:<18} {r['planned_date']:<12} {r['due_date']:<12} -> {r['planned_date']}")

    if not args.apply:
        print(f"\nDry run — nothing written. Re-run with --apply to fix these {len(rows)} row(s).")
        return

    backup = f"{args.db}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(args.db, backup)
    print(f"\nBackup written to {backup}")

    c.executemany("UPDATE jobs SET due_date = planned_date WHERE id = ?",
                  [(r["id"],) for r in rows])
    c.commit()
    left = c.execute("""SELECT COUNT(*) FROM jobs
                        WHERE planned_date IS NOT NULL AND planned_date <> ''
                          AND due_date IS NOT NULL AND due_date <> ''
                          AND due_date < planned_date""").fetchone()[0]
    print(f"Fixed {len(rows)} job(s). Rows still wrong: {left}")
    c.close()


if __name__ == "__main__":
    main()

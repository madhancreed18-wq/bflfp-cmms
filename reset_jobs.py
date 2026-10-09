"""Clear every work order so the plant starts from job 001.

Testing leaves a database full of jobs that never happened. Carrying those into the
first real month poisons every number the system produces — MTBF, downtime, PM
compliance — and the first genuine breakdown gets issued as PRD-2608-011 instead of
PRD-2608-001. This empties the work-order side of the database and nothing else.

  KEPT     machines, users, problem types, PM templates and schedule, crews,
           factories, holidays — everything you set up.
  REMOVED  jobs, their events, timers, sign-offs, job chat, parts moves,
           requisitions, and (with --photos) the before/after pictures.

The database is backed up first, every time, into data/backups/.

    python reset_jobs.py              ask before doing it
    python reset_jobs.py --yes        no questions
    python reset_jobs.py --yes --photos   also delete the job photos

Stop the server first if you can. It is not strictly required — SQLite will wait —
but nothing should be writing a job while its table is being emptied.
"""
import os
import shutil
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "cmms.db")
UPLOADS = os.path.join(HERE, "data", "uploads")

# Order matters only for readability — none of these carry foreign keys that bite.
TABLES = ["job_events", "timelogs", "signoffs", "activities", "part_moves", "requisitions"]
KEEP_CHECK = ["machines", "users", "problem_types", "pm_templates", "plan_teams", "factories"]


def counts(c, tables):
    out = {}
    for t in tables:
        try:
            out[t] = c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        except sqlite3.Error:
            out[t] = "—"
    return out


def main():
    args = sys.argv[1:]
    if not os.path.exists(DB):
        print("! no database at", DB)
        return 1

    con = sqlite3.connect(DB, timeout=20)
    con.execute("PRAGMA busy_timeout=20000")

    jobs = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    by_status = list(con.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status ORDER BY status"))
    print("\n  database : %s" % DB)
    print("  jobs     : %d  (%s)" % (jobs, ", ".join("%s %d" % r for r in by_status) or "none"))
    for t, n in counts(con, TABLES).items():
        print("  %-9s: %s" % (t, n))
    chat = con.execute("SELECT COUNT(*) FROM messages WHERE job_id IS NOT NULL").fetchone()[0]
    print("  job chat : %d messages (channel chat is kept)" % chat)
    print("  keeping  : " + ", ".join("%s %s" % (k, v) for k, v in counts(con, KEEP_CHECK).items()))

    if not jobs:
        print("\n  Nothing to do — there are no jobs.\n")
        return 0

    if "--yes" not in args:
        print("\n  This cannot be undone except from the backup.")
        if input("  Type DELETE to go ahead: ").strip() != "DELETE":
            print("  Cancelled — nothing was changed.\n")
            return 0

    os.makedirs(os.path.join(HERE, "data", "backups"), exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(HERE, "data", "backups", "cmms-before-job-reset-%s.db" % stamp)
    con.close()                      # let the file settle before it is copied
    shutil.copy2(DB, backup)
    print("\n  backup   : %s" % backup)

    con = sqlite3.connect(DB, timeout=20)
    con.execute("PRAGMA busy_timeout=20000")
    for t in TABLES:
        try:
            con.execute("DELETE FROM %s" % t)
        except sqlite3.Error as e:
            print("  ! %s: %s" % (t, e))
    # Chat tied to a job goes with the job. The team channels are a conversation, not
    # a work record, and they stay.
    con.execute("DELETE FROM messages WHERE job_id IS NOT NULL")
    con.execute("DELETE FROM jobs")
    # so a fresh job is row 1 again, not row 15
    con.execute("DELETE FROM sqlite_sequence WHERE name IN "
                "('jobs','job_events','timelogs','signoffs','activities','part_moves',"
                "'requisitions','messages')")
    con.commit()

    photos = 0
    if "--photos" in args and os.path.isdir(UPLOADS):
        for f in os.listdir(UPLOADS):
            head = f.split("_", 1)[0]
            if head.isdigit():       # 13_before.jpg — named after the job it belonged to
                try:
                    os.remove(os.path.join(UPLOADS, f))
                    photos += 1
                except OSError:
                    pass

    try:
        con.execute("VACUUM")        # only works if nothing else holds the file
    except sqlite3.Error:
        pass
    print("  jobs     : %d" % con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
    for t, n in counts(con, TABLES).items():
        print("  %-9s: %s" % (t, n))
    if "--photos" in args:
        print("  photos   : %d removed" % photos)
    print("  integrity: %s" % con.execute("PRAGMA integrity_check").fetchone()[0])
    ym = datetime.now().strftime("%y%m")
    print("\n  The next breakdown will be issued as BKD-%s-001.\n" % ym)
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

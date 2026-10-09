"""One-off: permanently delete job records, and optionally switch off the legacy
per-machine PM generator.

Removes the job rows plus their time logs, status history, chat messages, sign-offs,
activities, and any spare-part movements or requisitions, so no orphaned rows remain.

    python delete_cm_jobs.py                            # dry run: CM + BD
    python delete_cm_jobs.py --apply                    # back up, then delete

  PM work orders need an explicit opt-in, because deleting them is not enough on its
  own — see --stop-legacy-pm below:

    python delete_cm_jobs.py --delete-pm --stop-legacy-pm            # dry run
    python delete_cm_jobs.py --delete-pm --stop-legacy-pm --apply

--stop-legacy-pm clears `machines.pm_freq_days`. That field is read by exactly one
thing: `ensure_pm_jobs()` in kpi.py, which runs on every planner/admin sign-in and
recreates a PM job for any machine whose interval has elapsed — regardless of the
PM program's Start/Stop switch. Delete the PM jobs without clearing it and they are
back at the next login. Clearing it leaves PM entirely to the real PM plan
(pm_templates / pm_items and the Start button on the PM calendar), which is
untouched by this script.

Stop the server first. Uploaded photos and signatures under data/uploads are left
on disk; remove them by hand if you want them gone too.
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "cmms.db")

# every table that points back at a job, so nothing is orphaned
CHILDREN = ["timelogs", "job_events", "messages", "signoffs",
            "activities", "part_moves", "requisitions"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--types", default="CM,BD", help="job types to delete (default: CM,BD)")
    ap.add_argument("--delete-pm", action="store_true",
                    help="also delete PM work orders (explicit opt-in)")
    ap.add_argument("--stop-legacy-pm", action="store_true",
                    help="clear machines.pm_freq_days so the old generator stops recreating PM jobs")
    ap.add_argument("--db", default=DB, help=f"database file (default: {DB})")
    args = ap.parse_args()

    types = [t.strip().upper() for t in args.types.split(",") if t.strip()]
    if args.delete_pm and "PM" not in types:
        types.append("PM")
    if "PM" in types and not args.delete_pm:
        sys.exit("! PM jobs need --delete-pm as well, so they are never removed by accident")
    if not types:
        sys.exit("! no job types given")
    if not os.path.exists(args.db):
        sys.exit(f"! no database at {args.db}")

    c = sqlite3.connect(args.db)
    c.row_factory = sqlite3.Row
    ph = ",".join("?" * len(types))
    rows = c.execute(f"SELECT id, jobid, jobtype, status FROM jobs"
                     f" WHERE UPPER(COALESCE(jobtype,'')) IN ({ph}) ORDER BY id", types).fetchall()
    legacy = c.execute("SELECT id, code, name, pm_freq_days FROM machines"
                       " WHERE active=1 AND pm_freq_days>0 ORDER BY code").fetchall()

    if not rows and not (args.stop_legacy_pm and legacy):
        print(f"Nothing to do — no {'/'.join(types)} jobs, and no machine has a PM interval set.")
        return

    ids = [r["id"] for r in rows]
    total_children = 0
    if rows:
        iph = ",".join("?" * len(ids))
        print(f"Delete {len(rows)} job(s) of type {'/'.join(types)}:\n")
        by_type = {}
        for r in rows:
            by_type.setdefault(r["jobtype"], []).append(r)
        for t, rs in sorted(by_type.items()):
            print(f"  {t}: {len(rs)} — {', '.join(x['jobid'] for x in rs[:6])}"
                  + (" …" if len(rs) > 6 else ""))
        print("\n  attached records that go with them:")
        for tbl in CHILDREN:
            try:
                n = c.execute(f"SELECT COUNT(*) FROM {tbl} WHERE job_id IN ({iph})", ids).fetchone()[0]
            except sqlite3.OperationalError:
                continue                              # table not in this database
            total_children += n
            print(f"    {tbl:<14} {n}")
        kept = c.execute(f"SELECT COUNT(*) FROM jobs WHERE id NOT IN ({iph})", ids).fetchone()[0]
        print(f"\n  jobs that will remain: {kept}")
    else:
        print(f"No {'/'.join(types)} jobs to delete.")

    if args.stop_legacy_pm:
        print(f"\nSwitch off the legacy PM generator on {len(legacy)} machine(s):")
        for m in legacy:
            print(f"    {m['code']:<12} every {m['pm_freq_days']:>4} days -> off")
        if not legacy:
            print("    (none — it is already off)")
        print("  The PM plan itself (templates, checklist items, calendar) is not touched.")

    if not args.apply:
        print(f"\nDry run — nothing written. Re-run with --apply.")
        return

    backup = f"{args.db}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(args.db, backup)
    print(f"\nBackup written to {backup}")

    if ids:
        iph = ",".join("?" * len(ids))
        for tbl in CHILDREN:
            try:
                c.execute(f"DELETE FROM {tbl} WHERE job_id IN ({iph})", ids)
            except sqlite3.OperationalError:
                pass
        c.execute(f"DELETE FROM jobs WHERE id IN ({iph})", ids)
    if args.stop_legacy_pm:
        c.execute("UPDATE machines SET pm_freq_days=0 WHERE pm_freq_days>0")
    c.commit()

    remaining = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    still_gen = c.execute("SELECT COUNT(*) FROM machines"
                          " WHERE active=1 AND pm_freq_days>0").fetchone()[0]
    print(f"Deleted {len(rows)} job(s) and {total_children} attached record(s).")
    print(f"Jobs remaining: {remaining} · machines still auto-generating PM: {still_gen}")
    tpl = c.execute("SELECT COUNT(*) FROM pm_templates").fetchone()[0]
    itm = c.execute("SELECT COUNT(*) FROM pm_items").fetchone()[0]
    print(f"PM plan intact: {tpl} template(s), {itm} checklist item(s)")
    c.close()


if __name__ == "__main__":
    main()

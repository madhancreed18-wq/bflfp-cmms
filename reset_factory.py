# -*- coding: utf-8 -*-
"""Empty ONE factory completely — assets, PM plan, work orders — so it can be
loaded again from scratch.

    python reset_factory.py                    # dry run against data\\  (live)
    python reset_factory.py --apply            # do it
    set BFLFP_DATA=data-dev & python reset_factory.py --apply     # the test copy

Stop the server first, and run it on the Windows machine — SQLite cannot take its
lock over a network or sandbox mount.

What it deletes for the chosen factory:
    machines                     the asset register
    pm_templates / pm_items      the checklists
    pm_plans / pm_plan_sched     saved PM programmes
    pm_members                   sheets pinned to an asset by hand
    pm_marks / pm_overrides      per-machine checklist ticks and moved PM dates
    pm_config / pm_groups        the PM start/stop switch and the colour groups
    jobs + every child table     work orders of every type, and their logs
What it keeps: users, teams, factories, and every other factory's data.

Afterwards, in the app: Planner → Import assets (the register .xlsx), then
Planner → Upload PM plan (the PM Plan .xlsx). The upload refuses to run until
the asset register is in.
"""
import argparse, os, shutil, sys
from contextlib import closing
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server.config import DATA, DB_PATH                      # noqa: E402
from server.db import db                                     # noqa: E402

JOB_CHILDREN = ["timelogs", "job_events", "messages", "signoffs",
                "activities", "part_moves", "requisitions"]


def count(c, sql, args=()):
    try:
        return c.execute(sql, args).fetchone()[0]
    except Exception:
        return 0                                             # table not in this build


def run(c, sql, args=()):
    try:
        c.execute(sql, args)
    except Exception as e:
        print("   skipped: %s (%s)" % (sql[:60], e))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--factory", type=int, default=2, help="factory id (BFL 1 · FP 2 · PC 3)")
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    a = ap.parse_args()
    fac = a.factory

    print("data folder : %s" % DATA)
    print("database    : %s" % DB_PATH)
    print("factory     : %d\n" % fac)

    with closing(db()) as c:
        tids = [r[0] for r in c.execute("SELECT id FROM pm_templates WHERE factory_id=?", (fac,))]
        mids = [r[0] for r in c.execute("SELECT id FROM machines WHERE factory_id=?", (fac,))]
        # a job belongs to a factory through its machine — the jobs table has no factory_id
        jobs = []
        if mids:
            ph = ",".join("?" * len(mids))
            jobs = [r[0] for r in c.execute("SELECT id FROM jobs WHERE machine_id IN (%s)" % ph, mids)]
        orphans = count(c, "SELECT COUNT(*) FROM jobs WHERE machine_id IS NULL OR machine_id=0")
        rows = [("machines (assets)", len(mids)),
                ("pm_templates", len(tids)),
                ("pm_items", count(c, "SELECT COUNT(*) FROM pm_items")),
                ("pm_plans", count(c, "SELECT COUNT(*) FROM pm_plans WHERE factory_id=?", (fac,))),
                ("pm_members (pinned sheets)", count(c, "SELECT COUNT(*) FROM pm_members WHERE factory_id=?", (fac,))),
                ("pm_groups (colour groups)", count(c, "SELECT COUNT(*) FROM pm_groups WHERE factory_id=?", (fac,))),
                ("jobs (work orders)", len(jobs))]
        if orphans:
            print("   %-28s %d   (no machine — left alone)" % ("jobs with no machine", orphans))
        for name, n in rows:
            print("   %-28s %d" % (name, n))

        if not a.apply:
            print("\nDRY RUN — nothing deleted. Re-run with --apply.")
            return

        bak = DB_PATH + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(DB_PATH, bak)
        print("\nbacked up the database -> %s" % os.path.basename(bak))

        if jobs:
            ph = ",".join("?" * len(jobs))
            for t in JOB_CHILDREN:
                run(c, "DELETE FROM %s WHERE job_id IN (%s)" % (t, ph), jobs)
            run(c, "DELETE FROM jobs WHERE id IN (%s)" % ph, jobs)
        if tids:
            ph = ",".join("?" * len(tids))
            run(c, "DELETE FROM pm_items WHERE template_id IN (%s)" % ph, tids)
        pids = [r[0] for r in c.execute("SELECT id FROM pm_plans WHERE factory_id=?", (fac,))]
        if pids:
            ph = ",".join("?" * len(pids))
            run(c, "DELETE FROM pm_plan_sched WHERE plan_id IN (%s)" % ph, pids)
        if mids:
            ph = ",".join("?" * len(mids))
            run(c, "DELETE FROM pm_marks WHERE machine_id IN (%s)" % ph, mids)
            run(c, "DELETE FROM pm_overrides WHERE machine_id IN (%s)" % ph, mids)
        for sql in ("DELETE FROM pm_plans WHERE factory_id=?",
                    "DELETE FROM pm_members WHERE factory_id=?",
                    "DELETE FROM pm_templates WHERE factory_id=?",
                    "DELETE FROM pm_groups WHERE factory_id=?",
                    "DELETE FROM pm_config WHERE factory_id=?",
                    "DELETE FROM machines WHERE factory_id=?"):
            run(c, sql, (fac,))
        c.commit()

        print("\nafter:")
        print("   machines     %d" % count(c, "SELECT COUNT(*) FROM machines WHERE factory_id=?", (fac,)))
        print("   pm_templates %d" % count(c, "SELECT COUNT(*) FROM pm_templates WHERE factory_id=?", (fac,)))
        print("   jobs         %d" % count(c, "SELECT COUNT(*) FROM jobs"))
    print("\nNow, in the app: Planner → Import assets, then Planner → Upload PM plan.")


if __name__ == "__main__":
    main()

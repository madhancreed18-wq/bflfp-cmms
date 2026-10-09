# ══════════════════════════════════════════════════════════════════════════════════
#  Put two training PM jobs back the way the planner left them.
#
#  PRM-2609-018 (W01ST05) and PRM-2609-021 (W01CT07) were worked through as a training
#  run: started, checklisted, stopped, signed and accepted. Everything after the
#  planner's assignment is training data — it is in the work-time figure, the PM
#  compliance figure and the filed paperwork, and none of it happened on a machine.
#
#  This puts each job back to the state its 19 sisters are still in: Assigned to Mark
#  (lead) with Choke as helper, both working through the tech1 login, planned for
#  04/09/2569, nothing done yet. The assignment itself is NOT touched — that was real
#  planning, and the crew still has to do these two.
#
#  Nothing is deleted that belongs to another job. Run with no arguments first: it
#  prints every row it would change and writes nothing.
#
#      python fix_training_pm.py  path\to\cmms.db            ← dry run, shows the plan
#      python fix_training_pm.py  path\to\cmms.db  --apply   ← writes, after a backup
# ══════════════════════════════════════════════════════════════════════════════════
import sys, os, shutil, sqlite3
from datetime import datetime

TARGETS = [("PRM-2609-018", "W01ST05"), ("PRM-2609-021", "W01CT07")]

# what a still-assigned PM job looks like — job 23 is the reference
RESET = {"status": "Assigned", "stage1": "Reported", "stage2": "Assigned",
         "progress": 0, "done_at": None, "started_at": None, "approved_at": None,
         "approver_id": None, "sign_tech": "", "sign_appr": "",
         "report_name": "", "solution": ""}
# left alone on purpose: lead_tech, helpers, planned_date, planned_at, descr,
# pm_freq, due_date, created_at, created_by — the planner's work, not the training run
KEEP_EVENTS = ("Reported", "Assigned")


def main():
    if len(sys.argv) < 2:
        print(__doc__ or "usage: fix_training_pm.py <cmms.db> [--apply]")
        return 1
    path = sys.argv[1]
    apply = "--apply" in sys.argv
    if not os.path.exists(path):
        print("not found:", path)
        return 1

    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row

    # ── find them, and refuse if they are not what this script was written for ──
    jobs = []
    for jobid, mcode in TARGETS:
        r = c.execute("""SELECT j.*, m.code mcode FROM jobs j
                         LEFT JOIN machines m ON m.id=j.machine_id
                         WHERE j.jobid=?""", (jobid,)).fetchone()
        if not r:
            print("STOP — %s is not in this database" % jobid)
            return 2
        if (r["mcode"] or "") != mcode:
            print("STOP — %s is on %s, expected %s" % (jobid, r["mcode"], mcode))
            return 2
        if r["jobtype"] != "PM":
            print("STOP — %s is a %s job, not PM" % (jobid, r["jobtype"]))
            return 2
        if r["status"] == "Assigned":
            print("SKIP — %s is already back to Assigned" % jobid)
            continue
        jobs.append(r)
    if not jobs:
        print("Nothing to do.")
        return 0

    ids = [j["id"] for j in jobs]
    q = ",".join("?" * len(ids))

    print("=" * 78)
    print("PLAN" if not apply else "APPLYING")
    print("=" * 78)
    for j in jobs:
        print("\n%s  ·  %s  ·  %s" % (j["jobid"], j["mcode"], j["descr"].split("\n")[0][:46]))
        print("   status      %-22s → Assigned" % j["status"])
        print("   started_at  %-22s → (cleared)" % (j["started_at"] or "—"))
        print("   done_at     %-22s → (cleared)" % (j["done_at"] or "—"))
        print("   approved_at %-22s → (cleared)" % (j["approved_at"] or "—"))
        print("   report_name %-22s → (cleared)" % (j["report_name"] or "—"))
        print("   progress    %-22s → 0" % j["progress"])
        print("   KEPT: lead_tech=%s helpers=%s planned=%s"
              % (j["lead_tech"], j["helpers"], j["planned_date"]))

    def count(t, extra=""):
        return c.execute("SELECT COUNT(*) FROM %s WHERE job_id IN (%s) %s" % (t, q, extra),
                         ids).fetchone()[0]
    print("\nrows to delete")
    print("   timelogs     %d   (the training clock)" % count("timelogs"))
    print("   pm_results   %d   (the checklist answers)" % count("pm_results"))
    print("   signoffs     %d   (the acceptance signature)" % count("signoffs"))
    print("   messages     %d   (the system status note)" % count("messages"))
    print("   job_events   %d   (InProgress / ServiceCompleted / Done only)"
          % count("job_events", "AND status NOT IN ('Reported','Assigned')"))

    # ── the files those rows point at ──────────────────────────────────────────
    root = os.path.dirname(os.path.abspath(path))
    files = []
    for j in jobs:
        for col in ("sign_tech", "sign_appr"):
            if j[col]:
                f = os.path.join(root, str(j[col]).lstrip("/").replace("/", os.sep))
                if os.path.exists(f):
                    files.append(f)
        if j["report_name"]:
            day = str(j["done_at"] or j["planned_date"])[:10].split("-")
            if len(day) == 3:
                d = os.path.join(root, "Report", day[0], day[1], day[2])
                for nm in ("PM_%s.pdf" % j["report_name"],):
                    f = os.path.join(d, nm)
                    if os.path.exists(f):
                        files.append(f)
    print("\nfiles to delete")
    for f in files:
        print("   " + f)
    if not files:
        print("   (none found beside this database — delete them on the server by hand)")

    if not apply:
        print("\n" + "=" * 78)
        print("DRY RUN — nothing was written. Re-run with --apply to make these changes.")
        return 0

    # ── backup first, always ───────────────────────────────────────────────────
    bak = path + ".before-training-reset-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, bak)
    print("\nbackup written: " + bak)

    c.execute("UPDATE jobs SET %s WHERE id IN (%s)"
              % (",".join(k + "=?" for k in RESET), q),
              (*RESET.values(), *ids))
    for t in ("timelogs", "pm_results", "signoffs", "messages"):
        c.execute("DELETE FROM %s WHERE job_id IN (%s)" % (t, q), ids)
    c.execute("DELETE FROM job_events WHERE job_id IN (%s) AND status NOT IN ('Reported','Assigned')" % q, ids)
    c.commit()

    for f in files:
        try:
            os.remove(f)
            print("   deleted " + f)
        except OSError as e:
            print("   COULD NOT DELETE " + f + " — " + str(e))

    print("\nafter:")
    for r in c.execute("""SELECT j.jobid,j.status,j.stage1,j.stage2,j.lead_tech,j.helpers,j.planned_date,
                                 (SELECT COUNT(*) FROM timelogs t WHERE t.job_id=j.id) tl,
                                 (SELECT COUNT(*) FROM pm_results p WHERE p.job_id=j.id) pr
                          FROM jobs j WHERE j.id IN (%s)""" % q, ids):
        print("   %s  %s / %s / %s  lead=%s helpers=%s planned=%s  timelogs=%d results=%d"
              % (r["jobid"], r["status"], r["stage1"], r["stage2"], r["lead_tech"],
                 r["helpers"], r["planned_date"], r["tl"], r["pr"]))
    c.close()
    print("\nDone. Copy this database back to the server and start the app.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

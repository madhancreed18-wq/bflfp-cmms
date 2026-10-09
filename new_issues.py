"""What the operators have reported — read straight from the live database.

    python new_issues.py                 # the last 7 days, all three plants
    python new_issues.py --days 14
    python new_issues.py --fac FP        # one plant: BFL / FP / PC
    python new_issues.py --open          # only the ones nobody has finished
    python new_issues.py --csv out.csv   # the same rows as a file

Reports raised by an OPERATOR (jobsource = OperatorReport) — the faults the shop floor
found, as opposed to PM coming out of the programme or work a planner raised. Newest
first, grouped by day, with the one thing a morning meeting actually needs beside each:
whether anybody has picked it up yet, and how long it has been sitting.

Read-only. It opens the database, prints, and closes — nothing is written, so it is safe
to run against the live file while the server is up.
"""
import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

OPEN_ST = ("Reported", "WaitingApproval", "WaitingAssignment", "Assigned",
           "InProgress", "Paused", "Rework", "Hold")
# what each status means to somebody reading the list, rather than to the code
SAY = {"Reported": "nobody assigned yet", "WaitingAssignment": "nobody assigned yet",
       "WaitingApproval": "waiting approval", "Assigned": "with a crew",
       "InProgress": "being worked on", "Paused": "paused", "Hold": "on hold",
       "Rework": "sent back to the crew", "ServiceCompleted": "done, awaiting accept",
       "Done": "closed", "Rejected": "rejected", "Cancelled": "cancelled"}


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(here / "data" / "cmms.db"))
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--fac", default="", help="BFL, FP or PC — default: all three")
    ap.add_argument("--open", action="store_true", dest="only_open",
                    help="only reports that are not finished")
    ap.add_argument("--csv", default="", help="also write the rows to this file")
    a = ap.parse_args()

    if not Path(a.db).exists():
        sys.exit(f"no database at {a.db}")
    since = (datetime.now() - timedelta(days=a.days)).strftime("%Y-%m-%d 00:00:00")
    c = sqlite3.connect(a.db)
    c.row_factory = sqlite3.Row

    # req_dept arrived with the department codes; an older backup will not have it, and
    # this script should still run against one rather than dying on a missing column
    has_dept = any(r[1] == "req_dept" for r in c.execute("PRAGMA table_info(jobs)"))
    dept_col = "j.req_dept" if has_dept else "'' req_dept"
    q = """SELECT j.jobid, j.jobtype, j.status, j.created_at, j.descr, j.problem_type,
                  j.priority, j.production_impact, j.planned_date, j.lead_tech,
                  """ + dept_col + """, m.code mcode, m.name mname, m.line mline,
                  f.code fac, u.name reporter, lt.name lead
             FROM jobs j
             LEFT JOIN machines  m  ON m.id = j.machine_id
             LEFT JOIN factories f  ON f.id = COALESCE(m.factory_id, j.factory_id)
             LEFT JOIN users     u  ON u.id = COALESCE(j.requester_id, j.created_by)
             LEFT JOIN users     lt ON lt.id = j.lead_tech
            WHERE j.jobsource = 'OperatorReport'
              AND j.created_at >= ?"""
    args = [since]
    if a.fac:
        q += " AND f.code = ?"
        args.append(a.fac.upper())
    if a.only_open:
        q += " AND j.status IN (%s)" % ",".join("?" * len(OPEN_ST))
        args += list(OPEN_ST)
    q += " ORDER BY j.created_at DESC"
    rows = [dict(r) for r in c.execute(q, args)]
    c.close()

    scope = a.fac.upper() if a.fac else "all plants"
    print(f"\nOperator reports · last {a.days} days · {scope}"
          f"{' · not finished only' if a.only_open else ''}")
    print(f"since {since[:10]}   ·   {len(rows)} report(s)\n")
    if not rows:
        print("  nothing reported in that window\n")
        return

    now = datetime.now()
    day = None
    for r in rows:
        d = (r["created_at"] or "")[:10]
        if d != day:
            day = d
            when = datetime.strptime(d, "%Y-%m-%d")
            ago = (now.date() - when.date()).days
            print(f"── {d}  ({'today' if ago == 0 else 'yesterday' if ago == 1 else f'{ago} days ago'})")
        # the fault in one line: the symptom the operator picked, else what they typed
        what = (r["problem_type"] or r["descr"] or "").split("\n")[0].strip()
        what = (what[:52] + "…") if len(what) > 53 else what
        where = r["mcode"] or "(no asset)"
        stop = "  ⛔ STOPPED" if (r["production_impact"] or "") == "Stopped" else ""
        pri = {3: " ‼", 2: " !"}.get(r["priority"] or 1, "")
        state = SAY.get(r["status"], r["status"])
        if r["status"] in ("Reported", "WaitingAssignment") and not r["lead_tech"]:
            hrs = int((now - datetime.strptime(r["created_at"][:19],
                                               "%Y-%m-%d %H:%M:%S")).total_seconds() // 3600)
            state += f" — {hrs}h" if hrs < 48 else f" — {hrs // 24}d"
        who = r["lead"] or ""
        print(f"   {r['created_at'][11:16]}  {r['fac'] or '?':<4} {r['jobid']:<14}"
              f" {r['jobtype']:<3}{pri:<3} {where:<10} {what:<54} {state}"
              f"{(' · ' + who) if who else ''}{stop}")
        if r["mname"]:
            print(f"          {'':<4} {'':<14} {'':<6} {r['mname'][:44]}"
                  f"{('  · ' + r['mline']) if r['mline'] else ''}"
                  f"{('  · reported by ' + r['reporter']) if r['reporter'] else ''}")
    print()

    nobody = [r for r in rows if r["status"] in ("Reported", "WaitingAssignment")
              and not r["lead_tech"]]
    stopped = [r for r in rows if (r["production_impact"] or "") == "Stopped"
               and r["status"] in OPEN_ST]
    print(f"── {len(rows)} reported · {len([r for r in rows if r['status'] in OPEN_ST])} still open"
          f" · {len(nobody)} with nobody assigned"
          + (f" · {len(stopped)} with the machine STOPPED" if stopped else ""))
    if nobody:
        print("   nobody assigned: " + ", ".join(r["jobid"] for r in nobody[:12])
              + (" …" if len(nobody) > 12 else ""))
    print()

    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["Reported", "Plant", "Job ID", "Type", "Priority", "Asset",
                        "Machine", "Line", "Symptom", "Status", "Lead tech",
                        "Production", "Reported by", "Dept"])
            for r in rows:
                w.writerow([r["created_at"], r["fac"], r["jobid"], r["jobtype"],
                            r["priority"], r["mcode"], r["mname"], r["mline"],
                            (r["problem_type"] or r["descr"] or "").replace("\n", " "),
                            r["status"], r["lead"], r["production_impact"],
                            r["reporter"], r["req_dept"]])
        print(f"   written to {a.csv}\n")


if __name__ == "__main__":
    main()

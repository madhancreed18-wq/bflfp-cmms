"""check_plan.py — why did a day's plan disappear?  READ-ONLY.

Run it against the LIVE database (over the share from raja, or on the server PC):

    .venv\\Scripts\\python.exe check_plan.py --db "\\\\DESKTOP-0BE5LED\\bflfp-cmms\\data\\cmms.db" --plant PC --date 2026-09-18

It never writes. It opens the file read-only (immutable), so it cannot disturb the
running app, and it prints:

  * the file it read, its size and the moment it was last written — if this timestamp
    is older than the save, the save did not reach this file at all;
  * the jobs created that day for that plant: how many, of what type, on what date,
    in what status, and who they are assigned to;
  * the PM work orders created that day (the "46 PM work orders created" message) —
    the whole question is whether they are in here;
  * the crews saved for that day (plan_teams);
  * the status history written that day, newest first, which shows an Assigned that
    later became Reported again, and by whom;
  * anything odd: a jobs VIEW instead of a table, journal files left behind, a plant
    whose machines are filed under a different factory.

Run it TWICE: once immediately after pressing Save, and once after reopening the board
and seeing the work gone. The difference between the two answers the question.
"""
import argparse
import datetime
import os
import sqlite3


def open_ro(path):
    uri = "file:" + path.replace("\\", "/").replace("?", "%3f").replace("#", "%23")
    return sqlite3.connect(uri + "?mode=ro&immutable=1", uri=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="path to cmms.db")
    ap.add_argument("--plant", default="PC", help="plant code: BFL, FP or PC")
    ap.add_argument("--date", default=datetime.date.today().isoformat(), help="YYYY-MM-DD")
    a = ap.parse_args()
    d = a.date[:10]

    print("=" * 78)
    print("file      :", a.db)
    try:
        st = os.stat(a.db)
        print("size      : %.1f MB" % (st.st_size / 1048576))
        print("last write:", datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"))
    except OSError as e:
        print("cannot stat the file:", e)
    for ext in ("-wal", "-journal"):
        if os.path.exists(a.db + ext):
            print("note      : %s exists (%d bytes) — writes may still be in it"
                  % (os.path.basename(a.db + ext), os.path.getsize(a.db + ext)))
    print("plant     :", a.plant, " date:", d)
    print("=" * 78)

    c = open_ro(a.db)
    c.row_factory = sqlite3.Row
    q = lambda s, p=(): c.execute(s, p).fetchall()

    f = q("SELECT id, code, name FROM factories WHERE UPPER(code)=?", (a.plant.upper(),))
    if not f:
        print("!! no plant with code", a.plant, "— plants are:",
              ", ".join(r["code"] for r in q("SELECT code FROM factories")))
        return
    fac = f[0]["id"]
    print("factory_id:", fac, "-", f[0]["name"])

    kind = q("SELECT type FROM sqlite_master WHERE name='jobs'")
    print("jobs is a:", kind[0]["type"] if kind else "MISSING")
    print("machines in this plant:", q("SELECT COUNT(*) n FROM machines WHERE factory_id=?", (fac,))[0]["n"],
          "· active:", q("SELECT COUNT(*) n FROM machines WHERE factory_id=? AND active=1", (fac,))[0]["n"])
    print("jobs in the database  :", q("SELECT COUNT(*) n FROM jobs")[0]["n"],
          "· highest id:", q("SELECT COALESCE(MAX(id),0) n FROM jobs")[0]["n"])

    W = ("(SELECT COALESCE(m.factory_id, j.factory_id) FROM machines m WHERE m.id=j.machine_id)=?"
         " OR (j.machine_id IS NULL AND j.factory_id=?)")

    print("\n-- JOBS CREATED ON", d, "IN THIS PLANT " + "-" * 34)
    rows = q(f"""SELECT j.id, j.jobid, j.jobtype, j.jobsource, j.status, j.planned_date,
                        j.created_at, j.lead_tech, m.code mcode
                   FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
                  WHERE SUBSTR(j.created_at,1,10)=? AND ({W})
                  ORDER BY j.id""", (d, fac, fac))
    print("count:", len(rows))
    for r in rows[:60]:
        print("   #%-5s %-16s %-4s %-8s %-16s planned=%-10s lead=%-5s %s"
              % (r["id"], r["jobid"], r["jobtype"], r["jobsource"] or "", r["status"],
                 r["planned_date"] or "-", r["lead_tech"] or "-", r["mcode"] or ""))
    if len(rows) > 60:
        print("   … and", len(rows) - 60, "more")
    pm_new = [r for r in rows if (r["jobtype"] or "") == "PM"]
    print("PM work orders created today:", len(pm_new),
          "· of those planned for", d, ":", sum(1 for r in pm_new if (r["planned_date"] or "") == d))

    print("\n-- WORK PLANNED FOR", d, "IN THIS PLANT " + "-" * 36)
    for r in q(f"""SELECT j.jobtype, j.status, COUNT(*) n,
                          SUM(CASE WHEN j.lead_tech IS NULL THEN 1 ELSE 0 END) nolead
                     FROM jobs j WHERE j.planned_date=? AND ({W})
                    GROUP BY j.jobtype, j.status ORDER BY j.jobtype, j.status""", (d, fac, fac)):
        print("   %-4s %-18s %4d  (no crew: %d)" % (r["jobtype"], r["status"], r["n"], r["nolead"]))

    print("\n-- CREWS SAVED FOR", d, "(plan_teams) " + "-" * 38)
    try:
        ts = q("SELECT * FROM plan_teams WHERE factory_id=? AND day=? ORDER BY seq", (fac, d))
        print("teams:", len(ts))
        for t in ts:
            print("   seq %-2s %-18s lead=%-5s members=%-20s phone=%s"
                  % (t["seq"], (t["name"] or "")[:18], t["lead"] or "-",
                     t["members"] or "-", t["login_id"] or "-"))
    except sqlite3.Error as e:
        print("   cannot read plan_teams:", e)

    print("\n-- STATUS CHANGES WRITTEN ON", d, "(newest first) " + "-" * 24)
    ev = q(f"""SELECT e.created_at, e.status, e.job_id, e.user_id, j.jobid, j.jobtype
                 FROM job_events e JOIN jobs j ON j.id=e.job_id
                WHERE SUBSTR(e.created_at,1,10)=? AND ({W})
                ORDER BY e.id DESC LIMIT 40""", (d, fac, fac))
    for r in ev:
        print("   %s  %-16s %-16s by user %s" % (r["created_at"][11:19], r["jobid"] or r["job_id"],
                                                 r["status"], r["user_id"] if r["user_id"] is not None else "-"))
    if not ev:
        print("   none")
    back = [r for r in ev if r["status"] in ("Reported", "WaitingAssignment")]
    if back:
        print("   ⚠ %d job(s) were put BACK to Reported today — something un-assigned them" % len(back))

    print("\n-- PM PROGRAMME " + "-" * 60)
    try:
        for r in q("SELECT * FROM pm_config WHERE factory_id=?", (fac,)):
            print("   start_date:", r["start_date"], " running:", "yes" if r["active"] else "NO")
        print("   checklists:", q("SELECT COUNT(*) n FROM pm_templates WHERE factory_id=?", (fac,))[0]["n"],
              "· machines pinned to one:", q("SELECT COUNT(*) n FROM pm_members WHERE factory_id=?", (fac,))[0]["n"])
    except sqlite3.Error as e:
        print("   cannot read the PM tables:", e)

    print("\n-- PEOPLE THIS PLANT CAN PUT ON A CREW " + "-" * 38)
    ppl = q("""SELECT id, name, COALESCE(can_login,1) can_login FROM users
                WHERE active=1 AND role='technician' AND factory_id=? ORDER BY can_login, name""", (fac,))
    print("   technician people (can be a crew member):",
          ", ".join(r["name"] for r in ppl if not r["can_login"]) or "NONE")
    print("   technician logins (the phones)          :",
          ", ".join(r["name"] for r in ppl if r["can_login"]) or "NONE")
    print("\nDone. Nothing was changed.")


if __name__ == "__main__":
    main()

"""Why can this phone not start a job? Answers it for one login, job by job.

Start is refused for exactly three reasons, and the phone does not always show which:

  1. THE CREW GATE. Work is assigned to PEOPLE (Mark, Choke); a shared handset is an
     ACCOUNT (tech1). The only thing linking the two is `users.login_id` on the person
     pointing at the account. Break that link and the account is no longer part of the
     crew of any job, so every assigned job answers 403 — "assigned to Mark, Choke;
     nobody else can start it" — and the phone can start nothing at all.

  2. ONE TIMER PER PHONE. A timer left open on another job refuses every new Start
     with 409. The app closes leftovers on jobs that are finished or cancelled, but a
     timer on a job that is merely sitting in Hold or Assigned is not a leftover by
     that rule, and it blocks the whole handset until somebody closes it.

  3. THE JOB IS FINISHED. Done, Rejected, Cancelled or waiting for the operator to
     accept it — a stale screen can still show a Start button for it.

Run it where the database is:

    python check_handset.py                    # tech1, data\\cmms.db
    python check_handset.py --user techfp1
    python check_handset.py --db "D:\\bflfp-cmms\\data\\cmms.db" --user tech1

It only reads. Nothing is changed; the fixes are printed for a person to decide on.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

OPEN_STATUS = ("Reported", "WaitingApproval", "WaitingAssignment", "Assigned",
               "Released", "Rework", "Hold", "Paused", "InProgress")
CLOSED = ("Done", "Rejected", "Cancelled", "ServiceCompleted")


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(here / "data" / "cmms.db"))
    ap.add_argument("--user", default="tech1", help="the login to test, by username")
    a = ap.parse_args()

    if not Path(a.db).exists():
        sys.exit(f"no database at {a.db}")
    c = sqlite3.connect(a.db)
    c.row_factory = sqlite3.Row

    u = c.execute("SELECT * FROM users WHERE username=?", (a.user,)).fetchone()
    if not u:
        sys.exit(f"no user called {a.user}")
    fac = u["factory_id"]
    fname = (c.execute("SELECT code,name FROM factories WHERE id=?", (fac,)).fetchone()
             or ["?", "?"])
    print(f"\n{a.db}")
    print(f"LOGIN  {u['username']}  (id {u['id']}, {u['name']}, role {u['role']}, "
          f"plant {fname[0]} {fname[1]})")
    if u["role"] != "technician":
        print("  ⚠ only a technician may press Start — this account is a "
              f"{u['role']}, so every Start is refused with 403.")
    if not (u["can_login"] if u["can_login"] is not None else 1):
        print("  ⚠ can_login = 0 — this is a PERSON record, not a phone. It cannot sign in.")

    # ── 1 · who this phone stands for ────────────────────────────────────────────
    crew = [dict(r) for r in c.execute(
        "SELECT id,name,role,active FROM users WHERE login_id=? ORDER BY name", (u["id"],))]
    ids = [u["id"]] + [r["id"] for r in crew]
    print(f"\n1 · THE PHONE STANDS FOR  {', '.join(str(i) for i in ids)}")
    print(f"    the account itself: {u['id']} {u['name']}")
    if crew:
        for r in crew:
            print(f"    linked person:      {r['id']} {r['name']}"
                  f"{'' if r['active'] else '  (INACTIVE)'}")
    else:
        print("    linked people:      NONE — users.login_id points at this account "
              "from nobody.")
        print("    → every job assigned to a person will be refused. This is the usual "
              "cause of\n      \"tech1 cannot start any job\". Fix: Users → the person → "
              "set their phone to this account.")

    # people in this plant that no phone can reach at all
    orphan = [dict(r) for r in c.execute(
        "SELECT id,name FROM users WHERE factory_id=? AND role='technician'"
        " AND COALESCE(can_login,1)=0 AND login_id IS NULL ORDER BY name", (fac,))]
    if orphan:
        print("\n    people in this plant on NO phone at all "
              "(work assigned to them is invisible everywhere):")
        for r in orphan:
            print(f"      {r['id']} {r['name']}")

    # ── 2 · a timer left running blocks every Start ──────────────────────────────
    qs = ",".join("?" * len(ids))
    open_seg = [dict(r) for r in c.execute(
        f"""SELECT t.id, t.job_id, t.tech, t.start, j.jobid, j.status,
                   (SELECT name FROM users WHERE id=t.tech) who
            FROM timelogs t LEFT JOIN jobs j ON j.id=t.job_id
            WHERE t.tech IN ({qs}) AND t.end IS NULL ORDER BY t.id""", ids)]
    print(f"\n2 · TIMERS STILL RUNNING FOR THIS PHONE: {len(open_seg)}")
    for r in open_seg:
        stale = r["status"] in CLOSED
        print(f"    {r['jobid']} · {r['status']} · started {r['start']} by {r['who']}"
              + ("   ← on a CLOSED job; the app clears this one by itself" if stale else
                 "   ← BLOCKS every other Start on this phone"))
    if len(open_seg) > 1 or (open_seg and open_seg[0]["status"] not in CLOSED):
        print("    → press Stop or Hold on that job from this phone, or have the planner"
              " force-stop it.")
    if not open_seg:
        print("    none — nothing is blocking on this account.")

    # ── 3 · job by job, the answer the server would give ─────────────────────────
    jobs = [dict(r) for r in c.execute(
        f"""SELECT j.id,j.jobid,j.jobtype,j.status,j.lead_tech,j.helpers,j.planned_date,
                   m.code mcode
            FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
            WHERE COALESCE(m.factory_id,j.factory_id)=?
              AND j.status IN ({','.join('?' * len(OPEN_STATUS))})
            ORDER BY j.planned_date DESC, j.id DESC LIMIT 40""", [fac, *OPEN_STATUS])]
    names = {r["id"]: r["name"] for r in c.execute("SELECT id,name FROM users")}
    mine = set(ids)
    can = blocked = 0
    print(f"\n3 · CAN THIS PHONE START THESE JOBS?  (open jobs in this plant, newest 40)")
    for j in jobs:
        team = [j["lead_tech"]] + [int(x) for x in str(j["helpers"] or "").split(",")
                                   if str(x).strip().isdigit()]
        team = [t for t in dict.fromkeys(team) if t]
        if not team:
            why = "OK — nobody is on it, so anyone in the plant may take it"
        elif mine & set(team):
            why = "OK — this phone is on the crew"
        else:
            who = ", ".join(names.get(t, str(t)) for t in team)
            why = f"REFUSED 403 — assigned to {who}"
        if open_seg and any(s["status"] not in CLOSED for s in open_seg):
            first = next(s for s in open_seg if s["status"] not in CLOSED)
            why = f"REFUSED 409 — {first['jobid']} is still running on this phone"
        if why.startswith("OK"):
            can += 1
        else:
            blocked += 1
        print(f"    {j['jobid']:16s} {j['jobtype']:3s} {j['status']:16s} "
              f"{(j['mcode'] or '—'):9s} {why}")
    print(f"\n    startable {can} · refused {blocked}")
    print("\nNothing was changed by this script.\n")
    c.close()


if __name__ == "__main__":
    main()

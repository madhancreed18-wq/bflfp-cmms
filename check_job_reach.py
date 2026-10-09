"""check_job_reach.py - why is this job blinking on the planner screen but not on any phone?
READ-ONLY. Nothing is written; the database is opened read-only.

    .venv\\Scripts\\python.exe check_job_reach.py PRM-2609-451..463
    .venv\\Scripts\\python.exe check_job_reach.py 451-463            (same, numbers only)
    .venv\\Scripts\\python.exe check_job_reach.py PRM-2609-455       (one job)

For every job it prints what the planner's screen sees (status, planned day, due day,
who it is booked to) and then answers the only question that matters: WHICH PHONE
lists it, using exactly the rule the phone uses -

    "My jobs" on a phone = every job where that login, or a technician whose record
    points at that login, is the lead or a helper, AND the status is one of
    Assigned / InProgress / Paused / Rework / Hold.
    Plus anything nobody can reach: no technician on it at all, or booked to a
    technician record that points at no login - those show on EVERY phone of the plant.

So a job reaches no phone when its status is outside that list (Reported, Waiting…,
ServiceCompleted, Done), or when it is booked to a technician of ANOTHER plant, or when
the technician's record points at a login that is not an active technician login of the
plant. The line under each job says which of these it is.
"""
import datetime
import os
import re
import sqlite3
import sys

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cmms.db")
if not os.path.exists(DB):
    DB = r"D:\bflfp-cmms\data\cmms.db"
PHONE_ST = ("Assigned", "InProgress", "Paused", "Rework", "Hold")     # the phone's "My jobs"
WAIT_ST = ("ServiceCompleted",)                                       # the phone's "Approve pending"

arg = " ".join(sys.argv[1:]).strip()
if not arg:
    sys.exit("give a job or a range, e.g.  check_job_reach.py PRM-2609-451..463")

c = sqlite3.connect("file:" + DB.replace("\\", "/") + "?mode=ro", uri=True)
c.row_factory = sqlite3.Row
q = lambda s, a=(): c.execute(s, a).fetchall()
TODAY = datetime.date.today().isoformat()

# --- work out which job numbers were asked for ------------------------------------
def _split(a):
    """'PRM-2609-451..463' / '451-463' / 'PRM-2609-455' -> the list of job numbers."""
    a = a.replace(" to ", "..").replace("..", "\x00").replace("—", "\x00")
    if "\x00" not in a and re.match(r"^\s*\d+\s*-\s*\d+\s*$", a):
        a = a.replace("-", "\x00")
    if "\x00" not in a:
        return [a.strip()], None
    lo, hi = [x.strip() for x in a.split("\x00", 1)]
    ml, mh = re.search(r"(\d+)\s*$", lo), re.search(r"(\d+)\s*$", hi)
    if not ml or not mh:
        sys.exit("could not read that range - try  PRM-2609-451..463")
    pre, w = lo[:ml.start(1)], len(ml.group(1))
    nums = range(int(ml.group(1)), int(mh.group(1)) + 1)
    if pre:                                   # full job numbers
        return [f"{pre}{str(n).zfill(w)}" for n in nums], None
    return None, [f"%-{str(n).zfill(max(w, 3))}" for n in nums]   # numbers only: match the tail


want, like = _split(arg)

W = "COALESCE((SELECT m.factory_id FROM machines m WHERE m.id=j.machine_id), j.factory_id)"
BASE = f"""SELECT j.id, j.jobid, j.jobtype, j.status, j.planned_date, j.due_date,
                  j.lead_tech, COALESCE(j.helpers,'') helpers, j.started_at, j.done_at,
                  j.due_ack, m.code mcode, m.name mname, {W} fac,
                  (SELECT code FROM factories WHERE id={W}) plant
             FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id WHERE """
if want:
    rows = q(BASE + "j.jobid IN (%s) ORDER BY j.jobid" % ",".join("?" * len(want)), want)
    missing = sorted(set(want) - {r["jobid"] for r in rows})
else:
    rows = q(BASE + "(" + " OR ".join(["j.jobid LIKE ?"] * len(like)) + ") ORDER BY j.jobid", like)
    missing = []

print("database:", DB, "  today:", TODAY)
if missing:
    print("not found in the database:", ", ".join(missing))
if not rows:
    sys.exit("\nNo job matched. Check the job number and try again.")

users = {r["id"]: r for r in q("SELECT id,name,username,role,active,factory_id,"
                               "COALESCE(can_login,1) cl, login_id FROM users")}


def who(uid):
    u = users.get(uid)
    return f"{u['name']} (#{uid})" if u else f"#{uid} - NO SUCH USER" if uid else "- nobody -"


def phone_of(uid, fac):
    """The phone that lists work booked to this technician, or why there is none."""
    u = users.get(uid)
    if not u:
        return None, "the job is booked to a user id that does not exist"
    if u["cl"]:
        return u, None                                  # a login account itself
    lg = users.get(u["login_id"])
    if not lg:
        return None, f"{u['name']} is not linked to any phone login"
    if not lg["active"] or lg["role"] != "technician":
        return None, f"{u['name']} points at {lg['username']}, which is not an active technician login"
    if lg["factory_id"] != fac:
        return None, f"{u['name']} points at {lg['username']}, a login of another plant"
    return lg, None


for r in rows:
    ids = [r["lead_tech"]] + [int(x) for x in r["helpers"].split(",") if x.strip().isdigit()]
    ids = [i for i in ids if i]
    blink = ""
    d = (r["due_date"] or "")[:10]
    pd = (r["planned_date"] or "")[:10]
    if r["status"] not in ("Done", "Rejected", "Cancelled", "ServiceCompleted"):
        if (r["due_ack"] or "")[:10] == TODAY:
            blink = "acknowledged today - quiet until tomorrow"
        elif d and d < TODAY:
            blink = f"BLINKS RED - due {d}, past"
        elif d == TODAY:
            blink = "BLINKS YELLOW - due today"
        elif pd and pd < TODAY:
            blink = f"amber row - planned {pd}, still not done"
    print("\n" + "=" * 78)
    print(f"{r['jobid']}   {r['plant'] or '?'}   {r['jobtype']}   {r['mcode'] or ''} {r['mname'] or ''}")
    print(f"  status {r['status']:16} planned {pd or '-':10} due {d or '-':10} "
          f"started {(r['started_at'] or '-')[:16]}")
    if blink:
        print("  planner screen:", blink)
    print("  booked to     :", ", ".join(who(i) for i in ids) or "- nobody -")

    # --- the phone rule, applied ---------------------------------------------------
    if r["status"] in PHONE_ST:
        listname = '"My jobs"'
    elif r["status"] in WAIT_ST:
        listname = '"Approve pending"'
    else:
        listname = None
    if not listname:
        print(f"  ON A PHONE    : NO - a job at status {r['status']} is on no phone list.")
        print("                  The phone lists Assigned / InProgress / Paused / Rework / Hold,")
        print("                  and finished work under Approve pending. This one is none of those.")
        continue
    if not ids:
        print(f"  ON A PHONE    : YES - nobody is booked on it, so it shows in {listname} on EVERY")
        print("                  phone of the plant until somebody takes it.")
        continue
    seen, why = [], []
    for i in ids:
        lg, err = phone_of(i, r["fac"])
        if lg:
            seen.append(f"{lg['username']} ({lg['name']})")
        else:
            why.append(err)
    if seen:
        print(f"  ON A PHONE    : YES - {listname} on {', '.join(sorted(set(seen)))}")
        for w in why:
            print("                  note:", w)
    else:
        print("  ON A PHONE    : NO - reaches nobody:")
        for w in why:
            print("                 ", w)
        u = users.get(r["lead_tech"])
        if u and not u["cl"] and not u["login_id"]:
            print("                  (a technician with NO login at all: the job falls into the")
            print("                   list every phone of the plant shows - check the plant is right)")
        if u and u["factory_id"] != r["fac"]:
            print(f"                  the job is in plant {r['plant']} but {u['name']} belongs to another plant")

print("\nDone. Nothing was changed.")

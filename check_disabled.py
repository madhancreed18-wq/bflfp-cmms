"""Disabled accounts — are they still used?  (read-only)   python check_disabled.py

For every account switched off (users.active = 0) it shows:
  login?      can this record sign in at all (can_login), or is it only a name for crews
  works via   the account it is attached to (login_id) — the person may now work under that one
  open jobs   unfinished jobs where it is the lead or a helper
  last work   last Start/Stop time booked to it (timelogs)
  last action last job action done by it (job_events)
  last seen   last API call by it (perf_log, kept 90 days, slow calls / errors only)
"""
import os, sqlite3, sys

path = sys.argv[1] if len(sys.argv) > 1 else os.path.join("data", "cmms.db")
c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
fac = {r["id"]: r["code"] for r in c.execute("SELECT id, code FROM factories")}
name = {r["id"]: (r["username"], r["name"], r["active"]) for r in c.execute("SELECT id, username, name, active FROM users")}
tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}


def one(sql, args):
    try:
        r = c.execute(sql, args).fetchone()
        return (r[0] if r else None) or "-"
    except Exception:
        return "-"


rows = c.execute("SELECT id, username, name, role, factory_id, COALESCE(can_login,1) can_login, login_id"
                 " FROM users WHERE COALESCE(active,1)=0 ORDER BY role, name").fetchall()
print(f"{len(rows)} disabled account(s) in {path}\n")
for u in rows:
    uid = u["id"]
    via = name.get(u["login_id"]) if u["login_id"] else None
    via_s = f"{via[0]} ({via[1]}){'' if via[2] else ' — ALSO DISABLED'}" if via else "-"
    open_jobs = one("SELECT COUNT(*) FROM jobs WHERE (lead_tech=? OR (','||COALESCE(helpers,'')||',') LIKE ?)"
                    " AND status NOT IN ('Done','Cancelled','Rejected','ServiceCompleted')", (uid, f"%,{uid},%"))
    last_work = one("SELECT MAX(start) FROM timelogs WHERE tech=?", (uid,))
    last_act = one("SELECT MAX(created_at) FROM job_events WHERE user_id=?", (uid,))
    last_seen = one("SELECT MAX(at) FROM perf_log WHERE uid=?", (uid,)) if "perf_log" in tables else "-"
    print(f"- {u['username']} · {u['name']} · {u['role']} · plant {fac.get(u['factory_id'], 'ALL')}"
          f" · login {'yes' if u['can_login'] else 'no (crew name only)'}")
    print(f"    works via: {via_s}")
    print(f"    open jobs: {open_jobs} · last work: {last_work} · last action: {last_act} · last seen: {last_seen}")

# -*- coding: utf-8 -*-
"""merge_users.py - make two records of ONE person into one name.

Typical case: a technician record with no login ("Kanya") and the phone account
the same person signs in with ("kanya" / tech2). Everything on the FROM record moves
to the INTO account, the FROM record is removed, and INTO can be renamed.

Same hand-over as Manage -> Users -> Delete -> "Move their work to", plus the
Owner 1 / Owner 2 columns (same person, so they follow).

Nothing is written until --apply, and --apply copies the database first.
STOP THE APP before --apply.

    python merge_users.py --db data\\cmms.db --from 124 --into 4 --name Kanya
    python merge_users.py --db data\\cmms.db --from 124 --into 4 --name Kanya --apply
"""
import argparse, datetime, os, shutil, sqlite3, sys

ap = argparse.ArgumentParser()
ap.add_argument("--db", default=os.path.join("data", "cmms.db"))
ap.add_argument("--from", dest="src", type=int, required=True, help="user id that disappears")
ap.add_argument("--into", dest="dst", type=int, required=True, help="user id that stays (the login)")
ap.add_argument("--name", default="", help="final display name for the one that stays")
ap.add_argument("--by", type=int, default=143, help="admin id written in the job history")
ap.add_argument("--apply", action="store_true")
a = ap.parse_args()
if a.src == a.dst:
    sys.exit("--from and --into must differ")
if not os.path.exists(a.db):
    sys.exit("no database at " + a.db)

c = sqlite3.connect(a.db)
c.row_factory = sqlite3.Row
U = lambda i: c.execute("SELECT id,name,username,role,COALESCE(can_login,1) can_login,"
                        "login_id,factory_id,active FROM users WHERE id=?", (i,)).fetchone()
s, d = U(a.src), U(a.dst)
if not s or not d:
    sys.exit("user not found: from=%s into=%s" % (bool(s), bool(d)))
print("database :", a.db)
print("FROM     : id %d  %-12s login=%s  (%s)" % (s["id"], s["name"], s["username"], "has login" if s["can_login"] else "no login"))
print("INTO     : id %d  %-12s login=%s  (%s)" % (d["id"], d["name"], d["username"], "has login" if d["can_login"] else "no login"))
if not d["can_login"]:
    print("!! INTO cannot sign in - usually you want INTO to be the login account")
if a.name:
    print("rename   : %s -> %s" % (d["name"], a.name))

SIMPLE = [("jobs", "lead_tech"), ("jobs", "created_by"), ("jobs", "requester_id"),
          ("jobs", "approver_id"), ("jobs", "owner1"), ("jobs", "owner2"),
          ("timelogs", "tech"), ("signoffs", "user_id"), ("job_events", "user_id"),
          ("activities", "assignee")]

def has(t, col):
    return any(r[1] == col for r in c.execute("PRAGMA table_info('%s')" % t))

def swap(val, old, new):
    out, seen = [], set()
    for x in [x.strip() for x in str(val or "").split(",") if x.strip()]:
        y = str(new) if x == str(old) else x
        if y not in seen:
            seen.add(y); out.append(y)
    return ",".join(out)

print("\nwhat moves:")
touched = set()
for t, col in SIMPLE:
    if not has(t, col):
        continue
    n = c.execute("SELECT COUNT(*) FROM %s WHERE %s=?" % (t, col), (a.src,)).fetchone()[0]
    if t == "jobs":
        touched |= {r[0] for r in c.execute("SELECT id FROM jobs WHERE %s=?" % col, (a.src,))}
    if n:
        print("  %-10s %-13s %4d" % (t, col, n))
helpers = [(r["id"], r["helpers"]) for r in c.execute(
    "SELECT id,helpers FROM jobs WHERE ','||REPLACE(COALESCE(helpers,''),' ','')||',' LIKE ?", ("%%,%d,%%" % a.src,))]
teams = [(r["id"], r["members"], r["lead"]) for r in c.execute(
    "SELECT id,members,lead FROM plan_teams WHERE lead=? OR ','||REPLACE(COALESCE(members,''),' ','')||',' LIKE ?",
    (a.src, "%%,%d,%%" % a.src))]
logins = c.execute("SELECT COUNT(*) FROM users WHERE login_id=?", (a.src,)).fetchone()[0]
touched |= {h[0] for h in helpers}
print("  %-10s %-13s %4d" % ("jobs", "helpers", len(helpers)))
print("  %-10s %-13s %4d" % ("plan_teams", "lead/members", len(teams)))
st = c.execute("SELECT status,COUNT(*) n FROM jobs WHERE id IN (%s) GROUP BY status" %
               (",".join(map(str, touched)) or "0")).fetchall()
print("  jobs by status:", ", ".join("%s %d" % (r["status"], r["n"]) for r in st))

if not a.apply:
    print("\nDRY RUN - nothing written. Stop the app, then add --apply.")
    sys.exit(0)

bak = a.db + ".before-merge-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
c.close(); shutil.copy2(a.db, bak); print("\nbackup   :", bak)
c = sqlite3.connect(a.db); c.row_factory = sqlite3.Row
now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
final = a.name or d["name"]
try:
    c.execute("BEGIN")
    for t, col in SIMPLE:
        if has(t, col):
            c.execute("UPDATE %s SET %s=? WHERE %s=?" % (t, col, col), (a.dst, a.src))
    for col in ("owner1", "owner2"):
        c.execute("UPDATE jobs SET %s_name=? WHERE %s=?" % (col, col), (final, a.dst))
    for jid, h in helpers:
        c.execute("UPDATE jobs SET helpers=? WHERE id=?", (swap(h, a.src, a.dst), jid))
    for tid, m, lead in teams:
        c.execute("UPDATE plan_teams SET members=?, lead=? WHERE id=?",
                  (swap(m, a.src, a.dst), a.dst if lead == a.src else lead, tid))
    c.execute("UPDATE users SET login_id=NULL WHERE login_id=?", (a.src,))
    who = s["name"] or s["username"]
    for jid in sorted(touched):
        c.execute("INSERT INTO messages(job_id,author,text,kind,created_at) VALUES(?,?,?,'system',?)",
                  (jid, a.by, "⇄ รวมชื่อ %s → %s (%s) / merged %s into %s (%s)"
                   % (who, final, d["username"], who, final, d["username"]), now))
    try:
        c.execute("DELETE FROM push_subs WHERE user_id=?", (a.src,))
    except sqlite3.Error:
        pass
    c.execute("DELETE FROM users WHERE id=?", (a.src,))
    if a.name:
        c.execute("UPDATE users SET name=? WHERE id=?", (a.name, a.dst))
    c.execute("COMMIT")
except Exception as e:
    c.execute("ROLLBACK")
    sys.exit("FAILED, nothing changed: %s" % e)
left = c.execute("SELECT COUNT(*) FROM jobs WHERE lead_tech=? OR owner1=? OR owner2=?",
                 (a.src,) * 3).fetchone()[0]
now_n = c.execute("SELECT COUNT(*) FROM jobs WHERE lead_tech=?", (a.dst,)).fetchone()[0]
print("done     : %d jobs touched, %s now leads %d jobs, %d left on old id" % (len(touched), final, now_n, left))

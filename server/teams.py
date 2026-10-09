"""Day team assignment.

The planner double-clicks a day on the Plan calendar and builds *teams* for that
day: a lead technician plus helpers, an optional time window, and the jobs that
team will do. A team is therefore just the (lead_tech, helpers) pair the jobs
table already stores — saving writes straight onto the jobs, exactly like
`/api/jobs/plan` does.

`plan_teams` exists only so the crews survive to the next day: opening a day
with no saved teams carries forward the most recent day's crews (members and
times, no jobs), so the planner adjusts instead of rebuilding.
"""
from contextlib import closing
from datetime import date, datetime

from fastapi import APIRouter, Request, HTTPException

from .db import (db, now, today, log_status, user_names, next_jobid, set_stage,
                 own_account_map)
from .auth import require_role
from .push import notify_users
from . import pm

# A job with no day at all belongs on the assign board whatever else is true of it:
# giving it to a crew is what dates it. The list used to be Reported and
# WaitingAssignment only, so a dateless job that had been assigned, put on hold or sent
# back for rework appeared NOWHERE — not in the day's work, not in the chips, not in
# the tray — and nothing on any screen said it existed. InProgress and ServiceCompleted
# stay out on purpose: somebody already has those in hand.
UNDATED_ST = ("('Reported','WaitingApproval','WaitingAssignment',"
              "'Assigned','Paused','Hold','Rework')")


router = APIRouter(prefix="/api/teams")

# statuses a job can still be (re)assigned from — work already finished is left alone
ASSIGNABLE = ("Reported", "WaitingAssignment", "Assigned", "Rework", "Hold")
COLORS = ["#2563EB", "#EA6A0A", "#16A34A", "#7C3AED", "#0891B2", "#DB2777", "#CA8A04", "#475569"]


def _ensure(c):
    c.execute("""CREATE TABLE IF NOT EXISTS plan_teams(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER, day TEXT,
        name TEXT DEFAULT '', color TEXT DEFAULT '', lead INTEGER,
        members TEXT DEFAULT '', start_time TEXT DEFAULT '', end_time TEXT DEFAULT '',
        seq INTEGER DEFAULT 0, login_id INTEGER)""")
    # the company phone each crew carries: Team A → techfp1, Team B → techfp2 …
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(plan_teams)")}
        if "login_id" not in cols:
            c.execute("ALTER TABLE plan_teams ADD COLUMN login_id INTEGER")
    except Exception:
        pass
    # Which days a planner has actually SAVED. Crews now carry forward from the last
    # planned day until the planner removes them, which means "this day has no crew
    # rows" can no longer be read as "this day was never planned": a planner who deleted
    # every crew and saved must get an empty board back, not yesterday's crews again.
    # A row here is that distinction, and nothing else.
    c.execute("""CREATE TABLE IF NOT EXISTS plan_days(
        factory_id INTEGER, day TEXT, PRIMARY KEY(factory_id, day))""")
    # Who was LENT to a job from another crew, as the planner ticked it. Until b358 this
    # was worked out on every load from the job's helpers ("a helper not in the crew
    # holding the job was lent to it"), which cannot tell a real lend from a helper left
    # over from yesterday's crew: Choke, moved from Team A to Team B, showed as lent to
    # all twenty of Team A's jobs and the next save wrote him back onto every one.
    # Recorded from b358 on; `lends` on plan_days marks a day saved that way. Days saved
    # before keep being read the old way, so nothing already issued changes.
    c.execute("""CREATE TABLE IF NOT EXISTS plan_lends(
        factory_id INTEGER, day TEXT, job_id INTEGER, tech INTEGER,
        PRIMARY KEY(factory_id, day, job_id, tech))""")
    try:
        if "lends" not in {r[1] for r in c.execute("PRAGMA table_info(plan_days)")}:
            c.execute("ALTER TABLE plan_days ADD COLUMN lends INTEGER DEFAULT 0")
    except Exception:
        pass


def _carry_source(c, fac, d):
    """The last day before `d` that a planner planned — by saving it, or by the crews
    saved on it before plan_days existed. None when nothing earlier was ever planned."""
    r = c.execute("""SELECT MAX(day) d FROM (
                        SELECT day FROM plan_days  WHERE factory_id=? AND day<?
                        UNION
                        SELECT day FROM plan_teams WHERE factory_id=? AND day<?)""",
                  (fac, d, fac, d)).fetchone()
    return r["d"] if r and r["d"] else None


def _fac(u):
    return u.get("active_factory") or u.get("factory_id")


def _handset_people(c, fac):
    """({account: the one person on it}, {accounts nobody is on}) for one plant.

    A technician login with exactly one person attached is that person's own phone,
    so work booked to the account is his. A login with nobody attached is a
    technician who signs in as themselves and has no separate person record — they
    are a crew member in their own right. A login shared by two or more people is a
    crew handset and stays what it was."""
    cnt, one = {}, {}
    for r in c.execute("SELECT login_id, id FROM users WHERE active=1 AND role='technician'"
                       " AND COALESCE(can_login,1)=0 AND login_id IS NOT NULL AND (factory_id=? OR COALESCE(factory_id,0)=0)", (fac,)):
        cnt[r["login_id"]] = cnt.get(r["login_id"], 0) + 1
        one[r["login_id"]] = r["id"]
    one = {a: p for a, p in one.items() if cnt[a] == 1}
    alone = {r["id"] for r in c.execute(
        "SELECT id FROM users WHERE active=1 AND role='technician' AND COALESCE(can_login,1)=1"
        " AND (factory_id=? OR COALESCE(factory_id,0)=0)", (fac,)) if r["id"] not in cnt}
    return one, alone


def _ids(csv):
    return [int(x) for x in str(csv or "").split(",") if str(x).strip().isdigit()]


def _pm_due(c, fac, d, ce=False):
    """The PM occurrences the calendar draws for this day that have no work order yet.
    They are offered in the popup as 'PM due' cards; assigning one creates the job."""
    try:
        dd = date.fromisoformat(d)
        occ = pm._occurrences(c, fac, dd, dd)
    except Exception:
        return []
    # A PM work order that exists for this occurrence — on this day, or MOVED from it
    # (its due_date keeps the day it was created for). Checking the planned day alone is
    # how one machine ended up with three PM work orders on one day: once the first had
    # been re-dated away, this found nothing and the board offered the PM again.
    have = {r["machine_id"] for r in c.execute(
        "SELECT machine_id FROM jobs WHERE jobtype='PM' AND (planned_date=? OR due_date=?)"
        " AND machine_id IS NOT NULL AND status NOT IN ('Cancelled','Rejected')", (d, d))}
    # Where each machine stands, so a PM that is merely DUE groups beside the work
    # orders on the same floor instead of sitting apart from them.
    place = {r["id"]: r for r in c.execute(
        "SELECT id,floor,line,department,asset_group,pm_group_color FROM machines WHERE factory_id=?", (fac,))}
    # b413: how many of each sheet's points are electrical. A PM whose points are ALL
    # electrical is Central Electrical's work from start to finish — the plant's board
    # does not offer it; the CE planner's Electrical PM page does.
    try:
        from .elec import counts as _elc
        _cnt, _m2t = _elc(c, fac), pm._match_all(c, fac)
    except Exception:
        _cnt, _m2t = {}, {}
    out = []
    for o in occ:
        if o.get("cm") or o["machine_id"] in have:
            continue
        _na, _ne = _cnt.get((_m2t.get(o["machine_id"]), o["freq"])) or (0, 0)
        if ce:
            if not _ne:
                continue                         # CE board: electrical PM only
        elif _na and _na == _ne:
            continue
        p = place.get(o["machine_id"])
        out.append({"uid": f"pm:{o['machine_id']}:{o['freq']}", "machine_id": o["machine_id"],
                    "jobtype": "PM", "pmdue": True, "status": "Planned", "priority": 1,
                    "freq": o["freq"], "freq_label": pm.FREQ_LABEL.get(o["freq"], o["freq"]),
                    "mcode": o["code"], "mname": o["name"],
                    "mfloor": (p["floor"] if p else "") or "",
                    "mline": (p["line"] if p else "") or o.get("line") or "",
                    "mdept": (p["department"] if p else "") or "",
                    "mgroup": (p["asset_group"] if p else "") or "",
                    "mcolor": (p["pm_group_color"] if p else "") or "", "el": _ne})
    return out


def _make_pm_job(c, fac, mid, freq, uid, d, m2t, tname):
    """Turn one PM occurrence into a real work order, dated for this day."""
    ex = c.execute("SELECT id FROM jobs WHERE jobtype='PM' AND machine_id=? AND (planned_date=? OR due_date=?)"
                   " AND status NOT IN ('Cancelled','Rejected') ORDER BY (planned_date=?) DESC, id",
                   (mid, d, d, d)).fetchone()
    if ex:
        return ex["id"]
    tid = m2t.get(mid)
    if not tid:
        return None
    lines = [f"PM · {pm.FREQ_LABEL.get(freq, freq)} · {tname.get(tid, '')}"]
    for it in c.execute("SELECT seq,item,normal_status,method FROM pm_items"
                        " WHERE template_id=? AND freq=? AND COALESCE(active,1)=1 ORDER BY seq", (tid, freq)):
        lines.append(f"{it['seq']}. {it['item']}  [{it['method'] or 'เช็ค'}]"
                     + (f" — ปกติ: {it['normal_status']}" if it["normal_status"] else ""))
    jid = c.insert_id("""INSERT INTO jobs(jobid,jobtype,machine_id,descr,priority,status,jobsource,
        planned_date,due_date,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (next_jobid(c, "PM", machine_id=mid), "PM", mid, "\n".join(lines), 1, "Reported", "PM", d, d, uid, now()))
    try:
        c.execute("UPDATE jobs SET pm_freq=? WHERE id=?", (freq, jid))
    except Exception:
        pass                       # an older database without the column
    log_status(c, jid, "Reported", uid)
    set_stage(c, jid)
    c.execute("UPDATE machines SET last_pm_date=? WHERE id=?", (d, mid))
    return jid


def _team_row(r):
    mem = _ids(r["members"])
    lead = r["lead"] if r["lead"] in mem else (mem[0] if mem else None)
    try:
        login = r["login_id"]
    except (IndexError, KeyError):
        login = None
    return {"name": r["name"] or "", "color": r["color"] or "", "lead": lead, "members": mem,
            "login": login, "start": r["start_time"] or "", "end": r["end_time"] or "", "jobs": []}


@router.get("/day")
async def day(req: Request, d: str = ""):
    """Everything the assignment popup needs for one day."""
    u = require_role(req, "planner", "admin", "manager", "engcenter")
    fac = _fac(u)
    d = (d or today())[:10]
    # b417: the Central Electrical board — the same board, its own crews (kept under the
    # plant's NEGATIVE id, so they never mix with the plant's crews), CE technicians,
    # and only the electrical / other / unknown work plus electrical PM.
    from .elec import is_ce_planner, ce_ids, CeFilter
    ce = (req.query_params.get("ce") == "1") or is_ce_planner(u)
    tk = -fac if ce else fac
    with closing(db()) as c:
        _ensure(c)
        from .elec import ensure_board as _eb0
        _eb0(c)                                  # b433: one-off repairs before the lists are read
        techs = [{"id": r["id"], "name": r["name"], "username": r["username"],
                  "photo": r["photo"] or "", "department": r["department"] or ""}
                 for r in c.execute(
                     "SELECT id,name,username,photo,department FROM users"
                     " WHERE active=1 AND role='technician' AND COALESCE(can_login,1)=0"
                     " AND (factory_id=? OR COALESCE(factory_id,0)=0) ORDER BY name", (fac,))]
        # the company phones — technician login accounts this factory's crews sign in with
        logins = [{"id": r["id"], "username": r["username"], "name": r["name"]}
                  for r in c.execute(
                      "SELECT id,username,name FROM users WHERE active=1 AND role='technician'"
                      " AND COALESCE(can_login,1)=1 AND (factory_id=? OR COALESCE(factory_id,0)=0) ORDER BY username", (fac,))]
        one_person, standalone = _handset_people(c, fac)
        cm = req.query_params.get("cm") == "1"
        if ce and cm:
            # b432: the Central CM board offers Central technicians AND this plant's own
            standalone = set(standalone) | set(ce_ids(c, fac))
        elif ce:
            techs, logins, one_person = [], [], {}
            standalone = ce_ids(c, fac)
        # An account nobody is attached to IS the technician: the person signs in as
        # themselves and has no separate record (BFLFP: tech2 is Kanya). Without this they
        # could not be picked for a crew, showed as a bare "#4", and their work sat in a
        # phone-account column that came back every time it was deleted.
        have = {t["id"] for t in techs}
        for r in c.execute("SELECT id,name,username,photo,department FROM users WHERE id IN (%s)"
                           % (",".join(str(i) for i in standalone) or "0")):
            if r["id"] not in have:
                techs.append({"id": r["id"], "name": r["name"] or r["username"], "username": r["username"],
                              "photo": r["photo"] or "", "department": r["department"] or ""})
        techs.sort(key=lambda t: (t["name"] or "").lower())
        jobs = [dict(r) for r in c.execute(
            """SELECT j.id,j.jobid,j.jobtype,j.status,j.priority,j.descr,j.lead_tech,j.helpers,
                      j.problem_type,j.fault_category,j.machine_id,j.pm_freq,
                      j.planned_start,j.planned_end, m.code mcode, m.name mname, m.floor mfloor, m.line mline, m.department mdept, m.asset_group mgroup, m.pm_group_color mcolor
               FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
               WHERE j.planned_date=? AND j.status NOT IN ('Cancelled','Done','Rejected','ServiceCompleted')
                 AND (COALESCE(m.factory_id,j.factory_id)=? OR COALESCE(m.factory_id,j.factory_id) IS NULL)
               ORDER BY j.priority DESC, j.jobtype, j.id""", (d, fac))]
        # Work planned for an earlier day that never got finished. It is still somebody's
        # job, so it belongs in front of the planner when they build the next day — but
        # it stays on its own date until they deliberately drag it onto today's crew.
        # Each one carries `carry_from` so the popup can label it and keep it apart from
        # the day's own work.
        for r in c.execute(
            """SELECT j.id,j.jobid,j.jobtype,j.status,j.priority,j.descr,j.lead_tech,j.helpers,
                      j.problem_type,j.fault_category,j.machine_id,j.pm_freq,
                      j.planned_start,j.planned_end,j.planned_date, m.code mcode, m.name mname, m.floor mfloor, m.line mline, m.department mdept, m.asset_group mgroup, m.pm_group_color mcolor
               FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
               WHERE j.planned_date < ? AND j.planned_date IS NOT NULL AND j.planned_date != ''
                 AND j.status NOT IN ('Cancelled','Done','Rejected','ServiceCompleted')
                 AND (COALESCE(m.factory_id,j.factory_id)=? OR COALESCE(m.factory_id,j.factory_id) IS NULL)
               ORDER BY j.planned_date DESC, j.priority DESC, j.id""", (min(d, today()), fac)):
            # ONLY WORK THAT IS ACTUALLY LATE is carried. This used to be "any date before
            # the board's date", so planning ahead broke the plan behind it: opening 23 Sep
            # on the 21st pulled every job of 22 Sep in as "carried over", laid each one
            # under its own technician, and the 23 Sep save then re-dated all of them to
            # the 23rd. 22 Sep was left with nothing assigned. Work planned for a day that
            # has not come yet is not late; it belongs on its own day's board.
            j = dict(r)
            j["carry_from"] = j.pop("planned_date")
            jobs.append(j)
        # b413: electrical points on each PM job, and the all-electrical PM jobs the CE
        # planner raised — those are not the plant crew's to take, so they stay off this
        # board unless the plant already put somebody on one.
        try:
            from .elec import counts as _elc
            _cnt, _m2t = _elc(c, fac), pm._match_all(c, fac)
        except Exception:
            _cnt, _m2t = {}, {}
        _keep = []
        from .elec import ensure_board as _eb
        _eb(c)
        _brd = {r["id"]: (r["board"] or "") for r in c.execute(
            "SELECT id, board FROM jobs WHERE id IN (%s)" % ",".join(str(int(x["id"])) for x in jobs))} if jobs else {}
        _ce = ce_ids(c)
        _cef = CeFilter(c) if ce else None
        from .elec import CE_PLANTS as _CEP
        _pf = CeFilter(c)
        _elst = {r["job_id"]: r["tech"] for r in c.execute(
            "SELECT job_id, tech FROM pm_elec WHERE factory_id=?", (fac,))} if ce else {}
        for j in jobs:
            _pm = (j.get("jobtype") or "") == "PM" and j.get("machine_id")
            if _pm:
                _na, _ne = _cnt.get((_m2t.get(j["machine_id"]), j.get("pm_freq") or "weekly")) or (0, 0)
                j["el"] = _ne
            if ce:
                if not _cef.ok(dict(j, factory_id=fac)):
                    continue
                if _pm:
                    # on the CE board a PM job's "crew" is its electrical part's technician
                    j["lead_tech"], j["helpers"] = _elst.get(j["id"]), ""
                elif j.get("lead_tech") and _brd.get(j["id"]) != "ce":
                    continue                    # b430: the plant gave it its crew (whoever is on it)
            else:
                if _pm and _na and _na == _ne and not j.get("lead_tech"):
                    continue
                if not _pm and j.get("lead_tech") and _brd.get(j["id"]) == "ce":
                    continue                    # b430: the Central Electrical board gave it its crew
                # b421: electrical corrective work on BFL / BFLPC is Central Electrical's
                if not _pm and fac in _CEP and not j.get("lead_tech") and _pf.trade(dict(j, factory_id=fac)) == "Electrical":
                    continue
            _keep.append(j)
        jobs = _keep
        # jobs already carrying a lead go back to that lead's team
        jobs_by_lead = {}
        for j in jobs:
            if j["lead_tech"]:
                # work booked to a handset only ONE person uses is that person's work
                # (Mark on tech3, Choke on tech1): it goes to his crew, not to a column
                # named after the phone that nobody can delete
                jobs_by_lead.setdefault(one_person.get(j["lead_tech"], j["lead_tech"]), []).append(j["id"])
        rows = c.execute("SELECT * FROM plan_teams WHERE factory_id=? AND day=? ORDER BY seq,id",
                         (tk, d)).fetchall()
        # A CREW, ONCE FORMED, STAYS UNTIL THE PLANNER REMOVES IT — on all three plants.
        # A day nobody has saved yet opens with the crews of the last day that WAS
        # planned: their names, colours, leads, members, phone and hours. Never their
        # jobs — `_team_row` hands each one over with an empty job list, and the work
        # shown under it is only what is really assigned to its lead on THIS day. (For
        # a few builds a new day opened empty instead; the plant asked for this back.)
        # A day that was saved with every crew deleted stays empty: plan_days says it
        # was planned, so nothing is carried into it.
        carried, carried_from = False, None
        if not rows and not c.execute("SELECT 1 FROM plan_days WHERE factory_id=? AND day=?",
                                      (tk, d)).fetchone():
            src = _carry_source(c, tk, d)
            if src:
                rows = c.execute("SELECT * FROM plan_teams WHERE factory_id=? AND day=?"
                                 " ORDER BY seq,id", (tk, src)).fetchall()
                if rows:
                    carried, carried_from = True, src
        teams = [_team_row(r) for r in rows]
        for t in teams:                      # a crew saved with the handset in it means its person
            t["members"] = list(dict.fromkeys(one_person.get(m, m) for m in t["members"]))
            t["lead"] = one_person.get(t["lead"], t["lead"])
        names = user_names(c)
        # Which of this plant's technician rows are HANDSETS rather than people. A job
        # whose lead is one of these was started from a shared phone, and the person who
        # actually did it was never recorded.
        acct_ids = {l["id"]: l["username"] for l in logins if l["id"] not in standalone}
        # put each already-assigned job in the team that holds its lead; a lead with no team
        # at all gets one, or that work would look unassigned. Never the same tech twice.
        for lead, jids in jobs_by_lead.items():
            t = (next((t for t in teams if t["lead"] == lead), None)
                 or next((t for t in teams if lead in t["members"]), None))
            if t:
                t["jobs"].extend(jids)
                continue
            # A lead with no crew gets a column of their own, or their work would look
            # unassigned. But WHO the lead is matters, and this was not saying.
            #
            # When a technician picks up a job on a shared company handset, the job is
            # booked to the ACCOUNT they signed in as, not to the person holding the
            # phone. `techs` above lists people only, so that lead resolves to nothing
            # and the column came out named after the handset with a bare `#73` inside
            # it — indistinguishable from a real crew, and impossible to get rid of:
            # deleting the column does nothing because the column is not stored, it is
            # regenerated from the job every time the board loads. The only thing that
            # clears it is giving the job to a person.
            #
            # So the column now says what it is, and the board can offer the fix.
            nm = names.get(str(lead), "")
            # An account row carries a PERSON's name on these two plants ("mon" the
            # person and "mon" the phone are one keystroke apart), so a column standing
            # for an account is titled by the account instead — the badge beside it then
            # has something to agree with.
            if lead in acct_ids:
                nm = acct_ids.get(lead) or nm
            # b450: a lead who is not a technician at all (a planner) — same treatment as a
            # phone account: the column says so and takes no drops
            _lr = c.execute("SELECT role FROM users WHERE id=?", (lead,)).fetchone()
            _not_tech = bool(_lr) and _lr["role"] != "technician"
            teams.append({"name": nm.split("(")[0].strip() or "Team", "color": "", "lead": lead,
                          "members": [lead], "login": None, "start": "", "end": "",
                          "jobs": list(jids), "auto": True,
                          "lead_not_tech": _not_tech,
                          "lead_is_account": (lead in acct_ids) or _not_tech,
                          "lead_username": acct_ids.get(lead, "")})
        # Work that has never been given a day. It is not "unassigned on this day" —
        # it is nowhere yet. On a desk that pile lives on the calendar next door; on a
        # phone there is no next door, so it belongs on this board. Dropping one on a
        # crew is what puts it on this day, because a crew only exists on a day.
        undated = [dict(r) for r in c.execute(
            """SELECT j.id,j.jobid,j.jobtype,j.status,j.priority,j.descr,j.lead_tech,
                      j.helpers,j.created_at, m.code mcode, m.name mname, m.floor mfloor, m.line mline, m.department mdept, m.asset_group mgroup, m.pm_group_color mcolor
               FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
               WHERE COALESCE(j.planned_date,'')=''
                 AND j.status IN """ + UNDATED_ST + """
                 AND (COALESCE(m.factory_id,j.factory_id)=? OR COALESCE(m.factory_id,j.factory_id) IS NULL)
               ORDER BY j.priority DESC, j.id LIMIT 200""", (fac,))]
        pm_due = _pm_due(c, fac, d, ce=ce)
        if ce:
            undated = [j for j in undated if _cef.ok(dict(j, factory_id=fac))
                       and not (j.get("lead_tech") and j["lead_tech"] not in _ce)]
        elif fac in _CEP:
            undated = [j for j in undated if (j.get("jobtype") or "").upper() == "PM" or j.get("lead_tech")
                       or _pf.trade(dict(j, factory_id=fac)) != "Electrical"]
        # lends as recorded (see plan_lends). A day saved before b358 keeps the old
        # reading — lends worked out from the helpers — so an issued plan looks the same.
        pd = c.execute("SELECT COALESCE(lends,0) l FROM plan_days WHERE factory_id=? AND day=?",
                       (tk, d)).fetchone()
        lends_known = (pd is None) or bool(pd["l"])
        lends = {}
        if lends_known:
            ids = [j["id"] for j in jobs]
            for i in range(0, len(ids), 500):
                ch = ids[i:i + 500]
                for r in c.execute("SELECT job_id, tech, day FROM plan_lends WHERE factory_id=?"
                                   " AND job_id IN (%s) ORDER BY day" % ",".join("?" * len(ch)),
                                   (tk, *ch)):
                    lends.setdefault(str(r["job_id"]), {})[r["day"]] = lends.get(
                        str(r["job_id"]), {}).get(r["day"], []) + [r["tech"]]
            # the latest day a job was saved on is what it carries
            lends = {k: v[max(v)] for k, v in lends.items()}
        # Does this plant give every technician their own account? If it does, the crew
        # has no phone to choose — each person carries their own — and the board must
        # stop asking. Leaving that picker on screen is most of why this was hard to
        # follow: it implied the crew's work goes to whichever account is selected, which
        # on BFL and BFLPC has not been true since item 93 and was never what was wanted.
        # b375: the crew phone is retired — every technician signs in with their own
        # account, and a job goes to the account of the person it is given to.
        self_login = True
        # the day's own work, counted exactly as the calendar counts it (pm.day_counts)
        day_cnt = pm.day_counts(c, fac, d, d).get(d) or {
            "total": 0, "done": 0, "accept": 0, "working": 0, "assigned": 0, "waiting": 0}
    for i, t in enumerate(teams):
        t["color"] = t["color"] or COLORS[i % len(COLORS)]
        t["name"] = t["name"] or ("Team " + chr(65 + i))
    return {"date": d, "techs": techs, "logins": logins, "teams": teams, "jobs": jobs,
            "pm_due": pm_due, "carried": carried, "carried_from": carried_from,
            "lends_known": lends_known, "lends": lends,
            "counts": day_cnt,
            "undated": undated, "self_login": self_login, "ce": ce}


@router.post("/save")
async def save(req: Request):
    """Persist the day's crews and write the assignment onto their jobs."""
    u = require_role(req, "planner", "admin")
    fac = _fac(u)
    b = await req.json()
    d = (b.get("date") or "")[:10]
    if not d:
        raise HTTPException(400, "date required")
    teams = b.get("teams") or []
    from .elec import is_ce_planner, ce_ids          # b417: the Central Electrical board
    ce = bool(b.get("ce")) or is_ce_planner(u)
    tk = -fac if ce else fac
    with closing(db()) as c:
        _ensure(c)
        valid = {r["id"] for r in c.execute(
            "SELECT id FROM users WHERE active=1 AND role='technician'"
            " AND COALESCE(can_login,1)=0 AND (factory_id=? OR COALESCE(factory_id,0)=0)", (fac,))}
        valid |= _handset_people(c, fac)[1]      # a technician who signs in as themselves
        if ce and b.get("cm"):
            valid |= set(ce_ids(c, fac))            # b432: Central CM — plant technicians too
        elif ce:
            valid = {i for i in ce_ids(c, fac)}
        _ceset = ce_ids(c)
        from .elec import ensure_board as _eb
        _eb(c)
        _brd = lambda jid: ((c.execute("SELECT board FROM jobs WHERE id=?", (jid,)).fetchone() or {"board": ""})["board"] or "")
        # Who signs in as themselves. Empty on a plant whose accounts are not named after
        # its technicians, which is what keeps the shared-handset plants working as before.
        own = {} if ce else own_account_map(c, fac, strict=False)
        # ── a column built from a PHONE ACCOUNT is not thrown away any more ──────────
        # A crew is made of technician PEOPLE, and a team whose members are login
        # accounts used to be skipped whole, with every job on it — silently, because
        # the "not saved" counter only counted numeric job ids and a PM card is
        # "pm:<machine>:<freq>". On BFLPC every technician has BOTH a person record and
        # a login account with the SAME NAME, so picking the wrong one of the two is a
        # single mis-tap, and the planner was told "saved · 46 jobs" for work that was
        # never written. An account now resolves to the person behind it wherever that
        # is unambiguous — their own account, or the one person signed in on it — and
        # what still cannot be resolved is NAMED in the answer instead of vanishing.
        acct_person = {a: p for p, a in own.items()}      # their own account → them
        sharing = {}                                     # a handset only one person uses
        for r in c.execute(
                "SELECT login_id, COUNT(*) n, MIN(id) one FROM users WHERE active=1"
                " AND role='technician' AND COALESCE(can_login,1)=0 AND login_id IS NOT NULL"
                " AND (factory_id=? OR COALESCE(factory_id,0)=0) GROUP BY login_id", (fac,)):
            if r["n"] == 1:
                sharing[r["login_id"]] = r["one"]

        def _people(raw_ids):
            """The technician PEOPLE a team's member list stands for."""
            out = []
            for m in raw_ids:
                if m in valid:
                    out.append(m)
                    continue
                p = acct_person.get(m) or sharing.get(m)
                if p and p in valid and p not in out:
                    out.append(p)
            return out
        # The company phones, in the order the crews use them: the first team carries
        # the first phone, the second team the second. A planner who picks a different
        # one still wins — this only fills a team left on "none", which would otherwise
        # be a crew whose work reaches no handset at all.
        login_order = [r["id"] for r in c.execute(
            "SELECT id FROM users WHERE active=1 AND role='technician'"
            " AND COALESCE(can_login,1)=1 AND (factory_id=? OR COALESCE(factory_id,0)=0) ORDER BY username", (fac,))]
        if ce:
            login_order = []                     # CE technicians each sign in as themselves
        valid_logins = set(login_order)
        day_jobs = {r["id"]: dict(r) for r in c.execute(
            """SELECT j.id,j.jobid,j.status,j.lead_tech,j.started_at,j.planned_date, m.code mcode FROM jobs j
               LEFT JOIN machines m ON m.id=j.machine_id
               WHERE j.planned_date=? AND (COALESCE(m.factory_id,j.factory_id)=? OR COALESCE(m.factory_id,j.factory_id) IS NULL)""", (d, fac))}
        # This day's own work — the only jobs this save may take a technician away from.
        own_day = set(day_jobs)
        # Unfinished work from earlier days can be pulled onto today's crew, and doing
        # that re-dates it: the plan says what is being worked on today. Leaving it alone
        # changes nothing, so opening tomorrow never disturbs yesterday's board.
        for r in c.execute(
            """SELECT j.id,j.jobid,j.status,j.lead_tech,j.started_at,j.planned_date, m.code mcode FROM jobs j
               LEFT JOIN machines m ON m.id=j.machine_id
               WHERE j.planned_date < ? AND j.planned_date IS NOT NULL AND j.planned_date != ''
                 AND j.status NOT IN ('Cancelled','Done','Rejected','ServiceCompleted')
                 AND (COALESCE(m.factory_id,j.factory_id)=? OR COALESCE(m.factory_id,j.factory_id) IS NULL)""", (d, fac)):
            day_jobs.setdefault(r["id"], dict(r))
        # never dated at all — assigning one here is also what gives it its day
        for r in c.execute(
            """SELECT j.id,j.jobid,j.status,j.lead_tech,j.started_at,j.planned_date, m.code mcode FROM jobs j
               LEFT JOIN machines m ON m.id=j.machine_id
               WHERE COALESCE(j.planned_date,'')=''
                 AND j.status IN """ + UNDATED_ST + """
                 AND (COALESCE(m.factory_id,j.factory_id)=? OR COALESCE(m.factory_id,j.factory_id) IS NULL)""", (fac,)):
            day_jobs.setdefault(r["id"], dict(r))
        m2t = pm._match_all(c, fac)
        tname = {r["id"]: r["machine_type"] for r in
                 c.execute("SELECT id,machine_type FROM pm_templates WHERE factory_id=?", (fac,))}
        seen_tech, assigned, clean, made = set(), set(), [], 0
        no_crew_names = []
        renamed = []
        # Jobs the save throws away along with the column they were sitting in. A crew is
        # made of technician PEOPLE, so a payload team whose only member is a login
        # account — the generated phone-account column — resolves to no members and is
        # skipped whole, its jobs with it. That was silent, and silence here is
        # indistinguishable from a failed save: a planner put three jobs on those columns
        # and was told "4 job(s) assigned". Counted so the message can name what did not
        # go in, even though the board now refuses the drop that creates this.
        n_no_tech = 0

        # Extra people on ONE job, lent from another crew. Keyed by the payload's own job
        # reference rather than a job id, because a PM occurrence ("pm:<machine>:<freq>")
        # does not have an id until _resolve below creates it.
        #
        # These NEVER join the crew: a crew's members are what claim its phone
        # (`UPDATE users SET login_id`) and what the printed plan counts, so lending a
        # technician for one job must not move their phone or empty their own crew.
        # They land in that job's helpers and nowhere else.
        extra = {}
        for k, v in (b.get("extra") or {}).items():
            ids = [m for m in _ids(",".join(str(x) for x in (v or []))) if m in valid]
            if ids:
                extra[str(k)] = ids

        def _resolve(raw):
            """A payload job entry is either an existing job id or a 'pm:<machine>:<freq>'
            occurrence, which becomes a real work order the moment it is assigned."""
            nonlocal made
            s = str(raw)
            if s.startswith("pm:"):
                _, mid, freq = (s.split(":", 2) + ["", ""])[:3]
                if not mid.isdigit():
                    return None
                before = c.execute("SELECT COALESCE(MAX(id),0) n FROM jobs").fetchone()["n"]
                jid = _make_pm_job(c, fac, int(mid), freq, u["id"], d, m2t, tname)
                if jid and jid not in day_jobs:
                    # The work order may already have existed — moved to another day, or
                    # even finished. It must be judged by what it really is, never stubbed
                    # as a fresh "Reported" job, or a stale board could re-assign done work.
                    row = c.execute("SELECT id,jobid,status,lead_tech,started_at,planned_date"
                                    " FROM jobs WHERE id=?", (jid,)).fetchone()
                    day_jobs[jid] = dict(row) if row else {
                        "id": jid, "jobid": "", "status": "Reported", "lead_tech": None,
                        "started_at": None, "planned_date": d, "mcode": ""}
                    day_jobs[jid].setdefault("mcode", "")
                    if jid > before:
                        made += 1
                return jid
            return int(s) if s.isdigit() and int(s) in day_jobs else None

        for t in teams:
            mem = _people(_ids(",".join(str(x) for x in (t.get("members") or []))))
            mem = list(dict.fromkeys(mem))      # b431: one person may be on several teams a day (lead on one, helper on another)
            seen_tech.update(mem)
            if not mem:
                # every card on it, PM occurrences included — the count used to miss those
                n_no_tech += len(t.get("jobs") or [])
                if t.get("jobs"):
                    no_crew_names.append((t.get("name") or "?")[:40])
                continue
            _ld = t.get("lead")
            _ld = _ld if _ld in mem else ((_people([_ld]) or [None])[0] if _ld else None)
            lead = _ld if _ld in mem else mem[0]
            jids, raw_of = [], {}
            for raw in (t.get("jobs") or []):
                jid = _resolve(raw)
                if jid and jid not in assigned:
                    jids.append(jid)
                    raw_of[jid] = str(raw)
            assigned.update(jids)
            login = t.get("login")
            login = int(login) if str(login or "").isdigit() and int(login) in valid_logins else None
            if login is None:
                # fall back to this team's own phone, and never to one another team on
                # this day has already claimed
                taken = {x["login"] for x in clean if x["login"]}
                free = [i for i in login_order if i not in taken]
                pos = len(clean)
                login = login_order[pos] if pos < len(login_order) and login_order[pos] in free \
                    else (free[0] if free else None)
            _nm = (t.get("name") or "").strip()[:60]
            # One day, one name per crew. Two columns both called "Team B" is a plan
            # nobody can read back — on the printed sheet, in the technician's
            # notification, or on this board. A repeat gets a number rather than being
            # refused, so a save is never lost to a typo.
            _taken = {x["name"].strip().lower() for x in clean if x["name"]}
            if not _nm or _nm.lower() in _taken:
                # crews are lettered, in order: Team A, Team B, Team C … The next FREE
                # letter is taken, so a repeat never becomes "Team A (2)" and the board
                # reads down the alphabet however the columns were made.
                _base = _nm
                for _k in range(26):
                    _cand = "Team " + chr(65 + _k)
                    if _cand.lower() not in _taken:
                        _nm = _cand
                        break
                else:
                    _k = 2
                    while ("team %d" % _k) in _taken:
                        _k += 1
                    _nm = "Team %d" % _k
                if _base:
                    renamed.append((_base, _nm))
            clean.append({"name": _nm, "color": (t.get("color") or "")[:9],
                          "lead": lead, "members": mem, "login": login,
                          "start": (t.get("start") or "")[:5],
                          "end": (t.get("end") or "")[:5], "jobs": jids, "raw": raw_of,
                          # a column the board built from someone's unfinished work and
                          # the planner left as it was — its jobs are saved, the column
                          # is not stored as a crew (see the INSERT below)
                          "auto": bool(t.get("auto"))})
        # who was on this day's crews before this save — anyone dropped loses the phone
        was = set()
        for r in c.execute("SELECT members FROM plan_teams WHERE factory_id=? AND day=?", (tk, d)):
            was.update(_ids(r["members"]))
        # b395 ── WHO WAS ON EACH CREW when the board opened ─────────────────────────
        # A person taken off a crew came straight back: the change was saved on this day
        # only, while a day planned ahead kept its own copy of the old crew (and every
        # day after it carried that copy on), and the crew's jobs still listed them as a
        # helper. The crew as the board showed it — this day's rows, or the day it was
        # carried from — is what the save compares against to see who left and who joined.
        _src_rows = c.execute("SELECT name, lead, members FROM plan_teams WHERE factory_id=? AND day=?",
                              (tk, d)).fetchall()
        if not _src_rows and not c.execute("SELECT 1 FROM plan_days WHERE factory_id=? AND day=?",
                                           (tk, d)).fetchone():
            _cs = _carry_source(c, tk, d)
            if _cs:
                _src_rows = c.execute("SELECT name, lead, members FROM plan_teams WHERE factory_id=? AND day=?",
                                      (tk, _cs)).fetchall()
        before_crew = {}
        for r in _src_rows:
            k = (r["name"] or "").strip().lower()
            if k:
                before_crew[k] = set(_ids(r["members"])) | ({r["lead"]} if r["lead"] else set())
        crew_change = {}                 # name → (who left, who joined)
        for t in clean:
            k = (t["name"] or "").strip().lower()
            if t.get("auto") or k not in before_crew:
                continue
            now_set = set(t["members"]) | {t["lead"]}
            left, joined = before_crew[k] - now_set, now_set - before_crew[k]
            if left or joined:
                crew_change[k] = (left, [m for m in t["members"] if m in joined])
        c.execute("DELETE FROM plan_teams WHERE factory_id=? AND day=?", (tk, d))
        # this day is now PLANNED — even with no crews on it, so nothing is carried into it
        c.execute("INSERT OR IGNORE INTO plan_days(factory_id,day) VALUES(?,?)", (tk, d))
        # ── a crew the planner REMOVED stays removed on the days ahead too ──────────
        # Crews carry forward, so a later day that nobody has saved simply stops showing
        # it. But a day planned in advance holds its own copy, and "stays until the
        # planner deletes it" means deleting it once — not hunting it down day by day.
        # Only crews the planner explicitly binned are sent (the board records each 🗑),
        # so a RENAME is never mistaken for a delete; and a name re-used in this very
        # save is still a crew and is left alone. A later day where that crew already
        # has work booked keeps it, and the planner is told which: removing it there
        # would quietly strand jobs someone already planned.
        _now_names = {x["name"].strip().lower() for x in clean if x["name"] and not x.get("auto")}
        dropped_crews = [s for s in {str(x).strip() for x in (b.get("dropped") or [])}
                         if s and s.lower() not in _now_names]
        dropped_ahead, kept_ahead = 0, []
        # b395 ── …and a change of MEMBERS reaches the days planned ahead as well ──────
        # Same rule as a removed crew: the crew of that name on each later day that
        # already has its own copy loses the person who left and gains the one who
        # joined. Someone who LEADS unfinished work booked on that later day is kept
        # there, and the planner is told, so no planned job is stranded.
        members_ahead, kept_member = 0, []
        for k, (left, joined) in crew_change.items():
            for r in c.execute("""SELECT id, day, lead, members FROM plan_teams
                                   WHERE factory_id=? AND day>? AND LOWER(TRIM(name))=?""",
                               (tk, d, k)).fetchall():
                mem0 = _ids(r["members"])
                mem = list(mem0)
                for x in left:
                    if x not in mem and x != r["lead"]:
                        continue
                    busy = c.execute(
                        "SELECT COUNT(*) n FROM jobs WHERE planned_date=? AND lead_tech=?"
                        " AND status NOT IN ('Done','Approved','Cancelled','Rejected','ServiceCompleted')",
                        (r["day"], x)).fetchone()["n"]
                    if busy:
                        kept_member.append({"name": user_names(c).get(str(x), str(x)),
                                            "crew": k, "day": r["day"], "jobs": busy})
                        continue
                    mem = [m for m in mem if m != x]
                other = set()
                for o in c.execute("SELECT members, lead FROM plan_teams WHERE factory_id=? AND day=? AND id<>?",
                                   (tk, r["day"], r["id"])):
                    other |= set(_ids(o["members"])) | ({o["lead"]} if o["lead"] else set())
                for x in joined:
                    if x not in mem and x not in other and len(mem) < 3:
                        mem.append(x)
                lead = r["lead"] if r["lead"] in mem else (mem[0] if mem else None)
                if mem == mem0 and lead == r["lead"]:
                    continue
                if mem:
                    c.execute("UPDATE plan_teams SET members=?, lead=? WHERE id=?",
                              (",".join(str(m) for m in mem), lead, r["id"]))
                else:
                    c.execute("DELETE FROM plan_teams WHERE id=?", (r["id"],))
                members_ahead += 1
        for nm in dropped_crews:
            for r in c.execute("""SELECT id, day, lead, members FROM plan_teams
                                   WHERE factory_id=? AND day>? AND LOWER(TRIM(name))=?""",
                               (tk, d, nm.lower())).fetchall():
                crew = [x for x in _ids(r["members"])] + ([r["lead"]] if r["lead"] else [])
                busy = 0
                if crew:
                    busy = c.execute(
                        "SELECT COUNT(*) n FROM jobs WHERE planned_date=? AND lead_tech IN (%s)"
                        " AND status NOT IN ('Done','Approved','Cancelled','Rejected','ServiceCompleted')"
                        % ",".join("?" * len(crew)), (r["day"], *crew)).fetchone()["n"]
                if busy:
                    kept_ahead.append({"name": nm, "day": r["day"], "jobs": busy})
                else:
                    c.execute("DELETE FROM plan_teams WHERE id=?", (r["id"],))
                    dropped_ahead += 1
        for i, t in enumerate(clean):
            # AN AUTOMATIC COLUMN IS NOT A CREW. The board builds one for each technician
            # who still holds unfinished work but is on no crew, so that work never looks
            # unassigned. It used to be written here like any crew — and with crews now
            # carrying forward, that turned "Mark still has two jobs open" into a
            # standing crew called Mark on every day after, one nobody had created and
            # the planner would have to find and delete. Left as the board made it, it is
            # rebuilt from the work each day instead. Once the planner renames it or
            # changes who is on it, it is their crew and is stored like any other.
            if t.get("auto"):
                continue
            c.execute("""INSERT INTO plan_teams(factory_id,day,name,color,lead,members,start_time,
                end_time,seq,login_id) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (tk, d, t["name"], t["color"], t["lead"],
                 ",".join(str(m) for m in t["members"]), t["start"], t["end"], i, None))
            # The phone belongs to the crew, not to a person: whoever is on this team today
            # sees its work on this login. Members change; the phone stays with the team.
            # …but only for a member who has NO account of their own. This is where the
            # phones were going wrong. The handset is chosen by position — first crew
            # takes the first username in order — and it was then written over every
            # member's `login_id` without asking whether that person already signs in as
            # themselves. On BFL, which issued one account per technician, that pointed
            # `neng` at `techbfl2` (phaey's) and `jack` at `techbfl1` (neng's); a
            # technician signing in on their own account then saw an empty phone, because
            # the app shows the work of the people pointed AT that account and nobody was
            # pointed at `techbfl7`. `own` below is the person→own-account map, matched
            # by name and only where it is unambiguous both ways, so a plant whose
            # accounts are not named after its people is untouched and keeps the shared
            # handset it has always used.
            # b375: THE CREW PHONE IS RETIRED. Every technician has their own account, so
            # a team save no longer writes a crew handset over anybody's login link — a
            # person whose own account is not matched by name (ตุ่ม on BFL, whose account
            # is filed under another plant) was the one it could still overwrite. The
            # link a person has is set in Admin and is left exactly as it is.
            if t["members"]:
                for m in t["members"]:
                    if m in own:
                        c.execute("UPDATE users SET login_id=? WHERE id=?", (own[m], m))
        # someone taken off today's crews hands the phone back, or their old team would
        # keep seeing them on it — but a technician's OWN account is not the crew's to
        # take away. Clearing it was what left them signed in to an empty app.
        # b375: nothing to hand back — no crew phone was lent (see above)
        n_jobs = 0
        # Jobs the save deliberately does NOT touch. They were being counted as nothing
        # at all, so a planner who put four jobs on the board was told "2 jobs assigned"
        # with no hint where the other two went — which reads as a failure rather than
        # as the rule working. Counted here so the message can say what happened.
        n_started = 0
        n_kept_day = 0                  # carried work left on its own day, see below
        touched = {int(x) for x in (b.get("touched") or []) if str(x).isdigit()}
        c.execute("DELETE FROM plan_lends WHERE factory_id=? AND day=?", (tk, d))
        c.execute("UPDATE plan_days SET lends=1 WHERE factory_id=? AND day=?", (tk, d))
        for t in clean:
            base = [m for m in t["members"] if m != t["lead"]]
            crew_ids = set(t["members"]) | {t["lead"]}
            for jid in t["jobs"]:
                job = day_jobs[jid]
                if ce:
                    # b417: on the CE board a PM job keeps its plant crew; what is given is
                    # its ELECTRICAL part. Corrective work is given the CE crew outright —
                    # unless the plant already gave it a crew of its own.
                    _jt = (c.execute("SELECT jobtype FROM jobs WHERE id=?", (jid,)).fetchone() or {"jobtype": ""})["jobtype"]
                    if (_jt or "").upper() == "PM":
                        if c.execute("SELECT 1 FROM pm_elec WHERE job_id=?", (jid,)).fetchone():
                            c.execute("UPDATE pm_elec SET tech=?, assigned_by=?, assigned_at=? WHERE job_id=?"
                                      " AND COALESCE(done_at,'')=''", (t["lead"], u["id"], now(), jid))
                        else:
                            c.execute("INSERT INTO pm_elec(job_id,factory_id,tech,assigned_by,assigned_at)"
                                      " VALUES(?,?,?,?,?)", (jid, fac, t["lead"], u["id"], now()))
                        n_jobs += 1
                        continue
                    if job.get("lead_tech") and _brd(jid) != "ce":
                        n_started += 1          # b430: the plant gave it its crew
                        continue
                elif job.get("lead_tech") and _brd(jid) == "ce":
                    n_started += 1              # b430: the Central Electrical board's
                    continue
                for m in extra.get(t["raw"].get(jid, ""), []):
                    if m not in crew_ids:
                        c.execute("INSERT OR IGNORE INTO plan_lends(factory_id,day,job_id,tech)"
                                  " VALUES(?,?,?,?)", (tk, d, jid, m))
                if job["status"] not in ASSIGNABLE:
                    n_started += 1
                    continue                                # started/finished work is never re-assigned
                # Status alone was not enough: a job paused or put on hold is back in
                # ASSIGNABLE, and it HAS been started — so a team save could take it off
                # the technician whose timer is on it. Once work has begun the crew is
                # the crew.
                if job.get("started_at"):
                    n_started += 1
                    continue
                # WORK FROM ANOTHER DAY IS ONLY RE-DATED IF THE PLANNER MOVED IT. The board
                # lays unfinished work from earlier days out under its own technician by
                # itself, so "it is on a column" says nothing about what the planner
                # meant — and every such job used to be rewritten to this day on save.
                # Left where the board put it, still with the same technician, it keeps
                # its own day. Dragged, sent or picked onto a crew (the board sends those
                # as `touched`), or given to a different technician, it is re-planned here.
                if (jid not in own_day and (job.get("planned_date") or "")
                        and job.get("lead_tech") == t["lead"] and jid not in touched):
                    n_kept_day += 1
                    continue
                # The lead is always the OWNING crew's lead, never a borrowed one: the
                # printed plan files a job under the crew whose members include its lead
                # technician, so a lent lead would put the job on the wrong crew's sheet.
                lent = [m for m in extra.get(t["raw"].get(jid, ""), []) if m != t["lead"]]
                helpers = ",".join(str(m) for m in dict.fromkeys(base + lent))
                c.execute("""UPDATE jobs SET lead_tech=?, helpers=?, planned_start=?, planned_end=?,
                    planned_at=?, planned_date=?, status='Assigned', board=? WHERE id=?""",
                    (t["lead"], helpers, t["start"] or None, t["end"] or None, now(), d,
                     "ce" if ce else "", jid))
                if job["status"] != "Assigned":
                    log_status(c, jid, "Assigned", u["id"])
                # the Stage 1 / Stage 2 pair is a stored column, so it has to be
                # recomputed here too — otherwise the planner's list keeps showing
                # "Reported · Not assigned" for a job that now has a whole crew on it
                set_stage(c, jid)
                n_jobs += 1
        # b396: a person taken off a crew is NOT taken off work already given to them.
        # Jobs this save rewrites (this day's own, not-started work) follow the new crew
        # through the loop above. Everything else — carried work from earlier days, work
        # already started — stays exactly as it was, lead or helper.
        # Anything dropped out of every team goes back to the unassigned pool — but only
        # this day's own work. A carried-over job the planner simply did not touch stays
        # exactly as it is on its own day; saving tomorrow must never un-assign yesterday.
        # ── an assignment is only undone on purpose ─────────────────────────────────
        # Every assigned job of the day that was not on a column at save time used to be
        # put back in the pool. That is why a plan could vanish: anything the board did
        # not happen to be showing — a filter, a column the planner had not scrolled to,
        # an older browser tab — was un-assigned by a save that looked routine. The board
        # now names the work the planner actually dragged back to the pool, and nothing
        # else is touched. (An older client sends nothing, and then nothing is freed.)
        want_free = {int(x) for x in (b.get("unassign") or []) if str(x).isdigit()}
        freed = 0
        for jid, job in day_jobs.items():
            if ce and jid in want_free and jid not in assigned:
                _jt = (c.execute("SELECT jobtype FROM jobs WHERE id=?", (jid,)).fetchone() or {"jobtype": ""})["jobtype"]
                if (_jt or "").upper() == "PM":
                    c.execute("UPDATE pm_elec SET tech=NULL WHERE job_id=? AND COALESCE(done_at,'')=''", (jid,))
                    freed += 1
                    continue
                if job.get("lead_tech") and _brd(jid) != "ce":
                    continue                    # the plant's crew is not the CE board's to remove
            if (not ce) and jid in want_free and job.get("lead_tech") and _brd(jid) == "ce":
                continue                        # b430: nor the CE board's crew the plant's
            if jid not in want_free or jid in assigned or not job["lead_tech"]:
                continue
            if job["status"] not in ("Reported", "WaitingAssignment", "Assigned"):
                continue                                    # started or finished work is left alone
            if job.get("started_at"):
                continue                                    # …and so is anything already begun
            c.execute("UPDATE jobs SET lead_tech=NULL, helpers='', planned_start=NULL,"
                      " planned_end=NULL, status='Reported', board='' WHERE id=?", (jid,))
            log_status(c, jid, "Reported", u["id"])
            set_stage(c, jid)
            freed += 1
        c.commit()
    for t in clean:
        if t["jobs"]:
            when = f" {t['start']}-{t['end']}" if t["start"] else ""
            notify_users(t["members"], "📋 งานที่วางแผนให้คุณ / Your work plan",
                         f"{t['name']}{when} · {d} · {len(t['jobs'])} งาน", "/")
    return {"ok": True, "teams": sum(1 for t in clean if not t.get("auto")), "jobs": n_jobs,
            "kept_own_day": n_kept_day, "unassigned": freed,
            "pm_created": made, "already_started": n_started, "no_crew": n_no_tech,
            "no_crew_teams": no_crew_names,
            "renamed": [{"from": a, "to": b_} for a, b_ in renamed],
            "dropped_ahead": dropped_ahead, "kept_ahead": kept_ahead,
            "members_ahead": members_ahead, "kept_member": kept_member}


@router.delete("/day")
async def clear_day(req: Request, d: str = ""):
    """Drop the saved crews for a day (jobs keep whatever they were last given)."""
    u = require_role(req, "planner", "admin")
    d = (d or today())[:10]
    with closing(db()) as c:
        _ensure(c)
        c.execute("DELETE FROM plan_teams WHERE factory_id=? AND day=?", (_fac(u), d))
        # cleared means back to never-planned — so the day carries its crews forward again
        c.execute("DELETE FROM plan_days WHERE factory_id=? AND day=?", (_fac(u), d))
        c.commit()
    return {"ok": True}


# ── Assigned jobs, by technician ──────────────────────────────────────────────────
OPEN_ST = ("Reported", "WaitingApproval", "WaitingAssignment", "Assigned", "Released",
           "InProgress", "Paused", "Hold", "Rework", "ServiceCompleted")


WORKING_ST = ("InProgress", "Paused", "Hold", "Rework")


def _grp(s):
    return ("done" if s == "Done" else "accept" if s == "ServiceCompleted"
            else "working" if s in WORKING_ST else "assigned")


def _tech_jobs(c, fac, d_from, d_to, open_any, done_day=None):
    """The plant's technicians and, per technician, the jobs they lead or help on.

    Window: planned day in [d_from, d_to]; with `open_any` also every unfinished job
    whatever its day; with `done_day` also every job finished on that day."""
    try:
        c.execute("CREATE INDEX IF NOT EXISTS ix_job_events_job ON job_events(job_id)")
    except Exception:
        pass
    techs = [dict(r) for r in c.execute(
        "SELECT id, name, username, COALESCE(can_login,1) cl, photo FROM users"
        " WHERE active=1 AND role='technician' AND (factory_id=? OR COALESCE(factory_id,0)=0) ORDER BY name", (fac,))]
    where = "j.planned_date BETWEEN ? AND ?"
    args = [d_from, d_to]
    if open_any:
        where += " OR j.status IN (%s)" % ",".join("?" * len(OPEN_ST))
        args += list(OPEN_ST)
    if done_day:
        where += " OR SUBSTR(COALESCE(j.done_at,''),1,10)=?"
        args.append(done_day)
    rows = [dict(r) for r in c.execute(f"""
        SELECT j.id, j.jobid, j.jobtype, j.status, j.priority, j.descr, j.planned_date,
               j.lead_tech, COALESCE(j.helpers,'') helpers, j.created_at, j.started_at,
               j.done_at, j.approved_at, j.planned_at, j.machine_id, m.code mcode, m.name mname,
               COALESCE(j.report_name,'') place
          FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
         WHERE ({where}) AND j.status NOT IN ('Cancelled','Rejected')
           AND (COALESCE(m.factory_id,j.factory_id)=? OR COALESCE(m.factory_id,j.factory_id) IS NULL)
           AND (j.lead_tech IS NOT NULL OR COALESCE(j.helpers,'')<>'')""", (*args, fac))]
    first = {}
    ids = [r["id"] for r in rows]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for e in c.execute("SELECT job_id, MIN(created_at) t FROM job_events WHERE status='Assigned'"
                           " AND job_id IN (%s) GROUP BY job_id" % ",".join("?" * len(chunk)), chunk):
            first[e["job_id"]] = e["t"]
    by = {t["id"]: [] for t in techs}
    one_person = _handset_people(c, fac)[0]
    for r in rows:
        r["assigned_at"] = first.get(r["id"]) or r.get("planned_at")
        helpers = [int(x) for x in r["helpers"].split(",") if x.strip().isdigit()]
        for who, role in [(r["lead_tech"], "lead")] + [(h, "helper") for h in helpers]:
            # a login only ONE person uses is that person: one row per technician
            # (Choke, not "Choke" + "choke · PHONE ACCT"). Reading only — jobs keep
            # whatever they were booked to.
            who = one_person.get(who, who)
            if who in by and not any(x["id"] == r["id"] for x in by[who]):
                by[who].append({**r, "role": role})
    return techs, by


@router.get("/by-tech")
async def by_tech(req: Request, d_from: str = "", d_to: str = "", open_any: int = 1):
    """Every technician of the plant, each with the jobs they are on.

    A job belongs to a technician when they LEAD it or HELP on it — the same rule the
    phone uses to decide what a login shows — so this page and the phones cannot tell
    two different stories. The window is on the job's planned day. With `open_any`,
    unfinished work from outside the window is included too (and work with no date),
    because a job still open from last week is still on that person's plate.

    For each job: when it was first ASSIGNED (the first Assigned in its history — the
    assignment that put it on someone's phone, not the latest re-save of the board),
    when the technician STARTED, when they FINISHED, and when it was ACCEPTED.
    """
    u = require_role(req, "planner", "admin", "manager")
    fac = _fac(u)
    d_to = (d_to or today())[:10]
    d_from = (d_from or d_to)[:10]
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    with closing(db()) as c:
        techs, by = _tech_jobs(c, fac, d_from, d_to, open_any)
    out = []
    for t in techs:
        jobs = sorted(by[t["id"]], key=lambda j: (j.get("planned_date") or "9999", j["jobid"] or ""))
        k = {"assigned": 0, "working": 0, "accept": 0, "done": 0}
        for j in jobs:
            k[_grp(j["status"])] += 1
        # a login account (a phone) is listed only when work is booked to it directly
        if t["cl"] and not jobs:
            continue
        out.append({"id": t["id"], "name": t["name"], "username": t["username"],
                    "photo": t["photo"] or "", "is_account": bool(t["cl"]),
                    "counts": k, "jobs": jobs})
    return {"from": d_from, "to": d_to, "open_any": bool(open_any), "techs": out}



@router.get("/by-tech/export")
async def by_tech_export(req: Request, d_from: str = "", d_to: str = "", open_any: int = 1,
                         st: str = "all", q: str = "", lang: str = "TH"):
    """The Assigned-jobs List as an .xlsx: the same filters, grouped the same way — one
    blue row per technician with his jobs under it as an Excel outline (the +/- on the
    left folds them), so the sheet opens looking like the screen."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from fastapi.responses import StreamingResponse
    u = require_role(req, "planner", "admin", "manager")
    fac = _fac(u)
    d_to = (d_to or today())[:10]
    d_from = (d_from or d_to)[:10]
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    with closing(db()) as c:
        techs, by = _tech_jobs(c, fac, d_from, d_to, open_any)
        fcode = (c.execute("SELECT code FROM factories WHERE id=?", (fac,)).fetchone() or {"code": ""})["code"]
    th = (lang or "").upper() == "TH"
    ql = (q or "").strip().lower()
    GL = {"assigned": ("มอบหมายแล้ว", "Assigned"), "working": ("กำลังทำ", "Working"),
          "accept": ("รอตรวจรับ", "To accept"), "done": ("เสร็จแล้ว", "Done")}
    STL = {"Reported": "Reported", "Assigned": "Assigned", "Released": "Assigned", "InProgress": "In progress",
           "Paused": "Paused", "Hold": "On hold", "Rework": "Rework", "ServiceCompleted": "To accept", "Done": "Done"}
    heads = [("#", 6), ("เลขงาน / Job", 16), ("เครื่อง / Machine", 14), ("ชื่อเครื่อง / Machine name", 34),
             ("ประเภท / Type", 9), ("บทบาท / Role", 12), ("สถานะ / Status", 14), ("วันที่วางแผน / Planned", 14),
             ("มอบหมาย / Assigned", 17), ("เริ่ม / Started", 17), ("เสร็จ / Finished", 17), ("ตรวจรับ / Accepted", 17)]
    wb = Workbook()
    ws = wb.active
    ws.title = "Assigned jobs"
    ws.sheet_properties.outlinePr.summaryBelow = False
    thin = Border(*(Side(style="thin", color="D4D4D4"),) * 4)
    ws.cell(1, 1, f"{fcode}  ·  {'งานที่มอบหมาย' if th else 'Assigned jobs'}  ·  {d_from} – {d_to}"
                  + ("  ·  " + ("รวมงานค้างจากวันอื่น" if th else "incl. unfinished from other days") if open_any else "")).font = Font(bold=True, size=12)
    for i, (h, w) in enumerate(heads, 1):
        cl = ws.cell(3, i, h)
        cl.fill = PatternFill("solid", fgColor="F3F3F3")
        cl.font = Font(bold=True)
        cl.border = thin
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "C4"
    gfill = PatternFill("solid", fgColor="DDEBF7")
    r = 4
    n = 0
    for t in techs:
        js = by[t["id"]]
        if t["cl"] and not js:
            continue
        shown = [j for j in js if (st in ("", "all") or _grp(j["status"]) == st) and
                 (not ql or any(ql in str(x or "").lower() for x in
                                (j["jobid"], j["mcode"], j["mname"], j["place"], j["descr"], t["name"])))]
        if not shown and (js or st not in ("", "all") or ql):
            continue
        k = {"assigned": 0, "working": 0, "accept": 0, "done": 0}
        for j in shown:
            k[_grp(j["status"])] += 1
        summ = "   ".join(f"{v} {GL[g][0 if th else 1]}" for g, v in k.items() if v) or ("ไม่มีงาน" if th else "no work")
        ws.cell(r, 1, "").fill = gfill
        ws.cell(r, 2, f"{t['name']}  ({len(shown)} {'งาน' if th else 'jobs'})")
        ws.cell(r, 7, summ)
        for col in range(1, len(heads) + 1):
            cl = ws.cell(r, col)
            cl.fill = gfill
            cl.font = Font(bold=True, color="1F4E79")
            cl.border = thin
        r += 1
        # same order as the screen: BD, CM, PM, then the rest — latest first in each
        _to = {"BD": 0, "CM": 1, "IMP": 2, "PM": 3}
        _lt = lambda j: str(j.get("done_at") or j.get("started_at") or j.get("planned_date") or j.get("created_at") or "")
        shown = sorted(shown, key=lambda j: (j["jobid"] or ""), reverse=True)
        shown = sorted(shown, key=_lt, reverse=True)
        shown = sorted(shown, key=lambda j: _to.get(str(j.get("jobtype") or "").upper(), 4))
        for i, j in enumerate(shown, 1):
            asg = j.get("assigned_at") or (("↳ ช่างรับเอง" if th else "↳ self-picked") if j.get("started_at") else "")
            vals = [i, j["jobid"], j["mcode"] or "", j["mname"] or j["place"] or (j["descr"] or "")[:60], j["jobtype"],
                    ("หัวหน้า" if th else "lead") if j["role"] == "lead" else ("ผู้ช่วย" if th else "helper"),
                    STL.get(j["status"], j["status"]), j.get("planned_date") or "",
                    str(asg or "")[:16], str(j.get("started_at") or "")[:16], str(j.get("done_at") or "")[:16],
                    str(j.get("approved_at") or "")[:16]]
            for col, v in enumerate(vals, 1):
                cl = ws.cell(r, col, v)
                cl.border = thin
                if col == 8 and v and v < today() and j["status"] not in ("Done", "ServiceCompleted"):
                    cl.font = Font(color="B91C1C", bold=True)
            ws.row_dimensions[r].outlineLevel = 1
            r += 1
            n += 1
    ws.auto_filter.ref = f"A3:{get_column_letter(len(heads))}{max(r - 1, 3)}"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fn = f"assigned-jobs-{fcode}-{d_from}_{d_to}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{fn}"'})

LOAD_BUSY, LOAD_OVER = 9, 13       # jobs on one technician's day: busy from 9, overloaded from 13
LATE_START_H = 4                   # assigned and not started this many hours into the day
ACCEPT_WAIT_H = 24                 # finished and not accepted after this many hours


def _ts(v):
    try:
        return datetime.strptime(str(v)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


@router.get("/now")
async def tech_now(req: Request):
    """Who is doing what right now, today's numbers per technician, and the warnings.

    State of a technician: WORKING when a job of theirs has a running time segment (or is
    InProgress with none — started from a handset that did not log), else PAUSED / ON
    HOLD from the job status, else IDLE. Busy time is the logged work on today's clock,
    per job, credited to its lead and every helper."""
    u = require_role(req, "planner", "admin", "manager")
    fac = _fac(u)
    t0 = today()
    nw = datetime.now()
    with closing(db()) as c:
        techs, by = _tech_jobs(c, fac, t0, t0, 1, done_day=t0)
        ids = sorted({j["id"] for js in by.values() for j in js})
        segs = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in c.execute("SELECT job_id, start, \"end\" e, pause_reason FROM timelogs"
                               " WHERE job_id IN (%s) AND (\"end\" IS NULL OR \"end\">=? )"
                               " ORDER BY start" % ",".join("?" * len(chunk)), (*chunk, t0)):
                segs.setdefault(r["job_id"], []).append(dict(r))
        # the same machine planned twice or more on one day (today .. a week ahead)
        dup = [dict(r) for r in c.execute("""
            SELECT m.code mcode, j.planned_date d, j.jobtype, COUNT(*) n, GROUP_CONCAT(j.jobid, ', ') jobids
              FROM jobs j JOIN machines m ON m.id=j.machine_id
             WHERE m.factory_id=? AND j.planned_date BETWEEN ? AND date(?, '+7 day')
               AND j.status NOT IN ('Done','ServiceCompleted','Cancelled','Rejected')
               AND j.jobtype='PM'
             GROUP BY j.machine_id, j.planned_date, j.jobtype HAVING COUNT(*)>1
             ORDER BY j.planned_date, m.code""", (fac, t0, t0))]
    day0 = datetime.strptime(t0, "%Y-%m-%d")

    def mins_today(jid):
        m = 0
        for g in segs.get(jid, []):
            a, b = _ts(g["start"]), (_ts(g["e"]) if g["e"] else nw)
            if not a or not b:
                continue
            a = max(a, day0)
            if b > a:
                m += (b - a).total_seconds() / 60
        return m

    out, late, wait, seen_l, seen_w = [], [], [], set(), set()
    for t in techs:
        js = by[t["id"]]
        if t["cl"] and not js:
            continue
        today_js = [j for j in js if j.get("planned_date") == t0 or str(j.get("done_at") or "")[:10] == t0]
        k = {"assigned": 0, "working": 0, "accept": 0, "done": 0}
        for j in today_js:
            k[_grp(j["status"])] += 1
        act = []
        for j in js:
            if j["status"] not in WORKING_ST:
                continue
            sg = segs.get(j["id"], [])
            run = next((g for g in sg if not g["e"]), None)
            if run or j["status"] in ("InProgress", "Rework"):
                st, since = "work", (run["start"] if run else j.get("started_at"))
            elif j["status"] == "Hold":
                st, since = "hold", (sg[-1]["e"] if sg else j.get("started_at"))
            else:
                st, since = "pause", (sg[-1]["e"] if sg else j.get("started_at"))
            act.append({"id": j["id"], "jobid": j["jobid"], "jobtype": j["jobtype"], "mcode": j["mcode"],
                        "mname": j["mname"] or j["place"] or (j["descr"] or "")[:40], "role": j["role"],
                        "state": st, "since": since, "reason": (sg[-1]["pause_reason"] if sg and st != "work" else "")})
        act.sort(key=lambda a: ({"work": 0, "pause": 1, "hold": 2}[a["state"]], a["since"] or ""))
        done_today = [j for j in js if str(j.get("done_at") or "")[:10] == t0]
        last = max(done_today, key=lambda j: j["done_at"]) if done_today else None
        busy = round(sum(mins_today(j["id"]) for j in js))
        nxt = sorted([j for j in js if j["status"] in ("Assigned", "Released") and (j.get("planned_date") or "") <= t0],
                     key=lambda j: (j.get("planned_date") or "", j["jobid"] or ""))
        out.append({"id": t["id"], "name": t["name"], "is_account": bool(t["cl"]),
                    "state": act[0]["state"] if act else "idle", "active": act,
                    "today": k, "total_today": len(today_js), "busy_min": busy,
                    "last_done": ({"jobid": last["jobid"], "mcode": last["mcode"], "at": last["done_at"]} if last else None),
                    "next": [{"id": j["id"], "jobid": j["jobid"], "mcode": j["mcode"]} for j in nxt[:3]],
                    "open_left": len(nxt)})
        for j in js:
            if j["status"] in ("Assigned", "Released") and (j.get("planned_date") or "9999") <= t0 and j["id"] not in seen_l:
                ref = max([x for x in (_ts(j.get("assigned_at")), _ts((j["planned_date"] or t0) + " 08:00:00")) if x])
                if (nw - ref).total_seconds() >= LATE_START_H * 3600:
                    seen_l.add(j["id"])
                    late.append({"id": j["id"], "jobid": j["jobid"], "mcode": j["mcode"], "tech": t["name"],
                                 "day": j["planned_date"], "hours": int((nw - ref).total_seconds() // 3600)})
            if j["status"] == "ServiceCompleted" and j["id"] not in seen_w:
                d = _ts(j.get("done_at"))
                if d and (nw - d).total_seconds() >= ACCEPT_WAIT_H * 3600:
                    seen_w.add(j["id"])
                    wait.append({"id": j["id"], "jobid": j["jobid"], "mcode": j["mcode"], "tech": t["name"],
                                 "done_at": j["done_at"], "days": int((nw - d).total_seconds() // 86400)})
    people = [o for o in out if not o["is_account"]]
    avg = round(sum(o["total_today"] for o in people) / len(people), 1) if people else 0
    warn = {
        "late_start": sorted(late, key=lambda x: -x["hours"]),
        "not_accepted": sorted(wait, key=lambda x: -x["days"]),
        "no_jobs": [o["name"] for o in people if not o["total_today"] and not o["active"] and not o["open_left"]],
        "overload": [{"name": o["name"], "n": o["total_today"]} for o in out if o["total_today"] >= LOAD_OVER],
        "duplicate": dup,
    }
    return {"now": nw.strftime("%Y-%m-%d %H:%M:%S"), "date": t0, "avg": avg,
            "limits": {"busy": LOAD_BUSY, "over": LOAD_OVER, "late_h": LATE_START_H, "accept_h": ACCEPT_WAIT_H},
            "techs": out, "warn": warn}


# ---------------- Day timeline: who worked on what, and when -----------------------
SHIFT_START_H = 8       # a shift day runs 08:00 -> 08:00 next morning (Morning 08-20, Night 20-08)


@router.get("/timeline")
async def tech_day(req: Request, d: str = ""):
    """One shift day, per technician: the time segments they worked, when each job was
    finished, and what was planned for them that day.

    The shift day `d` runs from d 08:00 to d+1 08:00 (Morning 08-20, Night 20-08). A
    segment is credited to the job's lead AND every helper - the same rule as the Now
    tab and the phones - falling back to the person who logged it when the job has no
    crew. The time between a segment that stopped with a reason (spare part, lunch,
    machine running...) and the next segment of the same job is returned as a wait."""
    from datetime import timedelta
    u = require_role(req, "planner", "admin", "manager")
    fac = _fac(u)
    nw = datetime.now()
    if not d:
        d = (nw - timedelta(hours=SHIFT_START_H)).strftime("%Y-%m-%d")
    d = d[:10]
    try:
        a = datetime.strptime(d, "%Y-%m-%d") + timedelta(hours=SHIFT_START_H)
    except ValueError:
        raise HTTPException(400, "bad date")
    b = a + timedelta(hours=24)
    fmt = "%Y-%m-%d %H:%M:%S"
    A, B, NW = a.strftime(fmt), b.strftime(fmt), nw.strftime(fmt)
    FAC = "(COALESCE(m.factory_id,j.factory_id)=? OR COALESCE(m.factory_id,j.factory_id) IS NULL)"
    with closing(db()) as c:
        techs = [dict(r) for r in c.execute(
            "SELECT id, name, COALESCE(can_login,1) cl FROM users"
            " WHERE active=1 AND role='technician' AND (factory_id=? OR COALESCE(factory_id,0)=0) ORDER BY name", (fac,))]
        one_person = _handset_people(c, fac)[0]
        # every segment of a job that touches the window, plus the one before it, so a
        # wait that started before 08:00 is still drawn
        jids = [r[0] for r in c.execute(f"""
            SELECT DISTINCT t.job_id FROM timelogs t JOIN jobs j ON j.id=t.job_id
              LEFT JOIN machines m ON m.id=j.machine_id
             WHERE t.start < ? AND COALESCE(t."end", ?) >= ? AND {FAC}""", (B, NW, A, fac))]
        jids += [r[0] for r in c.execute(f"""
            SELECT j.id FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
             WHERE j.done_at >= ? AND j.done_at < ? AND {FAC}""", (A, B, fac))]
        jids = sorted(set(jids))
        jobs, segs = {}, {}
        for i in range(0, len(jids), 500):
            ch = jids[i:i + 500]
            q = ",".join("?" * len(ch))
            for r in c.execute(f"""
                SELECT j.id, j.jobid, j.jobtype, j.status, j.lead_tech, COALESCE(j.helpers,'') helpers,
                       j.started_at, j.done_at, m.code mcode,
                       COALESCE(m.name, NULLIF(j.report_name,''), SUBSTR(COALESCE(j.descr,''),1,60)) mname,
                       COALESCE(j.descr,'') descr, COALESCE(j.problem,'') problem,
                       COALESCE(j.problem_type,'') ptype, COALESCE(j.root_cause,'') cause,
                       COALESCE(j.solution,'') solution
                  FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id WHERE j.id IN ({q})""", ch):
                jobs[r["id"]] = dict(r)
            for r in c.execute(f"""SELECT job_id, tech, start, "end" e, COALESCE(pause_reason,'') pr
                                    FROM timelogs WHERE job_id IN ({q}) ORDER BY start""", ch):
                segs.setdefault(r["job_id"], []).append(dict(r))
        planned = [dict(r) for r in c.execute(f"""
            SELECT j.id, j.lead_tech, COALESCE(j.helpers,'') helpers FROM jobs j
              LEFT JOIN machines m ON m.id=j.machine_id
             WHERE j.planned_date=? AND j.status NOT IN ('Cancelled','Rejected') AND {FAC}""", (d, fac))]

    per = {t["id"]: {"id": t["id"], "name": t["name"], "is_account": bool(t["cl"]), "segs": [],
                     "waits": [], "done": [], "planned": 0, "work_min": 0.0, "run_min": 0.0} for t in techs}

    def crew(j, fallback=None):
        ids = ([j["lead_tech"]] if j.get("lead_tech") else []) + _ids(j.get("helpers"))
        ids = [one_person.get(i, i) for i in ids]      # a one-person phone is that person
        ids = [i for i in dict.fromkeys(ids) if i in per]
        fallback = one_person.get(fallback, fallback)
        return ids or ([fallback] if fallback in per else [])

    def clip(x, y):
        x, y = max(x, a), min(y, b)
        return (x, y) if y > x else None

    STOP_OK = ("", "complete", "switch")
    for jid, sg in segs.items():
        j = jobs.get(jid)
        if not j:
            continue
        for k, g in enumerate(sg):
            s = _ts(g["start"])
            e = _ts(g["e"]) if g["e"] else nw
            if not s or not e:
                continue
            who = crew(j, g["tech"])
            # The person who pressed Start owns the time. The crew is the job's CURRENT
            # lead and helpers, which is not always who was there: a job taken over after
            # hold, or re-planned to another crew later, put its earlier minutes on the
            # new crew. So when the one who logged it is a technician here and is not on
            # the crew, the segment is theirs alone. Checked against the raw time logs
            # 19-25 Sept: 80 segments (6% of logged minutes) were landing on the wrong name.
            lg = one_person.get(g["tech"], g["tech"])
            if lg in per and lg not in who:
                who = [lg]
            w = clip(s, e)
            if w:
                seg = {"s": w[0].strftime(fmt), "e": w[1].strftime(fmt), "jid": jid,
                       "running": not g["e"], "stop": g["pr"]}
                for t in who:
                    per[t]["segs"].append(seg)
                    # a timer still running is shown, but not counted as work done:
                    # one left on overnight would otherwise read as a 20-hour day
                    per[t]["run_min" if not g["e"] else "work_min"] += (w[1] - w[0]).total_seconds() / 60
            if g["e"] and g["pr"] == "complete" and a <= e < b:
                for t in who:
                    per[t]["done"].append({"t": g["e"], "jid": jid})
            if g["e"] and g["pr"] not in STOP_OK:
                nxt = _ts(sg[k + 1]["start"]) if k + 1 < len(sg) else (
                    nw if j["status"] in ("Paused", "Hold") else None)
                ww = clip(e, nxt) if nxt else None
                if ww:
                    for t in who:
                        per[t]["waits"].append({"s": ww[0].strftime(fmt), "e": ww[1].strftime(fmt),
                                                "jid": jid, "reason": g["pr"]})
    # finished inside the window with no time logged at all (an older handset)
    for jid, j in jobs.items():
        dn = _ts(j.get("done_at"))
        if dn and a <= dn < b and not segs.get(jid):
            for t in crew(j):
                per[t]["done"].append({"t": j["done_at"], "jid": jid, "nolog": True})
    for p in planned:
        for t in crew(p):
            per[t]["planned"] += 1

    out = []
    for p in per.values():
        seen, dn = set(), []
        for x in sorted(p["done"], key=lambda x: x["t"]):
            if x["jid"] not in seen:
                seen.add(x["jid"])
                dn.append(x)
        p["done"] = dn
        p["segs"].sort(key=lambda x: x["s"])
        p["work_min"] = round(p["work_min"])
        p["run_min"] = round(p["run_min"])
        p["first"] = p["segs"][0]["s"] if p["segs"] else (dn[0]["t"] if dn else None)
        ends = [x["e"] for x in p["segs"] if not x["running"]] + [x["t"] for x in dn]
        p["last"] = max(ends) if ends else None
        if p["is_account"] and p["id"] in one_person:
            continue                                   # shown under the person who uses it
        if p["is_account"] and not (p["segs"] or dn or p["planned"]):
            continue
        out.append(p)
    used = {x["jid"] for p in out for k in ("segs", "waits", "done") for x in p[k]}
    return {"date": d, "start": A, "end": B, "now": NW, "shift_h": SHIFT_START_H,
            "techs": out,
            "jobs": {str(k): {**{x: v[x] for x in ("jobid", "jobtype", "status", "mcode", "mname")},
                              "issue": _issue_text(v), "fix": _fix_text(v)}
                     for k, v in jobs.items() if k in used}}


def _clip(t, n=220):
    t = " ".join(str(t or "").split())
    return t if len(t) <= n else t[:n - 1] + "…"


def _issue_text(j):
    """What was reported: the reporter's words, else the problem type. A PM job's
    description is its checklist, so only its first line (PM · Weekly · machine)."""
    d = str(j.get("descr") or "")
    if j.get("jobtype") == "PM":
        d = d.split("\n", 1)[0]
    pt = str(j.get("ptype") or "")
    if pt.startswith("อื่น") or pt.lower().startswith("other"):
        pt = ""                          # "Other — described below": the words are in descr
    parts = [x for x in (j.get("problem"), d, pt) if str(x or "").strip()]
    return _clip(" · ".join(dict.fromkeys(str(x).strip() for x in parts)))


def _fix_text(j):
    """What the technician wrote when finishing: cause and solution (a PM's summary)."""
    parts = []
    if str(j.get("cause") or "").strip():
        parts.append(str(j["cause"]).strip())
    if str(j.get("solution") or "").strip():
        parts.append(str(j["solution"]).strip())
    return _clip(" → ".join(parts), 260)

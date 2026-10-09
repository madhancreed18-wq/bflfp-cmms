# -*- coding: utf-8 -*-
"""b413 — Central Engineering (electrical) PM.

The plants' PM checklists carry two kinds of point. Most are the plant technicians'
work. The points the master workbook fills YELLOW are electrical — tightening motor
terminals, megger tests, control cabinets — and they belong to Central Engineering
(users.team = 'CE'), a group-level electrical team that covers BFL and BFLPC.

One PM job, two crews. Nothing extra is raised: the same PRM job carries both parts.

* The plant technician answers the plant points; the electrical ones are locked for
  him. His Stop needs only his own points (pm.checklist_block).
* A CE technician sees only the electrical points of that job, on his own screen, with
  his own start time and his own Submit — he does not touch the job's timer, so two
  crews on one job can never stop each other's clock.
* The job is accepted (Done) and its PM record printed only when EVERY point is
  answered. A job whose points are all electrical is closed by CE's Submit.

The electrical programme starts on ELEC_START for both plants. Work that was already
overdue on the day this build was installed ("ce:count-from") is shown but not counted
against the CE team — the team cannot be late for work that was late before it existed.
"""
from contextlib import closing
from datetime import date, timedelta

from fastapi import APIRouter, Request, HTTPException

from .db import db, now, today, job_row, log_status, set_stage, user_names, state_get
from .auth import user_from, require_role
from . import pm

router = APIRouter(prefix="/api/elec")

ELEC_START = "2026-09-17"          # user decision, 2026-10-05: BFL and BFLPC both
CE = "CE"


def is_ce(u):
    return (u.get("team") or "") == CE


def count_from(c):
    return state_get(c, "ce:count-from", "") or today()


def _fac(u):
    return u.get("active_factory") or u.get("factory_id")


def ensure(c):
    """Column + table. Called from pm._ensure, so every PM path has them."""
    try:
        c.execute("SELECT elec FROM pm_items LIMIT 1")
    except Exception:
        try:
            c.execute("ALTER TABLE pm_items ADD COLUMN elec INTEGER DEFAULT 0")
        except Exception:
            pass
    c.execute("""CREATE TABLE IF NOT EXISTS pm_elec(
        job_id INTEGER PRIMARY KEY, factory_id INTEGER, tech INTEGER,
        assigned_by INTEGER, assigned_at TEXT DEFAULT '', started_at TEXT DEFAULT '',
        done_at TEXT DEFAULT '', done_by INTEGER)""")


def remap_person(c, pid, acc):
    """b430 — a plant technician moved to Central signs in as his login (acc); his old
    person record (pid) is switched off. Everything still ahead points at the login:
    open jobs (lead and helper) and the crews on the boards from today on. Finished
    jobs and past days keep the old record — that is history."""
    from .db import today
    OPEN = "('Reported','WaitingApproval','WaitingAssignment','Assigned','Released','InProgress','Paused','Hold','Rework')"
    sw = lambda csv: ",".join(dict.fromkeys(str(acc) if x.strip() == str(pid) else x.strip()
                                            for x in str(csv or "").split(",") if x.strip()))
    n = 0
    c.execute("UPDATE jobs SET lead_tech=? WHERE lead_tech=? AND status IN " + OPEN, (acc, pid))
    for r in c.execute("SELECT id, helpers FROM jobs WHERE status IN " + OPEN +
                       " AND ','||COALESCE(helpers,'')||',' LIKE ?", (f"%,{pid},%",)).fetchall():
        c.execute("UPDATE jobs SET helpers=? WHERE id=?", (sw(r["helpers"]), r["id"]))
        n += 1
    td = today()
    # every saved crew, not only today's on: a board with no crew saved for its day
    # carries the last saved one forward, so an older day's crew is still "ahead"
    for r in c.execute("SELECT id, lead, members FROM plan_teams WHERE (lead=? OR ','||COALESCE(members,'')||',' LIKE ?)",
                       (pid, f"%,{pid},%")).fetchall():
        c.execute("UPDATE plan_teams SET lead=?, members=? WHERE id=?",
                  (acc if r["lead"] == pid else r["lead"], sw(r["members"]), r["id"]))
    try:
        c.execute("UPDATE OR IGNORE plan_lends SET tech=? WHERE tech=?", (acc, pid))
    except Exception:
        pass
    return n


def ensure_board(c):
    """b430 — which board gave a corrective job its crew: 'ce' (Central Electrical board)
    or '' (the plant's board). Since b428 a Central technician can be put on a job by
    either planner, so "led by a Central technician" no longer says whose job it is.

    One-off backfill: open non-PM jobs led by a Central technician were the CE board's —
    except those of a technician who was MOVED to Central (his old person record is
    switched off and linked to his login): that work came with him from the plant."""
    try:                                    # the column itself comes from db.py's migration list
        from .db import state_get, state_set
        if state_get(c, "jobs:board-backfill") != "done":
            moved = {r["login_id"] for r in c.execute(
                "SELECT login_id FROM users WHERE active=0 AND login_id IS NOT NULL AND role='technician'")}
            for r in c.execute("""SELECT j.id, j.lead_tech FROM jobs j JOIN users u ON u.id=j.lead_tech
                    WHERE COALESCE(u.team,'')='CE' AND UPPER(COALESCE(j.jobtype,''))<>'PM'
                      AND j.status NOT IN ('Done','Cancelled','Rejected')""").fetchall():
                if r["lead_tech"] not in moved:
                    c.execute("UPDATE jobs SET board='ce' WHERE id=?", (r["id"],))
            state_set(c, "jobs:board-backfill", "done")
            c.commit()
        if state_get(c, "users:central-plant") != "done":
            # b433: a move to Central used to file the login under ALL plants; it takes the
            # plant the technician was filed under (all plants stays all plants)
            for r in c.execute("""SELECT p.factory_id pf, a.id acc FROM users p JOIN users a ON a.id=p.login_id
                    WHERE p.active=0 AND p.role='technician' AND COALESCE(p.can_login,1)=0
                      AND COALESCE(a.team,'')='CE'""").fetchall():
                c.execute("UPDATE users SET factory_id=? WHERE id=?", (r["pf"] or 0, r["acc"]))
            state_set(c, "users:central-plant", "done")
            c.commit()
        if state_get(c, "users:central-remap") != "done":
            # technicians moved to Central before b430: point what is ahead at their login
            for r in c.execute("""SELECT p.id pid, a.id acc FROM users p JOIN users a ON a.id=p.login_id
                    WHERE p.active=0 AND p.role='technician' AND COALESCE(p.can_login,1)=0
                      AND COALESCE(a.team,'')='CE' AND a.active=1""").fetchall():
                remap_person(c, r["pid"], r["acc"])
            state_set(c, "users:central-remap", "done")
            c.commit()
        if state_get(c, "jobs:lead-not-tech") != "done":
            # b450: a planner/admin set as a job's "technician" (a project task given to its
            # planner) made a board column nobody can save into. Work not started yet goes
            # back to the pool; work somebody already started is left alone.
            c.execute("""UPDATE jobs SET lead_tech=NULL WHERE lead_tech IN
                    (SELECT id FROM users WHERE role<>'technician')
                  AND status IN ('Reported','WaitingAssignment','WaitingApproval','Assigned')""")
            c.execute("""UPDATE pm_elec SET tech=NULL WHERE tech IN
                    (SELECT id FROM users WHERE role<>'technician')
                  AND COALESCE(started_at,'')='' AND COALESCE(done_at,'')=''""")
            state_set(c, "jobs:lead-not-tech", "done")
            c.commit()
    except Exception:
        pass


def elec_items(c, ids):
    """The subset of these pm_items ids that are electrical."""
    ids = [int(i) for i in ids if i]
    out = set()
    for k in range(0, len(ids), 400):
        ch = ids[k:k + 400]
        if not ch:
            continue
        for r in c.execute("SELECT id FROM pm_items WHERE COALESCE(elec,0)=1 AND id IN (%s)"
                           % ",".join("?" * len(ch)), ch):
            out.add(r["id"])
    return out


def counts(c, fac):
    """{(template_id, freq): (all, electrical)} over the active checklist of one plant."""
    out = {}
    for r in c.execute("""SELECT i.template_id tid, i.freq freq, COUNT(*) n,
                SUM(CASE WHEN COALESCE(i.elec,0)=1 THEN 1 ELSE 0 END) e
            FROM pm_items i JOIN pm_templates t ON t.id=i.template_id
            WHERE t.factory_id=? AND COALESCE(i.active,1)=1 GROUP BY i.template_id, i.freq""", (fac,)):
        out[(r["tid"], r["freq"])] = (r["n"] or 0, r["e"] or 0)
    return out


def plant_has_elec(c, fac):
    r = c.execute("""SELECT 1 FROM pm_items i JOIN pm_templates t ON t.id=i.template_id
        WHERE t.factory_id=? AND COALESCE(i.elec,0)=1 AND COALESCE(i.active,1)=1 LIMIT 1""",
                  (fac,)).fetchone()
    return bool(r)


def state_of(c, jid):
    r = c.execute("SELECT * FROM pm_elec WHERE job_id=?", (jid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    nm = user_names(c)
    d["tech_name"] = nm.get(str(d.get("tech") or ""), "")
    d["done_name"] = nm.get(str(d.get("done_by") or ""), "")
    return d


def open_points(c, job):
    """How many electrical points of this job are still unanswered (0 = nothing owed)."""
    try:
        cl = pm.job_checklist(c, job)
    except Exception:
        cl = None
    return (cl or {}).get("el_missing_n", 0)


def accept_block(c, job):
    """Why a PM job may not be accepted yet — '' when it may."""
    if (job.get("jobtype") or "") != "PM":
        return ""
    n = open_points(c, job)
    if n:
        return ("รอช่างไฟฟ้า (วิศวกรรมส่วนกลาง) ตรวจอีก %d ข้อ — ยังตรวจรับไม่ได้ / waiting for "
                "Central Electrical: %d electrical point(s) not answered yet" % (n, n))
    return ""


def parts(c, job, cl=None):
    """b422 — the two halves of one PM job, as the planner needs to see them.

    mechanical: the plant crew's points; finished when the plant crew pressed Stop
                (status ServiceCompleted/Done) — or there are none.
    electrical: Central Electrical's points; finished when every one is answered.
    The PM record can be signed only when both are finished."""
    if (job.get("jobtype") or "") != "PM":
        return None
    if cl is None:
        cl = pm.job_checklist(c, job)
    if not cl:
        return None
    items = cl.get("items") or []
    gen = [i for i in items if not i.get("elec")]
    m_total, e_total = len(gen), cl.get("el_total", 0)
    m_done = sum(1 for i in gen if i.get("result") in ("OK", "NG"))
    m_ok = (job.get("status") in ("ServiceCompleted", "Done")) or m_total == 0
    e_done = cl.get("el_done", 0)
    st = cl.get("elec_state") or {}
    return {"m_total": m_total, "m_done": m_done, "m_ok": bool(m_ok),
            "e_total": e_total, "e_done": e_done, "e_ok": e_done >= e_total,
            "ce_name": st.get("done_name") or st.get("tech_name") or "",
            "ce_done_at": st.get("done_at") or "", "ce_started_at": st.get("started_at") or ""}


def _ce_techs(c, fac=None):
    q = ("SELECT id,name,username FROM users WHERE active=1 AND role='technician'"
         " AND COALESCE(team,'')='CE'")
    a = ()
    if fac is not None:
        q += " AND (factory_id=? OR COALESCE(factory_id,0)=0)"
        a = (fac,)
    return [{"id": r["id"], "name": r["name"] or r["username"], "username": r["username"]}
            for r in c.execute(q + " ORDER BY name", a)]


def _jobs_for(c, fac, frm, to):
    """PM jobs on these days, by (machine_id, day) — the key _make_pm_job dedups on."""
    out = {}
    for r in c.execute("""SELECT j.id,j.jobid,j.status,j.machine_id,j.planned_date,j.due_date,j.pm_freq
            FROM jobs j JOIN machines m ON m.id=j.machine_id
            WHERE j.jobtype='PM' AND m.factory_id=? AND j.status NOT IN ('Cancelled','Rejected')
              AND ((j.planned_date>=? AND j.planned_date<=?) OR (j.due_date>=? AND j.due_date<=?))""",
                       (fac, frm, to, frm, to)):
        for d in {(r["planned_date"] or "")[:10], (r["due_date"] or "")[:10]}:
            if d:
                out.setdefault((r["machine_id"], d), dict(r))
    return out


@router.get("/plan")
async def plan(req: Request, d_from: str = "", d_to: str = ""):
    """The electrical PM programme of the plant the caller is in: every PM occurrence that
    holds at least one electrical point, with the job (if raised) and the CE technician."""
    u = require_role(req, "planner", "admin", "manager", "technician")
    fac = _fac(u)
    td = today()
    frm = (d_from or ELEC_START)[:10]
    to = (d_to or (date.fromisoformat(td) + timedelta(days=31)).isoformat())[:10]
    frm = max(frm, ELEC_START)
    with closing(db()) as c:
        pm._ensure(c)
        cf = count_from(c)
        occ = pm._occurrences(c, fac, date.fromisoformat(frm), date.fromisoformat(to)) if frm <= to else []
        m2t = pm._match_all(c, fac)
        tname = {r["id"]: r["machine_type"] for r in c.execute(
            "SELECT id,machine_type FROM pm_templates WHERE factory_id=?", (fac,))}
        items = {}
        for r in c.execute("""SELECT i.template_id tid,i.freq,i.seq,i.item FROM pm_items i
                JOIN pm_templates t ON t.id=i.template_id WHERE t.factory_id=? AND COALESCE(i.elec,0)=1
                AND COALESCE(i.active,1)=1 ORDER BY i.seq""", (fac,)):
            items.setdefault((r["tid"], r["freq"]), []).append([r["seq"], r["item"]])
        cnt = counts(c, fac)
        jobs = _jobs_for(c, fac, frm, to)
        st = {r["job_id"]: dict(r) for r in c.execute("SELECT * FROM pm_elec WHERE factory_id=?", (fac,))}
        names = user_names(c)
        days, summ = {}, {"due": 0, "done": 0, "late": 0, "not_counted": 0}
        for o in occ:
            if o.get("cm"):
                continue
            tid = m2t.get(o["machine_id"])
            el = items.get((tid, o["freq"]))
            if not el:
                continue
            j = jobs.get((o["machine_id"], o["date"]))
            cl = None
            if j:
                try:
                    cl = pm.job_checklist(c, job_row(c, j["id"]))
                except Exception:
                    cl = None
            s = st.get(j["id"]) if j else None
            el_left = (cl or {}).get("el_missing_n", len(el)) if j else len(el)
            done = bool(j) and el_left == 0
            counted = o["date"] >= cf
            row = {"machine_id": o["machine_id"], "code": o["code"], "name": o["name"],
                   "group": tname.get(tid, ""), "freq": o["freq"],
                   "freq_label": pm.FREQ_LABEL.get(o["freq"], o["freq"]),
                   "n_all": (cnt.get((tid, o["freq"])) or (0, 0))[0], "n_el": len(el), "el": el,
                   "el_left": el_left, "done": done, "counted": counted,
                   "line": o.get("line") or "", "floor": o.get("floor") or "",
                   "job": ({"id": j["id"], "jobid": j["jobid"], "status": j["status"]} if j else None),
                   "tech": (s or {}).get("tech"), "tech_name": names.get(str((s or {}).get("tech") or ""), ""),
                   "started_at": (s or {}).get("started_at") or "", "done_at": (s or {}).get("done_at") or ""}
            days.setdefault(o["date"], []).append(row)
            if not counted:
                summ["not_counted"] += 1
            elif o["date"] <= td:
                summ["due"] += 1
                if done:
                    summ["done"] += 1
                elif o["date"] < td:
                    summ["late"] += 1
        techs = _ce_techs(c, fac)
    return {"factory_id": fac, "start": ELEC_START, "count_from": cf, "from": frm, "to": to,
            "days": days, "summary": summ, "techs": techs, "has_elec": bool(items)}


@router.post("/assign")
async def assign(req: Request):
    """Give the electrical part of one PM occurrence to a CE technician. Raises the PM job
    when nobody has yet — the same job the plant's planner would raise, on the same day."""
    u = require_role(req, "planner", "admin")
    b = await req.json()
    fac = _fac(u)
    try:
        mid, freq, d = int(b.get("machine_id")), str(b.get("freq") or ""), str(b.get("date") or "")[:10]
    except (TypeError, ValueError):
        raise HTTPException(400, "machine_id, freq and date are required")
    tech = b.get("tech")
    tech = int(tech) if str(tech or "").isdigit() else None
    from .teams import _make_pm_job
    with closing(db()) as c:
        pm._ensure(c)
        if tech and not c.execute("SELECT 1 FROM users WHERE id=? AND COALESCE(team,'')='CE'", (tech,)).fetchone():
            raise HTTPException(400, "เลือกได้เฉพาะช่างทีมไฟฟ้าส่วนกลาง / only a Central Electrical technician")
        m2t = pm._match_all(c, fac)
        tname = {r["id"]: r["machine_type"] for r in c.execute(
            "SELECT id,machine_type FROM pm_templates WHERE factory_id=?", (fac,))}
        jid = b.get("job_id") or _make_pm_job(c, fac, mid, freq, u["id"], d, m2t, tname)
        if not jid:
            raise HTTPException(400, "เครื่องนี้ไม่มีเช็คลิสต์ / this machine has no checklist")
        ex = c.execute("SELECT job_id FROM pm_elec WHERE job_id=?", (jid,)).fetchone()
        if ex:
            c.execute("UPDATE pm_elec SET tech=?, assigned_by=?, assigned_at=? WHERE job_id=?",
                      (tech, u["id"], now(), jid))
        else:
            c.execute("INSERT INTO pm_elec(job_id,factory_id,tech,assigned_by,assigned_at) VALUES(?,?,?,?,?)",
                      (jid, fac, tech, u["id"], now()))
        try:
            from .chat import log_job_event
            who = user_names(c).get(str(tech or ""), "")
            log_job_event(c, jid, u["id"], ("⚡ งานไฟฟ้า → %s / electrical part → %s" % (who, who)) if tech
                          else "⚡ งานไฟฟ้า — ยกเลิกการมอบหมาย / electrical part unassigned")
        except Exception:
            pass
        c.commit()
        if tech:
            try:
                from .push import notify_users
                j = job_row(c, jid)
                notify_users([tech], "⚡ งาน PM ไฟฟ้า / Electrical PM", f"{j['jobid']} {j.get('mcode') or ''}",
                             f"/?elec={jid}")
            except Exception:
                pass
    return {"ok": True, "job_id": jid}


@router.get("/mine")
async def mine(req: Request):
    """A CE technician's list in the plant he is signed into: what he was given, then
    the electrical work nobody has been given yet (from the start of the programme)."""
    u = require_role(req, "technician", "planner", "admin")
    fac = _fac(u)
    td = today()
    with closing(db()) as c:
        pm._ensure(c)
        cf = count_from(c)
        rows = c.execute("""SELECT j.id FROM jobs j JOIN machines m ON m.id=j.machine_id
            WHERE j.jobtype='PM' AND m.factory_id=? AND j.status NOT IN ('Cancelled','Rejected','Done')
              AND COALESCE(NULLIF(j.planned_date,''),j.due_date) >= ?
              AND COALESCE(NULLIF(j.planned_date,''),j.due_date) <= ?
            ORDER BY COALESCE(NULLIF(j.planned_date,''),j.due_date), j.id""",
                         (fac, ELEC_START, (date.fromisoformat(td) + timedelta(days=7)).isoformat())).fetchall()
        st = {r["job_id"]: dict(r) for r in c.execute("SELECT * FROM pm_elec WHERE factory_id=?", (fac,))}
        names = user_names(c)
        mineL, freeL, doneL = [], [], []
        me = u["id"]
        for r in rows:
            j = job_row(c, r["id"])
            cl = pm.job_checklist(c, j)
            if not cl or not cl.get("el_total"):
                continue
            s = st.get(j["id"]) or {}
            d = (j.get("planned_date") or j.get("due_date") or "")[:10]
            row = {"id": j["id"], "jobid": j["jobid"], "status": j["status"], "date": d,
                   "mcode": j.get("mcode") or "", "mname": j.get("mname") or "",
                   "freq": cl["freq"], "label": cl["label"], "el_total": cl["el_total"],
                   "el_left": cl["el_missing_n"], "gen_total": cl["total"] - cl["el_total"],
                   "tech": s.get("tech"), "tech_name": names.get(str(s.get("tech") or ""), ""),
                   "started_at": s.get("started_at") or "", "late": d < td, "counted": d >= cf}
            if s.get("done_at"):
                continue                     # stopped: listed under Completed below
            if not cl["el_missing_n"] and not s.get("started_at"):
                continue                     # answered before Central Electrical existed
            if s.get("tech") == me:
                mineL.append(row)
            elif not s.get("tech"):
                freeL.append(row)
        # b423: the CE technician's Completed tab — his stopped electrical parts, waiting
        # for the planner (job not accepted yet), plus the last 7 days of accepted ones
        wk = (date.fromisoformat(td) - timedelta(days=7)).isoformat()
        mon = td[:7] + "-01"
        kpi = {"done": 0, "ontime": 0, "late": 0, "open_late": sum(1 for r in mineL if r["late"] and r["counted"])}
        for s in c.execute("""SELECT e.*, j.jobid, j.status, j.planned_date, j.due_date, j.pm_freq,
                    m.code mcode, m.name mname
                FROM pm_elec e JOIN jobs j ON j.id=e.job_id JOIN machines m ON m.id=j.machine_id
                WHERE m.factory_id=? AND COALESCE(e.done_at,'')<>'' AND (e.done_by=? OR e.tech=?)
                ORDER BY e.done_at DESC""", (fac, me, me)):
            s = dict(s)
            d = (s.get("planned_date") or s.get("due_date") or "")[:10]
            on = s["done_at"][:10] <= d if d else True
            if s["done_at"][:10] >= mon and d >= cf:
                kpi["done"] += 1
                kpi["ontime" if on else "late"] += 1
            if s["status"] == "Done" and s["done_at"][:10] < wk:
                continue
            doneL.append({"id": s["job_id"], "jobid": s["jobid"], "status": s["status"], "date": d,
                          "mcode": s.get("mcode") or "", "mname": s.get("mname") or "",
                          "freq": s.get("pm_freq") or "", "done_at": s["done_at"], "ontime": on,
                          "accepted": s["status"] == "Done", "plant_done": s["status"] in ("ServiceCompleted", "Done")})
    return {"mine": mineL, "open": freeL, "done_today": doneL, "completed": doneL, "kpi": kpi, "count_from": cf}


@router.post("/{jid}/start")
async def start(jid: int, req: Request):
    """The CE technician is at the machine. Only now can the electrical points be ticked —
    the same rule the plant technician's Start applies to his."""
    u = user_from(req)
    if u["role"] not in ("technician", "planner", "admin") or (u["role"] == "technician" and not is_ce(u)):
        raise HTTPException(403, "เฉพาะช่างไฟฟ้าส่วนกลาง / Central Electrical technicians only")
    with closing(db()) as c:
        pm._ensure(c)
        from .jobs import _job_in_factory
        j = _job_in_factory(c, jid, u)
        if (j.get("jobtype") or "") != "PM":
            raise HTTPException(400, "not a PM job")
        if j["status"] in ("Done", "Rejected", "Cancelled"):
            raise HTTPException(409, "งานนี้ปิดแล้ว / this job is closed")
        fac = c.execute("SELECT factory_id FROM machines WHERE id=?", (j["machine_id"],)).fetchone()
        fac = fac["factory_id"] if fac else _fac(u)
        s = c.execute("SELECT * FROM pm_elec WHERE job_id=?", (jid,)).fetchone()
        if s and s["tech"] and s["tech"] != u["id"] and u["role"] == "technician":
            who = user_names(c).get(str(s["tech"]), "")
            raise HTTPException(409, f"งานไฟฟ้านี้มอบหมายให้ {who} / this electrical part is assigned to {who}")
        if s:
            c.execute("UPDATE pm_elec SET tech=COALESCE(tech,?), started_at=CASE WHEN COALESCE(started_at,'')='' THEN ? ELSE started_at END WHERE job_id=?",
                      (u["id"], now(), jid))
        else:
            c.execute("INSERT INTO pm_elec(job_id,factory_id,tech,assigned_by,assigned_at,started_at) VALUES(?,?,?,?,?,?)",
                      (jid, fac, u["id"], u["id"], now(), now()))
        try:
            from .chat import log_job_event
            log_job_event(c, jid, u["id"], "⚡ เริ่มตรวจงานไฟฟ้า / electrical check started — "
                          + (user_names(c).get(str(u["id"]), u.get("name") or "")))
        except Exception:
            pass
        c.commit()
    return {"ok": True}


@router.post("/{jid}/done")
async def done(jid: int, req: Request):
    """Submit the electrical part. Every electrical point answered, a remark on every NG.
    When the job has no plant points at all, this closes the job (ServiceCompleted)."""
    u = user_from(req)
    if u["role"] not in ("technician", "planner", "admin") or (u["role"] == "technician" and not is_ce(u)):
        raise HTTPException(403, "เฉพาะช่างไฟฟ้าส่วนกลาง / Central Electrical technicians only")
    with closing(db()) as c:
        pm._ensure(c)
        from .jobs import _job_in_factory
        j = _job_in_factory(c, jid, u)
        cl = pm.job_checklist(c, j)
        if not cl or not cl.get("el_total"):
            raise HTTPException(400, "งานนี้ไม่มีข้อไฟฟ้า / this job has no electrical points")
        if cl["el_missing"]:
            raise HTTPException(409, "ยังตรวจไม่ครบ ข้อ %s / not answered yet: no. %s"
                                % (", ".join(map(str, cl["el_missing"])), ", ".join(map(str, cl["el_missing"]))))
        if cl["el_ng_no_remark"]:
            raise HTTPException(409, "ข้อที่ NG ต้องใส่หมายเหตุ — ข้อ %s / an NG needs a remark — no. %s"
                                % (", ".join(map(str, cl["el_ng_no_remark"])), ", ".join(map(str, cl["el_ng_no_remark"]))))
        fac = c.execute("SELECT factory_id FROM machines WHERE id=?", (j["machine_id"],)).fetchone()
        fac = fac["factory_id"] if fac else _fac(u)
        if c.execute("SELECT 1 FROM pm_elec WHERE job_id=?", (jid,)).fetchone():
            c.execute("UPDATE pm_elec SET done_at=?, done_by=?, tech=COALESCE(tech,?) WHERE job_id=?",
                      (now(), u["id"], u["id"], jid))
        else:
            c.execute("INSERT INTO pm_elec(job_id,factory_id,tech,assigned_by,assigned_at,started_at,done_at,done_by)"
                      " VALUES(?,?,?,?,?,?,?,?)", (jid, fac, u["id"], u["id"], now(), now(), now(), u["id"]))
        names = user_names(c)
        who = names.get(str(u["id"]), u.get("name") or "")
        from .chat import log_job_event
        el_ng = sum(1 for i in cl["items"] if i.get("elec") and i["result"] == "NG")
        log_job_event(c, jid, u["id"], f"⚡ งานไฟฟ้าเสร็จ / electrical part done — {who} · "
                                       f"OK {cl['el_total'] - el_ng} · NG {el_ng}")
        closed = False
        # all-electrical job: the CE Submit is the job's finish
        if cl["total"] == cl["el_total"] and j["status"] not in ("ServiceCompleted", "Done"):
            c.execute("UPDATE jobs SET status='ServiceCompleted', progress=100, done_at=COALESCE(done_at,?),"
                      " started_at=COALESCE(started_at,?) WHERE id=?", (now(), now(), jid))
            rec = f"{j.get('mcode') or j['jobid']}_{now()[:10]}"
            note = f"PM {cl['label']} · {cl['total']} รายการ · OK {cl['total'] - cl['ng']} · NG {cl['ng']}"
            c.execute("UPDATE jobs SET report_name=?, solution=? WHERE id=?", (rec[:120], note, jid))
            log_status(c, jid, "ServiceCompleted", u["id"])
            set_stage(c, jid)
            closed = True
        c.commit()
        # whoever accepts PM in this plant is told when the job is now ready to accept
        row = job_row(c, jid)
        if row["status"] == "ServiceCompleted":
            try:
                from .push import notify_users
                pl = [r["id"] for r in c.execute(
                    "SELECT id FROM users WHERE role IN ('planner','admin') AND active=1 AND COALESCE(team,'')<>'CE'"
                    " AND (factory_id=? OR COALESCE(factory_id,0)=0)", (fac,))]
                if pl:
                    notify_users(pl, "PM completed - please accept", f"{row['jobid']} {row.get('mcode') or ''}",
                                 f"/?job={jid}")
            except Exception:
                pass
    return {"ok": True, "closed": closed}


# ── b417: what a Central Electrical PLANNER works from ───────────────────────────
# Decided with the plant 2026-10-05: the CE planner's Jobs page and CM Plan show the
# work whose trade is Electrical, Other or not yet known — the plant and CE planners
# both see "Other / unknown" and whoever gives it a crew first has it — plus the PM
# jobs that carry an electrical point. Mechanical, Instrument and Process stay with the
# plant.
CE_TRADES = ("Electrical", "Other", "")


def is_ce_planner(u):
    return is_ce(u) and u.get("role") == "planner"


def ce_ids(c, fac=None):
    """Central technicians — with `fac`, only those filed under that plant or all plants
    (b433: a Central technician filed under BFLPC is not offered on the BFL board)."""
    if fac is None:
        return {r["id"] for r in c.execute(
            "SELECT id FROM users WHERE COALESCE(team,'')='CE' AND role='technician'")}
    return {r["id"] for r in c.execute(
        "SELECT id FROM users WHERE COALESCE(team,'')='CE' AND role='technician' AND active=1"
        " AND (factory_id=? OR COALESCE(factory_id,0)=0)", (fac,))}


class CeFilter:
    """ok(job) — does this job belong on a CE planner's screens? Caches per plant."""

    def __init__(self, c):
        self.c = c
        self._pt, self._cnt, self._m2t, self._mf = {}, {}, {}, {}

    def _fac_of(self, j):
        mid = j.get("machine_id")
        if mid:
            if mid not in self._mf:
                r = self.c.execute("SELECT factory_id FROM machines WHERE id=?", (mid,)).fetchone()
                self._mf[mid] = r["factory_id"] if r else None
            return self._mf[mid]
        return j.get("factory_id")

    def trade(self, j):
        fac = self._fac_of(j)
        if fac not in self._pt:
            self._pt[fac] = {r["name"]: (r["category"] or "") for r in self.c.execute(
                "SELECT name,category FROM problem_types WHERE factory_id=?", (fac,))}
        return self._pt[fac].get(j.get("problem_type") or "") or (j.get("fault_category") or "")

    def pm_elec(self, j):
        fac = self._fac_of(j)
        if fac not in self._cnt:
            self._cnt[fac] = counts(self.c, fac)
            from . import pm as _pm
            self._m2t[fac] = _pm._match_all(self.c, fac)
        tid = self._m2t[fac].get(j.get("machine_id"))
        return (self._cnt[fac].get((tid, (j.get("pm_freq") or "weekly"))) or (0, 0))

    def ok(self, j):
        if (j.get("jobtype") or "").upper() == "PM":
            return self.pm_elec(j)[1] > 0
        return self.trade(j) in CE_TRADES


# ── b421: the other side — the plants' own planners ───────────────────────────────
# Decided 2026-10-07: on BFL and BFLPC (the plants Central Electrical covers) the
# plant's planners and managers see every job EXCEPT corrective work whose trade is
# Electrical — that is Central Electrical's. Other / unknown stays visible to both.
# PM jobs stay: the plant crew still does their plant points. BFLFP is unchanged.
CE_PLANTS = (1, 3)


def plant_ok(f, j):
    if (j.get("jobtype") or "").upper() == "PM":
        return True
    if f._fac_of(j) not in CE_PLANTS:
        return True
    return f.trade(j) != "Electrical"


def on_crew(job, u):
    """b428 — is this login on the job's PLANT crew (lead or helper)? A Central technician
    the plant planner put on a job works it like the plant crew does: the whole sheet,
    on the job's own Start/Stop."""
    try:
        from .db import db as _db
        from contextlib import closing as _cl
        from .jobs import _tech_ids
        with _cl(_db()) as _c:
            mine = set(_tech_ids(_c, u))          # this login and the people booked through it
    except Exception:
        mine = {int(u.get("id") or 0)}
    me = int(u.get("id") or 0)
    ids = {int(job.get("lead_tech") or 0)} | {int(x) for x in str(job.get("helpers") or "")
                                             .replace(";", ",").split(",") if x.strip().isdigit()}
    return bool(me) and bool(ids & mine)


def all_elec(f, j):
    """b423 — a PM job with ONLY electrical points (e.g. PRM-2609-626, one point: tighten
    the motor terminals). The plant crew has nothing to do on it, so it is not theirs to
    carry in My jobs or in their open-job count; Central Electrical's Stop closes it."""
    if (j.get("jobtype") or "").upper() != "PM":
        return False
    d = (j.get("planned_date") or j.get("due_date") or "")[:10]
    if d and d < ELEC_START:
        return False
    n, e = f.pm_elec(j)
    return n > 0 and n == e


def scope_of(c, u):
    """The job filter a user's lists go through, or None for no filter."""
    role = u.get("role")
    if role == "technician" and not is_ce(u):
        f = CeFilter(c)
        return lambda j: not all_elec(f, j)
    if is_ce_planner(u):
        f = CeFilter(c)
        return f.ok
    if role in ("planner", "manager") and not is_ce(u):
        f = CeFilter(c)
        return lambda j: plant_ok(f, j)
    return None

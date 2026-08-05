import os, base64
from datetime import datetime, date
from contextlib import closing

from fastapi import APIRouter, Request, HTTPException

from .config import UPLOADS, BASE
from .db import db, now, today, day_range, job_row, role_ids, user_names
from .auth import user_from
from .push import notify_users

router = APIRouter(prefix="/api")

ACTIVE = ("('Reported','WaitingApproval','WaitingAssignment','Assigned',"
          "'Released','InProgress','Paused','Rework','ServiceCompleted','Hold')")


@router.get("/jobs")
async def jobs(req: Request, view: str = "", d: str = "", q_text: str = ""):
    u = user_from(req)
    d = d or today()
    q = """SELECT j.*, m.code mcode, m.name mname, u.name lead_name
           FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
           LEFT JOIN users u ON u.id=j.lead_tech"""
    args = []
    if view == "pool":
        q += " WHERE j.status IN ('Reported','WaitingApproval','WaitingAssignment','Assigned','Hold') OR (j.planned_date=?)"
        args = [d]
    elif view == "myday":
        q += """ WHERE j.planned_date=? AND j.status IN ('Released','InProgress','Paused','Rework','ServiceCompleted')
                 AND (j.lead_tech=? OR ','||j.helpers||',' LIKE '%,'||?||',%')"""
        args = [d, u["id"], str(u["id"])]
    elif view == "mine":
        q += " WHERE j.created_by=?"
        args = [u["id"]]
    elif view == "recent":
        from datetime import date as _d, timedelta as _td
        start = (_d.fromisoformat(d) - _td(days=9)).isoformat() + " 00:00:00"
        q += f" WHERE j.status IN {ACTIVE} AND j.created_at >= ?"
        args = [start]
    elif view == "overdue":
        q += f" WHERE j.status IN {ACTIVE} AND j.due_date IS NOT NULL AND j.due_date < ?"
        args = [d]
    elif view == "duetoday":
        q += f" WHERE j.status IN {ACTIVE} AND j.due_date = ?"
        args = [d]
    elif view == "assigned":
        q += " WHERE j.status IN ('Assigned','Released','InProgress','Paused','Rework')"
    elif view == "history":
        from datetime import date as _d, timedelta as _td
        h0 = (_d.fromisoformat(d) - _td(days=9)).isoformat() + " 00:00:00"
        h1 = (_d.fromisoformat(d) + _td(days=1)).isoformat() + " 23:59:59"
        q += " WHERE j.status IN ('Done','Rejected') AND j.created_at >= ? AND j.created_at <= ?"
        args = [h0, h1]
    if q_text:
        q += (" AND" if "WHERE" in q else " WHERE") + \
             " (j.jobid LIKE ? OR j.descr LIKE ? OR m.code LIKE ? OR m.name LIKE ?)"
        args += [f"%{q_text}%"] * 4
    q += " ORDER BY j.priority DESC, j.planned_start IS NULL, j.planned_start, j.id DESC"
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute(q, args)]
        names = user_names(c)
        for r in rows:
            r["helper_names"] = ", ".join(
                names.get(h, "") for h in (r["helpers"] or "").split(",") if h)
        open_seg = c.execute(
            "SELECT id, job_id, seg_type, activity, start FROM timelogs WHERE tech=? AND end IS NULL",
            (u["id"],)).fetchone()
    return {"jobs": rows, "open_segment": dict(open_seg) if open_seg else None}


@router.post("/jobs")
async def create_job(req: Request):
    u = user_from(req)
    b = await req.json()
    jt = b.get("jobtype", "CM")
    with closing(db()) as c:
        ym = datetime.now().strftime("%y%m")
        seq = c.execute("SELECT COUNT(*) FROM jobs WHERE jobid LIKE ?",
                        (f"PRD-{ym}-%",)).fetchone()[0] + 1
        jobid = f"PRD-{ym}-{seq:03d}"
        if u["role"] == "operator":
            status = "WaitingApproval" if b.get("need_approval") else "Reported"
            source = "OperatorReport"
        else:
            status = b.get("status", "Assigned")
            source = "Planner"
        new_id = c.insert_id("""INSERT INTO jobs(jobid,jobtype,machine_id,descr,priority,status,
            planned_date,planned_start,planned_end,lead_tech,due_date,jobsource,
            production_impact,created_by,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (jobid, jt, b.get("machine_id"), b.get("descr", ""), b.get("priority", 1), status,
             b.get("planned_date"), b.get("planned_start"), b.get("planned_end"),
             b.get("lead_tech"), b.get("due_date"), source,
             b.get("production_impact", ""), u["id"], now()))
        c.commit()
        row = job_row(c, new_id)
        if jt == "BD" or int(b.get("priority", 1)) == 3:
            notify_users(role_ids(c, "technician", "planner"), "🔴 Breakdown!",
                         f"{row['jobid']} {row['mcode'] or ''}: {row['descr'][:80]}")
            from .chat import post_system
            post_system(c, "system",
                        f"🔴 {row['jobid']} {row['mcode'] or ''}: {row['descr'][:120]}",
                        job_id=row["id"], author=u["id"])
            c.commit()
        elif u["role"] == "operator":
            notify_users(role_ids(c, "planner"), "งานแจ้งซ่อมใหม่",
                         f"{row['jobid']} {row['mcode'] or ''}: {row['descr'][:80]}")
        return row


@router.patch("/jobs/{jid}")
async def update_job(jid: int, req: Request):
    u = user_from(req)
    b = await req.json()
    allowed = ["status", "planned_date", "planned_start", "planned_end", "lead_tech",
               "helpers", "priority", "progress", "pending_reason", "problem",
               "root_cause", "solution", "descr", "carryover", "due_date",
               "cleared_worksite", "new_issue_id", "production_impact"]
    sets = {k: b[k] for k in allowed if k in b}
    if not sets:
        raise HTTPException(400, "nothing to update")
    with closing(db()) as c:
        c.execute(f"UPDATE jobs SET {','.join(k + '=?' for k in sets)} WHERE id=?",
                  (*sets.values(), jid))
        st = sets.get("status")
        if st == "Rework":
            c.execute("UPDATE jobs SET rework_count=rework_count+1 WHERE id=?", (jid,))
        if st in ("ServiceCompleted", "Done"):
            c.execute("UPDATE jobs SET done_at=? WHERE id=? AND done_at IS NULL", (now(), jid))
        c.commit()
        row = job_row(c, jid)
        if st:
            from .chat import log_job_event
            log_job_event(c, jid, u["id"], f"⚙️ สถานะ → {st} (โดย {u['name']})")
            c.commit()
        techs = [t for t in [row["lead_tech"],
                             *[int(h) for h in (row["helpers"] or "").split(",") if h]] if t]
        if st == "Released":
            notify_users(techs, "📋 งานใหม่ถึงคุณ",
                         f"{row['jobid']} {row['mcode'] or ''} {row['planned_start'] or ''}-{row['planned_end'] or ''}")
        elif st == "Rework":
            notify_users(techs, "↩ งานถูกส่งกลับแก้ไข", f"{row['jobid']} {row['mcode'] or ''}")
        elif st in ("Done", "Rejected"):
            notify_users(techs, f"ผลตรวจงาน: {st}", f"{row['jobid']} {row['mcode'] or ''}")
        return row


@router.get("/jobs/{jid}/detail")
async def job_detail(jid: int, req: Request):
    user_from(req)
    with closing(db()) as c:
        row = job_row(c, jid)
        names = user_names(c)
        row["helper_names"] = ", ".join(
            names.get(h, "") for h in (row["helpers"] or "").split(",") if h)
        row["creator_name"] = names.get(str(row["created_by"]), "")
        segs = [dict(r) for r in c.execute("""SELECT t.*, u.name tech_name FROM timelogs t
            JOIN users u ON u.id=t.tech WHERE t.job_id=? ORDER BY t.start""", (jid,))]
    return {"job": row, "segments": segs}


@router.post("/jobs/{jid}/media")
async def job_media(jid: int, req: Request):
    user_from(req)
    b = await req.json()
    kind, data = b.get("kind"), b.get("data", "")
    col = {"before": "img_before", "after": "img_after",
           "sign_requester": "sign_requester", "sign_inspector": "sign_inspector"}.get(kind)
    if not col or "," not in data:
        raise HTTPException(400, "bad media")
    head, b64 = data.split(",", 1)
    ext = "png" if "png" in head else "jpg"
    fn = f"{jid}_{kind}.{ext}"
    with open(os.path.join(UPLOADS, fn), "wb") as f:
        f.write(base64.b64decode(b64))
    with closing(db()) as c:
        c.execute(f"UPDATE jobs SET {col}=? WHERE id=?", (f"/uploads/{fn}", jid))
        c.commit()
    return {"path": f"/uploads/{fn}"}


@router.post("/jobs/{jid}/reissue")
async def job_reissue(jid: int, req: Request):
    u = user_from(req)
    with closing(db()) as c:
        old = job_row(c, jid)
        ym = datetime.now().strftime("%y%m")
        seq = c.execute("SELECT COUNT(*) FROM jobs WHERE jobid LIKE ?",
                        (f"PRD-{ym}-%",)).fetchone()[0] + 1
        newid = f"PRD-{ym}-{seq:03d}"
        c.insert_id("""INSERT INTO jobs(jobid,jobtype,machine_id,descr,priority,status,
            jobsource,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (newid, old["jobtype"], old["machine_id"],
             f"[Re-issue {old['jobid']}] {old['descr']}", old["priority"], "Reported",
             old["jobsource"], u["id"], now()))
        c.execute("UPDATE jobs SET new_issue_id=? WHERE id=?", (newid, jid))
        c.commit()
    return {"new_jobid": newid}


def close_open_segment(c, tech, reason=""):
    c.execute("UPDATE timelogs SET end=?, pause_reason=? WHERE tech=? AND end IS NULL",
              (now(), reason, tech))


@router.post("/segments/start")
async def seg_start(req: Request):
    u = user_from(req)
    b = await req.json()
    with closing(db()) as c:
        close_open_segment(c, u["id"], b.get("pause_reason", "switch"))
        c.execute("INSERT INTO timelogs(job_id,tech,seg_type,activity,start) VALUES(?,?,?,?,?)",
                  (b.get("job_id"), u["id"], b.get("seg_type", "work"), b.get("activity", ""), now()))
        if b.get("job_id"):
            c.execute("UPDATE jobs SET status='InProgress' WHERE id=?", (b["job_id"],))
        c.commit()
    return {"ok": True}


@router.post("/segments/stop")
async def seg_stop(req: Request):
    u = user_from(req)
    b = await req.json()
    action = b.get("action", "pause")
    with closing(db()) as c:
        seg = c.execute("SELECT * FROM timelogs WHERE tech=? AND end IS NULL",
                        (u["id"],)).fetchone()
        close_open_segment(c, u["id"], b.get("reason", ""))
        if seg and seg["job_id"]:
            if action == "finish":
                c.execute("UPDATE jobs SET done_at=? WHERE id=? AND done_at IS NULL",
                          (now(), seg["job_id"]))
                c.execute("""UPDATE jobs SET status='ServiceCompleted', progress=100,
                    problem=?, root_cause=?, solution=?,
                    fault_category=?, fault_component=?, maint_action=? WHERE id=?""",
                    (b.get("problem", ""), b.get("root_cause", ""), b.get("solution", ""),
                     b.get("fault_category", ""), b.get("fault_component", ""),
                     b.get("maint_action", ""), seg["job_id"]))
                row = job_row(c, seg["job_id"])
                from .chat import log_job_event
                log_job_event(c, seg["job_id"], u["id"],
                              f"⚙️ สถานะ → ServiceCompleted (โดย {u['name']})")
                notify_users(role_ids(c, "planner"), "✅ งานเสร็จ รอตรวจรับ",
                             f"{row['jobid']} {row['mcode'] or ''} โดย {u['name']}")
            else:
                c.execute("UPDATE jobs SET status='Paused', progress=?, pending_reason=? WHERE id=?",
                          (b.get("progress", 0), b.get("reason", ""), seg["job_id"]))
        c.commit()
    return {"ok": True}


@router.get("/dashboard")
async def dashboard(req: Request, d: str = ""):
    user_from(req)
    d = d or today()
    with closing(db()) as c:
        planned = [dict(r) for r in c.execute("""SELECT j.*, m.code mcode, u.name lead_name
            FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
            LEFT JOIN users u ON u.id=j.lead_tech
            WHERE j.planned_date=? AND j.jobtype IN ('PM','CM','IMP')
            AND j.status NOT IN ('Cancelled')""", (d,))]
        d0, d1 = day_range(d)
        breakdowns = [dict(r) for r in c.execute("""SELECT j.*, m.code mcode, u.name lead_name
            FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
            LEFT JOIN users u ON u.id=j.lead_tech
            WHERE j.jobtype='BD' AND j.created_at >= ? AND j.created_at <= ?""", (d0, d1))]
        segs = [dict(r) for r in c.execute("""SELECT t.*, u.name tech_name, j.jobid, j.jobtype
            FROM timelogs t JOIN users u ON u.id=t.tech
            LEFT JOIN jobs j ON j.id=t.job_id
            WHERE t.start >= ? AND t.start <= ? ORDER BY t.tech, t.start""", (d0, d1))]
    rel = [j for j in planned if j["status"] in
           ("Released", "InProgress", "Paused", "Rework", "ServiceCompleted", "Done")]
    done_planned = [j for j in rel if j["status"] in ("ServiceCompleted", "Done")]
    done_bd = [j for j in breakdowns if j["status"] in ("ServiceCompleted", "Done")]
    pct = lambda a, b: round(a / b * 100) if b else None
    return {
        "date": d,
        "planned_total": len(rel), "planned_done": len(done_planned),
        "planned_pct": pct(len(done_planned), len(rel)),
        "bd_total": len(breakdowns), "bd_done": len(done_bd),
        "bd_pct": pct(len(done_bd), len(breakdowns)),
        "total_pct": pct(len(done_planned) + len(done_bd), len(rel) + len(breakdowns)),
        "planned": planned, "breakdowns": breakdowns, "segments": segs,
        "pending": [j for j in planned + breakdowns
                    if j["status"] in ("Paused", "Hold") or
                    (j["status"] in ("Released", "Assigned") and j.get("pending_reason"))],
    }

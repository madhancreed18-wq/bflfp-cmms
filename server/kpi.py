"""Shift logs + KPI engine (OEE / MTBF / MTTR per OPERATIONS-DESIGN.md §4)
and PM auto-recurrence (§ PM flow)."""
from datetime import datetime, date, timedelta
from contextlib import closing

from fastapi import APIRouter, Request, HTTPException

from .db import db, now, today, day_range
from .auth import user_from
from .push import notify_users

router = APIRouter(prefix="/api")


# ---------------- shift logs ----------------

@router.post("/shiftlogs")
async def save_shiftlog(req: Request):
    u = user_from(req)
    b = await req.json()
    if not b.get("machine_id"):
        raise HTTPException(400, "machine_id required")
    d = b.get("log_date") or today()
    shift = b.get("shift") or "เช้า"
    output = int(b.get("output") or 0)
    good = min(int(b.get("good") or 0), output)
    with closing(db()) as c:
        upd = c.execute("""UPDATE shiftlogs SET planned_min=?, output=?, good=?, reject=?, entered_by=?
            WHERE log_date=? AND shift=? AND machine_id=?""",
            (int(b.get("planned_min") or 0), output, good, output - good, u["id"],
             d, shift, b["machine_id"]))
        if upd.rowcount == 0:
            c.execute("""INSERT INTO shiftlogs(log_date,shift,machine_id,planned_min,output,good,reject,entered_by,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (d, shift, b["machine_id"], int(b.get("planned_min") or 0),
                 output, good, output - good, u["id"], now()))
        c.commit()
    return {"ok": True}


@router.get("/shiftlogs")
async def list_shiftlogs(req: Request, d: str = ""):
    user_from(req)
    d = d or today()
    with closing(db()) as c:
        return [dict(r) for r in c.execute("""SELECT s.*, m.code mcode, u.name entered_name
            FROM shiftlogs s JOIN machines m ON m.id=s.machine_id
            LEFT JOIN users u ON u.id=s.entered_by
            WHERE s.log_date=? ORDER BY s.shift, m.code""", (d,))]


# ---------------- KPI engine ----------------

def _minutes(a, b):
    try:
        t0 = datetime.strptime(a, "%Y-%m-%d %H:%M:%S")
        t1 = datetime.strptime(b, "%Y-%m-%d %H:%M:%S") if b else datetime.now()
        return max(0.0, (t1 - t0).total_seconds() / 60)
    except Exception:
        return 0.0


@router.get("/kpi")
async def kpi(req: Request, d_from: str = "", d_to: str = ""):
    user_from(req)
    d_to = d_to or today()
    d_from = d_from or (date.fromisoformat(d_to) - timedelta(days=6)).isoformat()
    with closing(db()) as c:
        machines = {m["id"]: dict(m) for m in c.execute(
            "SELECT * FROM machines WHERE active=1")}
        prod = {r["machine_id"]: dict(r) for r in c.execute("""
            SELECT machine_id, SUM(planned_min) pm, SUM(output) out, SUM(good) gd
            FROM shiftlogs WHERE log_date BETWEEN ? AND ? GROUP BY machine_id""",
            (d_from, d_to))}
        f0, t1 = d_from + " 00:00:00", d_to + " 23:59:59"
        bd_jobs = [dict(r) for r in c.execute("""
            SELECT id, machine_id, created_at, done_at, status FROM jobs
            WHERE jobtype='BD' AND created_at >= ? AND created_at <= ?""", (f0, t1))]
        repair = {}
        response = {}
        seg_rows = [dict(r) for r in c.execute("""SELECT j.id jid, j.machine_id mid,
                j.created_at jcreated, t.start s, t.end e, t.seg_type st
            FROM jobs j JOIN timelogs t ON t.job_id=j.id
            WHERE j.jobtype='BD' AND j.created_at >= ? AND j.created_at <= ?""", (f0, t1))]
        first_start = {}
        for r in seg_rows:
            if r["st"] == "work":
                repair[r["mid"]] = repair.get(r["mid"], 0) + _minutes(r["s"], r["e"])
            k = r["jid"]
            if r["s"] and (k not in first_start or r["s"] < first_start[k][1]):
                first_start[k] = (r["mid"], r["s"], r["jcreated"])
        for mid, s, jcreated in first_start.values():
            response.setdefault(mid, []).append(_minutes(jcreated, s))
        pm_jobs = {r["machine_id"]: dict(r) for r in c.execute("""
            SELECT machine_id,
              SUM(CASE WHEN status IN ('ServiceCompleted','Done') THEN 1 ELSE 0 END) done,
              COUNT(*) total
            FROM jobs WHERE jobtype='PM' AND planned_date BETWEEN ? AND ?
            GROUP BY machine_id""", (d_from, d_to))}

    out, plant = [], {"planned": 0.0, "run": 0.0, "ideal_good_min": 0.0,
                      "bd": 0, "repair": 0.0, "downtime": 0.0}
    for mid, m in machines.items():
        p = prod.get(mid, {})
        planned = float(p.get("pm") or 0)
        output = float(p.get("out") or 0)
        good = float(p.get("gd") or 0)
        my_bd = [j for j in bd_jobs if j["machine_id"] == mid]
        downtime = sum(_minutes(j["created_at"], j["done_at"]) for j in my_bd
                       if j["status"] in ("ServiceCompleted", "Done"))
        run = max(0.0, planned - downtime)
        rep = repair.get(mid, 0.0)
        n_bd = len(my_bd)
        rate = float(m.get("ideal_rate") or 0)
        A = run / planned * 100 if planned else None
        P = min(100.0, output / (rate * run) * 100) if (rate and run) else None
        Q = good / output * 100 if output else None
        oee = A * P * Q / 10000 if None not in (A, P, Q) else None
        resp = response.get(mid, [])
        pmj = pm_jobs.get(mid, {})
        out.append({
            "machine_id": mid, "code": m["code"], "name": m["name"],
            "line": m["line"], "criticality": m["criticality"],
            "planned_min": round(planned), "run_min": round(run),
            "downtime_min": round(downtime), "repair_min": round(rep),
            "bd_count": n_bd,
            "mtbf_min": round(run / n_bd) if n_bd else None,
            "mttr_min": round(rep / n_bd) if n_bd else None,
            "response_min": round(sum(resp) / len(resp)) if resp else None,
            "availability": round(A, 1) if A is not None else None,
            "performance": round(P, 1) if P is not None else None,
            "quality": round(Q, 1) if Q is not None else None,
            "oee": round(oee, 1) if oee is not None else None,
            "pm_done": pmj.get("done", 0), "pm_total": pmj.get("total", 0),
            "output": round(output), "good": round(good),
        })
        plant["planned"] += planned
        plant["run"] += run
        plant["downtime"] += downtime
        plant["repair"] += rep
        plant["bd"] += n_bd
        if rate:
            plant["ideal_good_min"] += good / rate

    pa = plant["run"] / plant["planned"] * 100 if plant["planned"] else None
    plant_oee = (plant["ideal_good_min"] / plant["planned"] * 100
                 if plant["planned"] and plant["ideal_good_min"] else None)

    with closing(db()) as c:
        # FTFR: done jobs fixed first time (no rework, no re-issue)
        f = c.execute("""SELECT COUNT(*) total,
              SUM(CASE WHEN rework_count=0 AND (new_issue_id='' OR new_issue_id IS NULL)
                  THEN 1 ELSE 0 END) first
            FROM jobs WHERE status IN ('Done','ServiceCompleted')
            AND created_at >= ? AND created_at <= ?""", (f0, t1)).fetchone()
        ftfr = round(f["first"] / f["total"] * 100, 1) if f["total"] else None
        # planned vs reactive ratio from actual work minutes (portable: Python)
        pm_min = tot_min = 0.0
        for r in c.execute("""SELECT j.jobtype jt, t.start s, t.end e
            FROM timelogs t JOIN jobs j ON j.id=t.job_id
            WHERE t.seg_type='work' AND t.start >= ? AND t.start <= ?""", (f0, t1)):
            m = _minutes(r["s"], r["e"])
            tot_min += m
            if r["jt"] in ("PM", "CM", "IMP"):
                pm_min += m
        planned_ratio = round(pm_min / tot_min * 100, 1) if tot_min else None
        # backlog age buckets (portable: Python)
        bl = {"b0": 0, "b7": 0, "b30": 0, "p1_late": 0}
        now_dt = datetime.now()
        for r in c.execute(f"SELECT created_at, priority FROM jobs WHERE status IN {ACTIVE_J}"):
            try:
                age = (now_dt - datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")).days
            except Exception:
                age = 0
            if age <= 7:
                bl["b0"] += 1
            elif age <= 30:
                bl["b7"] += 1
            else:
                bl["b30"] += 1
            if r["priority"] == 3 and age >= 1:
                bl["p1_late"] += 1
        # top failure components
        top_fail = [dict(r) for r in c.execute("""SELECT fault_component comp, COUNT(*) n
            FROM jobs WHERE fault_component!='' AND created_at >= ? AND created_at <= ?
            GROUP BY fault_component ORDER BY n DESC LIMIT 5""", (f0, t1))]
        # PM compliance — delta #5: overdue OPEN PMs stay in the denominator
        pm_all = c.execute("""SELECT
              SUM(CASE WHEN status IN ('ServiceCompleted','Done') THEN 1 ELSE 0 END) done,
              COUNT(*) total FROM jobs WHERE jobtype='PM'
              AND ((due_date >= ? AND due_date <= ?)
                   OR (due_date < ? AND status NOT IN ('Done','Rejected','Cancelled')))""",
              (d_from, d_to, d_from)).fetchone()
        pm_comp = (round((pm_all["done"] or 0) / pm_all["total"] * 100, 1)
                   if pm_all["total"] else None)

    return {
        "from": d_from, "to": d_to, "machines": out,
        "plant": {
            "availability": round(pa, 1) if pa is not None else None,
            "oee": round(plant_oee, 1) if plant_oee is not None else None,
            "downtime_min": round(plant["downtime"]),
            "bd_count": plant["bd"],
            "fleet_mtbf_min": round(plant["run"] / plant["bd"]) if plant["bd"] else None,
            "fleet_mttr_min": round(plant["repair"] / plant["bd"]) if plant["bd"] else None,
            "ftfr": ftfr, "planned_ratio": planned_ratio,
            "pm_compliance": pm_comp,
            "backlog": dict(bl) if bl else {},
            "top_failures": top_fail,
        },
        "targets": {"oee": 65, "pm_compliance": 90, "planned_ratio": 70,
                    "ftfr": 85, "mttr_min": 240, "availability": 90},
    }


ACTIVE_J = ("('Reported','WaitingApproval','WaitingAssignment','Assigned',"
            "'Released','InProgress','Paused','Rework','Hold')")


@router.get("/kpi/trend")
async def kpi_trend(req: Request, days: int = 14):
    user_from(req)
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    with closing(db()) as c:
        sl = {r["log_date"]: dict(r) for r in c.execute("""
            SELECT s.log_date, SUM(s.planned_min) pm,
              SUM(CASE WHEN m.ideal_rate>0 THEN s.good/m.ideal_rate ELSE 0 END) igm
            FROM shiftlogs s JOIN machines m ON m.id=s.machine_id
            WHERE s.log_date >= ? GROUP BY s.log_date""", (start,))}
        bd = {}
        for r in c.execute("""SELECT created_at, done_at FROM jobs
            WHERE jobtype='BD' AND created_at >= ?""", (start + " 00:00:00",)):
            dkey = (r["created_at"] or "")[:10]
            e = bd.setdefault(dkey, {"n": 0, "dt": 0.0})
            e["n"] += 1
            if r["done_at"]:
                e["dt"] += _minutes(r["created_at"], r["done_at"])
    series = []
    for i in range(days):
        d = (date.today() - timedelta(days=days - 1 - i)).isoformat()
        s, b = sl.get(d, {}), bd.get(d, {})
        pm = float(s.get("pm") or 0)
        oee = round(float(s.get("igm") or 0) / pm * 100, 1) if pm else None
        series.append({"d": d, "oee": oee, "bd": b.get("n", 0),
                       "downtime": round(float(b.get("dt") or 0))})
    return {"series": series}


@router.get("/machines/{mid}/history")
async def machine_history(mid: int, req: Request):
    user_from(req)
    with closing(db()) as c:
        m = c.execute("SELECT * FROM machines WHERE id=?", (mid,)).fetchone()
        if not m:
            raise HTTPException(404, "no machine")
        jobs = [dict(r) for r in c.execute("""SELECT j.*, u.name lead_name FROM jobs j
            LEFT JOIN users u ON u.id=j.lead_tech
            WHERE j.machine_id=? ORDER BY j.id DESC LIMIT 100""", (mid,))]
        logs = [dict(r) for r in c.execute("""SELECT * FROM shiftlogs
            WHERE machine_id=? ORDER BY log_date DESC, shift LIMIT 60""", (mid,))]
    return {"machine": dict(m), "jobs": jobs, "shiftlogs": logs}


# ---------------- PM auto-recurrence ----------------

def ensure_pm_jobs():
    """Create a PM job when a machine's PM frequency has elapsed and no PM is open.
    Called at bootstrap; idempotent."""
    created = []
    with closing(db()) as c:
        for m in c.execute("SELECT * FROM machines WHERE active=1 AND pm_freq_days>0"):
            open_pm = c.execute("""SELECT 1 FROM jobs WHERE machine_id=? AND jobtype='PM'
                AND status NOT IN ('Done','Rejected','Cancelled') LIMIT 1""",
                (m["id"],)).fetchone()
            if open_pm:
                continue
            last = c.execute("""SELECT MAX(COALESCE(done_at, planned_date, created_at)) d
                FROM jobs WHERE machine_id=? AND jobtype='PM'""", (m["id"],)).fetchone()
            last_d = (last["d"] or "1970-01-01")[:10]
            due = (date.fromisoformat(last_d) + timedelta(days=m["pm_freq_days"])).isoformat()
            if due <= today():
                ym = datetime.now().strftime("%y%m")
                seq = c.execute("SELECT COUNT(*) FROM jobs WHERE jobid LIKE ?",
                                (f"PRD-{ym}-%",)).fetchone()[0] + 1
                c.execute("""INSERT INTO jobs(jobid,jobtype,machine_id,descr,priority,status,
                    due_date,jobsource,created_by,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (f"PRD-{ym}-{seq:03d}", "PM", m["id"],
                     f"PM ตามรอบ {m['pm_freq_days']} วัน — {m['name']}",
                     1, "Reported", today(), "PM-Auto", 1, now()))
                created.append(m["code"])
        c.commit()
        if created:
            planners = [r["id"] for r in c.execute(
                "SELECT id FROM users WHERE role='planner' AND active=1")]
            notify_users(planners, "🗓 PM ถึงรอบ",
                         f"สร้างงาน PM อัตโนมัติ: {', '.join(created[:5])}"
                         + ("..." if len(created) > 5 else ""))
    return created

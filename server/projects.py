"""Project planning — the S-curve, the plan-against-actual timeline, and crew load.

Three questions, one set of data:

  How much is earned?      the S-curve — cumulative planned hours against earned hours.
  Which task is late?      the timeline — the baseline bar against what actually happened.
  Can the crew do it?      the load grid — PM route + project + breakdown, per person, per week.

The third is the one nobody asks until November. The same four technicians who install
a bagging line owe 302 PM jobs a week and whatever breaks on Tuesday, and a project plan
that ignores that is a promise made with somebody else's time.

WHAT IS MEASURED AND WHAT IS ESTIMATED — this matters, so the API says so per number:
  * project hours   measured  — typed on the task, spread across the days it spans
  * actual dates    measured  — started_at / done_at on the work order, or typed
  * PM hours        measured where the week is planned (assign board wrote a start and
                    end time), otherwise estimated from that person's own last 8 weeks
  * breakdown       estimated — that person's own average over the last 12 weeks
The page prints which, because a capacity number nobody trusts gets ignored, and a
capacity number trusted more than it deserves is worse.
"""
import datetime
from contextlib import closing

from fastapi import APIRouter, Request, HTTPException

from .db import db, now, today, next_jobid, set_stage, log_status
from .auth import user_from, all_plants

router = APIRouter(prefix="/api/projects")

DAY = datetime.timedelta(days=1)
STATUSES = ("Planning", "Active", "Hold", "Done", "Cancelled")
PM_LOOKBACK_WEEKS = 8          # how far back to average a person's PM route
BD_LOOKBACK_WEEKS = 12         # …and their breakdown work
DEFAULT_WEEK_HOURS = 45

# Dividing by a very small per cent is how a plan starts predicting the next decade. A
# task pressed Start five weeks ago and left at 1% does not mean 500 days of work — it
# means nobody has updated the number. Below this, there is no rate to extrapolate and
# saying so is more useful than a date in 2036.
MIN_PCT_TO_FORECAST = 10
# And even above it: a task cannot honestly be forecast to take more than this many
# times its planned length. Past that the figure is an artifact of the arithmetic, not
# a statement about the work.
MAX_STRETCH = 4


# ---------------------------------------------------------------- dates
def _d(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _s(d):
    return d.isoformat() if d else None


def _add(s, n):
    d = _d(s)
    return _s(d + n * DAY) if d else None


def _dd(a, b):
    a, b = _d(a), _d(b)
    return (b - a).days if (a and b) else 0


def _monday(s):
    d = _d(s)
    return _s(d - d.weekday() * DAY) if d else None


def _overlap(a0, a1, b0, b1):
    """Days shared by two inclusive date ranges."""
    a0, a1, b0, b1 = _d(a0), _d(a1), _d(b0), _d(b1)
    if not (a0 and a1 and b0 and b1):
        return 0
    lo, hi = max(a0, b0), min(a1, b1)
    return max(0, (hi - lo).days + 1)


# ---------------------------------------------------------------- access
def _fac(u, req=None):
    """The plant this request is about — a group account may name one."""
    if req is not None and all_plants(u):
        q = req.query_params.get("factory_id")
        if q and str(q).isdigit() and int(q):
            return int(q)
    # the plant this session signed in to (or switched to), not where the account is filed
    return u.get("active_factory") or u.get("factory_id") or 2


def _may_read(u):
    return u["role"] in ("planner", "admin", "manager", "engcenter")


def _may_write(u):
    return u["role"] in ("planner", "admin")


def _need_write(u):
    if not _may_write(u):
        raise HTTPException(403, "not allowed")


def _need_read(u):
    if not _may_read(u):
        raise HTTPException(403, "not allowed")


# ---------------------------------------------------------------- code
def _next_code(c):
    """PJ-YYMM-001. Deliberately not PRJ-: the work orders raised from a project's
    tasks are PRJ-YYMM-XXXX, and two different numbers sharing one prefix is how a
    store-room ends up issuing parts against the wrong thing."""
    ym = datetime.datetime.now().strftime("%y%m")
    top = 0
    for r in c.execute("SELECT code FROM projects WHERE code LIKE ?", (f"PJ-{ym}-%",)):
        tail = str(r[0]).rsplit("-", 1)[-1]
        if tail.isdigit():
            top = max(top, int(tail))
    return f"PJ-{ym}-{top + 1:03d}"


# ---------------------------------------------------------------- tasks
def _tasks(c, pid):
    """Tasks with their actual dates resolved.

    A task linked to a work order takes its actuals from that job — started_at when the
    technician pressed Start, done_at when they finished, progress as they left it.
    Nobody re-types them, which is the only reason a chart like this is still true in
    week nine. An unlinked task falls back to what the planner typed.
    """
    rows = [dict(r) for r in c.execute(
        """SELECT t.*, u.name who_name, u.department who_trade,
                  j.jobid, j.started_at, j.done_at, j.progress jprog, j.status jstatus
           FROM project_tasks t
           LEFT JOIN users u ON u.id=t.who
           LEFT JOIN jobs  j ON j.id=t.job_id
           WHERE t.project_id=? ORDER BY t.seq, t.id""", (pid,))]
    for t in rows:
        linked = bool(t.get("job_id") and t.get("jobid"))
        t["linked"] = linked
        if linked:
            t["act_start"] = (t.get("started_at") or "")[:10] or None
            t["act_end"] = (t.get("done_at") or "")[:10] or None
            if t.get("jstatus") in ("Done", "ServiceCompleted", "Accepted"):
                t["pct"] = 100
            else:
                t["pct"] = int(t.get("jprog") or 0)
        t["act_start"] = t.get("act_start") or None
        t["act_end"] = t.get("act_end") or None
        t["pct"] = max(0, min(100, int(t.get("pct") or 0)))
        t["hours"] = float(t.get("hours") or 0)
    return rows


def _forecast(tasks, today_s):
    """Three rules, in this order, and nothing cleverer.

        finished        → the date it actually finished.
        running         → elapsed so far, scaled up by how much of it is done.
        not started yet → it cannot begin before the task it waits for is forecast to
                          end, keeping the gap (or the overlap) the plan gave it, then
                          it takes as long as the plan said.

    Rule 3 is the one that earns its keep: it is what carries a slip down the chain to
    the handover date instead of letting it quietly disappear. Without it every
    un-started task reads "on time" however late the work in front of it ran, and the
    page reports a finish date that cannot happen.
    """
    by = {t["id"]: t for t in tasks}
    out, busy = {}, set()

    def fc(t):
        tid = t["id"]
        if tid in out:
            return out[tid]
        if tid in busy:                       # a circular waits-for: stop, keep the plan
            return {"s": t["plan_start"], "f": t["plan_end"], "cyc": True}
        busy.add(tid)
        ps, pf = t["plan_start"], t["plan_end"]
        # A task planned to start and finish on the same day is ONE day long, and the
        # distance between those two dates is zero. Forcing a minimum of 1 here added a
        # day to every single-day task and reported it as a day late before anybody had
        # touched it — "panel assembly, 21 Sept → 21 Sept, +1 d".
        length = max(0, _dd(ps, pf))
        a0, a1, pct = t["act_start"], t["act_end"], t["pct"]
        if a1:
            s, f = a0 or ps, a1
        elif a0:
            s = a0
            elapsed = _dd(a0, today_s)
            cap = max(1, length) * MAX_STRETCH
            if elapsed <= 0:
                # started today, or a start date typed ahead of today: nothing has
                # elapsed, so it keeps its planned length
                run = length
            elif pct < MIN_PCT_TO_FORECAST:
                # too little done to imply a rate. The honest answer is "at least as
                # long as it has already taken, and at least as long as the plan said"
                run = max(elapsed, length)
                t["fc_note"] = "low_pct"
            else:
                run = max(elapsed, round(elapsed * 100.0 / pct))
                if run > cap:
                    run, t["fc_note"] = cap, "capped"
            f = _add(a0, run)
        else:
            s = ps
            dep = by.get(t.get("waits_for"))
            if dep:
                lag = _dd(dep["plan_end"], ps)          # negative = a planned overlap
                cand = _add(fc(dep)["f"], lag)
                if cand and _d(cand) > _d(s):
                    s = cand
            f = _add(s, length)
        busy.discard(tid)
        out[tid] = {"s": s, "f": f}
        return out[tid]

    for t in tasks:
        t.setdefault("fc_note", "")
        r = fc(t)
        t["fc_start"], t["fc_end"] = r["s"], r["f"]
        t["slip"] = _dd(t["plan_end"], r["f"])
        t["own_slip"] = t["slip"] if t["act_start"] else 0
        t["pushed"] = (not t["act_start"]) and t["slip"] > 0
        t["state"] = "done" if t["act_end"] else ("run" if t["act_start"] else "soon")
    return tasks


# ---------------------------------------------------------------- S-curve
def _work_days(a, b, off, hol):
    """The days between two dates that the plant actually works."""
    out, d0, d1 = [], _d(a), _d(b)
    if not (d0 and d1) or d1 < d0:
        return out
    d = d0
    for _ in range(4000):
        if d.weekday() not in off and d.isoformat() not in hol:
            out.append(d.isoformat())
        if d >= d1:
            break
        d += DAY
    return out


def _curve(tasks, p_start, p_end, today_s, off=(6,), hol=()):
    """Cumulative hours, planned against earned.

    Spread over the days the plant WORKS — the same days the work-plan sheet shades,
    so the curve and the sheet cannot report two different percentages for one project.
    (They used to: the curve counted calendar days and the sheet counted working ones.)

    Points are one per day on a short project and one per week on a long one. A curve
    drawn through four points is not a curve, it is three straight lines, and it will
    never look like anything whatever the plan underneath it says.

    The earned line stops at today. Everything to the right of that is a plan, not a
    measurement, and drawing it would be an invention.
    """
    last = p_end
    for t in tasks:
        if t.get("fc_end") and t["fc_end"] > last:
            last = t["fc_end"]
    span = max(1, _dd(p_start, last) + 1)
    daily = span <= 60
    buckets = []
    if daily:
        d = _d(p_start)
        while d <= _d(last):
            buckets.append((_s(d), _s(d)))
            d += DAY
    else:
        d = _d(_monday(p_start))
        while d <= _d(last):
            buckets.append((_s(d), _s(d + 6 * DAY)))
            d += 7 * DAY
    where = {}
    for i, (b0, b1) in enumerate(buckets):
        d = _d(b0)
        while d <= _d(b1):
            where[_s(d)] = i
            d += DAY

    plan = [0.0] * len(buckets)
    earn = [0.0] * len(buckets)
    total = 0.0
    for t in tasks:
        h = float(t["hours"] or 0)
        total += h
        pw = _work_days(t["plan_start"], t["plan_end"], off, hol)
        if pw:
            for d in pw:
                i = where.get(d)
                if i is not None:
                    plan[i] += h / len(pw)
        elif h:
            # a task planned entirely on days the plant is shut still has to land
            # somewhere, or its hours quietly vanish out of the total
            i = where.get(t["plan_start"])
            if i is not None:
                plan[i] += h
        if t["act_start"]:
            banked = h * (100 if t["act_end"] else t["pct"]) / 100.0
            aw = _work_days(t["act_start"], t["act_end"] or today_s, off, hol)
            if aw:
                for d in aw:
                    i = where.get(d)
                    if i is not None:
                        earn[i] += banked / len(aw)
            elif banked:
                i = where.get(t["act_start"])
                if i is not None:
                    earn[i] += banked

    pts, cp, ca = [], 0.0, 0.0
    for i, (b0, b1) in enumerate(buckets):
        cp += plan[i]
        ca += earn[i]
        row = {"week": b0, "end": b1, "plan": round(cp, 1), "earned": None}
        if _d(b0) <= _d(today_s):
            row["earned"] = round(ca, 1)
        pts.append(row)

    # Where the plan puts its middle. An S-curve is only S-shaped when work ramps up,
    # peaks and tails off; a plan that front-loads draws a different shape, and that is
    # information about the plan rather than a fault in the chart.
    half, mid = (total / 2.0) if total else 0.0, None
    for i, r in enumerate(pts):
        if r["plan"] >= half:
            mid = (i + 1) / len(pts)
            break
    shape = "even"
    if mid is not None:
        shape = "front" if mid < 0.4 else "back" if mid > 0.62 else "s"
    return {"total": round(total, 1), "points": pts,
            "step": "day" if daily else "week", "shape": shape,
            "mid_at": round((mid or 0) * 100)}


# ---------------------------------------------------------------- crew load
def _week_list(p_start, p_end, tasks):
    last = p_end
    for t in tasks:
        if t.get("fc_end") and t["fc_end"] > last:
            last = t["fc_end"]
    out, w = [], _monday(p_start)
    while _d(w) <= _d(last):
        out.append(w)
        w = _add(w, 7)
    return out


def _job_minutes(row):
    """Minutes a planned job asks for, from the window the assign board gave it."""
    a, b = (row.get("planned_start") or ""), (row.get("planned_end") or "")
    try:
        h1, m1 = int(a[:2]), int(a[3:5])
        h2, m2 = int(b[:2]), int(b[3:5])
        mins = (h2 * 60 + m2) - (h1 * 60 + m1)
        return mins if 0 < mins <= 12 * 60 else 0
    except Exception:
        return 0


def _crew(c, fac, weeks, tasks, today_s):
    """PM route + this project + breakdown, per person, per week, against what that
    person can actually give.

    The team total is a trap and this function exists because of it: add every hour in
    the plant together and a project can look affordable while one fitter is at 176 per
    cent for six weeks. Hours are not pooled — they belong to people.
    """
    if not weeks:
        return {"people": [], "weeks": []}
    people = {}
    for r in c.execute(
            "SELECT id,name,department,week_hours,role FROM users"
            " WHERE active=1 AND role IN ('technician','planner') AND (factory_id=? OR factory_id=0)",
            (fac,)):
        people[r["id"]] = {"id": r["id"], "name": r["name"], "trade": r["department"] or "",
                           "cap": int(r["week_hours"] or DEFAULT_WEEK_HOURS), "role": r["role"]}
    # anybody carrying a task belongs on the chart even if they are not filed as crew
    for t in tasks:
        if t.get("who") and t["who"] not in people:
            people[t["who"]] = {"id": t["who"], "name": t.get("who_name") or "?",
                                "trade": t.get("who_trade") or "", "cap": DEFAULT_WEEK_HOURS,
                                "role": ""}
    if not people:
        return {"people": [], "weeks": weeks}

    w0, w1 = weeks[0], _add(weeks[-1], 6)
    back = _add(w0, -7 * max(PM_LOOKBACK_WEEKS, BD_LOOKBACK_WEEKS))

    # ---- measured: PM/CM/BD hours the assign board has actually planned
    pm_wk, bd_hist, pm_hist = {}, {}, {}
    for r in c.execute(
            """SELECT j.lead_tech uid, j.jobtype, j.planned_date, j.planned_start, j.planned_end
               FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
               WHERE j.lead_tech IS NOT NULL AND j.planned_date IS NOT NULL
                 AND j.planned_date>=? AND j.planned_date<=?
                 AND COALESCE(m.factory_id,?)=?
                 AND j.status NOT IN ('Cancelled','Rejected')""",
            (back, w1, fac, fac)):
        mins = _job_minutes(dict(r))
        if not mins:
            continue
        uid, wk = r["uid"], _monday(r["planned_date"])
        if uid not in people:
            continue
        if r["jobtype"] == "PM":
            (pm_wk if wk in weeks else pm_hist).setdefault(uid, {}).setdefault(wk, 0.0)
            (pm_wk if wk in weeks else pm_hist)[uid][wk] += mins / 60.0
        elif r["jobtype"] in ("CM", "BD"):
            bd_hist.setdefault(uid, {}).setdefault(wk, 0.0)
            bd_hist[uid][wk] += mins / 60.0

    def _avg(hist, uid, n):
        d = hist.get(uid) or {}
        if not d:
            return None
        vals = sorted(d.items())[-n:]
        return round(sum(v for _k, v in vals) / max(1, len(vals)), 1)

    # ---- this project's hours, spread across the days each task actually spans
    pj = {}
    for t in tasks:
        uid = t.get("who")
        if not uid or not t["hours"]:
            continue
        days = max(1, _dd(t["plan_start"], t["plan_end"]) + 1)
        per = t["hours"] / days
        for w in weeks:
            n = _overlap(t["plan_start"], t["plan_end"], w, _add(w, 6))
            if n:
                pj.setdefault(uid, {}).setdefault(w, 0.0)
                pj[uid][w] += per * n

    # A technician new to the plant, or one the assign board has not reached yet, has no
    # history of their own. Rather than draw them at zero — which makes the page look
    # comfortable for exactly the person nobody has planned for — fall back to what the
    # rest of the crew averages. Marked "estimated" either way, so nobody mistakes it.
    techs = [p for p in people.values() if p["role"] == "technician"] or list(people.values())
    seen = [v for v in (_avg(pm_hist, p["id"], PM_LOOKBACK_WEEKS) for p in techs) if v]
    plant_pm = round(sum(seen) / len(seen), 1) if seen else None
    seen_bd = [v for v in (_avg(bd_hist, p["id"], BD_LOOKBACK_WEEKS) for p in techs) if v]
    plant_bd = round(sum(seen_bd) / len(seen_bd), 1) if seen_bd else 0.0

    out = []
    for p in people.values():
        pm_avg = _avg(pm_hist, p["id"], PM_LOOKBACK_WEEKS)
        bd_avg = _avg(bd_hist, p["id"], BD_LOOKBACK_WEEKS)
        if bd_avg is None:
            bd_avg = plant_bd if p["role"] == "technician" else 0.0
        cells = []
        for w in weeks:
            got = (pm_wk.get(p["id"]) or {}).get(w)
            if got is not None:
                pm, basis = round(got, 1), "measured"
            elif pm_avg is not None:
                pm, basis = pm_avg, "estimated"
            elif plant_pm is not None and p["role"] == "technician":
                pm, basis = plant_pm, "crew average"
            else:
                pm, basis = 0.0, "none"
            proj = round((pj.get(p["id"]) or {}).get(w, 0.0), 1)
            cells.append({"week": w, "pm": pm, "pj": proj, "bd": bd_avg,
                          "tot": round(pm + proj + bd_avg, 1), "basis": basis})
        if not any(cl["tot"] for cl in cells):
            continue                                  # nothing to say about this person
        out.append({**p, "cells": cells,
                    "over": sum(1 for cl in cells if cl["tot"] - p["cap"] > 0.5),
                    "short": round(sum(max(0.0, cl["tot"] - p["cap"]) for cl in cells), 1)})
    out.sort(key=lambda p: (-p["short"], p["name"] or ""))
    # The page has to be able to say when it is drawing a picture of nothing: a plant
    # that has never assigned a PM job with a time window has no PM hours to show, and
    # a load chart without them is reassuring for the wrong reason.
    return {"people": out, "weeks": weeks,
            "pm_lookback": PM_LOOKBACK_WEEKS, "bd_lookback": BD_LOOKBACK_WEEKS,
            "has_pm": bool(pm_wk or pm_hist), "has_bd": bool(bd_hist),
            "cap_default": DEFAULT_WEEK_HOURS}


# ---------------------------------------------------------------- routes
@router.get("")
async def listing(req: Request):
    u = user_from(req)
    _need_read(u)
    fac = _fac(u, req)
    with closing(db()) as c:
        rows = []
        for r in c.execute(
                """SELECT p.*, u.name owner_name,
                          (SELECT COUNT(*) FROM project_tasks t WHERE t.project_id=p.id) ntask
                   FROM projects p LEFT JOIN users u ON u.id=p.owner_id
                   WHERE p.factory_id=? ORDER BY
                     CASE p.status WHEN 'Active' THEN 0 WHEN 'Planning' THEN 1
                                   WHEN 'Hold' THEN 2 ELSE 3 END, p.start_date DESC, p.id DESC""",
                (fac,)):
            d = dict(r)
            tasks = _forecast(_tasks(c, d["id"]), today())
            fin = d["finish_date"]
            for t in tasks:
                if t["fc_end"] and t["fc_end"] > fin:
                    fin = t["fc_end"]
            d["forecast_finish"] = fin
            d["slip"] = _dd(d["finish_date"], fin)
            d["done_pct"] = (round(sum(t["hours"] * t["pct"] for t in tasks)
                                   / max(1.0, sum(t["hours"] for t in tasks)))
                             if tasks else 0)
            rows.append(d)
    return {"projects": rows, "can_edit": _may_write(u)}


@router.post("")
async def create(req: Request):
    u = user_from(req)
    _need_write(u)
    b = await req.json()
    name = (b.get("name") or "").strip()[:200]
    if not name:
        raise HTTPException(400, "name required")
    st, fi = b.get("start_date") or today(), b.get("finish_date") or ""
    if not _d(st) or not _d(fi):
        raise HTTPException(400, "start and finish dates required")
    if _d(fi) < _d(st):
        raise HTTPException(400, "finish is before start")
    fac = _fac(u, req)
    with closing(db()) as c:
        pid = c.insert_id(
            """INSERT INTO projects(factory_id,code,name,area,owner_id,start_date,finish_date,
                 status,notes,created_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (fac, _next_code(c), name, (b.get("area") or "")[:120],
             b.get("owner_id") or u["id"], st, fi,
             b.get("status") if b.get("status") in STATUSES else "Planning",
             (b.get("notes") or "")[:2000], u["id"], now()))
        c.commit()
    return {"ok": True, "id": pid}


@router.post("/{pid}")
async def update(pid: int, req: Request):
    u = user_from(req)
    _need_write(u)
    b = await req.json()
    with closing(db()) as c:
        row = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "no such project")
        sets, vals = [], []
        for k, cap in (("name", 200), ("area", 120), ("notes", 2000)):
            if k in b:
                sets.append(f"{k}=?")
                vals.append((b.get(k) or "")[:cap])
        for k in ("start_date", "finish_date"):
            if k in b:
                if not _d(b[k]):
                    raise HTTPException(400, f"{k} is not a date")
                sets.append(f"{k}=?")
                vals.append(b[k])
        if "owner_id" in b:
            sets.append("owner_id=?")
            vals.append(b.get("owner_id"))
        if "status" in b:
            if b["status"] not in STATUSES:
                raise HTTPException(400, "unknown status")
            sets.append("status=?")
            vals.append(b["status"])
        if sets:
            c.execute(f"UPDATE projects SET {','.join(sets)} WHERE id=?", (*vals, pid))
            c.commit()
    return {"ok": True}


@router.post("/{pid}/delete")
async def remove(pid: int, req: Request):
    u = user_from(req)
    _need_write(u)
    with closing(db()) as c:
        n = c.execute("SELECT COUNT(*) FROM project_tasks WHERE project_id=? AND job_id IS NOT NULL",
                      (pid,)).fetchone()[0]
        if n:
            # Deleting the project would orphan real work orders that people have been
            # given and may already have started. Say so rather than doing it quietly.
            raise HTTPException(400, f"{n} task(s) have a work order raised — cancel those jobs first")
        c.execute("DELETE FROM project_tasks WHERE project_id=?", (pid,))
        c.execute("DELETE FROM projects WHERE id=?", (pid,))
        c.commit()
    return {"ok": True}


@router.get("/{pid}")
async def detail(pid: int, req: Request):
    u = user_from(req)
    _need_read(u)
    with closing(db()) as c:
        p = c.execute("""SELECT p.*, u.name owner_name, b.name baseline_name
                         FROM projects p LEFT JOIN users u ON u.id=p.owner_id
                         LEFT JOIN users b ON b.id=p.baseline_by WHERE p.id=?""", (pid,)).fetchone()
        if not p:
            raise HTTPException(404, "no such project")
        p = dict(p)
        t0 = today()
        tasks = _forecast(_tasks(c, pid), t0)
        try:
            from .pm import _cal_config   # imported here, so module load order can never bite
            off, hol = _cal_config(c, p["factory_id"])
        except Exception:
            off, hol = {6}, set()
        weeks = _week_list(p["start_date"], p["finish_date"], tasks)
        crew = _crew(c, p["factory_id"], weeks, tasks, t0)
        fin = p["finish_date"]
        for t in tasks:
            if t["fc_end"] and t["fc_end"] > fin:
                fin = t["fc_end"]
        people = [dict(r) for r in c.execute(
            "SELECT id,name,department FROM users WHERE active=1 AND role IN"
            " ('technician','planner','manager','admin') AND (factory_id=? OR factory_id=0)"
            " ORDER BY name", (p["factory_id"],))]
        # The work-plan sheet shades a cell per WORKING day. A plan that spreads a task's
        # effort across a Sunday the plant does not work is a plan quietly promising work
        # nobody is there to do — so the sheet has to know the plant's own calendar, not
        # a guess at it. Same source the PM route and the KPI calendar already use.
        cal = {"weekly_off": sorted(off), "holidays": sorted(hol)}
        return {"project": p, "tasks": tasks, "today": t0,
                "forecast_finish": fin, "slip": _dd(p["finish_date"], fin),
                "curve": _curve(tasks, p["start_date"], p["finish_date"], t0, off, hol),
                "crew": crew, "people": people, "calendar": cal,
                "can_edit": _may_write(u)}


@router.post("/{pid}/task")
async def save_task(pid: int, req: Request):
    u = user_from(req)
    _need_write(u)
    b = await req.json()
    name = (b.get("name") or "").strip()[:200]
    ps, pe = b.get("plan_start"), b.get("plan_end")
    if not name:
        raise HTTPException(400, "task name required")
    if not _d(ps) or not _d(pe):
        raise HTTPException(400, "plan start and end required")
    if _d(pe) < _d(ps):
        raise HTTPException(400, "plan end is before plan start")
    tid = b.get("id")
    with closing(db()) as c:
        if not c.execute("SELECT id FROM projects WHERE id=?", (pid,)).fetchone():
            raise HTTPException(404, "no such project")
        wf = b.get("waits_for") or None
        if wf:
            if int(wf) == int(tid or 0):
                raise HTTPException(400, "a task cannot wait for itself")
            if not c.execute("SELECT id FROM project_tasks WHERE id=? AND project_id=?",
                             (wf, pid)).fetchone():
                raise HTTPException(400, "waits-for is not a task of this project")
        vals = (name, ps, pe, float(b.get("hours") or 0), b.get("who") or None, wf,
                b.get("act_start") or None, b.get("act_end") or None,
                max(0, min(100, int(b.get("pct") or 0))))
        if tid:
            # Only touch what was actually sent. A save that quietly blanked the fields
            # it was not given would let one edit of a task's name wipe its hours, its
            # owner and its actual dates — and nothing on screen would say so.
            cols = {"name": name, "plan_start": ps, "plan_end": pe}
            if "hours" in b:
                cols["hours"] = float(b.get("hours") or 0)
            if "who" in b:
                cols["who"] = b.get("who") or None
            if "waits_for" in b:
                cols["waits_for"] = wf
            for k in ("act_start", "act_end"):
                if k in b:
                    cols[k] = b.get(k) or None
            if "pct" in b:
                cols["pct"] = max(0, min(100, int(b.get("pct") or 0)))
            for k, cap in (("wbs", 12), ("phase", 60), ("remark", 200)):
                if k in b:
                    cols[k] = (b.get(k) or "")[:cap]
            c.execute("UPDATE project_tasks SET " + ",".join(f"{k}=?" for k in cols)
                      + " WHERE id=? AND project_id=?", (*cols.values(), tid, pid))
        else:
            # `x or -1` reads fine and is wrong: the first task's seq is 0, which is
            # falsy, so every task after it was handed seq 0 again — and with the row
            # number coming off seq, a whole project came out numbered "1".
            top = c.execute("SELECT COALESCE(MAX(seq),-1) FROM project_tasks WHERE project_id=?",
                            (pid,)).fetchone()[0]
            seq = (top if top is not None else -1) + 1
            tid = c.insert_id(
                """INSERT INTO project_tasks(project_id,seq,name,plan_start,plan_end,hours,who,
                     waits_for,act_start,act_end,pct,wbs,phase,remark,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, seq, *vals, (b.get("wbs") or str(seq + 1))[:12],
                 (b.get("phase") or "")[:60], (b.get("remark") or "")[:200], now()))
        c.commit()
        # a chain that loops would make the forecast meaningless; refuse and undo
        if _loops(c, pid):
            c.execute("UPDATE project_tasks SET waits_for=NULL WHERE id=?", (tid,))
            c.commit()
            raise HTTPException(400, "that waits-for makes a loop — it was not saved")
    return {"ok": True, "id": tid}


def _loops(c, pid):
    nxt = {r["id"]: r["waits_for"] for r in
           c.execute("SELECT id,waits_for FROM project_tasks WHERE project_id=?", (pid,))}
    for start in nxt:
        seen, cur = set(), start
        while cur:
            if cur in seen:
                return True
            seen.add(cur)
            cur = nxt.get(cur)
    return False


@router.post("/{pid}/task/{tid}/delete")
async def del_task(pid: int, tid: int, req: Request):
    u = user_from(req)
    _need_write(u)
    with closing(db()) as c:
        c.execute("UPDATE project_tasks SET waits_for=NULL WHERE project_id=? AND waits_for=?",
                  (pid, tid))
        c.execute("DELETE FROM project_tasks WHERE id=? AND project_id=?", (tid, pid))
        c.commit()
    return {"ok": True}


@router.post("/{pid}/task/{tid}/order")
async def raise_job(pid: int, tid: int, req: Request):
    """Turn a task into a real PRJ work order.

    This is what makes the actual bar draw itself: from here on the task's start and
    finish come from the technician pressing the buttons, not from somebody remembering
    to update a plan.
    """
    u = user_from(req)
    _need_write(u)
    b = await req.json() if req.headers.get("content-type", "").startswith("application/json") else {}
    with closing(db()) as c:
        t = c.execute("SELECT * FROM project_tasks WHERE id=? AND project_id=?", (tid, pid)).fetchone()
        if not t:
            raise HTTPException(404, "no such task")
        if t["job_id"]:
            raise HTTPException(400, "this task already has a work order")
        p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        _pfac = p["factory_id"] or _fac(u, req)       # the project's plant numbers the job
        jid = next_jobid(c, "PRJ", factory_id=_pfac)
        # b450: only a technician can be a job's technician — a task given to its planner
        # becomes a job waiting to be assigned, not a job booked to the planner
        _who = t["who"]
        if _who and not c.execute("SELECT 1 FROM users WHERE id=? AND role='technician'", (_who,)).fetchone():
            _who = None
        descr = f"{p['code']} · {t['name']}"[:400]
        new_id = c.insert_id(
            """INSERT INTO jobs(jobid,jobtype,machine_id,descr,priority,status,planned_date,
                 lead_tech,due_date,jobsource,created_by,created_at,ptask_id,factory_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (jid, "PRJ", b.get("machine_id"), descr, 1, "Reported", t["plan_start"],
             _who, t["plan_end"], "project", u["id"], now(), tid, _pfac))
        set_stage(c, new_id)
        c.execute("UPDATE project_tasks SET job_id=? WHERE id=?", (new_id, tid))
        c.commit()
        log_status(c, new_id, "Reported", u["id"])
        c.commit()
    return {"ok": True, "job_id": new_id, "jobid": jid}


@router.post("/{pid}/baseline")
async def rebaseline(pid: int, req: Request):
    """Move the baseline onto the current forecast — deliberately, and on the record.

    A project that is re-baselined every fortnight is always on time and always late.
    So this is a button somebody has to press, it says who pressed it and why, and the
    page keeps saying so afterwards.
    """
    u = user_from(req)
    _need_write(u)
    b = await req.json()
    note = (b.get("note") or "").strip()[:200]
    if not note:
        raise HTTPException(400, "say why the baseline is moving")
    with closing(db()) as c:
        p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404, "no such project")
        tasks = _forecast(_tasks(c, pid), today())
        fin = p["finish_date"]
        for t in tasks:
            if t["fc_end"] and t["fc_end"] > fin:
                fin = t["fc_end"]
            c.execute("UPDATE project_tasks SET plan_start=?, plan_end=? WHERE id=?",
                      (t["fc_start"], t["fc_end"], t["id"]))
        c.execute("UPDATE projects SET finish_date=?, baseline_at=?, baseline_by=?, baseline_note=?"
                  " WHERE id=?", (fin, now(), u["id"], note, pid))
        c.commit()
    return {"ok": True, "finish_date": fin}

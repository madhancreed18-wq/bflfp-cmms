"""Manpower: are there enough technicians for the work? (b585)

Two numbers for a day, side by side:

  AVAILABLE  every technician on the plant's board, minus anyone the planner has
             marked OFF for that day (leave, sick, training …), times the hours a
             technician gives in a working day (their own week_hours spread over the
             plant's working days; 45 h when not set). A weekly-off day or a holiday
             has nothing available and no "short".
  NEEDED     the estimated man-hours of the day's work: every open job planned for
             the day plus the PM the programme puts on it that has no work order
             yet. Today also lists the open work from earlier days as OVERDUE.

How long a job takes is estimated, never typed in by default:
  1. the planner's own figure for that job, if they set one (⏱ on the card);
  2. the median real time (all technicians' timers added up) of the last finished
     jobs of the same kind on the same machine — PM of the same frequency for a PM;
  3. the median for that job type (and PM frequency) across the plant;
  4. a starting figure: PM 1 h, CM 2 h, BD 2 h, IMP 4 h.
Each estimate says which of these it came from.

Tables (created here, nothing else touches them):
  mp_off(factory_id, day, user_id)  who is off on a day
  mp_est(job_id, hours)             the planner's own figure for one job
"""
import time
from contextlib import closing
from datetime import date, timedelta
from statistics import median

from fastapi import APIRouter, Request, HTTPException

from .auth import require_role
from .db import db, now, today

router = APIRouter(prefix="/api/manpower")

DEFAULT_H = {"PM": 1.0, "CM": 2.0, "BD": 2.0, "IMP": 4.0}
DEFAULT_WEEK = 45
OPEN = "('Cancelled','Done','Rejected','ServiceCompleted')"
_HIST = {}          # factory -> (time, tables) — the history medians, 10 minutes


def _fac(u):
    return u.get("active_factory") or u.get("factory_id")


def _ensure(c):
    c.execute("""CREATE TABLE IF NOT EXISTS mp_off(factory_id INTEGER, day TEXT, user_id INTEGER,
                 by_id INTEGER, at TEXT, PRIMARY KEY(factory_id, day, user_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS mp_est(job_id INTEGER PRIMARY KEY, hours REAL,
                 by_id INTEGER, at TEXT)""")


# ── who is on the board, and what a day of theirs is worth ─────────────────────────
def _techs(c, fac, u=None):
    """The same people the assign board offers: technician PEOPLE, plus technicians who
    sign in as themselves with no separate person record.
    b599: the Central Air Conditioning planner counts only AC technicians; everyone else's
    count leaves them out."""
    from .elec import is_ac_planner, ac_ids
    acs = ac_ids(c, fac)
    if u is not None and is_ac_planner(u):
        return sorted([dict(r) for r in c.execute(
            "SELECT id,name,username,department,COALESCE(week_hours,0) wh FROM users WHERE id IN (%s)"
            % (",".join(str(int(i)) for i in acs) or "0"))],
            key=lambda t: (t["name"] or t["username"] or "").lower())
    return [t for t in _techs_plant(c, fac) if t["id"] not in acs]


def _techs_plant(c, fac):
    from .teams import _handset_people
    out = {r["id"]: dict(r) for r in c.execute(
        "SELECT id,name,username,department,COALESCE(week_hours,0) wh FROM users"
        " WHERE active=1 AND role='technician' AND COALESCE(can_login,1)=0"
        " AND (factory_id=? OR COALESCE(factory_id,0)=0)", (fac,))}
    try:
        _one, alone = _handset_people(c, fac)
    except Exception:
        alone = set()
    if alone:
        for r in c.execute("SELECT id,name,username,department,COALESCE(week_hours,0) wh FROM users"
                           " WHERE id IN (%s)" % ",".join(str(int(i)) for i in alone)):
            out.setdefault(r["id"], dict(r))
    return sorted(out.values(), key=lambda t: (t["name"] or t["username"] or "").lower())


def _calendar(c, fac):
    try:
        from .pm import _cal_config
        off, hol = _cal_config(c, fac)
    except Exception:
        off, hol = {6}, set()
    return set(off), set(hol)


def _workday(d, off, hol):
    dd = date.fromisoformat(d)
    return dd.weekday() not in off and d not in hol


def _day_hours(t, off):
    wd = max(1, 7 - len(off))
    return round((int(t.get("wh") or 0) or DEFAULT_WEEK) / wd, 1)


# ── how long work takes ────────────────────────────────────────────────────────────
def _history(c, fac):
    hit = _HIST.get(fac)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    since = (date.today() - timedelta(days=365)).isoformat()
    rows = c.execute(
        """SELECT j.id, j.machine_id mid, UPPER(COALESCE(j.jobtype,'')) jt, COALESCE(j.pm_freq,'') fq,
                  SUM((julianday(t.end) - julianday(t.start)) * 24.0) h
             FROM timelogs t JOIN jobs j ON j.id = t.job_id
             LEFT JOIN machines m ON m.id = j.machine_id
            WHERE t.end IS NOT NULL AND t.start >= ?
              AND j.status IN ('Done','ServiceCompleted')
              AND (COALESCE(m.factory_id, j.factory_id) = ? OR COALESCE(m.factory_id, j.factory_id) IS NULL)
            GROUP BY j.id""", (since, fac)).fetchall()
    by_m, by_t = {}, {}
    for r in rows:
        h = r["h"] or 0
        if h < 0.05 or h > 72:              # a timer left running over a weekend is not a job
            continue
        fq = r["fq"] if r["jt"] == "PM" else ""
        if r["mid"]:
            by_m.setdefault((r["mid"], r["jt"], fq), []).append(h)
        by_t.setdefault((r["jt"], fq), []).append(h)
        if fq:
            by_t.setdefault((r["jt"], ""), []).append(h)
    tabs = ({k: (round(median(v[-10:]), 1), len(v)) for k, v in by_m.items()},
            {k: (round(median(v[-30:]), 1), len(v)) for k, v in by_t.items()})
    _HIST[fac] = (time.time(), tabs)
    return tabs


def _estimate(it, hist, own):
    """(hours, basis) for one piece of work: {id, machine_id, jobtype, freq}."""
    jid = it.get("id")
    if jid and int(jid) in own:
        return own[int(jid)], "set"
    jt = str(it.get("jobtype") or "").upper()
    fq = str(it.get("freq") or "") if jt == "PM" else ""
    by_m, by_t = hist
    mid = it.get("machine_id")
    if mid:
        v = by_m.get((mid, jt, fq))
        if v and v[1] >= 2:
            return max(0.25, v[0]), f"machine:{v[1]}"
    v = by_t.get((jt, fq)) or by_t.get((jt, ""))
    if v and v[1] >= 3:
        return max(0.25, v[0]), f"type:{v[1]}"
    return DEFAULT_H.get(jt, 2.0), "default"


def _own(c, ids):
    ids = [int(i) for i in ids if str(i).isdigit()]
    out = {}
    for i in range(0, len(ids), 500):
        ch = ids[i:i + 500]
        for r in c.execute("SELECT job_id, hours FROM mp_est WHERE job_id IN (%s)" % ",".join("?" * len(ch)), ch):
            if r["hours"] is not None:
                out[r["job_id"]] = float(r["hours"])
    return out


def _trade(c, fac, cache, j):
    if str(j.get("jobtype") or "").upper() == "PM":
        return "PM"
    pt = j.get("problem_type") or ""
    if fac not in cache:
        cache[fac] = {r["name"]: (r["category"] or "") for r in c.execute(
            "SELECT name, category FROM problem_types WHERE factory_id=?", (fac,))}
    from .elec import job_trade
    return job_trade(j, cache[fac].get(pt)) or "Other"      # b594: the planner's department first


# ── the assign board: one day, live ────────────────────────────────────────────────
@router.post("/board")
async def board(req: Request):
    """The board sends the work it holds; this returns who is in and what each piece of
    work is estimated at. The board adds them up itself, so the bar moves as jobs are
    dragged onto crews without another round trip."""
    u = require_role(req, "planner", "admin", "manager", "engcenter")
    fac = _fac(u)
    b = await req.json()
    d = str(b.get("d") or today())[:10]
    items = b.get("items") or []
    with closing(db()) as c:
        _ensure(c)
        off_wd, hol = _calendar(c, fac)
        techs = _techs(c, fac, _as_ac(req, u))
        offs = {r["user_id"] for r in c.execute(
            "SELECT user_id FROM mp_off WHERE factory_id=? AND day=?", (fac, d))}
        hist = _history(c, fac)
        own = _own(c, [it.get("id") for it in items if it.get("id")])
        est = {}
        for it in items:
            h, basis = _estimate(it, hist, own)
            est[str(it.get("k"))] = {"h": h, "b": basis}
    return {"d": d, "workday": _workday(d, off_wd, hol),
            "techs": [{"id": t["id"], "name": t["name"] or t["username"], "h": _day_hours(t, off_wd),
                       "off": t["id"] in offs} for t in techs],
            "est": est}


@router.post("/off")
async def set_off(req: Request):
    u = require_role(req, "planner", "admin")
    fac = _fac(u)
    b = await req.json()
    d = str(b.get("d") or "")[:10]
    uid = int(b.get("user_id") or 0)
    if not d or not uid:
        raise HTTPException(400, "day and technician needed")
    with closing(db()) as c:
        _ensure(c)
        if b.get("off"):
            c.execute("INSERT OR REPLACE INTO mp_off(factory_id,day,user_id,by_id,at) VALUES(?,?,?,?,?)",
                      (fac, d, uid, u["id"], now()))
        else:
            c.execute("DELETE FROM mp_off WHERE factory_id=? AND day=? AND user_id=?", (fac, d, uid))
        c.commit()
    return {"ok": True}


@router.post("/est")
async def set_est(req: Request):
    """The planner's own figure for one job (hours), or null to go back to the estimate."""
    u = require_role(req, "planner", "admin")
    b = await req.json()
    jid = int(b.get("job_id") or 0)
    if not jid:
        raise HTTPException(400, "job needed")
    h = b.get("hours")
    with closing(db()) as c:
        _ensure(c)
        if h in (None, ""):
            c.execute("DELETE FROM mp_est WHERE job_id=?", (jid,))
        else:
            h = float(h)
            if not (0 < h <= 200):
                raise HTTPException(400, "hours must be between 0 and 200")
            c.execute("INSERT OR REPLACE INTO mp_est(job_id,hours,by_id,at) VALUES(?,?,?,?)",
                      (jid, round(h, 2), u["id"], now()))
        c.commit()
    return {"ok": True}


# ── many days at once: the plan calendar and the dashboard ─────────────────────────
@router.get("/range")
async def day_range(req: Request, frm: str = "", to: str = ""):
    u = require_role(req, "planner", "admin", "manager", "engcenter")
    fac = _fac(u)
    t0 = today()
    frm = (frm or t0)[:10]
    to = (to or frm)[:10]
    if to < frm:
        frm, to = to, frm
    if (date.fromisoformat(to) - date.fromisoformat(frm)).days > 62:
        raise HTTPException(400, "62 days at most")
    with closing(db()) as c:
        _ensure(c)
        off_wd, hol = _calendar(c, fac)
        techs = _techs(c, fac, _as_ac(req, u))
        offs = {}
        for r in c.execute("SELECT day, user_id FROM mp_off WHERE factory_id=? AND day BETWEEN ? AND ?",
                           (fac, frm, to)):
            offs.setdefault(r["day"], set()).add(r["user_id"])
        hist = _history(c, fac)
        tc = {}
        work = {}                                 # day -> list of items
        jobs = [dict(r) for r in c.execute(
            """SELECT j.id, j.machine_id, j.jobtype, j.pm_freq, j.planned_date, j.problem_type,
                      j.fault_category, j.trade, j.lead_tech
                 FROM jobs j LEFT JOIN machines m ON m.id = j.machine_id
                WHERE j.planned_date BETWEEN ? AND ? AND j.status NOT IN """ + OPEN + """
                  AND (COALESCE(m.factory_id, j.factory_id) = ? OR COALESCE(m.factory_id, j.factory_id) IS NULL)""",
            (frm, to, fac))]
        overdue = []
        if frm <= t0 <= to:
            overdue = [dict(r) for r in c.execute(
                """SELECT j.id, j.machine_id, j.jobtype, j.pm_freq, j.problem_type, j.fault_category, j.trade
                     FROM jobs j LEFT JOIN machines m ON m.id = j.machine_id
                    WHERE COALESCE(j.planned_date,'') != '' AND j.planned_date < ?
                      AND j.status NOT IN """ + OPEN + """
                      AND (COALESCE(m.factory_id, j.factory_id) = ? OR COALESCE(m.factory_id, j.factory_id) IS NULL)""",
                (t0, fac))]
        # b599: AC work is the AC planner's; nobody else's need counts it
        from .elec import AcFilter, is_ac_planner
        _acf, _ac = AcFilter(c), is_ac_planner(_as_ac(req, u))
        jobs = [j for j in jobs if bool(_acf.ok(dict(j, factory_id=fac))) == _ac]
        overdue = [j for j in overdue if bool(_acf.ok(dict(j, factory_id=fac))) == _ac]
        own = _own(c, [j["id"] for j in jobs + overdue])
        for j in jobs:
            work.setdefault(str(j["planned_date"])[:10], []).append(
                dict(j, freq=j.get("pm_freq"), trade=_trade(c, fac, tc, j)))
        # PM the programme puts on a day with no work order yet (as the board's DUE cards)
        try:
            from . import pm as _pm
            occ = _pm._occurrences(c, fac, date.fromisoformat(frm), date.fromisoformat(to))
            have = set()
            for r in c.execute("SELECT machine_id, planned_date, due_date FROM jobs WHERE jobtype='PM'"
                               " AND machine_id IS NOT NULL AND status NOT IN ('Cancelled','Rejected')"
                               " AND (planned_date BETWEEN ? AND ? OR due_date BETWEEN ? AND ?)",
                               (frm, to, frm, to)):
                for v in (r["planned_date"], r["due_date"]):
                    if v:
                        have.add((r["machine_id"], str(v)[:10]))
            for o in occ:
                dd = str(o.get("date") or "")[:10]
                if o.get("cm") or not dd or (o["machine_id"], dd) in have:
                    continue
                if _acf.ac_machine(fac, o["machine_id"]) != _ac:
                    continue
                work.setdefault(dd, []).append({"machine_id": o["machine_id"], "jobtype": "PM",
                                                "freq": o.get("freq"), "trade": "PM"})
        except Exception:
            pass
        days = []
        dd = date.fromisoformat(frm)
        while dd.isoformat() <= to:
            d = dd.isoformat()
            wd = _workday(d, off_wd, hol)
            off = offs.get(d, set())
            av_t = [t for t in techs if t["id"] not in off] if wd else []
            avail = round(sum(_day_hours(t, off_wd) for t in av_t), 1)
            need, by = 0.0, {}
            for it in work.get(d, []):
                h, _b = _estimate(it, hist, own)
                need += h
                by[it["trade"]] = round(by.get(it["trade"], 0) + h, 1)
            row = {"d": d, "workday": wd, "techs": len(av_t), "off": len(off) if wd else 0,
                   "avail": avail, "need": round(need, 1), "jobs": len(work.get(d, [])),
                   "by_trade": by, "short": round(max(0.0, need - avail), 1) if wd else 0}
            if d == t0 and overdue:
                row["overdue_h"] = round(sum(_estimate(dict(j, freq=j.get("pm_freq")), hist, own)[0]
                                             for j in overdue), 1)
                row["overdue_n"] = len(overdue)
            days.append(row)
            dd += timedelta(days=1)
    hpd = round(sum(_day_hours(t, off_wd) for t in techs) / max(1, len(techs)), 1) if techs else 0
    return {"from": frm, "to": to, "techs": len(techs), "hpd": hpd, "days": days}


def _as_ac(req, u):
    """b601: see elec.as_ac — the admin's ❄️ Air-con Planning view."""
    from .elec import as_ac
    return as_ac(req, u)

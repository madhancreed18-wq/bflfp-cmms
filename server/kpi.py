"""Shift logs + KPI engine (OEE / MTBF / MTTR per OPERATIONS-DESIGN.md §4)
and PM auto-recurrence (§ PM flow)."""
from datetime import datetime, date, timedelta
from contextlib import closing

from fastapi import APIRouter, Request, HTTPException

from .db import (db, now, today, day_range, mins_between, next_jobid, VOID_SQL,
                 NOTKPI, NOTKPI_J, user_names, day_windows)
from . import downtime as dt

# ── When is a PM late? ───────────────────────────────────────────────────────────
# Not on its due date. A PM belongs to a PERIOD, and the plant's rule is that the
# work is on time if it is finished inside that period: a weekly PM planned for
# Thursday and done on Saturday was done that week, and the schedule held. Judging it
# on the exact day made a plan that moves one job to the next morning look like a
# failure — thirty jobs planned, twenty-five done, five finished next day is not a
# missed schedule, and reading it as one taught everybody to distrust the figure.
#
# The period, by frequency:
#   weekly (and any job with no frequency recorded) → to the end of that week,
#       Monday-Sunday. Sunday counts: the crew works shutdown days and picks up PM
#       when there is room, which is exactly the catch-up the rule is meant to allow.
#   monthly, 3-month, 6-month, yearly → to the end of the month it is due in. Those
#       are planned to a month, not to a day, and giving a 3-monthly PM three whole
#       months to be done in would measure nothing at all.
#
# date(d,'weekday 0') is SQLite's "the next Sunday, or d itself if d IS Sunday",
# which is the end of the Monday-Sunday week containing d.
PM_END = ("CASE WHEN COALESCE(j.pm_freq,'') IN ('','weekly','w')"
          " THEN date(j.due_date,'weekday 0')"
          " ELSE date(j.due_date,'start of month','+1 month','-1 day') END")

# On time = closed on or before the end of its period.
PM_ONTIME = ("(j.status IN ('ServiceCompleted','Done') AND j.done_at IS NOT NULL"
             " AND j.done_at != '' AND SUBSTR(j.done_at,1,10) <= " + PM_END + ")")

# A period that has not closed yet cannot have been missed, so its unfinished jobs are
# not counted as due — they are counted separately and shown as work still to come.
# Without this the ring reads low from Monday morning purely because the week is young,
# and a figure that is red by design is a figure nobody reads.
PM_COUNTS = ("(j.status IN ('ServiceCompleted','Done') OR "
             + PM_END + " < date('now','localtime'))")

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
    u = user_from(req)
    fac = u.get("active_factory") or u.get("factory_id")     # the plant chosen at login
    d = d or today()
    with closing(db()) as c:
        return [dict(r) for r in c.execute("""SELECT s.*, m.code mcode, u.name entered_name
            FROM shiftlogs s JOIN machines m ON m.id=s.machine_id
            LEFT JOIN users u ON u.id=s.entered_by
            WHERE s.log_date=? AND m.factory_id=? ORDER BY s.shift, m.code""", (d, fac))]


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
    u = user_from(req)
    d_to = d_to or today()
    d_from = d_from or (date.fromisoformat(d_to) - timedelta(days=6)).isoformat()
    _nowts = now()                       # an open breakdown is still down, right now
    _fac_id = u.get("active_factory") or u.get("factory_id")
    with closing(db()) as c:
        # ── ONE plant at a time ───────────────────────────────────────────────────
        # This used to read every active machine in the database, all three plants at
        # once, while the downtime panel beside it filtered to the plant the user was
        # actually looking at. With 1,671 machines on the register and breakdowns
        # concentrated in one plant, a fleet availability averaged over the other two
        # is not a number about anywhere — it is 800 petcare machines voting on how
        # BFLFP's week went. The factory bar at the top of the page says which plant
        # is being reported on; this now agrees with it.
        machines = {m["id"]: dict(m) for m in c.execute(
            "SELECT * FROM machines WHERE active=1 AND factory_id=?", (_fac_id,))}
        prod = {r["machine_id"]: dict(r) for r in c.execute("""
            SELECT machine_id, SUM(planned_min) pm, SUM(output) out, SUM(good) gd
            FROM shiftlogs WHERE log_date BETWEEN ? AND ? GROUP BY machine_id""",
            (d_from, d_to))}
        f0, t1 = d_from + " 00:00:00", d_to + " 23:59:59"
        # One plant, everywhere on this endpoint. The machine list above was scoped and
        # the job queries below were not, so the plant totals were built from one plant's
        # machines and three plants' jobs: the downtime headline read four hours higher
        # than the panel underneath it, which is the sort of disagreement that costs a
        # dashboard its credibility long before anyone works out which half was wrong.
        # A job is placed by its machine's plant, falling back to its own factory_id for
        # rows with no machine attached — the same rule /api/kpi/downtime already used.
        FJ = (" AND (COALESCE(m.factory_id,j.factory_id)=?"
              " OR COALESCE(m.factory_id,j.factory_id) IS NULL) ")
        bd_jobs = [dict(r) for r in c.execute("""
            SELECT j.id, j.machine_id, j.created_at, j.done_at, j.status
            FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
            WHERE j.jobtype='BD' AND j.created_at >= ? AND j.created_at <= ?
              AND j.status NOT IN """ + VOID_SQL + NOTKPI_J + FJ, (f0, t1, _fac_id))]
        # MTBF used to be read off a fixed 180-day history of breakdown reports — the
        # average calendar gap between one and the next — no matter which window the
        # rest of the page was showing. Two things were wrong with that. It answered
        # for a period nobody had asked for, so picking "This week" changed every
        # figure on the screen except this one; and it is not the standard's formula.
        # The ENG standard is running hours ÷ breakdowns, over the window in hand, and
        # that is now computed in the per-machine loop below from the same required
        # time and the same downtime that availability uses. Nothing here needs a
        # 180-day query any more.

        # ── the events behind the breakdowns, for the downtime split ───────────────
        # This comment used to say the opposite of what the code does, which is worse
        # than no comment: it described MTTR as engineering downtime ÷ breakdowns, an
        # earlier design, while the line that computes it has summed the technician's
        # logged Start→Stop minutes for months. A reviewer reading the two together can
        # only conclude the number is wrong.
        #
        # It is not. The plant chose the ENG standard's own wording — **total breakdown
        # repair hours ÷ breakdown count** — and repair hours are the hours somebody
        # logged repairing. It reads far lower than the time the machines were actually
        # stopped, because it can only count minutes the crew told the app about, and
        # that gap is itself the thing worth seeing: it is the Start/Stop habit, not
        # repair skill.
        #
        # So BOTH are on the row, and the second is named so it cannot be read as the
        # standard: `fleet_down_per_bd_min` is the whole time the machine was off —
        # report → production accepts it back — and it is what the downtime panel below
        # is built from. Two questions, two numbers, neither pretending to be the other.
        _bdev = {}
        if bd_jobs:
            _qs = ",".join("?" * len(bd_jobs))
            for e in c.execute(
                    f"SELECT job_id,status,created_at FROM job_events"
                    f" WHERE job_id IN ({_qs}) ORDER BY job_id, id",
                    [j["id"] for j in bd_jobs]):
                _bdev.setdefault(e["job_id"], []).append((e["status"], e["created_at"]))

        import json as _json
        _fac = u.get("active_factory") or u.get("factory_id")
        _fr = c.execute("SELECT hours_json FROM factories WHERE id=?", (_fac,)).fetchone()
        try:
            _cfg = _json.loads(_fr["hours_json"]) if _fr and _fr["hours_json"] else {}
        except Exception:
            _cfg = {}
        _dsh = int(str(_cfg.get("start") or "07:00").split(":")[0])
        _deh = int(str(_cfg.get("end") or "21:00").split(":")[0])
        _deh = 24 if _deh == 0 else _deh      # end 00:00 = runs to midnight (24/7)
        # Every call below passes hours from _hours_for, which already does this;
        # the defaults are converted too so a future bare call cannot quietly
        # return zero operating minutes for a plant that never stops.
        _offdays = set(_cfg["weekly_off"] if _cfg.get("weekly_off") is not None else [6])  # 6 = Sunday
        _holidays = set(_cfg.get("holidays") or [])
        _groups = _cfg.get("groups") or {}
        # Weekdays that work their own hours rather than the plant's — Sunday 07:00-20:00
        # is 13 hours, not a day off and not a full 14. A group may name its own, and
        # they layer over the plant's: {**plant, **group}. See db.day_windows.
        _cfgdays = day_windows(_cfg)

        def _hours_for(mid):   # group hours (bucket elevator / compressor / boiler …) else factory default
            gc = _groups.get((machines.get(mid) or {}).get("asset_group") or "") or {}
            sh = int(str(gc.get("start") or _cfg.get("start") or "07:00").split(":")[0])
            eh = int(str(gc.get("end") or _cfg.get("end") or "21:00").split(":")[0])
            days = {**_cfgdays, **day_windows(gc)} if gc else _cfgdays
            return sh, (24 if eh == 0 else eh), days   # end 00:00 = runs to midnight (24/7)

        def _op_minutes(a, b, sh=_dsh, eh=_deh, days=None):
            # working-window minutes, skipping off-days + holidays; eh>=24 = runs to midnight (24/7)
            # A weekday in `days` carries its own window and overrides both the hours
            # above and the off-day list — the one place a 13-hour Sunday can be said.
            ta = datetime.strptime(a[:19], "%Y-%m-%d %H:%M:%S")
            tb = datetime.strptime(b[:19], "%Y-%m-%d %H:%M:%S")
            if tb <= ta:
                return 0.0
            total, cur = 0.0, ta
            while cur.date() <= tb.date():
                _wd = cur.weekday()
                if days and _wd in days:
                    _win = days[_wd]
                    _open, _sh, _eh = (_win is not None), (_win or (0, 0))[0], (_win or (0, 0))[1]
                else:
                    _open, _sh, _eh = (_wd not in _offdays), sh, eh
                if _open and cur.date().isoformat() not in _holidays:
                    ws = cur.replace(hour=_sh, minute=0, second=0, microsecond=0)
                    we = (cur.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)) \
                        if _eh >= 24 else cur.replace(hour=_eh, minute=0, second=0, microsecond=0)
                    s, e = max(ta, ws), min(tb, we)
                    if e > s:
                        total += (e - s).total_seconds() / 60
                cur = (cur + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return total

        def _split_of(j):
            # The clock stops at the END OF THE WINDOW, never at today. A breakdown that
            # is still open ran to `now` while the required time it is measured against
            # stopped at the window — so asking for last month, five weeks of downtime
            # was charged against four weeks of required time, run clamped to zero, and
            # the machine read 0% available with nothing on screen to say why. Both
            # sides of the subtraction now end at the same minute (_win_end is itself
            # min(window end, now), so a current window still stops at this minute).
            sh, eh, days = _hours_for(j.get("machine_id"))
            return dt.split(j["created_at"], _bdev.get(j["id"], []),
                            (sh, eh, _offdays, _holidays, days), _win_end,
                            (j.get("done_at"), j.get("approved_at")))

        # The window's own end, never later than this minute. Downtime already stops at
        # now — an open breakdown is counted up to the present and no further — so the
        # required time it is measured against has to stop there too. Left uncapped,
        # "Today" at nine in the morning would divide a real hour of downtime by a whole
        # day of required time and report availability that has not been earned yet.
        _win_end = min(t1, _nowts)

        def _required_min(mid):
            """Required time for one machine over the window, in minutes.

            The standard's denominator is required time — planned production time, not
            calendar hours. That is exactly what shiftlogs.planned_min holds, and it is
            used wherever the plant has entered it (see the loop below). This is the
            fallback for machines with no shift log in the window: the machine's own
            operating window, off-days and holidays skipped, asset-group hours
            respected. It is the same clock the downtime is counted on, so the
            subtraction stays honest, and each machine reports which of the two sources
            it used so a fallback is never mistaken for a production plan.
            """
            sh, eh, days = _hours_for(mid)
            # Memoised on the window, not the machine. _op_minutes walks the range a
            # day at a time, and a plant carries hundreds of machines that share two or
            # three working windows between them — a boiler group, a compressor group,
            # the factory default. Computing the same quarter eight hundred times is
            # several seconds of the manager waiting for a number that was already
            # known after the first one.
            _key = (sh, eh, tuple(sorted((days or {}).items())))
            if _key not in _reqcache:
                _reqcache[_key] = _op_minutes(f0, _win_end, sh, eh, days)
            return _reqcache[_key]
        _reqcache = {}
        repair = {}
        response = {}
        seg_rows = [dict(r) for r in c.execute("""SELECT j.id jid, j.machine_id mid,
                j.created_at jcreated, t.start s, t.end e, t.seg_type st
            FROM jobs j JOIN timelogs t ON t.job_id=j.id
                 LEFT JOIN machines m ON m.id=j.machine_id
            WHERE j.jobtype='BD' AND j.created_at >= ? AND j.created_at <= ?
              AND j.status NOT IN """ + VOID_SQL + NOTKPI_J + FJ, (f0, t1, _fac_id))]
        # Repair minutes and response time are counted on the SAME clock as everything
        # else on this page — the machine's own operating window, off-days and holidays
        # skipped — and they stop at the end of the window rather than at this minute.
        # On wall-clock, one timer a technician forgot to stop on Friday afternoon added
        # the whole weekend to the plant's MTTR, and a Saturday-night report picked up on
        # Monday booked thirty-six hours of response time for a plant that was shut.
        def _op(a, b, mid):
            sh, eh, days = _hours_for(mid)
            return dt.op_minutes(a, min(b, _win_end) if b else _win_end,
                                 sh, eh, _offdays, _holidays, days)

        first_start = {}
        for r in seg_rows:
            if r["st"] == "work":
                repair[r["mid"]] = repair.get(r["mid"], 0) + _op(r["s"], r["e"], r["mid"])
            k = r["jid"]
            if r["s"] and (k not in first_start or r["s"] < first_start[k][1]):
                first_start[k] = (r["mid"], r["s"], r["jcreated"])
        for mid, s, jcreated in first_start.values():
            response.setdefault(mid, []).append(_op(jcreated, s, mid))
        # Per machine, the same three counts the plant ring is built from — and off
        # due_date, not planned_date. The plant figure was already computed against the
        # date the PM was DUE while this one used the date it was PLANNED for, so a PM
        # due in the window but planned into the next one appeared in one and not the
        # other, and the machine rows could not be made to add up to the ring above
        # them. One date, one meaning, both places.
        pm_jobs = {r["machine_id"]: dict(r) for r in c.execute("""
            SELECT j.machine_id machine_id,
              SUM(CASE WHEN j.status IN ('ServiceCompleted','Done') THEN 1 ELSE 0 END) done,
              SUM(CASE WHEN """ + PM_ONTIME + """ THEN 1 ELSE 0 END) ontime,
              COUNT(*) total
            FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
            WHERE j.jobtype='PM'
              AND j.due_date IS NOT NULL AND j.due_date != ''
              AND j.due_date BETWEEN ? AND ?
              AND j.status NOT IN """ + VOID_SQL + NOTKPI_J + FJ
            + " GROUP BY j.machine_id", (d_from, d_to, _fac_id))}

    # Downtime the way the panel below counts it: operating hours, engineering and
    # production back to back, open jobs still running. NOT the same as the per-machine
    # `downtime` in the OEE loop, which is planned-production minutes lost and belongs
    # to a different calculation — they are kept apart on purpose.
    # Split once per job and keep it. Availability, MTBF and this pair all need the
    # same numbers, and dt.split walks the event list day by day — computing it three
    # times over a quarter of breakdowns is work nobody sees and everybody waits for.
    _sp = {j["id"]: _split_of(j) for j in bd_jobs}
    # ONE population behind every figure on this card. The machine list is active
    # machines in this plant; `bd_jobs` is wider — it also admits a breakdown raised on
    # no asset at all, or on a machine somebody has since deactivated. Those rows can
    # reach no per-machine figure (there is no required time to measure them against),
    # so MTTR and MTBF were already dividing by the narrower count while the "N BD"
    # printed beside them, and the downtime per breakdown, used the wider one. Same
    # card, two different N — invisible today because every breakdown sits on a live
    # machine, and quietly wrong the first time one is decommissioned.
    _inscope = [j for j in bd_jobs if j.get("machine_id") in machines]
    _offreg = len(bd_jobs) - len(_inscope)      # raised on no asset, or on a dead one
    _eng = sum(_sp[j["id"]]["eng_min"] for j in _inscope)
    _prod = sum(_sp[j["id"]]["prod_min"] for j in _inscope)

    out, plant = [], {"planned": 0.0, "required": 0.0, "run": 0.0, "ideal_good_min": 0.0,
                      "bd": 0, "repair": 0.0, "downtime": 0.0, "req_plan": 0, "req_hours": 0,
                      # the MTBF numerator, and it is NOT plant["run"] — see below
                      "fail_run": 0.0, "fail_n": 0}
    for mid, m in machines.items():
        p = prod.get(mid, {})
        planned = float(p.get("pm") or 0)          # the production plan, from the shift logs
        output = float(p.get("out") or 0)
        good = float(p.get("gd") or 0)
        my_bd = [j for j in bd_jobs if j["machine_id"] == mid]
        # ── Downtime and required time, on ONE clock ──────────────────────────────
        # This used to be wall-clock minutes from the report to done_at, subtracted
        # from the shift logs' planned production minutes: two different clocks either
        # side of the same minus sign. A breakdown reported at 18:00 on a plant that
        # stops at 21:00 and repaired at 09:00 the next morning charged fifteen hours
        # against a day that only ever had fourteen planned, and availability went
        # negative until max() hid it at zero.
        #
        # dt.split counts operating hours only — off-days and holidays skipped, the
        # machine's asset-group window respected — which is the clock the production
        # plan is written on. Open breakdowns count too, running to now: a machine that
        # is still down is still down, and leaving it out until somebody closes the job
        # is how a bad week reads clean right up to the moment it is over.
        downtime = sum(_sp[j["id"]]["total_min"] for j in my_bd)
        req_src = "plan" if planned else "hours"
        required = planned if planned else _required_min(mid)
        # ── Both sides of the subtraction on ONE clock, including the plan path ─────
        # Downtime is counted in OPERATING minutes. Where a production plan exists,
        # required time comes from the shift log instead — and a plan of 8 hours inside
        # a 14-hour operating window is a different clock, so ten operating hours lost
        # could be subtracted from eight planned and run would clamp to zero on a
        # machine that was not down all shift.
        #
        # The shift log records planned MINUTES and no start or end time, so which
        # hours were planned is not knowable from the data. The honest reading is the
        # machine's downtime scaled by the share of its operating window the plan
        # covers — it assumes a breakdown is as likely in one hour of the window as
        # another, which is the only assumption the data supports. Marked `est` so the
        # page can say the plan's availability is estimated, rather than implying a
        # precision the shift log does not carry. Add start/end to the shift log and
        # this becomes exact.
        down_src = "operating"
        if planned:
            _win = _required_min(mid)
            if _win > 0 and planned < _win:
                downtime = downtime * (planned / _win)
                down_src = "scaled-to-plan"
        run = max(0.0, required - downtime)        # running time — the MTBF numerator
        oee_run = max(0.0, planned - downtime)     # OEE stays on the production plan alone
        rep = repair.get(mid, 0.0)
        n_bd = len(my_bd)
        rate = float(m.get("ideal_rate") or 0)
        A = run / required * 100 if required else None
        # Gated on output as well as rate and time: with no shift log there is no output
        # to rate, and a bare 0% performance reads as a machine running badly rather
        # than one nobody logged.
        P = min(100.0, output / (rate * oee_run) * 100) if (rate and oee_run and output) else None
        Q = good / output * 100 if output else None
        oee = A * P * Q / 10000 if None not in (A, P, Q) else None
        resp = response.get(mid, [])
        pmj = pm_jobs.get(mid, {})
        out.append({
            "machine_id": mid, "code": m["code"], "name": m["name"],
            "line": m["line"], "criticality": m["criticality"],
            "planned_min": round(planned), "run_min": round(run),
            "required_min": round(required), "required_src": req_src,
            "downtime_src": down_src,
            "downtime_min": round(downtime), "repair_min": round(rep),
            "bd_count": n_bd,
            # ENG standard, both of them, over the window on screen:
            #   MTBF = running hours ÷ breakdowns
            #   MTTR = repair hours  ÷ breakdowns
            "mtbf_min": round(run / n_bd) if n_bd else None,
            "mttr_min": round(rep / n_bd) if n_bd else None,
            # what the machine was actually held for, per breakdown — not MTTR, and
            # named so it cannot be read as MTTR
            "down_per_bd_min": round(downtime / n_bd) if n_bd else None,
            "response_min": round(sum(resp) / len(resp)) if resp else None,
            "availability": round(A, 1) if A is not None else None,
            "performance": round(P, 1) if P is not None else None,
            "quality": round(Q, 1) if Q is not None else None,
            "oee": round(oee, 1) if oee is not None else None,
            "pm_done": pmj.get("done", 0), "pm_total": pmj.get("total", 0),
            "pm_ontime": int(pmj.get("ontime") or 0),
            "output": round(output), "good": round(good),
        })
        plant["planned"] += planned
        plant["required"] += required
        plant["run"] += run
        plant["downtime"] += downtime
        plant["repair"] += rep
        plant["bd"] += n_bd
        plant["req_plan" if planned else "req_hours"] += 1
        # A machine that did not break down has no time BETWEEN failures to contribute.
        # Its running hours belong in availability's denominator and nowhere near MTBF.
        if n_bd:
            plant["fail_run"] += run
            plant["fail_n"] += 1
        if rate:
            plant["ideal_good_min"] += good / rate

    # Availability to the standard: (required time − downtime) ÷ required time × 100.
    #
    # Rolled up over the plant this is a deliberately insensitive number, and the page
    # has to say so. 157 machines share one denominator, so a machine down for an
    # entire week moves the fleet figure by well under a percent and the ring reads
    # 99% on a week that felt like a bad one on the floor. That is not a fault in the
    # arithmetic — it is what a fleet average of a per-machine KPI means — but a ring
    # nobody can move is a ring nobody reads.
    #
    # So the count of machines that individually missed the target rides along with
    # it. The ring answers "how much of the plant's required time was available"; the
    # count answers "how many machines let us down", and the second is the one that
    # changes week to week and points at something a planner can do.
    pa = plant["run"] / plant["required"] * 100 if plant["required"] else None
    _AV_T = 90
    _av = [m for m in out if m["availability"] is not None]
    avail_below = sum(1 for m in _av if m["availability"] < _AV_T)
    avail_worst = sorted([m for m in _av if m["bd_count"]],
                         key=lambda m: m["availability"])[:5]
    plant_oee = (plant["ideal_good_min"] / plant["planned"] * 100
                 if plant["planned"] and plant["ideal_good_min"] else None)

    with closing(db()) as c:
        # FTFR: done jobs fixed first time (no rework, no re-issue)
        f = c.execute("""SELECT COUNT(*) total,
              SUM(CASE WHEN j.rework_count=0 AND (j.new_issue_id='' OR j.new_issue_id IS NULL)
                  THEN 1 ELSE 0 END) first
            FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
            WHERE j.status IN ('Done','ServiceCompleted')
            AND j.created_at >= ? AND j.created_at <= ?""" + FJ, (f0, t1, _fac_id)).fetchone()
        ftfr = round(f["first"] / f["total"] * 100, 1) if f["total"] else None
        # ── Planned vs reactive, from actual wrench minutes ────────────────────────
        # CM used to be counted as PLANNED work, alongside PM and IMP. It is not, and the
        # mistake made the number say the opposite of the truth: a plant that had
        # completed zero PM jobs read 98% "planned", because nearly every logged hour was
        # corrective.
        #
        # A corrective job exists because something is already wrong — the fault has
        # happened and the work is a response to it. That is reactive by definition
        # (EN 13306), and it is the whole point of the ratio: how much of the crew's time
        # goes into stopping machines from failing, against clearing up after they have.
        # Counting the clearing-up as prevention leaves a number that can never fall.
        #
        #   planned   PM   scheduled preventive work
        #             IMP  improvement and modification — chosen, not forced
        #             PRJ  a project — chosen, planned, and usually months long
        #   reactive  CM   corrective: the fault is already there
        #             BD   breakdown: the machine is already stopped
        #
        # The components go back with it, so the card can show its own arithmetic and a
        # figure this uncomfortable can be checked rather than argued with.
        PLANNED_TYPES = ("PM", "IMP", "PRJ")
        by_type = {}
        for r in c.execute("""SELECT j.jobtype jt, t.start s, t.end e
            FROM timelogs t JOIN jobs j ON j.id=t.job_id
                 LEFT JOIN machines m ON m.id=j.machine_id
            WHERE t.seg_type='work' AND t.start >= ? AND t.start <= ?
              AND j.status NOT IN """ + VOID_SQL + NOTKPI_J + FJ, (f0, t1, _fac_id)):
            by_type[r["jt"]] = by_type.get(r["jt"], 0.0) + _minutes(r["s"], r["e"])
        pm_min = sum(v for k, v in by_type.items() if k in PLANNED_TYPES)
        tot_min = sum(by_type.values())
        planned_ratio = round(pm_min / tot_min * 100, 1) if tot_min else None
        work_min_by_type = {k: round(v) for k, v in sorted(by_type.items())}
        # backlog age buckets (portable: Python)
        bl = {"b0": 0, "b7": 0, "b30": 0, "p1_late": 0}
        now_dt = datetime.now()
        for r in c.execute("SELECT j.created_at created_at, j.priority priority"
                           " FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id"
                           f" WHERE j.status IN {ACTIVE_J}" + FJ, (_fac_id,)):
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
        top_fail = [dict(r) for r in c.execute("""SELECT j.fault_component comp, COUNT(*) n
            FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
            WHERE j.fault_component!='' AND j.created_at >= ? AND j.created_at <= ?
              AND j.status NOT IN """ + VOID_SQL + NOTKPI_J + FJ + """
            GROUP BY j.fault_component ORDER BY n DESC LIMIT 5""", (f0, t1, _fac_id))]
        # ── PM compliance, to the ENG standard ───────────────────────────────────
        #   PM work orders completed ON TIME ÷ PM work orders DUE × 100
        #
        # It used to be completed ÷ due, which answers a different question. A PM
        # closed three weeks after its due date counted exactly like one closed on the
        # day, so the number could say whether the work happened but never whether the
        # schedule held — and on a plant that catches up on its PMs at the end of the
        # month those are not the same plant. The standard's reading is the lower one,
        # and it is the one that moves when the habit changes.
        #
        # On time = closed on or before the due date. There is no new column for it:
        # due_date and done_at are both already on the row, and deriving the answer
        # means it cannot drift out of step with the job it came from. A flag written
        # at close time would keep whatever it was set to on the day, so correcting a
        # due date afterwards would leave the compliance figure quoting the old one.
        #
        # The denominator keeps the rule it already had — PMs due inside the window,
        # plus any older PM still open. An overdue PM nobody has touched is the single
        # most important thing this number has to admit to, and dropping it once its
        # due date scrolls out of the window is how compliance climbs while the backlog
        # grows. What is new is the guard on due_date itself: '' compares as less than
        # any real date in SQL, so every PM with no due date at all was being swept
        # into the overdue arm of that clause and counted as due. A job with no due
        # date cannot be late.
        pm_all = c.execute("""SELECT
              SUM(CASE WHEN """ + PM_COUNTS + """ THEN 1 ELSE 0 END) due,
              SUM(CASE WHEN j.status IN ('ServiceCompleted','Done') THEN 1 ELSE 0 END) done,
              SUM(CASE WHEN """ + PM_ONTIME + """ THEN 1 ELSE 0 END) ontime,
              SUM(CASE WHEN """ + PM_COUNTS + """ THEN 0 ELSE 1 END) notdue
              FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
              WHERE j.jobtype='PM'
              AND j.due_date IS NOT NULL AND j.due_date != ''
              AND j.status NOT IN """ + VOID_SQL + NOTKPI_J + FJ + """
              AND ((j.due_date >= ? AND j.due_date <= ?)
                   OR (j.due_date < ? AND j.status NOT IN ('Done','Rejected','Cancelled')))""",
              (_fac_id, d_from, d_to, d_from)).fetchone()
        pm_due = int(pm_all["due"] or 0)
        pm_done_n = int(pm_all["done"] or 0)
        pm_ontime = int(pm_all["ontime"] or 0)
        # unfinished work whose period is still running: not late, not ignored either
        pm_notdue = int(pm_all["notdue"] or 0)
        pm_comp = round(pm_ontime / pm_due * 100, 1) if pm_due else None
        # The working, so the ring can show its own arithmetic. `late` is the gap
        # between the old number and this one — the jobs that got done but not on
        # time — and it is the first thing anyone asks about when the figure drops.
        pm_stat = {"due": pm_due, "ontime": pm_ontime, "done": pm_done_n,
                   "late": pm_done_n - pm_ontime, "open": max(0, pm_due - pm_done_n),
                   # still to do, in a week or month that has not ended yet
                   "not_due": pm_notdue}

        # ── Open vs completed, over the SAME window as MTTR / MTBF ──────────────
        # MTTR and MTBF are averages. Read on their own they say nothing about whether
        # the work was actually finished — a quick MTTR on a plant carrying forty open
        # jobs is not a plant in good order. So the same window is counted a second
        # time and split into raised against closed: CM against completed CM, PM
        # against completed PM. Whatever is left is what is still open.
        #
        # The window is deliberately the reporting window, not a live backlog count.
        # The backlog is a now-figure; setting it beside a 7-day average would put two
        # different periods on the same row and invite exactly the wrong conclusion.
        #
        # CM = CM + BD, PM = PM only — the same split as the completion ring on the
        # dashboard, so the two cannot disagree. IMP is reported on its own.
        # `back` counts the finished jobs the operator sent back at least once before
        # accepting them. It is the plant's FIRST-TIME-RIGHT figure, and it is the one
        # quality number a completion count cannot contain: a job that was returned
        # twice and finally accepted closes as "done" exactly like one that was right
        # the first time. rework_count is never reset, so the fact survives the job's
        # own history — which is what makes the figure worth reporting a month later.
        _oc_raw = {}
        for r in c.execute("SELECT j.jobtype jt, COUNT(*) total, "
                           "SUM(CASE WHEN j.status IN ('Done','ServiceCompleted') "
                           "THEN 1 ELSE 0 END) done, "
                           "SUM(CASE WHEN j.status IN ('Done','ServiceCompleted') "
                           "AND COALESCE(j.rework_count,0) > 0 THEN 1 ELSE 0 END) back "
                           "FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id "
                           "WHERE j.created_at >= ? AND j.created_at <= ? "
                           "AND j.status NOT IN " + VOID_SQL + NOTKPI_J + FJ +
                           " GROUP BY j.jobtype", (f0, t1, _fac_id)):
            _oc_raw[r["jt"]] = (r["total"], int(r["done"] or 0), int(r["back"] or 0))

        def _oc(*types):
            g = lambda i: sum(_oc_raw.get(k, (0, 0, 0))[i] for k in types)
            tt, dd, bk = g(0), g(1), g(2)
            return {"total": tt, "done": dd, "open": tt - dd, "back": bk,
                    # first time right: of what was CLOSED, how much was never returned
                    "ftr": round((dd - bk) / dd * 100, 1) if dd else None,
                    "pct": round(dd / tt * 100, 1) if tt else None}

        open_done = {"cm": _oc("CM", "BD"), "pm": _oc("PM"),
                     "by_type": {k: _oc(k) for k in sorted(_oc_raw)}}

        # ── Work that is not on an asset ──────────────────────────────────────────
        # Jobs raised on something the register does not hold: pipework, a new install,
        # a machine nobody has coded yet. They count in the job figures above — the work
        # was done and somebody's day went into it — and they can appear in no per-asset
        # figure, because availability needs a machine's required time and MTBF and MTTR
        # need its breakdown count. That is not a rule imposed here; it is what having no
        # machine row means, and every per-asset figure on this page is built by walking
        # the machines table.
        #
        # The count is returned so the dashboard can SAY so. An exclusion nobody can see
        # is indistinguishable from a figure that is quietly wrong, and this is the one
        # people will ask about the first time the counts do not tie out.
        _na = c.execute("SELECT COUNT(*) n FROM jobs j"
                        " WHERE j.machine_id IS NULL"
                        " AND j.created_at >= ? AND j.created_at <= ?"
                        " AND j.status NOT IN " + VOID_SQL + NOTKPI_J
                        + ("" if _fac_id is None
                           else " AND (j.factory_id=? OR j.factory_id IS NULL)"),
                        (f0, t1) if _fac_id is None else (f0, t1, _fac_id)).fetchone()
        no_asset = int((_na["n"] if _na else 0) or 0)

    # b435: wall-clock BD time, report → Stop (done_at), cut to the window
    from datetime import datetime as _dtm
    def _ts(x):
        try:
            return _dtm.strptime(str(x)[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    _we, _ws = _ts(_win_end), _ts(f0)
    _bd_wall = 0.0
    for j in _inscope:
        a0, b0 = _ts(j.get("created_at")), _ts(j.get("done_at")) or _we
        if a0 and b0 and _we:
            a0, b0 = max(a0, _ws or a0), min(b0, _we)
            if b0 > a0:
                _bd_wall += (b0 - a0).total_seconds() / 60.0
    _wdays = 0
    try:
        _d, _e = date.fromisoformat(d_from), min(date.fromisoformat(d_to), date.fromisoformat(today()))
        while _d <= _e:
            if _d.weekday() not in _offdays and _d.isoformat() not in _holidays:
                _wdays += 1
            _d += timedelta(days=1)
    except Exception:
        pass
    return {
        # how many of the job figures above stand on no asset, and so reach none of the
        # per-machine figures below them
        "no_asset": no_asset,
        "from": d_from, "to": d_to, "machines": out,
        "plant": {
            "availability": round(pa, 1) if pa is not None else None,
            "oee": round(plant_oee, 1) if plant_oee is not None else None,
            "downtime_min": round(_eng + _prod),      # operating hours, like the panel
            "downtime_eng_min": round(_eng), "downtime_prod_min": round(_prod),
            "bd_count": len(_inscope),
            # breakdowns that reach no per-machine figure, counted so the page can say so
            "bd_off_register": _offreg,
            "window_days": (_wd := (date.fromisoformat(d_to)
                                    - date.fromisoformat(d_from)).days + 1),
            # ── MTBF and MTTR, both to the ENG standard, both on this window ───────
            #   MTBF = total running hours ÷ number of breakdowns
            #   MTTR = total repair hours  ÷ number of breakdowns
            #
            # MTBF's numerator is the running hours of the machines that ACTUALLY BROKE
            # DOWN, not the plant's. Summed over every machine it stopped being a time
            # between failures at all: 157 machines' hours over one plant's breakdowns
            # reads 4,930h — 470 working days — and it rises every time a healthy machine
            # is added to the register. The unit of that number is machine-hours; the
            # unit on the card is time. A machine with no breakdown has no gap between
            # failures to average, so it is out of the numerator and the count of the
            # ones that are in rides along with the figure (the same treatment the
            # availability ring already gets, for the same reason).
            "fleet_mtbf_min": round(plant["fail_run"] / plant["bd"]) if plant["bd"] else None,
            # what that average is built from, so the card can show its own working
            "fleet_mtbf_machines": plant["fail_n"],
            "fleet_mtbf_run_min": round(plant["fail_run"]),
            # the machines behind it, worst first — the one number on this card a
            # planner can act on is which asset is failing most often
            "mtbf_worst": [{"code": m["code"], "name": m["name"], "bd_count": m["bd_count"],
                            "mtbf_min": m["mtbf_min"], "downtime_min": m["downtime_min"]}
                           for m in sorted([m for m in out if m["bd_count"]],
                                           key=lambda m: m["mtbf_min"])[:5]],
            "fleet_mttr_min": round(plant["repair"] / plant["bd"]) if plant["bd"] else None,
            # `fleet_wrench_min` used to hold this same figure under a second name, from
            # when MTTR meant engineering downtime and wrench time rode along beside it.
            # The plant chose the standard's wording — total breakdown repair hours ÷
            # breakdowns — so the two became one number under two keys, and nothing has
            # rendered the second one since. Removed rather than left to rot: a key with
            # no reader is a promise the next change will quietly break.
            # NOT MTTR: how long the machine was actually held, per breakdown — report
            # to the technician's Stop, plus production's own time getting it back.
            # It is the honest measure of a stopped machine and it is usually much
            # larger than MTTR, because MTTR only counts minutes somebody logged. The
            # gap between the two is the crew's Start/Stop habit, and it belongs on the
            # screen; it just does not belong under the standard's name.
            # The WHOLE time the machine was off, per breakdown: report → accepted back.
            # This was engineering's share alone (_eng), which contradicted three things
            # at once — its own label on the card ("report → accepted"), the per-machine
            # field of the same name a few lines up (which uses the total), and the
            # downtime that availability and MTBF are computed from on the same screen.
            # On the live week that gap is not academic: engineering's share is about
            # 2h 24m a breakdown while the machines were actually off for 22h, because
            # production takes a long time to accept them back. Reporting the smaller
            # number under the larger number's name hid exactly the thing worth seeing.
            "fleet_down_per_bd_min": round((_eng + _prod) / len(_inscope)) if _inscope else None,
            # and the split, so the card can say which half of that is whose
            "fleet_eng_per_bd_min": round(_eng / len(_inscope)) if _inscope else None,
            "fleet_prod_per_bd_min": round(_prod / len(_inscope)) if _inscope else None,
            # The standard reports MTBF and MTTR monthly. Over a shorter window the
            # breakdown count is too small for the average to mean much, so the figure
            # is flagged rather than quietly shown beside targets it cannot be judged
            # against. Five is the point below which one long repair moves the average
            # more than the plant's actual behaviour does.
            "mtbf_unstable": bool(_wd < 28 or plant["bd"] < 5),
            # Where the required time came from: how many machines had a real shift log
            # in this window, and how many fell back to their operating hours. A
            # required time that is mostly fallback is still a fair reading of
            # availability, but the page should say so rather than imply a plan exists.
            "required_min": round(plant["required"]),
            "required_from_plan": plant["req_plan"], "required_from_hours": plant["req_hours"],
            "machines_in_scope": len(out),
            # b435: BD loss % = BD hours (report → Stop, wall clock) ÷ (machines × 24 h ×
            # working days in the window) × 100 — the plant's own definition
            "bd_wall_min": round(_bd_wall), "work_days": _wdays,
            "avail_below": avail_below, "avail_target": _AV_T,
            "avail_worst": [{"code": m["code"], "name": m["name"],
                             "availability": m["availability"], "bd_count": m["bd_count"],
                             "downtime_min": m["downtime_min"],
                             "criticality": m.get("criticality") or ""} for m in avail_worst],
            # b397: how the plant's machines split by criticality, for the list header
            "crit_count": {k: sum(1 for m in out if (m.get("criticality") or "") == k)
                           for k in ("A", "B", "C")},
            "ftfr": ftfr, "planned_ratio": planned_ratio,
            # the arithmetic behind the ratio, so the card can show its own working
            "planned_min": round(pm_min), "reactive_min": round(tot_min - pm_min),
            "work_min_by_type": work_min_by_type,
            # raised vs closed in this same window, so MTTR/MTBF can be read against it
            "open_done": open_done,
            "pm_compliance": pm_comp, "pm": pm_stat,
            "backlog": dict(bl) if bl else {},
            "top_failures": top_fail,
        },
        "targets": {"oee": 65, "pm_compliance": 90, "planned_ratio": 70,
                    "ftfr": 85, "mttr_min": 240, "availability": 90},
    }


ACTIVE_J = ("('Reported','WaitingApproval','WaitingAssignment','Assigned',"
            "'InProgress','Paused','Rework','Hold')")


@router.get("/kpi/trend")
async def kpi_trend(req: Request, days: int = 14):
    u = user_from(req)
    fac = u.get("active_factory") or u.get("factory_id")     # the plant chosen at login
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    with closing(db()) as c:
        sl = {r["log_date"]: dict(r) for r in c.execute("""
            SELECT s.log_date, SUM(s.planned_min) pm,
              SUM(CASE WHEN m.ideal_rate>0 THEN s.good/m.ideal_rate ELSE 0 END) igm
            FROM shiftlogs s JOIN machines m ON m.id=s.machine_id
            WHERE s.log_date >= ? AND m.factory_id=? GROUP BY s.log_date""", (start, fac))}
        bd = {}
        for r in c.execute("""SELECT created_at, done_at FROM jobs
            WHERE jobtype='BD' AND created_at >= ? AND factory_id=?
              AND status NOT IN """ + VOID_SQL + NOTKPI, (start + " 00:00:00", fac)):
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


def _crew_credit(c, fac):
    """b390 — TEAM TIME. (one_person, crew_of) helpers for crediting a job to its crew.

    The rule the plant set (29 Sep): every member of a crew is credited with the WHOLE
    job and the WHOLE time — a two-man hour is one hour for each of them, not 30 + 30 —
    while plant totals still count each job once and each minute once. `one_person`
    turns a phone login used by exactly one person into that person (BFLFP books a
    Start pressed on tech1's phone to the account, not to Choke)."""
    cnt, one = {}, {}
    for r in c.execute("SELECT login_id, id FROM users WHERE COALESCE(can_login,1)=0"
                       " AND login_id IS NOT NULL AND active=1 AND (factory_id=? OR COALESCE(factory_id,0)=0)", (fac,)):
        cnt[r["login_id"]] = cnt.get(r["login_id"], 0) + 1
        one[r["login_id"]] = r["id"]
    one = {a: p for a, p in one.items() if cnt[a] == 1}

    def crew(lead, helpers, presser=None):
        ids = [lead] + [int(x) for x in str(helpers or "").replace(";", ",").split(",")
                        if str(x).strip().isdigit()]
        team = list(dict.fromkeys(one.get(i, i) for i in ids if i))
        if presser:
            p = one.get(presser, presser)
            # Same rule as the Assigned-jobs day view: a technician who logged time on a
            # job whose crew he is NOT on (taken over after hold, re-planned since) owns
            # that segment alone — the current crew was not there for it.
            if team and p not in team:
                return [p]
            if p not in team:
                team.append(p)
        return team
    return crew


@router.get("/kpi/worktime")
async def kpi_worktime(req: Request, n: int = 7, unit: str = "days"):
    """Planner throughput + time panel over a manual period (n days/weeks/months),
    scoped to the logged-in factory. Hands-on time = summed timelog segments;
    elapsed = started_at → done_at."""
    u = user_from(req)
    fac = u.get("active_factory") or u.get("factory_id")
    try:
        n = max(1, min(int(n), 999))
    except (TypeError, ValueError):
        n = 7
    days = n * 7 if unit == "weeks" else n * 30 if unit == "months" else n
    start = (date.today() - timedelta(days=days)).isoformat() + " 00:00:00"
    with closing(db()) as c:
        # Counted in the factory's own working window, the same clock the downtime card
        # and MTBF already use. A technician who presses Start at 16:19 and Stop at
        # 10:42 the next morning was not repairing anything at three in the morning:
        # the raw span said 18h 23m and one forgotten timer moved the whole panel by a
        # day. Only the hours the factory was open count, so the two panels finally
        # agree on what a day is.
        hours, _groups = _fac_hours(c, fac)
        omin = lambda a, b: round(dt.op_minutes(a, b, *hours))
        jobs = [dict(r) for r in c.execute(
            """SELECT j.id,j.jobid,j.jobtype,j.status,j.created_at,j.started_at,j.done_at,
                      j.pending_reason, j.lead_tech, j.helpers, m.code mcode
               FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
               WHERE j.created_at >= ? AND (COALESCE(m.factory_id,j.factory_id)=? OR COALESCE(m.factory_id,j.factory_id) IS NULL)
                 AND j.status NOT IN """ + VOID_SQL + NOTKPI_J, (start, fac))]
        ids = [j["id"] for j in jobs]
        work, evmap, techagg = {}, {}, {}
        _names = user_names(c)          # crew ids → names, for the per-person job count
        _crew = _crew_credit(c, fac)
        _jb = {j["id"]: j for j in jobs}
        if ids:
            qs = ",".join("?" * len(ids))
            for r in c.execute(f"SELECT t.job_id, t.tech, t.start, t.end FROM timelogs t "
                               f"WHERE t.job_id IN ({qs})", ids):
                if r["job_id"] and r["start"]:
                    mm = omin(r["start"], r["end"] or now())
                    if mm:
                        # the plant's total: each minute once
                        work[r["job_id"]] = work.get(r["job_id"], 0) + mm
                        # each person on the crew: the whole minute (b390 team time)
                        _j = _jb.get(r["job_id"]) or {}
                        for _id in _crew(_j.get("lead_tech"), _j.get("helpers"), r["tech"]):
                            _nm = _names.get(str(_id)) or "?"
                            _ta = techagg.setdefault(_nm, {"tech": _nm, "work_min": 0, "jobs": set()})
                            _ta["work_min"] += mm
                            _ta["jobs"].add(r["job_id"])
            for e in c.execute(f"SELECT job_id,status,created_at FROM job_events WHERE job_id IN ({qs}) ORDER BY job_id,id", ids):
                evmap.setdefault(e["job_id"], []).append((e["status"], e["created_at"]))
    _now = now()
    is_pm = lambda j: j["jobtype"] == "PM"
    is_cm = lambda j: j["jobtype"] in ("CM", "BD")
    done = lambda j: j["status"] in ("Done", "ServiceCompleted") or bool(j["done_at"])
    # Elapsed is start → finish on the same working clock, for the same reason: a job
    # started at five and signed off at nine next morning did not take sixteen hours of
    # anybody's day. None, not zero, when either end is missing — an unfinished job must
    # not drag the average down as though it took no time.
    elapsed = lambda j: (omin(j["started_at"], j["done_at"])
                         if (j["started_at"] and j["done_at"]) else None)
    pm = [j for j in jobs if is_pm(j)]
    cm = [j for j in jobs if is_cm(j)]
    sw = lambda js: sum(work.get(j["id"], 0) for j in js)
    se = lambda js: sum((elapsed(j) or 0) for j in js if done(j))
    completed = [j for j in jobs if done(j)]
    fin = [elapsed(j) for j in completed if elapsed(j) is not None]
    hold = []
    for j in jobs:
        if j["status"] == "Hold":
            evs = evmap.get(j["id"], [])
            last_hold = next((ts for st, ts in reversed(evs) if st == "Hold"), None)
            hold.append({"jobid": j["jobid"], "mcode": j["mcode"],
                         "reason": j["pending_reason"] or "",
                         "held_min": mins_between(last_hold, _now) if last_hold else None})
    mach = {}
    for j in jobs:
        code = j["mcode"] or "—"
        m = mach.setdefault(code, {"mcode": code, "jobs": 0, "work_min": 0, "bd": 0})
        m["jobs"] += 1
        m["work_min"] += work.get(j["id"], 0)
        if j["jobtype"] == "BD":
            m["bd"] += 1
    # ── The three states a job can be in, and the type split behind each ──────────
    # Every card carries its own denominator, because they are NOT all the same one and
    # writing a bare number hid that: Completed, Open and On hold are shares of all the
    # work; Waiting signature is a share of the FINISHED work, since nothing can wait for
    # a signature before it is done.
    #
    # Open is deliberately "everything not finished and not on hold" rather than a list
    # of statuses. The old card listed four (Assigned / InProgress / Paused / Rework) and
    # silently dropped a job that was still Reported or waiting to be assigned — so the
    # three cards did not add up to the total, on exactly the plants where that mattered.
    # Defined this way, completed + open + on_hold = total on any data, by construction,
    # which is what lets the panel print its own check.
    # An explicit partition, walked once: every job falls into exactly one bucket, in
    # this order. Written as three separate list comprehensions it was possible for a
    # job to satisfy two of them — a job left with a done_at but put back On hold would
    # have been counted as finished AND on hold, and the panel's own check would then
    # print an equation that does not add up. On hold wins: a job somebody has parked is
    # not finished work, whatever timestamp it still carries.
    hold_l, comp_l, open_l = [], [], []
    for j in jobs:
        (hold_l if j["status"] == "Hold" else comp_l if done(j) else open_l).append(j)
    pend_l = [j for j in comp_l if j["status"] == "ServiceCompleted"]

    def _by_type(js):
        d = {}
        for j in js:
            d[j["jobtype"]] = d.get(j["jobtype"], 0) + 1
        return d

    # Who has to sign is not one queue but two, and they are chased from different
    # screens: a finished PM is signed by the PLANNER in PM Reports, a finished CM or BD
    # by the OPERATOR who reported it. "Pending approval: 11" named neither of them.
    pend_planner = sum(1 for j in pend_l if j["jobtype"] == "PM")

    # b400: up to 15 — the dashboard picks Top 5 / 8 / 10 / 15. Work on no machine is
    # not a machine: it is sent apart and never ranked among them.
    no_machine = mach.pop("—", None)
    by_machine = sorted(mach.values(), key=lambda x: -x["work_min"])[:15]
    # A crew job counted only for whoever pressed Start, so three technicians on one
    # repair read as one technician doing everything and two doing nothing — the whole
    # reason the workload never looked balanced. Every member of the crew is credited
    # with the JOB; the MINUTES stay with the person who ran the clock, because that is
    # the only one of the two the app actually measured.
    for _j in jobs:
        for _id in _crew(_j["lead_tech"], _j.get("helpers")):   # b390: phone login → its one person
            _nm2 = _names.get(str(_id))
            if not _nm2:
                continue
            _t2 = techagg.setdefault(_nm2, {"tech": _nm2, "work_min": 0, "jobs": set()})
            _t2["jobs"].add(_j["id"])
    by_tech = sorted([{"tech": v["tech"], "work_min": v["work_min"], "jobs": len(v["jobs"])}
                      for v in techagg.values()], key=lambda x: (-x["work_min"], -x["jobs"]))[:15]
    return {
        "n": n, "unit": unit, "days": days,
        "total": len(jobs), "pm": len(pm), "cm": len(cm),
        "completed": len(comp_l),
        # kept under their old names so an older page still renders
        "planned_open": sum(1 for j in jobs if j["status"] in ("Assigned", "InProgress", "Paused", "Rework")),
        "pending_approval": len(pend_l),
        "on_hold": len(hold_l),
        # the three states, each with the types behind it
        "open_total": len(open_l),
        "jobs_by_type": _by_type(jobs),
        "completed_by_type": _by_type(comp_l),
        "open_by_type": _by_type(open_l),
        "hold_by_type": _by_type(hold_l),
        "open_unassigned": sum(1 for j in open_l if not j["lead_tech"]),
        # finished work, split by whether anybody has accepted it back yet
        "signed_off": len(comp_l) - len(pend_l),
        "pending_signature": len(pend_l),
        "pending_planner": pend_planner,
        "pending_requester": len(pend_l) - pend_planner,
        "pm_work_min": sw(pm), "pm_elapsed_min": se(pm),
        "cm_work_min": sw(cm), "cm_elapsed_min": se(cm),
        "prd_work_min": sw([j for j in cm if j["jobtype"] == "CM"]),
        "bkd_work_min": sw([j for j in cm if j["jobtype"] == "BD"]),
        "avg_finish_min": round(sum(fin) / len(fin)) if fin else None,
        "hold_jobs": sorted(hold, key=lambda h: -(h["held_min"] or 0)),
        "total_hold_min": sum(h["held_min"] or 0 for h in hold),
        "by_machine": by_machine, "by_tech": by_tech, "no_machine": no_machine,
    }


def _norm_words(*parts):
    """The words of a fault, with the noise taken out, for comparing two of them."""
    import re
    s = " ".join(str(p or "") for p in parts).lower()
    s = re.sub(r"[^\w฀-๿]+", " ", s)          # keep Latin, digits and Thai
    drop = {"the", "a", "an", "is", "was", "and", "to", "of", "on", "in", "it", "this",
            "that", "for", "at", "no", "not", "ok", "fix", "fixed", "done", "issue",
            "problem", "machine", "resolved", "clear", "please", "already"}
    return {w for w in s.split() if len(w) > 1 and w not in drop}


def _repeat_faults(jobs, days=60, sim=0.5):
    """Groups of jobs on one machine that look like the same fault coming back.

    Two jobs match when at least half the meaningful words they share — in what the
    operator reported and what the technician wrote down — are the same, and the second
    was raised within `days` of the first. That catches "LAN cable loose connection"
    written twice in slightly different words, which an exact-text match never would.

    This is the one thing a job list cannot tell you by being sorted. A fault fixed
    twice on the same asset means the first repair did not hold, and nobody notices
    unless something says so out loud.

    Two shapes, told apart, because they need different answers:

      REPEAT     the same fault came back days or weeks later — the repair did not hold,
                 and it is worth asking whether the real cause was ever found.
      DUPLICATE  the same fault written twice inside a day — almost always two people
                 reporting one stoppage, which wastes a technician rather than a machine.

    Cancelled jobs are deliberately INCLUDED here, and nowhere else. A cancelled ticket
    is invisible to every number in the app, and a duplicate report is very often exactly
    what got cancelled — so leaving them out would hide the pattern this function exists
    to find. They stay out of the counts; they only ever appear in this list.
    """
    items = []
    for j in jobs:
        if (j.get("jobtype") or "") == "PM":
            continue                          # a PM coming round again is not a repeat
        w = _norm_words(j.get("descr"), j.get("solution"))
        if len(w) >= 3:
            items.append((j, w))
    items.sort(key=lambda x: str(x[0].get("created_at") or ""))
    used, groups = set(), []
    for i, (ja, wa) in enumerate(items):
        if id(ja) in used:
            continue
        grp, span = [ja], wa
        for jb, wb in items[i + 1:]:
            if id(jb) in used:
                continue
            inter = len(span & wb)
            if not inter:
                continue
            if inter / min(len(span), len(wb)) < sim:
                continue
            d0 = str(grp[-1].get("created_at") or "")[:10]
            d1 = str(jb.get("created_at") or "")[:10]
            try:
                if (date.fromisoformat(d1) - date.fromisoformat(d0)).days > days:
                    continue
            except ValueError:
                continue
            grp.append(jb)
            span = span & wb or span
        if len(grp) > 1:
            for g in grp:
                used.add(id(g))
            first, last = str(grp[0].get("created_at") or ""), str(grp[-1].get("created_at") or "")
            try:
                apart = (date.fromisoformat(last[:10]) - date.fromisoformat(first[:10])).days
            except ValueError:
                apart = 0
            groups.append({"n": len(grp), "words": sorted(span)[:6],
                           "kind": "duplicate" if apart < 1 else "repeat",
                           "days_apart": apart,
                           "jobs": [{"id": g["id"], "jobid": g.get("jobid"),
                                     "jobtype": g.get("jobtype"),
                                     "status": g.get("status"),
                                     "created_at": g.get("created_at"),
                                     "text": (g.get("solution") or g.get("descr") or "")[:90]}
                                    for g in grp]})
    return groups


@router.get("/machines/search")
async def machines_search(req: Request, q: str = "", limit: int = 12):
    """Type-ahead for the machine picker: code and name together, this plant only.

    A dropdown was the obvious thing and is the wrong thing — the register holds over
    1,600 assets, which no <select> can carry on a phone. Typing narrows it, and each
    row carries its job count so you can see there is something to read before opening
    it. Matching runs over the code AND the Thai name, because half the plant knows a
    machine by one and half by the other.
    """
    u = user_from(req)
    fac = u.get("active_factory") or u.get("factory_id")
    q = (q or "").strip()
    if len(q) < 1:
        return {"machines": []}
    like = "%" + q + "%"
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute(
            """SELECT m.id, m.code, m.name, m.line, m.asset_group, m.active,
                      (SELECT COUNT(*) FROM jobs j WHERE j.machine_id=m.id) njobs
                 FROM machines m
                WHERE m.factory_id=? AND (m.code LIKE ? OR m.name LIKE ?)
                ORDER BY (m.code LIKE ?) DESC, njobs DESC, m.code
                LIMIT ?""",
            (fac, like, like, q + "%", max(1, min(50, limit))))]
    return {"machines": rows}


@router.get("/machines/{mid}/record")
async def machine_record(mid: int, req: Request, d_from: str = "", d_to: str = ""):
    """One asset's whole working record — the answer to "how is this machine doing?".

    Deliberately NOT the job list filtered by machine. A filtered list answers what
    happened; a record answers whether it is getting worse. So on top of the jobs it
    carries the counts a planner argues with (breakdowns, downtime, MTTR, MTBF, when
    the last PM was and when the next is due), twelve months of jobs-per-month so the
    trend is visible without reading a single row, and the repeat faults.

    The default window is the machine's whole life. Asking about one asset is asking
    about all of it; a 30-day default would quietly hide the fault that came back in
    March. Give d_from / d_to to narrow it.

    Cancelled and Rejected jobs are LISTED — they are part of the story of the asset,
    and a cancelled breakdown often is the story — but they are left out of every
    number, exactly as they are everywhere else a manager reads a figure.
    """
    u = user_from(req)
    _now = now()
    with closing(db()) as c:
        m = c.execute("SELECT * FROM machines WHERE id=?", (mid,)).fetchone()
        if not m:
            raise HTTPException(404, "no machine")
        m = dict(m)
        hours, groups = _fac_hours(c, m.get("factory_id"))
        g = groups.get(m.get("asset_group") or "") or {}
        if g:
            _sh = int(str(g.get("start") or "07:00").split(":")[0])
            _eh = int(str(g.get("end") or "21:00").split(":")[0])
            _gd = {**(hours[4] if len(hours) > 4 else {}), **day_windows(g)}
            hours = (_sh, (24 if _eh == 0 else _eh), hours[2], hours[3], _gd)

        where, args = ["j.machine_id=?"], [mid]
        if d_from:
            where.append("j.created_at >= ?"); args.append(d_from + " 00:00:00")
        if d_to:
            where.append("j.created_at <= ?"); args.append(d_to + " 23:59:59")
        jobs = [dict(r) for r in c.execute(
            """SELECT j.*, u.name lead_name, m.code mcode, m.name mname
                 FROM jobs j LEFT JOIN users u ON u.id=j.lead_tech
                 LEFT JOIN machines m ON m.id=j.machine_id
                WHERE """ + " AND ".join(where) + " ORDER BY j.created_at DESC", args)]
        ids = [j["id"] for j in jobs]
        evmap = {}
        if ids:
            qs = ",".join("?" * len(ids))
            for e in c.execute(f"SELECT job_id,status,created_at FROM job_events"
                               f" WHERE job_id IN ({qs}) ORDER BY job_id, id", ids):
                evmap.setdefault(e["job_id"], []).append((e["status"], e["created_at"]))

    void = ("Cancelled", "Rejected")
    counted = [j for j in jobs
               if j.get("status") not in void and not (j.get("kpi_exclude") or 0)]
    bd = [j for j in counted if (j.get("jobtype") or "") == "BD"]
    pm = [j for j in counted if (j.get("jobtype") or "") == "PM"]

    eng = wait = work = prod = 0.0
    for j in bd:
        evs = evmap.get(j["id"], [])
        sp = dt.split(j["created_at"], evs, hours, _now,
                      (j.get("done_at"), j.get("approved_at")))
        w = min(dt.work_inside(evs, hours, _now), sp["eng_min"])
        eng += sp["eng_min"]; prod += sp["prod_min"]
        work += w; wait += max(0, sp["eng_min"] - w)
        j["down_min"] = sp["total_min"]

    # ── One machine, one MTTR, one MTBF ───────────────────────────────────────────
    # This screen is opened BY CLICKING a machine on the dashboard's worst-five list,
    # and it used to answer with different arithmetic: MTTR as engineering downtime ÷
    # breakdowns, MTBF as the mean gap between one breakdown and the next. Both are
    # defensible measures; neither is the one the ring above was built from, so the
    # number changed under the reader's hand on the way in, which is how a dashboard
    # loses an argument it was right about.
    #
    # The plant's definitions win here: MTTR = logged repair hours ÷ breakdowns, MTBF =
    # running hours ÷ breakdowns, running = required − downtime, all on this machine's
    # own operating window. The gap-based figure is kept beside them under its own
    # name, because "it fails every nine days" is the sentence a planner actually uses.
    bd_asc = sorted(bd, key=lambda x: str(x.get("created_at") or ""))
    gaps = [dt.op_minutes(bd_asc[i]["created_at"], bd_asc[i + 1]["created_at"], *hours)
            for i in range(len(bd_asc) - 1)]
    _ids = [j["id"] for j in bd]
    _repair = 0.0
    if _ids:
        with closing(db()) as _c2:
            _qs = ",".join("?" * len(_ids))
            for t in _c2.execute(f"SELECT start s, end e FROM timelogs"
                                 f" WHERE job_id IN ({_qs}) AND seg_type='work'", _ids):
                _repair += dt.op_minutes(t["s"], t["e"] or _now, *hours)
    # required time over the window this record covers, the same clock as the dashboard
    _d0 = (d_from or min((str(j.get("created_at") or "")[:10] for j in jobs), default=today()))
    _d1 = min((d_to or today()) + " 23:59:59", _now)
    _required = dt.op_minutes(_d0 + " 00:00:00", _d1, *hours)
    _run = max(0.0, _required - (eng + prod))
    pm_done = [j for j in pm if j.get("done_at")]
    last_pm = max((str(j["done_at"])[:10] for j in pm_done), default="") \
        or (m.get("last_pm_date") or "")
    next_pm = ""
    if m.get("pm_freq_days") and last_pm:
        try:
            next_pm = (date.fromisoformat(last_pm)
                       + timedelta(days=int(m["pm_freq_days"]))).isoformat()
        except ValueError:
            next_pm = ""

    # twelve months of jobs by type — the trend, without reading a single row
    from collections import OrderedDict
    months = OrderedDict()
    t0 = date.fromisoformat(today()).replace(day=1)
    for k in range(11, -1, -1):
        y, mo = divmod((t0.year * 12 + t0.month - 1) - k, 12)
        months["%04d-%02d" % (y, mo + 1)] = {"PM": 0, "CM": 0, "BD": 0, "IMP": 0, "PRJ": 0}
    for j in counted:
        key = str(j.get("created_at") or "")[:7]
        if key in months:
            months[key][(j.get("jobtype") or "CM").upper()[:3]] = \
                months[key].get((j.get("jobtype") or "CM").upper()[:3], 0) + 1

    # Stripped only now — every figure above still had to READ the flag to leave those
    # jobs out of the counts. Popping it earlier would have handed a non-admin a machine
    # record whose numbers silently included work the admin had taken out.
    if u.get("role") != "admin":
        for j in jobs:
            for _k in ("kpi_exclude", "kpi_exclude_note", "kpi_exclude_by", "kpi_exclude_at"):
                j.pop(_k, None)
    else:
        for j in jobs:
            j["kpi_exclude"] = 1 if j.get("kpi_exclude") else 0

    return {
        "machine": m,
        "from": d_from, "to": d_to,
        "hours": {"start": hours[0], "end": hours[1]},
        "jobs": jobs,
        "stats": {
            "n_jobs": len(counted), "n_listed": len(jobs),
            "bd": len(bd), "cm": len([j for j in counted if (j.get("jobtype") or "") == "CM"]),
            "pm": len(pm), "pm_done": len(pm_done),
            "down_min": round(eng + prod),
            "eng_min": round(eng), "prod_min": round(prod),
            "wait_min": round(wait), "work_min": round(work),
            # the plant's two, identical to the dashboard's
            "mttr_min": round(_repair / len(bd)) if bd else None,
            "mtbf_min": round(_run / len(bd)) if bd else None,
            "repair_min": round(_repair), "run_min": round(_run),
            "required_min": round(_required),
            # kept, under its own name: the mean gap between one breakdown and the next,
            # which needs two breakdowns to exist and is blank rather than flattering
            "mtbf_gap_min": round(sum(gaps) / len(gaps)) if gaps else None,
            # what this screen used to call MTTR, so a saved report can still be read
            "eng_per_bd_min": round(eng / len(bd)) if bd else None,
            "last_bd": max((str(j["created_at"])[:10] for j in bd), default=""),
            "last_pm": last_pm, "next_pm": next_pm,
            "first_seen": min((str(j["created_at"])[:10] for j in jobs), default=""),
            "open": len([j for j in jobs if j.get("status")
                         not in ("Done", "Rejected", "Cancelled")]),
        },
        "months": [{"m": k, **v} for k, v in months.items()],
        "repeats": _repeat_faults(jobs),
    }


@router.get("/machines/{mid}/history")
async def machine_history(mid: int, req: Request):
    user_from(req)
    with closing(db()) as c:
        m = c.execute("SELECT * FROM machines WHERE id=?", (mid,)).fetchone()
        if not m:
            raise HTTPException(404, "no machine")
        jobs = [dict(r) for r in c.execute("""SELECT j.*, u.name lead_name FROM jobs j
            LEFT JOIN users u ON u.id=j.lead_tech
            WHERE j.machine_id=? AND j.status NOT IN """ + VOID_SQL + """
            ORDER BY j.id DESC LIMIT 100""", (mid,))]
        logs = [dict(r) for r in c.execute("""SELECT * FROM shiftlogs
            WHERE machine_id=? ORDER BY log_date DESC, shift LIMIT 60""", (mid,))]
    return {"machine": dict(m), "jobs": jobs, "shiftlogs": logs}


# ---------------- dashboard drill-downs ----------------
#
# Every ring on the dashboard opens the list it was counted from. That is the whole
# point of them: a percentage nobody can open is a percentage nobody can check, and
# the first question anyone asks a compliance figure is "which ones".
#
# Each list is built by the SAME WHERE clause as the ring above it, so the row count
# and the denominator are the same number by construction rather than by care. If the
# two ever disagree it is a bug here, not a rounding difference to explain away.

_PAGE_MAX = 200


def _page(q, page, per):
    """LIMIT/OFFSET, clamped. A quarter of PMs is 400+ rows and the tablets will not
    render that in one go — nor should a person be asked to scroll it."""
    per = max(10, min(_PAGE_MAX, int(per or 50)))
    page = max(1, int(page or 1))
    return per, page, f" {q} LIMIT {per} OFFSET {(page - 1) * per}"


# b397 ── the lists behind the rings: filter, search, sort, and whole-list counts ────
# The counts at the top of these lists used to be worked out in the browser from the
# page in hand, so page 1 of a month read "0 on time · 50 late · 0 open" under a ring
# that said 479/587. Every count now comes from the server over the WHOLE list, the
# list carries each machine's criticality (A critical · B important · C normal), and
# it can be cut by verdict, criticality and a search box without paging through it.
_CRIT_ORD = "CASE crit WHEN 'A' THEN 0 WHEN 'B' THEN 1 WHEN 'C' THEN 2 ELSE 3 END"


def _crit_arg(crit):
    crit = (crit or "").strip().upper()
    return crit if crit in ("A", "B", "C", "-") else ""


def _list_filters(crit, q, fields):
    """WHERE pieces for the outer query: criticality and a free-text search."""
    w, a = [], []
    crit = _crit_arg(crit)
    if crit == "-":
        w.append("crit NOT IN ('A','B','C')")
    elif crit:
        w.append("crit=?")
        a.append(crit)
    q = (q or "").strip()
    if q:
        w.append("(" + " OR ".join("COALESCE(%s,'') LIKE ?" % f for f in fields) + ")")
        a += ["%" + q + "%"] * len(fields)
    return w, a


def _crit_stats(c, base, args, ok_expr, n_expr):
    out = {}
    for r in c.execute("SELECT crit, SUM(CASE WHEN %s THEN 1 ELSE 0 END) ok,"
                       " SUM(CASE WHEN %s THEN 1 ELSE 0 END) n FROM (%s) GROUP BY crit"
                       % (ok_expr, n_expr, base), args):
        k = r["crit"] if r["crit"] in ("A", "B", "C") else "-"
        o = out.setdefault(k, {"ok": 0, "n": 0})
        o["ok"] += r["ok"] or 0
        o["n"] += r["n"] or 0
    return out


@router.get("/kpi/pmjobs")
async def kpi_pm_jobs(req: Request, d_from: str = "", d_to: str = "",
                      page: int = 1, per: int = 50, verdict: str = "all",
                      crit: str = "", q: str = "", sort: str = ""):
    """The PM jobs behind the compliance ring — every one that was DUE in the window.

    The list shows ALL due PMs, not just the on-time ones: showing only the numerator
    would hide the late work, the only part of this number anybody can still act on.
    Each row carries its verdict — on time · late (done after its period) · open
    (period over, not done) · not due yet (period still running: listed, not counted).
    `counts` is the whole list by verdict, so the header can state the arithmetic.
    """
    u = user_from(req)
    fac = u.get("active_factory") or u.get("factory_id")     # same plant as the ring
    d_to = d_to or today()
    d_from = d_from or (date.fromisoformat(d_to) - timedelta(days=6)).isoformat()
    base = ("""SELECT j.id, j.jobid, j.descr, j.status, j.due_date, j.done_at,
              j.planned_date, j.priority, m.code mcode, m.name mname, m.line mline,
              u.name lead_name, COALESCE(m.criticality,'') crit,
              """ + PM_END + """ period_end,
              CASE WHEN """ + PM_ONTIME + """ THEN 'ontime'
                   WHEN j.status IN ('ServiceCompleted','Done') THEN 'late'
                   WHEN NOT """ + PM_COUNTS + """ THEN 'notdue'
                   ELSE 'open' END v
              FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
                LEFT JOIN users u ON u.id=j.lead_tech
               WHERE j.jobtype='PM'
                 AND j.due_date IS NOT NULL AND j.due_date != ''
                 AND j.status NOT IN """ + VOID_SQL + NOTKPI_J + """
                 AND ((j.due_date >= ? AND j.due_date <= ?)
                      OR (j.due_date < ? AND j.status NOT IN ('Done','Rejected','Cancelled')))
                 AND COALESCE(m.factory_id,j.factory_id)=?""")
    args = [d_from, d_to, d_from, fac]
    w, fa = _list_filters(crit, q, ("jobid", "mcode", "mname", "lead_name", "descr", "mline"))
    counts_where = (" WHERE " + " AND ".join(w)) if w else ""
    n_cw = len(fa)
    verdict = (verdict or "all").lower()
    if verdict == "act":
        w.append("v IN ('open','late')")
    elif verdict in ("ontime", "late", "open", "notdue"):
        w.append("v=?")
        fa.append(verdict)
    elif verdict == "carried":
        w.append("v='ontime' AND SUBSTR(done_at,1,10) > due_date")
    where = (" WHERE " + " AND ".join(w)) if w else ""
    vord = "CASE v WHEN 'open' THEN 0 WHEN 'late' THEN 1 WHEN 'notdue' THEN 2 ELSE 3 END"
    sorts = {"crit": _CRIT_ORD, "job": "jobid", "asset": "mcode", "machine": "mname",
             "due": "due_date", "closed": "COALESCE(done_at,'9')", "lead": "lead_name",
             "verdict": vord}
    first = sorts.get(sort)
    order = "ORDER BY " + (first + ", " if first else "") + vord + ", " + _CRIT_ORD + ", due_date, id"
    per, page, lim = _page(order, page, per)
    with closing(db()) as c:
        counts = {"ontime": 0, "late": 0, "open": 0, "notdue": 0, "carried": 0}
        for r in c.execute("SELECT v, COUNT(*) n FROM (%s)%s GROUP BY v" % (base, counts_where),
                           args + fa[:n_cw]):
            counts[r["v"]] = r["n"]
        # b398: on time, but done after the day it was planned for — carried over
        # inside its own week / month. Still on time for the ring; shown apart.
        counts["carried"] = c.execute(
            "SELECT COUNT(*) n FROM (%s)%s%sv='ontime' AND SUBSTR(done_at,1,10) > due_date"
            % (base, counts_where, " AND " if counts_where else " WHERE "),
            args + fa[:n_cw]).fetchone()["n"]
        total = c.execute("SELECT COUNT(*) n FROM (%s)%s" % (base, where), args + fa).fetchone()["n"]
        rows = [dict(r) for r in c.execute("SELECT * FROM (%s)%s%s" % (base, where, lim), args + fa)]
        # criticality split of the RING itself — the whole period, no search, no filter
        crit_stats = _crit_stats(c, base, args, "v='ontime'", "v<>'notdue'")
    for r in rows:
        r["done"] = r["v"] in ("ontime", "late")
        r["ontime"], r["late"] = r["v"] == "ontime", r["v"] == "late"
        r["not_due_yet"] = r["v"] == "notdue"
        # How late, in days, measured from the END OF THE PERIOD — the deadline the
        # plant actually works to.
        r["late_days"] = ((date.fromisoformat(r["done_at"][:10])
                           - date.fromisoformat(r["period_end"])).days
                          if r["late"] and r.get("done_at") and r.get("period_end") else None)
        # b398: carried over = done after its planned (due) day but inside its period
        r["carried_days"] = None
        if r["ontime"] and r.get("done_at") and r.get("due_date"):
            _cd = (date.fromisoformat(r["done_at"][:10]) - date.fromisoformat(r["due_date"][:10])).days
            r["carried_days"] = _cd if _cd > 0 else None
        # …and an open PM whose period is over: how many days overdue it is now
        r["overdue_days"] = ((date.fromisoformat(today()) - date.fromisoformat(r["period_end"])).days
                             if r["v"] == "open" and r.get("period_end") else None)
    return {"from": d_from, "to": d_to, "total": total, "page": page, "per": per,
            "pages": max(1, -(-total // per)), "jobs": rows, "counts": counts,
            "crit_stats": crit_stats}


@router.get("/kpi/pmdaily")
async def kpi_pm_daily(req: Request, d_from: str = "", d_to: str = ""):
    """b400: PM planned vs completed, day by day, for the dashboard line chart.

    planned  = PM work orders DUE on that day (the plan)
    done     = of the PMs due in this window, how many were finished on that day
    Both are counted only up to today. The running totals of the two lines are the
    plan and what got done of it; the gap between them is the backlog carried forward.
    """
    u = user_from(req)
    fac = u.get("active_factory") or u.get("factory_id")
    d_to = d_to or today()
    d_from = d_from or (date.fromisoformat(d_to) - timedelta(days=6)).isoformat()
    last = min(d_to, today())
    with closing(db()) as c:
        rows = c.execute("""SELECT j.due_date d, SUBSTR(COALESCE(j.done_at,''),1,10) done, j.status
              FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
             WHERE j.jobtype='PM' AND j.due_date >= ? AND j.due_date <= ?
               AND j.status NOT IN """ + VOID_SQL + NOTKPI_J + """
               AND COALESCE(m.factory_id,j.factory_id)=?""", (d_from, d_to, fac)).fetchall()
    days, d = [], date.fromisoformat(d_from)
    while d.isoformat() <= last and len(days) < 400:
        days.append(d.isoformat())
        d += timedelta(days=1)
    plan = {k: 0 for k in days}
    done = {k: 0 for k in days}
    for r in rows:
        if r["d"] in plan:
            plan[r["d"]] += 1
        if r["status"] in ("Done", "ServiceCompleted") and r["done"] in done:
            done[r["done"]] += 1
    return {"from": d_from, "to": d_to, "days": [{"d": k, "planned": plan[k], "done": done[k]} for k in days],
            "planned": sum(plan.values()), "done": sum(done.values())}


@router.get("/kpi/cmjobs")
async def kpi_cm_jobs(req: Request, d_from: str = "", d_to: str = "",
                      page: int = 1, per: int = 50, verdict: str = "all",
                      crit: str = "", q: str = "", sort: str = "", jt: str = ""):
    """The corrective jobs behind the CM ring — CM and BD together, raised in the window.
    b436: jt=BD lists the breakdowns only (the BD ring's list).

    This ring is NOT a Bluefalo standard KPI and the dashboard labels it as an internal
    operational metric. "Closed" is status Done, the same rule as the ring.
    """
    u = user_from(req)
    fac = u.get("active_factory") or u.get("factory_id")     # same plant as the ring
    d_to = d_to or today()
    d_from = d_from or (date.fromisoformat(d_to) - timedelta(days=6)).isoformat()
    f0, t1 = d_from + " 00:00:00", d_to + " 23:59:59"
    base = ("""SELECT j.id, j.jobid, j.jobtype, j.descr, j.status, j.created_at, j.done_at,
              j.approved_at, j.priority, j.problem, j.solution, j.machine_id,
              COALESCE(m.code,'') mcode, COALESCE(m.name, j.asset_text, '') mname,
              u.name lead_name, COALESCE(m.criticality,'') crit,
              CASE WHEN j.status='Done' THEN 'closed' ELSE 'open' END v
              FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
                LEFT JOIN users u ON u.id=j.lead_tech
               WHERE j.jobtype IN """ + ("('BD')" if (jt or "").upper() == "BD" else "('CM','BD')") + """
                 AND j.created_at >= ? AND j.created_at <= ?
                 AND j.status NOT IN """ + VOID_SQL + NOTKPI_J + """
                 AND COALESCE(m.factory_id,j.factory_id)=?""")
    args = [f0, t1, fac]
    w, fa = _list_filters(crit, q, ("jobid", "mcode", "mname", "lead_name", "descr", "problem"))
    counts_where = (" WHERE " + " AND ".join(w)) if w else ""
    n_cw = len(fa)
    verdict = (verdict or "all").lower()
    if verdict in ("open", "closed"):
        w.append("v=?")
        fa.append(verdict)
    where = (" WHERE " + " AND ".join(w)) if w else ""
    sorts = {"crit": _CRIT_ORD, "job": "jobid", "asset": "mcode", "problem": "descr",
             "raised": "created_at DESC", "lead": "lead_name", "status": "status"}
    first = sorts.get(sort)
    order = ("ORDER BY " + (first + ", " if first else "")
             + "CASE v WHEN 'open' THEN 0 ELSE 1 END, " + _CRIT_ORD
             + ", CASE jobtype WHEN 'BD' THEN 0 ELSE 1 END, created_at DESC, id")
    per, page, lim = _page(order, page, per)
    with closing(db()) as c:
        counts = {"open": 0, "closed": 0}
        for r in c.execute("SELECT v, COUNT(*) n FROM (%s)%s GROUP BY v" % (base, counts_where),
                           args + fa[:n_cw]):
            counts[r["v"]] = r["n"]
        total = c.execute("SELECT COUNT(*) n FROM (%s)%s" % (base, where), args + fa).fetchone()["n"]
        rows = [dict(r) for r in c.execute("SELECT * FROM (%s)%s%s" % (base, where, lim), args + fa)]
        crit_stats = _crit_stats(c, base, args, "v='closed'", "1=1")
        no_machine = c.execute("SELECT COUNT(*) n FROM (%s) WHERE machine_id IS NULL" % base,
                               args).fetchone()["n"]
    for r in rows:
        r["done"] = r["v"] == "closed"
    return {"from": d_from, "to": d_to, "total": total, "page": page, "per": per,
            "pages": max(1, -(-total // per)), "jobs": rows, "counts": counts,
            "crit_stats": crit_stats, "no_machine": no_machine}


@router.get("/kpi/techs")
async def kpi_techs(req: Request, d_from: str = "", d_to: str = ""):
    """The technician panel: open jobs now, and average repair time over the window.

    Deliberately NOT a ranking by jobs closed. A leaderboard of closed jobs rewards
    picking up the quick ones and leaving the awkward machine for somebody else, and
    the crew works out what is being counted long before management does. Open jobs
    and average repair time are the two figures a planner can actually act on: who is
    loaded, and what the work on their bench costs.

    The open count is a NOW figure and the average is a window figure — two different
    periods, deliberately, because that is what each question needs. The panel says so
    on screen rather than implying one period for both.
    """
    u = user_from(req)
    _fac_id = u.get("active_factory") or u.get("factory_id")
    d_to = d_to or today()
    d_from = d_from or (date.fromisoformat(d_to) - timedelta(days=6)).isoformat()
    f0, t1 = d_from + " 00:00:00", d_to + " 23:59:59"
    with closing(db()) as c:
        # ── People, not handsets — and one plant ─────────────────────────────────
        # This asked for `role='technician' AND active=1` and nothing else, which was
        # wrong twice over on a page that is otherwise strictly one plant: it returned
        # every technician in the company, and it counted PHONE ACCOUNTS as if they
        # were people.
        #
        # The plant runs a few shared company handsets between many technicians, so the
        # register holds two different kinds of row under the same role. A technician
        # PERSON is `COALESCE(can_login,1)=0` — a real name, no login of their own —
        # and that is what work is booked to and what the assignment board offers. A
        # login account is the handset. Listing both put empty handset rows in the crew
        # panel beside the people using them.
        #
        # Login accounts are not dropped outright, though: where a job has actually
        # been booked to a handset rather than to a person, a planner needs to see it
        # — it is work whose owner is unrecorded, which is the one thing this panel
        # exists to make visible. So a handset appears only when it is carrying
        # something, and says what it is.
        techs = {r["id"]: {"tech_id": r["id"], "name": r["name"],
                           "is_login": bool(r["cl"]), "open": 0,
                           "open_bd": 0, "work_min": 0.0, "jobs_worked": 0,
                           "avg_repair_min": None, "oldest_open_days": None}
                 for r in c.execute(
                     "SELECT id, name, COALESCE(can_login,1) cl FROM users"
                     " WHERE role='technician' AND active=1 AND (factory_id=? OR COALESCE(factory_id,0)=0)",
                     (_fac_id,))}
        n_people = sum(1 for t in techs.values() if not t["is_login"])
        n_logins = sum(1 for t in techs.values() if t["is_login"])
        now_dt = datetime.now()
        _crew = _crew_credit(c, _fac_id)
        # b423: a PM job with only electrical points is Central Electrical's work, not an
        # open job on the plant technician's plate
        try:
            from .elec import CeFilter as _CF, all_elec as _ae
            _cf = _CF(c)
        except Exception:
            _cf = None
        for r in c.execute("SELECT lead_tech, helpers, jobtype, created_at, machine_id, pm_freq,"
                           " planned_date, due_date, factory_id FROM jobs "
                           f"WHERE lead_tech IS NOT NULL AND factory_id=? AND status IN {ACTIVE_J}",
                           (_fac_id,)):
          if _cf is not None and r["jobtype"] == "PM":
              try:
                  if _ae(_cf, dict(r)):
                      continue
              except Exception:
                  pass
          # b390: an open job is on the plate of everyone on its crew, not the lead alone
          for _id in _crew(r["lead_tech"], r["helpers"]):
            tk = techs.get(_id)
            if not tk:
                continue
            tk["open"] += 1
            if r["jobtype"] == "BD":
                tk["open_bd"] += 1
            try:
                age = (now_dt - datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")).days
                tk["oldest_open_days"] = max(tk["oldest_open_days"] or 0, age)
            except Exception:
                pass
        # Average repair time = logged wrench minutes ÷ the jobs they were logged on —
        # the same measure the standard's MTTR uses, per person instead of per fleet.
        seen = {}
        for r in c.execute("""SELECT t.tech, t.job_id, t.start s, t.end e, j.lead_tech, j.helpers
              FROM timelogs t JOIN jobs j ON j.id=t.job_id
              WHERE t.seg_type='work' AND t.start >= ? AND t.start <= ?
                AND j.jobtype IN ('CM','BD')
                AND j.factory_id=?
                AND j.status NOT IN """ + VOID_SQL + NOTKPI_J, (f0, t1, _fac_id)):
            # b390 team time: the whole segment to every crew member (and whoever ran it)
            _mm = _minutes(r["s"], r["e"])
            for _id in _crew(r["lead_tech"], r["helpers"], r["tech"]):
                tk = techs.get(_id)
                if not tk:
                    continue
                tk["work_min"] += _mm
                seen.setdefault(_id, set()).add(r["job_id"])
        for tid, tk in techs.items():
            n = len(seen.get(tid, ()))
            tk["jobs_worked"] = n
            tk["work_min"] = round(tk["work_min"])
            tk["avg_repair_min"] = round(tk["work_min"] / n) if n else None
    # A handset earns a row only by carrying something; a person always has one.
    out = [t for t in techs.values()
           if not t["is_login"] or t["open"] or t["jobs_worked"]]
    # Busiest first — the panel exists so a planner can see where the load is.
    out.sort(key=lambda x: (-x["open"], -(x["work_min"] or 0)))
    # The counts let the panel explain an empty crew list the way the assignment board
    # already does: a plant that has created its technician LOGINS but no technician
    # people has done half the setup, and "no technicians" on its own would send them
    # to create the accounts they have already got.
    return {"from": d_from, "to": d_to, "techs": out,
            "people": n_people, "logins": n_logins}


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
                c.execute("""INSERT INTO jobs(jobid,jobtype,machine_id,descr,priority,status,
                    due_date,jobsource,created_by,created_at,factory_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (next_jobid(c, "PM", factory_id=m["factory_id"]), "PM", m["id"],
                     f"PM ตามรอบ {m['pm_freq_days']} วัน — {m['name']}",
                     1, "Reported", today(), "PM-Auto", 1, now(), m["factory_id"]))
                created.append(m["code"])
        c.commit()
        if created:
            planners = [r["id"] for r in c.execute(
                "SELECT id FROM users WHERE role='planner' AND active=1")]
            notify_users(planners, "🗓 PM ถึงรอบ",
                         f"สร้างงาน PM อัตโนมัติ: {', '.join(created[:5])}"
                         + ("..." if len(created) > 5 else ""))
    return created


# ---------------- engineering vs production downtime ----------------

def _fac_hours(c, fac):  # noqa: E302  (defined below, used by machine_record above)
    """The factory's working window, as downtime.py wants it, plus its group overrides.

    Returns (default_hours, groups) where default_hours = (start_h, end_h, offdays,
    holidays). A machine in an asset group with its own hours (boiler, compressor)
    takes those instead — the same rule the OEE engine uses.
    """
    import json as _json
    r = c.execute("SELECT hours_json FROM factories WHERE id=?", (fac,)).fetchone()
    try:
        cfg = _json.loads(r["hours_json"]) if r and r["hours_json"] else {}
    except Exception:
        cfg = {}
    sh = int(str(cfg.get("start") or "07:00").split(":")[0])
    eh = int(str(cfg.get("end") or "21:00").split(":")[0])
    offdays = tuple(cfg["weekly_off"] if cfg.get("weekly_off") is not None else [6])
    holidays = tuple(cfg.get("holidays") or [])
    # Fifth item: the weekdays that carry their own window (Sunday 07:00-20:00 and the
    # like). Every consumer passes the tuple straight into downtime.op_minutes, which
    # takes it as its `days` argument, so nothing else has to know it is there.
    return ((sh, (24 if eh == 0 else eh), offdays, holidays, day_windows(cfg)),
            (cfg.get("groups") or {}))


def _buckets(d_from, d_to):
    """The downtime bar chart's x-axis, sized to the window on screen.

    One day is read hour by hour, a week or a month day by day, a quarter week by
    week, and anything longer month by month. The alternative — a fixed number of
    bars stretched over whatever period was picked — produces a chart whose bars mean
    something different every time the range changes, which is how somebody ends up
    comparing a Tuesday against a fortnight without noticing.

    Returns [(label, start, end), ...] with real datetimes, so the caller can clip a
    downtime span against a bucket without parsing anything back.
    """
    a, b = date.fromisoformat(d_from), date.fromisoformat(d_to)
    n = (b - a).days + 1
    D = datetime
    if n <= 1:
        return "hour", [(f"{h:02d}", D(a.year, a.month, a.day, h),
                         D(a.year, a.month, a.day) + timedelta(hours=h + 1))
                        for h in range(24)]
    if n <= 31:
        out, cur = [], a
        while cur <= b:
            s = D(cur.year, cur.month, cur.day)
            out.append((cur.isoformat()[5:], s, s + timedelta(days=1)))
            cur += timedelta(days=1)
        return "day", out
    if n <= 120:
        out = []
        cur = a - timedelta(days=a.weekday())          # back to that week's Monday
        while cur <= b:
            s = D(cur.year, cur.month, cur.day)
            out.append((cur.isoformat()[5:], s, s + timedelta(days=7)))
            cur += timedelta(days=7)
        return "week", out
    out, y, mo = [], a.year, a.month
    while (y, mo) <= (b.year, b.month):
        ny, nmo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        out.append((f"{y}-{mo:02d}", D(y, mo, 1), D(ny, nmo, 1)))
        y, mo = ny, nmo
    return "month", out


@router.get("/kpi/issues")
async def kpi_issues(req: Request, d_from: str = "", d_to: str = "",
                     machine_id: int = 0, bucket: str = "week"):
    """How many issues each machine has had — BD and CM, counted not timed.

    Downtime hours answer "how much did it cost us"; this answers "which machine keeps
    stopping", and they are not the same list. One long repair on a reliable machine
    tops the downtime chart for a month; the machine that fails every third day never
    appears on it at all, because each fault is short. A planner chasing root cause
    needs the second list, and the plant did not have it anywhere.

    Two shapes, one endpoint:
      * `machines` — every machine with at least one issue in the window on screen,
        worst first, BD and CM apart.
      * `series` — ONE machine over time, in the bucket asked for. Deliberately not
        limited to the dashboard's window: the question here is whether it is getting
        better or worse, and a week of history cannot answer that. Fourteen buckets.
    """
    u = user_from(req)
    fac = u.get("active_factory") or u.get("factory_id")
    d_to = d_to or today()
    d_from = d_from or (date.fromisoformat(d_to) - timedelta(days=6)).isoformat()
    bucket = bucket if bucket in ("day", "week", "month", "quarter") else "week"

    with closing(db()) as c:
        rows = [dict(r) for r in c.execute(
            """SELECT m.id, m.code, m.name, j.jobtype, COUNT(*) n
                 FROM jobs j JOIN machines m ON m.id=j.machine_id
                WHERE j.jobtype IN ('BD','CM')
                  AND j.created_at >= ? AND j.created_at <= ?
                  AND j.status NOT IN """ + VOID_SQL + NOTKPI_J + """
                  AND m.factory_id=?
                GROUP BY m.id, j.jobtype""", (d_from + " 00:00:00", d_to + " 23:59:59", fac))]
        by = {}
        for r in rows:
            e = by.setdefault(r["id"], {"id": r["id"], "code": r["code"], "name": r["name"],
                                        "bd": 0, "cm": 0})
            e["bd" if r["jobtype"] == "BD" else "cm"] = r["n"]
        machines = sorted(by.values(), key=lambda m: -(m["bd"] + m["cm"]))
        for m in machines:
            m["total"] = m["bd"] + m["cm"]

        # the machine to draw the history of: the one asked for, else the worst
        mid = machine_id or (machines[0]["id"] if machines else 0)
        mrow = c.execute("SELECT id,code,name FROM machines WHERE id=?", (mid,)).fetchone()
        hist = [dict(r) for r in c.execute(
            """SELECT j.jobtype, SUBSTR(j.created_at,1,10) d
                 FROM jobs j WHERE j.machine_id=? AND j.jobtype IN ('BD','CM')
                  AND j.status NOT IN """ + VOID_SQL + NOTKPI_J, (mid,))] if mid else []

    def key(d):
        if bucket == "day":
            return d
        if bucket == "month":
            return d[:7]
        if bucket == "quarter":
            y, mo = int(d[:4]), int(d[5:7])
            return f"{y}-Q{(mo - 1) // 3 + 1}"
        wd = date.fromisoformat(d)                    # Monday of that week
        return (wd - timedelta(days=wd.weekday())).isoformat()

    buckets = {}
    for r in hist:
        e = buckets.setdefault(key(r["d"]), {"k": key(r["d"]), "bd": 0, "cm": 0})
        e["bd" if r["jobtype"] == "BD" else "cm"] += 1
    series = [buckets[k] for k in sorted(buckets)][-14:]

    return {"from": d_from, "to": d_to, "machines": machines, "bucket": bucket,
            "machine": dict(mrow) if mrow else None, "series": series}


@router.get("/kpi/downtime")
async def kpi_downtime(req: Request, d_from: str = "", d_to: str = "", fac: str = ""):
    """Downtime on breakdowns, split between the two departments that own it.

    ENGINEERING runs from the report — the operator confirming the machine is
    stopped — to the technician pressing Stop, and picks up again at every rejection.
    PRODUCTION runs from that Stop until the operator accepts the machine back, or
    rejects it. The spans are back to back, so the two always sum to the total and
    neither department can lose a minute in the gap. Counted in operating hours only.

    Rejected and Cancelled jobs are left out, as they are everywhere else a manager
    reads a number (VOID_SQL) — a job nobody accepted has no closing time to measure.

    Jobs flagged kpi_exclude are left out too. That flag is for a row whose recorded
    clock is not the machine's real stop — a job left open for days, a duplicate, a
    test — and it is set by an admin, one job at a time. The job itself is untouched
    and still shows its own downtime; only this total skips it.
    """
    u = user_from(req)
    d_to = d_to or today()
    d_from = d_from or (date.fromisoformat(d_to) - timedelta(days=29)).isoformat()
    # Always the plant chosen at login (or switched to). A `fac` on the URL used to be
    # obeyed for anyone, so the downtime panel could show another plant's numbers.
    _fac = u.get("active_factory") or u.get("factory_id")
    f0, t1 = d_from + " 00:00:00", d_to + " 23:59:59"
    _now = now()
    with closing(db()) as c:
        hours, groups = _fac_hours(c, _fac)
        jobs = [dict(r) for r in c.execute("""
            SELECT j.id, j.jobid, j.machine_id, j.created_at, j.done_at, j.approved_at,
                   j.status, j.descr, j.pending_reason, j.report_name, m.code mcode,
                   m.name mname, m.asset_group agroup
            FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
            WHERE j.jobtype='BD' AND j.created_at >= ? AND j.created_at <= ?
              -- DOWNTIME IS TIME AGAINST AN ASSET. A breakdown with no machine has no
              -- required time, no availability and no MTBF, so its hours were landing
              -- in the plant total with nothing to carry them: the dashboard cards
              -- (which have always dropped them) and this panel then disagreed by
              -- exactly those hours. New work with no asset is no longer saved as a BD
              -- at all; this keeps the BD rows already in the database out of the
              -- figures too, without touching their job numbers.
              AND j.machine_id IS NOT NULL
              AND (COALESCE(m.factory_id,j.factory_id)=? OR COALESCE(m.factory_id,j.factory_id) IS NULL)
              AND COALESCE(j.kpi_exclude,0)=0
              AND j.status NOT IN """ + VOID_SQL + " ORDER BY j.created_at",
            (f0, t1, _fac))]
        evmap = {}
        if jobs:
            qs = ",".join("?" * len(jobs))
            for e in c.execute(
                    f"SELECT job_id,status,created_at FROM job_events"
                    f" WHERE job_id IN ({qs}) ORDER BY job_id, id", [j["id"] for j in jobs]):
                evmap.setdefault(e["job_id"], []).append((e["status"], e["created_at"]))

        # The same window counted by job type — preventive against corrective against
        # breakdown, the one ratio that says whether a plant is maintaining or
        # firefighting. It rides on this endpoint rather than getting its own so the two
        # pictures on the dashboard can never disagree about which jobs were in the
        # period, which window, or which plant.
        # Raised in the period, and how many of those are finished. One query, because
        # a completion figure whose numerator and denominator come from different WHERE
        # clauses is the classic way to publish a percentage nobody can reproduce.
        #
        # "Finished" is status Done — the technician stopped AND production accepted the
        # machine back. Not ServiceCompleted: that is work waiting on somebody else's
        # signature, and counting it would let the workshop close its own jobs. Every
        # other completion number on this dashboard already draws the line there.
        _mix = {}
        for r in c.execute(
            """SELECT j.jobtype jobtype, COUNT(*) n,
                      SUM(CASE WHEN j.status='Done' THEN 1 ELSE 0 END) done
                 FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
                WHERE j.created_at >= ? AND j.created_at <= ?
                  AND (COALESCE(m.factory_id,j.factory_id)=? OR COALESCE(m.factory_id,j.factory_id) IS NULL)
                  AND j.status NOT IN """ + VOID_SQL + NOTKPI_J + """ GROUP BY j.jobtype""",
                (f0, t1, _fac)):
            _mix[r["jobtype"] or "?"] = (r["n"], int(r["done"] or 0))
        mix = {k: v[0] for k, v in _mix.items()}
        # the same counts split by whether they are finished, so one ring can show both
        # the share of work AND how much of each share was closed
        mix_done = {k: v[1] for k, v in _mix.items()}

        # Two completion figures, because they answer different questions and a plant
        # can be good at one while doing none of the other. Corrective work is CM and BD
        # together — a breakdown and a fault report are the same promise to production,
        # kept or not. Preventive is PM alone, and it is the one that quietly goes to
        # zero when the week gets busy.
        def _comp(*types):
            tot = sum(_mix.get(t, (0, 0))[0] for t in types)
            dn = sum(_mix.get(t, (0, 0))[1] for t in types)
            return {"total": tot, "done": dn, "open": tot - dn,
                    "pct": round(dn / tot * 100) if tot else None}
        completion = {"pm": _comp("PM"), "cm": _comp("CM", "BD", "IMP", "PRJ")}

    def _hours_for(j):
        g = groups.get(j.get("agroup") or "") or {}
        if not g:
            return hours
        sh = int(str(g.get("start") or "07:00").split(":")[0])
        eh = int(str(g.get("end") or "21:00").split(":")[0])
        _gd = {**(hours[4] if len(hours) > 4 else {}), **day_windows(g)}
        return (sh, (24 if eh == 0 else eh), hours[2], hours[3], _gd)

    rows, machines, sp_all = [], {}, []
    plant = {"eng_min": 0, "prod_min": 0, "hold_min": 0, "wait_min": 0, "work_min": 0,
             "total_min": 0, "bd_count": 0}
    for j in jobs:
        h = _hours_for(j)
        evs = evmap.get(j["id"], [])
        sp = dt.split(j["created_at"], evs, h, _now, (j.get("done_at"), j.get("approved_at")))
        sp_all.append((sp["spans"], h))
        hold = dt.hold_inside(evs, h, _now)
        # engineering, told apart: repairing = logged Start→Stop, waiting = the rest.
        # Subtracting rather than adding two independent sums is deliberate — it makes
        # waiting + repairing equal engineering exactly, so the bar can never show a
        # sliver of nothing between two segments.
        work = min(dt.work_inside(evs, h, _now), sp["eng_min"])
        wait = max(0, sp["eng_min"] - work)
        row = {"job_id": j["id"], "jobid": j["jobid"], "mcode": j.get("mcode"),
               "mname": j.get("mname"), "status": j["status"], "descr": j.get("descr"),
               "created_at": j["created_at"], "done_at": j.get("done_at"),
               "approved_at": j.get("approved_at"), "open": not j.get("approved_at"),
               "eng_min": sp["eng_min"], "prod_min": sp["prod_min"],
               "wait_min": wait, "work_min": work,
               "total_min": sp["total_min"], "hold_min": min(hold, sp["eng_min"])}
        rows.append(row)
        # NOT EVERY BREAKDOWN IS AGAINST A MACHINE. A blocked drain, a door, a run of
        # pipe — the report form deliberately takes free text when the thing is not in
        # the asset register, and that text is the job's report_name. Every one of those
        # used to be bucketed together under machine_id 0 and drawn as a single row
        # labelled "—", which then ranked FIRST in "Top 5 breakdown assets": the worst
        # asset in the plant, with no name, and a row that did nothing when clicked
        # because there is no machine history to open. They are now kept apart by the
        # place they were raised against, and the place is what the row is called.
        mid = j.get("machine_id") or 0
        if mid:
            key, code, name = mid, (j.get("mcode") or ""), (j.get("mname") or "")
        else:
            place = (j.get("report_name") or "").strip()
            key = "place:" + (place.lower() or "?")
            code = place or "ไม่ได้ระบุเครื่อง / no asset named"
            name = ""
        m = machines.setdefault(key, {"machine_id": mid, "code": code, "name": name,
                                      "no_asset": not mid, "bd_count": 0,
                                      "eng_min": 0, "prod_min": 0, "hold_min": 0,
                                      "wait_min": 0, "work_min": 0, "total_min": 0})
        for k in ("eng_min", "prod_min", "hold_min", "wait_min", "work_min", "total_min"):
            m[k] += row[k]
            plant[k] += row[k]
        m["bd_count"] += 1
        plant["bd_count"] += 1

    # ── the bar chart ─────────────────────────────────────────────────────────────
    # Each span is clipped against each bucket it touches and re-counted in operating
    # hours there, rather than a job's total being dropped whole into the bucket it
    # started in. A breakdown that runs from Friday afternoon into Monday belongs to
    # both days in the proportion the machine was actually down on each, and a chart
    # that spikes on the report date is a chart that hides exactly that.
    import bisect as _bisect
    _unit, _bks = _buckets(d_from, d_to)
    _bstart = [s for _, s, _ in _bks]
    bars = [{"label": lb, "from": s.isoformat(sep=" "), "to": e.isoformat(sep=" "),
             "eng_min": 0.0, "prod_min": 0.0, "total_min": 0.0} for lb, s, e in _bks]
    for _spans, _h in sp_all:
        for _side, _a, _b in _spans:
            i = max(0, _bisect.bisect_right(_bstart, _a) - 1)
            while i < len(_bks) and _bks[i][1] < _b:
                _bs, _be = _bks[i][1], _bks[i][2]
                if _be > _a:
                    m = dt.op_minutes(max(_a, _bs), min(_b, _be), *_h)
                    bars[i]["eng_min" if _side == dt.ENG else "prod_min"] += m
                    bars[i]["total_min"] += m
                i += 1
    for _b2 in bars:
        for _k2 in ("eng_min", "prod_min", "total_min"):
            _b2[_k2] = round(_b2[_k2])

    ml = sorted(machines.values(), key=lambda x: -x["total_min"])
    plant["eng_pct"] = round(plant["eng_min"] / plant["total_min"] * 100, 1) if plant["total_min"] else None
    plant["prod_pct"] = round(plant["prod_min"] / plant["total_min"] * 100, 1) if plant["total_min"] else None
    plant["mttr_min"] = round(plant["eng_min"] / plant["bd_count"]) if plant["bd_count"] else None
    for _k, _p in (("wait_min", "wait_pct"), ("work_min", "work_pct")):
        plant[_p] = round(plant[_k] / plant["total_min"] * 100, 1) if plant["total_min"] else None
    return {"from": d_from, "to": d_to, "hours": {"start": hours[0], "end": hours[1],
            "offdays": list(hours[2]), "holidays": list(hours[3])},
            "plant": plant, "mix": mix, "mix_done": mix_done,
            "bars": bars, "bar_unit": _unit,
            "completion": completion, "machines": ml,
            "jobs": sorted(rows, key=lambda x: -x["total_min"])}


# ---------------- b412: dashboard report (PDF) ----------------
@router.get("/kpi/report-extra")
async def kpi_report_extra(req: Request, d_from: str = "", d_to: str = ""):
    """The parts of the printed dashboard report the dashboard itself has no endpoint
    for: the corrective jobs that took the most repair time, the week-by-week counts,
    throughput for the period, and crew hours per technician — all on the period the
    dashboard is showing, and all with the dashboard's own rules (void and
    kpi-excluded jobs out, PM verdicts as the compliance ring, team time per b390)."""
    u = user_from(req)
    fac = u.get("active_factory") or u.get("factory_id")
    d_to = d_to or today()
    d_from = d_from or (date.fromisoformat(d_to) - timedelta(days=6)).isoformat()
    f0, t1 = d_from + " 00:00:00", d_to + " 23:59:59"
    d0 = date.fromisoformat(d_from)
    wk = lambda s: max(0, (date.fromisoformat(str(s)[:10]) - d0).days // 7)   # week index in the period
    nweeks = (date.fromisoformat(d_to) - d0).days // 7 + 1
    def weeks():
        out = []
        for i in range(nweeks):
            a = d0 + timedelta(days=7 * i)
            b = min(a + timedelta(days=6), date.fromisoformat(d_to))
            out.append({"w": f"{a.day}/{a.month}–{b.day}/{b.month}"})
        return out
    with closing(db()) as c:
        names = {r["id"]: r["name"] for r in c.execute("SELECT id,name FROM users")}
        crew = _crew_credit(c, fac)
        # work minutes per job in the period (all job types), and who is still on the clock
        jm, running = {}, set()
        for r in c.execute("""SELECT t.job_id, t.start s, t.end e FROM timelogs t JOIN jobs j ON j.id=t.job_id
              WHERE t.seg_type='work' AND t.start>=? AND t.start<=? AND j.factory_id=?""", (f0, t1, fac)):
            jm[r["job_id"]] = jm.get(r["job_id"], 0) + _minutes(r["s"], r["e"])
            if not r["e"]:
                running.add(r["job_id"])
        # top corrective jobs by repair time
        cm = [dict(r) for r in c.execute("""SELECT j.id, j.jobid, j.jobtype, j.descr, j.status, j.lead_tech,
                 COALESCE(m.code,'') mcode, COALESCE(m.name, j.asset_text, '') mname, COALESCE(m.criticality,'') crit
              FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
              WHERE j.jobtype IN ('CM','BD') AND j.status NOT IN """ + VOID_SQL + NOTKPI_J + """
                AND COALESCE(m.factory_id,j.factory_id)=?""", (fac,)) if r["id"] in jm]
        for j in cm:
            j["work_min"] = round(jm.get(j["id"], 0)); j["running"] = j["id"] in running
            j["lead"] = names.get(j.pop("lead_tech"), "")
        cm.sort(key=lambda x: -x["work_min"])
        # weekly: PM by due date with the ring's verdicts; CM+BD and IMP by date raised
        pm_w, cm_w, imp_w = weeks(), weeks(), weeks()
        for x in pm_w: x.update(ontime=0, late=0, open=0, notdue=0)
        for x in cm_w + imp_w: x.update(done=0, sign=0, open=0)
        for r in c.execute("""SELECT j.due_date,
              CASE WHEN """ + PM_ONTIME + """ THEN 'ontime' WHEN j.status IN ('ServiceCompleted','Done') THEN 'late'
                   WHEN NOT """ + PM_COUNTS + """ THEN 'notdue' ELSE 'open' END v
              FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
              WHERE j.jobtype='PM' AND j.due_date>=? AND j.due_date<=? AND j.status NOT IN """ + VOID_SQL + NOTKPI_J + """
                AND COALESCE(m.factory_id,j.factory_id)=?""", (d_from, d_to, fac)):
            i = wk(r["due_date"])
            if i < nweeks: pm_w[i][r["v"]] += 1
        by_type, tp = {}, {"total": 0, "completed": 0, "pending_signature": 0, "open_total": 0, "on_hold": 0}
        for r in c.execute("""SELECT j.jobtype, j.status, j.created_at FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
              WHERE j.created_at>=? AND j.created_at<=? AND j.status NOT IN """ + VOID_SQL + NOTKPI_J + """
                AND COALESCE(m.factory_id,j.factory_id)=?""", (f0, t1, fac)):
            t, s = r["jobtype"], r["status"]
            by_type[t] = by_type.get(t, 0) + 1
            tp["total"] += 1
            if s in ("ServiceCompleted", "Done"): tp["completed"] += 1
            if s == "ServiceCompleted": tp["pending_signature"] += 1
            if s not in ("ServiceCompleted", "Done"): tp["open_total"] += 1
            if s in ("Hold", "Paused"): tp["on_hold"] += 1
            if t in ("CM", "BD", "IMP"):
                i = wk(r["created_at"])
                if i < nweeks:
                    b = (imp_w if t == "IMP" else cm_w)[i]
                    b["done" if s == "Done" else "sign" if s == "ServiceCompleted" else "open"] += 1
        tp["jobs_by_type"] = by_type
        # crew hours per technician, every job type (team time)
        bt = {}
        for r in c.execute("""SELECT t.tech, t.job_id, t.start s, t.end e, j.lead_tech, j.helpers FROM timelogs t JOIN jobs j ON j.id=t.job_id
              WHERE t.seg_type='work' AND t.start>=? AND t.start<=? AND j.factory_id=?
                AND j.status NOT IN """ + VOID_SQL + NOTKPI_J, (f0, t1, fac)):
            mm = _minutes(r["s"], r["e"])
            for i in crew(r["lead_tech"], r["helpers"], r["tech"]):
                x = bt.setdefault(i, {"tech": names.get(i, "?"), "work_min": 0.0, "jobs": set()})
                x["work_min"] += mm; x["jobs"].add(r["job_id"])
        by_tech = sorted(({"tech": x["tech"], "work_min": round(x["work_min"]), "jobs": len(x["jobs"])}
                          for x in bt.values()), key=lambda x: -x["work_min"])
    return {"from": d_from, "to": d_to, "cm_top": cm[:10], "pm_week": pm_w, "cm_week": cm_w,
            "imp_week": imp_w, "throughput": tp, "by_tech": by_tech}

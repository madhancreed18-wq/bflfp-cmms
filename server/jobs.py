import os, base64
from datetime import datetime, date, timedelta
from contextlib import closing

from fastapi import APIRouter, Request, HTTPException

from .config import UPLOADS, BASE
from .db import (db, now, today, day_range, job_row, role_ids, user_names, log_status,
                 hold_minutes, mins_between, next_jobid, set_stage, stage_for, VOID_SQL,
                 NOTKPI_J, next_workday, state_get, state_set, own_account_map,
                 offdays_of, dept_code)
from .auth import user_from, require_role
from .push import notify_users

import logging
# the app's own logger — the file handler is attached by logs.setup(), and
# everything written here shows in Manage → Logs. print() does not: it goes to
# a console nobody is watching, which is where these messages used to die.
_log = logging.getLogger("cmms")

# The same fault, on the same machine, from the same person cannot become two work
# orders while the first one is still open — at any age. Once that job is closed
# (Done / Rejected / Cancelled) the fault coming back is a new report, except inside
# DUP_WINDOW, which catches a double press whose first job was closed immediately.
OPEN_STATUSES = ("Reported", "WaitingApproval", "WaitingAssignment", "Assigned",
                 "InProgress", "Paused", "Hold", "Rework", "ServiceCompleted")
DUP_WINDOW = 120        # seconds

# A job the planner can still call off. Closed work (Done / Rejected / already
# Cancelled) is a record and stays as it is.
CANCELLABLE = ("Reported", "WaitingApproval", "WaitingAssignment", "Assigned",
               "InProgress", "Paused", "Hold", "Rework", "ServiceCompleted")

router = APIRouter(prefix="/api")

ACTIVE = ("('Reported','WaitingApproval','WaitingAssignment','Assigned',"
          "'InProgress','Paused','Rework','ServiceCompleted','Hold')")


# Engineering Center and an admin answer for the whole group, so their screens span
# all three plants. A plant manager reads one plant, like the planner they shadow.
# plants and carry a factory filter. Everyone else — operator, technician, planner —
# works at one plant and only ever sees the one they signed in to.
CROSS_FACTORY = ("engcenter", "admin")


def _view_factory(u, fac_param=""):
    """The plant a list should show: a factory id, or None for every plant.

    `fac` on the request is what the filter on screen sends — a factory id, or "all".
    It is honoured only for the roles that work across plants; for everyone else the
    plant chosen at login is the answer whatever the request asks for, so a filter
    cannot be used to see another plant's work.
    """
    # Every role — admin and Engineering Center included — sees the plant chosen at the
    # login screen, or switched to with the plant chips (POST /api/factory moves the
    # session). The URL's `fac` / "all" is no longer obeyed: it let a list, a count or
    # an export show all three plants mixed together while the header named one.
    return u.get("active_factory") or u.get("factory_id")


def _job_in_factory(c, jid, u):
    """Return a job only when the caller is allowed to see it.

    One plant for the people who work in it; any plant for Engineering Center or an admin.
    """
    row = job_row(c, jid)
    if u.get("role") in CROSS_FACTORY:
        return row
    fac = u.get("active_factory") or u.get("factory_id")
    machine = c.execute("SELECT factory_id FROM machines WHERE id=?", (row["machine_id"],)).fetchone()
    if machine and machine["factory_id"] != fac:
        raise HTTPException(403, "job belongs to another factory")
    # A job with no machine has no machine's plant to be judged by, and this test used to
    # let every one of them through to every plant. Rare while only planners could raise
    # one; not rare now that a reporter can file work on something unregistered. The
    # job's own factory_id is written when it is raised, so use that. A row old enough to
    # have neither is still shown — over-shared, never hidden, the same way the lists
    # treat it.
    elif not machine and row["factory_id"] and row["factory_id"] != fac:
        raise HTTPException(403, "job belongs to another factory")
    return row


def _tech_ids(c, u):
    """Ids this signed-in user stands for: themselves, plus every technician who
    signs in with this login. Work is assigned to people; people may share a login."""
    ids = [u["id"]]
    try:
        ids += [r["id"] for r in c.execute("SELECT id FROM users WHERE login_id=?", (u["id"],))]
    except Exception:
        pass
    return list(dict.fromkeys(ids))


def _mine(ids, alias="j"):
    """(sql, args) matching jobs led by, or helped on by, any of these ids."""
    ph = ",".join("?" * len(ids))
    like = " OR ".join([f"','||{alias}.helpers||',' LIKE '%,'||?||',%'"] * len(ids))
    return f"({alias}.lead_tech IN ({ph}) OR {like})", list(ids) + [str(i) for i in ids]


def _assigned_to(row, uid):
    ids = uid if isinstance(uid, (list, tuple, set)) else [uid]
    helpers = (row["helpers"] or "").split(",")
    return any(row["lead_tech"] == i or str(i) in helpers for i in ids)


def _team_of(c, row):
    """The crew the planner put on this job — lead first, then helpers.

    One phone is shared by the whole team, so when work is held or finished the
    app has to ask *which* of these people did it. This is the only list it offers.
    """
    ids = [row["lead_tech"]] + [h for h in (row["helpers"] or "").split(",") if h.strip().isdigit()]
    ids = [int(i) for i in dict.fromkeys(ids) if i]
    names = user_names(c)
    return [{"id": i, "name": names.get(str(i), "")} for i in ids]


def stamp_owner1(c, jid, uid):
    """The first technician to take this job. Write-once, and that is the whole point.

    Called from Start. If it is already set nothing happens — not for a move, not for a
    reassignment, not for admin. A field that can be overwritten is `lead_tech`, and
    `lead_tech` is what failed.
    """
    if not (jid and uid):
        return
    try:
        row = c.execute("SELECT owner1 FROM jobs WHERE id=?", (jid,)).fetchone()
        if row and row["owner1"]:
            return
        nm = user_names(c).get(str(uid), "")
        c.execute("UPDATE jobs SET owner1=?, owner1_name=? WHERE id=? AND owner1 IS NULL",
                  (uid, nm, jid))
    except Exception:
        pass


def stamp_owner2(c, jid, uid):
    """Who the job was moved to. Rewritten on every move, so it is the current holder.

    The name travels with the id because the reason this column exists is somebody being
    deleted: an id on its own points at a user row that may not be there tomorrow.
    """
    if not jid:
        return
    try:
        nm = user_names(c).get(str(uid), "") if uid else ""
        c.execute("UPDATE jobs SET owner2=?, owner2_name=? WHERE id=?", (uid or None, nm, jid))
    except Exception:
        pass


def _crew_gate(c, row, u, verb="start"):
    """Refuse a technician who is not on this job's crew.

    Only when the job HAS a crew. An unassigned job is deliberately open — see
    _acting_tech — because the person on shift at 2am is the only one who can take it.
    Planner and admin pass: the planner is the release valve for a timer nobody comes
    back to, and admin answers for the record.
    """
    if u["role"] in ("planner", "admin"):
        return
    team = [m["id"] for m in _team_of(c, row)]
    if not team:
        return
    mine = set(_tech_ids(c, u))
    if not (mine & set(team)):
        who = ", ".join(m["name"] for m in _team_of(c, row) if m["name"]) or "the assigned crew"
        raise HTTPException(403, f"งานนี้มอบหมายให้ {who} — คนอื่นเริ่ม/ปิดงานนี้ไม่ได้ /"
                                 f" this job is assigned to {who}; nobody else can {verb} it")


def _crew_today(c, u, uid):
    """The rest of this technician's crew for today, from the day's saved teams.

    Starting an unassigned job puts it on the person who picked it up — and on the crew
    he is working with, because that is who will actually be standing at the machine. A
    technician on no team today gets the job to himself, which is right: plenty of work
    is a one-person job.
    """
    try:
        from .db import today as _today
        fac = u.get("active_factory") or u.get("factory_id")
        for r in c.execute("SELECT members FROM plan_teams WHERE factory_id=? AND day=?",
                           (fac, _today())):
            ids = [int(x) for x in str(r["members"] or "").split(",") if str(x).strip().isdigit()]
            if uid in ids:
                return [i for i in ids if i != uid]
    except Exception:
        pass
    return []


def _self_person(c, u):
    """The PERSON behind this login, where the plant issued one account per technician.

    Nothing on the phone sends a name — the front end has never had a picker — so an
    unnamed call has always fallen back to the ACCOUNT, and pressing Start booked the job
    to the handset rather than to whoever was holding it. That is where the `jack` and
    `nut` columns on the assignment board come from: `techbfl7` is signed in, `techbfl7`
    becomes the job's lead, and the board draws a column for a lead that is on no crew,
    which cannot be deleted because it is regenerated from the job every time.

    On a plant with one account per technician there is nothing to ask: `techbfl7` IS
    jack, and the link now says so. Resolve it and the work lands on the person. Confined
    to those plants by `own_account_map`, which is all-or-nothing per plant — BFLFP,
    where one handset really is shared by Choke, Mark and boss, has no answer to this
    question and keeps booking to the account exactly as before.
    """
    if u.get("role") != "technician":
        return 0
    fac = u.get("active_factory") or u.get("factory_id")
    if not fac:
        return 0
    for pid, aid in own_account_map(c, fac).items():
        if aid == u["id"]:
            return pid
    return 0


def _acting_tech(c, row, u, as_tech):
    """Resolve who is reporting the work: a chosen team member, else the login itself.

    A name is only accepted when the planner actually put that person on the job —
    otherwise a shared phone could log work against anyone in the factory.
    """
    if as_tech in (None, "", 0):
        return _self_person(c, u) or u["id"]
    try:
        aid = int(as_tech)
    except (TypeError, ValueError):
        raise HTTPException(400, "bad technician")
    team = [m["id"] for m in _team_of(c, row)]
    # A job with a crew belongs to that crew, and to nobody else. Naming any technician
    # in the factory was how one shared handset covered for a sick day; individual
    # logins are what replaced that, and leaving the door open means a job can be worked
    # and closed by somebody the planner never put on it. A job with NO crew is open to
    # the whole plant — the two-in-the-morning breakdown has to be pickable by whoever
    # is actually there.
    allowed = list(dict.fromkeys(team)) if team else _factory_tech_ids(c, u)
    if aid not in allowed:
        who = user_names(c).get(str(aid), str(aid))
        raise HTTPException(403, f"{who} ไม่ใช่ช่างของโรงงานนี้ / {who} is not a technician in this factory")
    return aid


def _named_tech(c, u, as_tech):
    """The technician id named on this call, if it is a real technician in this factory.

    Nothing links a person to a phone, so the name travels on every request. It is what
    tells us which running timer belongs to the crew using this phone.
    """
    try:
        aid = int(as_tech or 0)
    except (TypeError, ValueError):
        return 0
    if not aid:
        return 0
    fac = u.get("active_factory") or u.get("factory_id")
    ok = c.execute("SELECT 1 FROM users WHERE id=? AND active=1 AND role='technician'"
                   " AND (factory_id=? OR COALESCE(factory_id,0)=0)", (aid, fac)).fetchone()
    return aid if ok else 0


def _factory_tech_ids(c, u):
    """Every active technician in the user's factory."""
    fac = u.get("active_factory") or u.get("factory_id")
    return [r["id"] for r in c.execute(
        "SELECT id FROM users WHERE active=1 AND role='technician' AND (factory_id=? OR COALESCE(factory_id,0)=0)", (fac,))]


def _no_phone(c, ids):
    """Technicians among `ids` who no login can sign in as.

    Work booked to one of them is invisible on every phone — it silently sits there —
    so assignment refuses rather than losing it. A phone comes from the team the
    planner puts them on (Team A → techfp1).
    """
    ids = [int(i) for i in ids if str(i).strip().isdigit()]
    if not ids:
        return []
    return [r["name"] for r in c.execute(
        "SELECT name FROM users WHERE id IN (%s) AND COALESCE(can_login,1)=0"
        " AND login_id IS NULL ORDER BY name" % ",".join("?" * len(ids)), ids)]


def _job_factory(c, machine_id, u):
    """The plant a job belongs to — its machine's plant, because that is where the work
    physically is. Everyone notified about a job is read from this, so a report always
    reaches the people who can walk to the machine and nobody else. A job with no
    machine on it falls back to the plant the caller is signed in to.
    """
    if machine_id:
        r = c.execute("SELECT factory_id FROM machines WHERE id=?", (machine_id,)).fetchone()
        if r and r["factory_id"]:
            return r["factory_id"]
    return u.get("active_factory") or u["factory_id"]


def _planner_ids(c, factory_id):
    return [r["id"] for r in c.execute(
        "SELECT id FROM users WHERE active=1 AND role='planner' AND factory_id=?", (factory_id,))]



# ── Work that did not get done on its day ────────────────────────────────────────
# A plan nobody moves stops being a plan. Yesterday's undone jobs used to keep
# yesterday's date for ever: the morning sheet was wrong, today's plan did not
# include them, and the only reason the crew ever saw them was that My jobs ignores
# dates. So once a day the undone corrective work is moved on to the next working
# day — and it is moved LOUDLY: the day it was first promised is kept in
# planned_date_orig, `carryover` counts how many times it has slipped, and the move
# is written into the job's own history. A job that has been carried four times is
# not a scheduling detail, it is a question for the meeting.
#
# PM is deliberately excluded. The compliance question is "was the July checklist
# done in July", and quietly re-dating it into August is exactly how a plant answers
# that question wrongly. A missed PM stays on its date and shows as overdue until a
# planner re-dates it themselves.
CARRY_TYPES = ("CM", "BD", "IMP", "PRJ", "PRD")
CARRY_STATUS = ("Reported", "WaitingAssignment", "Assigned", "Released",
                "Rework", "Hold", "Paused", "InProgress")


def roll_forward(c, fac, d_today=None):
    """Move a factory's undone corrective work onto the next working day. Idempotent
    within a day — the marker is stored, so a restart cannot run it twice."""
    import json as _json
    d_today = d_today or today()
    key = f"rollfwd:{fac}"
    if state_get(c, key) == d_today:
        return 0
    r = c.execute("SELECT hours_json FROM factories WHERE id=?", (fac,)).fetchone()
    try:
        cfg = _json.loads(r["hours_json"]) if r and r["hours_json"] else {}
    except Exception:
        cfg = {}
    offdays = offdays_of(cfg)          # a weekday with its own hours is a working day
    holidays = tuple(cfg.get("holidays") or [])
    # An undone job lands on today, unless today is not a working day — then on the
    # next one that is. Rolling work onto a Sunday is how a plan stops being believed.
    from datetime import date as _date
    nxt = d_today
    if _date.fromisoformat(d_today).weekday() in offdays or d_today in holidays:
        nxt = next_workday(d_today, offdays, holidays)
    qs = ",".join("?" * len(CARRY_TYPES))
    ss = ",".join("?" * len(CARRY_STATUS))
    rows = [dict(x) for x in c.execute(
        f"""SELECT j.id, j.jobid, j.planned_date, j.planned_date_orig, j.carryover
            FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
            WHERE j.jobtype IN ({qs}) AND j.status IN ({ss})
              AND j.planned_date IS NOT NULL AND j.planned_date != ''
              AND j.planned_date < ? AND (COALESCE(m.factory_id,j.factory_id)=? OR COALESCE(m.factory_id,j.factory_id) IS NULL)""",
        [*CARRY_TYPES, *CARRY_STATUS, d_today, fac])]
    from .chat import log_job_event as _lje
    n = 0
    for j in rows:
        orig = (j.get("planned_date_orig") or "").strip() or j["planned_date"]
        cnt = int(j.get("carryover") or 0) + 1
        c.execute("UPDATE jobs SET planned_date=?, planned_date_orig=?, carryover=? WHERE id=?",
                  (nxt, orig, cnt, j["id"]))
        _lje(c, j["id"], None,
             f"↻ ยกยอดจาก {j['planned_date']} → {nxt} (ครั้งที่ {cnt}) /"
             f" carried forward from {j['planned_date']} to {nxt} (time {cnt})")
        n += 1
    state_set(c, key, d_today)
    c.commit()
    return n


# A finished CM/BD whose raiser also worked on it — a technician who reported a fault
# in reporter mode and then repaired it. The raiser may not accept their own repair,
# so it goes to the planner's queue instead. "The raiser" is the login and every
# person name linked to it (a shared crew login files its people under login_id).
_R = "COALESCE(j.requester_id, j.created_by)"
SELFDONE_SQL = ("EXISTS (SELECT 1 FROM users ru WHERE (ru.id=" + _R + " OR ru.login_id=" + _R + ") AND ("
                " ru.id=j.lead_tech"
                " OR ','||COALESCE(j.helpers,'')||',' LIKE '%,'||CAST(ru.id AS TEXT)||',%'"
                " OR EXISTS (SELECT 1 FROM timelogs t WHERE t.job_id=j.id AND t.tech=ru.id)))")

@router.get("/jobs")
async def jobs(req: Request, view: str = "", d: str = "", q_text: str = "", fac: str = "",
               d_from: str = "", d_to: str = ""):
    u = user_from(req)
    d = d or today()
    # fcode travels with every row so an all-plants list can be filtered BY plant on
    # the screen. It costs one join and is empty for a job raised against no machine.
    q = """SELECT j.*, m.code mcode, m.name mname, COALESCE(m.criticality,'') mcrit, u.name lead_name, f.code fcode
           FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
           LEFT JOIN users u ON u.id=j.lead_tech
           LEFT JOIN factories f ON f.id=m.factory_id"""
    args = []
    tids = None
    if view in ("myday", "techtree", "myplan", "techassigned", "techwaiting", "techrejected"):
        with closing(db()) as _c:
            tids = _tech_ids(_c, u)
    if view == "pool":
        # brackets matter: the plant filter and the search are ANDed on below, and without
        # them "A OR B AND plant" let every plant's planned-today jobs into this list
        q += " WHERE (j.status IN ('Reported','WaitingApproval','WaitingAssignment','Assigned','Hold') OR (j.planned_date=?))"
        args = [d]
    elif view == "myday":
        _m, _a = _mine(tids)
        q += (" WHERE j.planned_date=? AND j.status IN ('Assigned','InProgress','Paused','Rework','ServiceCompleted')"
              " AND " + _m)
        args = [d] + _a
    elif view == "techtree":
        _m, _a = _mine(tids)
        q += " WHERE j.planned_date=? AND " + _m + " AND j.status NOT IN ('Cancelled')"
        args = [d] + _a
    elif view == "mine":
        # Everything this operator reported, at every stage. It used to drop a job the
        # moment the planner assigned it, so the list looked empty; now they can follow
        # their own report through to sign-off and the status chips do the filtering.
        from datetime import date as _d, timedelta as _td
        _w = (_d.fromisoformat(d) - _td(days=30)).isoformat() + " 00:00:00"
        with closing(db()) as _c:
            _me = _tech_ids(_c, u)        # this login, plus the people who report under it
        _ph = ",".join("?" * len(_me))
        q += (f" WHERE (j.created_by IN ({_ph}) OR j.requester_id IN ({_ph}))"
              " AND j.created_at >= ? AND j.status NOT IN ('Cancelled')")
        args = _me + _me + [_w]
    elif view == "myplan":
        _m, _a = _mine(tids)
        q += (" WHERE " + _m +
              " AND j.status IN ('Assigned','InProgress','Paused','Rework','Hold','ServiceCompleted')")
        args = _a
    elif view == "technew":                                    # unassigned new reports only (incoming pool)
        q += " WHERE j.lead_tech IS NULL AND j.status IN ('Reported','WaitingApproval','WaitingAssignment')"
        args = []
    elif view in ("techassigned", "techwaiting"):
        # The technician's phone keeps two lists of its own work. "My jobs" is what is
        # still to do — a job the operator sent back counts, because rework is work.
        # "Approve pending" is what has been finished and is waiting for the operator
        # to accept it, so the crew can see the job is not lost, only not theirs now.
        # Neither list is time-limited: open work does not stop being open after a month.
        _m, _a = _mine(tids)
        # Both also carry anything no phone can reach: a job with no technician on it,
        # or one booked to somebody no login can sign in as. Work in that state used to
        # vanish from every list while sitting at In progress, with no way to stop the
        # timer.
        _pool = ("(j.lead_tech IS NULL OR j.lead_tech IN (SELECT id FROM users"
                 " WHERE COALESCE(can_login,0)=0 AND COALESCE(login_id,'')=''))")
        _st = ("('ServiceCompleted')" if view == "techwaiting"
               else "('Assigned','InProgress','Paused','Rework','Hold')")
        q += f" WHERE ({_m} OR {_pool}) AND j.status IN {_st}"
        args = _a
    elif view == "techrejected":                               # sent back by operator
        _m, _a = _mine(tids)
        q += " WHERE " + _m + " AND j.status='Rework'"
        args = _a
    elif view == "toapprove":
        # Finished work waiting for whoever has to accept it, and that is not one rule
        # but two. A PM job has no human requester — it comes out of the PM programme —
        # so the plant's planner accepts it; the factory scope is applied further down,
        # which is what makes it *that* plant's planner and not another's. A CM or BD
        # belongs to the person who raised it, whatever their role: an operator accepts
        # their own report, a planner accepts the one they raised, a manager theirs.
        # Anything else (IMP, and pre-2026 rows) keeps the old rule untouched.
        with closing(db()) as _c:
            _me = _tech_ids(_c, u)        # the login and every name linked to it
        _ph = ",".join("?" * len(_me))
        _own = f"(j.created_by IN ({_ph}) OR j.requester_id IN ({_ph}))"
        # the raiser fixed it themselves (a technician who also reports): the planner accepts
        _selfdone = SELFDONE_SQL
        if u["role"] in ("planner", "admin"):
            q += (f" WHERE j.status='ServiceCompleted' AND (UPPER(j.jobtype)='PM'"
                  f" OR (UPPER(j.jobtype) IN ('CM','BD','IMP') AND ({_own} OR {_selfdone})))")
            args = _me + _me
        elif u["role"] == "manager":
            q += (f" WHERE j.status='ServiceCompleted'"
                  f" AND UPPER(j.jobtype) IN ('CM','BD','IMP') AND {_own}")
            args = _me + _me
        else:
            q += (f" WHERE j.status='ServiceCompleted' AND NOT {_selfdone} AND ("
                  f"(UPPER(j.jobtype) IN ('CM','BD') AND {_own})"
                  f" OR (UPPER(j.jobtype) NOT IN ('CM','BD','PM') AND ({_own}"
                  " OR COALESCE(j.requester_id, j.created_by) NOT IN"
                  " (SELECT id FROM users WHERE role='operator'))))")
            args = _me + _me + _me + _me
    elif view == "approved":
        from datetime import date as _d, timedelta as _td
        a0 = (_d.fromisoformat(d) - _td(days=60)).isoformat() + " 00:00:00"
        q += " WHERE j.status='Done' AND COALESCE(NULLIF(j.approved_at,''), j.done_at, j.created_at) >= ?"
        args = [a0]
    elif view == "recent":
        from datetime import date as _d, timedelta as _td
        start = (_d.fromisoformat(d) - _td(days=9)).isoformat() + " 00:00:00"
        q += (" WHERE j.status IN ('Reported','WaitingApproval','WaitingAssignment','Hold','Rework')"
              " AND j.created_at >= ?")
        args = [start]
    elif view == "recentall":
        # "All jobs": by default the last 30 days AND everything planned ahead. This one
        # list backs the planner's overdue / on-hold / pending filters, so future work
        # that an upper date bound would hide has to be in it — which is why there is no
        # upper bound unless somebody asks for one.
        #
        # d_from / d_to override that. An empty d_to keeps the open end, so setting only
        # a start date widens or narrows the history without losing the work ahead.
        # A job is placed by its PLANNED date where it has one, and by the day it was
        # reported where it does not — the same rule the default window uses, so turning
        # the filter on cannot move a job between days.
        from datetime import date as _d, timedelta as _td
        _day = "COALESCE(NULLIF(j.planned_date,''), date(j.created_at))"
        start = d_from or (_d.fromisoformat(d) - _td(days=30)).isoformat()
        q += " WHERE %s >= ? AND j.status NOT IN ('Cancelled')" % _day
        args = [start]
        if d_to:
            q += " AND %s <= ?" % _day
            args.append(d_to)
    elif view == "overdue":                                    # planned/assigned work past its planned date, not finished
        q += (" WHERE j.status IN ('Assigned','InProgress','Paused','Rework','Hold')"
              " AND j.planned_date IS NOT NULL AND j.planned_date != '' AND j.planned_date < ?")
        args = [d]
    elif view == "duetoday":
        q += f" WHERE j.status IN {ACTIVE} AND j.due_date = ?"
        args = [d]
    elif view == "onhold":                                     # held jobs → planner follows up + sets due date
        q += " WHERE j.status='Hold'"
    elif view == "rework":
        q += " WHERE j.status='Rework'"
    elif view == "assigned":
        q += " WHERE j.status IN ('Assigned','InProgress','Paused','ServiceCompleted')"
    elif view == "history":
        # A range, not a day. History used to be "the ten days ending on this date",
        # which is a strange thing to ask of an archive: you either want a period you
        # chose yourself, or everything about one machine. Both now say so directly.
        # No d_from given still means the old ten-day window, so an old link still works.
        from datetime import date as _d, timedelta as _td
        h0 = (d_from or (_d.fromisoformat(d) - _td(days=9)).isoformat()) + " 00:00:00"
        h1 = (d_to or d) + " 23:59:59"
        q += " WHERE j.status IN ('Done','Rejected') AND j.created_at >= ? AND j.created_at <= ?"
        args = [h0, h1]
    else:
        q += " WHERE 1=0"      # unknown/empty view → return nothing, never a full job dump
    if q_text:
        q += (" AND" if "WHERE" in q else " WHERE") + \
             " (j.jobid LIKE ? OR j.descr LIKE ? OR m.code LIKE ? OR m.name LIKE ?)"
        args += [f"%{q_text}%"] * 4
    _vf = _view_factory(u, fac)
    if _vf is not None:
        q += (" AND" if "WHERE" in q else " WHERE") + " (COALESCE(m.factory_id,j.factory_id)=? OR COALESCE(m.factory_id,j.factory_id) IS NULL)"
        args.append(_vf)
    if view == "approved":
        q += " ORDER BY COALESCE(NULLIF(j.approved_at,''), j.done_at, j.created_at) DESC, j.id DESC"
    else:
        q += " ORDER BY j.priority DESC, j.planned_start IS NULL, j.planned_start, j.id DESC"
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute(q, args)]
        names = user_names(c)
        for r in rows:
            r["helper_names"] = ", ".join(
                names.get(h, "") for h in (r["helpers"] or "").split(",") if h)
            r["creator_name"] = names.get(str(r["created_by"]), "")
            r["requester_name"] = names.get(str(r.get("requester_id")), "") or r["creator_name"]
        ids = [r["id"] for r in rows]
        evmap = {}
        if ids:
            _qs = ",".join("?" * len(ids))
            for e in c.execute(f"SELECT job_id,status,created_at FROM job_events WHERE job_id IN ({_qs}) ORDER BY job_id,id", ids):
                evmap.setdefault(e["job_id"], []).append((e["status"], e["created_at"]))
        _nowts = now()
        for r in rows:
            evs = evmap.get(r["id"], [])
            r["hold_min"] = hold_minutes(evs, _nowts) if evs else 0
            r["to_hold_min"] = mins_between(r.get("started_at"),
                                            next((ts for st, ts in evs if st == "Hold"), None))
            r["finish_min"] = mins_between(r.get("started_at"), r.get("done_at"))
            r["total_min"] = mins_between(r.get("created_at"), r.get("done_at"))
        open_seg = c.execute(
            "SELECT id, job_id, seg_type, activity, start FROM timelogs WHERE tech=? AND end IS NULL",
            (u["id"],)).fetchone()
    # The KPI-exclude flag is the admin's own bookkeeping. The job DETAIL endpoint has
    # always stripped it for every other role; this list was selecting j.* and shipping
    # it to everyone, so the rule only half held. It holds properly now — and because an
    # admin does get it, their own screens can mark an excluded job wherever it appears,
    # which is the point: a job quietly missing from a KPI should be quietly obvious to
    # the person who took it out.
    if u["role"] != "admin":
        for r in rows:
            for _k in ("kpi_exclude", "kpi_exclude_note", "kpi_exclude_by", "kpi_exclude_at"):
                r.pop(_k, None)
    else:
        for r in rows:
            r["kpi_exclude"] = 1 if r.get("kpi_exclude") else 0
    # b417: a Central Electrical planner sees electrical / other / unknown work and the
    # PM jobs that carry an electrical point — not the plant's mechanical work
    try:
        from .elec import scope_of           # b421: CE planner / plant planner scopes
        with closing(db()) as _c2:
            _ok = scope_of(_c2, u)
            if _ok:
                rows = [r for r in rows if _ok(r)]
    except Exception:
        pass
    return {"jobs": rows, "open_segment": dict(open_seg) if open_seg else None}


@router.get("/jobs/export")
async def export_jobs(req: Request, d: str = "", q_text: str = "", jtype: str = "", sbucket: str = "",
                      fac: str = "", ids: str = ""):
    """The on-screen All-jobs table as a real .xlsx, factory-scoped.

    `ids` is the list the table is actually showing. It exists because the older
    parameters could not describe the screen: they carried the search box, the Type
    chip and the Status chip, but not the Show chip (Overdue, To accept, Carried
    forward…) and not the From / To dates — so filtering to Overdue and pressing
    Export quietly handed back the whole month. The page now sends the rows it has,
    and the sheet is those rows and nothing else. The plant scope is still applied on
    top, so a crafted id list can never pull another plant's work."""
    import io
    from datetime import date as _d, timedelta as _td
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from fastapi.responses import StreamingResponse
    u = user_from(req)
    d = d or today()
    fac = _view_factory(u, fac)
    start = (_d.fromisoformat(d) - _td(days=30)).isoformat()
    idl = [int(x) for x in (ids or "").split(",") if x.strip().isdigit()][:4000]
    q = """SELECT j.*, m.code mcode, m.name mname, COALESCE(m.criticality,'') mcrit, uu.name lead_name
           FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
           LEFT JOIN users uu ON uu.id=j.lead_tech
           WHERE """
    if idl:
        q += "j.id IN (%s)" % ",".join("?" * len(idl))
        args = list(idl)
    else:
        q += ("""COALESCE(NULLIF(j.planned_date,''), date(j.created_at)) BETWEEN ? AND ?
             AND j.status NOT IN ('Cancelled','Rejected')""")
        args = [start, d]
    if fac is not None:
        q += " AND (COALESCE(m.factory_id,j.factory_id)=? OR COALESCE(m.factory_id,j.factory_id) IS NULL)"
        args.append(fac)
    # With an explicit id list the screen has already applied every filter; re-applying
    # them here could only remove rows the planner can see, which is the bug in reverse.
    if not idl:
        if q_text:
            q += " AND (j.jobid LIKE ? OR j.descr LIKE ? OR m.code LIKE ? OR m.name LIKE ?)"
            args += [f"%{q_text}%"] * 4
        if jtype:
            q += " AND UPPER(j.jobtype)=?"
            args.append(jtype.upper())
    q += " ORDER BY COALESCE(NULLIF(j.planned_date,''), date(j.created_at)) DESC, j.priority DESC, j.id DESC"
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute(q, args)]
        names = user_names(c)
        # the department that reported it; for a job raised before the code existed,
        # the department its reporter is in today — good enough to sort a sheet by
        udept = {str(r["id"]): dept_code(r["department"] or "") for r in
                 c.execute("SELECT id, COALESCE(department,'') department FROM users")}
    if not idl and sbucket in ("open", "prog", "done"):
        prog = {"InProgress", "Paused", "ServiceCompleted"}
        bkt = lambda s: "done" if s == "Done" else ("prog" if s in prog else "open")
        rows = [r for r in rows if bkt(r["status"]) == sbucket]

    def hn(ids):
        return ", ".join(names.get(str(x), "") for x in (ids or "").split(",") if x)

    STL = {"Reported": "Reported", "WaitingApproval": "Waiting approval", "WaitingAssignment": "Waiting assignment",
           "Assigned": "Assigned", "InProgress": "In progress", "Paused": "Paused",
           "Rework": "Rework", "Hold": "On hold", "ServiceCompleted": "Service completed", "Done": "Done",
           "Rejected": "Rejected", "Cancelled": "Cancelled", "Planned": "Planned"}

    headers = [("No", 6), ("Created", 18), ("Job ID", 16), ("Type", 7), ("Machine", 26), ("Description", 30),
               ("Planned date", 13), ("Asset", 12), ("Class", 7), ("Priority", 22),
               ("Job status", 14), ("Waiting on", 18), ("Status", 16), ("Progress %", 10),
               ("Start", 7), ("End", 7), ("Due", 12), ("Started", 18), ("Done", 18), ("Approved", 18),
               ("Lead tech", 14), ("Helpers", 20), ("Requester", 14), ("Reporting dept", 14),
               ("Source", 10), ("Fault category", 16),
               ("Fault component", 16), ("Maint action", 16), ("Problem", 30), ("Root cause", 30),
               ("Solution", 30), ("Prod impact", 12), ("Rework", 7), ("Pending reason", 22)]
    wb = Workbook()
    ws = wb.active
    ws.title = "Jobs"
    hf = PatternFill("solid", fgColor="1E293B")
    hfont = Font(color="FFFFFF", bold=True, size=10)
    thin = Border(*(Side(style="thin", color="D0D5DD"),) * 4)
    for i, (h, w) in enumerate(headers, 1):
        cell = ws.cell(1, i, h)
        cell.fill = hf
        cell.font = hfont
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin
        ws.column_dimensions[get_column_letter(i)].width = w
    for ri, r in enumerate(rows, 2):
        vals = [ri - 1, r.get("created_at"), r.get("jobid"), r.get("jobtype"), r.get("mname"), r.get("descr"),
                r.get("planned_date"), r.get("mcode"), r.get("mcrit") or "", {1: "Normal / ปกติ", 2: "Urgent / เร่ง", 3: "Critical! (machine stopped) / ด่วน! (เครื่องหยุด)"}.get(r.get("priority"), r.get("priority")),
                r.get("stage1") or stage_for(r.get("status"), r.get("lead_tech"), r.get("rework_count"), r.get("helpers"), r.get("due_date"))[0],
                r.get("stage2") or stage_for(r.get("status"), r.get("lead_tech"), r.get("rework_count"), r.get("helpers"), r.get("due_date"))[1],
                STL.get(r.get("status"), r.get("status")),
                r.get("progress"), r.get("planned_start"), r.get("planned_end"), r.get("due_date"),
                r.get("started_at"), r.get("done_at"), r.get("approved_at"), r.get("lead_name"), hn(r.get("helpers")),
                names.get(str(r.get("requester_id")), ""),
                (r.get("req_dept") or udept.get(str(r.get("requester_id")))
                 or udept.get(str(r.get("created_by"))) or ""),
                r.get("jobsource"), r.get("fault_category"),
                r.get("fault_component"), r.get("maint_action"), r.get("problem"), r.get("root_cause"),
                r.get("solution"), r.get("production_impact"), r.get("rework_count"), r.get("pending_reason")]
        for ci, v in enumerate(vals, 1):
            cc = ws.cell(ri, ci, v)
            cc.border = thin
            cc.alignment = Alignment(vertical="top", wrap_text=ci in (6, 25, 26, 27))
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="jobs_{d}.xlsx"'})


@router.get("/pulse")
async def pulse(req: Request):
    """The same token the live connection sends, for a screen that cannot hold one open.

    A phone falls back to asking for this on a timer when the connection drops — an old
    browser, a proxy that buffers, a screen the OS froze. Both paths read one function,
    so the fallback can never disagree with the live feed about what changed.
    """
    user_from(req)
    from .live import token, wait_change
    v = (req.query_params.get("v") or "").strip()
    # ?wait=1 with the token the screen already has: hold the answer back until the
    # token actually moves, then return at once. An update lands in about a second
    # without a single wasted request in between.
    if v and req.query_params.get("wait"):
        try:
            secs = float(req.query_params.get("wait") or 20)
        except ValueError:
            secs = 20.0
        return {"v": await wait_change(v, max(1.0, min(secs, 55.0)))}
    return {"v": token()}


@router.get("/jobs/counts")
async def job_counts(req: Request, d: str = "", fac: str = ""):
    """Counts for the planner tab badges — the same factory scope as the list itself."""
    u = user_from(req)
    fac = _view_factory(u, fac)
    d = d or today()
    from datetime import date as _d, timedelta as _td
    rstart = (_d.fromisoformat(d) - _td(days=9)).isoformat() + " 00:00:00"
    with closing(db()) as c:
        from .elec import scope_of        # b417/b421: the CE planner's and plant planner's scopes
        _cok = scope_of(c, u)

        def cnt(where, a):
            w, aa = where, list(a)
            if fac is not None:
                w += " AND (COALESCE(m.factory_id,j.factory_id)=? OR COALESCE(m.factory_id,j.factory_id) IS NULL)"
                aa.append(fac)
            if _cok:
                return sum(1 for r in c.execute("SELECT j.* FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id "
                                                "WHERE " + w, aa).fetchall() if _cok(dict(r)))
            return c.execute("SELECT COUNT(*) n FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id "
                             "WHERE " + w, aa).fetchone()["n"]
        return {
            "recent": cnt("j.status IN ('Reported','WaitingApproval','WaitingAssignment','Hold','Rework') AND j.created_at >= ?", [rstart]),
            "overdue": cnt("j.status IN ('Assigned','InProgress','Paused','Rework','Hold') AND j.planned_date IS NOT NULL AND j.planned_date != '' AND j.planned_date < ?", [d]),
            "assigned": cnt("j.status IN ('Assigned','InProgress','Paused','ServiceCompleted')", []),
            "history": cnt("j.status IN ('Done','Rejected') AND j.created_at >= ?", [rstart]),
            "cmplan": cnt("j.lead_tech IS NULL AND j.status IN ('Reported','WaitingApproval','WaitingAssignment')", []),
            # the same split as view=toapprove, so the badge cannot promise a job the
            # list will not show: PM for a planner, own CM/BD for anybody who raised one
            "toapprove": cnt(
                "j.status='ServiceCompleted' AND (UPPER(j.jobtype)='PM'"
                " OR (UPPER(j.jobtype) IN ('CM','BD','IMP')"
                " AND (j.created_by=? OR j.requester_id=? OR " + SELFDONE_SQL + ")))"
                if u["role"] in ("planner", "admin") else
                "j.status='ServiceCompleted' AND UPPER(j.jobtype) IN ('CM','BD','IMP')"
                " AND (j.created_by=? OR j.requester_id=?)"
                + ("" if u["role"] == "manager" else " AND NOT " + SELFDONE_SQL),
                [u["id"], u["id"]]),
            "onhold": cnt("j.status='Hold'", []),
            "rework": cnt("j.status='Rework'", []),
        }


@router.post("/jobs")
async def create_job(req: Request):
    u = user_from(req)
    b = await req.json()
    jt = b.get("jobtype", "CM")
    # A BREAKDOWN IS A CLAIM ABOUT A REGISTERED MACHINE, so one cannot be raised
    # without an asset. BD is what drives the whole downtime side of the system —
    # availability, MTBF, MTTR — and every one of those divides by an asset's required
    # time. A BD on a place has no such time, so its hours landed in the plant total
    # with nothing to carry them, and the dashboard and the downtime panel disagreed by
    # exactly that amount. Work on something not in the register is corrective work: it
    # is stored as CM and numbered from its department like any other. Enforced here
    # and not only on the phone, because a cached page from an older build must not be
    # able to create one either.
    if jt == "BD" and not b.get("machine_id"):
        jt = "CM"
        b["production_impact"] = "CanRun"
    # What the reporter typed when nothing in the register matched. Only meaningful
    # without a machine: once an asset is picked, the asset IS the answer and a leftover
    # line of text beside it would be a second, contradictory one.
    asset_text = "" if b.get("machine_id") else (b.get("asset_text") or "").strip()[:80]

    # b393 — A REPAIR CANNOT BE FILED WITHOUT ITS SYMPTOM. CM and BD need a symptom from
    # the list, and "Other" needs the description that says what it is. Checked here as
    # well as on the form, so no screen — an old cached one included — can file a job
    # nobody can act on. PM / IMP / PRJ are not faults and are not asked.
    if jt in ("CM", "BD"):
        _miss = []
        if not b.get("machine_id") and not asset_text and not (b.get("report_name") or "").strip():
            _miss.append("เครื่องจักร หรือสถานที่ / machine or place")
        _pt = (b.get("problem_type") or "").strip()
        with closing(db()) as _c:
            _hit = _c.execute("SELECT category FROM problem_types WHERE name=? LIMIT 1", (_pt,)).fetchone() if _pt else None
        if not _hit:
            _miss.append("อาการที่พบ (เลือกจากรายการ) / symptom from the list")
        elif str(_hit["category"] or "").lower() == "other" and not (b.get("descr") or "").strip():
            _miss.append("รายละเอียดปัญหา (เลือก อื่น ๆ แล้ว) / description — “Other” was chosen")
        if _miss:
            raise HTTPException(400, "กรุณากรอกให้ครบ / Please fill in: " + " · ".join(_miss))

    with closing(db()) as c:
        # One tap, one work order. A phone on a slow link shows nothing while the save
        # is in flight, so the Save button gets pressed again — and a repeated request
        # used to become a second, third, fourth identical job, each with its own push
        # to every technician. An identical report gives back the job that already
        # exists, so the phone shows the same job number and nobody is notified twice.
        #
        # asset_text is part of what makes a report identical. Without it every
        # unregistered job matched every other unregistered job with the same
        # description — machine_id NULL on both sides of the COALESCE — so the second
        # pipe someone reported that morning came back as the first one's job number.
        cutoff = (datetime.now() - timedelta(seconds=DUP_WINDOW)).strftime("%Y-%m-%d %H:%M:%S")
        dup = c.execute(f"""SELECT id FROM jobs
                            WHERE COALESCE(machine_id,0)=COALESCE(?,0) AND descr=? AND created_by=?
                              AND COALESCE(asset_text,'')=?
                              AND (status IN ({','.join('?' * len(OPEN_STATUSES))})
                                   OR created_at >= ?)
                            ORDER BY id DESC LIMIT 1""",
                        (b.get("machine_id"), b.get("descr", ""), u["id"], asset_text,
                         *OPEN_STATUSES, cutoff)).fetchone()
        if dup:
            return job_row(c, dup["id"])
        # ── Which department is reporting this ───────────────────────────────────
        # Taken from the person raising it and overridable on the form, because a
        # production operator who finds a fault in the warehouse should be able to say
        # so. It is written onto the JOB rather than read back off the user later:
        # people move department, and a job's history must not move with them. From
        # 1 Oct it is also what a CM job's number is built from.
        _dept = dept_code(b.get("dept") or "") or dept_code(u.get("department") or "")
        _jfac = _job_factory(c, b.get("machine_id"), u)   # numbered inside this plant
        jobid = next_jobid(c, jt, _dept, factory_id=_jfac)
        if u["role"] == "operator":
            # Every report goes straight into the planner's queue. Cost approval used to
            # park it at WaitingApproval first; that step was dropped — the planner
            # decides what the work costs when they plan it, not before they see it.
            status = "Reported"
            source = "OperatorReport"
        elif u["role"] == "manager" and jt == "PM":
            # PM comes out of the PM programme, which a manager cannot see or edit.
            # Refused here as well as hidden on screen, so the rule holds whatever
            # sends the request.
            raise HTTPException(403, "งาน PM ต้องออกจากแผน PM ของผู้วางแผน /"
                                     " PM work is raised from the planner's PM programme")
        elif u["role"] in ("planner", "admin", "manager"):
            # Planner-created PM/CM/IMP work enters the same planning queue as reports.
            # A plant manager may raise one too — they walk the floor and see things —
            # but it lands in the same queue for the planner to date and crew, and the
            # source says whose it was. Everything else on a job stays read-only to them.
            status = "Reported"
            source = "Manager" if u["role"] == "manager" else "Planner"
        else:
            raise HTTPException(403, "only operator, planner or manager may create jobs")
        # The symptom the operator picked carries the trade it usually turns out to be,
        # so the job starts life with a category the planner can route on. It is a first
        # guess from someone who has not opened the machine — the technician replaces it
        # with what it really was when they press Finish.
        ptype = (b.get("problem_type") or "").strip()[:80]
        guess = ""
        if ptype:
            try:
                hit = c.execute("SELECT category FROM problem_types WHERE name=? LIMIT 1",
                                (ptype,)).fetchone()
                guess = (hit["category"] if hit else "") or ""
            except Exception:
                guess = ""
        new_id = c.insert_id("""INSERT INTO jobs(jobid,jobtype,machine_id,descr,report_name,priority,status,
            planned_date,planned_start,planned_end,lead_tech,due_date,jobsource,
            production_impact,requester_id,created_by,created_at,problem_type,fault_category,
            asset_text,req_dept,factory_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (jobid, jt, b.get("machine_id"), b.get("descr", ""), b.get("report_name", ""),
             b.get("priority", 1), status,
             b.get("planned_date"), b.get("planned_start"), b.get("planned_end"),
             b.get("lead_tech"), b.get("due_date"), source,
             b.get("production_impact", ""), (b.get("requester_id") or u["id"]), u["id"], now(),
             ptype, guess, asset_text, _dept, _jfac))
        set_stage(c, new_id)
        c.commit()
        row = job_row(c, new_id)
        fac = _job_factory(c, row["machine_id"], u)   # the plant this machine stands in
        # …written onto the job, not just used for the notification. Without it a job
        # raised with no machine has no plant, and every list treats that as all plants.
        c.execute("UPDATE jobs SET factory_id=? WHERE id=?", (fac, new_id))
        log_status(c, new_id, status, u["id"])
        c.commit()
        if jt == "BD" or int(b.get("priority", 1)) == 3:
            notify_users(role_ids(c, "technician", "planner", "manager", "engcenter", factory_id=fac),
                         "🔴 Breakdown!",
                         f"{row['jobid']} {row['mcode'] or ''}: {row['descr'][:80]}",
                         f"/?job={row['id']}")
            from .chat import post_system
            post_system(c, "system",
                        f"🔴 {row['jobid']} {row['mcode'] or ''}: {row['descr'][:120]}",
                        job_id=row["id"], author=u["id"])
            c.commit()
        elif u["role"] == "operator":
            notify_users(role_ids(c, "technician", "planner", "manager", "engcenter", factory_id=fac),
                         "งานแจ้งซ่อมใหม่ / New job request",
                         f"{row['jobid']} {row['mcode'] or ''}: {row['descr'][:80]}",
                         f"/?job={row['id']}")
        return row


@router.delete("/jobs/{jid}")
async def delete_job(jid: int, req: Request):
    """Erase a job and everything hanging off it.

    Cancelling is the normal way to retire a job — it keeps the record and drops out
    of every list and count. This is for clearing a mistake, a duplicate report above
    all, where even a cancelled row is just clutter. Admin only, and there is no undo.
    """
    u = user_from(req)
    if u["role"] != "admin":
        raise HTTPException(403, "ลบงานได้เฉพาะผู้ดูแลระบบ / only an admin may delete a job")
    with closing(db()) as c:
        row = _job_in_factory(c, jid, u)
        for tbl in ("timelogs", "job_events", "messages", "signoffs",
                    "activities", "part_moves", "requisitions"):
            try:
                c.execute(f"DELETE FROM {tbl} WHERE job_id=?", (jid,))
            except Exception:
                pass                       # a table this build does not have
        c.execute("DELETE FROM jobs WHERE id=?", (jid,))
        c.commit()
    return {"ok": True, "jobid": row["jobid"]}


@router.patch("/jobs/{jid}")
async def update_job(jid: int, req: Request):
    u = user_from(req)
    b = await req.json()
    allowed = ["status", "planned_date", "planned_start", "planned_end", "lead_tech",
               "helpers", "priority", "progress", "pending_reason", "problem",
               "root_cause", "solution", "descr", "report_name", "machine_id",
               "carryover", "due_date", "requester_id", "approver_id", "problem_type",
               "cleared_worksite", "new_issue_id", "production_impact", "asset_text",
               "req_dept"]
    sets = {k: b[k] for k in allowed if k in b}
    if not sets:
        raise HTTPException(400, "nothing to update")
    if "req_dept" in sets:
        # free text or a code, both land as a code; the JOB NUMBER is never rebuilt from
        # a correction — the number is the record, and it was issued when the job was
        sets["req_dept"] = dept_code(sets["req_dept"] or "")
    if "asset_text" in sets:
        sets["asset_text"] = (sets["asset_text"] or "").strip()[:80]
    # Naming the asset is the answer to the same question as typing it, so the two can
    # never both stand. A planner attaching the real machine to an unregistered job
    # clears the typed line in the same write — otherwise the job sheet carries the
    # asset code and, underneath it, the guess somebody made before it was known.
    if sets.get("machine_id"):
        sets["asset_text"] = ""
    with closing(db()) as c:
        old = _job_in_factory(c, jid, u)
        # a shared phone may name the team member who is actually reporting this
        actor = _acting_tech(c, old, u, b.get("as_tech")) if u["role"] == "technician" else u["id"]
        st = sets.get("status")
        planning_fields = {"planned_date", "planned_start", "planned_end", "lead_tech",
                           "helpers", "priority", "due_date"}
        # The reporter may correct the priority they chose, like any other detail of
        # their report, until the work starts (checked with the rest of the reporter's
        # rules below). Every other planning field stays the planner's.
        _is_reporter = u["role"] not in ("planner", "admin", "technician")
        _plan_hit = planning_fields.intersection(sets) - ({"priority"} if _is_reporter else set())
        if _plan_hit and u["role"] not in ("planner", "admin"):
            raise HTTPException(403, "only planner may plan or assign work")
        if "priority" in sets:
            try:
                sets["priority"] = max(1, min(3, int(sets["priority"])))
            except (TypeError, ValueError):
                raise HTTPException(400, "bad priority")
        # a deadline before the day the work is planned for is not a deadline
        _pd = (sets.get("planned_date", old["planned_date"]) or "")[:10]
        _dd = (sets.get("due_date", old["due_date"]) or "")[:10]
        if _pd and _dd and _dd < _pd:
            raise HTTPException(400, "กำหนดเสร็จต้องไม่ก่อนวันที่วางแผน /"
                                     " the due date cannot be before the planned date")
        # a planned day changed from the job screen follows the same rules as a drag
        if "planned_date" in sets and (sets["planned_date"] or "")[:10] != (old["planned_date"] or "")[:10]:
            from .pm import move_refusal, log_move, _fac as _pmfac
            _why = move_refusal(c, _pmfac(u), old, sets["planned_date"] or "")
            if _why:
                raise HTTPException(409, _why)
            log_move(c, old, (sets["planned_date"] or "")[:10], u["id"])
        # Helpers only. A *lead* with no login is no longer lost work: the technician
        # list carries every job whose lead nobody can sign in as (the `_pool` clause in
        # the job list), which is what lets the planner hand a resigned technician's work
        # to another technician by name before the day's teams are built. Helpers get no
        # such fallback, so booking one who has no phone is still refused.
        if "helpers" in sets:
            orphan = _no_phone(c, str(sets.get("helpers", old["helpers"]) or "").split(","))
            if orphan:
                raise HTTPException(409, "ยังไม่มีโทรศัพท์สำหรับ " + ", ".join(orphan) +
                                    " — ใส่เข้าทีมในหน้ามอบหมายงานก่อน / no phone yet for "
                                    + ", ".join(orphan) + " — put them on a team first")
        # The reporter is also the approver, so handing the job to an operator name that
        # no login can sign in as would leave it waiting for an approval nobody can give.
        if "requester_id" in sets and (sets["requester_id"] or None) != (old["requester_id"] or None):
            orphan = _no_phone(c, [sets["requester_id"]])
            if orphan:
                raise HTTPException(409, "ยังไม่มีโทรศัพท์สำหรับ " + ", ".join(orphan) +
                                    " — ผูกชื่อนี้กับบัญชีเข้าระบบก่อน มิฉะนั้นจะไม่มีใครตรวจรับงานได้ /"
                                    " no phone yet for " + ", ".join(orphan) +
                                    " — link that name to a login account first, or nobody"
                                    " can approve the finished work")
        if st:
            transitions = {
                "planner": {"Assigned": ("Reported", "Hold", "Rework"),
                            "Cancelled": CANCELLABLE},
                "admin": {"Assigned": ("Reported", "Hold", "Rework"),
                          "Cancelled": CANCELLABLE},
                "technician": {"Hold": ("InProgress", "Paused"),
                               "ServiceCompleted": ("InProgress", "Paused")},
            }
            valid_from = transitions.get(u["role"], {}).get(st)
            if not valid_from or old["status"] not in valid_from:
                raise HTTPException(403, f"cannot change {old['status']} to {st}")
            if u["role"] == "technician" and not _assigned_to(old, _tech_ids(c, u)):
                raise HTTPException(403, "job is not assigned to you")
        op_fields = {"report_name", "descr", "machine_id", "requester_id",
                     "production_impact", "problem_type", "asset_text", "req_dept", "priority"}
        op_edit = False
        # Who may correct a report: the person the job names as ผู้แจ้ง / reporter,
        # whatever their role. This used to read `u["role"] == "operator"` and test
        # created_by alone, so a manager or engcenter user who raised a job could not fix
        # their own typo — the endpoint refused them outright, and they had to ask the
        # planner — and on a job raised BY one person FOR another it was the wrong one of
        # the two who could edit it. requester_id first, then created_by: the same pair
        # /signoff accepts on, so the person who will sign for it is the person who can
        # correct it.
        if u["role"] not in ("planner", "admin", "technician"):
            _mine = _tech_ids(c, u)
            if not ((old["requester_id"] in _mine) or (old["created_by"] in _mine)):
                raise HTTPException(403, "เฉพาะผู้แจ้งเท่านั้นที่แก้ไขรายงานนี้ได้ /"
                                         " only the person who reported this job may correct it")
            if not set(sets).issubset(op_fields):
                raise HTTPException(403, "ผู้แจ้งแก้ไขได้เฉพาะรายละเอียดที่ตนแจ้ง /"
                                         " a reporter may change only the details of the report")
            # A report can be corrected until the work STARTS — also after a crew has
            # been given it, as long as nobody has pressed Start (the crew is told).
            # From In progress on, the description is what they are working from and,
            # once finished, a record: nothing changes.
            _started = bool(old["started_at"]) or c.execute(
                "SELECT 1 FROM timelogs WHERE job_id=? LIMIT 1", (jid,)).fetchone() is not None
            if _started or old["status"] not in ("Reported", "WaitingAssignment",
                                                 "WaitingApproval", "Assigned"):
                raise HTTPException(409, "งานนี้เริ่มทำแล้ว — แก้ไขรายงานไม่ได้ /"
                                         " work on this job has started — the report"
                                         " cannot be changed")
            op_edit = True
        if old["status"] == "WaitingApproval" and (st == "Assigned" or (_plan_hit if _is_reporter else planning_fields.intersection(sets))):
            raise HTTPException(409, "approve this request before planning it")
        c.execute(f"UPDATE jobs SET {','.join(k + '=?' for k in sets)} WHERE id=?",
                  (*sets.values(), jid))
        # A corrected report is, for the shop floor, the report as of now: the planner
        # picking work off the queue should see the time the details were last right,
        # not when the first wrong version was typed. The original time is written into
        # the job's own history first, so nothing is quietly lost.
        if op_edit:
            from .chat import log_job_event as _lje
            _lje(c, jid, u["id"], f"✎ แก้ไขรายงาน / report corrected by {u['name']}"
                                  f" (เดิมแจ้งเมื่อ / first reported {old['created_at'] or '—'})")
            if "priority" in sets and int(sets["priority"]) != int(old["priority"] or 2):
                _pn = {1: "ต่ำ/Low", 2: "กลาง/Medium", 3: "สูง/High"}
                _lje(c, jid, u["id"], f"⚑ ความสำคัญ / priority: {_pn.get(int(old['priority'] or 2))}"
                                      f" → {_pn.get(int(sets['priority']))} (โดย {u['name']})")
            # Still in the queue: the corrected report is the report as of now. Once a
            # crew has it, the times already on the job (assigned, planned) stay as
            # they are — only the history records the correction.
            if not old["lead_tech"]:
                c.execute("UPDATE jobs SET created_at=? WHERE id=?", (now(), jid))
        if st == "Rework":
            c.execute("UPDATE jobs SET rework_count=rework_count+1 WHERE id=?", (jid,))
        if st in ("ServiceCompleted", "Done"):
            c.execute("UPDATE jobs SET done_at=? WHERE id=? AND done_at IS NULL", (now(), jid))
        if st == "Done":
            c.execute("UPDATE jobs SET approved_at=? WHERE id=? AND approved_at IS NULL", (now(), jid))
        # Cancelling a job that a technician is standing on top of has to stop their
        # timer too. Left running, it belongs to a job that is gone from every screen,
        # so nothing can ever close it and that technician can start no other work.
        if st == "Cancelled":
            c.execute("UPDATE timelogs SET end=?, pause_reason=? WHERE job_id=? AND end IS NULL",
                      (now(), "job cancelled", jid))
        # Hold is the shop floor saying the job is waiting on somebody else — a spare
        # part, a supplier, a decision. Nobody's hands are on the machine, so the timer
        # has to stop with it. Left running it booked the whole wait as repair labour:
        # a job held on Friday afternoon came back on Monday with two days of "work"
        # against a technician who was not there, and the work-time panel drifted by a
        # day at a time with nothing on any screen to explain it. The wait is not lost —
        # it stays in the job's history and, on a breakdown, it is still engineering's
        # downtime, which is where a supplier delay belongs. Pressing Start again opens
        # a fresh segment, so resuming needs nothing here.
        #
        # Paused already closes its own timer inside seg_stop (the phone's Stop button),
        # but a Paused arriving through this endpoint gets the same treatment rather
        # than depending on which screen it came from.
        if st in ("Hold", "Paused"):
            _rsn = str(sets.get("pending_reason") or old["pending_reason"] or "").strip()
            _lbl = ("on hold" if st == "Hold" else "paused") + (f" — {_rsn}" if _rsn else "")
            c.execute("UPDATE timelogs SET end=?, pause_reason=? WHERE job_id=? AND end IS NULL",
                      (now(), _lbl[:120], jid))
        # Once the work has started the crew is fixed. A job's crew decides who may
        # touch it and who is credited for it, so moving it under the people doing the
        # work rewrites both — and the technician holding the running timer can find
        # themselves off their own job. Admin only, because somebody has to be able to
        # correct a genuine mistake.
        if old["started_at"] and u["role"] != "admin":
            for _f in ("lead_tech", "helpers"):
                if _f in sets and (sets[_f] or None) != (old[_f] or None):
                    raise HTTPException(409,
                        "งานนี้เริ่มทำแล้ว — เปลี่ยนช่างไม่ได้ /"
                        " this job has started; its crew can no longer be changed")
        # who reports a job and who leads it decide who may finish and accept it, so a
        # change of either belongs in the job's own history — not only in the database
        # A change of lead technician IS the move this column records. Stamped here
        # rather than in the Move-job route alone, because the same rewrite arrives from
        # the assign board, from a team save and from the delete-and-reassign — and a
        # record of where work went is worth nothing if it depends on which screen was
        # used. owner1 is filled in too if this job never had a Start to stamp it.
        if "lead_tech" in sets and (sets["lead_tech"] or None) != (old["lead_tech"] or None):
            stamp_owner1(c, jid, old["lead_tech"])
            stamp_owner2(c, jid, sets["lead_tech"])
        for fld, label in (("requester_id", "ผู้แจ้ง / reporter"),
                           ("lead_tech", "ช่างหลัก / lead technician")):
            if fld in sets and (sets[fld] or None) != (old[fld] or None):
                nm = user_names(c)
                frm = nm.get(str(old[fld]), "—") if old[fld] else "—"
                to = nm.get(str(sets[fld]), "—") if sets[fld] else "—"
                from .chat import log_job_event as _lje
                _lje(c, jid, u["id"], f"⇄ {label}: {frm} → {to} (โดย {u['name']})")
        set_stage(c, jid)
        c.commit()
        row = job_row(c, jid)
        if st:
            from .chat import log_job_event
            who = user_names(c).get(str(actor), u["name"])
            log_job_event(c, jid, actor, f"⚙️ สถานะ → {st} (โดย {who})")
            log_status(c, jid, st, actor)
            c.commit()
        _fac = _job_factory(c, row["machine_id"], u)       # the plant this machine stands in
        techs = [t for t in [row["lead_tech"],
                             *[int(h) for h in (row["helpers"] or "").split(",") if h]] if t]
        if st == "Assigned":
            notify_users(techs, "📋 งานใหม่ถึงคุณ",
                         f"{row['jobid']} {row['mcode'] or ''} {row['planned_start'] or ''}-{row['planned_end'] or ''}")
        elif st == "Rework":
            notify_users(techs, "↩ งานถูกส่งกลับแก้ไข", f"{row['jobid']} {row['mcode'] or ''}")
        elif st == "Hold":
            notify_users(role_ids(c, "planner", "manager", "engcenter", factory_id=_fac), "⏸ งานถูกพัก / Job on hold",
                         f"{row['jobid']} {row['mcode'] or ''}: {row['pending_reason'] or ''}", f"/?job={jid}")
        elif st == "ServiceCompleted":
            notify_users(role_ids(c, "operator", factory_id=_fac), "✅ งานเสร็จ · รออนุมัติ / Completed — please approve",
                         f"{row['jobid']} {row['mcode'] or ''} · {row['lead_name'] or ''}", f"/?job={jid}")
        elif st in ("Done", "Rejected"):
            notify_users(techs, f"ผลตรวจงาน: {st}", f"{row['jobid']} {row['mcode'] or ''}")
        if op_edit:
            _raised = int(sets.get("priority") or 0) == 3 and int(old["priority"] or 2) != 3
            if _raised:
                notify_users(role_ids(c, "planner", "manager", "engcenter", factory_id=_fac),
                             "🔴 ความสำคัญสูง / Priority raised to High",
                             f"{row['jobid']} {row['mcode'] or ''}: {(row['descr'] or '')[:80]}", f"/?job={jid}")
            if techs:                       # the crew already has it: tell them it changed
                notify_users(techs, "✎ รายงานถูกแก้ไข / Report updated"
                             + (" · 🔴 High" if _raised else ""),
                             f"{row['jobid']} {row['mcode'] or ''}: {(row['descr'] or '')[:80]}", f"/?job={jid}")
        return row


@router.get("/jobs/{jid}/detail")
async def job_detail(jid: int, req: Request):
    u = user_from(req)
    with closing(db()) as c:
        row = _job_in_factory(c, jid, u)      # another plant's job is not opened here
        names = user_names(c)
        row["helper_names"] = ", ".join(
            names.get(h, "") for h in (row["helpers"] or "").split(",") if h)
        row["team"] = _team_of(c, row)          # the only names the phone may pick from
        row["creator_name"] = names.get(str(row["created_by"]), "")
        row["requester_name"] = names.get(str(row.get("requester_id")), "") or row["creator_name"]
        # where each photo came from, so the screen can badge an uploaded one rather
        # than leaving the reader to assume every picture was taken at the machine
        try:
            from . import photos as _ph
            row["photo_src"] = {k: {"src": v.get("src"), "exif_dt": v.get("exif_dt"),
                                    "device": v.get("device")}
                                for k, v in _ph.meta_for(c, jid).items()}
        except Exception:
            row["photo_src"] = {}
        # which photos THIS user may remove or replace — the screen only offers what the
        # server will allow (see photo_can_remove)
        row["photo_can"] = {k: photo_can_remove(c, u, row, k) for k in PHOTO_KINDS}
        segs = [dict(r) for r in c.execute("""SELECT t.*, u.name tech_name FROM timelogs t
            JOIN users u ON u.id=t.tech WHERE t.job_id=? ORDER BY t.start""", (jid,))]
        # A breakdown carries the plant's downtime split with it: engineering owns the
        # clock from the report to the technician's Stop (and again after a rejection),
        # production owns it from that Stop until the machine is accepted back. Put it
        # on the job itself so the two departments read the same number the dashboard
        # totals, rather than each keeping their own arithmetic.
        if (row.get("jobtype") or "") == "BD":
            try:
                from . import downtime as _dt
                from .kpi import _fac_hours
                evs = [(e["status"], e["created_at"]) for e in c.execute(
                    "SELECT status,created_at FROM job_events WHERE job_id=? ORDER BY id", (jid,))]
                _fac = _job_factory(c, row.get("machine_id"), u)
                hours, _groups = _fac_hours(c, _fac)
                sp = _dt.split(row.get("created_at"), evs, hours, now(),
                               (row.get("done_at"), row.get("approved_at")))
                row["downtime"] = {"eng_min": sp["eng_min"], "prod_min": sp["prod_min"],
                                   "total_min": sp["total_min"],
                                   "hold_min": min(_dt.hold_inside(evs, hours, now()), sp["eng_min"])}
            except Exception:
                row["downtime"] = None
        # A PM job carries its checklist: the items of the sheet this job is actually
        # for, with whatever has been answered. The technician's screen needs it in the
        # same fetch as the job, so a crew on a weak signal in the plant gets one round
        # trip instead of two.
        if (row.get("jobtype") or "") == "PM":
            try:
                from .pm import job_checklist
                row["checklist"] = job_checklist(c, row)
            except Exception:
                row["checklist"] = None
        # Why the work was sent back. A rejection always carries a reason — the operator
        # cannot save one without it — but it was nowhere on this page, so the technician
        # being asked to do the job again could not see what was wrong with it the first
        # time, and the planner saw only "Rework 1×". It is read from the signoff rather
        # than from pending_reason, because that column holds the latest reason of any
        # kind: a Hold after a rejection overwrites the rejection's words.
        if (row.get("status") or "") == "Rework" or row.get("rework_count"):
            try:
                sr = c.execute("SELECT reason, created_at, user_id FROM signoffs"
                               " WHERE job_id=? AND action='reject'"
                               " ORDER BY id DESC LIMIT 1", (jid,)).fetchone()
                if sr:
                    row["reject_reason"] = sr["reason"] or ""
                    row["reject_at"] = sr["created_at"]
                    row["reject_by"] = names.get(str(sr["user_id"]), "")
            except Exception:
                pass
        # The KPI-exclude flag is an admin's own bookkeeping. It never leaves the
        # server for anybody else, so no other role's screen can render it by
        # accident, now or after a future edit to the page.
        if u["role"] == "admin":
            row["kpi_exclude"] = 1 if row.get("kpi_exclude") else 0
            row["kpi_exclude_note"] = row.get("kpi_exclude_note") or ""
        else:
            for _k in ("kpi_exclude", "kpi_exclude_note", "kpi_exclude_by", "kpi_exclude_at"):
                row.pop(_k, None)
    return {"job": row, "segments": segs}


@router.post("/jobs/{jid}/kpi-exclude")
async def job_kpi_exclude(jid: int, req: Request):
    """Keep this job out of the breakdown-downtime KPI, or put it back.

    For a row whose clock is not the machine's real stop: a job left open for days, a
    duplicate report, a test. Nothing about the job changes — its own downtime figures
    are still calculated and still shown on it, and the flag is written nowhere the
    shop floor reads: no job_events row, no activity entry, no notification, and the
    dashboard card says nothing about anything having been left out.

    Admin only. The reason is stored for the admin's own reference.
    """
    u = user_from(req)
    if u["role"] != "admin":
        raise HTTPException(403, "เฉพาะผู้ดูแลระบบ / admin only")
    b = await req.json()
    on = 1 if b.get("exclude") else 0
    note = str(b.get("note") or "")[:200]
    with closing(db()) as c:
        _job_in_factory(c, jid, u)        # 404 unless it is a job this admin may see
        c.execute("UPDATE jobs SET kpi_exclude=?, kpi_exclude_note=?,"
                  " kpi_exclude_by=?, kpi_exclude_at=? WHERE id=?",
                  (on, note if on else "", u["id"] if on else None,
                   now() if on else None, jid))
        c.commit()
    return {"ok": True, "kpi_exclude": on}


PHOTO_KINDS = ("before", "before2", "after", "after2")
NOT_STARTED = ("Reported", "WaitingApproval", "WaitingAssignment", "Assigned", "Released")
WORKING = ("InProgress", "Paused", "Hold", "Rework")


def photo_can_remove(c, u, job, kind):
    """Who may take a photo off a job (or put a different one in its place). All plants.

      · admin — always.
      · BEFORE photos — only the person who REPORTED the job, and only until work
        starts. Once it is in progress the picture is evidence: nobody but admin.
        A technician never removes a before photo.
      · AFTER photos — only a technician on the job (lead or helper, or the phone they
        sign in on) while the work is still going. Once he has finished — completed,
        waiting for acceptance, accepted — they are read-only for him and everyone else.
    """
    if u.get("role") == "admin":
        return True
    st = job.get("status") or ""
    me = set(_tech_ids(c, u))
    if kind in ("before", "before2"):
        started = bool(job.get("started_at")) or st not in NOT_STARTED
        reporter = {job.get("created_by"), job.get("requester_id")} - {None}
        return (not started) and u.get("role") != "technician" and bool(me & reporter)
    if kind in ("after", "after2"):
        crew = {job.get("lead_tech")} | {int(x) for x in str(job.get("helpers") or "").split(",")
                                         if x.strip().isdigit()}
        return u.get("role") == "technician" and st in WORKING and bool(me & crew)
    return True


@router.post("/jobs/{jid}/media")
async def job_media(jid: int, req: Request):
    u = user_from(req)
    b = await req.json()
    kind, data = b.get("kind"), b.get("data", "")
    _signer = None
    if kind in ("sign_tech", "sign_appr", "sign_requester", "sign_inspector") and not b.get("remove"):
        from .signature import resolve as _sres          # b406: "reg:<id>" → the stored signature
        data, _signer = _sres(u, data, b.get("signer"))
    # A signature is a claim about who did the work. The technician's is written by the
    # technician finishing the job; the acceptance one belongs to the operator who
    # reported it and is written by /signoff. A planner may put one in after the fact —
    # paper signed on the floor, entered later — but nobody else may sign for anybody.
    # Acceptance is no longer one role's: a PM is the planner's, a CM/BD the raiser's.
    #
    # This route used to check the ROLE only and leave the per-job rule to /signoff — but
    # nothing sends this route through /signoff. Tapping the ผู้ตรวจรับ / Approver box on
    # the job page posts straight here, so any planner could put a signature in that box
    # for the person who reported the job: the plant signing its own work off. The same
    # per-job rule /signoff applies now applies here, with an admin override that is
    # written into the job's history rather than left silent.
    if kind == "sign_appr":
        with closing(db()) as c:
            _row = _job_in_factory(c, jid, u)
            _jt = str(_row["jobtype"] or "").upper()
            _owner = _row["requester_id"] or _row["created_by"]
            _ok = (u["role"] in ("planner", "admin")) if _jt == "PM" \
                else bool(_owner) and _owner in _tech_ids(c, u)
            if not _ok:
                if u["role"] != "admin":
                    _who = user_names(c).get(str(_owner), "") if _owner else ""
                    raise HTTPException(403,
                        "งาน PM ต้องให้ผู้วางแผนเซ็นตรวจรับ / a PM is signed off by the planner"
                        if _jt == "PM" else
                        f"เฉพาะผู้แจ้ง{(' ' + _who) if _who else ''} เท่านั้นที่เซ็นตรวจรับได้ / "
                        f"only the reporter{(' (' + _who + ')') if _who else ''}"
                        f" may sign the acceptance box")
                from .chat import log_job_event as _lje0
                _lje0(c, jid, u["id"],
                      "⚠ ลายเซ็นตรวจรับกรอกโดยผู้ดูแลระบบ /"
                      f" acceptance signature entered by admin {u['name']}")
                c.commit()
    if kind == "sign_tech" and u["role"] not in ("technician", "planner", "admin"):
        raise HTTPException(403, "only the technician may sign for the work")
    if kind == "sign_inspector" and u["role"] not in ("planner", "admin"):
        raise HTTPException(403, "only a planner may sign the review")
    col = {"before": "img_before", "after": "img_after",
           "before2": "img_before2", "after2": "img_after2",
           "sign_tech": "sign_tech", "sign_appr": "sign_appr",
           "sign_requester": "sign_requester", "sign_inspector": "sign_inspector"}.get(kind)
    if not col:
        raise HTTPException(400, "bad media")
    if kind in PHOTO_KINDS:
        # removing a photo, or putting a new one over it, is the same act: the old one goes
        with closing(db()) as c:
            _jr = dict(_job_in_factory(c, jid, u))
            if (b.get("remove") or _jr.get(col)) and not photo_can_remove(c, u, _jr, kind):
                raise HTTPException(403, "ลบหรือเปลี่ยนรูปนี้ไม่ได้ — รูปก่อนซ่อม ลบได้เฉพาะผู้แจ้งก่อนเริ่มงาน,"
                                         " รูปหลังซ่อม ลบได้เฉพาะช่างระหว่างทำงาน, นอกนั้นเฉพาะผู้ดูแลระบบ /"
                                         " this photo cannot be removed or replaced: a BEFORE photo only by"
                                         " the reporter before work starts, an AFTER photo only by the"
                                         " technician while working; otherwise admin only")
    if b.get("remove"):
        # A wrong photo can be taken back: clear the slot so the add box returns.
        # The file stays on disk only until the slot is filled again (same name).
        with closing(db()) as c:
            c.execute(f"UPDATE jobs SET {col}='' WHERE id=?", (jid,))
            c.commit()
        return {"path": ""}
    if "," not in data:
        raise HTTPException(400, "bad media")
    head, b64 = data.split(",", 1)
    # A browser that cannot decode a picture — an iPhone .HEIC is the usual one — hands
    # back the four characters "data:,". That used to be written out as a ZERO-BYTE file
    # and answered with 200, so the job carried a photo that was not there and nobody
    # was told. An empty picture is not a picture; say so.
    if not b64.strip():
        raise HTTPException(400, "ไฟล์รูปว่างเปล่า — กรุณาถ่ายใหม่ /"
                                 " the picture came through empty — please take it again")
    ext = "png" if "png" in head else "jpg"
    fn = f"{jid}_{kind}.{ext}"
    full = os.path.join(UPLOADS, fn)
    with open(full, "wb") as f:
        f.write(base64.b64decode(b64))
    # ── A photo says where it came from, on the photo ───────────────────────────
    # "Before" and "after" are evidence, and their whole value is that they were taken
    # at those two moments. Nothing recorded whether a picture was taken just now or
    # picked out of the gallery from last month, and the printed sheet said nothing
    # either. The caption is drawn HERE and not in the browser, because a browser stamp
    # can be faked by uploading an already-stamped file, and the clock on it is this
    # server's rather than the phone's.
    if kind in ("before", "after", "before2", "after2"):
        try:
            from . import photos
            src = str(b.get("src") or "upload").lower()
            if src not in ("camera", "capture", "upload"):
                src = "upload"
            exif = b.get("exif") if isinstance(b.get("exif"), dict) else {}
            with closing(db()) as c:
                _j = job_row(c, jid)
            lines, warn = photos.caption(_j, u.get("name") or "", src, exif)
            photos.stamp(full, lines, warn)
        except Exception as _e:
            from .logs import log as _lg
            _lg.warning("[photos] stamping %s failed: %s", fn, _e)
    with closing(db()) as c:
        c.execute(f"UPDATE jobs SET {col}=? WHERE id=?", (f"/uploads/{fn}", jid))
        if kind in ("before", "after", "before2", "after2"):
            try:
                from . import photos
                photos.note(c, jid, kind, str(b.get("src") or "upload").lower(),
                            b.get("exif") if isinstance(b.get("exif"), dict) else {},
                            u["id"], f"/uploads/{fn}")
            except Exception:
                pass
        if _signer:                                       # b406: who actually signed
            from .signature import record as _srec
            _srec(c, jid, kind, u, _signer)
        if kind == "sign_inspector":
            # the review moment, printed on the form beside the planner's signature
            c.execute("UPDATE jobs SET inspected_at=? WHERE id=?", (now(), jid))
        c.commit()
    return {"path": f"/uploads/{fn}"}


@router.post("/jobs/{jid}/ack-due")
async def ack_due(jid: int, req: Request):
    """"Seen it" on a job that has run past its date — good for TODAY only.

    A row that has gone past its due date flashes, and on a busy morning a planner may
    already know about every one of them. This stops the flashing without pretending
    the job is finished: the DAY is stored, not a flag, and the row starts flashing
    again tomorrow unless the work is actually done. A flag would let one click silence
    a job for ever, which is how a warning becomes wallpaper.

    Acknowledging is not a change to the work, so it is not a status transition and it
    leaves no entry in the job's history — but it does record who, so "why is nobody
    chasing this" has an answer.
    """
    u = require_role(req, "planner", "admin", "manager", "engcenter")
    b = await req.json() if await req.body() else {}
    with closing(db()) as c:
        _job_in_factory(c, jid, u)                  # pins it to the reader's plant
        if b.get("clear"):
            c.execute("UPDATE jobs SET due_ack='', due_ack_by=NULL WHERE id=?", (jid,))
            day = ""
        else:
            day = today()
            c.execute("UPDATE jobs SET due_ack=?, due_ack_by=? WHERE id=?", (day, u["id"], jid))
        c.commit()
        _log.info("[jobs] due %s on %s by %s/%s",
                  "acknowledged" if day else "acknowledgement cleared",
                  job_row(c, jid)["jobid"], u.get("username"), u["role"])
    return {"ok": True, "due_ack": day, "due_ack_by_name": u.get("name") or ""}


@router.post("/jobs/{jid}/reissue")
async def job_reissue(jid: int, req: Request):
    u = user_from(req)
    with closing(db()) as c:
        old = job_row(c, jid)
        # the same work, reported by the same department — the number is rebuilt from it
        _dept = (old["req_dept"] if "req_dept" in old.keys() else "") or ""
        _ofac = old["factory_id"] or _job_factory(c, old["machine_id"], u)
        newid = next_jobid(c, old["jobtype"], _dept, factory_id=_ofac)
        new_pk = c.insert_id("""INSERT INTO jobs(jobid,jobtype,machine_id,descr,priority,status,
            jobsource,created_by,created_at,req_dept,factory_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (newid, old["jobtype"], old["machine_id"],
             f"[Re-issue {old['jobid']}] {old['descr']}", old["priority"], "Reported",
             old["jobsource"], u["id"], now(), _dept, _ofac))
        c.execute("UPDATE jobs SET new_issue_id=? WHERE id=?", (newid, jid))
        # a re-issue is the same work at the same place — it inherits the plant, which
        # matters for the one case that has no machine to read it from
        try:
            # by id: a job NUMBER is only unique inside one plant now
            c.execute("UPDATE jobs SET factory_id=? WHERE id=?", (_ofac, new_pk))
        except Exception:
            pass
        c.commit()
    return {"new_jobid": newid}


@router.post("/jobs/plan")
async def plan_jobs(req: Request):
    u = user_from(req)
    if u["role"] not in ("planner", "admin"):
        raise HTTPException(403, "only planner may assign work")
    b = await req.json()
    ids = [int(x) for x in b.get("job_ids", []) if str(x).isdigit()]
    lead = b.get("lead_tech")
    helpers = b.get("helpers") or ""
    pdate = b.get("planned_date")
    due = b.get("due_date") or None
    status = b.get("status", "Assigned")
    if not ids or not lead:
        raise HTTPException(400, "need job_ids and lead_tech")
    if pdate and due and str(due)[:10] < str(pdate)[:10]:
        raise HTTPException(400, "กำหนดเสร็จต้องไม่ก่อนวันที่วางแผน /"
                                 " the due date cannot be before the planned date")
    with closing(db()) as c:
        orphan = _no_phone(c, [lead] + str(helpers).split(","))
        if orphan:
            raise HTTPException(409, "ยังไม่มีโทรศัพท์สำหรับ " + ", ".join(orphan) +
                                " — ใส่เข้าทีมในหน้ามอบหมายงานก่อน / no phone yet for "
                                + ", ".join(orphan) + " — put them on a team first")
        for jid in ids:
            job = _job_in_factory(c, jid, u)
            if job["status"] == "WaitingApproval":
                raise HTTPException(409, "approve every selected request before planning it")
            if job["status"] not in ("Reported", "Assigned", "Hold", "Rework"):
                raise HTTPException(409, f"job {job['jobid']} cannot be planned from {job['status']}")
            if pdate:
                from .pm import move_refusal, _fac as _pmfac
                why = move_refusal(c, _pmfac(u), job, str(pdate)[:10])
                if why:
                    raise HTTPException(409, why)
        qs = ",".join("?" * len(ids))
        c.execute(f"UPDATE jobs SET lead_tech=?, helpers=?, planned_date=?, due_date=?, planned_at=?, status=? WHERE id IN ({qs})",
                  (lead, helpers, pdate, due, now(), status, *ids))
        for _jid in ids:
            log_status(c, _jid, status, u["id"])
            set_stage(c, _jid)          # stage1/stage2 are stored — keep them in step
        c.commit()
    team = [int(lead)] + [int(h) for h in str(helpers).split(",") if str(h).strip().isdigit()]
    notify_users(list(dict.fromkeys(team)), "งานที่วางแผนให้คุณ / Planned jobs",
                 f"{len(ids)} งาน · วันที่ {pdate}", "/")
    return {"ok": True, "count": len(ids), "planned_date": pdate}


def _did_work(c, job):
    """Every id that worked on a job — lead, helpers, anyone who logged time on it — and
    the login each of those people signs in with, so a phone account and the person
    behind it count as the same worker."""
    ids = set()
    if job.get("lead_tech"):
        ids.add(job["lead_tech"])
    ids |= {int(x) for x in str(job.get("helpers") or "").split(",") if x.strip().isdigit()}
    try:
        ids |= {r[0] for r in c.execute("SELECT DISTINCT tech FROM timelogs WHERE job_id=? AND tech IS NOT NULL",
                                        (job["id"],))}
    except Exception:
        pass
    if ids:
        q = ",".join("?" * len(ids))
        ids |= {r[0] for r in c.execute(f"SELECT login_id FROM users WHERE id IN ({q}) AND login_id IS NOT NULL",
                                        list(ids))}
    return ids


@router.post("/jobs/{jid}/signoff")
async def signoff(jid: int, req: Request):
    u = user_from(req)
    b = await req.json()
    action = b.get("action")
    reason = (b.get("reason") or "").strip()
    sig_data = b.get("signature") or ""
    sig_path = ""
    _signer = None
    if b.get("action") == "approve" and sig_data:
        from .signature import resolve as _sres          # b406
        sig_data, _signer = _sres(u, sig_data, b.get("signer"))
    with closing(db()) as c:
        existing = _job_in_factory(c, jid, u)   # also pins this to the signer's factory
        if existing["status"] != "ServiceCompleted":
            # "Already accepted" is not the same refusal as "not finished yet", and
            # telling an operator the second when the first is true reads as though the
            # technician had not done the work. It is what a double-tap on a slow phone
            # produces: the first signature lands, the job becomes Done, and the second
            # request arrives to find nothing left to sign.
            if existing["status"] == "Done":
                raise HTTPException(409, "งานนี้ถูกตรวจรับเรียบร้อยแล้ว — ไม่ต้องเซ็นซ้ำ /"
                                         " this job has already been accepted — no second"
                                         " signature is needed")
            raise HTTPException(409, "เซ็นรับได้เฉพาะงานที่ช่างกดเสร็จแล้วเท่านั้น /"
                                     " only completed work can be signed off")
        # The same split the Pending-approval tab uses — hiding a button is not a rule.
        _jt = str(existing["jobtype"] or "").upper()
        _owner = existing["requester_id"] or existing["created_by"]
        # NOBODY ACCEPTS THEIR OWN REPAIR. A technician who may also report problems can
        # raise a job and then fix it; accepting it as well would close work nobody else
        # has looked at. So whoever worked on it (lead, helper, or logged time) cannot
        # accept it — and when the raiser did the work, the plant's planner accepts it.
        _did = _did_work(c, existing)
        _mine = set(_tech_ids(c, u))
        if action == "approve" and u["role"] != "admin" and (_mine & _did):
            raise HTTPException(403, "คุณเป็นผู้ทำงานนี้เอง — ต้องให้ผู้อื่นตรวจรับ (ผู้วางแผน) /"
                                     " you worked on this job yourself — someone else (the planner) has to accept it")
        _self_done = bool(_owner) and (_owner in _did)
        if _jt == "PM":
            # Nobody reported a PM: it is the programme's work, and the plant's planner
            # is the one who answers for it. _job_in_factory already made that *this*
            # plant's planner.
            if u["role"] not in ("planner", "admin"):
                raise HTTPException(403, "งาน PM ต้องให้ผู้วางแผนของโรงงานเป็นผู้ตรวจรับ / "
                                         "a PM job is accepted by the plant's planner")
        elif _jt in ("CM", "BD", "IMP"):
            # Whoever raised it accepts it, whatever their role. Admin is the way back
            # in when that person has left the company.
            # b404: IMP joins the same rule. It used to be operator-only, so a planner
            # who raised an improvement job could never accept it once it was finished.
            if (_owner and _owner not in _tech_ids(c, u) and u["role"] != "admin"
                    and not (_self_done and u["role"] == "planner")):
                who = user_names(c).get(str(_owner), "")
                raise HTTPException(403, f"งานนี้ {who} เป็นผู้แจ้ง — ต้องให้ผู้แจ้งเป็นคนตรวจรับ / "
                                         f"{who} raised this job and has to be the one to accept it")
        else:
            # IMP and older rows: untouched, operator-only, with the old open fallback
            if u["role"] != "operator":
                raise HTTPException(403, "only operator may approve or reject completed work")
            if _owner and _owner not in _tech_ids(c, u):
                owner_is_op = c.execute("SELECT 1 FROM users WHERE id=? AND role='operator'",
                                        (_owner,)).fetchone()
                if owner_is_op:
                    who = user_names(c).get(str(_owner), "")
                    raise HTTPException(403, f"งานนี้ {who} เป็นผู้แจ้ง — ต้องให้ผู้แจ้งเป็นคนตรวจรับ / "
                                             f"{who} reported this job and has to be the one to accept it")
        if action == "approve" and _jt == "PM":
            from .elec import accept_block as _elab      # b413
            _why = _elab(c, existing)
            if _why:
                raise HTTPException(409, _why)
        if action == "approve":
            if "," in sig_data:
                head, b64 = sig_data.split(",", 1)
                ext = "png" if "png" in head else "jpg"
                fn = f"{jid}_sign_appr.{ext}"
                with open(os.path.join(UPLOADS, fn), "wb") as f:
                    f.write(base64.b64decode(b64))
                sig_path = f"/uploads/{fn}"
                c.execute("UPDATE jobs SET sign_appr=? WHERE id=?", (sig_path, jid))
                from .signature import record as _srec
                _srec(c, jid, "sign_appr", u, _signer)
            c.execute("UPDATE jobs SET status='Done', approver_id=?, "
                      "approved_at=COALESCE(approved_at,?), done_at=COALESCE(done_at,?) WHERE id=?",
                      (u["id"], now(), now(), jid))
        elif action == "reject":
            if not reason:
                raise HTTPException(400, "reason required")
            c.execute("UPDATE jobs SET status='Rework', pending_reason=?, rework_count=rework_count+1 WHERE id=?",
                      (reason, jid))
        else:
            raise HTTPException(400, "bad action")
        log_status(c, jid, "Done" if action == "approve" else "Rework", u["id"])
        set_stage(c, jid)
        c.execute("INSERT INTO signoffs(job_id,action,user_id,signature,reason,created_at) VALUES(?,?,?,?,?,?)",
                  (jid, action, u["id"], sig_path, reason, now()))
        c.commit()
        row = job_row(c, jid)
    if action == "approve":
        # The job is now a finished record — file its ใบแจ้งซ่อม (F-SP-ENG02-03) so
        # the form reaches the archive and the Reports shelf without anyone printing
        # it. A PDF failure must never undo an approval that already happened.
        try:
            from .jobform import file_job_form
            file_job_form(jid, u)
        except Exception as e:
            _log.warning("[jobs] could not file the repair form: %s", e)
    if action == "reject":
        # Everyone who needs to know the job came back: the crew that did it, the
        # planner who must re-plan it, and the person who reported it. One push, not two.
        techs = [t for t in [row["lead_tech"],
                             *[int(h) for h in (row["helpers"] or "").split(",") if h]] if t]
        with closing(db()) as c2:               # the outer connection is already closed here
            _fac2 = _job_factory(c2, row["machine_id"], u)
            recips = techs + role_ids(c2, "planner", "manager", "engcenter", factory_id=_fac2)
        if row["requester_id"]:
            recips.append(row["requester_id"])
        notify_users(list(dict.fromkeys(recips)), "\u21a9 \u0e07\u0e32\u0e19\u0e16\u0e39\u0e01\u0e15\u0e35\u0e01\u0e25\u0e31\u0e1a / Rejected",
                     f"{row['jobid']} {row['mcode'] or ''}: {reason[:80]}", f"/?job={jid}")
    return {"ok": True}


def close_stale_segments(c, ids):
    """Close timers still running against a job that is already over.

    A job cancelled or finished while its timer was going leaves a segment with no end.
    Nothing in the app can stop it — the job is closed, so it is gone from every list —
    and it then blocks that technician from starting anything, for ever. The timer is
    closed at the moment the job actually ended, so no invented minutes are added.
    """
    ids = list(ids) or [0]
    ph = ",".join("?" * len(ids))
    rows = c.execute(
        "SELECT t.id, t.start, j.done_at, j.id jid FROM timelogs t JOIN jobs j ON j.id=t.job_id"
        " WHERE t.tech IN (%s) AND t.end IS NULL"
        " AND j.status IN ('Cancelled','Rejected','Done','ServiceCompleted')" % ph, ids).fetchall()
    for r in rows:
        endts = r["done_at"]
        if not endts:
            ev = c.execute("SELECT created_at FROM job_events WHERE job_id=?"
                           " ORDER BY id DESC LIMIT 1", (r["jid"],)).fetchone()
            endts = ev["created_at"] if ev else None
        if not endts or endts < (r["start"] or ""):
            endts = r["start"]
        c.execute("UPDATE timelogs SET end=?, pause_reason=? WHERE id=?",
                  (endts, "job closed", r["id"]))
    return len(rows)


def close_open_segment(c, tech, reason=""):
    """Close the running timer. `tech` may be one id or every id this login stands for —
    a shared phone books time against the person, so the login alone would not find it."""
    ids = tech if isinstance(tech, (list, tuple, set)) else [tech]
    ids = list(ids) or [0]
    c.execute("UPDATE timelogs SET end=?, pause_reason=? WHERE tech IN (%s) AND end IS NULL"
              % ",".join("?" * len(ids)), [now(), reason, *ids])


@router.post("/segments/start")
async def seg_start(req: Request):
    u = user_from(req)
    if u["role"] != "technician":
        raise HTTPException(403, "only technician may start work")
    b = await req.json()
    with closing(db()) as c:
        jid = b.get("job_id")
        _ids = _tech_ids(c, u)                  # this login and everyone who shares it
        _named = _named_tech(c, u, b.get("as_tech"))
        _look = list(dict.fromkeys(_ids + ([_named] if _named else [])))
        if jid:
            job = _job_in_factory(c, jid, u)
            # finished work is closed: never let a stale screen restart it and quietly
            # pull the job back out of the operator's approval queue
            if job["status"] in ("ServiceCompleted", "Done", "Rejected", "Cancelled"):
                raise HTTPException(409, "งานนี้ปิดแล้ว ไม่สามารถเริ่มใหม่ได้ / this job is already finished")
            # b422: a Central Electrical technician works only the electrical points of a
            # PM job, on his own Start/Stop. Starting the PLANT timer would let his Stop
            # close the plant crew's half — which is how a job reached "waiting to accept"
            # with its electrical point never answered.
            from .elec import on_crew as _onc
            if (job.get("jobtype") or "") == "PM" and (u.get("team") or "") == "CE" and not _onc(job, u):
                from .pm import job_checklist as _jcl
                if ((_jcl(c, job) or {}).get("el_total")):
                    raise HTTPException(409, "ช่างไฟฟ้าส่วนกลาง: ใช้ปุ่มเริ่ม/หยุดในหน้างานไฟฟ้า ⚡ /"
                                             " Central Electrical: use Start/Stop on the ⚡ electrical page")
            _crew_gate(c, job, u, "start")   # assigned work is the crew's; open work is anyone's
            # A timer left running on a job that has since been cancelled or finished is
            # not work in progress — it is a leftover. Clear it before the check below,
            # or it locks this technician out of every job there is.
            close_stale_segments(c, _look)
            # One running timer per phone — two jobs cannot both be "being worked on"
            # by the same hands. The message names the job in the way, because
            # "your current job" told the technician nothing about which one it is.
            open_seg = c.execute(
                "SELECT j.id, j.jobid, j.status FROM timelogs t JOIN jobs j ON j.id=t.job_id"
                " WHERE t.tech IN (%s) AND t.end IS NULL AND t.job_id IS NOT NULL"
                " AND t.job_id<>? LIMIT 1" % ",".join("?" * len(_look)),
                [*_look, jid]).fetchone()
            if open_seg:
                # The job id travels with the refusal. Naming the job told the technician
                # WHICH one is in the way but not where it is, and Stop lives on that
                # job's own screen — so the only way out was to back out of here, find it
                # in a list and open it. The phone turns this into one button.
                raise HTTPException(409, {
                    "msg": f"{open_seg['jobid']} กำลังทำอยู่ — จบหรือพักงานนั้นก่อน /"
                           f" {open_seg['jobid']} is still running — finish or hold it first",
                    "open_job": open_seg["id"], "open_jobid": open_seg["jobid"]})
            # Taking an unassigned job is allowed even with planned work outstanding.
            # The shop floor decides what is urgent in the moment; the one-timer rule
            # above is the only thing that has to hold.
            #
            # But a job already being worked on belongs to whoever picked it up. A second
            # technician pressing Start used to open a second timer on the same job —
            # two people clocked onto one fault, and whichever of them finished first
            # left the other's timer running with nothing able to stop it. Now the second
            # press changes nothing at all and simply says who has it.
            if job["status"] == "InProgress":
                held = c.execute("SELECT tech, start FROM timelogs WHERE job_id=? AND end IS NULL"
                                 " ORDER BY id DESC LIMIT 1", (jid,)).fetchone()
                holder = (held["tech"] if held else None) or job["lead_tech"]
                if holder and holder not in _look:
                    who = user_names(c).get(str(holder), "")
                    at = (held["start"] or "")[11:16] if held else ""
                    when = f" ({at})" if at else ""
                    raise HTTPException(409, f"{who} เริ่มงานนี้ไปแล้ว{when} — เปิดซ้ำไม่ได้ /"
                                             f" {who} already started this job{when}")
        # the crew member taking this on, so an unassigned job is booked to a real person
        # rather than to the shared account the phone happens to be signed in with
        actor = _acting_tech(c, job, u, b.get("as_tech")) if jid else u["id"]
        close_open_segment(c, _look, b.get("pause_reason", "switch"))
        c.execute("INSERT INTO timelogs(job_id,tech,seg_type,activity,start) VALUES(?,?,?,?,?)",
                  (b.get("job_id"), actor, b.get("seg_type", "work"), b.get("activity", ""), now()))
        if b.get("job_id"):
            c.execute("UPDATE jobs SET status='InProgress', started_at=COALESCE(started_at,?) WHERE id=?",
                      (now(), b["job_id"]))
            # Starting a job nobody was given is accepting it: the technician who picks
            # it up becomes its lead, so the planner's board shows who has it instead of
            # a job in progress with no name against it. Work the planner already
            # assigned keeps its crew — pressing Start never takes a job off someone.
            from .chat import log_job_event as _lje
            who = user_names(c).get(str(actor), u["name"])
            if not job["lead_tech"]:
                # …and it joins the crew he is working with today, because that is who
                # will be at the machine. The day's team is read once, here, and written
                # onto the job: what lands on a job stays on it, so tomorrow's teams
                # cannot quietly move work somebody has already started.
                _mates = _crew_today(c, u, actor)
                c.execute("UPDATE jobs SET lead_tech=?, helpers=? WHERE id=?",
                          (actor, ",".join(str(m) for m in _mates), b["job_id"]))
                # An urgent job that was never planned still belongs to today, or it
                # sits outside every list that is organised by day.
                c.execute("UPDATE jobs SET planned_date=? WHERE id=?"
                          " AND (planned_date IS NULL OR planned_date='')",
                          (today(), b["job_id"]))
                _lje(c, b["job_id"], actor, f"✋ รับงานเอง / job accepted by {who}")
            elif job["status"] == "Hold":
                # Work left on hold is picked up by whoever is free — often the next
                # shift. The job moves to them, and both names stay in the history so
                # "who held it, who finished it" can always be answered.
                if actor != job["lead_tech"]:
                    was = user_names(c).get(str(job["lead_tech"]), "—")
                    c.execute("UPDATE jobs SET lead_tech=? WHERE id=?", (actor, b["job_id"]))
                    _lje(c, b["job_id"], actor,
                         f"↷ รับช่วงต่อจากพักงาน / taken over after hold: {was} → {who}")
                else:
                    _lje(c, b["job_id"], actor, f"▶ ทำงานต่อหลังพัก / resumed after hold by {who}")
            # The first person to take this job, recorded once and for good. Every
            # other field naming a technician is the CURRENT one and gets overwritten.
            stamp_owner1(c, b["job_id"], actor)
            # a PM job keeps the checklist it was started with, whatever is edited later
            if (job.get("jobtype") or "") == "PM":
                try:
                    from .pm import snapshot_job_items
                    snapshot_job_items(c, job)
                except Exception:
                    pass
            log_status(c, b["job_id"], "InProgress", actor)
            set_stage(c, b["job_id"])
            # The crew is told who picked it up. "Who has this one?" used to be answered
            # by shouting across the plant; the job knows, so the phone can say it.
            try:
                _row2 = job_row(c, b["job_id"])
                _mates2 = [m["id"] for m in _team_of(c, _row2) if m["id"] != actor]
                if _mates2:
                    notify_users(_mates2, "▶ งานเริ่มแล้ว / Job started",
                                 f"{_row2['jobid']} {_row2['mcode'] or ''} · {who}",
                                 f"/?job={b['job_id']}")
            except Exception:
                pass
        c.commit()
    return {"ok": True}


@router.post("/segments/stop")
async def seg_stop(req: Request):
    u = user_from(req)
    # The planner is here as the release valve. A technician who starts a job and then
    # goes home sick leaves a running timer that, before individual logins, any crew mate
    # could close from the shared handset — now nobody can, and the job is stuck at
    # In progress for ever. Admin for the same reason, one level up.
    if u["role"] not in ("technician", "planner", "admin"):
        raise HTTPException(403, "only a technician or the planner may stop work")
    b = await req.json()
    action = b.get("action", "pause")
    with closing(db()) as c:
        # The timer may be booked to any of the crew — to this login, to someone who
        # shares it, or to the person named on this very call. Nothing links a name to a
        # phone, so without that last one the Stop found no timer at all and the job ran
        # on for ever.
        _ids = _tech_ids(c, u)
        _named = _named_tech(c, u, b.get("as_tech"))
        _look = list(dict.fromkeys(_ids + ([_named] if _named else [])))
        # The timer belongs to the JOB, not to the person closing it. Looking for one
        # owned by the signed-in technician was right while a crew shared a login: with
        # a login each, the second member pressing Stop found nothing and was told the
        # job was not running, on a job that plainly was. Any crew member may close it —
        # which of them ran the clock is a fact the log already holds and this does not
        # touch.
        _jid = b.get("job_id")
        seg = None
        if _jid:
            seg = c.execute("SELECT * FROM timelogs WHERE job_id=? AND end IS NULL"
                            " ORDER BY id DESC LIMIT 1", (_jid,)).fetchone()
            if seg:
                _crew_gate(c, job_row(c, _jid), u, "stop")
        if not seg:
            seg = c.execute("SELECT * FROM timelogs WHERE tech IN (%s) AND end IS NULL"
                            % ",".join("?" * len(_look)), _look).fetchone()
        # Finishing means closing the timer that is running. On a job that has been put
        # down there is no timer, so this quietly did nothing at all and still answered
        # "ok" — the phone showed the job closed and the job stayed on Hold. Say so
        # instead. (The Stop button is greyed out on a held job; this is for the stale
        # screen that still has it.)
        if not seg and action == "finish":
            raise HTTPException(409, "งานนี้ไม่ได้กำลังทำอยู่ — กด “ทำต่อ” ก่อนจึงจะปิดงานได้ /"
                                     " this job is not running — press Resume first")
        # A PM job is its checklist. Closing one with items unanswered files a sheet
        # that says the machine was checked when nobody looked at it — the single thing
        # a PM record must never do — so the Stop is refused and says which items.
        if action == "finish" and seg and seg["job_id"]:
            try:
                from .pm import checklist_block
                why = checklist_block(c, job_row(c, seg["job_id"]))
            except Exception:
                why = ""
            if why:
                raise HTTPException(409, why)
        # Close the timer that was found, by its own id — closing "every timer this
        # login stands for" would reach across to another job when a crew mate presses
        # Stop from their own phone.
        if seg:
            c.execute("UPDATE timelogs SET end=?, pause_reason=? WHERE id=?",
                      (now(), b.get("reason", ""), seg["id"]))
        else:
            close_open_segment(c, _look, b.get("reason", ""))
        if seg and seg["job_id"]:
            # The minutes stay with the person who ran the clock. This used to rewrite
            # the timer's owner to whoever pressed Stop, so an hour A spent on the
            # machine became B's hour because B closed the job — and the workload
            # figures were fiction in exactly the direction nobody checks. Who closed it
            # is recorded on the job's history and carried by the signature.
            actor = seg["tech"] or u["id"]   # the clock's owner: the technician credited
            closer = u["id"]                 # whoever pressed Stop, which can be someone else
            if action == "finish":
                c.execute("UPDATE jobs SET done_at=? WHERE id=? AND done_at IS NULL",
                          (now(), seg["job_id"]))
                c.execute("""UPDATE jobs SET status='ServiceCompleted', progress=100,
                    problem=?, root_cause=?, solution=?,
                    fault_category=?, fault_component=?, maint_action=? WHERE id=?""",
                    (b.get("problem", ""), b.get("root_cause", ""), b.get("solution", ""),
                     b.get("fault_category", ""), b.get("fault_component", ""),
                     b.get("maint_action", ""), seg["job_id"]))
                log_status(c, seg["job_id"], "ServiceCompleted", actor)
                row = job_row(c, seg["job_id"])
                # A finished PM sheet becomes a named record: the asset it was done on
                # and the day it was done, which is how anyone looks for it afterwards.
                # The one-line result goes onto the job so a planner reads "OK 4 · NG 2"
                # without opening the checklist, and an NG is a finding worth chasing.
                if (row.get("jobtype") or "") == "PM":
                    try:
                        from .pm import job_checklist
                        cl = job_checklist(c, row)
                        if cl:
                            rec = f"{row.get('mcode') or row['jobid']}_{(row.get('done_at') or now())[:10]}"
                            note = (f"PM {cl['label']} · {cl['total']} \u0e23\u0e32\u0e22\u0e01\u0e32\u0e23"
                                    f" · OK {cl['total'] - cl['ng']} \u00b7 NG {cl['ng']}")
                            c.execute("UPDATE jobs SET report_name=? WHERE id=?", (rec[:120], seg["job_id"]))
                            sol = (b.get("solution") or "").strip()
                            c.execute("UPDATE jobs SET solution=? WHERE id=?",
                                      ((note + ("\n" + sol if sol else ""))[:2000], seg["job_id"]))
                    except Exception:
                        pass
                from .chat import log_job_event
                _nm = user_names(c)
                who = _nm.get(str(actor), u["name"])
                _closer = _nm.get(str(closer), u["name"])
                # Who ran the clock and who closed the job can be two people now, and a
                # history that names only one of them loses the other for good.
                log_job_event(c, seg["job_id"], closer,
                              f"⚙️ สถานะ → ServiceCompleted (ปิดโดย {_closer}"
                              + (f" · ช่างผู้ทำงาน {who}" if str(actor) != str(closer) else "") + ")")
                try:
                    _mates3 = [m["id"] for m in _team_of(c, row) if m["id"] != closer]
                    if _mates3:
                        notify_users(_mates3, "■ งานปิดแล้ว / Job closed",
                                     f"{row['jobid']} {row['mcode'] or ''} · {_closer}",
                                     f"/?job={seg['job_id']}")
                except Exception:
                    pass
                # The buzz goes to whoever now has to accept it, which is the same
                # split as the tab and the signoff guard: a PM wakes the plant's
                # planners, everything else wakes the person who raised it.
                if str(row["jobtype"] or "").upper() == "PM":
                    _fac = c.execute("SELECT factory_id FROM machines WHERE id=?",
                                     (row["machine_id"],)).fetchone()
                    _fac = _fac["factory_id"] if _fac else None
                    _pl = [r["id"] for r in c.execute(
                        "SELECT id FROM users WHERE role IN ('planner','admin') AND active=1"
                        " AND (factory_id=? OR COALESCE(factory_id,0)=0)", (_fac,))]
                    if _pl:
                        notify_users(_pl, "PM completed - please accept",
                                     f"{row['jobid']} {row['mcode'] or ''}", f"/?job={seg['job_id']}")
                elif row["requester_id"]:
                    notify_users([row["requester_id"]], "Completed - please approve",
                                 f"{row['jobid']} {row['mcode'] or ''}", f"/?job={seg['job_id']}")
            else:
                c.execute("UPDATE jobs SET status='Paused', progress=?, pending_reason=? WHERE id=?",
                          (b.get("progress", 0), b.get("reason", ""), seg["job_id"]))
                from .chat import log_job_event as _lje2
                _who = user_names(c).get(str(actor), u["name"])
                _rsn = (b.get("reason") or "").strip()
                _lje2(c, seg["job_id"], actor,
                      f"⏸ พักงาน / paused by {_who}" + (f" — {_rsn}" if _rsn else ""))
            set_stage(c, seg["job_id"])
        c.commit()
    return {"ok": True}


@router.get("/dashboard")
async def dashboard(req: Request, d: str = "", fac: str = ""):
    u = user_from(req)
    d = d or today()
    _vf = _view_factory(u, fac)
    _fw = "" if _vf is None else " AND (COALESCE(m.factory_id,j.factory_id)=? OR COALESCE(m.factory_id,j.factory_id) IS NULL)"
    _fa = [] if _vf is None else [_vf]
    with closing(db()) as c:
        planned = [dict(r) for r in c.execute("""SELECT j.*, m.code mcode, u.name lead_name
            FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
            LEFT JOIN users u ON u.id=j.lead_tech
            WHERE j.planned_date=? AND j.jobtype IN ('PM','CM','IMP','PRJ')
            AND j.status NOT IN """ + VOID_SQL + _fw, [d] + _fa)]
        d0, d1 = day_range(d)
        breakdowns = [dict(r) for r in c.execute("""SELECT j.*, m.code mcode, u.name lead_name
            FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
            LEFT JOIN users u ON u.id=j.lead_tech
            WHERE j.jobtype='BD' AND j.created_at >= ? AND j.created_at <= ?
            AND j.status NOT IN """ + VOID_SQL + _fw, [d0, d1] + _fa)]
        # a cancelled job's minutes are not work anyone did for the plant, so the
        # technician time panel joins the job row and drops them like everything else —
        # and so does a job an admin took out of the numbers by hand.
        #
        # The two boards above keep theirs. An excluded job is still work somebody has to
        # do; taking it off the day's list would hide it from the person who has to do it.
        # The flag is about the figures, not about the plan.
        segs = [dict(r) for r in c.execute("""SELECT t.*, u.name tech_name, j.jobid, j.jobtype
            FROM timelogs t JOIN users u ON u.id=t.tech
            JOIN jobs j ON j.id=t.job_id
            LEFT JOIN machines m ON m.id=j.machine_id
            WHERE t.start >= ? AND t.start <= ?
              AND j.status NOT IN """ + VOID_SQL + NOTKPI_J + _fw + """ ORDER BY t.tech, t.start""",
            [d0, d1] + _fa)]
    rel = [j for j in planned if j["status"] in
           ("Assigned", "InProgress", "Paused", "Rework", "ServiceCompleted", "Done")]
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
                    (j["status"] == "Assigned" and j.get("pending_reason"))],
    }

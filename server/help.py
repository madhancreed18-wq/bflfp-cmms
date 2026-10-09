"""The help bot's back end: where a number comes from, and what to do when it is wrong.

Two jobs, and they are deliberately separate.

**Show the working.** A number on a screen is only trusted once somebody has seen it
taken apart. `/api/help/explain` returns the ROWS behind a number — the time segments
that add up to a technician's work time, the history line that fixed "assigned at", the
jobs that make a day's count — straight from the same tables the screen reads. Nothing
is recomputed here in a second way: a second implementation is a second answer waiting
to disagree with the first, so these read the rows and let the screen's own arithmetic
be checked against them.

**Ask a person.** When the bot cannot answer, or the working shows the number is wrong,
the question goes to the people who can do something about it, with the screen, the
number and the job ids already attached — a technician should not have to describe a
bug. `help_tickets` holds those, the answer comes back to the person who asked, and an
answer marked `to_kb` is one the admin thinks everyone should have.
"""
import json
import logging
from contextlib import closing
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException

from .db import db, now, today
from .auth import user_from, require_role

router = APIRouter(prefix="/api/help")
_log = logging.getLogger("cmms")

ANSWERERS = ("planner", "admin", "manager")
OPEN_ST = ("Assigned", "InProgress", "Paused", "Rework", "Hold")


def _ensure(c):
    c.execute("""CREATE TABLE IF NOT EXISTS help_tickets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        factory_id INTEGER, user_id INTEGER, user_name VARCHAR(120),
        kind VARCHAR(12),                      -- question | wrong
        page VARCHAR(80), ctx TEXT,            -- where they were, and what was on screen
        question TEXT, answer TEXT DEFAULT '',
        status VARCHAR(12) DEFAULT 'new',      -- new | answered | fixed | closed
        to_kb INTEGER DEFAULT 0,
        created_at VARCHAR(19), answered_by INTEGER, answered_at VARCHAR(19))""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_help_fac ON help_tickets(factory_id, status)")
    # Every question the assistant is asked, whether or not it had an answer. The
    # escalated ones (help_tickets) are the loud minority; this is what people actually
    # want to know, and the unmatched ones are the list of rules still to write.
    c.execute("""CREATE TABLE IF NOT EXISTS help_asks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        factory_id INTEGER, user_id INTEGER, user_name VARCHAR(120), role VARCHAR(20),
        page VARCHAR(80), question TEXT,
        matched VARCHAR(40) DEFAULT '',        -- the rule that answered it, if any
        kind VARCHAR(10) DEFAULT '',           -- rule | how | data | none
        created_at VARCHAR(19))""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_help_asks ON help_asks(factory_id, id)")


def _fac(u):
    return u.get("active_factory") or u.get("factory_id")


def _row(r):
    d = dict(r)
    try:
        d["ctx"] = json.loads(d.get("ctx") or "{}")
    except Exception:
        d["ctx"] = {}
    return d


# ── asking ────────────────────────────────────────────────────────────────────────
@router.post("/ticket")
async def ticket(req: Request):
    """Anyone signed in may ask. The screen sends the context; the person types a line."""
    u = user_from(req)
    b = await req.json()
    q = (b.get("question") or "").strip()[:2000]
    if not q:
        raise HTTPException(400, "question is empty")
    kind = "wrong" if b.get("kind") == "wrong" else "question"
    with closing(db()) as c:
        _ensure(c)
        tid = c.insert_id(
            "INSERT INTO help_tickets(factory_id,user_id,user_name,kind,page,ctx,question,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (_fac(u), u["id"], u.get("name") or u.get("username") or "", kind,
             (b.get("page") or "")[:80], json.dumps(b.get("ctx") or {}, ensure_ascii=False)[:4000],
             q, now()))
        c.commit()
    _log.info("help: %s #%s from %s (%s)", kind, tid, u.get("username"), (b.get("page") or "-"))
    return {"ok": True, "id": tid}


@router.post("/ask")
async def ask(req: Request):
    """Record the question. Fire-and-forget from the panel: it must never delay an answer."""
    u = user_from(req)
    b = await req.json()
    q = (b.get("question") or "").strip()[:500]
    if not q:
        return {"ok": True}
    with closing(db()) as c:
        _ensure(c)
        c.execute("INSERT INTO help_asks(factory_id,user_id,user_name,role,page,question,matched,kind,created_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?)",
                  (_fac(u), u["id"], u.get("name") or u.get("username") or "", u.get("role") or "",
                   (b.get("page") or "")[:80], q, (b.get("matched") or "")[:40],
                   (b.get("kind") or "")[:10], now()))
        c.commit()
    return {"ok": True}


@router.get("/asks")
async def asks(req: Request, only: str = "", limit: int = 200):
    """What people are asking — and the ones nobody has written a rule for yet."""
    u = require_role(req, *ANSWERERS)
    q = "SELECT * FROM help_asks WHERE factory_id=?"
    a = [_fac(u)]
    if only == "none":
        q += " AND (kind='none' OR kind='')"
    q += " ORDER BY id DESC LIMIT ?"
    a.append(max(1, min(int(limit), 1000)))
    with closing(db()) as c:
        _ensure(c)
        rows = [dict(r) for r in c.execute(q, a)]
        n_none = c.execute("SELECT COUNT(*) n FROM help_asks WHERE factory_id=? AND (kind='none' OR kind='')",
                           (_fac(u),)).fetchone()["n"]
        n_all = c.execute("SELECT COUNT(*) n FROM help_asks WHERE factory_id=?", (_fac(u),)).fetchone()["n"]
    return {"asks": rows, "unanswered": n_none, "total": n_all}


@router.get("/mine")
async def mine(req: Request):
    """What I asked, and what came back — the reply lands in the same panel."""
    u = user_from(req)
    with closing(db()) as c:
        _ensure(c)
        rows = [_row(r) for r in c.execute(
            "SELECT * FROM help_tickets WHERE user_id=? ORDER BY id DESC LIMIT 25", (u["id"],))]
    return {"tickets": rows, "unread": sum(1 for r in rows if r["status"] == "answered")}


# ── answering ─────────────────────────────────────────────────────────────────────
@router.get("/tickets")
async def tickets(req: Request, status: str = "", limit: int = 100):
    u = require_role(req, *ANSWERERS)
    q = "SELECT * FROM help_tickets WHERE factory_id=?"
    a = [_fac(u)]
    if status:
        q += " AND status=?"
        a.append(status)
    q += " ORDER BY (status='new') DESC, id DESC LIMIT ?"
    a.append(max(1, min(int(limit), 500)))
    with closing(db()) as c:
        _ensure(c)
        rows = [_row(r) for r in c.execute(q, a)]
        n_new = c.execute("SELECT COUNT(*) n FROM help_tickets WHERE factory_id=? AND status='new'",
                          (_fac(u),)).fetchone()["n"]
    return {"tickets": rows, "new": n_new}


@router.post("/tickets/{tid}/answer")
async def answer(tid: int, req: Request):
    u = require_role(req, *ANSWERERS)
    b = await req.json()
    st = b.get("status") if b.get("status") in ("new", "answered", "fixed", "closed") else "answered"
    with closing(db()) as c:
        _ensure(c)
        r = c.execute("SELECT factory_id FROM help_tickets WHERE id=?", (tid,)).fetchone()
        if not r:
            raise HTTPException(404, "no such question")
        if r["factory_id"] != _fac(u) and u["role"] != "admin":
            raise HTTPException(403, "another plant's question")
        c.execute("UPDATE help_tickets SET answer=?, status=?, to_kb=?, answered_by=?, answered_at=?"
                  " WHERE id=?",
                  ((b.get("answer") or "").strip()[:4000], st, 1 if b.get("to_kb") else 0,
                   u["id"], now(), tid))
        c.commit()
    return {"ok": True, "id": tid, "status": st}


# ── showing the working ───────────────────────────────────────────────────────────
def _mins(a, b):
    try:
        x = datetime.strptime(str(a)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
        y = (datetime.strptime(str(b)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
             if b else datetime.now())
        return max(0, round((y - x).total_seconds() / 60))
    except Exception:
        return 0


@router.get("/explain")
async def explain(req: Request, what: str = "", tech: int = 0, job: int = 0, d: str = ""):
    """The rows behind one number on one screen.

    what=busy   tech,d   the work segments counted in that technician's time for the day
    what=count  tech,d   the jobs counted in that technician's number for the day
    what=assigned job    the job's own history: when it was assigned, started, finished
    """
    u = require_role(req, *ANSWERERS)
    fac = _fac(u)
    d = (d or today())[:10]
    W = "COALESCE((SELECT m.factory_id FROM machines m WHERE m.id=j.machine_id), j.factory_id)"
    with closing(db()) as c:
        if what == "busy":
            rows = [dict(r) for r in c.execute(f"""
                SELECT j.jobid, m.code mcode, t.start, t."end" e, COALESCE(t.pause_reason,'') why,
                       u.name tech_name
                  FROM timelogs t JOIN jobs j ON j.id=t.job_id
                  LEFT JOIN machines m ON m.id=j.machine_id
                  LEFT JOIN users u ON u.id=t.tech
                 WHERE {W}=? AND SUBSTR(COALESCE(t.start,''),1,10)<=?
                   AND (t."end" IS NULL OR SUBSTR(t."end",1,10)>=?)
                   AND (j.lead_tech=? OR ','||COALESCE(j.helpers,'')||',' LIKE '%,'||?||',%')
                 ORDER BY t.start""", (fac, d, d, tech, str(tech)))]
            for r in rows:
                r["minutes"] = _mins(max(str(r["start"] or ""), d + " 00:00:00"), r["e"])
                r["running"] = not r["e"]
            return {"what": what, "date": d, "rows": rows,
                    "total_min": sum(r["minutes"] for r in rows)}
        if what == "count":
            rows = [dict(r) for r in c.execute(f"""
                SELECT j.id, j.jobid, j.jobtype, j.status, j.planned_date, m.code mcode,
                       CASE WHEN j.lead_tech=? THEN 'lead' ELSE 'helper' END role
                  FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
                 WHERE {W}=? AND j.planned_date=? AND j.status NOT IN ('Cancelled','Rejected')
                   AND (j.lead_tech=? OR ','||COALESCE(j.helpers,'')||',' LIKE '%,'||?||',%')
                 ORDER BY j.jobid""", (tech, fac, d, tech, str(tech)))]
            return {"what": what, "date": d, "rows": rows, "total": len(rows)}
        if what == "assigned":
            j = c.execute(f"""SELECT j.id, j.jobid, j.status, j.planned_date, j.planned_at,
                                     j.started_at, j.done_at, j.approved_at, {W} fac
                                FROM jobs j WHERE j.id=?""", (job,)).fetchone()
            if not j or j["fac"] != fac:
                raise HTTPException(404, "no such job in this plant")
            ev = [dict(r) for r in c.execute(
                "SELECT e.status, e.created_at, u.name who FROM job_events e"
                " LEFT JOIN users u ON u.id=e.user_id WHERE e.job_id=? ORDER BY e.id", (job,))]
            return {"what": what, "job": dict(j), "events": ev,
                    "first_assigned": next((e["created_at"] for e in ev if e["status"] == "Assigned"),
                                           j["planned_at"])}
    raise HTTPException(400, "unknown 'what'")

# ── questions about the data itself ───────────────────────────────────────────────
# Each entry is ONE named question with ONE query. A question the bot can ask is a
# question somebody wrote a query for — there is no free-form SQL here, and nothing a
# user types reaches the database: the key picks the query, the plant comes from the
# session. Every answer carries the rows behind it, so the number can be checked the
# same way the rest of the bot works.
_W = "COALESCE((SELECT m.factory_id FROM machines m WHERE m.id=j.machine_id), j.factory_id)"
_SEL = f"""SELECT j.id, j.jobid, j.jobtype, j.status, j.planned_date, j.due_date,
                  SUBSTR(COALESCE(j.created_at,''),1,16) created, m.code mcode,
                  COALESCE(m.name, j.report_name, SUBSTR(COALESCE(j.descr,''),1,40)) what,
                  (SELECT name FROM users WHERE id=j.lead_tech) tech
             FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
            WHERE {_W}=? AND j.status NOT IN ('Cancelled','Rejected') """
DATAQ = {
    "new_today":   (_SEL + "AND SUBSTR(COALESCE(j.created_at,''),1,10)=:d ORDER BY j.id DESC", "d"),
    "new_week":    (_SEL + "AND SUBSTR(COALESCE(j.created_at,''),1,10) BETWEEN :w AND :d ORDER BY j.id DESC", "dw"),
    "unassigned":  (_SEL + "AND j.lead_tech IS NULL AND COALESCE(j.helpers,'')=''"
                    " AND j.status IN ('Reported','WaitingApproval','WaitingAssignment') ORDER BY j.planned_date, j.id", ""),
    "late":        (_SEL + "AND COALESCE(j.due_date,'')<>'' AND j.due_date < :d"
                    " AND j.status NOT IN ('Done','ServiceCompleted') ORDER BY j.due_date", "d"),
    "due_today":   (_SEL + "AND j.due_date = :d AND j.status NOT IN ('Done','ServiceCompleted') ORDER BY j.jobid", "d"),
    "to_accept":   (_SEL + "AND j.status='ServiceCompleted' ORDER BY j.done_at", ""),
    "done_today":  (_SEL + "AND SUBSTR(COALESCE(j.done_at,''),1,10)=:d ORDER BY j.done_at DESC", "d"),
    "working_now": (_SEL + "AND j.status IN ('InProgress','Rework') ORDER BY j.started_at", ""),
    "on_hold":     (_SEL + "AND j.status IN ('Paused','Hold') ORDER BY j.started_at", ""),
    "today_plan":  (_SEL + "AND j.planned_date=:d ORDER BY j.jobid", "d"),
    "carried":     (_SEL + "AND j.planned_date < :d AND j.status NOT IN ('Done','ServiceCompleted')"
                    " ORDER BY j.planned_date", "d"),
    "breakdowns_month": (_SEL + "AND j.jobtype='BD' AND SUBSTR(COALESCE(j.created_at,''),1,7)=SUBSTR(:d,1,7)"
                         " ORDER BY j.created_at DESC", "d"),
}


@router.get("/data")
async def data(req: Request, q: str = "", d: str = ""):
    """One named question about the plant's own work, with the rows that answer it."""
    u = require_role(req, *ANSWERERS)
    if q not in DATAQ:
        raise HTTPException(400, "unknown question")
    sql, _needs = DATAQ[q]
    d = (d or today())[:10]
    args = [_fac(u)]                      # the plant is the session's, never the caller's
    if q == "new_week":
        from datetime import date as _dt, timedelta as _td
        args += [(_dt.fromisoformat(d) - _td(days=6)).isoformat(), d]
        sql = sql.replace(":w", "?").replace(":d", "?")
    else:
        n = sql.count(":d")
        sql = sql.replace(":d", "?")
        args += [d] * n
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute(sql, args)]
        # The day's plan is counted by one function, shared with the calendar and the
        # assign board (pm.day_counts) — a second count here is a second answer waiting
        # to disagree with the screens. It also knows about PM that is due with no work
        # order yet, which no query over `jobs` can see.
        split = None
        if q == "today_plan":
            try:
                from . import pm as _pm
                split = (_pm.day_counts(c, _fac(u), d, d) or {}).get(d)
            except Exception:
                split = None
    by_tech = {}
    for r in rows:
        by_tech[r["tech"] or ""] = by_tech.get(r["tech"] or "", 0) + 1
    top = sorted(by_tech.items(), key=lambda x: -x[1])[:5]
    out = {"q": q, "date": d, "n": len(rows), "rows": rows[:40], "more": max(0, len(rows) - 40),
           "by_tech": [{"name": k or "—", "n": v} for k, v in top]}
    if split:
        out["split"] = split
        out["n"] = split.get("total", len(rows))      # the number the board shows
        out["no_wo"] = max(0, split.get("total", 0) - len(rows))
    return out

# ── counting, along three axes instead of one question at a time ──────────────────
# "How many CM jobs were raised today" is the same question as "how many PM finished
# this month" with two words changed, and writing one canned query per wording is how a
# help system rots. Three enumerated axes — what kind of work, which date on the job,
# which stretch of days — give 4 × 5 × 6 answers from one query. The words still never
# reach SQL: the panel sends keys from these tables, and anything else is refused.
METRIC = {                       # which date on the job the question is about
    "raised":   ("SUBSTR(COALESCE(j.created_at,''),1,10)", "แจ้งเข้ามา", "raised"),
    "planned":  ("COALESCE(j.planned_date,'')", "วางแผนไว้", "planned"),
    "finished": ("SUBSTR(COALESCE(j.done_at,''),1,10)", "ทำเสร็จ", "finished"),
    "accepted": ("SUBSTR(COALESCE(j.approved_at,''),1,10)", "ตรวจรับ", "accepted"),
    "due":      ("COALESCE(j.due_date,'')", "ครบกำหนด", "due"),
}
JTYPE = {"PM": "PM", "CM": "CM", "BD": "BD", "IMP": "IMP"}
PERIOD = {"today": "วันนี้", "yesterday": "เมื่อวาน", "week7": "7 วันที่ผ่านมา",
          "month": "เดือนนี้", "lastmonth": "เดือนที่แล้ว", "tomorrow": "พรุ่งนี้",
          # "how many are past due" is not a question about a stretch of days at all
          "any": "ทุกช่วงเวลา"}


def _span(period, d):
    """The first and last day the question covers."""
    from datetime import date as _d, timedelta as _td
    day = _d.fromisoformat(d)
    if period == "any":
        return "1900-01-01", "2999-12-31"
    if period == "today":
        return d, d
    if period == "yesterday":
        y = (day - _td(days=1)).isoformat()
        return y, y
    if period == "tomorrow":
        t = (day + _td(days=1)).isoformat()
        return t, t
    if period == "week7":
        return (day - _td(days=6)).isoformat(), d
    if period == "month":
        return day.replace(day=1).isoformat(), d
    if period == "lastmonth":
        first = day.replace(day=1)
        prev_end = first - _td(days=1)
        return prev_end.replace(day=1).isoformat(), prev_end.isoformat()
    return d, d


@router.get("/count")
async def count(req: Request, metric: str = "raised", jtype: str = "", period: str = "today",
                d: str = "", tech: str = "", mcode: str = "", stgroup: str = "", dept: str = "",
                plant: str = ""):
    """How many work orders of a kind, by one of their dates, over a stretch of days —
    optionally narrowed to one technician, one machine, one status group or one
    department. Every narrowing is a value this plant really has (see /vocab) or a key
    from a fixed table; none of it is text pasted into SQL."""
    u = require_role(req, *ANSWERERS)
    if metric not in METRIC or period not in PERIOD:
        raise HTTPException(400, "unknown question")
    # "how many jobs are planned in BFLPC" names the plant it means. Honoured only for an
    # account that may move between plants — the same rule the plant switcher enforces —
    # and _plant refuses anything else, so the answer is never quietly about another one.
    fac, pcode, _pn = _plant(u, plant)
    jtype = (jtype or "").upper()
    if jtype and jtype not in JTYPE:
        raise HTTPException(400, "unknown job type")
    if stgroup and stgroup not in STGROUP:
        raise HTTPException(400, "unknown status group")
    d = (d or today())[:10]
    a, b = _span(period, d)
    col = METRIC[metric][0]
    q = _SEL + f" AND {col} BETWEEN ? AND ?"
    args = [fac, a, b]
    if jtype:
        q += " AND UPPER(j.jobtype)=?"
        args.append(jtype)
    applied = {}
    with closing(db()) as c0:
        if tech:
            ids = [r["id"] for r in c0.execute(
                "SELECT id FROM users WHERE factory_id=? AND LOWER(COALESCE(name,''))=LOWER(?)",
                (fac, tech.strip()))]
            if not ids:
                raise HTTPException(400, "unknown technician")
            ph = ",".join("?" * len(ids))
            like = " OR ".join(["','||COALESCE(j.helpers,'')||',' LIKE '%,'||?||',%'"] * len(ids))
            q += f" AND (j.lead_tech IN ({ph}) OR {like}"
            q += " OR LOWER(COALESCE(j.owner1_name,''))=LOWER(?) OR LOWER(COALESCE(j.owner2_name,''))=LOWER(?))"
            args += ids + [str(i) for i in ids] + [tech.strip(), tech.strip()]
            applied["tech"] = tech.strip()
        if mcode:
            r = c0.execute("SELECT id, code FROM machines WHERE factory_id=? AND UPPER(code)=UPPER(?)",
                           (fac, mcode.strip())).fetchone()
            if not r:
                raise HTTPException(400, "unknown machine code")
            q += " AND j.machine_id=?"
            args.append(r["id"])
            applied["mcode"] = r["code"]
        if dept:
            q += " AND UPPER(COALESCE(j.req_dept,''))=UPPER(?)"
            args.append(dept.strip())
            applied["dept"] = dept.strip().upper()
    if stgroup:
        q += " AND (" + STGROUP[stgroup].replace(":today", "?") + ")"
        if ":today" in STGROUP[stgroup]:
            args.append(today())
        applied["stgroup"] = stgroup
    q += " ORDER BY j.id DESC"
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute(q, args)]
        split = carried = plan_doc = None
        if metric == "planned" and a == b and not jtype and not applied:
            # the day's plan has one definition, shared with the calendar and the board
            try:
                from . import pm as _pm
                split = (_pm.day_counts(c, fac, a, a) or {}).get(a)
            except Exception:
                split = None
            # …and the two numbers people compare it against, so a mismatch explains
            # itself instead of becoming "the bot is wrong": work CARRIED in from earlier
            # days (on the board, deliberately not in the day's own total) and the PLAN
            # DOCUMENT issued for the day, which is a snapshot taken when it was issued.
            W2 = "COALESCE((SELECT m.factory_id FROM machines m WHERE m.id=j.machine_id), j.factory_id)"
            carried = c.execute(
                "SELECT COUNT(*) n FROM jobs j WHERE " + W2 + "=? AND COALESCE(j.planned_date,'')<>''"
                " AND j.planned_date < ? AND j.status NOT IN"
                " ('Done','ServiceCompleted','Cancelled','Rejected')", (fac, a)).fetchone()["n"]
            try:
                r = c.execute("SELECT jobs, created_at FROM plan_reports WHERE factory_id=? AND plan_date=?"
                              " ORDER BY id DESC LIMIT 1", (fac, a)).fetchone()
                plan_doc = {"jobs": r["jobs"], "at": r["created_at"]} if r else None
            except Exception:
                plan_doc = None
    by_type, by_st = {}, {}
    for r in rows:
        by_type[r["jobtype"] or "?"] = by_type.get(r["jobtype"] or "?", 0) + 1
        by_st[r["status"]] = by_st.get(r["status"], 0) + 1
    out = {"metric": metric, "jtype": jtype, "period": period, "plant": pcode, "from": a, "to": b, "applied": applied,
           "n": len(rows), "rows": rows[:40], "more": max(0, len(rows) - 40),
           "by_type": [{"t": k, "n": v} for k, v in sorted(by_type.items(), key=lambda x: -x[1])],
           "by_status": [{"s": k, "n": v} for k, v in sorted(by_st.items(), key=lambda x: -x[1])]}
    if split:
        out["split"] = split
        out["n"] = split.get("total", len(rows))
        out["no_wo"] = max(0, split.get("total", 0) - len(rows))
        out["carried"] = carried
        out["plan_doc"] = plan_doc
    return out

# ── the words a plant actually uses, so the panel can recognise them ──────────────
@router.get("/vocab")
async def vocab(req: Request):
    """Technician names, asset codes and departments of THIS plant.

    The panel matches the question against these lists, so a name or a machine code in
    a question is recognised only if the plant really has it — and what travels to the
    server is the matched value, never the sentence."""
    u = require_role(req, *ANSWERERS)
    fac = _fac(u)
    with closing(db()) as c:
        techs = [r["name"] for r in c.execute(
            "SELECT DISTINCT name FROM users WHERE active=1 AND role='technician' AND (factory_id=? OR COALESCE(factory_id,0)=0)"
            " AND COALESCE(name,'')<>'' ORDER BY LENGTH(name) DESC", (fac,))]
        codes = [r["code"] for r in c.execute(
            "SELECT code FROM machines WHERE factory_id=? AND COALESCE(code,'')<>''", (fac,))]
        try:
            depts = [{"code": r["code"], "th": r["name_th"], "en": r["name_en"]} for r in c.execute(
                "SELECT code, name_th, name_en FROM departments WHERE COALESCE(active,1)=1 ORDER BY seq, code")]
        except Exception:
            depts = []
        who = _planners(c, fac)
    return {"techs": techs, "codes": codes, "depts": depts, **who}


# Status groups, as the screens name them. A question may narrow a count to one of
# these; anything else is refused.
STGROUP = {
    "open":       "j.status NOT IN ('Done','ServiceCompleted','Cancelled','Rejected')",
    "assigned":   "j.status='Assigned'",
    "working":    "j.status IN ('InProgress','Paused','Hold','Rework')",
    "accept":     "j.status='ServiceCompleted'",
    "done":       "j.status='Done'",
    "unassigned": "j.lead_tech IS NULL AND COALESCE(j.helpers,'')=''",
    "late":       ("COALESCE(j.due_date,'')<>'' AND j.due_date < :today"
                   " AND j.status NOT IN ('Done','ServiceCompleted','Cancelled','Rejected')"),
}

def _planners(c, fac):
    """The people who can actually answer "when will it be done" for this plant."""
    rows = [dict(r) for r in c.execute(
        "SELECT name, role FROM users WHERE active=1 AND role IN ('planner','manager')"
        " AND (factory_id=? OR COALESCE(factory_id,0)=0) AND COALESCE(name,'')<>''"
        " ORDER BY (role='planner') DESC, name", (fac,))]
    return {"planners": [r["name"] for r in rows if r["role"] == "planner"][:3],
            "managers": [r["name"] for r in rows if r["role"] == "manager"][:2]}


# ── "when will it be finished?" ───────────────────────────────────────────────────
@router.get("/when")
async def when(req: Request, job: str = ""):
    """What is PROMISED for one job, and what has happened to it so far.

    The app holds no estimate of when a repair will end, and inventing one would be the
    worst thing this assistant could do: a technician standing at a machine would read
    it as a commitment. So this returns only facts — the day it is planned for, the day
    it was promised by, whether anyone has started, who is on it, and when it last
    moved — and the panel says plainly that the rest is a question for the crew."""
    u = require_role(req, *ANSWERERS)
    fac = _fac(u)
    key = (job or "").strip()
    if not key:
        raise HTTPException(400, "no job given")
    W = "COALESCE((SELECT m.factory_id FROM machines m WHERE m.id=j.machine_id), j.factory_id)"
    with closing(db()) as c:
        r = c.execute(f"""SELECT j.id, j.jobid, j.jobtype, j.status, j.planned_date, j.due_date,
                                 j.planned_at, j.started_at, j.done_at, j.approved_at, j.progress,
                                 j.pending_reason, m.code mcode, m.name mname,
                                 COALESCE(j.report_name,'') place, j.lead_tech,
                                 COALESCE(j.helpers,'') helpers,
                                 (SELECT name FROM users WHERE id=j.lead_tech) lead_name
                            FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
                           WHERE {W}=? AND (UPPER(j.jobid)=UPPER(?) OR j.jobid LIKE ?)
                           ORDER BY j.id DESC LIMIT 1""",
                      (fac, key, "%" + key)).fetchone()
        if not r:
            raise HTTPException(404, "no such job in this plant")
        d = dict(r)
        names = {str(x["id"]): x["name"] for x in c.execute("SELECT id, name FROM users")}
        d["helper_names"] = [names.get(h.strip(), "") for h in d["helpers"].split(",") if h.strip()]
        d["events"] = [dict(x) for x in c.execute(
            "SELECT e.status, e.created_at, u.name who FROM job_events e"
            " LEFT JOIN users u ON u.id=e.user_id WHERE e.job_id=? ORDER BY e.id DESC LIMIT 6",
            (d["id"],))]
        seg = c.execute("SELECT start FROM timelogs WHERE job_id=? AND \"end\" IS NULL"
                        " ORDER BY id DESC LIMIT 1", (d["id"],)).fetchone()
        d["running_since"] = seg["start"] if seg else None
        # Why it stopped, and when. A held job whose reason is only in the last time
        # segment used to read as "started but stopped", which tells nobody anything.
        last = c.execute("SELECT \"end\" e, COALESCE(pause_reason,'') why FROM timelogs"
                         " WHERE job_id=? AND \"end\" IS NOT NULL ORDER BY id DESC LIMIT 1",
                         (d["id"],)).fetchone()
        d["stopped_at"] = last["e"] if last else None
        why = (d.get("pending_reason") or "").strip() or (last["why"].strip() if last else "")
        d["hold_reason"] = "" if why.lower() in ("complete", "completed", "done") else why
        d.update(_planners(c, fac))
    d["today"] = today()
    return d

# ── who the technicians are, and what one of them is doing ───────────────────────
# A plant's roster and a person's live picture are the two questions the floor actually
# asks out loud — "who do we have?" and "is he on something right now?" — and both were
# answered until now by a count of work orders, which is not what was asked.
def _plant(u, code):
    """The plant a question names, or the session's own.

    A named plant is honoured only for an account that may move between plants (the same
    rule the plant switcher enforces); anyone else gets told which plant they are in
    rather than being shown another plant's people.
    """
    from .auth import all_plants
    mine = _fac(u)
    code = (code or "").strip().upper()
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute("SELECT id, code, name FROM factories ORDER BY id")]
    here = next((r for r in rows if r["id"] == mine), None)
    if not code:
        return mine, (here or {}).get("code", ""), (here or {}).get("name", "")
    # The plants are filed as BFL / FP / PC, and everybody on site calls them BFL /
    # BFLFP / BFLPC — the group's initials in front of the plant's own letters. A
    # question is asked in the spoken name, so both spellings have to land, and so do
    # the words in the plant's full name ("wet food", "petcare").
    hit = next((r for r in rows if r["code"].upper() == code), None)
    if not hit and code.startswith("BFL") and len(code) > 3:
        tail = code[3:]
        hit = next((r for r in rows if r["code"].upper() == tail), None)
    if not hit:
        near = [r for r in rows if r["code"].upper().startswith(code)
                or code in (r["name"] or "").upper().replace(" ", "")]
        hit = near[0] if len(near) == 1 else None
    if not hit:
        raise HTTPException(400, {"msg": "unknown plant",
                                  "plants": [r["code"] for r in rows]})
    if hit["id"] != mine and not all_plants(u):
        raise HTTPException(403, {"msg": "this account only sees " + (here or {}).get("code", "its own plant"),
                                  "plant": (here or {}).get("code", "")})
    return hit["id"], hit["code"], hit["name"]


def _tech_rows(c, fac):
    """The plant's technicians — one row per PERSON, with the phone their work reaches.

    Two kinds of row live in `users`: a login account, and a person who carries no login
    of their own and rides on somebody else's (`login_id`). Both are technicians to a
    planner, so both are listed — with the account named, because "why is this not on my
    phone" is almost always a person pointing at the wrong login.

    A human filed BOTH ways — a login account plus a person record under the same name —
    is one technician and gets one row, with both user ids kept: work is booked against
    whichever row the planner picked that day, and a roster that lists Mark twice and
    splits his jobs between the two answers nobody's question.
    """
    rows = [dict(r) for r in c.execute(
        "SELECT id, name, username, COALESCE(can_login,1) cl, login_id FROM users"
        " WHERE active=1 AND role='technician' AND (factory_id=? OR COALESCE(factory_id,0)=0) AND COALESCE(name,'')<>''"
        " ORDER BY COALESCE(can_login,1) DESC, name", (fac,))]
    byid = {r["id"]: r for r in rows}
    out, seen = [], {}
    for r in rows:
        lg = r if r["cl"] else byid.get(r["login_id"])
        key = (r["name"] or "").strip().lower()
        if key in seen:
            p = seen[key]
            p["ids"].append(r["id"])
            p["is_account"] = p["is_account"] or bool(r["cl"])
            p["phone"] = p["phone"] or ((lg or {}).get("username") or "")
            continue
        seen[key] = {"name": (r["name"] or "").strip(), "ids": [r["id"]],
                     "phone": (lg or {}).get("username") or "",
                     "is_account": bool(r["cl"])}
        out.append(seen[key])
    # SHARING is two people on ONE login, which is what the word means to a planner and
    # what makes "why is this not on my phone" happen. A person filed as their own record
    # pointing at their own account shares nothing, and calling that "shared" — which the
    # first cut did — labelled every technician of BFL as sharing a phone with nobody.
    used = {}
    for p in out:
        if p["phone"]:
            used.setdefault(p["phone"].lower(), []).append(p["name"])
    for p in out:
        mates = used.get((p["phone"] or "").lower(), [])
        p["with"] = [m for m in mates if m != p["name"]]
        p["shares"] = len(mates) > 1
        p["nologin"] = not p["phone"]          # reaches no phone at all
    out.sort(key=lambda p: p["name"].lower())
    return out


@router.get("/techs")
async def techs(req: Request, plant: str = ""):
    """The technicians of one plant — the session's, or a named one."""
    u = require_role(req, *ANSWERERS)
    fac, code, name = _plant(u, plant)
    with closing(db()) as c:
        rows = _tech_rows(c, fac)
        t0 = today()
        # how much each of them is carrying, so the roster answers "who is free" too
        open_n = {}
        for r in c.execute(
            "SELECT j.lead_tech t, COUNT(*) n FROM jobs j WHERE " + _W + "=? AND j.status IN (%s)"
            " GROUP BY j.lead_tech" % ",".join("?" * len(OPEN_ST)), (fac, *OPEN_ST)):
            open_n[r["t"]] = r["n"]
    return {"plant": code, "plant_name": name, "today": t0,
            "techs": [{"name": r["name"], "phone": r["phone"], "is_account": r["is_account"],
                       "shares": r["shares"], "with": r["with"], "nologin": r["nologin"],
                       "open": sum(open_n.get(i, 0) for i in r["ids"])}
                      for r in rows]}


@router.get("/tech")
async def tech_state(req: Request, name: str = "", plant: str = ""):
    """One technician: what they are on right now, what is waiting, what they last finished.

    The floor's question is never "how many work orders carry his name" — it is "is he on
    something, and if not, what did he last finish". So this reads the running time
    segment first (the same signal the live board uses), then the work still on his plate,
    then the most recent finished job WHATEVER day it was finished on: a last-completed
    job limited to today reads as "he has finished nothing" at eight in the morning.

    A person who shares a login is resolved the way the phone resolves them, so the
    answer is about the person named, not about the account they sign in with.
    """
    u = require_role(req, *ANSWERERS)
    fac, code, _pn = _plant(u, plant)
    key = (name or "").strip()
    if not key:
        raise HTTPException(400, "no name given")
    with closing(db()) as c:
        rows = _tech_rows(c, fac)
        me = next((r for r in rows if r["name"].lower() == key.lower()), None)
        if not me:
            near = [r for r in rows if key.lower() in r["name"].lower()]
            me = near[0] if len(near) == 1 else None
        if not me:
            raise HTTPException(400, {"msg": "unknown technician", "plant": code,
                                      "techs": [r["name"] for r in rows]})
        uids = me["ids"]
        ph = ",".join("?" * len(uids))
        like = " OR ".join(["','||COALESCE(j.helpers,'')||',' LIKE '%,'||?||',%'"] * len(uids))
        jobs = [dict(r) for r in c.execute(f"""
            SELECT j.id, j.jobid, j.jobtype, j.status, j.planned_date, j.due_date,
                   j.started_at, j.done_at, j.approved_at, COALESCE(j.pending_reason,'') pending,
                   m.code mcode, COALESCE(m.name, j.report_name, SUBSTR(COALESCE(j.descr,''),1,40)) what,
                   (j.lead_tech IN ({ph})) is_lead
              FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
             WHERE {_W}=? AND j.status NOT IN ('Cancelled','Rejected')
               AND (j.lead_tech IN ({ph}) OR {like})
             ORDER BY j.id DESC""", (*uids, fac, *uids, *[str(i) for i in uids]))]
        ids = [j["id"] for j in jobs]
        run, stopped = {}, {}
        for i in range(0, len(ids), 400):
            ch = ids[i:i + 400]
            ph = ",".join("?" * len(ch))
            for r in c.execute("SELECT job_id, start FROM timelogs WHERE \"end\" IS NULL"
                               " AND job_id IN (%s) ORDER BY id" % ph, ch):
                run[r["job_id"]] = r["start"]
            for r in c.execute("SELECT job_id, MAX(\"end\") e FROM timelogs WHERE \"end\" IS NOT NULL"
                               " AND job_id IN (%s) GROUP BY job_id" % ph, ch):
                stopped[r["job_id"]] = r["e"]
    t0 = today()
    def slim(j, **extra):
        return {"id": j["id"], "jobid": j["jobid"], "jobtype": j["jobtype"], "status": j["status"],
                "mcode": j["mcode"] or "", "what": j["what"] or "", "planned_date": j["planned_date"],
                "role": "lead" if j["is_lead"] else "helper", **extra}
    # RIGHT NOW MEANS A TIMER IS RUNNING. Taking it from the status instead — the first
    # cut counted InProgress and Rework as "being worked on", the way the live board does
    # for its own purposes — said "Mark is working right now" about a job whose timer
    # stopped six days ago and which had been sent back for rework. A status is what
    # somebody last pressed; a running segment is what is happening.
    nowj = [slim(j, since=run[j["id"]]) for j in jobs if run.get(j["id"])]
    # started and not running: each with the reason it is not moving, because "he started
    # nothing" is false about a job he started and stopped, and that is where his day is
    S = {"Paused": "pause", "Hold": "hold", "Rework": "rework", "InProgress": "nolog"}
    stop = [slim(j, state=S[j["status"]],
                 stopped_at=stopped.get(j["id"]) or j["started_at"],
                 reason=(j["pending"] or "").strip())
            for j in jobs if j["status"] in S and not run.get(j["id"])]
    stop.sort(key=lambda j: str(j.get("stopped_at") or ""), reverse=True)
    done = [j for j in jobs if j["status"] in ("Done", "ServiceCompleted") and j["done_at"]]
    done.sort(key=lambda j: str(j["done_at"]), reverse=True)
    last = slim(done[0], at=done[0]["done_at"]) if done else None
    openj = [j for j in jobs if j["status"] in OPEN_ST]
    return {"plant": code, "name": me["name"], "phone": me["phone"], "shares": me["shares"],
            "with": me["with"], "nologin": me["nologin"],
            "is_account": me["is_account"], "today": t0,
            "now": nowj, "stopped": stop, "paused": stop, "last_done": last,
            "counts": {"assigned": sum(1 for j in openj if j["status"] == "Assigned"),
                       "open": len(openj),
                       "working": len(nowj),
                       "paused": len(stop),
                       "to_accept": sum(1 for j in jobs if j["status"] == "ServiceCompleted"),
                       "done_today": sum(1 for j in done if str(j["done_at"])[:10] == t0),
                       "planned_today": sum(1 for j in jobs if j["planned_date"] == t0),
                       "done_all": len(done)},
            "next": [slim(j) for j in sorted(openj, key=lambda j: (j["planned_date"] or "9999", j["jobid"] or ""))
                     if j["status"] == "Assigned"][:5]}

# ── the plan that was ISSUED for a day ───────────────────────────────────────────
# "How many jobs are planned today" has an official answer on this plant: the sheet the
# planner issued and the crews work from. The board's own number is a live count and the
# two drift apart during the day — work is moved, added, finished — which is exactly how
# "the bot says 19 and my plan says 52" happens. So the assistant answers with the SHEET
# when there is one, and then says plainly what has changed under it since.
def _plan_row(c, fac, d):
    try:
        from .db import ensure_plan_reports
        ensure_plan_reports(c)
    except Exception:
        pass
    try:
        return c.execute(
            "SELECT * FROM plan_reports WHERE factory_id=? AND plan_date=?"
            " AND COALESCE(kind,'plan')='plan' ORDER BY id DESC LIMIT 1", (fac, d)).fetchone()
    except Exception:
        return None


def _ids_from_sheet(path):
    """The job numbers printed on an issued sheet, read back off the sheet itself.

    Plans issued before the ids were filed still exist as the HTML that was printed and
    signed, and that paper IS the plan — so for those days the numbers are read from it
    rather than re-derived from a database that has moved on since.
    """
    import os
    import re
    from .config import UPLOADS
    name = os.path.basename(str(path or ""))
    if not name:
        return []
    f = os.path.join(UPLOADS, "planreports", name)
    if not os.path.exists(f):
        return []
    try:
        with open(f, encoding="utf-8") as fh:
            html = fh.read()
    except Exception:
        return []
    out, seen = [], set()
    for m in re.finditer(r"\b([A-Z]{2,4}-\d{3,4}-\d{2,5})\b", html):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(m.group(1))
    return out


@router.get("/plan")
async def plan(req: Request, d: str = "", plant: str = ""):
    """The issued plan for a day: what it listed, where that work stands now, what moved.

    Three sources, in the order of how much they are worth. The ids FILED with the sheet
    are exactly what was issued. Failing that, the numbers printed ON the sheet, read
    back off the file. Failing both — no plan was ever issued for that day — the sheet's
    own definition of a day's work, re-derived and flagged as such, so the answer never
    passes a reconstruction off as a document.
    """
    u = require_role(req, *ANSWERERS)
    fac, pcode, _pn = _plant(u, plant)
    d = (d or today())[:10]
    with closing(db()) as c:
        row = _plan_row(c, fac, d)
        meta, src, jobs = None, "derived", []
        if row:
            r = dict(row)
            meta = {"jobs": r.get("jobs"), "at": r.get("created_at"), "by": r.get("creator") or "",
                    "shift": r.get("shift") or "", "path": r.get("path") or "",
                    "crews": r.get("crews"), "people": r.get("people")}
            ids = [int(x) for x in str(r.get("job_ids") or "").split(",") if str(x).strip().isdigit()]
            if ids:
                src = "filed"
                ph = ",".join("?" * len(ids))
                got = {x["id"]: dict(x) for x in c.execute(
                    _SEL.replace("AND j.status NOT IN ('Cancelled','Rejected')", "")
                    + f" AND j.id IN ({ph})", (fac, *ids))}
                jobs = [got[i] for i in ids if i in got]
            else:
                nums = _ids_from_sheet(r.get("path"))
                if nums:
                    src = "sheet"
                    ph = ",".join("?" * len(nums))
                    got = {x["jobid"]: dict(x) for x in c.execute(
                        _SEL.replace("AND j.status NOT IN ('Cancelled','Rejected')", "")
                        + f" AND j.jobid IN ({ph})", (fac, *nums))}
                    jobs = [got[n] for n in nums if n in got]
        if not jobs:
            # no sheet to read: the same set a sheet would be built from, said to be so
            try:
                from .planreport import _day_jobs
                raw = _day_jobs(c, fac, d)
            except Exception:
                raw = []
            ph = ",".join("?" * len(raw)) if raw else ""
            got = {}
            if raw:
                got = {x["id"]: dict(x) for x in c.execute(
                    _SEL.replace("AND j.status NOT IN ('Cancelled','Rejected')", "")
                    + f" AND j.id IN ({ph})", (fac, *[j["id"] for j in raw]))}
            jobs = [got[j["id"]] for j in raw if j["id"] in got]
        # what has happened to the issued work since, and what was added after it
        on = {j["id"] for j in jobs}
        now_day = [dict(x) for x in c.execute(
            _SEL + " AND j.planned_date=? ORDER BY j.jobid", (fac, d))]
    # A listed job now planned for a LATER day was postponed after the sheet went out —
    # that is the "my plan says 52 and the board says 19" gap, and it has names. One
    # planned for an EARLIER day was carried in when the sheet was made and never moved;
    # calling that "moved" said every carried job had been postponed, which is nonsense.
    moved = [j for j in jobs if (j.get("planned_date") or "") > d
             and j["status"] not in ("Done", "ServiceCompleted")]
    carried = [j for j in jobs if (j.get("planned_date") or "9999") < d]
    gone = [j for j in jobs if j["status"] in ("Cancelled", "Rejected")]
    added = [j for j in now_day if j["id"] not in on]
    g = lambda *s: sum(1 for j in jobs if j["status"] in s)
    return {"date": d, "plant": pcode, "source": src, "issued": meta,
            "n": len(jobs), "rows": jobs[:60], "more": max(0, len(jobs) - 60),
            "state": {"done": g("Done"), "accept": g("ServiceCompleted"),
                      "working": g("InProgress", "Paused", "Hold", "Rework"),
                      "open": sum(1 for j in jobs if j["status"] in
                                  ("Reported", "WaitingApproval", "WaitingAssignment", "Assigned", "Released")),
                      "cancelled": len(gone)},
            "moved": [{"jobid": j["jobid"], "to": j.get("planned_date") or "", "mcode": j.get("mcode") or ""}
                      for j in moved[:20]], "moved_n": len(moved), "carried_n": len(carried),
            "added": [{"jobid": j["jobid"], "mcode": j.get("mcode") or "", "status": j["status"]}
                      for j in added[:20]], "added_n": len(added),
            "today": today()}

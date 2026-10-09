import os, secrets, time
from contextlib import closing

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

from .db import db, check_pw
from . import security

router = APIRouter(prefix="/api")
SESSIONS = {}


@router.get("/factories")
async def factories():
    """Public list for the login screen's factory selector."""
    with closing(db()) as c:
        return [{"id": r["id"], "code": r["code"], "name": r["name"]}
                for r in c.execute("SELECT id,code,name FROM factories ORDER BY id")]


@router.get("/captcha")
async def captcha(req: Request):
    token, image, answer = security.new_captcha()
    out = {"token": token, "image": image}
    if os.environ.get("CMMS_TEST_CAPTCHA") == "1":   # test mode only (smoke)
        out["answer"] = answer
    return out


def user_from(req: Request):
    u = SESSIONS.get(req.cookies.get("bflfp"))
    if not u:
        raise HTTPException(401, "login required")
    # A technician who may also report problems works in one of two modes, chosen on the
    # phone. In reporter mode the app sends X-Act-As: operator and every rule that reads
    # the role — creating a report, the reporter's lists, accepting finished work — sees
    # an operator. Same login, same name; the session itself is never changed.
    if (u.get("role") == "technician" and u.get("can_report")
            and (req.headers.get("x-act-as") or "").lower() == "operator"):
        v = dict(u)
        v["role"], v["real_role"] = "operator", "technician"
        return v
    return u


def all_plants(u):
    """True when this account is filed under every plant rather than one.

    Two ways in, and the first one is new. An account stored with factory_id = 0 — which
    ANY role may now be, so a planner or a manager who covers the group can be given the
    run of all three plants and pick which one they are in. Or admin / Engineering
    Center, who keep it however they are filed: an admin locked into one plant could not
    go and fix another, and that is the account you use when something is wrong.

    Takes a users row or a session dict; both answer to .get / [] the same way here.
    """
    fac = u["factory_id"] if not hasattr(u, "get") else u.get("factory_id")
    role = u["role"] if not hasattr(u, "get") else u.get("role")
    return (not fac) or role in ("admin", "engcenter")


def require_role(req: Request, *roles):
    u = user_from(req)
    if u["role"] not in roles:
        raise HTTPException(403, "not allowed")
    return u


@router.post("/login")
async def login(req: Request):
    b = await req.json()
    username = b.get("username", "")
    ip = (req.headers.get("cf-connecting-ip")
          or (req.headers.get("x-forwarded-for", "").split(",")[0].strip() or None)
          or (req.client.host if req.client else ""))

    remaining = security.locked_for(username, ip)
    if remaining:
        raise HTTPException(429, {"msg": f"พยายามผิดหลายครั้งเกินไป — ลองใหม่ใน {max(1, remaining // 60)} นาที"})

    if security.captcha_required(username, ip):
        if not security.verify_captcha(b.get("captcha_token"), b.get("captcha_answer")):
            raise HTTPException(401, {"captcha_required": True,
                                      "msg": "กรุณากรอกรหัสจากภาพให้ถูกต้อง"})

    with closing(db()) as c:
        r = c.execute("SELECT * FROM users WHERE username=? AND active=1",
                      (username,)).fetchone()
    if not r or not check_pw(b.get("password", ""), r["password"]):
        security.record_fail(username, ip)
        time.sleep(0.4)  # slow brute force
        detail = {"msg": "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"}
        if security.captcha_required(username, ip):
            detail["captcha_required"] = True
        raise HTTPException(401, detail)

    if not (r["can_login"] if "can_login" in r.keys() else 1):
        raise HTTPException(401, {"msg": "บัญชีนี้ใช้สำหรับมอบหมายงานเท่านั้น ไม่สามารถเข้าสู่ระบบได้ / this record is for job assignment only"})

    security.clear_fails(username, ip)

    # Which plant this sign-in enters. An account filed under one plant may only enter
    # that plant — the plant is not a preference there, it is the scope of everything
    # they are allowed to see. An account filed under ALL plants picks one at the login
    # screen and may move between them afterwards, whatever its role.
    try:
        fac_id = int(b.get("factory_id") or r["factory_id"])
    except (TypeError, ValueError):
        fac_id = r["factory_id"]
    if not all_plants(r) and fac_id != r["factory_id"]:
        raise HTTPException(401, {"msg": "บัญชีนี้ไม่ได้สังกัดโรงงานที่เลือก — กรุณาเลือกโรงงานของคุณ"})
    if not fac_id:
        # An all-plants account with no plant chosen would land in factory 0, which owns
        # no machines: every screen would come up empty and look broken rather than
        # unset. The login screen always sends one, so this only catches a bare API call.
        raise HTTPException(401, {"msg": "กรุณาเลือกโรงงานก่อนเข้าสู่ระบบ / choose a plant to sign in to"})
    with closing(db()) as c:
        f = c.execute("SELECT code,name FROM factories WHERE id=?", (fac_id,)).fetchone()
        # the technicians who share this login — the phone shows their names, not the account's
        people = [{"id": p["id"], "name": p["name"]} for p in c.execute(
            "SELECT id,name FROM users WHERE login_id=? AND active=1 ORDER BY name", (r["id"],))]

    tok = secrets.token_hex(16)
    sess = dict(r)
    sess["active_factory"] = fac_id
    sess["factory_code"] = f["code"] if f else ""
    sess["factory_name"] = f["name"] if f else ""
    SESSIONS[tok] = sess
    resp = JSONResponse({"id": r["id"], "name": r["name"], "role": r["role"],
                         "people": people, "username": r["username"],
                         "factory_id": fac_id, "factory_code": sess["factory_code"],
                         "factory_name": sess["factory_name"],
                         # whether this account may move between plants — the screen
                         # shows the plant switcher on this, not on the role any more
                         "all_plants": all_plants(r),
                         "can_report": bool(sess.get("can_report")) and r["role"] == "technician",
                         "team": sess.get("team") or ""})
    max_age = 86400 * 30 if b.get("remember") else 86400   # remember me = 30 days
    resp.set_cookie("bflfp", tok, httponly=True, max_age=max_age)
    return resp


@router.post("/logout")
async def logout(req: Request):
    SESSIONS.pop(req.cookies.get("bflfp"), None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("bflfp")
    return resp


@router.get("/me")
async def me(req: Request):
    u = user_from(req)
    # The session is a snapshot taken at sign-in. An admin may have renamed this
    # person, changed their role or switched them off since, so read identity from
    # the table and write it back into the session — no need to sign out and in again.
    with closing(db()) as c:
        r = c.execute("SELECT name,role,active,COALESCE(can_report,0) can_report,COALESCE(team,'') team FROM users WHERE id=?",
                      (u["id"],)).fetchone()
    if r:
        if not r["active"]:
            SESSIONS.pop(req.cookies.get("bflfp"), None)
            raise HTTPException(401, {"msg": "บัญชีนี้ถูกปิดใช้งาน / this account has been disabled"})
        # write into the real session, not the reporter-mode copy user_from may hand back
        sess = SESSIONS.get(req.cookies.get("bflfp")) or u
        sess["name"], sess["role"], sess["can_report"] = r["name"], r["role"], int(r["can_report"] or 0)
        sess["team"] = r["team"] or ""
        u = dict(sess)
    # A login is a key, not a person: one account may be shared by a whole crew.
    # Hand back the real technicians behind it so the phone can show their names
    # instead of the account label.
    with closing(db()) as c:
        people = [{"id": r["id"], "name": r["name"]} for r in c.execute(
            "SELECT id,name FROM users WHERE login_id=? AND active=1 ORDER BY name", (u["id"],))]
    # ── What is clocked on right now ──────────────────────────────────────────
    # One running timer per person is the rule, and until this existed the ONLY way a
    # technician discovered they had one was to be refused when starting the next job.
    # A timer nobody remembers is also a timer nobody stops, which is how a PM ends up
    # with eleven hours of "repair time" against it. It rides on /me because the phone
    # already asks for that; it costs one indexed query.
    running = None
    with closing(db()) as c:
        ids = [u["id"]] + [p["id"] for p in people]
        try:
            r = c.execute(
                "SELECT t.job_id, t.start, j.jobid, j.jobtype, m.code mcode, m.name mname,"
                "       tu.name tech_name"
                "  FROM timelogs t JOIN jobs j ON j.id=t.job_id"
                "  LEFT JOIN machines m ON m.id=j.machine_id"
                "  LEFT JOIN users tu ON tu.id=t.tech"
                " WHERE t.end IS NULL AND t.job_id IS NOT NULL"
                "   AND t.tech IN (%s) ORDER BY t.start LIMIT 1" % ",".join("?" * len(ids)),
                ids).fetchone()
            if r:
                running = dict(r)
        except Exception:
            running = None
    return {"id": u["id"], "name": u["name"], "role": u["role"], "people": people,
            "running": running,
            "username": u.get("username", ""),
            "factory_id": u.get("active_factory", u.get("factory_id")),
            "factory_code": u.get("factory_code", ""),
            "factory_name": u.get("factory_name", ""),
            "all_plants": all_plants(u),
            "can_report": bool(u.get("can_report")) and u.get("role") == "technician",
            "team": u.get("team") or ""}


@router.post("/factory")
async def switch_factory(req: Request):
    """Move this session to another plant.

    The plant chips at the top of the screen have called this since they stopped being a
    filter and became a switch — but the endpoint was never actually written, so every
    click posted into a 405 that the page swallowed, and the only way to change plant was
    to sign out and pick another one at the login screen.

    Session-only: it moves where this login is working, never what the account is filed
    under. Refused for an account that belongs to one plant, the same rule the login
    screen enforces, so a technician cannot reach another plant's work by posting here.
    """
    u = user_from(req)
    b = await req.json()
    try:
        fac_id = int(b.get("factory_id") or 0)
    except (TypeError, ValueError):
        fac_id = 0
    if not fac_id:
        raise HTTPException(400, "factory_id required")
    if not all_plants(u):
        raise HTTPException(403, {"msg": "บัญชีนี้สังกัดโรงงานเดียว / this account belongs to one plant"})
    with closing(db()) as c:
        f = c.execute("SELECT code,name FROM factories WHERE id=?", (fac_id,)).fetchone()
    if not f:
        raise HTTPException(400, "unknown factory")
    # SESSIONS holds this very dict, so writing to it moves the session
    u["active_factory"] = fac_id
    u["factory_code"], u["factory_name"] = f["code"], f["name"]
    return {"factory_id": fac_id, "factory_code": f["code"], "factory_name": f["name"]}


@router.get("/bootstrap")
async def bootstrap(req: Request):
    from .db import today
    u = user_from(req)
    if u["role"] in ("planner", "admin"):
        from .kpi import ensure_pm_jobs
        try:
            ensure_pm_jobs()
        except Exception:
            pass
    with closing(db()) as c:
        fac = u.get("active_factory") or u["factory_id"]
        # Undone work used to be MOVED onto today at the first sign-in of the day. The
        # plant asked for the opposite and they are right: a job keeps the day it was
        # planned for until it is finished, so opening that day months later shows the
        # plan that was made, with its crews. Unfinished work is not lost by this — it
        # is in the "carried" pile on every later day's board (planned_date < today and
        # not finished), on the daily plan sheet, and on the technician's own list,
        # which has never been limited by date. Set CMMS_ROLL_FORWARD=1 to bring the
        # old behaviour back.
        if os.environ.get("CMMS_ROLL_FORWARD", "0") == "1":
            try:
                from .jobs import roll_forward
                roll_forward(c, fac)
            except Exception:
                pass
        machines = [dict(r) for r in c.execute(
            "SELECT * FROM machines WHERE active=1 AND factory_id=? ORDER BY code", (fac,))]
        # Work is always planned inside one factory, so the people offered for
        # assignment are the people of the factory chosen at login — for every role.
        # can_login/login_id travel too, so the planner's pickers can flag a technician
        # no phone can sign in as — work booked to them would be invisible
        techs = [dict(r) for r in c.execute(
            "SELECT id,name,COALESCE(can_login,1) can_login,login_id FROM users"
            " WHERE role='technician' AND active=1 AND (factory_id=? OR COALESCE(factory_id,0)=0) ORDER BY name", (fac,))]
        # role travels too, so the report form can offer just the operators as ผู้แจ้ง,
        # and can_login/login_id let the move-job picker flag an operator no phone can
        # sign in as — a job handed to one of those could never be approved
        # department travels too: the report form pre-fills the reporting department from
        # whoever is named as ผู้แจ้ง, so changing the reporter changes the suggestion
        users = [dict(r) for r in c.execute(
            "SELECT id,name,username,role,COALESCE(can_login,1) can_login,login_id,"
            "COALESCE(department,'') department FROM users"
            " WHERE active=1 AND factory_id=? ORDER BY name", (fac,))]
        # The symptom list the operator's report form is built from. Without it the
        # "what is wrong" picker has nothing to offer and every report arrives with no
        # symptom, which then leaves the trade column blank on the daily plan.
        from .db import ensure_problem_types
        ensure_problem_types(c)
        ptypes = [dict(r) for r in c.execute(
            "SELECT id,name,category,seq FROM problem_types"
            " WHERE active=1 AND factory_id=? ORDER BY seq,name", (fac,))]
        # Who is reporting the work. Not every CM comes from production — warehouse,
        # QA, packing and the boiler house all raise jobs — and from 1 Oct a CM number
        # is built from this code, so the list has to be on the form, not guessed at.
        from .db import ensure_departments, dept_code, DEPT_JOBID_FROM
        ensure_departments(c)
        depts = [dict(r) for r in c.execute(
            "SELECT code,name_th,name_en FROM departments WHERE active=1 ORDER BY seq,code")]
    # the code is resolved here, once, rather than teaching the browser the alias table:
    # a user row may still hold "Qc lab" or "Quality Department" from before the codes
    for r in users:
        r["dept"] = dept_code(r.get("department") or "")
    my_dept = dept_code(u.get("department") or "")
    from .config import ENV
    return {"machines": machines, "techs": techs, "users": users, "today": today(),
            "problem_types": ptypes, "departments": depts, "my_dept": my_dept,
            "dept_jobid_from": DEPT_JOBID_FROM, "env": ENV}

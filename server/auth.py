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
    return u


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

    security.clear_fails(username, ip)

    # factory selector: admin/manager may enter any factory; others must match their own
    try:
        fac_id = int(b.get("factory_id") or r["factory_id"])
    except (TypeError, ValueError):
        fac_id = r["factory_id"]
    if r["role"] not in ("admin", "manager") and fac_id != r["factory_id"]:
        raise HTTPException(401, {"msg": "บัญชีนี้ไม่ได้สังกัดโรงงานที่เลือก — กรุณาเลือกโรงงานของคุณ"})
    with closing(db()) as c:
        f = c.execute("SELECT code,name FROM factories WHERE id=?", (fac_id,)).fetchone()

    tok = secrets.token_hex(16)
    sess = dict(r)
    sess["active_factory"] = fac_id
    sess["factory_code"] = f["code"] if f else ""
    sess["factory_name"] = f["name"] if f else ""
    SESSIONS[tok] = sess
    resp = JSONResponse({"id": r["id"], "name": r["name"], "role": r["role"],
                         "factory_id": fac_id, "factory_code": sess["factory_code"],
                         "factory_name": sess["factory_name"]})
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
    return {"id": u["id"], "name": u["name"], "role": u["role"],
            "factory_id": u.get("active_factory", u.get("factory_id")),
            "factory_code": u.get("factory_code", ""),
            "factory_name": u.get("factory_name", "")}


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
        machines = [dict(r) for r in c.execute(
            "SELECT * FROM machines WHERE active=1 ORDER BY code")]
        techs = [dict(r) for r in c.execute(
            "SELECT id,name FROM users WHERE role='technician' AND active=1")]
        users = [dict(r) for r in c.execute(
            "SELECT id,name,username FROM users WHERE active=1 ORDER BY name")]
    return {"machines": machines, "techs": techs, "users": users, "today": today()}

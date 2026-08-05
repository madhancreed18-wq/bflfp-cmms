from contextlib import closing

from fastapi import APIRouter, Request, HTTPException

from .db import db, hash_pw
from .auth import require_role

router = APIRouter(prefix="/api/admin")


@router.get("/machines")
async def list_machines(req: Request):
    require_role(req, "admin", "planner")
    with closing(db()) as c:
        return [dict(r) for r in c.execute("SELECT * FROM machines ORDER BY code")]


@router.post("/machines")
async def add_machine(req: Request):
    require_role(req, "admin")
    b = await req.json()
    if not b.get("code") or not b.get("name"):
        raise HTTPException(400, "code and name required")
    with closing(db()) as c:
        try:
            c.execute("INSERT INTO machines(code,name) VALUES(?,?)", (b["code"], b["name"]))
            c.commit()
        except Exception:
            raise HTTPException(400, "machine code already exists")
        return dict(c.execute("SELECT * FROM machines WHERE code=?", (b["code"],)).fetchone())


@router.patch("/machines/{mid}")
async def edit_machine(mid: int, req: Request):
    require_role(req, "admin")
    b = await req.json()
    sets = {k: b[k] for k in ("code", "name", "active", "ideal_rate",
                              "criticality", "line", "pm_freq_days") if k in b}
    if not sets:
        raise HTTPException(400, "nothing to update")
    with closing(db()) as c:
        c.execute(f"UPDATE machines SET {','.join(k+'=?' for k in sets)} WHERE id=?",
                  (*sets.values(), mid))
        c.commit()
        return dict(c.execute("SELECT * FROM machines WHERE id=?", (mid,)).fetchone())


@router.get("/users")
async def list_users(req: Request):
    require_role(req, "admin")
    with closing(db()) as c:
        return [dict(r) for r in c.execute(
            "SELECT id,username,name,role,active FROM users ORDER BY role,username")]


@router.post("/users")
async def add_user(req: Request):
    require_role(req, "admin")
    b = await req.json()
    if not all(b.get(k) for k in ("username", "name", "role", "password")):
        raise HTTPException(400, "username, name, role, password required")
    if b["role"] not in ("operator", "planner", "technician", "manager", "admin"):
        raise HTTPException(400, "bad role")
    with closing(db()) as c:
        try:
            c.execute("INSERT INTO users(username,password,name,role) VALUES(?,?,?,?)",
                      (b["username"], hash_pw(b["password"]), b["name"], b["role"]))
            c.commit()
        except Exception:
            raise HTTPException(400, "username already exists")
        return {"ok": True}


@router.patch("/users/{uid}")
async def edit_user(uid: int, req: Request):
    require_role(req, "admin")
    b = await req.json()
    with closing(db()) as c:
        if b.get("password"):
            c.execute("UPDATE users SET password=? WHERE id=?", (hash_pw(b["password"]), uid))
        sets = {k: b[k] for k in ("name", "role", "active") if k in b}
        if sets:
            c.execute(f"UPDATE users SET {','.join(k+'=?' for k in sets)} WHERE id=?",
                      (*sets.values(), uid))
        c.commit()
    return {"ok": True}

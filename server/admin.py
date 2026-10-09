from contextlib import closing

from fastapi import APIRouter, Request, HTTPException

from .db import db, hash_pw
from .auth import require_role, user_from

router = APIRouter(prefix="/api/admin")


@router.get("/machines")
async def list_machines(req: Request):
    u = require_role(req, "admin", "planner")
    with closing(db()) as c:
        # Only the admin works across plants — everyone else sees the factory they
        # chose on the login page. The factory travels with each row so the Manage
        # screen can label and filter them instead of showing one flat pile.
        q = ("SELECT m.*, f.code factory_code, f.name factory_name"
             " FROM machines m LEFT JOIN factories f ON f.id=m.factory_id")
        args = []
        if u["role"] != "admin":
            q += " WHERE m.factory_id=?"
            args.append(u.get("active_factory") or u.get("factory_id"))
        return [dict(r) for r in c.execute(q + " ORDER BY f.code, m.code", args)]


@router.post("/machines")
async def add_machine(req: Request):
    u = require_role(req, "admin")
    b = await req.json()
    if not b.get("code") or not b.get("name"):
        raise HTTPException(400, "code and name required")
    # A machine belongs to the plant you are signed into. It used to be created with no
    # factory at all — the column default decided — and then read back by code alone,
    # which now that two plants may share a code could hand back the OTHER plant's row.
    fac = u.get("active_factory") or u.get("factory_id")
    with closing(db()) as c:
        try:
            mid = c.insert_id("INSERT INTO machines(code,name,factory_id) VALUES(?,?,?)",
                              (b["code"], b["name"], fac))
            c.commit()
        except Exception:
            raise HTTPException(400, "รหัสเครื่องนี้มีอยู่แล้วในโรงงานนี้ /"
                                     " that machine code already exists in this plant")
        return dict(c.execute("SELECT * FROM machines WHERE id=?", (mid,)).fetchone())


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


@router.post("/import-assets")
async def import_assets_upload(req: Request):
    """Admin upload of an Asset-Review .xlsx (base64 in JSON) → upsert into machines for
    the factory the user is logged into.

    Admin only. It used to accept a planner as well, which is the wrong shape of
    permission for what it does: one upload rewrites the name, group, location and floor
    of every machine in a plant, and a wrong file quietly renames the register a whole
    plant works from. Planning a day and re-authoring the asset register are not the same
    authority. The page is off the planner's menu as well, but this is the rule — a menu
    is a convenience, not a permission.
    """
    import base64
    u = require_role(req, "admin")
    b = await req.json()
    data = b.get("b64") or ""
    if "," in data[:80]:                        # tolerate a data:...;base64, prefix
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data)
    except Exception:
        raise HTTPException(400, "bad file data")
    if not raw:
        raise HTTPException(400, "empty file")
    # guard: if the filename names a factory, it must match the one you're logged into
    name = (b.get("name") or "").upper().replace(" ", "")
    want = "PC" if "BFLPC" in name else "FP" if "BFLFP" in name else "BFL" if "BFL" in name else None
    if want and u.get("factory_code") and want != u["factory_code"]:
        raise HTTPException(400, f"ไฟล์นี้เป็นของโรงงาน {want} — คุณเข้าสู่ระบบโรงงาน {u.get('factory_code')} "
                                 f"(this file is for {want}; you are logged into {u.get('factory_code')})")
    fac = u.get("active_factory") or u["factory_id"]
    from .asset_import import import_xlsx
    try:
        with closing(db()) as c:
            res = import_xlsx(raw, fac, c)
    except ValueError as e:
        raise HTTPException(400, str(e))
    res["factory_id"] = fac
    res["factory_code"] = u.get("factory_code", "")
    return res


@router.get("/factory-hours")
async def get_factory_hours(req: Request):
    import json
    u = require_role(req, "admin", "planner", "manager")
    fac = u.get("active_factory") or u.get("factory_id")
    with closing(db()) as c:
        r = c.execute("SELECT hours_json FROM factories WHERE id=?", (fac,)).fetchone()
    try:
        cfg = json.loads(r["hours_json"]) if r and r["hours_json"] else {}
    except Exception:
        cfg = {}
    return {"start": cfg.get("start") or "07:00", "end": cfg.get("end") or "21:00",
            "weekly_off": cfg["weekly_off"] if cfg.get("weekly_off") is not None else [6],
            # per-weekday windows: {"6": {"start": "07:00", "end": "20:00"}} — only the
            # weekdays that differ from the plant's own hours are in here
            "days": cfg.get("days") or {},
            "holidays": cfg.get("holidays") or [], "groups": cfg.get("groups") or {}}


@router.post("/factory-hours")
async def set_factory_hours(req: Request):
    import json
    u = require_role(req, "admin", "planner", "manager")
    fac = u.get("active_factory") or u.get("factory_id")
    b = await req.json()
    # A weekday may carry its own window — Sunday 07:00-20:00 is 13 hours, which
    # `weekly_off` on its own cannot say. Only days that DIFFER are stored, and a day
    # written here wins over weekly_off, so the two can never contradict each other on
    # screen. Anything unparseable is dropped rather than stored half-formed.
    _days = {}
    for k, v in (b.get("days") or {}).items():
        try:
            wd = int(k)
        except (TypeError, ValueError):
            continue
        if not 0 <= wd <= 6:
            continue
        if isinstance(v, dict) and v.get("off"):
            _days[str(wd)] = {"off": True}
        elif isinstance(v, dict) and v.get("start") and v.get("end"):
            _days[str(wd)] = {"start": str(v["start"]), "end": str(v["end"])}
    cfg = {"start": b.get("start") or "07:00", "end": b.get("end") or "21:00",
           "weekly_off": [int(x) for x in (b.get("weekly_off") or [])],
           "days": _days,
           "holidays": [str(x) for x in (b.get("holidays") or [])],
           "groups": b.get("groups") or {}}
    with closing(db()) as c:
        c.execute("UPDATE factories SET hours_json=? WHERE id=?", (json.dumps(cfg), fac))
        c.commit()
    return {"ok": True}


@router.get("/tab-order")
async def get_tab_order(req: Request):
    """Saved order of the planner's view tabs (any planner can read it)."""
    import json
    require_role(req, "admin", "planner", "manager")
    with closing(db()) as c:
        c.execute("CREATE TABLE IF NOT EXISTS app_settings(k TEXT PRIMARY KEY, v TEXT)")
        r = c.execute("SELECT v FROM app_settings WHERE k='planner_tabs'").fetchone()
    try:
        order = json.loads(r["v"]) if r and r["v"] else []
    except Exception:
        order = []
    return {"order": order}


@router.post("/tab-order")
async def set_tab_order(req: Request):
    """Save the planner tab order — admin only."""
    import json
    require_role(req, "admin")
    b = await req.json()
    order = [str(x) for x in (b.get("order") or [])]
    with closing(db()) as c:
        c.execute("CREATE TABLE IF NOT EXISTS app_settings(k TEXT PRIMARY KEY, v TEXT)")
        c.execute("DELETE FROM app_settings WHERE k='planner_tabs'")
        c.execute("INSERT INTO app_settings(k,v) VALUES('planner_tabs',?)", (json.dumps(order),))
        ui_stamp_bump(c)
        c.commit()
    return {"ok": True}


@router.post("/users/{uid}/photo")
async def user_photo(uid: int, req: Request):
    """Store a person's photo (data URL) and hang it off the user record."""
    import base64, os
    from .config import UPLOADS
    require_role(req, "admin", "planner")
    b = await req.json()
    data = b.get("data") or ""
    if "," not in data:
        raise HTTPException(400, "bad image")
    head, b64 = data.split(",", 1)
    ext = "png" if "png" in head else "jpg"
    raw = base64.b64decode(b64)
    if len(raw) > 4_000_000:
        raise HTTPException(400, "image too large (max 4 MB)")
    fn = f"user_{uid}.{ext}"
    with open(os.path.join(UPLOADS, fn), "wb") as f:
        f.write(raw)
    path = f"/uploads/{fn}"
    with closing(db()) as c:
        c.execute("UPDATE users SET photo=? WHERE id=?", (path, uid))
        c.commit()
    return {"ok": True, "photo": path}


# b381: the admin arranges the menu FOR people. A layout used to be saved under the
# arranger's own role, so an admin dragging the menu changed only the admins' menu and
# a planner could quietly overwrite every planner's. Now only the admin saves, choosing
# who it is for — everyone, or named roles — and a stamp in app_settings moves with every
# save, which the live token carries, so open screens re-read the menu without a re-login.
NAV_ROLES = ("admin", "planner", "manager", "engcenter")


def ui_stamp_bump(c):
    import time
    c.execute("CREATE TABLE IF NOT EXISTS app_settings(k TEXT PRIMARY KEY, v TEXT)")
    c.execute("DELETE FROM app_settings WHERE k='ui_stamp'")
    c.execute("INSERT INTO app_settings(k,v) VALUES('ui_stamp',?)", (str(int(time.time() * 1000)),))


@router.get("/nav-order")
async def get_nav_order(req: Request):
    """Saved sidebar layout for a role: that role's own if the admin made one, else the
    layout the admin set for everyone. Only an admin may read another role's."""
    import json
    u = user_from(req)
    role = u["role"] or ""
    if u["role"] == "admin" and req.query_params.get("role"):
        role = req.query_params.get("role")[:20]
    with closing(db()) as c:
        c.execute("CREATE TABLE IF NOT EXISTS app_settings(k TEXT PRIMARY KEY, v TEXT)")
        r = c.execute("SELECT v FROM app_settings WHERE k=?", ("nav_" + role,)).fetchone()
        src = "role"
        if not (r and r["v"]):
            r = c.execute("SELECT v FROM app_settings WHERE k='nav__all'").fetchone()
            src = "all" if r and r["v"] else "default"
    try:
        lay = json.loads(r["v"]) if r and r["v"] else None
    except Exception:
        lay = None
    return {"role": role, "layout": lay, "source": src}


@router.post("/nav-order")
async def set_nav_order(req: Request):
    """Save a sidebar layout — admin only. Shape: {layout:{top:[key],kids:{parent:[key]}},
    roles:"all" | [role,...]}. "all" becomes everyone's menu and clears the per-role ones."""
    import json
    require_role(req, "admin")
    b = await req.json()
    lay = b.get("layout") or {}
    roles = b.get("roles")
    if roles is None and b.get("role"):          # an older page sends {role}
        roles = [b.get("role")]
    top = [str(x)[:24] for x in (lay.get("top") or [])]
    kids = {str(k)[:24]: [str(x)[:24] for x in (v or [])]
            for k, v in (lay.get("kids") or {}).items()}
    clean = json.dumps({"top": top, "kids": kids})
    with closing(db()) as c:
        c.execute("CREATE TABLE IF NOT EXISTS app_settings(k TEXT PRIMARY KEY, v TEXT)")
        if roles == "all":
            c.execute("DELETE FROM app_settings WHERE k='nav__all' OR k LIKE 'nav\\_%' ESCAPE '\\'")
            c.execute("INSERT INTO app_settings(k,v) VALUES('nav__all',?)", (clean,))
            saved = ["all"]
        else:
            saved = [r for r in (roles or []) if r in NAV_ROLES]
            if not saved:
                raise HTTPException(400, "roles required")
            for r in saved:
                c.execute("DELETE FROM app_settings WHERE k=?", ("nav_" + r,))
                c.execute("INSERT INTO app_settings(k,v) VALUES(?,?)", ("nav_" + r, clean))
        ui_stamp_bump(c)
        c.commit()
    return {"ok": True, "saved": saved}


@router.post("/nav-order/reset")
async def reset_nav_order(req: Request):
    """Back to the built-in menu order for everyone — admin only."""
    require_role(req, "admin")
    with closing(db()) as c:
        c.execute("CREATE TABLE IF NOT EXISTS app_settings(k TEXT PRIMARY KEY, v TEXT)")
        c.execute("DELETE FROM app_settings WHERE k='nav__all' OR k LIKE 'nav\\_%' ESCAPE '\\'")
        ui_stamp_bump(c)
        c.commit()
    return {"ok": True}


@router.get("/users")
async def list_users(req: Request):
    require_role(req, "admin")
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT u.id,u.username,u.name,u.role,u.active,u.factory_id,u.department,u.photo,"
            " u.can_login, u.login_id, COALESCE(u.can_report,0) can_report, COALESCE(u.team,'') team, f.code factory_code,"
            " COALESCE(u.shared_login,0) shared_login, COALESCE(u.sig_path,'') sig_path, COALESCE(u.sig_at,'') sig_at"
            " FROM users u LEFT JOIN factories f ON f.id=u.factory_id ORDER BY u.role,u.name")]
    # `dept` is the free text resolved to a code. It travels beside the text rather than
    # replacing it, so the Manage list still shows what somebody actually typed while the
    # edit form can pre-select the right entry — "Qc lab" and "Quality Department" are
    # both QLY, and neither would match a dropdown on its own.
    from .db import dept_code
    for r in rows:
        r["dept"] = dept_code(r.get("department") or "")
    return rows


ROLES = ("operator", "planner", "technician", "manager", "engcenter", "admin")

# "All plants" used to be admin / Eng Center only. It is now offered for every role,
# and which accounts get it is a decision for the person creating them rather than a
# rule in here: a planner or a manager may genuinely cover all three sites, and there
# was no way to say so. The plant still defaults to one — all-plants has to be chosen.
ALL_PLANT_ROLES = ROLES


def _factory_for(b, u, role):
    """The factory to store on a user row — 0 meaning every plant.

    A user MUST belong to a factory, because the data is factory-scoped and a technician
    with no factory would never appear in the planner's assignment popup. The exception
    is an account filed under all plants — any role may be — which is stored as 0 and
    picks the plant it is working in when it signs in, or from the plant switcher.
    """
    try:
        fac = int(b.get("factory_id") if b.get("factory_id") is not None
                  else (u.get("active_factory") or u.get("factory_id") or 0))
    except (TypeError, ValueError):
        fac = 0
    if fac == 0 and role not in ALL_PLANT_ROLES:
        raise HTTPException(400, "ต้องเลือกโรงงาน / this role has to belong to one plant")
    if fac < 0:
        raise HTTPException(400, "factory_id required")
    return fac


# A planner may add a PERSON. Only an admin may create a CREDENTIAL.
#
# These were one authority because they are one endpoint, and that put the planner
# on the wrong side of a wall at the exact moment it blocked their own work: the
# assignment board offers people, a plant with none cannot be given any work at all,
# and the planner — who knows perfectly well who the technicians are — had to stop
# and find an admin.
#
# The two things are not comparable. An assign-only person is written with a random
# placeholder username and an UNUSABLE password (see below): it can never sign in,
# it holds a name and a department, and all it does is become pickable when crews
# are made up. A login account is a credential. So the planner is let through for
# the first and refused the first, narrowly:
#
#   · can_login must be 0            — no credential, ever
#   · role must be technician or operator — not planner, manager or admin
#   · the plant is FORCED to their own, whatever the request body says
#   · login_id may not be set        — attaching a person to an account stays admin's
#
# Editing and deleting people stay admin-only: a name on a person row is printed on
# signed job records, and changing one afterwards rewrites what those records say.
PERSON_ROLES = ("technician", "operator")


def _person_guard(u, b, can_login):
    """Narrow the request to what a non-admin is allowed to create. Returns the
    factory the row must be filed under."""
    if u.get("role") == "admin":
        return None
    if can_login:
        raise HTTPException(403, "only an admin can create a login account")
    if b.get("role") not in PERSON_ROLES:
        raise HTTPException(403, "a planner can add technicians and operators only")
    if b.get("login_id"):
        raise HTTPException(403, "only an admin can attach a person to an account")
    fac = u.get("active_factory") or u.get("factory_id")
    if not fac:
        raise HTTPException(403, "no plant on this account")
    return fac


@router.post("/users")
async def add_user(req: Request):
    u = require_role(req, "admin", "planner")
    b = await req.json()
    can_login = 0 if b.get("can_login") in (0, False, "0") else 1
    if not b.get("name") or not b.get("role"):
        raise HTTPException(400, "name and role required")
    if b["role"] not in ROLES:
        raise HTTPException(400, "bad role")
    _forced = _person_guard(u, b, can_login)
    if _forced is not None:
        b["factory_id"] = _forced          # a planner files into their own plant only
    if can_login and not all(b.get(k) for k in ("username", "password")):
        raise HTTPException(400, "username and password required for a login account")
    fac = _factory_for(b, u, b["role"])
    with closing(db()) as c:
        if fac and not c.execute("SELECT 1 FROM factories WHERE id=?", (fac,)).fetchone():
            raise HTTPException(400, "unknown factory")
        import secrets
        # an assign-only technician gets a placeholder username and an unusable password
        username = (b.get("username") or "").strip() or ("t" + secrets.token_hex(4))
        pw = hash_pw(b["password"]) if can_login else hash_pw(secrets.token_hex(16))
        try:
            uid = c.insert_id("""INSERT INTO users(username,password,name,role,active,factory_id,
                department,photo,can_login,login_id) VALUES(?,?,?,?,1,?,?,'',?,?)""",
                (username, pw, b["name"], b["role"], fac,
                 (b.get("department") or "").strip()[:60], can_login,
                 b.get("login_id") or None))
            if b.get("can_report") and b.get("role") == "technician":
                c.execute("UPDATE users SET can_report=1 WHERE id=?", (uid,))
            if b.get("team") == "CE":
                c.execute("UPDATE users SET team='CE' WHERE id=?", (uid,))
            c.commit()
        except Exception:
            raise HTTPException(400, "username already exists")
        return {"ok": True, "id": uid}


def _swap_id_in_list(val, old, new):
    """helpers and team members are comma-separated id lists."""
    ids = [x.strip() for x in str(val or "").split(",") if x.strip()]
    out, seen = [], set()
    for x in ids:
        y = str(new) if x == str(old) else x
        if y not in seen:
            seen.add(y)
            out.append(y)
    return ",".join(out)


@router.delete("/users/{uid}")
async def delete_user(uid: int, req: Request, reassign_to: int = 0):
    """Remove a person or a login account, handing their work to somebody else.

    Nothing in the history is thrown away: every job they reported, led or approved,
    every time log, signature and team place is moved to `reassign_to` first — the
    admin doing the deleting, unless another name is given. So the record still says
    who owns the work, and the name that left is simply gone.
    """
    u = require_role(req, "admin")
    if uid == u["id"]:
        raise HTTPException(400, "ลบบัญชีที่กำลังใช้อยู่ไม่ได้ / you cannot delete the account you are signed in with")
    target = int(reassign_to or u["id"])
    if target == uid:
        raise HTTPException(400, "ต้องย้ายงานไปให้คนอื่น / the work has to move to somebody else")
    with closing(db()) as c:
        row = c.execute("SELECT id,name,username,role,COALESCE(can_login,1) can_login"
                        " FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise HTTPException(404, "ไม่พบผู้ใช้นี้ / no such user")
        tgt = c.execute("SELECT id,name FROM users WHERE id=? AND active=1", (target,)).fetchone()
        if not tgt:
            raise HTTPException(400, "ไม่พบผู้รับงาน / the person the work moves to was not found")
        if row["role"] == "admin" and row["can_login"]:
            left = c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id<>?",
                             (uid,)).fetchone()[0]
            if not left:
                raise HTTPException(400, "ต้องมีผู้ดูแลระบบอย่างน้อยหนึ่งคน / at least one admin must remain")

        # note every job that is about to change hands, so a line can be written into
        # its own history afterwards — an admin who inherits work must be able to see
        # where it came from, and who to hand it on to
        touched = set()
        for col in ("created_by", "requester_id", "lead_tech", "approver_id"):
            try:
                touched |= {r["id"] for r in c.execute(f"SELECT id FROM jobs WHERE {col}=?", (uid,))}
            except Exception:
                pass
        moved = {}
        def hand_over(table, col, label):
            try:
                n = c.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (uid,)).fetchone()[0]
                if n:
                    c.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (target, uid))
                    moved[label] = moved.get(label, 0) + n
            except Exception:
                pass
        hand_over("jobs", "created_by", "jobs")
        hand_over("jobs", "requester_id", "jobs")
        hand_over("jobs", "lead_tech", "jobs_led")
        hand_over("jobs", "approver_id", "jobs_approved")
        hand_over("timelogs", "tech", "timelogs")
        hand_over("signoffs", "user_id", "signoffs")
        hand_over("job_events", "user_id", "events")
        hand_over("messages", "user_id", "messages")
        hand_over("activities", "user_id", "activities")
        try:                                        # the comma-separated lists
            for r in c.execute("SELECT id,helpers FROM jobs WHERE COALESCE(helpers,'')<>''").fetchall():
                nw = _swap_id_in_list(r["helpers"], uid, target)
                if nw != (r["helpers"] or ""):
                    c.execute("UPDATE jobs SET helpers=? WHERE id=?", (nw, r["id"]))
                    moved["jobs_helping"] = moved.get("jobs_helping", 0) + 1
        except Exception:
            pass
        try:
            for r in c.execute("SELECT id,members,lead FROM plan_teams").fetchall():
                nw = _swap_id_in_list(r["members"], uid, target)
                lead = target if r["lead"] == uid else r["lead"]
                if nw != (r["members"] or "") or lead != r["lead"]:
                    c.execute("UPDATE plan_teams SET members=?, lead=? WHERE id=?", (nw, lead, r["id"]))
                    moved["teams"] = moved.get("teams", 0) + 1
        except Exception:
            pass

        if touched:
            from .chat import log_job_event
            who = row["name"] or row["username"]
            for jid in sorted(touched):
                log_job_event(c, jid, u["id"],
                              f"⇄ ย้ายงานจาก {who} → {tgt['name']} (ลบผู้ใช้ โดย {u['name']}) / "
                              f"moved from {who} to {tgt['name']} — {who} was deleted by {u['name']}")
        c.execute("UPDATE users SET login_id=NULL WHERE login_id=?", (uid,))   # free anyone behind it
        try:
            c.execute("DELETE FROM push_subs WHERE user_id=?", (uid,))
        except Exception:
            pass
        c.execute("DELETE FROM users WHERE id=?", (uid,))
        c.commit()
    return {"ok": True, "name": row["name"], "username": row["username"],
            "moved_to": tgt["name"], "moved": moved}


@router.patch("/users/{uid}")
async def edit_user(uid: int, req: Request):
    require_role(req, "admin")
    b = await req.json()
    with closing(db()) as c:
        if b.get("password"):
            c.execute("UPDATE users SET password=? WHERE id=?", (hash_pw(b["password"]), uid))
        sets = {k: b[k] for k in ("name", "role", "active", "factory_id",
                                  "department", "can_login", "login_id", "can_report", "team",
                                  "shared_login") if k in b}
        if "shared_login" in sets:         # b406: department (shared) login tick
            sets["shared_login"] = 1 if sets["shared_login"] else 0
        if "team" in sets:                 # b392: only two answers exist
            sets["team"] = "CE" if sets["team"] == "CE" else ""
        if "can_report" in sets:
            sets["can_report"] = 1 if sets["can_report"] else 0
            try:                          # a signed-in phone picks it up without logging out
                from .auth import SESSIONS
                for _s in SESSIONS.values():
                    if _s.get("id") == uid:
                        _s["can_report"] = sets["can_report"]
            except Exception:
                pass
        # "all plants" is only for the roles that answer for all of them — checked here
        # too, not only in the dialog, because a role can be demoted in the same save
        if "factory_id" in sets:
            role = sets.get("role")
            if role is None:
                r = c.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
                role = r["role"] if r else ""
            sets["factory_id"] = _factory_for({"factory_id": sets["factory_id"]},
                                              {}, role)
        if sets:
            c.execute(f"UPDATE users SET {','.join(k+'=?' for k in sets)} WHERE id=?",
                      (*sets.values(), uid))
        c.commit()
    return {"ok": True}


# ── Symptoms ("what is wrong") ────────────────────────────────────────────────
# The list the operator's report form offers. It is the only place a fault gets a
# trade before a technician has looked at it, so the planner's routing and the TRADE
# column on the daily plan both come from here.

PCATS = ["Mechanical", "Electrical", "Instrument", "Process", "Other"]


@router.get("/problem-types")
async def list_problem_types(req: Request):
    u = require_role(req, "admin", "planner")
    fac = u.get("active_factory") or u.get("factory_id")
    with closing(db()) as c:
        from .db import ensure_problem_types
        ensure_problem_types(c)
        rows = [dict(r) for r in c.execute(
            "SELECT id,name,category,seq,active FROM problem_types"
            " WHERE factory_id=? ORDER BY seq,name", (fac,))]
        # The screen has always had a "Used" column and it has always read 0, because
        # nothing ever counted. An admin pruning a 34-entry list needs to know which
        # entries operators actually reach for — that is the whole basis for dropping
        # one. Counted by the text stored on the job, which is what a report saves.
        used = {}
        for r in c.execute(
                "SELECT j.problem_type nm, COUNT(*) n FROM jobs j"
                " LEFT JOIN machines m ON m.id=j.machine_id"
                " WHERE COALESCE(j.problem_type,'')<>'' AND (m.factory_id=? OR j.machine_id IS NULL)"
                " GROUP BY j.problem_type", (fac,)):
            used[r["nm"]] = r["n"]
    for r in rows:
        r["jobs"] = used.get(r["name"], 0)
    return {"types": rows, "categories": PCATS}


@router.post("/problem-types")
async def add_problem_type(req: Request):
    u = require_role(req, "admin")
    fac = u.get("active_factory") or u.get("factory_id")
    b = await req.json()
    name = (b.get("name") or "").strip()[:120]
    cat = b.get("category") if b.get("category") in PCATS else "Other"
    if not name:
        raise HTTPException(400, "name required")
    with closing(db()) as c:
        from .db import ensure_problem_types
        ensure_problem_types(c)
        dup = c.execute("SELECT id FROM problem_types WHERE factory_id=? AND name=?",
                        (fac, name)).fetchone()
        if dup:
            raise HTTPException(409, "that symptom is already on the list")
        seq = (c.execute("SELECT COALESCE(MAX(seq),0) FROM problem_types WHERE factory_id=?",
                         (fac,)).fetchone()[0] or 0) + 1
        c.execute("INSERT INTO problem_types(factory_id,name,category,seq,active)"
                  " VALUES(?,?,?,?,1)", (fac, name, cat, seq))
        c.commit()
    return {"ok": True}


@router.patch("/problem-types/{pid}")
async def edit_problem_type(req: Request, pid: int):
    u = require_role(req, "admin")
    fac = u.get("active_factory") or u.get("factory_id")
    b = await req.json()
    sets, args = [], []
    if "name" in b and (b.get("name") or "").strip():
        sets.append("name=?"); args.append((b["name"] or "").strip()[:120])
    if b.get("category") in PCATS:
        sets.append("category=?"); args.append(b["category"])
    if "active" in b:
        sets.append("active=?"); args.append(1 if b.get("active") else 0)
    if not sets:
        return {"ok": True}
    with closing(db()) as c:
        args += [pid, fac]
        c.execute("UPDATE problem_types SET %s WHERE id=? AND factory_id=?" % ",".join(sets), args)
        c.commit()
    return {"ok": True}


@router.delete("/problem-types/{pid}")
async def drop_problem_type(req: Request, pid: int):
    """Retire a symptom rather than delete it — jobs already reported name it, and a
    row that vanishes would make those jobs unreadable."""
    u = require_role(req, "admin")
    fac = u.get("active_factory") or u.get("factory_id")
    with closing(db()) as c:
        c.execute("UPDATE problem_types SET active=0 WHERE id=? AND factory_id=?", (pid, fac))
        c.commit()
    return {"ok": True}


# ── b427: a plant technician moves to Central Engineering (electrical) ─────────────
# Central Electrical technicians sign in as themselves (raja, techpc15 …): the LOGIN
# ACCOUNT is the technician, it carries team CE and covers every plant. A plant
# technician is a PERSON record booked through a phone login (กอล์ฟ as techpc7). Moving
# one to Central therefore moves him onto his own login: the account takes his name,
# team CE and all plants, and the person record is switched off so he is not offered on
# the plant boards as well. Refused when the login is shared — that phone is a crew's.
@router.post("/users/{uid}/to-central")
async def to_central(uid: int, req: Request):
    require_role(req, "admin")
    with closing(db()) as c:
        p = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not p or p["role"] != "technician":
            raise HTTPException(404, "ไม่พบช่าง / technician not found")
        if (p["can_login"] if p["can_login"] is not None else 1) != 0:
            # already a login account: just set the team
            c.execute("UPDATE users SET team='CE' WHERE id=?", (uid,))
            c.commit()
            return {"ok": True, "account": p["username"]}
        if not p["login_id"]:
            raise HTTPException(409, "ช่างคนนี้ยังไม่มีบัญชีโทรศัพท์ — สร้างบัญชีให้ก่อน / "
                                     "this person has no phone login yet — create an account for him first")
        acc = c.execute("SELECT * FROM users WHERE id=?", (p["login_id"],)).fetchone()
        n = c.execute("SELECT COUNT(*) n FROM users WHERE login_id=? AND active=1 AND id<>?",
                      (p["login_id"], uid)).fetchone()["n"]
        if n:
            raise HTTPException(409, f"บัญชี {acc['username']} ใช้ร่วมกับช่างอีก {n} คน — ย้ายไปส่วนกลางไม่ได้ / "
                                     f"login {acc['username']} is shared with {n} other technician(s) — "
                                     f"give him his own login first")
        c.execute("UPDATE users SET team='CE', factory_id=?, name=?, role='technician', active=1 WHERE id=?",
                  (p["factory_id"] or 0, p["name"], acc["id"]))      # b433: keeps his plant
        c.execute("UPDATE users SET active=0 WHERE id=?", (uid,))
        # b429/b430: his open work and the crews ahead move with him to his login
        from .elec import remap_person
        moved = remap_person(c, uid, acc["id"])
        c.commit()
        return {"ok": True, "account": acc["username"], "moved_jobs": moved}

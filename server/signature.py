"""b406 — a signature registered once, used at every sign point.

Personal logins register their own signature (☰ → My signature). Department logins —
one account a whole shift signs in with — never hold one: the person signing picks
their name from the people linked to that login and uses THEIR signature, or signs by
hand and the name they picked prints under it.

At a sign point the browser sends, instead of a drawing, one of
    "reg:0"      the signed-in person's own registered signature
    "reg:<id>"   the registered signature of a person linked to this login
and `resolve()` turns that into the stored picture. Only the server reads the file, so
nobody can send somebody else's signature: the id must be this login or a person
linked to it.
"""
import os, base64, time
from contextlib import closing

from fastapi import APIRouter, Request, HTTPException

from .config import UPLOADS
from .db import db, now
from .auth import user_from

router = APIRouter(prefix="/api")

SIG_DIR = os.path.join(UPLOADS, "sig")

# area / department accounts in the live database on 2 Oct 2026 (b406 seed, once)
SHARED_SEED = ("coating", "controlbfl", "digest", "extruder", "premix", "quality",
               "suppack", "packaging", "lab", "safety", "towerc", "warehouse",
               "autopackpc", "coatingpc", "controlpc", "extruderpc", "intakepc",
               "packingpc", "bflfphr1", "bflhr1", "bflpchr1")


def seed_shared_logins(c):
    n = 0
    for un in SHARED_SEED:
        n += c.execute("UPDATE users SET shared_login=1 WHERE username=? AND COALESCE(can_login,1)=1",
                       (un,)).rowcount
    c.commit()
    return n


def _me(c, u):
    r = c.execute("SELECT id,name,role,COALESCE(shared_login,0) shared,"
                  " COALESCE(sig_path,'') sig, COALESCE(sig_at,'') sig_at FROM users WHERE id=?",
                  (u["id"],)).fetchone()
    if not r:
        raise HTTPException(401, "login required")
    return dict(r)


def _people(c, uid):
    """People linked to this login — not the placeholder row named after the login
    itself ("coatingpc" the person, behind "coatingpc" the account), which is no one."""
    lg = c.execute("SELECT LOWER(username) un, LOWER(name) nm FROM users WHERE id=?", (uid,)).fetchone()
    skip = {lg["un"], lg["nm"]} if lg else set()
    return [dict(r) for r in c.execute(
        "SELECT id,name,role,COALESCE(sig_path,'') sig,COALESCE(sig_at,'') sig_at FROM users"
        " WHERE login_id=? AND COALESCE(active,1)=1 AND id<>? ORDER BY name", (uid, uid))
        if (r["name"] or "").strip().lower() not in skip]


def _person(c, u, pid):
    """The person `pid` may sign as on this login: the login itself (0 / own id) or a
    person linked to it. Anything else is refused."""
    pid = int(pid or 0)
    if not pid or pid == u["id"]:
        return _me(c, u)
    r = c.execute("SELECT id,name,role,COALESCE(sig_path,'') sig,COALESCE(sig_at,'') sig_at"
                  " FROM users WHERE id=? AND login_id=? AND COALESCE(active,1)=1",
                  (pid, u["id"])).fetchone()
    if not r:
        raise HTTPException(403, "เลือกได้เฉพาะชื่อที่ผูกกับบัญชีนี้ / you can only sign as a person linked to this login")
    return dict(r)


def _data_url(path):
    fp = os.path.join(os.path.dirname(UPLOADS), path.lstrip("/")) if path else ""
    if not fp or not os.path.exists(fp):
        return ""
    with open(fp, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()


def resolve(u, data, signer=None):
    """(picture data URL, person dict or None) for what the browser sent.
    A drawn picture passes through untouched; "reg:<id>" becomes the stored file."""
    data = data or ""
    with closing(db()) as c:
        if data.startswith("reg:"):
            p = _person(c, u, data[4:])
            if not p["sig"]:
                raise HTTPException(400, "ยังไม่ได้ลงทะเบียนลายเซ็น / no signature registered")
            url = _data_url(p["sig"])
            if not url:
                raise HTTPException(400, "ไม่พบไฟล์ลายเซ็น — กรุณาลงทะเบียนใหม่ / the registered signature file is missing — register it again")
            return url, (p if p["id"] != u["id"] else None)
        if signer:
            if isinstance(signer, str) and not signer.isdigit():     # typed name (no linked person)
                return data, {"id": None, "name": signer.strip()[:120]}
            p = _person(c, u, signer)
            return data, (p if p["id"] != u["id"] else None)
    return data, None


def record(c, jid, kind, u, person):
    """Write who actually signed and put it in the job's history."""
    if not person:
        return
    c.execute("DELETE FROM sig_signers WHERE job_id=? AND kind=?", (jid, kind))
    c.execute("INSERT INTO sig_signers(job_id,kind,person_id,person_name,login_id,created_at)"
              " VALUES(?,?,?,?,?,?)", (jid, kind, person.get("id"), person.get("name") or "", u["id"], now()))
    try:
        from .chat import log_job_event
        log_job_event(c, jid, u["id"], f"✍ ลงชื่อโดย {person.get('name')} (บัญชี {u.get('name')}) /"
                                       f" signed by {person.get('name')} on login {u.get('name')}")
    except Exception:
        pass


def signer_names(c, jid):
    try:
        return {r["kind"]: r["person_name"] for r in c.execute(
            "SELECT kind,person_name FROM sig_signers WHERE job_id=? ORDER BY id", (jid,))}
    except Exception:
        return {}


def _save_png(data, uid):
    if "," not in (data or ""):
        raise HTTPException(400, "bad signature")
    head, b64 = data.split(",", 1)
    raw = base64.b64decode(b64)
    if len(raw) < 200:
        raise HTTPException(400, "ลายเซ็นว่างเปล่า / the signature is empty")
    os.makedirs(SIG_DIR, exist_ok=True)
    fn = f"u{uid}_{int(time.time())}.png"
    with open(os.path.join(SIG_DIR, fn), "wb") as f:
        f.write(raw)
    return f"/uploads/sig/{fn}"


def _with_print(p):
    """Add `sigp`: the copy that prints (cut to the ink, pen-width line), so what the
    person sees on the phone is what goes on the paper."""
    p["sigp"] = p.get("sig") or ""
    if p["sigp"]:
        try:
            from .sigclear import clear_sig
            data = os.path.dirname(UPLOADS)
            fp = clear_sig(os.path.join(data, p["sig"].lstrip("/")))
            p["sigp"] = "/" + os.path.relpath(fp, data).replace(os.sep, "/")
        except Exception:
            pass
    return p


@router.get("/me/signature")
def my_signature(req: Request):
    u = user_from(req)
    with closing(db()) as c:
        me = _me(c, u)
        return {"shared": bool(me["shared"]), "me": _with_print(me),
                "people": [_with_print(x) for x in _people(c, u["id"])]}


@router.post("/me/signature")
async def set_my_signature(req: Request):
    """Register (or replace) a signature — your own, or, on a department login, the
    person's who is standing there and picked their name."""
    u = user_from(req)
    b = await req.json()
    with closing(db()) as c:
        me = _me(c, u)
        pid = int(b.get("person_id") or 0)
        if me["shared"] and (not pid or pid == u["id"]):
            raise HTTPException(400, "บัญชีแผนกเก็บลายเซ็นของคนเดียวไม่ได้ — เลือกชื่อก่อน /"
                                     " a department login cannot hold one person's signature — pick a name first")
        p = _person(c, u, pid)
        path = _save_png(b.get("data"), p["id"])
        c.execute("UPDATE users SET sig_path=?, sig_at=? WHERE id=?", (path, now(), p["id"]))
        c.commit()
        return {"ok": True, "sig": path, "id": p["id"]}


@router.post("/users/{uid}/signature/reset")
def reset_signature(uid: int, req: Request):
    """Admin: take a registered signature off (a bad one, someone who left)."""
    u = user_from(req)
    if u["role"] != "admin":
        raise HTTPException(403, "admin only")
    with closing(db()) as c:
        c.execute("UPDATE users SET sig_path='', sig_at='' WHERE id=?", (uid,))
        c.commit()
    return {"ok": True}

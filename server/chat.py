import os, re, base64
from contextlib import closing

from fastapi import APIRouter, Request, HTTPException

from .config import UPLOADS
from .db import db, now
from .auth import user_from
from .push import notify_users

router = APIRouter(prefix="/api/chat")


@router.get("/channels")
async def channels(req: Request):
    user_from(req)
    with closing(db()) as c:
        return [dict(r) for r in c.execute("""
            SELECT c.*,
              (SELECT COUNT(*) FROM messages m WHERE m.channel_id=c.id) AS cnt,
              (SELECT MAX(created_at) FROM messages m WHERE m.channel_id=c.id) AS last
            FROM channels c ORDER BY c.kind DESC, c.id""")]


@router.get("/messages")
async def messages(req: Request, channel_id: int = 0, job_id: int = 0, parent_id: int = 0):
    user_from(req)
    with closing(db()) as c:
        if parent_id:
            rows = c.execute("""SELECT m.*, u.name author_name, 0 AS replies
                FROM messages m JOIN users u ON u.id=m.author
                WHERE m.parent_id=? ORDER BY m.id""", (parent_id,)).fetchall()
            parent = c.execute("""SELECT m.*, u.name author_name FROM messages m
                JOIN users u ON u.id=m.author WHERE m.id=?""", (parent_id,)).fetchone()
            return {"parent": dict(parent) if parent else None,
                    "messages": [dict(r) for r in rows]}
        if job_id:
            rows = c.execute("""SELECT m.*, u.name author_name, 0 AS replies
                FROM messages m JOIN users u ON u.id=m.author
                WHERE m.job_id=? AND m.channel_id IS NULL ORDER BY m.id""", (job_id,)).fetchall()
        elif channel_id:
            rows = c.execute("""SELECT m.*, u.name author_name,
                (SELECT COUNT(*) FROM messages r WHERE r.parent_id=m.id) AS replies
                FROM messages m JOIN users u ON u.id=m.author
                WHERE m.channel_id=? AND m.parent_id IS NULL
                ORDER BY m.id DESC LIMIT 60""", (channel_id,)).fetchall()
            rows = list(rows)[::-1]
        else:
            raise HTTPException(400, "channel_id, job_id or parent_id required")
        return {"messages": [dict(r) for r in rows]}


@router.post("/messages")
async def post_message(req: Request):
    u = user_from(req)
    b = await req.json()
    text = (b.get("text") or "").strip()
    img_data = b.get("img") or ""
    if not text and not img_data:
        raise HTTPException(400, "empty message")
    channel_id = b.get("channel_id") or None
    job_id = b.get("job_id") or None
    parent_id = b.get("parent_id") or None
    kind = b.get("kind") if b.get("kind") in ("message", "note") else "message"
    with closing(db()) as c:
        mid = c.insert_id("""INSERT INTO messages(channel_id,job_id,parent_id,author,text,kind,created_at)
            VALUES(?,?,?,?,?,?,?)""", (channel_id, job_id, parent_id, u["id"], text, kind, now()))
        img_path = ""
        if img_data and "," in img_data:
            head, b64 = img_data.split(",", 1)
            ext = "png" if "png" in head else "jpg"
            fn = f"chat_{mid}.{ext}"
            with open(os.path.join(UPLOADS, fn), "wb") as f:
                f.write(base64.b64decode(b64))
            img_path = f"/uploads/{fn}"
            c.execute("UPDATE messages SET img=? WHERE id=?", (img_path, mid))
        c.commit()

        # @mention push (notes are silent — Odoo "Log note" behaviour)
        mentioned = [] if kind == "note" else re.findall(r"@([A-Za-z0-9_]+)", text)
        ids = []
        if mentioned:
            qs = ",".join("?" * len(mentioned))
            ids = [r["id"] for r in c.execute(
                f"SELECT id FROM users WHERE username IN ({qs}) AND id != ?",
                (*mentioned, u["id"]))]
            notify_users(ids, f"💬 {u['name']} กล่าวถึงคุณ", text[:90], "/")
        # job discussion push to participants
        if job_id and not parent_id and kind == "message":
            j = c.execute("SELECT jobid, lead_tech, helpers, created_by FROM jobs WHERE id=?",
                          (job_id,)).fetchone()
            if j:
                parts = {j["lead_tech"], j["created_by"],
                         *[int(h) for h in (j["helpers"] or "").split(",") if h]}
                parts.discard(None); parts.discard(u["id"])
                parts -= set(ids)
                notify_users(list(parts), f"💬 {j['jobid']} — {u['name']}", text[:90], "/")
        row = c.execute("""SELECT m.*, u.name author_name FROM messages m
            JOIN users u ON u.id=m.author WHERE m.id=?""", (mid,)).fetchone()
    return dict(row)


def post_system(c, channel_kind, text, job_id=None, author=1):
    ch = c.execute("SELECT id FROM channels WHERE kind=? LIMIT 1", (channel_kind,)).fetchone()
    if ch:
        c.execute("""INSERT INTO messages(channel_id,job_id,author,text,kind,created_at)
            VALUES(?,?,?,?,'system',?)""", (ch["id"], job_id, author, text, now()))


def log_job_event(c, job_id, author, text):
    """Odoo-style system line in the job timeline (status changes etc.)."""
    c.execute("""INSERT INTO messages(job_id,author,text,kind,created_at)
        VALUES(?,?,?,'system',?)""", (job_id, author, text, now()))


# ---------------- activities (Odoo-style scheduled activities) ----------------

@router.get("/activities")
async def activities(req: Request, job_id: int = 0, mine: int = 0):
    u = user_from(req)
    q = """SELECT a.*, ua.name assignee_name, uc.name creator_name, j.jobid
           FROM activities a LEFT JOIN users ua ON ua.id=a.assignee
           LEFT JOIN users uc ON uc.id=a.created_by
           LEFT JOIN jobs j ON j.id=a.job_id WHERE a.done=0"""
    args = []
    if job_id:
        q += " AND a.job_id=?"; args.append(job_id)
    if mine:
        q += " AND a.assignee=?"; args.append(u["id"])
    q += " ORDER BY a.due_date"
    with closing(db()) as c:
        return [dict(r) for r in c.execute(q, args)]


@router.post("/activities")
async def add_activity(req: Request):
    u = user_from(req)
    b = await req.json()
    if not b.get("summary") or not b.get("assignee"):
        raise HTTPException(400, "summary and assignee required")
    with closing(db()) as c:
        c.execute("""INSERT INTO activities(job_id,act_type,summary,assignee,due_date,
            created_by,created_at) VALUES(?,?,?,?,?,?,?)""",
            (b.get("job_id"), b.get("act_type", "todo"), b["summary"], b["assignee"],
             b.get("due_date"), u["id"], now()))
        if b.get("job_id"):
            log_job_event(c, b["job_id"], u["id"],
                          f"📌 กิจกรรม: {b.get('act_type','todo')} — {b['summary']} "
                          f"(มอบให้ @{b['assignee']} ภายใน {b.get('due_date','')})")
        c.commit()
    notify_users([b["assignee"]], "📌 กิจกรรมใหม่ถึงคุณ",
                 f"{b.get('act_type','todo')}: {b['summary'][:80]}")
    return {"ok": True}


@router.patch("/activities/{aid}")
async def done_activity(aid: int, req: Request):
    u = user_from(req)
    with closing(db()) as c:
        a = c.execute("SELECT * FROM activities WHERE id=?", (aid,)).fetchone()
        if not a:
            raise HTTPException(404, "no activity")
        c.execute("UPDATE activities SET done=1 WHERE id=?", (aid,))
        if a["job_id"]:
            log_job_event(c, a["job_id"], u["id"], f"✅ กิจกรรมเสร็จ: {a['summary']}")
        c.commit()
    return {"ok": True}

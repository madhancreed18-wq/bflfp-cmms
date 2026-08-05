import os, json, base64, threading
from contextlib import closing

from fastapi import APIRouter, Request

from .config import VAPID_PEM, PUSH_CONTACT
from .db import db
from .auth import user_from

router = APIRouter(prefix="/api/push")

try:
    from pywebpush import webpush, WebPushException
    from py_vapid import Vapid
    from cryptography.hazmat.primitives import serialization
    PUSH_OK = True
except ImportError:
    PUSH_OK = False

VAPID_PUB = None
if PUSH_OK:
    v = Vapid()
    if os.path.exists(VAPID_PEM):
        v = Vapid.from_file(VAPID_PEM)
    else:
        v.generate_keys()
        v.save_key(VAPID_PEM)
    _raw = v.public_key.public_bytes(serialization.Encoding.X962,
                                     serialization.PublicFormat.UncompressedPoint)
    VAPID_PUB = base64.urlsafe_b64encode(_raw).rstrip(b"=").decode()


def notify_users(user_ids, title, body, url="/"):
    if not (PUSH_OK and user_ids):
        return

    def run():
        with closing(db()) as c:
            subs = c.execute(
                f"SELECT id, sub FROM push_subs WHERE user_id IN ({','.join('?' * len(user_ids))})",
                list(user_ids)).fetchall()
            dead = []
            for s in subs:
                try:
                    webpush(json.loads(s["sub"]),
                            json.dumps({"title": title, "body": body, "url": url}),
                            vapid_private_key=VAPID_PEM,
                            vapid_claims={"sub": PUSH_CONTACT})
                except WebPushException as e:
                    if e.response is not None and e.response.status_code in (404, 410):
                        dead.append(s["id"])
                except Exception:
                    pass
            for i in dead:
                c.execute("DELETE FROM push_subs WHERE id=?", (i,))
            c.commit()

    threading.Thread(target=run, daemon=True).start()


@router.get("/key")
async def push_key(req: Request):
    user_from(req)
    return {"enabled": PUSH_OK, "key": VAPID_PUB}


@router.post("/subscribe")
async def push_subscribe(req: Request):
    u = user_from(req)
    b = await req.json()
    sub = json.dumps(b.get("subscription"))
    with closing(db()) as c:
        ex = c.execute("SELECT id FROM push_subs WHERE sub=?", (sub,)).fetchone()
        if ex:
            c.execute("UPDATE push_subs SET user_id=? WHERE id=?", (u["id"], ex["id"]))
        else:
            c.execute("INSERT INTO push_subs(user_id, sub) VALUES(?,?)", (u["id"], sub))
        c.commit()
    return {"ok": True}

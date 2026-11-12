import os, json, base64, threading
from contextlib import closing

from fastapi import APIRouter, Request

from .config import VAPID_PEM, PUSH_CONTACT, IS_LIVE, ENV
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

# Push works on the test copy too — you cannot test notifications without it.
# It cannot reach a real technician by accident: subscriptions live in the data
# folder, so a sandbox only ever pushes to devices that subscribed ON the sandbox,
# and refresh_dev.py wipes the live subscriptions it copies in. Anything sent from
# a non-live copy is labelled, so a stray notification is obvious on the phone.
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


def _endpoint(sub_str):
    """Stable per-device id inside a stored subscription — used as the dedup key."""
    try:
        return json.loads(sub_str).get("endpoint", "")
    except Exception:
        return ""


def notify_users(user_ids, title, body, url="/"):
    if not (PUSH_OK and user_ids):
        return
    if not IS_LIVE:                      # unmistakable on the lock screen
        title = f"[{ENV.upper()}] {title}"

    def run():
        with closing(db()) as c:
            subs = c.execute(
                f"SELECT id, sub FROM push_subs WHERE user_id IN ({','.join('?' * len(user_ids))})",
                list(user_ids)).fetchall()
            dead, seen = [], set()
            for s in subs:
                ep = _endpoint(s["sub"])
                if ep and ep in seen:          # same device already sent — no double notify
                    continue
                seen.add(ep)
                try:
                    # Urgency matters more than it looks. Without it the push goes out
                    # at normal priority, and Apple and Google are free to sit on it and
                    # deliver with the next batch to save the phone's battery — which is
                    # why a breakdown alert arrived minutes after the job. A machine is
                    # down: this is the one case that is genuinely urgent. TTL gives it
                    # five minutes to find a phone that is briefly offline, instead of
                    # being dropped the moment it cannot be delivered.
                    webpush(json.loads(s["sub"]),
                            json.dumps({"title": title, "body": body, "url": url}),
                            vapid_private_key=VAPID_PEM,
                            vapid_claims={"sub": PUSH_CONTACT},
                            ttl=300, timeout=10,
                            headers={"Urgency": "high"})
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
    return {"enabled": PUSH_OK, "key": VAPID_PUB, "env": ENV}


@router.post("/subscribe")
async def push_subscribe(req: Request):
    u = user_from(req)
    b = await req.json()
    subobj = b.get("subscription") or {}
    endpoint = subobj.get("endpoint", "")
    sub = json.dumps(subobj)
    with closing(db()) as c:
        if endpoint:                           # drop any existing rows for THIS device first
            for r in c.execute("SELECT id, sub FROM push_subs").fetchall():
                if _endpoint(r["sub"]) == endpoint:
                    c.execute("DELETE FROM push_subs WHERE id=?", (r["id"],))
        c.execute("INSERT INTO push_subs(user_id, sub) VALUES(?,?)", (u["id"], sub))
        c.commit()
    return {"ok": True}


@router.post("/test")
async def push_test(req: Request):
    u = user_from(req)
    with closing(db()) as c:
        n = c.execute("SELECT COUNT(*) FROM push_subs WHERE user_id=?", (u["id"],)).fetchone()[0]
    notify_users([u["id"]], "🔔 Test — BFLFP CMMS",
                 "Notifications are working. You'll be alerted on new job requests.")
    return {"enabled": PUSH_OK, "devices": n}

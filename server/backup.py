"""System settings › Backup (b573) — one-click backup of the live server, for the admin.

A backup is ONE zip file in data/backups/:
    cmms-backup-YYYYMMDD-HHMMSS.zip
      cmms.db                  every CMMS + FG Stock table (copied with SQLite's online backup,
                               so it is safe while people are working)
      fg_access_archive.db     the old Access rows (when the file exists)
      uploads/  Report/        photos, signatures, filed reports
      pm_plan.json, vapid_private.pem   settings the server needs to come back the same
      RESTORE.txt              how to put it back

- Make one now (button), or every night at a set hour (System settings › Backup).
- Keeps the newest N zips (default 14) and deletes older ones.
- Optional second folder (a USB disk, NAS or network share as the server PC sees it):
  each new backup is also copied there, so one disk failing does not take both.
- Download any zip to your own PC from the same page.
Only for the SQLite set-up (the normal one). With DATABASE_URL = PostgreSQL use pg_dump.
"""
import asyncio
import datetime
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import zipfile

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import FileResponse

from .auth import require_role, user_from
from .config import DATA

router = APIRouter(prefix="/api/admin/backup")
_log = logging.getLogger("cmms")

BDIR = os.path.join(DATA, "backups")
SETF = os.path.join(BDIR, "settings.json")
NAME = re.compile(r"^cmms-backup-\d{8}-\d{6}(-auto)?\.zip$")
DBS = ("cmms.db", "fg_access_archive.db")
FOLDERS = ("uploads", "Report")
FILES = ("pm_plan.json", "vapid_private.pem")
DEFAULTS = {"auto": False, "hour": 2, "keep": 14, "copy_to": ""}

_lock = threading.Lock()
STATE = {"running": False, "step": "", "started": "", "by": "", "last": "", "last_error": "", "last_ok": ""}


def _settings():
    s = dict(DEFAULTS)
    try:
        with open(SETF, encoding="utf-8") as f:
            s.update(json.load(f) or {})
    except Exception:
        pass
    s["hour"] = max(0, min(23, int(s.get("hour") or 0)))
    s["keep"] = max(1, min(90, int(s.get("keep") or 14)))
    s["copy_to"] = str(s.get("copy_to") or "").strip()
    s["auto"] = bool(s.get("auto"))
    return s


def _save_settings(s):
    os.makedirs(BDIR, exist_ok=True)
    with open(SETF, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1)


def _sqlite_mode():
    url = os.environ.get("DATABASE_URL") or ""
    return not url or url.startswith("sqlite")


def _snapshot(src_path, dst_path):
    """A consistent copy of a live SQLite file (online backup API — readers and writers keep working)."""
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=60)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst, pages=4096, sleep=0.01)
    finally:
        dst.close()
        src.close()


RESTORE = """BFL Smart Factory (CMMS + FG Stock) — backup made {at} by {by}

HOW TO RESTORE
1. Stop the CMMS server (close the "Start CMMS" window).
2. Rename the server's current  data\\cmms.db  to  cmms.db.old  (keep it until you are sure).
3. Copy cmms.db from this zip into the data folder. Do the same for fg_access_archive.db
   and the uploads / Report folders and the two settings files if you need them back.
4. Start the server again ("Start CMMS.bat") and sign in.

Files in this zip: {files}
"""


def make_backup(by="", auto=False):
    """Build one backup zip. Returns its file name. Raises on failure (and records it)."""
    if not _sqlite_mode():
        raise RuntimeError("This server uses PostgreSQL (DATABASE_URL) — back it up with pg_dump instead.")
    if not _lock.acquire(blocking=False):
        raise RuntimeError("A backup is already running")
    t0 = time.time()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"cmms-backup-{stamp}{'-auto' if auto else ''}.zip"
    os.makedirs(BDIR, exist_ok=True)
    tmpdir = os.path.join(BDIR, f".work-{stamp}")
    part = os.path.join(BDIR, name + ".part")
    STATE.update(running=True, step="starting", started=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 by=by or ("auto" if auto else ""), last_error="")
    try:
        need = sum(os.path.getsize(os.path.join(DATA, d)) for d in DBS if os.path.exists(os.path.join(DATA, d)))
        if shutil.disk_usage(BDIR).free < need * 2 + 200 * 1024 * 1024:
            raise RuntimeError(f"Not enough free disk space for a backup (need about {need * 2 // 1048576:,} MB)")
        os.makedirs(tmpdir, exist_ok=True)
        files = []
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, allowZip64=True, compresslevel=6) as z:
            for d in DBS:
                p = os.path.join(DATA, d)
                if not os.path.exists(p):
                    continue
                STATE["step"] = f"copying {d}"
                snap = os.path.join(tmpdir, d)
                _snapshot(p, snap)
                STATE["step"] = f"zipping {d}"
                z.write(snap, d)
                os.remove(snap)
                files.append(d)
            for folder in FOLDERS:
                root = os.path.join(DATA, folder)
                if not os.path.isdir(root):
                    continue
                STATE["step"] = f"adding {folder}/"
                for dp, _dn, fn in os.walk(root):
                    for f in fn:
                        full = os.path.join(dp, f)
                        try:
                            z.write(full, os.path.relpath(full, DATA))
                        except (OSError, ValueError):
                            pass           # a file in use / vanished — the database is what matters
                files.append(folder + "/")
            for f in FILES:
                p = os.path.join(DATA, f)
                if os.path.exists(p):
                    z.write(p, f)
                    files.append(f)
            z.writestr("RESTORE.txt", RESTORE.format(at=STATE["started"], by=STATE["by"] or "-", files=", ".join(files)))
        os.replace(part, os.path.join(BDIR, name))
        s = _settings()
        if s["copy_to"]:
            STATE["step"] = "copying to the second folder"
            try:
                os.makedirs(s["copy_to"], exist_ok=True)
                shutil.copy2(os.path.join(BDIR, name), os.path.join(s["copy_to"], name))
                _prune(s["copy_to"], s["keep"])
            except Exception as e:     # the backup itself is fine — say so, but keep it
                STATE["last_error"] = f"Backup made, but copying to {s['copy_to']} failed: {e}"
                _log.error("[backup] copy to %s failed: %s", s["copy_to"], e)
        _prune(BDIR, s["keep"])
        STATE["last_ok"] = name
        _log.info("[backup] %s made in %.0f s by %s", name, time.time() - t0, STATE["by"] or "-")
        return name
    except Exception as e:
        STATE["last_error"] = str(e)
        _log.error("[backup] failed: %s", e)
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        STATE.update(running=False, step="", last=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        _lock.release()


def _prune(folder, keep):
    zs = sorted((f for f in os.listdir(folder) if NAME.match(f)), reverse=True)
    for f in zs[keep:]:
        try:
            os.remove(os.path.join(folder, f))
        except OSError:
            pass


def _list():
    if not os.path.isdir(BDIR):
        return []
    out = []
    for f in sorted(os.listdir(BDIR), reverse=True):
        if NAME.match(f):
            st = os.stat(os.path.join(BDIR, f))
            out.append({"name": f, "size": st.st_size, "at": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                        "auto": f.endswith("-auto.zip")})
    return out


def _admin(req):
    return require_role(req, "admin")


@router.get("")
async def backup_info(req: Request):
    _admin(req)
    dbp = os.path.join(DATA, "cmms.db")
    os.makedirs(BDIR, exist_ok=True)
    return {"files": _list(), "state": STATE, "settings": _settings(), "folder": BDIR, "sqlite": _sqlite_mode(),
            "db_size": os.path.getsize(dbp) if os.path.exists(dbp) else 0, "free": shutil.disk_usage(BDIR).free}


@router.post("")
async def backup_now(req: Request):
    u = _admin(req)
    if STATE["running"]:
        raise HTTPException(409, {"msg": "กำลังสำรองข้อมูลอยู่ (a backup is already running)"})
    if not _sqlite_mode():
        raise HTTPException(400, {"msg": "This server uses PostgreSQL — back it up with pg_dump."})
    who = u.get("name") or u.get("username") or ""

    def run():
        try:
            make_backup(who)
        except Exception:
            pass                    # recorded in STATE["last_error"]
    threading.Thread(target=run, daemon=True).start()
    await asyncio.sleep(0.2)
    return {"ok": True, "state": STATE}


@router.post("/settings")
async def backup_settings(req: Request):
    _admin(req)
    b = await req.json()
    s = _settings()
    if "auto" in b:
        s["auto"] = bool(b["auto"])
    if "hour" in b:
        s["hour"] = max(0, min(23, int(b.get("hour") or 0)))
    if "keep" in b:
        s["keep"] = max(1, min(90, int(b.get("keep") or 14)))
    if "copy_to" in b:
        p = str(b.get("copy_to") or "").strip()
        if p:
            if not os.path.isabs(p):
                raise HTTPException(400, {"msg": "ใส่ path เต็ม เช่น D:\\Backup หรือ \\\\NAS\\share\\cmms (use a full path)"})
            try:
                os.makedirs(p, exist_ok=True)
                tf = os.path.join(p, ".cmms-write-test")
                with open(tf, "w") as f:
                    f.write("ok")
                os.remove(tf)
            except Exception as e:
                raise HTTPException(400, {"msg": f"เขียนลงโฟลเดอร์นี้ไม่ได้ (cannot write to this folder from the server): {e}"})
        s["copy_to"] = p
    _save_settings(s)
    return {"ok": True, "settings": s}


@router.get("/file/{name}")
async def backup_download(req: Request, name: str):
    _admin(req)
    p = os.path.join(BDIR, name)
    if not NAME.match(name) or not os.path.isfile(p):
        raise HTTPException(404, {"msg": "not found"})
    return FileResponse(p, media_type="application/zip", filename=name)


@router.delete("/file/{name}")
async def backup_delete(req: Request, name: str):
    _admin(req)
    p = os.path.join(BDIR, name)
    if not NAME.match(name) or not os.path.isfile(p):
        raise HTTPException(404, {"msg": "not found"})
    os.remove(p)
    return {"ok": True}


def _seconds_to(hour):
    n = datetime.datetime.now()
    t = n.replace(hour=hour, minute=0, second=0, microsecond=0)
    if t <= n:
        t += datetime.timedelta(days=1)
    return (t - n).total_seconds()


@router.on_event("startup")
async def _nightly():
    """Every night at the set hour, when 'Automatic backup' is on. Checks the setting each time,
    so switching it on / off or changing the hour needs no restart."""
    if os.environ.get("CMMS_AUTO_BACKUP", "1") == "0":
        return

    async def loop():
        while True:
            await asyncio.sleep(min(_seconds_to(_settings()["hour"]), 900) + 1)
            s = _settings()
            now = datetime.datetime.now()
            if not s["auto"] or now.hour != s["hour"]:
                continue
            if any(f["auto"] and f["name"][12:20] == now.strftime("%Y%m%d") for f in _list()):
                continue                        # already made one tonight
            try:
                await asyncio.to_thread(make_backup, "", True)
            except Exception:
                pass
    asyncio.create_task(loop())

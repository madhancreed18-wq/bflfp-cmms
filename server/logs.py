"""Error log — one place to look when something goes wrong.

Three sources land in the same file (`data/logs/cmms.log`, rotating, 5 x 2 MB):

  * server exceptions — every unhandled error, with the request path and who was
    signed in when it happened;
  * browser errors — a technician's phone posts its JS errors here, so a crash
    on a device you cannot inspect still shows up;
  * anything the code logs itself via `log = logging.getLogger("cmms")`.

Read it in the app at Manage → Logs, or open the file directly.
"""
import json
import logging
import os
import time
import traceback
from logging.handlers import RotatingFileHandler

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from .config import DATA
from .auth import require_role, SESSIONS

router = APIRouter(prefix="/api")

LOG_DIR = os.path.join(DATA, "logs")
LOG_FILE = os.path.join(LOG_DIR, "cmms.log")
os.makedirs(LOG_DIR, exist_ok=True)

log = logging.getLogger("cmms")


def setup(level="INFO"):
    """Attach the rotating file handler once; uvicorn's own logs join it too."""
    if getattr(setup, "_done", False):
        return log
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    log.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    log.addHandler(fh)
    log.propagate = False
    for name in ("uvicorn.error", "uvicorn.access"):          # keep server errors in the file too
        lg = logging.getLogger(name)
        if not any(isinstance(h, RotatingFileHandler) for h in lg.handlers):
            lg.addHandler(fh)
    setup._done = True
    log.info("--- log started (level=%s) ---", level)
    return log


def _who(req: Request):
    try:
        u = SESSIONS.get(req.cookies.get("bflfp"))
        return f"{u['username']}/{u['role']}" if u else "anonymous"
    except Exception:
        return "?"


# Refusals worth writing down. 404 on a static file and the 401 the login screen asks
# for on purpose are noise; everything else is the server telling somebody NO, and the
# reason is the only thing that explains a screen where nothing happened.
_QUIET_REFUSALS = {"/api/me", "/api/pulse", "/api/health"}


def install(app):
    """Record every unhandled exception — and every refusal — instead of losing them.

    An HTTPException is HANDLED: FastAPI turns it into a 403 or a 409 and it never
    reaches the middleware below, so until now a refusal existed only as a uvicorn
    access line — a status code, no message, at INFO level, under a log screen that
    filters for ERROR. "The technician says he cannot start any job and there is
    nothing in the log" was exactly that: the app was working as designed and saying
    so to nobody. The reason is now written down, with who was asking.
    """
    from starlette.exceptions import HTTPException as _HTTPExc
    from fastapi.exception_handlers import http_exception_handler as _default

    @app.exception_handler(_HTTPExc)
    async def _log_refusal(request: Request, exc: _HTTPExc):
        try:
            path = request.url.path
            noisy = (exc.status_code == 404
                     or (exc.status_code == 401 and path in _QUIET_REFUSALS))
            if path.startswith("/api/") and not noisy:
                detail = exc.detail
                if isinstance(detail, dict):
                    detail = detail.get("msg") or detail
                (log.error if exc.status_code >= 500 else log.warning)(
                    "REFUSED %s  %s %s  user=%s  %s", exc.status_code, request.method,
                    path, _who(request), str(detail)[:400])
        except Exception:
            pass                       # logging must never be the thing that breaks a reply
        return await _default(request, exc)

    @app.middleware("http")
    async def _catch(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception:
            # A REFERENCE, printed in the log and handed to the screen. "It failed" is
            # not a report anybody can act on; "it says ERR-4F2A" is one search in
            # Manage → Logs. The person gets a message and someone to call instead of
            # a blank 500 the phone shows as nothing at all.
            import uuid
            ref = "ERR-" + uuid.uuid4().hex[:4].upper()
            log.error("UNHANDLED %s  %s %s  user=%s\n%s", ref, request.method,
                      request.url.path, _who(request), traceback.format_exc())
            if request.url.path.startswith("/api/"):
                from fastapi.responses import JSONResponse
                return JSONResponse(status_code=500, content={"detail":
                    f"ระบบขัดข้อง / Something went wrong on the server. {ref} · "
                    f"ติดต่อแผนก IT / Contact IT Department"})
            raise
    return app


# a phone that keeps erroring must not be able to fill the disk
_recent, _WINDOW = {}, 60.0
_REF = __import__("re").compile(r"\bERR-[A-Z0-9]{4}\b\s*")


@router.post("/log/client")
async def client_log(req: Request):
    """A browser reporting its own error. Deliberately open to any signed-in role."""
    try:
        b = await req.json()
    except Exception:
        return {"ok": False}
    msg = str(b.get("message") or "")[:400]
    if not msg:
        return {"ok": False}
    # The flood guard below compares messages, and every API failure begins with its own
    # freshly-minted ERR-XXXX reference — so no two messages were ever equal and the
    # guard has never once matched. tech2 pressing Start on a job that was already
    # running wrote the SAME refusal seven times inside one second. The reference is
    # what makes each report findable, so it stays in the log; it just must not be part
    # of what decides whether this is the same error as the last one.
    key = _REF.sub("", msg)[:120]
    now = time.time()
    for k, t in list(_recent.items()):                        # forget old entries
        if now - t > _WINDOW:
            _recent.pop(k, None)
    if key in _recent:                                        # same error again within a minute
        return {"ok": True, "deduped": True}
    _recent[key] = now
    log.error("BROWSER  user=%s  page=%s\n  %s\n  %s\n  ua=%s", _who(req),
              str(b.get("url") or "")[:200], msg,
              str(b.get("stack") or "")[:1500], str(b.get("ua") or "")[:200])
    return {"ok": True}


@router.get("/admin/logs")
async def read_logs(req: Request, lines: int = 300, level: str = "", q: str = ""):
    """Tail of the log for the in-app viewer."""
    require_role(req, "admin", "planner", "manager")
    lines = max(1, min(int(lines or 300), 3000))
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except FileNotFoundError:
        return {"file": LOG_FILE, "lines": [], "total": 0}
    rows = [ln.rstrip("\r\n") for ln in all_lines]      # written CRLF on Windows
    if level:
        want = level.upper()
        keep, hit = [], False
        for ln in rows:                                       # keep a record's continuation lines
            if any(f" {lv:<7} " in ln for lv in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")):
                hit = f" {want:<7} " in ln or (want == "ERROR" and " CRITICAL " in ln)
            if hit:
                keep.append(ln)
        rows = keep
    if q:
        ql = q.lower()
        rows = [ln for ln in rows if ql in ln.lower()]
    return {"file": LOG_FILE, "total": len(rows), "lines": rows[-lines:]}


@router.get("/admin/logs/download")
async def download_logs(req: Request):
    require_role(req, "admin")
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            body = f.read()
    except FileNotFoundError:
        body = ""
    return PlainTextResponse(body, headers={
        "Content-Disposition": 'attachment; filename="cmms-log.txt"'})


@router.delete("/admin/logs")
async def clear_logs(req: Request):
    """Start a clean log — useful right before reproducing a problem."""
    u = require_role(req, "admin")
    try:
        open(LOG_FILE, "w", encoding="utf-8").close()
    except Exception as e:
        log.warning("could not clear the log: %s", e)
    log.info("--- log cleared by %s ---", u.get("username"))
    return {"ok": True}

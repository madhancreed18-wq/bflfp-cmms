"""b474 — System health: is the CMMS slow, failing or hanging, and who is it happening to?

Three sources, one admin page (Manage → System health), and push alerts to admins.

  Server   every API call is timed. Slow calls (≥ 3 s) and server errors are written to
           perf_log with the user and plant; everything is counted per hour (perf_hourly)
           and per endpoint. A watchdog measures how late the server's own clock tick
           runs — when the server itself stalls, that shows as "server lag".
  Phones   the page reports what only the phone can see: a request still waiting after
           10 s (hang), a request that took long over a bad Wi-Fi, a screen whose script
           froze (the page stopped responding), a page that took long to open.
  Machine  disk free, database size, memory/CPU (when psutil is installed), up-time.

Alerts (push to every admin's phone, at most once per 30 min per kind):
  errors ≥ 5 in 10 min · slow calls ≥ 10 in 10 min · hangs/freezes ≥ 3 in 10 min ·
  server lag ≥ 5 s · disk < 10 % or < 5 GB free · the app (re)started.

perf_log is kept 30 days in the database (b543, was 90); perf_hourly 400 days. Older rows are
not deleted: they are moved to monthly CSV files in data/Report/log-archive/ (b544).
Every night at 03:00 the database is tidied (PRAGMA optimize + WAL checkpoint); on Sunday
night it is also compacted (VACUUM) so deleted rows give their space back.
"""
import asyncio, json, logging, os, re, shutil, threading, time
from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timedelta

from fastapi import APIRouter, Request, HTTPException

from .config import DATA
from .db import db

_log = logging.getLogger("cmms")
router = APIRouter(prefix="/api/monitor")

SLOW_MS = 3000
STARTED = datetime.now()
SKIP = ("/api/pulse", "/api/monitor/", "/api/log/client", "/api/health")

_lock = threading.Lock()
_hour = {}                         # "YYYY-MM-DD HH" -> {"n","err","slow","ms":[...]}
_ep = defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0, 0]))   # hour -> path -> [n,sum,max,slow,err]
_seen = {}                         # uid -> {name, role, fac, at, page}
_events = deque(maxlen=2000)       # (ts, kind) for alert windows
_alerted = {}                      # kind -> ts
_ID = re.compile(r"/\d+(?=/|$)")


def _norm(path):
    return _ID.sub("/{id}", path)[:120]


def _ensure(c):
    c.execute("""CREATE TABLE IF NOT EXISTS perf_log(id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT, kind TEXT, path TEXT, ms INTEGER, status INTEGER, uid INTEGER, who TEXT,
        role TEXT, fac INTEGER, page TEXT, detail TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_perf_at ON perf_log(at)")
    c.execute("""CREATE TABLE IF NOT EXISTS perf_hourly(hour TEXT PRIMARY KEY, n INTEGER, err INTEGER,
        slow INTEGER, p50 INTEGER, p95 INTEGER, mx INTEGER)""")


def _write(kind, path="", ms=0, status=0, u=None, page="", detail=""):
    u = u or {}
    try:
        with closing(db()) as c:
            _ensure(c)
            c.execute("INSERT INTO perf_log(at,kind,path,ms,status,uid,who,role,fac,page,detail)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), kind, path[:160], int(ms or 0),
                       int(status or 0), u.get("id"), (u.get("name") or u.get("username") or "")[:60],
                       u.get("role") or "", u.get("active_factory") or u.get("factory_id") or None,
                       (page or "")[:80], (detail or "")[:400]))
            c.commit()
    except Exception as e:
        _log.warning("[monitor] write failed: %s", e)
    with _lock:
        _events.append((time.time(), kind))


# ---------------------------------------------------------------- request timing
def install(app):
    from .auth import SESSIONS

    @app.middleware("http")
    async def _time(request, call_next):
        p = request.url.path
        if not p.startswith("/api/") or p.startswith(SKIP):
            return await call_next(request)
        t0 = time.perf_counter()
        status = 500
        try:
            resp = await call_next(request)
            status = resp.status_code
            return resp
        finally:
            ms = int((time.perf_counter() - t0) * 1000)
            u = SESSIONS.get(request.cookies.get("bflfp")) or {}
            _count(request.method, _norm(p), ms, status, u, request.headers.get("x-page") or "")

    @app.on_event("startup")
    async def _start():
        asyncio.get_event_loop().create_task(_lag_watch())
        threading.Thread(target=_bg, daemon=True, name="monitor").start()
    _log.info("[monitor] system health on")


def _count(method, path, ms, status, u, page):
    hk = datetime.now().strftime("%Y-%m-%d %H")
    key = f"{method} {path}"
    slow, err = ms >= SLOW_MS, status >= 500
    with _lock:
        h = _hour.setdefault(hk, {"n": 0, "err": 0, "slow": 0, "ms": []})
        h["n"] += 1
        h["err"] += err
        h["slow"] += slow
        if len(h["ms"]) < 20000:
            h["ms"].append(ms)
        e = _ep[hk][key]
        e[0] += 1; e[1] += ms; e[2] = max(e[2], ms); e[3] += slow; e[4] += err
        if u.get("id"):
            _seen[u["id"]] = {"name": u.get("name") or u.get("username") or "", "role": u.get("role") or "",
                              "fac": u.get("active_factory") or u.get("factory_id") or None,
                              "at": time.time(), "page": page or (_seen.get(u["id"]) or {}).get("page", "")}
    if slow or err:
        threading.Thread(target=_write, daemon=True,
                         args=("error" if err else "slow", key, ms, status, u, page)).start()


async def _lag_watch():
    """The server's own heartbeat: a 1-second sleep that wakes up late means the server
    was too busy (or stuck) to answer anybody during that time."""
    while True:
        t = time.perf_counter()
        await asyncio.sleep(1)
        lag = (time.perf_counter() - t - 1) * 1000
        if lag >= 2000:
            threading.Thread(target=_write, daemon=True,
                             args=("server-lag", "", int(lag), 0, None, "", "server did not respond for this long")).start()


# ---------------------------------------------------------------- background: hourly save, alerts, prune
def _pct(xs, p):
    if not xs:
        return 0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def _flush():
    with _lock:
        items = [(k, dict(v)) for k, v in _hour.items()]
        cur = datetime.now().strftime("%Y-%m-%d %H")
        for k in [k for k in _hour if k < (datetime.now() - timedelta(hours=26)).strftime("%Y-%m-%d %H")]:
            _hour.pop(k, None)
            _ep.pop(k, None)
    with closing(db()) as c:
        _ensure(c)
        for k, v in items:
            ms = v["ms"]
            row = (k, v["n"], v["err"], v["slow"], _pct(ms, .5), _pct(ms, .95), max(ms) if ms else 0)
            if c.execute("UPDATE perf_hourly SET n=?,err=?,slow=?,p50=?,p95=?,mx=? WHERE hour=?",
                         (*row[1:], k)).rowcount == 0:
                c.execute("INSERT INTO perf_hourly(hour,n,err,slow,p50,p95,mx) VALUES(?,?,?,?,?,?,?)", row)
        c.commit()


def _disk():
    try:
        d = shutil.disk_usage(DATA)
        return {"free_gb": round(d.free / 1e9, 1), "total_gb": round(d.total / 1e9, 1),
                "free_pct": round(100 * d.free / d.total, 1)}
    except Exception:
        return {}


def _alert(kind, title, body):
    now = time.time()
    if now - _alerted.get(kind, 0) < 1800:
        return
    _alerted[kind] = now
    _write("alert", "", 0, 0, None, "", f"{title} — {body}")
    try:
        from .push import notify_users
        from .db import role_ids
        with closing(db()) as c:
            ids = role_ids(c, "admin")
        notify_users(ids, f"⚠ CMMS: {title}", body, "/?page=health")
    except Exception as e:
        _log.warning("[monitor] alert push failed: %s", e)
    _log.warning("[monitor] ALERT %s — %s", title, body)


def _check():
    now = time.time()
    with _lock:
        recent = [k for t, k in _events if now - t <= 600]
    n = lambda *ks: sum(1 for k in recent if k in ks)
    if n("error") >= 5:
        _alert("error", "server errors", f"{n('error')} errors in the last 10 minutes")
    if n("slow") >= 10:
        _alert("slow", "the app is slow", f"{n('slow')} requests took over {SLOW_MS // 1000} s in 10 minutes")
    if n("hang", "freeze") >= 3:
        _alert("hang", "screens hanging", f"{n('hang', 'freeze')} hang / freeze reports from users in 10 minutes")
    if n("server-lag") >= 1:
        _alert("lag", "server stalled", "the server stopped responding for several seconds")
    d = _disk()
    if d and (d["free_pct"] < 10 or d["free_gb"] < 5):
        _alert("disk", "disk almost full", f'{d["free_gb"]} GB free ({d["free_pct"]} %)')


def _archive(c, table, col, cutoff):
    """b544: old log rows are not thrown away — they are moved out of the database into
    monthly CSV files (open in Excel): data/Report/log-archive/<table>-YYYY-MM.csv"""
    import csv
    from .config import REPORTS
    rows = [dict(r) for r in c.execute(f"SELECT * FROM {table} WHERE {col}<? ORDER BY {col}", (cutoff,))]
    if not rows:
        return 0
    folder = os.path.join(REPORTS, "log-archive")
    os.makedirs(folder, exist_ok=True)
    by_month = {}
    for r in rows:
        by_month.setdefault(str(r.get(col) or "")[:7] or "unknown", []).append(r)
    for month, items in by_month.items():
        path = os.path.join(folder, f"{table}-{month}.csv")
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(items[0].keys()))
            if new:
                w.writeheader()
            w.writerows(items)
    c.execute(f"DELETE FROM {table} WHERE {col}<?", (cutoff,))   # only after the file is written
    return len(rows)


def _prune():
    with closing(db()) as c:
        _ensure(c)
        n1 = _archive(c, "perf_log", "at", (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"))
        n2 = _archive(c, "perf_hourly", "hour", (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d"))
        c.commit()
    if n1 or n2:
        _log.info("[monitor] moved %s old log rows to data/Report/log-archive", n1 + n2)


_MAINT = {"day": ""}


def _maint():
    """b543: nightly database tidy-up — runs once a day at 03:00 (quiet time)."""
    now = datetime.now()
    if now.hour != 3 or _MAINT["day"] == now.strftime("%Y-%m-%d"):
        return
    _MAINT["day"] = now.strftime("%Y-%m-%d")
    t0 = time.time()
    _prune()
    import sqlite3
    from .database import engine
    path = engine.url.database
    if engine.dialect.name != "sqlite" or not path:
        return
    con = sqlite3.connect(path, timeout=60, isolation_level=None)
    try:
        con.execute("PRAGMA busy_timeout=60000")
        con.execute("PRAGMA optimize")
        if now.weekday() == 6:                       # Sunday night: give deleted space back
            con.execute("VACUUM")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    _log.info("[monitor] nightly database tidy-up done in %.1f s", time.time() - t0)


def _bg():
    time.sleep(20)
    _write("start", "", 0, 0, None, "", "CMMS started")
    _alert("start", "app started", f"the CMMS server (re)started at {STARTED:%d-%m-%Y %H:%M}")
    tick = 0
    while True:
        try:
            _check()
            if tick % 5 == 0:
                _flush()
            if tick % 1440 == 0:
                _prune()
            _maint()
        except Exception as e:
            _log.warning("[monitor] background: %s", e)
        tick += 1
        time.sleep(60)


# ---------------------------------------------------------------- what the phone reports
_crate = {}


@router.post("/client")
async def client_report(req: Request):
    """Hangs, freezes and slow screens as the user's phone saw them. Any signed-in user."""
    from .auth import SESSIONS
    u = SESSIONS.get(req.cookies.get("bflfp"))
    if not u:
        return {"ok": False}
    try:
        b = await req.json()
    except Exception:
        return {"ok": False}
    kind = str(b.get("kind") or "")
    if kind not in ("hang", "freeze", "slow-net", "slow-page", "offline"):
        return {"ok": False}
    k = (u.get("id"), kind)
    if time.time() - _crate.get(k, 0) < 30:          # one per user per kind per 30 s
        return {"ok": True, "deduped": True}
    _crate[k] = time.time()
    ua = req.headers.get("user-agent") or ""
    dev = "phone" if re.search(r"Android|iPhone|Mobile", ua) else "PC"
    threading.Thread(target=_write, daemon=True,
                     args=(kind, _norm(str(b.get("path") or ""))[:160], int(b.get("ms") or 0), 0, u,
                           str(b.get("page") or ""), f'{dev} · {str(b.get("detail") or "")[:300]}')).start()
    with _lock:
        if u.get("id") in _seen:
            _seen[u["id"]]["page"] = str(b.get("page") or "")
    return {"ok": True}


# ---------------------------------------------------------------- the admin page
@router.get("/health")
async def health(req: Request):
    from .auth import user_from
    u = user_from(req)
    if u.get("role") != "admin":
        raise HTTPException(403, "admin only")
    try:
        _flush()
    except Exception:
        pass
    now = datetime.now()
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    with closing(db()) as c:
        _ensure(c)
        got = {r["hour"]: dict(r) for r in c.execute(
            "SELECT * FROM perf_hourly WHERE hour>=?", ((now - timedelta(hours=23)).strftime("%Y-%m-%d %H"),))}
        hours = []
        for i in range(23, -1, -1):                       # all 24 slots, quiet hours as zero
            hk = (now - timedelta(hours=i)).strftime("%Y-%m-%d %H")
            hours.append(got.get(hk) or {"hour": hk, "n": 0, "err": 0, "slow": 0, "p50": 0, "p95": 0, "mx": 0})
        kinds = {r["kind"]: r["n"] for r in c.execute(
            "SELECT kind, COUNT(*) n FROM perf_log WHERE at>=? GROUP BY kind", (since,))}
        problems = [dict(r) for r in c.execute(
            "SELECT * FROM perf_log WHERE kind<>'start' ORDER BY id DESC LIMIT 150")]
        starts = [r["at"] for r in c.execute("SELECT at FROM perf_log WHERE kind='start' ORDER BY id DESC LIMIT 5")]
        facs = {r["id"]: r["code"] for r in c.execute("SELECT id,code FROM factories")}
    with _lock:
        eps = defaultdict(lambda: [0, 0, 0, 0, 0])
        for hk, d in _ep.items():
            for k, v in d.items():
                e = eps[k]; e[0] += v[0]; e[1] += v[1]; e[2] = max(e[2], v[2]); e[3] += v[3]; e[4] += v[4]
        cur = _hour.get(now.strftime("%Y-%m-%d %H"), {"n": 0, "err": 0, "slow": 0, "ms": []})
        online = [{**v, "plant": facs.get(v["fac"], ""), "ago": int(time.time() - v["at"])}
                  for v in _seen.values() if time.time() - v["at"] < 600]
    top = sorted(({"ep": k, "n": v[0], "avg": int(v[1] / v[0]) if v[0] else 0, "max": v[2], "slow": v[3], "err": v[4]}
                  for k, v in eps.items()), key=lambda x: (-x["avg"] if x["n"] >= 3 else 0, -x["max"]))[:15]
    for p in problems:
        p["plant"] = facs.get(p["fac"], "")
    sysinfo = {"disk": _disk(), "started": STARTED.strftime("%Y-%m-%d %H:%M"),
               "up_h": round((now - STARTED).total_seconds() / 3600, 1), "restarts": starts}
    try:
        sysinfo["db_mb"] = round(os.path.getsize(os.path.join(DATA, "cmms.db")) / 1e6, 1)
    except Exception:
        pass
    try:
        import psutil
        pr = psutil.Process()
        sysinfo.update({"cpu": psutil.cpu_percent(interval=0.3), "ram_pct": psutil.virtual_memory().percent,
                        "app_mb": round(pr.memory_info().rss / 1e6)})
    except Exception:
        pass
    n24 = sum(h["n"] for h in hours)
    err24, slow24 = kinds.get("error", 0), kinds.get("slow", 0)
    hang24 = kinds.get("hang", 0) + kinds.get("freeze", 0)
    lag24 = kinds.get("server-lag", 0)
    status = ("red" if err24 >= 20 or lag24 >= 3 or (sysinfo["disk"] and sysinfo["disk"].get("free_pct", 100) < 10)
              else "amber" if err24 or slow24 >= 20 or hang24 or lag24 else "green")
    return {"status": status, "now": now.strftime("%Y-%m-%d %H:%M:%S"), "sys": sysinfo,
            "last_hour": {"n": cur["n"], "err": cur["err"], "slow": cur["slow"], "p95": _pct(cur["ms"], .95)},
            "day": {"n": n24, "err": err24, "slow": slow24, "hang": hang24, "lag": lag24,
                    "slow_net": kinds.get("slow-net", 0), "slow_page": kinds.get("slow-page", 0),
                    "alerts": kinds.get("alert", 0)},
            "hours": hours, "top": top, "problems": problems,
            "online": sorted(online, key=lambda x: x["ago"]), "slow_ms": SLOW_MS}

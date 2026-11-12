"""b472 — audit trail: who changed what, when, in which plant (admin only).

How it records
  Every change to the business tables is caught in the database itself, by triggers,
  so no screen and no endpoint can change a job, an asset, a checklist or a user
  without leaving a row — including code written later that nobody remembered to log.
  The triggers are TEMP triggers, created on each of the app's own connections: they
  call audit_who(), a function only the app registers, and a person opening cmms.db
  in another tool is not stopped by a function that tool does not have.

  audit_who() answers from the request being served: user, role, plant, IP, and the
  API call. Work the app does by itself (night jobs, start-up repairs) is "system".

What a row holds
  I (added) and D (deleted): the whole row.  U (changed): only the fields that changed,
  old and new.  Passwords are never written — only that one was changed.

Kept for 2 years. Older rows are written to data/audit_archive/audit_<year>.csv.gz and
then removed from the database, once a day.
"""
import contextvars, csv, gzip, json, logging, os, threading, time
from contextlib import closing
from datetime import datetime, timedelta

from fastapi import APIRouter, Request, HTTPException

from .config import DATA
from .database import engine
from .db import db

_log = logging.getLogger("cmms")
router = APIRouter(prefix="/api/audit")

KEEP_DAYS = 730
CTX = contextvars.ContextVar("audit_ctx", default=None)

# table -> (area EN, area TH)
AREAS = {
    "jobs_bfl": ("Jobs", "งาน"), "jobs_fp": ("Jobs", "งาน"), "jobs_pc": ("Jobs", "งาน"),
    "machines": ("Assets", "สินทรัพย์"),
    "users": ("Users", "ผู้ใช้"),
    "pm_templates": ("Checklists", "เช็คลิสต์"), "pm_items": ("Checklists", "เช็คลิสต์"),
    "pm_members": ("Checklists", "เช็คลิสต์"), "pm_groups": ("Checklists", "เช็คลิสต์"),
    "pm_sched": ("PM plan", "แผน PM"), "pm_plans": ("PM plan", "แผน PM"),
    "pm_config": ("PM plan", "แผน PM"), "pm_overrides": ("PM plan", "แผน PM"),
    "pm_elec": ("Electrical PM", "PM ไฟฟ้า"),
    "pm_results": ("PM results", "ผลตรวจ PM"),
    "plan_teams": ("Planning board", "บอร์ดวางแผน"), "plan_lends": ("Planning board", "บอร์ดวางแผน"),
    "plan_days": ("Planning board", "บอร์ดวางแผน"), "plan_reports": ("Plan reports", "รายงานแผน"),
    "signoffs": ("Approvals / signatures", "อนุมัติ / ลายเซ็น"),
    "sig_signers": ("Approvals / signatures", "อนุมัติ / ลายเซ็น"),
    "projects": ("Projects", "โปรเจกต์"), "project_tasks": ("Projects", "โปรเจกต์"),
    "problem_types": ("Settings", "ตั้งค่า"), "departments": ("Settings", "ตั้งค่า"),
    "factories": ("Settings", "ตั้งค่า"), "app_settings": ("Settings", "ตั้งค่า"),
    "spare_parts": ("Spare parts", "อะไหล่"), "requisitions": ("Spare parts", "อะไหล่"),
}
JOB_FAC = {"jobs_bfl": 1, "jobs_fp": 2, "jobs_pc": 3}
SECRET = {"password"}


# ---------------------------------------------------------------- request context
def begin(request):
    """Called by the middleware for every API call that can change something."""
    from .auth import SESSIONS
    u = SESSIONS.get(request.cookies.get("bflfp")) or {}
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() \
        or (request.client.host if request.client else "")
    ctx = {"uid": u.get("id"), "name": u.get("name") or u.get("username") or "",
           "role": u.get("role") or "", "fac": u.get("active_factory") or u.get("factory_id") or None,
           "ip": ip, "api": f"{request.method} {request.url.path}"[:160]}
    return CTX.set(ctx)


def end(token):
    try:
        CTX.reset(token)
    except Exception:
        pass


def _who():
    c = CTX.get()
    return json.dumps(c if c else {"name": "system", "role": "system"}, ensure_ascii=False)


# ---------------------------------------------------------------- triggers
def _ensure_table(con):
    con.execute("""CREATE TABLE IF NOT EXISTS main.audit_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, who TEXT, tbl TEXT, rid TEXT,
        act TEXT, fac INTEGER, old TEXT, new TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS main.ix_audit_at ON audit_log(at)")
    con.execute("CREATE INDEX IF NOT EXISTS main.ix_audit_tbl ON audit_log(tbl, rid)")


def _val(alias, col):
    if col in SECRET:
        # never the hash — the salt prefix alone shows a change and cannot be reversed
        return f"CASE WHEN {alias}.\"{col}\" IS NULL THEN NULL ELSE '(set) ' || substr({alias}.\"{col}\",1,4) END"
    return f'{alias}."{col}"'


def _bind(dbapi_conn):
    """Register audit_who() and create this connection's TEMP triggers."""
    try:
        dbapi_conn.create_function("audit_who", 0, _who)
        _ensure_table(dbapi_conn)
        have = {r[0] for r in dbapi_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in AREAS:
            if t not in have:
                continue
            cols = [r[1] for r in dbapi_conn.execute(f'PRAGMA main.table_info("{t}")')]
            if not cols:
                continue
            key = "id" if "id" in cols else "rowid"
            fac = lambda a: (f'{a}."factory_id"' if "factory_id" in cols else
                             (str(JOB_FAC[t]) if t in JOB_FAC else "NULL"))
            # json_object takes at most ~100 arguments: build it in slices of 40 columns
            # and merge the slices (empty values are dropped by the merge — harmless here)
            def _obj(parts):
                objs = ["json_object(" + ",".join(parts[i:i + 40]) + ")" for i in range(0, len(parts), 40)]
                out = "'{}'"
                for o in objs:
                    out = f"json_patch({out},{o})"
                return out
            full = lambda a: _obj([f"'{c}',{_val(a, c)}" for c in cols])
            # only what changed: {"col":[value]} — the [] keeps a change to/from NULL
            diff = lambda a: _obj([f"'{c}',CASE WHEN OLD.\"{c}\" IS NOT NEW.\"{c}\" THEN json_array({_val(a, c)}) END"
                                  for c in cols])
            changed = " OR ".join(f'OLD."{c}" IS NOT NEW."{c}"' for c in cols)
            ins = ("INSERT INTO audit_log(at,who,tbl,rid,act,fac,old,new) VALUES("
                   "strftime('%Y-%m-%d %H:%M:%S','now','localtime'),audit_who(),")
            dbapi_conn.executescript(f"""
                DROP TRIGGER IF EXISTS temp.aud_{t}_i; DROP TRIGGER IF EXISTS temp.aud_{t}_u;
                DROP TRIGGER IF EXISTS temp.aud_{t}_d;
                CREATE TEMP TRIGGER aud_{t}_i AFTER INSERT ON main."{t}" BEGIN
                  {ins}'{t}',NEW.{key},'I',{fac('NEW')},NULL,{full('NEW')}); END;
                CREATE TEMP TRIGGER aud_{t}_u AFTER UPDATE ON main."{t}" WHEN {changed} BEGIN
                  {ins}'{t}',NEW.{key},'U',{fac('NEW')},{diff('OLD')},{diff('NEW')}); END;
                CREATE TEMP TRIGGER aud_{t}_d AFTER DELETE ON main."{t}" BEGIN
                  {ins}'{t}',OLD.{key},'D',{fac('OLD')},{full('OLD')},NULL); END;
            """)
    except Exception as e:                                   # never stop the app over this
        _log.warning("[audit] triggers not installed on a connection: %s", e)


def install():
    """Hook every new SQLite connection, then drop the ones opened before start-up
    migrations so each connection is built against the final table layout."""
    if engine.dialect.name != "sqlite":
        _log.info("[audit] not SQLite — audit trail off")
        return
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _rec):
        _bind(dbapi_conn)

    engine.dispose()
    threading.Thread(target=_prune_loop, daemon=True, name="audit-prune").start()
    _log.info("[audit] audit trail on — %d tables, kept %d days", len(AREAS), KEEP_DAYS)


# ---------------------------------------------------------------- 2-year retention
def prune():
    cut = (datetime.now() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with closing(db()) as c:
        rows = c.execute("SELECT id,at,who,tbl,rid,act,fac,old,new FROM audit_log WHERE at<? ORDER BY id",
                         (cut,)).fetchall()
        if not rows:
            return 0
        d = os.path.join(DATA, "audit_archive")
        os.makedirs(d, exist_ok=True)
        by_year = {}
        for r in rows:
            by_year.setdefault((r["at"] or "0000")[:4], []).append(r)
        for y, rs in by_year.items():
            p = os.path.join(d, f"audit_{y}.csv.gz")
            new = not os.path.exists(p)
            with gzip.open(p, "at", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(["id", "at", "who", "table", "row", "action", "plant", "old", "new"])
                for r in rs:
                    w.writerow([r["id"], r["at"], r["who"], r["tbl"], r["rid"], r["act"], r["fac"], r["old"], r["new"]])
        c.execute("DELETE FROM audit_log WHERE at<?", (cut,))
        c.commit()
    _log.info("[audit] archived %d rows older than %s", len(rows), cut[:10])
    return len(rows)


def _prune_loop():
    time.sleep(60)
    while True:
        try:
            prune()
        except Exception as e:
            _log.warning("[audit] prune failed: %s", e)
        time.sleep(24 * 3600)


# ---------------------------------------------------------------- reading it back
def _require_admin(req):
    from .auth import user_from
    u = user_from(req)
    if u.get("role") != "admin":
        raise HTTPException(403, "ดูประวัติการแก้ไขได้เฉพาะผู้ดูแลระบบ / audit trail is admin only")
    return u


def _j(s):
    try:
        return json.loads(s) if s else {}
    except Exception:
        return {}


class _Labels:
    """Turns (table, row id) into something a person recognises: a job number, an
    asset ID, a user's name, a checklist group."""
    def __init__(self, c):
        self.c, self.cache = c, {}
        self.fac = {r["id"]: r["code"] for r in c.execute("SELECT id,code FROM factories")}

    def row(self, tbl, rid):
        k = (tbl, rid)
        if k not in self.cache:
            r = None
            try:
                key = "id" if tbl not in ("plan_lends", "plan_days", "pm_elec", "app_settings") else "rowid"
                r = self.c.execute(f'SELECT * FROM "{tbl}" WHERE {key}=?', (rid,)).fetchone()
            except Exception:
                pass
            self.cache[k] = dict(r) if r else {}
        return self.cache[k]

    def _gone(self, tbls, rid, field):
        """A record deleted since — its name is still in its own 'deleted' audit row."""
        ph = ",".join("?" * len(tbls))
        r = self.c.execute(f"SELECT old FROM audit_log WHERE tbl IN ({ph}) AND rid=? AND act='D'"
                           " ORDER BY id DESC LIMIT 1", (*tbls, str(rid))).fetchone()
        return _j(r["old"]).get(field) if r else None

    def job(self, jid):
        if not jid:
            return ""
        k = ("job", jid)
        if k not in self.cache:
            r = self.c.execute("SELECT j.jobid, m.code FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id"
                               " WHERE j.id=?", (jid,)).fetchone()
            self.cache[k] = ((r["jobid"] + (f" · {r['code']}" if r["code"] else "")) if r else
                             (self._gone(list(JOB_FAC), jid, "jobid") or f"job #{jid}"))
        return self.cache[k]

    def machine(self, mid):
        if not mid:
            return ""
        k = ("m", mid)
        if k not in self.cache:
            r = self.c.execute("SELECT code FROM machines WHERE id=?", (mid,)).fetchone()
            self.cache[k] = r["code"] if r else (self._gone(["machines"], mid, "code") or f"asset #{mid}")
        return self.cache[k]

    def tpl(self, tid):
        if not tid:
            return ""
        k = ("t", tid)
        if k not in self.cache:
            r = self.c.execute("SELECT machine_type FROM pm_templates WHERE id=?", (tid,)).fetchone()
            self.cache[k] = r["machine_type"] if r else (self._gone(["pm_templates"], tid, "machine_type")
                                                         or f"group #{tid}")
        return self.cache[k]

    def label(self, tbl, rid, row):
        g = lambda k: row.get(k)
        if tbl in JOB_FAC:
            j = self.job(rid)
            return j if (not g("jobid") or j.startswith(g("jobid"))) else g("jobid")
        if tbl == "machines":
            return " · ".join(x for x in (g("code"), g("name")) if x)
        if tbl == "users":
            return f'{g("name") or ""} ({g("username") or ""})'
        if tbl == "pm_templates":
            return g("machine_type") or self.tpl(rid)
        if tbl == "pm_items":
            return f'{self.tpl(g("template_id"))} › {(g("item") or "")[:60]}'
        if tbl == "pm_members":
            return f'{self.machine(g("machine_id"))} → {self.tpl(g("template_id"))}'
        if tbl in ("pm_sched",):
            return f'{self.tpl(g("template_id"))} · {g("freq") or ""}'
        if tbl in ("pm_plans", "pm_overrides"):
            return self.machine(g("machine_id"))
        if tbl in ("signoffs", "sig_signers", "pm_results", "pm_elec", "plan_lends", "requisitions"):
            extra = g("action") or g("kind") or (g("item_text") or "")[:40] or g("tech") or ""
            return f'{self.job(g("job_id"))}' + (f" · {extra}" if extra else "")
        if tbl == "plan_teams":
            return f'{g("day") or ""} · {g("name") or ""}'
        if tbl in ("plan_days", "plan_reports"):
            return g("day") or g("plan_date") or ""
        if tbl in ("projects", "project_tasks", "problem_types", "spare_parts"):
            return g("name") or ""
        if tbl == "departments":
            return f'{g("code") or ""} {g("name_en") or ""}'
        if tbl == "factories":
            return g("code") or ""
        if tbl == "app_settings":
            return g("k") or ""
        return str(rid)


HIDE = {"id", "factory_id", "template_id", "machine_id", "job_id", "app_edit", "item_id", "project_id"}


def _fmt(v):
    if v is None:
        return ""
    s = str(v)
    return s if len(s) <= 300 else s[:300] + "…"


def _query(req, limit=200, offset=0, cap=None):
    p = req.query_params
    frm, to = p.get("from") or "", p.get("to") or ""
    fac, area, act = p.get("fac") or "", p.get("area") or "", p.get("act") or ""
    user, q = (p.get("user") or "").strip().lower(), (p.get("q") or "").strip().lower()
    where, args = [], []
    if frm:
        where.append("at>=?"); args.append(frm + " 00:00:00")
    if to:
        where.append("at<=?"); args.append(to + " 23:59:59")
    if area:
        tbls = [t for t, a in AREAS.items() if a[0] == area]
        where.append("tbl IN (%s)" % ",".join("?" * len(tbls)) if tbls else "1=0"); args += tbls
    if act in ("I", "U", "D"):
        where.append("act=?"); args.append(act)
    if p.get("hide_system") == "1":
        where.append("who NOT LIKE '%\"role\": \"system\"%'")
    sql = "SELECT * FROM audit_log" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC"
    out, skipped = [], 0
    with closing(db()) as c:
        _ensure_table_conn(c)
        L = _Labels(c)
        cur = c.execute(sql + " LIMIT 20000", args)
        for r in cur:
            who = _j(r["who"])
            old, new = _j(r["old"]), _j(r["new"])
            f = r["fac"] or who.get("fac")
            if fac and str(f or "") != fac:
                continue
            if user and user not in (who.get("name") or "").lower():
                continue
            if r["act"] == "U":
                cur_row = L.row(r["tbl"], r["rid"])
                row = {**cur_row, **{k: v[0] for k, v in new.items() if isinstance(v, list)}}
                changes = [{"f": k, "o": _fmt((old.get(k) or [None])[0]), "n": _fmt(v[0] if isinstance(v, list) else v)}
                           for k, v in new.items()]
                for x in changes:
                    if x["f"] in SECRET:
                        x["o"], x["n"] = "••••", "•••• (เปลี่ยนแล้ว / changed)"
            elif r["act"] == "I":
                row = new
                new = {k: ("••••" if k in SECRET and v else v) for k, v in new.items()}
                changes = [{"f": k, "o": "", "n": _fmt(v)} for k, v in new.items()
                           if v not in (None, "", 0) and k not in HIDE]
            else:
                row = old
                old = {k: ("••••" if k in SECRET and v else v) for k, v in old.items()}
                changes = [{"f": k, "o": _fmt(v), "n": ""} for k, v in old.items()
                           if v not in (None, "", 0) and k not in HIDE]
            lab = L.label(r["tbl"], r["rid"], row)
            if q and q not in lab.lower() and q not in (r["old"] or "").lower() and q not in (r["new"] or "").lower():
                continue
            if skipped < offset:
                skipped += 1
                continue
            a = AREAS.get(r["tbl"], (r["tbl"], r["tbl"]))
            out.append({"id": r["id"], "at": r["at"], "who": who.get("name") or "", "role": who.get("role") or "",
                        "ip": who.get("ip") or "", "api": who.get("api") or "",
                        "plant": L.fac.get(f, "") if f else "", "area": a[0], "area_th": a[1],
                        "tbl": r["tbl"], "record": lab, "act": r["act"], "changes": changes})
            if len(out) >= (cap or limit):
                break
        total = c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        first = c.execute("SELECT MIN(at) FROM audit_log").fetchone()[0]
    return out, total, first


def _ensure_table_conn(c):
    c.execute("""CREATE TABLE IF NOT EXISTS audit_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, who TEXT, tbl TEXT, rid TEXT,
        act TEXT, fac INTEGER, old TEXT, new TEXT)""")


@router.get("")
async def list_audit(req: Request, limit: int = 100, offset: int = 0):
    _require_admin(req)
    limit = max(1, min(int(limit), 500))
    rows, total, first = _query(req, limit=limit + 1, offset=offset)
    more = len(rows) > limit
    return {"rows": rows[:limit], "more": more, "total": total, "since": first,
            "areas": sorted({a[0] for a in AREAS.values()}), "keep_days": KEEP_DAYS}


@router.get("/export")
async def export_audit(req: Request):
    import io
    from fastapi.responses import StreamingResponse
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    u = _require_admin(req)
    rows, _t, _f = _query(req, cap=20000)
    wb = Workbook()
    ws = wb.active
    ws.title = "Audit trail"
    heads = [("When", 19), ("Who", 22), ("Role", 11), ("Plant", 7), ("Area", 18), ("Record", 38),
             ("Action", 9), ("Field", 20), ("Old value", 34), ("New value", 34), ("IP", 15), ("API call", 34)]
    for i, (h, w) in enumerate(heads, 1):
        cell = ws.cell(1, i, h)
        cell.fill, cell.font = PatternFill("solid", fgColor="1E293B"), Font(bold=True, color="FFFFFF")
        ws.column_dimensions[get_column_letter(i)].width = w
    ACT = {"I": "Added", "U": "Changed", "D": "Deleted"}
    n = 2
    for r in rows:
        ch = r["changes"] or [{"f": "", "o": "", "n": ""}]
        for x in ch:
            for i, v in enumerate([r["at"], r["who"], r["role"], r["plant"], r["area"], r["record"],
                                   ACT.get(r["act"], r["act"]), x["f"], x["o"], x["n"], r["ip"], r["api"]], 1):
                ws.cell(n, i, v).alignment = Alignment(vertical="top", wrap_text=i in (6, 9, 10))
            n += 1
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(heads))}{max(1, n - 1)}"
    from .xlstamp import stamp, file_name
    fac = req.query_params.get("fac") or None
    name = file_name("audit-trail", int(fac) if fac else None)
    stamp(wb, int(fac) if fac else None, name, u, "Audit trail / ประวัติการแก้ไข")
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})

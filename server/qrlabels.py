# -*- coding: utf-8 -*-
"""QR labels for the machines, and what a scanned label opens.

A label carries one web link: https://<public address>/?asset=<PLANT>-<ASSET ID>,
e.g. ...?asset=FP-W01AC01. The plant code is INTERNAL — it is inside the QR only,
never printed — and it is what keeps W01AC01 of BFLFP apart from a W01AC01 any other
plant may one day have. The printed text is the asset ID, machine name, location and
floor.

  GET  /api/assets/qr         the plant's assets with their label state, and the log
  POST /api/assets/qr/print   record a print run (who, when, which, what was printed)
  GET  /api/assets/scan       a scanned label → the machine page's data

A label goes stale when what is PRINTED on it changes: the name, the location or the
floor. The asset ID cannot change without the QR changing too. Each print run stores
what it printed per asset (qr_labels), and the list compares that with the machine
row now — that difference is the "needs reprint" flag.
"""
import json
from contextlib import closing
from datetime import date

from fastapi import APIRouter, Request, HTTPException

from .db import db, now
from .auth import require_role, user_from, all_plants

router = APIRouter(prefix="/api/assets")

PRINT_FIELDS = ("name", "line", "floor")
LABEL_SIZES = ("a4_24", "a4_12", "st_50", "st_70")


def _ensure(c):
    c.execute("""CREATE TABLE IF NOT EXISTS qr_prints(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER, at TEXT,
        by_id INTEGER, by_name TEXT, size TEXT, n INTEGER, ids TEXT)""")
    # what the LAST label printed for each machine said — the reprint check reads this
    c.execute("""CREATE TABLE IF NOT EXISTS qr_labels(
        machine_id INTEGER PRIMARY KEY, factory_id INTEGER, print_id INTEGER,
        printed_at TEXT, by_name TEXT, name TEXT, line TEXT, floor TEXT)""")


def _fac(u):
    return u.get("active_factory") or u.get("factory_id")


def _plant_code(c, fac):
    r = c.execute("SELECT code FROM factories WHERE id=?", (fac,)).fetchone()
    return (r["code"] if r else "") or ""


@router.get("/qr")
async def qr_list(req: Request):
    u = require_role(req, "planner", "admin", "manager", "engcenter")
    fac = _fac(u)
    with closing(db()) as c:
        _ensure(c)
        code = _plant_code(c, fac)
        last = {r["machine_id"]: dict(r) for r in c.execute(
            "SELECT * FROM qr_labels WHERE factory_id=?", (fac,))}
        assets = []
        for r in c.execute("""SELECT id, code, name, COALESCE(line,'') line, COALESCE(floor,'') floor,
                                     COALESCE(department,'') department, COALESCE(criticality,'') criticality,
                                     COALESCE(asset_group,'') asset_group
                                FROM machines WHERE factory_id=? AND COALESCE(active,1)=1
                               ORDER BY code""", (fac,)):
            a = dict(r)
            p = last.get(a["id"])
            a["printed_at"] = p["printed_at"] if p else None
            a["printed_by"] = p["by_name"] if p else None
            a["changed"] = [f for f in PRINT_FIELDS
                            if p and str(p.get(f) or "").strip() != str(a.get(f) or "").strip()]
            assets.append(a)
        hist = [dict(r) for r in c.execute(
            "SELECT id, at, by_name, size, n, ids FROM qr_prints WHERE factory_id=?"
            " ORDER BY id DESC LIMIT 100", (fac,))]
    byid = {a["id"]: a["code"] for a in assets}
    for h in hist:
        ids = [int(x) for x in str(h.pop("ids") or "").split(",") if x.strip().isdigit()]
        h["ids"] = ids
        h["codes"] = [byid[i] for i in ids if i in byid]
    return {"plant": code, "assets": assets, "history": hist}


@router.post("/qr/print")
async def qr_print(req: Request):
    """Record a print run. Called when the planner presses Print; the labels
    themselves are drawn by the browser."""
    u = require_role(req, "planner", "admin", "manager", "engcenter")
    fac = _fac(u)
    b = await req.json()
    size = b.get("size") if b.get("size") in LABEL_SIZES else "a4_24"
    ids = [int(x) for x in (b.get("ids") or []) if str(x).isdigit()][:5000]
    if not ids:
        raise HTTPException(400, "no assets")
    at = now()
    with closing(db()) as c:
        _ensure(c)
        q = ",".join("?" * len(ids))
        rows = [dict(r) for r in c.execute(
            f"SELECT id, name, COALESCE(line,'') line, COALESCE(floor,'') floor FROM machines"
            f" WHERE factory_id=? AND id IN ({q})", (fac, *ids))]
        if not rows:
            raise HTTPException(400, "no assets of this plant")
        pid = c.insert_id("INSERT INTO qr_prints(factory_id, at, by_id, by_name, size, n, ids)"
                          " VALUES(?,?,?,?,?,?,?)",
                          (fac, at, u["id"], u.get("name") or "", size, len(rows),
                           ",".join(str(r["id"]) for r in rows)))
        for r in rows:
            c.execute("""INSERT INTO qr_labels(machine_id, factory_id, print_id, printed_at, by_name,
                           name, line, floor) VALUES(?,?,?,?,?,?,?,?)
                         ON CONFLICT(machine_id) DO UPDATE SET factory_id=excluded.factory_id,
                           print_id=excluded.print_id, printed_at=excluded.printed_at,
                           by_name=excluded.by_name, name=excluded.name, line=excluded.line,
                           floor=excluded.floor""",
                      (r["id"], fac, pid, at, u.get("name") or "", r["name"], r["line"], r["floor"]))
        c.commit()
    return {"ok": True, "id": pid, "n": len(rows), "at": at}


def parse_key(key):
    """'FP-W01AC01' → ('FP', 'W01AC01'). Also takes a whole link, and a bare asset ID
    (old labels), which returns plant ''."""
    k = str(key or "").strip()
    if "asset=" in k:
        k = k.split("asset=", 1)[1].split("&", 1)[0]
    from urllib.parse import unquote
    k = unquote(k).strip()
    if "-" in k:
        p, rest = k.split("-", 1)
        if p.upper() in ("BFL", "FP", "PC") and rest:
            return p.upper(), rest.strip()
    return "", k


@router.get("/scan")
async def scan(req: Request, key: str = "", n: int = 5):
    """What a scanned label opens — b378: ONE page, the same for every role.

    Title (machine) · Open now · History (BD / CM / PM: what, when, who, fix) · PM
    (previous and next) · spare parts. Who may raise a request is decided on the
    phone from the role; the data is the same for everybody. `n` is how many history
    lines per type come back (5 by default, "Show more" asks for 50)."""
    u = user_from(req)
    plant, code = parse_key(key)
    if not code:
        raise HTTPException(400, "no asset")
    n = max(1, min(int(n or 5), 200))
    fac = _fac(u)
    DONE = ("Done", "ServiceCompleted", "Cancelled", "Rejected")
    with closing(db()) as c:
        facs = {r["code"]: dict(r) for r in c.execute("SELECT id, code, name FROM factories")}
        want = facs[plant]["id"] if plant in facs else fac
        m = c.execute("""SELECT m.*, f.code fcode, f.name fname FROM machines m
                           LEFT JOIN factories f ON f.id=m.factory_id
                          WHERE UPPER(m.code)=UPPER(?) AND m.factory_id=?""", (code, want)).fetchone()
        if not m and not plant:                       # an old bare-ID label: any plant
            m = c.execute("""SELECT m.*, f.code fcode, f.name fname FROM machines m
                               LEFT JOIN factories f ON f.id=m.factory_id
                              WHERE UPPER(m.code)=UPPER(?) ORDER BY m.factory_id=? DESC""",
                          (code, fac)).fetchone()
        if not m:
            return {"found": False, "code": code}
        m = dict(m)
        if m["factory_id"] != fac and not all_plants(u):
            return {"found": True, "other_plant": True, "code": m["code"],
                    "plant_name": m.get("fname") or m.get("fcode") or ""}
        names = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM users")}

        def who(j):
            out = []
            if j.get("lead_tech"):
                out.append(names.get(j["lead_tech"]) or "")
            for h in str(j.get("helpers") or "").replace(";", ",").split(","):
                h = h.strip()
                if h.isdigit() and int(h) != j.get("lead_tech"):
                    out.append(names.get(int(h)) or "")
            return ", ".join(x for x in out if x)

        def first(t):
            return str(t or "").strip().split("\n", 1)[0][:160]

        t0 = date.today().isoformat()
        out = {"found": True, "other_plant": False, "today": t0,
               "switch_to": m["factory_id"] if m["factory_id"] != fac else None,
               "machine": {k: m.get(k) for k in ("id", "code", "name", "line", "floor", "department",
                                                  "criticality", "asset_group", "brand_model", "category",
                                                  "manufacturer", "remark", "active", "fcode", "fname")},
               "role": u.get("role")}
        # Open now — everything not finished, INCLUDING work waiting for the requester to
        # accept it, so nobody reports a fault that has already been repaired.
        out["open"] = []
        for r in c.execute("""
            SELECT id, jobid, jobtype, status, planned_date, planned_start, lead_tech, helpers,
                   descr, created_at FROM jobs
             WHERE machine_id=? AND status NOT IN ('Done','Cancelled','Rejected')
             ORDER BY CASE jobtype WHEN 'BD' THEN 0 WHEN 'CM' THEN 1 WHEN 'IMP' THEN 2 ELSE 3 END,
                      id DESC LIMIT 30""", (m["id"],)):
            j = dict(r)
            j["who"] = who(j); j["descr"] = first(j["descr"]); j.pop("helpers", None)
            out["open"].append(j)
        # History — finished work, newest first, per type
        out["history"], out["counts"] = {}, {}
        for t, types in (("BD", ("BD",)), ("CM", ("CM", "IMP")), ("PM", ("PM",))):
            q = ",".join("?" * len(types))
            out["counts"][t] = c.execute(f"""SELECT COUNT(*) FROM jobs WHERE machine_id=? AND jobtype IN ({q})
                AND done_at IS NOT NULL AND status='Done'""", (m["id"], *types)).fetchone()[0]
            rows = []
            for r in c.execute(f"""
                SELECT id, jobid, jobtype, status, SUBSTR(done_at,1,16) d, lead_tech, helpers,
                       descr, problem, root_cause, solution FROM jobs
                 WHERE machine_id=? AND jobtype IN ({q}) AND done_at IS NOT NULL
                   AND status='Done'
                 ORDER BY done_at DESC LIMIT ?""", (m["id"], *types, n)):
                j = dict(r)
                j["who"] = who(j)
                j["what"] = first(j.get("problem")) or first(j.get("descr"))
                sol = str(j.get("solution") or "").strip()
                j["fix"] = " → ".join(x for x in (first(j.get("root_cause")), sol.replace("\n", " · ")[:220]) if x)
                for k in ("helpers", "descr", "problem", "root_cause", "solution"):
                    j.pop(k, None)
                rows.append(j)
            out["history"][t] = rows
        out["prev_pm"] = out["history"]["PM"][0] if out["history"]["PM"] else None
        nxt = c.execute(f"""SELECT id, jobid, status, planned_date, planned_start, lead_tech, helpers,
                                   COALESCE(pm_freq,'') pm_freq, descr FROM jobs
                             WHERE machine_id=? AND jobtype='PM' AND status NOT IN ({",".join("?"*len(DONE))})
                             ORDER BY planned_date LIMIT 1""", (m["id"], *DONE)).fetchone()
        if nxt:
            j = dict(nxt); j["who"] = who(j); j["descr"] = first(j["descr"]); j.pop("helpers", None)
            out["next_pm"] = j
        else:
            out["next_pm"] = None
        out["last_pm_date"] = m.get("last_pm_date")
        # spare parts — the register exists but holds nothing yet and has no machine link
        try:
            out["parts_real"] = c.execute("SELECT COUNT(*) FROM spare_parts WHERE factory_id=?",
                                          (m["factory_id"],)).fetchone()[0]
        except Exception:
            out["parts_real"] = 0
    return out

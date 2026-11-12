"""Warehouse (WMS) — finished-goods pallets from QC check to dispatch.

A second app that lives inside the CMMS login: same server, same users, same plant
chosen at sign-in. Its pages are /wms.html; its API is /api/wms/*; its data is the
wms_* tables below, kept per plant (factory_id), so nothing here touches a job, a PM
or a machine.

THE PALLET'S IDENTITY IS THE SLIP QR. The packing room prints a QR on every pallet slip
and that system is not changed. The QR carries JSON:
    {"Product_code","Product_Name","Lot_No","No"(bags),"weight","Station","Pallet",
     "Operator"}
It carries no MFG or EXP date and no id of its own, so a pallet is keyed by
Lot_No + Pallet (the pallet number restarts for each lot). Scanning the same slip twice
finds the same pallet — it never makes a second one.

DATES. MFG is read from the first six digits of the lot number (YYMMDD, e.g.
261023129-PC -> 2026-10-23) and QC can correct it. EXP = MFG + the product's shelf
life once that is set in Products; until then QC may type the EXP printed on the bag.
A typed EXP is never overwritten by a later shelf-life change.

FEFO. Picking always proposes the pallet with the earliest EXP; a pallet without EXP
sorts after every dated one, by MFG and then by arrival (FIFO). A picker who scans a
later-expiring pallet is refused; a supervisor may override with a written reason,
and the override is logged.

ROLES (users.wms_role, set on the Warehouse > Users page):
    view   read every page            qc     + QC check (approve / hold / reject)
    store  + put-away, move, pick     super  everything: products, orders, layout, users
CMMS admins are always 'super'.
"""
import datetime
import json
import re
from contextlib import closing

from fastapi import APIRouter, Request, HTTPException

from .db import db, now, _table_cols
from .auth import user_from

router = APIRouter(prefix="/api/wms")

WMS_ROLES = ("view", "qc", "store", "super")
CAN = {
    "view": {"view"},
    "qc": {"view", "qc"},
    "store": {"view", "store"},
    "super": {"view", "qc", "store", "super"},
}
STATUSES = ("pending", "released", "stored", "hold", "rejected", "picked", "dispatched")
IN_RACK = ("stored", "hold")          # these hold a slot
DEFAULTS = {"row_start": "AA", "rows": "12", "bays": "42", "levels": "5", "slots": "2",
            "warn_days": "90", "crit_days": "30"}


# ---------------------------------------------------------------- schema
def ensure(c):
    c.execute("""CREATE TABLE IF NOT EXISTS wms_settings(
        factory_id INTEGER, k TEXT, v TEXT DEFAULT '', PRIMARY KEY(factory_id, k))""")
    c.execute("""CREATE TABLE IF NOT EXISTS wms_products(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER, code TEXT, name TEXT DEFAULT '',
        weight TEXT DEFAULT '', shelf_life_days INTEGER DEFAULT 0, bags_per_pallet INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1, updated_at TEXT DEFAULT '', UNIQUE(factory_id, code))""")
    c.execute("""CREATE TABLE IF NOT EXISTS wms_locations(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER, code TEXT, row_code TEXT,
        bay INTEGER, level INTEGER, slot INTEGER, blocked INTEGER DEFAULT 0,
        UNIQUE(factory_id, code))""")
    c.execute("""CREATE TABLE IF NOT EXISTS wms_pallets(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER, pkey TEXT,
        lot_no TEXT DEFAULT '', pallet_no TEXT DEFAULT '', product_code TEXT DEFAULT '',
        product_name TEXT DEFAULT '', weight TEXT DEFAULT '', bags_slip INTEGER DEFAULT 0,
        bags_counted INTEGER DEFAULT 0, station TEXT DEFAULT '', operator TEXT DEFAULT '',
        raw_qr TEXT DEFAULT '', mfg_date TEXT DEFAULT '', exp_date TEXT DEFAULT '',
        exp_src TEXT DEFAULT '', status TEXT DEFAULT 'pending', location TEXT DEFAULT '',
        received_at TEXT DEFAULT '', received_by TEXT DEFAULT '',
        qc_at TEXT DEFAULT '', qc_by TEXT DEFAULT '', qc_note TEXT DEFAULT '',
        stored_at TEXT DEFAULT '', order_id INTEGER DEFAULT 0, picked_at TEXT DEFAULT '',
        dispatched_at TEXT DEFAULT '', updated_at TEXT DEFAULT '',
        UNIQUE(factory_id, pkey))""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_wms_pallets_st ON wms_pallets(factory_id, status)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_wms_pallets_prod ON wms_pallets(factory_id, product_code)")
    c.execute("""CREATE TABLE IF NOT EXISTS wms_moves(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER, pallet_id INTEGER,
        action TEXT, from_loc TEXT DEFAULT '', to_loc TEXT DEFAULT '',
        status_from TEXT DEFAULT '', status_to TEXT DEFAULT '', note TEXT DEFAULT '',
        user_id INTEGER, user_name TEXT DEFAULT '', created_at TEXT DEFAULT '')""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_wms_moves_p ON wms_moves(pallet_id)")
    c.execute("""CREATE TABLE IF NOT EXISTS wms_orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER, order_no TEXT,
        customer TEXT DEFAULT '', truck TEXT DEFAULT '', due_date TEXT DEFAULT '',
        status TEXT DEFAULT 'open', note TEXT DEFAULT '', created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT '', dispatched_at TEXT DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS wms_order_lines(
        id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, product_code TEXT,
        qty INTEGER DEFAULT 0)""")
    if "wms_role" not in _table_cols(c, "users"):
        c.execute("ALTER TABLE users ADD COLUMN wms_role TEXT DEFAULT ''")


_READY = False


def conn():
    global _READY
    c = db()
    if not _READY:
        ensure(c)
        c.commit()
        _READY = True
    return c


# ---------------------------------------------------------------- who
def ctx(req: Request, need="view"):
    u = user_from(req)
    fac = u.get("active_factory") or u.get("factory_id")
    if u.get("role") == "admin":
        wr = "super"
    else:
        with closing(conn()) as c:
            r = c.execute("SELECT COALESCE(wms_role,'') r FROM users WHERE id=?", (u["id"],)).fetchone()
        wr = (r["r"] if r else "") or ""
    if need and need not in CAN.get(wr, set()):
        raise HTTPException(403, {"msg": "ไม่มีสิทธิ์ (no permission) — ask the warehouse supervisor"})
    return u, fac, wr


def _who(u):
    return u.get("name") or u.get("username") or ""


# ---------------------------------------------------------------- settings / layout
def settings(c, fac):
    s = dict(DEFAULTS)
    for r in c.execute("SELECT k,v FROM wms_settings WHERE factory_id=?", (fac,)):
        s[r["k"]] = r["v"]
    return s


def _ints(s):
    out = {}
    for k in ("rows", "bays", "levels", "slots", "warn_days", "crit_days"):
        try:
            out[k] = max(0, int(s.get(k) or DEFAULTS[k]))
        except ValueError:
            out[k] = int(DEFAULTS[k])
    return out


def row_codes(start, n):
    start = (start or "AA").upper()
    if not re.fullmatch(r"[A-Z]{2}", start):
        start = "AA"
    a, b = ord(start[0]) - 65, ord(start[1]) - 65
    out = []
    for _ in range(n):
        if a > 25:
            break
        out.append(chr(65 + a) + chr(65 + b))
        b += 1
        if b > 25:
            a, b = a + 1, 0
    return out


def loc_code(row, bay, level, slot):
    return f"{row}{bay:02d}-{level}-{slot}"


LOC_RE = re.compile(r"^(?:LOC:)?([A-Z]{2})(\d{2,3})-(\d)-(\d)$")


def build_layout(c, fac):
    """Make the slot table match the layout settings. Adds what is missing; removes a
    slot that is no longer in the layout only when no pallet is in it."""
    s = settings(c, fac)
    n = _ints(s)
    want = {}
    for row in row_codes(s["row_start"], n["rows"]):
        for bay in range(1, n["bays"] + 1):
            for lv in range(1, n["levels"] + 1):
                for sl in range(1, n["slots"] + 1):
                    want[loc_code(row, bay, lv, sl)] = (row, bay, lv, sl)
    have = {r["code"] for r in c.execute("SELECT code FROM wms_locations WHERE factory_id=?", (fac,))}
    used = _occupied(c, fac)
    added = removed = 0
    for code, (row, bay, lv, sl) in want.items():
        if code not in have:
            c.execute("INSERT INTO wms_locations(factory_id,code,row_code,bay,level,slot) VALUES(?,?,?,?,?,?)",
                      (fac, code, row, bay, lv, sl))
            added += 1
    for code in have - set(want):
        if code not in used:
            c.execute("DELETE FROM wms_locations WHERE factory_id=? AND code=?", (fac, code))
            removed += 1
    return added, removed, len(want)


def _ensure_layout(c, fac):
    if not c.execute("SELECT 1 FROM wms_locations WHERE factory_id=? LIMIT 1", (fac,)).fetchone():
        build_layout(c, fac)
        c.commit()


def _occupied(c, fac):
    return {r["location"]: r["id"] for r in c.execute(
        "SELECT id,location FROM wms_pallets WHERE factory_id=? AND status IN ('stored','hold')"
        " AND COALESCE(location,'')<>''", (fac,))}


# ---------------------------------------------------------------- QR + dates
def parse_qr(text):
    """The slip QR (JSON) -> dict, or a location label -> {'loc': code}, or None."""
    t = (text or "").strip()
    if not t:
        return None
    m = LOC_RE.match(t.upper())
    if m:
        return {"loc": loc_code(m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)))}
    try:
        d = json.loads(t)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    k = {str(a).strip().lower(): (str(b).strip() if b is not None else "") for a, b in d.items()}
    lot, pal = k.get("lot_no", ""), k.get("pallet", "")
    if not lot or not pal:
        return None
    try:
        bags = int(re.sub(r"[^\d]", "", k.get("no", "")) or 0)
    except ValueError:
        bags = 0
    return {"pkey": f"{lot}|{pal}", "lot_no": lot, "pallet_no": pal,
            "product_code": k.get("product_code", ""), "product_name": k.get("product_name", ""),
            "weight": k.get("weight", ""), "bags_slip": bags, "station": k.get("station", ""),
            "operator": k.get("operator", ""), "raw_qr": t}


def mfg_from_lot(lot):
    m = re.match(r"^\s*(\d{2})(\d{2})(\d{2})", lot or "")
    if not m:
        return ""
    try:
        return datetime.date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return ""


def _date(s):
    s = (s or "").strip()
    if not s:
        return ""
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    raise HTTPException(400, {"msg": f"วันที่ไม่ถูกต้อง (invalid date): {s}"})


def _calc_exp(mfg, days):
    if not mfg or not days:
        return ""
    return (datetime.date.fromisoformat(mfg) + datetime.timedelta(days=int(days))).isoformat()


def _product(c, fac, code, name="", weight=""):
    r = c.execute("SELECT * FROM wms_products WHERE factory_id=? AND code=?", (fac, code)).fetchone()
    if r:
        return dict(r)
    c.execute("INSERT INTO wms_products(factory_id,code,name,weight,updated_at) VALUES(?,?,?,?,?)",
              (fac, code, name, weight, now()))
    return dict(c.execute("SELECT * FROM wms_products WHERE factory_id=? AND code=?", (fac, code)).fetchone())


def fefo_key(p):
    return (p.get("exp_date") or "9999-99-99", p.get("mfg_date") or "9999-99-99", p.get("received_at") or "")


def _fefo_rank(p):
    """What decides 'too late to pick': the EXP; with no EXP on either, the MFG."""
    e = p.get("exp_date") or ""
    return (e or "9999-99-99", "" if e else (p.get("mfg_date") or "9999-99-99"))


def _days_left(exp):
    if not exp:
        return None
    return (datetime.date.fromisoformat(exp) - datetime.date.today()).days


def _out(p, n=None):
    d = dict(p)
    d["days_left"] = _days_left(d.get("exp_date"))
    if n:
        dl = d["days_left"]
        d["exp_level"] = ("none" if dl is None else "expired" if dl < 0 else
                          "crit" if dl <= n["crit_days"] else "warn" if dl <= n["warn_days"] else "ok")
    return d


def _move(c, fac, pid, action, u, from_loc="", to_loc="", s_from="", s_to="", note=""):
    c.execute("INSERT INTO wms_moves(factory_id,pallet_id,action,from_loc,to_loc,status_from,status_to,note,"
              "user_id,user_name,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (fac, pid, action, from_loc or "", to_loc or "", s_from or "", s_to or "", note or "",
               u["id"], _who(u), now()))


def _pallet(c, fac, pid):
    r = c.execute("SELECT * FROM wms_pallets WHERE factory_id=? AND id=?", (fac, pid)).fetchone()
    if not r:
        raise HTTPException(404, {"msg": "ไม่พบพาเลท (pallet not found)"})
    return dict(r)


def _find(c, fac, b):
    """A pallet from {pallet_id} or {text: scanned QR}."""
    if b.get("pallet_id"):
        return _pallet(c, fac, int(b["pallet_id"]))
    q = parse_qr(b.get("text"))
    if not q or "pkey" not in q:
        raise HTTPException(400, {"msg": "QR นี้ไม่ใช่ใบพาเลท (this QR is not a pallet slip)"})
    r = c.execute("SELECT * FROM wms_pallets WHERE factory_id=? AND pkey=?", (fac, q["pkey"])).fetchone()
    if not r:
        raise HTTPException(404, {"msg": "พาเลทนี้ยังไม่ผ่าน QC (this pallet has not been checked by QC yet)"})
    return dict(r)


# ---------------------------------------------------------------- endpoints
@router.get("/me")
async def me(req: Request):
    u, fac, wr = ctx(req, need=None)
    with closing(conn()) as c:
        f = c.execute("SELECT code FROM factories WHERE id=?", (fac,)).fetchone()
    return {"id": u["id"], "name": _who(u), "role": u.get("role"), "wms_role": wr,
            "can": sorted(CAN.get(wr, set())), "factory_id": fac, "factory_code": f["code"] if f else ""}


@router.get("/summary")
async def summary(req: Request):
    u, fac, _ = ctx(req)
    with closing(conn()) as c:
        _ensure_layout(c, fac)
        n = _ints(settings(c, fac))
        cnt = {s: 0 for s in STATUSES}
        for r in c.execute("SELECT status, COUNT(*) n FROM wms_pallets WHERE factory_id=? GROUP BY status", (fac,)):
            cnt[r["status"]] = r["n"]
        total = c.execute("SELECT COUNT(*) FROM wms_locations WHERE factory_id=? AND blocked=0", (fac,)).fetchone()[0]
        used = len(_occupied(c, fac))
        stock = [_out(r, n) for r in c.execute(
            "SELECT * FROM wms_pallets WHERE factory_id=? AND status IN ('stored','released','hold')", (fac,))]
        stock.sort(key=fefo_key)
        exp = {"expired": 0, "crit": 0, "warn": 0, "none": 0}
        for p in stock:
            if p["exp_level"] in exp:
                exp[p["exp_level"]] += 1
        rows = {}
        for r in c.execute("SELECT row_code, COUNT(*) n FROM wms_locations WHERE factory_id=? GROUP BY row_code", (fac,)):
            rows[r["row_code"]] = {"row": r["row_code"], "total": r["n"], "used": 0}
        for code in _occupied(c, fac):
            m = LOC_RE.match(code)
            if m and m.group(1) in rows:
                rows[m.group(1)]["used"] += 1
        t0 = datetime.date.today().isoformat()
        tin = c.execute("SELECT COUNT(*) FROM wms_pallets WHERE factory_id=? AND received_at>=?", (fac, t0)).fetchone()[0]
        tout = c.execute("SELECT COUNT(*) FROM wms_pallets WHERE factory_id=? AND dispatched_at>=?", (fac, t0)).fetchone()[0]
        qc = [_out(r, n) for r in c.execute(
            "SELECT * FROM wms_pallets WHERE factory_id=? AND status IN ('pending','hold') ORDER BY received_at", (fac,))]
        waiting = [_out(r, n) for r in c.execute(
            "SELECT * FROM wms_pallets WHERE factory_id=? AND status='released' ORDER BY qc_at", (fac,))]
    return {"counts": cnt, "positions": {"total": total, "used": used}, "expiry": exp,
            "fefo": [p for p in stock if p["status"] == "stored"][:12], "qc": qc[:20],
            "putaway": waiting[:20], "rows": sorted(rows.values(), key=lambda r: r["row"]),
            "today": {"in": tin, "out": tout}, "settings": n}


@router.post("/scan")
async def scan(req: Request):
    """What is this QR? A slip (known pallet or new) or a slot label."""
    u, fac, _ = ctx(req)
    b = await req.json()
    q = parse_qr(b.get("text"))
    if not q:
        raise HTTPException(400, {"msg": "อ่าน QR ไม่ได้ หรือไม่ใช่ใบพาเลท (unreadable or not a pallet slip)"})
    with closing(conn()) as c:
        if "loc" in q:
            r = c.execute("SELECT * FROM wms_locations WHERE factory_id=? AND code=?", (fac, q["loc"])).fetchone()
            if not r:
                raise HTTPException(404, {"msg": f"ไม่มีช่อง {q['loc']} (no such slot)"})
            occ = c.execute("SELECT * FROM wms_pallets WHERE factory_id=? AND location=? AND status IN ('stored','hold')",
                            (fac, q["loc"])).fetchone()
            return {"kind": "loc", "loc": dict(r), "pallet": _out(occ) if occ else None}
        r = c.execute("SELECT * FROM wms_pallets WHERE factory_id=? AND pkey=?", (fac, q["pkey"])).fetchone()
        prod = c.execute("SELECT * FROM wms_products WHERE factory_id=? AND code=?", (fac, q["product_code"])).fetchone()
        mfg = mfg_from_lot(q["lot_no"])
        sl = prod["shelf_life_days"] if prod else 0
        q.update({"mfg_date": mfg, "exp_date": _calc_exp(mfg, sl), "shelf_life_days": sl})
        return {"kind": "pallet", "exists": bool(r), "pallet": _out(r) if r else None, "slip": q}


@router.post("/qc")
async def qc(req: Request):
    """QC decision on a pallet. First check creates the pallet from its slip QR."""
    u, fac, _ = ctx(req, "qc")
    b = await req.json()
    dec = (b.get("decision") or "").lower()
    if dec not in ("release", "hold", "reject"):
        raise HTTPException(400, {"msg": "decision must be release / hold / reject"})
    note = (b.get("note") or "").strip()
    if dec in ("hold", "reject") and not note:
        raise HTTPException(400, {"msg": "กรุณาใส่เหตุผล (a reason is required for Hold / Reject)"})
    try:
        counted = int(b.get("bags_counted"))
    except (TypeError, ValueError):
        raise HTTPException(400, {"msg": "กรุณาใส่จำนวนถุงที่นับได้ (enter the bags counted)"})
    mfg = _date(b.get("mfg_date"))
    exp_in = _date(b.get("exp_date"))
    with closing(conn()) as c:
        if b.get("pallet_id"):
            p = _pallet(c, fac, int(b["pallet_id"]))
        else:
            q = parse_qr(b.get("text"))
            if not q or "pkey" not in q:
                raise HTTPException(400, {"msg": "QR นี้ไม่ใช่ใบพาเลท (this QR is not a pallet slip)"})
            r = c.execute("SELECT * FROM wms_pallets WHERE factory_id=? AND pkey=?", (fac, q["pkey"])).fetchone()
            if r:
                p = dict(r)
            else:
                _product(c, fac, q["product_code"], q["product_name"], q["weight"])
                pid = c.insert_id(
                    "INSERT INTO wms_pallets(factory_id,pkey,lot_no,pallet_no,product_code,product_name,weight,"
                    "bags_slip,station,operator,raw_qr,status,received_at,received_by,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
                    (fac, q["pkey"], q["lot_no"], q["pallet_no"], q["product_code"], q["product_name"],
                     q["weight"], q["bags_slip"], q["station"], q["operator"], q["raw_qr"], now(), _who(u), now()))
                _move(c, fac, pid, "receive", u, s_to="pending")
                p = _pallet(c, fac, pid)
        if p["status"] not in ("pending", "hold"):
            raise HTTPException(409, {"msg": f"พาเลทนี้ตรวจแล้ว สถานะ {p['status']} (already checked — status {p['status']})",
                                      "pallet_id": p["id"]})
        if dec == "release" and counted != int(p["bags_slip"] or 0):
            raise HTTPException(400, {"msg": f"จำนวนถุงไม่ตรงกับใบพาเลท ({counted} ≠ {p['bags_slip']}) — กด Hold หรือนับใหม่"
                                             f" (bag count does not match the slip — Hold or recount)"})
        prod = _product(c, fac, p["product_code"], p["product_name"], p["weight"])
        mfg = mfg or p["mfg_date"] or mfg_from_lot(p["lot_no"])
        calc = _calc_exp(mfg, prod["shelf_life_days"])
        if exp_in and exp_in != calc:
            exp, src = exp_in, "manual"
        else:
            exp, src = calc, ("calc" if calc else "")
        if exp and mfg and exp <= mfg:
            raise HTTPException(400, {"msg": "EXP ต้องหลัง MFG (EXP must be after MFG)"})
        if dec == "release":
            to = "stored" if p["location"] else "released"
        else:
            to = "hold" if dec == "hold" else "rejected"
        loc = p["location"] if to in ("stored", "hold") else ""
        c.execute("UPDATE wms_pallets SET bags_counted=?, mfg_date=?, exp_date=?, exp_src=?, status=?, location=?,"
                  " qc_at=?, qc_by=?, qc_note=?, updated_at=? WHERE id=?",
                  (counted, mfg, exp, src, to, loc, now(), _who(u), note, now(), p["id"]))
        _move(c, fac, p["id"], "qc-" + dec, u, from_loc=p["location"], to_loc=loc,
              s_from=p["status"], s_to=to, note=note)
        c.commit()
        return {"ok": True, "pallet": _out(_pallet(c, fac, p["id"]))}


def _suggest(c, fac, p):
    occ = _occupied(c, fac)
    locs = [dict(r) for r in c.execute(
        "SELECT code,row_code,bay,level,slot FROM wms_locations WHERE factory_id=? AND blocked=0", (fac,))]
    free = [l for l in locs if l["code"] not in occ]
    if not free:
        return None, "full"
    anchors = [r["location"] for r in c.execute(
        "SELECT location FROM wms_pallets WHERE factory_id=? AND status IN ('stored','hold') AND lot_no=?"
        " AND COALESCE(location,'')<>''", (fac, p["lot_no"]))]
    why = "same lot"
    if not anchors:
        anchors = [r["location"] for r in c.execute(
            "SELECT location FROM wms_pallets WHERE factory_id=? AND status IN ('stored','hold') AND product_code=?"
            " AND COALESCE(location,'')<>'' ORDER BY stored_at DESC LIMIT 30", (fac, p["product_code"]))]
        why = "same product"
    byc = {l["code"]: l for l in locs}
    anc = [byc[a] for a in anchors if a in byc][:30]
    if anc:
        def score(l):
            return min((l["row_code"] != a["row_code"]) * 10000 + abs(l["bay"] - a["bay"]) * 10
                       + abs(l["level"] - a["level"]) * 3 + abs(l["slot"] - a["slot"]) for a in anc)
        best = min(free, key=lambda l: (score(l), l["level"], l["row_code"], l["bay"], l["slot"]))
        return best["code"], why
    best = min(free, key=lambda l: (l["level"], l["row_code"], l["bay"], l["slot"]))
    return best["code"], "first free low slot"


@router.post("/suggest")
async def suggest(req: Request):
    u, fac, _ = ctx(req)
    b = await req.json()
    with closing(conn()) as c:
        _ensure_layout(c, fac)
        p = _find(c, fac, b)
        code, why = _suggest(c, fac, p)
    return {"pallet": _out(p), "suggest": code, "why": why}


@router.post("/putaway")
async def putaway(req: Request):
    """Put a released pallet into a slot, or move a stored one to another slot."""
    u, fac, _ = ctx(req, "store")
    b = await req.json()
    q = parse_qr(b.get("location")) or {}
    code = q.get("loc") or (b.get("location") or "").strip().upper()
    with closing(conn()) as c:
        p = _find(c, fac, b)
        if p["status"] not in ("released", "stored", "hold"):
            raise HTTPException(409, {"msg": f"ย้ายไม่ได้ สถานะ {p['status']} (cannot store a pallet with status {p['status']})"})
        loc = c.execute("SELECT * FROM wms_locations WHERE factory_id=? AND code=?", (fac, code)).fetchone()
        if not loc:
            raise HTTPException(404, {"msg": f"ไม่มีช่อง {code} (no such slot)"})
        if loc["blocked"]:
            raise HTTPException(409, {"msg": f"ช่อง {code} ถูกปิดใช้งาน (slot is blocked)"})
        occ = c.execute("SELECT id,pkey FROM wms_pallets WHERE factory_id=? AND location=? AND status IN ('stored','hold')"
                        " AND id<>?", (fac, code, p["id"])).fetchone()
        if occ:
            raise HTTPException(409, {"msg": f"ช่อง {code} มีพาเลทอยู่แล้ว (slot already holds {occ['pkey']})"})
        to = "hold" if p["status"] == "hold" else "stored"
        c.execute("UPDATE wms_pallets SET location=?, status=?, stored_at=?, updated_at=? WHERE id=?",
                  (code, to, now(), now(), p["id"]))
        _move(c, fac, p["id"], "move" if p["location"] else "putaway", u, from_loc=p["location"], to_loc=code,
              s_from=p["status"], s_to=to)
        c.commit()
        return {"ok": True, "pallet": _out(_pallet(c, fac, p["id"]))}


@router.get("/pallets")
async def pallets(req: Request, status: str = "", q: str = "", product: str = "", exp: str = ""):
    u, fac, _ = ctx(req)
    with closing(conn()) as c:
        n = _ints(settings(c, fac))
        sql, args = "SELECT * FROM wms_pallets WHERE factory_id=?", [fac]
        sts = [s for s in status.split(",") if s in STATUSES]
        if sts:
            sql += " AND status IN (%s)" % ",".join("?" * len(sts))
            args += sts
        if product:
            sql += " AND product_code=?"
            args.append(product)
        if q:
            sql += " AND (lot_no LIKE ? OR product_code LIKE ? OR product_name LIKE ? OR location LIKE ?)"
            args += ["%" + q + "%"] * 4
        rows = [_out(r, n) for r in c.execute(sql + " LIMIT 5000", args)]
    if exp:
        rows = [r for r in rows if r["exp_level"] in exp.split(",")]
    rows.sort(key=fefo_key)
    return {"pallets": rows}


@router.get("/pallet/{pid}")
async def pallet(req: Request, pid: int):
    u, fac, _ = ctx(req)
    with closing(conn()) as c:
        n = _ints(settings(c, fac))
        p = _out(_pallet(c, fac, pid), n)
        moves = [dict(r) for r in c.execute("SELECT * FROM wms_moves WHERE pallet_id=? AND factory_id=? ORDER BY id", (pid, fac))]
        if p["order_id"]:
            o = c.execute("SELECT order_no,customer,truck FROM wms_orders WHERE id=?", (p["order_id"],)).fetchone()
            p["order"] = dict(o) if o else None
    return {"pallet": p, "moves": moves}


@router.post("/pallet/{pid}/hold")
async def hold(req: Request, pid: int):
    """Put a stored pallet on hold (it keeps its slot) — e.g. a complaint on the lot."""
    u, fac, _ = ctx(req, "super")
    b = await req.json()
    note = (b.get("note") or "").strip()
    if not note:
        raise HTTPException(400, {"msg": "กรุณาใส่เหตุผล (a reason is required)"})
    with closing(conn()) as c:
        p = _pallet(c, fac, pid)
        if p["status"] not in ("stored", "released"):
            raise HTTPException(409, {"msg": f"status {p['status']}"})
        c.execute("UPDATE wms_pallets SET status='hold', qc_note=?, updated_at=? WHERE id=?", (note, now(), pid))
        _move(c, fac, pid, "hold", u, from_loc=p["location"], to_loc=p["location"], s_from=p["status"], s_to="hold", note=note)
        c.commit()
    return {"ok": True}


@router.get("/locations")
async def locations(req: Request, row: str = ""):
    u, fac, _ = ctx(req)
    with closing(conn()) as c:
        _ensure_layout(c, fac)
        s = settings(c, fac)
        n = _ints(s)
        rows = row_codes(s["row_start"], n["rows"])
        row = row if row in rows else (rows[0] if rows else "")
        pal = {r["location"]: _out(r, n) for r in c.execute(
            "SELECT * FROM wms_pallets WHERE factory_id=? AND status IN ('stored','hold') AND location LIKE ?",
            (fac, row + "%"))}
        locs = []
        for r in c.execute("SELECT * FROM wms_locations WHERE factory_id=? AND row_code=? ORDER BY level,bay,slot",
                           (fac, row)):
            d = dict(r)
            p = pal.get(d["code"])
            if p:
                d["pallet"] = {k: p[k] for k in ("id", "lot_no", "pallet_no", "product_code", "product_name",
                                                  "exp_date", "mfg_date", "days_left", "exp_level", "status",
                                                  "bags_counted")}
            locs.append(d)
    return {"rows": rows, "row": row, "levels": n["levels"], "bays": n["bays"], "slots": n["slots"], "locations": locs}


@router.post("/locations/block")
async def block(req: Request):
    u, fac, _ = ctx(req, "super")
    b = await req.json()
    with closing(conn()) as c:
        c.execute("UPDATE wms_locations SET blocked=? WHERE factory_id=? AND code=?",
                  (1 if b.get("blocked") else 0, fac, (b.get("code") or "").upper()))
        c.commit()
    return {"ok": True}


@router.get("/settings")
async def get_settings(req: Request):
    u, fac, _ = ctx(req)
    with closing(conn()) as c:
        return settings(c, fac)


@router.post("/settings")
async def set_settings(req: Request):
    u, fac, _ = ctx(req, "super")
    b = await req.json()
    with closing(conn()) as c:
        for k in DEFAULTS:
            if k in b:
                v = str(b[k]).strip().upper() if k == "row_start" else str(int(b[k]))
                c.execute("DELETE FROM wms_settings WHERE factory_id=? AND k=?", (fac, k))
                c.execute("INSERT INTO wms_settings(factory_id,k,v) VALUES(?,?,?)", (fac, k, v))
        added, removed, total = build_layout(c, fac)
        c.commit()
    return {"ok": True, "added": added, "removed": removed, "total": total}


@router.get("/products")
async def products(req: Request):
    u, fac, _ = ctx(req)
    with closing(conn()) as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM wms_products WHERE factory_id=? ORDER BY code", (fac,))]
        stock = {r["product_code"]: r["n"] for r in c.execute(
            "SELECT product_code, COUNT(*) n FROM wms_pallets WHERE factory_id=? AND status IN ('stored','hold','released')"
            " GROUP BY product_code", (fac,))}
    for r in rows:
        r["in_stock"] = stock.get(r["code"], 0)
    return {"products": rows}


@router.post("/products")
async def save_product(req: Request):
    u, fac, _ = ctx(req, "super")
    b = await req.json()
    code = (b.get("code") or "").strip()
    if not code:
        raise HTTPException(400, {"msg": "product code required"})
    days = int(b.get("shelf_life_days") or 0)
    with closing(conn()) as c:
        _product(c, fac, code, b.get("name", ""), b.get("weight", ""))
        c.execute("UPDATE wms_products SET name=COALESCE(NULLIF(?,''),name), shelf_life_days=?, bags_per_pallet=?,"
                  " updated_at=? WHERE factory_id=? AND code=?",
                  (b.get("name", ""), days, int(b.get("bags_per_pallet") or 0), now(), fac, code))
        # pallets whose EXP was calculated (not typed) follow the new shelf life
        n = 0
        for p in c.execute("SELECT id,mfg_date FROM wms_pallets WHERE factory_id=? AND product_code=?"
                           " AND exp_src IN ('calc','') AND COALESCE(mfg_date,'')<>''"
                           " AND status NOT IN ('dispatched','rejected')", (fac, code)).fetchall():
            e = _calc_exp(p["mfg_date"], days)
            c.execute("UPDATE wms_pallets SET exp_date=?, exp_src=? WHERE id=?", (e, "calc" if e else "", p["id"]))
            n += 1
        c.commit()
    return {"ok": True, "pallets_updated": n}


# ---------------------------------------------------------------- dispatch
def _order(c, fac, oid):
    o = c.execute("SELECT * FROM wms_orders WHERE factory_id=? AND id=?", (fac, oid)).fetchone()
    if not o:
        raise HTTPException(404, {"msg": "order not found"})
    return dict(o)


def _order_full(c, fac, oid, n):
    o = _order(c, fac, oid)
    lines = []
    for ln in c.execute("SELECT * FROM wms_order_lines WHERE order_id=? ORDER BY id", (oid,)):
        d = dict(ln)
        pr = c.execute("SELECT name FROM wms_products WHERE factory_id=? AND code=?", (fac, d["product_code"])).fetchone()
        d["product_name"] = pr["name"] if pr else ""
        d["picked"] = [_out(r, n) for r in c.execute(
            "SELECT * FROM wms_pallets WHERE factory_id=? AND order_id=? AND product_code=? AND status IN ('picked','dispatched')",
            (fac, oid, d["product_code"]))]
        cand = [_out(r, n) for r in c.execute(
            "SELECT * FROM wms_pallets WHERE factory_id=? AND status='stored' AND product_code=?",
            (fac, d["product_code"]))]
        cand.sort(key=fefo_key)
        d["available"] = len(cand)
        d["next"] = cand[:max(0, d["qty"] - len(d["picked"]))]
        lines.append(d)
    o["lines"] = lines
    return o


@router.get("/orders")
async def orders(req: Request, status: str = ""):
    u, fac, _ = ctx(req)
    with closing(conn()) as c:
        sql, args = "SELECT * FROM wms_orders WHERE factory_id=?", [fac]
        if status:
            sts = status.split(",")
            sql += " AND status IN (%s)" % ",".join("?" * len(sts))
            args += sts
        out = []
        for o in c.execute(sql + " ORDER BY id DESC LIMIT 300", args).fetchall():
            d = dict(o)
            d["qty"] = c.execute("SELECT COALESCE(SUM(qty),0) FROM wms_order_lines WHERE order_id=?", (o["id"],)).fetchone()[0]
            d["picked"] = c.execute("SELECT COUNT(*) FROM wms_pallets WHERE order_id=? AND factory_id=?", (o["id"], fac)).fetchone()[0]
            out.append(d)
    return {"orders": out}


@router.get("/orders/{oid}")
async def order(req: Request, oid: int):
    u, fac, _ = ctx(req)
    with closing(conn()) as c:
        return _order_full(c, fac, oid, _ints(settings(c, fac)))


@router.post("/orders")
async def new_order(req: Request):
    u, fac, _ = ctx(req, "super")
    b = await req.json()
    no = (b.get("order_no") or "").strip()
    lines = [l for l in (b.get("lines") or []) if (l.get("product_code") or "").strip() and int(l.get("qty") or 0) > 0]
    if not no or not lines:
        raise HTTPException(400, {"msg": "ใส่เลขที่ใบสั่งและสินค้าอย่างน้อย 1 รายการ (order no. and at least one line)"})
    with closing(conn()) as c:
        if c.execute("SELECT 1 FROM wms_orders WHERE factory_id=? AND order_no=?", (fac, no)).fetchone():
            raise HTTPException(409, {"msg": "เลขที่ใบสั่งซ้ำ (order no. already used)"})
        oid = c.insert_id("INSERT INTO wms_orders(factory_id,order_no,customer,truck,due_date,note,created_by,created_at)"
                          " VALUES(?,?,?,?,?,?,?,?)",
                          (fac, no, b.get("customer", ""), b.get("truck", ""), _date(b.get("due_date")),
                           b.get("note", ""), _who(u), now()))
        for l in lines:
            c.execute("INSERT INTO wms_order_lines(order_id,product_code,qty) VALUES(?,?,?)",
                      (oid, l["product_code"].strip(), int(l["qty"])))
        c.commit()
    return {"ok": True, "id": oid}


@router.post("/orders/{oid}/pick")
async def pick(req: Request, oid: int):
    u, fac, wr = ctx(req, "store")
    b = await req.json()
    with closing(conn()) as c:
        n = _ints(settings(c, fac))
        o = _order(c, fac, oid)
        if o["status"] not in ("open", "picking"):
            raise HTTPException(409, {"msg": f"order is {o['status']}"})
        p = _find(c, fac, b)
        if p["status"] != "stored":
            raise HTTPException(409, {"msg": f"หยิบไม่ได้ สถานะ {p['status']} (cannot pick — status {p['status']})"})
        ln = c.execute("SELECT * FROM wms_order_lines WHERE order_id=? AND product_code=?", (oid, p["product_code"])).fetchone()
        if not ln:
            raise HTTPException(409, {"msg": f"สินค้า {p['product_code']} ไม่อยู่ในใบสั่งนี้ (product not on this order)"})
        got = c.execute("SELECT COUNT(*) FROM wms_pallets WHERE order_id=? AND product_code=? AND factory_id=?",
                        (oid, p["product_code"], fac)).fetchone()[0]
        if got >= ln["qty"]:
            raise HTTPException(409, {"msg": "สินค้านี้หยิบครบแล้ว (this line is already complete)"})
        cand = [dict(r) for r in c.execute("SELECT * FROM wms_pallets WHERE factory_id=? AND status='stored' AND product_code=?",
                                           (fac, p["product_code"]))]
        cand.sort(key=fefo_key)
        first = cand[0]
        note = ""
        if _fefo_rank(p) > _fefo_rank(first):
            reason = (b.get("override") or "").strip()
            if not reason or wr != "super":
                raise HTTPException(409, {"msg": f"ผิด FEFO — ต้องหยิบพาเลทที่หมดอายุก่อน: {first['location']} "
                                                 f"(EXP {first['exp_date'] or '-'}) / wrong pallet — pick the earlier-expiry one first",
                                          "fefo": True, "expected": _out(first, n)})
            note = "FEFO override: " + reason
        c.execute("UPDATE wms_pallets SET status='picked', order_id=?, location='', picked_at=?, updated_at=? WHERE id=?",
                  (oid, now(), now(), p["id"]))
        _move(c, fac, p["id"], "pick", u, from_loc=p["location"], s_from="stored", s_to="picked",
              note=(f"order {o['order_no']} " + note).strip())
        if o["status"] == "open":
            c.execute("UPDATE wms_orders SET status='picking' WHERE id=?", (oid,))
        c.commit()
        return _order_full(c, fac, oid, n)


@router.post("/orders/{oid}/unpick")
async def unpick(req: Request, oid: int):
    """Take a picked pallet back off the order. It needs a slot again (put-away)."""
    u, fac, _ = ctx(req, "store")
    b = await req.json()
    with closing(conn()) as c:
        p = _pallet(c, fac, int(b.get("pallet_id") or 0))
        if p["order_id"] != oid or p["status"] != "picked":
            raise HTTPException(409, {"msg": "pallet is not picked on this order"})
        c.execute("UPDATE wms_pallets SET status='released', order_id=0, picked_at='', updated_at=? WHERE id=?", (now(), p["id"]))
        _move(c, fac, p["id"], "unpick", u, s_from="picked", s_to="released")
        c.commit()
        return _order_full(c, fac, oid, _ints(settings(c, fac)))


@router.post("/orders/{oid}/dispatch")
async def dispatch(req: Request, oid: int):
    u, fac, _ = ctx(req, "store")
    b = await req.json()
    with closing(conn()) as c:
        o = _order_full(c, fac, oid, _ints(settings(c, fac)))
        if o["status"] not in ("open", "picking"):
            raise HTTPException(409, {"msg": f"order is {o['status']}"})
        short = sum(max(0, l["qty"] - len(l["picked"])) for l in o["lines"])
        if short and not b.get("partial"):
            raise HTTPException(409, {"msg": f"ยังขาดอีก {short} พาเลท (still {short} pallet(s) short)", "short": short})
        if not any(l["picked"] for l in o["lines"]):
            raise HTTPException(409, {"msg": "ยังไม่ได้หยิบพาเลท (nothing picked)"})
        for l in o["lines"]:
            for p in l["picked"]:
                if p["status"] == "picked":
                    c.execute("UPDATE wms_pallets SET status='dispatched', dispatched_at=?, updated_at=? WHERE id=?",
                              (now(), now(), p["id"]))
                    _move(c, fac, p["id"], "dispatch", u, s_from="picked", s_to="dispatched",
                          note=f"order {o['order_no']} truck {o['truck']}" + (" (partial)" if short else ""))
        c.execute("UPDATE wms_orders SET status='dispatched', dispatched_at=?, truck=COALESCE(NULLIF(?,''),truck) WHERE id=?",
                  (now(), b.get("truck", ""), oid))
        c.commit()
    return {"ok": True}


@router.post("/orders/{oid}/cancel")
async def cancel(req: Request, oid: int):
    u, fac, _ = ctx(req, "super")
    with closing(conn()) as c:
        o = _order(c, fac, oid)
        if o["status"] == "dispatched":
            raise HTTPException(409, {"msg": "already dispatched"})
        for p in c.execute("SELECT id FROM wms_pallets WHERE order_id=? AND factory_id=? AND status='picked'", (oid, fac)).fetchall():
            c.execute("UPDATE wms_pallets SET status='released', order_id=0, updated_at=? WHERE id=?", (now(), p["id"]))
            _move(c, fac, p["id"], "unpick", u, s_from="picked", s_to="released", note="order cancelled")
        c.execute("UPDATE wms_orders SET status='cancelled' WHERE id=?", (oid,))
        c.commit()
    return {"ok": True}


# ---------------------------------------------------------------- trace + users
@router.get("/trace")
async def trace(req: Request, lot: str = ""):
    u, fac, _ = ctx(req)
    lot = lot.strip()
    if not lot:
        return {"pallets": []}
    with closing(conn()) as c:
        n = _ints(settings(c, fac))
        rows = []
        for r in c.execute("SELECT * FROM wms_pallets WHERE factory_id=? AND lot_no LIKE ? ORDER BY lot_no, pallet_no",
                           (fac, "%" + lot + "%")):
            d = _out(r, n)
            if d["order_id"]:
                o = c.execute("SELECT order_no,customer,truck,dispatched_at FROM wms_orders WHERE id=?", (d["order_id"],)).fetchone()
                d["order"] = dict(o) if o else None
            rows.append(d)
    return {"pallets": rows}


@router.get("/users")
async def users(req: Request):
    u, fac, _ = ctx(req, "super")
    with closing(conn()) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id,username,name,role,COALESCE(wms_role,'') wms_role FROM users WHERE active=1"
            " AND COALESCE(can_login,1)=1 AND (factory_id=? OR COALESCE(factory_id,0)=0) ORDER BY name", (fac,))]
    return {"users": rows, "roles": WMS_ROLES}


@router.post("/users/{uid}")
async def set_user(req: Request, uid: int):
    u, fac, _ = ctx(req, "super")
    b = await req.json()
    r = (b.get("wms_role") or "").strip()
    if r and r not in WMS_ROLES:
        raise HTTPException(400, {"msg": "bad role"})
    with closing(conn()) as c:
        c.execute("UPDATE users SET wms_role=? WHERE id=?", (r, uid))
        c.commit()
    return {"ok": True}

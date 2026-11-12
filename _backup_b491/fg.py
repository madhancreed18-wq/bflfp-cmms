"""FG Stock — finished-goods receive / issue / checker. Replaces the FG team's Access
database (Stock_FinishedGoods New.accdb) and the paper form F-WI04-WHD01-04.

A third app inside the CMMS login (after CMMS and Warehouse): same server, same users,
same plant chosen at sign-in. Pages: /fg.html. API: /api/fg/*. Tables: fg_*.

THE FLOW
  1. FG Receiver (phone) scans the pallet-slip QR. The pallet's data is read from the
     PACKING record (fg_packing — see "Packing source" below), shown as a checklist:
     every item OK / NG, NG needs a comment (same idea as the PM checklist).
       Approve  -> a row in fg_receive (status 'waiting'), packing record = 'moved_to_fg'
       Reject   -> a row in fg_receive (status 'rejected', never counted),
                   packing record = 'rejected_by_fg' with the comments
  2. FG Issuer (phone) scans a pallet (or types a lot) that is in stock, fills customer /
     release type / qty / plate, ticks the checklist -> a row in fg_release ('waiting').
  3. Checker (PC) sees everything 'waiting' and confirms -> 'confirmed' = posted to stock.
     Send back -> 'sentback' (a received pallet goes back to 'packed' so it can be
     scanned again; an issue line simply stops reserving stock).

STOCK = confirmed receive − (confirmed + waiting release), per product + lot. Waiting
releases already reserve, so the same bags cannot be issued twice while the checker is
away. Nothing is deleted; every change writes fg_log.

PACKING SOURCE. The real packing-room database is not connected yet. Until it is,
fg_packing holds the packing record: a slip QR that is not there yet is added from the
QR's own JSON on first scan (src='qr'), and admins can key a record by hand. When the
packing DB is given, replace packing_lookup() / packing_mark() — nothing else changes.

ACCESS HISTORY. import_access() copies tbl_Product, tbl_Customer, tbl_ProductType,
tbl_Checker, tbl_EMPshift, tbl_Receive_Product and tbl_Release_Product (as 'confirmed',
src='access'). It is keyed on the Access ID, so it can be re-run while the team is still
using Access before go-live — it only adds rows it has not seen. Run:
    python import_fg_access.py --plant PC --csv fg_import        (CSV export)
    python import_fg_access.py --plant PC --accdb "...accdb"      (Windows + Access driver)

ROLES (users.fg_role, set on FG › Users): admin · receiver · issuer · checker.
CMMS admins are always FG admin. users.fg_only = 1 sends the login straight to /fg.html.
"""
import csv
import datetime
import json
import os
import re
import time
from contextlib import closing

from fastapi import APIRouter, Request, HTTPException

from .db import db, now, hash_pw, _table_cols
from .auth import user_from

router = APIRouter(prefix="/api/fg")

FG_ROLES = ("admin", "packing", "packcheck", "receiver", "issuer", "checker")
CAN = {
    "admin": {"view", "pack", "packcheck", "receive", "issue", "check", "admin"},
    "packing": {"view", "pack"},                 # packing room: make + print the A6 QR slip
    "packcheck": {"view", "pack", "packcheck"},  # packing-room checker: approve / reject slips
    "checker": {"view", "check"},
    "receiver": {"view", "receive"},
    "issuer": {"view", "issue"},
}
PACK_TYPES = ("Finished Goods", "Repack")
DEFAULT_LISTS = {   # the old VB pallet-slip app's lists — editable in FG › Lists
    "machine": ("Pack 1 - Rovema", "Pack 2 - Form Fill", "Pack 3 - Rotary Pack", "Pack 4 - Statec", "Pack 6 - Chronos"),
    "line": ("Extruder Line 1", "Extruder Line 2", "Extruder Line 3", "Extruder 1 & 2", "Extruder 2 & 3", "Extruder 1 & 3"),
}
RELEASE_TYPES = ("ขาย", "อื่นๆ", "ชำรุด", "Repack")          # sale · other · damaged · repack
GET_TYPES = ("รับเข้าจากแพคกิ้ง", "รับเข้างานรีแพค")          # from packing · from repack


# ---------------------------------------------------------------- schema
def ensure(c):
    c.execute("""CREATE TABLE IF NOT EXISTS fg_products(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER, code TEXT, name TEXT DEFAULT '',
        type_id TEXT DEFAULT '', packaging TEXT DEFAULT '', weight REAL DEFAULT 0,
        shelf_life_days INTEGER DEFAULT 0, owner TEXT DEFAULT '', active INTEGER DEFAULT 1,
        updated_at TEXT DEFAULT '', UNIQUE(factory_id, code))""")
    c.execute("""CREATE TABLE IF NOT EXISTS fg_product_types(
        factory_id INTEGER, code TEXT, name TEXT DEFAULT '', PRIMARY KEY(factory_id, code))""")
    c.execute("""CREATE TABLE IF NOT EXISTS fg_customers(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER, code TEXT, name TEXT DEFAULT '',
        active INTEGER DEFAULT 1, UNIQUE(factory_id, code))""")
    c.execute("""CREATE TABLE IF NOT EXISTS fg_people(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER, kind TEXT, name TEXT,
        active INTEGER DEFAULT 1, UNIQUE(factory_id, kind, name))""")       # kind: checker | leader
    c.execute("""CREATE TABLE IF NOT EXISTS fg_packing(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER, pkey TEXT,
        lot_no TEXT DEFAULT '', pallet_no TEXT DEFAULT '', product_code TEXT DEFAULT '',
        product_name TEXT DEFAULT '', packaging TEXT DEFAULT '', qty REAL DEFAULT 0,
        unit TEXT DEFAULT 'กระสอบ', weight_unit REAL DEFAULT 0, mfg TEXT DEFAULT '', exp TEXT DEFAULT '',
        station TEXT DEFAULT '', operator TEXT DEFAULT '', raw_qr TEXT DEFAULT '',
        packed_at TEXT DEFAULT '', src TEXT DEFAULT 'qr',
        status TEXT DEFAULT 'packed', status_at TEXT DEFAULT '', status_by TEXT DEFAULT '',
        note TEXT DEFAULT '', UNIQUE(factory_id, pkey))""")
    pcols = _table_cols(c, "fg_packing")
    for col, typ in (("slip_no", "INTEGER DEFAULT 0"), ("doc_no", "TEXT DEFAULT ''"), ("pack_machine", "TEXT DEFAULT ''"),
                     ("pd_line", "TEXT DEFAULT ''"), ("pack_type", "TEXT DEFAULT ''"), ("bag_weight", "TEXT DEFAULT ''"),
                     ("total_weight", "REAL DEFAULT 0"), ("staff_name", "TEXT DEFAULT ''"),
                     ("pc_status", "TEXT DEFAULT 'approved'"), ("pc_by", "TEXT DEFAULT ''"), ("pc_at", "TEXT DEFAULT ''"),
                     ("pc_note", "TEXT DEFAULT ''"), ("created_by", "TEXT DEFAULT ''"), ("edited_by", "TEXT DEFAULT ''"),
                     ("edited_at", "TEXT DEFAULT ''"), ("legacy_code", "TEXT DEFAULT ''"),
                     ("print_count", "INTEGER DEFAULT 0"), ("printed_at", "TEXT DEFAULT ''"),
                     ("fg_reject_at", "TEXT DEFAULT ''"), ("fixed_at", "TEXT DEFAULT ''")):
        if col not in pcols:
            c.execute(f"ALTER TABLE fg_packing ADD COLUMN {col} {typ}")
    c.execute("CREATE INDEX IF NOT EXISTS ix_fg_pack_lot ON fg_packing(factory_id, product_code, lot_no)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_fg_pack_leg ON fg_packing(factory_id, legacy_code)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_fg_pack_pc ON fg_packing(factory_id, pc_status)")
    c.execute("""CREATE TABLE IF NOT EXISTS fg_receive(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER, src TEXT DEFAULT 'app',
        access_id INTEGER, pkey TEXT DEFAULT '', lot_no TEXT DEFAULT '', pallet_no TEXT DEFAULT '',
        product_id TEXT DEFAULT '', product_name TEXT DEFAULT '', packaging TEXT DEFAULT '',
        qty REAL DEFAULT 0, unit TEXT DEFAULT '', weight_unit REAL DEFAULT 0, total_kg REAL DEFAULT 0,
        mfg TEXT DEFAULT '', exp TEXT DEFAULT '', net_weight TEXT DEFAULT '',
        receive_date TEXT DEFAULT '', receive_time TEXT DEFAULT '', receive_man TEXT DEFAULT '',
        receive_type TEXT DEFAULT 'FG', get_type TEXT DEFAULT '', location TEXT DEFAULT '',
        memo TEXT DEFAULT '', target_weight REAL DEFAULT 0, packing_id INTEGER DEFAULT 0,
        checklist TEXT DEFAULT '', recv_by TEXT DEFAULT '', recv_at TEXT DEFAULT '',
        status TEXT DEFAULT 'waiting', checker TEXT DEFAULT '', checked_by TEXT DEFAULT '',
        checked_at TEXT DEFAULT '', note TEXT DEFAULT '', log TEXT DEFAULT '')""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_fg_recv_st ON fg_receive(factory_id, status)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_fg_recv_lot ON fg_receive(factory_id, product_id, lot_no)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_fg_recv_pkey ON fg_receive(factory_id, pkey)")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_fg_recv_acc ON fg_receive(factory_id, src, access_id)")
    c.execute("""CREATE TABLE IF NOT EXISTS fg_release(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER, src TEXT DEFAULT 'app',
        access_id INTEGER, release_no TEXT DEFAULT '', lot_no TEXT DEFAULT '', pkey TEXT DEFAULT '',
        receive_id INTEGER DEFAULT 0, product_id TEXT DEFAULT '', product_name TEXT DEFAULT '',
        packaging TEXT DEFAULT '', qty REAL DEFAULT 0, unit TEXT DEFAULT '', total_kg REAL DEFAULT 0,
        release_date TEXT DEFAULT '', release_time TEXT DEFAULT '', release_end TEXT DEFAULT '',
        release_man TEXT DEFAULT '', customer_id TEXT DEFAULT '', release_type TEXT DEFAULT '',
        memo TEXT DEFAULT '', plate TEXT DEFAULT '', checklist TEXT DEFAULT '',
        issue_by TEXT DEFAULT '', issue_at TEXT DEFAULT '', status TEXT DEFAULT 'waiting',
        checker TEXT DEFAULT '', checked_by TEXT DEFAULT '', checked_at TEXT DEFAULT '',
        note TEXT DEFAULT '', log TEXT DEFAULT '')""")
    rcols = _table_cols(c, "fg_release")
    for col, typ in (("issue_mode", "TEXT DEFAULT ''"), ("pallet_bags", "REAL DEFAULT 0"),
                     ("photo", "TEXT DEFAULT ''"), ("photo_at", "TEXT DEFAULT ''"), ("photo_saved_at", "TEXT DEFAULT ''")):
        if col not in rcols:
            c.execute(f"ALTER TABLE fg_release ADD COLUMN {col} {typ}")
    c.execute("CREATE INDEX IF NOT EXISTS ix_fg_rel_st ON fg_release(factory_id, status)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_fg_rel_lot ON fg_release(factory_id, product_id, lot_no)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_fg_rel_rid ON fg_release(receive_id)")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_fg_rel_acc ON fg_release(factory_id, src, access_id)")
    # b481 speed: date lookups for history / today counters / stock-cache signature
    for ix, t, cols in (("ix_fg_recv_d", "fg_receive", "factory_id, receive_date"), ("ix_fg_recv_at", "fg_receive", "factory_id, recv_at"),
                        ("ix_fg_recv_ck", "fg_receive", "factory_id, checked_at"), ("ix_fg_rel_d", "fg_release", "factory_id, release_date"),
                        ("ix_fg_rel_at", "fg_release", "factory_id, issue_at"), ("ix_fg_rel_ck", "fg_release", "factory_id, checked_at"),
                        ("ix_fg_recv_stq", "fg_receive", "factory_id, status, product_id, lot_no, qty"),
                        ("ix_fg_rel_stq", "fg_release", "factory_id, status, product_id, lot_no, qty")):
        c.execute(f"CREATE INDEX IF NOT EXISTS {ix} ON {t}({cols})")
    # change counter: any insert / update / delete on receive or release bumps it, so the
    # cached stock balance knows when to recompute (covers the app, the importers and hand edits)
    try:
        c.execute("CREATE TABLE IF NOT EXISTS fg_ver(k TEXT PRIMARY KEY, v INTEGER DEFAULT 0)")
        c.execute("INSERT OR IGNORE INTO fg_ver(k, v) VALUES('stock', 0)")
        for t in ("fg_receive", "fg_release"):
            for ev in ("INSERT", "UPDATE", "DELETE"):
                c.execute(f"""CREATE TRIGGER IF NOT EXISTS tr_{t}_{ev.lower()}_ver AFTER {ev} ON {t}
                              BEGIN UPDATE fg_ver SET v = v + 1 WHERE k = 'stock'; END""")
    except Exception:
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS fg_lists(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER DEFAULT 0, kind TEXT, name TEXT,
        sort INTEGER DEFAULT 0, active INTEGER DEFAULT 1)""")
    if not c.execute("SELECT 1 FROM fg_lists LIMIT 1").fetchone():
        for kind, names in DEFAULT_LISTS.items():
            for i, n in enumerate(names):
                c.execute("INSERT INTO fg_lists(factory_id,kind,name,sort,active) VALUES(0,?,?,?,1)", (kind, n, i))
    c.execute("""CREATE TABLE IF NOT EXISTS fg_plants(factory_id INTEGER PRIMARY KEY, enabled INTEGER DEFAULT 1)""")
    c.execute("""CREATE TABLE IF NOT EXISTS fg_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER, kind TEXT, ref_id INTEGER,
        action TEXT, note TEXT DEFAULT '', user_name TEXT DEFAULT '', created_at TEXT DEFAULT '')""")
    cols = _table_cols(c, "users")
    if "fg_role" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN fg_role TEXT DEFAULT ''")
    if "fg_only" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN fg_only INTEGER DEFAULT 0")
    # b471: one person, several functions. fg_roles = comma list, fg_default = what opens first
    # ('cmms' or one of the roles). fg_role is kept = the first role, for older code.
    if "fg_roles" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN fg_roles TEXT DEFAULT ''")
        c.execute("UPDATE users SET fg_roles=COALESCE(fg_role,'') WHERE COALESCE(fg_role,'')<>''")
    if "fg_default" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN fg_default TEXT DEFAULT ''")


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
FN_ORDER = ("packing", "packcheck", "receiver", "issuer", "checker")


def _clean_roles(v):
    if isinstance(v, str):
        v = v.split(",")
    out = [r.strip() for r in (v or []) if r and r.strip() in FG_ROLES and r.strip() != "admin"]
    return [r for r in FN_ORDER if r in out]


def roles_of(u):
    """All FG roles of a person (admin -> ['admin'])."""
    if u.get("role") == "admin":
        return ["admin"]
    try:
        with closing(conn()) as c:
            r = c.execute("SELECT COALESCE(fg_roles,'') rs, COALESCE(fg_role,'') r FROM users WHERE id=?",
                          (u["id"],)).fetchone()
        if not r:
            return []
        return _clean_roles(r["rs"] or r["r"])
    except Exception:
        return []


def role_of(u):
    """Main FG role ('' = no FG access) — the first of roles_of()."""
    rs = roles_of(u)
    return rs[0] if rs else ""


def fg_default(u):
    if u.get("role") == "admin":
        return "cmms"
    try:
        with closing(conn()) as c:
            r = c.execute("SELECT COALESCE(fg_default,'') d FROM users WHERE id=?", (u["id"],)).fetchone()
        return (r["d"] if r else "") or ""
    except Exception:
        return ""


def can_set(roles):
    out = set()
    for r in roles:
        out |= CAN.get(r, set())
    return out


def fg_only(u):
    if u.get("role") == "admin":
        return False
    try:
        with closing(conn()) as c:
            r = c.execute("SELECT COALESCE(fg_only,0) o FROM users WHERE id=?", (u["id"],)).fetchone()
        return bool(r and r["o"])
    except Exception:
        return False


def plant_on(c, fac):
    """Is FG Stock used in this plant? Set by admin on the app screen; until then a plant
    counts as on when it already holds FG data (today: BFLPC from the Access import)."""
    r = c.execute("SELECT enabled FROM fg_plants WHERE factory_id=?", (fac,)).fetchone()
    if r:
        return bool(r["enabled"])
    return bool(c.execute("SELECT 1 FROM fg_products WHERE factory_id=? LIMIT 1", (fac,)).fetchone()
                or c.execute("SELECT 1 FROM fg_packing WHERE factory_id=? LIMIT 1", (fac,)).fetchone())


def ctx(req: Request, need="view"):
    u = user_from(req)
    fac = u.get("active_factory") or u.get("factory_id")
    rs = roles_of(u)
    fr = rs[0] if rs else ""
    if need and need not in can_set(rs):
        raise HTTPException(403, {"msg": "ไม่มีสิทธิ์ (no permission) — ask the FG admin"})
    if not fac:
        raise HTTPException(400, {"msg": "เลือกโรงงานก่อน (choose a plant at sign-in)"})
    if "admin" not in rs:
        with closing(conn()) as c:
            if not plant_on(c, fac):
                raise HTTPException(403, {"msg": "โรงงานนี้ยังไม่ได้เปิดใช้ FG Stock (FG Stock is not used in this plant)"})
    return u, fac, fr


def _who(u):
    return u.get("name") or u.get("username") or ""


def _log(c, fac, kind, ref, action, note, who):
    c.execute("INSERT INTO fg_log(factory_id,kind,ref_id,action,note,user_name,created_at) VALUES(?,?,?,?,?,?,?)",
              (fac, kind, ref, action, (note or "")[:500], who, now()))


def _num(v, default=0.0):
    try:
        return float(str(v).replace(",", "").strip() or default)
    except (TypeError, ValueError):
        return default


def _today():
    return datetime.date.today().isoformat()


# ---------------------------------------------------------------- QR + dates
def parse_qr(text):
    """Pallet-slip QR (JSON) -> dict; anything else -> None.
    {"Product_code","Product_Name","Lot_No","No"(bags),"weight","Station","Pallet","Operator"}"""
    t = (text or "").strip()
    if not t.startswith("{"):
        return None
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
    prod = k.get("product_code", "")
    return {"pkey": pkey_of(prod, lot, pal), "lot_no": lot, "pallet_no": pal, "id": k.get("id", ""),
            "product_code": k.get("product_code", ""), "product_name": k.get("product_name", ""),
            "weight": k.get("weight", ""), "qty": _num(re.sub(r"[^\d.]", "", k.get("no", "")) or 0),
            "station": k.get("station", ""), "operator": k.get("operator", ""), "raw_qr": t}


def pkey_of(product, lot, pallet):
    """A pallet = product + lot + slip number (one lot can carry several products, and the
    slip number restarts per lot + product — same rule as the old packing app)."""
    return f"{(product or '').strip()}|{(lot or '').strip()}|{str(pallet).strip()}"


EAN_RE = re.compile(r"^\d{12,13}$")


def mfg_from_lot(lot):
    m = re.match(r"^\s*(\d{2})(\d{2})(\d{2})", lot or "")
    if not m:
        return ""
    try:
        return datetime.date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return ""


def _add_months(d, months):
    y, m = d.year + (d.month - 1 + months) // 12, (d.month - 1 + months) % 12 + 1
    for day in (d.day, 30, 29, 28):
        try:
            return datetime.date(y, m, day)
        except ValueError:
            pass
    return d


def exp_for(mfg, shelf_days):
    if not mfg or not shelf_days:
        return ""
    try:
        return (datetime.date.fromisoformat(mfg) + datetime.timedelta(days=int(shelf_days))).isoformat()
    except ValueError:
        return ""


def _product(c, fac, code):
    r = c.execute("SELECT * FROM fg_products WHERE factory_id=? AND code=?", (fac, code)).fetchone()
    return dict(r) if r else None


def group_of(owner):
    o = (owner or "").strip().lower()
    if o.startswith("brand"):
        return "Brand"
    if o.startswith("oem"):
        return "B2B"
    return "BB" if o in ("bb", "bulk") else ""


# ---------------------------------------------------------------- packing source
def packing_lookup(c, fac, slip, who):
    """The packing record for a scanned slip. TODAY: fg_packing, created from the QR the
    first time it is seen. LATER: read the real packing DB here."""
    r = c.execute("SELECT * FROM fg_packing WHERE factory_id=? AND pkey=?", (fac, slip["pkey"])).fetchone()
    if r:
        return dict(r)
    p = _product(c, fac, slip["product_code"]) or {}
    wt = _num(p.get("weight")) or _num(re.sub(r"[^\d.]", "", slip.get("weight") or ""))
    mfg = mfg_from_lot(slip["lot_no"])
    pid = c.insert_id("""INSERT INTO fg_packing(factory_id,pkey,lot_no,pallet_no,product_code,product_name,
        packaging,qty,unit,weight_unit,mfg,exp,station,operator,raw_qr,packed_at,src,status,status_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'qr','packed',?)""",
        (fac, slip["pkey"], slip["lot_no"], slip["pallet_no"], slip["product_code"],
         slip["product_name"] or p.get("name", ""), p.get("packaging", ""), slip["qty"], "กระสอบ", wt,
         mfg, exp_for(mfg, p.get("shelf_life_days")), slip["station"], slip["operator"],
         slip["raw_qr"], now(), now()))
    _log(c, fac, "packing", pid, "created_from_qr", "", who)
    return dict(c.execute("SELECT * FROM fg_packing WHERE id=?", (pid,)).fetchone())


def packing_mark(c, fac, pid, status, who, note=""):
    """Write the FG decision back to the packing record ('moved_to_fg' / 'rejected_by_fg' /
    'packed'). LATER: also update the real packing DB here."""
    c.execute("UPDATE fg_packing SET status=?,status_at=?,status_by=?,note=? WHERE id=? AND factory_id=?",
              (status, now(), who, (note or "")[:500], pid, fac))
    _log(c, fac, "packing", pid, status, note, who)


# ---------------------------------------------------------------- checklists
def recv_items(pk):
    tot = _num(pk["qty"]) * _num(pk["weight_unit"])
    me = (_fmt_d(pk.get("mfg")) or "-") + " → " + (_fmt_d(pk.get("exp")) or "-")
    return [
        {"id": "type", "th": "ประเภท", "en": "Type", "value": "FG"},
        {"id": "pallet", "th": "เลขพาเลท", "en": "Pallet No.", "value": pk["pallet_no"]},
        {"id": "product", "th": "เบอร์อาหาร", "en": "Product", "value": f'{pk["product_code"]} · {pk["product_name"]}'},
        {"id": "lot", "th": "ล็อต", "en": "Lot No.", "value": pk["lot_no"]},
        {"id": "pack", "th": "บรรจุภัณฑ์", "en": "Packaging", "value": pk.get("packaging") or "-"},
        {"id": "qty", "th": "จำนวน/พาเลท", "en": "Qty / pallet", "value": f'{_n(pk["qty"])} {pk.get("unit") or ""}'},
        {"id": "wt", "th": "น้ำหนัก/หน่วย", "en": "Weight / unit", "value": f'{_n(pk["weight_unit"])} kg'},
        {"id": "total", "th": "รวมน้ำหนัก", "en": "Total weight", "value": f'{_n(tot)} kg'},
        {"id": "mfgexp", "th": "วันผลิต/หมดอายุ บนถุง", "en": "MFG / EXP on bag", "value": me},
        {"id": "op", "th": "ผู้ดำเนินการ", "en": "Packed by", "value": " · ".join(x for x in (pk.get("pd_line"), pk.get("station"), pk.get("operator")) if x) or "-"},
        {"id": "loc", "th": "แหล่งจัดเก็บ", "en": "Store to", "value": "คลังสินค้า (Warehouse)"},
    ]


def issue_items(s, b):
    return [
        {"id": "product", "th": "เบอร์อาหาร", "en": "Product", "value": f'{s["product_id"]} · {s["product_name"]}'},
        {"id": "lot", "th": "ล็อต", "en": "Lot No.", "value": s["lot_no"]},
        {"id": "qty", "th": "จำนวนบนพาเลท", "en": "Bags on pallet", "value": f'{_n(b.get("pallet_bags"))} {s.get("unit") or ""}'},
        {"id": "customer", "th": "ลูกค้า", "en": "Customer", "value": b.get("customer_id") or "-"},
        {"id": "type", "th": "ประเภทจ่าย", "en": "Release type", "value": b.get("release_type") or "-"},
        {"id": "plate", "th": "ทะเบียนรถ", "en": "Truck plate", "value": b.get("plate") or "-"},
    ]


def _check(items, checks):
    """checks = {id: {"v": "ok"|"ng", "note": ""}} -> (all_ok, any_ng, ng_missing_note, stored)"""
    out, any_ng, missing = [], False, False
    for it in items:
        ch = (checks or {}).get(it["id"]) or {}
        v = ch.get("v") if ch.get("v") in ("ok", "ng") else ""
        note = (ch.get("note") or "").strip()[:200]
        if v == "ng":
            any_ng = True
            missing = missing or not note
        out.append(dict(it, v=v, note=note))
    all_ok = all(x["v"] == "ok" for x in out)
    return all_ok, any_ng, missing, out


def _n(v):
    f = _num(v)
    return f"{int(f):,}" if f == int(f) else f"{f:,.2f}"


def _fmt_d(s):
    return f"{s[8:10]}/{s[5:7]}/{s[0:4]}" if s and len(s) >= 10 else ""


# ---------------------------------------------------------------- stock maths
BAL_SQL = """
SELECT p, l, SUM(rq) rq, SUM(sq) sq FROM (
  SELECT product_id p, lot_no l, qty rq, 0 sq FROM fg_receive WHERE factory_id=:f AND status='confirmed'
  UNION ALL
  SELECT product_id p, lot_no l, 0 rq, qty sq FROM fg_release WHERE factory_id=:f AND status IN ('confirmed','waiting')
) t {where} GROUP BY p, l"""


_BAL_CACHE = {}   # factory -> (signature, time, rows): the whole-plant balance is re-used until data changes


def _bal_sig(c, fac):
    try:
        return c.execute("SELECT v FROM fg_ver WHERE k='stock'").fetchone()[0]
    except Exception:
        return None   # no counter table -> fall back to the time limit below


def lot_balances(c, fac, product=None, lot=None):
    if not product and not lot:
        sig, now = _bal_sig(c, fac), time.time()
        hit = _BAL_CACHE.get(fac)
        if hit and hit[0] == sig and now - hit[1] < (300 if sig is not None else 20):
            return hit[2]
        rows = _lot_balances(c, fac)
        _BAL_CACHE[fac] = (sig, now, rows)
        return rows
    return _lot_balances(c, fac, product, lot)


def _lot_balances(c, fac, product=None, lot=None):
    # filters go inside both halves of the UNION so SQLite can use the (factory, status, product, lot) indexes
    w, args = "", []
    if product:
        w += " AND product_id=?"
        args.append(product)
    if lot:
        w += " AND lot_no=?"
        args.append(lot)
    sql = f"""SELECT p, l, SUM(rq) rq, SUM(sq) sq FROM (
  SELECT product_id p, lot_no l, qty rq, 0 sq FROM fg_receive WHERE factory_id=? AND status='confirmed'{w}
  UNION ALL
  SELECT product_id p, lot_no l, 0 rq, qty sq FROM fg_release WHERE factory_id=? AND status IN ('confirmed','waiting'){w}
) t GROUP BY p, l"""
    return [dict(r) for r in c.execute(sql, tuple([fac] + args + [fac] + args))]


def pallet_left(c, rid, qty):
    used = c.execute("SELECT COALESCE(SUM(qty),0) s FROM fg_release WHERE receive_id=? AND status IN ('confirmed','waiting')",
                     (rid,)).fetchone()["s"]
    return _num(qty) - _num(used)


# ================================================================ API
@router.get("/me")
async def me(req: Request):
    u, fac, fr = ctx(req, "view")
    with closing(conn()) as c:
        f = c.execute("SELECT code,name FROM factories WHERE id=?", (fac,)).fetchone()
        checkers = [r["name"] for r in c.execute(
            "SELECT name FROM fg_people WHERE factory_id=? AND kind='checker' AND active=1 ORDER BY name", (fac,))]
    rs = roles_of(u)
    return {"id": u["id"], "name": _who(u), "username": u.get("username", ""), "fg_role": fr,
            "fg_roles": rs, "fg_default": fg_default(u), "cmms_role": u.get("role"),
            "can": sorted(can_set(rs)), "role_can": {r: sorted(CAN.get(r, set())) for r in FG_ROLES}, "factory_id": fac,
            "factory_code": f["code"] if f else "", "factory_name": f["name"] if f else "",
            "fg_only": fg_only(u), "checkers": checkers, "release_types": RELEASE_TYPES}


@router.get("/summary")
async def summary(req: Request):
    u, fac, fr = ctx(req, "view")
    d0 = _today()
    with closing(conn()) as c:
        q = lambda sql, *a: c.execute(sql, (fac,) + a).fetchone()[0]
        out = {
            "recv_waiting": q("SELECT COUNT(*) FROM fg_receive WHERE factory_id=? AND status='waiting'"),
            "issue_waiting": q("SELECT COUNT(*) FROM fg_release WHERE factory_id=? AND status='waiting'"),
            "recv_today": q("SELECT COUNT(*) FROM fg_receive WHERE factory_id=? AND src='app' AND status IN ('waiting','confirmed') AND recv_at>=?", d0),
            "issue_today": q("SELECT COUNT(*) FROM fg_release WHERE factory_id=? AND src='app' AND status IN ('waiting','confirmed') AND issue_at>=?", d0),
            "rejected_today": q("SELECT COUNT(*) FROM fg_receive WHERE factory_id=? AND status='rejected' AND recv_at>=?", d0),
            "confirmed_today": q("SELECT COUNT(*) FROM fg_receive WHERE factory_id=? AND status='confirmed' AND checked_at>=?", d0)
                               + q("SELECT COUNT(*) FROM fg_release WHERE factory_id=? AND status='confirmed' AND checked_at>=?", d0),
        }
        mine = []
        rs = roles_of(u)
        if {"receiver", "admin"} & set(rs):
            mine += [dict(r, kind="recv") for r in c.execute(
                """SELECT id,lot_no,pallet_no,product_name,qty,status,recv_at at FROM fg_receive
                   WHERE factory_id=? AND src='app' AND recv_by=? AND recv_at>=? ORDER BY id DESC LIMIT 30""",
                (fac, _who(u), d0))]
        if {"issuer", "admin"} & set(rs):
            mine += [dict(r, kind="issue") for r in c.execute(
                """SELECT id,lot_no,'' pallet_no,product_name,qty,status,issue_at at,customer_id FROM fg_release
                   WHERE factory_id=? AND src='app' AND issue_by=? AND issue_at>=? ORDER BY id DESC LIMIT 30""",
                (fac, _who(u), d0))]
        mine.sort(key=lambda r: r["at"] or "", reverse=True)
        out["access_rows"] = q("SELECT COUNT(*) FROM fg_receive WHERE factory_id=? AND src='access'") + \
            q("SELECT COUNT(*) FROM fg_release WHERE factory_id=? AND src='access'")
        bal = lot_balances(c, fac)
        out["stock_lots"] = sum(1 for b in bal if (b["rq"] or 0) - (b["sq"] or 0) > 0)
        out["mine"] = mine[:30]
    return out


# ---------------------------------------------------------------- receive (phone)
@router.post("/scan/receive")
async def scan_receive(req: Request):
    u, fac, _ = ctx(req, "receive")
    b = await req.json()
    text = (b.get("text") or "").strip()
    slip = parse_qr(text)
    with closing(conn()) as c:
        if slip:
            pk = packing_lookup(c, fac, slip, _who(u))
            c.commit()
        elif EAN_RE.match(text):
            # an old EAN-13 slip from the VB packing app (imported from PalletSlip)
            r = c.execute("SELECT * FROM fg_packing WHERE factory_id=? AND legacy_code=?", (fac, text[:12])).fetchone()
            if not r:
                raise HTTPException(404, {"msg": "ไม่พบบาร์โค้ดนี้ในข้อมูลแพคกิ้งเดิม (old barcode not found)"})
            pk = dict(r)
        else:
            raise HTTPException(400, {"msg": "ไม่ใช่ QR ใบพาเลท (not a pallet-slip QR)"})
        done = _already_received(c, fac, pk)
        if done:
            return {"packing": pk, "already": done}
        if pk.get("status") == "cancelled":
            raise HTTPException(409, {"msg": "ใบพาเลทนี้ถูกยกเลิก (this slip was cancelled)"})
        if pk.get("pc_status") == "pending":
            raise HTTPException(409, {"msg": "ใบพาเลทยังไม่ผ่าน Checker แพคกิ้ง (waiting for the packing checker)"})
        if pk.get("pc_status") == "rejected":
            raise HTTPException(409, {"msg": "Checker แพคกิ้งไม่อนุมัติ (rejected by the packing checker): " + (pk.get("pc_note") or "")})
        if pk["status"] == "moved_to_fg":
            r = c.execute("SELECT status,recv_by,recv_at FROM fg_receive WHERE factory_id=? AND packing_id=? "
                          "AND status IN ('waiting','confirmed') ORDER BY id DESC", (fac, pk["id"])).fetchone()
            raise HTTPException(409, {"msg": "พาเลทนี้รับเข้า FG แล้ว (already moved to FG)"
                                             + (f" — {r['recv_by']} {r['recv_at'][:16]}" if r else "")})
        prev = None
        if pk["status"] == "rejected_by_fg":
            prev = pk["note"]
    return {"packing": pk, "items": recv_items(pk), "previous_reject": prev}


def _already_received(c, fac, pk):
    """b483: a pallet that is already in FG — received on a phone (waiting / confirmed), or a lot that
    the old Access system already received (the imported history counts as approved). Read-only view."""
    r = c.execute("""SELECT * FROM fg_receive WHERE factory_id=? AND src='app' AND status IN ('waiting','confirmed')
                     AND (packing_id=? OR (pkey<>'' AND pkey=?)) ORDER BY id DESC""",
                  (fac, pk.get("id") or -1, pk.get("pkey") or "")).fetchone()
    if r:
        d = _recv_row(r, _prods(c, fac))
        d["log"] = [dict(x) for x in c.execute(
            "SELECT action,note,user_name,created_at FROM fg_log WHERE factory_id=? AND kind='receive' AND ref_id=? ORDER BY id",
            (fac, d["id"]))]
        return {"src": "app", "rows": [d]}
    sel = "SELECT id,receive_date,receive_time,qty,unit,total_kg,receive_man,get_type,location,checker,status,pallet_no FROM fg_receive"
    rows = [dict(x) for x in c.execute(sel + " WHERE factory_id=? AND src='access' AND packing_id=? AND packing_id>0", (fac, pk.get("id") or -1))]
    if rows:
        return {"src": "access", "rows": rows, "pallet": True}
    rows = [dict(x) for x in c.execute(
        sel + """ WHERE factory_id=? AND src='access' AND status='confirmed' AND product_id=? AND lot_no=?
           ORDER BY receive_date DESC, id DESC LIMIT 30""", (fac, pk.get("product_code") or "", pk.get("lot_no") or ""))]
    if rows:
        return {"src": "access", "rows": rows}
    return None


@router.post("/receive")
async def receive(req: Request):
    u, fac, _ = ctx(req, "receive")
    b = await req.json()
    decision = b.get("decision")
    if decision not in ("approve", "reject"):
        raise HTTPException(400, {"msg": "bad decision"})
    who = _who(u)
    with closing(conn()) as c:
        pk = c.execute("SELECT * FROM fg_packing WHERE id=? AND factory_id=?", (b.get("packing_id"), fac)).fetchone()
        if not pk:
            raise HTTPException(404, {"msg": "ไม่พบข้อมูลแพคกิ้ง (packing record not found)"})
        pk = dict(pk)
        if pk["status"] == "moved_to_fg" or _already_received(c, fac, pk):
            raise HTTPException(409, {"msg": "พาเลทนี้รับเข้า FG แล้ว (already moved to FG)"})
        if pk.get("pc_status") in ("pending", "rejected") or pk["status"] == "cancelled":
            raise HTTPException(409, {"msg": "ใบพาเลทนี้ยังรับเข้าไม่ได้ (slip not approved by packing)"})
        all_ok, any_ng, missing, stored = _check(recv_items(pk), b.get("checks"))
        if decision == "approve" and not all_ok:
            raise HTTPException(400, {"msg": "ต้อง OK ทุกข้อจึงอนุมัติได้ (every item must be OK to approve)"})
        if decision == "reject" and (not any_ng or missing):
            raise HTTPException(400, {"msg": "ไม่อนุมัติ: ต้องมีข้อ NG พร้อมเหตุผล (reject needs an NG item with a comment)"})
        note = (b.get("note") or "").strip()[:300]
        tot = _num(pk["qty"]) * _num(pk["weight_unit"])
        net = ""
        if pk.get("mfg") or pk.get("exp"):
            net = f"MFG {_fmt_d(pk.get('mfg'))}\nEXP {_fmt_d(pk.get('exp'))}"
        rid = c.insert_id("""INSERT INTO fg_receive(factory_id,src,pkey,lot_no,pallet_no,product_id,product_name,
            packaging,qty,unit,weight_unit,total_kg,mfg,exp,net_weight,receive_date,receive_time,receive_man,
            receive_type,get_type,location,memo,packing_id,checklist,recv_by,recv_at,status,note,log)
            VALUES(?,'app',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'FG',?,?,?,?,?,?,?,?,?,?)""",
            (fac, pk["pkey"], pk["lot_no"], pk["pallet_no"], pk["product_code"], pk["product_name"],
             pk.get("packaging", ""), pk["qty"], pk.get("unit") or "กระสอบ", pk["weight_unit"], tot,
             pk.get("mfg", ""), pk.get("exp", ""), net, _today(), now()[11:16],
             " · ".join(x for x in (pk.get("station"), pk.get("operator")) if x),
             GET_TYPES[0], "คลังสินค้า", note, pk["id"], json.dumps(stored, ensure_ascii=False),
             who, now(), "waiting" if decision == "approve" else "rejected", note, now()))
        if decision == "approve":
            packing_mark(c, fac, pk["id"], "moved_to_fg", who)
            _log(c, fac, "receive", rid, "approved_on_phone", note, who)
        else:
            why = "; ".join(f'{x["en"]}: {x["note"]}' for x in stored if x["v"] == "ng")
            packing_mark(c, fac, pk["id"], "rejected_by_fg", who, (why + ("; " + note if note else ""))[:500])
            c.execute("UPDATE fg_packing SET fg_reject_at=?, fixed_at='' WHERE id=?", (now(), pk["id"]))
            _log(c, fac, "receive", rid, "rejected_on_phone", why, who)
        c.commit()
    return {"ok": True, "id": rid, "status": "waiting" if decision == "approve" else "rejected"}


# ---------------------------------------------------------------- issue (phone)
def _stock_hit(c, fac, rec):
    """Stock line for one pallet/lot + FEFO hint (an older lot of the same product)."""
    bal = lot_balances(c, fac, product=rec["product_id"])
    lots = sorted(((b["l"], (b["rq"] or 0) - (b["sq"] or 0)) for b in bal if (b["rq"] or 0) - (b["sq"] or 0) > 0),
                  key=lambda x: mfg_from_lot(x[0]) or x[0])
    this_mfg = mfg_from_lot(rec["lot_no"]) or rec["lot_no"]
    older = [l for l, q in lots if (mfg_from_lot(l) or l) < this_mfg]
    lot_left = next((q for l, q in lots if l == rec["lot_no"]), 0)
    return lot_left, older[:3]


def _flow_id():
    import random
    return int(time.time() * 1000) * 10 + random.randint(0, 9)


@router.post("/scan/issue")
async def scan_issue(req: Request):
    u, fac, _ = ctx(req, "issue")
    b = await req.json()
    text = (b.get("text") or "").strip()
    slip = parse_qr(text)
    who = _who(u)
    with closing(conn()) as c:
        rec = None
        if not slip and EAN_RE.match(text):
            pk = c.execute("SELECT product_code,lot_no,pallet_no,pkey,qty FROM fg_packing WHERE factory_id=? AND legacy_code=?",
                           (fac, text[:12])).fetchone()
            if pk:
                slip = {"pkey": pk["pkey"], "product_code": pk["product_code"], "lot_no": pk["lot_no"],
                        "pallet_no": pk["pallet_no"], "qty": _num(pk["qty"])}
        if slip:
            r = c.execute("""SELECT * FROM fg_receive WHERE factory_id=? AND pkey=? AND status='confirmed'
                             ORDER BY id DESC""", (fac, slip["pkey"])).fetchone()
            if r:
                rec = dict(r)
                left = pallet_left(c, rec["id"], rec["qty"])
                if left <= 0:
                    raise HTTPException(409, {"msg": "พาเลทนี้จ่ายออกหมดแล้ว (pallet already fully issued)"})
                pallet = {"receive_id": rec["id"], "pallet_left": left, "src": "app"}
            else:
                w = c.execute("SELECT status FROM fg_receive WHERE factory_id=? AND pkey=? ORDER BY id DESC",
                              (fac, slip["pkey"])).fetchone()
                if w and w["status"] in ("waiting", "rejected"):
                    raise HTTPException(404, {"msg": {"waiting": "พาเลทนี้ยังรอ Checker ยืนยันรับเข้า (still waiting for the checker)",
                                                      "rejected": "พาเลทนี้ถูกปฏิเสธตอนรับเข้า (rejected at receive)"}[w["status"]]})
                # b483: received in the old Access system (imported history = approved) -> load from the lot balance
                a2 = c.execute("""SELECT product_id,product_name,lot_no,unit,packaging,MAX(weight_unit) weight_unit,MAX(mfg) mfg,MAX(exp) exp,
                                         MAX(location) location,MAX(receive_date) receive_date FROM fg_receive
                                  WHERE factory_id=? AND lot_no=? AND status='confirmed' AND (product_id=? OR ?='')
                                  GROUP BY product_id,product_name,lot_no,unit,packaging""",
                               (fac, slip.get("lot_no") or "", slip.get("product_code") or "", slip.get("product_code") or "")).fetchone()
                if not a2:
                    raise HTTPException(404, {"msg": "ไม่พบพาเลทนี้ในสต็อก (pallet not in stock)"})
                rec = dict(a2)
                rec["pallet_no"] = slip.get("pallet_no", "")
                pallet = {"receive_id": 0, "pallet_left": None, "slip_qty": _num(slip.get("qty")), "src": "access"}
        else:
            # a typed lot number (Access history has no pallet QR)
            if not text:
                raise HTTPException(400, {"msg": "สแกน QR หรือพิมพ์เลขล็อต (scan a QR or type a lot)"})
            r = c.execute("""SELECT product_id,product_name,lot_no,unit,packaging,MAX(weight_unit) weight_unit,MAX(mfg) mfg,MAX(exp) exp,
                                    MAX(location) location,MAX(receive_date) receive_date
                             FROM fg_receive WHERE factory_id=? AND lot_no=? AND status='confirmed'
                             GROUP BY product_id,product_name,lot_no,unit,packaging""", (fac, text)).fetchall()
            if not r:
                raise HTTPException(404, {"msg": "ไม่พบล็อตนี้ในสต็อก (lot not in stock)"})
            rec = dict(r[0])
            pallet = {"receive_id": 0, "pallet_left": None, "src": "lot"}
        lot_left, older = _stock_hit(c, fac, rec)
        if lot_left <= 0:
            raise HTTPException(409, {"msg": "ล็อตนี้ไม่มีคงเหลือ (no balance left for this lot)"})
        if pallet.get("src") == "app" and rec.get("src") == "access" and pallet["pallet_left"] is not None:
            # Access issues were booked per lot, not per pallet -> never offer more than the lot still has
            pallet["pallet_left"] = min(pallet["pallet_left"], lot_left)
            pallet["src"] = "access"
        p = _product(c, fac, rec["product_id"]) or {}
        custs = [dict(x) for x in c.execute("SELECT code,name FROM fg_customers WHERE factory_id=? AND active=1 ORDER BY code", (fac,))]
        if pallet.get("slip_qty"):
            pallet["slip_qty"] = min(pallet["slip_qty"], lot_left)
        avail = pallet["pallet_left"] if pallet["pallet_left"] else (pallet.get("slip_qty") or lot_left)
        flow = _flow_id()
        _log(c, fac, "issue_flow", flow, "scanned",
             f'{rec["product_id"]} · lot {rec["lot_no"]}' + (f' · P{rec.get("pallet_no")}' if rec.get("pallet_no") else "")
             + f' · {_n(avail)} on pallet · lot balance {_n(lot_left)}' + (" · FEFO: older lots " + ", ".join(older) if older else ""), who)
        c.commit()
    wt = _num(rec.get("weight_unit")) or _num(p.get("weight"))
    return {"flow": flow,
            "stock": {"product_id": rec["product_id"], "product_name": rec["product_name"], "lot_no": rec["lot_no"],
                      "unit": rec.get("unit") or "กระสอบ", "packaging": rec.get("packaging") or p.get("packaging", ""),
                      "weight_unit": wt, "pallet_no": rec.get("pallet_no", ""), "pkey": rec.get("pkey", ""),
                      "mfg": rec.get("mfg", ""), "exp": rec.get("exp", ""), "location": rec.get("location", ""),
                      "receive_date": rec.get("receive_date", ""), "lot_left": lot_left, "avail": avail, **pallet},
            "qty_default": avail, "fefo_older": older, "customers": custs,
            "release_types": RELEASE_TYPES}


ISSUE_STEPS = {"correct": "pallet_correct", "not_correct": "pallet_not_correct", "full": "chose_full_pallet",
               "part": "chose_part_pallet", "photo": "photo_taken", "back": "went_back", "cancel": "cancelled"}


@router.post("/issue-step")
async def issue_step(req: Request):
    """b483 audit trail: every tap of the loader between scan and save (kind issue_flow, ref = flow id)."""
    u, fac, _ = ctx(req, "issue")
    b = await req.json()
    step = ISSUE_STEPS.get(b.get("step"))
    flow = int(b.get("flow") or 0)
    if not step or not flow:
        raise HTTPException(400, {"msg": "bad step"})
    with closing(conn()) as c:
        _log(c, fac, "issue_flow", flow, step, (b.get("note") or "").strip()[:300], _who(u))
        c.commit()
    return {"ok": True}


@router.post("/issue")
async def issue(req: Request):
    u, fac, _ = ctx(req, "issue")
    b = await req.json()
    if b.get("decision") not in ("approve", "cancel"):
        raise HTTPException(400, {"msg": "bad decision"})
    if b["decision"] == "cancel":
        return {"ok": True, "status": "cancelled"}
    s = b.get("stock") or {}
    who = _who(u)
    mode = b.get("mode")
    if mode not in ("full", "part"):
        raise HTTPException(400, {"msg": "เลือก ทั้งพาเลท หรือ บางส่วน (choose full pallet or part)"})
    avail = _num(s.get("pallet_left")) if s.get("pallet_left") is not None else (_num(s.get("slip_qty")) or _num(s.get("lot_left")))
    qty = avail if mode == "full" else _num(b.get("qty"))
    if mode == "part" and qty >= avail:
        raise HTTPException(400, {"msg": "บางส่วนต้องน้อยกว่าที่มีบนพาเลท — ถ้าใช้หมดให้เลือกทั้งพาเลท (part must be less than the pallet — choose full)"})
    if mode == "part" and not (b.get("photo") or "").startswith("data:image/"):
        raise HTTPException(400, {"msg": "ต้องถ่ายรูปหรืออัปโหลดรูปเมื่อจ่ายบางส่วน (a photo is required for a part pallet)"})
    if qty <= 0:
        raise HTTPException(400, {"msg": "จำนวนต้องมากกว่า 0 (qty must be more than 0)"})
    if not b.get("customer_id") or b.get("release_type") not in RELEASE_TYPES:
        raise HTTPException(400, {"msg": "เลือกลูกค้าและประเภทจ่าย (choose customer and release type)"})
    with closing(conn()) as c:
        rid = int(s.get("receive_id") or 0)
        if rid:
            rec = c.execute("SELECT * FROM fg_receive WHERE id=? AND factory_id=? AND status='confirmed'", (rid, fac)).fetchone()
            if not rec:
                raise HTTPException(404, {"msg": "pallet not in stock"})
            rec = dict(rec)
            if qty > pallet_left(c, rid, rec["qty"]) + 1e-9:
                raise HTTPException(400, {"msg": "เกินจำนวนคงเหลือในพาเลท (more than left on the pallet)"})
        else:
            rec = dict(product_id=s.get("product_id"), product_name=s.get("product_name"), lot_no=s.get("lot_no"),
                       unit=s.get("unit"), packaging=s.get("packaging"), weight_unit=s.get("weight_unit"), pkey="")
        bal = lot_balances(c, fac, product=rec["product_id"], lot=rec["lot_no"])
        left = sum((x["rq"] or 0) - (x["sq"] or 0) for x in bal)
        if qty > left + 1e-9:
            raise HTTPException(400, {"msg": f"เกินจำนวนคงเหลือของล็อต (more than the lot balance: {_n(left)})"})
        all_ok, _, _, stored = _check(issue_items(rec, b), b.get("checks"))
        if not all_ok:
            raise HTTPException(400, {"msg": "ต้อง OK ทุกข้อ (every item must be OK)"})
        p = _product(c, fac, rec["product_id"]) or {}
        wt = _num(rec.get("weight_unit")) or _num(p.get("weight"))
        t = now()
        b["pallet_bags"] = avail
        iid = c.insert_id("""INSERT INTO fg_release(factory_id,src,release_no,lot_no,pkey,receive_id,product_id,
            product_name,packaging,qty,unit,total_kg,release_date,release_time,release_man,customer_id,release_type,
            memo,plate,checklist,issue_by,issue_at,status,note,log)
            VALUES(?,'app',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'waiting',?,?)""",
            (fac, (b.get("release_no") or "").strip()[:60], rec["lot_no"], rec.get("pkey", ""), rid,
             rec["product_id"], rec["product_name"], rec.get("packaging") or p.get("packaging", ""), qty,
             rec.get("unit") or "กระสอบ", qty * wt, _today(), t[11:16], who, b["customer_id"], b["release_type"],
             (b.get("memo") or "").strip()[:200], (b.get("plate") or "").strip()[:30],
             json.dumps(stored, ensure_ascii=False), who, t, (b.get("memo") or "").strip()[:200], t))
        photo = ""
        if mode == "part":
            photo = _save_photo(b["photo"], fac, iid)
        c.execute("UPDATE fg_release SET issue_mode=?, pallet_bags=?, photo=?, photo_at=?, photo_saved_at=? WHERE id=?",
                  (mode, avail, photo, (b.get("photo_at") or "")[:19] if photo else "", t if photo else "", iid))
        flow = int(b.get("flow") or 0)
        if flow:   # the steps before saving (scan, correct?, full/part, photo) become this release's audit trail
            c.execute("""INSERT INTO fg_log(factory_id,kind,ref_id,action,note,user_name,created_at)
                         SELECT factory_id,'release',?,action,note,user_name,created_at FROM fg_log
                         WHERE factory_id=? AND kind='issue_flow' AND ref_id=? ORDER BY id""", (iid, fac, flow))
        _log(c, fac, "release", iid, "issued_on_phone",
             f"{mode} {_n(qty)} of {_n(avail)} · {b.get('customer_id')} · {b.get('release_type')}"
             + (f" · plate {b.get('plate')}" if b.get("plate") else ""), who)
        c.commit()
    return {"ok": True, "id": iid, "status": "waiting", "qty": qty, "mode": mode}


def _save_photo(data_url, fac, iid):
    """Part-pallet photo (already stamped with date/time on the phone) -> uploads/fg/."""
    import base64
    from .config import UPLOADS
    head, _, b64 = data_url.partition(",")
    ext = "png" if "png" in head else "jpg"
    raw = base64.b64decode(b64 or "")
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(400, {"msg": "รูปใหญ่เกินไป (photo too large)"})
    folder = os.path.join(UPLOADS, "fg", datetime.date.today().strftime("%Y-%m"))
    os.makedirs(folder, exist_ok=True)
    name = f"issue-{fac}-{iid}-{datetime.datetime.now().strftime('%H%M%S')}.{ext}"
    with open(os.path.join(folder, name), "wb") as f:
        f.write(raw)
    return "/uploads/fg/" + datetime.date.today().strftime("%Y-%m") + "/" + name


# ---------------------------------------------------------------- my jobs (receiver / issuer)
def _fix_state(r):
    """What happened after FG rejected this pallet: packing fixed it? reprinted? received again?"""
    if r["status"] != "rejected":
        return ""
    ps, fixed, printed = r.get("p_status") or "", r.get("fixed_at") or "", r.get("printed_at") or ""
    if r.get("again"):
        return "received_again"
    if ps == "cancelled":
        return "cancelled"
    if fixed and fixed >= (r["recv_at"] or ""):
        if printed and printed >= fixed:
            return "reprinted_" + (r.get("pc_status") or "pending")
        return "fixed_not_printed"
    return "waiting_packing"


@router.get("/my")
async def my_jobs(req: Request, kind: str = "recv", d1: str = "", d2: str = "", status: str = ""):
    u, fac, fr = ctx(req, "receive" if kind == "recv" else "issue")
    who = _who(u)
    d1 = d1 or (datetime.date.today() - datetime.timedelta(days=6)).isoformat()
    d2 = (d2 or _today()) + " 23:59:59"
    with closing(conn()) as c:
        if kind == "recv":
            rows = [dict(r) for r in c.execute(
                """SELECT r.id,r.lot_no,r.pallet_no,r.product_id,r.product_name,r.qty,r.unit,r.weight_unit,r.total_kg,r.status,r.recv_at,r.note,
                          r.mfg,r.exp,r.receive_man,r.packaging,p.doc_no,p.pack_machine,p.pd_line,p.staff_name,p.pc_by,p.pc_at,p.packed_at,
                          r.checker,r.checked_at,r.packing_id,r.checklist,p.status p_status,p.fixed_at,p.printed_at,p.print_count,
                          p.pc_status,p.note p_note,
                          (SELECT MAX(x.recv_at) FROM fg_receive x WHERE x.packing_id=r.packing_id AND x.id>r.id
                             AND x.status IN ('waiting','confirmed')) again
                   FROM fg_receive r LEFT JOIN fg_packing p ON p.id=r.packing_id
                   WHERE r.factory_id=? AND r.src='app' AND r.recv_by=? AND r.recv_at>=? AND r.recv_at<=?
                   ORDER BY r.recv_at DESC, r.id DESC LIMIT 1000""", (fac, who, d1, d2))]
            for r in rows:
                r["fix"] = _fix_state(r)
                try:
                    r["checklist"] = json.loads(r["checklist"] or "[]")
                except Exception:
                    r["checklist"] = []
        else:
            rows = [dict(r) for r in c.execute(
                """SELECT id,lot_no,product_id,product_name,qty,total_kg,status,issue_at,customer_id,release_type,plate,
                          release_no,issue_mode,pallet_bags,photo,photo_at,checker,checked_at,note,pkey
                   FROM fg_release WHERE factory_id=? AND src='app' AND issue_by=? AND issue_at>=? AND issue_at<=?
                   ORDER BY issue_at DESC, id DESC LIMIT 1000""", (fac, who, d1, d2))]
        if status:
            rows = [r for r in rows if r["status"] == status]
    return {"rows": rows}


# ---------------------------------------------------------------- checker (PC)
def _recv_row(r, prods):
    d = dict(r)
    p = prods.get(d["product_id"]) or {}
    d["group"] = group_of(p.get("owner"))
    d["packaging"] = d.get("packaging") or p.get("packaging", "")
    if not d.get("total_kg"):
        d["total_kg"] = _num(d["qty"]) * _num(p.get("weight"))
    try:
        d["checklist"] = json.loads(d.get("checklist") or "[]")
    except Exception:
        d["checklist"] = []
    return d


def _prods(c, fac):
    return {r["code"]: dict(r) for r in c.execute("SELECT code,owner,packaging,weight,type_id FROM fg_products WHERE factory_id=?", (fac,))}


@router.get("/queue")
async def queue(req: Request, kind: str = "recv"):
    u, fac, _ = ctx(req, "check")
    with closing(conn()) as c:
        prods = _prods(c, fac)
        if kind == "issue":
            rows = c.execute("SELECT * FROM fg_release WHERE factory_id=? AND status='waiting' ORDER BY id", (fac,)).fetchall()
            cust = {r["code"]: r["name"] for r in c.execute("SELECT code,name FROM fg_customers WHERE factory_id=?", (fac,))}
            out = []
            for r in rows:
                d = _recv_row(r, prods)
                d["customer_name"] = cust.get(d["customer_id"], "")
                d["pallet_no"] = (d.get("pkey") or "||").split("|")[2] if (d.get("pkey") or "").count("|") >= 2 else ""
                out.append(d)
        else:
            out = [_recv_row(r, prods) for r in c.execute(
                "SELECT * FROM fg_receive WHERE factory_id=? AND status='waiting' ORDER BY id", (fac,))]
    return {"rows": out}


@router.post("/confirm")
async def confirm(req: Request):
    u, fac, _ = ctx(req, "check")
    b = await req.json()
    kind, ids = b.get("kind"), [int(x) for x in (b.get("ids") or [])]
    checker = (b.get("checker") or "").strip() or _who(u)
    action = b.get("action") or "confirm"
    if kind not in ("recv", "issue") or not ids or action not in ("confirm", "sendback"):
        raise HTTPException(400, {"msg": "nothing selected"})
    note = (b.get("note") or "").strip()[:300]
    if action == "sendback" and not note:
        raise HTTPException(400, {"msg": "ใส่เหตุผลที่ส่งกลับ (give a reason for sending back)"})
    tbl = "fg_receive" if kind == "recv" else "fg_release"
    done = 0
    with closing(conn()) as c:
        for i in ids:
            r = c.execute(f"SELECT * FROM {tbl} WHERE id=? AND factory_id=? AND status='waiting'", (i, fac)).fetchone()
            if not r:
                continue
            st = "confirmed" if action == "confirm" else "sentback"
            c.execute(f"UPDATE {tbl} SET status=?,checker=?,checked_by=?,checked_at=?,note=CASE WHEN ?<>'' THEN ? ELSE note END WHERE id=?",
                      (st, checker, _who(u), now(), note, note, i))
            if kind == "recv" and st == "sentback" and r["packing_id"]:
                packing_mark(c, fac, r["packing_id"], "packed", _who(u), "sent back by checker: " + note)
            _log(c, fac, "receive" if kind == "recv" else "release", i, st, note, _who(u))
            done += 1
        c.commit()
    return {"ok": True, "done": done}


# ---------------------------------------------------------------- history / stock / reports
@router.get("/history")
async def history(req: Request, kind: str = "recv", d1: str = "", d2: str = "", q: str = "",
                  status: str = "", limit: int = 500):
    u, fac, _ = ctx(req, "check")
    tbl, dcol = ("fg_receive", "receive_date") if kind == "recv" else ("fg_release", "release_date")
    conds, args = ["factory_id=?"], [fac]
    if d1:
        conds.append(f"{dcol}>=?")
        args.append(d1)
    if d2:
        conds.append(f"{dcol}<=?")
        args.append(d2 + " 23:59:59")
    if status:
        conds.append("status=?")
        args.append(status)
    if q:
        conds.append("(lot_no LIKE ? OR product_id LIKE ? OR product_name LIKE ?)")
        args += ["%" + q + "%"] * 3
    limit = max(1, min(int(limit or 500), 5000))
    with closing(conn()) as c:
        prods = _prods(c, fac)
        rows = [_recv_row(r, prods) for r in c.execute(
            f"SELECT * FROM {tbl} WHERE {' AND '.join(conds)} ORDER BY {dcol} DESC, id DESC LIMIT {limit}", tuple(args))]
        for r in rows:
            r.pop("checklist", None)
            r.pop("log", None)
        n = c.execute(f"SELECT COUNT(*) FROM {tbl} WHERE {' AND '.join(conds)}", tuple(args)).fetchone()[0]
    return {"rows": rows, "total": n}


@router.get("/stock")
async def stock(req: Request, q: str = "", all: int = 0):
    u, fac, _ = ctx(req, "check")
    with closing(conn()) as c:
        prods = {r["code"]: dict(r) for r in c.execute("SELECT * FROM fg_products WHERE factory_id=?", (fac,))}
        names = {r["product_id"]: r["product_name"] for r in c.execute(
            "SELECT product_id, MAX(product_name) product_name FROM fg_receive WHERE factory_id=? GROUP BY product_id", (fac,))}
        types = {r["code"]: r["name"] for r in c.execute("SELECT code,name FROM fg_product_types WHERE factory_id=?", (fac,))}
        rows = []
        for b in lot_balances(c, fac):
            bal = (b["rq"] or 0) - (b["sq"] or 0)
            if not all and bal <= 0:
                continue
            p = prods.get(b["p"]) or {}
            name = p.get("name") or names.get(b["p"], "")
            if q and q.lower() not in (b["p"] + " " + (b["l"] or "") + " " + name).lower():
                continue
            mfg = mfg_from_lot(b["l"])
            rows.append({"product_id": b["p"], "product_name": name, "lot_no": b["l"], "received": b["rq"] or 0,
                         "issued": b["sq"] or 0, "balance": bal, "kg": bal * _num(p.get("weight")),
                         "type": types.get(p.get("type_id"), p.get("type_id", "")), "group": group_of(p.get("owner")),
                         "mfg": mfg, "age_days": (datetime.date.today() - datetime.date.fromisoformat(mfg)).days if mfg else None})
    rows.sort(key=lambda r: (r["product_id"], r["mfg"] or r["lot_no"] or ""))
    return {"rows": rows}


@router.get("/stockcard")
async def stockcard(req: Request, product: str, lot: str = ""):
    u, fac, _ = ctx(req, "check")
    args = [fac, product]
    lc = ""
    if lot:
        lc = " AND lot_no=?"
        args.append(lot)
    with closing(conn()) as c:
        ev = [dict(r, kind="in") for r in c.execute(
            f"SELECT receive_date d, receive_time t, lot_no, qty, receive_man who, get_type info, id, src FROM fg_receive "
            f"WHERE factory_id=? AND product_id=? AND status='confirmed'{lc}", tuple(args))]
        ev += [dict(r, kind="out") for r in c.execute(
            f"SELECT release_date d, release_time t, lot_no, qty, release_man who, customer_id||' · '||release_type info, id, src, status "
            f"FROM fg_release WHERE factory_id=? AND product_id=? AND status IN ('confirmed','waiting'){lc}", tuple(args))]
        p = _product(c, fac, product) or {}
    ev.sort(key=lambda e: ((e["d"] or "")[:10], 0 if e["kind"] == "in" else 1, e["id"]))
    bal = 0
    for e in ev:
        bal += e["qty"] if e["kind"] == "in" else -e["qty"]
        e["balance"] = bal
    return {"product": p, "rows": ev[-2000:], "total_rows": len(ev)}


@router.get("/daily")
async def daily(req: Request, d1: str = "", d2: str = ""):
    """Daily receive / release totals by product type (bags and kg)."""
    u, fac, _ = ctx(req, "check")
    d2 = d2 or _today()
    d1 = d1 or (datetime.date.fromisoformat(d2) - datetime.timedelta(days=13)).isoformat()
    with closing(conn()) as c:
        prods = _prods(c, fac)
        types = {r["code"]: r["name"] for r in c.execute("SELECT code,name FROM fg_product_types WHERE factory_id=?", (fac,))}
        days = {}
        for kind, tbl, dcol, sts in (("in", "fg_receive", "receive_date", "('confirmed')"),
                                     ("out", "fg_release", "release_date", "('confirmed')")):
            for r in c.execute(f"SELECT substr({dcol},1,10) d, product_id, SUM(qty) q FROM {tbl} WHERE factory_id=? "
                               f"AND status IN {sts} AND {dcol}>=? AND {dcol}<=? GROUP BY substr({dcol},1,10), product_id",
                               (fac, d1, d2 + " 23:59:59")):
                p = prods.get(r["product_id"]) or {}
                t = types.get(p.get("type_id"), p.get("type_id") or "-")
                day = days.setdefault(r["d"], {"d": r["d"], "in_q": 0, "in_kg": 0, "out_q": 0, "out_kg": 0, "types": {}})
                kg = (r["q"] or 0) * _num(p.get("weight"))
                day[kind + "_q"] += r["q"] or 0
                day[kind + "_kg"] += kg
                tt = day["types"].setdefault(t, {"in_kg": 0, "out_kg": 0})
                tt[kind + "_kg"] += kg
    return {"d1": d1, "d2": d2, "days": sorted(days.values(), key=lambda x: x["d"], reverse=True)}


@router.get("/log")
async def log(req: Request, kind: str, ref: int):
    u, fac, _ = ctx(req, "view")
    with closing(conn()) as c:
        return {"rows": [dict(r) for r in c.execute(
            "SELECT action,note,user_name,created_at FROM fg_log WHERE factory_id=? AND kind=? AND ref_id=? ORDER BY id",
            (fac, kind, ref))]}


# ---------------------------------------------------------------- packing (stand-in)
@router.get("/packing")
async def packing_list(req: Request, status: str = "", q: str = ""):
    u, fac, _ = ctx(req, "check")
    conds, args = ["factory_id=?"], [fac]
    if status:
        conds.append("status=?")
        args.append(status)
    if q:
        conds.append("(lot_no LIKE ? OR product_code LIKE ? OR product_name LIKE ?)")
        args += ["%" + q + "%"] * 3
    with closing(conn()) as c:
        rows = [dict(r) for r in c.execute(
            f"SELECT * FROM fg_packing WHERE {' AND '.join(conds)} ORDER BY id DESC LIMIT 500", tuple(args))]
    return {"rows": rows}


@router.post("/packing")
async def packing_add(req: Request):
    """Admin: key a packing record by hand (for testing until the packing DB is linked).
    Returns the slip QR text so a test label can be printed."""
    u, fac, _ = ctx(req, "admin")
    b = await req.json()
    lot, pal, code = (b.get("lot_no") or "").strip(), str(b.get("pallet_no") or "").strip(), (b.get("product_code") or "").strip()
    if not lot or not pal or not code:
        raise HTTPException(400, {"msg": "lot, pallet and product are required"})
    with closing(conn()) as c:
        p = _product(c, fac, code)
        if not p:
            raise HTTPException(400, {"msg": "ไม่พบรหัสสินค้า (unknown product code)"})
        qr = json.dumps({"Product_code": code, "Product_Name": p["name"], "Lot_No": lot, "No": str(int(_num(b.get("qty")))),
                         "weight": _n(p["weight"]) + " kg", "Station": b.get("station") or "", "Pallet": pal,
                         "Operator": b.get("operator") or ""}, ensure_ascii=False)
        slip = parse_qr(qr)
        if c.execute("SELECT 1 FROM fg_packing WHERE factory_id=? AND pkey=?", (fac, slip["pkey"])).fetchone():
            raise HTTPException(409, {"msg": "มีพาเลทนี้แล้ว (this lot + pallet already exists)"})
        pk = packing_lookup(c, fac, slip, _who(u))
        c.execute("UPDATE fg_packing SET src='manual' WHERE id=?", (pk["id"],))
        c.commit()
    return {"ok": True, "id": pk["id"], "qr": qr}



# ================================================================ packing room: A6 QR pallet slips
def bag_kg(text):
    """'20' -> 20 ; '1*20' -> 20 (1 kg x 20 packs in one bag) — same rule as the old app."""
    t = str(text or "").replace("x", "*").replace("X", "*")
    if "*" in t:
        a, _, b = t.partition("*")
        return _num(a) * _num(b)
    return _num(t)


def slip_qr(pk):
    """The text inside the slip QR. Same keys the FG receiver (and the WMS app) already read."""
    return json.dumps({"Id": pk["id"], "Product_code": pk["product_code"], "Product_Name": pk["product_name"],
                       "Lot_No": pk["lot_no"], "Pallet": str(pk["pallet_no"]), "No": _n(pk["qty"]),
                       "weight": _n(pk["weight_unit"]) + " kg", "MFG": pk.get("mfg", ""), "EXP": pk.get("exp", ""),
                       "Station": pk.get("pack_machine") or pk.get("station", ""), "Operator": pk.get("operator", "")},
                      ensure_ascii=False, separators=(",", ":"))


def _slip_out(r):
    d = dict(r)
    d["qr"] = slip_qr(d)
    return d


def _next_slip(c, fac, product, lot):
    r = c.execute("SELECT MAX(slip_no) m FROM fg_packing WHERE factory_id=? AND product_code=? AND lot_no=?",
                  (fac, product, lot)).fetchone()
    m = r["m"] if r and r["m"] else 0
    # pallets that came in by QR only (no slip_no) still hold their pallet numbers
    for x in c.execute("SELECT pallet_no FROM fg_packing WHERE factory_id=? AND product_code=? AND lot_no=?", (fac, product, lot)):
        try:
            m = max(m, int(x["pallet_no"]))
        except (TypeError, ValueError):
            pass
    return m + 1


def _slip_fields(c, fac, b):
    code = (b.get("product_code") or "").strip()
    lot = (b.get("lot_no") or "").strip().upper()
    p = _product(c, fac, code)
    if not p:
        raise HTTPException(400, {"msg": "ไม่พบรหัสสินค้า (unknown product code) — add it in Products first"})
    if not lot:
        raise HTTPException(400, {"msg": "ใส่เลขล็อต (lot number required)"})
    bags = _num(b.get("bags"))
    if bags <= 0:
        raise HTTPException(400, {"msg": "ใส่จำนวนถุง (total bags required)"})
    bw = (str(b.get("bag_weight") or "").strip()) or _n(p.get("weight"))
    wu = bag_kg(bw)
    if wu <= 0:
        raise HTTPException(400, {"msg": "น้ำหนักต่อถุงไม่ถูกต้อง (bad bag weight)"})
    mfg = (b.get("mfg") or "").strip() or mfg_from_lot(lot)
    exp = (b.get("exp") or "").strip() or exp_for(mfg, p.get("shelf_life_days"))
    for d in (mfg, exp):
        if d:
            try:
                datetime.date.fromisoformat(d)
            except ValueError:
                raise HTTPException(400, {"msg": f"วันที่ไม่ถูกต้อง (bad date) {d}"})
    if not exp:
        raise HTTPException(400, {"msg": "ใส่วันหมดอายุ (expiry date required — no shelf life set for this product)"})
    pt = b.get("pack_type") or PACK_TYPES[0]
    return {"product_code": code, "product_name": p["name"], "packaging": p.get("packaging", ""), "lot_no": lot,
            "qty": bags, "unit": "กระสอบ", "weight_unit": wu, "bag_weight": bw, "total_weight": wu * bags,
            "mfg": mfg, "exp": exp, "doc_no": (b.get("doc_no") or "").strip()[:60],
            "pack_machine": (b.get("pack_machine") or "").strip()[:60], "pd_line": (b.get("pd_line") or "").strip()[:60],
            "pack_type": pt if pt in PACK_TYPES else PACK_TYPES[0], "staff_name": (b.get("staff_name") or "").strip()[:60]}


@router.get("/lists")
async def lists(req: Request):
    u, fac, _ = ctx(req, "view")
    with closing(conn()) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM fg_lists WHERE factory_id IN (0,?) ORDER BY kind, sort, name", (fac,))]
    return {"rows": rows, "pack_types": PACK_TYPES}


@router.post("/lists")
async def list_save(req: Request):
    u, fac, _ = ctx(req, "admin")
    b = await req.json()
    kind, name = b.get("kind"), (b.get("name") or "").strip()
    if kind not in DEFAULT_LISTS or not name:
        raise HTTPException(400, {"msg": "kind and name required"})
    with closing(conn()) as c:
        r = c.execute("SELECT id FROM fg_lists WHERE factory_id IN (0,?) AND kind=? AND name=?", (fac, kind, name)).fetchone()
        if r:
            c.execute("UPDATE fg_lists SET active=? WHERE id=?", (1 if b.get("active", True) else 0, r["id"]))
        else:
            n = c.execute("SELECT COALESCE(MAX(sort),0)+1 FROM fg_lists WHERE kind=?", (kind,)).fetchone()[0]
            c.execute("INSERT INTO fg_lists(factory_id,kind,name,sort,active) VALUES(?,?,?,?,1)", (fac, kind, name, n))
        c.commit()
    return {"ok": True}


@router.get("/slips-next")
async def slip_next(req: Request, product: str = "", lot: str = ""):
    """Next pallet / slip number for this product + lot (shown on the form before saving)."""
    u, fac, _ = ctx(req, "pack")
    with closing(conn()) as c:
        return {"next": _next_slip(c, fac, product.strip(), lot.strip().upper()) if product and lot else 1}


@router.post("/slips")
async def slip_new(req: Request):
    """Packing: save one or more pallet slips (copies = pallets of the same lot) — returns them
    with their QR text for printing. Slip numbers run per product + lot: 1, 2, 3 …"""
    u, fac, _ = ctx(req, "pack")
    b = await req.json()
    copies = max(1, min(int(_num(b.get("copies") or 1)), 50))
    who = _who(u)
    out = []
    with closing(conn()) as c:
        f = _slip_fields(c, fac, b)
        f["staff_name"] = f["staff_name"] or who
        for _ in range(copies):
            sn = _next_slip(c, fac, f["product_code"], f["lot_no"])
            t = now()
            pid = c.insert_id("""INSERT INTO fg_packing(factory_id,pkey,lot_no,pallet_no,slip_no,product_code,product_name,
                packaging,qty,unit,weight_unit,bag_weight,total_weight,mfg,exp,doc_no,pack_machine,pd_line,pack_type,
                station,operator,staff_name,raw_qr,packed_at,src,status,status_at,pc_status,created_by)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'',?,'slip','packed',?,'pending',?)""",
                (fac, pkey_of(f["product_code"], f["lot_no"], sn), f["lot_no"], str(sn), sn, f["product_code"],
                 f["product_name"], f["packaging"], f["qty"], f["unit"], f["weight_unit"], f["bag_weight"],
                 f["total_weight"], f["mfg"], f["exp"], f["doc_no"], f["pack_machine"], f["pd_line"], f["pack_type"],
                 f["pack_machine"], f["staff_name"], f["staff_name"], t, t, who))
            r = c.execute("SELECT * FROM fg_packing WHERE id=?", (pid,)).fetchone()
            c.execute("UPDATE fg_packing SET raw_qr=? WHERE id=?", (slip_qr(dict(r)), pid))
            _log(c, fac, "packing", pid, "slip_created", f'{f["lot_no"]} #{sn}', who)
            out.append(_slip_out(c.execute("SELECT * FROM fg_packing WHERE id=?", (pid,)).fetchone()))
        c.commit()
    return {"ok": True, "slips": out}


# ---------------------------------------------------------------- Access history -> QR slips (b488)
_NW_RE = re.compile(r"MFG\s*(\d{1,2})\D(\d{1,2})\D(\d{4}).*?EXP\s*(\d{1,2})\D(\d{1,2})\D(\d{4})", re.S | re.I)


def _nw_dates(nw):
    """Access 'Net weight' text: 'MFG 14 10 2026\r\nEXP 14 10 2028 B' -> ('2026-10-14', '2028-10-14')."""
    m = _NW_RE.search(nw or "")
    if not m:
        return "", ""
    try:
        a = datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        b = datetime.date(int(m.group(6)), int(m.group(5)), int(m.group(4))).isoformat()
        return a, b
    except ValueError:
        return "", ""


def access_to_slips(c, fac, who):
    """One QR pallet slip per pallet received in the old Access system. The slips are packing-approved
    and 'moved to FG' (the Access history counts as approved); the receive row is linked to its slip
    (pkey / pallet_no / packing_id), so scanning it shows the received record and the loader can load
    that pallet. Pallets are numbered 1, 2, 3 … per product + lot in receive order, after any slip
    numbers that lot already has. Stock does not change. Safe to run again: only unlinked rows."""
    rows = c.execute("""SELECT * FROM fg_receive WHERE factory_id=? AND src='access' AND COALESCE(packing_id,0)=0
                        ORDER BY product_id, lot_no, receive_date, receive_time, id""", (fac,)).fetchall()
    prods = {r["code"]: dict(r) for r in c.execute("SELECT code,name,packaging,weight,shelf_life_days FROM fg_products WHERE factory_id=?", (fac,))}
    nxt, made, t = {}, 0, now()
    for r in rows:
        r = dict(r)
        key = (r["product_id"], r["lot_no"])
        if key not in nxt:
            m = c.execute("SELECT MAX(CAST(pallet_no AS INTEGER)) m, MAX(slip_no) s FROM fg_packing WHERE factory_id=? AND product_code=? AND lot_no=?",
                          (fac, key[0], key[1])).fetchone()
            nxt[key] = max(int(m["m"] or 0), int(m["s"] or 0))
        nxt[key] += 1
        pal = nxt[key]
        pkey = pkey_of(key[0], key[1], str(pal))
        while c.execute("SELECT 1 FROM fg_packing WHERE factory_id=? AND pkey=?", (fac, pkey)).fetchone():
            nxt[key] += 1
            pal = nxt[key]
            pkey = pkey_of(key[0], key[1], str(pal))
        p = prods.get(key[0]) or {}
        mfg, exp = (r.get("mfg") or "", r.get("exp") or "")
        if not mfg or not exp:
            a, b = _nw_dates(r.get("net_weight"))
            mfg, exp = mfg or a or mfg_from_lot(key[1]), exp or b or exp_for(mfg or a or mfg_from_lot(key[1]), p.get("shelf_life_days"))
        wu = _num(r.get("weight_unit")) or _num(p.get("weight"))
        at = (r.get("receive_date") or "")[:10] + " " + ((r.get("receive_time") or "")[:8] or "00:00")
        pid = c.insert_id("""INSERT INTO fg_packing(factory_id,pkey,lot_no,pallet_no,slip_no,product_code,product_name,packaging,qty,unit,
            weight_unit,bag_weight,total_weight,mfg,exp,station,operator,staff_name,raw_qr,packed_at,src,status,status_at,status_by,
            pc_status,pc_by,pc_at,pack_type,note,created_by)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'',?,?,'',?,'access','moved_to_fg',?,?,'approved','Access',?,'Finished Goods',?,?)""",
            (fac, pkey, key[1], str(pal), pal, key[0], r.get("product_name") or p.get("name", ""), r.get("packaging") or p.get("packaging", ""),
             _num(r["qty"]), r.get("unit") or "กระสอบ", wu, _n(wu), _num(r.get("total_kg")) or _num(r["qty"]) * wu, mfg, exp,
             r.get("receive_man") or "", r.get("receive_man") or "", at, t, who, at,
             "Access receive #" + str(r.get("access_id") or r["id"]), who))
        c.execute("UPDATE fg_receive SET pkey=?, pallet_no=?, packing_id=?, mfg=CASE WHEN mfg='' THEN ? ELSE mfg END, exp=CASE WHEN exp='' THEN ? ELSE exp END WHERE id=?",
                  (pkey, str(pal), pid, mfg, exp, r["id"]))
        _log(c, fac, "packing", pid, "slip_from_access", f'{key[1]} #{pal} · Access receive {r.get("receive_date", "")[:10]}', who)
        made += 1
    return made


@router.get("/access-slips")
async def access_slips_info(req: Request):
    u, fac, _ = ctx(req, "admin")
    with closing(conn()) as c:
        a = c.execute("SELECT COUNT(*) FROM fg_receive WHERE factory_id=? AND src='access' AND COALESCE(packing_id,0)=0", (fac,)).fetchone()[0]
        b = c.execute("SELECT COUNT(*) FROM fg_packing WHERE factory_id=? AND src='access'", (fac,)).fetchone()[0]
    return {"missing": a, "made": b}


@router.post("/access-slips")
async def access_slips_make(req: Request):
    import asyncio
    u, fac, _ = ctx(req, "admin")

    def run():
        with closing(conn()) as c:
            n = access_to_slips(c, fac, _who(u))
            c.commit()
            return n
    return {"ok": True, "made": await asyncio.to_thread(run)}


@router.get("/slips")
async def slip_list(req: Request, d1: str = "", d2: str = "", q: str = "", pc: str = "", status: str = "",
                    ids: str = "", limit: int = 500):
    u, fac, fr = ctx(req, "view")
    if not ({"pack", "check"} & can_set(roles_of(u))):
        raise HTTPException(403, {"msg": "ไม่มีสิทธิ์ (no permission)"})
    conds, args = ["factory_id=?"], [fac]
    if ids:
        idl = [int(x) for x in ids.split(",") if x.strip().isdigit()][:200]
        conds.append("id IN (" + ",".join("?" * len(idl)) + ")")
        args += idl
    else:
        if d1 and not status:
            conds.append("packed_at>=?")
            args.append(d1)
        if d2 and not status:
            conds.append("packed_at<=?")
            args.append(d2 + " 23:59:59")
        if pc:
            conds.append("pc_status=?")
            args.append(pc)
        if status:
            conds.append("status=?")
            args.append(status)
        if q:
            conds.append("(lot_no LIKE ? OR product_code LIKE ? OR product_name LIKE ? OR doc_no LIKE ?)")
            args += ["%" + q + "%"] * 4
    limit = max(1, min(int(limit or 500), 3000))
    with closing(conn()) as c:
        rows = [_slip_out(r) for r in c.execute(
            f"SELECT * FROM fg_packing WHERE {' AND '.join(conds)} ORDER BY packed_at DESC, id DESC LIMIT {limit}", tuple(args))]
        cnt = {r["pc_status"]: r["n"] for r in c.execute(
            "SELECT pc_status, COUNT(*) n FROM fg_packing WHERE factory_id=? AND status='packed' GROUP BY pc_status", (fac,))}
        cnt["fg_rejected"] = c.execute("SELECT COUNT(*) FROM fg_packing WHERE factory_id=? AND status='rejected_by_fg'", (fac,)).fetchone()[0]
    return {"rows": rows, "counts": cnt}


@router.post("/slips/{sid}")
async def slip_edit(req: Request, sid: int):
    """Fix a slip before FG has received it. It goes back to the packing checker."""
    u, fac, _ = ctx(req, "pack")
    b = await req.json()
    who = _who(u)
    with closing(conn()) as c:
        r = c.execute("SELECT * FROM fg_packing WHERE id=? AND factory_id=?", (sid, fac)).fetchone()
        if not r:
            raise HTTPException(404, {"msg": "not found"})
        r = dict(r)
        if r["status"] not in ("packed", "rejected_by_fg"):
            raise HTTPException(409, {"msg": "แก้ไขไม่ได้ — FG รับไปแล้วหรือยกเลิกแล้ว (FG already has it, or it is cancelled)"})
        was_fg_reject = r["status"] == "rejected_by_fg"
        f = _slip_fields(c, fac, dict(b, product_code=b.get("product_code") or r["product_code"], lot_no=b.get("lot_no") or r["lot_no"]))
        sn = r["slip_no"] or r["pallet_no"]
        if (f["product_code"], f["lot_no"]) != (r["product_code"], r["lot_no"]):
            sn = _next_slip(c, fac, f["product_code"], f["lot_no"])
        c.execute("""UPDATE fg_packing SET pkey=?,lot_no=?,pallet_no=?,slip_no=?,product_code=?,product_name=?,packaging=?,
            qty=?,weight_unit=?,bag_weight=?,total_weight=?,mfg=?,exp=?,doc_no=?,pack_machine=?,pd_line=?,pack_type=?,
            station=?,operator=?,staff_name=?,pc_status='pending',pc_by='',pc_at='',edited_by=?,edited_at=? WHERE id=?""",
            (pkey_of(f["product_code"], f["lot_no"], sn), f["lot_no"], str(sn), int(_num(sn)), f["product_code"],
             f["product_name"], f["packaging"], f["qty"], f["weight_unit"], f["bag_weight"], f["total_weight"], f["mfg"],
             f["exp"], f["doc_no"], f["pack_machine"], f["pd_line"], f["pack_type"], f["pack_machine"],
             f["staff_name"] or r["staff_name"], f["staff_name"] or r["staff_name"], who, now(), sid))
        nr = dict(c.execute("SELECT * FROM fg_packing WHERE id=?", (sid,)).fetchone())
        c.execute("UPDATE fg_packing SET raw_qr=? WHERE id=?", (slip_qr(nr), sid))
        if was_fg_reject:
            c.execute("UPDATE fg_packing SET status='packed', status_at=?, status_by=?, fixed_at=? WHERE id=?", (now(), who, now(), sid))
        _log(c, fac, "packing", sid, "slip_fixed_after_fg_reject" if was_fg_reject else "slip_edited", "", who)
        c.commit()
        return {"ok": True, "slip": _slip_out(c.execute("SELECT * FROM fg_packing WHERE id=?", (sid,)).fetchone())}


@router.post("/slips-printed")
async def slips_printed(req: Request):
    """The print window opened for these slips — counts prints so FG can see a fixed slip was reprinted."""
    u, fac, _ = ctx(req, "pack")
    b = await req.json()
    ids = [int(x) for x in (b.get("ids") or [])][:200]
    with closing(conn()) as c:
        for i in ids:
            c.execute("UPDATE fg_packing SET print_count=COALESCE(print_count,0)+1, printed_at=? WHERE id=? AND factory_id=?",
                      (now(), i, fac))
            _log(c, fac, "packing", i, "printed", "", _who(u))
        c.commit()
    return {"ok": True}


@router.post("/slips/{sid}/cancel")
async def slip_cancel(req: Request, sid: int):
    u, fac, _ = ctx(req, "pack")
    b = await req.json()
    why = (b.get("note") or "").strip()
    if not why:
        raise HTTPException(400, {"msg": "ใส่เหตุผล (give a reason)"})
    with closing(conn()) as c:
        r = c.execute("SELECT status FROM fg_packing WHERE id=? AND factory_id=?", (sid, fac)).fetchone()
        if not r or r["status"] != "packed":
            raise HTTPException(409, {"msg": "ยกเลิกไม่ได้ — FG รับไปแล้ว (FG already has this pallet)"})
        packing_mark(c, fac, sid, "cancelled", _who(u), why)
        c.commit()
    return {"ok": True}


@router.post("/slips-check")
async def slip_check(req: Request):
    """Packing-room checker: approve slips (FG may then receive them) or reject with a reason."""
    u, fac, _ = ctx(req, "packcheck")
    b = await req.json()
    ids, action = [int(x) for x in (b.get("ids") or [])], b.get("action")
    note = (b.get("note") or "").strip()[:300]
    if action not in ("approve", "reject") or not ids:
        raise HTTPException(400, {"msg": "nothing selected"})
    if action == "reject" and not note:
        raise HTTPException(400, {"msg": "ใส่เหตุผลที่ไม่อนุมัติ (give a reason)"})
    who, done = _who(u), 0
    with closing(conn()) as c:
        for i in ids:
            r = c.execute("SELECT id FROM fg_packing WHERE id=? AND factory_id=? AND status='packed' AND pc_status IN ('pending','rejected')",
                          (i, fac)).fetchone()
            if not r:
                continue
            c.execute("UPDATE fg_packing SET pc_status=?,pc_by=?,pc_at=?,pc_note=? WHERE id=?",
                      ("approved" if action == "approve" else "rejected", who, now(), note, i))
            _log(c, fac, "packing", i, "pack_" + action, note, who)
            done += 1
        c.commit()
    return {"ok": True, "done": done}


# ---------------------------------------------------------------- old PalletSlip (VB app) import
def import_palletslip(c, fac, pallet_rows, warehouse_rows, log=print):
    """Copy PalletForm rows of the old VB packing app (SQL Server PalletSlip) into fg_packing.
    Keyed on the old 12-digit Bar_Code, so it can be re-run. A slip that is also in
    WarehouseData counts as already moved to FG; Approved/Rejected keep the packing check."""
    ensure(c)
    moved = {str(r.get("Bar_Code") or "").strip() for r in warehouse_rows}
    have = {r[0] for r in c.execute("SELECT legacy_code FROM fg_packing WHERE factory_id=? AND legacy_code<>''", (fac,))}
    k = skipped = 0
    for r in pallet_rows:
        bc = str(r.get("Bar_Code") or "").strip()
        if not bc or bc in have:
            skipped += 1
            continue
        code, lot, sn = (r.get("Formula_Code") or "").strip(), (r.get("Lot_No") or "").strip(), str(r.get("Slip_No") or "").strip()
        bags = _num(r.get("Total_Bag_No"))
        wu = bag_kg(r.get("Bag_Weight"))
        pc = "approved" if str(r.get("Approved", "")).lower() == "yes" else ("rejected" if str(r.get("Rejected", "")).lower() == "yes" else "pending")
        st = "moved_to_fg" if bc in moved else "packed"
        d = _d(r.get("Date"))
        t = _t(r.get("Time"))
        pkey = pkey_of(code, lot, sn)
        if c.execute("SELECT 1 FROM fg_packing WHERE factory_id=? AND pkey=?", (fac, pkey)).fetchone():
            pkey = pkey + "|" + bc                      # an old duplicate slip number — keep both
        c.execute("""INSERT INTO fg_packing(factory_id,pkey,lot_no,pallet_no,slip_no,product_code,product_name,qty,unit,
            weight_unit,bag_weight,total_weight,mfg,exp,doc_no,pack_machine,pd_line,pack_type,station,operator,staff_name,
            raw_qr,packed_at,src,status,status_at,status_by,pc_status,pc_by,pc_at,legacy_code,created_by)
            VALUES(?,?,?,?,?,?,?,?,'กระสอบ',?,?,?,?,?,?,?,?,?,?,?,?,'',?,'palletslip',?,?,?,?,?,?,?,'PalletSlip')""",
            (fac, pkey, lot, sn, int(_num(sn)), code, r.get("Formula_Name", ""), bags, wu, r.get("Bag_Weight", ""),
             _num(r.get("TotalWeight")) or wu * bags, _any_date(r.get("MFG_Date")), _any_date(r.get("Expiry_Date")),
             r.get("Document_No", ""), r.get("Pack_No", ""), r.get("PD_Line", ""), r.get("Pack_Type", ""), r.get("Pack_No", ""),
             r.get("Staff_Name", ""), r.get("Staff_Name", ""), (d + " " + t).strip(), st,
             (d + " " + t).strip() if st == "moved_to_fg" else "", "old system" if st == "moved_to_fg" else "",
             pc, r.get("Approved_by") or r.get("Rejected_by") or "", r.get("Approved_DT") or r.get("Rejected_DT") or "", bc))
        have.add(bc)
        k += 1
    c.commit()
    log(f"[fg] PalletSlip import into plant {fac}: +{k} slips ({skipped} skipped, {len(moved)} in WarehouseData)")
    return {"added": k, "skipped": skipped}


def _any_date(s):
    """Old slips typed dates by hand: 14/10/2026, 14 10 2026, 2026-10-14, 10/14/2026 …"""
    s = (s or "").strip()
    if not s:
        return ""
    m = re.match(r"^(\d{4})[-/ .](\d{1,2})[-/ .](\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.match(r"^(\d{1,2})[-/ .](\d{1,2})[-/ .](\d{2,4})", s)
        if not m:
            return ""
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if mo > 12 and d <= 12:
            d, mo = mo, d
    if y < 100:
        y += 2000
    if y > 2400:
        y -= 543                                          # Thai Buddhist year
    try:
        return datetime.date(y, mo, d).isoformat()
    except ValueError:
        return ""

# ---------------------------------------------------------------- masters
@router.get("/products")
async def products(req: Request, q: str = ""):
    u, fac, _ = ctx(req, "view")
    with closing(conn()) as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM fg_products WHERE factory_id=? ORDER BY code", (fac,))]
        types = [dict(r) for r in c.execute("SELECT code,name FROM fg_product_types WHERE factory_id=? ORDER BY code", (fac,))]
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in (r["code"] + " " + r["name"]).lower()]
    return {"rows": rows, "types": types}


@router.post("/products")
async def product_save(req: Request):
    u, fac, _ = ctx(req, "admin")
    b = await req.json()
    code = (b.get("code") or "").strip()
    if not code:
        raise HTTPException(400, {"msg": "code required"})
    with closing(conn()) as c:
        vals = ((b.get("name") or "").strip(), (b.get("type_id") or "").strip(), (b.get("packaging") or "").strip(),
                _num(b.get("weight")), int(_num(b.get("shelf_life_days"))), (b.get("owner") or "").strip(),
                1 if b.get("active", True) else 0, now())
        if c.execute("SELECT 1 FROM fg_products WHERE factory_id=? AND code=?", (fac, code)).fetchone():
            c.execute("""UPDATE fg_products SET name=?,type_id=?,packaging=?,weight=?,shelf_life_days=?,owner=?,active=?,
                         updated_at=? WHERE factory_id=? AND code=?""", vals + (fac, code))
        else:
            c.execute("""INSERT INTO fg_products(name,type_id,packaging,weight,shelf_life_days,owner,active,updated_at,
                         factory_id,code) VALUES(?,?,?,?,?,?,?,?,?,?)""", vals + (fac, code))
        c.commit()
    return {"ok": True}


@router.get("/customers")
async def customers(req: Request):
    u, fac, _ = ctx(req, "view")
    with closing(conn()) as c:
        return {"rows": [dict(r) for r in c.execute("SELECT * FROM fg_customers WHERE factory_id=? ORDER BY code", (fac,))]}


@router.post("/customers")
async def customer_save(req: Request):
    u, fac, _ = ctx(req, "admin")
    b = await req.json()
    code = (b.get("code") or "").strip()
    if not code:
        raise HTTPException(400, {"msg": "code required"})
    with closing(conn()) as c:
        if c.execute("SELECT 1 FROM fg_customers WHERE factory_id=? AND code=?", (fac, code)).fetchone():
            c.execute("UPDATE fg_customers SET name=?,active=? WHERE factory_id=? AND code=?",
                      ((b.get("name") or "").strip(), 1 if b.get("active", True) else 0, fac, code))
        else:
            c.execute("INSERT INTO fg_customers(factory_id,code,name,active) VALUES(?,?,?,1)", (fac, code, (b.get("name") or "").strip()))
        c.commit()
    return {"ok": True}


@router.get("/people")
async def people(req: Request):
    u, fac, _ = ctx(req, "view")
    with closing(conn()) as c:
        return {"rows": [dict(r) for r in c.execute("SELECT * FROM fg_people WHERE factory_id=? ORDER BY kind,name", (fac,))]}


@router.post("/people")
async def people_save(req: Request):
    u, fac, _ = ctx(req, "admin")
    b = await req.json()
    kind, name = b.get("kind"), (b.get("name") or "").strip()
    if kind not in ("checker", "leader") or not name:
        raise HTTPException(400, {"msg": "kind and name required"})
    with closing(conn()) as c:
        if c.execute("SELECT 1 FROM fg_people WHERE factory_id=? AND kind=? AND name=?", (fac, kind, name)).fetchone():
            c.execute("UPDATE fg_people SET active=? WHERE factory_id=? AND kind=? AND name=?",
                      (1 if b.get("active", True) else 0, fac, kind, name))
        else:
            c.execute("INSERT INTO fg_people(factory_id,kind,name,active) VALUES(?,?,?,1)", (fac, kind, name))
        c.commit()
    return {"ok": True}


# ---------------------------------------------------------------- users
@router.get("/users")
async def users(req: Request):
    u, fac, _ = ctx(req, "admin")
    with closing(conn()) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id,username,name,role,COALESCE(fg_role,'') fg_role,COALESCE(fg_roles,'') fg_roles,"
            "COALESCE(fg_default,'') fg_default,COALESCE(fg_only,0) fg_only,factory_id,active "
            "FROM users WHERE active=1 AND COALESCE(can_login,1)=1 AND (factory_id=? OR COALESCE(factory_id,0)=0) "
            "ORDER BY (COALESCE(fg_role,'')=''), name", (fac,))]
    return {"users": rows, "roles": FG_ROLES}


@router.get("/users/{uid}")
async def user_get(req: Request, uid: int):
    """FG role of one account — used by CMMS › Manage › Users."""
    u, fac, _ = ctx(req, "admin")
    with closing(conn()) as c:
        r = c.execute("SELECT COALESCE(fg_role,'') fg_role, COALESCE(fg_roles,'') fg_roles, COALESCE(fg_default,'') fg_default,"
                      " COALESCE(fg_only,0) fg_only, role FROM users WHERE id=?", (uid,)).fetchone()
    if not r:
        raise HTTPException(404, {"msg": "not found"})
    d = dict(r)
    d["fg_roles"] = _clean_roles(d["fg_roles"] or d["fg_role"])
    return d


@router.post("/users/{uid}")
async def user_set(req: Request, uid: int):
    u, fac, _ = ctx(req, "admin")
    b = await req.json()
    rs = _clean_roles(b["fg_roles"] if "fg_roles" in b else (b.get("fg_role") or ""))
    only = 1 if (b.get("fg_only") and rs) else 0
    dflt = (b.get("fg_default") or "").strip()
    if dflt not in rs and not (dflt == "cmms" and not only):
        dflt = rs[0] if (rs and only) else ("cmms" if not only else "")
    with closing(conn()) as c:
        c.execute("UPDATE users SET fg_role=?, fg_roles=?, fg_default=?, fg_only=? WHERE id=?",
                  (rs[0] if rs else "", ",".join(rs), dflt if rs else "", only, uid))
        c.commit()
    return {"ok": True, "fg_roles": rs, "fg_default": dflt}


@router.post("/users")
async def user_new(req: Request):
    """New FG-only login (goes straight to the FG app). CMMS role 'operator', no CMMS menu."""
    u, fac, _ = ctx(req, "admin")
    b = await req.json()
    un, name, pw = (b.get("username") or "").strip(), (b.get("name") or "").strip(), b.get("password") or ""
    rs = _clean_roles(b.get("fg_roles") or b.get("fg_role") or "")
    r = rs[0] if rs else ""
    if not un or not name or len(pw) < 4 or not rs:
        raise HTTPException(400, {"msg": "username, name, password (4+ chars) and FG role are required"})
    with closing(conn()) as c:
        if c.execute("SELECT 1 FROM users WHERE username=?", (un,)).fetchone():
            raise HTTPException(400, {"msg": "ชื่อผู้ใช้นี้มีแล้ว (username already exists)"})
        uid = c.insert_id("""INSERT INTO users(username,password,name,role,active,factory_id,department,photo,can_login)
                             VALUES(?,?,?,'operator',1,?,'WRH','',1)""", (un, hash_pw(pw), name, fac))
        c.execute("UPDATE users SET fg_role=?, fg_roles=?, fg_default=?, fg_only=1 WHERE id=?", (r, ",".join(rs), r, uid))
        c.commit()
    return {"ok": True, "id": uid}


@router.post("/import-access")
async def import_access_now(req: Request):
    """Admin button: load the Access history (fg_import/*.csv.gz next to run.py) into this plant.
    Safe to press again — only rows with new Access IDs are added."""
    import asyncio
    from .config import BASE
    u, fac, _ = ctx(req, "admin")
    folder = os.path.join(BASE, "fg_import")
    data = read_csv_folder(folder)
    if not data.get("tbl_Receive_Product") and not data.get("tbl_Product"):
        raise HTTPException(404, {"msg": f"ไม่พบไฟล์ใน fg_import (no export files found in {folder})"})

    def run():
        with closing(conn()) as c:
            n = import_access(c, fac, data)
            n["pallet slips"] = access_to_slips(c, fac, _who(u))   # b488: QR slip per received Access pallet
            c.commit()
            return n
    n = await asyncio.to_thread(run)
    return {"ok": True, "added": n}


# ================================================================ Access import
ACCESS_TABLES = ("tbl_ProductType", "tbl_Product", "tbl_Customer", "tbl_Checker", "tbl_EMPshift",
                 "tbl_Receive_Product", "tbl_Release_Product")


def read_csv_folder(folder):
    out = {}
    import gzip
    for t in ACCESS_TABLES:
        p = os.path.join(folder, t + ".csv")
        if os.path.exists(p):
            with open(p, encoding="utf-8-sig", newline="") as f:
                out[t] = list(csv.DictReader(f))
        elif os.path.exists(p + ".gz"):
            with gzip.open(p + ".gz", "rt", encoding="utf-8-sig", newline="") as f:
                out[t] = list(csv.DictReader(f))
    return out


def read_accdb(path):
    """Read the Access file directly (Windows, Microsoft Access Database Engine + pyodbc)."""
    import pyodbc
    cn = pyodbc.connect(r"DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};DBQ=" + path + ";")
    out = {}
    cur = cn.cursor()
    for t in ACCESS_TABLES:
        cur.execute(f"SELECT * FROM [{t}]")
        cols = [d[0] for d in cur.description]
        rows = []
        for r in cur.fetchall():
            rows.append({k: ("" if v is None else (v.strftime("%Y-%m-%d %H:%M:%S") if hasattr(v, "strftime") else str(v)))
                         for k, v in zip(cols, r)})
        out[t] = rows
    cn.close()
    return out


def _d(s):
    s = (s or "").strip()
    if not s:
        return ""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return s[:10]
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})", s)          # mdb-export default m/d/yy
    if m:
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        return f"{y:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return s[:10]


def _t(s):
    m = re.search(r"(\d{1,2}):(\d{2})", s or "")
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


def import_access(c, fac, data, log=print):
    ensure(c)
    n = {}
    for r in data.get("tbl_ProductType", []):
        c.execute("INSERT OR REPLACE INTO fg_product_types(factory_id,code,name) VALUES(?,?,?)",
                  (fac, r["ProductTpyeID"], r["ProducttypeName"]))
    n["types"] = len(data.get("tbl_ProductType", []))
    k = 0
    for r in data.get("tbl_Product", []):
        if not r.get("ProductID"):
            continue
        if c.execute("SELECT 1 FROM fg_products WHERE factory_id=? AND code=?", (fac, r["ProductID"])).fetchone():
            continue                                   # never overwrite a product edited in the app
        c.execute("""INSERT INTO fg_products(factory_id,code,name,type_id,packaging,weight,shelf_life_days,owner,active,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?)""",
                  (fac, r["ProductID"], r.get("ProductName", ""), r.get("ProductTpyeID", ""), r.get("PackagingName", ""),
                   _num(r.get("Weight")), int(_num(r.get("Shelflife"))), r.get("Owner", ""),
                   0 if str(r.get("Status", "1")).strip().lower() in ("0", "false") else 1, now()))
        k += 1
    n["products"] = k
    k = 0
    for r in data.get("tbl_Customer", []):
        if r.get("CustomerID") and not c.execute("SELECT 1 FROM fg_customers WHERE factory_id=? AND code=?", (fac, r["CustomerID"])).fetchone():
            c.execute("INSERT INTO fg_customers(factory_id,code,name,active) VALUES(?,?,?,1)", (fac, r["CustomerID"], r.get("CustomerName", "")))
            k += 1
    n["customers"] = k
    for kind, t, col in (("checker", "tbl_Checker", "CName"), ("leader", "tbl_EMPshift", "EmpShiftLeader")):
        for r in data.get(t, []):
            if (r.get(col) or "").strip():
                c.execute("INSERT OR IGNORE INTO fg_people(factory_id,kind,name,active) VALUES(?,?,?,1)", (fac, kind, r[col].strip()))
    wt = {r["code"]: _num(r["weight"]) for r in c.execute("SELECT code,weight FROM fg_products WHERE factory_id=?", (fac,))}
    have = {r[0] for r in c.execute("SELECT access_id FROM fg_receive WHERE factory_id=? AND src='access'", (fac,))}
    k = 0
    for r in data.get("tbl_Receive_Product", []):
        aid = int(_num(r.get("ID")))
        if aid in have:
            continue
        q = _num(r.get("Qty"))
        w = wt.get(r.get("ProductID"), 0)
        c.execute("""INSERT INTO fg_receive(factory_id,src,access_id,lot_no,product_id,product_name,qty,unit,weight_unit,total_kg,
                     net_weight,receive_date,receive_time,receive_man,receive_type,get_type,location,memo,target_weight,
                     recv_by,recv_at,status,checker,checked_at,log)
                     VALUES(?,'access',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'confirmed','Access',?,?)""",
                  (fac, aid, r.get("LotNo", ""), r.get("ProductID", ""), r.get("ProductName", ""), q, r.get("Unit", ""),
                   w, q * w, r.get("NetWeight", ""), _d(r.get("ReceiveDate")) or _d(r.get("Log")), _t(r.get("ReceiveTime")),
                   r.get("ReceiveMan", ""), r.get("ReceiveType", "") or "FG", r.get("GetType", ""), r.get("Location", ""),
                   r.get("Memo", ""), _num(r.get("TargetWeight")), r.get("User", ""), r.get("Log", ""), r.get("Log", ""),
                   r.get("Log", "")))
        k += 1
    n["receive"] = k
    have = {r[0] for r in c.execute("SELECT access_id FROM fg_release WHERE factory_id=? AND src='access'", (fac,))}
    k = 0
    for r in data.get("tbl_Release_Product", []):
        aid = int(_num(r.get("ID")))
        if aid in have:
            continue
        q = _num(r.get("Qty"))
        tw = _num(r.get("TotalWeight")) or q * wt.get(r.get("ProductID"), 0)
        c.execute("""INSERT INTO fg_release(factory_id,src,access_id,release_no,lot_no,product_id,product_name,packaging,qty,unit,
                     total_kg,release_date,release_time,release_end,release_man,customer_id,release_type,memo,plate,
                     issue_by,issue_at,status,checker,checked_at,log)
                     VALUES(?,'access',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'confirmed','Access',?,?)""",
                  (fac, aid, r.get("ReleaseNo", ""), r.get("LotNo", ""), r.get("ProductID", ""), r.get("ProductName", ""),
                   r.get("PackagingName", ""), q, r.get("Unit", ""), tw, _d(r.get("ReleaseDate")) or _d(r.get("Log")),
                   _t(r.get("ReleaseTime")), _t(r.get("ReleaseEndtime")), r.get("ReleaseMan", ""), r.get("CustomerID", ""),
                   r.get("ReleaseType", ""), r.get("Memo", ""), r.get("LicencePlate", ""), r.get("User", ""),
                   r.get("Log", ""), r.get("Log", ""), r.get("Log", "")))
        k += 1
    n["release"] = k
    c.commit()
    log(f"[fg] Access import into plant {fac}: " + ", ".join(f"{a} +{b}" for a, b in n.items()))
    return n


# ================================================================ dashboard (Warehouse home)
@router.get("/dashboard")
async def dashboard(req: Request, days: int = 30):
    """Numbers for the Warehouse home: stock now, and receive / release over the chosen period.
    Works on the imported Access history and the app's own records alike."""
    u, fac, _ = ctx(req, "check")
    days = days if days in (7, 30, 90, 365) else 30
    d2 = _today()
    d1 = (datetime.date.today() - datetime.timedelta(days=days - 1)).isoformat()
    monthly = days == 365
    with closing(conn()) as c:
        prods = {r["code"]: dict(r) for r in c.execute("SELECT code,name,type_id,owner,weight FROM fg_products WHERE factory_id=?", (fac,))}
        types = {r["code"]: r["name"] for r in c.execute("SELECT code,name FROM fg_product_types WHERE factory_id=?", (fac,))}
        first = {(r["p"], r["l"]): r["d"] for r in c.execute(
            "SELECT product_id p, lot_no l, MIN(receive_date) d FROM fg_receive WHERE factory_id=? AND status='confirmed' GROUP BY 1,2", (fac,))}
        names = {r["product_id"]: r["n"] for r in c.execute(
            "SELECT product_id, MAX(product_name) n FROM fg_receive WHERE factory_id=? GROUP BY 1", (fac,))}
        # ---- stock now
        st = {"bags": 0, "kg": 0, "lots": 0}
        by_type, by_group, by_prod = {}, {}, {}
        ages = [["0–30", 0], ["31–90", 0], ["91–180", 0], ["181–365", 0], ["> 365", 0]]
        today = datetime.date.today()
        for b in lot_balances(c, fac):
            bal = (b["rq"] or 0) - (b["sq"] or 0)
            if bal <= 0:
                continue
            p = prods.get(b["p"]) or {}
            kg = bal * _num(p.get("weight"))
            st["bags"] += bal
            st["kg"] += kg
            st["lots"] += 1
            t = types.get(p.get("type_id"), p.get("type_id") or "อื่นๆ")
            by_type[t] = by_type.get(t, 0) + kg
            g = group_of(p.get("owner")) or "Other"
            by_group[g] = by_group.get(g, 0) + kg
            k = b["p"]
            e = by_prod.setdefault(k, {"code": k, "name": p.get("name") or names.get(k, ""), "kg": 0, "bags": 0, "lots": 0})
            e["kg"] += kg
            e["bags"] += bal
            e["lots"] += 1
            born = mfg_from_lot(b["l"]) or (first.get((b["p"], b["l"])) or "")[:10]
            try:
                a = (today - datetime.date.fromisoformat(born)).days
            except ValueError:
                a = None
            if a is not None:
                i = 0 if a <= 30 else 1 if a <= 90 else 2 if a <= 180 else 3 if a <= 365 else 4
                ages[i][1] += kg
        # ---- period
        key = (lambda d: d[:7]) if monthly else (lambda d: d[:10])
        series = {}
        if monthly:
            y, m = today.year, today.month
            for _ in range(12):
                series[f"{y:04d}-{m:02d}"] = {"in": 0, "out": 0}
                m -= 1
                if m == 0:
                    y, m = y - 1, 12
        else:
            for i in range(days):
                series[(today - datetime.timedelta(days=i)).isoformat()] = {"in": 0, "out": 0}
        tin = tout = 0
        for r in c.execute("SELECT receive_date d, SUM(total_kg) kg FROM fg_receive WHERE factory_id=? AND status='confirmed' "
                           "AND receive_date>=? AND receive_date<=? GROUP BY 1", (fac, d1 if not monthly else min(series)[:7] + "-01", d2 + " 23:59:59")):
            k = key(r["d"] or "")
            if k in series:
                series[k]["in"] += r["kg"] or 0
                tin += r["kg"] or 0
        rtypes, custs = {}, {}
        for r in c.execute("SELECT release_date d, release_type t, customer_id cu, SUM(total_kg) kg FROM fg_release WHERE factory_id=? "
                           "AND status='confirmed' AND release_date>=? AND release_date<=? GROUP BY 1,2,3",
                           (fac, d1 if not monthly else min(series)[:7] + "-01", d2 + " 23:59:59")):
            k = key(r["d"] or "")
            if k not in series:
                continue
            kg = r["kg"] or 0
            series[k]["out"] += kg
            tout += kg
            rtypes[r["t"] or "-"] = rtypes.get(r["t"] or "-", 0) + kg
            custs[r["cu"] or "-"] = custs.get(r["cu"] or "-", 0) + kg
        cname = {r["code"]: r["name"] for r in c.execute("SELECT code,name FROM fg_customers WHERE factory_id=?", (fac,))}
        q = lambda sql: c.execute(sql, (fac,)).fetchone()[0]
        waiting = {"recv": q("SELECT COUNT(*) FROM fg_receive WHERE factory_id=? AND status='waiting'"),
                   "issue": q("SELECT COUNT(*) FROM fg_release WHERE factory_id=? AND status='waiting'"),
                   "pack": q("SELECT COUNT(*) FROM fg_packing WHERE factory_id=? AND status='packed' AND pc_status='pending'")}
        last = c.execute("SELECT MAX(receive_date) FROM fg_receive WHERE factory_id=? AND status='confirmed'", (fac,)).fetchone()[0]
    srt = lambda d: sorted(([k, v] for k, v in d.items()), key=lambda x: -x[1])
    return {"days": days, "d1": d1, "d2": d2, "monthly": monthly, "stock": st,
            "by_type": srt(by_type), "by_group": srt(by_group), "ages": ages,
            "top_products": sorted(by_prod.values(), key=lambda x: -x["kg"])[:10],
            "series": [{"k": k, **v} for k, v in sorted(series.items())],
            "period": {"in_kg": tin, "out_kg": tout}, "release_types": srt(rtypes),
            "top_customers": [{"code": k, "name": cname.get(k, ""), "kg": v} for k, v in srt(custs)[:10]],
            "waiting": waiting, "last_receive": (last or "")[:10]}


# ================================================================ plants (app screen)
@router.get("/plants")
async def plants(req: Request):
    """Plants and whether FG Stock is used there — drives the app screen."""
    u = user_from(req)
    with closing(conn()) as c:
        rows = [dict(r) for r in c.execute("SELECT id,code,name FROM factories ORDER BY id")]
        for r in rows:
            r["fg"] = plant_on(c, r["id"])
    return {"plants": rows}


@router.post("/plants")
async def plants_set(req: Request):
    u = user_from(req)
    if u.get("role") != "admin":
        raise HTTPException(403, {"msg": "admin only"})
    b = await req.json()
    fid = int(b.get("factory_id") or 0)
    with closing(conn()) as c:
        if not c.execute("SELECT 1 FROM factories WHERE id=?", (fid,)).fetchone():
            raise HTTPException(404, {"msg": "unknown plant"})
        c.execute("DELETE FROM fg_plants WHERE factory_id=?", (fid,))
        c.execute("INSERT INTO fg_plants(factory_id,enabled) VALUES(?,?)", (fid, 1 if b.get("enabled") else 0))
        c.commit()
    return {"ok": True}


# ================================================================ type-ahead suggestions
@router.get("/suggest")
async def suggest(req: Request, q: str = "", kind: str = "all", product: str = ""):
    """Drop-down suggestions while typing: products (code + name) and lot numbers."""
    u, fac, _ = ctx(req, "view")
    q = (q or "").strip()
    if len(q) < 1 and not (kind == "lot" and product):
        return {"items": []}
    like = "%" + q + "%"
    out, lots = [], []
    per = 7 if kind == "all" else 14
    with closing(conn()) as c:
        if kind in ("all", "product"):
            for r in c.execute("""SELECT code, name FROM fg_products WHERE factory_id=? AND (code LIKE ? OR name LIKE ?)
                                  ORDER BY CASE WHEN code LIKE ? THEN 0 ELSE 1 END, active DESC, code LIMIT ?""",
                               (fac, like, like, q + "%", per)):
                out.append({"v": r["code"], "t": r["code"], "s": r["name"], "k": "product"})
        if kind in ("all", "lot"):
            args = [fac, like]
            pc = ""
            if product:
                pc = " AND product_id=?"
                args.append(product)
            for r in c.execute(f"""SELECT lot_no, MAX(product_name) n, MAX(receive_date) d FROM fg_receive WHERE factory_id=? AND lot_no LIKE ?{pc}
                                   GROUP BY lot_no ORDER BY MAX(receive_date) DESC LIMIT ?""", tuple(args + [per])):
                lots.append({"v": r["lot_no"], "t": r["lot_no"], "s": "Lot · " + (r["n"] or "") + (" · " + r["d"][:10] if r["d"] else ""), "k": "lot"})
    # a lot number starts with digits (YYMMDD…) — show lots first when the typing looks like one
    return {"items": lots + out if q[:1].isdigit() else out + lots}

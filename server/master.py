"""Production → Master Data (b495).

The product master list the Production team keeps from what R&D hands over — the paper
form F-SP-RDP01-06 "LIST OF MASTER FORMULA". One list per plant (BFLPC first).

Who: the menu and the list are for the Production department (department PRD) and
admin. Editing is for Production MANAGERS and admin; other PRD users read only.
Nothing is ever deleted — a product is switched to Disable, as on the sheet.
"""
import io
import re
from contextlib import closing

from fastapi import APIRouter, Request, HTTPException

from .db import db, now, dept_code
from .auth import user_from

router = APIRouter(prefix="/api/master")

# (db column, Excel heading) in the sheet's own order
COLS = [("no", "No."), ("status", "Status"), ("cust_code", "Cust Code"),
        ("product_name", "Product Name"), ("reg_no", "Registration Number"),
        ("order_detail", "Order detail"), ("document", "Document"),
        ("packing_line", "Packing Line"), ("fg_code", "FG CODE"), ("pkg_code", "PKG CODE"),
        ("color1", "COLOR#1"), ("color2", "COLOR#2"), ("color3", "COLOR#3"), ("color4", "COLOR#4"),
        ("grp", "GROUP"), ("kibble_ratio", "KIBBLE MIX RATIO"), ("customer_name", "CUSTOMER NAME"),
        ("mix", "MIX"), ("packsize", "PACKSIZE")]
FIELDS = [c for c, _ in COLS]
EDIT = [c for c in FIELDS if c != "no"]


def ensure(c):
    c.execute(f"""CREATE TABLE IF NOT EXISTS product_master(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER,
        no INTEGER DEFAULT 0, {", ".join(f"{f} TEXT DEFAULT ''" for f in EDIT)},
        created_at TEXT DEFAULT '', created_by TEXT DEFAULT '',
        updated_at TEXT DEFAULT '', updated_by TEXT DEFAULT '')""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_pm_fac ON product_master(factory_id, no)")
    c.execute("""CREATE TABLE IF NOT EXISTS product_master_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER, item_id INTEGER,
        action TEXT, field TEXT DEFAULT '', old TEXT DEFAULT '', new TEXT DEFAULT '',
        at TEXT DEFAULT '', by TEXT DEFAULT '')""")


def _fac(u):
    return u.get("active_factory") or u.get("factory_id")


def can_see(u):
    return u.get("role") == "admin" or dept_code(u.get("department") or "") == "PRD"


def can_edit(u):
    return u.get("role") == "admin" or (u.get("role") == "manager"
                                        and dept_code(u.get("department") or "") == "PRD")


def _guard(req, edit=False):
    u = user_from(req)
    if not (can_edit(u) if edit else can_see(u)):
        raise HTTPException(403, {"msg": "Master Data is for the Production team" if not edit
                                  else "Only Production managers can change Master Data"})
    return u


def _clean(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


@router.get("")
async def list_items(req: Request):
    u = _guard(req)
    with closing(db()) as c:
        ensure(c)
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM product_master WHERE factory_id=? ORDER BY no, id", (_fac(u),))]
    return {"items": rows, "can_edit": can_edit(u),
            "cols": [{"k": k, "h": h} for k, h in COLS]}


def _body(b):
    d = {f: _clean(b.get(f)) for f in EDIT if f in b}
    if "status" in d and d["status"] not in ("Active", "Disable"):
        d["status"] = "Active"
    return d


@router.post("")
async def add_item(req: Request):
    u = _guard(req, edit=True)
    b = _body(await req.json())
    if not b.get("product_name"):
        raise HTTPException(400, {"msg": "Product Name is required"})
    fac = _fac(u)
    with closing(db()) as c:
        ensure(c)
        if b.get("fg_code") and c.execute(
                "SELECT 1 FROM product_master WHERE factory_id=? AND fg_code=? AND fg_code NOT IN ('','-')",
                (fac, b["fg_code"])).fetchone():
            raise HTTPException(400, {"msg": f"FG CODE {b['fg_code']} is already in the list"})
        no = (c.execute("SELECT COALESCE(MAX(no),0) FROM product_master WHERE factory_id=?",
                        (fac,)).fetchone()[0] or 0) + 1
        b.setdefault("status", "Active")
        cols = ["factory_id", "no", *b.keys(), "created_at", "created_by", "updated_at", "updated_by"]
        vals = [fac, no, *b.values(), now(), u.get("name") or "", now(), u.get("name") or ""]
        new_id = c.insert_id(f"INSERT INTO product_master({','.join(cols)}) VALUES({','.join('?'*len(cols))})", vals)
        c.execute("INSERT INTO product_master_log(factory_id,item_id,action,new,at,by) VALUES(?,?,?,?,?,?)",
                  (fac, new_id, "add", b.get("product_name"), now(), u.get("name") or ""))
        c.commit()
        return {"ok": True, "id": new_id, "no": no}


@router.patch("/{iid}")
async def edit_item(iid: int, req: Request):
    u = _guard(req, edit=True)
    b = _body(await req.json())
    fac = _fac(u)
    with closing(db()) as c:
        ensure(c)
        old = c.execute("SELECT * FROM product_master WHERE id=? AND factory_id=?", (iid, fac)).fetchone()
        if not old:
            raise HTTPException(404, {"msg": "not found"})
        ch = {k: v for k, v in b.items() if str(old[k] or "") != v}
        if not ch:
            return {"ok": True, "changed": 0}
        c.execute(f"UPDATE product_master SET {', '.join(f'{k}=?' for k in ch)}, updated_at=?, updated_by=?"
                  " WHERE id=?", [*ch.values(), now(), u.get("name") or "", iid])
        for k, v in ch.items():
            c.execute("INSERT INTO product_master_log(factory_id,item_id,action,field,old,new,at,by)"
                      " VALUES(?,?,?,?,?,?,?,?)", (fac, iid, "edit", k, old[k] or "", v, now(), u.get("name") or ""))
        c.commit()
        return {"ok": True, "changed": len(ch)}


@router.get("/{iid}/log")
async def item_log(iid: int, req: Request):
    u = _guard(req)
    with closing(db()) as c:
        ensure(c)
        return {"log": [dict(r) for r in c.execute(
            "SELECT * FROM product_master_log WHERE item_id=? AND factory_id=? ORDER BY id DESC LIMIT 200",
            (iid, _fac(u)))]}


@router.post("/import")
async def import_xlsx(req: Request, name: str = "", replace: str = ""):
    """Load the R&D Excel (F-SP-RDP01-06). Rows are matched by FG CODE + Product Name + Cust
    Code: a known row is updated, a new one is added. ?replace=1 empties this plant's list first (admin only)."""
    u = _guard(req, edit=True)
    import openpyxl
    data = await req.body()          # the browser sends the .xlsx bytes as the body (no multipart needed)
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception:
        raise HTTPException(400, {"msg": "That file is not an Excel workbook (.xlsx)"})
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    norm = lambda s: re.sub(r"[^A-Z0-9#]", "", str(s or "").upper())
    want = {norm(h): k for k, h in COLS}
    hi, idx = None, {}
    for i, r in enumerate(rows[:20]):                     # find the heading row
        m = {j: want[norm(v)] for j, v in enumerate(r) if norm(v) in want}
        if "product_name" in m.values() and len(m) >= 5:
            hi, idx = i, m
            break
    if hi is None:
        raise HTTPException(400, {"msg": "Could not find the heading row (No. / Status / Product Name …)"})
    fac = _fac(u)
    added = updated = 0
    with closing(db()) as c:
        ensure(c)
        if replace and u.get("role") == "admin":
            c.execute("DELETE FROM product_master WHERE factory_id=?", (fac,))
        for r in rows[hi + 1:]:
            rec = {k: _clean(r[j]) for j, k in idx.items() if j < len(r)}
            if not rec.get("product_name"):
                continue
            no = int(rec.pop("no")) if str(rec.get("no", "")).isdigit() else None
            rec.pop("no", None)
            # matched on FG CODE + Product Name + Cust Code together: the R&D sheet repeats
            # an FG code (even a name) for two customers, and matching on less overwrote one
            ex = c.execute("SELECT id FROM product_master WHERE factory_id=? AND fg_code=?"
                           " AND product_name=? AND cust_code=?",
                           (fac, rec.get("fg_code", ""), rec["product_name"],
                            rec.get("cust_code", ""))).fetchone()
            if ex:
                c.execute(f"UPDATE product_master SET {', '.join(f'{k}=?' for k in rec)}, updated_at=?, updated_by=?"
                          " WHERE id=?", [*rec.values(), now(), u.get("name") or "", ex["id"]])
                updated += 1
            else:
                if no is None:
                    no = (c.execute("SELECT COALESCE(MAX(no),0) FROM product_master WHERE factory_id=?",
                                    (fac,)).fetchone()[0] or 0) + 1
                cols = ["factory_id", "no", *rec.keys(), "created_at", "created_by", "updated_at", "updated_by"]
                c.execute(f"INSERT INTO product_master({','.join(cols)}) VALUES({','.join('?'*len(cols))})",
                          [fac, no, *rec.values(), now(), u.get("name") or "", now(), u.get("name") or ""])
                added += 1
        c.execute("INSERT INTO product_master_log(factory_id,item_id,action,new,at,by) VALUES(?,?,?,?,?,?)",
                  (fac, 0, "import", f"{name or 'excel'}: +{added} / ~{updated}", now(), u.get("name") or ""))
        c.commit()
    return {"ok": True, "added": added, "updated": updated}

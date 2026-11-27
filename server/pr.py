"""PR — purchase requests (ใบขอซื้อ). Brings the stand-alone "PR Document Tracking" app
(Flask, its own users and SQLite file) into the Smart Factory login.

A fourth app inside the CMMS login (after CMMS, Warehouse and FG Stock): same server, same
users, same plant chosen at sign-in. Page: /pr.html. API: /api/pr/*. Tables: pr_*.
Files (quotation, PR PDF, PO PDF): DATA/pr/ — inside data/, so the nightly backup has them.

THE FLOW
  1. Anyone with a PR role writes a request: company, department, purpose, reason, supplier
     (optional), item lines (description · qty · unit price) and VAT 7 % yes/no. It gets a
     document code DEPT-COMPANY-YYMM-NNN (ENG-BFL-2611-001), numbered across the month.
       a new request starts as pending_approval (รออนุมัติ). waiting_quotation is only
       kept so old rows still have a label; ensure() moves them to pending_approval.
  2. Approver approves or cancels (cancel needs a reason). Approve needs amount + quotation.
     An approved / cancelled request is locked: only the approver (and super admin) edits it.
  3. PR Admin records the PR number PRYYMM-XXXX (types YYMM-XXXX after the fixed "PR") (+ PR PDF). The PR status follows the number:
     a number = approved, and the request becomes approved too; no number = pending_approval.
     Cancelling the PR number cancels the request.
  4. PO Admin records the PO number <plant>-POXXXX-XXXX (+ PO PDF) and whether it went to the
     supplier.
Money is kept as text "1234.50" and added up with Decimal, never float. Every change writes
pr_log. Nothing reads another module: users / departments / factories are core tables.

ROLES (users.pr_role, set in CMMS › Manage › Users or PR › Users)
  user · pr_admin · po_admin · approver · super_admin. CMMS admins are always super_admin.
  An empty pr_role = no PR access (the menu does not show PR).

OLD APP. import_legacy() copies the old pr_tracker.sqlite + instance/uploads in once
(re-runnable: keyed on the old ids). Run: python tools/import_pr_tracker.py --src <folder>
"""
import asyncio
import base64
import csv
import io
import json
import os
import re
import secrets
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from .auth import user_from
from .config import DATA
from .db import db, now, _table_cols

router = APIRouter(prefix="/api/pr")

FILES = os.path.join(DATA, "pr")

PR_ROLES = ("user", "pr_admin", "po_admin", "approver", "super_admin")
ROLE_LABELS = {
    "user": ("ผู้ใช้ทั่วไป", "User"),
    "pr_admin": ("PR Admin", "PR Admin"),
    "po_admin": ("PO Admin", "PO Admin"),
    "approver": ("ผู้อนุมัติเอกสาร", "Approver"),
    "super_admin": ("Super Admin", "Super Admin"),
}
_BASE = {"view", "create", "suppliers_view"}
CAN = {
    "user": _BASE,
    "pr_admin": _BASE | {"manage_pr"},
    "po_admin": _BASE | {"manage_po", "manage_suppliers"},
    "approver": _BASE | {"approve", "view_log"},
    "super_admin": _BASE | {"manage_pr", "manage_po", "manage_suppliers", "approve", "view_log",
                            "delete", "users"},
}

STATUSES = {
    "waiting_quotation": ("รอใบเสนอราคา", "Waiting for quotation"),
    "pending_approval": ("รออนุมัติ", "Waiting for approval"),
    "approved": ("อนุมัติ", "Approved"),
    "cancelled": ("ยกเลิก", "Cancelled"),
}
LOCKED = ("approved", "cancelled")
PURPOSES = {
    "Stock": ("Stock", "Stock"),
    "Project": ("Project", "Project"),
    "Repair": ("งานซ่อม", "Repair"),
    "Other": ("อื่น ๆ", "Other"),
}
PR_STATUSES = {
    "pending_approval": ("รออนุมัติ", "Waiting for approval"),
    "approved": ("อนุมัติแล้ว", "Approved"),
    "cancelled": ("ยกเลิก", "Cancelled"),
}
PO_STATUSES = {
    "sent_to_supplier": ("ส่งให้ supplier แล้ว", "Sent to supplier"),
    "not_sent_to_supplier": ("ยังไม่ส่งให้ supplier", "Not sent to supplier"),
}
# การตั้งเบิก on the paper form (optional). pay_by = "ภายในวันที่".
PAYMENT_METHODS = {
    "advance": ("เงินทดรองจ่าย", "Cash advance"),
    "petty_cash": ("เงินสดย่อย", "Petty cash"),
    "purchasing_cash": ("เงินสดแผนกจัดซื้อ", "Purchasing dept. cash"),
}
# Full company names printed on the paper form, and companies that are not a plant in `factories`.
COMPANY_TH = {
    "BFL": "บริษัท บลูฟาโล่ จำกัด",
    "BFLFP": "บริษัท บลูฟาโล่ ฟู้ด โปรดักส์ จำกัด",
    "BFLPC": "บริษัท บลูฟาโล่ เพ็ทแคร์ จำกัด",
    "SMEC": "บริษัท สยามแมค เอ็นจิเนียริ่ง แอน คอนสตรัคชั่น จำกัด",
}
EXTRA_COMPANIES = [("SMEC", "Siam Mac Engineering and Construction")]
FILE_KINDS = ("quotation", "pr", "po")
# a PR company code per plant (factories.code). The PO prefix is the plant code itself.
COMPANY_OF_PLANT = {"BFL": "BFL", "FP": "BFLFP", "PC": "BFLPC"}

CENT = Decimal("0.01")
VAT_RATE = Decimal("0.07")
MAX_ITEMS = 50
MAX_FILE = 10 * 1024 * 1024
FILE_TYPES = {
    ".pdf": ("application/pdf", b"%PDF-"),
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
}
PR_SFX = re.compile(r"[0-9]{2}(0[1-9]|1[0-2])-[0-9]{4}")   # YYMM-XXXX, digits only
PO_SFX = re.compile(r"[A-Z0-9]{4}-[A-Z0-9]{4}")
SORTS = {"code": "doc_code", "date": "request_date", "company": "company", "department": "department",
         "requester": "requester_name", "supplier": "supplier_name", "total": "CAST(net_total AS REAL)",
         "status": "status", "pr": "pr_number", "po": "po_number", "updated": "updated_at",
         "book": "book_no", "purpose": "purpose"}


def _bad(msg, code=400):
    return HTTPException(code, {"msg": msg})


# ---------------------------------------------------------------- schema
def ensure(c):
    if "pr_role" not in _table_cols(c, "users"):
        c.execute("ALTER TABLE users ADD COLUMN pr_role TEXT DEFAULT ''")
    c.execute("""CREATE TABLE IF NOT EXISTS pr_requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT, doc_code TEXT UNIQUE, company TEXT DEFAULT '',
        department TEXT DEFAULT '', requester_id INTEGER DEFAULT 0, requester_name TEXT DEFAULT '',
        supplier_id INTEGER DEFAULT 0, supplier_name TEXT DEFAULT '', request_date TEXT DEFAULT '',
        purpose TEXT DEFAULT '', purpose_other TEXT DEFAULT '', reason TEXT DEFAULT '',
        amount_entered INTEGER DEFAULT 0, subtotal TEXT DEFAULT '0.00', vat_included INTEGER DEFAULT 1,
        vat TEXT DEFAULT '0.00', grand_total TEXT DEFAULT '0.00', withholding_amount TEXT DEFAULT '0.00',
        net_total TEXT DEFAULT '0.00', status TEXT DEFAULT 'pending_approval', status_note TEXT DEFAULT '',
        status_by TEXT DEFAULT '', status_at TEXT DEFAULT '',
        pr_number TEXT DEFAULT '', pr_status TEXT DEFAULT 'pending_approval', pr_date TEXT DEFAULT '',
        pr_cancel_reason TEXT DEFAULT '',
        po_number TEXT DEFAULT '', po_status TEXT DEFAULT '', po_date TEXT DEFAULT '',
        po_cancel_reason TEXT DEFAULT '',
        job_id INTEGER DEFAULT 0, legacy_id INTEGER DEFAULT 0,
        created_by INTEGER DEFAULT 0, created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '')""")
    have = _table_cols(c, "pr_requests")
    for col in ("payment_method", "pay_by", "book_no", "sheet_no"):
        if col not in have:
            c.execute(f"ALTER TABLE pr_requests ADD COLUMN {col} TEXT DEFAULT ''")
    # PR numbers are stored as PRYYMM-XXXX (a short-lived version saved YYMM-XXXX; put the
    # prefix back), and there is no "waiting for quotation" step: a request waits for approval.
    c.execute("UPDATE pr_requests SET pr_number='PR'||pr_number "
              "WHERE COALESCE(pr_number,'')<>'' AND pr_number NOT LIKE 'PR%'")
    c.execute("UPDATE pr_requests SET status='pending_approval' WHERE status='waiting_quotation'")
    # A PR number means approved. Requests numbered before that rule are still waiting: approve them.
    c.execute("UPDATE pr_requests SET status='approved', pr_status='approved' "
              "WHERE COALESCE(pr_number,'')<>'' AND status='pending_approval' "
              "AND COALESCE(pr_status,'')<>'cancelled'")
    c.execute("CREATE INDEX IF NOT EXISTS ix_pr_req_date ON pr_requests(request_date)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_pr_req_status ON pr_requests(status)")
    c.execute("""CREATE TABLE IF NOT EXISTS pr_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, position INTEGER DEFAULT 0,
        description TEXT DEFAULT '', quantity TEXT DEFAULT '0.00', unit_price TEXT DEFAULT '0.00',
        line_total TEXT DEFAULT '0.00', note TEXT DEFAULT '')""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_pr_items_req ON pr_items(request_id, position)")
    c.execute("""CREATE TABLE IF NOT EXISTS pr_files(
        id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, kind TEXT, original_name TEXT DEFAULT '',
        stored_name TEXT DEFAULT '', size INTEGER DEFAULT 0, uploaded_by TEXT DEFAULT '',
        uploaded_at TEXT DEFAULT '', UNIQUE(request_id, kind))""")
    c.execute("""CREATE TABLE IF NOT EXISTS pr_suppliers(
        id INTEGER PRIMARY KEY AUTOINCREMENT, company_name TEXT DEFAULT '', address TEXT DEFAULT '',
        phone TEXT DEFAULT '', email TEXT DEFAULT '', line_id TEXT DEFAULT '', note TEXT DEFAULT '',
        active INTEGER DEFAULT 1, legacy_id INTEGER DEFAULT 0, created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS pr_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER DEFAULT 0, doc_code TEXT DEFAULT '',
        action TEXT DEFAULT '', summary TEXT DEFAULT '', details_json TEXT DEFAULT '{}',
        user_name TEXT DEFAULT '', legacy_id INTEGER DEFAULT 0, created_at TEXT DEFAULT '')""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_pr_log_at ON pr_log(created_at)")
    os.makedirs(FILES, exist_ok=True)


# ---------------------------------------------------------------- who
def role_of(u):
    """PR role of a person ('' = no PR access). CMMS admins are always super_admin."""
    if u.get("role") == "admin":
        return "super_admin"
    try:
        with closing(db()) as c:
            r = c.execute("SELECT COALESCE(pr_role,'') r FROM users WHERE id=?", (u["id"],)).fetchone()
        r = (r["r"] if r else "") or ""
        return r if r in PR_ROLES else ""
    except Exception:
        return ""          # column not made yet


def caps_of(role):
    return set(CAN.get(role, set()))


def ctx(req: Request, need="view"):
    u = user_from(req)
    role = role_of(u)
    caps = caps_of(role)
    if not role or (need and need not in caps):
        raise _bad("ไม่มีสิทธิ์ (no permission) — ask the PR super admin", 403)
    return u, role, caps


def _who(u):
    return u.get("name") or u.get("username") or ""


def _log(c, rid, code, action, summary, who, details=None):
    c.execute("INSERT INTO pr_log(request_id,doc_code,action,summary,details_json,user_name,created_at) "
              "VALUES(?,?,?,?,?,?,?)",
              (rid or 0, code or "", action, (summary or "")[:500],
               json.dumps(details or {}, ensure_ascii=False)[:4000], who, now()))


# ---------------------------------------------------------------- reference lists
def companies(c):
    out = []
    for f in c.execute("SELECT id,code,name FROM factories ORDER BY id").fetchall():
        code = COMPANY_OF_PLANT.get(f["code"], f["code"])
        out.append({"code": code, "plant_id": f["id"], "plant": f["code"], "name": f["name"],
                    "name_th": COMPANY_TH.get(code, f["name"]), "po_prefix": f["code"]})
    have = {x["code"] for x in out}
    for code, name in EXTRA_COMPANIES:
        if code not in have:
            out.append({"code": code, "plant_id": None, "plant": "", "name": name,
                        "name_th": COMPANY_TH.get(code, name), "po_prefix": ""})
    return out


def requesters(c):
    """Active users who can be named as the requester on the form."""
    try:
        rows = c.execute("SELECT id, name, username FROM users WHERE COALESCE(active,1)=1 "
                         "ORDER BY name, username").fetchall()
    except Exception:
        return []
    return [{"id": r["id"], "name": r["name"] or r["username"]} for r in rows]


def departments(c):
    try:
        return [dict(r) for r in c.execute(
            "SELECT code, name_th, name_en FROM departments WHERE COALESCE(active,1)=1 ORDER BY seq, code")]
    except Exception:
        return [{"code": "ENG", "name_th": "วิศวกรรม", "name_en": "Engineering"},
                {"code": "IT", "name_th": "ไอที", "name_en": "IT"}]


def _po_prefix(c, company):
    for co in companies(c):
        if co["code"] == company:
            return co["po_prefix"]
    return ""


# ---------------------------------------------------------------- money
def _money(v):
    return str(Decimal(v).quantize(CENT, rounding=ROUND_HALF_UP))


def _dec(v, label, allow_zero=True):
    s = str(v if v is not None else "").replace(",", "").strip()
    try:
        d = Decimal(s)
        if not d.is_finite() or d < 0 or (d == 0 and not allow_zero) or d != d.quantize(CENT):
            raise InvalidOperation
        return d
    except (InvalidOperation, ValueError):
        cond = "ไม่ติดลบ" if allow_zero else "มากกว่า 0"
        raise _bad(f"{label} ต้องเป็นตัวเลข{cond} ไม่เกิน 2 ตำแหน่งทศนิยม ({label}: number, max 2 decimals)")


def _items(raw):
    """Clean item rows. Rows left completely empty are skipped."""
    rows = [r for r in (raw or []) if any(str(r.get(k) or "").strip()
                                          for k in ("description", "quantity", "unit_price", "note"))]
    if len(rows) > MAX_ITEMS:
        raise _bad(f"รายการได้สูงสุด {MAX_ITEMS} รายการ (max {MAX_ITEMS} lines)")
    out = []
    for i, r in enumerate(rows, 1):
        desc = str(r.get("description") or "").strip()
        if not desc:
            raise _bad(f"รายการที่ {i}: กรุณาระบุรายการ (line {i}: description)")
        q = _dec(r.get("quantity"), f"รายการที่ {i} จำนวน", allow_zero=False)
        p = _dec(r.get("unit_price"), f"รายการที่ {i} ราคา/หน่วย")
        out.append({"position": i, "description": desc[:500], "quantity": _money(q), "unit_price": _money(p),
                    "line_total": _money(q * p), "note": str(r.get("note") or "").strip()[:500]})
    return out


def totals(items, vat_included, amount=None):
    """(amount_entered, subtotal, vat, grand_total). With item lines the subtotal is their sum;
    without, a typed amount is used (old documents had only a total)."""
    if items:
        sub = sum((Decimal(i["line_total"]) for i in items), Decimal("0"))
        entered = True
    elif amount not in (None, ""):
        sub = _dec(amount, "ยอดรวมราคา")
        entered = True
    else:
        sub, entered = Decimal("0"), False
    vat = (sub * VAT_RATE).quantize(CENT, rounding=ROUND_HALF_UP) if vat_included else Decimal("0")
    return entered, _money(sub), _money(vat), _money(sub + vat)


# ---------------------------------------------------------------- numbering
def next_code(c, department, company, request_date):
    yymm = date.fromisoformat(request_date).strftime("%y%m")
    seqs = []
    for r in c.execute("SELECT doc_code FROM pr_requests WHERE doc_code LIKE ?", (f"%-{yymm}-%",)).fetchall():
        try:
            _, mid, seq = r["doc_code"].rsplit("-", 2)
            if mid == yymm:
                seqs.append(int(seq))
        except ValueError:
            continue
    n = max(seqs, default=0) + 1
    if n > 999:
        raise _bad("เอกสารเดือนนี้ครบ 999 รายการแล้ว (999 documents this month)", 409)
    return f"{department}-{company}-{yymm}-{n:03d}"


# ---------------------------------------------------------------- read
def _files(c, rid):
    return {r["kind"]: {"name": r["original_name"], "size": r["size"], "at": r["uploaded_at"], "by": r["uploaded_by"]}
            for r in c.execute("SELECT * FROM pr_files WHERE request_id=?", (rid,)).fetchall()}


def _ready(c, rid, amount_entered):
    have = _files(c, rid)
    return bool(amount_entered) and ("quotation" in have or "pr" in have)


def _restatus(c, rid):
    """An open request is pending_approval. Approved and cancelled only change by hand."""
    r = c.execute("SELECT status FROM pr_requests WHERE id=?", (rid,)).fetchone()
    if not r or r["status"] in LOCKED:
        return r["status"] if r else ""
    st = "pending_approval"
    if st != r["status"]:
        c.execute("UPDATE pr_requests SET status=? WHERE id=?", (st, rid))
    return st


def _get(c, rid):
    r = c.execute("SELECT * FROM pr_requests WHERE id=?", (rid,)).fetchone()
    if not r:
        raise _bad("ไม่พบเอกสาร (not found)", 404)
    return dict(r)


def _full(c, rid):
    d = _get(c, rid)
    d["items"] = [dict(i) for i in c.execute(
        "SELECT position,description,quantity,unit_price,line_total,note FROM pr_items "
        "WHERE request_id=? ORDER BY position, id", (rid,)).fetchall()]
    d["files"] = _files(c, rid)
    d["ready"] = _ready(c, rid, d["amount_entered"])
    d["log"] = [dict(x) for x in c.execute(
        "SELECT action,summary,user_name,created_at FROM pr_log WHERE request_id=? ORDER BY id DESC LIMIT 50",
        (rid,)).fetchall()]
    return d


def _may_edit(d, caps):
    return d["status"] not in LOCKED or "approve" in caps


# ---------------------------------------------------------------- /me
@router.get("/me")
async def me(req: Request):
    u = user_from(req)
    role = role_of(u)
    with closing(db()) as c:
        cos = companies(c)
        deps = departments(c)
        people = requesters(c)
        dep = ""
        try:
            r = c.execute("SELECT COALESCE(department,'') d FROM users WHERE id=?", (u["id"],)).fetchone()
            dep = (r["d"] if r else "") or ""
        except Exception:
            pass
    plant = u.get("factory_code", "")
    return {"id": u["id"], "name": u.get("name", ""), "username": u.get("username", ""),
            "cmms_role": u.get("role", ""), "pr_role": role, "caps": sorted(caps_of(role)),
            "factory_id": u.get("active_factory", u.get("factory_id")), "factory_code": plant,
            "factory_name": u.get("factory_name", ""),
            "company": COMPANY_OF_PLANT.get(plant, plant), "department": dep.upper(),
            "companies": cos, "departments": deps,
            "statuses": STATUSES, "purposes": PURPOSES, "pr_statuses": PR_STATUSES,
            "po_statuses": PO_STATUSES, "payment_methods": PAYMENT_METHODS, "requesters": people, "roles": ROLE_LABELS, "vat_rate": str(VAT_RATE),
            "max_file_mb": MAX_FILE // (1024 * 1024)}


# ---------------------------------------------------------------- list
def _where(q):
    w, p = [], []
    s = (q.get("q") or "").strip()
    if s:
        like = f"%{s}%"
        w.append("(doc_code LIKE ? OR reason LIKE ? OR requester_name LIKE ? OR supplier_name LIKE ? "
                 "OR pr_number LIKE ? OR po_number LIKE ? OR id IN (SELECT request_id FROM pr_items WHERE description LIKE ?))")
        p += [like] * 7
    for key, col in (("company", "company"), ("department", "department"), ("status", "status"),
                     ("purpose", "purpose"), ("pr_status", "pr_status"), ("po_status", "po_status")):
        v = (q.get(key) or "").strip()
        if v == "__none__":
            w.append(f"COALESCE({col},'')=''")
        elif v:
            w.append(f"{col}=?")
            p.append(v)
    if q.get("date_from"):
        w.append("request_date>=?")
        p.append(q["date_from"])
    if q.get("date_to"):
        w.append("request_date<=?")
        p.append(q["date_to"])
    if q.get("mine") == "1" and q.get("_uid"):
        w.append("requester_id=?")
        p.append(q["_uid"])
    return (" WHERE " + " AND ".join(w)) if w else "", p


@router.get("/requests")
async def list_requests(req: Request):
    u, role, caps = ctx(req, "view")
    q = dict(req.query_params)
    q["_uid"] = u["id"]
    where, p = _where(q)
    try:
        size = min(max(int(q.get("size") or 20), 5), 200)
        page = max(int(q.get("page") or 1), 1)
    except ValueError:
        size, page = 20, 1
    col = SORTS.get(q.get("sort") or "", "request_date")
    direction = "ASC" if (q.get("dir") or "").lower() == "asc" else "DESC"
    with closing(db()) as c:
        total = c.execute(f"SELECT COUNT(*) n FROM pr_requests{where}", p).fetchone()["n"]
        rows = [dict(r) for r in c.execute(
            f"SELECT id,doc_code,company,department,requester_name,supplier_name,request_date,purpose,"
            f"purpose_other,book_no,sheet_no,"
            f"reason,amount_entered,grand_total,net_total,status,pr_number,pr_status,po_number,po_status,"
            f"updated_at FROM pr_requests{where} ORDER BY {col} {direction}, id DESC LIMIT ? OFFSET ?",
            p + [size, (page - 1) * size]).fetchall()]
        counts = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM pr_requests GROUP BY status").fetchall()}
        po_counts = {r["po_status"]: r["n"] for r in c.execute(
            "SELECT po_status, COUNT(*) n FROM pr_requests WHERE COALESCE(po_status,'')<>'' "
            "GROUP BY po_status").fetchall()}
        ids = [r["id"] for r in rows]
        have, items = {}, {}
        if ids:
            for f in c.execute(f"SELECT request_id, kind FROM pr_files WHERE request_id IN ({','.join('?' * len(ids))})",
                               ids).fetchall():
                have.setdefault(f["request_id"], []).append(f["kind"])
            for it in c.execute(f"SELECT request_id, description FROM pr_items WHERE request_id IN "
                                f"({','.join('?' * len(ids))}) ORDER BY request_id, position, id", ids).fetchall():
                items.setdefault(it["request_id"], []).append(it["description"] or "")
    for r in rows:
        r["files"] = have.get(r["id"], [])
        r["items"] = items.get(r["id"], [])
    return {"rows": rows, "total": total, "page": page, "size": size, "counts": counts, "po_counts": po_counts}


@router.get("/requests/{rid}")
async def get_request(req: Request, rid: int):
    u, role, caps = ctx(req, "view")
    with closing(db()) as c:
        d = _full(c, rid)
    d["can_edit"] = _may_edit(d, caps)
    return d


# ---------------------------------------------------------------- write
def _clean_head(c, b, u):
    company = (b.get("company") or "").strip()
    if company not in {x["code"] for x in companies(c)}:
        raise _bad("กรุณาเลือกบริษัท (choose a company)")
    department = (b.get("department") or "").strip().upper()
    if department not in {x["code"] for x in departments(c)}:
        raise _bad("กรุณาเลือกแผนก (choose a department)")
    rd = (b.get("request_date") or "").strip()
    try:
        date.fromisoformat(rd)
    except ValueError:
        raise _bad("กรุณาระบุวันที่ขอซื้อ (request date)")
    purpose = b.get("purpose") or ""
    if purpose not in PURPOSES:
        raise _bad("กรุณาเลือกวัตถุประสงค์ (purpose)")
    other = (b.get("purpose_other") or "").strip()[:200] if purpose in ("Other", "Project") else ""
    if purpose == "Other" and not other:
        raise _bad("กรุณาระบุวัตถุประสงค์อื่น ๆ (other purpose)")
    reason = (b.get("reason") or "").strip()
    if not reason:
        raise _bad("กรุณาระบุงานหรือเหตุผลในการขอซื้อ (reason)")
    if len(reason) > 2000:
        raise _bad("งาน/เหตุผล ต้องไม่เกิน 2,000 ตัวอักษร (reason max 2,000)")
    sid, sname = 0, ""
    if b.get("supplier_id"):
        try:
            s = c.execute("SELECT id, company_name FROM pr_suppliers WHERE id=?", (int(b["supplier_id"]),)).fetchone()
        except (TypeError, ValueError):
            s = None
        if not s:
            raise _bad("กรุณาเลือก Supplier จากรายการ (supplier)")
        sid, sname = s["id"], s["company_name"]
    payment_method = (b.get("payment_method") or "").strip()
    if payment_method and payment_method not in PAYMENT_METHODS:
        raise _bad("การตั้งเบิกไม่ถูกต้อง (bad payment method)")
    pay_by = (b.get("pay_by") or "").strip()
    if pay_by:
        try:
            date.fromisoformat(pay_by)
        except ValueError:
            raise _bad("วันที่ตั้งเบิกไม่ถูกต้อง (bad pay-by date)")
    book_no = (b.get("book_no") or "").strip()[:20]
    sheet_no = (b.get("sheet_no") or "").strip()[:20]
    rq_id, rq_name = u["id"], _who(u)
    if b.get("requester_id") not in (None, "", 0, "0"):
        try:
            r = c.execute("SELECT id, name, username FROM users WHERE id=? AND COALESCE(active,1)=1",
                          (int(b["requester_id"]),)).fetchone()
        except (TypeError, ValueError):
            r = None
        if not r:
            raise _bad("กรุณาเลือกผู้ขอซื้อจากรายการ (requester)")
        rq_id, rq_name = r["id"], r["name"] or r["username"]
    vat_included = 1 if b.get("vat_included", True) not in (False, 0, "0", "") else 0
    items = _items(b.get("items"))
    entered, sub, vat, grand = totals(items, vat_included, None if items else b.get("amount"))
    try:
        job_id = int(b.get("job_id") or 0)
    except (TypeError, ValueError):
        job_id = 0
    return {"company": company, "department": department, "request_date": rd, "purpose": purpose,
            "purpose_other": other, "reason": reason, "supplier_id": sid, "supplier_name": sname,
            "vat_included": vat_included, "amount_entered": 1 if entered else 0, "subtotal": sub,
            "vat": vat, "grand_total": grand, "net_total": grand, "job_id": job_id,
            "payment_method": payment_method, "pay_by": pay_by, "book_no": book_no, "sheet_no": sheet_no,
            "requester_id": rq_id, "requester_name": rq_name}, items


def _save_items(c, rid, items):
    c.execute("DELETE FROM pr_items WHERE request_id=?", (rid,))
    for i in items:
        c.execute("INSERT INTO pr_items(request_id,position,description,quantity,unit_price,line_total,note) "
                  "VALUES(?,?,?,?,?,?,?)", (rid, i["position"], i["description"], i["quantity"],
                                            i["unit_price"], i["line_total"], i["note"]))


@router.post("/requests")
async def create_request(req: Request):
    u, role, caps = ctx(req, "create")
    b = await req.json()
    with closing(db()) as c:
        head, items = _clean_head(c, b, u)
        t = now()
        rid = None
        for _ in range(5):                 # two people saving in the same second -> next number
            code = next_code(c, head["department"], head["company"], head["request_date"])
            try:
                rid = c.insert_id(
                    "INSERT INTO pr_requests(doc_code,company,department,requester_id,requester_name,supplier_id,"
                    "supplier_name,request_date,purpose,purpose_other,reason,amount_entered,subtotal,vat_included,"
                    "vat,grand_total,net_total,status,pr_status,job_id,payment_method,pay_by,book_no,sheet_no,created_by,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (code, head["company"], head["department"], head["requester_id"], head["requester_name"],
                     head["supplier_id"],
                     head["supplier_name"], head["request_date"], head["purpose"], head["purpose_other"],
                     head["reason"], head["amount_entered"], head["subtotal"], head["vat_included"], head["vat"],
                     head["grand_total"], head["net_total"], "pending_approval", "pending_approval",
                     head["job_id"], head["payment_method"], head["pay_by"], head["book_no"], head["sheet_no"],
                     u["id"], t, t))
                break
            except Exception as e:
                if "UNIQUE" not in str(e).upper() and "DUPLICATE" not in str(e).upper():
                    raise
                try:
                    c._c.rollback()        # PostgreSQL aborts the transaction on a failed insert
                except Exception:
                    pass
        if not rid:
            raise _bad("บันทึกไม่สำเร็จ ลองอีกครั้ง (could not save, try again)", 409)
        _save_items(c, rid, items)
        _log(c, rid, code, "CREATE", f"สร้างเอกสาร {code} · {head['company']} · {head['department']}", _who(u),
             {"net_total": head["net_total"], "items": len(items), "supplier": head["supplier_name"]})
        c.commit()
        d = _full(c, rid)
    return d


@router.put("/requests/{rid}")
async def update_request(req: Request, rid: int):
    u, role, caps = ctx(req, "create")
    b = await req.json()
    with closing(db()) as c:
        old = _get(c, rid)
        if not _may_edit(old, caps):
            raise _bad("เอกสารที่อนุมัติ/ยกเลิกแล้วถูกล็อก — เฉพาะผู้อนุมัติแก้ไขได้ (locked: approver only)", 403)
        head, items = _clean_head(c, b, u)
        if b.get("requester_id") in (None, "", 0, "0"):     # not sent: the requester stays as it was
            head["requester_id"], head["requester_name"] = old.get("requester_id") or 0, old.get("requester_name") or ""
        changed = [k for k in ("company", "department", "request_date", "purpose", "purpose_other", "reason",
                               "supplier_name", "vat_included", "net_total", "payment_method", "pay_by",
                               "book_no", "sheet_no", "requester_name")
                   if str(old.get(k) or "") != str(head[k] or "")]
        old_items = [(i["description"], i["quantity"], i["unit_price"], i["note"]) for i in c.execute(
            "SELECT description,quantity,unit_price,note FROM pr_items WHERE request_id=? ORDER BY position", (rid,))]
        if old_items != [(i["description"], i["quantity"], i["unit_price"], i["note"]) for i in items]:
            changed.append("items")
        c.execute("UPDATE pr_requests SET company=?,department=?,request_date=?,purpose=?,purpose_other=?,reason=?,"
                  "supplier_id=?,supplier_name=?,vat_included=?,amount_entered=?,subtotal=?,vat=?,grand_total=?,"
                  "net_total=?,job_id=?,payment_method=?,pay_by=?,book_no=?,sheet_no=?,requester_id=?,requester_name=?,"
                  "updated_at=? WHERE id=?",
                  (head["company"], head["department"], head["request_date"], head["purpose"], head["purpose_other"],
                   head["reason"], head["supplier_id"], head["supplier_name"], head["vat_included"],
                   head["amount_entered"], head["subtotal"], head["vat"], head["grand_total"], head["net_total"],
                   head["job_id"] or old.get("job_id") or 0, head["payment_method"], head["pay_by"],
                   head["book_no"], head["sheet_no"], head["requester_id"], head["requester_name"], now(), rid))
        _save_items(c, rid, items)
        _restatus(c, rid)
        if changed:
            _log(c, rid, old["doc_code"], "UPDATE", f"แก้ไข {old['doc_code']}: {', '.join(changed)}", _who(u),
                 {"changed": changed, "net_total": [old["net_total"], head["net_total"]]})
        c.commit()
        d = _full(c, rid)
    d["can_edit"] = _may_edit(d, caps)
    return d


@router.post("/requests/{rid}/status")
async def set_status(req: Request, rid: int):
    """Approver: approve / cancel / reopen (back to pending_approval)."""
    u, role, caps = ctx(req, "approve")
    b = await req.json()
    want = b.get("status") or ""
    note = (b.get("note") or "").strip()[:500]
    with closing(db()) as c:
        d = _get(c, rid)
        if want == "approved":
            if not _ready(c, rid, d["amount_entered"]):
                raise _bad("ต้องมียอดรวมและแนบใบเสนอราคาก่อนอนุมัติ (amount + quotation needed before approval)")
        elif want == "cancelled":
            if not note:
                raise _bad("กรุณาระบุเหตุผลที่ยกเลิก (reason for cancelling)")
        elif want != "reopen":
            raise _bad("สถานะไม่ถูกต้อง (bad status)")
        c.execute("UPDATE pr_requests SET status=?, status_note=?, status_by=?, status_at=?, updated_at=? WHERE id=?",
                  ("pending_approval" if want == "reopen" else want, note, _who(u), now(), now(), rid))
        st = "pending_approval" if want == "reopen" else want
        _log(c, rid, d["doc_code"], "STATUS",
             f"{d['doc_code']}: {STATUSES[d['status']][0]} → {STATUSES[st][0]}" + (f" ({note})" if note else ""),
             _who(u), {"from": d["status"], "to": st, "note": note})
        c.commit()
        out = _full(c, rid)
    out["can_edit"] = _may_edit(out, caps)
    return out


def clean_tracking(kind, b, company_prefix, old):
    """PR: PRYYMM-XXXX (date filled in on save); a number = approved, none = pending_approval,
    or cancelled.
    PO: <plant>-POXXXX-XXXX."""
    number = (b.get("number") or "").strip().upper().replace(" ", "")
    status = (b.get("status") or "").strip()
    when = (b.get("date") or "").strip()
    reason = (b.get("cancel_reason") or "").strip()[:500]
    if kind == "pr":
        if number.startswith("PR"):
            number = number[2:]
        if number and not PR_SFX.fullmatch(number):
            raise _bad("เลข PR ต้องเป็นตัวเลข PR YYMM-XXXX เช่น PR2611-0042 (PR number: PRYYMM-XXXX, digits)")
        number = f"PR{number}" if number else ""
        if status and status not in PR_STATUSES:
            raise _bad("สถานะเลข PR ไม่ถูกต้อง (PR status)")
        if status == "approved" and not number:
            raise _bad("กรุณาใส่เลข PR ก่อนเลือกอนุมัติแล้ว (PR number needed to approve)")
        if status == "pending_approval" and number:
            raise _bad("สถานะรออนุมัติต้องไม่มีเลข PR (remove the number to set pending)")
        status = "cancelled" if status == "cancelled" else ("approved" if number else "pending_approval")
        # The PR date is not typed: it is the day the number (or the cancel) was saved, and
        # stays as it was while the number / cancel is unchanged.
        kept = old.get("pr_date") or ""
        if number:
            same = number == (old.get("pr_number") or "") and old.get("pr_status") == "approved"
            when = kept if same and kept else date.today().isoformat()
        elif status == "cancelled":
            when = kept if old.get("pr_status") == "cancelled" and kept else date.today().isoformat()
        else:
            when = ""
    else:
        for pfx in sorted({company_prefix, "BFL", "FP", "PC"} - {""}, key=len, reverse=True):
            if number.startswith(f"{pfx}-PO"):
                number = number[len(pfx) + 3:]
                break
        if number.startswith("PO"):
            number = number[2:]
        if number and not PO_SFX.fullmatch(number):
            raise _bad("เลข PO ต้องเป็น XX-POXXXX-XXXX (PO number)")
        number = f"{company_prefix}-PO{number}" if number and company_prefix else (f"PO{number}" if number else "")
        if status and status not in PO_STATUSES:
            raise _bad("สถานะเลข PO ไม่ถูกต้อง (PO status)")
    if when:
        try:
            date.fromisoformat(when)
        except ValueError:
            raise _bad(f"วันที่ของเลข {kind.upper()} ไม่ถูกต้อง (date)")
    if status == "cancelled" and not reason:
        raise _bad(f"กรุณาระบุเหตุผลที่ยกเลิกเลข {kind.upper()} (cancel reason)")
    return number, status, when, reason if status == "cancelled" else ""


@router.post("/requests/{rid}/tracking/{kind}")
async def set_tracking(req: Request, rid: int, kind: str):
    if kind not in ("pr", "po"):
        raise _bad("bad kind", 404)
    u, role, caps = ctx(req, "manage_pr" if kind == "pr" else "manage_po")
    b = await req.json()
    with closing(db()) as c:
        d = _get(c, rid)
        number, status, when, reason = clean_tracking(kind, b, _po_prefix(c, d["company"]), d)
        dup = c.execute(f"SELECT doc_code FROM pr_requests WHERE {kind}_number=? AND id<>? AND {kind}_number<>''",
                        (number, rid)).fetchone() if number else None
        if dup:
            raise _bad(f"เลข {number} ใช้แล้วในเอกสาร {dup['doc_code']} (already used)")
        doc_status = d["status"]
        if kind == "pr" and status != d["pr_status"]:
            # the paper flow: the PR number IS the approval
            if status == "approved":
                doc_status = "approved"
            elif status == "cancelled":
                doc_status = "cancelled"
            else:
                doc_status = "pending_approval"
        c.execute(f"UPDATE pr_requests SET {kind}_number=?, {kind}_status=?, {kind}_date=?, {kind}_cancel_reason=?, "
                  "status=?, updated_at=? WHERE id=?", (number, status, when, reason, doc_status, now(), rid))
        _log(c, rid, d["doc_code"], "TRACKING",
             f"{d['doc_code']}: {kind.upper()} {number or '-'} · {status or '-'}" + (f" ({reason})" if reason else ""),
             _who(u), {"kind": kind, "number": number, "status": status, "date": when})
        c.commit()
        out = _full(c, rid)
    out["can_edit"] = _may_edit(out, caps)
    return out


@router.delete("/requests/{rid}")
async def delete_request(req: Request, rid: int):
    u, role, caps = ctx(req, "delete")
    with closing(db()) as c:
        d = _get(c, rid)
        names = [r["stored_name"] for r in c.execute("SELECT stored_name FROM pr_files WHERE request_id=?", (rid,))]
        c.execute("DELETE FROM pr_items WHERE request_id=?", (rid,))
        c.execute("DELETE FROM pr_files WHERE request_id=?", (rid,))
        c.execute("DELETE FROM pr_requests WHERE id=?", (rid,))
        _log(c, rid, d["doc_code"], "DELETE", f"ลบเอกสาร {d['doc_code']} (deleted)", _who(u),
             {"net_total": d["net_total"], "reason": d["reason"][:200]})
        c.commit()
    for n in names:
        _remove(n)
    return {"ok": True}


# ---------------------------------------------------------------- files
def _decode(b):
    name = os.path.basename(str(b.get("name") or "").replace("\\", "/"))[:200]
    ext = os.path.splitext(name)[1].lower()
    if ext not in FILE_TYPES:
        raise _bad("รองรับเฉพาะ PDF, JPG, PNG (PDF / JPG / PNG only)")
    data = b.get("data") or ""
    if "," in data[:100] and data.startswith("data:"):
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data, validate=False)
    except Exception:
        raise _bad("อ่านไฟล์ไม่ได้ (cannot read file)")
    if not raw:
        raise _bad("ไฟล์ว่าง (empty file)")
    if len(raw) > MAX_FILE:
        raise _bad(f"ไฟล์ต้องไม่เกิน {MAX_FILE // (1024 * 1024)} MB")
    if not raw.startswith(FILE_TYPES[ext][1]):
        raise _bad("เนื้อไฟล์ไม่ตรงกับชนิดไฟล์ (file content does not match its type)")
    return name, ext, raw


def _remove(stored):
    if not stored:
        return
    p = os.path.join(FILES, os.path.basename(stored))
    try:
        os.remove(p)
    except OSError:
        pass


def _store(raw, ext):
    os.makedirs(FILES, exist_ok=True)
    stored = secrets.token_hex(24) + ext
    with open(os.path.join(FILES, stored), "wb") as fh:
        fh.write(raw)
    return stored


def _file_need(kind):
    return {"quotation": "create", "pr": "manage_pr", "po": "manage_po"}[kind]


@router.post("/requests/{rid}/files/{kind}")
async def upload_file(req: Request, rid: int, kind: str):
    if kind not in FILE_KINDS:
        raise _bad("bad kind", 404)
    u, role, caps = ctx(req, _file_need(kind))
    b = await req.json()
    name, ext, raw = _decode(b)
    with closing(db()) as c:
        d = _get(c, rid)
        if kind == "quotation" and not _may_edit(d, caps):
            raise _bad("เอกสารถูกล็อก (locked)", 403)
        stored = _store(raw, ext)
        old = c.execute("SELECT stored_name FROM pr_files WHERE request_id=? AND kind=?", (rid, kind)).fetchone()
        c.execute("DELETE FROM pr_files WHERE request_id=? AND kind=?", (rid, kind))
        c.execute("INSERT INTO pr_files(request_id,kind,original_name,stored_name,size,uploaded_by,uploaded_at) "
                  "VALUES(?,?,?,?,?,?,?)", (rid, kind, name, stored, len(raw), _who(u), now()))
        c.execute("UPDATE pr_requests SET updated_at=? WHERE id=?", (now(), rid))
        _restatus(c, rid)
        _log(c, rid, d["doc_code"], "FILE", f"{d['doc_code']}: แนบไฟล์ {kind.upper()} {name}", _who(u),
             {"kind": kind, "name": name, "replaced": bool(old)})
        c.commit()
        out = _full(c, rid)
    if old:
        _remove(old["stored_name"])
    out["can_edit"] = _may_edit(out, caps)
    return out


@router.delete("/requests/{rid}/files/{kind}")
async def delete_file(req: Request, rid: int, kind: str):
    if kind not in FILE_KINDS:
        raise _bad("bad kind", 404)
    u, role, caps = ctx(req, _file_need(kind))
    with closing(db()) as c:
        d = _get(c, rid)
        if kind == "quotation" and not _may_edit(d, caps):
            raise _bad("เอกสารถูกล็อก (locked)", 403)
        old = c.execute("SELECT stored_name, original_name FROM pr_files WHERE request_id=? AND kind=?",
                        (rid, kind)).fetchone()
        if not old:
            raise _bad("ไม่มีไฟล์ (no file)", 404)
        c.execute("DELETE FROM pr_files WHERE request_id=? AND kind=?", (rid, kind))
        _restatus(c, rid)
        _log(c, rid, d["doc_code"], "FILE", f"{d['doc_code']}: ลบไฟล์ {kind.upper()} {old['original_name']}", _who(u),
             {"kind": kind, "removed": old["original_name"]})
        c.commit()
        out = _full(c, rid)
    _remove(old["stored_name"])
    out["can_edit"] = _may_edit(out, caps)
    return out


@router.get("/requests/{rid}/files/{kind}")
async def get_file(req: Request, rid: int, kind: str):
    ctx(req, "view")
    with closing(db()) as c:
        r = c.execute("SELECT stored_name, original_name FROM pr_files WHERE request_id=? AND kind=?",
                      (rid, kind)).fetchone()
    if not r:
        raise _bad("ไม่มีไฟล์ (no file)", 404)
    p = os.path.join(FILES, os.path.basename(r["stored_name"]))
    if not os.path.exists(p):
        raise _bad("ไฟล์หายจากเครื่อง (file missing on the server)", 404)
    ext = os.path.splitext(p)[1].lower()
    return FileResponse(p, media_type=FILE_TYPES.get(ext, ("application/octet-stream",))[0],
                        filename=r["original_name"], content_disposition_type="inline")


# ---------------------------------------------------------------- quotation scan
@router.post("/scan")
async def scan(req: Request):
    """Read a quotation (PDF / image) and suggest the form values. Nothing is saved."""
    ctx(req, "create")
    b = await req.json()
    name, ext, raw = _decode(b)
    from . import pr_scanner
    try:
        text, method, warnings = await asyncio.to_thread(
            pr_scanner.extract_text, raw, ext, os.environ.get("TESSERACT_CMD", ""))
    except pr_scanner.ScannerUnavailable as e:
        raise _bad(str(e), 503)
    except pr_scanner.ScanFailed as e:
        raise _bad(str(e), 422)
    with closing(db()) as c:
        sups = [(r["id"], r["company_name"]) for r in c.execute(
            "SELECT id, company_name FROM pr_suppliers WHERE COALESCE(active,1)=1")]
    res = pr_scanner.parse_quotation(text, sups, MAX_ITEMS)
    res["warnings"] = warnings + res["warnings"]
    return {"ok": True, "method": method, **res}


# ---------------------------------------------------------------- suppliers
SUP_FIELDS = (("company_name", 200), ("address", 500), ("phone", 100), ("email", 200), ("line_id", 100),
              ("note", 500))


def _sup(b):
    v = {k: str(b.get(k) or "").strip()[:n] for k, n in SUP_FIELDS}
    if not v["company_name"]:
        raise _bad("กรุณาระบุชื่อบริษัท (company name)")
    if v["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v["email"]):
        raise _bad("Email ไม่ถูกต้อง (email)")
    return v


@router.get("/suppliers")
async def suppliers(req: Request):
    ctx(req, "suppliers_view")
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM pr_requests r WHERE r.supplier_id=s.id) used "
            "FROM pr_suppliers s WHERE COALESCE(active,1)=1 ORDER BY company_name").fetchall()]
    return {"rows": rows}


@router.post("/suppliers")
async def supplier_new(req: Request):
    u, role, caps = ctx(req, "suppliers_view")      # everyone may add one from the form (as before)
    v = _sup(await req.json())
    with closing(db()) as c:
        if c.execute("SELECT 1 FROM pr_suppliers WHERE lower(company_name)=lower(?) AND COALESCE(active,1)=1",
                     (v["company_name"],)).fetchone():
            raise _bad("มี Supplier ชื่อนี้แล้ว (already exists)")
        t = now()
        sid = c.insert_id("INSERT INTO pr_suppliers(company_name,address,phone,email,line_id,note,active,created_at,"
                          "updated_at) VALUES(?,?,?,?,?,?,1,?,?)", (*[v[k] for k, _ in SUP_FIELDS], t, t))
        _log(c, 0, "", "SUPPLIER", f"เพิ่ม Supplier {v['company_name']}", _who(u))
        c.commit()
    return {"id": sid, **v}


@router.put("/suppliers/{sid}")
async def supplier_edit(req: Request, sid: int):
    u, role, caps = ctx(req, "manage_suppliers")
    v = _sup(await req.json())
    with closing(db()) as c:
        c.execute("UPDATE pr_suppliers SET company_name=?,address=?,phone=?,email=?,line_id=?,note=?,updated_at=? "
                  "WHERE id=?", (*[v[k] for k, _ in SUP_FIELDS], now(), sid))
        c.execute("UPDATE pr_requests SET supplier_name=? WHERE supplier_id=? AND status NOT IN ('approved','cancelled')",
                  (v["company_name"], sid))
        _log(c, 0, "", "SUPPLIER", f"แก้ไข Supplier {v['company_name']}", _who(u))
        c.commit()
    return {"id": sid, **v}


@router.delete("/suppliers/{sid}")
async def supplier_delete(req: Request, sid: int):
    """Hidden, not erased — old requests keep the name they were saved with."""
    u, role, caps = ctx(req, "manage_suppliers")
    with closing(db()) as c:
        r = c.execute("SELECT company_name FROM pr_suppliers WHERE id=?", (sid,)).fetchone()
        if not r:
            raise _bad("not found", 404)
        c.execute("UPDATE pr_suppliers SET active=0, updated_at=? WHERE id=?", (now(), sid))
        _log(c, 0, "", "SUPPLIER", f"ลบ Supplier {r['company_name']}", _who(u))
        c.commit()
    return {"ok": True}


# ---------------------------------------------------------------- dashboard
@router.get("/summary")
async def summary(req: Request):
    ctx(req, "view")
    try:
        year = int(req.query_params.get("year") or date.today().year)
    except ValueError:
        year = date.today().year
    company = req.query_params.get("company") or ""
    with closing(db()) as c:
        years = sorted({int(r["y"]) for r in c.execute(
            "SELECT DISTINCT substr(request_date,1,4) y FROM pr_requests WHERE request_date<>''")
            if (r["y"] or "").isdigit()} | {date.today().year}, reverse=True)
        p = [f"{year}-%"]
        w = "request_date LIKE ?"
        if company:
            w += " AND company=?"
            p.append(company)
        rows = c.execute(f"SELECT company,department,purpose,status,pr_status,po_number,net_total,request_date,"
                         f"amount_entered FROM pr_requests WHERE {w}", p).fetchall()
    month = [Decimal("0")] * 12
    by_co, by_dep, by_purpose, by_status = {}, {}, {}, {}
    total, n, pending_po = Decimal("0"), 0, 0
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        if r["status"] == "cancelled":
            continue
        n += 1
        by_purpose[r["purpose"]] = by_purpose.get(r["purpose"], 0) + 1
        v = Decimal(r["net_total"] or "0")
        total += v
        try:
            month[int(r["request_date"][5:7]) - 1] += v
        except (ValueError, IndexError):
            pass
        by_co[r["company"]] = by_co.get(r["company"], Decimal("0")) + v
        by_dep[r["department"]] = by_dep.get(r["department"], Decimal("0")) + v
        if r["status"] == "approved" and not r["po_number"]:
            pending_po += 1
    m = lambda d: {k: _money(v) for k, v in sorted(d.items(), key=lambda kv: -kv[1])}
    return {"year": year, "years": years, "count": n, "total": _money(total), "month": [_money(v) for v in month],
            "by_company": m(by_co), "by_department": m(by_dep), "by_purpose": by_purpose,
            "by_status": by_status, "approved_without_po": pending_po}


# ---------------------------------------------------------------- activity log
@router.get("/log")
async def log(req: Request):
    ctx(req, "view_log")
    q = req.query_params
    w, p = [], []
    if q.get("q"):
        w.append("(doc_code LIKE ? OR summary LIKE ? OR user_name LIKE ?)")
        p += [f"%{q['q']}%"] * 3
    if q.get("action"):
        w.append("action=?")
        p.append(q["action"])
    where = (" WHERE " + " AND ".join(w)) if w else ""
    try:
        page = max(int(q.get("page") or 1), 1)
    except ValueError:
        page = 1
    with closing(db()) as c:
        total = c.execute(f"SELECT COUNT(*) n FROM pr_log{where}", p).fetchone()["n"]
        rows = [dict(r) for r in c.execute(
            f"SELECT id,request_id,doc_code,action,summary,user_name,created_at FROM pr_log{where} "
            f"ORDER BY created_at DESC, id DESC LIMIT 50 OFFSET ?", p + [(page - 1) * 50]).fetchall()]
    return {"rows": rows, "total": total, "page": page, "size": 50}


# ---------------------------------------------------------------- export
@router.get("/export.csv")
async def export_csv(req: Request):
    u, role, caps = ctx(req, "view")
    q = dict(req.query_params)
    q["_uid"] = u["id"]
    where, p = _where(q)
    with closing(db()) as c:
        rows = c.execute(f"SELECT * FROM pr_requests{where} ORDER BY request_date DESC, id DESC", p).fetchall()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["รหัสเอกสาร", "วันที่", "บริษัท", "แผนก", "ผู้ขอซื้อ", "Supplier", "วัตถุประสงค์", "งาน/เหตุผล",
                "ก่อน VAT", "VAT", "ยอดสุทธิ", "สถานะ", "เลข PR", "สถานะ PR", "วันที่ PR", "เลข PO", "สถานะ PO",
                "วันที่ PO"])
    for r in rows:
        w.writerow([r["doc_code"], r["request_date"], r["company"], r["department"], r["requester_name"],
                    r["supplier_name"], PURPOSES.get(r["purpose"], (r["purpose"],))[0]
                    + (f" ({r['purpose_other']})" if r["purpose_other"] else ""), r["reason"],
                    r["subtotal"], r["vat"], r["net_total"], STATUSES.get(r["status"], (r["status"],))[0],
                    r["pr_number"], PR_STATUSES.get(r["pr_status"], ("",))[0] if r["pr_status"] else "",
                    r["pr_date"], r["po_number"], PO_STATUSES.get(r["po_status"], ("",))[0] if r["po_status"] else "",
                    r["po_date"]])
    name = f"PR-{date.today().isoformat()}.csv"
    return Response("﻿" + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- users (who has PR access)
def _can_users(u, caps):
    return u.get("role") == "admin" or "users" in caps


@router.get("/users")
async def users(req: Request):
    u, role, caps = ctx(req, "users")
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id,username,name,role,COALESCE(department,'') department,COALESCE(pr_role,'') pr_role,"
            "factory_id FROM users WHERE active=1 AND COALESCE(can_login,1)=1 ORDER BY (COALESCE(pr_role,'')=''), name")]
    for r in rows:
        if r["role"] == "admin":
            r["pr_role"] = "super_admin"
    return {"users": rows, "roles": ROLE_LABELS}


@router.get("/users/{uid}")
async def user_get(req: Request, uid: int):
    """PR role of one account — used by CMMS › Manage › Users."""
    u = user_from(req)
    if not _can_users(u, caps_of(role_of(u))):
        raise _bad("no permission", 403)
    with closing(db()) as c:
        r = c.execute("SELECT role, COALESCE(pr_role,'') pr_role FROM users WHERE id=?", (uid,)).fetchone()
    if not r:
        raise _bad("not found", 404)
    return {"pr_role": "super_admin" if r["role"] == "admin" else r["pr_role"], "roles": ROLE_LABELS}


@router.post("/users/{uid}")
async def user_set(req: Request, uid: int):
    u = user_from(req)
    if not _can_users(u, caps_of(role_of(u))):
        raise _bad("no permission", 403)
    b = await req.json()
    pr_role = (b.get("pr_role") or "").strip()
    if pr_role and pr_role not in PR_ROLES:
        raise _bad("bad role")
    if pr_role == "super_admin" and u.get("role") != "admin" and role_of(u) != "super_admin":
        raise _bad("no permission", 403)
    with closing(db()) as c:
        r = c.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
        if not r:
            raise _bad("not found", 404)
        c.execute("UPDATE users SET pr_role=? WHERE id=?", (pr_role, uid))
        _log(c, 0, "", "USER", f"สิทธิ์ PR ของ {r['name']}: {pr_role or '-'}", _who(u))
        c.commit()
    return {"ok": True, "pr_role": pr_role}


# ---------------------------------------------------------------- old app import
def import_legacy(src_db, src_uploads, log=print):
    """Copy the stand-alone PR app (pr_tracker.sqlite + instance/uploads) in. Re-runnable:
    suppliers, requests and log rows already brought in (by their old id) are skipped."""
    import shutil
    import sqlite3
    old = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    old.row_factory = sqlite3.Row
    cols = lambda t: {r[1] for r in old.execute(f"PRAGMA table_info({t})")}
    made = {"suppliers": 0, "requests": 0, "items": 0, "files": 0, "log": 0, "missing_files": 0}
    with closing(db()) as c:
        ensure(c)
        names = {}
        for r in c.execute("SELECT id, name FROM users").fetchall():
            names.setdefault((r["name"] or "").strip().lower(), r["id"])
        sup_map = {r["legacy_id"]: r["id"] for r in c.execute(
            "SELECT id, legacy_id FROM pr_suppliers WHERE legacy_id>0").fetchall()}
        for s in old.execute("SELECT * FROM suppliers ORDER BY id"):
            if s["id"] in sup_map:
                continue
            sup_map[s["id"]] = c.insert_id(
                "INSERT INTO pr_suppliers(company_name,address,phone,email,line_id,note,active,legacy_id,created_at,"
                "updated_at) VALUES(?,?,?,?,?,?,1,?,?,?)",
                (s["company_name"], s["address"], s["phone"], s["email"], s["line_id"], s["note"], s["id"],
                 (s["created_at"] or "")[:19].replace("T", " "), (s["updated_at"] or "")[:19].replace("T", " ")))
            made["suppliers"] += 1
        have = {r["legacy_id"]: r["id"] for r in c.execute(
            "SELECT id, legacy_id FROM pr_requests WHERE legacy_id>0").fetchall()}
        rc = cols("purchase_requests")
        g = lambda r, k, d="": (r[k] if k in rc and r[k] is not None else d)
        for r in old.execute("SELECT * FROM purchase_requests ORDER BY id"):
            if r["id"] in have:
                continue
            if c.execute("SELECT 1 FROM pr_requests WHERE doc_code=?", (r["document_code"],)).fetchone():
                log(f"  skip {r['document_code']}: that code is already used here")
                continue
            entered = 1 if (r["subtotal"] or "") != "" else 0
            sub = r["subtotal"] if entered else "0.00"
            t1 = (g(r, "created_at") or "")[:19].replace("T", " ")
            t2 = (g(r, "updated_at") or "")[:19].replace("T", " ")
            rid = c.insert_id(
                "INSERT INTO pr_requests(doc_code,company,department,requester_id,requester_name,supplier_id,"
                "supplier_name,request_date,purpose,purpose_other,reason,amount_entered,subtotal,vat_included,vat,"
                "grand_total,withholding_amount,net_total,status,pr_number,pr_status,pr_date,pr_cancel_reason,"
                "po_number,po_status,po_date,po_cancel_reason,legacy_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["document_code"], r["company"], r["department"],
                 names.get((g(r, "requester_name") or "").strip().lower(), 0), g(r, "requester_name"),
                 sup_map.get(g(r, "supplier_id"), 0) or 0, g(r, "supplier_name"), r["request_date"], r["purpose"],
                 g(r, "purpose_other"), r["reason"], entered, sub, int(g(r, "vat_included", 1) or 0), r["vat"],
                 r["grand_total"], g(r, "withholding_amount", "0.00"), g(r, "net_total", r["grand_total"]) or r["grand_total"],
                 "pending_approval" if r["status"] == "waiting_quotation" else r["status"],
                 re.sub(r"^(?:PR)?(?=.)", "PR", (g(r, "pr_number") or "").strip().upper()),
                 g(r, "pr_number_status") or "pending_approval",
                 g(r, "pr_number_date"), g(r, "pr_cancel_reason"), g(r, "po_number"), g(r, "po_number_status"),
                 g(r, "po_number_date"), g(r, "po_cancel_reason"), r["id"], t1, t2))
            made["requests"] += 1
            for i in old.execute("SELECT * FROM request_items WHERE request_id=? ORDER BY position, id", (r["id"],)):
                c.execute("INSERT INTO pr_items(request_id,position,description,quantity,unit_price,line_total,note) "
                          "VALUES(?,?,?,?,?,?,?)", (rid, i["position"], i["description"], i["quantity"],
                                                    i["unit_price"], i["line_total"], i["note"] or ""))
                made["items"] += 1
            files = [("quotation", q) for q in old.execute(
                "SELECT original_name, stored_name, uploaded_at FROM request_quotations WHERE request_id=?", (r["id"],))]
            files += [(f["kind"], f) for f in old.execute(
                "SELECT kind, original_name, stored_name, uploaded_at FROM request_tracking_files WHERE request_id=?",
                (r["id"],))]
            for kind, f in files:
                srcp = os.path.join(src_uploads, f["stored_name"])
                if not os.path.exists(srcp):
                    made["missing_files"] += 1
                    continue
                os.makedirs(FILES, exist_ok=True)
                stored = secrets.token_hex(24) + os.path.splitext(f["stored_name"])[1].lower()
                shutil.copy2(srcp, os.path.join(FILES, stored))
                c.execute("INSERT INTO pr_files(request_id,kind,original_name,stored_name,size,uploaded_by,uploaded_at) "
                          "VALUES(?,?,?,?,?,?,?)", (rid, kind, f["original_name"], stored, os.path.getsize(srcp),
                                                    "(old PR app)", (f["uploaded_at"] or "")[:19].replace("T", " ")))
                made["files"] += 1
            have[r["id"]] = rid
        seen = {r["legacy_id"] for r in c.execute("SELECT legacy_id FROM pr_log WHERE legacy_id>0").fetchall()}
        for a in old.execute("SELECT * FROM activity_logs WHERE action<>'READ' ORDER BY id"):
            if a["id"] in seen:
                continue
            c.execute("INSERT INTO pr_log(request_id,doc_code,action,summary,details_json,user_name,legacy_id,created_at) "
                      "VALUES(?,?,?,?,?,?,?,?)",
                      (have.get(a["request_id"], 0), a["document_code"], a["action"], a["summary"], a["details_json"],
                       "(old PR app)", a["id"], (a["occurred_at"] or "")[:19].replace("T", " ")))
            made["log"] += 1
        c.commit()
    old.close()
    log("  " + " · ".join(f"{k} {v}" for k, v in made.items()))
    return made

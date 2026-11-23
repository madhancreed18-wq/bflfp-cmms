"""PR module (purchase requests) — end-to-end checks.

    python tests/test_pr.py

Starts the app on port 8768 with a THROWAWAY data folder (your real data/ is untouched),
then walks one request through the whole flow with the five PR roles. Exit code 0 = pass.
"""
import base64, json, os, subprocess, sys, tempfile, time, urllib.request, urllib.error

PORT = 8768
BASE = f"http://localhost:{PORT}"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


class Client:
    def __init__(self):
        self.cookie = None

    def req(self, method, path, body=None, raw=False):
        r = urllib.request.Request(BASE + path, method=method, headers={"Content-Type": "application/json"})
        if self.cookie:
            r.add_header("Cookie", self.cookie)
        data = json.dumps(body).encode() if body is not None else None
        try:
            with urllib.request.urlopen(r, data=data, timeout=30) as resp:
                ck = resp.headers.get("Set-Cookie")
                if ck:
                    self.cookie = ck.split(";")[0]
                payload = resp.read()
                return resp.status, payload if raw else (json.loads(payload) if payload else {})
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except Exception:
                return e.code, {}

    def login(self, u, pw="1234"):
        return self.req("POST", "/api/login", {"username": u, "password": pw})


def msg(r):
    d = r.get("detail") if isinstance(r, dict) else None
    return (d or {}).get("msg", "") if isinstance(d, dict) else str(d or r)


def main():
    tmp = tempfile.mkdtemp(prefix="bflfp_pr_")
    env = {**os.environ, "BFLFP_DATA": tmp, "PORT": str(PORT), "CMMS_PM_SCHED": "0", "CMMS_AUTO_DAILY": "0"}
    env.pop("DATABASE_URL", None)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "server.app:create_app", "--factory",
                             "--port", str(PORT)], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(BASE + "/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        run()
    finally:
        proc.terminate()
    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    sys.exit(1 if bad else 0)


def thai_text_checks():
    """Thai read from PDFs / OCR comes back in the standard spelling."""
    sys.path.insert(0, ROOT)
    from server.pr_scanner import fix_thai, garbage_ratio, parse_quotation
    for raw, want in (("บริษัท จ\u0e4d\u0e32กัด", "บริษัท จำกัด"), ("น\u0e4d\u0e49\u0e32ดื่ม", "น้ำดื่ม"),
                      ("เเผ่น", "แผ่น"), ("ก\u0e48\u0e35", "กี่"), ("ศักด\u0e4c\u0e34", "ศักดิ์"),
                      ("ป\uf70b\u0e32ย", "ป้าย"), ("ท\uf713\u0e35", "ที่")):
        check(f"Thai fixed: {want}", fix_thai(raw) == want, repr(fix_thai(raw)))
    check("Thai font glyphs no longer count as garbage", garbage_ratio(fix_thai("ป\uf70bาย ท\uf713ี")) == 0)
    r = parse_quotation("ใบเสนอราคา\nลำดับ รายการ จำนวน ราคา จำนวนเงิน\n"
                        "1 ป\uf70bายช\u0e37\u0e48อ   2   150.00   300.00\nรวมเป็นเงิน 300.00")
    r2 = parse_quotation("ลำดับ รายการ จำนวน ราคา จำนวนเงิน\n1 สายพาน กว้าง 30 ซม.   1   1,250.00   1,250.00")
    check("Thai abbreviation dot kept", r2["items"] and r2["items"][0]["description"] == "สายพาน กว้าง 30 ซม.",
          r2["items"][:1])
    check("scanned item keeps correct Thai", r["items"] and r["items"][0]["description"] == "ป้ายชื่อ",
          r["items"][:1])


def run():
    thai_text_checks()
    adm = Client()
    st, me = adm.login("admin1")
    check("admin login", st == 200)
    check("CMMS admin is PR super_admin", me.get("pr_role") == "super_admin", me.get("pr_role"))

    # people: technician = requester, planner = PR admin, manager = approver, tech2 = PO admin
    st, us = adm.req("GET", "/api/pr/users")
    ids = {u["username"]: u["id"] for u in us.get("users", [])}
    for name, role in (("tech1", "user"), ("planner1", "pr_admin"), ("manager1", "approver"), ("tech2", "po_admin")):
        st, _ = adm.req("POST", f"/api/pr/users/{ids[name]}", {"pr_role": role})
        check(f"admin gives {name} PR role {role}", st == 200)
    op = Client(); op.login("op1")
    st, _ = op.req("GET", "/api/pr/requests")
    check("no PR role -> 403", st == 403)
    st, r = op.req("GET", "/api/me")
    check("/api/me carries pr_role ('' for op1)", r.get("pr_role") == "", r.get("pr_role"))

    u = Client(); u.login("tech1")
    st, m = u.req("GET", "/api/pr/me")
    check("requester /me", st == 200 and m["pr_role"] == "user" and m["companies"], st)
    company = m["company"] or m["companies"][0]["code"]

    # supplier added from the form by a plain user
    st, s = u.req("POST", "/api/pr/suppliers", {"company_name": "SMEC Engineering", "phone": "02-000-0000"})
    check("user adds a supplier", st == 200 and s.get("id"), msg(s))
    sid = s.get("id")
    st, s2 = u.req("POST", "/api/pr/suppliers", {"company_name": "smec engineering"})
    check("duplicate supplier refused", st == 400)

    body = {"company": company, "department": "ENG", "request_date": "2026-11-20", "purpose": "Repair",
            "reason": "Mixer paddle spare", "supplier_id": sid, "vat_included": True,
            "items": [{"description": "Paddle", "quantity": "2", "unit_price": "1500.50"},
                      {"description": "Bolt set", "quantity": "1", "unit_price": "99.99", "note": "M12"},
                      {"description": "", "quantity": "", "unit_price": ""}]}
    st, d = u.req("POST", "/api/pr/requests", body)
    check("create request", st == 200, msg(d))
    rid = d.get("id")
    code = d.get("doc_code", "")
    check("document code DEPT-COMPANY-YYMM-001", code == f"ENG-{company}-2611-001", code)
    check("totals: 3100.99 + VAT 217.07 = 3318.06",
          (d.get("subtotal"), d.get("vat"), d.get("net_total")) == ("3100.99", "217.07", "3318.06"),
          (d.get("subtotal"), d.get("vat"), d.get("net_total")))
    check("empty item row skipped", len(d.get("items", [])) == 2)
    check("no quotation -> waiting_quotation", d.get("status") == "waiting_quotation", d.get("status"))
    st, d2 = u.req("POST", "/api/pr/requests", {**body, "items": []})
    check("second request same month -> 002, no amount allowed", st == 200 and d2["doc_code"].endswith("-002")
          and d2["amount_entered"] == 0, msg(d2))
    st, bad = u.req("POST", "/api/pr/requests", {**body, "items": [{"description": "x", "quantity": "1.234", "unit_price": "1"}]})
    check("3-decimal quantity refused", st == 400)
    st, bad = u.req("POST", "/api/pr/requests", {**body, "reason": ""})
    check("empty reason refused", st == 400)
    check("payment fields empty by default", (d.get("payment_method"), d.get("pay_by")) == ("", ""),
          (d.get("payment_method"), d.get("pay_by")))
    st, bad = u.req("POST", "/api/pr/requests", {**body, "payment_method": "bitcoin"})
    check("unknown payment method refused", st == 400)
    st, bad = u.req("POST", "/api/pr/requests", {**body, "pay_by": "31/12/2026"})
    check("bad pay-by date refused", st == 400)
    st, x = u.req("PUT", f"/api/pr/requests/{d2['id']}", {**body, "items": [], "payment_method": "petty_cash",
                                                         "pay_by": "2026-11-30"})
    check("payment method + pay-by saved", st == 200 and (x.get("payment_method"), x.get("pay_by"))
          == ("petty_cash", "2026-11-30"), msg(x) or (x.get("payment_method"), x.get("pay_by")))

    # paper form: เล่มที่/เลขที่, 4th company, requester picked from a list, project name
    smec = [c for c in m["companies"] if c["code"] == "SMEC"]
    check("/me lists SMEC with its Thai name", smec and "สยามแมค" in smec[0]["name_th"], m["companies"])
    check("/me lists requesters", any(r["id"] == ids["planner1"] for r in m.get("requesters", [])))
    check("requester defaults to the signed-in user", d.get("requester_id") == ids["tech1"], d.get("requester_id"))
    st, x = u.req("PUT", f"/api/pr/requests/{d2['id']}", {**body, "items": [], "book_no": "242", "sheet_no": "12073",
                                                         "requester_id": ids["planner1"], "purpose": "Project",
                                                         "purpose_other": "Line 3"})
    check("book/sheet no, requester and project saved", st == 200 and (
        x.get("book_no"), x.get("sheet_no"), x.get("requester_id"), x.get("purpose_other"))
        == ("242", "12073", ids["planner1"], "Line 3"), msg(x))
    st, x = u.req("PUT", f"/api/pr/requests/{d2['id']}", {**body, "items": [], "book_no": "242", "sheet_no": "12073"})
    check("requester kept when not sent", st == 200 and x.get("requester_id") == ids["planner1"], x.get("requester_id"))
    st, bad = u.req("PUT", f"/api/pr/requests/{d2['id']}", {**body, "items": [], "requester_id": 999999})
    check("unknown requester refused", st == 400)

    b64 = base64.b64encode(PDF).decode()
    st, bad = u.req("POST", f"/api/pr/requests/{rid}/files/quotation", {"name": "q.pdf", "data": base64.b64encode(b"hello").decode()})
    check("fake PDF refused (content check)", st == 400)
    st, d = u.req("POST", f"/api/pr/requests/{rid}/files/quotation", {"name": "Q-SMEC 69-11-24.pdf", "data": b64})
    check("attach quotation -> pending_approval", st == 200 and d["status"] == "pending_approval", d.get("status"))
    st, raw = u.req("GET", f"/api/pr/requests/{rid}/files/quotation", raw=True)
    check("download quotation", st == 200 and raw.startswith(b"%PDF"))
    st, _ = u.req("POST", f"/api/pr/requests/{rid}/status", {"status": "approved"})
    check("requester cannot approve (403)", st == 403)

    ap = Client(); ap.login("manager1")
    st, x = ap.req("POST", f"/api/pr/requests/{d2['id']}/status", {"status": "approved"})
    check("approve without amount+quotation refused", st == 400)
    st, x = ap.req("POST", f"/api/pr/requests/{rid}/status", {"status": "cancelled"})
    check("cancel without reason refused", st == 400)
    st, d = ap.req("POST", f"/api/pr/requests/{rid}/status", {"status": "approved"})
    check("approver approves", st == 200 and d["status"] == "approved")
    st, x = u.req("PUT", f"/api/pr/requests/{rid}", body)
    check("approved request locked for requester", st == 403)
    st, x = ap.req("PUT", f"/api/pr/requests/{rid}", {**body, "reason": "Mixer paddle spare (urgent)"})
    check("approver may still edit an approved request", st == 200 and x["status"] == "approved", msg(x))

    pa = Client(); pa.login("planner1")
    st, x = pa.req("POST", f"/api/pr/requests/{rid}/tracking/pr", {"number": "2611-AB12"})
    check("PR admin enters PR number (short form)", st == 200 and x["pr_number"] == "PR2611-AB12"
          and x["pr_status"] == "approved" and x["pr_date"], (x.get("pr_number"), x.get("pr_status")))
    st, x = pa.req("POST", f"/api/pr/requests/{rid}/tracking/pr", {"number": "PR2613-0001"})
    check("bad PR month refused", st == 400)
    st, x = pa.req("POST", f"/api/pr/requests/{d2['id']}/tracking/pr", {"number": "PR2611-AB12"})
    check("same PR number twice refused", st == 400)
    st, x = pa.req("POST", f"/api/pr/requests/{rid}/tracking/po", {"number": "1234-5678"})
    check("PR admin cannot set PO (403)", st == 403)

    po = Client(); po.login("tech2")
    prefix = next(c["po_prefix"] for c in m["companies"] if c["code"] == company)
    st, x = po.req("POST", f"/api/pr/requests/{rid}/tracking/po", {"number": "1234-5678", "status": "sent_to_supplier"})
    check("PO admin enters PO number with plant prefix", st == 200 and x["po_number"] == f"{prefix}-PO1234-5678",
          x.get("po_number"))
    st, x = po.req("POST", f"/api/pr/requests/{rid}/files/po", {"name": "po.pdf", "data": b64})
    check("PO admin attaches PO PDF", st == 200 and "po" in x["files"])
    st, x = po.req("PUT", f"/api/pr/suppliers/{sid}", {"company_name": "SMEC Engineering Co., Ltd."})
    check("PO admin edits supplier", st == 200)
    st, x = u.req("PUT", f"/api/pr/suppliers/{sid}", {"company_name": "nope"})
    check("requester cannot edit supplier (403)", st == 403)

    st, x = pa.req("POST", f"/api/pr/requests/{d2['id']}/tracking/pr", {"status": "cancelled", "cancel_reason": "duplicate"})
    check("cancelling PR number cancels the request", st == 200 and x["status"] == "cancelled", x.get("status"))

    st, ls = u.req("GET", "/api/pr/requests?q=bolt")
    check("search finds by item description", st == 200 and ls["total"] == 1, ls.get("total"))
    st, ls = u.req("GET", f"/api/pr/requests?status=approved&company={company}&sort=total&dir=asc")
    check("filter by status + company", st == 200 and ls["total"] == 1)
    check("list carries PO status counts", ls.get("po_counts") == {"sent_to_supplier": 1}, ls.get("po_counts"))
    st, ls = u.req("GET", "/api/pr/requests?po_status=sent_to_supplier")
    check("PO count matches PO status filter", st == 200 and ls["total"] == 1, ls.get("total"))
    st, sm = u.req("GET", "/api/pr/summary?year=2026")
    check("dashboard: cancelled not counted", st == 200 and sm["count"] == 1 and sm["total"] == "3318.06",
          (sm.get("count"), sm.get("total")))
    check("dashboard: monthly November", sm.get("month", [0] * 12)[10] == "3318.06")
    st, raw = u.req("GET", "/api/pr/export.csv", raw=True)
    check("CSV export", st == 200 and "ENG-" in raw.decode("utf-8-sig"))
    st, lg = u.req("GET", "/api/pr/log")
    check("activity log closed to requester", st == 403)
    st, lg = ap.req("GET", "/api/pr/log")
    check("approver reads activity log", st == 200 and lg["total"] >= 6, lg.get("total"))
    st, x = ap.req("DELETE", f"/api/pr/requests/{d2['id']}")
    check("approver cannot delete (super admin only)", st == 403)
    st, x = adm.req("DELETE", f"/api/pr/requests/{d2['id']}")
    check("super admin deletes", st == 200)
    st, x = adm.req("POST", f"/api/pr/users/{ids['tech1']}", {"pr_role": ""})
    st, _ = u.req("GET", "/api/pr/requests")
    check("role removed -> access gone at once", st == 403)


if __name__ == "__main__":
    main()

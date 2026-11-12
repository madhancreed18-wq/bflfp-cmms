import io, sys
P = sys.argv[1]
s = io.open(P, encoding="utf-8").read()
def rep(old, new, cnt=1):
    global s
    assert s.count(old) == cnt, ("not found / not unique", old[:90], s.count(old))
    s = s.replace(old, new)

# 1. history: time range + separate product / lot search
rep('''async def history(req: Request, kind: str = "recv", d1: str = "", d2: str = "", q: str = "",
                  status: str = "", limit: int = 500):
    u, fac, _ = ctx(req, "check")
    tbl, dcol = ("fg_receive", "receive_date") if kind == "recv" else ("fg_release", "release_date")''',
'''def _time_cond(tcol, t1, t2):
    """b506: HH:MM range on a time column; '20:00'-'08:00' wraps past midnight (night shift)."""
    ok = lambda t: bool(re.fullmatch(r"\\d{1,2}:\\d{2}", t or ""))
    t1, t2 = (t1 if ok(t1) else ""), (t2 if ok(t2) else "")
    t1 = t1.zfill(5) if t1 else ""
    t2 = t2.zfill(5) if t2 else ""
    if t1 and t2 and t1 > t2:
        return f"({tcol}>=? OR ({tcol}<=? AND {tcol}<>''))", [t1, t2]
    c, a = [], []
    if t1:
        c.append(f"{tcol}>=?")
        a.append(t1)
    if t2:
        c.append(f"({tcol}<=? AND {tcol}<>'')")
        a.append(t2)
    return (" AND ".join(c), a) if c else ("", [])


async def history(req: Request, kind: str = "recv", d1: str = "", d2: str = "", q: str = "",
                  status: str = "", limit: int = 500, t1: str = "", t2: str = "", product: str = "", lot: str = ""):
    u, fac, _ = ctx(req, "check")
    tbl, dcol = ("fg_receive", "receive_date") if kind == "recv" else ("fg_release", "release_date")
    tcol = ("substr(COALESCE(NULLIF(receive_time,''),substr(recv_at,12,5)),1,5)" if kind == "recv"
            else "substr(COALESCE(NULLIF(release_time,''),substr(issue_at,12,5)),1,5)")''')
rep('''    if q:
        conds.append("(lot_no LIKE ? OR product_id LIKE ? OR product_name LIKE ?)")
        args += ["%" + q + "%"] * 3
    limit = max(1, min(int(limit or 500), 5000))
    with closing(conn()) as c:
        prods = _prods(c, fac)
        rows = [_recv_row(r, prods) for r in c.execute(''',
'''    if q:
        conds.append("(lot_no LIKE ? OR product_id LIKE ? OR product_name LIKE ?)")
        args += ["%" + q + "%"] * 3
    if product.strip():
        conds.append("(product_id LIKE ? OR product_name LIKE ?)")
        args += ["%" + product.strip() + "%"] * 2
    if lot.strip():
        conds.append("lot_no LIKE ?")
        args.append("%" + lot.strip() + "%")
    tc, ta = _time_cond(tcol, t1, t2)
    if tc:
        conds.append(tc)
        args += ta
    limit = max(1, min(int(limit or 500), 5000))
    with closing(conn()) as c:
        prods = _prods(c, fac)
        rows = [_recv_row(r, prods) for r in c.execute(''')

# 2. dashboard: 1 day
rep('''    days = days if days in (7, 30, 90, 365) else 30''', '''    days = days if days in (1, 7, 30, 90, 365) else 30''')

# 3. generic table -> PDF (A4 landscape, Thai font)
rep('''@router.get("/stock")
async def stock(''',
'''@router.post("/pdf")
async def table_pdf(req: Request):
    """b506: any FG table as a real PDF file (A4 landscape, Sarabun) — same content as Print A4."""
    import asyncio
    from fastapi import Response
    from xml.sax.saxutils import escape as xe
    u, fac, _ = ctx(req, "view")
    b = await req.json()
    head = [str(h) for h in (b.get("head") or [])][:30]
    rows = [[("" if v is None else str(v)) for v in r][:30] for r in (b.get("rows") or [])][:6000]
    right = set(int(i) for i in (b.get("right") or []) if str(i).isdigit())
    title, sub, foot = str(b.get("title") or "FG report")[:200], str(b.get("sub") or "")[:300], str(b.get("foot") or "")[:300]
    fname = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(b.get("file") or "fg-report"))[:80] + ".pdf"
    who = _who(u)

    def build():
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, KeepTogether
        from . import reports
        reports._pdf_font()
        k = reports.PDF_SCALE or 1.0
        fs = 7.2 * k
        st = ParagraphStyle("c", fontName="APP", fontSize=fs, leading=fs * 1.15)
        sr = ParagraphStyle("r", parent=st, alignment=2)
        sh = ParagraphStyle("h", fontName="APPB", fontSize=fs, leading=fs * 1.15)
        buf = io.BytesIO()
        W, H = landscape(A4)
        doc = SimpleDocTemplate(buf, pagesize=(W, H), leftMargin=8 * mm, rightMargin=8 * mm, topMargin=8 * mm, bottomMargin=10 * mm,
                                title=title, author=who)
        P = lambda t, s_: Paragraph(xe(t).replace("\\n", "<br/>"), s_)
        data = [[P(h, sh) for h in head]] + [[P(v, sr if i in right else st) for i, v in enumerate(r)] for r in rows]
        # column widths from content length, capped, scaled to the page width
        n = max(1, len(head))
        lens = [max([len(head[i])] + [len(r[i]) for r in rows[:400] if i < len(r)]) for i in range(n)]
        lens = [min(max(l, 4), 45) for l in lens]
        tot = sum(lens) or 1
        cw = [(W - 16 * mm) * l / tot for l in lens]
        t = Table(data, colWidths=cw, repeatRows=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.black), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E5E7EB")),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 1.2),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 1.2), ("LEFTPADDING", (0, 0), (-1, -1), 2.5), ("RIGHTPADDING", (0, 0), (-1, -1), 2.5)]))
        ts = ParagraphStyle("t", fontName="APPB", fontSize=11 * k, leading=13 * k)
        ss = ParagraphStyle("s", fontName="APP", fontSize=7.5 * k, leading=9 * k)
        story = [P(title, ts), P(f"{sub} · printed by {who} {now()[:16]}", ss), Spacer(1, 3 * mm), t]
        end = []
        if foot:
            end.append(Spacer(1, 2 * mm))
            end.append(P(foot, ParagraphStyle("f", fontName="APPB", fontSize=8 * k, leading=10 * k)))
        sg = Table([["..............................\\nChecker", "..............................\\nApproved by / ผู้ตรวจสอบ"]], colWidths=[(W - 16 * mm) / 2] * 2)
        sg.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), "APP"), ("FONTSIZE", (0, 0), (-1, -1), 8 * k), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                                ("TOPPADDING", (0, 0), (-1, -1), 8 * mm)]))
        end.append(sg)
        story.append(KeepTogether(end))

        def pn(cv, d):
            cv.saveState()
            cv.setFont("APP", 7 * k)
            cv.drawRightString(W - 8 * mm, 5 * mm, f"{d.page}")
            cv.restoreState()
        doc.build(story, onFirstPage=pn, onLaterPages=pn)
        return buf.getvalue()
    pdf = await asyncio.to_thread(build)
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/stock")
async def stock(''')

io.open(P, "w", encoding="utf-8", newline="\n").write(s)
print("ok")

# ══════════════════════════════════════════════════════════════════════════════════
#  F-SP-ENG02-03 — ใบแจ้งซ่อม
#  The plant's controlled repair-notification form, one sheet per CM/BD job. Built to
#  match the paper template exactly: the three sections, the blue bars, the checkbox
#  pairs, the signature boxes and the before/after photo panel at the foot. Wording
#  is the document's own, not ours — this sheet gets filed and audited.
#
#  Filing follows the daily sheet's rule: every route that produces the form also
#  archives it (data/Report/YYYY/MM/DD/, the day the job finished) and puts one row
#  on the Reports shelf — one job, one file, one row, updated rather than duplicated.
# ══════════════════════════════════════════════════════════════════════════════════
import os
from datetime import datetime, date
from contextlib import closing

from .config import DATA, COMPANY_TH, FORM_CODE
from .db import db, job_row, user_names, factory_company

import logging
# the app's own logger — the file handler is attached by logs.setup(), and
# everything written here shows in Manage → Logs. print() does not: it goes to
# a console nobody is watching, which is where these messages used to die.
_log = logging.getLogger("cmms")

FORM03_TITLE = "ใบแจ้งซ่อม"

# job types that belong on this form — preventive work has its own checklist sheets
FORM03_TYPES = ("CM", "BD", "IMP", "PRJ")

ROLE_TH = {"operator": "พนักงานฝ่ายผลิต", "technician": "ช่างซ่อมบำรุง",
           "planner": "เจ้าหน้าที่วางแผนซ่อมบำรุง", "manager": "ผู้จัดการ",
           "engcenter": "ศูนย์วิศวกรรมกลาง",
           "admin": "ผู้ดูแลระบบ"}


def form_filename(jobid):
    """ใบแจ้งซ่อมPRD-2608-001.pdf — Thai in the folder and in the download."""
    return f"{FORM03_TITLE}{jobid}.pdf"


def ascii_filename(jobid):
    """The name offered to any client too old for a UTF-8 filename — ASCII by construction."""
    return f"repairform_{jobid}.pdf"


def _dmy(ts):
    """27-08-2026 out of a stored timestamp or date, and nothing out of a missing one."""
    t = str(ts or "")[:10]
    if len(t) == 10 and t[4] == "-":
        return f"{t[8:10]}-{t[5:7]}-{t[0:4]}"
    return t


def _job_day(j):
    """The day this job belongs to on the shelf: finished > approved > reported."""
    for k in ("done_at", "approved_at", "created_at"):
        v = str(j.get(k) or "")[:10]
        if v:
            return v
    return date.today().isoformat()


def archive_job_path(j):
    """Where this form is filed — under its PLANT, then the day the job finished.

    A form already sitting in the old date-only folder keeps its place: moving files as
    a side effect of opening one is how an archive quietly loses things. The migration
    script moves them deliberately, or they are rewritten here on the next open.
    """
    from .reports import _outdir, _outdir_find
    dt = datetime.strptime(_job_day(j), "%Y-%m-%d").date()
    name = form_filename(j["jobid"])
    return (_outdir_find(dt, j.get("factory_id"), name)
            or os.path.join(_outdir(dt, j.get("factory_id")), name))


def _media(p):
    """Absolute path of a stored upload ('/uploads/x.png'), honouring BFLFP_DATA."""
    return os.path.join(DATA, p.lstrip("/")) if p else ""


def _collect(jid):
    """Everything the sheet prints, in one read."""
    with closing(db()) as c:
        j = job_row(c, jid)
        names = user_names(c)
        rid = j.get("requester_id") or j.get("created_by")
        req = c.execute("SELECT name,role,department FROM users WHERE id=?",
                        (rid,)).fetchone() if rid else None
        # who took the report in / approved the repair: the person who planned it
        ev = c.execute("SELECT user_id FROM job_events WHERE job_id=? AND status='Assigned'"
                       " ORDER BY id LIMIT 1", (jid,)).fetchone()
        planner = names.get(str(ev["user_id"]), "") if ev and ev["user_id"] else ""
        span = c.execute("SELECT MIN(start) a, MAX(end) b FROM timelogs"
                         " WHERE job_id=? AND seg_type='work' AND end IS NOT NULL",
                         (jid,)).fetchone()
        fac = None
        if j.get("machine_id"):
            m = c.execute("SELECT factory_id FROM machines WHERE id=?",
                          (j["machine_id"],)).fetchone()
            fac = m["factory_id"] if m else None
        code = None
        if fac:
            f = c.execute("SELECT form_code FROM factories WHERE id=?", (fac,)).fetchone()
            code = (f["form_code"] or "").strip() if f else ""
        # The name of the company that owns the machine — not a constant. Three plants
        # are three legal entities and this sheet is filed and audited under one of them.
        company = factory_company(c, fac)
    j["requester_name"] = (req["name"] if req else "") or \
        names.get(str(j.get("created_by")), "")
    j["requester_pos"] = ROLE_TH.get(req["role"], "") if req else ""
    j["requester_dept"] = (req["department"] or "") if req else ""
    j["planner_name"] = planner
    j["approver_name"] = names.get(str(j.get("approver_id")), "")
    # b406: a sheet signed on a department login prints the person who signed
    from .signature import signer_names
    with closing(db()) as c:
        _sn = signer_names(c, jid)
    if _sn.get("sign_requester"):
        j["requester_name"] = _sn["sign_requester"]
    if _sn.get("sign_appr"):
        j["approver_name"] = _sn["sign_appr"]
    if _sn.get("sign_inspector"):
        j["planner_name"] = _sn["sign_inspector"]
    j["helper_names"] = ", ".join(names.get(h, "") for h in
                                  (j.get("helpers") or "").split(",") if h)
    j["work_a"], j["work_b"] = (span["a"], span["b"]) if span else (None, None)
    j["factory_id"] = fac
    j["form_code"] = code or FORM_CODE
    j["company_th"] = company
    return j


def build_job_form(jid, path=None):
    """The job on form F-SP-ENG02-03 — one page, whatever the type size has to be.

    F-SP-ENG02-03 is a one-page controlled document. Setting it in TH Sarabun at a
    readable size is what can push a full form — a long fault description, a machine
    with a long name — onto a second sheet, so the size is settled per form rather
    than once for the plant: built at the intended size, and rebuilt a step smaller
    only if that particular job spilled.
    """
    from .reports import fit_pages
    return fit_pages(lambda p: _job_form_once(jid, p), path, 1)


def _job_form_once(jid, path=None):
    """One pass of the form at whatever text size is currently set.

    A job early in its life still prints — sections 2 and 3 come out blank, because a
    blank on a controlled form is the honest signal the work has not happened yet.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.graphics.shapes import Drawing, Line, Rect
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle, Paragraph,
                                    Spacer, Image as RLImage)
    from reportlab.lib.styles import ParagraphStyle
    from .reports import _pdf_font, _logo_flowable, _hm, FS
    _pdf_font()
    j = _collect(jid)

    BAR = colors.HexColor("#C9DCF0")           # the template's section bars
    GRID = TableStyle([("BOX", (0, 0), (-1, -1), .8, colors.black),
                       ("INNERGRID", (0, 0), (-1, -1), .3, colors.grey),
                       ("VALIGN", (0, 0), (-1, -1), "TOP")])
    # like GRID, but images (signatures) sit centred in their cells
    GRIDC = TableStyle([("BOX", (0, 0), (-1, -1), .8, colors.black),
                        ("INNERGRID", (0, 0), (-1, -1), .3, colors.grey),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("ALIGN", (0, 0), (-1, -1), "CENTER")])
    S  = ParagraphStyle("s",  fontName="APP",  fontSize=FS(9),  leading=FS(13))
    SB = ParagraphStyle("sb", fontName="APPB", fontSize=FS(9),  leading=FS(13))
    SC = ParagraphStyle("sc", fontName="APP",  fontSize=FS(9),  leading=FS(13), alignment=1)
    HC = ParagraphStyle("hc", fontName="APPB", fontSize=FS(9.5), leading=FS(13), alignment=1)
    T1 = ParagraphStyle("t1", fontName="APPB", fontSize=FS(12), leading=FS(15), alignment=1)
    T2 = ParagraphStyle("t2", fontName="APPB", fontSize=FS(11), leading=FS(14), alignment=1)
    SR = ParagraphStyle("sr", fontName="APP",  fontSize=FS(8.5), leading=FS(11), alignment=2)

    def cbx(on):
        """A checkbox drawn, not typed — Thai fonts lack the glyphs (see _tick)."""
        z = 3.8 * mm
        d = Drawing(z, z)
        d.add(Rect(0, 0, z, z, strokeColor=colors.black, strokeWidth=.8,
                   fillColor=colors.white))
        if on:
            d.add(Line(z*.2, z*.5, z*.42, z*.22, strokeWidth=1.2))
            d.add(Line(z*.42, z*.22, z*.84, z*.8, strokeWidth=1.2))
        return d

    def sig(img_path, name, when):
        """Signature block: the stored signature where the app has one, then the
        (name) line and the date — blank space to sign by hand where it does not."""
        out = []
        fp = _media(img_path)
        if fp and os.path.exists(fp):
            # b406: the print copy — cut to the ink, pen-width line, dark ink — kept in
            # its own proportions (it used to be stretched to the box)
            from .sigclear import clear_sig
            fp = clear_sig(fp)
            from PIL import Image as PILImage
            with PILImage.open(fp) as _im:
                _w, _h = _im.size
            _sc = min(34*mm / _w, 13*mm / _h)
            im = RLImage(fp, width=_w*_sc, height=_h*_sc)
            im.hAlign = "CENTER"
            out.append(im)
        else:
            out.append(Spacer(1, 13*mm))
        out.append(Paragraph(f"({name or '.'*44})", SC))
        out.append(Paragraph(_dmy(when), SC))
        return out

    def photo(p):
        """One photo fitted to its grid box (84 x 48 mm), aspect kept — or None."""
        from PIL import Image as PILImage
        fp = _media(p)
        if fp and os.path.exists(fp):
            try:
                with PILImage.open(fp) as im:
                    w, h = im.size
                scale = min(84*mm / w, 48*mm / h)
                return RLImage(fp, width=w*scale, height=h*scale)
            except Exception:
                pass
        return None

    def bar(txt):
        return Table([[Paragraph(txt, HC)]], colWidths=[186*mm],
                     style=TableStyle([("BOX", (0, 0), (-1, -1), .8, colors.black),
                                       ("BACKGROUND", (0, 0), (-1, -1), BAR),
                                       ("TOPPADDING", (0, 0), (-1, -1), 2),
                                       ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))

    j_day = _job_day(j)
    fn = path or archive_job_path(j)
    os.makedirs(os.path.dirname(fn), exist_ok=True)
    doc = SimpleDocTemplate(fn, pagesize=A4, leftMargin=12*mm, rightMargin=12*mm,
                            topMargin=10*mm, bottomMargin=10*mm)
    el = []

    logo = _logo_flowable(12)
    el.append(Table([[logo or "",
                      [Paragraph(j.get("company_th") or COMPANY_TH, T1), Paragraph(FORM03_TITLE, T2)],
                      Paragraph(j["form_code"], SR)]],
                    colWidths=[34*mm, 112*mm, 40*mm],
                    style=TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                      ("BOX", (0, 0), (-1, -1), .8, colors.black)])))

    jt = str(j.get("jobtype") or "").upper()
    el.append(Table([[cbx(jt in ("CM", "BD", "PM")), Paragraph("แจ้งซ่อม", S),
                      cbx(jt in ("IMP", "PRJ")), Paragraph("แจ้งปรับปรุง,แก้ไข", S),
                      Paragraph("เลขที่ใบแจ้งซ่อมบำรุง", S), Paragraph(j["jobid"], SB)]],
                    colWidths=[8*mm, 30*mm, 8*mm, 42*mm, 44*mm, 54*mm],
                    style=TableStyle([("BOX", (0, 0), (-1, -1), .8, colors.black),
                                      ("VALIGN", (0, 0), (-1, -1), "MIDDLE")])))

    # ── ส่วนที่ 1 ─────────────────────────────────────────────────────────────
    el.append(bar("ส่วนที่ 1 การขอทำเรื่อง แจ้งซ่อม"))
    el.append(Table([[Paragraph("ผู้แจ้งซ่อมบำรุง :", S), Paragraph(j["requester_name"], S),
                      Paragraph("ตำแหน่ง :", S), Paragraph(j["requester_pos"], S),
                      Paragraph("หน่วยงาน :", S), Paragraph(j["requester_dept"], S)]],
                    colWidths=[30*mm, 56*mm, 20*mm, 34*mm, 22*mm, 24*mm], style=GRID))
    symptom = " · ".join(x for x in [j.get("problem_type"), j.get("descr")] if x)
    el.append(Table([[Paragraph("รหัสเครื่องจักร/อุปกรณ์/สถานที่ :", S),
                      # work raised on a place rather than an asset has no machine row
                      # behind it. What the reporter typed stands in for the code, said
                      # in words on the form rather than left blank — a blank box on a
                      # signed sheet is read as an omission by whoever signs it next.
                      Paragraph((f"{j.get('mcode') or ''}  {j.get('mname') or ''}".strip()
                                 or (f"ไม่มีในทะเบียน / not listed : {j['asset_text']}"
                                     if j.get('asset_text') else '')
                                 or (j.get('report_name') or '')), S)],
                     [Paragraph("รายละเอียด,อาการ แจ้งซ่อม/ปรับปรุง :", S),
                      Paragraph(symptom, S)]],
                    colWidths=[56*mm, 130*mm], style=GRID))
    el.append(Table([[Paragraph("ผู้แจ้ง", HC), Paragraph("ผู้รับแจ้ง", HC)],
                     # ผู้แจ้ง is the signature given when the fault was REPORTED. It used
                     # to fall back to sign_appr — the operator's acceptance signature —
                     # which put the same mark in two boxes and dated a signature to a
                     # moment it was not given. A missing one is left blank to sign by hand.
                     # b441: no report-time signature → the box stays blank (dotted
                     # line, no name, no date). Printing the reporter's name there read
                     # as his signature a second time beside the ผู้ตรวจรับงาน one.
                     [sig(j.get("sign_requester"),
                          j["requester_name"] if j.get("sign_requester") else "",
                          j.get("created_at") if j.get("sign_requester") else None),
                      sig(j.get("sign_inspector"), j["planner_name"],
                          j.get("inspected_at"))]],
                    colWidths=[93*mm, 93*mm], style=GRIDC))

    # ── ส่วนที่ 2 ─────────────────────────────────────────────────────────────
    el.append(bar("ส่วนที่ 2 การซ่อมบำรุง"))
    cause = " / ".join(x for x in [j.get("root_cause"), j.get("solution")] if x)
    el.append(Table([[Paragraph("การวิเคราะห์สาเหตุและวิธีแก้ไขความเสียหาย :", S),
                      Paragraph(cause, S)]],
                    colWidths=[66*mm, 120*mm], style=GRID))
    planned = bool(j.get("planned_at") or j.get("lead_tech"))
    fixby = _dmy(j.get("due_date") or j.get("planned_date"))
    el.append(Table([[Paragraph("การขออนุมัติ", SB), cbx(planned),
                      Paragraph("อนุมัติให้ซ่อมแล้วเสร็จภายใน วันที่ :", S),
                      Paragraph(fixby if planned else "", S),
                      cbx(False), Paragraph("ไม่อนุมัติ เพราะ:", S), Paragraph("", S)]],
                    colWidths=[27*mm, 7*mm, 52*mm, 24*mm, 7*mm, 24*mm, 45*mm],
                    style=TableStyle([("BOX", (0, 0), (-1, -1), .8, colors.black),
                                      ("VALIGN", (0, 0), (-1, -1), "MIDDLE")])))
    when = ""
    if j.get("work_a") or j.get("work_b"):
        a, b = j.get("work_a"), j.get("work_b")
        when = f"{_dmy(a)}  {_hm(a)} - {_hm(b)}"
    elif j.get("started_at") or j.get("done_at"):
        when = f"{_dmy(j.get('started_at') or j.get('done_at'))}" \
               f"  {_hm(j.get('started_at'))} - {_hm(j.get('done_at'))}"
    techcell = [Paragraph(f"วันที่ทำการซ่อม :  {when}", S), Spacer(1, 1.5*mm),
                Paragraph(f"ช่างผู้รับผิดชอบ 1)  {j.get('lead_name') or ''}", S)]
    fp = _media(j.get("sign_tech"))
    if fp and os.path.exists(fp):
        from .sigclear import clear_sig          # b406: clear print copy, own proportions
        from PIL import Image as PILImage
        fp = clear_sig(fp, (30.0, 11.0))
        with PILImage.open(fp) as _im:
            _w, _h = _im.size
        _sc = min(30*mm / _w, 11*mm / _h)
        techcell.append(RLImage(fp, width=_w*_sc, height=_h*_sc))
    techcell.append(Paragraph(f"ช่างผู้รับผิดชอบ 2)  {j['helper_names']}", S))
    el.append(Table([[Paragraph("ผู้อนุมัติ", HC), Paragraph("วันที่ทำการซ่อม", HC),
                      Paragraph("Supplier/ผู้รับเหมา", HC)],
                     [sig(j.get("sign_inspector"), j["planner_name"],
                          j.get("inspected_at")),
                      techcell,
                      [Paragraph("1)", S), Spacer(1, 6*mm), Paragraph("2)", S)]]],
                    colWidths=[58*mm, 76*mm, 52*mm], style=GRIDC))

    # ── ส่วนที่ 3 ─────────────────────────────────────────────────────────────
    el.append(bar("ส่วนที่ 3 การตรวจสอบการซ่อมแซม"))
    el.append(Table([[Paragraph("บันทึกแก้ไข:", S),
                      Paragraph(j.get("maint_action") or j.get("solution") or "", S)]],
                    colWidths=[26*mm, 160*mm], style=GRID))
    # The operator's approval IS the statement that the machine works normally, so an
    # approved (Done) job ticks the first box even when the technician forgot the
    # clearance tick. Rejected or reworked jobs never tick it.
    ok = j.get("cleared_worksite") or j.get("status") == "Done"
    bad = (j.get("status") in ("Rework", "Rejected")) or (not ok and bool(j.get("pending_reason")))
    checks = Table([[cbx(bool(ok)),
                     Paragraph("ใช้งานได้ตามปกติ สะอาดปลอดภัย ไม่พบการปนเปื้อนข้ามที่อุปกรณ์และพื้นที่"
                               " เช่น เศษสิ่งแปลกปลอม", S)],
                    [cbx(bad), Paragraph(f"ไม่เรียบร้อย เพราะ  {j.get('pending_reason') or ''}", S)],
                    ["", Paragraph(f"ออกใบแจ้งซ่อมใหม่ เลขที่  {j.get('new_issue_id') or ''}", S)]],
                   colWidths=[7*mm, 119*mm],
                   style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                     ("LEFTPADDING", (0, 0), (-1, -1), 1),
                                     ("RIGHTPADDING", (0, 0), (-1, -1), 1)]))
    el.append(Table([[Paragraph("การตรวจสอบการซ่อมแซม", HC), Paragraph("ผู้ตรวจรับงาน", HC)],
                     [checks,
                      sig(j.get("sign_appr"), j["approver_name"] or j["requester_name"],
                          j.get("approved_at"))]],
                    colWidths=[128*mm, 58*mm], style=GRIDC))
    # Photos as pairs: the first before/after pair fills the space left on page 1,
    # each further pair flows onto the next page under a repeated header.
    befores = [photo(j.get("img_before")), photo(j.get("img_before2"))]
    afters = [photo(j.get("img_after")), photo(j.get("img_after2"))]
    befores = [x for x in befores if x]
    afters = [x for x in afters if x]
    prow = [[Paragraph("ก่อน", HC), Paragraph("หลังจาก", HC)]]
    for i in range(max(len(befores), len(afters), 1)):
        prow.append([befores[i] if i < len(befores) else Paragraph("—", SC),
                     afters[i] if i < len(afters) else Paragraph("—", SC)])
    el.append(Table(prow, colWidths=[93*mm, 93*mm], repeatRows=1,
                    style=TableStyle([("BOX", (0, 0), (-1, -1), .8, colors.black),
                                      ("INNERGRID", (0, 0), (-1, -1), .3, colors.grey),
                                      ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                                      ("VALIGN", (1, 0), (-1, -1), "MIDDLE")])))
    doc.build(el)
    return fn


def file_job_form(jid, user=None):
    """Build the job's form, keep it, and put it on the Reports shelf.

    Same contract as reports.file_daily: every route that produces this form comes
    through here, so a form can never be handed to somebody without also being kept.
    One job, one archived file, one shelf row — regenerating updates both in place.
    PM jobs still get a PDF for the button, but are neither archived nor shelved:
    this form is for corrective and breakdown work.
    """
    j = _collect(jid)
    if str(j.get("jobtype") or "").upper() not in FORM03_TYPES:
        # not this form's work — build to a scratch name, skip the archive
        from .reports import _outdir
        dt = datetime.strptime(_job_day(j), "%Y-%m-%d").date()
        return build_job_form(jid, os.path.join(_outdir(dt, j.get("factory_id")),
                                                ascii_filename(j["jobid"])))
    fn = build_job_form(jid, archive_job_path(j))
    # the earlier route filed under repairform_dd-mm-yyyy_<jobid>.pdf — fold it away
    old = os.path.join(os.path.dirname(fn),
                       f"repairform_{datetime.strptime(_job_day(j), '%Y-%m-%d').strftime('%d-%m-%Y')}_{j['jobid']}.pdf")
    if os.path.exists(old):
        try:
            os.remove(old)
        except OSError:
            pass
    try:
        from .db import ensure_plan_reports, now as _now
        with closing(db()) as c:
            ensure_plan_reports(c)
            path = f"/api/jobs/{jid}/pdf?inline=1"
            row = c.execute("SELECT id FROM plan_reports WHERE kind='jobform' AND shift=?",
                            (j["jobid"],)).fetchone()
            if row:
                c.execute("UPDATE plan_reports SET plan_date=?, created_at=?, created_by=?,"
                          " creator=?, path=?, factory_id=? WHERE id=?",
                          (_job_day(j), _now(), (user or {}).get("id"),
                           (user or {}).get("name") or "", path,
                           j.get("factory_id") or (user or {}).get("factory_id"), row["id"]))
            else:
                c.execute("""INSERT INTO plan_reports(factory_id,plan_date,shift,created_at,
                             created_by,creator,jobs,crews,people,unassigned,path,kind)
                             VALUES(?,?,?,?,?,?,1,0,0,0,?,'jobform')""",
                          (j.get("factory_id") or (user or {}).get("factory_id"),
                           _job_day(j), j["jobid"], _now(), (user or {}).get("id"),
                           (user or {}).get("name") or "", path))
            c.commit()
    except Exception as e:
        # The shelf is a convenience; the archived PDF is the record.
        _log.warning("[jobform] could not file on the shelf: %s", e)
    return fn

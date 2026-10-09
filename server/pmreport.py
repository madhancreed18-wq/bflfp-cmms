# ══════════════════════════════════════════════════════════════════════════════════
#  F-SP-ENG02-05 — ใบบันทึกการบำรุงรักษาเชิงป้องกัน (PM checklist record)
#
#  One A4 sheet per PM job: what was checked, what passed, what did not, who looked and
#  when. It is the counterpart to jobform.py — that sheet is for work somebody asked for,
#  this one is for work the calendar asked for.
#
#  Two things here are deliberate and easy to undo by accident:
#
#  1. The checklist grid is ruled to the foot of the page. A six-item sheet used to leave
#     a hand's width of blank paper under the signatures, which on a filed document reads
#     as a page somebody tore the bottom off — and leaves a reviewer standing at the
#     machine nowhere to write. The number of empty rows is MEASURED, not guessed: every
#     block is wrapped against the real frame first, so the grid lands flush whatever the
#     checklist's length and however the criteria text wraps.
#
#  2. Several sheets print into ONE pdf, in the order they were selected, so a planner
#     scrolls a stack rather than opening files one at a time.
# ══════════════════════════════════════════════════════════════════════════════════
import os
import base64
from datetime import datetime
from contextlib import closing

from fastapi import APIRouter, Request, HTTPException

from .config import DATA, UPLOADS, COMPANY_TH, PM_FORM_CODE
from .db import db, job_row, user_names, now, log_status, set_stage, factory_company
from .auth import require_role
from .pm import job_checklist

router = APIRouter(prefix="/api/pm")

TITLE_TH = "ใบบันทึกการบำรุงรักษาเชิงป้องกัน"
TITLE_EN = "PM Checklist Record"

# the five boxes across the top of the paper form, in the plant's order
FREQ_BOXES = [("weekly", "สัปดาห์"), ("monthly", "เดือน"), ("q3m", "3 เดือน"),
              ("m6", "6 เดือน"), ("yearly", "ปี")]


def _media(p):
    """Absolute path of a stored upload ('/uploads/x.jpg'), honouring BFLFP_DATA."""
    return os.path.join(DATA, p.lstrip("/")) if p else ""


def _dmy(ts):
    """2026-09-04 01:41:03 → 04/09/2569 01:41 — the plant reads Buddhist years."""
    s = str(ts or "")
    if len(s) < 10:
        return ""
    try:
        d = datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return s
    out = f"{d.day:02d}/{d.month:02d}/{d.year + 543}"
    return out + (" " + s[11:16] if len(s) >= 16 else "")


def _span(a, b):
    """How long the work took, in words a person reads rather than a timestamp diff."""
    try:
        t0 = datetime.strptime(str(a)[:19], "%Y-%m-%d %H:%M:%S")
        t1 = datetime.strptime(str(b)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ""
    s = max(0, int((t1 - t0).total_seconds()))
    if s < 60:
        return f"{s} วินาที"
    if s < 3600:
        return f"{s // 60} นาที {s % 60} วินาที"
    return f"{s // 3600} ชั่วโมง {(s % 3600) // 60} นาที"


def collect(jid):
    """Everything one sheet prints, in one read."""
    with closing(db()) as c:
        j = job_row(c, jid)
        if (j.get("jobtype") or "") != "PM":
            raise HTTPException(400, "not a PM job")
        cl = job_checklist(c, j) or {"freq": j.get("pm_freq") or "", "label": "",
                                     "items": [], "total": 0, "ng": 0}
        names = user_names(c)
        m = {}
        if j.get("machine_id"):
            r = c.execute("SELECT code,name,line,floor,department,brand_model,serial_no"
                          " FROM machines WHERE id=?", (j["machine_id"],)).fetchone()
            m = dict(r) if r else {}
        sp = c.execute("SELECT MIN(start) a, MAX(end) b FROM timelogs"
                       " WHERE job_id=? AND seg_type='work' AND end IS NOT NULL",
                       (jid,)).fetchone()
        # the plant that owns the machine decides whose name is on the sheet
        _fac = j.get("factory_id")
        if not _fac and j.get("machine_id"):
            _fr = c.execute("SELECT factory_id FROM machines WHERE id=?",
                            (j["machine_id"],)).fetchone()
            _fac = _fr["factory_id"] if _fr else None
        _company = factory_company(c, _fac)
    j["checklist"] = cl
    j["machine"] = m
    j["work_a"], j["work_b"] = (sp["a"], sp["b"]) if sp else (None, None)
    # whoever actually answered the items — not whoever the job is booked to, which on a
    # job passed between shifts is a different person
    j["tech_name"] = next((i["tech_name"] for i in cl["items"] if i.get("tech_name")),
                          names.get(str(j.get("lead_tech")), ""))
    j["approver_name"] = names.get(str(j.get("approver_id")), "")
    j["company_th"] = _company
    return j


def record_name(j):
    """W01CL01_2026-09-03 — the name the sheet is filed under, same as the job's."""
    if j.get("report_name"):
        return j["report_name"]
    code = (j.get("machine") or {}).get("code") or ""
    day = str(j.get("done_at") or j.get("planned_date") or "")[:10]
    return f"{code}_{day}"


def _sheet(j, style, width_mm):
    """The flowables of one sheet, as (fixed_blocks, make_checklist(fillers)).

    Returned in two halves so the caller can measure the fixed part against the real
    frame and only then decide how many empty rows the grid needs.
    """
    from reportlab.platypus import Table, TableStyle, Paragraph, Image as RLImage
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    S, SB, HC, T1, T2, SR, SM = style
    from .reports import _logo_flowable

    cl = j["checklist"]
    m = j["machine"]
    BAR = colors.HexColor("#DCE6F1")
    GRID = TableStyle([("BOX", (0, 0), (-1, -1), .8, colors.black),
                       ("INNERGRID", (0, 0), (-1, -1), .4, colors.black),
                       ("VALIGN", (0, 0), (-1, -1), "MIDDLE")])

    def bar(txt):
        return Table([[Paragraph(txt, HC)]], colWidths=[width_mm * mm],
                     style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), BAR),
                                       ("BOX", (0, 0), (-1, -1), .8, colors.black),
                                       ("TOPPADDING", (0, 0), (-1, -1), 2),
                                       ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))

    head = Table([[_logo_flowable(11) or "",
                   [Paragraph(j.get("company_th") or COMPANY_TH, T1),
                    Paragraph(f"{TITLE_TH} · {TITLE_EN}", T2)],
                   [Paragraph(j.get("form_code") or PM_FORM_CODE, SR),
                    Paragraph(j.get("jobid") or "", SR)]]],
                 colWidths=[30 * mm, (width_mm - 70) * mm, 40 * mm],
                 style=TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                   ("BOX", (0, 0), (-1, -1), .8, colors.black)]))

    # The five frequency boxes. Drawn as real ruled cells rather than ☐/☑ characters:
    # the Thai fonts the PDF falls back to on a Windows server do not all carry those
    # glyphs, and a form whose tick box prints as a hollow rectangle is worse than none.
    def cbx(on):
        return Table([[_mark("OK") if on else Paragraph("", SB)]],
                     colWidths=[4.4 * mm], rowHeights=[4.4 * mm],
                     style=TableStyle([("BOX", (0, 0), (-1, -1), .7, colors.black),
                                       ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                       ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                       ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                       ("TOPPADDING", (0, 0), (-1, -1), 0),
                                       ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    fcells, fw = [], []
    for k, lb in FREQ_BOXES:
        fcells += [cbx(k == cl.get("freq")), Paragraph(lb, S)]
        fw += [5 * mm, 17 * mm]
    boxes = Table([fcells], colWidths=fw,
                  style=TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                    ("LEFTPADDING", (0, 0), (-1, -1), 1),
                                    ("RIGHTPADDING", (0, 0), (-1, -1), 1)]))
    freq = Table([[Paragraph("ความถี่ PM", SB), boxes,
                   Paragraph("วันที่", SB), Paragraph(_dmy(j.get("done_at") or j.get("planned_date")), S)]],
                 colWidths=[22 * mm, (width_mm - 74) * mm, 16 * mm, 36 * mm], style=GRID)

    mach = Table([[Paragraph("รหัสเครื่อง", SB), Paragraph(m.get("code") or "", SB),
                   Paragraph("ชื่อเครื่องจักร", SB), Paragraph(m.get("name") or "", S)],
                  [Paragraph("สถานที่ / ห้อง", SB), Paragraph(m.get("line") or "", S),
                   Paragraph("ช่างผู้ทำงาน", SB), Paragraph(j.get("tech_name") or "", S)]],
                 colWidths=[26 * mm, 34 * mm, 26 * mm, (width_mm - 86) * mm], style=GRID)

    # ── the grid, built to order once the fillers are known ────────────────────────
    CW = [10 * mm, 30 * mm, (width_mm - 122) * mm, 12 * mm, 9 * mm, 9 * mm, 52 * mm]

    def checklist(fillers):
        rows = [[Paragraph(x, HC) for x in
                 ("ลำดับ", "รายการตรวจเช็ค", "เกณฑ์ปกติ", "วิธี", "OK", "NG", "หมายเหตุ")]]
        style = [("BOX", (0, 0), (-1, -1), .8, colors.black),
                 ("INNERGRID", (0, 0), (-1, -1), .4, colors.black),
                 ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                 # tight rows: reportlab's default 6pt above and below turns a one-line
                 # row into 22pt, which is half a page of air on a six-item sheet
                 ("TOPPADDING", (0, 0), (-1, -1), 2),
                 ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                 ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F4F6F9"))]
        for n, it in enumerate(cl["items"], start=1):
            ok = it.get("result") == "OK"
            ng = it.get("result") == "NG"
            rows.append([Paragraph(str(it.get("seq") or n), HC),
                         Paragraph(it.get("item") or "", S),
                         Paragraph(it.get("normal") or "", S),
                         Paragraph(it.get("method") or "", HC),
                         _mark("OK") if ok else Paragraph("", HC),
                         _mark("NG") if ng else Paragraph("", HC),
                         Paragraph(it.get("remark") or "", SM)])
            if ng:
                style.append(("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1),
                              colors.HexColor("#FDECEC")))
        # ruled empty rows: the grid reaches the foot of the page and a reviewer has
        # somewhere to write. Numbered in grey so nobody reads them as missed items.
        for k in range(fillers):
            rows.append([Paragraph(f'<font color="#C9CFD6">{len(cl["items"]) + k + 1}</font>', HC)]
                        + [Paragraph("", S)] * 6)
        nok = sum(1 for i in cl["items"] if i.get("result") == "OK")
        nng = sum(1 for i in cl["items"] if i.get("result") == "NG")
        rows.append([Paragraph(f'<para alignment="right">รวม {len(cl["items"])} รายการ</para>', SB),
                     "", "", "", Paragraph(str(nok), HC), Paragraph(str(nng), HC),
                     Paragraph(f"NG {nng} รายการ", HC)])
        last = len(rows) - 1
        style += [("SPAN", (0, last), (3, last)),
                  ("BACKGROUND", (0, last), (-1, last), colors.HexColor("#F4F6F9"))]
        return Table(rows, colWidths=CW, repeatRows=1, style=TableStyle(style))

    # ── ส่วนที่ 3: what has to be chased, and room to write what turns up ──────────
    ngs = [i for i in cl["items"] if i.get("result") == "NG"]
    nrows = [[Paragraph(x, HC) for x in ("ลำดับ", "รายการ", "สิ่งที่พบ / การแก้ไข", "ใบแจ้งซ่อม CM")]]
    for i in ngs:
        nrows.append([Paragraph(str(i.get("seq") or ""), HC), Paragraph(i.get("item") or "", S),
                      Paragraph(i.get("remark") or "", S), Paragraph("เลขที่ ..............", HC)])
    if not ngs:
        nrows.append([Paragraph('<para alignment="center">— ไม่มีรายการ NG —</para>', S),
                      "", "", ""])
    # two ruled lines whether or not this PM found anything: a reviewer walking the
    # machine afterwards needs a place for what the technician did not see
    for _ in range(2):
        nrows.append([Paragraph("", S)] * 4)
    nstyle = [("BOX", (0, 0), (-1, -1), .8, colors.black),
              ("INNERGRID", (0, 0), (-1, -1), .4, colors.black),
              ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
              ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F4F6F9"))]
    if not ngs:
        nstyle.append(("SPAN", (0, 1), (-1, 1)))
    ngtab = Table(nrows, colWidths=[12 * mm, 44 * mm, (width_mm - 90) * mm, 34 * mm],
                  style=TableStyle(nstyle))

    def fit(path, max_w_mm, max_h_mm, sig=False):
        """An image scaled into a box, aspect kept — or None if it cannot be read.

        The file is DECODED here, not merely opened: PIL reads a header lazily, so a
        truncated upload passes .size happily and then throws inside doc.build, where the
        failure takes the whole PDF with it. A sheet with a missing photo is a small
        problem; a stack of sheets that will not print because one phone dropped a
        connection mid-upload is a much larger one.
        """
        f = _media(path)
        if not f or not os.path.exists(f):
            return None
        if sig:                                  # b406: the clear print copy
            from .sigclear import clear_sig
            f = clear_sig(f, (max_w_mm, max_h_mm))
        try:
            from PIL import Image as PILImage
            with PILImage.open(f) as im:
                im.load()
                w, h = im.size
            if not w or not h:
                return None
            sc = min(max_w_mm * mm / w, max_h_mm * mm / h)
            return RLImage(f, width=w * sc, height=h * sc)
        except Exception:
            return None

    def photo(p, h_mm=40):
        return fit(p, width_mm / 2 - 6, h_mm) or Paragraph(
            '<para alignment="center"><font color="#9AA0A6">— ไม่ได้แนบรูป —</font></para>', S)

    pics = Table([[Paragraph("รูปก่อนทำ PM", HC), Paragraph("รูปหลังทำ PM", HC)],
                  [photo(j.get("img_before")), photo(j.get("img_after"))]],
                 colWidths=[width_mm / 2 * mm, width_mm / 2 * mm],
                 style=TableStyle([("BOX", (0, 0), (-1, -1), .8, colors.black),
                                   ("INNERGRID", (0, 0), (-1, -1), .4, colors.black),
                                   ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F4F6F9")),
                                   ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                                   ("VALIGN", (0, 1), (-1, 1), "MIDDLE"),
                                   ("TOPPADDING", (0, 1), (-1, 1), 4),
                                   ("BOTTOMPADDING", (0, 1), (-1, 1), 4)]))

    when = Table([[Paragraph("วันที่ตามแผน", SB), Paragraph(_dmy(j.get("planned_date")), S),
                   Paragraph("เริ่มงาน", SB), Paragraph(_dmy(j.get("work_a")), S),
                   Paragraph("เสร็จงาน", SB), Paragraph(_dmy(j.get("work_b") or j.get("done_at")), S)],
                  [Paragraph("รวมเวลา", SB), Paragraph(_span(j.get("work_a"), j.get("work_b") or j.get("done_at")), S),
                   Paragraph("ช่างผู้ทำงาน", SB), Paragraph(j.get("tech_name") or "", S),
                   Paragraph("สถานะ", SB),
                   Paragraph("ตรวจรับแล้ว" if j.get("approved_at") else "เสร็จสิ้น — รอตรวจรับ", S)]],
                 colWidths=[22 * mm, 30 * mm, 18 * mm, 32 * mm, 16 * mm,
                            (width_mm - 118) * mm], style=GRID)

    def sigcell(img_path, name, when_ts):
        # the mark keeps its own proportions inside the box: a signature stretched to fit
        # a fixed rectangle is not the signature that was given
        im = fit(img_path, 34, 13, sig=True)
        out = [im] if im else [Paragraph("<br/><br/>", S)]
        out.append(Paragraph(f'<para alignment="center">{name or ""}<br/>'
                             f'{_dmy(when_ts) if when_ts else "วันที่ ....... / ....... / ......."}</para>', SM))
        return out

    sigs = Table([[Paragraph("ช่างผู้ทำงาน", HC), Paragraph("หัวหน้าช่างซ่อมบำรุง", HC),
                   Paragraph("ผู้ตรวจรับ", HC)],
                  [sigcell(j.get("sign_tech"), j.get("tech_name"), j.get("done_at")),
                   sigcell("", "", ""),
                   sigcell(j.get("sign_appr"), j.get("approver_name"), j.get("approved_at"))]],
                 colWidths=[width_mm / 3 * mm] * 3,
                 style=TableStyle([("BOX", (0, 0), (-1, -1), .8, colors.black),
                                   ("INNERGRID", (0, 0), (-1, -1), .4, colors.black),
                                   ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F4F6F9")),
                                   ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                                   ("VALIGN", (0, 1), (-1, 1), "BOTTOM")]))

    foot = Table([[Paragraph(f"บันทึกเลขที่ {record_name(j)}", SM),
                   Paragraph(f'<para alignment="right">พิมพ์เมื่อ {_dmy(now())} · BFLFP CMMS</para>', SM)]],
                 colWidths=[width_mm / 2 * mm, width_mm / 2 * mm],
                 style=TableStyle([("TOPPADDING", (0, 0), (-1, -1), 1),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))

    before = [head, freq, _gap(2), bar("ส่วนที่ 1   ข้อมูลเครื่องจักร"), mach,
              _gap(2), bar("ส่วนที่ 2   รายการตรวจเช็ค")]
    after = [_gap(2), bar("ส่วนที่ 3   งานที่ต้องติดตาม (NG)"), ngtab,
             _gap(2), pics, _gap(2), bar("ส่วนที่ 4   เวลาทำงาน และผู้ปฏิบัติงาน"), when,
             _gap(2), sigs, foot]
    return before, checklist, after


def _mark(kind):
    """A tick or a cross, DRAWN rather than typed.

    ✓ and ✗ are not in Tahoma, and not in the Thai fonts this falls back to on Linux, so
    a typed one prints as a hollow box on some machines and fine on others — the kind of
    difference nobody notices until an auditor is holding the wrong copy. Two lines of
    vector cost nothing and look the same everywhere.
    """
    from reportlab.graphics.shapes import Drawing, Line
    from reportlab.lib import colors
    d = Drawing(9, 9)
    if kind == "OK":
        col = colors.HexColor("#14532D")
        d.add(Line(1.0, 4.6, 3.4, 1.6, strokeColor=col, strokeWidth=1.5,
                   strokeLineCap=1))
        d.add(Line(3.4, 1.6, 8.0, 7.6, strokeColor=col, strokeWidth=1.5,
                   strokeLineCap=1))
    else:
        col = colors.HexColor("#991B1B")
        d.add(Line(1.4, 1.4, 7.6, 7.6, strokeColor=col, strokeWidth=1.5, strokeLineCap=1))
        d.add(Line(7.6, 1.4, 1.4, 7.6, strokeColor=col, strokeWidth=1.5, strokeLineCap=1))
    return d


def _gap(mm_h):
    from reportlab.platypus import Spacer
    from reportlab.lib.units import mm
    return Spacer(1, mm_h * mm)


def build(ids, path):
    """The PM sheets — one page per job, whatever the type size has to be.

    F-SP-ENG02-05 is one sheet per PM. The grid already measures itself to the foot of
    the page, so a bigger face fills it with fewer ruled rows rather than overflowing;
    what can still spill is the fixed matter above and below it on a job with a long
    machine name or many NG remarks. Same rule as the repair form: build, and step the
    size down only for the sheets that actually needed it.
    """
    from .reports import fit_pages
    return fit_pages(lambda p: _build_once(ids, p), path, max(1, len(ids)))


def _build_once(ids, path):
    """Write one PDF holding a sheet for each job id, in the order given."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, PageBreak
    from reportlab.lib.styles import ParagraphStyle
    from .reports import _pdf_font, FS
    _pdf_font()

    S = ParagraphStyle("s", fontName="APP", fontSize=FS(8), leading=FS(10.5))
    SB = ParagraphStyle("sb", fontName="APPB", fontSize=FS(8), leading=FS(10.5))
    SM = ParagraphStyle("sm", fontName="APP", fontSize=FS(7), leading=FS(9.5))
    HC = ParagraphStyle("hc", fontName="APPB", fontSize=FS(8), leading=FS(10.5), alignment=1)
    T1 = ParagraphStyle("t1", fontName="APPB", fontSize=FS(12), leading=FS(15), alignment=1)
    T2 = ParagraphStyle("t2", fontName="APP", fontSize=FS(9.5), leading=FS(12), alignment=1)
    SR = ParagraphStyle("sr", fontName="APP", fontSize=FS(7.5), leading=FS(10), alignment=2)
    style = (S, SB, HC, T1, T2, SR, SM)

    L = R = 12 * mm
    T = B = 10 * mm
    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=L, rightMargin=R,
                            topMargin=T, bottomMargin=B, title="PM Checklist Record")
    # A frame is not the same as the margin box: SimpleDocTemplate pads its frame by 6pt
    # on every side, so the space a flowable actually gets is 12pt narrower and 12pt
    # shorter than the margins suggest. Measuring against the wrong number is how a sheet
    # that fits by 10pt on the arithmetic spills onto a second page in the file.
    PAD = 12
    FW = A4[0] - L - R - PAD
    FH = A4[1] - T - B - PAD
    width_mm = FW / mm

    el = []
    for n, jid in enumerate(ids):
        j = collect(jid)
        before, checklist, after = _sheet(j, style, width_mm)

        # How many empty rows the grid can carry. Everything is wrapped against the real
        # frame first — including the checklist itself with no fillers — so the answer
        # holds however the criteria text happens to wrap on this particular machine.
        def measured(flow):
            tot = 0
            for f in flow:
                try:
                    tot += f.wrap(FW, FH)[1]
                except Exception:
                    tot += 0
            return tot

        # Measure, do not estimate. An empty row's real height is the difference between
        # a grid with one and a grid with none — which is the only number that survives
        # a font change, a padding change, or a criteria line that wraps to three lines.
        h0 = checklist(0).wrap(FW, FH)[1]
        rowh = max(6.0, checklist(1).wrap(FW, FH)[1] - h0)
        used = measured(before) + measured(after) + h0
        fillers = max(0, int((FH - used - 3 * mm) / rowh))
        fillers = min(fillers, 40)           # a runaway measurement must not paginate

        el += before + [checklist(fillers)] + after
        if n + 1 < len(ids):
            el.append(PageBreak())

    doc.build(el)
    return path


def archive_path(j):
    """Where this sheet is filed: data/Report/<PLANT>/YYYY/MM/DD/, the day it finished.

    A sheet already filed in the old date-only folder is left where it is and found
    there, so nothing moves by itself; the migration script relocates them on purpose.
    """
    from .reports import _outdir, _outdir_find
    day = str(j.get("done_at") or j.get("planned_date") or now())[:10]
    try:
        dt = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        dt = datetime.now().date()
    fac = j.get("factory_id") or (j.get("machine") or {}).get("factory_id")
    name = f"PM_{record_name(j)}.pdf"
    return _outdir_find(dt, fac, name) or os.path.join(_outdir(dt, fac), name)


def file_sheet(jid):
    """Build one sheet and keep it. Same contract as the repair form: every route that
    produces a sheet also archives it, so a document can never be handed to somebody
    without also being on the shelf."""
    j = collect(jid)
    return build([jid], archive_path(j))


# ══════════════════════════════════════════════════════════════════════════════════
#  API
# ══════════════════════════════════════════════════════════════════════════════════
@router.get("/reports")
async def pm_reports(req: Request, days: int = 120):
    """Completed PM work, newest first — one row per job, with its checklist tally."""
    u = require_role(req, "planner", "admin", "manager", "engcenter")
    fac = u.get("active_factory") or u.get("factory_id")     # the plant chosen at login
    with closing(db()) as c:
        try:
            from .elec import ensure as _elens
            _elens(c)
        except Exception:
            pass
        rows = [dict(r) for r in c.execute(
            "SELECT j.id, j.jobid, j.pm_freq, j.status, j.planned_date, j.done_at,"
            "       j.approved_at, j.report_name, j.lead_tech,"
            "       m.code mcode, m.name mname, m.line mline,"
            "       (SELECT MIN(start) FROM timelogs t WHERE t.job_id=j.id AND t.seg_type='work') started_at"
            "  FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id"
            " WHERE j.jobtype='PM' AND (j.status IN ('ServiceCompleted','Done')"
            "        OR (j.status NOT IN ('Cancelled','Rejected') AND j.id IN"
            "            (SELECT job_id FROM pm_elec WHERE COALESCE(done_at,'')<>'')))"
            "   AND COALESCE(j.done_at,(SELECT done_at FROM pm_elec e WHERE e.job_id=j.id),'') >= date('now', ?)"
            "   AND COALESCE(m.factory_id, j.factory_id)=?"
            " ORDER BY COALESCE(j.done_at, j.planned_date) DESC, j.id DESC",
            (f"-{max(1, int(days))} days", fac))]
        names = user_names(c)
        try:
            from .elec import plant_has_elec
            ELX_ANY = plant_has_elec(c, fac)
        except Exception:
            ELX_ANY = False
        for r in rows:
            t = c.execute("SELECT COUNT(*) n, SUM(result='OK') ok, SUM(result='NG') ng,"
                          "       MIN(tech_name) tech"
                          "  FROM pm_results WHERE job_id=?", (r["id"],)).fetchone()
            r["items"], r["ok"], r["ng"] = t["n"] or 0, t["ok"] or 0, t["ng"] or 0
            r["tech_name"] = t["tech"] or names.get(str(r.get("lead_tech")), "")
            r["signed"] = 1 if r.get("approved_at") else 0
            r["took"] = _span(r.get("started_at"), r.get("done_at"))
            r["record"] = r.get("report_name") or ""
            # b413: a sheet whose electrical points Central Electrical has not done yet is
            # not a finished record — it is shown, marked, and cannot be accepted
            r["el_wait"] = 0
            # b422: the two halves — plant (mechanical) and Central Electrical
            r["parts"] = None
            if r["status"] != "Done" and ELX_ANY:
                try:
                    from .elec import parts as _parts
                    from .db import job_row as _jr
                    r["parts"] = _parts(c, _jr(c, r["id"]))
                except Exception:
                    r["parts"] = None
            p = r["parts"]
            if p and not p["e_total"]:
                r["parts"] = p = None                      # nothing electrical: one crew, as before
            if p:
                r["el_wait"] = p["e_total"] - p["e_done"]
                r["m_wait"] = 0 if p["m_ok"] else 1
                if not r.get("done_at"):
                    r["done_at"] = p["ce_done_at"]
    return {"rows": rows}


def _ids(s):
    out = []
    for part in str(s or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    if not out:
        raise HTTPException(400, "no job ids")
    if len(out) > 60:
        raise HTTPException(400, "too many sheets in one file (max 60)")
    return out


@router.get("/reports/pdf")
async def pm_reports_pdf(req: Request, ids: str = "", inline: str = ""):
    """One PDF, one sheet per job, in the order asked for — the stack a planner scrolls."""
    require_role(req, "planner", "admin", "manager", "engcenter")
    from .reports import _pdf_response, _outdir
    jids = _ids(ids)
    if len(jids) == 1:
        fn = file_sheet(jids[0])              # a single sheet is filed as it is made
        name = f"PM_{record_name(collect(jids[0]))}.pdf"
    else:
        d = _outdir(datetime.now().date())
        name = f"PM_{len(jids)}_sheets_{datetime.now().strftime('%Y%m%d-%H%M%S')}.pdf"
        fn = build(jids, os.path.join(d, name))
    return _pdf_response(fn, inline, name)


@router.post("/reports/sign")
async def pm_reports_sign(req: Request):
    """Accept one or more completed PM sheets.

    The signature lands in the ผู้ตรวจรับ box of every sheet signed, the job closes as
    Done, and the sheet is rebuilt so the filed copy carries the mark. Signing several at
    once writes the SAME image to each — one act of acceptance over a stack of paper —
    but each job keeps its own timestamp and its own signoff row, so the record still
    says exactly when each sheet was accepted.
    """
    u = require_role(req, "planner", "admin", "manager")
    b = await req.json()
    ids = b.get("ids") or []
    sig = b.get("signature") or ""
    if sig.startswith("reg:"):                            # b406: the planner's registered signature
        from .signature import resolve as _sres
        sig, _ = _sres(u, sig)
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "no job ids")
    if "," not in sig:
        raise HTTPException(400, "signature required")
    head, b64 = sig.split(",", 1)
    ext = "png" if "png" in head else "jpg"
    raw = base64.b64decode(b64)

    done, skipped, waiting = [], [], []
    with closing(db()) as c:
        for jid in ids:
            try:
                jid = int(jid)
            except (TypeError, ValueError):
                continue
            j = c.execute("SELECT id,jobtype,status FROM jobs WHERE id=?", (jid,)).fetchone()
            if not j or (j["jobtype"] or "") != "PM":
                skipped.append(jid)
                continue
            if j["status"] not in ("ServiceCompleted",):
                skipped.append(jid)            # already accepted, or not finished
                continue
            try:                               # b413: electrical points still owed
                from .elec import accept_block
                from .db import job_row as _jr
                if accept_block(c, _jr(c, jid)):
                    skipped.append(jid)
                    waiting.append(jid)
                    continue
            except Exception:
                pass
            fn = f"{jid}_sign_appr.{ext}"
            with open(os.path.join(UPLOADS, fn), "wb") as f:
                f.write(raw)
            ts = now()
            c.execute("UPDATE jobs SET sign_appr=?, status='Done', approver_id=?,"
                      " approved_at=COALESCE(approved_at,?) WHERE id=?",
                      (f"/uploads/{fn}", u["id"], ts, jid))
            c.execute("INSERT INTO signoffs(job_id,action,user_id,signature,reason,created_at)"
                      " VALUES(?,?,?,?,?,?)",
                      (jid, "pm_accept", u["id"], f"/uploads/{fn}", "", ts))
            log_status(c, jid, "Done", u["id"])
            set_stage(c, jid)
            done.append(jid)
        c.commit()

    # file the accepted sheets with the signature on them; a failure here must not undo
    # an acceptance the database has already recorded
    for jid in done:
        try:
            file_sheet(jid)
        except Exception:
            pass
    return {"ok": True, "signed": done, "skipped": skipped, "waiting_electrical": waiting}

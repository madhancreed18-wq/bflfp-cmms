import os
from datetime import datetime, date
from contextlib import closing

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import FileResponse

from .config import BASE, REPORTS, COMPANY_TH, FORM_CODE
from .db import db, job_row, user_names
from .auth import user_from

router = APIRouter(prefix="/api")


def _pdf_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for p in [r"C:\Windows\Fonts\tahoma.ttf", r"C:\Windows\Fonts\LeelawUI.ttf",
              "/usr/share/fonts/truetype/tlwg/Garuda.ttf",
              "/usr/share/fonts/truetype/tlwg/Loma.ttf",
              "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
        if os.path.exists(p):
            pdfmetrics.registerFont(TTFont("APP", p))
            bd = p.replace("tahoma.ttf", "tahomabd.ttf")
            pdfmetrics.registerFont(TTFont("APPB", bd if os.path.exists(bd) else p))
            return
    raise HTTPException(500, "no TTF font found for PDF")


def _outdir(dt):
    d = os.path.join(REPORTS, f"{dt.year}", f"{dt.month:02d}", f"{dt.day:02d}")
    os.makedirs(d, exist_ok=True)
    return d


@router.get("/jobs/{jid}/pdf")
async def job_pdf(jid: int, req: Request):
    user_from(req)
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle, Paragraph,
                                    Spacer, Image as RLImage)
    from reportlab.lib.styles import ParagraphStyle
    _pdf_font()
    with closing(db()) as c:
        j = job_row(c, jid)
        names = user_names(c)
        j["creator_name"] = names.get(str(j["created_by"]), "")
        helpers = ", ".join(names.get(h, "") for h in (j["helpers"] or "").split(",") if h)
    dt = date.today()
    fn = os.path.join(_outdir(dt), f"repairform_{dt.strftime('%d-%m-%Y')}_{j['jobid']}.pdf")

    S = ParagraphStyle("s", fontName="APP", fontSize=9, leading=13)
    SB = ParagraphStyle("sb", fontName="APPB", fontSize=10, leading=14)
    SH = ParagraphStyle("sh", fontName="APPB", fontSize=12, leading=16, alignment=1)
    SC = ParagraphStyle("sc", fontName="APP", fontSize=9, leading=13, alignment=1)
    doc = SimpleDocTemplate(fn, pagesize=A4, leftMargin=12*mm, rightMargin=12*mm,
                            topMargin=10*mm, bottomMargin=10*mm)
    el = []
    chk = lambda b: "☑" if b else "☐"

    def imgcell(p):
        fp = os.path.join(BASE, "data", p.lstrip("/")) if p else ""
        return (RLImage(fp, width=70*mm, height=50*mm)
                if p and os.path.exists(fp) else Paragraph("—", SC))

    def signcell(p):
        fp = os.path.join(BASE, "data", p.lstrip("/")) if p else ""
        return (RLImage(fp, width=35*mm, height=15*mm)
                if p and os.path.exists(fp) else Spacer(1, 15*mm))

    el.append(Table([[Paragraph(COMPANY_TH, SB), Paragraph("ใบแจ้งซ่อม", SH),
                      Paragraph(FORM_CODE, S)]], colWidths=[62*mm, 62*mm, 62*mm]))
    el.append(Spacer(1, 2*mm))
    el.append(Table([[Paragraph(f"{chk(j['jobtype'] in ('CM','BD','PM'))} แจ้งซ่อม", S),
                      Paragraph(f"{chk(j['jobtype']=='IMP')} แจ้งปรับปรุง,แก้ไข", S),
                      Paragraph("เลขที่ใบแจ้งซ่อมบำรุง", S), Paragraph(j["jobid"], SB)]],
                    colWidths=[40*mm, 55*mm, 45*mm, 46*mm],
                    style=TableStyle([("BOX", (0,0), (-1,-1), .7, colors.black),
                                      ("INNERGRID", (0,0), (-1,-1), .3, colors.grey)])))
    el.append(Spacer(1, 2*mm))
    el.append(Paragraph("ส่วนที่ 1 การขอทำเรื่องแจ้งซ่อม", SB))
    el.append(Table([
        [Paragraph("ผู้แจ้งซ่อมบำรุง:", S), Paragraph(j["creator_name"], S),
         Paragraph("วันที่แจ้ง:", S), Paragraph((j["created_at"] or "")[:10], S)],
        [Paragraph("รหัสเครื่องจักร/อุปกรณ์/สถานที่:", S),
         Paragraph(f"{j['mcode'] or ''} {j['mname'] or ''}", S),
         Paragraph("ความเร่งด่วน:", S),
         Paragraph({1: "ปกติ", 2: "เร่ง", 3: "ด่วน!"}.get(j["priority"], ""), S)],
        [Paragraph("รายละเอียด,อาการ แจ้งซ่อม/ปรับปรุง:", S),
         Paragraph(j["descr"] or "", S), "", ""]],
        colWidths=[48*mm, 78*mm, 30*mm, 30*mm],
        style=TableStyle([("BOX", (0,0), (-1,-1), .7, colors.black),
                          ("INNERGRID", (0,0), (-1,-1), .3, colors.grey),
                          ("SPAN", (1,2), (3,2)), ("VALIGN", (0,0), (-1,-1), "TOP")])))
    el.append(Spacer(1, 2*mm))
    el.append(Paragraph("ส่วนที่ 2 การซ่อมบำรุง", SB))
    el.append(Table([
        [Paragraph("ปัญหาที่พบ (Problem):", S), Paragraph(j["problem"] or "", S)],
        [Paragraph("การวิเคราะห์สาเหตุ (Root cause):", S), Paragraph(j["root_cause"] or "", S)],
        [Paragraph("วิธีแก้ไข (Solution):", S), Paragraph(j["solution"] or "", S)],
        [Paragraph("ช่างผู้รับผิดชอบ:", S),
         Paragraph(f"1) {j['lead_name'] or ''}   2) {helpers}", S)],
        [Paragraph("วันที่ทำการซ่อม:", S),
         Paragraph(f"{j['planned_date'] or ''}  {j['planned_start'] or ''}-{j['planned_end'] or ''}", S)]],
        colWidths=[58*mm, 128*mm],
        style=TableStyle([("BOX", (0,0), (-1,-1), .7, colors.black),
                          ("INNERGRID", (0,0), (-1,-1), .3, colors.grey),
                          ("VALIGN", (0,0), (-1,-1), "TOP")])))
    el.append(Spacer(1, 2*mm))
    el.append(Paragraph("ส่วนที่ 3 การตรวจสอบการซ่อมแซม", SB))
    ok = j["cleared_worksite"]
    el.append(Table([
        [Paragraph(f"{chk(ok==1)} ใช้งานได้ตามปกติ สะอาดปลอดภัย ไม่พบการปนเปื้อนข้ามที่อุปกรณ์และพื้นที่", S),
         Paragraph("ผู้ตรวจรับงาน", SC)],
        [Paragraph(f"{chk(ok==0)} ไม่เรียบร้อย เพราะ: {j['pending_reason'] or ''}", S),
         signcell(j["sign_inspector"])],
        [Paragraph(f"ออกใบแจ้งซ่อมใหม่ เลขที่: {j['new_issue_id'] or '—'}", S),
         Paragraph(f"(.....................................)<br/>{dt.strftime('%d/%m/%Y')}", SC)]],
        colWidths=[128*mm, 58*mm],
        style=TableStyle([("BOX", (0,0), (-1,-1), .7, colors.black),
                          ("INNERGRID", (0,0), (-1,-1), .3, colors.grey),
                          ("VALIGN", (0,0), (-1,-1), "TOP")])))
    el.append(Spacer(1, 2*mm))
    el.append(Table([[Paragraph("ก่อน", SC), Paragraph("หลังจาก", SC)],
                     [imgcell(j["img_before"]), imgcell(j["img_after"])]],
                    colWidths=[93*mm, 93*mm],
                    style=TableStyle([("BOX", (0,0), (-1,-1), .7, colors.black),
                                      ("INNERGRID", (0,0), (-1,-1), .3, colors.grey),
                                      ("ALIGN", (0,0), (-1,-1), "CENTER")])))
    doc.build(el)
    return FileResponse(fn, media_type="application/pdf", filename=os.path.basename(fn))


@router.get("/reports/daily")
async def daily_report_pdf(req: Request, d: str = ""):
    from .db import today
    user_from(req)
    d = d or today()
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import ParagraphStyle
    _pdf_font()
    from .db import day_range
    d0, d1 = day_range(d)
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute("""SELECT j.*, m.code mcode, u.name lead_name
            FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
            LEFT JOIN users u ON u.id=j.lead_tech
            WHERE (j.planned_date=? OR (j.jobtype='BD' AND j.created_at >= ? AND j.created_at <= ?))
            AND j.status NOT IN ('Cancelled') ORDER BY j.planned_start""", (d, d0, d1))]
        segs = [dict(r) for r in c.execute("""SELECT t.*, u.name tech_name FROM timelogs t
            JOIN users u ON u.id=t.tech WHERE t.start >= ? AND t.start <= ?""", (d0, d1))]
    rel = [j for j in rows if j["jobtype"] != "BD" and j["status"] in
           ("Released", "InProgress", "Paused", "Rework", "ServiceCompleted", "Done")]
    bds = [j for j in rows if j["jobtype"] == "BD"]
    dp = [j for j in rel if j["status"] in ("ServiceCompleted", "Done")]
    dbd = [j for j in bds if j["status"] in ("ServiceCompleted", "Done")]
    pc = lambda a, b: f"{round(a/b*100)}%" if b else "—"
    mins = {}
    for s in segs:
        t0 = datetime.strptime(s["start"], "%Y-%m-%d %H:%M:%S")
        t1 = datetime.strptime(s["end"], "%Y-%m-%d %H:%M:%S") if s["end"] else datetime.now()
        k = (s["tech_name"], "งานซ่อม/PM" if s["seg_type"] == "work" else
             "Routine" if s["seg_type"] == "routine" else "Standby/รอ/ประชุม")
        mins[k] = mins.get(k, 0) + (t1 - t0).total_seconds() / 60

    dt = datetime.strptime(d, "%Y-%m-%d").date()
    fn = os.path.join(_outdir(dt), f"MaintenanceReport_{dt.strftime('%d-%m-%Y')}.pdf")
    S = ParagraphStyle("s", fontName="APP", fontSize=8, leading=11)
    SB = ParagraphStyle("sb", fontName="APPB", fontSize=10, leading=14)
    SH = ParagraphStyle("sh", fontName="APPB", fontSize=14, leading=18, alignment=1)
    doc = SimpleDocTemplate(fn, pagesize=landscape(A4), leftMargin=10*mm, rightMargin=10*mm,
                            topMargin=8*mm, bottomMargin=8*mm)
    el = [Paragraph(f"{COMPANY_TH} — รายงานซ่อมบำรุงประจำวัน (Daily Maintenance Report)", SH),
          Paragraph(f"วันที่ {dt.strftime('%d/%m/%Y')}",
                    ParagraphStyle("c", fontName="APP", fontSize=10, alignment=1)),
          Spacer(1, 4*mm),
          Paragraph(f"งานตามแผนสำเร็จ: {len(dp)}/{len(rel)} = {pc(len(dp), len(rel))}     "
                    f"งาน Breakdown สำเร็จ: {len(dbd)}/{len(bds)} = {pc(len(dbd), len(bds))}     "
                    f"รวมทั้งหมด: {len(dp)+len(dbd)}/{len(rel)+len(bds)} = "
                    f"{pc(len(dp)+len(dbd), len(rel)+len(bds))}", SB),
          Spacer(1, 4*mm)]
    hdr = ["เลขที่", "ประเภท", "เครื่องจักร", "รายละเอียด", "ช่าง", "แผน", "สถานะ", "%", "เหตุผลค้าง"]
    data = [[Paragraph(h, SB) for h in hdr]]
    for j in rows:
        data.append([Paragraph(str(x or ""), S) for x in [
            j["jobid"], j["jobtype"], j["mcode"], (j["descr"] or "")[:60], j["lead_name"],
            f"{j['planned_start'] or ''}-{j['planned_end'] or ''}", j["status"],
            str(j["progress"]), j["pending_reason"]]])
    el.append(Table(data, colWidths=[24*mm, 12*mm, 20*mm, 78*mm, 30*mm, 24*mm, 30*mm, 10*mm, 45*mm],
                    style=TableStyle([("BOX", (0,0), (-1,-1), .7, colors.black),
                                      ("INNERGRID", (0,0), (-1,-1), .3, colors.grey),
                                      ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#FEF3C7")),
                                      ("VALIGN", (0,0), (-1,-1), "TOP")])))
    el.append(Spacer(1, 4*mm))
    el.append(Paragraph("สรุปเวลาช่าง (นาที)", SB))
    tdata = [[Paragraph(x, SB) for x in ["ช่าง", "ประเภทเวลา", "นาที"]]]
    for (tech, kind), m in sorted(mins.items()):
        tdata.append([Paragraph(tech, S), Paragraph(kind, S), Paragraph(str(round(m)), S)])
    el.append(Table(tdata, colWidths=[60*mm, 60*mm, 25*mm],
                    style=TableStyle([("BOX", (0,0), (-1,-1), .7, colors.black),
                                      ("INNERGRID", (0,0), (-1,-1), .3, colors.grey)])))
    doc.build(el)
    return FileResponse(fn, media_type="application/pdf", filename=os.path.basename(fn))

import os
from datetime import datetime, date
from contextlib import closing
from urllib.parse import quote

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import FileResponse

from .config import BASE, REPORTS, COMPANY_TH, FORM_CODE
from .db import db, job_row, user_names
from .auth import user_from

import logging
# the app's own logger — the file handler is attached by logs.setup(), and
# everything written here shows in Manage → Logs. print() does not: it goes to
# a console nobody is watching, which is where these messages used to die.
_log = logging.getLogger("cmms")

router = APIRouter(prefix="/api")


# ── The face every filed form is set in ──────────────────────────────────────────
# The daily report, the repair notification F-SP-ENG02-03 and the PM checklist
# F-SP-ENG02-05 are all set in TH Sarabun PSK — the Thai government standard face
# these documents are meant to be printed in. One registration serves all three:
# every style in reports.py, jobform.py and pmreport.py asks for "APP" / "APPB".
#
# It is looked for in the REPO first, not in C:\Windows\Fonts. A controlled document
# has to come out the same on the planner's PC, on the server and on a Linux box, and
# reading whatever each machine happens to have installed is exactly how one form ends
# up in three different faces with nobody noticing. The installed copies stay as a
# fallback so a machine that has not had the file copied still prints something Thai
# rather than failing — and _pdf_font records which file it actually used, so the
# question "is this sheet in the right font" has an answer that is not a guess.
FONT_DIR = os.path.join(BASE, "static", "fonts")

# The family ships under several filenames depending on where it came from. All of
# them are the same design; the first that exists wins.
_SARABUN = [("THSarabunPSK.ttf", "THSarabunPSK Bold.ttf"),
            ("THSarabunNew.ttf", "THSarabunNew Bold.ttf"),
            ("Sarabun-Regular.ttf", "Sarabun-Bold.ttf")]

# TH Sarabun sets a good deal smaller than Tahoma at the same point size — it is drawn
# for 14-16pt Thai body text, and the sizes in these three modules were tuned against
# Tahoma at 7.5-14pt. Left alone, every form would print correct but shrunken. So the
# sizes stay written as they were tuned and are passed through FS(), which scales them
# only when a Sarabun face is the one in use. Override with PDF_FONT_SCALE if a printer
# wants it tighter or looser; 1.0 reproduces the literal point sizes.
SARABUN_SCALE = float(os.environ.get("PDF_FONT_SCALE") or 1.35)

PDF_FONT = ""            # basename of the file actually registered
PDF_SCALE = 1.0          # the FACE's scale — set by _pdf_font()
PDF_FIT = 1.0            # this DOCUMENT's scale — set by fit_pages()


def FS(v):
    """A point size or leading, for the face in use and the sheet being built.

    Two multipliers, deliberately kept apart. PDF_SCALE belongs to the typeface and is
    re-decided every time a font is registered; PDF_FIT belongs to the one document
    being fitted onto its page. Folding them into a single number is a trap: every
    builder calls _pdf_font() on entry, so a fitting pass that had just reduced the
    size would have it reset underneath on the very next rebuild, and the retry loop
    would quietly do nothing at all.
    """
    return round(v * PDF_SCALE * PDF_FIT, 2)


def _bundled_sarabun():
    """Whatever Sarabun actually got dropped into static/fonts, whatever it is named.

    The named list above only matches the three spellings we thought of, and the file
    that turned up on the server is 'THSarabun.ttf' — none of them. The PDF then falls
    back to Tahoma without saying so, which is exactly the silent-wrong-font failure
    this change was meant to end. So read the folder first and take what is in it: any
    .ttf with 'sarabun' in the name, italics dropped, bold paired to its regular by
    name. Release names differ; the folder does not lie.
    """
    try:
        files = [f for f in os.listdir(FONT_DIR) if f.lower().endswith(".ttf")]
    except OSError:
        return []
    sar = [f for f in files if "sarabun" in f.lower() and "italic" not in f.lower()]
    out = []
    for r in sorted(f for f in sar if "bold" not in f.lower()):
        stem = r[:-4].lower()
        want = {stem + " bold.ttf", stem + "-bold.ttf", stem + "bold.ttf", stem + "_bold.ttf"}
        bold = next((b for b in sar if b.lower() in want), r)
        out.append((os.path.join(FONT_DIR, r), os.path.join(FONT_DIR, bold), True))
    return out


def _font_candidates():
    """(regular, bold, is_sarabun), most preferred first."""
    win = r"C:\Windows\Fonts" + "\\"
    out = _bundled_sarabun()
    out += [(os.path.join(FONT_DIR, r), os.path.join(FONT_DIR, b), True) for r, b in _SARABUN]
    out += [(win + r, win + b, True) for r, b in _SARABUN]
    out += [("/usr/share/fonts/truetype/thai/" + r, "/usr/share/fonts/truetype/thai/" + b, True)
            for r, b in _SARABUN]
    out += [(r"C:\Windows\Fonts\tahoma.ttf", r"C:\Windows\Fonts\tahomabd.ttf", False),
            (r"C:\Windows\Fonts\LeelawUI.ttf", r"C:\Windows\Fonts\LeelaUIb.ttf", False),
            ("/usr/share/fonts/truetype/tlwg/Garuda.ttf",
             "/usr/share/fonts/truetype/tlwg/Garuda-Bold.ttf", False),
            ("/usr/share/fonts/truetype/tlwg/Loma.ttf",
             "/usr/share/fonts/truetype/tlwg/Loma-Bold.ttf", False),
            ("/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
             "/usr/share/fonts/truetype/noto/NotoSansThai-Bold.ttf", False),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", False)]
    return out


def _pdf_font():
    global PDF_FONT, PDF_SCALE
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for reg, bold, sarabun in _font_candidates():
        if not os.path.exists(reg):
            continue
        # Existing is not the same as usable. A file with PostScript outlines (an .otf
        # renamed .ttf), a truncated copy, half a download — reportlab raises on all of
        # them, and without this the first bad file in the list would take every PDF in
        # the app down with it rather than letting the next candidate have a go. The
        # one thing worth being loud about is having nothing at all, at the end.
        try:
            pdfmetrics.registerFont(TTFont("APP", reg))
            # A missing or unreadable bold is not a reason to fail — the regular stands
            # in, which reads as a form with weak headings rather than no form at all.
            try:
                pdfmetrics.registerFont(TTFont("APPB", bold if os.path.exists(bold) else reg))
            except Exception:
                pdfmetrics.registerFont(TTFont("APPB", reg))
        except Exception:
            continue
        PDF_FONT = os.path.basename(reg)
        PDF_SCALE = SARABUN_SCALE if sarabun else 1.0
        return
    raise HTTPException(500, "no usable TTF font found for PDF")


def _page_count(path):
    """Pages in a PDF, without a parser. Counting /Type /Page objects is crude but it
    only has to answer "did this controlled form stay on one sheet"."""
    import re
    try:
        with open(path, "rb") as fh:
            n = len(re.findall(rb"/Type\s*/Page(?![s/])", fh.read()))
        return n or 1
    except Exception:
        return 1


def fit_pages(build, path, want):
    """Build at the largest text size that still fits `want` pages.

    F-SP-ENG02-03 and F-SP-ENG02-05 are one-page controlled documents — a sheet that
    silently becomes two is a QA failure, not a cosmetic one. Scaling the type up to
    keep TH Sarabun readable is exactly the thing that can push a full form over the
    edge, and how close it comes depends on the job: a long fault description, a
    checklist whose criteria wrap to three lines, a machine with a very long name.

    So the size is not decided once for the whole plant, it is decided per sheet. The
    form is built at the intended scale, and only if it spilled is it rebuilt smaller,
    stepping down until it fits. Almost every form takes the first size; the awkward
    ones quietly come out a little tighter instead of arriving as two pages.

    Measuring the real output rather than estimating from metrics is deliberate — it is
    the same rule the PM grid already follows, and it is the only method that survives
    a change of font, of padding, or of how a line happens to wrap.
    """
    global PDF_FIT
    out = path
    try:
        for step in (1.0, 0.93, 0.86, 0.80, 0.74, 0.68, 0.62):
            PDF_FIT = step
            # `path` may be None on the first pass — the builder decides where the file
            # goes and hands the name back; every retry overwrites that same file.
            out = build(out)
            if _page_count(out) <= want:
                break
    finally:
        PDF_FIT = 1.0        # never leave a shrunk size behind for the next document
    return out


@router.get("/reports/font")
async def report_font(req: Request):
    """Which face the forms are actually being printed in, and where it came from.

    Worth an endpoint because the failure this guards against is silent: a machine
    without the file still produces a perfectly good-looking PDF, in the wrong font,
    and nobody notices until QA compares two copies of the same form.
    """
    user_from(req)
    _pdf_font()
    return {"font": PDF_FONT, "scale": PDF_SCALE,
            "is_sarabun": PDF_SCALE != 1.0,
            "bundled": bool(_bundled_sarabun()) or any(
                os.path.exists(os.path.join(FONT_DIR, r)) for r, _b in _SARABUN),
            "font_dir": FONT_DIR}


def _fac_code(fac):
    """The plant's short code — BFL, FP, PC — or "" when there is no plant."""
    if not fac:
        return ""
    try:
        with closing(db()) as c:
            r = c.execute("SELECT code FROM factories WHERE id=?", (fac,)).fetchone()
        return (r["code"] or "").strip() if r else ""
    except Exception:
        return ""


def _outdir(dt, fac=None):
    """Where a document is filed: data/Report/<PLANT>/YYYY/MM/DD/.

    The plant is the FIRST level because that is the unit somebody asks for — "the
    Petcare records for August" is then one folder to copy, back up or hand to an
    auditor. Dated folders with three plants' paperwork mixed in them answer that
    question only by reading filenames one at a time.

    Without a plant (a job on no asset, an older call) the old date-only path is used,
    unchanged, so nothing already filed moves on its own and every read still finds it.
    """
    parts = [REPORTS]
    code = _fac_code(fac)
    if code:
        parts.append(code)
    parts += [f"{dt.year}", f"{dt.month:02d}", f"{dt.day:02d}"]
    d = os.path.join(*parts)
    os.makedirs(d, exist_ok=True)
    return d


def _outdir_find(dt, fac=None, name=""):
    """The plant's folder if the file is there, else the old date-only folder.

    Reading must never depend on a file having been moved. Filing writes to the new
    place; finding looks in both, newest layout first.
    """
    if name:
        p = os.path.join(_outdir(dt, fac), name)
        if os.path.exists(p):
            return p
        p2 = os.path.join(_outdir(dt), name)
        if os.path.exists(p2):
            return p2
    return ""


@router.get("/jobs/{jid}/pdf")
async def job_pdf(jid: int, req: Request, inline: str = ""):
    """The job's own document — whichever one the job actually is.

    A PM job is not a repair. Printing it on F-SP-ENG02-03 produced a sheet headed
    ใบแจ้งซ่อม / repair notification for work nobody reported broken, with the
    symptom, root cause and repair-action boxes empty because a PM has none — and
    the checklist that IS the record of the work nowhere on the page. One button on
    the job screen, so the button has to pick: PM work goes to the PM checklist
    record (F-SP-ENG02-05), everything else to the repair form.

    Either way the file is archived and listed on the Reports page, so nobody has to
    remember to save anything. ?inline=1 is for looking; without it the browser saves.
    """
    u = user_from(req)
    with closing(db()) as c:
        j = job_row(c, jid)
    if str(j.get("jobtype") or "").upper() == "PM":
        from .pmreport import file_sheet, collect, record_name
        fn = file_sheet(jid)
        return _pdf_response(fn, inline, f"PM_{record_name(collect(jid))}.pdf")
    from .jobform import file_job_form, ascii_filename
    fn = file_job_form(jid, u)
    return _pdf_response(fn, inline, ascii_filename(j["jobid"]))


def _logo_flowable(h_mm=11):
    """The company mark for the top of a report, or None if the file is missing.

    A report that refuses to build because a logo moved is worse than a report with
    no logo, so every failure here is silent.
    """
    try:
        from reportlab.platypus import Image as RLImage
        from reportlab.lib.units import mm
        from PIL import Image as PILImage
        for name in ("BFL260.png", "BFL100.png", "logo.png"):
            p = os.path.join(BASE, "static", name)
            if os.path.exists(p):
                with PILImage.open(p) as im:
                    w, h = im.size
                return RLImage(p, width=h_mm * mm * (w / h), height=h_mm * mm)
    except Exception:
        pass
    return None


def build_daily_report(d, path=None, fac=None):
    """The daily maintenance report, exactly as the plant reads it on paper.

    Returns the path of the PDF it wrote. Both the Dashboard button and the filed
    copy on the Reports page come through here, so the two can never drift apart:
    one layout, in one place.
    """
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import ParagraphStyle
    _pdf_font()
    from .db import day_range, factory_company
    d0, d1 = day_range(d)
    with closing(db()) as c:
        # three plants, three legal entities — the sheet carries the name of the one
        # it reports on, and falls back to the compiled-in name only when unscoped
        company = factory_company(c, fac)
        rows = [dict(r) for r in c.execute("""SELECT j.*, m.code mcode, u.name lead_name
            FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
            LEFT JOIN users u ON u.id=j.lead_tech
            WHERE (j.planned_date=? OR (j.jobtype='BD' AND j.created_at >= ? AND j.created_at <= ?))
            AND j.status NOT IN ('Cancelled') ORDER BY j.planned_start""", (d, d0, d1))]
        segs = [dict(r) for r in c.execute("""SELECT t.*, u.name tech_name FROM timelogs t
            JOIN users u ON u.id=t.tech WHERE t.start >= ? AND t.start <= ?""", (d0, d1))]
    rel = [j for j in rows if j["jobtype"] != "BD" and j["status"] in
           ("Assigned", "InProgress", "Paused", "Rework", "ServiceCompleted", "Done")]
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
    fn = path or os.path.join(_outdir(dt), f"MaintenanceReport_{dt.strftime('%d-%m-%Y')}.pdf")
    os.makedirs(os.path.dirname(fn), exist_ok=True)
    S = ParagraphStyle("s", fontName="APP", fontSize=FS(8), leading=FS(11))
    SB = ParagraphStyle("sb", fontName="APPB", fontSize=FS(10), leading=FS(14))
    SH = ParagraphStyle("sh", fontName="APPB", fontSize=FS(14), leading=FS(18), alignment=1)
    doc = SimpleDocTemplate(fn, pagesize=landscape(A4), leftMargin=10*mm, rightMargin=10*mm,
                            topMargin=8*mm, bottomMargin=8*mm)

    title = Paragraph(f"{company} — รายงานซ่อมบำรุงประจำวัน (Daily Maintenance Report)", SH)
    logo = _logo_flowable()
    if logo:
        # The mark on the left with the title still centred on the PAGE, not on the
        # space beside it: hence the empty third cell of matching width. Two cells
        # would nudge the heading right by half the logo column.
        head = Table([[logo, title, ""]], colWidths=[26*mm, None, 26*mm],
                     style=TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                       ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                       ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                       ("TOPPADDING", (0, 0), (-1, -1), 0),
                                       ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    else:
        head = title

    el = [head,
          Paragraph(f"วันที่ {dt.strftime('%d/%m/%Y')}",
                    ParagraphStyle("c", fontName="APP", fontSize=FS(10), alignment=1)),
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
    return fn


def _pdf_response(fn, inline="", fallback="report.pdf"):
    """Hand back a PDF the browser will either show or save.

    Content-Disposition decides this, and getting it wrong is not cosmetic: with
    "attachment" a phone downloads the file the moment anything fetches the URL — so an
    on-screen preview quietly fills the downloads folder. "inline" is for looking,
    "attachment" is for keeping, and the caller says which it wants.
    """
    name = os.path.basename(fn)
    kind = "inline" if inline else "attachment"
    try:
        name.encode("ascii")
        cd = f'{kind}; filename="{name}"'
    except UnicodeEncodeError:
        # A Thai filename cannot go in the plain filename= field: that field is latin-1
        # only, and one Thai byte makes the whole header invalid — some browsers then
        # save the file as the URL path, which is "daily". RFC 5987's filename* carries
        # the UTF-8 name; the ASCII name beside it is what an older browser falls back
        # to, so nobody ever gets a file called "daily".
        cd = (f'{kind}; filename="{fallback}"; '
              f"filename*=utf-8''{quote(name)}")
    return FileResponse(fn, media_type="application/pdf",
                        headers={"Content-Disposition": cd})


# What the file is called, in the folder and in the download. The people who open this
# folder read Thai, and a filename is the only label a PDF has once it leaves the app.
FORM06_FILE = "รายงานการซ่อมบำรุง"


def report_filename(d, fac=None):
    """The day's form as a filename: รายงานการซ่อมบำรุง_FP_dd-mm-yyyy.pdf

    The plant is IN THE NAME because there is one of these per plant per day. Without
    it, three plants shared one file: whichever one was generated last overwrote the
    others, so a BFL manager opening Tuesday's report got Petcare's sheet under the
    Food Products company name, and no amount of fixing the header would have helped.
    """
    dt = datetime.strptime(d, "%Y-%m-%d").date()
    code = _fac_code(fac)
    tag = f"_{code}" if code else ""
    return f"{FORM06_FILE}{tag}{dt.strftime('%d-%m-%Y')}.pdf"


def _legacy_path(d):
    """The ASCII name this form carried before the Thai one.

    Also the name offered to any client too old to read a UTF-8 filename, which is why
    it stays ASCII by construction.
    """
    dt = datetime.strptime(d, "%Y-%m-%d").date()
    return os.path.join(_outdir(dt), f"F-SP-ENG02-06_{dt.strftime('%d-%m-%Y')}.pdf")


def _old_paths(d):
    """Every name this day's form has been filed under before now.

    The name has changed twice. A report already in the archive must not vanish from the
    Reports list because of that, and regenerating a day must not leave last month's
    spelling sitting beside this month's as a second copy of the same report.
    """
    dt = datetime.strptime(d, "%Y-%m-%d").date()
    dmy = dt.strftime("%d-%m-%Y")
    return [_legacy_path(d),
            os.path.join(_outdir(dt), f"รายงานการซ่อมบำรุงประจำวัน {dmy}.pdf")]


def archive_path(d, fac=None):
    """Where the day's form lives on disk, for good.

    data/Report/YYYY/MM/DD/ — one file per day, rewritten whenever the report is
    generated again, so the archive holds the day's latest truth rather than a pile of
    near-identical copies.
    """
    dt = datetime.strptime(d, "%Y-%m-%d").date()
    return os.path.join(_outdir(dt, fac), report_filename(d, fac))


def archive_found(d, fac=None):
    """The day's filed form if there is one, under either name — else "".

    Every read goes through this, so a day filed under the old English name still opens
    from the Reports list exactly as it did before.
    """
    dt = datetime.strptime(d, "%Y-%m-%d").date()
    cands = [archive_path(d, fac),                       # plant folder, plant in the name
             os.path.join(_outdir(dt), report_filename(d, fac)),   # old folder, new name
             archive_path(d)]                            # old folder, old shared name
    cands += _old_paths(d)
    for p in cands:
        if os.path.exists(p):
            return p
    return ""


def file_daily(d, user=None):
    """Build the day's form, keep it, and put it on the Reports shelf.

    Every route that produces this report comes through here, so a report can never be
    handed to somebody without also being kept: opening it, previewing it, or pressing
    Issue all leave the same single archived file and the same one shelf row for that
    day. The row is updated rather than duplicated — a planner who checks the sheet
    four times before the shift ends should not find four entries afterwards.
    """
    _fac = (user or {}).get("active_factory") or (user or {}).get("factory_id")
    fn = build_maintenance_form(d, archive_path(d, _fac), _fac)
    # A day filed under an earlier spelling would otherwise sit beside this one as a
    # second copy of the same report. One day, one file.
    for old in _old_paths(d):
        if old != fn and os.path.exists(old):
            try:
                os.remove(old)
            except OSError as e:
                _log.warning("[reports] could not remove the old-named copy: %s", e)
    try:
        from .db import ensure_plan_reports, now as _now
        from .planreport import _day_jobs, _day_counts, _file_counts
        fac = (user or {}).get("active_factory") or (user or {}).get("factory_id")
        with closing(db()) as c:
            ensure_plan_reports(c)
            K = _day_counts(_day_jobs(c, fac, d))
            path = f"/api/reports/archive?d={d}&fac={fac or ''}"
            row = c.execute("SELECT id FROM plan_reports WHERE factory_id=? AND plan_date=?"
                            " AND COALESCE(kind,'plan')='done'", (fac, d)).fetchone()
            if row:
                c.execute("UPDATE plan_reports SET created_at=?, created_by=?, creator=?,"
                          " jobs=?, unassigned=?, path=? WHERE id=?",
                          (_now(), (user or {}).get("id"), (user or {}).get("name") or "",
                           K["n"], K["nocrew"], path, row["id"]))
            else:
                c.execute("""INSERT INTO plan_reports(factory_id,plan_date,shift,created_at,
                             created_by,creator,jobs,crews,people,unassigned,path,kind)
                             VALUES(?,?,'',?,?,?,?,0,0,?,?,'done')""",
                          (fac, d, _now(), (user or {}).get("id"),
                           (user or {}).get("name") or "", K["n"], K["nocrew"], path))
            # Earlier builds filed a new timestamped row every time the button was
            # pressed, and those rows point at formats this plant no longer uses. One
            # day, one row: fold any older duplicates into the one just written. The
            # PDFs they referenced stay on disk — only the index entry goes.
            keep = c.execute("SELECT id FROM plan_reports WHERE factory_id=? AND plan_date=?"
                             " AND COALESCE(kind,'plan')='done' AND path=?",
                             (fac, d, path)).fetchone()
            if keep:
                c.execute("DELETE FROM plan_reports WHERE factory_id=? AND plan_date=?"
                          " AND COALESCE(kind,'plan')='done' AND id<>?",
                          (fac, d, keep["id"]))
            _file_counts(c, fac, d, "done", K)
            c.commit()
    except Exception as e:
        # The shelf is a convenience; the archived PDF is the record. A bookkeeping
        # failure must never stop somebody getting their report.
        _log.warning("[reports] could not file on the shelf: %s", e)
    return fn


def _done_rows(d, fac=None):
    """The completed work the form will print for day d — the form's own selection.

    The sheet and the summary must never disagree about how many jobs a day holds, so
    both read the day through this one query rather than each writing its own.
    """
    with closing(db()) as c:
        _fx = (" AND (COALESCE(m.factory_id,j.factory_id)=?"
               " OR COALESCE(m.factory_id,j.factory_id) IS NULL)") if fac else ""
        return [dict(r) for r in c.execute(
            """SELECT j.jobtype, j.cleared_worksite, j.root_cause
               FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
               WHERE substr(COALESCE(j.done_at,''),1,10)=?
                 AND j.status IN ('Done','ServiceCompleted')""" + _fx,
            (d,) if not fac else (d, fac))]


@router.get("/reports/daystats")
async def report_day_stats(req: Request, days: str = ""):
    """Where the work on a set of report days actually stands TODAY.

    The document shelf used to answer this by adding its columns up, and a column of
    snapshots does not add up. Each report records the day as it stood when it was
    issued, so a job still open across six days is counted in six reports — twelve BFLFP
    reports read "216 jobs · 13 Finished · 6% of them" over a far smaller number of real
    jobs. Every one of those figures was an artefact of the addition, and a plant reading
    6% complete when it is three quarters done is not a cosmetic problem.

    So the shelf asks instead, and this counts the JOBS: distinct, current, scoped to the
    reader's plant. Crucially it asks for each day exactly what the report asked —
    `_day_jobs`, the same three-part selection the sheet is built from: planned for that
    day, **still open from an earlier day** (the carried work, which is most of why a day
    like 5 Sep listed 51 jobs when only 26 were dated to it), and finished that day
    whatever day it was planned for. Counting `planned_date` alone would describe a
    different set of jobs from the one on the paper, which is the same class of mistake
    as the totals this replaces.
    """
    u = user_from(req)
    fac = u.get("active_factory") or u.get("factory_id")
    ds = sorted({x.strip()[:10] for x in str(days or "").split(",") if x.strip()})[:120]
    if not ds or not fac:
        return {"days": 0, "jobs": 0, "done": 0, "accept": 0, "doing": 0,
                "open": 0, "closed": 0, "carried": 0}
    # imported here, not at module scope: planreport imports this module for file_daily
    from .planreport import _day_jobs
    seen, carried = {}, set()
    with closing(db()) as c:
        for d in ds:
            for j in _day_jobs(c, fac, d):
                seen[j["id"]] = j["status"]
                if j.get("carry_from"):
                    carried.add(j["id"])
    st = lambda *s: sum(1 for x in seen.values() if x in s)
    done, accept = st("Done"), st("ServiceCompleted")
    doing, closed = st("InProgress", "Paused", "Hold"), st("Cancelled", "Rejected")
    return {"days": len(ds), "jobs": len(seen), "done": done, "accept": accept,
            "doing": doing, "closed": closed, "carried": len(carried),
            "open": len(seen) - done - accept - doing - closed}


@router.get("/reports/daily/summary")
async def daily_report_summary(req: Request, d: str = ""):
    """What the day's form would contain, without building it.

    The sheet in the app asks this before it offers a download. Fetching the PDF just
    to find out whether it is worth fetching costs a second and, on a phone, silently
    saves a file nobody asked for.
    """
    from .db import today
    _u = user_from(req)
    d = d or today()
    # the summary must agree with the sheet, so it reads the same plant's day
    rows = _done_rows(d, _u.get("active_factory") or _u.get("factory_id"))
    ty = lambda t: sum(1 for r in rows if str(r["jobtype"] or "").upper() == t)
    return {"date": d, "jobs": len(rows),
            "pm": ty("PM"), "cm": ty("CM"), "bd": ty("BD"),
            "cleared": sum(1 for r in rows if r["cleared_worksite"]),
            "no_cause": sum(1 for r in rows if not str(r["root_cause"] or "").strip()),
            "filed": bool(archive_found(d))}


@router.get("/reports/daily")
async def daily_report_pdf(req: Request, d: str = "", summary: str = "", inline: str = ""):
    """The day's completed work on form F-SP-ENG02-06 — the sheet the plant files.

    Generating it also archives it and lists it on the Reports page, so nobody has to
    remember to save anything.

    ?summary=1 returns the older internal readout instead (percent complete and
    technician minutes). That is a morning-meeting readout rather than the controlled
    form, so it is neither archived nor filed.
    """
    from .db import today
    u = user_from(req)
    d = d or today()
    if summary:
        return _pdf_response(build_daily_report(
            d, None, u.get("active_factory") or u.get("factory_id")), inline)
    # A day with nothing finished on it has no form to file. Building one anyway would
    # archive a blank sheet and put a phantom entry on the Reports shelf, so the day
    # would look reported when no work was done. Say so instead.
    if not _done_rows(d, u.get("active_factory") or u.get("factory_id")):
        raise HTTPException(404, "no completed work on that day")
    fn = file_daily(d, u)
    return _pdf_response(fn, inline, os.path.basename(_legacy_path(d)))


@router.get("/reports/archive")
async def daily_report_archived(req: Request, d: str = "", inline: str = "",
                                fac: str = "", rebuild: str = ""):
    """The stored copy of a day's form, exactly as it was last generated.

    This one does not rebuild by default. A report opened from the history should show
    what was filed, not what today's database would now say about that day — that is
    the whole point of an archive, and it is why correcting the template does NOT
    correct the sheets already on the shelf.

    ?rebuild=1 is the deliberate exception: re-issue this day's form from today's data,
    replacing the filed copy. It is how a document filed under a wrong company name or
    an old layout is brought up to date, and it is an explicit act rather than a side
    effect of opening the file.
    """
    from .db import today
    u = user_from(req)
    d = d or today()
    _fac = int(fac) if str(fac).isdigit() else (
        u.get("active_factory") or u.get("factory_id"))
    if rebuild:
        if not _done_rows(d, _fac):
            raise HTTPException(404, "no completed work on that day")
        return _pdf_response(file_daily(d, {**u, "active_factory": _fac}), inline,
                             os.path.basename(_legacy_path(d)))
    fn = archive_found(d, _fac)
    if not fn:
        raise HTTPException(404, "no report has been filed for that day yet")
    return _pdf_response(fn, inline, os.path.basename(_legacy_path(d)))


# ══════════════════════════════════════════════════════════════════════════════════
#  F-SP-ENG02-06 — รายงานการซ่อมบำรุงประจำวัน
#  The plant's own controlled form. The column headings, the note at the foot and the
#  signature line are the document's wording, not ours: this sheet gets filed and
#  audited, so it has to come out of the system looking exactly as it looks on paper.
# ══════════════════════════════════════════════════════════════════════════════════

FORM06_ROWS = 14          # lines on the printed sheet, filled or not
FORM06_CODE = "F-SP-ENG02-06 Rev.01"
FORM06_DEPT = "แผนกวิศวกรรม"
FORM06_HEAD = ["ชื่อเครื่องจักร", "ตั้งแต่เวลา-เวลา", "อาการที่เกิด",
               "สาเหตุปัญหาที่เกิดขึ้น", "การปรับแต่ง/การแก้ไข/แนวทางการปรับปรุง",
               "เคลียร์หลังซ่อม", "ผู้ปรับแต่ง"]
FORM06_NOTE = ("หมายเหตุ : เจ้าหน้าที่ซ่อมบำรุงดำเนินการเคลียร์สิ่งแปลกปลอมและความสะอาด"
               "หลังซ่อมเสร็จ ใส่เครื่องหมาย / หลังเคลียร์เรียบร้อย")


def _hm(ts):
    """08:14 out of a stored timestamp, and nothing out of a missing one."""
    t = str(ts or "")
    return t[11:16] if len(t) >= 16 else ""


def _tick(mm_size, colour):
    """The clearance mark, drawn rather than typed.

    Thai fonts do not carry U+2713, and which font the sheet prints with depends on
    the PC. Two lines always render.
    """
    from reportlab.graphics.shapes import Drawing, Line
    z = mm_size
    d = Drawing(z, z)
    d.add(Line(z*.18, z*.52, z*.42, z*.22, strokeColor=colour, strokeWidth=1.4))
    d.add(Line(z*.42, z*.22, z*.86, z*.80, strokeColor=colour, strokeWidth=1.4))
    return d


def build_maintenance_form(d, path=None, fac=None):
    """The day's completed work on form F-SP-ENG02-06. Returns the path written.

    Only work that was actually finished appears — the form is a record of repairs
    done, not a list of what is outstanding. A job with no cause or no fix recorded
    still prints, with those cells blank, because a blank on an audited form is the
    honest signal that somebody skipped a box.
    """
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import ParagraphStyle
    _pdf_font()
    from .db import factory_company
    with closing(db()) as c:
        # whose name goes on it — the plant this report is for, not a constant
        company = factory_company(c, fac)
        # ONE plant per sheet. This query had no factory filter at all, so the daily
        # form printed every plant's completed work on one page — BFL machines on a
        # Petcare sheet — and the three plants overwrote each other's archived copy.
        _fx = (" AND (COALESCE(m.factory_id,j.factory_id)=?"
               " OR COALESCE(m.factory_id,j.factory_id) IS NULL)") if fac else ""
        jobs = [dict(r) for r in c.execute("""SELECT j.*, m.code mcode, m.name mname,
                   u.name lead_name
            FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
            LEFT JOIN users u ON u.id=j.lead_tech
            WHERE substr(COALESCE(j.done_at,''),1,10)=?
              AND j.status IN ('Done','ServiceCompleted')""" + _fx + """
            ORDER BY j.done_at, j.id""", (d,) if not fac else (d, fac))]
        names = {r["id"]: r["name"] for r in c.execute("SELECT id,name FROM users")}
        segs = [dict(r) for r in c.execute(
            "SELECT job_id,tech,start,end FROM timelogs WHERE end IS NOT NULL")]

    # who touched it: everyone who logged time, then the lead, then the helpers —
    # the form asks for the people, not for one owner
    worked = {}
    for s in segs:
        worked.setdefault(s["job_id"], []).append(s["tech"])
    span = {}
    for s in segs:
        a, b = span.get(s["job_id"], (None, None))
        span[s["job_id"]] = (min(a or s["start"], s["start"]),
                             max(b or s["end"], s["end"]))

    BLUE = colors.HexColor("#1F4E9C")
    S  = ParagraphStyle("s",  fontName="APP",  fontSize=FS(7.5), leading=FS(10), textColor=BLUE)
    SC = ParagraphStyle("sc", fontName="APP",  fontSize=FS(9),   leading=FS(11), alignment=1,
                        textColor=BLUE)
    H  = ParagraphStyle("h",  fontName="APPB", fontSize=FS(8.5), leading=FS(11), alignment=1)
    T1 = ParagraphStyle("t1", fontName="APPB", fontSize=FS(14),  leading=FS(18), alignment=1)
    T2 = ParagraphStyle("t2", fontName="APPB", fontSize=FS(12),  leading=FS(16), alignment=1)
    SM = ParagraphStyle("sm", fontName="APP",  fontSize=FS(8.5), leading=FS(11))
    SR = ParagraphStyle("sr", fontName="APP",  fontSize=FS(8.5), leading=FS(11), alignment=2)
    NB = ParagraphStyle("nb", fontName="APPB", fontSize=FS(8.5), leading=FS(12),
                        textColor=colors.HexColor("#8A1C1C"))

    dt = datetime.strptime(d, "%Y-%m-%d").date()
    fn = path or archive_path(d)
    os.makedirs(os.path.dirname(fn), exist_ok=True)
    doc = SimpleDocTemplate(fn, pagesize=landscape(A4), leftMargin=10*mm, rightMargin=10*mm,
                            topMargin=7*mm, bottomMargin=6*mm)

    logo = _logo_flowable(17)
    head = Table([[logo or "",
                   [Paragraph(company, T1), Paragraph("รายงานการซ่อมบำรุงประจำวัน", T2)],
                   Paragraph(FORM06_CODE, SR)]],
                 colWidths=[34*mm, None, 34*mm],
                 style=TableStyle([("VALIGN", (0, 0), (0, 0), "MIDDLE"),
                                   ("VALIGN", (1, 0), (1, 0), "MIDDLE"),
                                   ("VALIGN", (2, 0), (2, 0), "MIDDLE"),
                                   ("LEFTPADDING",  (0, 0), (-1, -1), 0),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    meta = Table([[Paragraph(f"แผนก:  {FORM06_DEPT}", SM),
                   Paragraph(f"วันที่: {dt.strftime('%d-%m-%Y')}", SR)]],
                 colWidths=[None, 60*mm],
                 style=TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))

    data = [[Paragraph(h, H) for h in FORM06_HEAD]]
    for j in jobs:
        a, b = span.get(j["id"], (j.get("started_at"), j.get("done_at")))
        when = f"{_hm(a)} - {_hm(b)}" if _hm(a) or _hm(b) else ""
        who = []
        for uid in worked.get(j["id"], []):
            if names.get(uid) and names[uid] not in who:
                who.append(names[uid])
        if j.get("lead_name") and j["lead_name"] not in who:
            who.insert(0, j["lead_name"])
        for x in str(j.get("helpers") or "").split(","):
            if x.strip().isdigit() and names.get(int(x)) and names[int(x)] not in who:
                who.append(names[int(x)])
        symptom = (j.get("problem") or j.get("problem_type")
                   or (j.get("descr") or "").split("\n")[0] or "")
        data.append([Paragraph(str(j.get("mname") or j.get("mcode") or ""), S),
                     Paragraph(when, SC),
                     Paragraph(symptom, S),
                     Paragraph(str(j.get("root_cause") or ""), S),
                     Paragraph(str(j.get("solution") or ""), S),
                     (_tick(4.4*mm, BLUE) if j.get("cleared_worksite") else ""),
                     Paragraph(", ".join(who), S)])
    # the sheet keeps its shape on a quiet day
    for _ in range(max(0, FORM06_ROWS - len(jobs))):
        data.append(["", "", "", "", "", "", ""])

    tbl = Table(data, colWidths=[40*mm, 30*mm, 41*mm, 40*mm, 70*mm, 27*mm, 29*mm],
                rowHeights=[12*mm] + [9.4*mm] * (len(data) - 1),
                repeatRows=1,
                style=TableStyle([("GRID", (0, 0), (-1, -1), .6, colors.black),
                                  ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                  ("TOPPADDING", (0, 0), (-1, -1), 2),
                                  ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))

    sign = Table([[Paragraph("ผู้ทวนสอบ  .............................................................", SR)],
                  [Paragraph("............./................/...............", SR)]],
                 colWidths=[None],
                 style=TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                                   ("TOPPADDING", (0, 0), (-1, -1), 2)]))

    doc.build([head, Spacer(1, 1.5*mm), meta, tbl, Spacer(1, 2.5*mm),
               Paragraph(FORM06_NOTE, NB), Spacer(1, 1.5*mm), sign])
    return fn



# ══════════════════════════════════════════════════════════════════════════════════
#  Several documents from the shelf as ONE file
#  The PM checklist shelf could always open a stack of sheets as one PDF; the other three
#  shelves could only open one document at a time. They share the same "tick the rows,
#  open N" now. PDFs are joined page by page (pypdf, shipped in server/_vendor so the live
#  server needs nothing installed); plan sheets are HTML and are joined into one page.
# ══════════════════════════════════════════════════════════════════════════════════

def _pdf_writer():
    try:
        from pypdf import PdfWriter
    except ImportError:
        import sys
        vend = os.path.join(os.path.dirname(__file__), "_vendor")
        if vend not in sys.path:
            sys.path.insert(0, vend)
        from pypdf import PdfWriter
    return PdfWriter()


def _shelf_rows(c, ids, u):
    """plan_reports rows the caller may open: this session's plant only."""
    fac = u.get("active_factory") or u.get("factory_id")
    ph = ",".join("?" * len(ids))
    rows = {r["id"]: dict(r) for r in c.execute(
        f"SELECT * FROM plan_reports WHERE id IN ({ph}) AND factory_id=?", (*ids, fac))}
    return [rows[i] for i in ids if i in rows]


def _row_pdf(c, r, u):
    """The filed PDF behind one shelf row, building it only if it was never filed."""
    import re as _re
    kind = r.get("kind") or "plan"
    if kind == "done":
        fn = archive_found(r["plan_date"], r["factory_id"])
        if not fn and _done_rows(r["plan_date"], r["factory_id"]):
            fn = file_daily(r["plan_date"], {**u, "active_factory": r["factory_id"]})
        return fn
    if kind == "jobform":
        m = _re.search(r"/api/jobs/(\d+)/pdf", r.get("path") or "")
        if not m:
            return ""
        jid = int(m.group(1))
        job = c.execute("SELECT id, factory_id FROM jobs WHERE id=?", (jid,)).fetchone()
        if not job or (job["factory_id"] and job["factory_id"] != r["factory_id"]):
            return ""
        from .jobform import file_job_form
        return file_job_form(jid, u)
    return ""


@router.get("/reports/bundle")
async def shelf_bundle(req: Request, ids: str = "", inline: str = ""):
    """Open several shelf documents as one file, in the order they were ticked."""
    u = user_from(req)
    want = []
    for x in str(ids or "").split(","):
        if x.strip().isdigit() and int(x) not in want:
            want.append(int(x))
    if not want:
        raise HTTPException(400, "no documents selected")
    if len(want) > 120:
        raise HTTPException(400, "too many documents in one file (max 120)")
    with closing(db()) as c:
        rows = _shelf_rows(c, want, u)
        if not rows:
            raise HTTPException(404, "none of those documents belong to this plant")
        kinds = {r.get("kind") or "plan" for r in rows}
        if len(kinds) > 1:
            raise HTTPException(400, "pick documents of one kind at a time")
        kind = kinds.pop()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        if kind == "plan":
            from fastapi.responses import HTMLResponse
            from .config import UPLOADS
            html, n = "", 0
            for r in rows:
                p = os.path.join(UPLOADS, str(r.get("path") or "").replace("/uploads/", "", 1))
                if not os.path.isfile(p):
                    continue
                t = open(p, encoding="utf-8").read()
                if not html:
                    html = t
                else:
                    i = t.find('<div class="sheet">')
                    html += t[i:] if i >= 0 else ""
                n += 1
            if not html:
                raise HTTPException(404, "the plan files were not found")
            return HTMLResponse(html)
        files = []
        for r in rows:
            try:
                fn = _row_pdf(c, r, u)
            except Exception as e:
                _log.warning("[shelf] could not produce document %s: %s", r.get("id"), e)
                fn = ""
            if fn and os.path.isfile(fn):
                files.append(fn)
    if not files:
        raise HTTPException(404, "none of the selected documents could be found")
    if len(files) == 1:
        return _pdf_response(files[0], inline, os.path.basename(files[0]))
    w = _pdf_writer()
    for fn in files:
        w.append(fn)
    name = ("Daily_reports" if kind == "done" else "Repair_forms") + f"_{len(files)}_{stamp}.pdf"
    out = os.path.join(_outdir(datetime.now().date()), "_bundles")
    os.makedirs(out, exist_ok=True)
    # a bundle is a view, not a record: keep only the last few so the folder never grows
    try:
        old = sorted((os.path.join(out, f) for f in os.listdir(out)), key=os.path.getmtime)
        for f in old[:-10]:
            os.remove(f)
    except OSError:
        pass
    path = os.path.join(out, name)
    with open(path, "wb") as fh:
        w.write(fh)
    return _pdf_response(path, inline, name)


# ══════════════════════════════════════════════════════════════════════════════════
#  The daily report files itself at 23:59, for every plant
#  Until now a day was only reported if somebody pressed "Issue today". The server now
#  files each plant's F-SP-ENG02-06 at 23:59 from that day's completed work. A plant
#  that finished nothing gets nothing (no blank sheet, same rule as the button). If the
#  server was off at 23:59, the days it missed — up to a week back — are filed when it
#  starts. A day somebody already issued by hand is refreshed in place, not duplicated.
# ══════════════════════════════════════════════════════════════════════════════════
AUTO_USER = {"id": None, "name": "Auto 23:59", "role": "admin"}
AUTO_AT = (23, 59)
AUTO_CATCHUP_DAYS = 7


def auto_file_day(d, only_missing=False):
    """File day d for every plant with completed work. Returns [(plant code, jobs)]."""
    from .db import state_get, state_set
    out = []
    with closing(db()) as c:
        facs = [(r["id"], r["code"]) for r in c.execute("SELECT id, code FROM factories ORDER BY id")]
    for fid, code in facs:
        try:
            rows = _done_rows(d, fid)
            if not rows:
                continue
            key = f"autodaily:{d}:{fid}"
            if only_missing:
                with closing(db()) as c:
                    done_before = state_get(c, key) or archive_found(d, fid)
                if done_before:
                    continue
            file_daily(d, {**AUTO_USER, "active_factory": fid})
            with closing(db()) as c:
                state_set(c, key, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                c.commit()
            out.append((code, len(rows)))
        except Exception as e:
            _log.error("[auto-daily] %s %s failed: %s", code, d, e)
    return out


def auto_catch_up():
    from datetime import timedelta
    today_d = datetime.now().date()
    filed = []
    for k in range(AUTO_CATCHUP_DAYS, 0, -1):
        d = (today_d - timedelta(days=k)).isoformat()
        for code, n in auto_file_day(d, only_missing=True):
            filed.append(f"{code} {d} ({n})")
    if filed:
        _log.info("[auto-daily] caught up missed days: %s", ", ".join(filed))


def seconds_to_next_run(now_dt=None):
    from datetime import timedelta
    now_dt = now_dt or datetime.now()
    run = now_dt.replace(hour=AUTO_AT[0], minute=AUTO_AT[1], second=0, microsecond=0)
    if run <= now_dt:
        run += timedelta(days=1)
    return (run - now_dt).total_seconds()


@router.get("/reports/auto-status")
async def auto_status(req: Request):
    """For the shelf header: when this plant's report last filed itself, and when next."""
    from .db import state_get
    u = user_from(req)
    fac = u.get("active_factory") or u.get("factory_id")
    with closing(db()) as c:
        from .db import ensure_app_state
        ensure_app_state(c)
        r = c.execute("SELECT k, v FROM app_state WHERE k LIKE ? ORDER BY v DESC LIMIT 1",
                      (f"autodaily:%:{fac}",)).fetchone()
    last = {"day": r["k"].split(":")[1], "at": r["v"]} if r else None
    return {"at": "%02d:%02d" % AUTO_AT, "last": last,
            "next_in_min": int(seconds_to_next_run() // 60)}

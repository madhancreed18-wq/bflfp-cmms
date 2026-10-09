"""Daily shift-plan report as a shareable PNG (for LINE).

Renders the planner's assigned jobs + free-text task lines into a table that
mirrors the manual Excel sheet, auto-filling the team's photos (matched by Thai
name from static/techphotos/) and names.
"""
import os
import re
import datetime
from contextlib import closing

from fastapi import APIRouter, Request, HTTPException

from .config import STATIC, UPLOADS
from .db import db, today, now, user_names, own_account_map, factory_company
from .auth import user_from

import logging
# the app's own logger — the file handler is attached by logs.setup(), and
# everything written here shows in Manage → Logs. print() does not: it goes to
# a console nobody is watching, which is where these messages used to die.
_log = logging.getLogger("cmms")

router = APIRouter(prefix="/api/plan")

PHOTO_DIR = os.path.join(STATIC, "techphotos")
TITLES = ("นางสาว", "น.ส.", "ว่าที่ร้อยตรี", "ว่าที่", "นาย", "นาง")

_FONTS = [r"C:\Windows\Fonts\tahoma.ttf", r"C:\Windows\Fonts\leelawad.ttf",
          r"C:\Windows\Fonts\LeelawUI.ttf", "/usr/share/fonts/truetype/tlwg/Garuda.ttf"]
_FONTS_B = [r"C:\Windows\Fonts\tahomabd.ttf", r"C:\Windows\Fonts\leelawdb.ttf",
            r"C:\Windows\Fonts\LeelaUIb.ttf", "/usr/share/fonts/truetype/tlwg/Garuda-Bold.ttf"]


def _norm(s):
    """Normalise a Thai name for matching: drop title + (nickname) + spaces."""
    s = re.sub(r"\(.*?\)", "", s or "").strip()
    for t in TITLES:
        if s.startswith(t):
            s = s[len(t):]
            break
    return re.sub(r"\s+", "", s).lower()


def _core(s):
    return re.sub(r"\(.*?\)", "", s or "").strip()


def _find_photo(name):
    try:
        tgt = _norm(name)
        if not tgt or not os.path.isdir(PHOTO_DIR):
            return None
        for fn in os.listdir(PHOTO_DIR):
            base, ext = os.path.splitext(fn)
            if ext.lower() in (".jpg", ".jpeg", ".png") and _norm(base) == tgt:
                return os.path.join(PHOTO_DIR, fn)
    except Exception:
        pass
    return None


def _font(size, bold=False):
    from PIL import ImageFont
    for p in (_FONTS_B if bold else _FONTS):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _wrap(draw, text, font, maxw):
    """Character-aware wrap (Thai has no spaces between words)."""
    out, cur = [], ""
    for ch in str(text):
        if draw.textlength(cur + ch, font=font) <= maxw or not cur:
            cur += ch
        else:
            out.append(cur)
            cur = ch
    if cur:
        out.append(cur)
    return out or [""]


def _render(d, shift, plan, team, planner, plant="BFLFP"):
    from PIL import Image, ImageDraw
    W = 1000
    ft = _font(26, True)
    fh = _font(16, True)
    f = _font(15)
    fnm = _font(14)
    try:
        dt = datetime.date.fromisoformat(d)
        dstr = "%d/%d/%d" % (dt.day, dt.month, dt.year + 543)
    except Exception:
        dstr = d

    tmp = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    plan_wrapped = [_wrap(tmp, "%d. %s" % (i, ln), f, W - 60) for i, ln in enumerate(plan, 1)]
    plan_h = sum(len(x) for x in plan_wrapped) * 24 + 12

    per_row = 4
    cell_w = (W - 44) // per_row
    photo = 92
    prows = (len(team) + per_row - 1) // per_row if team else 0
    team_h = prows * (photo + 40) + 8

    H = 54 + 42 + 34 + plan_h + 40 + team_h + 30
    img = Image.new("RGB", (W, H), "white")
    dr = ImageDraw.Draw(img)

    title = "แผนงานประจำวันช่างกะ %s   วันที่ %s" % (plant, dstr)
    dr.text(((W - dr.textlength(title, font=ft)) / 2, 14), title, font=ft, fill="#1a1a1a")

    y = 54
    dr.rectangle([0, y, W - 1, y + 32], fill="#C6E0B4", outline="#70AD47")
    dr.text((14, y + 8), "วันที่ %s      สถานที่ %s      เวลา %s      จ่ายงาน %s"
            % (dstr, plant, shift, planner),
            font=fh, fill="#375623")
    y += 42

    dr.text((14, y), "แผนงาน", font=fh, fill="#70AD47")
    y += 26
    for wl in plan_wrapped:
        for seg in wl:
            dr.text((22, y), seg, font=f, fill="#1a1a1a")
            y += 24
    y += 16

    dr.text((14, y), "ผู้รับผิดชอบ / รายชื่อทีมงาน", font=fh, fill="#70AD47")
    y += 28

    for idx, (tid, nm) in enumerate(team):
        col = idx % per_row
        row = idx // per_row
        cx = 22 + col * cell_w
        cy = y + row * (photo + 40)
        px = cx + (cell_w - photo) // 2 - 20
        p = _find_photo(nm)
        drawn = False
        if p:
            try:
                im = Image.open(p).convert("RGB").resize((photo, photo))
                img.paste(im, (px, cy))
                drawn = True
            except Exception:
                drawn = False
        if not drawn:
            dr.rectangle([px, cy, px + photo, cy + photo], fill="#E0F2FE")
            ini = (_core(nm)[:1] or "?")
            dr.text((px + (photo - dr.textlength(ini, font=ft)) / 2, cy + photo / 2 - 18), ini, font=ft, fill="#075985")
        dr.rectangle([px, cy, px + photo, cy + photo], outline="#70AD47")
        ny = cy + photo + 4
        for nl in _wrap(dr, nm, fnm, cell_w - 8)[:2]:
            dr.text((cx, ny), nl, font=fnm, fill="#1a1a1a")
            ny += 16

    dr.text((14, H - 22), "%s CMMS" % plant, font=fnm, fill="#94A3B8")

    fn = "plan_%s.png" % d
    img.save(os.path.join(UPLOADS, fn))
    return fn


@router.post("/report")
async def plan_report(req: Request):
    u = user_from(req)
    b = await req.json()
    d = b.get("date") or today()
    tasks = b.get("tasks") or []
    shift = b.get("time") or "08.00-20.00"
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT j.jobid, j.jobtype, j.descr, j.lead_tech, j.helpers, m.code mcode, m.name mname, "
            "j.asset_text, j.report_name "
            "FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id "
            "WHERE j.planned_date=? AND j.status IN "
            "('Assigned','InProgress','Paused','Rework','Hold') "
            "ORDER BY j.priority DESC, j.id", (d,))]
        names = user_names(c)
    team_ids = []
    for r in rows:
        for tid in [r["lead_tech"]] + str(r["helpers"] or "").split(","):
            s = str(tid).strip()
            if s.isdigit() and int(s) not in team_ids:
                team_ids.append(int(s))
    team = [(tid, names.get(str(tid), "")) for tid in team_ids]
    plan = []
    for r in rows:
        mn = (r["mname"] or "").split("(")[0].strip() or (r["asset_text"] or r["report_name"] or "").strip()
        plan.append(("%s  %s %s — %s" % (r["jobid"], r["mcode"] or "", mn, r["descr"] or "")).strip())
    plan += [str(t) for t in tasks if str(t).strip()]
    # the plant this sheet is for — it used to say BFLFP on every plant's paper
    _fc = u.get("active_factory") or u.get("factory_id")
    try:
        with closing(db()) as _c:
            _fr = _c.execute("SELECT code FROM factories WHERE id=?", (_fc,)).fetchone()
        _plant = (_fr["code"] if _fr else "") or "BFL"
    except Exception:
        _plant = "BFL"
    try:
        fn = _render(d, shift, plan, team, u["name"], _plant)
    except Exception as e:
        raise HTTPException(500, "render failed: %s" % e)
    return {"url": "/uploads/%s" % fn, "jobs": len(rows), "team": len(team)}


# ── The daily plan, as an issued document ────────────────────────────────────────
# Rendered on the server, not the phone: a plan is a record, and a record has to be
# reproducible and the same for everyone who opens it. The file is written once and
# never regenerated — open one from three weeks ago and it still says what the plan
# said that morning, not what the jobs did afterwards.

DR_DIR = os.path.join(UPLOADS, "planreports")
os.makedirs(DR_DIR, exist_ok=True)

TH_MON = ["", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
          "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]
TH_DOW = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
DR_TYPE = {"BD": ("#FEE2E2", "#991B1B"), "CM": ("#DBEAFE", "#1E40AF"),
           "PM": ("#DBEAFE", "#2563EB"), "IMP": ("#F1EAFB", "#5B21B6"),
           "PRJ": ("#E0F2FE", "#0369A1")}
DR_PRIO = {3: ("ด่วนมาก", "#FEE2E2", "#991B1B"), 2: ("ด่วน", "#FEF3C7", "#B45309"),
           1: ("ปกติ", "#F1F5F9", "#64748B")}
DR_TRADE = {"Mechanical": ("เครื่องกล / Mech", "#0E7A70"),
            "Electrical": ("ไฟฟ้า / Elec", "#B45309"),
            "Instrument": ("เครื่องมือวัด / Inst", "#3364C4"),
            "Process": ("กระบวนการ / Proc", "#7C3AED")}
DR_COL = ["#2563EB", "#EA6A0A", "#16A34A", "#7C3AED", "#0891B2", "#DB2777"]
DR_ICON = ('<svg class="av df" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
           ' stroke-width="1.4"><circle cx="12" cy="8.6" r="3.6"/>'
           '<path d="M4.6 20c0-3.6 3.3-5.6 7.4-5.6s7.4 2 7.4 5.6"/></svg>')


def _e(x):
    return (str(x if x is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _fill(tpl, ctx):
    """Substitute @@name@@ tokens. Not %-formatting: the template is CSS, and CSS is
    full of literal per-cent signs that %-formatting would try to read as tokens."""
    for k, v in ctx.items():
        tpl = tpl.replace("@@%s@@" % k, str(v))
    return tpl


def _thdate(iso):
    try:
        d = datetime.date.fromisoformat(iso)
        return "%s %d %s %d" % (TH_DOW[d.weekday()], d.day, TH_MON[d.month], d.year + 543)
    except Exception:
        return iso


def _trade(job, cats):
    c = cats.get(job.get("problem_type") or "") or (job.get("fault_category") or "")
    return DR_TRADE.get(c, ("", ""))


def _dr_rows(jobs, crew_of, cats, start=1):
    out = []
    for n, j in enumerate(jobs, start):
        t = str(j.get("jobtype") or "CM").upper()
        tb, tf = DR_TYPE.get(t, ("#F1F5F9", "#64748B"))
        pl, pb, pf = DR_PRIO.get(int(j.get("priority") or 1), DR_PRIO[1])
        tl, tc = _trade(j, cats)
        cw = crew_of.get(str(j["id"]))
        cname, ccol = (cw or ("— ไม่มีทีม", "#DC2626"))
        carry = ""
        if j.get("carry_from"):
            try:
                d = datetime.date.fromisoformat(j["carry_from"])
                carry = ' <span class="cy">↤%d %s</span>' % (d.day, TH_MON[d.month][:3] + ".")
            except Exception:
                carry = ' <span class="cy">↤</span>'
        # b393: "Other — described below" is not work anybody can do; what was described
        # below is. Any symptom from the list is shown as it is.
        _pt = (j.get("problem_type") or "").strip()
        _ds = (j.get("descr") or "").split("\n")[0].strip()
        _other = str(cats.get(_pt) or "").lower() == "other" or _pt.startswith("อื่น")
        work = ((_ds or _pt) if (_other or not _pt) else _pt).strip()
        if not work:
            # a PM order carries no symptom — it was never a fault. Say what it is, so the
            # line is not blank on a sheet somebody has to work from.
            work = "PM ตามแผน" if t == "PM" else "—"
        out.append(
            '<tr class="%s"><td class="c">%d</td>'
            '<td><span class="tm" style="background:%s"></span>%s</td>'
            '<td class="c"><span class="tag" style="background:%s;color:%s">%s</span></td>'
            '<td class="jid">%s%s</td><td class="ast">%s</td><td>%s</td><td>%s</td>'
            '<td class="c">%s</td>'
            '<td class="c"><span class="pr" style="background:%s;color:%s">%s</span></td>'
            '<td></td></tr>' % (
                "none" if not cw else "", n, ccol, _e(cname), tb, tf, _e(t),
                _e(j.get("jobid") or ""), carry, _e(j.get("mcode") or ""),
                # b393: a job on something not in the register keeps what was typed for it
                _e((j.get("mname") or "").split("(")[0].strip()
                   or (j.get("asset_text") or j.get("report_name") or "").strip()), _e(work),
                ('<span class="dept" style="background:%s1A;color:%s">%s</span>' % (tc, tc, tl)) if tl else "",
                pb, pf, pl))
    return "".join(out)


def _dr_carry(jobs):
    """The block that names yesterday's unfinished work.

    Carried jobs are already marked ↤ in the table, but they are scattered through it
    in priority order, and a crew reading a sheet at 07:00 cannot see at a glance what
    came from yesterday. So they are also listed together, shortest possible form: job
    number, the day it was first promised, and — the number that matters — how many
    times it has now slipped. A job on its fourth carry is not a scheduling detail.

    It stays to one or two lines whatever the day looks like: the list is capped and
    the rest is a count, because the signature block is pinned to the bottom line of
    the paper and a block that grows would push it off.
    """
    car = [j for j in jobs if j.get("carry_from")]
    if not car:
        return ""
    car.sort(key=lambda j: (-int(j.get("carryover") or 0), j.get("carry_from") or ""))
    CAP = 8
    chips = []
    for j in car[:CAP]:
        try:
            d = datetime.date.fromisoformat(j["carry_from"])
            when = "%d %s" % (d.day, TH_MON[d.month][:3] + ".")
        except Exception:
            when = j.get("carry_from") or ""
        n = int(j.get("carryover") or 0)
        chips.append('<span class="cychip">%s <i>↤%s</i>%s</span>'
                     % (_e(j.get("jobid") or j.get("mcode") or ""), _e(when),
                        (" <b>×%d</b>" % n) if n > 1 else ""))
    more = len(car) - len(chips)
    return ('<div class="note"><b>↤ ยกยอดมาจากวันก่อน / Carried forward — %d งาน</b> '
            'ต้องปิดให้ได้ในวันนี้<div class="cyrow">%s%s</div></div>'
            % (len(car), "".join(chips),
               ('<span class="cychip more">+%d</span>' % more) if more > 0 else ""))


def _dr_crews(teams, people, photos):
    out = []
    for t in teams:
        mem = t.get("members") or []
        tiles = []
        for uid in mem:
            nm = people.get(uid, {}).get("name", "#%s" % uid)
            dept = people.get(uid, {}).get("department", "") or ""
            ph = photos.get(uid) or ""
            av = ('<img class="av" src="%s">' % _e(ph)) if ph else DR_ICON
            tiles.append('<div class="pp">%s<div class="nm">%s%s</div><div class="rl">%s</div></div>'
                         % (av, "★ " if t.get("lead") == uid else "", _e(nm), _e(dept)))
        if not tiles:
            tiles = ['<span style="font-size:10px;color:#94A3B8;padding:18px 0;display:block">'
                     'ยังไม่มีช่างในทีมนี้ · no technicians yet</span>']
        hrs = " · ".join(x for x in [(("%s – %s" % (t.get("start"), t.get("end")))
                                      if t.get("start") and t.get("end") else ""),
                                     "%d คน" % len(mem)] if x)
        out.append('<div class="cw"><div class="cwh"><span class="tm" style="background:%s"></span>'
                   '<b>%s</b><span class="ph">%s%s</span></div><div class="ppl">%s</div></div>'
                   % (t.get("color") or "#2563EB", _e(t.get("name") or "Team"),
                      # Blanking the crew handset was not enough on its own: an empty
                      # login_name fell through to "— ยังไม่ผูกโทรศัพท์" (no phone linked),
                      # so a BFL crew whose members each carry their own phone printed a
                      # warning saying none of them had one. The flag has to be checked
                      # BEFORE the name, not instead of it.
                      "" if t.get("self_login") else
                      (("📱 " + _e(t.get("login_name"))) if t.get("login_name")
                       else "— ยังไม่ผูกโทรศัพท์"),
                      (" · " + hrs) if hrs else "", "".join(tiles)))
    return '<div class="crews">%s</div>' % "".join(out) if out else ""


# ══════════════════════════════════════════════════════════════════════════════════
#  ONE DAY, ONE SET OF NUMBERS
#  The plan sheet and the completed-work sheet are the same day seen twice — in the
#  morning and at night. They must therefore count the same jobs, or the two pieces of
#  paper contradict each other on the wall. So both take their figures from here.
#
#  A day's work is everything that was on the floor that day:
#    · planned for the day,
#    · carried over from an earlier day and still open,
#    · finished on the day, whichever day it was planned for.
#  Every figure printed is a slice of that one set, written n / N, and the four status
#  slices add up to N exactly.
#
#  Job types, as the plant names them:
#    PM  งานตามแผน            preventive, on the schedule
#    CM  ซ่อมแก้ไข             corrective — the head, and under it:
#        MT  ใบแจ้งซ่อม        a repair request from the floor        (stored as CM)
#        BD  เครื่องเสีย        a breakdown, the machine has stopped   (stored as BD)
# ══════════════════════════════════════════════════════════════════════════════════

DAY_OPEN   = ("Reported", "WaitingApproval", "WaitingAssignment", "Assigned",
              "Released", "Rework")
DAY_GONE   = ("Cancelled", "Rejected")
JOB_COLS   = """SELECT j.id,j.jobid,j.jobtype,j.status,j.priority,j.descr,j.problem,
                       j.problem_type,j.fault_category,j.root_cause,j.solution,
                       j.cleared_worksite,j.planned_date,j.started_at,j.done_at,
                       j.lead_tech,j.helpers,j.approver_id,
                       j.carryover,j.planned_date_orig, m.code mcode, m.name mname,
                       j.asset_text, j.report_name
                FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id"""


def _day_jobs(c, fac, d):
    """Every job that was on this plant's floor on day d, each one once."""
    seen, out = set(), []

    def take(rows, **extra):
        for r in rows:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            row = dict(r)
            row.update(extra)
            out.append(row)

    fx = " AND (COALESCE(m.factory_id,j.factory_id)=? OR COALESCE(m.factory_id,j.factory_id) IS NULL)"
    take(c.execute(JOB_COLS + " WHERE j.planned_date=?" + fx
                   + " AND j.status NOT IN ('Cancelled','Rejected')"
                   + " ORDER BY j.priority DESC, j.jobtype, j.id", (d, fac)))
    # still open from an earlier day — it is on today's floor whatever the plan says
    take(c.execute(JOB_COLS + " WHERE j.planned_date < ? AND COALESCE(j.planned_date,'')<>''"
                   + fx + " AND j.status IN (%s)" % ",".join("?" * len(DAY_OPEN))
                   + " ORDER BY j.planned_date DESC, j.priority DESC",
                   (d, fac, *DAY_OPEN)), carried=True)
    # finished today, whichever day it was planned for — otherwise the work would be
    # counted as done on a sheet that never listed it
    take(c.execute(JOB_COLS + " WHERE substr(COALESCE(j.done_at,''),1,10)=?" + fx
                   + " AND j.status IN ('Done','ServiceCompleted')"
                   + " ORDER BY j.done_at", (d, fac)), carried=True)
    for j in out:
        if j.get("carried") and (j.get("planned_date") or "") < d:
            j["carry_from"] = j.get("planned_date") or ""
    return out


def _day_counts(jobs):
    """The one set of figures both sheets print. The four status slices total N."""
    n = len(jobs)
    st = lambda *s: sum(1 for j in jobs if j.get("status") in s)
    ty = lambda *t: sum(1 for j in jobs if str(j.get("jobtype") or "").upper() in t)
    crewed = sum(1 for j in jobs if j.get("lead_tech"))
    c = {"n": n,
         "done": st("Done"),                       # accepted and closed
         "accept": st("ServiceCompleted"),         # repaired, waiting for the operator
         "doing": st("InProgress", "Paused", "Hold"),
         "pm": ty("PM"), "mt": ty("CM"), "bd": ty("BD"),
         "crewed": crewed, "nocrew": n - crewed,
         "carried": sum(1 for j in jobs if j.get("carry_from"))}
    c["cm"] = c["mt"] + c["bd"]                    # CM is the head: MT + BD
    c["open"] = n - c["done"] - c["accept"] - c["doing"]   # not started yet
    return c


def _file_counts(c, fac, d, kind, k):
    """Store on the filed row the same figures the sheet printed, so the report list
    can show them without re-deriving a day that has since moved on."""
    try:
        c.execute("""UPDATE plan_reports SET done_n=?, open_n=?, accept_n=?, doing_n=?,
                     pm_n=?, cm_n=? WHERE id=(SELECT MAX(id) FROM plan_reports
                     WHERE factory_id=? AND plan_date=? AND COALESCE(kind,'plan')=?)""",
                  (k["done"], k["open"], k["accept"], k["doing"], k["pm"], k["cm"],
                   fac, d, kind))
    except Exception:
        pass          # an older database without the columns still files the report


def _frac(v, n):
    return "%d <span class=\"of\">/ %d</span>" % (v, n) if n else "%d" % v


def _strip(k, future=False):
    """The figure strip at the top of both sheets.

    A plan issued for a day that has not begun has no progress to report — every job
    would read "not started", which tells the reader nothing. So a future day's strip
    speaks the planner's language instead: how much work, how much of it has a crew,
    and the PM/CM split. The day itself (and any past day) keeps the progress strip,
    because by then the figures mean something.
    """
    if future:
        return (
          '<div class="st"><b>%d</b>งานทั้งหมดในแผน · Planned jobs'
          '<span class="sub">%s</span></div>'
          '<div class="st"><b style="color:#0E7A70">%s</b>มอบหมายแล้ว · Assigned'
          '<span class="sub">มีทีมรับผิดชอบแล้ว</span></div>'
          '<div class="st"><b style="color:#DC2626">%s</b>ยังไม่มอบหมาย · Not assigned'
          '<span class="sub">จัดทีมก่อนเริ่มงาน</span></div>'
          '<div class="st" style="flex:1.5"><b style="color:#123A63">PM %s</b>'
          'CM %s <span class="of">·</span> ใบแจ้งซ่อม %d <span class="of">·</span> เครื่องเสีย %d'
          '<span class="sub">PM ตามแผน · CM ซ่อมแก้ไข (MT + BD)</span></div>'
          % (k["n"],
             ("มีงานค้างจากวันก่อน %d" % k["carried"]) if k["carried"] else "แผนงานของวันนี้",
             _frac(k["crewed"], k["n"]), _frac(k["nocrew"], k["n"]),
             _frac(k["pm"], k["n"]), _frac(k["cm"], k["n"]), k["mt"], k["bd"]))
    return (
      '<div class="st"><b>%d</b>งานทั้งหมดวันนี้ · Total on the floor'
      '<span class="sub">%s</span></div>'
      '<div class="st"><b style="color:#0E7A70">%s</b>เสร็จแล้ว · Completed'
      '<span class="sub">ตรวจรับและปิดงาน</span></div>'
      '<div class="st"><b style="color:#B45309">%s</b>รอตรวจรับ · Waiting to accept'
      '<span class="sub">ซ่อมเสร็จ รอผู้แจ้งเซ็น</span></div>'
      '<div class="st"><b style="color:#1B75BC">%s</b>กำลังทำ · In progress'
      '<span class="sub">ช่างกด Start แล้ว</span></div>'
      '<div class="st"><b style="color:#334155">%s</b>ยังไม่เริ่ม · Not started'
      '<span class="sub">%s</span></div>'
      '<div class="st" style="flex:1.5"><b style="color:#123A63">PM %s</b>'
      'CM %s <span class="of">·</span> ใบแจ้งซ่อม %d <span class="of">·</span> เครื่องเสีย %d'
      '<span class="sub">PM ตามแผน · CM ซ่อมแก้ไข (MT + BD)</span></div>'
      % (k["n"], ("มีงานค้างจากวันก่อน %d" % k["carried"]) if k["carried"] else "งานของวันนี้ทั้งหมด",
         _frac(k["done"], k["n"]), _frac(k["accept"], k["n"]), _frac(k["doing"], k["n"]),
         _frac(k["open"], k["n"]),
         ("ยังไม่มอบหมาย %d" % k["nocrew"]) if k["nocrew"] else "มอบหมายครบทุกงาน",
         _frac(k["pm"], k["n"]), _frac(k["cm"], k["n"]), k["mt"], k["bd"]))


DR_ROWS_CONT = 22     # job rows on a page that carries nothing else — measured
# A page that also carries the crew block and the signatures holds fewer, and HOW MANY
# fewer depends on how tall the crew block is. That used to be a single number, 12, and
# it was wrong in both directions: with two crews the sheet really holds 15, so every
# plan threw away three lines a page; with eight crews it holds 10, so the signature was
# being pushed off the paper. Now it is worked out.
#
# Every constant below was measured in a browser at A4 landscape, not estimated, and the
# arithmetic reproduces all 56 measured combinations exactly or one row to the safe side.
DR_SHEET_ROOM = 430   # px left for job rows on a last page, before the crew block
DR_ROW_PX = 22        # one job line
DR_CREW_LINE = 108    # one line of crew cards holding one row of technician tiles
DR_TILE_ROW = 83      # each further row of technician tiles inside a card
DR_NOTE_PX = 44       # the carry-over note, when there is one
DR_CARDS_PER_LINE = 4       # what the CSS wraps at
DR_TILES_PER_CARD = 3       # technician tiles across a card on a four-up line
DR_ROWS_LAST_MIN = 0        # a dozen crews can leave no room for job rows at all, and
                            # forcing some on anyway is how a signature block ends up
                            # off the paper. At zero the closing sheet carries the crews
                            # and the signatures alone, which is honest and still prints.
DR_ROWS_LAST = 12           # the old fixed figure, kept only as a safe fallback for a
                            # caller that does not say how many crews the day has


def _dr_crew_px(teams):
    """How tall the crew block prints, in pixels.

    Cards wrap four to a line. A line carrying four cards is narrow enough that a team of
    more than three technicians spills onto a second row of tiles inside its card; the
    last line, if it is not full, is wider and never does.

    Returns (pixels, lines).
    """
    n = len(teams or [])
    if not n:
        return 0, 0
    full, tail = divmod(n, DR_CARDS_PER_LINE)
    lines = full + (1 if tail else 0)
    widest = max((len(t.get("members") or []) for t in teams), default=0)
    tile_rows = max(1, -(-widest // DR_TILES_PER_CARD))
    tall = DR_CREW_LINE + (tile_rows - 1) * DR_TILE_ROW
    return full * tall + (DR_CREW_LINE if tail else 0) + (lines - 1) * 8, lines


def _dr_rows_last(teams, has_note=False):
    """Job rows left on the sheet that also carries the crews and the signatures."""
    px, lines = _dr_crew_px(teams)
    if has_note:
        px += DR_NOTE_PX
        # once the crew block wraps to a third line the note's own chips wrap too and it
        # costs a further row. Measured, not assumed.
        if lines >= 3:
            px += DR_ROW_PX
    return max(DR_ROWS_LAST_MIN, (DR_SHEET_ROOM - px) // DR_ROW_PX)

DR_CSS = """<!doctype html><meta charset="utf-8"><title>@@title@@</title>
<style>
 /* TH Sarabun PSK — the same face the filed PDFs are set in — served from the app's own
    static folder. It replaces a Google Fonts <link> for two reasons. This sheet is
    printed on a plant LAN that may have no route to the internet, where that link
    silently fell back to whatever system-ui happened to be, so the plan sheet and the
    forms filed beside it were already coming out in two different faces. And a
    controlled document should not depend on a typeface arriving over the wire at print
    time. The local() names come first, so a machine with the font installed uses it
    without downloading anything. */
 @font-face{font-family:'THSarabun';font-weight:400;font-style:normal;font-display:swap;
   src:local('TH Sarabun PSK'),local('TH SarabunPSK'),local('TH Sarabun New'),
       url('/fonts/THSarabunPSK.ttf') format('truetype'),
       url('/fonts/THSarabunNew.ttf') format('truetype')}
 @font-face{font-family:'THSarabun';font-weight:700;font-style:normal;font-display:swap;
   src:local('TH Sarabun PSK Bold'),local('TH SarabunPSK Bold'),local('TH Sarabun New Bold'),
       url('/fonts/THSarabunPSK Bold.ttf') format('truetype'),
       url('/fonts/THSarabunNew Bold.ttf') format('truetype')}
 @page{size:A4 landscape;margin:8mm}
 *{box-sizing:border-box}
 /* Sarabun sets smaller than the face this sheet was laid out against, so the document
    is scaled once here rather than by touching every rule — the sheet is a fixed
    281×193mm box and its proportions must not move. */
 body{margin:0;font-family:'THSarabun','TH Sarabun PSK',system-ui,sans-serif;
      font-size:1.3em;color:#1E293B;background:#E9EDF2}
 /* Every sheet is exactly the printable box. Fixed height is what lets the signature
    sit on the bottom line of the paper instead of wherever the last job happened to
    end — margin-top:auto pushes it down, and the padded rows hold the table's shape. */
 .sheet{width:281mm;height:193mm;padding:5mm 6mm;margin:0 auto;background:#fff;
        display:flex;flex-direction:column;overflow:hidden}
 .sheet+.sheet{page-break-before:always}
 .hd{display:flex;align-items:center;gap:13px;border-bottom:2.5px solid #123A63;padding-bottom:7px;flex:none}
 .hd img{height:44px;width:auto;flex:none}
 .hd h1{margin:0;font-size:17px;font-weight:600}
 .hd .sub{font-size:11.5px;color:#5C789A;margin-top:1px}
 .hd .rt{margin-left:auto;text-align:right;font-size:11px;color:#5C789A;line-height:1.6}
 .hd .rt b{color:#1E293B;font-size:12.5px}
 .strip{display:flex;gap:7px;padding:6px 0;flex:none}
 .st{flex:1;border:1px solid #E5EAF0;border-radius:7px;padding:4px 7px;font-size:9.5px;color:#5C789A}
 .st b{display:block;font-size:16px;line-height:1.15;font-weight:600;color:#1E293B}
 .st .of{font-size:11px;font-weight:500;color:#94A3B8}
 .st .sub{display:block;font-size:8px;color:#A3B4C6;line-height:1.25}
 table{width:100%;border-collapse:collapse;flex:none}
 th,td{border:1px solid #B9C4D2;padding:3px 7px;font-size:10.5px;line-height:1.3;height:22px;vertical-align:middle}
 th{background:#EDF1F6;font-weight:600;font-size:9.5px;text-align:center;padding:5px 6px;height:auto}
 th span{font-weight:400;color:#5C789A;font-size:8.5px}
 td{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:0}
 td.c{text-align:center}
 tbody tr:nth-child(even){background:#FAFBFD}
 tr.none{background:#FFF5F5}
 .tm{width:8px;height:8px;border-radius:2px;display:inline-block;margin-right:5px}
 .cy{color:#9A3412;font-weight:400;font-size:9.5px}
 .tag{display:inline-block;font-size:9px;font-weight:700;padding:1px 5px;border-radius:4px}
 .jid{font-weight:600;color:#F97316;font-size:10.5px}
 .ast{font-weight:600}
 .dept{display:inline-block;font-size:9px;padding:1px 6px;border-radius:9px}
 .pr{font-size:9px;font-weight:700;padding:1px 6px;border-radius:9px}
 .note{font-size:9.5px;color:#9A3412;background:#FFF7ED;border:1px solid #FDE7CC;border-radius:6px;padding:5px 9px;margin-top:8px;flex:none}
 .cyrow{display:flex;flex-wrap:wrap;gap:4px;margin-top:3px}
 .cychip{font-size:9px;background:#fff;border:1px solid #FDE7CC;border-radius:4px;padding:1px 5px;white-space:nowrap;color:#7C2D12}
 .cychip i{font-style:normal;color:#9A3412;opacity:.8}
 .cychip b{color:#B91C1C}
 .cychip.more{color:#9A3412;opacity:.75}
 /* Crew cards WRAP, and never squeeze below a readable width. Before this they all
    shared one line however many there were: at six teams the card headings folded onto
    two lines and at eight the technician tiles wrapped as well, so the block's height
    depended on the crew count in a way nothing could predict. That is fatal here — the
    server has to know, without a browser, how many job rows are left on the last sheet.
    Four cards to a line, a fixed height each, so the arithmetic below is exact. */
 .crews{display:flex;flex-wrap:wrap;gap:8px;margin-top:7px;flex:none}
 .cw{flex:1 1 232px;max-width:100%;border:1px solid #D9E6F2;border-radius:8px;padding:5px 9px}
 .cwh{display:flex;align-items:center;gap:6px;margin-bottom:6px}
 .cwh b{font-size:12px}
 .cwh .ph{margin-left:auto;font-size:9.5px;color:#5C789A}
 .ppl{display:flex;gap:9px;flex-wrap:wrap}
 .pp{width:58px;text-align:center}
 .pp .av{width:42px;height:42px;border-radius:7px;border:1px solid #D9E6F2;background:#F1F5F9;object-fit:cover;display:block;margin:0 auto}
 .pp .df{display:flex;align-items:center;justify-content:center;color:#B6C2D2}
 .pp .nm{font-size:9px;line-height:1.2;margin-top:2px;font-weight:500}
 .pp .rl{font-size:8px;color:#94A3B8}
 .sign{display:flex;gap:26px;margin-top:auto;padding-top:6px;flex:none}
 .sg{flex:1;text-align:center;font-size:10.5px;color:#5C789A}
 .sg .ln{border-bottom:1px dotted #94A3B8;height:24px;margin-bottom:4px}
 .foot{border-top:1px solid #E5EAF0;padding-top:4px;margin-top:6px;display:flex;font-size:8.5px;color:#94A3B8;flex:none}
 .bar{max-width:281mm;margin:12px auto 0;display:flex;gap:8px;align-items:center}
 .bar button{background:#1B75BC;color:#fff;border:none;border-radius:7px;padding:9px 16px;font-family:inherit;font-size:13.5px;font-weight:600;cursor:pointer}
 .bar .m{font-size:12px;color:#5C789A}
 @media screen{body{padding:10px}.sheet{margin:0 auto 14px;box-shadow:0 2px 12px rgba(15,40,80,.10)}}
 @media print{body{background:#fff;padding:0}.sheet{box-shadow:none;margin:0}.bar{display:none}}
</style>
<div class="bar"><button onclick="window.print()">🖨 พิมพ์ / Print</button><span class="m">@@stamp@@</span></div>
"""

DR_HEAD = """<div class="sheet">
 <div class="hd"><img src="/BFL260.png" alt="Bluefalo">
   <div><h1>แผนงานประจำวัน · Daily Work Plan</h1><div class="sub">@@company@@</div></div>
   <div class="rt"><b>@@date@@</b><br><span style="font-weight:700;color:#1B75BC">เลขที่แผน / Plan no. @@planno@@</span><br>กะ @@shift@@<br>ผู้วางแผน: @@planner@@@@pageno@@</div></div>
 <div class="strip">@@strip@@</div>
 <table><thead><tr>
   <th style="width:28px">ลำดับ<br><span>NO</span></th>
   <th style="width:88px">ทีมช่าง<br><span>CREW</span></th>
   <th style="width:40px">ประเภท<br><span>TYPE</span></th>
   <th style="width:126px">เลขที่งาน<br><span>JOB NO</span></th>
   <th style="width:74px">รหัสเครื่อง<br><span>ASSET</span></th>
   <th style="width:186px">ชื่อเครื่องจักร<br><span>MACHINE</span></th>
   <th>อาการ / งานที่ต้องทำ<br><span>WORK REQUIRED</span></th>
   <th style="width:104px">แผนก<br><span>TRADE</span></th>
   <th style="width:66px">ความสำคัญ<br><span>PRIORITY</span></th>
   <th style="width:126px">หมายเหตุ / ผลงาน<br><span>COMMENT</span></th>
 </tr></thead><tbody>@@rows@@</tbody></table>
"""

DR_TAIL = """ @@carrynote@@
 @@crews@@
 <div class="sign">
   <div class="sg"><div class="ln"></div>ผู้วางแผน / Planner</div>
   <div class="sg"><div class="ln"></div>หัวหน้าช่าง / Lead technician</div>
   <div class="sg"><div class="ln"></div>ผู้จัดการ / Manager</div>
 </div>
 <div class="foot"><span>@@fac@@ CMMS · ออกเมื่อ @@stamp@@ · โดย @@planner@@</span><span style="flex:1"></span><span>@@form@@</span></div>
</div>
"""

DR_TAIL_CONT = """ <div style="margin-top:auto"></div>
 <div class="foot"><span>@@fac@@ CMMS · ออกเมื่อ @@stamp@@ · โดย @@planner@@</span><span style="flex:1"></span><span>@@form@@</span></div>
</div>
"""


def _dr_pad(first_no, count, numbered=True):
    """Empty rows so the table keeps its shape and reaches the foot of the paper.

    On the LAST sheet these are spare lines: a planner writes a late job onto one, so
    they carry the next numbers and nothing follows them to collide with.

    On a continuation sheet they are ruling and nothing else, and they must be
    UNNUMBERED. Numbering them printed the same numbers twice — a 50-job day put 19
    lines on sheet 1 and then ruled empty rows 20, 21, 22 under them, while sheet 2
    opened with a real row 20. The rows were never missing; the blanks at the foot of
    each sheet were wearing the numbers of the work on the next one.
    """
    cell = ('<td class="c">%d</td>' if numbered else '<td class="c"></td>%.0s')
    return "".join('<tr>' + (cell % (first_no + i)) + "<td></td>" * 9 + '</tr>'
                   for i in range(max(0, count)))


DR_LAST_MIN_ROWS = 5   # below this the closing sheet looks abandoned, so balance instead


def _dr_paginate(jobs, rows_last=None):
    """Split the day across sheets of paper.

    FILL EACH SHEET, front to back. It used to spread the work evenly across however
    many sheets were needed, which was meant kindly and read as a mistake: a 35-job day
    came out 12 / 11 / 12 with the first sheet — which holds 22 — two-thirds empty. A
    plan is read in order, so it should be full in order.

    The one exception is the closing sheet. It carries the crews and the signatures, so
    it exists whatever happens, and a page holding two job lines under a signature block
    looks like something went wrong. When filling would leave it nearly bare, the last
    two sheets share what is left between them instead.
    """
    last_cap = DR_ROWS_LAST if rows_last is None else max(DR_ROWS_LAST_MIN, int(rows_last))
    n = len(jobs)
    if n <= last_cap:
        return [(list(jobs), last_cap, True)]
    # how many table-only sheets are needed in front of the closing one
    n_cont = 0
    while n_cont * DR_ROWS_CONT + last_cap < n:
        n_cont += 1
    pages, cut = [], 0
    for i in range(n_cont):
        rest = n - cut
        take = min(DR_ROWS_CONT, rest)
        # …but never take so much that the sheets still to come cannot hold the remainder
        floor_take = rest - ((n_cont - 1 - i) * DR_ROWS_CONT + last_cap)
        take = max(take if take > 0 else 0, floor_take, 0)
        take = min(take, DR_ROWS_CONT, rest)
        pages.append([list(jobs[cut:cut + take]), DR_ROWS_CONT, False])
        cut += take
    tail = n - cut
    if pages and tail < DR_LAST_MIN_ROWS:
        # share the last two sheets rather than sign off under three lines of work
        pool = len(pages[-1][0]) + tail
        tail = min(last_cap, -(-pool // 2))
        keep = pool - tail
        cut = n - tail
        pages[-1][0] = list(jobs[n - pool:n - tail])
        assert len(pages[-1][0]) == keep
    pages.append([list(jobs[cut:]), last_cap, True])
    return [(p, cap, is_last) for p, cap, is_last in pages]


@router.post("/daily")
async def plan_daily(req: Request):
    """Issue the plan for a day and file it. Planner and admin only."""
    u = user_from(req)
    if u["role"] not in ("planner", "admin"):
        raise HTTPException(403, "only a planner may issue the plan")
    b = await req.json()
    d = (b.get("date") or today())[:10]
    shift = (b.get("shift") or "08:00 – 17:00").strip()
    fac = u.get("active_factory") or u.get("factory_id")

    with closing(db()) as c:
        from .db import ensure_plan_reports
        ensure_plan_reports(c)
        f = c.execute("SELECT code,name,form_code FROM factories WHERE id=?", (fac,)).fetchone()
        _company = factory_company(c, fac)
        jobs = _day_jobs(c, fac, d)          # the one set both sheets are about
        teams = [dict(r) for r in c.execute(
            "SELECT * FROM plan_teams WHERE day=? AND factory_id=? ORDER BY seq,id", (d, fac))]
        people = {r["id"]: {"name": r["name"], "department": r["department"] or "",
                            "photo": r["photo"] or ""}
                  for r in c.execute("SELECT id,name,department,photo FROM users WHERE active=1")}
        logins = {r["id"]: r["username"] for r in c.execute("SELECT id,username FROM users")}
        cats = {r["name"]: r["category"] for r in c.execute(
            "SELECT name,category FROM problem_types")}
        # who on this plant signs in as themselves rather than on the crew's handset
        selfacct = own_account_map(c, fac)
        # a login only ONE person uses is that person (BFLFP: tech1 = Choke, tech2 =
        # kanya, tech3 = mark). Work booked to the phone then prints under his crew
        # instead of "— ไม่มีทีม". Only the sheet being issued now is affected.
        from .teams import _handset_people
        one_person = _handset_people(c, fac)[0]

    # which crew each job belongs to: the crew whose members include its lead technician
    crew_of, seq = {}, 0
    for t in teams:
        t["members"] = [int(x) for x in str(t.get("members") or "").split(",") if str(x).strip().isdigit()]
        t["members"] = list(dict.fromkeys(one_person.get(m, m) for m in t["members"]))
        t["color"] = t.get("color") or DR_COL[seq % len(DR_COL)]
        # The crew handset — but only where a crew handset is what the members actually
        # use. On a plant that issued one account per technician the phone is chosen by
        # POSITION and belongs to nobody on the crew: Team C's line read "📱 techbfl2"
        # while its two members, neng and mon, sign in as techbfl1 and techbfl6. An
        # unrelated account printed on a sheet that gets signed is worse than no line at
        # all, so it goes, and the names beneath it answer "who is on this crew".
        t["self_login"] = True          # b375: crew phones retired — everyone has their own account
        t["login_name"] = "" if t["self_login"] else (logins.get(t.get("login_id")) or "")
        t["start"], t["end"] = t.get("start_time") or "", t.get("end_time") or ""
        seq += 1
    # A job booked to a handset names an account, and an account is on no crew, so the
    # CREW column printed "— ไม่มีทีม" for work that plainly had an owner: five rows on
    # BFL's sheet, every one of them a job started from a phone. Where the plant issues
    # one account per technician the owner is knowable, so resolve it here as the
    # assignment board does. The stored rows are repaired on startup; this is what makes
    # the sheet right whatever state they are in, and it changes nothing on a plant that
    # really does share a handset.
    _self = {a: p for p, a in selfacct.items()}
    if _self:
        for j in jobs:
            if j.get("lead_tech") in _self:
                j["lead_tech"] = _self[j["lead_tech"]]
    for j in jobs:
        if j.get("lead_tech") in one_person:
            j["lead_tech"] = one_person[j["lead_tech"]]
        own = [x for x in [j.get("lead_tech")] if x]
        for t in teams:
            if own and own[0] in t["members"]:
                crew_of[str(j["id"])] = (t.get("name") or "Team", t["color"])
                break
    # b385 — THE BOARD'S AUTOMATIC COLUMNS ARE CREWS ON PAPER TOO. A technician who
    # holds assigned work but was never put on a crew gets a column on the assign board
    # ("Choke · AUTO"), built from his jobs each time the board loads and deliberately
    # NOT stored — so the sheet, which only read stored crews, printed every one of his
    # jobs as "— ไม่มีทีม" (no crew) while the board plainly showed him with them. The
    # sheet now builds the same column the board does: one crew per such lead, named
    # after the person, holding just him.
    for j in jobs:
        lead = j.get("lead_tech")
        if not lead or str(j["id"]) in crew_of:
            continue
        t = next((x for x in teams if x.get("auto") and x.get("lead") == lead), None)
        if not t:
            nm = (people.get(lead, {}).get("name") or logins.get(lead) or "Team").split("(")[0].strip()
            t = {"name": nm, "members": [lead], "lead": lead, "auto": True,
                 "color": DR_COL[seq % len(DR_COL)], "self_login": True, "login_name": "",
                 "start": "", "end": ""}
            seq += 1
            teams.append(t)
        crew_of[str(j["id"])] = (t["name"], t["color"])

    K = _day_counts(jobs)
    stamp = datetime.datetime.now().strftime("%d/%m/") + str(datetime.date.today().year + 543) \
        + datetime.datetime.now().strftime(" %H:%M")
    # ── b394: the plan's own number, the plant first: BFL-PLAN-260930-01, FP-…, PC-…
    # Numbered per plant per day. Save & assign that REPLACES the day's plan keeps the
    # number of the sheet it replaces — it is the same plan, brought up to date.
    _code = ((f["code"] if f else "") or "BFL").upper()
    _now0 = datetime.datetime.now()
    _edit0 = d > _now0.date().isoformat() or (d == _now0.date().isoformat() and _now0.hour < 12)
    with closing(db()) as _c:
        _prev0 = None
        if b.get("replace") and _edit0:
            _prev0 = _c.execute("SELECT plan_no FROM plan_reports WHERE factory_id=? AND plan_date=?"
                                " AND COALESCE(kind,'plan')='plan' ORDER BY id DESC LIMIT 1",
                                (fac, d)).fetchone()
        if _prev0 and (_prev0["plan_no"] or ""):
            plan_no = _prev0["plan_no"]
        else:
            _n = _c.execute("SELECT COUNT(*) FROM plan_reports WHERE factory_id=? AND plan_date=?"
                            " AND COALESCE(kind,'plan')='plan' AND COALESCE(plan_no,'')<>''",
                            (fac, d)).fetchone()[0]
            plan_no = "%s-PLAN-%s-%02d" % (_code, d[2:4] + d[5:7] + d[8:10], _n + 1)
    ctx = {"title": "แผนงาน %s · %s" % (d, plan_no), "stamp": stamp, "planno": _e(plan_no),
           "fac": _e((f["code"] if f else "BFL")),
           # the legal entity, not the plant's working name — this sheet is filed
           "company": _e(_company),
           "date": _thdate(d), "shift": _e(shift), "planner": _e(u.get("name") or ""),
           "strip": _strip(K, d > today()), "rows": "", "pageno": "",
           "crews": _dr_crews(teams, people, {k: v["photo"] for k, v in people.items()}),
           "carrynote": _dr_carry(jobs),
           "form": _e((f["form_code"] if f and "form_code" in f.keys() else "") or "")}

    # One sheet is one sheet of paper. Each gets the full header and the table, padded
    # out to its fixed line count; only the final sheet carries the crews and the
    # signature block, and that block is pinned to the bottom of the paper by CSS.
    sheets = _dr_paginate(jobs, _dr_rows_last(teams, bool(ctx["carrynote"])))
    parts, no = [], 1
    for i, (page_jobs, cap, is_last) in enumerate(sheets):
        pctx = dict(ctx)
        pctx["rows"] = (_dr_rows(page_jobs, crew_of, cats, no)
                        + _dr_pad(no + len(page_jobs), cap - len(page_jobs), is_last))
        pctx["pageno"] = (" · หน้า %d / %d" % (i + 1, len(sheets))) if len(sheets) > 1 else ""
        parts.append(_fill(DR_HEAD, pctx))
        parts.append(_fill(DR_TAIL if is_last else DR_TAIL_CONT, pctx))
        no += len(page_jobs)
    html = _fill(DR_CSS, ctx) + "".join(parts)

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    fn = "%s-%s.html" % (plan_no, ts)
    with open(os.path.join(DR_DIR, fn), "w", encoding="utf-8") as fh:
        fh.write(html)
    path = "/uploads/planreports/%s" % fn
    # ── Save & assign issues the plan by itself ─────────────────────────────────────
    # With `replace`, the day keeps ONE issued plan: the latest version overwrites the
    # previous sheet rather than adding a second, so a planner who saves the board three
    # times while adjusting it does not leave three plans for one day on the Reports
    # shelf. Only while the day can still be planned — before noon on the day itself;
    # after that the sheet the crews started from is history and is never overwritten.
    # A plain "Create work plan" (no flag) still files a new sheet, as it always did.
    replaced = False
    _now = datetime.datetime.now()
    editable = d > _now.date().isoformat() or (d == _now.date().isoformat() and _now.hour < 12)
    with closing(db()) as c:
        prev = None
        if b.get("replace") and editable:
            prev = c.execute("SELECT id FROM plan_reports WHERE factory_id=? AND plan_date=?"
                             " AND COALESCE(kind,'plan')='plan' ORDER BY id DESC LIMIT 1",
                             (fac, d)).fetchone()
        # the ids of the work this sheet listed, in the order it printed them
        ids = ",".join(str(j["id"]) for j in jobs)
        if prev:
            c.execute("""UPDATE plan_reports SET shift=?, created_at=?, created_by=?, creator=?,
                         jobs=?, crews=?, people=?, unassigned=?, path=?, job_ids=?, plan_no=? WHERE id=?""",
                      (shift, now(), u["id"], u.get("name") or "", K["n"], len(teams),
                       sum(len(t["members"]) for t in teams), K["nocrew"], path, ids, plan_no, prev["id"]))
            replaced = True
        else:
            c.execute("""INSERT INTO plan_reports(factory_id,plan_date,shift,created_at,created_by,
                         creator,jobs,crews,people,unassigned,path,kind,job_ids,plan_no)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,'plan',?,?)""",
                      (fac, d, shift, now(), u["id"], u.get("name") or "", K["n"], len(teams),
                       sum(len(t["members"]) for t in teams), K["nocrew"], path, ids, plan_no))
        _file_counts(c, fac, d, "plan", K)
        c.commit()
    return {"ok": True, "url": path, "plan_no": plan_no, "jobs": K["n"], "crews": len(teams), "replaced": replaced,
            "done": K["done"], "open": K["open"], "unassigned": K["nocrew"]}


@router.get("/daily/history")
async def plan_daily_history(req: Request):
    """Every plan ever issued for this plant, newest first."""
    u = user_from(req)
    fac = u.get("active_factory") or u.get("factory_id")
    with closing(db()) as c:
        from .db import ensure_plan_reports
        ensure_plan_reports(c)
        kind = (req.query_params.get("kind") or "").strip()
        if kind in ("plan", "done"):
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM plan_reports WHERE factory_id=? AND COALESCE(kind,'plan')=?"
                " ORDER BY id DESC LIMIT 200", (fac, kind))]
        else:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM plan_reports WHERE factory_id=? ORDER BY id DESC LIMIT 200", (fac,))]
        try:
            _shelf_job_state(c, rows)
        except Exception as e:
            # The shelf still lists its documents without this; it must not fail on it.
            _log.warning("[shelf] could not read job state: %s", e)
    return {"reports": rows}


def _shelf_job_state(c, rows):
    """Tell each repair-form row how far its job has actually got.

    A ใบแจ้งซ่อม (bai chaeng som / repair form) on the shelf is a PDF, and a PDF says
    nothing about whether anyone has signed it yet. Somebody looking for the sign
    button had no way to see that the job is still sitting at "waiting for the person
    who reported it", which is exactly why the button was not offered. So the row
    carries the job's own state: who raised it, whether they have accepted, and
    whether a planner has signed. One query for the whole shelf, not one per row.
    """
    want = {}
    for r in rows:
        if (r.get("kind") or "plan") == "jobform" and r.get("shift"):
            want.setdefault(str(r["shift"]), []).append(r)
    if not want:
        return
    ids = list(want.keys())
    out = {}
    # SQLite caps a statement's variables, so ask in blocks rather than one long IN().
    for i in range(0, len(ids), 400):
        blk = ids[i:i + 400]
        ph = ",".join("?" * len(blk))
        for j in c.execute(
                f"""SELECT j.id, j.jobid, j.jobtype, j.status, j.factory_id,
                           COALESCE(ur.name, uc.name, '') req_name,
                           COALESCE(j.sign_requester,'') sr,
                           COALESCE(j.sign_inspector,'') si,
                           COALESCE(j.accepted_at,'') acc
                    FROM jobs j
                    LEFT JOIN users ur ON ur.id = j.requester_id
                    LEFT JOIN users uc ON uc.id = j.created_by
                    WHERE j.jobid IN ({ph})""", blk):
            # a job NUMBER repeats across plants since each plant numbers its own jobs
            out.setdefault(str(j["jobid"]), []).append(j)
    for jid, rs in want.items():
        cands = out.get(jid) or []
        if not cands:
            continue
        for r in rs:
            j = next((x for x in cands if x["factory_id"] == r.get("factory_id")), None)
            if j is None:
                continue
            r["job_id"] = j["id"]
            r["job_type"] = j["jobtype"] or ""
            r["job_status"] = j["status"] or ""
            r["requester"] = j["req_name"] or ""
            r["sign_req"] = 1 if j["sr"] else 0      # the reporter accepted the work
            r["sign_insp"] = 1 if j["si"] else 0     # a planner/admin signed it off
            r["accepted_at"] = j["acc"] or ""


@router.post("/completed")
async def plan_completed(req: Request):
    """Issue the day's maintenance report.

    There is nothing special about pressing this button: it produces exactly what the
    Daily report button produces, and archives and files it the same way. It exists so
    a planner can say "the day is closed, put it on the shelf" without hunting for the
    download — not because it makes a different document.
    """
    u = user_from(req)
    if u["role"] not in ("planner", "admin"):
        raise HTTPException(403, "only a planner may issue this report")
    b = await req.json()
    d = (b.get("date") or today())[:10]
    fac = u.get("active_factory") or u.get("factory_id")

    from .reports import file_daily
    file_daily(d, u)

    with closing(db()) as c:
        jobs = _day_jobs(c, fac, d)
    K = _day_counts(jobs)
    return {"ok": True, "url": "/api/reports/archive?d=%s" % d,
            "jobs": K["n"], "done": K["done"], "open": K["open"]}

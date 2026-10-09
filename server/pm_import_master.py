# -*- coding: utf-8 -*-
"""Read a **machine-register master** workbook (บัญชีรายชื่อเครื่องมือเครื่องจักร).

This is the second shape of PM plan the plants keep, and it is not the BFLFP one.
`pm_import.py` reads the BFLFP PM Plan: one sheet per machine type, the frequency
carried by the FILL COLOUR of the ลำดับ cell, a part photo anchored on each row. That
reader is untouched — this is a separate file for a separate document, chosen at upload
time by :func:`looks_like_master`.

The master workbook is laid out like this::

    MList                     every asset: รหัส · ชื่อ · ผู้ผลิต · ขนาด · สถานที่ตั้ง · ชั้นที่ · กลุ่มเครื่องจักร
    <one sheet per group>     กลุ่มเครื่องจักร : AIR LOCK
        row 6   ลำดับ | รหัสเครื่องจักร | ชื่อเครื่องจักร | รายการตรวจสอบและบำรุงรักษา | ความถี่ในการบำรุงรักษา | วิธีการบำรุงรักษา
        row 7                                                                          สัปดาห์ | 1 เดือน | 3 เดือน | 6 เดือน | 1 ปี
        row 8 →  the machines in this group AND the checklist for them

Three things about that layout decide how this reader is written.

**The two lists are independent.** Columns B/C list the machines in the group; column D
lists the check items. They share rows only because they are printed side by side. A
group with 67 machines and 6 items is normal, and neither list may be read as if it
described the other.

**The checklist repeats once per printed page.** A sheet holding 85 machines prints them
15 at a time, and re-prints the whole checklist above each block so the paper sheet is
self-contained. Read literally, SCREW CONVEYOR yields 66 items instead of 11 — and every
one of those duplicates would become a real PM job. Items are therefore de-duplicated by
their own text; the first copy wins and keeps its number.

**Frequency and method are separate columns here.** A tick — Ö in the Wingdings font —
in one of the five ความถี่ columns says how often; the วิธีการ column says what to do
(เช็ค check · ทำ do · วัด measure). The one item with no tick anywhere is the closing
line "การเคลียร์สิ่งแปลกปลอม/ความสะอาดก่อนส่งมอบทุกครั้งหลังเสร็จงาน" — clear foreign
objects and clean before handover, every time — which is exactly the plant's each_op
rule, so it is filed as each_op rather than dropped.

There are no part photos in this document. The technician's sheet shows the item text,
the frequency and the method, and collects OK/NG and a remark as it always has.
"""
import io, re
from collections import OrderedDict

# "…2500ชม" / "5000 ชม." — a due-by-running-hours item, see _read_group
HOURS_RE = re.compile(r"\d{3,}\s*(?:ชม|ชั่วโมง|hr|hrs|hour)")

# sheets that are not a machine group
SKIP = ("MLIST", "TEMPLATE", "CHKLIST", "HIST", "PROGRAM")

# the five ความถี่ columns, by the words in the sub-header row
FREQ_BY_HEAD = (("สัปดาห", "weekly"), ("1 เดือน", "monthly"), ("3 เดือน", "q3m"),
                ("6 เดือน", "m6"), ("ปี", "yearly"))

# b413: the fill that marks an item cell as electrical work (Central Engineering)
YELLOW = ("FFFFFF00", "00FFFF00")

# the closing clean-up line — every sheet carries it, none ticks a frequency for it
EACH_OP_MARK = "การเคลียร์สิ่งแปลกปลอม"

# what a group with no checklist yet is called. A machine parked here generates no PM
# job — a template with no items has nothing to schedule — but it is still visible in
# the PM group screen, which is the point: an asset with no PM should be something you
# can see and fix, not something that quietly never appears.
PENDING = "PENDING — ยังไม่มีกลุ่ม PM / not yet grouped"


def _base(name):
    """A sheet's name without the copy suffix Excel adds — "MList (2)" → "MLIST".
    Rev.01 of the BFL master carries its register as "MList (2)"; read literally it was
    taken for a machine group and its 709 asset names became 709 check items."""
    return re.sub(r"\s*\(\d+\)\s*$", "", name.strip()).upper()


def _txt(v):
    return re.sub(r"\s+", " ", str(v)).strip() if v not in (None, "") else ""


def _load(source):
    try:
        import openpyxl
    except ImportError:
        raise ValueError("openpyxl is not installed on the server")
    try:
        return openpyxl.load_workbook(
            io.BytesIO(source) if isinstance(source, bytes) else source,
            read_only=True, data_only=True)
    except Exception:
        raise ValueError("อ่านไฟล์ไม่ได้ — เป็นไฟล์ .xlsx หรือไม่ / "
                         "could not read the file — is it a valid .xlsx?")


def looks_like_master(wb):
    """True when this workbook is a register master rather than a BFLFP PM Plan.

    The tell is the group banner — "กลุ่มเครื่องจักร :" — above a ลำดับ header whose
    second column is รหัสเครื่องจักร. The BFLFP plan has neither: its ลำดับ header is
    followed by รูปภาพ Part, and it has no banner at all. Checked on the sheets rather
    than on the file name, because both documents get renamed.
    """
    for name in wb.sheetnames:
        if _base(name) in SKIP:
            continue
        ws = wb[name]
        try:
            rows = [list(r) for r in ws.iter_rows(min_row=1, max_row=12,
                                                  max_col=6, values_only=True)]
        except Exception:
            continue
        banner = any("กลุ่มเครื่องจักร" in _txt(v) for r in rows for v in r)
        hd = next((r for r in rows if _txt(r[0]) == "ลำดับ"), None)
        if banner and hd and "รหัสเครื่องจักร" in _txt(hd[1] if len(hd) > 1 else ""):
            return True
    return False


def read_mlist(wb):
    """The asset register sheet, as [{code,name,maker,size,loc,floor,group}].

    Absent on some copies of the workbook — the group sheets alone are enough to build
    the checklists — so this returns an empty list rather than failing.
    """
    ws = next((wb[n] for n in wb.sheetnames if _base(n) == "MLIST"), None)
    if ws is None:
        return []
    rows = [list(r) for r in ws.iter_rows(min_row=1, max_row=ws.max_row or 1,
                                          max_col=12, values_only=True)]
    hd = next((i for i, r in enumerate(rows)
               if any(_txt(v).startswith("ลำดับ") for v in r[:3])), None)
    if hd is None:
        return []

    def col(label, default):
        for c in range(len(rows[hd])):
            if label in _txt(rows[hd][c]):
                return c
        return default
    cc, nc = col("รหัส", 2), col("ชื่อ", 3)
    lc, fc, gc = col("สถานที่", 8), col("ชั้น", 9), col("กลุ่มเครื่องจักร", 10)
    out, seen = [], set()
    for r in rows[hd + 1:]:
        code = _txt(r[cc]) if cc < len(r) else ""
        if not code or code in seen:
            continue
        seen.add(code)
        g = lambda i: _txt(r[i]) if i < len(r) else ""      # noqa: E731
        out.append({"code": code, "name": g(nc), "maker": g(4), "size": g(7),
                    "loc": g(lc), "floor": g(fc), "group": g(gc)})
    return out


def _read_group(ws, known_codes):
    """One group sheet → (machines, items, misplaced_rows). See the module docstring.

    The whole sheet is read, deliberately, and NOT `ws.print_area`. The print ranges on
    this document are years out of date — SCREW DISCHARGER prints to row 26 and carries
    real machines to row 52, BIN prints to 121 and holds WG3BIN05-12 below it — so
    honouring them would silently drop about forty machines that are in service. The
    sheets grow; nobody re-sets the range. Read everything, then judge each row.
    """
    rows = [list(r) for r in ws.iter_rows(min_row=1, max_row=ws.max_row or 1,
                                          max_col=14, values_only=True)]
    hd = next((i for i, r in enumerate(rows) if _txt(r[0]) == "ลำดับ"), None)
    if hd is None:
        return [], []

    def col(label, default):
        for c in range(len(rows[hd])):
            if label in _txt(rows[hd][c]):
                return c
        return default
    ccol, ncol = col("รหัสเครื่องจักร", 1), col("ชื่อเครื่องจักร", 2)
    icol, fcol = col("รายการตรวจสอบ", 3), col("ความถี่", 4)
    mcol = col("วิธีการ", 9)

    sub = rows[hd + 1] if hd + 1 < len(rows) else []
    fmap = {}
    for c in range(fcol, min(fcol + 7, len(sub))):
        t = _txt(sub[c])
        for needle, freq in FREQ_BY_HEAD:
            if needle in t and freq not in fmap.values():
                fmap[c] = freq
                break

    # b413: an item cell filled YELLOW is an electrical point — the work of Central
    # Engineering (electrical), not of the plant's own technicians. The sheet says so in
    # colour and nowhere else, so the colour is read here; the values pass above cannot
    # see it. Every printed copy of the list is looked at: a point marked on any page
    # is marked.
    yellow = set()
    try:
        for k, rr in enumerate(ws.iter_rows(min_row=1, max_row=ws.max_row or 1,
                                            min_col=icol + 1, max_col=icol + 1)):
            cl = rr[0] if rr else None
            try:
                fg = cl.fill.fgColor.rgb if cl is not None and cl.fill is not None else None
            except Exception:
                fg = None
            if isinstance(fg, str) and fg.upper() in YELLOW and (cl.fill.fill_type or "") == "solid":
                yellow.add(k)
    except Exception:
        yellow = set()

    machines, items, misplaced, seen_m, seen_i = [], [], [], set(), {}
    for k, r in enumerate(rows[hd + 2:], start=hd + 2):
        cell = lambda i: _txt(r[i]) if i < len(r) else ""   # noqa: E731
        code, nm = cell(ccol), cell(ncol)

        # A couple of rows are typed one column across: an asset code lands in
        # ชื่อเครื่องจักร and the machine's name in รายการตรวจสอบ. Every one of them is a
        # leftover — the same asset is also listed properly, in column B, on this sheet or
        # its real one. So the row is NOT read as a machine and NOT read as a check item;
        # it is reported, and a person tidies the workbook. Guessing here once put a belt
        # conveyor into the AIR LOCK group, which is worse than saying nothing.
        if not code and nm and known_codes and nm in known_codes:
            misplaced.append({"sheet": ws.title, "code": nm, "text": cell(icol)})
            continue

        if code and code not in seen_m:
            seen_m.add(code)
            machines.append({"code": code, "name": nm})

        raw = cell(icol)
        if not raw:
            continue
        item = re.sub(r"^\s*\d+\s*[.)]\s*", "", raw)         # "3.ตรวจเช็ค…" → "ตรวจเช็ค…"
        key = item.lower()
        if key in seen_i:
            if k in yellow:                                 # marked on a later page
                seen_i[key]["elec"] = 1
            continue                                        # the same list, next page
        ticked = [fmap[c] for c in sorted(fmap) if cell(c)]
        if ticked:
            freq = ticked[0]
        elif EACH_OP_MARK in item:
            freq = "each_op"
        elif HOURS_RE.search(item):
            # A handful of supplier items are due by RUNNING HOURS — "PM Air Oil Filter
            # 2500ชม" — and this app schedules by the calendar. There is no honest
            # conversion without a run-hour meter, so they are filed yearly: a reminder
            # once a year that a technician can bring forward is worth more than an item
            # with no schedule, which would never appear on any sheet at all.
            freq = "yearly"
        else:
            freq = ""
        items.append({"seq": len(items) + 1, "item": item, "normal": "",
                      "method": cell(mcol), "freq": freq, "img": "",
                      "elec": 1 if k in yellow else 0})
        seen_i[key] = items[-1]
    return machines, items, misplaced


def read_workbook(source):
    """(templates, mlist, form_no, notes).

    Each template is {machine_type, color, items, members} — members being the asset
    codes the sheet itself lists. That is the real gain over the BFLFP plan, where the
    importer has to guess which machines a template covers by matching its name: here
    the document states it, so nothing is guessed and nothing is silently left out.
    """
    wb = _load(source)
    mlist = read_mlist(wb)
    known = {m["code"] for m in mlist}
    form_no = ""
    templates, groups, placed = [], [], set()
    misplaced, doubled = [], []
    for name in wb.sheetnames:
        if _base(name) in SKIP:
            continue
        ws = wb[name]
        if not form_no:
            for r in ws.iter_rows(min_row=1, max_row=4, max_col=14, values_only=True):
                m = re.search(r"(SD-[A-Z0-9\-]+)", " ".join(_txt(v) for v in r))
                if m:
                    form_no = m.group(1)
                    break
        machines, items, mis = _read_group(ws, known)
        misplaced.extend(mis)
        if not machines and not items:
            continue
        groups.append((name.strip(), machines, items))

    # One machine, one checklist. A code listed on two sheets keeps the first and is
    # reported — left alone it would collect two PM rounds a week for the rest of its life.
    for name, machines, items in groups:
        members = []
        for m in machines:
            cd = m["code"]
            if cd in placed:
                if cd not in {d["code"] for d in doubled}:
                    doubled.append({"code": cd, "sheet": name})
                continue
            placed.add(cd)
            members.append(cd)
        templates.append({"machine_type": name, "color": "", "items": items,
                          "members": members})

    # Assets the register knows about that no group sheet claims. They are collected
    # rather than ignored: an asset with no PM is a decision somebody has to make, and
    # it cannot be made about a machine nobody can see.
    if mlist:
        orphans = [m["code"] for m in mlist if m["code"] not in placed]
        if orphans:
            templates.append({"machine_type": PENDING, "color": "", "items": [],
                              "members": orphans})
    wb.close()
    if not templates:
        raise ValueError("ไม่พบชีตกลุ่มเครื่องจักรในไฟล์นี้ / "
                         "no machine-group sheets found in this workbook")
    return templates, mlist, form_no, {"misplaced": misplaced, "doubled": doubled}


def plan_from_workbook(source, factory_id):
    """(plan dict ready for the importer, mlist) — the same shape pm_import.py returns,
    minus the images, because this document has no part photos.

    The plan also carries `notes`: rows the reader would not act on — an asset code typed
    one column across, a machine listed on two sheets. Nothing is guessed from them; they
    are handed back so the planner can tidy the workbook.
    """
    templates, mlist, form_no, notes = read_workbook(source)
    return ({"form_no": form_no, "factory_id": factory_id, "templates": templates,
             "color_groups": [], "notes": notes}, mlist)


def summary(templates, mlist):
    """Counts worth showing the planner before anything is written."""
    per = {"weekly": 52, "monthly": 12, "q3m": 4, "m6": 2, "yearly": 1, "each_op": 0}
    jobs = 0
    for t in templates:
        freqs = {i["freq"] for i in t["items"] if per.get(i["freq"])}
        jobs += len(t["members"]) * sum(per[f] for f in freqs)
    pend = next((t for t in templates if t["machine_type"] == PENDING), None)
    return OrderedDict([
        ("templates", len([t for t in templates if t["items"]])),
        ("items", sum(len(t["items"]) for t in templates)),
        ("machines", sum(len(t["members"]) for t in templates)),
        ("pending", len(pend["members"]) if pend else 0),
        ("hour_based_filed_yearly",
         len([i for t in templates for i in t["items"] if HOURS_RE.search(i["item"])])),
        ("items_without_frequency",
         len([i for t in templates for i in t["items"] if not i["freq"]])),
        ("assets_in_mlist", len(mlist)),
        ("pm_jobs_per_year", jobs),
    ])

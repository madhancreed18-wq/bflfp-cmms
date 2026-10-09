# -*- coding: utf-8 -*-
"""Read a PM Plan workbook (.xlsx) into the shape the importer stores.

The planner uploads the workbook in the browser; this turns it into
{templates, color_groups, form_no} plus the part photos, without touching the
database. `build_pm_plan.py` at the repo root is the same reader with a CLI
around it, for regenerating data/pm_plan.json offline.

Every sheet is laid out the same way:
    row 7      ลำดับ | รูปภาพ Part | รายการตรวจเช็ค | สถานะปกติ | Week … | วิธีการ
    row 9 →    one checklist item per row, down to the ลำดับ legend row
    column A   the item number — and its FILL COLOUR is the frequency
    column B   the part photo, anchored on the item's row
    tab colour the template's colour group
"""
import io, re
from collections import Counter

FREQ_BY_COLOR = {"FFFFC000": "weekly", "FFFF0000": "monthly", "FF00B050": "q3m",
                 "FF00B0F0": "m6", "FFFFFF00": "each_op"}
DEFAULT_FREQ = "each_op"          # no fill = the closing clean-up line


def _txt(v):
    return re.sub(r"\s+", " ", str(v)).strip() if v not in (None, "") else ""


def _fill(cell):
    f = cell.fill
    if not f or not f.patternType:
        return ""
    c = f.fgColor
    return (c.rgb or "") if c is not None and c.type == "rgb" else ""


def _tab_color(ws):
    c = ws.sheet_properties.tabColor
    if c is None:
        return ""
    if c.type == "rgb":
        return c.rgb or ""
    if c.type == "theme":
        return "T%s" % c.theme
    return ""


def read_workbook(source):
    """source: bytes or a path. Returns (templates, images, form_no).

    images maps the photo filename to its raw bytes, ready to be written into
    static/pmimg/. Raises ValueError with a readable message on a bad file.
    """
    try:
        import openpyxl
    except ImportError:
        raise ValueError("openpyxl is not installed on the server")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(source) if isinstance(source, bytes) else source)
    except Exception:
        raise ValueError("อ่านไฟล์ไม่ได้ — เป็นไฟล์ .xlsx หรือไม่ / could not read the file — is it a valid .xlsx?")

    templates, images, form_no = [], {}, ""
    for idx, name in enumerate(wb.sheetnames, 1):
        ws = wb[name]
        head = next((r for r in range(1, 16) if _txt(ws.cell(r, 1).value) == "ลำดับ"), None)
        if not head:
            continue                                  # a blank tab, not a checklist
        form_no = form_no or _txt(ws.cell(3, 10).value)

        # the columns are not in the same place on every sheet — one has an extra
        # ความถี่ column, another shifts วิธีการ — so read them off the header row
        def col_of(label, default):
            return next((c for c in range(1, 22) if label in _txt(ws.cell(head, c).value)), default)
        icol, ncol = col_of("รายการตรวจเช็ค", 3), col_of("สถานะปกติ", 4)
        mcol, pcol = col_of("วิธีการ", 14), col_of("รูปภาพ", 2)
        stop = next((r for r in range(head + 1, (ws.max_row or head) + 1)
                     if "สัญลักษณ์" in _txt(ws.cell(r, 1).value)), (ws.max_row or head) + 1)

        # a photo belongs to the row it is anchored on; the one on the title rows is the logo
        by_row = {}
        for im in ws._images:
            r = im.anchor._from.row + 1
            if r > head:
                by_row.setdefault(r, im)

        items = []
        for r in range(head + 1, stop):
            if not _txt(ws.cell(r, 1).value).isdigit():
                continue
            # the closing clean-up line writes its text in the photo column
            item = _txt(ws.cell(r, icol).value) or _txt(ws.cell(r, pcol).value)
            if not item:
                continue                              # a spare numbered row with nothing on it
            seq = len(items) + 1                      # numbered fresh: sheets repeat numbers by hand
            img = ""
            if r in by_row:
                ext = (getattr(by_row[r], "path", "") or ".jpg").rsplit(".", 1)[-1].lower()
                ext = "jpg" if ext in ("jpeg", "jpg") else ext
                img = "t%02d_%d.%s" % (idx, seq, ext)
                try:
                    images[img] = by_row[r]._data()
                except Exception:
                    img = ""
            items.append({"seq": seq, "item": item,
                          "normal": _txt(ws.cell(r, ncol).value),
                          "method": _txt(ws.cell(r, mcol).value),
                          "freq": FREQ_BY_COLOR.get(_fill(ws.cell(r, 1)), DEFAULT_FREQ),
                          "img": img})
        if items:
            templates.append({"machine_type": name.strip(), "color": _tab_color(ws), "items": items})
    wb.close()
    if not templates:
        raise ValueError("ไม่พบชีตเช็คลิสต์ในไฟล์นี้ / no checklist sheets found in this workbook")
    return templates, images, form_no


def color_groups(templates):
    n = Counter(t["color"] for t in templates if t["color"])
    return [{"color": c, "name": "Group %d" % (i + 1), "n": n[c]}
            for i, (c, _) in enumerate(n.most_common())]


def plan_from_workbook(source, factory_id):
    """The whole thing: (plan dict ready for the importer, images to write)."""
    templates, images, form_no = read_workbook(source)
    return ({"form_no": form_no, "factory_id": factory_id,
             "templates": templates, "color_groups": color_groups(templates)}, images)

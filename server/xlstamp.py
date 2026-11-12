"""b469 — every Excel file the CMMS hands out says where it came from.

A sheet copied out of the app gets forwarded, merged with another plant's and printed.
Three things have to travel with it so nobody has to guess later:
  · the plant (BFL / FP / PC),
  · when it was downloaded, and by whom,
  · its own file name — the same name the browser saves it under.

The name is  <what>_<PLANT>_<YYYY-MM-DD_HHMM>.xlsx  and the same three facts are written
in five rows at the top (the fifth left blank) of every sheet, in the document properties, and in the printed
page header / footer. Rows are inserted ABOVE what each export already built, so no
export has to change how it lays out its own table; freeze panes, filters, merged cells
and grouped (outline) rows are moved down with it.
"""
from contextlib import closing
from datetime import datetime

from .db import db

STAMP_ROWS = 5          # title · plant · downloaded · file · blank spacer


def plant_code(fac_id):
    """'BFL' / 'FP' / 'PC' for a plant id, 'ALL' for no plant (all-plants view)."""
    if not fac_id:
        return "ALL"
    with closing(db()) as c:
        r = c.execute("SELECT code, name FROM factories WHERE id=?", (int(fac_id),)).fetchone()
    return (r["code"] if r and r["code"] else "ALL")


def plant_name(fac_id):
    if not fac_id:
        return "All plants / ทุกโรงงาน"
    with closing(db()) as c:
        r = c.execute("SELECT name FROM factories WHERE id=?", (int(fac_id),)).fetchone()
    return (r["name"] if r else "") or ""


def user_fac(u):
    return u.get("active_factory") or u.get("factory_id") or None


def file_name(what, fac_id, when=None, ext="xlsx"):
    when = when or datetime.now()
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (what or "export")).strip("-")
    return f"{safe}_{plant_code(fac_id)}_{when.strftime('%Y-%m-%d_%H%M')}.{ext}"


def _shift(ref, n):
    """Move an A1 reference (cell or range) down by n rows."""
    from openpyxl.utils.cell import range_boundaries, get_column_letter
    if not ref:
        return ref
    if ":" not in ref:
        from openpyxl.utils.cell import coordinate_from_string
        col, row = coordinate_from_string(ref)
        return f"{col}{row + n}"
    c1, r1, c2, r2 = range_boundaries(ref)
    return f"{get_column_letter(c1)}{r1 + n}:{get_column_letter(c2)}{r2 + n}"


def stamp(wb, fac_id, fname, user=None, title=""):
    """Write the plant / date / file reference into every sheet of `wb`."""
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.worksheet.dimensions import RowDimension
    when = datetime.now()
    code, pname = plant_code(fac_id), plant_name(fac_id)
    who = ((user or {}).get("name") or "") if user else ""
    stamp_txt = when.strftime("%d-%m-%Y %H:%M")
    for ws in wb.worksheets:
        n = STAMP_ROWS
        merged = [str(m) for m in ws.merged_cells.ranges]
        for m in merged:
            ws.unmerge_cells(m)
        dims = {k: v for k, v in ws.row_dimensions.items()}
        freeze = ws.freeze_panes
        filt = ws.auto_filter.ref
        ws.insert_rows(1, n)
        # row heights and outline (grouped rows) belong to the row numbers — move them too
        ws.row_dimensions.clear()
        for k, v in dims.items():
            nd = RowDimension(ws, index=k + n, ht=v.ht, customHeight=v.customHeight,
                              outlineLevel=v.outlineLevel, hidden=v.hidden, collapsed=v.collapsed)
            ws.row_dimensions[k + n] = nd
        for m in merged:
            ws.merge_cells(_shift(m, n))
        if freeze:
            ws.freeze_panes = _shift(freeze, n)
        if filt:
            ws.auto_filter.ref = _shift(filt, n)
        band = PatternFill("solid", fgColor="EEF4FB")
        lines = [
            (f"BFL Smart Factory  ·  {title or ws.title}", True),
            (f"โรงงาน / Plant:  {code}  —  {pname}", True),
            (f"วันที่ดาวน์โหลด / Downloaded:  {stamp_txt}" + (f"   ·   โดย / by: {who}" if who else ""), False),
            (f"ชื่อไฟล์ / File:  {fname}", False),
        ]
        for i, (txt, bold) in enumerate(lines, 1):
            c = ws.cell(i, 1, txt)
            c.font = Font(bold=bold, size=11 if i > 1 else 12, color="0F172A")
            c.alignment = Alignment(horizontal="left", vertical="center")
            for col in range(1, max(ws.max_column, 6) + 1):
                ws.cell(i, col).fill = band
        # printed copies carry it too
        try:
            ws.oddHeader.left.text = f"{code} · {pname}"
            ws.oddHeader.right.text = f"{stamp_txt}"
            ws.oddFooter.left.text = fname
            ws.oddFooter.right.text = "Page &P / &N"
        except Exception:
            pass
    p = wb.properties
    p.title = title or fname
    p.subject = f"Plant {code}"
    p.creator = "BFL Smart Factory" + (f" · {who}" if who else "")
    p.keywords = f"{code}; {stamp_txt}; {fname}"
    p.created = when
    return wb

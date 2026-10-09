"""Read the sheet tab colours out of the PM Plan workbook and put them on the
PM templates, so every asset shows its colour group.

Each sheet in `BFLFP PM Plan/BFLFP PM Plan _R1.xlsx` is one machine type and its
tab is coloured — yellow, green, amber, purple, blue. The importer that built the
templates never captured that, so `pm_templates.color` is empty for all 80 and the
**PM group** column on the Assets page shows "—" for everything.

This fills it in:
  * matches each sheet title to a template by machine type
  * writes the tab colour onto that template
  * creates a named colour group per colour (rename them later in the app)

Assets pick the colour up through the template they already match, so nothing on
the machines table is touched.

    python import_pm_colors.py              # show what would change
    python import_pm_colors.py --apply      # back up the database, then write
"""
import argparse
import os
import re
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "cmms.db")
XLSX = os.path.join(HERE, "BFLFP PM Plan", "BFLFP PM Plan _R1.xlsx")

# Excel's palette -> a name a planner would recognise. Renameable in the app afterwards.
COLOR_NAMES = {
    "FFFFFF00": "เหลือง / Yellow",
    "FF92D050": "เขียว / Green",
    "FFFFC000": "ส้ม / Amber",
    "FF7030A0": "ม่วง / Purple",
    "FF00B0F0": "ฟ้า / Blue",
    "FFFF0000": "แดง / Red",
}


def tab_color(ws):
    """The sheet's tab colour as an ARGB string, or '' when it has none.

    A tab coloured from the theme palette rather than a fixed RGB raises inside
    openpyxl, so every access is guarded and falls back to the theme index.
    """
    t = ws.sheet_properties.tabColor
    if t is None:
        return ""
    try:
        rgb = t.rgb
        if isinstance(rgb, str) and re.fullmatch(r"[0-9A-Fa-f]{8}", rgb):
            return rgb.upper()
    except Exception:
        pass
    try:
        if t.theme is not None:
            # A tab painted from the theme palette (Excel's "Light 1" etc.) is not one
            # of the five real groups — treat it as uncoloured and report it instead of
            # inventing a group nobody chose.
            return f"~theme{t.theme}"
    except Exception:
        pass
    return ""


def norm(s):
    return re.sub(r"\s+", "", (s or "").strip().lower())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--xlsx", default=XLSX)
    ap.add_argument("--factory", type=int, default=0,
                    help="factory id to update (default: every factory that has templates)")
    args = ap.parse_args()

    for path, what in ((args.db, "database"), (args.xlsx, "workbook")):
        if not os.path.exists(path):
            sys.exit(f"! no {what} at {path}")
    try:
        import openpyxl
    except ImportError:
        sys.exit("! openpyxl is not installed — run this with the venv Python")

    print(f"  workbook : {args.xlsx}")
    print(f"  database : {args.db}\n  reading tab colours…\n")
    wb = openpyxl.load_workbook(args.xlsx, read_only=False, data_only=True, keep_links=False)
    sheet_color, odd, plain = {}, [], []
    for ws in wb.worksheets:
        col = tab_color(ws)
        if col.startswith("~"):
            odd.append((ws.title, col[1:]))
        elif col:
            sheet_color[norm(ws.title)] = (ws.title, col)
        else:
            plain.append(ws.title)
    wb.close()

    tally = Counter(c for _t, c in sheet_color.values())
    print(f"  {len(sheet_color)} sheet(s) carry a tab colour:")
    for col, n in tally.most_common():
        print(f"    {col:<10} {COLOR_NAMES.get(col, col):<18} {n} sheet(s)")
    for title, why in odd:
        print(f"    ~ {title}: tab uses a theme colour ({why}), not a group colour — skipped")
    for title in plain:
        print(f"    ~ {title}: tab has no colour — skipped")
    if odd or plain:
        print("      colour those tabs in Excel and re-run to put them in a group")

    c = sqlite3.connect(args.db)
    c.row_factory = sqlite3.Row
    q = "SELECT id, factory_id, machine_type, COALESCE(color,'') color FROM pm_templates"
    args_sql = []
    if args.factory:
        q += " WHERE factory_id=?"
        args_sql = [args.factory]
    templates = c.execute(q, args_sql).fetchall()
    if not templates:
        sys.exit("! no PM templates in this database — import the PM plan first")

    updates, unmatched = [], []
    for t in templates:
        hit = sheet_color.get(norm(t["machine_type"]))
        if not hit:
            unmatched.append(t["machine_type"])
            continue
        if t["color"] != hit[1]:
            updates.append((hit[1], t["id"], t["machine_type"]))

    print(f"\n  templates            : {len(templates)}")
    print(f"  will get a colour    : {len(updates)}")
    print(f"  already correct      : {len(templates) - len(updates) - len(unmatched)}")
    print(f"  no matching sheet    : {len(unmatched)}")
    if unmatched:
        for n in unmatched[:8]:
            print(f"      · {n}")
        if len(unmatched) > 8:
            print(f"      · … and {len(unmatched) - 8} more")

    facs = sorted({t["factory_id"] for t in templates})
    want_groups = []
    for fac in facs:
        have = {r["color"] for r in c.execute(
            "SELECT color FROM pm_groups WHERE factory_id=?", (fac,))}
        for col in tally:
            if col not in have:
                want_groups.append((fac, col, COLOR_NAMES.get(col, col)))
    print(f"\n  colour groups to create: {len(want_groups)}")
    for fac, col, name in want_groups:
        print(f"      factory {fac}  {col}  {name}")

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply.")
        return

    backup = f"{args.db}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(args.db, backup)
    print(f"\nBackup written to {backup}")

    c.executemany("UPDATE pm_templates SET color=? WHERE id=?",
                  [(col, tid) for col, tid, _n in updates])
    c.executemany("INSERT INTO pm_groups(factory_id,color,name) VALUES(?,?,?)", want_groups)
    c.commit()

    left = c.execute("SELECT COUNT(*) FROM pm_templates WHERE COALESCE(color,'')=''").fetchone()[0]
    print(f"Coloured {len(updates)} template(s), created {len(want_groups)} group(s).")
    print(f"Templates still without a colour: {left}")
    c.close()


if __name__ == "__main__":
    main()

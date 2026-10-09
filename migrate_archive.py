"""Sort the report archive into one folder per plant.

    data/Report/YYYY/MM/DD/…        →   data/Report/<PLANT>/YYYY/MM/DD/…

Why: "the Petcare records for August" should be one folder to copy, back up or hand to
an auditor. With three plants' paperwork mixed into dated folders that question can only
be answered by reading filenames one at a time — and for the daily report it could not
be answered at all, because all three plants wrote the SAME file for a day and
overwrote each other.

Each file is placed by asking the database, not by guessing at the name:

    ใบแจ้งซ่อม<JOBID>.pdf · repairform_<JOBID>.pdf   the job → its machine → its plant
    PM_<ASSET>_<date>.pdf                            the PM job for that asset and day
    รายงานการซ่อมบำรุง_<CODE>_<date>.pdf              the plant is already in the name
    รายงานการซ่อมบำรุง<date>.pdf · F-SP-ENG02-06_…    a SHARED daily report — belongs to
                                                     no plant; goes to _shared

Anything it cannot place with certainty is left exactly where it is and listed at the
end, so a doubtful file is never filed under the wrong company.

    python migrate_archive.py                                  # dry run, changes nothing
    python migrate_archive.py --write                          # move them
    python migrate_archive.py --db "D:\\bflfp-cmms\\data\\cmms.db" --write

Nothing is ever deleted or overwritten: if a file of the same name is already at the
destination, the move is skipped and reported.
"""
import argparse
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

SHARED = "_shared"
DAILY_PATTERNS = (
    re.compile(r"^รายงานการซ่อมบำรุง(?:ประจำวัน)?[ _]?(\d{2}-\d{2}-\d{4})\.pdf$"),
    re.compile(r"^F-SP-ENG02-06_(\d{2}-\d{2}-\d{4})\.pdf$"),
    re.compile(r"^MaintenanceReport_(\d{2}-\d{2}-\d{4})\.pdf$"),
)
DAILY_TAGGED = re.compile(r"^รายงานการซ่อมบำรุง_([A-Za-z]+)_(\d{2}-\d{2}-\d{4})\.pdf$")
# Two to four letters, not exactly three: from 1 Oct 2026 a CM is numbered with its
# reporting department's code, and IT is two letters — IT-2610-001 is a real job number
JOB_THAI = re.compile(r"^ใบแจ้งซ่อม([A-Z]{2,4}-\d{4}-\d+)\.pdf$")
JOB_ASCII = re.compile(r"^repairform_(?:\d{2}-\d{2}-\d{4}_)?([A-Z]{2,4}-\d{4}-\d+)\.pdf$")
PM_SHEET = re.compile(r"^PM_(.+)_(\d{4}-\d{2}-\d{2})\.pdf$")


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(here / "data" / "cmms.db"))
    ap.add_argument("--root", default="", help="the Report folder (default: data/Report)")
    ap.add_argument("--write", action="store_true", help="actually move (default: dry run)")
    a = ap.parse_args()

    root = Path(a.root or (Path(a.db).resolve().parent / "Report"))
    if not root.exists():
        sys.exit(f"no report folder at {root}")
    if not Path(a.db).exists():
        sys.exit(f"no database at {a.db}")
    c = sqlite3.connect(a.db)
    c.row_factory = sqlite3.Row
    codes = {r["id"]: (r["code"] or "").strip() for r in
             c.execute("SELECT id, code FROM factories")}
    known = {v for v in codes.values() if v}

    def plant_of_job(jobid):
        r = c.execute("""SELECT COALESCE(m.factory_id, j.factory_id) f
                           FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
                          WHERE j.jobid=?""", (jobid,)).fetchone()
        return codes.get(r["f"]) if r and r["f"] else None

    def plant_of_pm(asset, day):
        rows = c.execute("""SELECT DISTINCT COALESCE(m.factory_id, j.factory_id) f
                              FROM jobs j JOIN machines m ON m.id=j.machine_id
                             WHERE j.jobtype='PM' AND m.code=?
                               AND SUBSTR(COALESCE(j.done_at, j.planned_date),1,10)=?""",
                         (asset, day)).fetchall()
        if len(rows) == 1 and rows[0]["f"]:
            return codes.get(rows[0]["f"])
        # no job matched — fall back to the asset register, but only if the code
        # belongs to exactly one plant; W01FP01 exists in two, and a guess there
        # would file one plant's PM sheet under another's name
        rows = c.execute("SELECT DISTINCT factory_id f FROM machines WHERE code=?",
                         (asset,)).fetchall()
        return codes.get(rows[0]["f"]) if len(rows) == 1 and rows[0]["f"] else None

    moves, shared, stuck = [], [], []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in known | {SHARED}:
            continue                                  # already sorted
        if len(rel.parts) != 4:
            stuck.append((rel, "not a YYYY/MM/DD file"))
            continue
        y, m, d, name = rel.parts
        plant = why = None
        if (mt := DAILY_TAGGED.match(name)):
            plant, why = mt.group(1), "the plant is in the name"
        elif any(p.match(name) for p in DAILY_PATTERNS):
            why = "daily report with no plant — shared by all three, belongs to none"
        elif (mt := JOB_THAI.match(name)) or (mt := JOB_ASCII.match(name)):
            plant = plant_of_job(mt.group(1))
            why = f"job {mt.group(1)}" + ("" if plant else " — not in the database")
        elif (mt := PM_SHEET.match(name)):
            plant = plant_of_pm(mt.group(1), mt.group(2))
            why = f"PM on {mt.group(1)}" + ("" if plant else " — asset is in two plants "
                                                             "or has no PM job that day")
        else:
            why = "unrecognised name"
        dest_plant = plant or (SHARED if why.startswith("daily report") else None)
        if not dest_plant:
            stuck.append((rel, why))
            continue
        dest = root / dest_plant / y / m / d / name
        (shared if dest_plant == SHARED else moves).append((rel, dest, why))

    print(f"\n{root}\n{'MOVING' if a.write else 'DRY RUN — nothing will be moved'}\n")
    for title, group in (("SORTED BY PLANT", moves), ("SHARED — no plant owns these", shared)):
        print(f"── {title}: {len(group)}")
        for rel, dest, why in group:
            print(f"   {rel}\n     → {dest.relative_to(root)}   ({why})")
            if a.write:
                if dest.exists():
                    print("     SKIPPED — a file of that name is already there")
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(root / rel), str(dest))
        print()
    if stuck:
        print(f"── LEFT WHERE THEY ARE: {len(stuck)}  (placed by hand, or not at all)")
        for rel, why in stuck:
            print(f"   {rel}   ({why})")
        print()
    if not a.write:
        print("run again with --write to move them")
    else:
        print("done · the app reads both layouts, so nothing breaks either way")
        print("NOTE: the _shared daily reports are superseded as you press Re-issue on")
        print("      each plant's row — that writes a properly named file per plant.")
    c.close()


if __name__ == "__main__":
    main()

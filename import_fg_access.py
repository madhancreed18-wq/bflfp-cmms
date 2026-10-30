"""Copy the FG team's Access history into the CMMS database (FG Stock app).

    python import_fg_access.py --plant PC --csv fg_import
    python import_fg_access.py --plant PC --accdb "E:\\github-projects\\WMS V2\\Stock_FinishedGoods New\\Stock_FinishedGoods New.accdb"

--csv    a folder of CSV files, one per Access table (tbl_Product.csv, tbl_Receive_Product.csv, ...)
--accdb  read the .accdb directly — needs Windows, pyodbc and the Microsoft Access Database
         Engine with the same 32/64-bit as Python.

Safe to run again: rows are keyed on the Access ID, so only new rows are added. Keep running
it until go-live while the team is still typing into Access. Stop the CMMS first, or run it
against the dev copy (set BFLFP_DATA=data-dev) to try it out.
"""
import argparse
import os
import sys
from contextlib import closing

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server.db import db, init          # noqa: E402
from server import fg                   # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plant", default="PC", help="factory code: BFL, FP or PC (default PC)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--csv")
    g.add_argument("--accdb")
    a = ap.parse_args()
    init()
    with closing(db()) as c:
        f = c.execute("SELECT id,name FROM factories WHERE code=?", (a.plant.upper(),)).fetchone()
        if not f:
            sys.exit(f"unknown plant {a.plant}")
        data = fg.read_csv_folder(a.csv) if a.csv else fg.read_accdb(a.accdb)
        print("read:", ", ".join(f"{k} {len(v)}" for k, v in data.items()))
        fg.import_access(c, f["id"], data)
        print("into plant", f["name"])


if __name__ == "__main__":
    main()

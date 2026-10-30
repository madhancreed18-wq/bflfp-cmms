"""Copy the old packing-room slips (VB PalletBarcode app, SQL Server database PalletSlip)
into the FG Stock app, so old EAN-13 slips still scan at FG receive.

Straight from the SQL Server (Windows, `pip install pyodbc`, ODBC Driver 17/18 for SQL Server):
    python import_palletslip.py --plant PC --server "BFLPC114\\PALLETFORM" --user sa
    (the password is asked for — it is never stored in this file)

Or from CSV exports (SSMS: right-click the table > Select > save results as CSV with headers):
    python import_palletslip.py --plant PC --csv-dir palletslip_export
    (folder holds PalletForm.csv and WarehouseData.csv)

Safe to run again: slips are keyed on the old 12-digit Bar_Code.
"""
import argparse
import csv
import getpass
import os
import sys
from contextlib import closing

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server.db import db, init          # noqa: E402
from server import fg                   # noqa: E402

COLS = ("Slip_No", "Date", "Time", "Document_No", "Pack_No", "Formula_Code", "Formula_Name", "PD_Line", "Lot_No",
        "Pack_Type", "Bag_Weight", "TotalWeight", "Total_Bag_No", "MFG_Date", "Expiry_Date", "Staff_Name", "Bar_Code",
        "Approved", "Approved_by", "Approved_DT", "Rejected", "Rejected_by", "Rejected_DT")


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def read_sql(server, user, database):
    import pyodbc
    pw = os.environ.get("PALLETSLIP_PW") or getpass.getpass(f"password for {user}@{server}: ")
    drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
    if not drivers:
        sys.exit("no SQL Server ODBC driver installed")
    cn = pyodbc.connect(f"DRIVER={{{drivers[-1]}}};SERVER={server};DATABASE={database};UID={user};PWD={pw};"
                        "TrustServerCertificate=yes;")
    out = {}
    for t in ("PalletForm", "WarehouseData"):
        cur = cn.cursor()
        cur.execute(f"SELECT {', '.join('[' + c + ']' for c in COLS)} FROM dbo.[{t}]")
        rows = []
        for r in cur.fetchall():
            rows.append({k: ("" if v is None else (v.strftime("%Y-%m-%d %H:%M:%S") if hasattr(v, "strftime") else str(v)))
                         for k, v in zip(COLS, r)})
        out[t] = rows
    cn.close()
    return out["PalletForm"], out["WarehouseData"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plant", default="PC")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--csv-dir")
    g.add_argument("--server")
    ap.add_argument("--user", default="sa")
    ap.add_argument("--database", default="PalletSlip")
    a = ap.parse_args()
    if a.csv_dir:
        pf, wh = read_csv(os.path.join(a.csv_dir, "PalletForm.csv")), read_csv(os.path.join(a.csv_dir, "WarehouseData.csv"))
    else:
        pf, wh = read_sql(a.server, a.user, a.database)
    print(f"read: PalletForm {len(pf)}, WarehouseData {len(wh)}")
    init()
    with closing(db()) as c:
        f = c.execute("SELECT id,name FROM factories WHERE code=?", (a.plant.upper(),)).fetchone()
        if not f:
            sys.exit(f"unknown plant {a.plant}")
        fg.import_palletslip(c, f["id"], pf, wh)


if __name__ == "__main__":
    main()

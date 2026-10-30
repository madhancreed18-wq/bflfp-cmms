# FG Stock app (b467) — replaces the FG team's Access database

Third app in the CMMS login (CMMS · Warehouse · **FG Stock**). Page `/fg.html`, API `/api/fg/*`,
code `server/fg.py`, tables `fg_*` (created at startup). Mockup: Design canvas "FG Receive & Checker Mockup".

## Roles (FG › Users)
- **admin** — everything (CMMS admins are always FG admin)
- **receiver** — phone: scan pallet-slip QR → checklist OK/NG (+comment) → Approve / Reject
- **issuer** — phone: scan pallet (or type lot) → customer, release type, qty, plate → checklist → Confirm issue
- **checker** — PC: queue of phone-approved rows → Confirm (posts to stock) / Send back; history, stock, stock card, daily report, print form F-WI04-WHD01-04
- "FG only" tick = the login goes straight to /fg.html (no CMMS menu). FG › Users › New FG login makes such an account.

## Flow
1. Approve → `fg_receive` status `waiting`, packing record `moved_to_fg`. Reject → `fg_receive` `rejected` (never counted), packing `rejected_by_fg` + comments.
2. Checker Confirm → `confirmed` (in stock). Send back → `sentback`, packing back to `packed` (can be scanned again).
3. Issue → `fg_release` `waiting` (already reserves stock) → checker Confirm → `confirmed`.
Stock per product + lot = confirmed receive − (confirmed + waiting) release.

## Packing room — A6 QR pallet slip (b468, replaces the VB PalletBarcode app + EAN-13 + Crystal/RDLC)
- Roles **packing** (FG › New pallet slip / Pallet slips) and **packcheck** (FG › Packing check).
- New slip: doc no, packing machine, production line, pack type, product (from Products), lot, MFG (read from lot), EXP,
  bag weight (`20` or `1*20`), total bags, staff, number of pallets. Slip/pallet no. runs per product + lot (1, 2, 3…) like the old app.
- Prints A6 landscape (148 × 105 mm) from the browser: fields + QR (46 mm). Printer must be set to A6 landscape, margins none, scale 100%.
- QR text = JSON {Id, Product_code, Product_Name, Lot_No, Pallet, No, weight, MFG, EXP, Station, Operator}.
- Packing checker approves / rejects (reason). FG receive refuses a slip that is pending, rejected or cancelled.
- Edit only while FG has not received it (goes back to packing check — reprint); cancel needs a reason; nothing is deleted.
- Machines and lines: FG › Machines & lines (seeded from the old app's lists).
- A pallet key is product + lot + pallet no (one lot can hold several products).

## Old PalletSlip slips (SQL Server BFLPC114\PALLETFORM, database PalletSlip)
```
pip install pyodbc
python import_palletslip.py --plant PC --server "BFLPC114\PALLETFORM" --user sa      (asks for the password)
```
or `--csv-dir folder` with PalletForm.csv + WarehouseData.csv exported from SSMS. Re-runnable (keyed on the 12-digit Bar_Code).
Slips in WarehouseData come in as already moved to FG. Old EAN-13 labels still scan at FG receive / issue.
Cut-over: anything received from an old slip in this app must not also be typed into Access.

Security note: the old VB source has the SQL Server `sa` password written in Form1.vb, PrintReportA6.vb, Warhouse.vb and App.config — change it once the old app is retired.

## Access history import (run once now, again just before go-live)
```
python import_fg_access.py --plant PC --csv fg_import
```
`fg_import\*.csv.gz` = export of the .accdb taken 27 Oct 2026. Re-running only adds rows with new Access IDs.
For a fresh export straight from the .accdb (Windows + Access driver + `pip install pyodbc`):
```
python import_fg_access.py --plant PC --accdb "E:\github-projects\WMS V2\Stock_FinishedGoods New\Stock_FinishedGoods New.accdb"
```
Imported 27 Oct: 1,452 products · 69 customers · 9 types · 14 checkers · 25 shift leaders · 28,456 receive · 151,858 release.
Access logins (tbl_User) are NOT imported — passwords there are plain text; make new accounts.

## Open questions
- Group mapping for the "Brand / B2B / BB" total: Owner "Brand…" → Brand, "OEM…" → B2B, "BB" → BB. 533 products have no Owner.
- Shelf life is 0 for every product in Access, so EXP is blank unless the packing record carries it.

# PR app (b587): purchase requests (ใบขอซื้อ)

This replaces the stand-alone **PR Document Tracking** app (Flask, its own users, `pr_tracker.sqlite`).
It is the fourth app in the Smart Factory login, after CMMS, Warehouse and FG Stock.

| | |
|---|---|
| Page | `/pr.html` (one file, no build, same look as `fg.html`) |
| API | `/api/pr/*` (`server/pr.py`) |
| Quotation reader | `server/pr_scanner.py` (moved unchanged from the old `quotation_scanner.py`) |
| Tables | `pr_requests`, `pr_items`, `pr_files`, `pr_suppliers`, `pr_log`, plus the column `users.pr_role` |
| Files | `data/pr/` (inside `data/`, so the normal backup covers them) |
| Tests | `python tests/test_pr.py` (full flow, 47 checks) · `tests/smoke.py` has a short PR section |
| Old data | `python tools/import_pr_tracker.py --src "E:\github-projects\PR-Document-Tracking"` |

## Who can do what (`users.pr_role`)
Roles are set in **CMMS › Manage › Users** (the "Purchase requests (PR) — role" box) or in **PR › PR users**.
An empty role means no PR access, and the PR tile and tab are not shown. CMMS admins are always Super Admin.

| Role | Can |
|---|---|
| `user` | Create requests. Edit requests that are not approved or cancelled. Attach the quotation. Add a supplier. |
| `pr_admin` | Everything `user` can, plus the PR number, PR status and PR PDF |
| `po_admin` | Everything `user` can, plus the PO number, PO status and PO PDF, and editing or deleting suppliers |
| `approver` | Everything `user` can, plus approve, cancel (a reason is required) and reopen. Can edit approved requests. Can read the activity log. |
| `super_admin` | Everything above, plus deleting requests and setting PR roles |

The rights live in `CAN` in `server/pr.py`. The page only hides buttons. **The server is what refuses an action.**

## Flow and statuses
1. **Create.** The requester picks company, department, date, purpose, reason, an optional supplier, item lines and VAT 7 % yes/no.
   The document code is `DEPT-COMPANY-YYMM-NNN`, e.g. `ENG-BFL-2611-001`. Numbering runs across the whole month, the same as the old app.
   - With no amount or no quotation, the status is `waiting_quotation`.
   - With an amount and a quotation (or a PR PDF), the status becomes `pending_approval` by itself (`_restatus()`).
2. **Approve or cancel.** Only the approver does this. Approving needs an amount and a quotation. Cancelling needs a reason.
   An approved or cancelled request is locked for everyone except the approver and super admin.
3. **PR number** (PR Admin): `PRYYMM-XXXX`. Typing `2611-AB12` is enough. The PR status follows the number.
   Entering the number **also approves the request**, which is how the old paper flow worked. Cancelling the PR number cancels the request.
4. **PO number** (PO Admin): `<plant>-POXXXX-XXXX`. The plant prefix (`BFL`, `FP` or `PC`) is added from the company.
   Status is "sent to supplier" or "not sent to supplier".

Companies come from `factories`: BFL → `BFL`, FP → `BFLFP`, PC → `BFLPC` (`COMPANY_OF_PLANT`).
Departments come from the core `departments` table.
Money is stored as text (`"1234.50"`) and added up with `Decimal`, never float.

## Quotation scan
`POST /api/pr/scan` reads a PDF from its text layer. Scanned PDFs and photos need Tesseract OCR **with Thai** on the
server. Install it to `C:\Program Files\Tesseract-OCR`, or set `TESSERACT_CMD`. Without Tesseract, text PDFs still read.
Python packages: `pypdfium2` and `pytesseract` (in `requirements.txt`).

**Main OCR: OpenThai OCR Text** (iApp, Apache-2.0, CPU only, about 15 MB), with Tesseract as its backup. OpenThai
reads a scan first (the scan message says "OCR · OpenThai"). When its reading misses the items, the total or the
date, Tesseract reads the page too and the better reading is used (OpenThai on a tie). A date line only the other
reader found is added, because OpenThai skips hand-filled dotted lines. Set `PR_OCR_PRIMARY=tesseract` to put
Tesseract first again. Without OpenThai installed, Tesseract reads alone, as before. To install, in the app folder
with the app's `.venv`:

```
pip install openvino opencv-python-headless pyclipper numpy huggingface_hub
python -c "from huggingface_hub import snapshot_download; snapshot_download('iapp/OpenThai2.2-OCR-Text-20261127', local_dir='models/OpenThai-OCR-Text')"
```

Or put the model elsewhere and set `OPENTHAI_OCR_DIR`. Restart the app.
Use **OpenVINO, not onnxruntime**: OpenThai is only loaded when OpenVINO is installed, because onnxruntime
crashes the process on Windows with an old Visual C++ runtime (14.28 on the office PC).
The model folder `models/OpenThai-OCR-Text` is not in `.gitignore`; keep it out of commits.

## Developing the PR app: rules
- Change only `server/pr.py`, `server/pr_scanner.py`, `static/pr.html` and `tests/test_pr.py`.
  The core touch points are listed below. Change them only in a reviewed PR.
- Schema changes **only add**. New columns go into `ensure()` as `ALTER TABLE … ADD COLUMN` behind a `_table_cols` check.
  Never rename or drop a column.
- Do not import other modules (`fg`, `jobs`, …). Read core tables (`users`, `departments`, `factories`) or go through `db.py`.
- Every write calls `_log()`.
- API answers may gain fields, but never lose or change one.
- Before merging, run `python tests/test_pr.py` (must be all green) and `python tests/smoke.py`.
  Then add a CHANGELOG line, `CMMS bNNN — … (PR: …)`.

### Core touch points (b587)
| File | What |
|---|---|
| `server/app.py` | `pr.ensure()` at start-up. `pr.router` registered. |
| `server/auth.py` | `_pr_flags()` adds `pr_role` to `/api/me` and the login answer |
| `static/fnbar.js` | the `pr` function (shown when `me.pr_role`), `FnBar.url()` |
| `static/index.html` | Apps tile and icon (`APPIC.pr`), app names, the PR role box in Manage › Users (`UPRR`), `hasApps()` |

## Next steps (not built yet)
- **CMMS job → PR link.** `pr_requests.job_id` already exists. Next: a "Raise PR" button on a job that is on Hold
  waiting for parts, and the PR / PO status shown on the job page.
- The paper form F-SP-PUR02-04 print (book / sheet numbers) and the P/R entry screen with LOT lines from the old app.
- Tasks on a request (the old `request_tasks` table was empty, so it was not carried over).
- Saved filters per user.

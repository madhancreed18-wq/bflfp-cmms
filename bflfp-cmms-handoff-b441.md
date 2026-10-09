# BFL Group CMMS — Handoff (up to build b441, 8 Oct 2026)

## 1. System
- CMMS covering 3 plants: **BFL** (factory_id 1), **BFLFP** (2), **BFLPC** (3).
- Backend is FastAPI + SQLite (`server/*.py`). Frontend is a single file, `static/index.html`, with a service worker in `static/sw.js`.
- Dev repo on the PC: `E:\github-projects\bflfp-cmms`. Its source is updated through b441.
- Live server: `D:\bflfp-cmms`, reached via AnyDesk.
- Real DB on the PC: `E:\github-projects\bflfp-cmms\data\cmms\cmms.db`. The latest copy analysed was from 7 Oct.

## 2. Deploy steps
1. Stop the app (server builds only).
2. Run: `cd D:\bflfp-cmms; powershell -ExecutionPolicy Bypass -File deploy\copy_to_server.ps1 -Zip _to_server\bflfp-cmms-bNNN.zip -Dest D:\bflfp-cmms -AppUrl http://localhost:8000` (type it without quotes; quotes cause the PowerShell `>>` prompt).
3. Start the app.
4. Press Ctrl+F5 and check that the footer shows `ui bNNN`.
- UI-only builds: copy `index.html` and `sw.js` from `_to_server\bNNN_static\` into `D:\bflfp-cmms\static\`. No stop is needed.

## 3. Build rules
- Bump `UIBUILD='bNNN'` (single quotes) in `static/index.html`.
- Bump `CACHE "bflfp-vNNN"` in `static/sw.js`.
- `jobs` is a VIEW over the per-plant tables. Add new jobs columns in the `db.py` migration list (`adds`), never with ALTER.
- One-time repairs are flags inside `elec.ensure_board`: `jobs:board-backfill`, `users:central-plant`, `users:central-remap`.
- Zips go to `E:\github-projects\bflfp-cmms\_to_server\bflfp-cmms-bNNN.zip`.

## 4. Builds b422–b441 (what changed)
- **b422 — two-part PM:**
  - A plant technician's Stop completes the mechanical half.
  - Central Electrical (CE) has its own Start/Stop for the electrical points.
  - The planner sees a Mechanical/Electrical status card; signing is blocked until both halves are done.
  - CE technicians are blocked from the plant Start (this was the ERR-LV7I popup).
- **b423:**
  - All-electrical PM jobs are removed from plant technicians' My jobs and KPI.
  - The CE technician page has My jobs / Completed tabs and a KPI.
- **b424:** CE planners get the full planner menu, with Electrical Planning in place of the plant Plan/PM/CM.
- **b425:** The "All plants" person save is fixed (value 0 was being dropped). All-plants technicians appear in the plant lists, boards and KPI.
- **b426:** All-plants technicians are listed under each plant on the Manage page.
- **b427:**
  - The person dialog has a Team option: Plant / ⚡ Central Electrical.
  - New endpoint: `/api/admin/users/{uid}/to-central`.
- **b428:**
  - CE technicians can be assigned from the plant boards.
  - A CE technician on a plant crew works the whole PM sheet.
- **b429:** Moving a person to Central also moves his open jobs. `on_crew` uses `_tech_ids`.
- **b430:** New `jobs.board` column (`'ce'` or `''`) records which board owns a corrective job. Includes a backfill and a remap of moved people.
- **b431:** One person can be on several teams per day (lead on one, helper on another).
- **b432:** The Central CM board shows CE technicians plus the plant's own technicians (`&cm=1`, includes CMELEC).
- **b433:**
  - CE technicians are filtered by plant (`factory_id = fac` or 0).
  - Move-to-Central keeps the person's plant.
  - Rule: Central + All plants shows everywhere; Central + one plant shows in that plant and in Central only.
- **b434/435 — BD ring is now "Breakdown (BD) loss %":**
  - Formula: BD wall hours (report → Stop) ÷ (machines × 24 h × working days) × 100.
  - Target: under 3%.
- **b436:**
  - The BD ring drill-down lists BD jobs only.
  - CM plan right panel: unplanned jobs only, a search box by job code, a tree grouped by type (BD/CM/IMP/PRJ), and multi-select drag onto a day.
  - Planner phone "All jobs" is grouped by created day (today on top). Within a day: IMP, BD, CM, PM, then job number descending.
- **b437:** The BD ring turns green when under 3%.
- **b438:**
  - PM cycle-cover bug fixed: every PM job of a machine covers its cycle. This caused the duplicate PMs.
  - Project is also shown under Electrical Planning.
- **b439:** Clicking outside the Assign work board closes it, with a confirm if there are unsaved changes.
- **b440:** The Manage user list is side by side: phone accounts on the left, people on the right.
- **b441:** On the ใบแจ้งซ่อม (job request form) PDF, the ผู้แจ้ง (reporter) signature box stays blank unless a report-time signature exists. Before this fix, the reporter's acceptance appeared in both ผู้แจ้ง (reporter) and ผู้ตรวจรับงาน (inspector/acceptor). File changed: `server/jobform.py`. **This build needs a server restart.**

## 5. Key code map
- `elec.py`:
  - `parts()`, `on_crew()`, `all_elec()`, `scope_of`
  - `ce_ids(c, fac)`, `_ce_techs`, `remap_person`, `ensure_board`
- `teams.py`: `day()` / `save()` with `_brd` board ownership. `cm=1` selects the Central CM board.
- Plant technician list SQL: `(factory_id=? OR COALESCE(factory_id,0)=0)`.
- `pm.py` `_occurrences`: the cycle-cover check.
- `kpi.py`: `bd_wall_min`, `work_days`, and the `cmjobs` `jt` param.
- `jobform.py`:
  - Builds the ใบแจ้งซ่อม (job request form) PDF.
  - Signature columns: `sign_requester`, `sign_inspector`, `sign_appr`.
  - Signer names come from the `sig_signers` table.
- `jobs.py`:
  - `/jobs/{jid}/media` saves signatures and photos.
  - `/signoff` approve writes `sign_appr`.

## 6. Open items / pending decisions
- **Downtime % KPI** (recommended per production line). The user still has to decide:
  - Mark lines as criticality A, or add a "line stopped" tick on the job.
  - Use a shift log, or a fixed schedule per line.
- Should BD/IMP jobs with Trade Electrical stay visible to plant planners? Not answered.
- PM plan bundling (KPI count per bundled job; keep each machine on its own week). Not answered.
- Suggested actions:
  - Cancel the duplicate PRM-2609-613.
  - Check the BFL PM start date, which was changed from 2026-09-17 to 2026-10-07.
- Data notes:
  - Nut (นัท, a person's name) shows Disabled because his person record moved to Central. His password is changed on login `techpc9`.
  - ธนพัฒน์ (Thanaphat, a person's name) has duplicate person records (ids 177 and 178, both BFL); these should be merged or cleaned.
- Pricing done (no build):
  - Development value about 4.5–6.5M THB (about 31k lines of code, 172 endpoints).
  - Base package price list at 525,000 THB (150 machines; 5 managers, 5 technicians, 5 reporters, 3 planners) plus add-ons.

## 7. Working rules (user preferences)
- Answer only what is asked. Keep answers to brief bullets, and use numbered steps for instructions.
- Always give Thai text with its English meaning, written as thai (english).
- Use real data only (copies of the real DB) for previews and screenshots; never mock data. Show a preview before implementing.
- Don't disturb jobs or plans that were already issued.
- Never enter real passwords. Temp passwords go only in DB copies, and the `.pw` file is deleted after tests.

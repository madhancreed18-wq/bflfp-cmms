# BFLFP CMMS — Session Handoff

_Living "where we are / what's next" note. Read this first when continuing (new chat: say "read docs/HANDOFF.md and continue")._

_Last updated: 1 Sep 2026 (engineering vs production downtime · job detail rebuilt · Hold → due-date rule on stage 2 · photo zoom viewer · planner review+sign on ใบแจ้งซ่อม · plan-day guard · Reports history rework · Jobs table rework · 45 electrical symptoms)._

---

## ▶ Start here

1. **Which PC am I on?** The **live server PC** runs the plant (public URL `cmms.bflgroup.workers.dev`); this machine, `E:\github-projects\bflfp-cmms`, is the **development** copy (`cmms-dev.bflgroup.workers.dev`). Ship to the server with `deploy/copy_to_server.ps1` - see **Deployment** below.
2. **Which copy am I running?** There are now two, side by side — see **Live vs test** below. `Start CMMS.bat` = **live** on :8000. `Start DEV.bat` = **test** on :8001 with its own data folder and an orange TEST SYSTEM stripe across every screen.
3. **Restart the app once** after a `run.py` change; after that it **auto-reloads** on any `server/*.py` edit. Startup banner should show `Push : ENABLED` and `Reload : ON`. Both bats run the **venv** Python.
4. **Refresh phones + PC after a frontend change.** `index.html`, `sw.js` and `manifest.json` are served `no-store` and the service worker is **network-first**, so a phone with signal always gets the new build — but iPhone still wants the installed PWA fully closed and reopened. Backend changes need the reload/restart, not a browser refresh.
5. **Assets**: 1,605 in `machines` (BFL 716 · FP ~158 · PC 731). Data is **factory-scoped** — log in via the right factory tile. Add/refresh via **Planner → Import assets**.
6. **Test the completion loop**: tech1 → New Jobs / My jobs → open a job → pick the name in *ช่างผู้ทำงาน* → Start → **Stop** → work report + sign → **Completed · awaiting approval** → op1 → Pending approval → Approve (sign) / Reject (reason required). Rejected → back to the tech **Rejected** tab.
7. Technician photos upload through **Manage → Users** (stored in `data/uploads/`). The old `static/techphotos/` Thai-name matching is used by `planreport.py` only.

---

## What this is

Self-hosted **CMMS** for **Bluefalo Group** (Thai food/petcare). Factories: **BFL** (1), **FP/BFLFP** (2), **PC/BFLPC** (3).
Python **FastAPI** + SQLAlchemy (SQLite `data/cmms.db`, Postgres-portable via `DATABASE_URL`). Single-file PWA `static/index.html` (vanilla JS, bilingual TH/EN via `LANG`+`t()`). Service worker `static/sw.js`. VAPID web push. Roles: operator (Reporter), technician, planner, manager, admin — one login routes by role via `buildNav()`.

Run: `python run.py` (or `Start CMMS.bat`) → localhost:8000, auto-reload on. Public URL: **https://cmms.bflgroup.workers.dev** via `deploy/start_tunnel.py` (run.py auto-starts it). No SSL certificate is needed — the Cloudflare tunnel terminates HTTPS.

**Status flow:** Reported → (planner) Assigned/Released → InProgress ⇄ Paused/Hold → **ServiceCompleted** ("Completed · awaiting approval") → **Done** (op approves) or **Rework** (op rejects → back to tech).

---

## Live vs test — two copies of the same code

Same code, different data folder. Nothing in one can touch the other.

| | live | test |
|---|---|---|
| start | `Start CMMS.bat` | `Start DEV.bat` |
| port | 8000 | 8001 |
| data | `data\` | `data-dev\` |
| tunnel | on (public URL) | **off** — office network only |
| on screen | normal | orange **TEST SYSTEM** stripe |
| push titles | normal | prefixed **`[DEV]`** |

Driven by two environment variables, both set inside the bat file: `CMMS_ENV` names the copy (`config.ENV` / `IS_LIVE`), `BFLFP_DATA` relocates the whole data folder (database, uploads, reports, VAPID key).

**Push works on the test copy on purpose** — you cannot test notifications without a buzz. It cannot reach a real technician by accident: subscriptions live in the data folder, so :8001 only ever pushes to devices that subscribed *on* :8001, and every title carries `[DEV]` on the lock screen.

`python refresh_dev.py --apply` copies today's live data into `data-dev` (live is only ever read), then resets every password in the copy to `1234` and clears push subscriptions.

---

## Role screens (current)

- **Technician** — 3 tabs: **งานใหม่วันนี้ / New Jobs** (badge; unassigned reports waiting to be handed out) · **งานของฉัน / My jobs** · **ถูกตีกลับ / Rejected** (badge; Rework). Views: `technew`, `techassigned`, `techrejected`.
  - **My jobs = last 30 days, every status, plus the unassigned pool.** It carries this crew's work at every stage *and* every report nobody has been given yet, so an operator's report is visible to every technician in the factory until the planner assigns it. Chips: ทั้งหมด · **แจ้งใหม่ / New report** · รอ · ค้าง · กำลังทำ · รอตรวจรับ · เสร็จ · พัก · ตีกลับ, each with a count.
- **Operator** — 3 tabs: **All Jobs** (own reports, last 30 days, every status, same chip set) · **Pending approval** (badge; `ServiceCompleted`) · **Approved** (last 60 days). Views: `mine`, `toapprove`, `approved`.
- **Planner** — nav: **Planning** (read-only Plan calendar by default, with **PM plan · CM Plan · Jobs** nested), **Dashboard**, **Machine list**, **Assets**, **Stages**, **Import assets**, **Hours**, Chat, More.
- **Admin** — planner's nav plus **Manage**, and sees **all three factories** with a plant filter; planner/technician/operator see only their login factory.

---

## Built this stretch (Aug 2026)

### Assets page — full-row editing

- **Click any row to edit it.** Every column becomes an input in place: Asset ID, Name, Category, Group, Crit, and in **Full** view also Line, Floor, Department, Manufacturer, Brand/model, Serial, Size, Year, Last PM, Remark. **Enter** saves, **Esc** discards; Save/Cancel sit in the frozen Actions column. The tick box and the ✎/▤ buttons keep their own jobs and do not open the editor.
- **PM group and PM sheet are editable too** (Aug 2026) — every one of the seventeen columns now opens. Neither is a plain machine column, so each has its own path: **PM sheet** is a picker of the factory's 80 template names and pins the asset to that template (a `pm_members` row — the explicit assignment `_match_all()` already preferred); **PM group** is a picker of the colour groups and writes `machines.pm_group_color`, which beats the colour the template brings — and **a name nobody has used yet starts a new group** on the next free colour from `NEW_GROUP_COLORS`, so the register can grow area groups (colour first, rename to the area from the Colour groups row). The chips row now counts assets by the colour actually in force, pinned ones included, and lists groups that have no template of their own. **Leave either blank and the asset goes back to matching by name.** A name that is not a real sheet or group is refused (bilingual 400) and nothing on the row moves. Pinning a sheet also changes which frequencies the asset gets, since `_machine_freqs()` reads the same match. Saving either column re-reads the register from the server, as `code` and `name` already did.
- A column hidden in Compact view is carried through the draft untouched, never blanked. Switch to **Full** to reach those fields.
- **Server**: `ASSET_EDIT` in `pm.py` lists the fifteen plain machine columns; `PATCH /api/pm/assets/{id}` also takes `pm_sheet` and `pm_group` (resolved and validated *before* anything is written, so a typo cannot half-move an asset). It refuses a blank Asset ID or one already used in the factory (bilingual 400) and the row stays open with the typing intact. `GET /api/pm/assets` now returns `sheets` — every template name in the factory — so the edit row can offer them all, not only the ones an asset already matches.
- **Excel-grid look**, matching the All-jobs Table view: dark sticky header, hairline borders, zebra rows, checkbox + Asset ID frozen left, Actions frozen right — in read, Compact, Full **and** edit mode. Constants `ASTH` / `ASTD` / `ASIN`.
- **Export Excel** → `GET /api/pm/assets/export` (openpyxl, 19 columns, freeze `C2`, auto-filter, honours the tab + search on screen).
- **Search covers Asset ID, Name, Group and Category.** A Group chip row was built and then removed at request — the header is back to one line and the table starts ~130px higher. Typing `PACKING` in Search is how you pull up a group now.
- The **Colour groups (PM)** row is still there and still renameable in place. `import_pm_colors.py` has been run: green 36 · amber 30 · purple 28 · yellow 43 · blue 17 · red 1.

### Jobs

- **An operator's report reaches everyone.** `techassigned` now unions the crew's own work with `lead_tech IS NULL AND status IN (Reported, WaitingApproval, WaitingAssignment)`. Verified: a fresh report appears in both technicians' My jobs; assigning it to tech1 drops it off tech2's list the same minute.
- **Derived status labels, nothing stored, no nightly job**: `jobUnassigned()` → *ยังไม่มอบหมาย / Not assigned*; `jobIsPending()` → *ค้าง / Pending* (given to someone, deadline gone, Start never pressed); `jobDue()` defaults the deadline to **planned date + 1** when no due date is set. `jobBucket()` maps a job to one chip; `PEND_OPEN` is the open-status list they all key off.
- **Unassigned is never Pending** — nobody was given it, so nobody is late.
- **A due date can never precede its planned date** — guarded in `update_job` and `plan_jobs` (400, bilingual). The ten bad rows already in the database were pulled up by `fix_due_dates.py`.
- **Released → Assigned** in every label (`STTH` / `STEN` / `RCSTL`); `Assigned` itself now reads *ยังไม่ปล่อยงาน / Not released*. The planner tab is **Assigned work**.
- **One job-numbering rule: `next_jobid()` in `db.py`** — five places invented their own id. Now: **PRD** corrective · **BKD** breakdown · **PRM** preventive, `PREFIX-YYMM-NNN`, restarting at 001 each month, counted from **MAX(existing number)** not `COUNT(*)` (the old code reused a number after any deletion).
- **All jobs (planner)** — "Recent" renamed; Assigned work / OverDue / On Hold / Due today / Rejected / Unassigned moved off the top tabs into **filter chips** with counts (`RCVIEWS` predicates + `rcSetFV`); only **History** stayed on top.
- **Re-plan dialog** — Helpers removed. A team **checkbox list with a leader tick**, and only two dates: **Planned** + **Due**.
- **Finished work is a record, not a task** — `ServiceCompleted / Done / Rejected / Cancelled` are CLOSED on the tech screen: status card instead of buttons, work report read-only, plus the technician who actually did it from the last work timelog. `seg_start` refuses those statuses with 409 so a stale page cannot restart closed work.

### Jobs — fixed 26 Aug 2026

- **One tap, one work order.** A phone on a slow link shows nothing while a report is saving, so Save got pressed again — one report became four jobs and four rounds of push. `create_job` now returns the job that already exists for an identical report (same machine, same words, same person) **while that job is still open** (`OPEN_STATUSES`), plus a `DUP_WINDOW` of 120 s for one closed immediately. Client side, `opSave` refuses re-entry and greys the dialog out with *กำลังบันทึก…*.
- **`pmOpenJob()` was dead.** It looked for `openJob` / `showJob` / `jobDetail`, none of which exist, so clicking a CM job did nothing — from the Jobs table, the CM plan list, the calendar chips, everywhere. It calls `openJobById()` now, which picks the detail screen for the signed-in role.
- **A job can be retired.** `Cancelled` was a label nothing could set. The job detail screen now has **ยกเลิกงาน / Cancel job** (planner + admin, reason required, `CANCELLABLE` statuses) and **ลบถาวร / Delete** (admin only, `DELETE /api/jobs/{id}`, removes the job and its seven child tables — for duplicates, where even a cancelled row is clutter).
- **An unowned job can be claimed by name.** `_acting_tech` fell back to *technicians sharing this login*, but the phone offers every technician in the factory — so with `users.login_id` still empty (no day teams set up yet) every choice was refused with "that technician is not on this job". A job with **no** team now accepts any active technician in the factory (`_factory_tech_ids`); a job **with** a team still only accepts that team, and the refusal names the person and the team.
- **Start / Hold / Stop no longer swallow the error.** All three were `try{…}catch(e){}` and carried on: Stop uploaded the signature, closed the dialog and announced *"บันทึกงานเสร็จ + เซ็นแล้ว"* for work the server had rejected. Each now returns on failure, leaving the job untouched.
- **Two different approvals, told apart.** `WaitingApproval` is the operator's *cost* request (ต้องขออนุมัติ) and is decided by the **planner**; `ServiceCompleted` is finished work the **operator** accepts, which is what the operator's *รอตรวจรับ / Pending approval* tab lists. Nothing in the app ever called `POST /api/jobs/{id}/request-approval`, so a cost request sat at WaitingApproval for ever, in no queue. The planner's job screen now has **อนุมัติคำขอ / ไม่อนุมัติ**, and the status label reads *รออนุมัติค่าใช้จ่าย / Awaiting cost approval* so it is not mistaken for the operator's tab.

### Reports — the two daily sheets (28-29 Aug 2026)

Two different pieces of paper, one set of numbers.

- **The plan sheet** (`planreport.py`) is what goes out in the morning: the day's jobs, the crews, the signatures. Fixed row counts with blank rows padding the rest - **12** on a page that also carries crews and signatures, **22** on a continuation page (`DR_ROWS_LAST` / `DR_ROWS_CONT`) - so the signature block sits on the bottom line of the page instead of wherever the jobs happen to end. Real logo, A4 landscape.
- **The completed sheet** (`reports.py`) is the plant's own controlled form **F-SP-ENG02-06**. It gets filed and audited, so it is built to match the paper exactly: the seven Thai column headings, 14 lines whether or not they are filled, the note at the foot and the ผู้ทวนสอบ signature line, all in the document's own wording rather than ours. The clearance tick is **drawn as two lines**, not typed - every Thai font on the machine lacks U+2713 and printed a box.
- **The two sheets agree with each other by construction.** `_day_jobs()` / `_day_counts()` in `planreport.py` are the one day model both of them divide: the four status slices total N, and figures print as `n / total` rather than as bare counts, so the morning plan and the evening report reconcile on the same denominator.

**Filing is automatic.** Every route that produces the completed form goes through `file_daily()`, so a report cannot be handed to somebody without also being kept: `data/Report/YYYY/MM/DD/`, **one file and one shelf row per day**, updated rather than duplicated. (A planner who checked the sheet four times before the shift ended used to leave four entries and a pile of near-identical PDFs.)

**Fixed 29 Aug 2026:**

- **Opening the report screen used to download a PDF.** The preview was an `<iframe>` pointing at an endpoint that sends `Content-Disposition: attachment` - a phone does not render an attachment, it saves it, so every day flicked past landed in Downloads and the box stayed blank anyway. The iframe is gone. The screen now asks `GET /api/reports/daily/summary` what the day holds (jobs, PM/CM/BD, cleared, missing causes, already-filed) and never fetches the document until somebody asks for it. **เปิดดู** passes `inline=1` to view; **ดาวน์โหลด** stays an attachment to save.
- **A day with nothing finished on it says so.** Both buttons grey out, and the server answers 404 rather than build a blank form - which would otherwise archive an empty sheet and put a phantom entry on the Reports list, so the day would look reported when no work was done.
- **The file is named in Thai**: `รายงานการซ่อมบำรุงdd-mm-yyyy.pdf`, in the folder and in the download. A Thai name cannot travel in the plain `filename=` field - that field is latin-1 only, and one Thai byte makes the whole header invalid, after which browsers save the file as `daily` - so it goes in **RFC 5987 `filename*`**, with the old English name beside it for anything too old to read that. The name has changed twice now; `archive_found()` reads all three spellings so nothing already filed disappears from the Reports list, and regenerating a day deletes the older-named copy instead of leaving two PDFs of the same report.

### ใบแจ้งซ่อม per job — F-SP-ENG02-03 (29 Aug 2026)

The third sheet: **one repair-notification form per CM/BD job**, built to match the plant's template (`CM Template/CM Template.pdf`) exactly — the three sections, the blue bars, drawn checkboxes (Thai fonts lack the glyphs), the ผู้แจ้ง/ผู้รับแจ้ง/ผู้อนุมัติ/ผู้ตรวจรับงาน signature boxes and the ก่อน/หลังจาก photo panel.

- **`server/jobform.py`** — `build_job_form()` renders it; `file_job_form()` archives it (`data/Report/YYYY/MM/DD/ใบแจ้งซ่อม<jobid>.pdf`, the day the job finished) and puts **one row per job** on the Reports shelf (`plan_reports`, `kind='jobform'`, `shift` holds the job number as the dedupe key). Same contract as `file_daily`: every route that produces the form also keeps it.
- **Filed automatically on acceptance** — the operator's Approve in `signoff` calls `file_job_form` (guarded: a PDF failure can never undo an approval). Also regenerated+refiled every time the job-detail **ใบแจ้งซ่อม PDF** button is pressed, which now opens `?inline=1` for viewing instead of downloading (the same phone-downloads fix the daily sheet got).
- **What fills in from the data**: reporter + role from the requester's user row, machine, symptom (`problem_type` + `descr`); cause/fix from the work report; อนุมัติให้ซ่อมแล้วเสร็จภายใน ticked with the due date once planned, ผู้อนุมัติ/ผู้รับแจ้ง = whoever planned it (first `Assigned` job event); repair date+times from the work timelogs; stored signatures (`sign_requester`, `sign_tech`, `sign_appr`) drawn in where the app has them, blank to hand-sign where it does not; section 3 ticks from `cleared_worksite` / `pending_reason` / `new_issue_id`; photos from `img_before(2)` / `img_after(2)`, aspect kept.
- **PM jobs** still get a PDF from the button but are neither archived nor shelved — this form is corrective/breakdown paperwork. IMP jobs tick แจ้งปรับปรุง,แก้ไข instead of แจ้งซ่อม.
- **Reports screen has a third tab** — แผนงาน · งานที่เสร็จ · **ใบแจ้งซ่อม** — listing every filed form by job number; search finds job numbers. `/api/plan/daily/history` already returned all kinds, so only the frontend changed.
- **Photos print as pairs** (31 Aug): the first ก่อน/หลังจาก pair fills the space left on page 1, further pairs flow to page 2 under a repeated header; an approved (Done) job ticks ใช้งานได้ตามปกติ automatically.
- **PDFs preview in-app now** (31 Aug): `pdfView()` renders any PDF with vendored pdf.js (`static/vendor/pdf.min.js` + worker, in the SHELL cache) in a full-screen overlay — Download button, closes itself after download. Used by the job-form buttons, the daily-report เปิดดู, and the Reports shelf rows (plan sheets are HTML and still open in a tab). A new window on a phone downloaded the file and stranded the user on a blank orange window. `imgView()` does the same for photo thumbnails (tap to view full screen).
- A sample built from seed data sits at `CM Template/sample-jobform.pdf` for layout comparison.

### ใบแจ้งซ่อม — planner review + sign (31 Aug 2026)

- **The form cannot be downloaded until the planner has reviewed and signed it.** Opening the form (job-detail button or Reports shelf) on a job the planner has not yet signed pops a bilingual review dialog — *ตรวจสอบงานแล้วหรือยัง? / Have you reviewed this job?* (blue-header design, follows the app language). **Yes** → signature pad → the signature is stored as **`jobs.sign_inspector`** with **`jobs.inspected_at`** stamped server-side, then the PDF opens with Download enabled. **No** → the PDF still previews, but the Download button is locked. Once signed it never asks again (`jobFormOpen` checks `sign_inspector`).
- **One signature, two boxes.** `sign_inspector` prints centred in both **ผู้รับแจ้ง / Receiver** and **ผู้อนุมัติ / Approver** with the `inspected_at` date under each. ผู้แจ้ง falls back `sign_requester` → `sign_appr`. Only planner/admin may upload kind `sign_inspector` (`job_media` guard).
- **Photos**: every photo input takes gallery **or** camera now (the `capture` attribute was removed — it forced camera-only). Tap a thumbnail → `imgView()` full-screen with **เปลี่ยนรูป / Change** and **ลบ / Remove** (remove blanks the column via `job_media {remove:true}`).

### Worked-by — substitute technician (31 Aug 2026)

A technician on leave means someone else works under that team's login. The *ช่างผู้ทำงาน / Worked by* dropdown now offers **two groups**: ทีมของงานนี้ (this job's team, listed first) and ช่างคนอื่น (ทำแทน) — every other active technician in the factory. `_acting_tech` accepts `team + _factory_tech_ids + _tech_ids`, so any factory technician can be named even on a job that has a team; the timelog credits the real person.

### Plan-day guard — issuing the daily plan (31 Aug 2026)

Everything keys off **the day selected on the Plan calendar** (`planIssueSmart(d0)` — the button and calendar double-click both route through it):

- **Selected day is today, after 12:00 noon** → bilingual *สายเกินไป / Too late* popup; no plan can be issued for today past noon.
- **Plan already exists for that day** → popup **แก้ไขแผนเดิม / Modify existing plan** (opens the assign board) or **สร้างใบสั่งงาน / Create work plan** (regenerates the sheet).
- **Future day with CM or PM missing** → popup offers to make the missing plan (opens the PM plan or CM plan page) or continue with what exists.
- **Crews already assigned on that day** (`t.jobs.some(j=>j.lead_tech)`) → the sheet is issued immediately, no questions — this is what killed the popup loop.
- **A future day's plan sheet shows planning cards, not progress**: `planreport.py _strip(k, future=True)` prints Planned jobs · มอบหมายแล้ว Assigned · ยังไม่มอบหมาย Not assigned · PM/CM split, instead of done/pending counts that make no sense before the day starts.

### Reports screen — history shelf (31 Aug 2026)

The Reports page is now a proper report history: **3 tabs — Daily report · ใบแจ้งซ่อม · Plan** (`RPT.kind`, default daily). Each tab lists **only the latest file per day**, named `<type> <jobno> ddMM-hhmm`, newest date on top, scrollable. Rows open in the in-app pdf.js viewer (plan sheets are HTML and still open in a tab).

### Jobs table (planner) — Trade + rework (b180-b181, 1 Sep 2026)

- **Trade column** (b180): shows the job's department/trade from `fault_category`, falling back to the category of its `problem_type` (`jobTrade()`), colour-coded per trade (`TRADE_COL`).
- **Table rework** (b181): **Helpers and Source columns removed.** *Machine / description* split into **เครื่อง / Machine** and a new **อาการที่แจ้ง (ผู้แจ้ง) / Issue reported (operator)** column (`j.descr || j.problem_type`, tooltip carries the full text), and **งานที่ทำ (ช่าง) / Work done (technician)** (`j.solution || j.maint_action`) added at the end. Table min-width 1260 → 1420px.

### Two oversight roles, told apart (b188, 1 Sep 2026)

The role stored as `manager` was, in practice, group oversight — every plant's numbers on one dashboard, the machine/OEE table, and no work orders at all. That is **Engineering Center**, and it now has its own name (`engcenter`), which leaves `manager` free for what a plant manager actually is: the planner's screens in read-only, on one plant.

| | Engineering Center (`engcenter`) | Manager (`manager`) |
|---|---|---|
| menu | Dashboard · Machines/OEE · Reports · Chat · More | Jobs · Dashboard · Stages · Reports · Chat · More |
| plants | **all three**, with the plant filter | its own login factory only |
| work orders | none | reads every one, changes none — **but may raise one** |

- **One-time migration** in `_migrate`: everyone holding the old role moves to `engcenter`, guarded by `app_state["role_split:engcenter"]` so it runs once and a manager created afterwards keeps the new role. Verified against a fixture: two old managers moved, second and third runs moved nobody, a manager created after the split stayed a manager.
- **`CROSS_FACTORY`** and the login's any-factory rule are `("engcenter", "admin")` now — a plant manager belongs to one plant, like the planner they shadow. `CROSSFAC()` on the frontend matches.
- **Both roles still get the alerts** a manager always got: breakdowns, holds and rejections notify `planner, manager, engcenter`. `teams.py`'s day board accepts both.
- **`roleLabel()`** gives every role a bilingual name — the sidebar and the admin's role picker now read *Engineering Center / ศูนย์วิศวกรรมกลาง*, not the raw key.

### Stage 1 / Stage 2 get names (b196–b198, 2 Sep 2026)

"Stage 1" and "Stage 2" told nobody anything, and the second one read **Assigned** for a job a technician was working on that minute — which is what made people think it had not been started.

- **สถานะงาน / Job status** — the phase: แจ้งแล้ว · กำลังทำ · พักงาน · ซ่อมเสร็จ · ตีกลับ · ยกเลิก.
- **รอใคร / Waiting on** — whose move it is, which is a different person at every step and is why it could not be labelled "by technician" once and left. The value names the role and **the colour carries it**: blue the planner owes something, orange the workshop has it, green production has it, amber it is parked on somebody outside, red it came back.

**The role is in the words, not only in the colour (b198).** The first cut wrote *รอมอบหมาย / To assign* and left the owner to the chip's colour. Two chips side by side then read as two statuses and nobody could tell whose end the job was sitting at — *"why i see 2 status but i dont from whose end or who is responsible"*. Every value now leads with the role, in both languages.

| stage2 | short (lists, table) | long (job screen) |
|---|---|---|
| Not assigned | ผู้วางแผน: รอมอบหมาย · Planner: to assign | ผู้วางแผน: รอมอบหมายงาน · Planner: to assign the job |
| Assigned | ช่าง: รอเริ่มงาน · Tech: to start | ช่าง: รอเริ่มงาน · Technician: to start work |
| Assigned **+ InProgress** | ช่าง: กำลังทำ · Tech: working | ช่าง: กำลังทำงานอยู่ · Technician: working now |
| Pending due date | ผู้วางแผน: รอกำหนดวัน · Planner: set due date | ผู้วางแผน: รอกำหนดวันแล้วเสร็จ · Planner: to set a due date |
| Wait for action | ผู้วางแผน: รออะไหล่ · Planner: parts / supplier | ผู้วางแผน: รออะไหล่ / ผู้รับเหมา · Planner: waiting for parts or supplier |
| Wait for approve | ผู้แจ้ง: รอตรวจรับ · Reporter: to accept | ผู้แจ้ง: รอตรวจรับงาน · Reporter: to accept the work |
| Approved | ตรวจรับแล้ว · Accepted | ตรวจรับแล้ว — ปิดงาน · Accepted — job closed |
| Rejected | ผู้แจ้ง: ตีกลับ · Reporter: sent back | ผู้แจ้ง: ตีกลับงาน · Reporter: sent the work back |
| Wait cost approval | ผู้วางแผน: รออนุมัติ · Planner: cost approval | ผู้วางแผน: รออนุมัติค่าใช้จ่าย · Planner: to approve the cost |

*ผู้แจ้ง / Reporter*, not "operator" — the person who raised the job is the one who accepts it back, and that is the word the plant uses. The parts wait is the **planner**, not a department: the planner is the engineering leader, and chasing a spare part or a supplier visit is his to do. The chip stays **amber** rather than planner-blue, because the job is parked on somebody outside the plant — a different thing from a planner decision he can make at his desk. The downtime KPI is unchanged: those minutes still book to engineering.

- **Nothing stored changed.** `S1L` / `S2L` / `s1Label()` / `s2Label(j, short)` / `s2Cls()` are a display map over the same `stage1` / `stage2` strings, so old rows, the two-stage logic and every query keep working. The one derived case is `Assigned + status InProgress` → *Working*.
- **Short in the table, long on the job screen**, with the full sentence in the cell's tooltip — the table already carries sixteen columns and the bilingual sentence would double the width. One language at a time, following the login.
- **Every list carries it** (b197): the planner's phone row (`ppRow`), the planner's card list (`pcard`, under the status chip on the right) and **`jobRow`** — the row the technician and the operator read, where the chip sits beside the trade. The first pass only did the table, the job screen and `ppRow`, so the two screens people actually live in still showed nothing.
- The Excel export's headers follow (`Job status` / `Waiting on`); its values stay the stored English, because it is data.

### The download that trapped the app on iPhone (b195, 2 Sep 2026)

Tapping **ดาวน์โหลด** on the daily report from an installed iPhone app opened the PDF full screen with no toolbar, no Close and no back gesture — the only way out was to kill the app and start it again.

The cause: `<a href="…pdf" download>`. iOS **ignores `download`** inside a Home-Screen app, so the tap navigated the app's one window to the PDF, and a standalone window has no browser chrome to escape from. The Open button was never affected — it goes through `pdfView()`.

**`dlFile(url, name, after)`** replaces every download link. It fetches the file as a blob first, then hands it over the way the device actually supports:

1. **The share sheet** (`navigator.share` with a file) where the browser has it — Save to Files, Mail, LINE. A dismissed sheet is not an error.
2. **A separate window** on an installed app — never this one, because navigating the app to a PDF is the trap.
3. **A plain `<a download>`** on a desktop, where it always worked.

Used by the daily report dialog and by `pdfDl()`, so the ใบแจ้งซ่อม download inside the viewer is fixed too. `pdfView(url, dl, dlname)` now carries the file name through, so a saved file keeps its Thai name instead of arriving as `daily`.

Checked all three branches: desktop clicks an anchor with `download` set on a blob URL; a standalone app opens a separate window and its own URL never changes; the share sheet wins where it exists and no window is opened behind it.

### Work that has no machine (b194, 2 Sep 2026)

A blocked drain in the restroom, a run of pipe, a door that will not close — none of it is in the asset register and none of it ever will be. The Create job combo refused anything that did not resolve to a machine, so that work could not be raised at all and went back onto paper.

- **The field now takes either.** A machine from the list, or free text. Typed text that matches nothing is accepted with an amber hint — *ไม่มีเครื่องนี้ในทะเบียน — จะบันทึกเป็นสถานที่/งานอื่น* — and Create enables. The label reads **เครื่องจักร · หรือสถานที่ / Machine · or a place**.
- **Where it is kept**: `machine_id` is null and the text goes into **`report_name`**, the job's own title, which already existed for the operator's report.
- **Where it shows**: `jobWhere(j)` = `mname` → `mcode` → `report_name`, wired into the Jobs table's Machine column, the job-detail header (which drops the `· code` when there is none), and the phone row (a ⌂ tile in place of the asset code). `jobform.py` prints the title on the ใบแจ้งซ่อม where the machine line would be.
- The confirmation screen names the place with a ⌂ and says plainly it is not an asset, and **Back** restores free text as it restores a machine.

**Known consequence:** a job with no machine belongs to no plant, because a job's factory is derived from its machine (`_job_factory`). The list queries carry `OR j.machine_id IS NULL`, so restroom work raised at FP is visible at BFL and PC too. The fix is a `factory_id` column on `jobs`, stamped from the creator at creation — not done yet.

### The phone Jobs screen, and the question that decides a breakdown (b191–b192, 2 Sep 2026)

- **The row carries the photo.** `ppShot()` fills the asset tile with `img_before` (or `img_before2`) and drops the asset code over it as a caption — the operator photographs the fault before anybody walks to the machine, so on a phone the picture is the fastest thing on the row to read. No photo, or one that fails to load (`ppShotFail`), and the tile is the code exactly as before.
- **The four counts moved onto All jobs** — Not assigned · Assigned · In progress · To accept, above the search box, and tapping one filters the list underneath. Then search, then the chips, then the list. **Today is gone for a manager** (`ppSegHtml`, and `plannerPhoneHtml` forces the tab), because that screen exists to assign a crew and a manager assigns nobody. The planner keeps it.
- **A manager cannot raise PM** (b192). PM work comes out of the PM programme on the planner's calendar — a page a manager does not have — so their type buttons are **CM · IMP** only, and `create_job` refuses `jobtype='PM'` from the role as well, bilingual 403. The rule holds whatever sends the request, not only the screen that hides the button.
- **Create job asks what only the floor can answer**: *ตอนนี้เครื่องหยุดหรือไม่ / Is the machine stopped right now?* — the same question the operator answers when reporting. **Stopped saves the job as a BD**, which is what puts it in the downtime figures; still running saves it as a **CM**, and `production_impact` records the answer either way.
- **It is asked after Create, on its own screen** (b193). As one field among eight it could be scrolled past, and it is the field that decides whether a machine's downtime is counted at all — so pressing Create hands over to a confirmation screen showing the machine, the words and the photo count, with two full-width choices that each say what will be saved. **Nothing is written until one is pressed.** `← กลับไปแก้ไข / Back` re-opens the form with every field and both photos still in it (`_njDraft` + `njBack()`), because a question somebody cannot answer yet must never cost them what they already typed. IMP skips the question entirely.

### Manager, trimmed to Jobs, and allowed to raise one (b189–b190, 1 Sep 2026)

- **Plan calendar and CM Plan are off the manager's menu** — those are boards for arranging a day's crews, and a manager arranges nothing. The menu is Jobs · Dashboard · Stages · Reports · Chat · More, and the phone bar matches (`PPBAR.manager`). The *Plan calendar* button on the Jobs tab row is behind `CANEDIT()` too.
- **A manager may raise a job**, using the planner's own Create job dialog — they walk the floor and see things. `CANNEW()` (planner · admin · manager) gates every entry point to it; `CANEDIT()` (planner · admin) still gates everything else, so Cancel, Move, the due date and the signatures stay read-only.
- **Reports shows two shelves, not three** (b190). The *แผนงาน / Plan* tab is the sheet a planner issues for a day's crews — a working document, not a record — so a manager gets รายงานประจำวัน and ใบแจ้งซ่อม only. A stale `RPT.kind` carried over from another login is snapped back to `done`, so the tab that is no longer offered cannot leave them staring at an empty page.
- Server-side, `create_job` accepts the role and stamps `jobsource='Manager'`, so a job raised this way is told apart from a planner's and from an operator's report. It enters the queue as `Reported` with no date and no crew, exactly like a report — the planner still dates and crews it.

### Manager — the planner's screens, none of the buttons (b187, 1 Sep 2026)

The role already existed (notifications have always gone to `role_ids('planner','manager')`) but had a four-item menu of its own. It now reads the plant the way the planner does and changes nothing.

- **Menu**: Plan calendar · Jobs · Dashboard · CM Plan · Stages · Reports · Chat · More. **Off the menu: PM plan, Machine list, Assets, Import assets, Hours** — the planner's working tools, and three of them write to the register. `PPBAR.manager` mirrors the planner's five-button phone bar, so the mobile UI follows without a second layout.
- **Nothing that writes is on screen**: every route into Create job is behind `CANEDIT()` (the All-jobs tab row, the More list, the CM plan board and both of its phone screens). Cancel job, Move job and Delete were already planner/admin only, as was the Plan calendar's Issue plan and its assign board.
- **The job screen reads.** The planner action card is replaced for a manager by the same card with its controls turned to text — due date, hold reason, and a line saying why it cannot be edited. Hiding it would leave a manager wondering whether a due date exists at all; a live date box that cannot save would be worse. Signature boxes are already non-editable for anyone but planner/admin.
- **Nothing was needed server-side.** `update_job` refuses any role outside operator/planner/admin/technician, planning fields are planner/admin only, and `job_media` already restricts every signature kind — a manager's write was a 403 before this change and still is. The screen now agrees with the server instead of offering buttons that could not work.
- **Assigning people**: Manage → Users already offers `manager` in the role list, so nothing changed there.

Note: the Dashboard's **📈 Machine list / OEE** button is the KPI table, not the asset register, and is still open to a manager.

### Work that slipped its day — carry forward (b186, 1 Sep 2026)

A plan nobody moves stops being a plan. Yesterday's undone jobs kept yesterday's date for ever: the morning sheet was wrong, today's plan did not include them, and the only reason the crew saw them at all is that My jobs ignores dates.

- **`roll_forward(c, fac)` in `jobs.py`** moves a factory's undone corrective work onto today — or the next working day, if today is a weekly-off day or a holiday. It runs **once a day per factory**, on the first `/api/bootstrap` of the day, with the marker kept in a new `app_state` table (`ensure_app_state` / `state_get` / `state_set` in `db.py`) so a restart cannot run it twice and push the plan another day out. A failure is swallowed — it must never keep anybody out of the app.
- **It moves loudly.** `planned_date_orig` (new column) keeps the day the job was first promised, `carryover` counts the slips, and each move writes a line into the job's own history. A job carried four times is not a scheduling detail, it is a question for the meeting.
- **PM is deliberately excluded** (`CARRY_TYPES = CM · BD · IMP · PRD`). The compliance question is "was the July checklist done in July", and quietly re-dating it into August is how a plant answers that wrongly. A missed PM stays on its date and reads as overdue until a planner re-dates it themselves. Closed work (`ServiceCompleted` / `Done` / `Rejected` / `Cancelled`) is untouched.
- **`next_workday()` in `db.py`** — skips weekly-off days and holidays from the factory's `hours_json`, with a 14-day guard so a bad holiday list cannot loop.
- **On screen**: a **ยกยอดมา / Carried forward** filter chip beside On hold, and a `↻N` badge on the Planned cell in the Jobs table whose tooltip gives the original date. Not the same as Overdue — an overdue job still carries the date it missed; a carried one has been given a new day and is quietly eating the plan.

Checked against a fixture DB: PM untouched, closed work untouched, today's work untouched, the counter and the original date correct across repeat slips, idempotent within a day, and a Sunday run landing on Monday.

**On the morning plan sheet**, `_dr_carry()` in `planreport.py` prints a block above the crews: *↤ ยกยอดมาจากวันก่อน / Carried forward — N งาน*, then one chip per job with the day it was first promised and ×n when it has slipped more than once, worst first. The chips are capped at eight with a +N tail, because the signature block is pinned to the bottom line of the paper and a block that grows would push it off. The ↤ marker on each table row stays. `JOB_COLS` now carries `carryover` and `planned_date_orig`.

### Create job, signature pad, default view (b185–b186, 1 Sep 2026)

- **One combo for 700 machines.** The machine field is a single control that opens as the list, narrows as you type (code prefix beats code substring beats name), takes arrow keys and Enter, and refuses to enable **Create** until the text resolves to a real asset. The type is three labelled buttons, priority follows the operator's own scale (ปกติ / เร่ง / ด่วน), and **two photo slots** attach `before` / `before2` after the job is saved — the job is what had to succeed, so a photo that will not upload says so rather than losing the job. **The planned date field was removed**: the planner gives the job its day and its crew on the Plan calendar.
- **Signature pads are bigger** — 190px on a phone, 240px on a PC (was 140), on a 960×300 canvas (was 640×190) so a signature stays sharp when the form prints. All three: technician finish, operator acceptance, planner review.
- **All jobs opens on the Table view**, and remembers whichever view was last chosen (`bflfp_rcview`).

### ใบแจ้งซ่อม — one signature, one box (1 Sep 2026)

**ผู้แจ้ง no longer borrows the acceptance signature.** It fell back `sign_requester` → `sign_appr`, on the reasoning that the reporter and the accepter are the same operator — but that printed the same mark in two boxes and dated a signature to a moment it was not given, on a controlled form that gets audited. ผู้แจ้ง now prints `sign_requester` only, and is left blank to sign by hand when there is none. **ผู้ตรวจรับงาน in section 3 is unchanged** and still prints `sign_appr` with `approver_name` and `approved_at` — the signature the operator draws when accepting the machine back. Nothing is collected at report time yet, so the box will be blank until a signature pad is added to the operator's report dialog.

### Downtime, split between the two departments (b184, 1 Sep 2026)

The argument in the production meeting was never about the total, it was about whose minutes they were. So the clock on a **BD** job is now cut in two, along lines the two departments draw themselves:

- **เวลาเสียของวิศวกรรม / engineering** — from the report (the operator confirming the machine is stopped) to the technician pressing **Stop**, and again from **every rejection** to the next Stop.
- **เวลาเสียของฝ่ายผลิต / production** — from that Stop until the operator **accepts** the machine back, or rejects it.

Two rules make it hold up: the spans are **back to back**, so engineering + production always equals report → final acceptance and no minute falls in a crack between them; and **waiting counts** — waiting for a technician, a spare part or a supplier is engineering's (the plant's own decision), waiting for acceptance is production's. Minutes are counted in **operating hours only**, the same clock MTBF already uses, so a Friday-evening breakdown does not book the weekend against the workshop.

- **`server/downtime.py`** (new) — `spans()` walks `job_events` and hands the job between the two sides on `ServiceCompleted` (→ production) and `Rework` (→ engineering), stopping at Done/Rejected/Cancelled; statuses that are not a handover (Hold, InProgress, Assigned) are deliberately ignored. `op_minutes()` is the working-window clock. `hold_inside()` reports how much of the engineering figure was a parts or supplier wait — a sub-figure, not a third bucket. A job with no events at all falls back to `done_at` / `approved_at`.
- **`GET /api/kpi/downtime?d_from=&d_to=`** — factory-scoped, BD only, `VOID_SQL` applied. Returns plant totals (with `eng_pct` / `prod_pct` / `mttr_min`), per machine, and per job. Asset-group hours override the factory default, as in the OEE engine.
- **Dashboard** — a panel under Throughput & Time: one bar for the plant split, the eight worst machines each with their own split, and 7/30/90-day chips.
- **Job detail** — a BD job carries its own three figures on the facts strip (วิศวกรรม · ฝ่ายผลิต · รวม), computed server-side in `/jobs/{id}/detail` so the job and the dashboard can never disagree.

Checked against nine hand-worked cases including a double rejection, an overnight hold, a Friday-to-Monday breakdown and a report outside working hours.

**Still open:** CM and PM jobs that stop a machine are counted nowhere — the split keys off `jobtype='BD'`, which keys off the operator's "can it still run?" answer at report time. The fix discussed was two real timestamps (เครื่องหยุดเมื่อ / back in production at) so downtime follows the machine rather than the job type.

### Job detail rebuilt, and what a held job is waiting for (b182, 1 Sep 2026)

**A hold now says who owes the next move.** Stage 2 was always about the crew — *Assigned* / *Not assigned* — which tells you nothing about a job that stopped for a part on order or a supplier visit. `stage_for()` in `db.py` takes **`due_date`** and, for **Hold and Paused**, returns **`Pending due date`** when there is none and **`Wait for action`** when the planner has set one: without a date the job sits in no day's plan and no chip on the Jobs list can chase it; with one it is parked, not forgotten. `set_stage()` reads the column, so every existing call site — `update_job`, `plan_jobs`, `signoff`, the assign board — restamps the pair the moment a date is saved; no new call was needed. The two fallback `stage_for()` calls in the job list pass `due_date` too, and `stagePair()` in `index.html` mirrors the branch for rows saved before the columns existed. **`_migrate()` restamps jobs already on Hold once**, so old rows do not keep the old label. Open question: whether **Paused** should follow Hold here or stay on *Assigned* — Paused is a break inside a shift, Hold is a wait on somebody outside.

**The planner's job screen (`renderDetail`) was rebuilt.** One header carries the identity — job number, machine, the **stage pair**, type, priority, trade — so this screen and the Jobs table say the same thing. Under it a fixed facts strip (แจ้งเมื่อ · วางแผน · กำหนดเสร็จ · ผู้รับผิดชอบ · เวลาที่ใช้ · อายุงาน) where **a missing due date turns that one cell red**. A held job then earns a banner carrying the `pending_reason` — until now visible only on the planner list — and, for planner/admin, **the due-date box itself**, so setting a date no longer means opening the re-plan dialog. Below: a progress rail (Reported → Assigned → In progress → Completed → Approved) off the real timestamps, with a red break where a job is held; the technician report; captioned before/after photos; **three** signature boxes, the third being the planner's `sign_inspector` that unlocks the PDF download; and a time log with a duration per row and a total, which is what the man-hour figure is built from. The chat column keeps its markup — `#jchatbox`, `#jmtxt`, `sendJobMsg`, `activityDlg` are untouched.

**`imgView()` is a zoom viewer.** A gasket seat is the evidence the planner signs off on and a 150px thumbnail shows none of it: **+ / −**, the wheel, pinch or a double tap zoom to 6×, drag pans once zoomed, **Esc** / the backdrop / ✕ closes and the keydown listener is torn down with it (`imgClose()` — `imgReplace` and `imgRemove` route through it too). The technician and operator screens get this for free, since they already called `imgView`.

Mockups of both layout ideas are kept at `docs/mockups/job-detail-redesign.html` (built) and `docs/mockups/job-detail-idea-b.html` (a single-column case-file alternative, not built).

### Problem types — 45 electrical symptoms (1 Sep 2026)

`add_problem_types.py` (root, same contract as the other scripts: dry-run default, `--apply`, backup first, server stopped, run on the owning machine) inserts **45 Thai electrical symptom types × all 3 factories** (`--factory` to limit; `--retire-old-electrical` to soft-retire the old ones). Idempotent — re-running says *to add: 0*. **Applied on the live server.** Note: each factory's picker counts its own rows (~65 = ~20 old + 45 new); the tap-open list caps at 40 entries but **search and the count cover all** — that cap is a UI choice, not missing data.

ui **b199** · cache **v173**.

### Deployment - a second PC (28 Aug 2026)

The project was cloned onto a **live server PC** at 09:30 on 28 Aug and runs the plant from there. This machine (`E:\github-projects\bflfp-cmms`) is now the development copy.

- **The two PCs cannot share a public URL.** The server owns **https://cmms.bflgroup.workers.dev**; this PC points at a second Worker, **https://cmms-dev.bflgroup.workers.dev**, with its own KV namespace. Which one a machine uses is `CF_WORKER_URL` in `deploy/secrets.env` - not a code edit, so the same checkout runs correctly on both.
- **`deploy/copy_to_server.ps1`** copies only the files changed since the clone, newest first, and **never touches `data\`, `.venv\` or `.git\`** - copying a development database over a live one is the one mistake that cannot be undone. `-WhatIf` shows what it would do without doing it. Keep its file list and its `ui bNNN` / `cache vNNN` comments current when you ship.
- **The token moved out of the code.** `deploy/secrets.env` (in `.gitignore`) holds `CF_API_TOKEN`, and on this PC `CF_KV_NAMESPACE_ID` and `CF_WORKER_URL` as well. `_load_secrets()` reads it at import and a real environment variable still wins, so a machine that uses `setx` is unaffected. Unlike `setx` it survives a reboot and needs no new terminal. **`python deploy/start_tunnel.py --check`** proves the credentials work and prints only a masked form of the token.
- **The tunnel used to die quietly.** The URL regex matched cloudflared's own log lines, so the KV key was rewritten dozens of times a day, and a single `ConnectionResetError` - caught only as `HTTPError` - took the tunnel down with it. Now only a genuinely new address triggers an API call; a KV failure retries four times and then carries on (the tunnel is up, only the redirect is stale); and cloudflared is terminated with its parent. `--protocol http2`, because QUIC rides on UDP and plenty of factory networks throttle it.
- **`run.py` waits a second and a half** before printing the public URL, and says the tunnel is **not** running if it has already exited. Printing an address nobody can reach sends people hunting the wrong problem.
- **VS Code**: `.vscode/launch.json` has a normal run and a no-reload debug profile; the tunnel child needs `"subProcess": false` or debugpy breaks on its `SystemExit`.

### Symptoms, and the assign board

- **The symptom picker was empty in production.** `/api/bootstrap` never sent the list and the admin routes did not exist at all. Bootstrap now returns `problem_types`, and `admin.py` has full CRUD on `/api/admin/problem-types` (categories `PCATS`: Mechanical · Electrical · Instrument · Process · Other). Delete is a **soft retire** (`active=0`) so jobs that already reference a symptom keep their words.
- **Undated jobs reach the assign board.** `/api/teams/day` returns an `undated` list alongside the day's work, and `/save` folds any undated job into `day_jobs` - so dropping one onto a crew is what gives it its date.

### Housekeeping

- **Database cleaned** — 17 CM + 3 BD + 10 PM jobs deleted with all seven child tables, and the legacy generator `kpi.ensure_pm_jobs()` switched off by zeroing `machines.pm_freq_days` (it ran on every planner/admin bootstrap and ignored the PM Start/Stop program).
- **App icons** come from `static/manifest.json` (+ `apple-touch-icon` / favicon). Now the Logo files: `Logo192/256/512/180.png` as `any`, `Logo-maskable-192/512.png` as `maskable` (artwork at 80% on a full-bleed `#F6761B`).
- **`.gitignore`** ignores `data-dev/`.

---

## Built earlier (still current)

- **The phone belongs to the team, not to a person.** `plan_teams.login_id` — each team card carries a **📱 Team phone** selector (Team A → techfp1, Team B → techfp2, Team C → techfp3). Saving writes `users.login_id` for every member and **clears it for anyone dropped from that day's crews**. Members change freely underneath; the phone follows the team.
- **"Worked by"** (`_team_of` / `_acting_tech`) — a crew shares one phone, so the app asks *which* member is reporting. The dropdown offers **only that job's team** (falling back to `ME.people` for a job nobody owns yet, so it can be claimed by name), auto-selects when the team is one person, and **Hold / Stop refuse to open until a name is picked**. The id travels as `as_tech`; the name is written to **`timelogs.tech`** (so man-hour KPIs credit the real person, not the login), `job_events.user_id` and the job chat. `close_open_segment()` and both "is a timer running" lookups match on `_tech_ids()`.
- **Logins and technicians are different things.** `users.can_login=0` = a person, `=1` = a credential; `users.login_id` links them. `_tech_ids()` resolves a signed-in user to [themselves + everyone linked to them]; `_mine()` builds the match. `_no_phone()` blocks assigning work to a technician no login can sign in as — it would be invisible to everyone.
- **A renamed or disabled account takes effect immediately** — `/api/me` re-reads `name`/`role`/`active` and writes them back into the session; a switched-off account gets a 401.
- **Factory scoping** — `/api/admin/machines` returns all 1,605 to an admin, the login factory's to anyone else; `/api/bootstrap` scopes `techs` and `users` too.
- **Day team assignment** (`teams.py`, `plan_teams`) — double-click a day on the Plan calendar → Teams (left) + that day's Planned jobs (right); drag jobs in, ★ lead, time window. Carries forward the previous day's crews when the day has none.
- **Asset ↔ PM sheet matching, corrected (26 Aug 2026).** `_match_all()` now: an **exact** normalised name beats a longer sheet name that merely contains it (six เครื่องตัดปลา were running the เครื่องตัดปลาชาดีน checklist); sheets split per machine — เครื่องบด / เครื่องบด 3, Hi-Mixer / HIMIXER3, Ribbon Mixer / Ribbon3, TTK-1 / TTK-2, นวดผสมแป้ง-1/-2/-3 — form a **family**, and the machine's own number (`#3`) picks its copy, falling back to the plain sheet when it has none; the family is decided on the NAME, not the score, because one stray shared word used to hand the machine to the wrong copy by a single point. `PM_ALIASES` gained eight lines for the spelling gaps between the register and the sheets (คอยเย็นห้อง→คอยเย็นแอร์ for 11 fan coils, ปั๊มวัติถุดิบ→ปั๊มวัตถุดิบ, เครื่งตรวจจับโลหะ→…, สเปร์คูลลิ่ง→…, สายพานรับซอง→…เพ้าช์, ฆ่าเชื้อกล่อง→หม้อฆ่าเชื้อกล่อง, นึ่งแรงดันไอน้ำ→หม้อฆ่าเชื้อเพ๊าท์ by elimination, and the TTK/Tony families). Keep an alias key specific — "ปั๊มวัติถุดิบ", not "วัติถุดิบ", which also appears in a room name. **36 of 151 assets moved, 115 unchanged**; ปั๊มลมสกรู went from 12 machines to 2, คอยเย็นแอร์ from 0 to 11.
- **The PM plan is uploaded in the browser now.** Planner → **Upload PM plan** is a file picker: `POST /api/pm/import-xlsx` takes the workbook as base64, **refuses if the factory has no asset register** (`_assets_or_400`, bilingual 400 — a plan needs machines to hang on, and it stops a plan being loaded into the wrong plant), reads it with `server/pm_import.py`, replaces the checklists, writes the part photos into `static/pmimg/`, and rewrites `data/pm_plan.json` so the old bundled-file `POST /api/pm/import` reloads the same thing. The filename guard from Import assets applies too (a `BFLPC…` file is refused on an FP login). Both import routes share `_apply_plan()`.
- **Import carries the pins across.** An import gives every template a new id, so `import_plan` now records each `pm_members` and `pm_plans` row by **template name** before the wipe and re-links it afterwards; anything whose sheet is gone from the workbook is deleted rather than left dangling. The button's confirm now says plainly that every checklist in the system is overwritten.
- **PM due → work order** — `/api/teams/day` returns the day's PM occurrences; dropping one on a team creates a real **PRM** work order and releases it.
- **PM engine (`pm.py`)** — 80 checklist templates, 637 items with frequencies read from cell fill colour, 515 part thumbnails in `static/pmimg/`, sheet-tab colours → colour groups, asset↔template matching with `_norm()` + `PM_ALIASES`. Tables created lazily in `_ensure(c)`.
- **PM calendar** — Start/Stop program, monthly/weekly, occurrences staggered per frequency, Sundays + weekly-off + holidays skipped, drag to reschedule (`pm_overrides`).
- **Three calendars, strict separation** — Planning (read-only, PM+CM), PM plan (PM only, editable), CM Plan (CM only).
- **Job timing** — `job_events` stamps every status change via `log_status()`; durations on the planner **Stages** tree. Dashboard **Throughput & Time** panel (`/api/kpi/worktime`).
- **Error log** (`server/logs.py`) — `data/logs/cmms.log`, fed by unhandled server exceptions, browser errors posted from phones, and anything logged to `cmms`. Read at **Manage → Logs**.
- **Manage → Users is three columns** — Login accounts · Technicians · Preview, with photo upload. Sidebar and planner tab row are drag-reorderable, saved server-side per role.

---

## Pending / open

### Deployment, before this settles

- **Roll the Cloudflare API token.** It was on screen during setup, so treat it as exposed: create a replacement in the dashboard, put it in `deploy/secrets.env` on each PC, then revoke the old one. Nothing else has to change.
- **`CF_ACCOUNT_ID` is still hard-coded as a default in `start_tunnel.py`** (and `CF_KV_NAMESPACE_ID` is, on any PC whose `secrets.env` does not override it). Move both into `secrets.env` on each machine so the file is the only place ids and secrets live.
- **`deploy/secrets.env.example` lists only `CF_API_TOKEN`.** Add `CF_ACCOUNT_ID`, `CF_KV_NAMESPACE_ID` and `CF_WORKER_URL` so the server PC can be set up from the example alone.
- **Change the seeded default passwords (`1234`).** The plant is live on the server PC now, so this one is overdue rather than pending.
- **Server PC pre-flight** - still to be done there: create `deploy\secrets.env`, confirm `C:\cloudflared\cloudflared.exe` exists, and decide whether `reset_jobs.py` should be run before the plant starts using it in earnest.

### Design questions parked

- **There are three separate paths that assign a job** (the assign board, the re-plan dialog, and claiming an unowned job by name). They overlap and can disagree. Flagged, deliberately not touched yet - to be discussed before anything is cut.
- Confirm whether **ServiceCompleted** should count as *In prog* or *Done* in the All-jobs status buckets; optional PM **compliance %** on the dashboard.
- **Should `Paused` follow the Hold due-date rule?** It does today (both read *Pending due date* / *Wait for action*). Paused is a break inside a shift and may be better left on *Assigned*.
- Should the app **remind the planner** while a job sits at *Pending due date*, and after how long?

### Known gaps

- **`role_ids()` in `db.py` is not factory-scoped.** A Wet Food report currently notifies technicians, planners and managers at BFL and PetCare too. One-line fix; worth doing before more plants go live.
- **Only Breakdown and priority-3 jobs post a system message into the job chat.** A normal operator report posts nothing, so a phone that never allowed notifications gets no in-app trace — though the job itself is now visible in every technician's list, which covers most of it.
- **Push subscriptions are per device and per data folder.** Anyone who never tapped "Enable notifications" on their phone stays silent whatever the code does. As of the last check on the live copy, tech2 had no subscription.
- `kpi.ensure_pm_jobs()` sets no sensible planned/due date — harmless while `pm_freq_days=0`, but fix it before ever switching the legacy generator back on.
- Old icon files (`icon-192.png`, `icon-512.png`, `icon-maskable-*.png`) and `cmms.db.bak-*` backups are safe to delete.
- Test the LINE report PNG (`planreport.py`) and send a screenshot for tweaks.
- Optional: production output entry (`shiftlogs` exists) → OEE/downtime-vs-output per machine.
- Optional: a `[TEST]` prefix on the dev browser tab title.

---

## Scripts

All of these **dry-run by default**, take `--apply` to write, and make a timestamped backup of the database first. Run them with the venv Python, **with the server stopped**.

- `fix_due_dates.py` — pulls any `due_date` that sits before its `planned_date` up to the planned date.
- `delete_cm_jobs.py` — `--types CM,BD`, `--delete-pm`, `--stop-legacy-pm`. Deletes jobs and all seven child tables.
- **`build_pm_plan.py`** — the missing half of the import. **Import templates does not read the .xlsx**, it reads `data/pm_plan.json`; this script is what turns one into the other. Reads every sheet of `BFLFP PM Plan/BFLFP PM Plan _R1.xlsx`: header row 7, items from row 9 to the ลำดับ legend, **the fill colour of the ลำดับ cell is the frequency** (amber weekly · red monthly · green 3-month · blue 6-month · yellow/blank every run), the tab colour is the colour group, and the photo anchored on an item's row is its part picture. Columns are read off the header row — one sheet has an extra ความถี่ column, another shifts วิธีการ. Items are renumbered as they are read, because several sheets repeat a number by hand. Dry run prints a diff against the current json. **Run: 84 sheets · 666 items · 541 photos** (was 80 · 637 · 515 — nothing lost, 29 lines recovered that the old converter had dropped).
- `import_pm_colors.py` — reads the sheet tab colours out of `BFLFP PM Plan/BFLFP PM Plan _R1.xlsx` into `pm_templates.color` + `pm_groups`. **Already applied.**
- **`reset_factory.py`** — empties ONE factory completely so it can be loaded from scratch: assets, checklists, PM plans, pinned sheets, colour groups, PM config, and every work order with its seven child tables. Keeps users, teams and the other factories. `--factory 2` (BFL 1 · FP 2 · PC 3), dry-run by default, backs the database up first. Run it with the server stopped, then in the app: **Import assets**, then **Upload PM plan** — in that order, because the upload refuses an empty factory.
- `refresh_dev.py` — copies live → `data-dev`, resets passwords to `1234`, clears push subscriptions.
- **`add_problem_types.py`** — inserts the 45 Thai electrical symptom types (all factories by default, `--factory` to limit, `--retire-old-electrical` optional). Idempotent by normalised name. **Applied on the live server.**
- Older: `import_assets.py` (also the Planner button) · `make_qr_labels.py` · `set_test_dates.py` · `fix_logos.py`.

---

## Key files

`static/index.html` (whole frontend) · `static/sw.js` (`CACHE = "bflfp-v173"`, network-first; `index.html` carries `UIBUILD = 'b199'` in its footer - **bump both together**) · `static/manifest.json` · `static/pmimg/` (515 PM thumbnails) · `static/vendor/jsqr.js` · `static/vendor/pdf.min.js` + `pdf.worker.min.js` (vendored pdf.js, in the SHELL cache)
`server/`: `app.py` (no-store headers, health) · `config.py` (**`ENV` / `IS_LIVE` / `BFLFP_DATA`**) · `auth.py` (login/factory/bootstrap/me) · `jobs.py` (job views + lifecycle + `job_events` + `/api/jobs/export` + `recentall` + `techassigned`) · `pm.py` (PM engine + **assets grid, `ASSET_COLS` / `ASSET_EDIT` / assets export**) · `teams.py` (day teams + `plan_teams`) · `kpi.py` · `admin.py` · `push.py` (**`[DEV]` prefix**) · `logs.py` · **`reports.py`** (F-SP-ENG02-06 form, archive, `file_daily`) · **`jobform.py`** (F-SP-ENG02-03 ใบแจ้งซ่อม, `build_job_form` / `file_job_form`) · **`planreport.py`** (plan sheet + the shared `_day_counts` day model + future-day planning strip) · `db.py` (**`next_jobid`**, `log_status`, timing helpers, `inspected_at` migration)
`run.py` / `Start CMMS.bat` / **`Start DEV.bat`** · `deploy/` · `data/` (live) · `data-dev/` (test).

---

## Gotchas

- **Never run a write against `data\cmms.db` over a network/sandbox mount.** SQLite cannot take its lock there: the write dies mid-transaction with `disk I/O error` and leaves a `cmms.db-journal` behind, after which reads fail too. **Do not delete the journal** — start the app normally and SQLite rolls the transaction back. (This happened once; nothing was lost.) Scripts must be run on the Windows machine.
- **Backend auto-reloads** on `server/*.py` edits (`CMMS_RELOAD=0` to disable). Editing `run.py` itself needs one manual restart.
- **Frontend edits need a browser refresh.** `sw.js`'s `CACHE` constant is hand-maintained — bump it if you ever change what the SHELL caches. The fetch handler is network-first and `index.html` is served `no-store`, so this rarely bites.
- iOS: fully close and reopen the installed PWA.
- Push needs `pywebpush` in the venv + HTTPS + per-device "Enable notifications" (iPhone must be Home-Screen installed).
- **Never put the Cloudflare API token in chat, in a terminal command, or anywhere it can be screenshotted.** It lives in `deploy/secrets.env`, which is in `.gitignore` - **not** in `start_tunnel.py` any more. Use `python deploy/start_tunnel.py --check` to verify it; that prints only a masked form. A token that has been on screen should be rolled in the Cloudflare dashboard and replaced in that one file. `secrets.env` still travels with the deployed folder, so keep read access to the server's copy restricted.
- Grep sometimes displays `/` as `\` and `</tag>` oddly — the Read tool shows the true characters.

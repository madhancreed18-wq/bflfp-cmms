# Changelog — BFLFP CMMS
All notable changes. Format: version · date · changes. Newest on top.

## v3.0.2 — 2026-08-27 (the planner, on a phone)
Built from the Planner-on-a-Phone design. Desktop planner untouched — every screen
below only renders under 900px, and a wide tablet still gets the two-pane version.
- Jobs: today's three counters (Not assigned · In progress · To accept) double as
  filters, an "Assign today · N waiting" button, search, filter chips and day-grouped
  cards showing machine, job no, symptom, who and how long it has been sitting.
- Assign: the two-pane drag board becomes a full-screen list — jobs needing a crew,
  each with "Give to a crew", then a card per crew with its members, its phone and its
  work. A bottom sheet picks the crew; the drag is gone.
- The sheet's hint says only what the board can prove: who already has that machine
  today, or who is carrying the least. It does not guess at the trade.
- Stages: the whole plant by where each job has got to — Reported / Assigned / In
  progress / On hold / To accept / Sent back — with counts and ages.
- Saving an assignment now returns to the screen you were on, not always the calendar.
- Plan calendar already reflows to 7 columns on a phone and was left as it is.

## v3.0.1 — 2026-08-27 (live updates: the screens keep up on their own)
Reported job PRD-2608-006 reached the technician's screen 32 seconds after it was
created, while the push notification was instant. Traced in data/logs/cmms.log.
- Server-sent events REMOVED (/api/live). The tunnel in front of the app buffers the
  stream: the server wrote a ping every 8s and the phone heard nothing, reopening the
  connection every 25s. The hang-up never came back either, so dead streams stayed
  open — and one open stream was enough to hold a restart on "waiting for connections
  to close" with the site down.
- Replaced by a held-open ordinary request: GET /api/pulse?wait=25&v=<token>. The
  answer is simply withheld until the token moves. Update lands in about a second;
  an idle screen sends one request per 25s and nothing else.
- run.py: timeout_graceful_shutdown=5 — a restart can never hang again.
- Web Push now goes out with Urgency: high and ttl=300. At normal urgency Apple and
  Google batch the delivery to save battery, which is why alerts arrived late; TTL 0
  also meant a push was dropped if the phone was briefly offline.
- Tab lamps read their own tab's list, not whatever list is on screen (Reports kept
  its lamp on Approved and lost it on Pending). Own actions no longer light a lamp.
- Front-end build stamp (ui bNNN) shown beside the version, and the service worker
  now refreshes itself — a stale phone can be told apart from a real bug.
- Every screen listening is logged with how long it waited and what it carried.
- Sessions now live in a `sessions` table instead of a dictionary in the server's
  memory, so a restart no longer signs out every phone on the floor. Only the token,
  the person and the plant they chose are stored; name, role and permissions are read
  fresh from `users` on the first request after a restart. Logout and a disabled
  account delete the row; expired rows are swept at boot.
- A 401 after a restart no longer files a browser error report from every phone.

## v3.0.0 — 2026-08-05 (MAJOR: production database schema — SCHEMA ONLY)
Approved table design from the role-based workflow document + external review.
App logic unchanged — screens/modules will wire to these tables one by one.
- 13 NEW tables (26 total): checklist_templates, checklist_items,
  job_checklist (pass/fail/na + measured value + verify), escalations,
  job_attachments (many photos/videos per job, storage keys, checksums),
  job_status_history (real audit trail), job_assignments (assignment history),
  pm_plans (multiple PM cycles per machine, calendar or meter based),
  meter_readings, machine_downtime (true downtime windows),
  production_calendar (honest planned time for OEE), machine_part_bom,
  evidence_rules (photo requirements per job type)
- jobs: lifecycle timestamps split — released/started/service_completed/
  downtime_restored/user_confirmed(+wait reason)/prod_release/closed(+by),
  plus approval_required, estimated/actual_cost, safety_risk, failure_mode
- machines: mgroup (machine group → which checklists apply)
- requisitions: full purchase loop fields · shiftlogs: kpi_class-specific fields
- Auto-migration in create_schema(): adds missing columns on any engine,
  append-only, idempotent, old data untouched (verified on simulated v2 DB)
- /api/health reports table count; smoke 54 checks

## v2.3.1 — 2026-08-05 (login: factory logo tiles)
- Factory selection is now 3 tappable logo tiles: BFL / BFLFP / BFLPC
  (each factory's own logo, selected tile highlighted in blue)
- Removed the big heading logo from the login page (per design feedback)
- No database or API change — same factory_id sent to the server

## v2.3.0 — 2026-08-05 (full bilingual Thai / English)
- Every screen now renders fully in Thai OR English — login, operator,
  technician, planner, detail, chat, dashboard, machines, admin, dialogs
- Language picker on the login screen (ไทย / English pills); also in อื่นๆ/More
- Choice remembered on the device (localStorage) — survives restarts
- Job statuses translated in Thai mode (Reported→แจ้งแล้ว, Done→เสร็จสิ้น …)
- Help guides written natively in both languages (not machine-mixed)
- Bottom navigation labels, empty-states, alerts, placeholders all switch
- Shift values stored canonically (เช้า/บ่าย/ดึก) regardless of display language
  — KPI queries unaffected
- Default language: Thai

## v2.2.0 — 2026-08-05 (new UI shell: login + navigation)
- New login screen (user's design): navy full-screen, Bluefalo logo,
  Welcome Back, rounded CMMS Login card, password eye toggle
- Factory selector on login (BFL / Wet Food / Petcare): operators/techs/
  planners must pick their own factory; admin & manager may enter any.
  Active factory stored in session, shown in header + /api/me
- Remember me: checked = 30-day session + username prefilled next time;
  unchecked = 1-day session (username only — password never stored)
- Whole app rebranded to Bluefalo blue/green; real logo in the header
- Mobile bottom navigation per role (LINE/app style tabs)
- Operator split into 3 tabs: 📢 แจ้งซ่อม / 📋 ของฉัน / 📊 ผลผลิต
- New "อื่นๆ (More)" screen: profile, help, TH/EN, logout, version
- Old demo role-card quick login removed (production login behaviour)
- Smoke suite grown to 53 checks (5 new factory-selector checks)

## v2.1.0 — 2026-08-04 (security: login protection)
- Captcha after 2 failed passwords — generated locally with Pillow
  (no Google, works on LAN with no internet, single-use, 5-min expiry)
- IP-level tracking: rotating usernames from one IP still hits captcha (4 fails)
- Temporary lockout: 10 fails/user or 20/IP → 15-minute block (HTTP 429)
- 0.4 s delay on every failed attempt (slows automated guessing)
- Real client IP read via CF-Connecting-IP / X-Forwarded-For (tunnel-correct)
- Successful login clears counters (shared factory NAT stays usable)
- Login screen shows the captcha box automatically when demanded
- Smoke suite grown to 48 checks (6 new brute-force checks)

## v2.0.0 — 2026-08-04 (MAJOR: database foundation)
- SQLAlchemy engine layer (`server/database.py`): SQLite for dev/pilot,
  PostgreSQL for production via one setting (`DATABASE_URL`) — no code changes
- Full schema defined in one place, with server-side defaults
- New tables (approved architecture): factories (BFL/FP/PC), spare_parts,
  part_moves, requisitions
- New job fields: production_impact (delta #2), accepted_at (delta #10)
- New machine fields: kpi_class OEE/BATCH/AVAIL + kpi_approved (delta #1),
  factory_id, brand_model, serial_no, year_install, last_pm_date
- Removed all SQLite-only SQL (julianday, date(), ON CONFLICT, INSERT OR
  IGNORE, lastrowid) — portable across both engines
- FIXED delta #5: PM compliance denominator now includes overdue open PMs
- Smoke suite: 42/42 green on the new layer
- Note: PostgreSQL runtime verification pending on a machine with PG installed
  (sandbox has no PG server); Alembic migrations planned at PG cutover

## v1.0.0 — 2026-08-03
First complete version. Modules:
- Work orders: PRD numbering, full status machine, approval/rework/re-issue
- PM auto-generation by machine cycle + compliance tracking
- Time segments engine (one open per tech, pause reasons)
- Photos + signatures + repair-form PDF (F-SP-ENG02-03) + daily report PDF
- Chatter: channels, threads, job discussion, notes, activities, @mentions
- Web push notifications (VAPID, self-issued)
- Shift log + KPI engine: OEE / MTBF / MTTR / FTFR / planned-ratio / backlog
- Fault codes (ISO 14224-lite) + top-failure analytics
- Admin: machines + users CRUD, hashed passwords (PBKDF2)
- PWA install, TH/EN toggle, /api/health, smoke test harness (42 checks)

Fixed during v1.0.0 hardening:
- 404 instead of crash on missing job (found by smoke test design)
- rework_count returned stale in PATCH response (caught by smoke test)

## b356 — 2026-09-25
- Assigned jobs → new **Day timeline** tab (🕒 ไทม์ไลน์รายวัน). One shift day (08:00 → 08:00, Morning 08–20 / Night 20–08) per technician: planned jobs, jobs finished, work time logged, first start / last finish, a 24-hour bar of the time segments worked (coloured by job type), stopped/waiting gaps, finish ticks, and a per-hour chart of when jobs were finished. Click a technician for the job-by-job list.
- New API `GET /api/teams/timeline?d=YYYY-MM-DD` (planner / manager / admin). Time counts for the job's lead and every helper, same rule as the Now tab. A timer still running is shown but not added to work time.

## b357 — 2026-09-25
- Assignment board (Plan calendar day popup): a technician login with nobody attached (BFLFP tech2 = Kanya) is now a technician in its own right — pickable as lead/member, shown by name instead of "#4", and saved like any crew member.
- Work booked to a login that exactly ONE person uses (Choke → tech1, Mark → tech3) now sits in that person's crew instead of a "PHONE ACCT" column that came back every time it was deleted. Crews saved with the handset in them show the person. Same rule in the Day timeline.

## b358 — 2026-09-25
- Lists (Assigned jobs: Now / Week / List): a login used by exactly one person is shown under that person — one row per technician instead of "Choke" + "choke · PHONE ACCT". Display only; no job is rewritten.
- Assignment board: lends ("+ Choke" on a job of another crew) are now RECORDED when saved (new table plan_lends; plan_days.lends marks days saved this way). A helper left over from an earlier crew is no longer shown as lent nor written back on the next save. Days saved before b358 are read exactly as before, so plans already issued do not change.
- Crew save no longer writes the crew's phone over a technician's OWN login (same-name person/account pair, e.g. mark → tech3), even on BFLFP where not every technician has one.

## b359 — 2026-09-25
- Daily plan sheet: work booked to a login that exactly one person uses (BFLFP tech1 = Choke, tech2 = kanya, tech3 = mark) now prints under that person's crew instead of "— ไม่มีทีม". Old crews saved with the login in them are read the same way. Only sheets issued from now on change; sheets already issued are untouched files.

## b360 — 2026-09-25
- Day timeline accuracy: a time segment belongs to the technician who pressed Start when that person is not on the job's current crew (job re-planned or taken over later). Audit 19–25 Sept, all 3 plants: 80 segments / 352 of 5,460 logged minutes (6.4%) had been credited to the wrong name.
- "Work time logged" tile counts each minute once, not once per crew member.
- Verified against an independent recount from the raw time logs: minutes, finished jobs and planned counts match 100% for every technician on every day checked.

## b361 — 2026-09-25
- Assigned jobs → List (sheet, phone cards and Excel export) and Day timeline job list: a technician's jobs are ordered BD, then CM, then PM (then IMP / project), latest first inside each type (finished, else started, else planned day). Clicking a column title still sorts by that column. All three plants.

## b362 — 2026-09-25
- Day timeline → technician's job list: each job now shows the **issue reported** (reporter's description / problem; for PM the first line — frequency and machine type) and the **technician's solution** (root cause → solution as written at finish; for PM the checklist summary OK/NG + note). "Not written yet" when empty.

## b363 — 2026-09-25
- Job photos, all plants — who may REMOVE or REPLACE (enforced on the server, buttons shown only when allowed):
  - BEFORE photos: only the reporter, and only until work starts. Technicians never. After start: admin only.
  - AFTER photos: only a technician on the job (lead/helper) while working (InProgress / Paused / Hold / Rework). Once completed, awaiting acceptance or accepted: read-only; admin only.
  - Admin: always.
- Delete and Change ask for confirmation first ("cannot be undone"). Adding a photo to an empty slot is unchanged.

## b364 — 2026-09-25
- Jobs → All jobs (tree, table and Excel export), all plants: newest day first; inside each day BD, then CM, then PM (then improvement / project); newest job first inside each type. The day is the job's planned day, or the day it was reported when it has none (unchanged rule).

## b365 — 2026-09-25
- Assigned jobs → new **📊 Dashboard** tab (แดชบอร์ด), all plants. One shift day (08:00 → 08:00, Both / Morning / Night), same data as the Day timeline: tiles (jobs finished, technicians who logged work, planned-but-nothing-logged, timer left on >4 h on one job, finished without a solution), jobs finished per technician stacked BD/CM/PM with the planned count as a black line, a technician × hour grid of when jobs were finished, and a summary table (planned, finished, % of plan, work, first/last, things to check). Clicking a technician opens his jobs in the Day timeline.

## b366 — 2026-09-25
- Jobs → All jobs → TABLE (and its Excel export): grouped by the day the job was CREATED (the date in the first column), newest day first; inside each day BD → CM → IMP → PM, job number from highest down. b364 grouped the table by planned day, which put jobs created on different days side by side and made the Created column look shuffled.
- Tree view keeps its planned-day grouping, with the same BD → CM → IMP → PM, highest number first, order inside each day.
- Assigned jobs List / Day timeline / its Excel: IMP now sits between CM and PM (BD → CM → IMP → PM).

## b367 — 2026-09-26
- **QR labels** (Assets → 🏷 QR labels), all plants. QR = https://cmms.bflgroup.workers.dev/?asset=<PLANT>-<ASSET ID> (plant code inside the QR only, never printed). Label prints asset ID, machine name, location, floor (department optional), colour stripe by criticality / floor / none. Sizes: A4 24/sheet (70×37), A4 12/sheet (105×48), sticker 50×30, sticker 70×40. Filters: search, floor, never printed, needs reprint, missing data (with warning). Print / PDF (browser print window), scan test with the device camera.
- Print history: every print run recorded (qr_prints); what each label said is kept (qr_labels) and an asset whose name, location or floor changes afterwards is flagged "↻ reprint".
- Scanning a label with any phone camera opens the machine page after sign-in: details, Report a problem (machine pre-filled; operators and planners), open jobs; technicians/planners also see next / last PM and the last 5 repairs (problem → solution); planners see this month's BD/CM count. A label of another plant says whose it is; all-plant users are switched to that plant.
- The in-app scanner (Report a problem) reads the new links, PLANT-ID and old bare-ID labels; a label from another plant is refused with a message.
- New endpoints: GET /api/assets/qr, POST /api/assets/qr/print, GET /api/assets/scan. New file static/vendor/qrcode.js (MIT).
- FIX: b366 shipped an EMPTY static/sw.js (phone offline cache). Restored.

## b368 — 2026-09-26
- QR labels: space for cutting after laminating. New A4 layouts for laminating — 21 per sheet (60×35 mm) and 10 per sheet (90×48 mm) — and a gap choice for every A4 layout (none / 3 / 5 / 8 mm) with dashed cut lines. The grid is refitted to A4 with at least 5 mm at the edges and the count per sheet is shown. Choosing a laminating layout sets a 5 mm gap. Gap is remembered per browser.

## b369 — 2026-09-26
- QR labels: asset ID and machine name printed larger (about +20–25% on every size, name bold), criticality moved to a small corner badge so the asset ID keeps the full width, "scan to report" line smaller and on one line. QR sizes unchanged except 21-per-sheet (24 mm) and 50×30 sticker (21 mm).

## b370 — 2026-09-26
- QR labels: QR enlarged to nearly the full label height on every size (70×37: 33 mm, 105×48: 44 mm, 60×35: 31 mm, 90×48: 44 mm, sticker 50×30: 27 mm, 70×40: 36 mm). The asset ID shrinks only as far as needed to stay on one line; machine name up to 3 lines.
- New layout for hanging on machines: A4 for laminating · 8 per sheet, big QR (95×68 mm, QR 62 mm), 5 mm cutting gap.

## b371 — 2026-09-26
- QR labels: the cutting gap is ON by default (5 mm) and the default size is "A4 for laminating · 10 per sheet". Laminating sizes are listed first. The 24- and 12-per-sheet layouts are now named as pre-cut A4 sticker sheets and never get a gap (their cuts are on the paper). Size and gap remembered under new keys, so an old "no gap" setting from b368 no longer applies.

## b372 — 2026-09-26
- **One person, two roles** (technician who also reports). Admin → Users → edit a technician → tick "แจ้งซ่อมได้ด้วย (Can also report problems)". That person's phone gets a switch in the top bar: ช่าง (Technician) | ผู้แจ้ง (Reporter). Reporter mode shows exactly the operator screens (report a problem, my reports, to accept) with the same login and name. Remembered per person on that phone.
- Rule: nobody accepts their own repair. If the person who raised a CM/BD also worked on it (lead, helper or logged time — including the names linked to their login), it is not in their "to accept" list and the server refuses their signature; it goes to the plant's planner / admin queue instead.
- Server: new column users.can_report (added automatically on start, default 0 — nobody changes until an admin ticks it). The phone sends X-Act-As: operator in reporter mode; the server honours it only for a technician with can_report. Existing jobs, plans and accounts are untouched.

## b373 — 2026-09-26
- The reporter can correct their report — priority included — until the work STARTS: Reported, waiting, and also Assigned as long as nobody has pressed Start (no timer ever run). From In progress / Paused / Hold / Rework / Completed on, nothing changes (existing rule); the planner can still change priority at any time.
- Before: the priority buttons showed on the reporter's edit screen but the change was silently dropped, and a report was locked as soon as a crew was given it.
- A priority change is written in the job history (⚑ old → new, by whom). Raised to High → planner / manager / engcenter notified. Already given to a crew → the crew is notified "Report updated".
- An assigned job's times are left alone when its report is corrected (only a still-unassigned report takes the correction time as its report time, as before).

## b374 — 2026-09-26
- Technician ⇄ operator switch redrawn as a small on/off toggle (50×26): orange with "T" = technician, green with "O" = operator (reporter). The whole switch is the button. The top-bar title now gives way instead of being covered by it.

## b375 — 2026-09-26
- Crew phone retired. Every technician in all three plants has their own login, and a job always went to the account of the person it is given to, so the team "📱 phone" picker, its auto-fill (Team A → first login …), the "no phone linked" warnings and the login name printed beside a team (e.g. "เอฟ · techpc1") are gone from the assignment popup, the phone planner and the printed plan.
- A team save no longer writes a crew handset over any person's login link, and no longer clears links of people taken off a team. The person ↔ own-account link is set in Admin only. (Closes a gap: a person whose account is filed under another plant — ตุ่ม on BFL — could have had their link overwritten by a crew phone.)
- plan_teams.login_id is kept in the database but no longer written (saved as empty). Nothing else changes; existing plans, teams and jobs untouched.

## b376 — 2026-09-26
- **Operator and technician phones, all three plants (BFL · BFLFP · BFLPC):** one header on every screen — ☰ · Bluefalo logo · BFL Group - CMMS. The old black bar ("BFLFP - CMMS" + logout) and the bottom tab bar (My reports / Output / Chat / More) are gone.
- ☰ menu holds everything the role has — Pages (operator: My reports, Output, Chat · technician: My jobs, Chat) and Tools: **Scan QR code** (opens the machine's page, same as its label), Daily maintenance report, Enable notifications, How to use / Help, Language, Logout, version. The "More" page is folded into it.
- On a screen with somewhere to go back to (new report, job detail, a scanned machine) the ☰ becomes ←.
- The name / role and 💬 button are off the header. In their place, for a technician allowed to report (Admin → can_report), the slider switch: **T** (technician, orange) ⇄ **R** (reporter, green).
- The plant chip moves from the header to beside the page title (All Jobs · My jobs · Output · Chat).
- Planner, manager, engcenter and admin screens unchanged. No server change.

## b377 — 2026-09-26
- Login page on Android: the 👁 (show password) sat outside the password box. Android gives an input a fixed minimum width inside a row; the box now lets the input shrink so the eye stays inside, on every phone width.

## b378 — 2026-09-27 (the machine page after a QR scan)
- One page for every role (was different per role). Order: ✕ Close · + Add New Request (requester, technician in R mode, planner, manager, admin — not technician in T mode) · machine (code, A/B/C, plant, name, description, location) · Open now · 1 History · 2 PM · 3 Spare parts.
- Open now includes jobs waiting for acceptance (were hidden, so a fixed fault could be reported again).
- History split Breakdown / Corrective / Preventive, each line What · When · Who (lead + helpers) · Fix; last 5 per type, "Show more"; a job closed without notes says "no solution written".
- PM: previous PM (what, when, who, result) and next PM (date, start time if set, red "Overdue N days", "Not assigned to a crew yet — the planner will assign it before this date").
- Spare parts: read-only table marked DEMO — the spare-part register is still empty; rows are examples only.
- A retired machine says so and has no Add New Request.
- ✕ Close and the phone's Back go to the person's home page (operator My reports, technician My jobs, planner/manager Jobs, admin Manage).
- Fixed: admin scanning another plant's machine switched plant but landed on the Jobs list. Fixed: a live update redrew the home page over the machine page (planner's first scan after login).
- Server: GET /api/assets/scan returns the same data for every role (n = history lines per type). No database change.

## b392 — 2026-09-30 (Central Engineering – Electrical: the team field)
- Manage → Users → edit a user: new **Team** field — Plant maintenance (default) or ⚡ Central Engineering – Electrical.
- Users rail: new ⚡ Central Engineering group with its count; the team's rows carry a ⚡ Central badge.
- Server: new column users.team ('' or 'CE'), added automatically on start. The sign-in and /me answers carry it, ready for the electrical PM screens.
- No screen changes for anyone yet: planners, technicians and plant PM plans are exactly as before. BFLFP untouched.

## b393 — 2026-09-30 (symptom required · Daily Work Plan details)
- Report a problem (operator) and + New job (planner, CM): the symptom must be picked from the list. Not in the list → pick "Other"; only then the description box opens, and it must be filled.
- If anything is missing, one popup lists all of it (machine/place, symptom, description for "Other"). The server refuses such a CM/BD too, so an older cached page cannot file one. PM / IMP / PRJ unchanged.
- Daily Work Plan: a job on a machine/place that is not in the register prints the typed name under MACHINE (was blank); an "Other" job prints the description instead of the word "Other".
- The planner's + New job now saves a typed place as the job's asset text (as the operator's form already did).

## b394 — 2026-09-30 (plan number with the plant in front)
- Every Daily Work Plan issued from now has its own number: BFL-PLAN-YYMMDD-NN, FP-PLAN-…, PC-PLAN-… (NN counts the plans issued for that day in that plant).
- Printed top-right on the sheet ("เลขที่แผน / Plan no."), shown under the plan name on the Reports shelf (and found by the search box), and used as the saved file name.
- Save & assign that replaces the day's plan keeps the same number. Plans issued before b394 keep no number; job numbers do not change.


## b395 — 2026-09-30 (removed crew member stays removed)
- Assign work: a person taken off a crew came back. The change was saved on that day only — a day already planned ahead kept its own copy of the old crew, and every day after it carried that copy on. The crew's jobs also still listed the person as a helper.
- Now a member removed from (or added to) a crew is also changed on the same crew on every later day already planned. Someone who LEADS unfinished work booked on that later day is kept there, and the save message names them.
- The removed person is taken out of the helpers of that crew's not-started jobs (today's, and carried work left on its own day). Work already started or finished keeps its people; a per-job lend stays.

## b396 — 2026-09-30 (a crew change never moves work already given)
- Taking a person off a crew no longer takes them off jobs they were already given. Earlier-day work stays exactly where it was — lead or helper — and so does work already started.
- The crew change itself still runs from the day saved into every later day (b395), until changed again. Only the day's own not-started work, rewritten by Save & assign, follows the new crew.
- Replaces b395 (do not deploy b395).

## b397 — 2026-10-01 (dashboard lists: criticality, filters, whole-list counts)
- The lists behind the three dashboard rings (PM compliance, Corrective closed, Machine availability) now show each machine's criticality: A critical · B important · C normal.
- A / B / C cards at the top give the ring's figure for each criticality; clicking one filters the list.
- The counts at the top are for the WHOLE list (they were "on this page", so page 1 could read 0 on time under a 479/587 ring). PM states the sum: on time + done late + still open = counted, plus not-due-yet listed but not counted.
- Tabs with counts (PM: Needs action · Open · Late · On time · Not due yet · All; CM: All · Still open · Closed), a search box, and sortable columns. It opens on Needs action, sorted open first, then A → C, oldest due first.
- One scrolling list with Load more instead of 13 pages. Export downloads the filtered list as CSV (opens in Excel with Thai intact).
- Corrective list: ServiceCompleted shows amber (waiting approval); the header says how many jobs have no machine.
- Server: /api/kpi/pmjobs and /api/kpi/cmjobs take verdict, crit, q and sort and return counts and crit_stats; /api/kpi adds criticality to the worst-machine list and crit_count. No database change.

## b398 — 2026-10-01 (PM verdicts: carried over / overdue)
- PM compliance list: a PM done after its planned day but still inside its own week / month is now labelled **carried over N days** (orange). It still counts as on time.
- A PM done after its week / month ended is now **overdue N days** (red) — was "Nd late" (amber).
- A PM still open after its period shows **still open · overdue N days** (red).
- New tab **Carried over** with its count; the header sum reads "on time (incl. N carried over) + overdue + still open". The ring's foot says "overdue (done after its period)" in red.
- Export adds "Days overdue" and "Days carried over" columns. No change to how the % is counted. No database change.

## b399 — 2026-10-01 (Week = Monday → Sunday)
- Dashboard **Week** is now the plan week, Monday → Sunday — the same week a weekly PM is judged by — instead of the last 7 days. The PM list and the PM % now always describe the same days.
- ‹ › beside it step back to earlier weeks (e.g. Mon 21 Sep – Sun 27 Sep) and forward again; the current week runs Monday → today.
- Today, Month, Quarter, Half year, Year and typed dates are unchanged. No database change.

## b400 — 2026-10-01 (dashboard: PM planned vs completed, Top N, swapped rows)
- New **PM planned vs completed** line chart under the three rings: Running total (the shaded gap is the PM backlog carried forward) or Per day. Follows the page period. New endpoint /api/kpi/pmdaily.
- **Top 5 / 8 / 10 / 15** selector on "Top breakdown assets" and "Top machines · work time" (one setting, remembered on the device).
- Rows swapped as the plant asked: Top breakdown assets + Technician workload now sit above the Downtime hours chart; Top machines · work time + Work hours per tech moved below it.
- Top machines · work time: jobs with no machine ("—") are no longer ranked as a machine — shown as a line under the list. Work hours per tech sorted by hours (was by job count) and labelled crew hours.
- No database change.

## b401 — 2026-10-02 (tunnel blips: 530 no longer a red error)
- "530 GET /api/teams/now" (and 502/503/504/520–530) come from the Cloudflare tunnel in front of the server — the tunnel had no live link to the server PC at that moment; the request never reached the app. Measured on a copy of the real data, /api/teams/now and /api/teams/timeline answer in under 0.1 s, so the page itself is not slow.
- The app now retries a READ quietly twice (after 1 s and 3 s) before saying anything. Saves are never repeated automatically.
- If it still fails: a plain "The link to the server dropped for a moment" panel (Thai + English), no ERR reference. Background refreshes (the Now tab's 60-second update) stay silent.
- Each such failure is written to the server log as "gateway 530 GET …" so IT can see how often the tunnel drops.

## b402 — 2026-10-02 (admin / planner / manager can scan a machine QR)
- QR labels → "Scan test": a label that reads correctly now opens the machine page (open jobs, BD / CM / PM history, previous and next PM, parts) instead of only a "✓ QR reads" message. A label of another plant still shows the warning.
- "Scan QR code" added at the top of the ☰ menu for every role (it was only in the operator / technician menu).
- No database change.

## b403 — 2026-10-02 (PC: open a machine page by typing)
- On a PC the top of the ☰ menu reads "Open machine page": type the machine code or name, pick it, and the same page a QR scan opens appears (open jobs, BD / CM / PM history, PM, parts). A "Use a webcam instead" button stays for a PC with a camera.
- On a phone the menu keeps "Scan QR code".
- No database change.

## b404 — 2026-10-02 (the person who raised an IMP job accepts it)
- IMP (improvement) jobs now follow the CM / BD rule: whoever raised the job accepts and signs it, whatever their role — operator, planner or manager. It used to be operator-only, so a planner who raised an IMP could never accept it ("only operator may approve or reject completed work").
- The planner's and manager's "To accept" list and badge now include their own IMP jobs.
- Admin keeps the override (written into the job's history). No database change.

## b405 — 2026-10-02 (accept + sign straight from the repair form)
- Reports shelf: when the person opening a finished job's repair form is the one who must accept it (e.g. a planner opening the CM he raised), the "can't sign yet" box no longer tells him to wait for himself. It says "คุณคือผู้ต้องตรวจรับงานนี้ / You are the one who accepts this job" and offers a green "✓ ตรวจรับ + เซ็น ตอนนี้ / Accept + sign now" button.
- After accepting, a planner or admin goes straight to the Approver sign box of the same form.
- Everyone else still sees the old message. No database change.

## b406 — 2026-10-02 (signatures: register once, sign sideways, print clearly)
- Signatures printed pale because a 960 px pad with a 2.6 px pen was squeezed into a 34 mm box (a ~0.09 mm line, stretched out of shape). Every report (repair form, PM sheet) now prints a clear copy: cut to the ink, a 0.4 mm pen-width line, dark ink, real proportions. Works for every signature already given — nobody re-signs; the stored files are not changed.
- ☰ → "ลายเซ็นของฉัน / My signature" for every role: register once on a full-screen pad. On a phone held upright the pad turns sideways (Android also locks the screen to landscape); the signature is saved upright, cut to the ink.
- Every sign point (technician finish, accept, job-page boxes, PM sign) opens the same pad, and a registered signature is already in the box — "use my signature" is one tap, "sign by hand instead" stays. Not registered yet: "keep as my signature for next time" is ticked.
- The browser sends "reg:<id>" and the server copies the file itself, so nobody can send someone else's signature.
- Department (shared) logins — 21 area / HR accounts ticked once — never hold one signature: "ใครเป็นผู้เซ็น / Who is signing?" picks a linked person (their own signature) or a typed name; that name prints under the signature and is written in the job history.
- Manage → Users: "Department login" tick, signature status, admin ↻ reset.
- Database (automatic on first start): users.sig_path, users.sig_at, users.shared_login, table sig_signers. Nothing existing is changed.

## b407 — 2026-10-02 (menu: Dashboard under Reports, Checklists and Import under Assets)
- ☰ menu: Dashboard is now a sub-item of Reports; "Machine list" is renamed "เช็คลิสต์ / Checklists" and sits under Assets, with Import assets. Applied on top of any menu order saved before, so every role sees it without re-arranging.
- PM compliance list: tab "ต้องดำเนินการ / Action must be taken", column "เสร็จเมื่อ / Action taken" (Excel column "Action taken").
- No database change.

## b408 — 2026-10-02 (one machine page: Asset history)
- PC ☰ menu: "Open machine page" is removed for roles that have Asset history (admin, planner) — Asset history already has the search and Scan QR, plus the full record.
- Phone ☰ "Scan QR code" (admin, planner) now opens Asset history for the scanned machine, as does Asset history's own Scan QR. A label from another plant switches to that plant when the account may work there (it used to say "not a machine of this plant").
- Operators, technicians, managers and engineering center keep the short machine page. Scanning a label with the phone camera (outside the app) still opens the short page for everyone.
- No database change.

## b409 — 2026-10-02 (download vs share; plant-named report files)
- PC: Download saves straight to the computer — the Windows share window no longer opens.
- Phone: Download saves to the phone (a short "saved" note confirms the name); a new green "↗ แชร์ / Share" button next to it sends the file to LINE, mail, WhatsApp… (iPhone Home-Screen app: Download opens the share sheet, where "Save to Files" is — iOS allows nothing else).
- File names carry the plant and the date: BFLFP_Daily-Report_2026-10-01_2359.pdf (shelf rows, with the issue time), BFLFP_Daily-Report_2026-10-01.pdf (daily report dialog), BFLFP_Daily-Reports_5.pdf / BFLFP_PM_3.pdf for bundles. Repair forms keep the name the server gives them (ใบแจ้งซ่อม + job no.) instead of "report.pdf".
- No database change.

## b410 — 2026-10-02 (report file names: FP_DR_DATE)
- Daily report files are named PLANT_DR_DATE: FP_DR_2026-10-01.pdf, PC_DR_2026-10-01.pdf, BFL_DR_2026-10-01.pdf (shelf rows and the daily report popup alike).
- Several daily reports opened together: FP_DR_2026-09-28_2026-10-01.pdf (first and last day); several repair forms: FP_RF_3.pdf; PM sheets: FP_PM_3.pdf.
- No database change.

## b411 — 2026-10-05 (dashboard "Daily report PDF" button)
- The button was a bare link to TODAY's daily report in a new tab. A day with no finished work yet (every morning) answered with raw text "no completed work on that day", so the PDF looked broken; on a phone app it also left the app.
- It now opens the same daily-report window as the Reports page, on the last day of the dashboard's period (never later than today): job counts for the day, ‹ › to change day, Open / Download (FP_DR_DATE.pdf) / Share on phones. A day with no finished work says so instead of failing.
- No database change.

## b412 — 2026-10-05 (dashboard Report PDF — weekly / monthly)
- The dashboard button is now "🖨 รายงาน PDF / Report PDF": the dashboard printed for the period chosen above (Week, Month, Quarter, Half year, Year, or From → To). It opens in its own tab with the browser's print window — choose "Save as PDF". Default file name FP_KPI_<from>_<to> (PC_ / BFL_).
- Thai + English. The three rings (PM compliance, Corrective closed, Machine availability) repeat as the header of every page and read exactly what the dashboard's rings read.
- Sections: PM planned vs completed (running totals + per week by verdict), Top 10 breakdowns by machine downtime, Top 10 corrective jobs by logged repair time, downtime engineering vs production, reliability (MTTR, down per BD, MTBF), work by type per week (CM+BD, IMP), throughput, crew hours per technician.
- New read-only endpoint GET /api/kpi/report-extra (top CM jobs, weekly counts, throughput, crew hours). The daily report stays on the Reports page.
- No database change.

## b413 — 2026-10-05 (Central Engineering — Electrical PM)
- Electrical points: an item cell filled YELLOW in the master workbook (SD-SP-ENG02-01) is an electrical point. The upload reads the colour (any printed copy of the list) and marks the item (pm_items.elec). BFL Rev.01: 93 points in 34 groups. BFLPC: none until its file is marked yellow and uploaded again.
- Fix: the Rev.01 workbook names its register "MList (2)". It was read as a machine group (709 asset names as check items). Sheet names ending "(n)" are now matched by their base name.
- One PM job, two crews. Plant technicians answer the plant points; electrical points are locked for them (🔒 ⚡ Central Electrical) and do not block their Stop. CE technicians (users.team='CE') answer only the electrical points on their own screen with Start check / Submit electrical — they never touch the job's timer.
- The job can be accepted (signoff, PM shelf sign) only when every point is answered. The PM shelf shows "⚡ waiting electrical (n)" and skips those sheets. A job whose points are all electrical is closed by CE's Submit.
- CE planner menu = Electrical PM only (plus chat / more): month calendar of machines with electrical points for BFL / BFLPC, tiles due / done / overdue / not counted, assign a CE technician per machine (raises the PM job if nobody has yet). CE technician menu: Electrical PM list first (assigned to me / not assigned — tap to take), then the normal job list. CE admin gets the page too.
- Programme starts 2026-09-17 (BFL and BFLPC). KPI counts from the day this build first runs (app_state ce:count-from); earlier overdue work is shown but not counted.
- Plant day board: an all-electrical PM is not offered to plant crews.
- DB: adds pm_items.elec and table pm_elec automatically; sets app_state ce:count-from once. No existing job, answer or plan is changed.

## b414 — 2026-10-05 (Electrical PM easy to find)
- A menu order the admin saved earlier does not know the new "⚡ Electrical PM" item, so it fell to the very bottom of the sidebar (under Error log / Chat). It now always sits near the top: 2nd for a Central admin, 1st for a Central planner / technician.
- No database change.

## b415 — 2026-10-05 (planner screen no longer flashes white)
- Cause: the live "has anything changed?" check is one token for all three plants, so any Start/Stop at BFL or BFLPC also redrew the BFLFP planner's Jobs page — 580 rows, 2.2 MB of HTML, rebuilt every time (≈0.4–0.5 s here, longer on an office PC). The page went white on each rebuild. Reports first replaced the page with a "Loading…" card, then redrew it.
- Fix: a live refresh now leaves the screen alone when the page it built is the same as the one shown, and never blanks the page with a "Loading…" card — the old screen stays until the new one is ready. Opening a page is still always drawn fresh; a real change still shows.
- Switching plant (BFL / BFLPC buttons) now also renames the sidebar heading.
- No database change.

## b416 — 2026-10-05 (Electrical PM as a planner calendar)
- On a PC the Central Electrical planner's "⚡ Electrical PM" is now the same PM calendar the plant planners use — Monthly / Weekly, By floor / By area / Assets, ‹ › Today, frequency colours, day panel grouped by room — cut down to machine-days that have electrical (yellow) points. Each cell ends "⚡ n machines · ✓done".
- Tiles: machines with electrical points, weekly/monthly load, machines per working day, and for the month on screen: due (counted), done, overdue, before counting (not counted).
- Day panel: each machine with ⚡ electrical/total points and a CE technician picker (or ✓ who did it). Picking raises the PM job if the plant has not yet, same as before.
- Read-only for the plant plan: no drag, no Stop PM / Issue plan / Holidays / Add task, no double-click crew board. BFL / BFLPC buttons only.
- Phones keep the simple Electrical PM list. Plant planners' PM calendar is unchanged.
- GET /api/pm/planned?elec=1 (read-only). No database change.

## b417 — 2026-10-05 (Central Electrical planner: own menu, jobs, CM plan, crew board)
- CE planner menu: Jobs · ⚡ Electrical Planning (PM plan, CM Plan) · Chat · More. The plant's Planning (PM / CM) is not on it. Plant bar shows BFL and BFLPC only.
- Jobs page and CM Plan for a CE planner show work whose Trade is Electrical, Other or empty, plus PM jobs that carry an electrical point. Mechanical / Instrument / Process stay with the plant. Counts on the tabs match.
- Crew board (double-click a day on the Electrical PM calendar, or "Plan calendar"): CE crews of CE technicians. Electrical PM cards give the PM job's ELECTRICAL part to the crew lead (the plant crew on the same job is untouched; a card with no job yet raises it). CM / BD jobs are given to the CE crew outright.
- Both planners see Other / empty jobs; whoever gives a crew first has it — a job with a CE crew leaves the plant board, a job with a plant crew is not on the CE board.
- CE crews are stored apart from the plant's (plan_teams under the plant's negative id) so the two boards never mix or carry into each other.
- No database change.

## b418 — 2026-10-05 (admins: Electrical Planning with PM + CM sub-menus)
- Every admin now has "⚡ Electrical Planning" with two sub-menus — Electrical PM plan and Electrical CM Plan — beside the plant's Planning (PM plan / CM Plan / Project). The general plans are unchanged.
- Electrical CM Plan = the CM plan calendar cut to Trade Electrical / Other / empty, with BFL / BFLPC buttons.
- Opening an electrical page while in BFLFP moves to BFL (Central Electrical covers BFL and BFLPC only) — it no longer shows an empty BFLFP calendar.
- CE planner's CM Plan uses the same electrical view. No database change.

## b419 — 2026-10-05 (Breakdown closed ring)
- Dashboard Headline KPIs: a 4th ring, "Breakdown closed" (BD closed ÷ BD raised in the period), between Corrective closed and Machine availability. Same period chips (Today / Week / Month / …), same rule as the corrective ring (closed = Done), marked INTERNAL. Opens the corrective list.
- Report PDF header carries the same BD ring.
- No database change.

## b420 — 2026-10-06 (rings box + Week = last week)
- Dashboard: Corrective closed and Breakdown closed share one box in the middle (dashed divider; stacked on a phone). PM compliance left, Machine availability right.
- "Week" now opens on the last complete week, Monday → Sunday before this one (e.g. on Tue 6 Oct: Mon 28 Sep – Sun 4 Oct). ‹ goes further back, › steps into the current week (to today). Report PDF follows the chosen week.
- No database change.

## b421 — 2026-10-07 (plant planners: no electrical corrective work)
- On BFL and BFLPC, the plant's planners and managers (not Central) no longer see CM / BD / IMP jobs whose Trade is Electrical — on the Jobs page (and its counts), the CM Plan and the day crew board. Those are Central Electrical's.
- Unchanged: Other / empty-trade jobs (both teams see them; first crew wins), all PM jobs (plant crews do their plant points), jobs a plant crew was already given, admins (see everything), BFLFP (no Central Electrical there).
- No database change.

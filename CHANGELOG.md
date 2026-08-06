# Changelog — BFLFP CMMS
All notable changes. Format: version · date · changes. Newest on top.

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

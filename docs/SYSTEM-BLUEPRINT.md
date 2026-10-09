# BFLFP CMMS — System Blueprint (consolidated)
Architecture · schema · workflows · roles · KPIs · **which machines get OEE**
Grounded in real data: BFL (745 assets) · BFLFP (180) · BFLPC (734 — already
classified Machine 572 / Utilities 156 / Vehicle 3 in your own register).

---

## 1. Database schema (implemented + v1.1 additions ⭐)

**factories** ⭐ — id, code (BFL/FP/PC), name, doc_form_code, kpi_targets(json)

**machines** — id, code, name, active, line/area, criticality A/B/C,
ideal_rate (pcs/min), pm_freq_days, ⭐factory_id, ⭐kpi_class (OEE/AVAIL/RUNTIME —
see §5), ⭐brand_model, ⭐serial_no, ⭐year_install, ⭐last_pm_date
(⭐ columns map 1:1 to your BFLPC Asset Register for direct import)

**users** — id, username, password(PBKDF2), name, role, active, ⭐factory_id

**jobs** (work orders) — id, jobid (PRD-YYMM-###), jobtype (PM/CM/IMP/BD),
machine_id, descr, priority 1-3, status (Reported→WaitingApproval→Assigned→
Released→InProgress⇄Paused→Rework→ServiceCompleted→Done/Rejected),
planned_date/start/end, due_date, lead_tech, helpers, progress, pending_reason,
problem, root_cause, solution, fault_category, fault_component, maint_action,
img_before/after, sign_requester/inspector, cleared_worksite, new_issue_id,
rework_count, jobsource, created_by, created_at, done_at

**timelogs** (segments) — id, job_id, tech, seg_type (work/routine/standby/
waiting/meeting), start, end, pause_reason — one open segment per tech (enforced)

**shiftlogs** — log_date, shift, machine_id, planned_min, output, good,
reject(auto), entered_by — UNIQUE(date,shift,machine)

**spare_parts** ⭐(next module) — id, part_no, name, category, factory_id,
stock, min_stock, unit_cost, location, machine links (BOM)
**part_moves** ⭐ — part_id, qty(+/-), job_id, moved_by, at — job usage = cost/asset
**requisitions** ⭐ — part_id, qty, status (Waiting→Approved→Ordered→Received),
requester, supplier, job_id

**channels / messages / activities** — chatter (kind: message/note/system),
threads, scheduled activities with assignee + due
**push_subs** — web-push subscriptions per user

## 2. Core workflows (canonical — details in DATA-AND-FLOW-SPEC.md)

- **Request** → operator (QR scan) → Reported (or WaitingApproval if cost) →
  push planner
- **Assignment**: planner releases with crew + window (BD skips the gate:
  push ALL techs, first ▶ = response time). Auto-assignment rule v1.1:
  BD routes by machine's line + criticality to the on-duty team
- **Execution**: timer segments, pause reasons split repair vs waiting-parts
- **Low-inventory alert** ⭐: part_moves drops stock below min_stock →
  push planner + line in requisition queue
- **Downtime logging**: automatic — BD created_at → done_at; no manual entry
- **Closure**: evidence contract (codes/photos/signature) → planner verdict →
  Done stops downtime clock; PDF + history + KPI update fire automatically

## 3. Roles & permissions (implemented)

| Role | Can | Cannot |
|---|---|---|
| Requester/Operator | report issues, shift log, track own, evaluate own jobs | see other plants' pools, plan, close jobs |
| Technician | accept/start/pause/finish own jobs, evidence, parts-used ⭐ | release jobs, edit master data, approve |
| Planner | approve/plan/release, verdicts, activities, requisition approve ⭐ | edit users, delete anything |
| Manager | dashboards, reports, PDFs — read-everything | operational writes |
| Admin | master data, users, factories, targets | daily operations bypass |

## 4. KPIs (EN 15341-aligned, implemented in /api/kpi)

| KPI | Formula | Target |
|---|---|---|
| MTTR | Σ work-segments on BD ÷ BD count | ≤ 4 h critical |
| MTBF | run time ÷ BD count | trend up |
| Response time | first ▶ − created_at | track |
| PM compliance | PM done ≤ due ÷ PM due | ≥ 90 (PC target: 95) |
| Planned-vs-reactive | planned work minutes ÷ total | ≥ 70% |
| FTFR | done w/o rework/re-issue ÷ done | ≥ 85% |
| Backlog age | open jobs 0-7/8-30/30+ | zero P1 >24h |
| OEE | A × P × Q (weighted) | ≥ 65% batch |
| Plant OEE | Σ(good×ideal cycle) ÷ Σ planned | weighted only |

## 5. ⭐ WHICH MACHINES GET OEE — the classification policy

**OEE requires three things: countable output + a defined ideal rate +
planned production time. If any is missing, OEE is meaningless — use
availability instead.** An air conditioner produces no countable output →
**NO OEE for aircons.** It gets availability + MTBF/MTTR + PM compliance.

Three KPI classes (field `machines.kpi_class`):

### Class 1 — OEE (production machines with rated speed)
Fillers, seamers, labelers, packing machines, sachet lines, conveyor-fed
packing. Data needed from production **per shift**: planned_min, output,
good (reject auto). → Full OEE A×P×Q.

### Class 2 — AVAIL (utilities & support: no product output)
Air compressors, **air conditioners/HVAC**, chillers, boilers, water pumps,
cooling towers, electrical panels, IT. BFLPC register: 156 "Utilities" rows.
Data needed: **none from production** — downtime comes from BD jobs
automatically; optional run-hour meter for meter-based PM.
→ KPIs: Availability (uptime), MTBF, MTTR, PM compliance. Never OEE.

### Class 3 — BATCH (cycle equipment)
Mixers, retorts, cookers. Output is batches, not pcs/min.
Data per shift: planned_min, cycles run, cycles good (failed batch = quality
loss), std cycle time. → Batch-OEE: A = run/planned ·
P = (cycles × std cycle time)/run · Q = good cycles/cycles.
If batch counting is too much for the pilot, treat as Class 2 first.

Vehicles (PC has 3): Class 2 + odometer meter.

**Default mapping for your registers** (importer applies automatically):
- Register type "Utilities" → AVAIL
- Groups AIR COMPRESSOR / CHILLER / BOILER / PUMP / HVAC / IT → AVAIL
- Groups FILLER / SEAMER / LABELER / PACK → OEE
- Groups MIXER / RETORT / COOKER → BATCH
- Anything unmapped → AVAIL (safe default; admin promotes to OEE by setting
  ideal_rate — no rate, no OEE, rule #3 protects you from fake numbers)

**Answer to the direct question:** if it's an air conditioner → no OEE;
the system tracks its availability, failures and PM only, and asks
production for nothing. Shift-log entry is only requested for Class 1/3
machines that actually ran — for ~1,600 assets across 3 factories, only
the production lines (a few hundred) ever need the 2-minute entry.

## 6. Approved-format reports (generated, filed to Report/YYYY/MM/DD)

| Report | Format | Trigger |
|---|---|---|
| ใบแจ้งซ่อม (repair form) | PDF replica of F-SP-ENG02-03 Rev.01 (per-factory form code in factory settings ⭐) | button / on Done |
| Daily maintenance report | Landscape PDF: 3 %, job table, tech time | button / evening |
| Evening Gantt | dashboard view (TV mode ⭐) | live |
| Monthly PM compliance | per machine × month grid (matches your BFLPC "PM Schedule" sheet style) ⭐ | monthly |
| OEE report | per machine A/P/Q/OEE + trend, Class-1/3 only ⭐ | weekly/monthly |
| Machine history sheet | per machine: jobs + causes + costs (matches BFL "Hist" sheet intent) | button |
| Audit pack (food safety) | job evidence bundle: photos/signatures/cleared-worksite ⭐Phase 4 | on demand |

⭐ = designed, next builds. Everything unstarred runs today and is covered
by the 42-check smoke suite.

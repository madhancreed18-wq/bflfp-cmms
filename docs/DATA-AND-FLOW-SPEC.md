# BFLFP CMMS — Flow & Data Specification (canonical)
Every flow, every field, who enters it, when, and which report consumes it.
Rule: if a field feeds no decision or report, we don't collect it.

---

## 0. Master data (entered once, by Admin — everything else depends on it)

| Field | Required | Used by |
|---|---|---|
| Machine code + name | ✅ | everything |
| Line/area | ✅ | line OEE roll-up, filters |
| Criticality A/B/C | ✅ | breakdown priority, planning order |
| Ideal rate (pcs/min) | for OEE machines | Performance, OEE |
| PM frequency (days) | for PM machines | PM auto-generation |
| PM checklist text | recommended | PM job instructions |
| Users: name, role, login | ✅ | assignment, audit trail |

---

## 1. PM flow (preventive)

```
AUTO   System creates PM job when frequency elapsed (due_date = today)
       └─ data born: jobid, machine, jobtype=PM, due_date, source=PM-Auto
PLAN   Planner sets planned_date + start/end + lead + helpers → Released
       └─ data: planned window, crew          [push → techs]
DO     Tech ▶ Start → work segment opens
       Tech follows checklist (instructions), ⏸ pause w/ reason if interrupted
       └─ data: segments (start/end/pause_reason), progress %
CLOSE  Tech ✅ Finish → solution + action code (+ photos if abnormal)
       └─ data: solution, maint_action, done_at   [push → planner]
CHECK  Planner approves (2 questions) → Done  (or Rework → back to DO)
       └─ data: cleared_worksite, sign_inspector, rework_count
```

**PM data contract:** machine, due_date (auto) · planned window + crew (planner)
· segments + action code (tech) · approval (planner).
**Feeds:** PM compliance %, planned-vs-reactive ratio, machine history,
daily report, carryover/OverDue list.

## 2. CM flow (corrective — found problem, machine still runs)

```
REPORT Operator reports issue: machine, symptom, priority (1-2)
       [NeedApproval ☑ if cost] → Reported / WaitingApproval
       └─ data: descr, priority, jobsource=OperatorReport, created_at
       [push → planner]
PLAN   Planner approves (if needed), schedules into a day → Released
DO     Same as PM: segments, pauses w/ reasons, progress
CLOSE  Tech ✅ Finish: problem + root_cause + solution
       + FAULT CODES: category / component / action   ← the analytics gold
       + before/after photos
CHECK  Approve → Done | Rework | Rejected → re-issue (new linked jobid)
```

**CM data contract:** symptom + priority (operator) · schedule (planner) ·
segments + problem/root-cause/solution + fault codes + photos (tech) ·
verdict (planner).
**Feeds:** top failure components, repeat-failure flag, FTFR, repair-form PDF,
machine history, backlog age.

## 3. BD flow (breakdown — machine stopped, production waiting)

```
REPORT Operator taps ด่วน! → BD job, clock starts (created_at = stop time)
       [push → ALL techs + planner]  [auto-post → 🔴 Breakdown feed]
GO     First available tech ▶ Start (no planner gate)
       └─ RESPONSE TIME = start − created_at
FIX    Work segments; ⏸ รออะไหล่ segments counted separately
       └─ ACTIVE REPAIR = Σ work segments · WAITING = Σ parts-wait
CLOSE  Finish: problem/root-cause/solution + fault codes + photos mandatory
CHECK  Approve → Done, done_at set
       └─ TOTAL DOWNTIME = done_at − created_at
```

**BD data contract:** stop report (operator) · immediate start (tech) ·
segments incl. waits (tech) · codes + evidence (tech) · release (planner).
**Feeds:** MTTR, MTBF, response time, downtime, OEE availability loss,
Gantt red blocks, breakdown completion %.

## 4. Shift log (production — the OEE fuel)

```
Shift end (per machine that ran): planned_min, output, good  (reject auto)
Entered by line leader — 2 minutes. No shift log = no OEE for that machine/day.
```
**Feeds:** Availability (with BD downtime), Performance (with ideal rate),
Quality, OEE, plant weighted OEE, trends.

## 5. Time segments (the engine under everything)

One open segment per technician, ever. Starting anything closes the previous.

| seg_type | Counts as | In Gantt |
|---|---|---|
| work (on PM/CM job) | planned hours | blue |
| work (on BD job) | reactive hours + MTTR | red |
| routine | routine hours | gray |
| standby / waiting / meeting | captured honestly, excluded from job math | orange |

Pause reasons: Breakdown แทรก · รออะไหล่ · รอฝ่ายผลิต · พักเบรก · หมดกะ.

## 6. Reporting — every output and its ingredients

| Output | Needs |
|---|---|
| ใบแจ้งซ่อม PDF (per job) | job fields, crew, problem/cause/solution, photos, signatures, cleared_worksite, re-issue link |
| Daily report PDF | day's jobs + statuses + pending reasons, 3 completion %, tech time summary |
| Evening Gantt | segments of the day, per tech, colored by type |
| 3 daily percentages | Released-only planned jobs, BD jobs, done counts |
| OEE (machine/plant) | shift log + BD downtime + ideal rate |
| MTBF / MTTR / response | run time, BD count, work segments, first-start times |
| FTFR | rework_count + new_issue_id on done jobs |
| Planned-vs-reactive | work segment minutes by jobtype |
| PM compliance | PM done_at vs due_date |
| Top failure components / repeat flag | fault codes on closed jobs |
| Backlog buckets | open jobs age + priority |
| Machine history | everything above filtered by machine |

## 7. Data quality rules (enforce or the metrics lie)

1. Job with no timer segments → cannot reach ServiceCompleted (block in UI)
2. BD close requires fault category + component (dropdowns, not optional)
3. Shift log missing → machine shows "no OEE data" not fake 100%
4. Only Released jobs count in planned % (Draft/Assigned excluded)
5. Rework always via status (never delete-and-recreate) — protects FTFR
6. One open segment per tech (already enforced server-side)
7. done_at set once, never edited — downtime math depends on it

## 8. Who touches what (RACI-style)

| Data | Operator | Planner | Tech | Manager | Admin |
|---|---|---|---|---|---|
| Issue reports | **Create** | approve/plan | — | view | — |
| Day plan / release | — | **Own** | view | view | — |
| Segments/pauses | — | — | **Own** | view | — |
| Close + fault codes + photos | — | verdict | **Own** | view | — |
| Shift log | **Own** | view | — | view | — |
| Master data | — | request | — | — | **Own** |
| Dashboards/PDF | — | daily use | — | **Own** | — |

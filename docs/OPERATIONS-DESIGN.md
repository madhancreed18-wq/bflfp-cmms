# BFLFP CMMS — Operating Design (Pilot: 1 line, 10 machines, 1 month)

## 1. Users needed

| Role | Who | Count (pilot) | What they enter |
|---|---|---|---|
| Operator | Line leader per shift | 2–3 | Issue reports; **shift run log** (2 min/machine at shift end: planned time, output, rejects) |
| Production supervisor | 1 | 1 | Reviews shift logs, confirms QC reject counts |
| Planner | Maintenance planner | 1 | Day plan, assignment, release, approve/rework |
| Technician | Maintenance team | 3–5 | Start/pause/finish, pause reasons, photos, signatures, service report |
| Manager | Maintenance/plant manager | 1–2 | Views dashboard, receives daily PDF |
| Admin | System owner (Madhan) | 1 | Machines, users, ideal rates |

**Total: ~10–12 accounts.** Licenses cost nothing, so every person gets their own
login — never share accounts, or the time segments and audit trail lose meaning.

## 2. Master data required per machine (one-time, admin)

- Machine code + name (done)
- **Ideal rate** (pcs/min or kg/hr) — required for Performance/OEE
- Criticality (A/B/C) — drives breakdown priority and planning order
- PM frequency (weekly/monthly) + PM checklist text
- Line/area grouping (for line-level OEE roll-up)

## 3. Data entry flows

### Daily (the habit that makes or breaks the pilot)
```
Shift start   Planner releases day plan (PM + CM jobs)
During shift  Operator reports issues the moment they happen
              Technicians run the timer on every job (work/pause/reasons)
Shift end     Operator enters shift run log per machine that ran:
              planned minutes / total output / good / reject
Evening       Manager opens dashboard; daily PDF auto-generated
```

### Breakdown flow (BD)
```
1. Operator taps ด่วน! → BD job created, push to ALL techs + planner
   → clock starts (report time = created_at)
2. First available tech taps ▶ เริ่ม → RESPONSE TIME captured
   (no waiting for planner at 2am — planner is notified, not a gate)
3. Repair happens in segments. Pauses with reasons:
   "รออะไหล่" = waiting-parts time (kept separate — protects techs
   in MTTR reviews), "รอฝ่ายผลิต" = production-caused wait
4. Tech finishes: Problem/RootCause/Solution + after photo mandatory
5. Planner/supervisor approves (2-question popup) → machine released
   → total downtime clock stops
6. Rejected work → Rework loop or re-issue (new linked PRD number)
```

### PM flow
```
1. Planner schedules PM jobs for the week (from PM calendar/checklists)
   with planned date + due date
2. Released each morning in the day plan alongside CM work
3. Tech executes checklist (in job Instructions), same timer flow
4. PM done on-or-before due date = compliant
5. Missed PM auto-appears in OverDue tab next day (carryover count +1)
```

## 4. Standard calculations (from data we already capture + shift log)

Period = day / week / month, per machine, per line, per plant (weighted).

| Metric | Formula | Data source |
|---|---|---|
| Total downtime (BD) | Σ (approve time − report time) per BD job | jobs table |
| Active repair time | Σ work segments on BD jobs | timelogs |
| Waiting-parts time | Σ pause gaps with reason รออะไหล่ | timelogs |
| Response time | first segment start − created_at | jobs + timelogs |
| **MTTR** | active repair time ÷ # completed BD | timelogs |
| Running time | shift planned minutes − BD downtime | shiftlog + jobs |
| **MTBF** | running time ÷ # BD in period | shiftlog + jobs |
| Failure rate | # BD ÷ (running hours ÷ 1000) | same |
| **Availability** | running ÷ planned | shiftlog + jobs |
| **Performance** | output ÷ (ideal rate × running) | shiftlog + machine master |
| **Quality** | good ÷ output | shiftlog |
| **OEE** | A × P × Q | above |
| Plant OEE | Σ(good × ideal cycle time) ÷ Σ planned time (weighted — never average of %) | all machines |
| PM compliance | PM done ≤ due ÷ PM due | jobs |
| Repeat failure flag | same machine + similar problem ≥3 in 90 days | jobs history |

Machine history page = all jobs + all shift logs + trend charts of the above.

## 5. What this means we build next (Phase 2 in the code)

1. `shiftlog` table + operator's 2-minute shift-end entry screen
2. Machine master fields: ideal_rate, criticality, line, pm_freq
3. Machine history page: job list + KPI trends per machine
4. KPI engine: nightly rollup per machine/day → daily summary table
   (dashboards read summaries, never raw history — scales to 100 machines)
5. OEE / MTBF / MTTR cards + loss split (downtime vs speed vs quality)
   on the manager dashboard
6. PM recurrence auto-generation (weekly/monthly templates)

## 6. Pilot success criteria (decide BEFORE starting)

- ≥95% of jobs have timer segments (not typed times afterwards)
- Shift log entered for every running machine ≥90% of shifts
- Every BD has root cause filled (no blanks)
- Evening dashboard used in the morning meeting daily
- After 1 month: first real MTBF/MTTR/OEE baseline per machine → THEN
  scale to remaining machines with credibility

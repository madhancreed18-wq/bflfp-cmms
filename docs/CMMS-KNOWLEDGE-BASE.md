# CMMS Knowledge Base — for the full plant version + commercialization
Compiled 2026-08 from industry research. This document guides build priorities
and the commercial strategy.

---

## 1. Market & pricing landscape (2026)

| Product | Entry price | Notes |
|---|---|---|
| MaintainX | free tier; ~$16–49/user/mo | Mobile-first leader, fastest adoption |
| Limble | ~$40/user/mo | Strong PM + reporting |
| UpKeep | ~$45/user/mo | Best-rated mobile app |
| Fiix | free tier; $45–75/user/mo | Rockwell-owned, integrations |
| QRmaint | mid-range | QR-code angle, Poland |
| OxMaint | free tier | Aggressive AI positioning |
| osapiens HUB | free ≤5 users; €29–49/user/mo | European; moat = SAP-certified integration; Coca-Cola reference |
| IBM Maximo | enterprise, on request | Deep but months-long implementations, dated UX |
| **Factorium (Thai)** | **freemium; premium ~$4,800/year** | Local rival: Thai language, LINE integration, CPF + Siam Kubota references |

Key economics: a 20-user plant on a $40/user product pays ~$9,600/year forever.
Factorium premium ≈ 170,000 THB/year. **Our structural advantage: self-hosted,
no per-user fee — one-time license + support contract undercuts everyone.**

## 2. The standard feature taxonomy (what "full version" must mean)

Core (we have): work orders w/ lifecycle + approval, requests, PM scheduling,
asset registry + history, mobile PWA + push, time tracking, photos/signatures,
PDF reports, dashboards/KPIs, chat/chatter, roles, TH/EN.

Expected by buyers (gap list, priority order):
1. **Spare parts inventory** + parts-per-job costing (→ cost per asset)
2. **Requisitions/purchasing** (request → approve → order → receive)
3. **Fault code taxonomy** (see §4 — ISO 14224-style)
4. **Structured checklists** with per-step ticking + values (temperature readings…)
5. **QR codes per asset** (report + history by scan)
6. **Meter-based PM** (run-hours triggers, not just calendar)
7. Calibration management (food plants — see §5)
8. Contractor/vendor management + external work orders
9. TV dashboard mode; multi-site support; CSV/Excel import-export everywhere
10. API for ERP integration (advertise "open API" — buyers ask even if unused)

## 3. KPI standards (credibility layer for sales + dashboards)

- **EN 15341**: the European maintenance-KPI standard — 71 indicators incl.
  MTBF, MTTR, PM compliance, schedule compliance. Quoting "KPIs per EN 15341"
  elevates the product in audits and tenders.
- **SMRP Best Practice metrics** (US equivalent) — same core set.
- Benchmarks to bake into dashboard targets:
  - PM compliance ≥ 90% (world-class); reactive rises sharply below 85%
  - Planned-vs-reactive ratio: 70–85% planned = mature program
  - MTTR < 4 h for critical assets
  - OEE: 85%+ world-class discrete; **65%+ batch/process (our food context)**
  - Backlog: zero P1 > 24 h, zero P2 > 72 h
- Availability = MTBF ÷ (MTBF + MTTR) — alternative formula to show.
- **First-Time Fix Rate (FTFR)** = jobs fixed without rework/re-issue ÷ total
  completed — one of the "most overlooked" KPIs (osapiens). We can compute it
  today: jobs without Rework events or NewIssueID ÷ jobs Done. (It was also in
  the original BFLFP Power Pages dashboard — restore it.)
- **Dashboard vs report duality** (osapiens): dashboards = real-time status for
  running today; reports = historical trends + root-cause for leadership
  decisions. Ship both; don't confuse them. "Does it show WHY something failed
  or just THAT it did" — the root-cause/fault-code layer is what separates us.
- Data-silo stat: <40% of maintenance teams use dedicated KPI tools — the
  market is still spreadsheets. Our pitch targets the spreadsheet-and-LINE crowd.

## 4. Failure data standard — ISO 14224 (adopt simplified)

ISO 14224 defines the taxonomy that makes failure data analyzable. Adopt a
simplified 2-level version:

- **Failure category**: mechanical / electrical / instrument-sensor / process /
  operator error / external / no-fault-found
- **Failed component**: bearing, seal/gasket, belt/chain, motor, pump, valve,
  sensor, heater, pneumatic, controller/PLC, wiring, lubrication, other
- **Maintenance action**: adjust, calibrate, lubricate, clean, replace part,
  repair, rebuild, inspect, modify

These become dropdowns on the breakdown finish form → unlocks MTBF by component,
top failure modes, repeat-failure detection (same machine+component ≥3 in 90 d).
Free text stays for detail; codes make the analytics.

## 5. Food & beverage specifics (BFLFP's world = our commercial niche)

Regulatory context: GMP (FDA 21 CFR 110/117 pattern, Thai GMP กฎกระทรวง),
HACCP prerequisite programs, BRC/IFS audits. Maintenance requirements:

- **Sanitation PM schedules** per equipment category with documented frequency,
  method, responsible person — auditors ask for these first
- **Calibration management**: temperature devices, pH meters, scales, pressure
  gauges — most-audited GMP area. Needs: instrument list, calibration due dates,
  certificates attached, out-of-tolerance workflow
- **Food-safe evidence**: post-repair cleaning verification (our ClearedWorkSite
  checkbox is exactly this — formalize as "food safety release"), food-grade
  lubricant tracking, foreign-material checks after maintenance (tools count)
- Audit stat worth quoting: HACCP-aligned PM programs hit 94% first-pass audit
  vs 41% for reactive plants
- 58% of food facilities still run reactive (2026) → big target market

**Commercial positioning: "CMMS สำหรับโรงงานอาหาร" — Thai-language, GMP/HACCP
-ready CMMS. Factorium is generic; we are the food-factory specialist.**

## 6. Why CMMS implementations fail (build + onboarding implications)

- Rushed big-bang rollouts: 60–70% failure; phased pilots: 75–85% adoption,
  benefits 40–60% faster → our 1-line pilot design is correct; make it the
  standard onboarding methodology for customers too
- Adoption benchmark: 85% of technicians actively using within 30 days
- #1 killer: requiring a desktop to close work → everything must complete
  on the phone (we're aligned)
- #2 killer: perfecting data before first use — months importing every asset
  before one work order runs → ship with 10 machines, grow
- Success levers: champion user on the floor, seed real (not demo) jobs in
  week 1, management actually reading the dashboard daily

## 7. Commercialization strategy

**Target segment**: Thai SME food/beverage factories (10–200 machines) that
find Factorium generic and international SaaS expensive/English-only.

**Pricing models to offer:**
1. Self-hosted perpetual: one-time license per plant + annual support (20%)
   — unique vs all SaaS rivals; data stays in the factory (food companies care)
2. Hosted subscription per PLANT (not per user!) — flat price, unlimited users;
   directly attacks the per-user model everyone resents
3. Free tier: 1 line / 10 machines — our current build, as the funnel

**Every winner has one moat** (lesson from the comparison articles):
MaintainX = mobile simplicity · Maximo = enterprise depth · osapiens =
SAP-certified integration · QRmaint = QR codes. **Ours = Thai food-factory
specialist + unlimited users self-hosted + time-segment Gantt/OEE.** Pick the
moat and repeat it everywhere.
ERP-integration question (buyers will ask): answer for Thai SMEs = CSV/Excel
import-export everywhere + open REST API (FastAPI gives /api/docs for free);
SAP-certified connectors are enterprise-tier, not our segment.

**Differentiators to lead with:** unlimited users; Thai-first + GMP/HACCP
modules; time-segment Gantt (nobody else has pause/resume with reasons);
OEE built-in (usually a separate product); LINE + web push; runs on a plant
PC without internet.

**Before selling (legal/ops checklist):** resolve Bluefalo IP ownership in
writing; company or personal entity; strip BFLFP branding into per-customer
config (form codes, logos already env-configurable); support commitment plan;
customer data backup/restore procedure; PDPA (Thai data protection) statement.

## 8. Build roadmap implied by this research

Phase 3 (make it a complete CMMS): inventory + requisitions (seed from
SpareParts CSVs), fault codes (§4), structured checklists, QR codes, dashboard
trends/targets/backlog (OxMaint principles), TV mode.

Phase 4 (food-factory edition = the commercial edge): calibration module,
sanitation PM templates, food-safety release workflow, audit report pack
(every job's evidence in one PDF for BRC/GMP auditors).

Phase 5 (commercial hardening): multi-plant/tenant, per-customer branding,
license keys, automated backup, installer (PyInstaller + Inno), PostgreSQL,
API docs, onboarding wizard following §6 methodology.

## Sources
- Pricing: fabrico.io CMMS pricing guide 2026; limble.com/learn/cost;
  oxmaint top-10 comparison 2026
- Factorium: capterra.com/p/10006105; fogwing.io best-CMMS-Thailand
- Standards: iso.org/standard/64076 (ISO 14224); EN 15341:2019+A1:2022
- Food: oxmaint GMP compliance food manufacturing; fda.gov HACCP guidelines;
  fooddocs.com GMP guide
- Implementation: l2l.com CMMS step-by-step; oxmaint implementation guides

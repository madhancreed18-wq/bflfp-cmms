# Two-team work plan — module split + workflow
(updated 2026-08-05 · current release v2.3.1 · smoke gate = 53 checks)

Works the same whether "team" = 1 person or several: each TEAM owns modules;
inside a team, people split the module's backend/frontend between them.

## 0.0 Git setup — exact commands (once, ~20 minutes)

On the machine that has the code today:
```
cd BFLFP-CMMS
git init
git add .
git commit -m "v2.3.1 baseline"
```
Create a free PRIVATE repo on github.com (no code is public), then:
```
git remote add origin https://github.com/<your-account>/bflfp-cmms.git
git push -u origin main
```
Everyone else (both teams):
```
git clone https://github.com/<your-account>/bflfp-cmms.git  C:\dev\bflfp-cmms
copy "Start CMMS (PostgreSQL).bat.example" "Start CMMS (PostgreSQL).bat"
     (then edit in your own DB password — this file is git-ignored)
python tests/smoke.py     ← must print 53/53 before you start working
```
- Clone to a LOCAL folder (C:\dev\...), never inside OneDrive
- `data/`, `*.db`, `uploads/`, and the real PostgreSQL .bat are git-ignored:
  no plant data and no passwords ever enter the repo
- No internet at the plant? Gitea (self-hosted git server, one .exe) works the
  same on LAN — same commands, different remote URL

## 0. Foundation first (do together, ~half a day) — non-negotiable

1. **Move development off OneDrive.** Two people editing a synced folder creates
   "conflicted copy" files and silent overwrites. Instead:
   - `git init` in BFLFP-CMMS → push to a **private GitHub repo** (free)
   - Each person `git clone` to a local folder on their own machine
   - The OneDrive folder keeps only deliverables (decks, docs, releases)
2. **Split the frontend monolith** (index.html) into module files:
   `static/js/core.js` (api, i18n, login, helpers) · `planner.js` · `tech.js` ·
   `detail.js` · `chat.js` · `admin.js` · `kpi.js` — index.html keeps only the
   shell + script tags. This is what makes parallel work conflict-free.
3. Agree the contracts: `DATA-AND-FLOW-SPEC.md` is the shared truth. Schema
   changes (db.py) must be announced in chat BEFORE coding — the database is
   the one file you both depend on.

## 1. Module ownership

New backend module = new file `server/<module>.py` + one line in app.py.
New frontend module = new js file + one script tag. That's why this split
almost never merge-conflicts.

### Developer A — Operations core (workflow depth)
| Module | Files | Notes |
|---|---|---|
| Spare-parts inventory | server/inventory.py + js/inventory.js | Seed from SpareParts CSVs; stock in/out; parts-used-on-job (cost per machine) |
| Requisitions | same module | request → approve → ordered → received; links to "รออะไหล่" pauses |
| Enforcement rules | jobs.py (small edits) | The 3 iron rules: no segments = no finish; BD needs fault codes; no shiftlog = no OEE shown |
| Structured checklists | server/checklists.py + tech UI | Per-step ticking with weights → real progress % |
| QR codes | small: /m/{code} route + sticker PDF | Scan → machine report/history |

### Developer B — Analytics & experience
| Module | Files | Notes |
|---|---|---|
| TV dashboard | static/tv.html | Full-screen dark, auto-refresh 30s, for the workshop screen |
| Machine analytics | kpi.py + js/kpi.js | Loss matrix (availability/speed/quality loss per machine), trends per machine |
| Report pack | reports.py | Excel exports, monthly summary PDF, audit evidence pack |
| Notifications+ | push.py + settings UI | Per-user notification preferences; LINE bot channel |
| ~~Polish & i18n~~ | ~~all frontend~~ | ✅ DONE in v2.3.0 — full TH/EN everywhere |
| Excel importers | tools/import_assets.py | Load the 3 factory registers (BFL 745 / FP 180 / PC 734) with kpi_class mapping |

### Shared / rotating
- Bug fixes on modules you own; review each other's PRs (rule: nobody merges
  their own PR unreviewed)
- Weekly integration test: run the smoke script end-to-end together
- The pilot support rota once live: alternate weeks as "first responder"

## 2. Daily workflow

```
morning:   git pull origin main
work:      branch per feature  →  feature/inventory-stock
           small commits, push daily even if unfinished
finish:    python tests/smoke.py  → 53/53 REQUIRED, then
           Pull Request → someone from the OTHER team reviews → merge to main
merge:     bump APP_VERSION (config.py) + add CHANGELOG entry — every merge
weekly:    run smoke on main together · plan next modules · 30 min
```

Rules that prevent 90% of team pain:
1. Never commit directly to main (except hotfix agreed in chat)
2. db.py migrations: append-only, comment with initials + date; announce first
3. If both must touch the same file, do it in sequence, not in parallel —
   say so in chat ("I'm in jobs.py until lunch")
4. The data/ folder is never in git (.gitignore already covers it)
5. Keep PRs small — one module feature per PR, reviewable in 10 minutes

## 3. Suggested first sprint (2 weeks)

| Week | Dev A | Dev B |
|---|---|---|
| 1 | Foundation together (git, frontend split) → inventory tables + parts master import | TV dashboard + per-user notification settings |
| 2 | Parts-used-on-job + stock movements + low-stock push | Loss matrix + machine trend page + Excel export |
| End | Integration test: breakdown consumes a part → stock drops → cost appears on machine history → TV shows it | |

## 4. Definition of done (per module)
- Works on phone + desktop · TH labels present · pushes fire where specified
- Smoke-tested via curl or UI end-to-end · reviewed by the other dev
- If it adds data: the field appears in DATA-AND-FLOW-SPEC.md with owner + report

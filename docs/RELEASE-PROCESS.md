# Release process — how we change modules without breaking each other

The promise: you can redesign any module, and if the smoke test is green,
the other modules still work. Version numbers make every state recoverable.

## 1. Module isolation rules (why changes stay contained)

- One backend module = one file in `server/` with its own router.
  Modules may import ONLY from `db.py`, `auth.py`, `push.py`, `config.py` —
  never from each other. (Cross-needs go through the database or a helper
  in db.py.)
- Frontend: one render function-family per module; shared helpers stay in core.
  (Next refactor: one JS file per module.)
- The database schema is the ONE shared contract:
  - migrations in db.py are APPEND-ONLY (add columns/tables; never rename,
    never drop, never change meaning of an existing column)
  - if a module needs a column changed → add a NEW column, migrate values,
    deprecate the old one in a comment
- API responses: fields may be ADDED freely; existing fields never change
  type or disappear (the frontend of another module may read them).

## 2. Versioning (semver)

`APP_VERSION` in `server/config.py` — shown in the app footer and /api/health,
so you always know what a phone is running.

| Bump | When |
|---|---|
| PATCH 1.0.x | bug fix, no behavior change |
| MINOR 1.x.0 | new module/feature, existing behavior unchanged |
| MAJOR x.0.0 | anything that breaks: schema meaning change, removed API field, changed workflow |

## 3. The change cycle (every module change, no exceptions)

```
1. branch        git checkout -b feature/<module>-<change>
2. build         change ONLY your module's files (+ append-only db.py if needed)
3. gate          python tests/smoke.py         ← must be 42+/42 GREEN
                 (add new checks for your new feature — the suite only grows)
4. version       bump APP_VERSION + add CHANGELOG.md entry
5. review        PR → the other developer reviews → merge to main
6. tag           git tag v1.1.0 && git push --tags
7. release       zip the repo state → releases/BFLFP-CMMS-v1.1.0.zip (OneDrive)
8. deploy        on the server: git pull (or unzip), restart Start CMMS.bat
9. verify        open /api/health → version matches · run smoke.py once on
                 the server (it uses a throwaway DB, production data untouched)
```

## 4. Rollback (when a release misbehaves in production)

1. Stop the app
2. Restore previous code: `git checkout v1.0.0` (or unzip previous release)
3. Data is safe: schema changes were append-only, so OLD code runs happily
   on the NEW database — this is exactly why rule 1 exists
4. Restart, verify /api/health shows the old version
5. Fix forward on a branch; never edit production in place

## 5. Monitoring the running system

- `/api/health` — ok flag, version, user/job counts. Check it in the browser,
  or point a free uptime monitor (e.g., UptimeRobot) at it via the tunnel URL
  → LINE/email alert when the app is down
- The console window shows every request + errors live
- Weekly: run smoke.py against the codebase + skim data/ folder size
- Nightly backup = copy `data/` (one folder, cron/Task Scheduler)

## 6. The growth rule for the smoke test

Every new module ships WITH its smoke checks in tests/smoke.py.
Every bug found in production gets a check that would have caught it.
The suite only grows — that is what "updating like real software" means.

Proof it works: the suite caught a real bug on its first run
(rework_count stale in API response) — fixed before any user ever saw it.

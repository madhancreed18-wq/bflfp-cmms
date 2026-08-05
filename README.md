# BFLFP CMMS — Full Version (Phase 1: 10 machines)

Self-hosted daily maintenance planner. Python + FastAPI + SQLite,
structured as a VS Code project. Everything from the demo, production-shaped.

## Run

- Double-click **Start CMMS.bat**, or
- Open the folder in **VS Code** → F5 (launch config included)

First run creates `data/cmms.db` with 10 machines and 6 users.

## Accounts (password `1234` — change via Admin after first login)

| Login | Role |
|---|---|
| `admin1` | Machines + users management |
| `planner1` | Day plan, tabs, approve/rework |
| `tech1` `tech2` | Timer, photos, signatures |
| `op1` | Report issues |
| `manager1` | Dashboard + daily PDF |

## Project structure

```
run.py                  entry point
server/
  config.py             paths, company name, port (env-overridable)
  db.py                 SQLite layer + schema + seed  ← swap engine here for PostgreSQL
  auth.py               sessions, login, hashed passwords (PBKDF2)
  jobs.py               jobs, tabs, time segments, media, re-issue, dashboard
  reports.py            repair-form PDF + daily report PDF (reportlab)
  push.py               web push (VAPID, auto-generated keys)
  admin.py              machines + users CRUD
static/                 the web app (PWA: manifest, service worker, icons)
data/                   database, uploads, Report/YYYY/MM/DD, VAPID key (gitignored)
deploy/Caddyfile        LAN HTTPS without Cloudflare
```

## Differences vs the demo

- Passwords hashed (PBKDF2) — demo used plain text
- Admin screens: add/deactivate machines and users, reset passwords
- Data lives in `data/` (clean git repo, easy backup = copy one folder)
- Indexed DB columns; API docs at `/api/docs`
- Config via environment variables (PORT, COMPANY_TH, FORM_CODE)

## Path to production

1. **HTTPS on LAN**: `caddy run --config deploy\Caddyfile` (see file for phone cert install) → web push works on phones
2. **PostgreSQL**: swap `server/db.py` engine when moving to the plant server
3. **Backups**: copy `data/` nightly (one folder = everything)
4. **setup.exe**: PyInstaller when distribution is needed
5. **Android APK**: Capacitor wrap of `static/`

## Reset

Delete the `data/` folder → fresh seed on next start.

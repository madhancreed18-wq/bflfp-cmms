# PostgreSQL setup (Windows) — BFLFP CMMS v2

## 1. Install PostgreSQL (~10 min)

1. Download the Windows installer: https://www.postgresql.org/download/windows/
   (EDB installer, latest 16.x or 17.x)
2. Run it. Keep defaults, EXCEPT:
   - **Password for user `postgres`**: choose one and WRITE IT DOWN
     (use letters+numbers only — special characters complicate the URL)
   - Port: **5432** (default)
3. Skip "Stack Builder" at the end — not needed.

## 2. Create the databases (~2 min)

Open **SQL Shell (psql)** from the Start menu (Enter through the prompts,
type your password), then:

```sql
CREATE DATABASE cmms;        -- production
CREATE DATABASE cmms_test;   -- scratch DB for the smoke test ONLY
\q
```

## 3. Python driver (once)

```
pip install psycopg2-binary
```

## 4. Start the CMMS on PostgreSQL

Use **Start CMMS (PostgreSQL).bat** — edit it once to put your password in.
First start auto-creates all tables and seed data in `cmms`.
The app footer should show **v2.0.0** and /api/health should say ok.

To go back to SQLite at any time: just run the normal `Start CMMS.bat`
(no DATABASE_URL = SQLite file). Both can coexist; they are separate data.

## 5. Verify with the smoke suite (against the SCRATCH db)

```
set SMOKE_DATABASE_URL=postgresql+psycopg2://postgres:YOURPASSWORD@localhost:5432/cmms_test
python tests\smoke.py
```

Expect **42/42 GREEN**. If anything is red, send me the output — those are
exactly the portability bugs the suite exists to catch.
(The smoke test refuses to touch your real DATABASE_URL by design — it only
uses SMOKE_DATABASE_URL, which should always point at cmms_test.)

## 6. Backups (set up the same day — one command)

Manual backup:
```
"C:\Program Files\PostgreSQL\16\bin\pg_dump" -U postgres -F c -f D:\backup\cmms_%date:~-4%%date:~3,2%%date:~0,2%.backup cmms
```
Automate: Windows Task Scheduler → daily 22:00 → run the command above
(create D:\backup first). Restore = `pg_restore -U postgres -d cmms file.backup`.

## 7. Migrating existing SQLite pilot data (when needed)

Don't hand-copy. Tell me when you're ready — I'll write
`tools/migrate_sqlite_to_pg.py` that reads data/cmms.db and inserts into
PostgreSQL table-by-table in dependency order. (Not needed for a fresh start.)

## Troubleshooting

- `password authentication failed` → wrong password in the URL
- `could not connect` → PostgreSQL service not running: services.msc →
  postgresql-x64-16 → Start
- Password with @ : # in it → URL-encode it, or simpler: change to a plain one
- Port busy → another PG instance; check the port chosen during install

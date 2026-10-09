"""One job table per plant — jobs_bfl, jobs_fp, jobs_pc.

WHY
    All three plants used to write into ONE `jobs` table with ONE job-number counter, so
    BFL, BFLFP and BFLPC took turns drawing from the same BKD / PRD / PRM run: FP got
    BKD-2609-019, BFL got BKD-2609-020, PC got PRD-2609-043. Every plant's register had
    holes in it that were somebody else's work. Each plant now keeps its own table, and
    a job number is counted inside that plant's table only.

HOW THE REST OF THE APP STILL WORKS
    `jobs` is now a VIEW over the three tables (UNION ALL), with INSTEAD OF triggers that
    send every UPDATE / DELETE to the table the row actually lives in. So the hundreds of
    existing `SELECT ... FROM jobs` / `UPDATE jobs ...` queries keep working unchanged and
    every screen still filters by factory_id exactly as before.

    INSERTs are routed in the connection layer (database.Conn → route_insert below): the
    new row is written straight into the right plant's table, so column defaults apply
    and the new id comes back to the caller. The INSERT trigger is only a safety net for
    anything that bypasses Conn (a maintenance script using sqlite3 directly).

ID RULE
    `id` stays unique across ALL plants — job_events, timelogs, signoffs, photos and chat
    hang off job id, and two plants owning job 180 would attach one plant's photos to the
    other plant's job. Ids come from the `job_seq` AUTOINCREMENT counter, never reused.
    Only the JOB NUMBER (jobid) is per plant; UNIQUE(jobid) is per table.

MIGRATION
    split_jobs() runs at every start. The first time, it copies the database file
    (cmms.db.before-jobs-split-<time>), moves each job into its plant's table untouched —
    same id, same number, nothing renumbered — checks the counts, and keeps the old table
    as `jobs_before_split` for checking. After that it only keeps the view and triggers in
    step with the tables (a new column, a new plant).

SQLite only. On PostgreSQL nothing here runs and the single table stays.
"""
import logging
import os
import re
import sqlite3
from datetime import datetime

from .database import engine

_log = logging.getLogger("cmms")

SEQ = "job_seq"
OLD = "jobs_before_split"
_MAP = {}          # factory_id -> table name, filled by refresh_map()


def table_for_code(code):
    return "jobs_" + re.sub(r"[^a-z0-9]", "", str(code or "").lower())


def _is_sqlite():
    return engine.dialect.name == "sqlite"


def _raw():
    con = sqlite3.connect(engine.url.database, isolation_level=None, timeout=60)
    con.row_factory = sqlite3.Row
    return con


# ───────────────────────────────────────────────────────── queries from the app ──
def is_split(c):
    if not _is_sqlite():
        return False
    r = c.execute("SELECT type FROM sqlite_master WHERE name='jobs'").fetchone()
    return bool(r and r[0] == "view")


def refresh_map(c):
    _MAP.clear()
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for r in c.execute("SELECT id, code FROM factories").fetchall():
        t = table_for_code(r[1])
        if t in names:
            _MAP[int(r[0])] = t
    return dict(_MAP)


def plant_table(c, factory_id):
    """The physical job table of this plant, or None (not split / unknown plant)."""
    try:
        fid = int(factory_id or 0)
    except (TypeError, ValueError):
        return None
    if not fid or not is_split(c):
        return None
    if fid not in _MAP:
        refresh_map(c)
    return _MAP.get(fid)


def machine_factory(c, machine_id):
    if not machine_id:
        return None
    r = c.execute("SELECT factory_id FROM machines WHERE id=?", (machine_id,)).fetchone()
    return r[0] if r and r[0] else None


_INS = re.compile(r"^\s*INSERT\s+INTO\s+jobs\s*\(([^)]*)\)\s*VALUES\s*\((.*)\)\s*$", re.I | re.S)


def route_insert(c, raw_exec, sql, params):
    """Write an `INSERT INTO jobs(...)` into the plant's own table.

    Returns (result, new_id), or None to let the statement run as written (not split,
    not a plain column/VALUES insert, or an explicit id — the trigger handles those).
    `raw_exec(sql, params)` runs SQL on the same connection without coming back here.
    """
    m = _INS.match(sql or "")
    if not m or not _is_sqlite() or not is_split(c):
        return None
    cols = [x.strip() for x in m.group(1).split(",") if x.strip()]
    vals = [x.strip() for x in m.group(2).split(",")]
    params = list(params or ())
    if "id" in cols or len(vals) != len(cols) or any(v != "?" for v in vals) \
            or len(params) != len(cols):
        return None
    row = dict(zip(cols, params))
    fac = row.get("factory_id") or machine_factory(c, row.get("machine_id"))
    if not fac and row.get("created_by"):
        u = c.execute("SELECT factory_id FROM users WHERE id=?", (row["created_by"],)).fetchone()
        fac = u[0] if u and u[0] else None
    table = plant_table(c, fac)
    if not table:
        raise RuntimeError(f"jobs: no job table for plant {fac!r} — job not saved")
    new_id = raw_exec(f"INSERT INTO {SEQ} DEFAULT VALUES", []).lastrowid
    raw_exec(f"DELETE FROM {SEQ}", [])
    row["factory_id"] = int(fac)
    row["id"] = new_id
    names = list(row.keys())
    res = raw_exec(f"INSERT INTO {table}({','.join(names)}) VALUES({','.join('?' * len(names))})",
                   [row[k] for k in names])
    return res, new_id


# ───────────────────────────────────────────────────────────── startup migration ──
def split_jobs():
    if not _is_sqlite():
        return
    con = _raw()
    try:
        typ = con.execute("SELECT type FROM sqlite_master WHERE name='jobs'").fetchone()
        facs = [(int(r["id"]), r["code"]) for r in con.execute("SELECT id, code FROM factories ORDER BY id")]
        if not typ or not facs:
            return
        if typ[0] == "table":
            if not _first_split(con, facs):
                return
        _rebuild(con, facs)
    finally:
        con.close()


def _first_split(con, facs):
    ids = [f[0] for f in facs]
    qm = ",".join("?" * len(ids))
    # a job with no plant yet takes its machine's plant, then its reporter's
    con.execute("UPDATE jobs SET factory_id=(SELECT m.factory_id FROM machines m WHERE m.id=jobs.machine_id)"
                " WHERE factory_id IS NULL AND machine_id IS NOT NULL")
    lost = con.execute(f"SELECT id, jobid FROM jobs WHERE factory_id IS NULL OR factory_id NOT IN ({qm})",
                       ids).fetchall()
    if lost:
        _log.error("[jobs-split] NOT split: %d job(s) have no plant, e.g. %s — give them a factory_id first",
                   len(lost), ", ".join(str(r["jobid"] or r["id"]) for r in lost[:10]))
        return False

    path = engine.url.database
    backup = f"{path}.before-jobs-split-{datetime.now():%Y%m%d-%H%M%S}"
    try:
        con.execute("VACUUM INTO ?", (backup,))
        _log.info("[jobs-split] database copied to %s", os.path.basename(backup))
    except Exception as e:
        _log.error("[jobs-split] NOT split: could not back up the database first (%s)", e)
        return False

    ddl = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()[0]
    head = re.compile(r'^\s*CREATE\s+TABLE\s+["`\[]?jobs["`\]]?\s*\(', re.I)
    if not head.match(ddl):
        _log.error("[jobs-split] NOT split: unexpected jobs DDL")
        return False
    total = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    try:
        con.execute("BEGIN IMMEDIATE")
        moved = 0
        for fid, code in facs:
            t = table_for_code(code)
            con.execute(head.sub(f"CREATE TABLE {t} (", ddl, count=1))
            moved += con.execute(f"INSERT INTO {t} SELECT * FROM jobs WHERE factory_id=?", (fid,)).rowcount
        if moved != total:
            raise RuntimeError(f"moved {moved} of {total} jobs")
        con.execute(f"CREATE TABLE IF NOT EXISTS {SEQ}(id INTEGER PRIMARY KEY AUTOINCREMENT)")
        top = con.execute("SELECT COALESCE(MAX(id),0) FROM jobs").fetchone()[0]
        if top:
            con.execute(f"INSERT INTO {SEQ}(id) VALUES(?)", (top,))
            con.execute(f"DELETE FROM {SEQ}")
        con.execute(f"ALTER TABLE jobs RENAME TO {OLD}")
        con.execute("COMMIT")
    except Exception as e:
        con.execute("ROLLBACK")
        _log.error("[jobs-split] NOT split, nothing changed: %s", e)
        return False
    per = ", ".join(f"{table_for_code(code)}={con.execute(f'SELECT COUNT(*) FROM {table_for_code(code)}').fetchone()[0]}"
                    for _, code in facs)
    _log.info("[jobs-split] %d job(s) moved into their plant tables (%s); old table kept as %s",
              total, per, OLD)
    return True


def _rebuild(con, facs):
    """Plant tables complete and identical in columns; view + triggers current."""
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    tables = [(fid, table_for_code(code)) for fid, code in facs]
    have = [t for _, t in tables if t in names]
    if not have:
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        # a plant added later gets its table, shaped like an existing one
        tmpl = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (have[0],)).fetchone()[0]
        head = re.compile(r'^\s*CREATE\s+TABLE\s+["`\[]?' + re.escape(have[0]) + r'["`\]]?\s*\(', re.I)
        for _, t in tables:
            if t not in names:
                con.execute(head.sub(f"CREATE TABLE {t} (", tmpl, count=1))
                _log.info("[jobs-split] new plant job table %s", t)
        # every table carries every column (a column added to one is added to all)
        info = {t: [dict(r) for r in con.execute(f"PRAGMA table_info({t})")] for _, t in tables}
        order, spec = [], {}
        for _, t in tables:
            for col in info[t]:
                if col["name"] not in spec:
                    order.append(col["name"])
                    spec[col["name"]] = col
        for _, t in tables:
            mine = {col["name"] for col in info[t]}
            for n in order:
                if n not in mine:
                    s = spec[n]
                    dd = f" DEFAULT {s['dflt_value']}" if s["dflt_value"] is not None else ""
                    con.execute(f"ALTER TABLE {t} ADD COLUMN {n} {s['type'] or ''}{dd}")
        con.execute(f"CREATE TABLE IF NOT EXISTS {SEQ}(id INTEGER PRIMARY KEY AUTOINCREMENT)")

        for obj in ("TRIGGER jobs_ins", "TRIGGER jobs_upd", "TRIGGER jobs_del", "VIEW jobs"):
            con.execute(f"DROP {obj.split()[0]} IF EXISTS {obj.split()[1]}")
        cl = ",".join(order)
        con.execute("CREATE VIEW jobs AS " +
                    " UNION ALL ".join(f"SELECT {cl} FROM {t}" for _, t in tables))

        ids = ",".join(str(fid) for fid, _ in tables)
        route = ("COALESCE(NEW.factory_id,(SELECT factory_id FROM machines WHERE id=NEW.machine_id),"
                 "(SELECT NULLIF(factory_id,0) FROM users WHERE id=NEW.created_by))")
        body = [f"SELECT RAISE(ABORT,'jobs: job has no plant table (factory_id)') WHERE COALESCE({route},-1) NOT IN ({ids});",
                f"INSERT INTO {SEQ}(id) SELECT NULL WHERE NEW.id IS NULL;",
                f"INSERT INTO {SEQ}(id) SELECT NEW.id WHERE NEW.id > COALESCE((SELECT seq FROM sqlite_sequence WHERE name='{SEQ}'),0);"]
        vals = []
        for n in order:
            if n == "id":
                vals.append(f"COALESCE(NEW.id,(SELECT MAX(id) FROM {SEQ}))")
            elif n == "factory_id":
                vals.append(route)
            elif spec[n]["dflt_value"] is not None:
                vals.append(f"COALESCE(NEW.{n},{spec[n]['dflt_value']})")
            else:
                vals.append(f"NEW.{n}")
        for fid, t in tables:
            body.append(f"INSERT INTO {t}({cl}) SELECT {','.join(vals)} WHERE {route}={fid};")
        body.append(f"DELETE FROM {SEQ};")
        con.execute("CREATE TRIGGER jobs_ins INSTEAD OF INSERT ON jobs BEGIN\n" + "\n".join(body) + "\nEND")

        sets = ",".join(f"{n}=NEW.{n}" for n in order if n != "id")
        body = [f"UPDATE {t} SET {sets} WHERE id=OLD.id;" for _, t in tables]
        # factory_id changed to another plant: the row moves to that plant's table
        for sfid, st in tables:
            for dfid, dt in tables:
                if sfid != dfid:
                    body.append(f"INSERT INTO {dt}({cl}) SELECT {cl} FROM {st}"
                                f" WHERE id=OLD.id AND NEW.factory_id={dfid};")
        for fid, t in tables:
            body.append(f"DELETE FROM {t} WHERE id=OLD.id AND NEW.factory_id IN ({ids})"
                        f" AND NEW.factory_id<>{fid};")
        con.execute("CREATE TRIGGER jobs_upd INSTEAD OF UPDATE ON jobs BEGIN\n" + "\n".join(body) + "\nEND")

        body = [f"DELETE FROM {t} WHERE id=OLD.id;" for _, t in tables]
        con.execute("CREATE TRIGGER jobs_del INSTEAD OF DELETE ON jobs BEGIN\n" + "\n".join(body) + "\nEND")

        # the id counter never falls behind a row written with an explicit id
        top = con.execute("SELECT COALESCE(MAX(id),0) FROM jobs").fetchone()[0]
        seq = con.execute(f"SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name='{SEQ}'),0)").fetchone()[0]
        if top > seq:
            con.execute(f"INSERT INTO {SEQ}(id) VALUES(?)", (top,))
            con.execute(f"DELETE FROM {SEQ}")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def add_job_column(c, col, ddl):
    """ALTER for a jobs column once jobs is a view: add to every plant table, then rebuild."""
    for t in {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'jobs\\_%' ESCAPE '\\'").fetchall()}:
        if t in (OLD, SEQ):
            continue
        if col not in {r[1] for r in c.execute(f"PRAGMA table_info({t})").fetchall()}:
            c.execute(f"ALTER TABLE {t} ADD COLUMN {col} {ddl}")
    c.commit()
    split_jobs()

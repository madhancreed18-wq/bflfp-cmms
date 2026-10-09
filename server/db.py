"""Database access layer (v2 — SQLAlchemy engine underneath).

Modules keep calling db(), execute with `?` placeholders, and commit —
identical code runs on SQLite (dev/pilot) and PostgreSQL (production).
"""
import hashlib, secrets, time
from datetime import datetime, date

from .database import get_conn, create_schema, engine

import logging
# the app's own logger — the file handler is attached by logs.setup(), and
# everything written here shows in Manage → Logs. print() does not: it goes to
# a console nobody is watching, which is where these messages used to die.
_log = logging.getLogger("cmms")


def db():
    return get_conn()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today():
    return date.today().isoformat()


def day_range(d):
    """Portable 'created during day d' boundaries for TEXT timestamps."""
    return d + " 00:00:00", d + " 23:59:59"


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()


def check_pw(pw, stored):
    if "$" not in (stored or ""):
        return pw == stored
    salt = stored.split("$", 1)[0]
    return hash_pw(pw, salt) == stored


# Job numbers: PRD for corrective work, BKD for breakdowns, PRM for preventive.
# An improvement used to be numbered PRD- like a repair, which made the two
# indistinguishable on a printed form and in every export: a plant cannot tell how much
# of its year went into making things better if that work is filed as breakdown repair.
# It has its own run now, and so does a project. Numbers already issued are left alone —
# renumbering a job that has been signed, printed and filed would rewrite history.
JOB_PREFIX = {"CM": "PRD", "BD": "BKD", "PM": "PRM", "IMP": "IMP", "PRJ": "PRJ", "PRD": "PRD"}


def next_jobid(c, jobtype, dept="", factory_id=None, machine_id=None):
    """The next free job number for this type, e.g. PRD-2608-001.

    Counted from the highest number already issued this month for that prefix, not
    from how many rows exist — deleting a job must never let the next one reuse its
    number. Each prefix has its own run and restarts at 001 every month.

    From DEPT_JOBID_FROM, a CM job is numbered with the code of the DEPARTMENT that
    reported it — WRH-2610-001, QLY-2610-001 — because not every corrective job comes
    from production, and PRD on all of them said it did. Only CM: a breakdown is a
    breakdown whoever reports it (BKD), and PM comes out of the programme (PRM).

    The switch is by DATE, so nothing has to be remembered on the day and nothing
    already issued is touched. Before the date, and whenever the department is unknown,
    the prefix is exactly what it always was.

    PER PLANT. Each plant keeps its own job table (jobtables.py), so the number is
    counted inside that plant's table only: BFL, BFLFP and BFLPC each run their own
    BKD-2609-001, 002, … Pass factory_id (or machine_id, and its plant is used). With no
    plant known the highest number in ANY plant is used, which can never collide.
    """
    from .jobtables import plant_table, machine_factory
    if not factory_id and machine_id:
        factory_id = machine_factory(c, machine_id)
    src = plant_table(c, factory_id) or "jobs"
    prefix = JOB_PREFIX.get(str(jobtype or "CM").upper(), "PRD")
    if (str(jobtype or "").upper() == "CM" and dept
            and datetime.now().strftime("%Y-%m-%d") >= DEPT_JOBID_FROM):
        code = dept_code(dept)
        if code:
            prefix = code
    ym = datetime.now().strftime("%y%m")
    top = 0
    for row in c.execute(f"SELECT jobid FROM {src} WHERE jobid LIKE ?", (f"{prefix}-{ym}-%",)):
        tail = str(row[0]).rsplit("-", 1)[-1]
        if tail.isdigit():
            top = max(top, int(tail))
    return f"{prefix}-{ym}-{top + 1:03d}"


def job_row(c, jid):
    r = c.execute("""SELECT j.*, m.code mcode, m.name mname, u.name lead_name
        FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
        LEFT JOIN users u ON u.id=j.lead_tech WHERE j.id=?""", (jid,)).fetchone()
    if not r:
        from fastapi import HTTPException
        raise HTTPException(404, f"job {jid} not found")
    return dict(r)


def role_ids(c, *roles, factory_id=None):
    """Everyone holding one of these roles — in ONE factory when factory_id is given.

    A person works at one plant. A breakdown at FP is nothing to a technician at PC,
    and a phone that buzzes for other people's plants gets silenced, taking the alerts
    that mattered with it. Every notification passes the factory the sender is signed
    in to; the unscoped form is left only for genuinely plant-wide lookups.
    """
    q = f"SELECT id FROM users WHERE active=1 AND role IN ({','.join('?' * len(roles))})"
    args = list(roles)
    if factory_id:
        q += " AND factory_id=?"
        args.append(factory_id)
    return [r["id"] for r in c.execute(q, args)]


def user_names(c):
    return {str(r["id"]): r["name"] for r in c.execute("SELECT id,name FROM users")}


def log_status(c, jid, status, uid=None):
    """Stamp a job status change into job_events — the source for cycle-time / hold timing.
    Skips if the job's latest event is already this status, so it is safe to call from the
    several code paths that can produce the same transition."""
    if not status:
        return
    last = c.execute("SELECT status FROM job_events WHERE job_id=? ORDER BY id DESC LIMIT 1",
                     (jid,)).fetchone()
    if last and last["status"] == status:
        return
    c.execute("INSERT INTO job_events(job_id,status,user_id,created_at) VALUES(?,?,?,?)",
              (jid, status, uid, now()))


def _parse_ts(s):
    try:
        return datetime.strptime((s or "")[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def mins_between(a, b):
    ta, tb = _parse_ts(a), _parse_ts(b)
    return round((tb - ta).total_seconds() / 60) if (ta and tb) else None


def hold_minutes(events, now_ts=None):
    """Total minutes a job spent on Hold across all episodes.
    events = [(status, at), ...] in chronological order; an open hold runs to now_ts."""
    total = 0.0
    for i, (st, ts) in enumerate(events):
        if st == "Hold":
            end = events[i + 1][1] if i + 1 < len(events) else now_ts
            t0, t1 = _parse_ts(ts), _parse_ts(end)
            if t0 and t1:
                total += max(0.0, (t1 - t0).total_seconds()) / 60
    return round(total)


def _table_cols(c, table):
    """Existing column names for a table (SQLite + PostgreSQL portable)."""
    if engine.dialect.name == "sqlite":
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    return {r[0] for r in c.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name=?",
        (table,)).fetchall()}


def _machine_code_per_factory(c):
    """Make an asset code unique WITHIN a plant instead of across the whole company.

    The register was created with a global UNIQUE(code), which is wrong for a
    three-plant app: BFL and BFLFP both have a W01FP01 and mean different machines —
    a fire pump and a feed pump. Under the old constraint the second plant could not
    have its own asset at all, and the honest workaround was to tell a plant to rename
    a real machine to suit a database.

    SQLite cannot drop a table-level UNIQUE, so the table is rebuilt: the constraint is
    the only thing that changes, and the DDL is taken from the live table rather than
    written out here, so every column an earlier migration added comes across too.

    Runs once, is skipped the moment the new constraint exists, and refuses to run at
    all if the data would not survive it — a duplicate code INSIDE one plant would be a
    different fault, and losing rows to a rebuild is not a way to discover it.
    """
    if engine.dialect.name != "sqlite":
        return                      # PostgreSQL: the model's UniqueConstraint is authoritative
    try:
        ddl = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='machines'"
                        ).fetchone()
        ddl = ddl[0] if ddl else ""
        if not ddl or "UNIQUE (code)" not in ddl:
            return                                          # already per-factory, or not ours
        dup = c.execute("SELECT factory_id, code, COUNT(*) n FROM machines"
                        " GROUP BY factory_id, code HAVING n > 1").fetchall()
        if dup:
            _log.warning("[db] machine-code migration SKIPPED — %d code(s) repeat inside one plant: %s"
                         % (len(dup), ", ".join(str(r[1]) for r in dup[:5])))
            return
        cols = [r[1] for r in c.execute("PRAGMA table_info(machines)").fetchall()]
        newddl = (ddl.replace("CREATE TABLE machines", "CREATE TABLE machines_mig", 1)
                     .replace("UNIQUE (code)", 'UNIQUE (factory_id, code)'))
        n_before = c.execute("SELECT COUNT(*) FROM machines").fetchone()[0]
        cl = ",".join('"%s"' % x for x in cols)
        c.execute(newddl)
        c.execute(f"INSERT INTO machines_mig ({cl}) SELECT {cl} FROM machines")
        n_after = c.execute("SELECT COUNT(*) FROM machines_mig").fetchone()[0]
        if n_after != n_before:                              # never trade rows for a constraint
            c.execute("DROP TABLE machines_mig")
            _log.error("[db] machine-code migration ABORTED — %d rows in, %d out" % (n_before, n_after))
            return
        c.execute("DROP TABLE machines")
        c.execute("ALTER TABLE machines_mig RENAME TO machines")
        c.commit()
        _log.info("[db] machines.code is now unique per factory (%d rows kept)" % n_after)
    except Exception as e:
        _log.warning("[db] machine-code migration skipped: %s", e)


def _fill_job_factory(c):
    """Give every job a plant. Runs at every startup, touching only rows still NULL.

    Three passes, in order of how much they know. The machine is the truth where there
    is one — a machine cannot move plant. Where there is none, the person who raised the
    job is the next best answer, then the person it was raised for. An all-plants
    account (factory_id 0) tells us nothing, so it is skipped rather than writing 0 and
    making the row belong to a plant that does not exist.

    It runs every time rather than once behind a flag so that a job created by some path
    nobody thought of lands in the right plant at the next restart, instead of staying
    visible in all three for good. Only NULL rows are touched, so it costs nothing once
    the work is done, and it can never move a job that already has a plant.
    """
    try:
        n0 = c.execute("SELECT COUNT(*) FROM jobs WHERE factory_id IS NULL").fetchone()[0]
        if not n0:
            return
        c.execute("UPDATE jobs SET factory_id=(SELECT m.factory_id FROM machines m"
                  " WHERE m.id=jobs.machine_id)"
                  " WHERE factory_id IS NULL AND machine_id IS NOT NULL")
        for col in ("created_by", "requester_id"):
            c.execute(f"UPDATE jobs SET factory_id=(SELECT u.factory_id FROM users u"
                      f" WHERE u.id=jobs.{col})"
                      f" WHERE factory_id IS NULL"
                      f" AND (SELECT COALESCE(u.factory_id,0) FROM users u WHERE u.id=jobs.{col}) > 0")
        c.commit()
        left = c.execute("SELECT COUNT(*) FROM jobs WHERE factory_id IS NULL").fetchone()[0]
        _log.info("[db] plant stamped on %d job(s)%s" % (
            n0 - left, "" if not left else "; %d still unknown — shown in every plant" % left))
    except Exception as e:
        _log.warning("[db] job plant backfill skipped: %s", e)


def _fill_job_owners(c):
    """Fill owner1 / owner2 on jobs raised before the columns existed.

    The FIRST time log is the only surviving evidence of who actually started a job —
    `lead_tech` has since been overwritten by every move and every reassignment. Where
    there is no time log the lead technician is the best available answer, and where the
    lead has since changed, that person becomes owner2.

    Runs at every startup over rows where owner1 is still NULL, so it costs nothing once
    the work is done and it heals a row created by a path that forgot to stamp it. Names
    come from `users` where the account still exists; where it does not, the id is kept
    and the name left blank, which is honest — the name was never recorded and cannot be
    invented now.
    """
    try:
        n0 = c.execute("SELECT COUNT(*) FROM jobs WHERE owner1 IS NULL").fetchone()[0]
        if not n0:
            return
        c.execute("""UPDATE jobs SET owner1=COALESCE(
                       (SELECT t.tech FROM timelogs t WHERE t.job_id=jobs.id
                         AND t.tech IS NOT NULL ORDER BY t.id LIMIT 1),
                       lead_tech)
                     WHERE owner1 IS NULL""")
        c.execute("""UPDATE jobs SET owner2=lead_tech
                     WHERE owner1 IS NOT NULL AND lead_tech IS NOT NULL
                       AND lead_tech<>owner1 AND owner2 IS NULL""")
        for col in ("owner1", "owner2"):
            c.execute(f"""UPDATE jobs SET {col}_name=COALESCE(
                            (SELECT u.name FROM users u WHERE u.id=jobs.{col}), '')
                          WHERE {col} IS NOT NULL AND COALESCE({col}_name,'')=''""")
        c.commit()
        left = c.execute("SELECT COUNT(*) FROM jobs WHERE owner1 IS NULL").fetchone()[0]
        _log.info("[db] owner stamped on %d job(s)%s" % (
            n0 - left, "" if not left else "; %d had nobody on them to stamp" % left))
    except Exception as e:
        _log.warning("[db] job owner backfill skipped: %s", e)


def _migrate(c):
    """Add columns to existing DBs (create_all only makes missing tables)."""
    adds = [("jobs", "report_name", "VARCHAR(120) DEFAULT ''"),
            ("jobs", "requester_id", "INTEGER"),
            ("jobs", "img_before2", "VARCHAR(200) DEFAULT ''"),
            ("jobs", "img_after2", "VARCHAR(200) DEFAULT ''"),
            ("jobs", "planned_at", "VARCHAR(30)"),
            ("jobs", "started_at", "VARCHAR(30)"),
            ("jobs", "approved_at", "VARCHAR(30)"),
            ("jobs", "approver_id", "INTEGER"),
            ("jobs", "sign_tech", "VARCHAR(200) DEFAULT ''"),
            ("jobs", "inspected_at", "VARCHAR(30)"),
            ("jobs", "sign_appr", "VARCHAR(200) DEFAULT ''"),
            ("factories", "hours_json", "VARCHAR(500) DEFAULT ''"),
            # people fields: a technician may exist purely to be assignable (can_login=0)
            ("users", "department", "VARCHAR(60) DEFAULT ''"),
            ("users", "photo", "VARCHAR(200) DEFAULT ''"),
            ("users", "can_login", "INTEGER DEFAULT 1"),
            ("users", "login_id", "INTEGER"),
            # a technician who also reports problems (the operator side) on the same login
            ("users", "can_report", "INTEGER DEFAULT 0"),
            # b392: the team a person works for — "" = plant maintenance, "CE" = Central Engineering (electrical)
            ("users", "team", "TEXT DEFAULT ''"),
            # the two-stage status the shop floor reads: stage 1 is the phase the job is
            # in, stage 2 is whether it has a crew / where it stands with the approver
            ("jobs", "stage1", "VARCHAR(20) DEFAULT ''"),
            ("jobs", "stage2", "VARCHAR(24) DEFAULT ''"),
            # what the operator says is wrong, chosen from a list the admin keeps. It is
            # the only clue to electrical-vs-mechanical before a technician looks at the
            # machine, so it seeds fault_category — which the technician still overrides
            # at Finish, because they are the one who actually saw it.
            ("jobs", "problem_type", "VARCHAR(80) DEFAULT ''"),
            # the day this job was first planned for, kept when the
            # carry-forward pass moves it, so a job that has slipped four
            # times can still say what it originally promised
            ("jobs", "planned_date_orig", "VARCHAR(12) DEFAULT ''"),
            # A breakdown whose recorded clock is not the machine's real stop — a job
            # left open for days, a duplicate report, a test row — would otherwise
            # decide the plant's downtime split on its own. The row stays exactly as it
            # is and still reports its own minutes; only the downtime KPI query skips
            # it. Admin only, and nothing about it appears on the job, in the job's
            # history, or on the dashboard.
            ("jobs", "kpi_exclude", "INTEGER DEFAULT 0"),
            ("jobs", "kpi_exclude_note", "VARCHAR(200) DEFAULT ''"),
            ("jobs", "kpi_exclude_by", "INTEGER"),
            ("jobs", "kpi_exclude_at", "VARCHAR(30)"),
            # Which frequency of the machine's PM sheet a PM job is for. A weekly job
            # asks for the weekly items and nothing else, so the technician's checklist
            # is the work actually due rather than the whole year's grid. It was
            # readable only by parsing the label out of the description's first line,
            # which stopped being true the moment anybody edited the description.
            ("jobs", "pm_freq", "VARCHAR(12) DEFAULT ''"),
            # What this person can actually give in a week, after walking between towers,
            # waiting for a permit and standing in the morning meeting. NOT the payroll
            # number — it is the line the crew-load page judges every week against, and a
            # payroll figure makes that page lie in the reassuring direction.
            ("users", "week_hours", "INTEGER DEFAULT 45"),
            # A project task points back at the work order raised from it, and the job
            # points back at the task, so a technician pressing Start on the shop floor
            # fills in the project's actual dates without anybody re-typing them.
            ("jobs", "ptask_id", "INTEGER"),
            # the work-plan sheet's own columns
            ("project_tasks", "wbs", "VARCHAR(12) DEFAULT ''"),
            ("project_tasks", "phase", "VARCHAR(60) DEFAULT ''"),
            ("project_tasks", "remark", "VARCHAR(200) DEFAULT ''"),
            # The plant this job belongs to. It used to be inferred from the job's
            # MACHINE and nowhere else, so a job raised without a machine belonged to
            # no plant — and every list read "no machine" as "every plant", which is how
            # a BFL job turned up in BFLFP's list. Written once when the job is raised:
            # the machine's plant where there is a machine, otherwise the plant the
            # person was signed in to. NULL still means "show everywhere", so a row the
            # backfill has not reached is over-shared, never hidden.
            ("jobs", "factory_id", "INTEGER"),
            # Who first took this job, and who it was later moved to. `lead_tech` is the
            # CURRENT technician and nothing else: a Move job, or deleting somebody and
            # reassigning their work, overwrites it and the person who actually did the
            # work is gone from the row. The NAME is stored beside the id on purpose —
            # an id alone points at nothing once the account is deleted, which is exactly
            # the case these columns exist for.
            ("jobs", "owner1", "INTEGER"),
            ("jobs", "owner1_name", "VARCHAR(120) DEFAULT ''"),
            ("jobs", "owner2", "INTEGER"),
            ("jobs", "owner2_name", "VARCHAR(120) DEFAULT ''"),
            # What the reporter typed when the thing they are reporting is not in the
            # asset register — pipework, a new install, a socket, a machine nobody has
            # coded yet. The report form used to refuse that save outright, so this work
            # was either raised by a planner on their own form (which never had the
            # check) or not raised at all. The text is kept HERE rather than pushed into
            # the description, because the description is the fault and this is the
            # thing: the planner's queue, the job sheet and the PDF all show it in the
            # asset column, marked as unregistered. A blank asset column reads as
            # missing data; this reads as an answer. machine_id stays NULL on these
            # rows, and that is what keeps them out of the per-asset KPIs.
            ("jobs", "asset_text", "VARCHAR(80) DEFAULT ''"),
            # WHO REPORTED IT, as a department. Kept on the job rather than read back
            # off the person: people move department, and a job's history must not
            # move with them. It is also what the job number is built from.
            ("jobs", "req_dept", "VARCHAR(8) DEFAULT ''"),
            # "I have seen it" — the DAY a planner acknowledged that this job is past its
            # date. It stops the row flashing, and it stops it only for that day: the
            # date is compared against today, so an acknowledgement that is not followed
            # by the work being finished expires overnight and the row is back in the
            # morning. Deliberately not a boolean — a boolean would let one click silence
            # a job for ever, which is how a warning turns into wallpaper.
            ("jobs", "due_ack", "VARCHAR(10) DEFAULT ''"),
            ("jobs", "due_ack_by", "INTEGER"),
            # b406: a signature registered once and used at every sign point, and the
            # department (shared) logins that must never hold one person's signature
            ("users", "sig_path", "VARCHAR(200) DEFAULT ''"),
            ("users", "sig_at", "VARCHAR(30) DEFAULT ''"),
            ("users", "shared_login", "INTEGER DEFAULT 0"),
            # b430: which board gave a corrective job its crew — 'ce' or '' (the plant's)
            ("jobs", "board", "VARCHAR(8) DEFAULT ''")]
    from .jobtables import is_split, add_job_column
    for table, col, ddl in adds:
        if col not in _table_cols(c, table):
            if table == "jobs" and is_split(c):
                add_job_column(c, col, ddl)      # jobs is a view over the plant tables
            else:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    _fill_job_factory(c)
    _fill_job_owners(c)
    _machine_code_per_factory(c)
    # Repair the person→account links once, on the first start after this ships. Guarded
    # by a flag and not repeated: from here the crew save keeps them right, and an admin
    # who deliberately points somebody somewhere else must not have it undone on every
    # restart. Only the plants that named their accounts after their people are touched.
    if state_get(c, "users:own-account-link") != "done":
        try:
            n = link_own_accounts(c)
            state_set(c, "users:own-account-link", "done")
            if n:
                _log.info("[db] %d technician(s) linked to their own login account" % n)
        except Exception as e:
            _log.warning("[db] own-account link skipped: %s", e)
    # b406: the logins named after an area or department, shared by a whole shift.
    # Ticked once; from then on the tick is admin's (Manage → Users).
    if state_get(c, "users:shared-logins") != "done":
        try:
            from .signature import seed_shared_logins
            n = seed_shared_logins(c)
            state_set(c, "users:shared-logins", "done")
            if n:
                _log.info("[db] %d department (shared) login(s) marked" % n)
        except Exception as e:
            _log.warning("[db] shared-login seed skipped: %s", e)
    # b413: the Central Electrical programme counts from the day this build first ran;
    # electrical PM already overdue before then is shown but not held against the team
    try:
        if not state_get(c, "ce:count-from"):
            state_set(c, "ce:count-from", today())
    except Exception as e:
        _log.warning("[db] ce:count-from: %s", e)
    # The feedmill symptoms, once. Guarded by a flag rather than only by name, because
    # an entry an admin has since DELETED must stay deleted — a list that quietly grows
    # itself back every restart is worse than no list at all.
    if state_get(c, "ptypes:feedmill-mech") != "done":
        try:
            n = add_feedmill_symptoms(c)
            state_set(c, "ptypes:feedmill-mech", "done")
            if n:
                _log.info("[db] %d feedmill symptoms added to the report screen" % n)
        except Exception as e:
            _log.warning("[db] feedmill symptoms skipped: %s", e)
    # Utilities go everywhere; the canning symptoms only where there are cans.
    if state_get(c, "ptypes:utilities") != "done":
        try:
            n = add_symptoms(c, UTILITY_SYMPTOMS)
            n += add_symptoms(c, WETFOOD_SYMPTOMS, WETFOOD_PLANTS)
            state_set(c, "ptypes:utilities", "done")
            if n:
                _log.info("[db] %d utility and wet-food symptoms added" % n)
        except Exception as e:
            _log.warning("[db] utility symptoms skipped: %s", e)
    # The role called "manager" was, in practice, group oversight: every plant's
    # numbers on one dashboard and no work orders at all. That is Engineering Center,
    # and it now has its own name — leaving "manager" free for what a plant manager
    # actually is, the planner's screens in read-only. Everyone who held the old role
    # is moved once, so nobody's login changes shape without the name changing too.
    if state_get(c, "role_split:engcenter") != "done":
        c.execute("UPDATE users SET role='engcenter' WHERE role='manager'")
        state_set(c, "role_split:engcenter", "done")
    # Stage 2 for a held job now depends on whether it has a due date. Rows already on
    # Hold were stamped under the old rule and nothing will touch them again until
    # somebody opens them, so restamp them once here.
    held = [r["id"] for r in c.execute(
        "SELECT id FROM jobs WHERE status IN ('Hold','Paused')"
        " AND stage2 NOT IN ('Pending due date','Wait for action',"
        "'R.Pending due date','R.Wait for action')")]
    for _jid in held:
        set_stage(c, _jid)
    c.commit()


# ── What the operator says is wrong ─────────────────────────────────────────────
# The operator knows the symptom, not the fault: "it will not start", "there is a
# leak". Each symptom, though, points at a trade often enough to be worth recording,
# so the list is kept as data — the admin edits it — and every entry carries the
# category it usually turns out to be. That gives the planner an electrical /
# mechanical hint the moment the job is reported, hours before anyone opens the panel.
# It is a hint and nothing more: the technician sets the real category at Finish.
PROBLEM_CATS = ("Mechanical", "Electrical", "Instrument", "Process", "Other")

PROBLEM_SEED = [
    ("เสียงดังผิดปกติ / Unusual noise", "Mechanical"),
    ("สั่นผิดปกติ / Excessive vibration", "Mechanical"),
    ("รั่วซึม — น้ำ / น้ำมัน / ลม / Leak — water, oil or air", "Mechanical"),
    ("สายพาน / โซ่ หลุดหรือขาด / Belt or chain slipped or broken", "Mechanical"),
    ("ชิ้นส่วนแตกหัก / Broken or worn part", "Mechanical"),
    ("ติดขัด เดินไม่ราบรื่น / Jammed or stiff movement", "Mechanical"),
    ("เครื่องไม่ทำงาน / ไม่มีไฟ / Machine dead — no power", "Electrical"),
    ("เบรกเกอร์ตัด / Breaker or overload tripped", "Electrical"),
    ("มอเตอร์ร้อน / มีกลิ่นไหม้ / Motor hot or burning smell", "Electrical"),
    ("ปุ่มกด / สวิตช์ ไม่ทำงาน / Button or switch not working", "Electrical"),
    ("ไฟส่องสว่างเสีย / Lighting fault", "Electrical"),
    ("เซนเซอร์ไม่จับงาน / Sensor not detecting", "Instrument"),
    ("อุณหภูมิ / แรงดัน ผิดปกติ / Temperature or pressure wrong", "Instrument"),
    ("จอแสดงผล / PLC ผิดพลาด / Display or PLC error", "Instrument"),
    ("ชั่งน้ำหนักไม่แม่น / Weighing inaccurate", "Instrument"),
    ("คุณภาพงานไม่ได้ / Product quality off spec", "Process"),
    ("ตั้งค่าเครื่องไม่ได้ / Cannot set the machine up", "Process"),
    ("อื่น ๆ — อธิบายในรายละเอียด / Other — described below", "Other"),
]


def ensure_sessions(c):
    """Where a signed-in session lives between restarts.

    Sessions used to be a dictionary in the server's memory, so every restart signed
    out every phone at once — fine at a desk, not on a factory floor where a
    technician has one hand on a machine and no wish to retype a password because
    somebody deployed. The cookie is unchanged; only the record behind it now
    outlives the process.
    """
    c.execute("""CREATE TABLE IF NOT EXISTS sessions(
        token TEXT PRIMARY KEY, user_id INTEGER, factory_id INTEGER,
        created TEXT DEFAULT '', expires INTEGER DEFAULT 0)""")


def ensure_app_state(c):
    """A few rows of housekeeping state that belong to the installation, not a user.

    One key so far: the last day the carry-forward pass ran for a factory. It has to
    survive a restart — a marker in memory would re-run the pass on every deploy and
    push half the plan a day further each time.
    """
    c.execute("""CREATE TABLE IF NOT EXISTS app_state(
        k TEXT PRIMARY KEY, v TEXT DEFAULT '', updated TEXT DEFAULT '')""")


def own_account_map(c, factory_id, strict=True):
    """{person_id: their OWN login account id} for one plant, matched by name.

    A technician person and a technician login account are two rows, and nothing in the
    schema says which account belongs to which person — `login_id` was built to point a
    person at the CREW's shared handset, which is a different idea entirely. On a plant
    that issued one account per technician the two ARE the same thing, and the only
    evidence of it in the data is the name: BFL holds `techbfl7` named *jack* beside a
    person named *jack*, and BFLPC has sixteen more of the same.

    So the match is the name, and it has to be exact and unambiguous in BOTH directions
    — one person of that name, one account of that name, within one plant. Two people
    called นัท would make the link a guess, and a wrong link sends a technician's work
    to somebody else's phone, which is the fault this exists to fix.

    And it is ALL OR NOTHING per plant. This must change BFL and BFLPC only; BFLFP keeps
    its shared handsets exactly as they are. Those two plants issued an account for every
    single technician — 10 of 10, 16 of 16 — while BFLFP's accounts are not named after
    its people at all (`techfp1`, `madhan` against *Choke*, *Mark*, *boss*), so it matches
    nothing today. Requiring EVERY active technician in the plant to match is what keeps
    that true tomorrow: one BFLFP account renamed to a technician's name would otherwise
    quietly move that one person off the crew handset and leave the rest on it, which is
    the worst of both. A plant is one way or the other, never half.
    """
    ppl, acc = {}, {}
    for r in c.execute(
            "SELECT id,name,COALESCE(can_login,1) cl FROM users"
            " WHERE active=1 AND role='technician' AND (factory_id=? OR COALESCE(factory_id,0)=0)", (factory_id,)):
        k = (r["name"] or "").strip().lower()
        if not k:
            continue
        (acc if r["cl"] else ppl).setdefault(k, []).append(r["id"])
    if not ppl:
        return {}
    pairs = {ppl[k][0]: acc[k][0] for k in ppl
             if k in acc and len(ppl[k]) == 1 and len(acc[k]) == 1}
    # every technician person in the plant, or none of them. `strict=False` (the crew
    # save only) keeps the pairs that DO match: on BFLFP Choke/choke (tech1), kanya/Kanya
    # (tech2) and mark/mark (tech3) each sign in as themselves while boss has no phone,
    # and a crew save must not write the crew's handset over their own account.
    if not strict:
        return pairs
    return pairs if len(pairs) == sum(len(v) for v in ppl.values()) else {}


def link_own_accounts(c):
    """Point every technician at their own account, where their plant issued them one.

    The crews were handing out handsets by POSITION — the first team takes the first
    phone in username order — so `neng`, who leads Team C, was pointed at `techbfl2`,
    which is *phaey's* account, and `jack` at `techbfl1`, which is *neng's*. A technician
    signing in as themselves then found nothing at all: the app lists the work of the
    people pointed AT that account, and nobody was pointed at `techbfl7`. The plan
    printed correctly, the jobs were assigned, and the phones were empty — which is what
    "assigned jobs not going to the technician account" turned out to mean.

    This repairs the rows that already exist; `teams.py` keeps it true from here on.
    """
    n = 0
    for f in c.execute("SELECT id FROM factories").fetchall():
        for pid, aid in own_account_map(c, f["id"]).items():
            n += c.execute("UPDATE users SET login_id=? WHERE id=?"
                           " AND COALESCE(login_id,0)!=?", (aid, pid, aid)).rowcount
    return n


def state_get(c, key, default=""):
    ensure_app_state(c)
    r = c.execute("SELECT v FROM app_state WHERE k=?", (key,)).fetchone()
    return (r["v"] if r else default) or default


def state_set(c, key, value):
    ensure_app_state(c)
    if c.execute("UPDATE app_state SET v=?, updated=? WHERE k=?",
                 (str(value), now(), key)).rowcount == 0:
        c.execute("INSERT INTO app_state(k,v,updated) VALUES(?,?,?)",
                  (key, str(value), now()))


def ensure_plan_reports(c):
    """Where a generated daily plan is kept.

    A plan is a statement about a day: these crews, this work. Re-deriving it a week
    later gives a different answer, because jobs move, get reassigned and get closed —
    so what is filed is the sheet exactly as it was issued, and the row here is its
    index card: which day it plans, when it was issued and by whom.
    """
    c.execute("""CREATE TABLE IF NOT EXISTS plan_reports(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER,
        plan_date TEXT DEFAULT '', shift TEXT DEFAULT '',
        created_at TEXT DEFAULT '', created_by INTEGER, creator TEXT DEFAULT '',
        jobs INTEGER DEFAULT 0, crews INTEGER DEFAULT 0, people INTEGER DEFAULT 0,
        unassigned INTEGER DEFAULT 0, path TEXT DEFAULT '',
        kind TEXT DEFAULT 'plan')""")
    # 'plan' is the work issued for a day, 'done' the completed-work sheet for the same
    # day. Older databases have the table without the column, so add it in place.
    have = _table_cols(c, "plan_reports")
    if "kind" not in have:
        c.execute("ALTER TABLE plan_reports ADD COLUMN kind TEXT DEFAULT 'plan'")
    # The figures as the sheet printed them. Re-deriving them a week later gives a
    # different answer, because the jobs have moved on — so the row keeps its own copy.
    for col in ("done_n", "open_n", "accept_n", "doing_n", "pm_n", "cm_n"):
        if col not in have:
            c.execute("ALTER TABLE plan_reports ADD COLUMN %s INTEGER DEFAULT 0" % col)
    # WHICH jobs the sheet listed, not only how many. The count alone cannot answer "so
    # what is on the plan?" — and it cannot say which of them have since been moved to
    # another day, which is the question a planner actually asks when the board and the
    # issued sheet disagree. Filed as the ids, in the order the sheet printed them.
    if "job_ids" not in have:
        c.execute("ALTER TABLE plan_reports ADD COLUMN job_ids TEXT DEFAULT ''")
    # b394: every issued plan carries its own number, the plant first — BFL-, FP-, PC-
    # — so a sheet on a desk or in an email says which factory it is without opening it.
    # Plans issued before this have none and keep none.
    if "plan_no" not in have:
        c.execute("ALTER TABLE plan_reports ADD COLUMN plan_no TEXT DEFAULT ''")


# ── The departments that report work ─────────────────────────────────────────────
# `users.department` was free text, so "Production" and "production" were two
# departments, a typo made a third, and nothing could be filtered or counted by it.
# It is a code from this list now. The codes are the plant's own, agreed with
# engineering, and they matter beyond tidiness: from 1 Oct 2026 a CM job is numbered
# with the code of the department that reported it, because "not every corrective job
# comes from production" and PRD on all of them said otherwise.
DEPARTMENTS = [
    ("PRD", "ผลิต", "Production"),
    ("WRH", "คลังสินค้า", "Warehouse"),
    ("HRM", "ทรัพยากรบุคคล", "Human Resources"),
    ("QLY", "ประกันคุณภาพ", "Quality"),
    ("PCK", "บรรจุ / แพ็คกิ้ง", "Packing / Packaging"),
    ("ENG", "วิศวกรรม (ไฟฟ้า / เครื่องกล)", "Engineering (Electrical / Mechanical)"),
    ("RMS", "คลังวัตถุดิบ", "Raw Material Storage"),
    ("PRX", "พรีมิกซ์", "Premix"),
    ("DGT", "ไดเจสต์", "Digest"),
    ("BLR", "หม้อไอน้ำและระบบน้ำ", "Boiler & Water Treatment"),
    ("EXT", "เอ็กซ์ทรูเดอร์", "Extruder"),
    ("FGS", "คลังสินค้าสำเร็จรูป", "Finished Goods Store"),
    # A thirteenth, added once the live database turned out to have somebody filed under
    # it already. IT raises maintenance work like any other department — a printer, a
    # network cabinet, a scale or a panel PC on a line — and until it had a code those
    # jobs were going to be numbered PRD, which would have said production.
    # NOTE it is TWO letters, so a job number can be IT-2610-001. Nothing parses a job
    # number by fixed width; `migrate_archive.py` was the one place that assumed three
    # and now accepts two to four.
    ("IT", "ไอที", "Information Technology"),
    # Three more the plant asked for. SAFETY raises work nobody else does — a guard, a
    # rail, an eyewash, an alarm — and it is the department whose jobs most need to be
    # findable by their own number afterwards. SALES reports the offices and the show
    # room they work in. LOADING AREA is a place rather than an office, but the work
    # raised there (docks, levellers, shutters, yard lighting) belongs to whoever runs
    # it, not to production, which is the whole reason these codes exist.
    ("SAF", "ความปลอดภัย", "Safety"),
    ("SAL", "ฝ่ายขาย", "Sales"),
    ("LOD", "พื้นที่ขนถ่ายสินค้า", "Loading Area"),
]

# What the free text people already have maps onto. Anything not here keeps whatever
# was typed and is listed by the migration rather than guessed at — a department is
# who answers for the work, and putting somebody in the wrong one silently is worse
# than leaving a value the admin can see and fix.
_DEPT_ALIAS = {
    "production": "PRD", "prd": "PRD",
    "engineering": "ENG", "electrical": "ENG", "mechanical": "ENG",
    "automation": "ENG", "eng": "ENG", "maintenance": "ENG",
    "quality department": "QLY", "quality": "QLY", "qc lab": "QLY", "qc": "QLY",
    "packing": "PCK", "packaging": "PCK",
    "premix": "PRX", "digest": "DGT", "extruder": "EXT",
    "warehouse": "WRH", "store": "WRH", "fg": "FGS", "finished goods": "FGS",
    "hr": "HRM", "human resource": "HRM", "human resources": "HRM",
    # written as one word on all six HR rows across the three plants — an alias, not a
    # typo to correct by hand, because the next person will type it the same way
    "humanresource": "HRM", "humanresources": "HRM", "human resource department": "HRM",
    "boiler": "BLR", "raw material": "RMS", "raw material storage": "RMS",
    "it department": "IT", "information technology": "IT", "mis": "IT",
    "safety": "SAF", "safety department": "SAF", "hse": "SAF", "ความปลอดภัย": "SAF",
    "sales": "SAL", "sale": "SAL", "sales department": "SAL", "ฝ่ายขาย": "SAL",
    "loading": "LOD", "loading area": "LOD", "loading bay": "LOD", "loading dock": "LOD",
    "ขนถ่ายสินค้า": "LOD",
}

# The day the new numbering starts. Before it, a CM is PRD-2610-001 as it always was;
# on and after it, the reporting department's code leads. Numbers already issued are
# never rewritten — a job that has been signed, printed and filed keeps its number.
DEPT_JOBID_FROM = "2026-10-01"


def ensure_departments(c):
    """The department list, created once and seeded with the plant's codes."""
    c.execute("""CREATE TABLE IF NOT EXISTS departments(
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT DEFAULT '',
        name_th TEXT DEFAULT '', name_en TEXT DEFAULT '',
        seq INTEGER DEFAULT 0, active INTEGER DEFAULT 1)""")
    have = {str(r["code"] or "").upper() for r in c.execute("SELECT code FROM departments")}
    n = 0
    for i, (code, th, en) in enumerate(DEPARTMENTS):
        if code in have:
            continue
        c.execute("INSERT INTO departments(code,name_th,name_en,seq,active) VALUES(?,?,?,?,1)",
                  (code, th, en, i))
        n += 1
    if n:
        _log.info("[db] department list: %d code(s) added", n)
    return n


def dept_code(txt):
    """A department code from whatever is on a user row — code, name, or old free text."""
    t = str(txt or "").strip()
    if not t:
        return ""
    up = t.upper()
    # Match the code by MEMBERSHIP, not by width. This read `len(up) == 3` while every
    # code happened to be three letters; IT is two, so "IT" would have fallen straight
    # through to the alias table and resolved to nothing — the one department that had
    # already been typed correctly would have been the one that did not work.
    if up in {d[0] for d in DEPARTMENTS}:
        return up
    return _DEPT_ALIAS.get(t.lower(), "")


def _migrate_departments(c):
    """Turn the free text on user rows into codes, once, and say what it could not."""
    try:
        ensure_departments(c)
        rows = [dict(r) for r in c.execute(
            "SELECT id, department FROM users WHERE COALESCE(department,'') != ''")]
        moved, stuck = 0, []
        for r in rows:
            code = dept_code(r["department"])
            if code and code != r["department"]:
                c.execute("UPDATE users SET department=? WHERE id=?", (code, r["id"]))
                moved += 1
            elif not code:
                stuck.append(r["department"])
        if moved:
            _log.info("[db] %d user department(s) turned into codes", moved)
        if stuck:
            _log.warning("[db] department(s) with no code, left as typed: %s",
                         ", ".join(sorted(set(stuck))))
    except Exception as e:
        _log.warning("[db] department migration skipped: %s", e)


def ensure_problem_types(c):
    """Create the symptom list and, the first time only, fill it with a starter set."""
    c.execute("""CREATE TABLE IF NOT EXISTS problem_types(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER,
        name TEXT DEFAULT '', category TEXT DEFAULT 'Other',
        seq INTEGER DEFAULT 0, active INTEGER DEFAULT 1)""")
    have = c.execute("SELECT COUNT(*) FROM problem_types").fetchone()[0]
    if have:
        return 0
    n = 0
    for fac in [r["id"] for r in c.execute("SELECT id FROM factories ORDER BY id")]:
        for i, (nm, cat) in enumerate(PROBLEM_SEED):
            c.execute("INSERT INTO problem_types(factory_id,name,category,seq,active)"
                      " VALUES(?,?,?,?,1)", (fac, nm, cat, i))
            n += 1
    return n



# ── Feedmill mechanical symptoms ───────────────────────────────────────────────
# The starter list carries six mechanical entries and they are deliberately generic —
# "unusual noise", "broken part" — because they have to serve any plant. A feedmill
# fails in particular ways, and an operator who cannot find their fault on the list
# picks "Other" and types nothing useful, which is how a plant ends up unable to say
# what actually breaks. These are the failures the equipment in the dry-food registers
# actually has: elevators, chain and screw conveyors, slide gates, flap boxes, air
# locks, bins, hammer mills, sifters, magnets, bag filters, packing.
#
# Written as WHAT THE OPERATOR SEES, never as a diagnosis — an operator can say the
# screw is turning but nothing is coming out; only a technician can say why.
FEEDMILL_MECH = [
    ("สายพานกระพ้อลื่น / ตกร่อง / Bucket elevator belt slipping or off-centre", "Mechanical"),
    ("ลูกกระพ้อหลุด / แตก / Elevator buckets loose, cracked or missing", "Mechanical"),
    ("โซ่ลำเลียงตกเฟือง / ขาด / Chain conveyor jumped the sprocket or snapped", "Mechanical"),
    ("ใบสกรูสึก — หมุนแต่ของไม่ออก / Screw turning but not moving material", "Mechanical"),
    ("สายพานส่ายออกข้าง / Conveyor belt running off to one side", "Mechanical"),
    ("สายพานฉีก / ขอบขาด / Conveyor belt torn or edge frayed", "Mechanical"),
    ("ตลับลูกปืนร้อน / มีเสียง / Bearing hot, noisy or seizing", "Mechanical"),
    ("คัปปลิ้ง / สลักนิรภัยขาด / Coupling or shear pin broken", "Mechanical"),
    ("เกียร์รั่วน้ำมัน / มีเสียงผิดปกติ / Gearbox leaking oil or noisy", "Mechanical"),
    ("สไลด์เกทเปิด-ปิดไม่สุด / Slide gate will not open or close fully", "Mechanical"),
    ("แฟลปบ็อกซ์ค้าง — ของไปผิดทาง / Flap box stuck, material going the wrong way", "Mechanical"),
    ("โรตารี่แอร์ล็อคติด / ใบครูด / Air lock jammed or rotor rubbing", "Mechanical"),
    ("ของค้างในถัง ไม่ไหลลง / Bin or hopper bridging, material not flowing", "Mechanical"),
    ("ของรั่วตามท่อ / รอยต่อ / Material leaking from a chute, duct or joint", "Mechanical"),
    ("โรตารี่ดิสทริบิวเตอร์เข้าไม่ตรงรู / Rotary distributor not lining up with the outlet", "Mechanical"),
    ("ตะแกรงบดแตก / ทะลุ / Hammer mill screen holed or broken", "Mechanical"),
    ("ค้อนบดสึก — กำลังผลิตตก / Hammer mill hammers worn, output down", "Mechanical"),
    ("ตะแกรงร่อนขาด — ของหยาบหลุดผ่าน / Sifter screen torn, oversize passing through", "Mechanical"),
    ("แม่เหล็กดักเหล็กหลวม / ทำความสะอาดไม่ได้ / Magnet loose or cannot be cleaned", "Mechanical"),
    ("โบลเวอร์สั่น / ใบพัดสึก / Blower vibrating or impeller worn", "Mechanical"),
    ("ถุงกรองตัน / ฝุ่นออกทางปล่อย / Bag filter blinded, dust carrying over", "Mechanical"),
    ("ไซโคลน / ท่อ ทะลุ ฝุ่นออก / Cyclone or ducting worn through, dust escaping", "Mechanical"),
    ("ลมรั่ว / Compressed air leaking", "Mechanical"),
    ("ปั๊มไม่ดูด / รั่วตามซีล / Pump not priming or leaking at the seal", "Mechanical"),
    ("ปากถุงจับไม่อยู่ / ของหก / Bag not clamping at the spout, spillage", "Mechanical"),
    ("เย็บ / ซีลปากถุงไม่ติด / Stitch skipping or bag seal not closing", "Mechanical"),
    ("การ์ด / ฝาครอบ หลุด หาย / Guard or cover loose or missing", "Mechanical"),
    ("โครงสร้าง / ขาตั้ง ร้าว น็อตหลวม / Frame, support or bolts cracked or loose", "Mechanical"),
]
# The plants that mill and convey dry material. BFLFP is wet food and fails differently,
# so it keeps its own list until somebody asks otherwise.
FEEDMILL_PLANTS = ("BFL", "PC", "BFLPC")


def add_feedmill_symptoms(c, codes=FEEDMILL_PLANTS):
    """Put the feedmill symptoms on the dry-food plants' report screens, once.

    Inserted immediately AFTER the mechanical entries already there rather than at the
    end of the list, so the categories stay grouped and "Other — described below" stays
    last where an operator expects it; everything below is pushed down by the number
    added, which preserves any order an admin has already set. Matched by name, so
    running it twice adds nothing and an entry the plant has since renamed or deleted is
    never resurrected.
    """
    ensure_problem_types(c)
    added = 0
    for f in c.execute("SELECT id,code FROM factories ORDER BY id").fetchall():
        if str(f["code"] or "").upper() not in codes:
            continue
        fac = f["id"]
        have = {r["name"] for r in c.execute(
            "SELECT name FROM problem_types WHERE factory_id=?", (fac,))}
        new = [(nm, cat) for nm, cat in FEEDMILL_MECH if nm not in have]
        if not new:
            continue
        after = c.execute("SELECT COALESCE(MAX(seq),-1) FROM problem_types"
                          " WHERE factory_id=? AND category='Mechanical'", (fac,)).fetchone()[0]
        c.execute("UPDATE problem_types SET seq=seq+? WHERE factory_id=? AND seq>?",
                  (len(new), fac, after))
        for i, (nm, cat) in enumerate(new):
            c.execute("INSERT INTO problem_types(factory_id,name,category,seq,active)"
                      " VALUES(?,?,?,?,1)", (fac, nm, cat, after + 1 + i))
            added += 1
        regroup_problem_types(c, fac)          # trades grouped, Other last
    return added



# ── Utilities: water, cold rooms, compressed air, air conditioning ─────────────
# Every plant runs these and none of them appeared on the list, so a leaking RO tank,
# a cold room drifting warm and a compressor cycling all shift were all reported as
# "Other". They go to ALL THREE plants — utilities are the one thing wet food and dry
# food have in common.
#
# The category is the trade that will most likely pick the job up, not the system the
# fault is in: a water pump that will not start is electrical work even though it is
# part of the water plant.
UTILITY_SYMPTOMS = [
    # water treatment, RO, tanks and pumps
    ("น้ำไม่ไหล / แรงดันน้ำตก / No water, or water pressure low", "Mechanical"),
    ("ท่อน้ำรั่ว / แตก / Water pipe leaking or burst", "Mechanical"),
    ("ถังน้ำรั่ว / ลูกลอยเสีย / Water tank leaking or float valve faulty", "Mechanical"),
    ("ปั๊มน้ำไม่ทำงาน / ตัดบ่อย / Water pump will not start or keeps tripping", "Electrical"),
    ("RO ผลิตน้ำได้น้อย / เมมเบรนตัน / RO output down, membrane fouled", "Process"),
    ("ค่าน้ำไม่ผ่าน — TDS / pH / คลอรีน / Water test out of spec (TDS, pH, chlorine)", "Process"),
    ("สารเคมีหมด / ปั๊มจ่ายสารไม่ทำงาน / Dosing chemical empty or dosing pump not running", "Process"),
    ("ท่อระบาย / สครับเบอร์ ตัน / Drain or scrubber blocked", "Mechanical"),
    # cold rooms and refrigeration
    ("ห้องเย็นอุณหภูมิไม่ลง / Cold room not holding temperature", "Process"),
    ("คอมเพรสเซอร์ทำความเย็นไม่ทำงาน / ตัดบ่อย / Refrigeration compressor not running or cutting out", "Electrical"),
    ("คอยล์เย็นเป็นน้ำแข็ง / Evaporator coil iced up", "Mechanical"),
    ("น้ำยาทำความเย็นรั่ว / Refrigerant leak", "Mechanical"),
    ("ประตูห้องเย็นปิดไม่สนิท / ซีลเสีย / Cold room door not sealing", "Mechanical"),
    ("น้ำหยด / น้ำแข็งเกาะพื้นห้องเย็น / Water dripping or ice on the cold room floor", "Mechanical"),
    # compressed air
    ("ลมไม่พอ / แรงดันลมตก / Not enough compressed air, pressure dropping", "Mechanical"),
    ("ปั๊มลมตัดบ่อย / ไม่ตัด / Compressor cycling too often, or never unloading", "Electrical"),
    ("น้ำ / น้ำมัน ปนมากับลม / Water or oil carrying over in the air line", "Mechanical"),
    ("แอร์ดรายเออร์ไม่ทำงาน / Air dryer not working", "Electrical"),
    ("ปั๊มลมร้อน / ตัดด้วยความร้อน / Compressor overheating or tripping on temperature", "Electrical"),
    # air conditioning
    ("แอร์ไม่เย็น / Air conditioner not cooling", "Mechanical"),
    ("แอร์มีน้ำหยด / Air conditioner dripping water", "Mechanical"),
    ("แอร์มีเสียงดัง / สั่น / Air conditioner noisy or vibrating", "Mechanical"),
    ("รีโมท / แผงควบคุมแอร์ ไม่ทำงาน / Air conditioner remote or control panel not working", "Electrical"),
    # electric and control
    ("อินเวอร์เตอร์ / VSD ขึ้นรหัสผิดพลาด / Inverter or VSD showing a fault code", "Electrical"),
    ("สายไฟชำรุด / ขั้วต่อหลวม ไหม้ / Cable damaged, or terminal loose and burnt", "Electrical"),
    ("ไฟรั่ว / ไฟดูด / Earth leakage — a shock was felt", "Electrical"),
    ("ไฟตก / ไฟดับ ทั้งไลน์ / Power dip or outage across the whole line", "Electrical"),
    ("ตู้คอนโทรลร้อน / พัดลมตู้เสีย / Control panel hot, or its cooling fan failed", "Electrical"),
    ("ปุ่มหยุดฉุกเฉินค้าง / รีเซ็ตไม่ได้ / Emergency stop stuck or will not reset", "Electrical"),
    # instrument
    ("หัววัดอุณหภูมิ / เกจ อ่านค่าเพี้ยน / Temperature probe or gauge reading wrong", "Instrument"),
    ("โหลดเซลล์ / เครื่องชั่ง ค่าเลื่อน / Load cell or scale drifting", "Instrument"),
    ("เครื่องตรวจโลหะไม่ตรวจจับ / ตัวคัดแยกไม่ทำงาน / Metal detector not detecting, or reject not firing", "Instrument"),
]

# ── Wet food: retort, seamer, filler, can washer ───────────────────────────────
# BFLFP cans and pouches; none of this equipment exists in the dry plants, so the list
# would only be noise there.
WETFOOD_SYMPTOMS = [
    ("หม้อฆ่าเชื้อ อุณหภูมิ / แรงดัน ไม่ถึง / Retort not reaching temperature or pressure", "Process"),
    ("หม้อฆ่าเชื้อ รอบฆ่าเชื้อหยุดกลางคัน / Retort cycle stopped part way through", "Process"),
    ("ตะเข็บกระป๋องไม่ได้มาตรฐาน / Can seam out of specification", "Process"),
    ("เครื่องปิดฝากระป๋องติด / ฝาเบี้ยว / Seamer jamming, or lids going on crooked", "Mechanical"),
    ("เครื่องบรรจุ น้ำหนักไม่ได้ / หก / Filler weight out of range, or spilling", "Process"),
    ("ซองรั่ว / ซีลไม่แน่น / Pouch leaking or seal not tight", "Process"),
    ("เครื่องล้างกระป๋อง หัวฉีดตัน / Can washer nozzles blocked", "Mechanical"),
    ("ไอน้ำไม่พอ / หม้อไอน้ำตัด / Not enough steam, boiler cutting out", "Mechanical"),
    ("กระป๋องล้ม / ติดบนสายพาน / Cans falling over or jamming on the conveyor", "Mechanical"),
    ("ห้องบ่ม อุณหภูมิไม่ได้ / Incubation room temperature out of range", "Process"),
]
WETFOOD_PLANTS = ("FP", "BFLFP")


# The order the trades read in. "Other — described below" is deliberately last: it is
# the entry an operator reaches for only after failing to find a real one, and putting
# it anywhere else invites it to be picked first. Its 18 uses on the live list are the
# argument for the whole exercise.
PCAT_ORDER = ["Mechanical", "Electrical", "Instrument", "Process", "Other"]


def regroup_problem_types(c, fac):
    """Renumber one plant's list so the trades are grouped and Other is last.

    Inserting "after the last row of the same category" is not enough on its own: a
    plant whose Electrical rows were appended later already has them sitting BELOW
    Other, so anything filed after them lands below it too. Sorting the whole list once
    fixes that and cannot make it worse — within a category the existing order is kept,
    so an order an admin set by hand survives inside its own group.
    """
    rows = [dict(r) for r in c.execute(
        "SELECT id,category,seq,name FROM problem_types WHERE factory_id=? ORDER BY seq,name", (fac,))]
    rows.sort(key=lambda r: (PCAT_ORDER.index(r["category"]) if r["category"] in PCAT_ORDER
                             else len(PCAT_ORDER), r["seq"], r["name"]))
    for i, r in enumerate(rows):
        if r["seq"] != i:
            c.execute("UPDATE problem_types SET seq=? WHERE id=?", (i, r["id"]))
    return len(rows)


def add_symptoms(c, rows, codes=None):
    """Put a set of symptoms on the plants named, once, keeping the categories grouped.

    Each entry is inserted after the LAST row already in its own category, so the list
    stays sorted by trade and "Other — described below" stays at the bottom where an
    operator expects it. Everything below the insertion point is pushed down, which
    preserves any order an admin has set by hand. Matched by name, so a second run adds
    nothing.
    """
    ensure_problem_types(c)
    added = 0
    for f in c.execute("SELECT id,code FROM factories ORDER BY id").fetchall():
        if codes and str(f["code"] or "").upper() not in codes:
            continue
        fac = f["id"]
        have = {r["name"] for r in c.execute(
            "SELECT name FROM problem_types WHERE factory_id=?", (fac,))}
        top = c.execute("SELECT COALESCE(MAX(seq),-1) FROM problem_types WHERE factory_id=?",
                        (fac,)).fetchone()[0]
        n0 = added
        for nm, cat in rows:
            if nm in have:
                continue
            top += 1
            c.execute("INSERT INTO problem_types(factory_id,name,category,seq,active)"
                      " VALUES(?,?,?,?,1)", (fac, nm, cat, top))
            have.add(nm)
            added += 1
        if added > n0:
            regroup_problem_types(c, fac)      # trades grouped, Other last
    return added


# ── Two-stage status ────────────────────────────────────────────────────────────
# The single `status` column stays the engine — every transition rule, list, KPI and
# the PM generator run on it. stage1/stage2 are written alongside it on every change,
# so the screens and the exports can speak the words the plant uses. Second time round
# (after the operator sends work back) everything carries an "R." — that is the whole
# difference between the first pass and the rework pass.
# Work that never happened. A cancelled job was called off; a rejected request was
# never approved. Neither is a maintenance event, so no metric, count, ranking, chart
# or export may include one — a cancelled breakdown is not a breakdown, and minutes
# logged before it was called off are not repair time. Deleted jobs need no rule: the
# row is gone. Every query that produces a number a manager reads must carry VOID_SQL.
VOID = ("Cancelled", "Rejected")
VOID_SQL = "('Cancelled','Rejected')"

# A job an admin has taken out of the numbers by hand. Same effect as a void status —
# the row stays, the number forgets it — but chosen by a person rather than by what
# happened: a training run, a duplicate report, a breakdown nobody ever pressed Stop on.
#
# It used to be honoured in ONE query, the downtime panel, so an excluded job still
# moved MTTR, MTBF, the trend, the work-time chart and the dashboard counts. "Not in the
# KPI" has to mean all of them or it means nothing, so both spellings live here and go
# on every query that produces a number: NOTKPI where the table is bare, NOTKPI_J where
# it is joined as j.
NOTKPI = " AND COALESCE(kpi_exclude,0)=0 "
NOTKPI_J = " AND COALESCE(j.kpi_exclude,0)=0 "

STAGE1 = {"Reported": "Reported", "WaitingApproval": "Reported", "WaitingAssignment": "Reported",
          "Assigned": "Reported", "Released": "Reported",
          "InProgress": "In progress", "Paused": "Hold", "Hold": "Hold",
          "ServiceCompleted": "Completed", "Done": "Completed", "Rejected": "Completed",
          "Rework": "Rework", "Cancelled": "Cancelled"}


def stage_for(status, lead_tech=None, rework_count=0, helpers="", due_date=None):
    """(stage 1, stage 2) for a job. Everything the pair needs is on the job row.

    A held job is the one case where stage 2 is about the PLANNER rather than the
    crew. Work stops for something outside the workshop — a part on order, a
    supplier visit — and until somebody says when it comes back the job sits in no
    plan and no chip can chase it. So a hold with no due date reads *Pending due
    date*, and one with a date reads *Wait for action*: parked, not forgotten.
    """
    st = (status or "Reported").strip()
    crew = bool(lead_tech) or bool(str(helpers or "").strip())
    again = int(rework_count or 0) > 0
    s1 = STAGE1.get(st, st)
    if st == "WaitingApproval":
        return "Reported", "Wait cost approval"
    if st == "Cancelled":
        return "Cancelled", "Cancelled"
    if st == "Rejected":                      # the operator turned the work down for good
        return "Completed", "Rejected"
    if st in ("ServiceCompleted", "Done"):
        s2 = "Wait for approve" if st == "ServiceCompleted" else "Approved"
    elif st == "Rework":
        s1, s2 = "Rework", ("Assigned" if crew else "Not assigned")
    elif st in ("Hold", "Paused"):
        s2 = "Wait for action" if str(due_date or "").strip() else "Pending due date"
    else:
        s2 = "Assigned" if crew else "Not assigned"
    if again:
        if s1 in ("In progress", "Hold", "Completed"):
            s1 = "R." + s1
        s2 = "R." + s2
    return s1, s2


def set_stage(c, jid):
    """Recompute the pair for one job. Call it after anything that moves a job."""
    r = c.execute("SELECT status,lead_tech,helpers,rework_count,due_date FROM jobs WHERE id=?",
                  (jid,)).fetchone()
    if not r:
        return
    s1, s2 = stage_for(r["status"], r["lead_tech"], r["rework_count"], r["helpers"], r["due_date"])
    c.execute("UPDATE jobs SET stage1=?, stage2=? WHERE id=?", (s1, s2, jid))
    return s1, s2


def init():
    create_schema()
    c = db()
    try:
        _migrate(c)
        if not c.execute("SELECT id FROM factories LIMIT 1").fetchone():
            c.executemany("INSERT INTO factories(code,name,form_code) VALUES(?,?,?)", [
                ("BFL", "Bluefalo Main Plant", "SD-SP-ENG02-01"),
                ("FP", "Bluefalo Food Products (Wet Food)", "F-SP-ENG02-03 Rev.01"),
                ("PC", "Bluefalo Petcare", "PC-ENG-01"),
            ])
        if not c.execute("SELECT id FROM users LIMIT 1").fetchone():
            seed(c)
        # Cost approval was removed: a report no longer waits for the planner to agree
        # to the money before it reaches the queue. Any job still parked at the old
        # status would sit for ever with no screen able to release it, so it is moved
        # into the normal queue once, here, and re-stamped with its stage pair.
        try:
            stuck = c.execute("SELECT id FROM jobs WHERE status='WaitingApproval'").fetchall()
            for r in stuck:
                c.execute("UPDATE jobs SET status='Reported', stage1='', stage2='' WHERE id=?", (r["id"],))
                log_status(c, r["id"], "Reported", None)
            if stuck:
                _log.info(f"[db] {len(stuck)} job(s) released from the old cost-approval queue")
        except Exception as e:
            _log.warning("[db] cost-approval cleanup skipped: %s", e)
        # "Released" and "Assigned" were two names for one thing — a technician has this
        # job — and the screens showed them as two different states. They are now one
        # status, Assigned. Old rows and their history are renamed once, here, so the
        # timeline of a job that predates the change still reads straight through.
        try:
            n = c.execute("SELECT COUNT(*) FROM jobs WHERE status='Released'").fetchone()[0]
            c.execute("UPDATE jobs SET status='Assigned', stage1='', stage2='' WHERE status='Released'")
            c.execute("UPDATE job_events SET status='Assigned' WHERE status='Released'")
            if n:
                _log.info(f"[db] {n} job(s) moved from Released to Assigned")
        except Exception as e:
            _log.warning("[db] Released→Assigned rename skipped: %s", e)
        # A timer left running on a job that has since been cancelled or finished can
        # never be stopped from the app — the job is gone from every screen — and it
        # locks that technician out of starting any other work. Close them at the
        # moment the job actually ended, so no invented minutes reach the KPIs.
        try:
            stale = c.execute(
                "SELECT t.id, t.start, j.done_at, j.id jid FROM timelogs t"
                " JOIN jobs j ON j.id=t.job_id WHERE t.end IS NULL"
                " AND j.status IN ('Cancelled','Rejected','Done','ServiceCompleted')").fetchall()
            for r in stale:
                endts = r["done_at"]
                if not endts:
                    ev = c.execute("SELECT created_at FROM job_events WHERE job_id=?"
                                   " ORDER BY id DESC LIMIT 1", (r["jid"],)).fetchone()
                    endts = ev["created_at"] if ev else None
                if not endts or endts < (r["start"] or ""):
                    endts = r["start"]
                c.execute("UPDATE timelogs SET end=?, pause_reason=? WHERE id=?",
                          (endts, "job closed", r["id"]))
            if stale:
                _log.info(f"[db] {len(stale)} stuck timer(s) closed on jobs that had already ended")
        except Exception as e:
            _log.warning("[db] stuck-timer cleanup skipped: %s", e)
        # The Stage 1 / Stage 2 pair is stored, not computed on read, so any code path
        # that forgot to re-stamp it leaves a job reading "Reported · Not assigned"
        # with a full crew on it. The pair is a pure function of status, crew and
        # rework count, so re-deriving every row at startup costs nothing and heals
        # anything that drifted — including jobs written before the columns existed.
        try:
            fixed = 0
            for r in c.execute("SELECT id,status,lead_tech,helpers,rework_count,stage1,stage2"
                               " FROM jobs").fetchall():
                s1, s2 = stage_for(r["status"], r["lead_tech"], r["rework_count"], r["helpers"])
                if (r["stage1"] or "") != s1 or (r["stage2"] or "") != s2:
                    c.execute("UPDATE jobs SET stage1=?, stage2=? WHERE id=?", (s1, s2, r["id"]))
                    fixed += 1
            if fixed:
                _log.info(f"[db] two-stage status re-stamped on {fixed} job(s)")
        except Exception as e:
            _log.warning("[db] stage backfill skipped: %s", e)
        try:
            ensure_plan_reports(c)
        except Exception as e:
            _log.warning("[db] plan_reports table skipped: %s", e)
        try:
            ensure_sessions(c)
            c.execute("DELETE FROM sessions WHERE expires>0 AND expires<?",
                      (int(time.time()),))          # yesterday's logins, swept on boot
        except Exception as e:
            _log.warning("[db] sessions table skipped: %s", e)
        try:
            n = ensure_problem_types(c)
            if n:
                _log.info(f"[db] problem type list created — {n} starter entries")
        except Exception as e:
            _log.warning("[db] problem types skipped: %s", e)
        # the department list, and the free text on user rows turned into its codes
        _migrate_departments(c)
        if not c.execute("SELECT id FROM channels LIMIT 1").fetchone():
            c.executemany("INSERT INTO channels(name,kind) VALUES(?,?)", [
                ("ทีมช่าง (Maintenance team)", "team"),
                ("ไลน์ผลิต 1 (Line 1)", "team"),
                ("🔴 Breakdown feed", "system"),
            ])
        c.commit()
    finally:
        c.close()
    # One job table per plant. First start: move every job into its plant's table (no
    # renumbering, backup taken first). Every start after: keep the view in step.
    try:
        from .jobtables import split_jobs
        split_jobs()
    except Exception as e:
        _log.error("[jobs-split] failed: %s", e)
    # PM records made safe before anything can edit or re-upload a checklist: every saved
    # answer gets its wording, every started PM job its frozen list (pm.py _ensure_records)
    try:
        from . import pm as _pm
        _c = db()
        try:
            _pm._ensure(_c)
            _c.commit()
        finally:
            _c.close()
    except Exception as e:
        _log.error("[pm] record protection at startup failed: %s", e)


def seed(c):
    users = [
        ("admin1", "ผู้ดูแลระบบ (Admin)", "admin"),
        ("planner1", "คุณวางแผน (Planner)", "planner"),
        ("tech1", "สมชาย (Somchai)", "technician"),
        ("tech2", "สมศักดิ์ (Somsak)", "technician"),
        ("op1", "โอเปอเรเตอร์ไลน์ 1", "operator"),
        ("manager1", "ผู้จัดการโรงงาน (Manager)", "manager"),
    ]
    for u, n, r in users:
        c.execute("INSERT INTO users(username,password,name,role,active,factory_id) VALUES(?,?,?,?,1,2)",
                  (u, hash_pw("1234"), n, r))
    machines = [
        # code, name, rate, crit, line, pm_days, kpi_class, approved
        ("W01MX01", "เครื่องผสม (Mixer) #1", 0, "A", "Line 1", 7, "BATCH", 1),
        ("W01LP01", "เครื่องบรรจุซอง (Sachet filler) #1", 120, "A", "Line 1", 7, "OEE", 1),
        ("W01SM01", "เครื่องปิดฝากระป๋อง (Can seamer) #1", 120, "A", "Line 1", 7, "OEE", 1),
        ("W01RT01", "หม้อฆ่าเชื้อ (Retort) #1", 0, "A", "Line 1", 30, "BATCH", 1),
        ("W01RT02", "หม้อฆ่าเชื้อ (Retort) #2", 0, "A", "Line 1", 30, "BATCH", 1),
        ("W01AC01", "ปั๊มลมสกรู (Air compressor) #1", 0, "B", "Utilities", 7, "AVAIL", 1),
        ("W01BL01", "หม้อไอน้ำ (Boiler) #1", 0, "A", "Utilities", 7, "AVAIL", 1),
        ("W01CH01", "ตู้แช่ทำความเย็น (Chiller) #1", 0, "B", "Utilities", 7, "AVAIL", 1),
        ("W01CV01", "สายพานลำเลียง (Conveyor) #1", 0, "C", "Line 1", 30, "AVAIL", 1),
        ("W01LB01", "เครื่องติดฉลาก (Labeler) #1", 100, "B", "Line 1", 7, "OEE", 1),
    ]
    for m in machines:
        c.execute("""INSERT INTO machines(code,name,ideal_rate,criticality,line,pm_freq_days,
            kpi_class,kpi_approved,active,factory_id) VALUES(?,?,?,?,?,?,?,?,1,2)""", m)


def next_workday(d, offdays=(6,), holidays=(), limit=14):
    """The next day the factory actually works, after `d` (an ISO date string).

    Rolling a job onto a Sunday and calling it planned is how a plan stops being
    believed. Falls back to d+1 if it cannot find one inside `limit` days, so a
    misconfigured holiday list can never loop.
    """
    from datetime import date as _date, timedelta as _td
    try:
        cur = _date.fromisoformat(str(d)[:10])
    except ValueError:
        return d
    for _ in range(limit):
        cur = cur + _td(days=1)
        if cur.weekday() not in offdays and cur.isoformat() not in holidays:
            return cur.isoformat()
    return (_date.fromisoformat(str(d)[:10]) + _td(days=1)).isoformat()


# ── Whose name goes on a printed form ────────────────────────────────────────────
def factory_company(c, factory_id, lang="th"):
    """The legal company name for one plant — Thai by default, English on request.

    Every controlled document (the repair form, the PM sheet, the daily report, the
    shift plan) must carry the name of the company that owns the machine. Falls back
    to the compiled-in name only when the job sits on no asset and no plant can be
    worked out, which is the one case where any answer is a guess.
    """
    from .config import COMPANY_TH, COMPANY_TH_BY_CODE, COMPANY_EN_BY_CODE
    code = ""
    try:
        if factory_id:
            r = c.execute("SELECT code FROM factories WHERE id=?", (factory_id,)).fetchone()
            code = (r["code"] or "").strip().upper() if r else ""
    except Exception:
        code = ""
    table = COMPANY_EN_BY_CODE if lang == "en" else COMPANY_TH_BY_CODE
    return table.get(code) or (COMPANY_EN_BY_CODE.get("FP") if lang == "en" else COMPANY_TH)


# ── Per-weekday working hours ────────────────────────────────────────────────────
# `weekly_off` answers one question — does the plant work this weekday — and for a
# plant that works a SHORTER day rather than no day at all it has no answer. BFLFP's
# Sunday is 07:00–20:00: ticked off it contributes nothing and the denominator is a
# day short every week; left on it claims the full 14 hours and every machine looks
# more available than it is. So a weekday may now carry its own window.
#
# The shape, inside the same hours_json (no schema change, nothing to migrate):
#
#     "days": {"6": {"start": "07:00", "end": "20:00"},   # Sunday, 13 hours
#              "5": {"off": true}}                        # Saturday, closed
#
# Keys are weekday numbers as strings, 0 = Monday … 6 = Sunday, and only the days
# that DIFFER need an entry. A day named here overrides `weekly_off` in both
# directions, which is the whole point: "Sunday is off" and "Sunday is 13 hours"
# are contradictory and the more specific statement has to win. Everything without
# an entry falls through to `start`/`end` + `weekly_off` exactly as before, so a
# plant that never touches this sees no change at all.
def day_windows(cfg):
    """{weekday int: (start_hour, end_hour) or None} — None meaning the day is off.

    Only weekdays the config names are in the map; the caller falls back to the
    plant's own start/end and weekly_off for every weekday that is absent.
    """
    out = {}
    for k, v in ((cfg or {}).get("days") or {}).items():
        try:
            wd = int(k)
        except (TypeError, ValueError):
            continue
        if not 0 <= wd <= 6:
            continue
        if v in (None, False, "", "off") or (isinstance(v, dict) and v.get("off")):
            out[wd] = None
            continue
        if not isinstance(v, dict):
            continue
        try:
            s = int(str(v.get("start") or "07:00").split(":")[0])
            e = int(str(v.get("end") or "21:00").split(":")[0])
        except ValueError:
            continue
        out[wd] = (s, 24 if e == 0 else e)          # end 00:00 = runs to midnight
    return out


def offdays_of(cfg):
    """The weekdays the plant does not work at all, `days` taken into account.

    Anything that only asks "is this a working day" — the PM calendar, the carry
    forward of undone jobs — reads this instead of `weekly_off`, or a Sunday with
    real hours on it would still be treated as closed.
    """
    cfg = cfg or {}
    base = set(cfg["weekly_off"] if cfg.get("weekly_off") is not None else [6])
    for wd, win in day_windows(cfg).items():
        if win is None:
            base.add(wd)
        else:
            base.discard(wd)
    return tuple(sorted(base))

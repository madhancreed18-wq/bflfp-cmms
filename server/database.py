"""Engine layer — SQLite for dev/pilot, PostgreSQL for production.

Switch engines with ONE setting (no code changes):
    DATABASE_URL=postgresql+psycopg2://user:pass@host/cmms

All application SQL goes through the Conn wrapper below, which:
- accepts `?` placeholders on every engine
- returns dict-like rows (row["col"] and row[0] both work)
- provides insert_id() portable across SQLite (lastrowid) and PG (RETURNING)
"""
import os, re
from sqlalchemy import create_engine, text, event, MetaData, Table, Column, \
    Integer, Float, String, Text as SAText, UniqueConstraint

from .config import DATA

DB_URL = os.environ.get("DATABASE_URL") or "sqlite:///" + os.path.join(DATA, "cmms.db")
engine = create_engine(DB_URL, future=True, pool_pre_ping=True)

if engine.dialect.name == "sqlite":
    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        try:
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass

metadata = MetaData()

def T(name, *cols):
    return Table(name, metadata, *cols)

T("factories",
  Column("id", Integer, primary_key=True),
  Column("code", String(10), unique=True),
  Column("name", String(120)),
  Column("form_code", String(60), server_default=text("''")),
  Column("targets_json", SAText, server_default=text("''")),
  Column("hours_json", SAText, server_default=text("''")))   # working hours + holidays (KPI calendar)

T("users",
  Column("id", Integer, primary_key=True),
  Column("username", String(60), unique=True),
  Column("password", String(200)),
  Column("name", String(120)),
  Column("role", String(20)),
  Column("active", Integer, server_default=text("1")),
  Column("factory_id", Integer, server_default=text("2")))

# An asset code is unique WITHIN a plant, not across the company. The three plants run
# their own numbering and collide legitimately: BFL and BFLFP both have a W01FP01 — one a
# fire pump (ปั้มน้ำดับเพลิง), the other a feed pump (ปั๊มวัตถุดิบ). A global UNIQUE(code)
# turned that into a database error and put the app in the position of telling a plant to
# rename a real machine to suit a schema.
T("machines",
  Column("id", Integer, primary_key=True),
  Column("code", String(40)),
  Column("name", String(200)),
  Column("active", Integer, server_default=text("1")),
  Column("ideal_rate", Float, server_default=text("0")),
  Column("criticality", String(4), server_default=text("'B'")),
  Column("line", String(80), server_default=text("'Line 1'")),
  Column("pm_freq_days", Integer, server_default=text("0")),
  Column("factory_id", Integer, server_default=text("2")),
  Column("kpi_class", String(10), server_default=text("'AVAIL'")),   # OEE / BATCH / AVAIL
  Column("kpi_approved", Integer, server_default=text("0")),          # delta #1
  Column("brand_model", String(120), server_default=text("''")),
  Column("serial_no", String(80), server_default=text("''")),
  Column("year_install", String(10), server_default=text("''")),
  Column("last_pm_date", String(10), server_default=text("''")),
  Column("category", String(40), server_default=text("''")),      # asset register imports
  Column("asset_group", String(80), server_default=text("''")),
  Column("floor", String(40), server_default=text("''")),
  Column("department", String(80), server_default=text("''")),
  Column("manufacturer", String(120), server_default=text("''")),
  Column("size", String(80), server_default=text("''")),
  Column("pm_group_color", String(20), server_default=text("''")),   # PM colour group pinned by hand
  Column("remark", SAText, server_default=text("''")),
  UniqueConstraint("factory_id", "code", name="uq_machines_factory_code"))

T("jobs",
  Column("id", Integer, primary_key=True),
  Column("jobid", String(30), unique=True),
  Column("jobtype", String(6)),
  Column("machine_id", Integer),
  Column("descr", SAText, server_default=text("''")),
  Column("report_name", String(120), server_default=text("''")),   # operator report title
  Column("priority", Integer, server_default=text("1")),
  Column("status", String(24), server_default=text("'Reported'")),
  Column("stage1", String(20), server_default=text("''")),      # phase the plant reads
  Column("stage2", String(24), server_default=text("''")),      # crew / approval state
  Column("planned_date", String(10)),
  Column("planned_start", String(5)),
  Column("planned_end", String(5)),
  Column("lead_tech", Integer),
  Column("helpers", String(120), server_default=text("''")),
  Column("progress", Integer, server_default=text("0")),
  Column("pending_reason", String(200), server_default=text("''")),
  Column("problem", SAText, server_default=text("''")),
  Column("root_cause", SAText, server_default=text("''")),
  Column("solution", SAText, server_default=text("''")),
  Column("carryover", Integer, server_default=text("0")),
  Column("due_date", String(10)),
  Column("jobsource", String(30), server_default=text("''")),
  Column("img_before", String(200), server_default=text("''")),
  Column("img_after", String(200), server_default=text("''")),
  Column("img_before2", String(200), server_default=text("''")),
  Column("img_after2", String(200), server_default=text("''")),
  Column("requester_id", Integer),
  Column("planned_at", String(30)),
  Column("started_at", String(30)),
  Column("approved_at", String(30)),
  Column("approver_id", Integer),
  Column("sign_tech", String(200), server_default=text("''")),
  Column("sign_appr", String(200), server_default=text("''")),
  Column("sign_requester", String(200), server_default=text("''")),
  Column("sign_inspector", String(200), server_default=text("''")),
  Column("cleared_worksite", Integer),
  Column("new_issue_id", String(30), server_default=text("''")),
  Column("created_by", Integer),
  Column("created_at", String(19)),
  Column("done_at", String(19)),
  Column("fault_category", String(60), server_default=text("''")),
  Column("fault_component", String(60), server_default=text("''")),
  Column("maint_action", String(60), server_default=text("''")),
  Column("rework_count", Integer, server_default=text("0")),
  Column("production_impact", String(30), server_default=text("''")),  # delta #2
  Column("accepted_at", String(19)))                    # delta #10

T("timelogs",
  Column("id", Integer, primary_key=True),
  Column("job_id", Integer),
  Column("tech", Integer),
  Column("seg_type", String(20)),
  Column("activity", String(120), server_default=text("''")),
  Column("start", String(19)),
  Column("end", String(19)),
  Column("pause_reason", String(120), server_default=text("''")))

T("shiftlogs",
  Column("id", Integer, primary_key=True),
  Column("log_date", String(10)),
  Column("shift", String(10)),
  Column("machine_id", Integer),
  Column("planned_min", Integer, server_default=text("0")),
  Column("output", Integer, server_default=text("0")),
  Column("good", Integer, server_default=text("0")),
  Column("reject", Integer, server_default=text("0")),
  Column("entered_by", Integer),
  Column("created_at", String(19)))

T("push_subs",
  Column("id", Integer, primary_key=True),
  Column("user_id", Integer),
  Column("sub", SAText))

T("channels",
  Column("id", Integer, primary_key=True),
  Column("name", String(120)),
  Column("kind", String(12), server_default=text("'team'")))

T("messages",
  Column("id", Integer, primary_key=True),
  Column("channel_id", Integer),
  Column("job_id", Integer),
  Column("parent_id", Integer),
  Column("author", Integer),
  Column("text", SAText, server_default=text("''")),
  Column("img", String(200), server_default=text("''")),
  Column("kind", String(10), server_default=text("'message'")),
  Column("created_at", String(19)))

T("activities",
  Column("id", Integer, primary_key=True),
  Column("job_id", Integer),
  Column("act_type", String(40)),
  Column("summary", String(200)),
  Column("assignee", Integer),
  Column("due_date", String(10)),
  Column("done", Integer, server_default=text("0")),
  Column("created_by", Integer),
  Column("created_at", String(19)))

T("spare_parts",                                        # delta #11 foundation
  Column("id", Integer, primary_key=True),
  Column("part_no", String(60)),
  Column("name", String(200)),
  Column("category", String(80), server_default=text("''")),
  Column("factory_id", Integer, server_default=text("2")),
  Column("stock", Integer, server_default=text("0")),                  # cached; part_moves = truth
  Column("min_stock", Integer, server_default=text("0")),
  Column("unit_cost", Float, server_default=text("0")),
  Column("location", String(80), server_default=text("''")))

T("part_moves",
  Column("id", Integer, primary_key=True),
  Column("part_id", Integer),
  Column("qty", Integer),
  Column("job_id", Integer),
  Column("moved_by", Integer),
  Column("at", String(19)))

T("requisitions",
  Column("id", Integer, primary_key=True),
  Column("part_id", Integer),
  Column("qty", Integer, server_default=text("1")),
  Column("status", String(20), server_default=text("'Waiting'")),
  Column("requester", Integer),
  Column("supplier", String(120), server_default=text("''")),
  Column("job_id", Integer),
  Column("created_at", String(19)))

T("signoffs",
  Column("id", Integer, primary_key=True),
  Column("job_id", Integer),
  Column("action", String(12)),
  Column("user_id", Integer),
  Column("signature", String(200), server_default=text("''")),
  Column("reason", SAText, server_default=text("''")),
  Column("created_at", String(19)))

# b406: who actually signed, when the login used was a department (shared) one or the
# person signing was picked from a list. The printed sheet shows this name.
T("sig_signers",
  Column("id", Integer, primary_key=True),
  Column("job_id", Integer),
  Column("kind", String(20)),
  Column("person_id", Integer),
  Column("person_name", String(120), server_default=text("''")),
  Column("login_id", Integer),
  Column("created_at", String(19)))

# ---------------- projects ----------------
# A project is a container with a promised finish date; its tasks are the work.
# The BASELINE (plan_start / plan_end on each task) is what was agreed on day one and
# never moves on its own — a baseline that gets quietly edited is not a baseline. Moving
# it is a deliberate act, stamped in baseline_at / baseline_by so the history survives.
T("projects",
  Column("id", Integer, primary_key=True),
  Column("factory_id", Integer, server_default=text("2")),
  Column("code", String(20)),                              # PRJ-YYMM-XXX
  Column("name", String(200)),
  Column("area", String(120), server_default=text("''")),
  Column("owner_id", Integer),
  Column("start_date", String(10)),
  Column("finish_date", String(10)),                       # the promised handover
  Column("status", String(16), server_default=text("'Planning'")),
  Column("notes", SAText, server_default=text("''")),
  Column("baseline_at", String(19)),                       # when the baseline was last stamped
  Column("baseline_by", Integer),
  Column("baseline_note", String(200), server_default=text("''")),
  Column("created_by", Integer),
  Column("created_at", String(19)))

# waits_for is the one field a planner has to type that the app cannot work out. Without
# it a task that has not started keeps its original dates however late the task before it
# ran, every un-started row reads "on time", and the page reports a handover date that
# cannot happen. It holds a project_tasks.id, or NULL for a task that waits for nothing.
T("project_tasks",
  Column("id", Integer, primary_key=True),
  Column("project_id", Integer),
  Column("seq", Integer, server_default=text("0")),
  Column("name", String(200)),
  Column("plan_start", String(10)),                        # baseline
  Column("plan_end", String(10)),
  Column("hours", Float, server_default=text("0")),        # planned man-hours
  Column("who", Integer),                                  # users.id
  Column("waits_for", Integer),                            # project_tasks.id
  Column("act_start", String(10)),                         # typed, or taken from the job
  Column("act_end", String(10)),
  Column("pct", Integer, server_default=text("0")),
  Column("job_id", Integer),                               # the PRJ work order, once raised
  # The three an Excel work plan carries that a Gantt does not. wbs is the number people
  # say out loud in the meeting ("item 4 is late"); phase is the band it sits under; and
  # remark is the column that ends up carrying the real story — "waiting PO", "vendor".
  Column("wbs", String(12), server_default=text("''")),
  Column("phase", String(60), server_default=text("''")),
  Column("remark", String(200), server_default=text("''")),
  Column("created_at", String(19)))

T("job_events",                                        # status-change log → cycle-time / hold timing
  Column("id", Integer, primary_key=True),
  Column("job_id", Integer),
  Column("status", String(24)),
  Column("user_id", Integer),
  Column("created_at", String(19)))


# ---------------- ?-placeholder Conn wrapper ----------------

_QMARK = re.compile(r"\?")
_JOBS_INSERT = re.compile(r"^\s*INSERT\s+INTO\s+jobs\s*\(", re.I)


class Row:
    __slots__ = ("_m",)

    def __init__(self, mapping):
        self._m = dict(mapping)

    def __getitem__(self, k):
        if isinstance(k, int):
            return list(self._m.values())[k]
        return self._m[k]

    def get(self, k, d=None):
        return self._m.get(k, d)

    def keys(self):
        return self._m.keys()

    def __contains__(self, k):
        return k in self._m

    def __iter__(self):
        return iter(self._m.values())


def _convert(sql, params):
    n = [0]

    def rep(_m):
        s = f":p{n[0]}"
        n[0] += 1
        return s

    sql2 = _QMARK.sub(rep, sql)
    return sql2, {f"p{i}": v for i, v in enumerate(params)}


class Result:
    def __init__(self, res):
        self._res = res

    def fetchone(self):
        r = self._res.fetchone()
        return Row(r._mapping) if r is not None else None

    def fetchall(self):
        return [Row(r._mapping) for r in self._res.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def rowcount(self):
        return self._res.rowcount


class Conn:
    def __init__(self):
        self._c = engine.connect()

    def _raw(self, sql, params=()):
        sql2, p = _convert(sql, list(params))
        return self._c.execute(text(sql2), p)

    def _job_insert(self, sql, params):
        # One job table per plant: an INSERT INTO jobs is written into the plant's own
        # table (see jobtables.py). None = not a job insert, run it as written.
        if _JOBS_INSERT.match(sql or ""):
            from .jobtables import route_insert
            return route_insert(self, self._raw, sql, params)
        return None

    def execute(self, sql, params=()):
        routed = self._job_insert(sql, params)
        if routed:
            return Result(routed[0])
        return Result(self._raw(sql, params))

    def executemany(self, sql, seq):
        for row in seq:
            self.execute(sql, row)

    def insert_id(self, sql, params=()):
        """Portable INSERT returning the new id."""
        routed = self._job_insert(sql, params)
        if routed:
            return routed[1]
        if _JOBS_INSERT.match(sql or "") and engine.dialect.name == "sqlite":
            from .jobtables import is_split, SEQ
            if is_split(self):
                # an insert the router could not take apart (literal values) went through
                # the view's trigger; lastrowid is meaningless on a view, the counter is not
                self._raw(sql, params)
                return self._raw("SELECT seq FROM sqlite_sequence WHERE name=?", [SEQ]).fetchone()[0]
        if engine.dialect.name == "postgresql":
            sql2, p = _convert(sql + " RETURNING id", list(params))
            return self._c.execute(text(sql2), p).fetchone()[0]
        sql2, p = _convert(sql, list(params))
        return self._c.execute(text(sql2), p).lastrowid

    def commit(self):
        self._c.commit()

    def close(self):
        self._c.close()


def get_conn():
    return Conn()


def create_schema():
    metadata.create_all(engine)

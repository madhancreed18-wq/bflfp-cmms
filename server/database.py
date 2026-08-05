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
    Integer, Float, String, Text as SAText

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
  Column("targets_json", SAText, server_default=text("''")))

T("users",
  Column("id", Integer, primary_key=True),
  Column("username", String(60), unique=True),
  Column("password", String(200)),
  Column("name", String(120)),
  Column("role", String(20)),
  Column("active", Integer, server_default=text("1")),
  Column("factory_id", Integer, server_default=text("2")))

T("machines",
  Column("id", Integer, primary_key=True),
  Column("code", String(40), unique=True),
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
  Column("last_pm_date", String(10), server_default=text("''")))

T("jobs",
  Column("id", Integer, primary_key=True),
  Column("jobid", String(30), unique=True),
  Column("jobtype", String(6)),
  Column("machine_id", Integer),
  Column("descr", SAText, server_default=text("''")),
  Column("priority", Integer, server_default=text("1")),
  Column("status", String(24), server_default=text("'Reported'")),
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


# ---------------- ?-placeholder Conn wrapper ----------------

_QMARK = re.compile(r"\?")


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

    def execute(self, sql, params=()):
        sql2, p = _convert(sql, list(params))
        return Result(self._c.execute(text(sql2), p))

    def executemany(self, sql, seq):
        for row in seq:
            self.execute(sql, row)

    def insert_id(self, sql, params=()):
        """Portable INSERT returning the new id."""
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

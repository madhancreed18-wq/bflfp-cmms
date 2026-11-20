"""System settings › Database (b583) — look inside cmms.db from the admin login.

READ-ONLY, ALWAYS. The browser opens its own SQLite connection in read-only mode
(`mode=ro` + `PRAGMA query_only`), so nothing typed here can change, add or delete a
row, whatever it says. It is for looking, the way SQL Server Management Studio's
object explorer and "Select top 1000 rows" are used — not for editing.

- GET  /api/admin/db/tables          every table and view, with row and column counts
- GET  /api/admin/db/table/{name}    columns, indexes, CREATE statement and one page of rows
                                     (search across all columns, sort by any column)
- POST /api/admin/db/query           run ONE SELECT / WITH / PRAGMA / EXPLAIN statement,
                                     up to 1000 rows, stopped after 15 seconds
- GET  /api/admin/db/export/{name}   a table or view as CSV (UTF-8, opens in Excel)

Passwords, password hashes, session tokens and keys are shown as ••••• in every result.
Every query run is written to the server log with who ran it.
Admin only. SQLite only (the normal set-up); with DATABASE_URL set it says so.
"""
import asyncio
import csv
import io
import logging
import os
import re
import sqlite3
import time

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse

from .auth import require_role
from .config import DATA

router = APIRouter(prefix="/api/admin/db")
_log = logging.getLogger("cmms")

DB_FILE = os.path.join(DATA, "cmms.db")
MAX_ROWS = 1000
TIME_LIMIT = 15.0
# columns whose values never leave the server, in any result
SECRET = re.compile(r"(pass(word)?|pwd|hash|salt|token|secret|api_?key|private|vapid|otp|pin_code)", re.I)
READ_FIRST = ("select", "with", "pragma", "explain", "values")


def _admin(req):
    return require_role(req, "admin")


def _con(limit=TIME_LIMIT):
    if os.environ.get("DATABASE_URL") and not os.environ["DATABASE_URL"].startswith("sqlite"):
        raise HTTPException(501, "The database viewer works with the SQLite set-up only")
    if not os.path.exists(DB_FILE):
        raise HTTPException(404, "cmms.db not found")
    con = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True, timeout=5,
                          check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    # abort a query that runs too long instead of tying up the server;
    # _restart(con) gives the next statement its own full allowance
    _T0[id(con)] = time.monotonic()
    con.set_progress_handler(lambda: 1 if time.monotonic() - _T0.get(id(con), 0) > limit else 0, 20000)
    return con


_T0 = {}


def _restart(con):
    _T0[id(con)] = time.monotonic()


def _objects(con):
    return {r["name"]: r["type"] for r in con.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table','view')"
        " AND name NOT LIKE 'sqlite_%'")}


def _qi(name):
    return '"' + str(name).replace('"', '""') + '"'


def _mask(cols, rows):
    hide = [i for i, c in enumerate(cols) if SECRET.search(str(c or ""))]
    out = []
    for r in rows:
        r = list(r)
        for i in hide:
            if r[i] not in (None, ""):
                r[i] = "•••••"
        for i, v in enumerate(r):
            if isinstance(v, (bytes, bytearray, memoryview)):
                r[i] = f"<binary {len(bytes(v))} bytes>"
        out.append(r)
    return out, [cols[i] for i in hide]


@router.get("/tables")
def tables(req: Request):
    _admin(req)
    con = _con(30)
    try:
        out = []
        for name, typ in sorted(_objects(con).items(), key=lambda x: (x[1] != "table", x[0].lower())):
            try:
                cols = len(con.execute(f"PRAGMA table_info({_qi(name)})").fetchall())
            except Exception:
                cols = 0
            try:
                _restart(con)
                n = con.execute(f"SELECT COUNT(*) FROM {_qi(name)}").fetchone()[0]
            except Exception:
                n = None
            out.append({"name": name, "type": typ, "rows": n, "cols": cols})
        size = os.path.getsize(DB_FILE)
        wal = DB_FILE + "-wal"
        return {"tables": out, "file": DB_FILE, "size": size,
                "wal": os.path.getsize(wal) if os.path.exists(wal) else 0,
                "sqlite": sqlite3.sqlite_version}
    finally:
        con.close()


@router.get("/table/{name}")
def table(name: str, req: Request, page: int = 0, size: int = 100, q: str = "",
                sort: str = "", dir: str = "asc"):
    _admin(req)
    size = max(10, min(int(size or 100), 500))
    con = _con()
    try:
        objs = _objects(con)
        if name not in objs:
            raise HTTPException(404, "no such table")
        info = [dict(r) for r in con.execute(f"PRAGMA table_info({_qi(name)})")]
        cols = [c["name"] for c in info]
        idx = []
        try:
            for r in con.execute(f"PRAGMA index_list({_qi(name)})"):
                icols = [x["name"] for x in con.execute(f"PRAGMA index_info({_qi(r['name'])})")]
                idx.append({"name": r["name"], "unique": bool(r["unique"]), "cols": icols})
        except Exception:
            pass
        ddl = (con.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone() or [""])[0] or ""
        where, args = "", []
        words = [w for w in (q or "").split() if w]
        if words:
            # every word must appear somewhere in the row (any column)
            one = " || ' ' || ".join(f"COALESCE(CAST({_qi(c)} AS TEXT),'')" for c in cols
                                     if not SECRET.search(c))
            where = " WHERE " + " AND ".join(f"({one}) LIKE ?" for _ in words)
            args = [f"%{w}%" for w in words]
        order = ""
        if sort in cols:
            order = f" ORDER BY {_qi(sort)} {'DESC' if dir == 'desc' else 'ASC'}"
        elif objs[name] == "table":
            order = " ORDER BY rowid DESC"          # newest rows first
        total = con.execute(f"SELECT COUNT(*) FROM {_qi(name)}{where}", args).fetchone()[0]
        cur = con.execute(f"SELECT * FROM {_qi(name)}{where}{order} LIMIT ? OFFSET ?",
                          (*args, size, max(0, page) * size))
        rcols = [d[0] for d in cur.description]
        rows, hidden = _mask(rcols, cur.fetchall())
        return {"name": name, "type": objs[name], "columns": info, "indexes": idx, "ddl": ddl,
                "total": total, "page": page, "size": size, "cols": rcols, "rows": rows,
                "masked": hidden}
    except sqlite3.OperationalError as e:
        raise HTTPException(400, str(e))
    finally:
        con.close()


def _one_read_statement(sql):
    s = re.sub(r"--[^\n]*", " ", sql or "")
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S).strip().rstrip(";").strip()
    if not s:
        raise HTTPException(400, "empty query")
    if not sqlite3.complete_statement(s + ";") or ";" in s:
        raise HTTPException(400, "one statement at a time, please")
    if s.split(None, 1)[0].lower() not in READ_FIRST:
        raise HTTPException(400, "read-only: only SELECT, WITH, PRAGMA or EXPLAIN can be run here")
    if s.split(None, 1)[0].lower() == "pragma" and "=" in s:
        raise HTTPException(400, "read-only: a PRAGMA can be read here, not set")
    return s


@router.post("/query")
async def query(req: Request):
    u = _admin(req)
    b = await req.json()
    sql = _one_read_statement(b.get("sql", ""))
    # the query runs on a worker thread, so a slow one never holds up the rest of the app
    return await asyncio.to_thread(_run, u, sql)


def _run(u, sql):
    con = _con()
    t0 = time.monotonic()
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in (cur.description or [])]
        data = cur.fetchmany(MAX_ROWS + 1)
        more = len(data) > MAX_ROWS
        rows, hidden = _mask(cols, data[:MAX_ROWS])
        ms = int((time.monotonic() - t0) * 1000)
        _log.info("db viewer: %s ran (%d rows, %d ms): %s", u.get("name") or u.get("id"),
                  len(rows), ms, sql[:500])
        return {"cols": cols, "rows": rows, "more": more, "ms": ms, "masked": hidden}
    except sqlite3.OperationalError as e:
        msg = str(e)
        if "interrupted" in msg:
            msg = f"stopped after {int(TIME_LIMIT)} seconds — narrow the query (WHERE / LIMIT)"
        raise HTTPException(400, msg)
    except sqlite3.DatabaseError as e:
        raise HTTPException(400, str(e))
    finally:
        con.close()


@router.get("/export/{name}")
def export(name: str, req: Request):
    u = _admin(req)
    con = _con(120)
    objs = _objects(con)
    if name not in objs:
        con.close()
        raise HTTPException(404, "no such table")
    _log.info("db viewer: %s exported %s", u.get("name") or u.get("id"), name)

    def gen():
        try:
            cur = con.execute(f"SELECT * FROM {_qi(name)}")
            cols = [d[0] for d in cur.description]
            buf = io.StringIO()
            w = csv.writer(buf)
            buf.write("﻿")
            w.writerow(cols)
            while True:
                chunk = cur.fetchmany(2000)
                if not chunk:
                    break
                rows, _ = _mask(cols, chunk)
                w.writerows(rows)
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
            if buf.tell():
                yield buf.getvalue()
        finally:
            con.close()

    return StreamingResponse(gen(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})

import io, sys
P = sys.argv[1]
s = io.open(P, encoding="utf-8").read()
def rep(old, new, cnt=1):
    global s
    assert s.count(old) == cnt, ("not found / not unique", old[:90], s.count(old))
    s = s.replace(old, new)

# 1. archive table for EVERY Access table / column (nothing from the old system is lost)
rep('''    # b497: every pallet-slip print, one row per slip per print, by user (reprint audit)''',
'''    # b501: the whole old Access database, every table and every column, row by row as JSON
    c.execute("""CREATE TABLE IF NOT EXISTS fg_access_raw(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER, tbl TEXT, row_key TEXT, sort_key TEXT DEFAULT '',
        data TEXT DEFAULT '', imported_at TEXT DEFAULT '', UNIQUE(factory_id, tbl, row_key))""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_fg_raw_t ON fg_access_raw(factory_id, tbl, sort_key)")
    # b497: every pallet-slip print, one row per slip per print, by user (reprint audit)''')

# 2. readers: all tables, not just the 7 the app uses
rep('''def read_csv_folder(folder):
    out = {}
    import gzip
    for t in ACCESS_TABLES:
        p = os.path.join(folder, t + ".csv")
        if os.path.exists(p):
            with open(p, encoding="utf-8-sig", newline="") as f:
                out[t] = list(csv.DictReader(f))
        elif os.path.exists(p + ".gz"):
            with gzip.open(p + ".gz", "rt", encoding="utf-8-sig", newline="") as f:
                out[t] = list(csv.DictReader(f))
    return out''',
'''def _open_csv(p):
    import gzip
    op = gzip.open if p.lower().endswith(".gz") else open
    for enc in ("utf-8-sig", "cp874"):            # Access 'Export Text' on a Thai PC writes cp874 (TIS-620)
        try:
            with op(p, "rt", encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    with op(p, "rt", encoding="utf-8", errors="replace", newline="") as f:
        return list(csv.DictReader(f))


def read_csv_folder(folder):
    """b501: every *.csv / *.csv.gz / *.txt in the folder = one Access table (file name = table name)."""
    out = {}
    if not os.path.isdir(folder):
        return out
    for fn in sorted(os.listdir(folder)):
        low = fn.lower()
        for ext in (".csv.gz", ".csv", ".txt"):
            if low.endswith(ext):
                t = fn[: -len(ext)]
                if t and t not in out:
                    out[t] = _open_csv(os.path.join(folder, fn))
                break
    return out''')
rep('''    out = {}
    cur = cn.cursor()
    for t in ACCESS_TABLES:
        cur.execute(f"SELECT * FROM [{t}]")''',
'''    out = {}
    cur = cn.cursor()
    names = [r.table_name for r in cur.tables(tableType="TABLE") if not r.table_name.startswith(("MSys", "~"))]  # b501: all tables
    for t in names:
        cur.execute(f"SELECT * FROM [{t}]")''')

# 3. archive at the end of every import
rep('''    n["release"] = k
    c.commit()
    log(f"[fg] Access import into plant {fac}: "''',
'''    n["release"] = k
    n["archive (all tables)"] = archive_access(c, fac, data)
    c.commit()
    log(f"[fg] Access import into plant {fac}: "''')
rep('''# ================================================================ dashboard (Warehouse home)''',
'''_DATE_COLS = ("ReceiveDate", "ReleaseDate", "Date", "LogDate", "Log", "CreateDate", "UpdateDate", "DocDate")


def _sort_key(r):
    """Newest-first ordering for the archive viewer: a date column if the table has one, else the ID."""
    for k in _DATE_COLS:
        v = _d(r.get(k) or "")
        if v:
            return v + " " + _t(r.get("ReceiveTime") or r.get("ReleaseTime") or r.get("Time") or "") + f" {int(_num(r.get('ID'))):012d}"
    i = r.get("ID") or r.get("Id") or r.get("id")
    return f"{int(_num(i)):012d}" if i not in (None, "") and str(i).strip().lstrip("-").isdigit() else ""


def archive_access(c, fac, data):
    """b501: keep EVERY row of EVERY Access table, all columns, so no old data is lost — even the
    tables and columns the FG app does not use. Keyed on the Access ID (or the row itself when a
    table has no ID), so importing again only adds new rows."""
    import hashlib
    t0, added = now(), 0
    for t, rows in data.items():
        for i, r in enumerate(rows):
            js = json.dumps(r, ensure_ascii=False, sort_keys=True)
            idv = r.get("ID") if "ID" in r else (r.get("Id") if "Id" in r else r.get("id"))
            key = ("id:" + str(idv).strip()) if idv not in (None, "") else ("h:" + hashlib.sha1(js.encode("utf-8")).hexdigest())
            cur = c.execute("INSERT OR IGNORE INTO fg_access_raw(factory_id,tbl,row_key,sort_key,data,imported_at) VALUES(?,?,?,?,?,?)",
                            (fac, t, key, _sort_key(r), js, t0))
            added += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return added


@router.get("/access-archive")
async def access_archive_tables(req: Request):
    u, fac, _ = ctx(req, "admin")
    with closing(conn()) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT tbl, COUNT(*) n, MAX(imported_at) last FROM fg_access_raw WHERE factory_id=? GROUP BY tbl ORDER BY tbl", (fac,))]
    return {"tables": rows}


@router.get("/access-archive/rows")
async def access_archive_rows(req: Request, tbl: str, q: str = "", page: int = 1, size: int = 200, all: int = 0):
    """Rows of one archived Access table, newest first."""
    u, fac, _ = ctx(req, "admin")
    conds, args = ["factory_id=?", "tbl=?"], [fac, tbl]
    if q:
        conds.append("data LIKE ?")
        args.append("%" + q + "%")
    w = " AND ".join(conds)
    size = 100000 if all else max(20, min(int(size or 200), 1000))
    page = max(1, int(page or 1))
    with closing(conn()) as c:
        total = c.execute(f"SELECT COUNT(*) FROM fg_access_raw WHERE {w}", tuple(args)).fetchone()[0]
        rows = [json.loads(r["data"]) for r in c.execute(
            f"SELECT data FROM fg_access_raw WHERE {w} ORDER BY sort_key DESC, id DESC LIMIT {size} OFFSET {(page - 1) * size}", tuple(args))]
    cols = []
    for r in rows[:500]:
        for k in r:
            if k not in cols:
                cols.append(k)
    return {"rows": rows, "cols": cols, "total": total, "page": page, "size": size}


# ================================================================ dashboard (Warehouse home)''')

# 4. newest on top everywhere a list is shown
rep('''            rows = c.execute("SELECT * FROM fg_release WHERE factory_id=? AND status='waiting' ORDER BY id", (fac,)).fetchall()''',
    '''            rows = c.execute("SELECT * FROM fg_release WHERE factory_id=? AND status='waiting' ORDER BY id DESC", (fac,)).fetchall()''')
rep('''                "SELECT * FROM fg_receive WHERE factory_id=? AND status='waiting' ORDER BY id", (fac,))]''',
    '''                "SELECT * FROM fg_receive WHERE factory_id=? AND status='waiting' ORDER BY id DESC", (fac,))]''')
rep('''    rows.sort(key=lambda r: (r["product_id"], r["mfg"] or r["lot_no"] or ""))''',
    '''    rows.sort(key=lambda r: (r["mfg"] or r["lot_no"] or "", r["product_id"]), reverse=True)   # b501: newest lot on top''')
rep('''            "SELECT action,note,user_name,created_at FROM fg_log WHERE factory_id=? AND kind=? AND ref_id=? ORDER BY id",''',
    '''            "SELECT action,note,user_name,created_at FROM fg_log WHERE factory_id=? AND kind=? AND ref_id=? ORDER BY id DESC",''')
rep('''            "SELECT action,note,user_name,created_at FROM fg_log WHERE factory_id=? AND kind='packing' AND ref_id=? ORDER BY id",''',
    '''            "SELECT action,note,user_name,created_at FROM fg_log WHERE factory_id=? AND kind='packing' AND ref_id=? ORDER BY id DESC",''')
rep('''            "SELECT copy_no,reprint,reason,user_name,printed_at FROM fg_print_log WHERE factory_id=? AND packing_id=? ORDER BY id",''',
    '''            "SELECT copy_no,reprint,reason,user_name,printed_at FROM fg_print_log WHERE factory_id=? AND packing_id=? ORDER BY id DESC",''')

io.open(P, "w", encoding="utf-8", newline="\n").write(s)
print("ok")

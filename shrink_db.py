"""b543 — make data/cmms.db small again (run ONCE, with the app STOPPED).

1. checks the app is not running (port 8000)
2. backs up cmms.db to data/backups/cmms-before-shrink-<time>.db
3. moves the raw Access archive (fg_access_raw) into data/fg_access_archive.db
   (the app also does this by itself at start-up; this script just makes sure)
4. VACUUM — gives the empty space back, so the file actually gets smaller

    python shrink_db.py            (live server: run in D:\\bflfp-cmms)
"""
import os, shutil, socket, sqlite3, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("BFLFP_DATA") or os.path.join(HERE, "data")
DB = os.path.join(DATA, "cmms.db")
ARC = os.path.join(DATA, "fg_access_archive.db")
PORT = int(os.environ.get("PORT", "8000"))
mb = lambda p: os.path.getsize(p) / 1048576 if os.path.exists(p) else 0


def app_running():
    s = socket.socket()
    s.settimeout(1)
    try:
        return s.connect_ex(("127.0.0.1", PORT)) == 0
    finally:
        s.close()


if app_running():
    sys.exit(f"The app is still running on port {PORT}. Stop it first, then run this again.")
if not os.path.exists(DB):
    sys.exit(f"Not found: {DB}")

print(f"cmms.db before: {mb(DB):.1f} MB")
os.makedirs(os.path.join(DATA, "backups"), exist_ok=True)
bak = os.path.join(DATA, "backups", time.strftime("cmms-before-shrink-%Y%m%d-%H%M%S.db"))
print(f"backup -> {bak}")
src = sqlite3.connect(DB)
dst = sqlite3.connect(bak)
src.backup(dst)            # safe copy, also takes anything still in the -wal file
dst.close()

c = src
has = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fg_access_raw'").fetchone()
if has:
    n = c.execute("SELECT COUNT(*) FROM fg_access_raw").fetchone()[0]
    print(f"moving {n:,} raw Access rows -> {ARC}")
    a = sqlite3.connect(ARC)
    a.execute("""CREATE TABLE IF NOT EXISTS fg_access_raw(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER, tbl TEXT, row_key TEXT, sort_key TEXT DEFAULT '',
        data TEXT DEFAULT '', imported_at TEXT DEFAULT '', UNIQUE(factory_id, tbl, row_key))""")
    a.execute("CREATE INDEX IF NOT EXISTS ix_fg_raw_t ON fg_access_raw(factory_id, tbl, sort_key)")
    last = 0
    while True:
        rows = c.execute("SELECT id,factory_id,tbl,row_key,sort_key,data,imported_at FROM fg_access_raw"
                         " WHERE id>? ORDER BY id LIMIT 5000", (last,)).fetchall()
        if not rows:
            break
        a.executemany("INSERT OR IGNORE INTO fg_access_raw(id,factory_id,tbl,row_key,sort_key,data,imported_at)"
                      " VALUES(?,?,?,?,?,?,?)", rows)
        a.commit()
        last = rows[-1][0]
        print(f"  … {last:,}", end="\r")
    have = a.execute("SELECT COUNT(*) FROM fg_access_raw").fetchone()[0]
    a.close()
    if have < n:
        sys.exit(f"\nSTOPPED: archive has {have:,} of {n:,} rows — nothing was deleted. Send this message to Claude.")
    c.execute("DROP TABLE fg_access_raw")
    c.commit()
    print(f"\nmoved OK — archive file {mb(ARC):.1f} MB")
else:
    print("raw Access archive already moved out")

print("VACUUM … (can take a few minutes)")
c.execute("VACUUM")
c.close()
print(f"cmms.db after: {mb(DB):.1f} MB")
print("Done. Start the app again.")

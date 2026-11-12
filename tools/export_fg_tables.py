"""Copy FG Stock tables from data/cmms.db into a small data/fg_export.db (read-only on the source).
Run from the project folder:  python tools\\export_fg_tables.py
"""
import os, sqlite3, sys

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = os.path.join(root, "data", "cmms.db")
dst = os.path.join(root, "data", "fg_export.db")
if os.path.exists(dst):
    os.replace(dst, dst + ".old")

con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
con.execute(f"ATTACH DATABASE ? AS x", (dst,))
names = [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE 'fg\\_%' ESCAPE '\\' OR name IN ('product_master','users'))")]
for n in names:
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name=?", (n,)).fetchone()[0]
    con.execute(sql.replace(f"CREATE TABLE {n}", f"CREATE TABLE x.{n}", 1).replace(f'CREATE TABLE "{n}"', f'CREATE TABLE x."{n}"', 1))
    if n == "users":  # names/roles only, no passwords
        cols = [c[1] for c in con.execute("PRAGMA table_info(users)") if "pass" not in c[1].lower() and "hash" not in c[1].lower() and "sig" not in c[1].lower()]
        con.execute("DROP TABLE x.users")
        con.execute(f"CREATE TABLE x.users AS SELECT {','.join(cols)} FROM main.users")
    else:
        con.execute(f'INSERT INTO x."{n}" SELECT * FROM main."{n}"')
    print(n, con.execute(f'SELECT COUNT(*) FROM x."{n}"').fetchone()[0])
con.commit()
con.close()
print("OK ->", dst, round(os.path.getsize(dst) / 1e6, 1), "MB")

"""Show which tables make data/cmms.db big (read-only).  Run:  python db_size.py"""
import os, sqlite3, sys

path = sys.argv[1] if len(sys.argv) > 1 else os.path.join("data", "cmms.db")
c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
print(f"file: {path}  {os.path.getsize(path) / 1048576:.1f} MB")
ps = c.execute("pragma page_size").fetchone()[0]
free = c.execute("pragma freelist_count").fetchone()[0] * ps / 1048576
print(f"empty space inside the file (VACUUM gives it back): {free:.1f} MB\n")

rows = []
for (t,) in c.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'").fetchall():
    cols = [r[1] for r in c.execute(f'pragma table_info("{t}")')]
    if not cols:
        continue
    expr = "+".join(f'coalesce(length(CAST("{x}" AS BLOB)),0)' for x in cols)
    n, b = c.execute(f'select count(*), coalesce(sum({expr}),0) from "{t}"').fetchone()
    rows.append((b, n, t))

rows.sort(reverse=True)
print(f"{'table':32s}{'rows':>12s}{'data MB':>10s}")
for b, n, t in rows[:15]:
    print(f"{t:32s}{n:12,d}{b / 1048576:10.1f}")
print("\n(data MB = data only; indexes add more on top)")

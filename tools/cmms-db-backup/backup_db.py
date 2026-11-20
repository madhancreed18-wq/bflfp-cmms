"""Stand-alone backup of the CMMS database (cmms.db) — separate from the CMMS app.

Safe while the app is running: it uses SQLite's own online-backup, which copies a
consistent snapshot (including changes still in cmms.db-wal). It only READS the live
database; it never writes to it.

Each run:
  1. copies cmms.db  ->  backups\\cmms-YYYYMMDD-HHMMSS.db   (one clean file, no -wal/-shm)
  2. checks the copy (PRAGMA integrity_check)
  3. writes a short review file next to it: row counts per table + latest dates
  4. zips the copy into ONE file per weekday:  backups\\cmms-1-Mon.zip … cmms-7-Sun.zip
     Next week the same day's file is overwritten, so there are never more than 7 files
     and the disk use stays flat. The newest copy is also kept unzipped as
     latest\\cmms-latest.db  for review.
Everything is logged to BACKUP_DIR\\backup_log.txt.

Nightly: Windows Task Scheduler runs backup_db.bat (see the command in the bat file).
"""
import datetime as dt
import os
import shutil
import sqlite3
import sys
import zipfile

SOURCE = r"D:\bflfp-cmms\data\cmms.db"         # the live database (read only)
# Where the backups go: OUTSIDE the CMMS folder, so a copy of the CMMS folder never
# drags every backup along with it. Change this one line to use another drive or a NAS.
BACKUP_DIR = r"D:\cmms-db-backup"
DEST = os.path.join(BACKUP_DIR, "backups")       # zipped backups
LATEST = os.path.join(BACKUP_DIR, "latest")      # newest copy, unzipped, for review
# one backup per weekday, overwritten the next week -> always 7 files at most

# tables worth a glance in the review file (missing ones are skipped)
DATE_COLS = {
    "jobs": "created_at", "fg_packing": "packed_at", "fg_receive": "receive_date",
    "fg_release": "release_date", "machines": None, "users": None, "pm_plans": None,
}


def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with open(os.path.join(BACKUP_DIR, "backup_log.txt"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def review(path, out_txt):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    views = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    lines = [f"Backup review  {os.path.basename(path)}",
             f"made {dt.datetime.now():%Y-%m-%d %H:%M:%S}   size {os.path.getsize(path)/1048576:,.1f} MB", ""]
    lines.append("Key tables:")
    for t, col in DATE_COLS.items():
        if t not in tables and t not in views:
            continue
        n = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        extra = ""
        if col:
            try:
                last = c.execute(f'SELECT MAX("{col}") FROM "{t}"').fetchone()[0]
                extra = f"   latest {col}: {last}"
            except sqlite3.Error:
                pass
        lines.append(f"  {t:<14}{n:>10,} rows{extra}")
    lines += ["", "All tables (rows):"]
    for t in tables:
        try:
            n = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        except sqlite3.Error as e:
            n = f"error: {e}"
        lines.append(f"  {t:<40}{n:>12}" if isinstance(n, str) else f"  {t:<40}{n:>12,}")
    c.close()
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    if not os.path.isfile(SOURCE):
        log(f"ERROR  database not found: {SOURCE}")
        return 1
    os.makedirs(DEST, exist_ok=True)
    os.makedirs(LATEST, exist_ok=True)
    stamp = f"{dt.datetime.now():%Y%m%d-%H%M%S}"
    copy = os.path.join(DEST, f"cmms-{stamp}.db")
    part = copy + ".part"

    # 1 - online backup (read-only on the source)
    src = sqlite3.connect(f"file:{SOURCE}?mode=ro", uri=True, timeout=60)
    dst = sqlite3.connect(part)
    src.backup(dst, pages=2000)          # copies in steps, so the app is never blocked for long
    dst.execute("PRAGMA journal_mode=DELETE")   # the copy is one plain file
    dst.commit()
    # 2 - check the copy
    ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
    dst.close()
    src.close()
    if ok != "ok":
        log(f"ERROR  copy failed the integrity check ({ok}) - kept as {part} for inspection")
        return 2
    os.replace(part, copy)
    mb = os.path.getsize(copy) / 1048576

    # 3 - review file
    rev = os.path.join(DEST, f"cmms-{stamp}-review.txt")
    review(copy, rev)

    # 4 - newest copy unzipped for review, everything zipped
    shutil.copy2(copy, os.path.join(LATEST, "cmms-latest.db"))
    shutil.copy2(rev, os.path.join(LATEST, "cmms-latest-review.txt"))
    now = dt.datetime.now()
    zpath = os.path.join(DEST, f"cmms-{now.isoweekday()}-{now:%a}.zip")   # cmms-1-Mon.zip … cmms-7-Sun.zip
    with zipfile.ZipFile(zpath + ".part", "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.write(copy, os.path.basename(copy))
        z.write(rev, os.path.basename(rev))
    os.replace(zpath + ".part", zpath)
    os.remove(copy)
    os.remove(rev)
    zmb = os.path.getsize(zpath) / 1048576

    log(f"OK     cmms.db {mb:,.1f} MB -> {os.path.basename(zpath)} ({zmb:,.1f} MB, replaces last week's), integrity ok")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"ERROR  {type(e).__name__}: {e}")
        sys.exit(3)

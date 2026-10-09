"""Test helper: give the auto/PM jobs a planned date of YESTERDAY so the
job tree shows multiple day-groups. Your operator reports keep their own date.

Run once, from the project folder:   python set_test_dates.py
Then pull-to-refresh (or reopen) the app.
"""
import sqlite3, datetime
from server.config import DB_PATH

c = sqlite3.connect(DB_PATH)
c.row_factory = sqlite3.Row
rows = c.execute("SELECT id, jobsource, planned_date FROM jobs").fetchall()

dates = [r["planned_date"] for r in rows if r["planned_date"]]
today = max(dates) if dates else datetime.date.today().isoformat()
yest = (datetime.date.fromisoformat(today) - datetime.timedelta(days=1)).isoformat()

move = [r["id"] for r in rows if r["jobsource"] != "OperatorReport"]
c.executemany("UPDATE jobs SET planned_date=? WHERE id=?", [(yest, i) for i in move])
c.commit()

print(f"Moved {len(move)} auto/PM jobs to {yest}. Operator reports stay on {today}.")
for r in c.execute("SELECT planned_date, COUNT(*) n FROM jobs GROUP BY planned_date ORDER BY planned_date DESC"):
    print("  ", r["planned_date"], "->", r["n"], "jobs")
c.close()

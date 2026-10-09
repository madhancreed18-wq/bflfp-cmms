# -*- coding: utf-8 -*-
"""Put เอฟ (techpc15, id 119) back into the 16 day-teams that went to Raja (admin2, id 143)
when the old เอฟ person record was deleted on 26 Sep 2026.

Only these 16 plan_teams rows (BFLPC) are touched, and on each only the 143 seat
(lead and/or member) is swapped for 119. The team's name, colour, phone login and
everything else stay as they are. No jobs, time logs or signatures were moved by that
delete, so nothing else needs putting back.

    python fix_ef_teams.py              # check only, changes nothing
    python fix_ef_teams.py --apply      # copies data/cmms.db first, then fixes
"""
import os, shutil, sqlite3, sys, datetime

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cmms.db")
FROM_ID, TO_ID = 143, 119
TEAM_IDS = [193, 265, 297, 370, 467, 522, 718, 727, 807, 1012, 1126, 1201, 1406, 1427, 1454, 1463]


def swap(lst, a, b):
    ids = [x.strip() for x in str(lst or "").split(",") if x.strip()]
    out = []
    for x in ids:
        x = str(b) if x == str(a) else x
        if x not in out:
            out.append(x)
    return ",".join(out)


def main():
    apply = "--apply" in sys.argv
    if not os.path.exists(DB):
        sys.exit("database not found: " + DB)
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    to = c.execute("SELECT id,username,name,active FROM users WHERE id=?", (TO_ID,)).fetchone()
    if not to or to["username"] != "techpc15":
        sys.exit("STOP: user 119 is not techpc15 on this database — nothing changed")
    print(f"moving to: {to['name']} ({to['username']}, id {TO_ID})\n")
    todo = []
    for tid in TEAM_IDS:
        r = c.execute("SELECT id,factory_id,day,name,lead,members FROM plan_teams WHERE id=?", (tid,)).fetchone()
        if not r:
            print(f"  team {tid}: not found — skipped"); continue
        nm = swap(r["members"], FROM_ID, TO_ID)
        nl = TO_ID if r["lead"] == FROM_ID else r["lead"]
        if nm == (r["members"] or "") and nl == r["lead"]:
            print(f"  {r['day']}  {r['name']:<8} already fixed / no Raja seat — skipped"); continue
        print(f"  {r['day']}  {r['name']:<8} lead {r['lead']}→{nl}  members {r['members']}→{nm}")
        todo.append((nm, nl, tid))
    print(f"\n{len(todo)} team(s) to fix")
    if not apply:
        print("check only — run again with --apply to write"); return
    if todo:
        bak = DB + ".before-ef-fix-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(DB, bak); print("backup:", bak)
        c.executemany("UPDATE plan_teams SET members=?, lead=? WHERE id=?", todo)
        c.commit(); print("done — fixed", len(todo))


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""BFLPC register Rev.05 (SD-SP-ENG02-02) - apply ONLY the agreed changes.

The full master import would also wipe every electrical (yellow) mark - Rev.05 carries
none - and drop 10 screw dischargers and 4 machines out of PM. So this script makes the
agreed changes by hand and touches nothing else:

  1  19 new LIQUID VALVE machines (LIQUID ROOM, floor 1)
  2  new checklist LIQUID VALVE, 3 weekly checks, those 19 bound to it
  3  COATER MIXER  weekly drive-chain check  -> reworded (alignment added)
  4  EXTRUDER      weekly cutter-housing check -> reworded (alignment added)
  5  BOILER CHAIN GRATE  + weekly strainer check; 3-monthly flange/valve check retired
                         (active=0, kept for history)
  6  ROTARY DISTRIBUTOR  cleaning before hand-over: 6-monthly -> every run

Not done on purpose: the AIR LOCK "AIR LOCK ใต้ CYCLONE #1" row - it is a machine name
typed in the check-item column (no frequency), not a check.

Existing electrical marks, issued jobs, CE assignments and every other machine are left
exactly as they are. Items are edited in place, so answers already given stay attached.

    python deploy\\update_pc_rev05.py data\\cmms.db            (dry run - writes nothing)
    python deploy\\update_pc_rev05.py data\\cmms.db --apply    (STOP THE APP FIRST)
"""
import json, os, sqlite3, sys
from datetime import datetime

FAC = 3
NOW = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
WHO = (None, "Rev.05 update")

VALVES = [  # code, name   (group LIQUID VALVE, LIQUID ROOM, floor 1)
    ("LI1BVP01", "วาล์วลงถังน้ำมันไก่"), ("LI1BVP01.3", "วาล์วน้ำมันไก่ก่อนฉีดพรีคอน"),
    ("LI1BVP02", "วาล์วน้ำมันพรีคอนไลน์1"), ("LI1BVP03", "วาล์วน้ำมันพรีคอนไลน์2"),
    ("LI1BVP04", "วาล์วน้ำมันพรีคอนไลน์3"), ("LI2BVP4.1", "วาล์วใต้ถังสเกล13"),
    ("LI1BVP05", "วาล์วน้ำมันไก่ลงสเกล12"), ("LI1BVP06", "วาล์วน้ำมันทาโล่ลงสเกล12"),
    ("LI1BVP08", "วาล์วBL02ลงสเกล12"), ("LI1BVP09", "วาล์วBL01ลงสเกล12"),
    ("LI1BVP10", "วาล์วใต้สเกล12"), ("LI2BVP01", "วาล์วน้ำมัน19ศลงสเกล13"),
    ("LI2BVP02", "วาล์วไดเจทแมวลงสเกล13"), ("LI2BVP04", "วาล์วก่อนลงถังสเกลไดเจทไลน์1"),
    ("LI2BVP05", "วาล์วก่อนลงถังสเกลไดเจทไลน์2-3"), ("LI2BVP09", "วาล์วน้ำมันT20"),
    ("LI2BVP10", "วาล์วน้ำมันT25"), ("LI2BVP11", "วาล์วก่อนลงถังสเกลน้ำมันไลน์1-2"),
    ("LI2BVP11.1", "วาล์วใต้ถังสเกล14")]
LV_ITEMS = ["ตรวจเช็คสภาพโครงสร้างของวาล์วและจุดรั่ว",
            "ตรวจเช็คการทำงานของโซลีนอยล์วาล์ว",
            "ตรวจเช็คการทำงานของวาล์วหมุนตามการสั่งงาน"]
CLEAN = "การเคลียร์สิ่งแปลกปลอม/ความสะอาดก่อนส่งมอบทุกครั้งหลังเสร็จงาน"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    if not args or not os.path.exists(args[0]):
        print(__doc__); return 2
    dbp = args[0]
    c = sqlite3.connect(dbp); c.row_factory = sqlite3.Row
    print("=" * 70); print(("APPLYING" if apply else "DRY RUN") + " - BFLPC Rev.05 changes"); print("=" * 70)
    tpl = {r["machine_type"]: r["id"] for r in c.execute(
        "SELECT id, machine_type FROM pm_templates WHERE factory_id=?", (FAC,))}
    have = {r["code"]: r["id"] for r in c.execute("SELECT id, code FROM machines WHERE factory_id=?", (FAC,))}
    todo = []                                     # (description, fn)

    def item(mt, text, freq=None):
        q = ("SELECT i.* FROM pm_items i WHERE i.template_id=? AND TRIM(i.item)=?"
             + (" AND i.freq=?" if freq else "") + " AND COALESCE(i.active,1)=1")
        return c.execute(q, (tpl.get(mt), text) + ((freq,) if freq else ())).fetchone()

    def log(tid, iid, action, before, after):
        try:
            c.execute("INSERT INTO pm_item_log(factory_id,template_id,item_id,action,before,after,user_id,user_name,at)"
                      " VALUES(?,?,?,?,?,?,?,?,?)", (FAC, tid, iid, action, before, after, WHO[0], WHO[1], NOW))
        except sqlite3.OperationalError:
            pass

    # 1+2  machines + checklist
    new = [(cd, nm) for cd, nm in VALVES if cd not in have]
    print("\n1  new machines: %d of %d (%d already there)" % (len(new), len(VALVES), len(VALVES) - len(new)))
    for cd, nm in new: print("     + %-11s %s" % (cd, nm))
    print("2  checklist LIQUID VALVE: " + ("already there" if "LIQUID VALVE" in tpl else "created, 3 weekly checks"))

    def do_valves():
        for cd, nm in new:
            c.execute("INSERT INTO machines(code,name,asset_group,line,floor,manufacturer,size,factory_id,active,category)"
                      " VALUES(?,?,?,?,?,?,?,?,1,'Machine')", (cd, nm, "LIQUID VALVE", "LIQUID ROOM", "1", "", "", FAC))
        if "LIQUID VALVE" not in tpl:
            cur = c.execute("INSERT INTO pm_templates(factory_id,machine_type,interval_days,active,last_gen,form_no,color)"
                            " VALUES(?,?,30,1,'','SD-SP-ENG02-02','')", (FAC, "LIQUID VALVE"))
            tid = cur.lastrowid
            for i, t in enumerate(LV_ITEMS, 1):
                r = c.execute("INSERT INTO pm_items(template_id,seq,item,normal_status,method,freq,img,active,elec)"
                              " VALUES(?,?,?,?,?,?,?,1,0)", (tid, i, t, "", "เช็ค", "weekly", ""))
                log(tid, r.lastrowid, "add", "", json.dumps({"item": t, "freq": "weekly"}, ensure_ascii=False))
        else:
            tid = tpl["LIQUID VALVE"]
        for cd, _ in VALVES:
            mid = c.execute("SELECT id FROM machines WHERE factory_id=? AND code=?", (FAC, cd)).fetchone()["id"]
            if not c.execute("SELECT 1 FROM pm_members WHERE template_id=? AND machine_id=?", (tid, mid)).fetchone():
                c.execute("DELETE FROM pm_members WHERE machine_id=? AND factory_id=?", (mid, FAC))
                c.execute("INSERT INTO pm_members(template_id,machine_id,factory_id) VALUES(?,?,?)", (tid, mid, FAC))
    todo.append(do_valves)

    # 3..6  checklist edits
    def reword(mt, old, newt, freq):
        r = item(mt, old, freq)
        if not r:
            done = item(mt, newt, freq)
            print("   %-20s %s" % (mt, "already done" if done else "! NOT FOUND - skipped: " + old)); return
        print("   %-20s ~ %s  ->  %s" % (mt, old, newt))
        def f():
            c.execute("UPDATE pm_items SET item=? WHERE id=?", (newt, r["id"]))
            log(r["template_id"], r["id"], "edit", json.dumps({"item": old}, ensure_ascii=False),
                json.dumps({"item": newt}, ensure_ascii=False))
        todo.append(f)

    print("\n3-6  checklist changes")
    reword("COATER MIXER", "ตรวจสอบสภาพเช็คความตึงของโซ่ขับ", "ตรวจสอบAlinmentและสภาพความตึงของโซ่ขับ", "weekly")
    reword("EXTRUDER", "ตรวจสอบชุดป้อมมีด", "ตรวจสอบชุดป้อมมีดและเช็คAlinement", "weekly")

    # boiler: add weekly strainer check at seq 10, retire the 3-monthly flange/valve check
    b_new, b_old = "ตรวจสภาพสแตนเนอร์และล้าง", "ตรวจสภาพ น๊อตหน้าแปลน / valve"
    if item("BOILER CHAIN GRATE", b_new, "weekly"):
        print("   %-20s already done" % "BOILER CHAIN GRATE")
    else:
        old = item("BOILER CHAIN GRATE", b_old, "q3m")
        print("   %-20s + %s (weekly)" % ("BOILER CHAIN GRATE", b_new))
        print("   %-20s - %s (3-month) %s" % ("", b_old, "retired, kept for history" if old else "! not found"))
        def f():
            tid = tpl["BOILER CHAIN GRATE"]
            seq = old["seq"] if old else 99
            r = c.execute("INSERT INTO pm_items(template_id,seq,item,normal_status,method,freq,img,active,elec)"
                          " VALUES(?,?,?,?,?,?,?,1,0)", (tid, seq, b_new, "", "เช็ค", "weekly", ""))
            log(tid, r.lastrowid, "add", "", json.dumps({"item": b_new, "freq": "weekly"}, ensure_ascii=False))
            if old:
                c.execute("UPDATE pm_items SET active=0 WHERE id=?", (old["id"],))
                log(tid, old["id"], "delete", json.dumps({"item": b_old, "freq": "q3m"}, ensure_ascii=False), "")
        todo.append(f)

    r = item("ROTARY DISTRIBUTOR", CLEAN, "m6")
    if r:
        print("   %-20s ~ %s: 6-month -> every run" % ("ROTARY DISTRIBUTOR", "cleaning before hand-over"))
        def f():
            c.execute("UPDATE pm_items SET freq='each_op' WHERE id=?", (r["id"],))
            log(r["template_id"], r["id"], "edit", '{"freq": "m6"}', '{"freq": "each_op"}')
        todo.append(f)
    else:
        print("   %-20s already done" % "ROTARY DISTRIBUTOR")

    print("\nleft alone: electrical marks, issued jobs, CE assignments, all other machines")
    if not apply:
        print("\nDRY RUN - nothing written. Stop the app, then re-run with --apply."); return 0
    bak = dbp + ".before-rev05-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = sqlite3.connect(bak); c.backup(dst); dst.close()
    print("\nbackup: " + bak)
    try:
        for f in todo:
            f()
        c.commit()
    except Exception:
        c.rollback(); raise
    n = c.execute("SELECT COUNT(*) FROM pm_members m JOIN pm_templates t ON t.id=m.template_id"
                  " WHERE t.factory_id=? AND t.machine_type='LIQUID VALVE'", (FAC,)).fetchone()[0]
    print("done - LIQUID VALVE checklist now has %d machines. Start the app." % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())

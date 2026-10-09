"""Add the electrical (งานไฟฟ้า) symptom list to the operator's problem picker.

45 entries, all category Electrical, added to EVERY factory (or one, with
--factory). An entry whose name already exists in that factory is skipped, so
the script can be run twice without doubling the list.

    python add_problem_types.py                 # dry run — shows what would change
    python add_problem_types.py --apply         # write, after backing up the DB
    python add_problem_types.py --apply --factory 2
    python add_problem_types.py --apply --retire-old-electrical
        also switches OFF (active=0) the old seeded Electrical entries, so the
        dropdown shows only this list for electrical work. Jobs that already
        reference an old entry keep their words — nothing is deleted.

Run it ON the machine that owns the database, with the server stopped,
using the venv Python. Never over a network share.
"""
import argparse, os, shutil, sqlite3, sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("BFLFP_DATA") or os.path.join(BASE, "data")
DB = os.path.join(DATA, "cmms.db")

ITEMS = [
    "ไฟฟ้าไม่เข้าเครื่องจักร",
    "เครื่องจักรไฟฟ้าไม่ทำงาน",
    "มอเตอร์ไม่ทำงาน",
    "มอเตอร์หมุนผิดปกติ",
    "มอเตอร์ร้อนผิดปกติ",
    "มอเตอร์เสียงดัง / สั่นผิดปกติ",
    "เบรกเกอร์ตัด / เบรกเกอร์ Trip",
    "ฟิวส์ขาด",
    "ไฟฟ้าลัดวงจร",
    "ไฟตก / ไฟเกิน",
    "ไฟฟ้า 3 เฟสผิดปกติ",
    "ไฟฟ้าขาดเฟส",
    "คอนแทคเตอร์ไม่ทำงาน",
    "รีเลย์ไม่ทำงาน",
    "โอเวอร์โหลดตัด",
    "Power Supply เสีย",
    "ระบบควบคุมไฟฟ้าขัดข้อง",
    "ปุ่มกดไฟฟ้าเสีย",
    "Emergency Stop ผิดปกติ",
    "Limit Switch ผิดปกติ",
    "Sensor ไม่ทำงาน",
    "Proximity Sensor ผิดปกติ",
    "Photo Sensor ผิดปกติ",
    "Encoder ผิดปกติ",
    "PLC ขัดข้อง",
    "PLC Error / Alarm",
    "PLC Input / Output ผิดปกติ",
    "HMI ขัดข้อง",
    "Inverter ขัดข้อง",
    "Inverter Alarm",
    "Servo ขัดข้อง",
    "Heater ไม่ทำงาน",
    "Temperature Control ผิดปกติ",
    "SSR เสีย",
    "ตู้ควบคุมไฟฟ้าขัดข้อง",
    "สายไฟชำรุด / ขั้วสายหลวม",
    "ไฟส่องสว่างเสีย",
    "ระบบ Safety ไฟฟ้าขัดข้อง",
    "ระบบ Interlock ขัดข้อง",
    "ระบบสื่อสาร PLC / Network ขัดข้อง",
    "ไฟฟ้ามีกลิ่นไหม้ / ความร้อนผิดปกติ",
    "พบประกายไฟ / เสียงผิดปกติจากไฟฟ้า",
    "ไฟแสดงสถานะไม่ทำงาน",
    "ระบบไฟฟ้าขัดข้อง ไม่ทราบสาเหตุ",
    "งานซ่อมไฟฟ้าอื่น ๆ",
]


def norm(s):
    return " ".join(str(s or "").split()).lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--factory", type=int, help="one factory id (BFL 1 · FP 2 · PC 3); default all")
    ap.add_argument("--retire-old-electrical", action="store_true",
                    help="switch off the previously seeded Electrical entries")
    a = ap.parse_args()

    if not os.path.exists(DB):
        sys.exit(f"database not found: {DB}")
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    # same DDL the server uses (db.ensure_problem_types) — a no-op when it exists
    c.execute("""CREATE TABLE IF NOT EXISTS problem_types(
        id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER,
        name TEXT DEFAULT '', category TEXT DEFAULT 'Other',
        seq INTEGER DEFAULT 0, active INTEGER DEFAULT 1)""")

    facs = [a.factory] if a.factory else \
           [r["id"] for r in c.execute("SELECT id FROM factories ORDER BY id")]

    plan, retire = [], []
    for fac in facs:
        have = {norm(r["name"]): r for r in c.execute(
            "SELECT * FROM problem_types WHERE factory_id=?", (fac,))}
        top = c.execute("SELECT COALESCE(MAX(seq),0) FROM problem_types"
                        " WHERE factory_id=?", (fac,)).fetchone()[0]
        new_norms = {norm(n) for n in ITEMS}
        for i, name in enumerate(ITEMS):
            r = have.get(norm(name))
            if r:
                if not r["active"]:
                    plan.append(("reactivate", fac, r["id"], name))
            else:
                top += 1
                plan.append(("add", fac, top, name))
        if a.retire_old_electrical:
            for r in c.execute("SELECT * FROM problem_types WHERE factory_id=?"
                               " AND category='Electrical' AND active=1", (fac,)):
                if norm(r["name"]) not in new_norms:
                    retire.append((fac, r["id"], r["name"]))

    adds = [p for p in plan if p[0] == "add"]
    reacts = [p for p in plan if p[0] == "reactivate"]
    print(f"factories: {facs}")
    print(f"to add: {len(adds)}   to reactivate: {len(reacts)}   to retire: {len(retire)}")
    for _, fac, _, name in adds[:8]:
        print(f"  + f{fac}  {name}")
    if len(adds) > 8:
        print(f"  … and {len(adds)-8} more")
    for fac, _, name in retire:
        print(f"  - f{fac}  retire: {name}")

    if not a.apply:
        print("\ndry run — nothing written. Add --apply to write.")
        return
    bak = DB + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(DB, bak)
    print("backup:", bak)
    for op in plan:
        if op[0] == "add":
            _, fac, seq, name = op
            c.execute("INSERT INTO problem_types(factory_id,name,category,seq,active)"
                      " VALUES(?,?,?,?,1)", (fac, name, "Electrical", seq))
        else:
            _, fac, pid, name = op
            c.execute("UPDATE problem_types SET active=1 WHERE id=?", (pid,))
    for fac, pid, name in retire:
        c.execute("UPDATE problem_types SET active=0 WHERE id=?", (pid,))
    c.commit()
    print("done. Restart is not needed; phones re-read the list on next app open.")


if __name__ == "__main__":
    main()

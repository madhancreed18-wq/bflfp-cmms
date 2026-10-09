# -*- coding: utf-8 -*-
"""Bring the BFLPC (Petcare) plant up from nothing, out of its own master workbook.

BFLPC has 731 machines in the register and nothing else: no PM checklists, no users, no
PM start date. Every screen in the app works per factory already — the Technician page,
the Operator page, the Planner board, the Manager dashboard, the repair request
(ใบแจ้งซ่อม), the Daily report, the PM checklist and the PM Plan are all code that runs
for whichever plant you are signed into. None of it needs writing. What is missing is
the data behind it, and that is what this script puts in.

    1  ASSETS      797 rows from the MList sheet — code, name, group, สถานที่ตั้ง
                   (location), ชั้นที่ (floor), maker, size. 67 of them are new; the rest
                   are updated. Only those seven fields are written, so criticality, KPI
                   class, serial numbers and anything else already on a machine survive.

    2  PM PLAN     57 group checklists, 421 items, every machine bound to its checklist
                   by the workbook itself rather than guessed from its name. Machines on
                   no group sheet go to the PENDING template: visible, generating nothing.

    3  USERS       One account per role — planner, technician, operator, manager — so
                   every screen and every document can actually be walked. They are
                   created the way the app creates an assign-only record: a real account
                   with an unusable password, so nobody can sign in until a planner opens
                   each one in the Users screen and sets a password. No password is
                   invented here and none is printed.

    4  PM START    The date PM scheduling counts from. Without it the planner's PM screen
                   has nothing to generate against and stays empty.

Run it with no arguments first. It prints everything it would do and writes nothing:

    python deploy/setup_bflpc.py  data\\cmms.db  "BFLPC Doc\\บัญชีรายชื่อ...2569.xlsx"
    python deploy/setup_bflpc.py  data\\cmms.db  "BFLPC Doc\\บัญชีรายชื่อ...2569.xlsx" --apply

**Stop the app before --apply.** SQLite is a file; a running server holds it open, and a
half-written import is worse than no import. A timestamped backup is taken first either
way, beside the database.
"""
import os
import secrets
import shutil
import sqlite3
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FACTORY = "PC"

# One per role. Usernames follow the BFLFP pattern (planner1 / tech1 / oper1 / manager1)
# with a pc suffix, so the two plants' test accounts can never be confused for each other.
USERS = [
    ("pcplanner1", "คุณวางแผน PC (Planner)", "planner"),
    ("pctech1", "ช่าง PC 1 (Technician)", "technician"),
    ("pcoper1", "พนักงานผลิต PC (Operator)", "operator"),
    ("pcmanager1", "ผู้จัดการโรงงาน PC (Manager)", "manager"),
]

# What an asset row is allowed to write. Deliberately short: this workbook is the
# authority on identity and where a machine lives, and on nothing else.
ASSET_FIELDS = ("name", "asset_group", "line", "floor", "manufacturer", "size")


def _die(msg):
    print("STOP — " + msg)
    sys.exit(2)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    if len(args) < 2:
        print(__doc__)
        return 1
    dbpath, xlsx = args[0], args[1]
    for p in (dbpath, xlsx):
        if not os.path.exists(p):
            _die("not found: " + p)

    try:
        from server import pm_import_master as MST
    except ImportError as e:
        _die("run this from the repo root — %s" % e)

    raw = open(xlsx, "rb").read()
    wb = MST._load(raw)
    is_master = MST.looks_like_master(wb)
    wb.close()
    if not is_master:
        _die("that workbook is not a machine-register master (no กลุ่มเครื่องจักร sheets).\n"
             "       Expected: BFLPC Doc\\บัญชีรายชื่อเครื่องมือเครื่องจักรPet Care 2569.xlsx")

    c = sqlite3.connect(dbpath)
    c.row_factory = sqlite3.Row
    f = c.execute("SELECT id,code,name FROM factories WHERE code=?", (FACTORY,)).fetchone()
    if not f:
        _die("no factory with code %s in this database" % FACTORY)
    fac = f["id"]
    print("=" * 78)
    print("%s  —  %s   (factory_id %d)" % ("APPLYING" if apply else "DRY RUN", f["name"], fac))
    print("database: %s" % os.path.abspath(dbpath))
    print("workbook: %s" % os.path.basename(xlsx))
    print("=" * 78)

    # ── 1. assets ──────────────────────────────────────────────────────────────────
    mlist = MST.read_mlist(MST._load(raw))
    if not mlist:
        _die("that workbook has no MList sheet, so there are no assets to load")
    have = {r["code"]: r["id"] for r in
            c.execute("SELECT id,code FROM machines WHERE factory_id=?", (fac,))}
    new = [m for m in mlist if m["code"] not in have]
    print("\n1  ASSETS")
    print("   MList rows           %d" % len(mlist))
    print("   already in register  %d  (updated: name, group, location, floor, maker, size)"
          % (len(mlist) - len(new)))
    print("   new                  %d  (created)" % len(new))
    for m in new[:6]:
        print("        + %-9s %-30s %s ชั้น %s" % (m["code"], m["name"][:30], m["loc"], m["floor"]))
    if len(new) > 6:
        print("        + … and %d more" % (len(new) - 6))
    gone = [k for k in have if k not in {m["code"] for m in mlist}]
    if gone:
        print("   in the register but NOT in MList (left untouched, decide separately):")
        for k in gone:
            print("        ? %s" % k)

    # ── 2. PM plan ─────────────────────────────────────────────────────────────────
    plan, _ = MST.plan_from_workbook(raw, fac)
    s = MST.summary(plan["templates"], mlist)
    pend = next((t for t in plan["templates"] if t["machine_type"] == MST.PENDING), None)
    notes = plan.get("notes") or {}
    print("\n2  PM PLAN")
    print("   form no              %s" % (plan["form_no"] or "—"))
    print("   checklists           %d" % s["templates"])
    print("   check items          %d" % s["items"])
    print("   machines bound       %d  (stated by the workbook, not guessed)" % s["machines"])
    print("   PENDING (no group)   %d  → no PM until they are added to a group sheet"
          % s["pending"])
    print("   PM jobs per year     %d  ≈ %d per working day"
          % (s["pm_jobs_per_year"], round(s["pm_jobs_per_year"] / 306)))
    if s.get("hour_based_filed_yearly"):
        print("   filed yearly         %d supplier items due by running hours, which this"
              % s["hour_based_filed_yearly"])
        print("                        app cannot schedule — a yearly reminder instead of none")
    for d in notes.get("misplaced", []):
        print("   ! %s row: %s typed in the wrong column — ignored, already listed correctly"
              % (d["sheet"], d["code"]))
    for d in notes.get("doubled", []):
        print("   ! %s is listed on %s as well — kept on the first sheet only"
              % (d["code"], d["sheet"]))
    if pend:
        print("   PENDING machines:")
        for cd in pend["members"]:
            print("        · %s" % cd)

    # ── 3. users ───────────────────────────────────────────────────────────────────
    print("\n3  USERS   (BFLPC currently has %d)"
          % c.execute("SELECT COUNT(*) n FROM users WHERE factory_id=?", (fac,)).fetchone()["n"])
    todo = []
    for un, nm, role in USERS:
        if c.execute("SELECT 1 FROM users WHERE username=?", (un,)).fetchone():
            print("   = %-12s %-28s %-11s already exists, left alone" % (un, nm, role))
        else:
            todo.append((un, nm, role))
            print("   + %-12s %-28s %-11s created, PASSWORD NOT SET" % (un, nm, role))
    if todo:
        print("   Each is created with an unusable password — the same thing the app does for")
        print("   an assign-only record. Nobody can sign in until a planner or admin opens the")
        print("   user in Settings → Users and sets one. No password is invented by this script.")

    # ── 4. PM start date ───────────────────────────────────────────────────────────
    cfg = c.execute("SELECT start_date,active FROM pm_config WHERE factory_id=?",
                    (fac,)).fetchone()
    # next Monday: a PM programme that starts mid-week produces a short first week and a
    # planner who thinks the numbers are wrong
    d = date.today()
    monday = (d + timedelta(days=(7 - d.weekday()) % 7 or 7)).isoformat()
    print("\n4  PM START DATE")
    if cfg:
        print("   already set to %s (active=%s) — left alone" % (cfg["start_date"], cfg["active"]))
    else:
        print("   not set → %s (next Monday), scheduling switched on" % monday)

    if not apply:
        print("\n" + "=" * 78)
        print("DRY RUN — nothing was written. Stop the app, then re-run with --apply.")
        return 0

    # ── write ──────────────────────────────────────────────────────────────────────
    bak = dbpath + ".before-bflpc-" + __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(dbpath, bak)
    print("\nbackup written: %s" % bak)

    ins = upd = 0
    for m in mlist:
        rec = (m["name"], m["group"], m["loc"], m["floor"], m["maker"], m["size"])
        if m["code"] in have:
            c.execute("UPDATE machines SET %s WHERE id=?"
                      % ",".join(k + "=?" for k in ASSET_FIELDS),
                      (*rec, have[m["code"]]))
            upd += 1
        else:
            c.execute("INSERT INTO machines(code,%s,factory_id,active,category) "
                      "VALUES(?,%s,?,1,?)"
                      % (",".join(ASSET_FIELDS), ",".join("?" * len(ASSET_FIELDS))),
                      (m["code"], *rec, fac, "Machine"))
            ins += 1
    c.commit()
    print("assets: %d created, %d updated" % (ins, upd))

    # the plan goes in through the app's own code, so the import behaves here exactly as
    # it does when a planner uploads the same workbook in the browser
    os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.abspath(dbpath))
    from contextlib import closing
    from server import pm as PM
    from server.db import db, hash_pw, now
    with closing(db()) as cc:
        PM._ensure(cc)
        res = PM._apply_plan(cc, fac, plan, False)
        cc.commit()
    print("pm plan: %d templates, %d items, %d machines bound"
          % (res["templates"], res["items"], res.get("bound_from_file", res["pins"])))
    if res.get("not_in_register_n"):
        print("   %d codes in the workbook are still not in the register: %s"
              % (res["not_in_register_n"], ", ".join(res["not_in_register"][:10])))

    for un, nm, role in todo:
        c.execute("""INSERT INTO users(username,password,name,role,active,factory_id,
                     department,can_login) VALUES(?,?,?,?,1,?,'',1)""",
                  (un, hash_pw(secrets.token_hex(16)), nm, role, fac))
    if todo:
        c.commit()
        print("users: %d created, all with no usable password" % len(todo))

    if not cfg:
        c.execute("INSERT INTO pm_config(factory_id,start_date,active) VALUES(?,?,1)",
                  (fac, monday))
        c.commit()
        print("pm start date: %s" % monday)

    # ── what it looks like now ─────────────────────────────────────────────────────
    print("\nafter:")
    for t, q in (("machines", "SELECT COUNT(*) n FROM machines WHERE factory_id=?"),
                 ("users", "SELECT COUNT(*) n FROM users WHERE factory_id=?"),
                 ("pm_templates", "SELECT COUNT(*) n FROM pm_templates WHERE factory_id=?"),
                 ("pm_members", "SELECT COUNT(*) n FROM pm_members WHERE factory_id=?"),
                 ("problem_types", "SELECT COUNT(*) n FROM problem_types WHERE factory_id=?")):
        print("   %-14s %d" % (t, c.execute(q, (fac,)).fetchone()["n"]))
    c.close()
    print("\nDone. Start the app, sign in as admin, switch to BFLPC, and set a password")
    print("on each of the new users before anyone tries to use them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

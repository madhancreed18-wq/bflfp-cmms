# -*- coding: utf-8 -*-
"""Load a plant's machine-register master workbook: assets, PM checklists, PM start date.

This is `setup_bflpc.py` with the plant taken off the top of the file and put on the
command line, because the second plant needed exactly the same thing and a copied script
is a script that drifts. Both BFLPC and BFL are filed on the same form — a sheet per
machine GROUP, the machines and the checklist as two independent lists side by side —
so one reader and one loader serve both.

    1  ASSETS      every MList row: code, name, group, สถานที่ตั้ง (location), ชั้นที่
                   (floor), maker, size. Only those seven fields are written, so
                   criticality, KPI class, serial numbers and anything else already on a
                   machine survive the import.

    2  PM PLAN     one checklist per group sheet, with every machine bound to its
                   checklist BY THE WORKBOOK rather than guessed from its name. Machines
                   on no group sheet go to the PENDING template: visible in the PM
                   screen, generating nothing, and moved to their real group by the next
                   import once somebody adds them to that sheet.

    3  USERS       only with --users, and only ever one account per role, created the way
                   the app creates an assign-only record: a real account with an unusable
                   password that nobody can sign in to until a planner sets one. A plant
                   that already has its own people does not want four test logins, which
                   is why this is off by default.

    4  PM START    the date PM scheduling counts from. Without it the planner's PM screen
                   has nothing to generate against and stays empty.

Run it with no --apply first. It prints everything it would do and writes nothing:

    python deploy/setup_plant.py BFL data\\cmms.db "BFL Master\\SD-SP-ENG02-01 ... .xlsm"
    python deploy/setup_plant.py BFL data\\cmms.db "BFL Master\\SD-SP-ENG02-01 ... .xlsm" --apply

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

# What an asset row is allowed to write. Deliberately short: this workbook is the
# authority on identity and where a machine lives, and on nothing else.
ASSET_FIELDS = ("name", "asset_group", "line", "floor", "manufacturer", "size")


def _die(msg):
    print("STOP — " + msg)
    sys.exit(2)


def _users_for(code):
    """One account per role, prefixed with the plant so two plants' test accounts can
    never be mistaken for each other."""
    p = code.lower()
    return [(p + "planner1", "คุณวางแผน %s (Planner)" % code, "planner"),
            (p + "tech1", "ช่าง %s 1 (Technician)" % code, "technician"),
            (p + "oper1", "พนักงานผลิต %s (Operator)" % code, "operator"),
            (p + "manager1", "ผู้จัดการโรงงาน %s (Manager)" % code, "manager")]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    want_users = "--users" in sys.argv
    if len(args) < 3:
        print(__doc__)
        return 1
    code, dbpath, xlsx = args[0].upper(), args[1], args[2]
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
             "       It is the wrong document — the BFLFP PM Plan book has its own importer\n"
             "       in the app, under PM plan → import.")

    c = sqlite3.connect(dbpath)
    c.row_factory = sqlite3.Row
    f = c.execute("SELECT id,code,name FROM factories WHERE code=?", (code,)).fetchone()
    if not f:
        have = [r["code"] for r in c.execute("SELECT code FROM factories ORDER BY id")]
        _die("no factory with code %s in this database — it has %s" % (code, ", ".join(have)))
    fac = f["id"]
    print("=" * 78)
    print("%s  —  %s  (%s, factory_id %d)"
          % ("APPLYING" if apply else "DRY RUN", f["name"], code, fac))
    print("database: %s" % os.path.abspath(dbpath))
    print("workbook: %s" % os.path.basename(xlsx))
    print("=" * 78)

    # ── 1. assets ──────────────────────────────────────────────────────────────────
    mlist = MST.read_mlist(MST._load(raw))
    if not mlist:
        _die("that workbook has no MList sheet, so there are no assets to load")
    have = {r["code"]: r["id"] for r in
            c.execute("SELECT id,code FROM machines WHERE factory_id=?", (fac,))}
    # `machines.code` is UNIQUE across the WHOLE register, not per plant, so a code this
    # workbook claims may already belong to a different plant's machine. That is a real
    # conflict — two machines wearing one asset code — and not something an importer may
    # decide: creating it is impossible, and updating it would rename the other plant's
    # asset out from under it. Both are refused and the row is reported instead.
    mine = {m["code"] for m in mlist}
    shared = {r["code"]: r for r in c.execute(
        "SELECT m.code, m.name, f.code fac FROM machines m"
        " LEFT JOIN factories f ON f.id=m.factory_id WHERE m.factory_id!=?", (fac,))
        if r["code"] in mine}
    # Whether a code shared with another plant is a CONFLICT or just a coincidence
    # depends on the register's own constraint, so ask it rather than assume. A database
    # still carrying the old global UNIQUE(code) physically cannot hold both, and trying
    # would abort the import halfway; one that has been through the per-factory migration
    # holds them side by side, which is what a three-plant company actually needs.
    _ddl = (c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='machines'"
                      ).fetchone() or [""])[0] or ""
    per_factory = "UNIQUE (code)" not in _ddl
    elsewhere = {} if per_factory else shared
    mlist = [m for m in mlist if m["code"] not in elsewhere]
    new = [m for m in mlist if m["code"] not in have]
    print("\n1  ASSETS")
    print("   MList rows           %d" % len(mlist))
    print("   already in register  %d  (updated: name, group, location, floor, maker, size)"
          % (len(mlist) - len(new)))
    print("   new                  %d  (created)" % len(new))
    for m in new[:8]:
        print("        + %-9s %-30s %s ชั้น %s" % (m["code"], m["name"][:30], m["loc"], m["floor"]))
    if len(new) > 8:
        print("        + … and %d more" % (len(new) - 8))
    if shared:
        by_code = {m["code"]: m for m in MST.read_mlist(MST._load(raw))}
        if per_factory:
            print("   · %d code(s) are also used by another plant for a different machine."
                  % len(shared))
            print("     That is allowed — an asset code is unique within a plant, not across")
            print("     the company — and both are kept. Listed so it is never a surprise:")
        else:
            print("   ! %d code(s) in this workbook already belong to ANOTHER plant — SKIPPED."
                  % len(shared))
            print("     This database still has the old company-wide unique code, so it cannot")
            print("     hold both. Restart the app once on the current build: it migrates the")
            print("     register to one code per plant, and these load on the next run.")
        for cd, r in sorted(shared.items()):
            print("        %s %-9s here: %-26s" % ("·" if per_factory else "!", cd,
                                                   str(by_code.get(cd, {}).get("name", ""))[:26]))
            print("          %-9s %-4s: %s" % ("", r["fac"] or "?", r["name"]))

    gone = [k for k in have if k not in {m["code"] for m in mlist}]
    if gone:
        print("   in the register but NOT in MList (left untouched, decide separately):")
        for k in gone[:12]:
            print("        ? %s" % k)
        if len(gone) > 12:
            print("        ? … and %d more" % (len(gone) - 12))

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
        by_code = {m["code"]: m for m in mlist}
        print("   PENDING machines — each one names the group it belongs to in MList, so the")
        print("   fix is to add it to that group's own sheet and import again:")
        for cd in pend["members"]:
            m = by_code.get(cd, {})
            print("        · %-9s %-28s → %s" % (cd, str(m.get("name", ""))[:28],
                                                 m.get("group", "—")))

    # ── 3. users ───────────────────────────────────────────────────────────────────
    n_users = c.execute("SELECT COUNT(*) n FROM users WHERE factory_id=?", (fac,)).fetchone()["n"]
    print("\n3  USERS   (%s currently has %d)" % (code, n_users))
    todo = []
    if not want_users:
        print("   skipped — pass --users to create one test account per role")
    else:
        for un, nm, role in _users_for(code):
            if c.execute("SELECT 1 FROM users WHERE username=?", (un,)).fetchone():
                print("   = %-12s %-28s %-11s already exists, left alone" % (un, nm, role))
            else:
                todo.append((un, nm, role))
                print("   + %-12s %-28s %-11s created, PASSWORD NOT SET" % (un, nm, role))
        if todo:
            print("   Each is created with an unusable password — the same thing the app does for")
            print("   an assign-only record. Nobody can sign in until a planner or admin opens the")
            print("   user in Settings → Users and sets one. No password is invented here.")

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
    import datetime as _dt
    bak = dbpath + ".before-%s-" % code.lower() + _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
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
    from server.db import db, hash_pw
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
    print("\nDone. Start the app, sign in, switch to %s and open PM plan." % code)
    return 0


if __name__ == "__main__":
    sys.exit(main())

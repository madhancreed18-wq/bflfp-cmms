"""check_phones.py - which jobs reach which technician's phone. READ-ONLY.

    .venv\\Scripts\\python.exe check_phones.py                 all plants, tomorrow
    .venv\\Scripts\\python.exe check_phones.py PC 2026-09-22   one plant, one day

Does exactly what a technician's phone does: a login sees the work led or helped on by
itself and by every technician record whose login_id points at it. Work booked to a
technician record that points at NO login falls into the list every phone of the plant
shows. Nothing is written; the database is opened read-only.
"""
import datetime
import os
import sqlite3
import sys

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cmms.db")
if not os.path.exists(DB):
    DB = r"D:\bflfp-cmms\data\cmms.db"
args = [a for a in sys.argv[1:]]
ONLY = next((a.upper() for a in args if not a[:1].isdigit()), None)
DAY = next((a for a in args if a[:1].isdigit()),
           (datetime.date.today() + datetime.timedelta(days=1)).isoformat())
OPEN = ("Assigned", "InProgress", "Paused", "Rework", "Hold")      # the phone's "My jobs"

c = sqlite3.connect("file:" + DB.replace("\\", "/") + "?mode=ro", uri=True)
c.row_factory = sqlite3.Row
q = lambda s, a=(): c.execute(s, a).fetchall()
W = "COALESCE((SELECT m.factory_id FROM machines m WHERE m.id=j.machine_id), j.factory_id)"
print("database:", DB, "  day checked:", DAY)

for f in q("SELECT id, code FROM factories ORDER BY id"):
    if ONLY and f["code"].upper() != ONLY:
        continue
    fac = f["id"]
    ppl = q("SELECT id,name,username,login_id FROM users WHERE active=1 AND role='technician'"
            " AND COALESCE(can_login,1)=0 AND factory_id=? ORDER BY name", (fac,))
    acc = q("SELECT id,name,username FROM users WHERE active=1 AND role='technician'"
            " AND COALESCE(can_login,1)=1 AND factory_id=? ORDER BY username", (fac,))
    accname = {a["id"]: a for a in acc}
    print("\n" + "=" * 78)
    print(f"{f['code']}  -  {len(ppl)} technician people, {len(acc)} technician logins (phones)")

    # --- 1. does this plant link each person to their own login by name? -------------
    def key(n): return (n or "").strip().lower()
    pk, ak = {}, {}
    for p in ppl: pk.setdefault(key(p["name"]), []).append(p)
    for a in acc: ak.setdefault(key(a["name"]), []).append(a)
    pairs = {p[0]["id"]: ak[k][0]["id"] for k, p in pk.items()
             if k and k in ak and len(p) == 1 and len(ak[k]) == 1}
    own = bool(ppl) and len(pairs) == len(ppl)
    print("  mode:", "ONE LOGIN PER TECHNICIAN (linked by name)" if own else
          "SHARED PHONES (each person points at a crew handset)")
    if not own and pairs:
        print(f"  !! {len(pairs)} of {len(ppl)} people match a login by name - the plant needs ALL of")
        print("     them to switch to one-login-per-technician. Not matched:")
        for k, p in pk.items():
            if not (k in ak and len(p) == 1 and len(ak[k]) == 1):
                why = ("no login with this name" if k not in ak else
                       "name used by more than one person" if len(p) > 1 else
                       "name used by more than one login")
                print(f"       person  {p[0]['name']!r:22} {why}")
        for k, a in ak.items():
            if k not in pk:
                print(f"       login   {a[0]['username']:10} {a[0]['name']!r:20} no person with this name")

    # --- 2. every person, and the phone their work goes to ---------------------------
    print("\n  person               -> phone that shows their work")
    nophone = []
    for p in ppl:
        a = accname.get(p["login_id"])
        where = (f"{a['username']} ({a['name']})" if a else
                 "NO PHONE - shows on every phone of the plant" if not p["login_id"] else
                 f"login #{p['login_id']} - not an active technician login of this plant")
        flag = ""
        if a and own and pairs.get(p["id"]) != a["id"]:
            flag = "   !! should be " + accname[pairs[p["id"]]]["username"]
        if not a:
            nophone.append(p["id"])
        print(f"   {p['name'][:20]:20} -> {where}{flag}")

    # --- 3. what each phone will list ------------------------------------------------
    jobs = q(f"""SELECT j.id, j.jobid, j.status, j.planned_date, j.lead_tech, COALESCE(j.helpers,'') h
                 FROM jobs j WHERE {W}=? AND j.status IN ({','.join('?'*len(OPEN))})""",
             (fac, *OPEN))
    ph_of = {p["id"]: p["login_id"] for p in ppl}
    print(f"\n  phone      open jobs   of which planned {DAY}")
    for a in acc:
        ids = {a["id"]} | {p["id"] for p in ppl if p["login_id"] == a["id"]}
        mine = [j for j in jobs if j["lead_tech"] in ids
                or ids & {int(x) for x in j["h"].split(",") if x.strip().isdigit()}]
        day = sum(1 for j in mine if j["planned_date"] == DAY)
        print(f"   {a['username']:10} {len(mine):6}      {day:6}      ({a['name']})")
    pool = [j for j in jobs if j["lead_tech"] is None or j["lead_tech"] in nophone]
    if pool:
        print(f"   + on EVERY phone (booked to nobody reachable): {len(pool)}, "
              f"{sum(1 for j in pool if j['planned_date']==DAY)} of them on {DAY}")

    # --- 4. the day's plan, and anything on it no phone of its own will get ----------
    dayjobs = [j for j in jobs if j["planned_date"] == DAY]
    print(f"\n  {DAY}: {len(dayjobs)} open job(s) planned "
          f"({sum(1 for j in dayjobs if j['status']=='Assigned')} Assigned)")
    bad = [j for j in dayjobs if j["lead_tech"] is None or
           (j["lead_tech"] in ph_of and not accname.get(ph_of[j["lead_tech"]]))]
    if bad:
        print(f"  !! {len(bad)} of them reach no technician's own phone:")
        for j in bad[:15]:
            print(f"       {j['jobid']:14} lead={j['lead_tech']}")
print("\nDone. Nothing was changed.")

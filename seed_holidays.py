"""Load a year's public holidays into a plant's working calendar.

Every KPI on the dashboard is measured in OPERATING hours — the plant's own window,
weekly-off days and holidays skipped — so a public holiday the calendar does not know
about is counted as a working day. Availability, MTBF and PM compliance are all
measured against a denominator that is too big, and a machine that was standing idle
in an empty factory looks like a machine that was available and did nothing.

Today every plant's `holidays` list is EMPTY. This puts the national calendar in.

    python seed_holidays.py                      # show what would change, write nothing
    python seed_holidays.py --write              # write the core list into every plant
    python seed_holidays.py --write --optional   # include the four debatable days
    python seed_holidays.py --write --fac 2      # BFLFP only
    python seed_holidays.py --db "\\\\DESKTOP-0BE5LED\\bflfp-cmms\\data\\cmms.db" --write

The list is a STARTING POINT, not the law. A Thai factory sets its own calendar — the
Labour Protection Act sets a minimum of 13 days and every plant picks a different
thirteen. Check this against what HR actually published for BFLFP and take out what
the plant worked. Nothing here is destructive: dates already in the calendar are left
alone, dates the plant added by hand are never removed, and only the `holidays` key of
hours_json is touched.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

# ── 2026 (พ.ศ. 2569) ─────────────────────────────────────────────────────────────
# The days nobody argues about: national public holidays, with their in-lieu days.
CORE_2026 = [
    ("2026-01-01", "วันขึ้นปีใหม่ / New Year's Day"),
    ("2026-03-03", "วันมาฆบูชา / Makha Bucha Day"),
    ("2026-04-06", "วันจักรี / Chakri Memorial Day"),
    ("2026-04-13", "วันสงกรานต์ / Songkran"),
    ("2026-04-14", "วันสงกรานต์ / Songkran"),
    ("2026-04-15", "วันสงกรานต์ / Songkran"),
    ("2026-05-01", "วันแรงงาน / National Labour Day"),
    ("2026-05-04", "วันฉัตรมงคล / Coronation Day"),
    ("2026-05-31", "วันวิสาขบูชา / Visakha Bucha Day (a Sunday)"),
    ("2026-06-01", "ชดเชยวันวิสาขบูชา / substitute for Visakha Bucha"),
    ("2026-06-03", "วันเฉลิมพระชนมพรรษา สมเด็จพระนางเจ้าสุทิดาฯ / H.M. Queen Suthida's Birthday"),
    ("2026-07-28", "วันเฉลิมพระชนมพรรษา ร.10 / H.M. the King's Birthday"),
    ("2026-07-29", "วันอาสาฬหบูชา / Asalha Bucha Day"),
    ("2026-07-30", "วันเข้าพรรษา / Buddhist Lent Day (Khao Phansa)"),
    ("2026-08-12", "วันแม่แห่งชาติ / Mother's Day"),
    ("2026-10-13", "วันคล้ายวันสวรรคต ร.9 / King Bhumibol Memorial Day"),
    ("2026-10-23", "วันปิยมหาราช / Chulalongkorn Day"),
    ("2026-12-05", "วันพ่อแห่งชาติ / Father's Day (a Saturday)"),
    ("2026-12-07", "ชดเชยวันพ่อแห่งชาติ / substitute for Father's Day"),
    ("2026-12-10", "วันรัฐธรรมนูญ / Constitution Day"),
    ("2026-12-31", "วันสิ้นปี / New Year's Eve"),
]

# Days some plants take and others work. Off by default — ask HR before adding them.
OPTIONAL_2026 = [
    ("2026-01-02", "วันหยุดพิเศษ (มติ ครม.) / extra cabinet-approved holiday"),
    ("2026-05-11", "วันพืชมงคล / Royal Ploughing Ceremony — government and banks, often worked in factories"),
    ("2026-10-26", "วันออกพรรษา / End of Buddhist Lent — an observance, not a statutory holiday"),
]


def load(c, fac):
    r = c.execute("SELECT hours_json FROM factories WHERE id=?", (fac,)).fetchone()
    try:
        return json.loads(r[0]) if r and r[0] else {}
    except Exception:
        return {}


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(here / "data" / "cmms.db"))
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--fac", default="all", help="factory id, or 'all'")
    ap.add_argument("--optional", action="store_true", help="include the debatable days")
    ap.add_argument("--write", action="store_true", help="actually save (default: dry run)")
    a = ap.parse_args()

    if a.year != 2026:
        sys.exit(f"only 2026 is in this file — add the {a.year} list before running it")
    wanted = CORE_2026 + (OPTIONAL_2026 if a.optional else [])

    if not Path(a.db).exists():
        sys.exit(f"no database at {a.db}")
    c = sqlite3.connect(a.db)
    facs = [dict(zip(("id", "code", "name"), r)) for r in c.execute(
        "SELECT id, code, name FROM factories ORDER BY id")]
    if a.fac != "all":
        facs = [f for f in facs if str(f["id"]) == str(a.fac)]
        if not facs:
            sys.exit(f"no factory with id {a.fac}")

    print(f"{a.db}\n{'WRITING' if a.write else 'DRY RUN — nothing will be saved'}\n")
    for f in facs:
        cfg = load(c, f["id"])
        have = set(cfg.get("holidays") or [])
        add = [(d, n) for d, n in wanted if d not in have]
        print(f"── {f['code']} · {f['name']}  ({len(have)} already, +{len(add)})")
        for d, n in add:
            print(f"     + {d}  {n}")
        if not add:
            print("     nothing to add")
        if a.write and add:
            cfg["holidays"] = sorted(have | {d for d, _ in add})
            # keep the rest of the calendar exactly as the plant set it
            cfg.setdefault("start", cfg.get("start", "07:00"))
            cfg.setdefault("end", cfg.get("end", "21:00"))
            cfg.setdefault("weekly_off", cfg.get("weekly_off", [6]))
            cfg.setdefault("days", cfg.get("days", {}))
            cfg.setdefault("groups", cfg.get("groups", {}))
            c.execute("UPDATE factories SET hours_json=? WHERE id=?",
                      (json.dumps(cfg, ensure_ascii=False), f["id"]))
        print()
    if a.write:
        c.commit()
        print("saved · restart is NOT needed, the calendar is read on every request")
        print("NOTE: this changes past KPI figures too — every window that contains one")
        print("      of these dates now has a smaller denominator, which is the point.")
    else:
        print("run again with --write to save")
    c.close()


if __name__ == "__main__":
    main()

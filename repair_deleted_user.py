# -*- coding: utf-8 -*-
"""Give a deleted person's work back — to somebody who is still here.

When a user is deleted, the app does not throw their history away: it hands every
job, time log and signature to somebody else first (the admin, unless another name
was chosen) and writes a line into each job's own history saying where the work came
from.  That line is the only thing left that remembers the name, because the users
row itself is gone — and it is what this script reads.

So the jobs are found by record, not by guesswork:

    job_events.status LIKE '%moved from <NAME> to %'

Only the jobs that line names are touched, and on each of them only the seats the
holder still occupies.  What moves:

  · jobs.lead_tech      — the technician seat, where the holder still sits in it
  · jobs.helpers        — the holder's id inside the crew list
  · timelogs.tech       — the minutes, so the workload report credits the right person
  · jobs.owner1/owner2  — the new Owner columns, so the table reads correctly again

What does NOT move, deliberately:

  · created_by / requester_id — who reported the job is a separate fact from who
    fixed it, and it was not part of what was asked for.
  · signoffs — a signature is a statement by the person who made it.  It was already
    wrong to hand it to the admin; handing it on again would not make it right.
    They stay where they are, and the check run says how many there are.
  · plan_teams — historical day crews.  The holder may legitimately sit in others,
    and there is no way to tell them apart from here.

Nothing is written until --apply, and --apply copies the database first.

    python repair_deleted_user.py --db data/cmms.db                     # who was deleted?
    python repair_deleted_user.py --db data/cmms.db --from Him --to kanya
    python repair_deleted_user.py --db data/cmms.db --from Him --to kanya --apply
"""
import argparse, os, re, shutil, sqlite3, sys, datetime

MOVE_EN = "moved from "          # "... / moved from <who> to <target> — <who> was deleted by <admin>"


def cols(c, table):
    try:
        return [r[1] for r in c.execute("PRAGMA table_info(%s)" % table)]
    except Exception:
        return []


def find_moves(c):
    """Every delete-hand-over line in the whole database, grouped by the name that left.

    The line lives in `messages` with kind='system' — that is where log_job_event() puts
    a job's own timeline, alongside the status changes. It is the only surviving record
    of the name, because the users row itself is gone.
    """
    out = {}
    for r in c.execute("SELECT job_id, text, created_at FROM messages"
                       " WHERE kind='system' AND text LIKE ? AND job_id IS NOT NULL"
                       " ORDER BY id", ("%" + MOVE_EN + "%",)):
        m = re.search(r"moved from (.+?) to (.+?) — ", r["text"])
        if not m:
            m = re.search(r"moved from (.+?) to (.+?)$", r["text"])
        if not m:
            continue
        who, tgt = m.group(1).strip(), m.group(2).strip()
        out.setdefault((who, tgt), {"jobs": set(), "first": r["created_at"], "last": r["created_at"]})
        e = out[(who, tgt)]
        e["jobs"].add(r["job_id"])
        e["last"] = r["created_at"]
    return out


def resolve(c, q, as_person=False):
    """Find one active user by username or name. Assignments belong to LOGIN accounts,
    so a roster person with an account behind them resolves to that account."""
    ql = q.strip().lower()
    rows = [dict(r) for r in c.execute(
        "SELECT id,name,username,role,COALESCE(can_login,1) can_login,login_id,factory_id"
        " FROM users WHERE active=1")]
    hit = [r for r in rows if (r["username"] or "").lower() == ql] or \
          [r for r in rows if (r["name"] or "").lower() == ql]
    if not hit:
        hit = [r for r in rows if ql in (r["name"] or "").lower()
               or ql in (r["username"] or "").lower()]
    if not hit:
        sys.exit("!! no active user matches %r" % q)
    if len(hit) > 1:
        print("!! %r matches more than one person:" % q)
        for r in hit:
            print("     id=%-4s %-22s username=%-12s role=%-11s login=%s"
                  % (r["id"], r["name"], r["username"], r["role"],
                     "yes" if r["can_login"] else "no (via %s)" % r["login_id"]))
        sys.exit("   run again with the exact username.")
    r = hit[0]
    if not r["can_login"] and r["login_id"] and not as_person:
        acct = next((x for x in rows if x["id"] == r["login_id"]), None)
        if acct:
            print("   %s has no login of their own; work is assigned to accounts, so the"
                  % r["name"])
            print("   target is their account %s (id %s). Use --as-person to override."
                  % (acct["username"], acct["id"]))
            return acct
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/cmms.db")
    ap.add_argument("--from", dest="who", help="the name that was deleted, as the history line spells it")
    ap.add_argument("--to", dest="to", help="username or name of the person/account that gets the work")
    ap.add_argument("--name", help="the name to show in the Owner columns "
                    "(default: the target's own name; the table prints the stored name, "
                    "not a lookup, so this is what people will read)")
    ap.add_argument("--as-person", action="store_true",
                    help="assign to the roster person rather than their login account")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: report only)")
    a = ap.parse_args()

    if not os.path.exists(a.db):
        sys.exit("!! no database at %s" % a.db)
    c = sqlite3.connect(a.db)
    c.row_factory = sqlite3.Row

    moves = find_moves(c)
    if not a.who or not a.to:
        if not moves:
            print("No delete hand-over lines found in this database.")
            print("Either nobody has been deleted, or it happened before the app started")
            print("writing that line. Nothing can be recovered by name in that case.")
            return
        print("Deleted people whose work is still traceable:\n")
        for (who, tgt), e in sorted(moves.items(), key=lambda kv: -len(kv[1]["jobs"])):
            print("  %-24s → %-24s %3d job(s)   %s" % (who, tgt, len(e["jobs"]), e["last"][:16]))
        print("\nRe-run with:  --from \"<name on the left>\" --to <username who gets it>")
        return

    key = [k for k in moves if k[0].lower() == a.who.strip().lower()]
    if not key:
        near = [k[0] for k in moves]
        sys.exit("!! nothing recorded for %r. Names on record: %s" % (a.who, ", ".join(near) or "none"))
    job_ids, holders = set(), set()
    for k in key:
        job_ids |= moves[k]["jobs"]
        holders.add(k[1])
    tgt = dict(resolve(c, a.to, a.as_person))
    # The Owner columns print the name STORED on the job, not a live lookup — that is the
    # whole reason they survive a deletion. So the name written here is the name people
    # will read, and it does not have to be the account's own name: an account called
    # "techfp2" carrying Kanya's work should read "Kanya".
    show = (a.name or tgt["name"] or tgt["username"])

    # who is holding the work now — resolved from the name the history line recorded
    hold_ids = []
    for h in holders:
        r = c.execute("SELECT id FROM users WHERE name=? OR username=?", (h, h)).fetchone()
        if r:
            hold_ids.append(r["id"])
    if not hold_ids:
        sys.exit("!! cannot find the account holding the work (%s) — it may itself have been deleted"
                 % ", ".join(holders))

    jc = cols(c, "jobs")
    has_owner = "owner1" in jc
    ph = ",".join("?" * len(job_ids))
    hp = ",".join("?" * len(hold_ids))
    args_j = list(job_ids)

    print("\n%s  →  %s (id %s, %s)" % (a.who, tgt["name"], tgt["id"], tgt["username"]))
    print("Owner columns will read: %s" % show)
    print("holder on record: %s (id %s)" % (", ".join(holders), ", ".join(map(str, hold_ids))))
    print("jobs recorded as %s's: %d\n" % (a.who, len(job_ids)))

    # Two things count as "still his", and only ever on a job the history named:
    #   · the account the work was handed to, and
    #   · an id that no longer resolves to anybody. The delete moved the columns it knew
    #     about, but the Owner columns came later — nothing moved those, so a job of his
    #     can still be pointing at the id that went with him. On one of HIS jobs, an id
    #     belonging to nobody is his.
    live = {r[0] for r in c.execute("SELECT id FROM users")}

    def his(v):
        return bool(v) and (v in hold_ids or v not in live)

    lead = c.execute("SELECT id,jobid,status,lead_tech,helpers%s FROM jobs"
                     " WHERE id IN (%s) ORDER BY id" % (",owner1,owner2" if has_owner else "", ph),
                     args_j).fetchall()
    n_lead = n_help = n_o1 = n_o2 = 0
    print("  job        status            change")
    print("  " + "-" * 62)
    for r in lead:
        chg = []
        if his(r["lead_tech"]):
            chg.append("lead → %s" % tgt["username"]); n_lead += 1
        hl = [x for x in (r["helpers"] or "").split(",") if x.strip()]
        if any(his(int(x)) for x in hl if x.strip().isdigit()):
            chg.append("crew → %s" % tgt["username"]); n_help += 1
        if has_owner:
            if his(r["owner1"]):
                chg.append("owner1 → %s, owner2 cleared" % tgt["username"]); n_o1 += 1
            elif his(r["owner2"]):
                chg.append("owner2 → %s" % tgt["username"]); n_o2 += 1
        if chg:
            print("  %-10s %-17s %s" % (r["jobid"], r["status"], "; ".join(chg)))
    TL_W = ("job_id IN (%s) AND tech IS NOT NULL"
            " AND (tech IN (%s) OR tech NOT IN (SELECT id FROM users))" % (ph, hp))
    tl = c.execute("SELECT COUNT(*) FROM timelogs WHERE " + TL_W,
                   args_j + hold_ids).fetchone()[0]
    so = 0
    if cols(c, "signoffs"):
        so = c.execute("SELECT COUNT(*) FROM signoffs WHERE job_id IN (%s) AND user_id IN (%s)"
                       % (ph, hp), args_j + hold_ids).fetchone()[0]
    print("  " + "-" * 62)
    print("  lead seat %d · crew list %d · owner1 %d · owner2 %d · time logs %d"
          % (n_lead, n_help, n_o1, n_o2, tl))
    if so:
        print("  %d signature(s) left where they are — a signature belongs to whoever made it." % so)
    if not (n_lead or n_help or n_o1 or n_o2 or tl):
        print("\nNothing to move — the work is no longer with %s." % ", ".join(holders))
        return
    if not a.apply:
        print("\nNothing was written. Add --apply to make these changes.")
        return

    bak = a.db + ".before-repair-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(a.db, bak)
    print("\nbackup: %s" % bak)

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    note = ("⇄ คืนงานให้ %s (แทน %s ที่ถูกลบ) / work returned to %s in place of %s, who was deleted"
            % (show, a.who, show, a.who))
    for r in lead:
        if his(r["lead_tech"]):
            c.execute("UPDATE jobs SET lead_tech=? WHERE id=?", (tgt["id"], r["id"]))
        hl = [x.strip() for x in (r["helpers"] or "").split(",") if x.strip()]
        if any(x.isdigit() and his(int(x)) for x in hl):
            # swap his id for the target's, drop duplicates, and drop the target from the
            # crew list if it has just become the lead — nobody listed twice on one job
            newlead = tgt["id"] if his(r["lead_tech"]) else r["lead_tech"]
            new, seen = [], set()
            for x in hl:
                y = str(tgt["id"]) if (x.isdigit() and his(int(x))) else x
                if y in seen or y == str(newlead):
                    continue
                seen.add(y); new.append(y)
            c.execute("UPDATE jobs SET helpers=? WHERE id=?", (",".join(new), r["id"]))
        if has_owner:
            if his(r["owner1"]):
                # he started it: he is owner 1, and there is no move left to record
                c.execute("UPDATE jobs SET owner1=?, owner1_name=?, owner2=NULL, owner2_name=''"
                          " WHERE id=?", (tgt["id"], show, r["id"]))
            elif his(r["owner2"]):
                # somebody else started it and it moved to him: owner 1 stays as it is
                c.execute("UPDATE jobs SET owner2=?, owner2_name=? WHERE id=?",
                          (tgt["id"], show, r["id"]))
        c.execute("INSERT INTO messages (job_id,author,text,kind,created_at)"
                  " VALUES (?,?,?,'system',?)", (r["id"], tgt["id"], note, now))
    c.execute("UPDATE timelogs SET tech=? WHERE " + TL_W, [tgt["id"]] + args_j + hold_ids)
    c.commit()
    print("done. Restart the app so it reads the changed rows.")


if __name__ == "__main__":
    main()

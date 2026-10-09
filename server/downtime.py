"""Engineering vs production downtime for breakdown (BD) jobs.

A stopped machine costs money from the minute it stops to the minute production
accepts it back. Two departments own different parts of that, and the argument in
the meeting is always about which. So the clock is split, and the split is defined
by events the two departments create themselves:

    เวลาเสียของวิศวกรรม / ENGINEERING   the job is with the workshop
      report (operator says the machine is stopped) → technician presses Stop
      and again after every rejection → the next Stop

    เวลาเสียของฝ่ายผลิต / PRODUCTION    the job is back with production
      technician's Stop → the operator accepts it, or rejects it

Two rules make the number defensible:

1. **Every minute belongs to exactly one side.** The spans are back to back from the
   report to the final acceptance — no gap, no overlap — so engineering + production
   always equals the total. Nobody can argue a minute into a crack between them.
2. **Waiting counts.** Waiting for a technician is engineering's, and so is waiting
   for a spare part or a supplier: the machine is down either way, and a purchasing
   delay is still the workshop's to chase. Waiting for the operator to accept the
   machine back is production's.

Minutes are counted in **operating hours only** — the factory's own working window,
skipping weekly-off days and holidays — the same clock MTBF already uses. A Friday
evening breakdown does not book the whole weekend against engineering.
"""

from datetime import datetime, timedelta

ENG, PROD = "eng", "prod"

# the events that hand the job from one side to the other
_TO_PROD = ("ServiceCompleted",)
_TO_ENG = ("Rework",)
_CLOSES = ("Done", "Rejected", "Cancelled")


def _ts(v):
    if not v:
        return None
    try:
        return datetime.strptime(str(v)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def op_minutes(a, b, sh=7, eh=21, offdays=(6,), holidays=(), days=None):
    """Minutes between two timestamps, counting only the working window.

    `sh`/`eh` are whole hours; eh >= 24 means the machine runs to midnight (24/7).
    Days in `offdays` (0 = Monday, 6 = Sunday) and dates in `holidays` are skipped.

    `days` is the optional per-weekday map from db.day_windows — {weekday: (sh, eh)}
    with None for a day that is off. A weekday named there overrides both `sh`/`eh`
    and `offdays`, so a plant that works Sunday 07:00-20:00 gets 13 hours for that
    day and 14 for the rest. Absent, nothing changes: this is the old function.
    """
    ta, tb = (a, b) if isinstance(a, datetime) else (_ts(a), _ts(b))
    if not ta or not tb or tb <= ta:
        return 0.0
    total, cur = 0.0, ta
    while cur.date() <= tb.date():
        wd = cur.weekday()
        if days and wd in days:
            win = days[wd]
            open_today, dsh, deh = (win is not None), (win or (0, 0))[0], (win or (0, 0))[1]
        else:
            open_today, dsh, deh = (wd not in offdays), sh, eh
        if open_today and cur.date().isoformat() not in holidays:
            ws = cur.replace(hour=dsh, minute=0, second=0, microsecond=0)
            we = ((cur.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
                  if deh >= 24 else cur.replace(hour=deh, minute=0, second=0, microsecond=0))
            s, e = max(ta, ws), min(tb, we)
            if e > s:
                total += (e - s).total_seconds() / 60
        cur = (cur + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return total


def _hrs(hours):
    """(sh, eh, offdays, holidays, days) from a 4- or 5-item hours tuple.

    Callers built four-item tuples for a year before per-weekday hours existed, and
    several of them are in other modules. Both shapes are accepted so no call site
    is obliged to change on the same day.
    """
    h = tuple(hours or (7, 21, (6,), ()))
    return (h[0], h[1], h[2], h[3], h[4] if len(h) > 4 else None)


def spans(created_at, events, now_ts=None, fallback=None):
    """The job's life as back-to-back (side, from, to) spans.

    `events` is [(status, at), ...] in chronological order — job_events as it is
    stored. Statuses that do not move the job between the two sides (Assigned,
    InProgress, Hold …) are deliberately ignored: a job on hold for a spare part is
    still engineering's, which is the rule the plant agreed.

    A job that is still open runs to `now_ts`. A job with no events at all — a row
    from before the history table was kept — falls back to the two timestamps on the
    job itself, given as `fallback = (done_at, approved_at)`.
    """
    t0 = _ts(created_at)
    if not t0:
        return []
    now = _ts(now_ts) or datetime.now()
    moves = [(st, _ts(at)) for st, at in (events or [])]
    moves = [(st, at) for st, at in moves if at and at >= t0]

    if not moves and fallback:
        done, appr = (_ts(fallback[0]), _ts(fallback[1]))
        out = []
        if done:
            out.append((ENG, t0, done))
            out.append((PROD, done, appr or now))
        else:
            out.append((ENG, t0, now))
        return [s for s in out if s[2] > s[1]]

    out, side, mark = [], ENG, t0
    for st, at in moves:
        if at < mark:
            at = mark
        if st in _CLOSES:
            out.append((side, mark, at))
            return [s for s in out if s[2] > s[1]]
        want = PROD if st in _TO_PROD else ENG if st in _TO_ENG else None
        if want is None or want == side:
            continue                      # not a handover, or already on that side
        out.append((side, mark, at))
        side, mark = want, at
    out.append((side, mark, now))         # still open
    return [s for s in out if s[2] > s[1]]


def split(created_at, events, hours=None, now_ts=None, fallback=None):
    """{eng_min, prod_min, total_min} in operating minutes, plus the raw spans.

    `hours` = (start_hour, end_hour, offdays, holidays[, per-weekday map]); defaults
    to 07:00-21:00, Sundays off, no holidays. The fifth item is db.day_windows().
    """
    sh, eh, offdays, holidays, days = _hrs(hours)
    eng = prod = 0.0
    rows = spans(created_at, events, now_ts, fallback)
    for side, a, b in rows:
        m = op_minutes(a, b, sh, eh, offdays, holidays, days)
        if side == ENG:
            eng += m
        else:
            prod += m
    return {"eng_min": round(eng), "prod_min": round(prod),
            "total_min": round(eng + prod), "spans": rows}


def work_inside(events, hours=None, now_ts=None):
    """Of the engineering minutes, how many the technician was logged as working.

    Start (InProgress) until the next event of any kind — a Hold, a Pause, the Stop.
    Everything else inside engineering is the machine down with nobody logged on it:
    waiting for the technician to arrive, or waiting because he is on the job but has
    not told the app yet.

    This is NOT a third department. The plant's rule stands — every engineering minute
    is engineering's, whether a spanner was turning or not. It is reported so the
    workshop can see the shape of its own number, and so the crew can see how much of
    the bar is simply the app not being told. As the habit takes hold the pale band
    shrinks on its own; that is the point of measuring it.
    """
    sh, eh, offdays, holidays, days = _hrs(hours)
    now = _ts(now_ts) or datetime.now()
    evs = [(st, _ts(at)) for st, at in (events or [])]
    evs = [(st, at) for st, at in evs if at]
    total = 0.0
    for i, (st, at) in enumerate(evs):
        if st == "InProgress":
            end = evs[i + 1][1] if i + 1 < len(evs) else now
            total += op_minutes(at, end, sh, eh, offdays, holidays, days)
    return round(total)


def hold_inside(events, hours=None, now_ts=None):
    """Of the engineering minutes, how many were spent on hold.

    Not a third bucket — the plant's rule is that a machine waiting for a part is
    still engineering's downtime. It is reported so the workshop can show how much
    of its number is a purchasing or supplier delay rather than repair.
    """
    sh, eh, offdays, holidays, days = _hrs(hours)
    now = _ts(now_ts) or datetime.now()
    evs = [(st, _ts(at)) for st, at in (events or [])]
    evs = [(st, at) for st, at in evs if at]
    total = 0.0
    for i, (st, at) in enumerate(evs):
        if st == "Hold":
            end = evs[i + 1][1] if i + 1 < len(evs) else now
            total += op_minutes(at, end, sh, eh, offdays, holidays, days)
    return round(total)

"""Live updates: the server tells the screens when something moved.

Screens used to sit still until somebody pulled to refresh, so a technician pressing
Start left the operator watching a stale page. The first fix was a timer on every
phone. The second was a connection held open by the server (server-sent events) — and
it did not survive the front door this app sits behind: the bytes were buffered
somewhere in the middle, phones heard nothing for 25s at a stretch, and the hang-up
never came back either, so dead streams piled up here and held restarts hostage.

What is left is the plain version. A screen asks "anything moved since token X?" and
the answer is simply withheld until something has, or until the question has been held
about 25 seconds. Ordinary request, ordinary answer, nothing in the middle to break —
an answer that takes its time is the entire mechanism.

What counts as "something moved" is that token — the high-water mark of the tables a
job screen reads, plus the status histogram, so a status change with no new row still
registers. ONE task recomputes it once a second, whatever the number of phones.
"""
import asyncio
import logging
import time
from contextlib import closing

from fastapi import APIRouter

from .db import db

log = logging.getLogger("cmms")
router = APIRouter(prefix="/api")   # kept so app.py can keep importing it

TICK = 1.0          # seconds between checks — the ceiling on how late an update lands

_state = {"v": "", "n": 0}
_bell = None        # asyncio.Event, created on the running loop at startup
_task = None
_stop = False       # set on shutdown so held-open streams let go of the server


def stop():
    """Release every screen that is currently waiting on an answer.

    A restart must not have to wait out somebody's 25-second question. (uvicorn runs
    this only after it has already waited for connections to close, so run.py also
    caps that wait — this is the tidy path, not the safety net.)
    """
    global _stop
    _stop = True
    if _bell:
        try:
            _bell.set()
        except Exception:
            pass


def token():
    """One string that changes whenever a job screen would look different.

    High-water marks alone are not enough: a job moving from In progress to Completed
    writes no new row of its own, and `log_status` skips an event that repeats the
    status it already recorded — so the marks would sit still while the screen was
    plainly wrong. The status histogram closes that: any job changing status changes
    two of its numbers. The sums beside it catch a change of crew, priority, rework or
    progress, which is the rest of what a list row draws.
    """
    with closing(db()) as c:
        def top(sql):
            r = c.execute(sql).fetchone()
            return (r[0] if r and r[0] is not None else 0)
        marks = "{}.{}.{}".format(
            top("SELECT MAX(id) FROM job_events"),
            top("SELECT MAX(id) FROM jobs"),
            top("SELECT MAX(id) FROM timelogs"))
        hist = ",".join(f"{r[0]}:{r[1]}" for r in c.execute(
            "SELECT status, COUNT(*) FROM jobs GROUP BY status ORDER BY status"))
        r = c.execute("""SELECT COALESCE(SUM(COALESCE(lead_tech,0)),0),
                                COALESCE(SUM(COALESCE(priority,0)),0),
                                COALESCE(SUM(COALESCE(rework_count,0)),0),
                                COALESCE(SUM(COALESCE(progress,0)),0),
                                COUNT(NULLIF(COALESCE(planned_date,''),''))
                         FROM jobs""").fetchone()
        sums = ".".join(str(x or 0) for x in r)
        run = top("SELECT COUNT(*) FROM timelogs WHERE end IS NULL")
        # b381: the admin's menu / tab layout — screens re-read it when this moves
        try:
            ui = c.execute("SELECT v FROM app_settings WHERE k='ui_stamp'").fetchone()
            ui = ui[0] if ui else "0"
        except Exception:
            ui = "0"
        return f"{marks}|{hist}|{sums}.{run}|ui:{ui}"


async def wait_change(v, timeout=20.0):
    """Hold an ordinary request open until the token differs from what a screen has.

    Server-sent events do not survive the front door this app sits behind: the bytes
    are held somewhere between uvicorn and the phone, so a screen heard nothing for
    25s at a time and fell back to a timer — and the server never even learned the
    phone had gone, so every dead stream stayed open. A held-open ordinary GET does
    survive, because in the end it is an ordinary answer; it just takes its time
    arriving. Same latency as the stream was supposed to give, no special plumbing to
    break, and it closes itself when it is done.
    """
    if not _bell:
        return _state["v"]
    if not _state["v"]:                  # first seconds after a restart
        _state["v"] = await asyncio.to_thread(token)
    end = time.monotonic() + timeout
    while _state["v"] == v and not _stop:
        left = end - time.monotonic()
        if left <= 0:
            break
        try:
            await asyncio.wait_for(_bell.wait(), timeout=min(left, 5.0))
        except asyncio.TimeoutError:
            pass
    return _state["v"]


async def _watch():
    """The single heartbeat. One query a second for the whole plant, not one per phone."""
    global _bell
    while True:
        try:
            v = await asyncio.to_thread(token)
            if v != _state["v"]:
                _state["v"] = v
                _state["n"] += 1
                if _bell:
                    _bell.set()          # wake every open connection
                    _bell.clear()
        except Exception:
            pass                          # a hiccup must never kill the heartbeat
        await asyncio.sleep(TICK)


def start():
    """Called once at startup, on the running loop."""
    global _bell, _task, _stop
    _stop = False
    _bell = asyncio.Event()
    if _task is None or _task.done():
        _task = asyncio.get_event_loop().create_task(_watch())


# The /live stream is gone. It was held open by the server and the bytes never
# arrived: the front door in front of this app buffers them, and it does not pass the
# phone hanging up back either — so dead streams stayed open here for ever and one of
# them was enough to hold a restart hostage ("waiting for connections to close").
# /api/pulse?wait= replaces it: same latency, an ordinary request that always ends.

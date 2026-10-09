from contextlib import closing

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import STATIC, UPLOADS, APP_VERSION, ENV, DATA
from .db import init, db
from . import (auth, push, jobs, reports, admin, chat, kpi, planreport, pm, pmreport, signature, elec,
               teams, logs, live, projects, help, qrlabels)

import logging
# the app's own logger — the file handler is attached by logs.setup(), and everything
# written here shows in Manage → Logs. print() does not: it goes to a console nobody is
# watching, which is where these messages used to die.
_log = logging.getLogger("cmms")


def create_app():
    # The log FIRST, then the database. init() runs the schema migrations and they are
    # the most interesting lines the app ever writes — "plant stamped on 66 jobs",
    # "3 stuck timers closed" — and with the handler attached afterwards every one of
    # them went to a console instead of the file, which is to say nowhere anybody looks.
    logs.setup()
    init()
    app = FastAPI(title="BFLFP CMMS", version=APP_VERSION, docs_url="/api/docs")

    @app.middleware("http")
    async def _no_cache(request, call_next):
        resp = await call_next(request)
        p = request.url.path
        # Every API answer is a fact about right now — a job's status, a count on a
        # badge. Left cacheable, a phone can repeat a GET and be handed the answer from
        # a minute ago: the operator's "waiting to approve" badge stayed empty while the
        # job sat in the list underneath it. Nothing under /api/ may be reused.
        if p.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
        elif p == "/" or p.endswith(".html") or p.endswith("/sw.js") or p.endswith("/manifest.json"):
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        elif (p == "/logo.png" or p.startswith("/Logo") or (p.startswith("/fac_") and p.endswith(".png"))
              or p.startswith("/techphotos/")):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp

    @app.get("/api/health")
    async def health():
        try:
            with closing(db()) as c:
                users = c.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
                jobs_n = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            from .database import metadata
            return {"ok": True, "version": APP_VERSION, "env": ENV, "data": DATA,
                    "users": users, "jobs": jobs_n, "tables": len(metadata.tables)}
        except Exception as e:
            return {"ok": False, "version": APP_VERSION, "error": str(e)}
    for m in (auth, push, jobs, reports, admin, chat, kpi, planreport, pm, pmreport,
              teams, logs, live, projects, help, qrlabels, signature, elec):
        app.include_router(m.router)
    # …and the middleware that writes an unhandled exception to the log instead of
    # losing it to a console nobody is watching. Defined since the log was written,
    # never called — so a 500 left no trace anywhere.
    logs.install(app)

    @app.on_event("startup")
    async def _live_watch():
        # one heartbeat for the whole plant — every open screen is woken from it
        try:
            live.start()
        except Exception as e:
            _log.warning("[live] watcher not started: %s", e)

    @app.on_event("shutdown")
    async def _live_release():
        # an open screen must not be able to hold a restart hostage
        try:
            live.stop()
        except Exception:
            pass

    @app.on_event("startup")
    async def _pm_daily():
        import os, asyncio
        if os.environ.get("CMMS_PM_SCHED", "1") == "0":
            return

        async def loop():
            while True:
                try:
                    made = pm.run_due_all()          # idempotent: only issues plans actually due
                    if made:
                        _log.info(f"[pm] auto-generated {len(made)} due PM job(s)")
                except Exception as e:
                    _log.error("[pm] scheduler error: %s", e)
                await asyncio.sleep(3600)             # re-check hourly; due-logic dedups per day

        asyncio.create_task(loop())
    @app.on_event("startup")
    async def _auto_daily():
        # Each plant's daily report files itself at 23:59 (reports.auto_file_day), and
        # days missed while the server was off are filed on start (auto_catch_up).
        import os, asyncio
        if os.environ.get("CMMS_AUTO_DAILY", "1") == "0":
            return

        async def loop():
            try:
                await asyncio.to_thread(reports.auto_catch_up)
            except Exception as e:
                _log.error("[auto-daily] catch-up failed: %s", e)
            while True:
                await asyncio.sleep(max(5.0, reports.seconds_to_next_run()))
                try:
                    from datetime import date as _date
                    d = _date.today().isoformat()
                    filed = await asyncio.to_thread(reports.auto_file_day, d)
                    _log.info("[auto-daily] %s filed: %s", d,
                              ", ".join(f"{c} ({n} jobs)" for c, n in filed) or "no plant finished any work")
                except Exception as e:
                    _log.error("[auto-daily] run failed: %s", e)
                await asyncio.sleep(90)          # step past 23:59 so it runs once

        asyncio.create_task(loop())
    app.mount("/uploads", StaticFiles(directory=UPLOADS))
    app.mount("/", StaticFiles(directory=STATIC, html=True))
    return app


app = create_app()

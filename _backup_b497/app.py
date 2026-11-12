from contextlib import closing

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import STATIC, UPLOADS, APP_VERSION, ENV, DATA
from .db import init, db
from . import (auth, push, jobs, reports, admin, chat, kpi, planreport, pm, pmreport, signature, elec,
               teams, logs, live, projects, help, qrlabels, wms, fg, master)

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
    try:                                  # Warehouse app tables (wms_*) + users.wms_role
        with closing(db()) as _c:
            wms.ensure(_c)
            _c.commit()
    except Exception as e:
        _log.error("[wms] table setup failed: %s", e)
    try:                                  # FG Stock app tables (fg_*) + users.fg_role / fg_only
        with closing(db()) as _c:
            fg.ensure(_c)
            _c.commit()
    except Exception as e:
        _log.error("[fg] table setup failed: %s", e)
    try:                                  # b495: Production master data (product_master)
        with closing(db()) as _c:
            master.ensure(_c)
            _c.commit()
    except Exception as e:
        _log.error("[master] table setup failed: %s", e)
    # b472 audit trail — after the migrations, so every connection sees the final tables
    # (restored b496: it was lost when the FG line of builds replaced app.py)
    from . import audit
    audit.install()
    app = FastAPI(title="BFLFP CMMS", version=APP_VERSION, docs_url="/api/docs")

    # b474 system health — times every API call, watches for stalls, alerts admins
    from . import monitor
    monitor.install(app)

    # b481: compress pages and API answers (index.html 1.7 MB -> ~0.3 MB, big FG lists ~10x smaller)
    from starlette.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)

    @app.middleware("http")
    async def _no_cache(request, call_next):
        # b472: the audit trail learns who is asking before anything is written
        _atok = audit.begin(request) if request.url.path.startswith("/api/") else None
        try:
            resp = await call_next(request)
        finally:
            if _atok is not None:
                audit.end(_atok)
        p = request.url.path
        # Every API answer is a fact about right now — a job's status, a count on a
        # badge. Left cacheable, a phone can repeat a GET and be handed the answer from
        # a minute ago: the operator's "waiting to approve" badge stayed empty while the
        # job sat in the list underneath it. Nothing under /api/ may be reused.
        if p.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
        elif p.endswith("/sw.js"):
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        elif p == "/" or p.endswith(".html") or p.endswith("/manifest.json") or p.endswith(".js"):
            # b481: "no-cache" (not "no-store") — the browser keeps its copy and asks the server
            # each time; an unchanged page comes back as a tiny 304 instead of 1.7 MB again.
            # A new build is still picked up on the very next open.
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
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
              teams, logs, live, projects, help, qrlabels, signature, elec, wms, fg, master,
              audit, monitor):
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

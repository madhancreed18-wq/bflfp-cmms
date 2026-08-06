from contextlib import closing

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import STATIC, UPLOADS, APP_VERSION
from .db import init, db
from . import auth, push, jobs, reports, admin, chat, kpi


def create_app():
    init()
    app = FastAPI(title="BFLFP CMMS", version=APP_VERSION, docs_url="/api/docs")

    @app.get("/api/health")
    async def health():
        try:
            with closing(db()) as c:
                users = c.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
                jobs_n = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            from .database import metadata
            return {"ok": True, "version": APP_VERSION, "users": users, "jobs": jobs_n,
                    "tables": len(metadata.tables)}
        except Exception as e:
            return {"ok": False, "version": APP_VERSION, "error": str(e)}
    for m in (auth, push, jobs, reports, admin, chat, kpi):
        app.include_router(m.router)
    app.mount("/uploads", StaticFiles(directory=UPLOADS))
    app.mount("/", StaticFiles(directory=STATIC, html=True))
    return app


app = create_app()

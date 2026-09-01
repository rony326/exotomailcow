from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db.session import SessionLocal, init_db
from app.logging_config import configure_logging
from app.security.auth import bootstrap_admin_from_env
from app.web.routes import admin, jobs, mappings, reports, resync, setup
from app.web.scheduler_dep import get_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_dir, settings.log_level)
    init_db()
    db = SessionLocal()
    try:
        bootstrap_admin_from_env(db)
    finally:
        db.close()
    get_scheduler().resume_incomplete_jobs()
    yield


app = FastAPI(title="exotomailcow", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
app.include_router(setup.router)
app.include_router(mappings.router)
app.include_router(jobs.router)
app.include_router(reports.router)
app.include_router(resync.router)
app.include_router(admin.router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/setup")

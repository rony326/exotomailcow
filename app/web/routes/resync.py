from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.jobs.resync import create_resync_job, create_resync_jobs_for_all
from app.security.auth import require_admin
from app.web.scheduler_dep import get_scheduler

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.post("/mappings/{mapping_id}/resync", response_class=HTMLResponse)
def resync_one(
    request: Request,
    mapping_id: int,
    db: Session = Depends(get_db),
    scheduler=Depends(get_scheduler),
    _: str = Depends(require_admin),
):
    try:
        job = create_resync_job(db, mapping_id)
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "_resync_error.html", {"error": str(exc)}, status_code=400
        )
    scheduler.submit(job.id)
    return templates.TemplateResponse(request, "_job_progress.html", {"job": job})


@router.post("/mappings/resync-all", response_class=HTMLResponse)
def resync_all(
    request: Request, db: Session = Depends(get_db), scheduler=Depends(get_scheduler), _: str = Depends(require_admin)
):
    jobs, skipped = create_resync_jobs_for_all(db)
    for job in jobs:
        scheduler.submit(job.id)
    return templates.TemplateResponse(request, "_resync_all_result.html", {"jobs": jobs, "skipped": skipped})

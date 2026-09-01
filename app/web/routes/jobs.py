from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db.models import JobType, MigrationJob
from app.db.session import get_db
from app.security.auth import require_admin
from app.web.scheduler_dep import get_scheduler

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.post("/jobs", response_class=HTMLResponse)
def create_jobs(
    request: Request,
    mapping_ids: list[int] = Form(...),
    migrate_mail: bool = Form(False),
    migrate_calendar: bool = Form(False),
    migrate_contacts: bool = Form(False),
    mail_since_date: str = Form(""),
    dry_run: bool = Form(False),
    db: Session = Depends(get_db),
    scheduler=Depends(get_scheduler),
    _: str = Depends(require_admin),
):
    since = datetime.fromisoformat(mail_since_date) if mail_since_date else None
    jobs: list[MigrationJob] = []
    for mapping_id in mapping_ids:
        job = MigrationJob(
            mapping_id=mapping_id,
            job_type=JobType.INITIAL.value,
            migrate_mail=migrate_mail,
            migrate_calendar=migrate_calendar,
            migrate_contacts=migrate_contacts,
            mail_since_date=since,
            dry_run=dry_run,
        )
        db.add(job)
        jobs.append(job)
    db.commit()
    for job in jobs:
        db.refresh(job)
        scheduler.submit(job.id)
    return templates.TemplateResponse(request, "_jobs_started.html", {"jobs": jobs})


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_progress(request: Request, job_id: int, db: Session = Depends(get_db), _: str = Depends(require_admin)):
    job = db.get(MigrationJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return templates.TemplateResponse(request, "_job_progress.html", {"job": job})


@router.post("/jobs/{job_id}/cancel", response_class=HTMLResponse)
def cancel_job(
    request: Request,
    job_id: int,
    db: Session = Depends(get_db),
    scheduler=Depends(get_scheduler),
    _: str = Depends(require_admin),
):
    job = db.get(MigrationJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    scheduler.cancel(job_id)
    return templates.TemplateResponse(request, "_job_progress.html", {"job": job})

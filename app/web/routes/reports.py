import csv
import io
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db.models import ItemStatus, MigrationItem, MigrationJob
from app.db.session import get_db
from app.security.auth import require_admin

router = APIRouter()


def _get_job_or_404(db: Session, job_id: int) -> MigrationJob:
    job = db.get(MigrationJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _failed_items(db: Session, job: MigrationJob) -> list[MigrationItem]:
    return (
        db.query(MigrationItem)
        .filter(MigrationItem.mapping_id == job.mapping_id, MigrationItem.status == ItemStatus.FAILED.value)
        .all()
    )


@router.get("/jobs/{job_id}/report.json")
def report_json(job_id: int, db: Session = Depends(get_db), _: str = Depends(require_admin)):
    job = _get_job_or_404(db, job_id)
    failed = _failed_items(db, job)
    payload = {
        "job_id": job.id,
        "status": job.status,
        "count_created": job.count_created,
        "count_updated": job.count_updated,
        "count_skipped": job.count_skipped,
        "count_failed": job.count_failed,
        "errors": [
            {"category": item.category, "external_id": item.external_id, "error": item.error_message}
            for item in failed
        ],
    }
    return Response(content=json.dumps(payload, indent=2), media_type="application/json")


@router.get("/jobs/{job_id}/report.csv")
def report_csv(job_id: int, db: Session = Depends(get_db), _: str = Depends(require_admin)):
    job = _get_job_or_404(db, job_id)
    failed = _failed_items(db, job)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["category", "external_id", "error"])
    for item in failed:
        writer.writerow([item.category, item.external_id, item.error_message])
    return Response(content=buffer.getvalue(), media_type="text/csv")

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from app.db.models import JobStatus, MigrationJob

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, max_workers: int, db_session_factory: Callable[[], Session], runner) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._db_session_factory = db_session_factory
        self._runner = runner

    def submit(self, job_id: int) -> None:
        self._pool.submit(self._run_safely, job_id)

    def _run_safely(self, job_id: int) -> None:
        try:
            self._runner.run(job_id)
        except Exception:
            logger.exception("Migration job %s crashed", job_id)

    def cancel(self, job_id: int) -> None:
        db = self._db_session_factory()
        try:
            job = db.get(MigrationJob, job_id)
            if job is not None and job.status in (JobStatus.PENDING.value, JobStatus.RUNNING.value):
                job.status = JobStatus.CANCELLED.value
                db.commit()
        finally:
            db.close()

    def resume_incomplete_jobs(self) -> None:
        db = self._db_session_factory()
        try:
            stuck = db.query(MigrationJob).filter(MigrationJob.status == JobStatus.RUNNING.value).all()
            stuck_ids = [job.id for job in stuck]
            for job in stuck:
                job.status = JobStatus.PENDING.value
            db.commit()

            pending_ids = [
                job.id for job in db.query(MigrationJob).filter(MigrationJob.status == JobStatus.PENDING.value).all()
            ]
        finally:
            db.close()

        for job_id in set(stuck_ids) | set(pending_ids):
            self.submit(job_id)

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait)

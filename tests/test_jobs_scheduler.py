import threading
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, JobStatus, MailboxMapping, MigrationJob
from app.jobs.scheduler import Scheduler


def _session_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed_jobs(session_factory, count: int, status: str = JobStatus.PENDING.value) -> list[int]:
    db = session_factory()
    mapping = MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc")
    db.add(mapping)
    db.commit()
    ids = []
    for _ in range(count):
        job = MigrationJob(mapping_id=mapping.id, status=status)
        db.add(job)
        db.commit()
        ids.append(job.id)
    db.close()
    return ids


class ConcurrencyTrackingRunner:
    def __init__(self):
        self.lock = threading.Lock()
        self.current = 0
        self.peak = 0
        self.calls: list[int] = []

    def run(self, job_id: int) -> None:
        with self.lock:
            self.current += 1
            self.peak = max(self.peak, self.current)
        time.sleep(0.2)
        with self.lock:
            self.calls.append(job_id)
            self.current -= 1


def test_submit_respects_max_workers_concurrency_limit():
    session_factory = _session_factory()
    job_ids = _seed_jobs(session_factory, 4)
    runner = ConcurrencyTrackingRunner()
    scheduler = Scheduler(max_workers=2, db_session_factory=session_factory, runner=runner)

    for job_id in job_ids:
        scheduler.submit(job_id)
    scheduler.shutdown(wait=True)

    assert runner.peak == 2
    assert sorted(runner.calls) == sorted(job_ids)


class RecordingRunner:
    def __init__(self):
        self.calls: list[int] = []

    def run(self, job_id: int) -> None:
        self.calls.append(job_id)


def test_resume_incomplete_jobs_resets_running_to_pending_and_resubmits():
    session_factory = _session_factory()
    running_ids = _seed_jobs(session_factory, 1, status=JobStatus.RUNNING.value)
    pending_ids = _seed_jobs(session_factory, 1, status=JobStatus.PENDING.value)
    runner = RecordingRunner()
    scheduler = Scheduler(max_workers=2, db_session_factory=session_factory, runner=runner)

    scheduler.resume_incomplete_jobs()
    scheduler.shutdown(wait=True)

    assert sorted(runner.calls) == sorted(running_ids + pending_ids)

    db = session_factory()
    for job_id in running_ids:
        assert db.get(MigrationJob, job_id).status == JobStatus.PENDING.value


def test_cancel_marks_pending_or_running_job_as_cancelled():
    session_factory = _session_factory()
    job_ids = _seed_jobs(session_factory, 1, status=JobStatus.RUNNING.value)
    runner = RecordingRunner()
    scheduler = Scheduler(max_workers=2, db_session_factory=session_factory, runner=runner)

    scheduler.cancel(job_ids[0])

    db = session_factory()
    assert db.get(MigrationJob, job_ids[0]).status == JobStatus.CANCELLED.value
    scheduler.shutdown(wait=True)


def test_cancel_leaves_completed_job_untouched():
    session_factory = _session_factory()
    job_ids = _seed_jobs(session_factory, 1, status=JobStatus.COMPLETED.value)
    runner = RecordingRunner()
    scheduler = Scheduler(max_workers=2, db_session_factory=session_factory, runner=runner)

    scheduler.cancel(job_ids[0])

    db = session_factory()
    assert db.get(MigrationJob, job_ids[0]).status == JobStatus.COMPLETED.value
    scheduler.shutdown(wait=True)

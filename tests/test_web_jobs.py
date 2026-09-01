from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session

from app.db.models import Base, JobStatus, MailboxMapping, MigrationJob
from app.db.session import get_db
from app.security.auth import require_admin
from app.web.routes import jobs
from app.web.scheduler_dep import get_scheduler


class FakeScheduler:
    def __init__(self):
        self.submitted: list[int] = []
        self.cancelled: list[int] = []

    def submit(self, job_id: int) -> None:
        self.submitted.append(job_id)

    def cancel(self, job_id: int) -> None:
        self.cancelled.append(job_id)


def _app_and_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    mapping = MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc")
    db.add(mapping)
    db.commit()

    fake_scheduler = FakeScheduler()
    app = FastAPI()
    app.include_router(jobs.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_admin] = lambda: "admin"
    app.dependency_overrides[get_scheduler] = lambda: fake_scheduler
    return app, db, mapping, fake_scheduler


def test_create_jobs_supports_single_mapping_selection():
    app, db, mapping, fake_scheduler = _app_and_db()
    client = TestClient(app)

    response = client.post(
        "/jobs",
        data={
            "mapping_ids": [str(mapping.id)],
            "migrate_mail": "true",
            "migrate_calendar": "true",
            "migrate_contacts": "false",
            "dry_run": "false",
        },
    )

    assert response.status_code == 200
    job = db.query(MigrationJob).one()
    assert job.migrate_mail is True
    assert job.migrate_contacts is False
    assert fake_scheduler.submitted == [job.id]


def test_create_jobs_supports_batch_selection_of_multiple_mappings():
    app, db, mapping, fake_scheduler = _app_and_db()
    mapping2 = MailboxMapping(exo_upn="b@c", mailcow_address="b@d", app_password_encrypted="enc")
    db.add(mapping2)
    db.commit()

    client = TestClient(app)
    response = client.post(
        "/jobs",
        data={"mapping_ids": [str(mapping.id), str(mapping2.id)], "migrate_mail": "true"},
    )

    assert response.status_code == 200
    jobs = db.query(MigrationJob).order_by(MigrationJob.id).all()
    assert len(jobs) == 2
    assert {job.mapping_id for job in jobs} == {mapping.id, mapping2.id}
    assert sorted(fake_scheduler.submitted) == sorted(job.id for job in jobs)


def test_job_progress_endpoint_returns_current_counts():
    app, db, mapping, fake_scheduler = _app_and_db()
    job = MigrationJob(mapping_id=mapping.id, status=JobStatus.RUNNING.value, count_created=3, count_failed=1)
    db.add(job)
    db.commit()

    client = TestClient(app)
    response = client.get(f"/jobs/{job.id}")

    assert "3" in response.text
    assert "1" in response.text


def test_cancel_job_calls_scheduler_cancel():
    app, db, mapping, fake_scheduler = _app_and_db()
    job = MigrationJob(mapping_id=mapping.id, status=JobStatus.RUNNING.value)
    db.add(job)
    db.commit()

    client = TestClient(app)
    response = client.post(f"/jobs/{job.id}/cancel")

    assert response.status_code == 200
    assert fake_scheduler.cancelled == [job.id]

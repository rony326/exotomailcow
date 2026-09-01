import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session

from app.db.models import Base, JobStatus, MailboxMapping, MigrationJob
from app.db.session import get_db
from app.security.auth import require_admin
from app.web import scheduler_dep
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


def test_create_jobs_skips_mapping_with_already_active_job():
    # Regression test for finding #4 (final whole-branch review): two
    # concurrent jobs for the same mapping would both independently see
    # nothing done yet and both append the same messages via IMAP APPEND,
    # which is not idempotent, duplicating messages in the target mailbox.
    app, db, mapping, fake_scheduler = _app_and_db()
    existing_job = MigrationJob(mapping_id=mapping.id, status=JobStatus.PENDING.value)
    db.add(existing_job)
    db.commit()

    client = TestClient(app)
    response = client.post(
        "/jobs",
        data={"mapping_ids": [str(mapping.id)], "migrate_mail": "true"},
    )

    assert response.status_code == 200
    assert db.query(MigrationJob).filter_by(mapping_id=mapping.id).count() == 1
    assert fake_scheduler.submitted == []


def test_create_jobs_still_creates_for_mappings_without_active_job():
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
    jobs_created = db.query(MigrationJob).all()
    assert len(jobs_created) == 2
    assert {job.mapping_id for job in jobs_created} == {mapping.id, mapping2.id}
    assert len(fake_scheduler.submitted) == 2


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


def test_job_progress_returns_404_for_missing_job():
    app, db, mapping, fake_scheduler = _app_and_db()
    client = TestClient(app)

    response = client.get("/jobs/99999")

    assert response.status_code == 404


def test_cancel_job_returns_404_for_missing_job_and_does_not_call_scheduler():
    app, db, mapping, fake_scheduler = _app_and_db()
    client = TestClient(app)

    response = client.post("/jobs/99999/cancel")

    assert response.status_code == 404
    assert fake_scheduler.cancelled == []


def test_get_scheduler_returns_the_same_instance_under_concurrent_first_access():
    """Regression test: get_scheduler() must not construct two Scheduler
    instances (each owning its own ThreadPoolExecutor) when multiple
    threads race through the check-then-set on the very first call.

    A threading.Barrier forces every worker thread to reach get_scheduler()
    at (as close to) the same instant as possible, maximizing the chance of
    hitting the race window that existed before the double-checked-locking
    fix. The assertion (single object identity across all callers) is
    deterministic regardless of whether the race window is actually hit on
    a given run, since correct locking guarantees exactly one instance is
    ever built -- so this cannot flake.
    """
    # Reset the process-wide singleton so this test observes first-access behavior.
    original_scheduler = scheduler_dep._scheduler
    scheduler_dep._scheduler = None
    results: list = []
    try:
        thread_count = 16
        barrier = threading.Barrier(thread_count)

        def call_get_scheduler():
            barrier.wait(timeout=5)
            return scheduler_dep.get_scheduler()

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(call_get_scheduler) for _ in range(thread_count)]
            results = [future.result(timeout=5) for future in futures]

        first = results[0]
        assert all(result is first for result in results)
    finally:
        # Tear down whatever Scheduler instance(s) got built during this test
        # (should be exactly one) so their ThreadPoolExecutor threads don't
        # leak into the rest of the test run, then restore the module global.
        for built_scheduler in {id(r): r for r in results}.values():
            built_scheduler.shutdown(wait=True)
        scheduler_dep._scheduler = original_scheduler

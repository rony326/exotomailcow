from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session

from app.db.models import Base, ItemStatus, MailboxMapping, MigrationItem, MigrationJob, TenantConfig
from app.db.session import get_db
from app.security.auth import require_admin
from app.web.routes import admin, reports, resync
from app.web.scheduler_dep import get_scheduler


class FakeScheduler:
    def __init__(self):
        self.submitted: list[int] = []

    def submit(self, job_id: int) -> None:
        self.submitted.append(job_id)


def _app_and_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)

    fake_scheduler = FakeScheduler()
    app = FastAPI()
    app.include_router(reports.router)
    app.include_router(resync.router)
    app.include_router(admin.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_admin] = lambda: "admin"
    app.dependency_overrides[get_scheduler] = lambda: fake_scheduler
    return app, db, fake_scheduler


def test_report_json_lists_failed_items_and_counts():
    app, db, _ = _app_and_db()
    mapping = MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc")
    db.add(mapping)
    db.commit()
    job = MigrationJob(mapping_id=mapping.id, count_created=2, count_failed=1)
    db.add(job)
    db.add(MigrationItem(mapping_id=mapping.id, category="mail", external_id="m1", status=ItemStatus.FAILED.value, error_message="boom"))
    db.commit()

    client = TestClient(app)
    response = client.get(f"/jobs/{job.id}/report.json")

    body = response.json()
    assert body["count_created"] == 2
    assert body["errors"][0]["error"] == "boom"


def test_report_csv_contains_error_rows():
    app, db, _ = _app_and_db()
    mapping = MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc")
    db.add(mapping)
    db.commit()
    job = MigrationJob(mapping_id=mapping.id)
    db.add(job)
    db.add(MigrationItem(mapping_id=mapping.id, category="calendar", external_id="e1", status=ItemStatus.FAILED.value, error_message="dav down"))
    db.commit()

    client = TestClient(app)
    response = client.get(f"/jobs/{job.id}/report.csv")

    assert "dav down" in response.text


def test_resync_one_submits_job_when_previously_synced():
    app, db, fake_scheduler = _app_and_db()
    mapping = MailboxMapping(
        exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc",
        last_synced_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    db.add(mapping)
    db.commit()

    client = TestClient(app)
    response = client.post(f"/mappings/{mapping.id}/resync")

    assert response.status_code == 200
    job = db.query(MigrationJob).one()
    assert fake_scheduler.submitted == [job.id]


def test_resync_one_rejects_mapping_without_prior_sync():
    app, db, fake_scheduler = _app_and_db()
    mapping = MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc")
    db.add(mapping)
    db.commit()

    client = TestClient(app)
    response = client.post(f"/mappings/{mapping.id}/resync")

    assert response.status_code == 400
    assert fake_scheduler.submitted == []


def test_resync_all_only_submits_for_synced_mappings():
    app, db, fake_scheduler = _app_and_db()
    synced = MailboxMapping(
        exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc",
        last_synced_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    unsynced = MailboxMapping(exo_upn="x@y", mailcow_address="x@z", app_password_encrypted="enc")
    db.add_all([synced, unsynced])
    db.commit()

    client = TestClient(app)
    response = client.post("/mappings/resync-all")

    assert response.status_code == 200
    assert len(fake_scheduler.submitted) == 1


def test_resync_all_skips_mapping_with_active_job():
    # Regression test for finding #4 (final whole-branch review).
    app, db, fake_scheduler = _app_and_db()
    synced = MailboxMapping(
        exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc",
        last_synced_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    db.add(synced)
    db.commit()
    db.add(MigrationJob(mapping_id=synced.id, status="pending"))
    db.commit()

    client = TestClient(app)
    response = client.post("/mappings/resync-all")

    assert response.status_code == 200
    assert fake_scheduler.submitted == []
    assert db.query(MigrationJob).filter_by(mapping_id=synced.id).count() == 1


def test_purge_secrets_clears_secrets_but_keeps_history():
    app, db, _ = _app_and_db()
    from app.security.crypto import encrypt

    db.add(TenantConfig(tenant_id="t", client_id="c", client_secret_encrypted=encrypt("s"), admin_user="a", admin_password_hash="h"))
    mapping = MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted=encrypt("pw"))
    db.add(mapping)
    db.commit()
    job = MigrationJob(mapping_id=mapping.id, count_created=1)
    db.add(job)
    db.commit()

    client = TestClient(app)
    response = client.post("/admin/purge-secrets")

    assert response.status_code == 200
    config = db.query(TenantConfig).one()
    assert config.client_secret_encrypted is None
    reloaded_mapping = db.query(MailboxMapping).one()
    assert reloaded_mapping.app_password_encrypted == ""
    assert db.query(MigrationJob).count() == 1

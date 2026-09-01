from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, JobType, MailboxMapping
from app.jobs.resync import RESYNC_BUFFER, create_resync_job, create_resync_jobs_for_all


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_create_resync_job_derives_mail_since_date_from_last_sync():
    db = _session()
    last_synced = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    mapping = MailboxMapping(
        exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc", last_synced_at=last_synced
    )
    db.add(mapping)
    db.commit()

    job = create_resync_job(db, mapping.id)

    assert job.job_type == JobType.RESYNC.value
    assert job.mail_since_date == last_synced - RESYNC_BUFFER
    assert job.migrate_mail and job.migrate_calendar and job.migrate_contacts


def test_create_resync_job_raises_without_prior_sync():
    db = _session()
    mapping = MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc")
    db.add(mapping)
    db.commit()

    with pytest.raises(ValueError):
        create_resync_job(db, mapping.id)


def test_create_resync_jobs_for_all_only_targets_synced_mappings():
    db = _session()
    synced = MailboxMapping(
        exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc",
        last_synced_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    unsynced = MailboxMapping(exo_upn="x@y", mailcow_address="x@z", app_password_encrypted="enc")
    db.add_all([synced, unsynced])
    db.commit()

    jobs = create_resync_jobs_for_all(db)

    assert len(jobs) == 1
    assert jobs[0].mapping_id == synced.id

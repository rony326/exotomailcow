from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, ItemStatus, JobStatus, JobType, MailboxMapping, MigrationItem, MigrationJob


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_insert_and_query_mailbox_mapping():
    db = _session()
    mapping = MailboxMapping(exo_upn="user@church.org", mailcow_address="user@mailcow.local", app_password_encrypted="enc")
    db.add(mapping)
    db.commit()

    fetched = db.query(MailboxMapping).one()
    assert fetched.exo_upn == "user@church.org"
    assert fetched.last_synced_at is None


def test_migration_item_unique_constraint():
    db = _session()
    mapping = MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc")
    db.add(mapping)
    db.commit()

    db.add(MigrationItem(mapping_id=mapping.id, category="mail", external_id="msg-1", status=ItemStatus.DONE.value))
    db.commit()

    db.add(MigrationItem(mapping_id=mapping.id, category="mail", external_id="msg-1", status=ItemStatus.DONE.value))
    import pytest
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        db.commit()


def test_migration_job_defaults():
    db = _session()
    mapping = MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc")
    db.add(mapping)
    db.commit()

    job = MigrationJob(mapping_id=mapping.id, job_type=JobType.INITIAL.value)
    db.add(job)
    db.commit()

    fetched = db.query(MigrationJob).one()
    assert fetched.status == JobStatus.PENDING.value
    assert fetched.count_created == 0
    assert fetched.dry_run is False

from datetime import timedelta

from sqlalchemy.orm import Session

from app.db.models import JobType, MailboxMapping, MigrationJob

RESYNC_BUFFER = timedelta(minutes=15)


def create_resync_job(db: Session, mapping_id: int) -> MigrationJob:
    mapping = db.get(MailboxMapping, mapping_id)
    if mapping is None or mapping.last_synced_at is None:
        raise ValueError(f"Mapping {mapping_id} has no completed initial migration yet")
    job = MigrationJob(
        mapping_id=mapping_id,
        job_type=JobType.RESYNC.value,
        migrate_mail=True,
        migrate_calendar=True,
        migrate_contacts=True,
        mail_since_date=mapping.last_synced_at - RESYNC_BUFFER,
        dry_run=False,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def create_resync_jobs_for_all(db: Session) -> list[MigrationJob]:
    mappings = db.query(MailboxMapping).filter(MailboxMapping.last_synced_at.isnot(None)).all()
    return [create_resync_job(db, mapping.id) for mapping in mappings]

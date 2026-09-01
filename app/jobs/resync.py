from datetime import timedelta

from sqlalchemy.orm import Session

from app.db.models import JobType, MailboxMapping, MigrationJob
from app.db.repositories import has_active_job_for_mapping

RESYNC_BUFFER = timedelta(minutes=15)


def create_resync_job(db: Session, mapping_id: int) -> MigrationJob:
    mapping = db.get(MailboxMapping, mapping_id)
    if mapping is None or mapping.last_synced_at is None:
        raise ValueError(f"Mapping {mapping_id} has no completed initial migration yet")
    if has_active_job_for_mapping(db, mapping_id):
        raise ValueError(f"Mapping {mapping_id} already has an active migration job")
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


def create_resync_jobs_for_all(db: Session) -> tuple[list[MigrationJob], list[tuple[int, str]]]:
    """Create resync jobs for every mapping with a completed initial migration.

    Mappings that already have an active (pending/running) job are skipped
    rather than aborting the whole batch -- returned alongside the created
    jobs as (mapping_id, reason) pairs, mirroring the CSV import's
    skip-and-report shape.
    """
    mappings = db.query(MailboxMapping).filter(MailboxMapping.last_synced_at.isnot(None)).all()
    jobs: list[MigrationJob] = []
    skipped: list[tuple[int, str]] = []
    for mapping in mappings:
        try:
            jobs.append(create_resync_job(db, mapping.id))
        except ValueError as exc:
            skipped.append((mapping.id, str(exc)))
    return jobs, skipped

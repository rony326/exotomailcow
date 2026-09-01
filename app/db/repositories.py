from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import ItemStatus, JobStatus, MailFolderMap, MigrationItem, MigrationJob

_ACTIVE_JOB_STATUSES = (JobStatus.PENDING.value, JobStatus.RUNNING.value)


def has_active_job_for_mapping(db: Session, mapping_id: int) -> bool:
    """True if the given mapping already has a job in pending/running status.

    Used to guard against creating two overlapping jobs for the same
    mapping: since IMAP APPEND is not idempotent, two concurrent jobs for
    the same mapping would both see nothing done yet (via needs_import) and
    both append the same messages, duplicating them in the target mailbox.
    """
    return (
        db.query(MigrationJob)
        .filter(MigrationJob.mapping_id == mapping_id, MigrationJob.status.in_(_ACTIVE_JOB_STATUSES))
        .first()
        is not None
    )


def get_item(db: Session, mapping_id: int, category: str, external_id: str) -> MigrationItem | None:
    return (
        db.query(MigrationItem)
        .filter_by(mapping_id=mapping_id, category=category, external_id=external_id)
        .one_or_none()
    )


def needs_import(item: MigrationItem | None, source_modified_at: datetime | None = None) -> bool:
    if item is None:
        return True
    if item.status == ItemStatus.FAILED.value:
        return True
    if source_modified_at is not None and item.source_modified_at is not None:
        # SQLite doesn't preserve timezone info, so a value read back from the DB
        # comes back naive. record_success normalizes any aware timestamp to UTC
        # before storing it, so labeling a naive DB value as UTC here is correct
        # (not just assumed) -- it just restores the tzinfo that SQLite dropped.
        db_ts = item.source_modified_at
        if db_ts.tzinfo is None and source_modified_at.tzinfo is not None:
            db_ts = db_ts.replace(tzinfo=timezone.utc)
        return source_modified_at > db_ts
    return False


def record_success(
    db: Session,
    mapping_id: int,
    category: str,
    external_id: str,
    target_ref: str,
    source_modified_at: datetime | None = None,
) -> None:
    item = get_item(db, mapping_id, category, external_id)
    if item is None:
        item = MigrationItem(mapping_id=mapping_id, category=category, external_id=external_id)
        db.add(item)
    item.status = ItemStatus.DONE.value
    item.target_ref = target_ref
    if source_modified_at is not None:
        # Normalize to UTC at write time: SQLite/SQLAlchemy's plain DateTime column
        # doesn't preserve timezone info on round-trip (it keeps the naive wall-clock
        # digits, not the UTC-converted instant). Converting here guarantees the DB
        # always holds a UTC-equivalent value, so needs_import's naive-to-UTC labeling
        # on read is correct rather than assumed.
        if source_modified_at.tzinfo is not None:
            source_modified_at = source_modified_at.astimezone(timezone.utc)
        item.source_modified_at = source_modified_at
    item.error_message = None
    db.commit()


def record_failure(db: Session, mapping_id: int, category: str, external_id: str, error_message: str) -> None:
    item = get_item(db, mapping_id, category, external_id)
    if item is None:
        item = MigrationItem(
            mapping_id=mapping_id, category=category, external_id=external_id, status=ItemStatus.FAILED.value
        )
        db.add(item)
    else:
        item.status = ItemStatus.FAILED.value
    item.error_message = error_message
    db.commit()


def get_or_create_folder_map(
    db: Session,
    mapping_id: int,
    graph_folder_id: str,
    graph_path: str,
    imap_mailbox_name: str,
    well_known_type: str | None,
) -> MailFolderMap:
    existing = (
        db.query(MailFolderMap).filter_by(mapping_id=mapping_id, graph_folder_id=graph_folder_id).one_or_none()
    )
    if existing is not None:
        return existing
    entry = MailFolderMap(
        mapping_id=mapping_id,
        graph_folder_id=graph_folder_id,
        graph_path=graph_path,
        imap_mailbox_name=imap_mailbox_name,
        well_known_type=well_known_type,
        created=False,
    )
    db.add(entry)
    db.commit()
    return entry


def mark_folder_created(db: Session, folder_map: MailFolderMap) -> None:
    folder_map.created = True
    db.commit()

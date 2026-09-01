from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import ItemStatus, MailFolderMap, MigrationItem


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
        # SQLite doesn't preserve timezone info, so ensure consistent comparison
        db_ts = item.source_modified_at
        if db_ts.tzinfo is None and source_modified_at.tzinfo is not None:
            # Assume DB datetime is UTC if naive
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

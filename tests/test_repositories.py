from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, ItemStatus, MailboxMapping
from app.db.repositories import get_item, needs_import, record_failure, record_success


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _mapping(db: Session) -> int:
    mapping = MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc")
    db.add(mapping)
    db.commit()
    return mapping.id


def test_needs_import_true_when_item_missing():
    assert needs_import(None) is True


def test_needs_import_false_when_done_and_no_modified_check():
    db = _session()
    mapping_id = _mapping(db)
    record_success(db, mapping_id, "mail", "msg-1", target_ref="42")
    item = get_item(db, mapping_id, "mail", "msg-1")
    assert needs_import(item) is False


def test_needs_import_true_when_previously_failed():
    db = _session()
    mapping_id = _mapping(db)
    record_failure(db, mapping_id, "mail", "msg-1", "boom")
    item = get_item(db, mapping_id, "mail", "msg-1")
    assert needs_import(item) is True
    assert item.status == ItemStatus.FAILED.value


def test_needs_import_detects_newer_source_modification():
    db = _session()
    mapping_id = _mapping(db)
    old_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    record_success(db, mapping_id, "calendar", "evt-1", target_ref="href-1", source_modified_at=old_ts)
    item = get_item(db, mapping_id, "calendar", "evt-1")

    unchanged = needs_import(item, old_ts)
    newer = needs_import(item, old_ts + timedelta(minutes=5))

    assert unchanged is False
    assert newer is True


def test_record_success_upserts_existing_item():
    db = _session()
    mapping_id = _mapping(db)
    record_failure(db, mapping_id, "mail", "msg-1", "boom")
    record_success(db, mapping_id, "mail", "msg-1", target_ref="42")

    item = get_item(db, mapping_id, "mail", "msg-1")
    assert item.status == ItemStatus.DONE.value
    assert item.target_ref == "42"
    assert item.error_message is None

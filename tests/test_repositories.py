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


def test_record_success_normalizes_non_utc_timestamp_to_utc():
    # 10:00 in UTC+2 is 08:00 UTC. SQLite's plain DateTime column drops tzinfo on
    # round-trip and preserves the naive wall-clock digits rather than converting to
    # UTC first -- so if record_success stored the value as-is, a naive read-back
    # would be mislabeled as 10:00 UTC instead of the true 08:00 UTC instant.
    db = _session()
    mapping_id = _mapping(db)
    plus_two = timezone(timedelta(hours=2))
    local_ts = datetime(2026, 1, 1, 10, 0, tzinfo=plus_two)
    expected_utc = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)

    record_success(db, mapping_id, "calendar", "evt-1", target_ref="href-1", source_modified_at=local_ts)

    item = get_item(db, mapping_id, "calendar", "evt-1")
    stored = item.source_modified_at
    assert stored.replace(tzinfo=timezone.utc) == expected_utc

    # And needs_import must compare against the correct UTC instant: a check using
    # the true UTC-equivalent timestamp should see it as "not newer" (no reimport),
    # while a check using the wrong (unconverted) 10:00-UTC interpretation would
    # incorrectly report source_modified_at as still-older.
    assert needs_import(item, expected_utc) is False
    assert needs_import(item, expected_utc + timedelta(minutes=1)) is True


def test_record_success_does_not_wipe_source_modified_at_when_omitted():
    db = _session()
    mapping_id = _mapping(db)
    original_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    record_success(db, mapping_id, "calendar", "evt-1", target_ref="href-1", source_modified_at=original_ts)

    # Re-recording success without passing source_modified_at (e.g. a caller that
    # doesn't track modification times) must not wipe the previously stored value.
    record_success(db, mapping_id, "calendar", "evt-1", target_ref="href-1")

    item = get_item(db, mapping_id, "calendar", "evt-1")
    assert item.source_modified_at is not None
    assert item.source_modified_at.replace(tzinfo=timezone.utc) == original_ts

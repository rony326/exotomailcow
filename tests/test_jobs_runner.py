from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, ItemCategory, JobStatus, JobType, MailboxMapping, MigrationItem, MigrationJob, TenantConfig
from app.graph.models import GraphCalendar, GraphContact, GraphEvent, GraphFolder, GraphMessageRef
from app.importers.base import MailcowTarget
from app.jobs.runner import JobCancelledError, MigrationJobRunner
from app.security.crypto import encrypt


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _mapping_and_job(db: Session, status: str = JobStatus.RUNNING.value) -> tuple[MailboxMapping, MigrationJob]:
    mapping = MailboxMapping(
        exo_upn="user@church.org", mailcow_address="user@mailcow.local",
        app_password_encrypted=encrypt("app-password-plaintext"),
    )
    db.add(mapping)
    db.commit()
    job = MigrationJob(mapping_id=mapping.id, job_type=JobType.INITIAL.value, status=status, dry_run=False)
    db.add(job)
    db.commit()
    return mapping, job


class FakeGraphClient:
    def __init__(self, folders, messages_by_folder, raw_by_id):
        self._folders = folders
        self._messages_by_folder = messages_by_folder
        self._raw_by_id = raw_by_id

    def list_mail_folders(self, user_id):
        return self._folders

    def list_messages(self, user_id, folder_id, since=None):
        return iter(self._messages_by_folder.get(folder_id, []))

    def get_message_raw(self, user_id, message_id):
        return self._raw_by_id[message_id]


class FakeMailImporter:
    def __init__(self):
        self.ensured_folders: list[str] = []
        self.appended: list[tuple] = []
        self.closed = False
        self.connected = False

    def connect(self, target):
        self.connected = True
        return "."

    def ensure_folder(self, imap_path):
        self.ensured_folders.append(imap_path)

    def append_message(self, imap_path, raw_mime, flags, internal_date):
        self.appended.append((imap_path, raw_mime, flags, internal_date))
        return f"uid-{len(self.appended)}"

    def close(self):
        self.closed = True


_TARGET = MailcowTarget(
    address="user@mailcow.local", app_password="pw", imap_host="mail.example.org", dav_base_url="https://mail.example.org"
)


def _runner(graph_client_factory=None, mail_importer_factory=None) -> MigrationJobRunner:
    return MigrationJobRunner(
        db_session_factory=lambda: None,
        graph_client_factory=graph_client_factory or (lambda tenant_config: None),
        mail_importer_factory=mail_importer_factory or FakeMailImporter,
        calendar_importer_factory=lambda: None,
        contact_importer_factory=lambda: None,
        imap_host="mail.example.org",
        dav_base_url="https://mail.example.org",
    )


def test_migrate_mail_creates_folders_and_appends_new_messages():
    db = _session()
    mapping, job = _mapping_and_job(db)

    folders = [GraphFolder(id="inbox", display_name="Inbox", parent_id=None, well_known_name="inbox", child_folder_count=0)]
    messages = {
        "inbox": [
            GraphMessageRef(id="m1", received_date_time=datetime(2026, 1, 1, tzinfo=timezone.utc), is_read=True, is_flagged=False),
        ]
    }
    graph_client = FakeGraphClient(folders, messages, {"m1": b"raw-bytes"})
    importer = FakeMailImporter()

    runner = _runner(mail_importer_factory=lambda: importer)
    runner._migrate_mail(db, job, mapping, graph_client, _TARGET)

    assert importer.ensured_folders == ["INBOX"]
    assert importer.appended[0][0] == "INBOX"
    assert importer.appended[0][2] == ["\\Seen"]
    assert importer.closed is True
    assert job.count_created == 1
    assert job.count_failed == 0


def test_migrate_mail_skips_already_imported_messages():
    db = _session()
    mapping, job = _mapping_and_job(db)
    from app.db.repositories import record_success

    record_success(db, mapping.id, ItemCategory.MAIL.value, "m1", target_ref="99")

    folders = [GraphFolder(id="inbox", display_name="Inbox", parent_id=None, well_known_name="inbox", child_folder_count=0)]
    messages = {
        "inbox": [
            GraphMessageRef(id="m1", received_date_time=datetime(2026, 1, 1, tzinfo=timezone.utc), is_read=True, is_flagged=False),
        ]
    }
    graph_client = FakeGraphClient(folders, messages, {"m1": b"raw-bytes"})
    importer = FakeMailImporter()

    runner = _runner(mail_importer_factory=lambda: importer)
    runner._migrate_mail(db, job, mapping, graph_client, _TARGET)

    assert importer.appended == []
    assert job.count_skipped == 1
    assert job.count_created == 0


def test_migrate_mail_marks_failed_after_max_retries(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    db = _session()
    mapping, job = _mapping_and_job(db)

    folders = [GraphFolder(id="inbox", display_name="Inbox", parent_id=None, well_known_name="inbox", child_folder_count=0)]
    messages = {
        "inbox": [
            GraphMessageRef(id="m1", received_date_time=datetime(2026, 1, 1, tzinfo=timezone.utc), is_read=False, is_flagged=False),
        ]
    }
    graph_client = FakeGraphClient(folders, messages, {"m1": b"raw-bytes"})

    class FailingImporter(FakeMailImporter):
        def append_message(self, imap_path, raw_mime, flags, internal_date):
            raise RuntimeError("IMAP down")

    importer = FailingImporter()
    runner = _runner(mail_importer_factory=lambda: importer)
    runner._migrate_mail(db, job, mapping, graph_client, _TARGET)

    assert job.count_failed == 1
    assert job.count_created == 0
    from app.db.repositories import get_item

    item = get_item(db, mapping.id, ItemCategory.MAIL.value, "m1")
    assert item.status == "failed"
    assert "IMAP down" in item.error_message


def test_migrate_mail_dry_run_counts_without_writing():
    db = _session()
    mapping, job = _mapping_and_job(db)
    job.dry_run = True

    folders = [GraphFolder(id="inbox", display_name="Inbox", parent_id=None, well_known_name="inbox", child_folder_count=0)]
    messages = {
        "inbox": [
            GraphMessageRef(id="m1", received_date_time=datetime(2026, 1, 1, tzinfo=timezone.utc), is_read=True, is_flagged=False),
        ]
    }
    graph_client = FakeGraphClient(folders, messages, {"m1": b"raw-bytes"})
    importer = FakeMailImporter()

    runner = _runner(mail_importer_factory=lambda: importer)
    runner._migrate_mail(db, job, mapping, graph_client, _TARGET)

    assert importer.appended == []
    assert job.count_created == 1
    from app.db.repositories import get_item

    assert get_item(db, mapping.id, ItemCategory.MAIL.value, "m1") is None


def test_migrate_mail_dry_run_does_not_create_folder_on_target_server():
    db = _session()
    mapping, job = _mapping_and_job(db)
    job.dry_run = True

    folders = [GraphFolder(id="inbox", display_name="Inbox", parent_id=None, well_known_name="inbox", child_folder_count=0)]
    messages = {
        "inbox": [
            GraphMessageRef(id="m1", received_date_time=datetime(2026, 1, 1, tzinfo=timezone.utc), is_read=True, is_flagged=False),
        ]
    }
    graph_client = FakeGraphClient(folders, messages, {"m1": b"raw-bytes"})
    importer = FakeMailImporter()

    runner = _runner(mail_importer_factory=lambda: importer)
    runner._migrate_mail(db, job, mapping, graph_client, _TARGET)

    # Critical: dry-run must never issue the server-side CREATE.
    assert importer.ensured_folders == []
    # But connect() (read-only: login + delimiter detection) should still happen,
    # so dry-run can still validate connectivity.
    assert importer.connected is True
    assert importer.closed is True
    # Counting for the dry-run report still works without the folder existing.
    assert job.count_created == 1


def test_migrate_mail_retries_connect_and_succeeds(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    db = _session()
    mapping, job = _mapping_and_job(db)

    folders = [GraphFolder(id="inbox", display_name="Inbox", parent_id=None, well_known_name="inbox", child_folder_count=0)]
    messages = {
        "inbox": [
            GraphMessageRef(id="m1", received_date_time=datetime(2026, 1, 1, tzinfo=timezone.utc), is_read=True, is_flagged=False),
        ]
    }
    graph_client = FakeGraphClient(folders, messages, {"m1": b"raw-bytes"})

    class FlakyConnectImporter(FakeMailImporter):
        def __init__(self):
            super().__init__()
            self.connect_attempts = 0

        def connect(self, target):
            self.connect_attempts += 1
            if self.connect_attempts < 3:
                raise RuntimeError("transient network blip")
            self.connected = True
            return "."

    importer = FlakyConnectImporter()
    runner = _runner(mail_importer_factory=lambda: importer)
    runner._migrate_mail(db, job, mapping, graph_client, _TARGET)

    assert importer.connect_attempts == 3
    assert importer.appended[0][0] == "INBOX"
    assert job.count_created == 1
    assert importer.closed is True


def test_migrate_mail_connect_exhausts_retries_and_still_closes(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    db = _session()
    mapping, job = _mapping_and_job(db)

    folders = [GraphFolder(id="inbox", display_name="Inbox", parent_id=None, well_known_name="inbox", child_folder_count=0)]
    graph_client = FakeGraphClient(folders, {}, {})

    class AlwaysFailingConnectImporter(FakeMailImporter):
        def connect(self, target):
            raise RuntimeError("IMAP server unreachable")

    importer = AlwaysFailingConnectImporter()
    runner = _runner(mail_importer_factory=lambda: importer)

    import pytest

    with pytest.raises(RuntimeError, match="IMAP server unreachable"):
        runner._migrate_mail(db, job, mapping, graph_client, _TARGET)

    assert importer.closed is True


def test_migrate_mail_raises_when_job_already_cancelled():
    db = _session()
    mapping, job = _mapping_and_job(db, status=JobStatus.CANCELLED.value)

    folders = [GraphFolder(id="inbox", display_name="Inbox", parent_id=None, well_known_name="inbox", child_folder_count=0)]
    messages = {
        "inbox": [
            GraphMessageRef(id="m1", received_date_time=datetime(2026, 1, 1, tzinfo=timezone.utc), is_read=True, is_flagged=False),
        ]
    }
    graph_client = FakeGraphClient(folders, messages, {"m1": b"raw-bytes"})
    importer = FakeMailImporter()
    runner = _runner(mail_importer_factory=lambda: importer)

    import pytest

    with pytest.raises(JobCancelledError):
        runner._migrate_mail(db, job, mapping, graph_client, _TARGET)

    assert importer.appended == []


class FakeCalendarGraphClient:
    def __init__(self, calendars, events_by_calendar):
        self._calendars = calendars
        self._events_by_calendar = events_by_calendar

    def list_calendars(self, user_id):
        return self._calendars

    def list_events(self, user_id, calendar_id, modified_since=None):
        return iter(self._events_by_calendar.get(calendar_id, []))


class FakeCalendarImporter:
    def __init__(self):
        self.put_calls: list[tuple] = []

    def put_event(self, target, uid, ics_data):
        self.put_calls.append((uid, ics_data))
        return f"https://mail.example.org/SOGo/dav/x/Calendar/{uid}.ics"


class FakeContactsGraphClient:
    def __init__(self, contacts):
        self._contacts = contacts

    def list_contacts(self, user_id, modified_since=None):
        return iter(self._contacts)


class FakeContactImporter:
    def __init__(self):
        self.put_calls: list[tuple] = []

    def put_contact(self, target, uid, vcard_data):
        self.put_calls.append((uid, vcard_data))
        return f"https://mail.example.org/SOGo/dav/x/Contacts/{uid}.vcf"


def _event(uid="evt-1", modified=datetime(2026, 2, 1, tzinfo=timezone.utc)) -> GraphEvent:
    return GraphEvent(
        id="graph-evt-1", ics_uid=uid, last_modified_date_time=modified, subject="Sitzung",
        start=modified, end=modified + timedelta(hours=1), is_all_day=False, location=None,
        body_html=None, organizer_email=None, attendees=[],
    )


def test_migrate_calendar_creates_and_updates(monkeypatch):
    db = _session()
    mapping, job = _mapping_and_job(db)
    old_ts = datetime(2026, 2, 1, tzinfo=timezone.utc)
    graph_client = FakeCalendarGraphClient(
        [GraphCalendar(id="cal-1", name="Kalender")], {"cal-1": [_event(modified=old_ts)]}
    )
    importer = FakeCalendarImporter()
    runner = _runner()
    runner._calendar_importer_factory = lambda: importer

    runner._migrate_calendar(db, job, mapping, graph_client, _TARGET, modified_since=None)
    assert job.count_created == 1
    assert len(importer.put_calls) == 1

    new_ts = old_ts + timedelta(hours=2)
    graph_client2 = FakeCalendarGraphClient(
        [GraphCalendar(id="cal-1", name="Kalender")], {"cal-1": [_event(modified=new_ts)]}
    )
    runner._migrate_calendar(db, job, mapping, graph_client2, _TARGET, modified_since=None)
    assert job.count_updated == 1
    assert len(importer.put_calls) == 2


def test_migrate_calendar_skips_unchanged_event():
    db = _session()
    mapping, job = _mapping_and_job(db)
    ts = datetime(2026, 2, 1, tzinfo=timezone.utc)
    graph_client = FakeCalendarGraphClient([GraphCalendar(id="cal-1", name="Kalender")], {"cal-1": [_event(modified=ts)]})
    importer = FakeCalendarImporter()
    runner = _runner()
    runner._calendar_importer_factory = lambda: importer

    runner._migrate_calendar(db, job, mapping, graph_client, _TARGET, modified_since=None)
    runner._migrate_calendar(db, job, mapping, graph_client, _TARGET, modified_since=None)

    assert job.count_created == 1
    assert job.count_skipped == 1
    assert len(importer.put_calls) == 1


def test_migrate_contacts_imports_new_contact():
    db = _session()
    mapping, job = _mapping_and_job(db)
    contact = GraphContact(id="c1", last_modified_date_time=datetime(2026, 1, 1, tzinfo=timezone.utc), display_name="Maria")
    graph_client = FakeContactsGraphClient([contact])
    importer = FakeContactImporter()
    runner = _runner()
    runner._contact_importer_factory = lambda: importer

    runner._migrate_contacts(db, job, mapping, graph_client, _TARGET, modified_since=None)

    assert job.count_created == 1
    assert importer.put_calls[0][0] == "c1"


def _contact(id="c1", modified=datetime(2026, 1, 1, tzinfo=timezone.utc), display_name="Maria") -> GraphContact:
    return GraphContact(id=id, last_modified_date_time=modified, display_name=display_name)


def test_migrate_contacts_updates_changed_contact_and_skips_unchanged():
    db = _session()
    mapping, job = _mapping_and_job(db)
    old_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    graph_client = FakeContactsGraphClient([_contact(modified=old_ts)])
    importer = FakeContactImporter()
    runner = _runner()
    runner._contact_importer_factory = lambda: importer

    runner._migrate_contacts(db, job, mapping, graph_client, _TARGET, modified_since=None)
    assert job.count_created == 1
    assert len(importer.put_calls) == 1

    # Same timestamp again -> unchanged -> skipped, no re-import.
    runner._migrate_contacts(db, job, mapping, graph_client, _TARGET, modified_since=None)
    assert job.count_skipped == 1
    assert len(importer.put_calls) == 1

    # Newer timestamp -> update.
    new_ts = old_ts + timedelta(hours=2)
    graph_client2 = FakeContactsGraphClient([_contact(modified=new_ts)])
    runner._migrate_contacts(db, job, mapping, graph_client2, _TARGET, modified_since=None)
    assert job.count_updated == 1
    assert len(importer.put_calls) == 2


def test_migrate_calendar_dry_run_counts_without_writing():
    db = _session()
    mapping, job = _mapping_and_job(db)
    job.dry_run = True
    old_ts = datetime(2026, 2, 1, tzinfo=timezone.utc)
    graph_client = FakeCalendarGraphClient(
        [GraphCalendar(id="cal-1", name="Kalender")], {"cal-1": [_event(modified=old_ts)]}
    )
    importer = FakeCalendarImporter()
    runner = _runner()
    runner._calendar_importer_factory = lambda: importer

    runner._migrate_calendar(db, job, mapping, graph_client, _TARGET, modified_since=None)

    assert job.count_created == 1
    assert importer.put_calls == []
    from app.db.repositories import get_item

    assert get_item(db, mapping.id, ItemCategory.CALENDAR.value, "evt-1") is None

    # A second dry-run pass over the *same* unchanged event still counts as a
    # create (no MigrationItem row was ever written), not a skip or update.
    new_ts = old_ts + timedelta(hours=2)
    graph_client2 = FakeCalendarGraphClient(
        [GraphCalendar(id="cal-1", name="Kalender")], {"cal-1": [_event(modified=new_ts)]}
    )
    runner._migrate_calendar(db, job, mapping, graph_client2, _TARGET, modified_since=None)

    assert job.count_created == 2
    assert job.count_updated == 0
    assert importer.put_calls == []


def test_migrate_contacts_dry_run_counts_without_writing():
    db = _session()
    mapping, job = _mapping_and_job(db)
    job.dry_run = True
    contact = _contact(modified=datetime(2026, 1, 1, tzinfo=timezone.utc))
    graph_client = FakeContactsGraphClient([contact])
    importer = FakeContactImporter()
    runner = _runner()
    runner._contact_importer_factory = lambda: importer

    runner._migrate_contacts(db, job, mapping, graph_client, _TARGET, modified_since=None)

    assert job.count_created == 1
    assert importer.put_calls == []
    from app.db.repositories import get_item

    assert get_item(db, mapping.id, ItemCategory.CONTACTS.value, "c1") is None


def test_migrate_calendar_marks_failed_after_max_retries(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    db = _session()
    mapping, job = _mapping_and_job(db)
    graph_client = FakeCalendarGraphClient(
        [GraphCalendar(id="cal-1", name="Kalender")], {"cal-1": [_event()]}
    )

    class FailingCalendarImporter(FakeCalendarImporter):
        def put_event(self, target, uid, ics_data):
            raise RuntimeError("CalDAV down")

    importer = FailingCalendarImporter()
    runner = _runner()
    runner._calendar_importer_factory = lambda: importer

    # Must not raise/abort the job despite every attempt failing.
    runner._migrate_calendar(db, job, mapping, graph_client, _TARGET, modified_since=None)

    assert job.count_failed == 1
    assert job.count_created == 0
    from app.db.repositories import get_item

    item = get_item(db, mapping.id, ItemCategory.CALENDAR.value, "evt-1")
    assert item.status == "failed"
    assert "CalDAV down" in item.error_message


def test_migrate_contacts_marks_failed_after_max_retries(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    db = _session()
    mapping, job = _mapping_and_job(db)
    graph_client = FakeContactsGraphClient([_contact()])

    class FailingContactImporter(FakeContactImporter):
        def put_contact(self, target, uid, vcard_data):
            raise RuntimeError("CardDAV down")

    importer = FailingContactImporter()
    runner = _runner()
    runner._contact_importer_factory = lambda: importer

    # Must not raise/abort the job despite every attempt failing.
    runner._migrate_contacts(db, job, mapping, graph_client, _TARGET, modified_since=None)

    assert job.count_failed == 1
    assert job.count_created == 0
    from app.db.repositories import get_item

    item = get_item(db, mapping.id, ItemCategory.CONTACTS.value, "c1")
    assert item.status == "failed"
    assert "CardDAV down" in item.error_message


def _make_session_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_run_completes_mail_only_job_and_updates_last_synced_at():
    session_factory = _make_session_factory()
    db = session_factory()
    db.add(TenantConfig(admin_user="a", admin_password_hash="h"))
    mapping, job = _mapping_and_job(db, status=JobStatus.PENDING.value)
    job.migrate_mail, job.migrate_calendar, job.migrate_contacts = True, False, False
    db.commit()
    job_id = job.id

    folders = [GraphFolder(id="inbox", display_name="Inbox", parent_id=None, well_known_name="inbox", child_folder_count=0)]
    graph_client = FakeGraphClient(folders, {"inbox": []}, {})

    runner = MigrationJobRunner(
        db_session_factory=session_factory,
        graph_client_factory=lambda tc: graph_client,
        mail_importer_factory=FakeMailImporter,
        calendar_importer_factory=lambda: None,
        contact_importer_factory=lambda: None,
        imap_host="mail.example.org",
        dav_base_url="https://mail.example.org",
    )
    runner.run(job_id)

    db.expire_all()
    reloaded = db.get(MigrationJob, job_id)
    db.refresh(mapping)
    assert reloaded.status == JobStatus.COMPLETED.value
    assert reloaded.finished_at is not None
    assert mapping.last_synced_at == reloaded.started_at


def test_run_dry_run_does_not_update_last_synced_at():
    session_factory = _make_session_factory()
    db = session_factory()
    db.add(TenantConfig(admin_user="a", admin_password_hash="h"))
    mapping, job = _mapping_and_job(db, status=JobStatus.PENDING.value)
    job.migrate_mail, job.migrate_calendar, job.migrate_contacts = True, False, False
    job.dry_run = True
    db.commit()
    job_id = job.id

    folders = [GraphFolder(id="inbox", display_name="Inbox", parent_id=None, well_known_name="inbox", child_folder_count=0)]
    graph_client = FakeGraphClient(folders, {"inbox": []}, {})

    runner = MigrationJobRunner(
        db_session_factory=session_factory,
        graph_client_factory=lambda tc: graph_client,
        mail_importer_factory=FakeMailImporter,
        calendar_importer_factory=lambda: None,
        contact_importer_factory=lambda: None,
        imap_host="mail.example.org",
        dav_base_url="https://mail.example.org",
    )
    runner.run(job_id)

    db.expire_all()
    db.refresh(mapping)
    assert mapping.last_synced_at is None


def test_run_resync_job_auto_derives_mail_since_date():
    session_factory = _make_session_factory()
    db = session_factory()
    db.add(TenantConfig(admin_user="a", admin_password_hash="h"))
    mapping = MailboxMapping(
        exo_upn="a@b", mailcow_address="a@c", app_password_encrypted=encrypt("app-password-plaintext"),
        last_synced_at=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
    )
    db.add(mapping)
    db.commit()
    job = MigrationJob(mapping_id=mapping.id, job_type=JobType.RESYNC.value, status=JobStatus.PENDING.value,
                        migrate_mail=True, migrate_calendar=False, migrate_contacts=False)
    db.add(job)
    db.commit()
    job_id = job.id
    # Capture the pre-run value: run() overwrites mapping.last_synced_at on
    # successful non-dry-run completion, so it must not be re-read afterward
    # to compute the expected derived mail_since_date.
    original_last_synced_at = mapping.last_synced_at

    folders = [GraphFolder(id="inbox", display_name="Inbox", parent_id=None, well_known_name="inbox", child_folder_count=0)]
    graph_client = FakeGraphClient(folders, {"inbox": []}, {})

    runner = MigrationJobRunner(
        db_session_factory=session_factory,
        graph_client_factory=lambda tc: graph_client,
        mail_importer_factory=FakeMailImporter,
        calendar_importer_factory=lambda: None,
        contact_importer_factory=lambda: None,
        imap_host="mail.example.org",
        dav_base_url="https://mail.example.org",
    )
    runner.run(job_id)

    from app.jobs.resync import RESYNC_BUFFER

    db.expire_all()
    reloaded = db.get(MigrationJob, job_id)
    assert reloaded.mail_since_date == original_last_synced_at - RESYNC_BUFFER


class BrokenGraphClient:
    def list_mail_folders(self, user_id):
        raise RuntimeError("Graph unavailable")


def test_run_marks_job_failed_on_unexpected_exception():
    session_factory = _make_session_factory()
    db = session_factory()
    db.add(TenantConfig(admin_user="a", admin_password_hash="h"))
    mapping, job = _mapping_and_job(db, status=JobStatus.PENDING.value)
    job.migrate_mail, job.migrate_calendar, job.migrate_contacts = True, False, False
    db.commit()
    job_id = job.id

    runner = MigrationJobRunner(
        db_session_factory=session_factory,
        graph_client_factory=lambda tc: BrokenGraphClient(),
        mail_importer_factory=FakeMailImporter,
        calendar_importer_factory=lambda: None,
        contact_importer_factory=lambda: None,
        imap_host="mail.example.org",
        dav_base_url="https://mail.example.org",
    )

    import pytest

    with pytest.raises(RuntimeError):
        runner.run(job_id)

    db.expire_all()
    reloaded = db.get(MigrationJob, job_id)
    assert reloaded.status == JobStatus.FAILED.value
    assert reloaded.finished_at is not None


def test_run_marks_job_failed_when_graph_client_factory_raises_before_dispatch():
    # Regression test for the status-transition bug: job/mapping/tenant_config
    # loading, resync-date derivation, graph_client_factory(...), and
    # MailcowTarget(..., decrypt(...)) construction all happen BEFORE the
    # three _migrate_* dispatch calls. If any of that setup raises (e.g. an
    # auth/token failure constructing the real GraphClient, or a corrupted
    # app-password ciphertext), the job must still be marked FAILED with
    # finished_at set -- not left stuck at RUNNING forever.
    session_factory = _make_session_factory()
    db = session_factory()
    db.add(TenantConfig(admin_user="a", admin_password_hash="h"))
    mapping, job = _mapping_and_job(db, status=JobStatus.PENDING.value)
    job.migrate_mail, job.migrate_calendar, job.migrate_contacts = True, False, False
    db.commit()
    job_id = job.id

    def _broken_graph_client_factory(tenant_config):
        raise RuntimeError("token acquisition failed")

    runner = MigrationJobRunner(
        db_session_factory=session_factory,
        graph_client_factory=_broken_graph_client_factory,
        mail_importer_factory=FakeMailImporter,
        calendar_importer_factory=lambda: None,
        contact_importer_factory=lambda: None,
        imap_host="mail.example.org",
        dav_base_url="https://mail.example.org",
    )

    import pytest

    with pytest.raises(RuntimeError):
        runner.run(job_id)

    db.expire_all()
    reloaded = db.get(MigrationJob, job_id)
    assert reloaded.status == JobStatus.FAILED.value
    assert reloaded.finished_at is not None


def test_run_marks_job_failed_when_tenant_config_missing():
    # Regression test for fix-round-2 finding #1: before this fix, the
    # `mapping = db.get(...)` / `tenant_config = db.query(TenantConfig).one()`
    # lookups happened *before* the try block (only `job = db.get(...)` plus
    # the RUNNING transition were meant to stay outside it). A NoResultFound
    # from `.one()` (e.g. no tenant configured yet) therefore escaped both
    # the except and the finally, leaving the job stuck at RUNNING forever --
    # the same zombie-job bug as the graph_client_factory case, just
    # triggered one step earlier. No TenantConfig row is created here.
    session_factory = _make_session_factory()
    db = session_factory()
    mapping, job = _mapping_and_job(db, status=JobStatus.PENDING.value)
    job.migrate_mail, job.migrate_calendar, job.migrate_contacts = True, False, False
    db.commit()
    job_id = job.id

    runner = MigrationJobRunner(
        db_session_factory=session_factory,
        graph_client_factory=lambda tc: FakeGraphClient([], {}, {}),
        mail_importer_factory=FakeMailImporter,
        calendar_importer_factory=lambda: None,
        contact_importer_factory=lambda: None,
        imap_host="mail.example.org",
        dav_base_url="https://mail.example.org",
    )

    import pytest
    from sqlalchemy.exc import NoResultFound

    with pytest.raises(NoResultFound):
        runner.run(job_id)

    db.expire_all()
    reloaded = db.get(MigrationJob, job_id)
    assert reloaded.status == JobStatus.FAILED.value
    assert reloaded.finished_at is not None


def test_run_resync_commit_failure_is_recovered_via_rollback():
    # Regression test for fix-round-2 finding #2: if the resync-date
    # `db.commit()` (inside the try block) raises with a genuine
    # SQLAlchemy/DBAPI-level failure, the session's transaction is left in a
    # state that requires an explicit rollback() before it can be used
    # again. Without that rollback, the finally block's `db.commit()` --
    # meant to persist `job.status = FAILED` / `finished_at` -- itself
    # raises PendingRollbackError, masking the real failure and losing the
    # FAILED status write (the job would appear stuck at RUNNING to any
    # caller).
    #
    # A Python-level exception raised directly from a `before_flush` event
    # does NOT reproduce this (verified separately: SQLAlchemy only enters
    # the "must rollback before reuse" state after a real backend/DBAPI
    # error during flush, not an arbitrary exception from an event hook).
    # So this test injects a genuine UNIQUE-constraint violation into the
    # *same* flush that persists `job.mail_since_date`, by adding two
    # conflicting MigrationItem rows to the session from inside the
    # `before_flush` hook once it sees the job in its "about to commit the
    # resync date" state -- SQLAlchemy folds newly-added objects into the
    # flush already in progress, so this really is the same commit() call,
    # and the resulting IntegrityError really does leave the session broken
    # until rollback() is called.
    from sqlalchemy import event

    session_factory = _make_session_factory()
    db = session_factory()
    db.add(TenantConfig(admin_user="a", admin_password_hash="h"))
    mapping = MailboxMapping(
        exo_upn="a@b", mailcow_address="a@c", app_password_encrypted=encrypt("app-password-plaintext"),
        last_synced_at=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
    )
    db.add(mapping)
    db.commit()
    job = MigrationJob(mapping_id=mapping.id, job_type=JobType.RESYNC.value, status=JobStatus.PENDING.value,
                        migrate_mail=True, migrate_calendar=False, migrate_contacts=False)
    db.add(job)
    db.commit()
    job_id = job.id
    mapping_id = mapping.id

    triggered = {"done": False}

    def _inject_conflict_on_resync_date_flush(session, flush_context, instances):
        if triggered["done"]:
            return
        for obj in list(session.dirty):
            if (
                isinstance(obj, MigrationJob)
                and obj.id == job_id
                and obj.status == JobStatus.RUNNING.value
                and obj.mail_since_date is not None
            ):
                triggered["done"] = True
                # Two rows with the same (mapping_id, category, external_id)
                # violate MigrationItem's uq_migration_item constraint.
                session.add(MigrationItem(mapping_id=mapping_id, category=ItemCategory.MAIL.value, external_id="dupe-conflict"))
                session.add(MigrationItem(mapping_id=mapping_id, category=ItemCategory.MAIL.value, external_id="dupe-conflict"))
                break

    event.listen(session_factory, "before_flush", _inject_conflict_on_resync_date_flush)
    try:
        runner = MigrationJobRunner(
            db_session_factory=session_factory,
            graph_client_factory=lambda tc: FakeGraphClient([], {}, {}),
            mail_importer_factory=FakeMailImporter,
            calendar_importer_factory=lambda: None,
            contact_importer_factory=lambda: None,
            imap_host="mail.example.org",
            dav_base_url="https://mail.example.org",
        )

        import pytest
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            runner.run(job_id)
    finally:
        event.remove(session_factory, "before_flush", _inject_conflict_on_resync_date_flush)

    # Sanity check the injected conflict actually fired -- otherwise this
    # test would pass vacuously without exercising the target code path.
    assert triggered["done"] is True

    db.expire_all()
    reloaded = db.get(MigrationJob, job_id)
    assert reloaded.status == JobStatus.FAILED.value
    assert reloaded.finished_at is not None


def test_run_leaves_status_cancelled_when_job_was_cancelled_mid_run():
    session_factory = _make_session_factory()
    db = session_factory()
    db.add(TenantConfig(admin_user="a", admin_password_hash="h"))
    mapping, job = _mapping_and_job(db, status=JobStatus.PENDING.value)
    job.migrate_mail, job.migrate_calendar, job.migrate_contacts = True, False, False
    db.commit()
    job_id = job.id

    folders = [GraphFolder(id="inbox", display_name="Inbox", parent_id=None, well_known_name="inbox", child_folder_count=0)]
    graph_client = FakeGraphClient(folders, {}, {})

    class CancellingImporter(FakeMailImporter):
        def connect(self, target):
            db.query(MigrationJob).filter_by(id=job_id).update({"status": JobStatus.CANCELLED.value})
            db.commit()
            return "."

    runner = MigrationJobRunner(
        db_session_factory=session_factory,
        graph_client_factory=lambda tc: graph_client,
        mail_importer_factory=CancellingImporter,
        calendar_importer_factory=lambda: None,
        contact_importer_factory=lambda: None,
        imap_host="mail.example.org",
        dav_base_url="https://mail.example.org",
    )
    runner.run(job_id)

    db.expire_all()
    reloaded = db.get(MigrationJob, job_id)
    assert reloaded.status == JobStatus.CANCELLED.value
    assert reloaded.finished_at is not None

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, ItemCategory, JobStatus, JobType, MailboxMapping, MigrationJob
from app.graph.models import GraphCalendar, GraphContact, GraphEvent, GraphFolder, GraphMessageRef
from app.importers.base import MailcowTarget
from app.jobs.runner import JobCancelledError, MigrationJobRunner


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _mapping_and_job(db: Session, status: str = JobStatus.RUNNING.value) -> tuple[MailboxMapping, MigrationJob]:
    mapping = MailboxMapping(exo_upn="user@church.org", mailcow_address="user@mailcow.local", app_password_encrypted="enc")
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

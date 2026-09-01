from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, ItemCategory, JobStatus, JobType, MailboxMapping, MigrationJob
from app.graph.models import GraphFolder, GraphMessageRef
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

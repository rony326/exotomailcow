import time
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.conversion.ics import graph_event_to_ics
from app.conversion.vcard import graph_contact_to_vcard
from app.db.models import ItemCategory, JobStatus, JobType, MailboxMapping, MigrationJob, TenantConfig
from app.db.repositories import get_item, get_or_create_folder_map, mark_folder_created, needs_import, record_failure, record_success
from app.graph.client import GraphClient
from app.importers.base import CalendarImporter, ContactImporter, MailcowTarget, MailImporter
from app.importers.folder_mapping import build_folder_paths, build_imap_path
from app.jobs.resync import RESYNC_BUFFER
from app.security.crypto import decrypt

MAX_ITEM_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2


class JobCancelledError(Exception):
    pass


def _commit_with_retry(db: Session) -> None:
    """Commit with the same per-item retry/backoff as the surrounding item
    processing.

    This commit persists the job's running counters (count_created/
    count_skipped/count_updated/count_failed) after each item and sits
    outside the per-item try/except above (record_success/record_failure
    already commit their own writes inside that try). Without this retry
    wrapper, a single transient SQLite lock-contention error on this commit
    would propagate all the way out of _migrate_mail/_migrate_calendar/
    _migrate_contacts to run()'s exception handler and fail the entire
    mailbox job over one item's commit contention, instead of being
    retried like the rest of that item's processing.
    """
    for attempt in range(1, MAX_ITEM_RETRIES + 1):
        try:
            db.commit()
            return
        except Exception:
            db.rollback()
            if attempt == MAX_ITEM_RETRIES:
                raise
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)


class MigrationJobRunner:
    def __init__(
        self,
        db_session_factory: Callable[[], Session],
        graph_client_factory: Callable[[TenantConfig], GraphClient],
        mail_importer_factory: Callable[[], MailImporter],
        calendar_importer_factory: Callable[[], CalendarImporter],
        contact_importer_factory: Callable[[], ContactImporter],
        imap_host: str,
        dav_base_url: str,
        imap_port: int = 993,
    ) -> None:
        self._db_session_factory = db_session_factory
        self._graph_client_factory = graph_client_factory
        self._mail_importer_factory = mail_importer_factory
        self._calendar_importer_factory = calendar_importer_factory
        self._contact_importer_factory = contact_importer_factory
        self._imap_host = imap_host
        self._imap_port = imap_port
        self._dav_base_url = dav_base_url

    def _is_cancelled(self, db: Session, job: MigrationJob) -> bool:
        db.refresh(job)
        return job.status == JobStatus.CANCELLED.value

    def _migrate_mail(self, db: Session, job: MigrationJob, mapping: MailboxMapping, graph_client, target: MailcowTarget) -> None:
        importer = self._mail_importer_factory()
        try:
            delimiter = None
            for attempt in range(1, MAX_ITEM_RETRIES + 1):
                try:
                    delimiter = importer.connect(target)
                    break
                except Exception:
                    if attempt == MAX_ITEM_RETRIES:
                        raise
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)

            folders = graph_client.list_mail_folders(mapping.exo_upn)
            paths = build_folder_paths(folders)
            for folder in folders:
                if self._is_cancelled(db, job):
                    raise JobCancelledError()

                graph_path = paths[folder.id]
                imap_path = build_imap_path(graph_path, folder.well_known_name, delimiter)
                folder_map = get_or_create_folder_map(db, mapping.id, folder.id, graph_path, imap_path, folder.well_known_name)
                if not folder_map.created and not job.dry_run:
                    importer.ensure_folder(imap_path)
                    mark_folder_created(db, folder_map)

                for msg_ref in graph_client.list_messages(mapping.exo_upn, folder.id, since=job.mail_since_date):
                    if self._is_cancelled(db, job):
                        raise JobCancelledError()
                    self._migrate_one_message(db, job, mapping, graph_client, importer, imap_path, msg_ref)
        finally:
            importer.close()

    def _migrate_one_message(self, db, job, mapping, graph_client, importer, imap_path, msg_ref) -> None:
        existing = get_item(db, mapping.id, ItemCategory.MAIL.value, msg_ref.id)

        if not needs_import(existing):
            job.count_skipped += 1
        elif job.dry_run:
            job.count_created += 1
        else:
            flags = []
            if msg_ref.is_read:
                flags.append("\\Seen")
            if msg_ref.is_flagged:
                flags.append("\\Flagged")
            for attempt in range(1, MAX_ITEM_RETRIES + 1):
                try:
                    raw = graph_client.get_message_raw(mapping.exo_upn, msg_ref.id)
                    uid = importer.append_message(imap_path, raw, flags, msg_ref.received_date_time)
                    record_success(db, mapping.id, ItemCategory.MAIL.value, msg_ref.id, target_ref=uid)
                    job.count_created += 1
                    break
                except Exception as exc:
                    if attempt == MAX_ITEM_RETRIES:
                        record_failure(db, mapping.id, ItemCategory.MAIL.value, msg_ref.id, str(exc))
                        job.count_failed += 1
                    else:
                        time.sleep(RETRY_BACKOFF_SECONDS * attempt)

        _commit_with_retry(db)

    def _migrate_calendar(self, db: Session, job: MigrationJob, mapping: MailboxMapping, graph_client, target: MailcowTarget, modified_since) -> None:
        importer = self._calendar_importer_factory()
        calendars = graph_client.list_calendars(mapping.exo_upn)
        for calendar in calendars:
            for event in graph_client.list_events(mapping.exo_upn, calendar.id, modified_since=modified_since):
                if self._is_cancelled(db, job):
                    raise JobCancelledError()
                self._migrate_one_calendar_event(db, job, mapping, importer, target, event)

    def _migrate_one_calendar_event(self, db, job, mapping, importer, target, event) -> None:
        existing = get_item(db, mapping.id, ItemCategory.CALENDAR.value, event.ics_uid)
        is_update = existing is not None

        if not needs_import(existing, event.last_modified_date_time):
            job.count_skipped += 1
        elif job.dry_run:
            job.count_updated += 1 if is_update else 0
            job.count_created += 0 if is_update else 1
        else:
            for attempt in range(1, MAX_ITEM_RETRIES + 1):
                try:
                    ics_data = graph_event_to_ics(event)
                    href = importer.put_event(target, event.ics_uid, ics_data)
                    record_success(
                        db, mapping.id, ItemCategory.CALENDAR.value, event.ics_uid,
                        target_ref=href, source_modified_at=event.last_modified_date_time,
                    )
                    job.count_updated += 1 if is_update else 0
                    job.count_created += 0 if is_update else 1
                    break
                except Exception as exc:
                    if attempt == MAX_ITEM_RETRIES:
                        record_failure(db, mapping.id, ItemCategory.CALENDAR.value, event.ics_uid, str(exc))
                        job.count_failed += 1
                    else:
                        time.sleep(RETRY_BACKOFF_SECONDS * attempt)

        _commit_with_retry(db)

    def _migrate_contacts(self, db: Session, job: MigrationJob, mapping: MailboxMapping, graph_client, target: MailcowTarget, modified_since) -> None:
        importer = self._contact_importer_factory()
        for contact in graph_client.list_contacts(mapping.exo_upn, modified_since=modified_since):
            if self._is_cancelled(db, job):
                raise JobCancelledError()
            self._migrate_one_contact(db, job, mapping, importer, target, contact)

    def _migrate_one_contact(self, db, job, mapping, importer, target, contact) -> None:
        existing = get_item(db, mapping.id, ItemCategory.CONTACTS.value, contact.id)
        is_update = existing is not None

        if not needs_import(existing, contact.last_modified_date_time):
            job.count_skipped += 1
        elif job.dry_run:
            job.count_updated += 1 if is_update else 0
            job.count_created += 0 if is_update else 1
        else:
            for attempt in range(1, MAX_ITEM_RETRIES + 1):
                try:
                    vcard_data = graph_contact_to_vcard(contact)
                    href = importer.put_contact(target, contact.id, vcard_data)
                    record_success(
                        db, mapping.id, ItemCategory.CONTACTS.value, contact.id,
                        target_ref=href, source_modified_at=contact.last_modified_date_time,
                    )
                    job.count_updated += 1 if is_update else 0
                    job.count_created += 0 if is_update else 1
                    break
                except Exception as exc:
                    if attempt == MAX_ITEM_RETRIES:
                        record_failure(db, mapping.id, ItemCategory.CONTACTS.value, contact.id, str(exc))
                        job.count_failed += 1
                    else:
                        time.sleep(RETRY_BACKOFF_SECONDS * attempt)

        _commit_with_retry(db)

    def run(self, job_id: int) -> None:
        db = self._db_session_factory()
        try:
            job = db.get(MigrationJob, job_id)

            # Guarded START transition: only move PENDING -> RUNNING. If
            # Scheduler.cancel() already flipped this job to CANCELLED while
            # it was sitting in the thread pool's queue, this UPDATE matches
            # zero rows and we must not run any migration work at all.
            start_result = db.execute(
                update(MigrationJob)
                .where(MigrationJob.id == job_id, MigrationJob.status == JobStatus.PENDING.value)
                .values(status=JobStatus.RUNNING.value, started_at=datetime.now(timezone.utc))
            )
            db.commit()
            if start_result.rowcount == 0:
                # Already cancelled (or otherwise not PENDING) before we
                # could start -- nothing to do.
                return
            db.refresh(job)

            try:
                mapping = db.get(MailboxMapping, job.mapping_id)
                tenant_config = db.query(TenantConfig).one()

                modified_since = None
                if job.job_type == JobType.RESYNC.value and mapping.last_synced_at is not None:
                    modified_since = mapping.last_synced_at - RESYNC_BUFFER
                    if job.mail_since_date is None:
                        job.mail_since_date = modified_since
                        db.commit()

                graph_client = self._graph_client_factory(tenant_config)
                target = MailcowTarget(
                    address=mapping.mailcow_address,
                    app_password=decrypt(mapping.app_password_encrypted),
                    imap_host=self._imap_host,
                    imap_port=self._imap_port,
                    dav_base_url=self._dav_base_url,
                )

                if job.migrate_mail:
                    self._migrate_mail(db, job, mapping, graph_client, target)
                if job.migrate_calendar:
                    self._migrate_calendar(db, job, mapping, graph_client, target, modified_since)
                if job.migrate_contacts:
                    self._migrate_contacts(db, job, mapping, graph_client, target, modified_since)
                if not job.dry_run:
                    mapping.last_synced_at = job.started_at
                # Guarded TERMINAL write: only move RUNNING -> COMPLETED. If a
                # concurrent Scheduler.cancel() already flipped this job to
                # CANCELLED while we were finishing up, don't stomp on it.
                db.execute(
                    update(MigrationJob)
                    .where(MigrationJob.id == job_id, MigrationJob.status == JobStatus.RUNNING.value)
                    .values(status=JobStatus.COMPLETED.value)
                )
            except JobCancelledError:
                pass
            except Exception as exc:
                db.rollback()
                # Guarded TERMINAL write: only move RUNNING -> FAILED, for the
                # same reason as the COMPLETED case above. Also record the
                # reason so an operator can see why without reading container
                # logs (a job-level failure, e.g. a bad IMAP login before any
                # item was even attempted, otherwise leaves no trace anywhere
                # the GUI can show — migration_item.error_message only exists
                # per-item, and none may have been created yet).
                db.execute(
                    update(MigrationJob)
                    .where(MigrationJob.id == job_id, MigrationJob.status == JobStatus.RUNNING.value)
                    .values(status=JobStatus.FAILED.value, error_message=str(exc)[:2000])
                )
                raise
            finally:
                job.finished_at = datetime.now(timezone.utc)
                db.commit()
        finally:
            db.close()

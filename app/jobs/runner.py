import time
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.db.models import ItemCategory, JobStatus, MailboxMapping, MigrationJob, TenantConfig
from app.db.repositories import get_item, get_or_create_folder_map, mark_folder_created, needs_import, record_failure, record_success
from app.graph.client import GraphClient
from app.importers.base import CalendarImporter, ContactImporter, MailcowTarget, MailImporter
from app.importers.folder_mapping import build_folder_paths, build_imap_path

MAX_ITEM_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2


class JobCancelledError(Exception):
    pass


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
        delimiter = importer.connect(target)
        try:
            folders = graph_client.list_mail_folders(mapping.exo_upn)
            paths = build_folder_paths(folders)
            for folder in folders:
                if self._is_cancelled(db, job):
                    raise JobCancelledError()

                graph_path = paths[folder.id]
                imap_path = build_imap_path(graph_path, folder.well_known_name, delimiter)
                folder_map = get_or_create_folder_map(db, mapping.id, folder.id, graph_path, imap_path, folder.well_known_name)
                if not folder_map.created:
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

        db.commit()

import threading

from app.config import get_settings
from app.db.models import TenantConfig
from app.db.session import SessionLocal
from app.graph.client import GraphClient
from app.importers.caldav_importer import CalDavCalendarImporter
from app.importers.carddav_importer import CardDavContactImporter
from app.importers.imap_importer import ImapMailImporter
from app.jobs.runner import MigrationJobRunner
from app.jobs.scheduler import Scheduler
from app.security.crypto import decrypt

_scheduler: Scheduler | None = None
_scheduler_lock = threading.Lock()


def _graph_client_factory(tenant_config: TenantConfig) -> GraphClient:
    return GraphClient(tenant_config.tenant_id, tenant_config.client_id, decrypt(tenant_config.client_secret_encrypted))


def build_scheduler() -> Scheduler:
    settings = get_settings()
    runner = MigrationJobRunner(
        db_session_factory=SessionLocal,
        graph_client_factory=_graph_client_factory,
        mail_importer_factory=ImapMailImporter,
        calendar_importer_factory=CalDavCalendarImporter,
        contact_importer_factory=CardDavContactImporter,
        imap_host=settings.mailcow_imap_host,
        imap_port=settings.mailcow_imap_port,
        dav_base_url=settings.mailcow_dav_base_url,
    )
    return Scheduler(max_workers=settings.concurrency, db_session_factory=SessionLocal, runner=runner)


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        with _scheduler_lock:
            if _scheduler is None:
                _scheduler = build_scheduler()
    return _scheduler

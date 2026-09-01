# Exchange Online → Mailcow Migrationstool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Docker-deployable FastAPI tool with a Jinja2+HTMX web GUI that migrates mail, calendar, and contacts for many mailboxes from Exchange Online (Microsoft Graph, app-only) to a Mailcow instance (IMAP + SOGo CalDAV/CardDAV), with idempotent resumable jobs and a post-cutover resync function.

**Architecture:** One FastAPI process, no separate worker service. Background migrations run in a `ThreadPoolExecutor` because IMAP/CalDAV/CardDAV access uses synchronous libraries; each mailbox migration runs entirely in one worker thread (Graph calls included, via synchronous `httpx.Client`). SQLite (SQLAlchemy) is the single source of truth for config, mappings, job status, and per-item idempotency — the GUI polls it directly via HTMX, so progress and resumability survive process crashes without any in-memory state.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2 + HTMX, SQLAlchemy 2.0 (SQLite), httpx, MSAL (Graph client-credentials), imapclient, icalendar, vobject, cryptography (Fernet + Scrypt), pytest.

**Spec:** [docs/superpowers/specs/2026-09-01-exo-to-mailcow-migration-tool-design.md](../specs/2026-09-01-exo-to-mailcow-migration-tool-design.md)

## Global Constraints

- Client-Credentials/App-Only Graph auth only — no delegated OAuth flows.
- Secrets (`client_secret`, `app_password`) are Fernet-encrypted at rest; `ENCRYPTION_KEY` comes only from ENV, never logged.
- All logging is JSON-structured with an active secret-redaction filter — never trust "we just don't log it."
- `migration_item` is keyed by `(mapping_id, category, external_id)`, not `job_id` — dedup must survive across job reruns/resyncs.
- `migration_item.status` has exactly two values: `done` / `failed`. There is no persisted `skipped` status — "skipped" only exists as a per-job aggregate counter.
- `migration_item.content_hash` is a reserved-but-currently-unused column (future fallback matching if Graph IDs ever become unstable) — do not populate or read it in this plan; exact `external_id` matching is sufficient for now.
- Mail resync only imports genuinely new messages (no flag-update sync). Calendar/contacts resync additionally re-imports items whose Graph `lastModifiedDateTime` is newer than what was stored at last import.
- No EAS adapter — IMAP/CalDAV/CardDAV are the only target adapters (rationale: spec §9).
- Every task must leave `pytest` green before moving to the next task.

---

## File Structure

```
exotomailcow/
├── pyproject.toml                      # deps + setuptools packaging + pytest config
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
├── README.md
├── app/
│   ├── __init__.py
│   ├── main.py                         # FastAPI app, lifespan startup wiring
│   ├── config.py                       # Settings (pydantic-settings)
│   ├── logging_config.py               # JSON logging + secret redaction
│   ├── db/
│   │   ├── __init__.py
│   │   ├── models.py                   # SQLAlchemy models + enums
│   │   ├── session.py                  # engine, SessionLocal, get_db(), init_db()
│   │   └── repositories.py             # migration_item + mail_folder_map data access
│   ├── security/
│   │   ├── __init__.py
│   │   ├── crypto.py                   # Fernet encrypt/decrypt
│   │   └── auth.py                     # Basic Auth (Scrypt hash) + first-run bootstrap
│   ├── graph/
│   │   ├── __init__.py
│   │   ├── retry.py                    # graph_request(): 429/5xx backoff
│   │   ├── models.py                   # GraphMailbox/Folder/MessageRef/Calendar/Event/Contact + parse_graph_datetime
│   │   └── client.py                   # GraphClient (MSAL + httpx, synchronous)
│   ├── conversion/
│   │   ├── __init__.py
│   │   ├── ics.py                      # graph_recurrence_to_rrule, graph_event_to_ics
│   │   └── vcard.py                    # graph_contact_to_vcard
│   ├── importers/
│   │   ├── __init__.py
│   │   ├── base.py                     # MailcowTarget, MailImporter/CalendarImporter/ContactImporter Protocols
│   │   ├── folder_mapping.py           # build_folder_paths, build_imap_path
│   │   ├── imap_importer.py            # ImapMailImporter
│   │   ├── caldav_importer.py          # CalDavCalendarImporter
│   │   └── carddav_importer.py         # CardDavContactImporter
│   ├── jobs/
│   │   ├── __init__.py
│   │   ├── runner.py                   # MigrationJobRunner
│   │   ├── resync.py                   # create_resync_job / create_resync_jobs_for_all / RESYNC_BUFFER
│   │   └── scheduler.py                # Scheduler (ThreadPoolExecutor)
│   └── web/
│       ├── __init__.py
│       ├── scheduler_dep.py            # process-wide Scheduler singleton + get_scheduler()
│       ├── routes/
│       │   ├── __init__.py
│       │   ├── setup.py
│       │   ├── mappings.py
│       │   ├── jobs.py
│       │   ├── reports.py
│       │   ├── resync.py
│       │   └── admin.py
│       ├── templates/
│       │   ├── base.html
│       │   ├── setup.html
│       │   ├── _setup_test_result.html
│       │   ├── mappings.html
│       │   ├── _mappings_table.html
│       │   ├── _job_progress.html
│       │   ├── _resync_all_result.html
│       │   ├── _resync_error.html
│       │   └── _purge_result.html
│       └── static/
│           └── style.css
└── tests/
    ├── conftest.py
    ├── test_main.py
    ├── test_config.py
    ├── test_db_models.py
    ├── test_crypto.py
    ├── test_auth.py
    ├── test_logging_config.py
    ├── test_graph_retry.py
    ├── test_graph_client.py
    ├── test_conversion_ics.py
    ├── test_conversion_vcard.py
    ├── test_imap_importer.py
    ├── test_dav_importers.py
    ├── test_repositories.py
    ├── test_folder_mapping.py
    ├── test_jobs_runner.py
    ├── test_jobs_resync.py
    ├── test_jobs_scheduler.py
    ├── test_web_setup.py
    ├── test_web_mappings.py
    ├── test_web_jobs.py
    ├── test_web_reports_resync_admin.py
    └── integration/
        └── test_single_mailbox_roundtrip.py   # manual, requires real credentials
```

---

### Task 1: Project scaffolding + health check

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `app/__init__.py`
- Create: `app/main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Produces: `app.main.app` (FastAPI instance) with `GET /healthz`.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "exotomailcow"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "jinja2>=3.1",
    "python-multipart>=0.0.9",
    "pydantic-settings>=2.4",
    "sqlalchemy>=2.0",
    "httpx>=0.27",
    "msal>=1.28",
    "imapclient>=3.0",
    "icalendar>=5.0",
    "vobject>=0.9.6.1",
    "cryptography>=42",
    "python-json-logger>=2.0",
]

[project.optional-dependencies]
dev = ["pytest>=8"]

[tool.setuptools.packages.find]
include = ["app*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```

- [ ] **Step 2: Write `.gitignore`**

```
__pycache__/
*.pyc
.venv/
data/
*.db
.env
```

- [ ] **Step 3: Write `.env.example`**

```
ENCRYPTION_KEY=
ADMIN_USER=admin
ADMIN_PASSWORD=
MAILCOW_DAV_BASE_URL=https://mail.example.org
MAILCOW_IMAP_HOST=mail.example.org
MAILCOW_IMAP_PORT=993
CONCURRENCY=4
DATABASE_URL=sqlite:////app/data/exotomailcow.db
LOG_DIR=/app/data/logs
LOG_LEVEL=INFO
```

- [ ] **Step 4: Write `app/__init__.py`** (empty file)

- [ ] **Step 5: Write `app/main.py`**

```python
from fastapi import FastAPI

app = FastAPI(title="exotomailcow")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
```

- [ ] **Step 6: Write the failing test**

```python
# tests/test_main.py
from fastapi.testclient import TestClient

from app.main import app


def test_healthz_returns_ok():
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 7: Install and run**

```bash
pip install -e ".[dev]"
pytest tests/test_main.py -v
```

Expected: PASS (this task has no prior implementation to fail against, since the app is written in the same step — verify green).

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml .gitignore .env.example app/__init__.py app/main.py tests/test_main.py
git commit -m "feat: project scaffolding with FastAPI health check"
```

---

### Task 2: Settings + test fixtures

**Files:**
- Create: `app/config.py`
- Create: `tests/conftest.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: none.
- Produces: `app.config.Settings` (pydantic-settings model), `app.config.get_settings() -> Settings` (`lru_cache`d). Fields: `encryption_key: str`, `admin_user: str`, `admin_password: str`, `database_url: str = "sqlite:///./data/exotomailcow.db"`, `concurrency: int = 4`, `mailcow_dav_base_url: str`, `mailcow_imap_host: str`, `mailcow_imap_port: int = 993`, `log_level: str = "INFO"`, `log_dir: str = "./data/logs"`.
- `tests/conftest.py` provides an autouse fixture that sets baseline required env vars for every test in the suite and clears the `get_settings` cache before/after each test.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
import pytest
from pydantic import ValidationError

from app.config import get_settings


def test_settings_loads_from_env(monkeypatch):
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.admin_user == "admin"
    assert settings.concurrency == 4
    assert settings.mailcow_imap_port == 993


def test_settings_raises_on_missing_required_var(monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        get_settings()
    get_settings.cache_clear()
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL/ERROR — `app.config` module does not exist yet, and there is no `conftest.py` setting env vars.

- [ ] **Step 3: Write `app/config.py`**

```python
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    encryption_key: str
    admin_user: str
    admin_password: str
    database_url: str = "sqlite:///./data/exotomailcow.db"
    concurrency: int = 4
    mailcow_dav_base_url: str
    mailcow_imap_host: str
    mailcow_imap_port: int = 993
    log_level: str = "INFO"
    log_dir: str = "./data/logs"

    model_config = SettingsConfigDict(env_file=".env")


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: Write `tests/conftest.py`**

```python
import pytest
from cryptography.fernet import Fernet

from app.config import get_settings


@pytest.fixture(autouse=True)
def _default_env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("MAILCOW_DAV_BASE_URL", "https://mail.example.org")
    monkeypatch.setenv("MAILCOW_IMAP_HOST", "mail.example.org")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
```

- [ ] **Step 5: Run to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/config.py tests/conftest.py tests/test_config.py
git commit -m "feat: add Settings and shared test env fixture"
```

---

### Task 3: DB models + session

**Files:**
- Create: `app/db/__init__.py`
- Create: `app/db/models.py`
- Create: `app/db/session.py`
- Test: `tests/test_db_models.py`

**Interfaces:**
- Consumes: `app.config.get_settings`.
- Produces: `Base` (declarative base), enums `JobStatus`, `JobType`, `ItemCategory`, `ItemStatus`, models `TenantConfig`, `MailboxMapping`, `MigrationJob`, `MigrationItem`, `MailFolderMap`. `app.db.session.SessionLocal` (callable `() -> Session`), `get_db()` (FastAPI generator dependency), `init_db()` (creates all tables against the configured engine).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_models.py
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, ItemStatus, JobStatus, JobType, MailboxMapping, MigrationItem, MigrationJob


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_insert_and_query_mailbox_mapping():
    db = _session()
    mapping = MailboxMapping(exo_upn="user@church.org", mailcow_address="user@mailcow.local", app_password_encrypted="enc")
    db.add(mapping)
    db.commit()

    fetched = db.query(MailboxMapping).one()
    assert fetched.exo_upn == "user@church.org"
    assert fetched.last_synced_at is None


def test_migration_item_unique_constraint():
    db = _session()
    mapping = MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc")
    db.add(mapping)
    db.commit()

    db.add(MigrationItem(mapping_id=mapping.id, category="mail", external_id="msg-1", status=ItemStatus.DONE.value))
    db.commit()

    db.add(MigrationItem(mapping_id=mapping.id, category="mail", external_id="msg-1", status=ItemStatus.DONE.value))
    import pytest
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        db.commit()


def test_migration_job_defaults():
    db = _session()
    mapping = MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc")
    db.add(mapping)
    db.commit()

    job = MigrationJob(mapping_id=mapping.id, job_type=JobType.INITIAL.value)
    db.add(job)
    db.commit()

    fetched = db.query(MigrationJob).one()
    assert fetched.status == JobStatus.PENDING.value
    assert fetched.count_created == 0
    assert fetched.dry_run is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_db_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.db'`

- [ ] **Step 3: Write `app/db/__init__.py`** (empty file)

- [ ] **Step 4: Write `app/db/models.py`**

```python
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobType(str, enum.Enum):
    INITIAL = "initial"
    RESYNC = "resync"


class ItemCategory(str, enum.Enum):
    MAIL = "mail"
    CALENDAR = "calendar"
    CONTACTS = "contacts"


class ItemStatus(str, enum.Enum):
    DONE = "done"
    FAILED = "failed"


class TenantConfig(Base):
    __tablename__ = "tenant_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    admin_user: Mapped[str] = mapped_column(String(255))
    admin_password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MailboxMapping(Base):
    __tablename__ = "mailbox_mapping"

    id: Mapped[int] = mapped_column(primary_key=True)
    exo_upn: Mapped[str] = mapped_column(String(255))
    mailcow_address: Mapped[str] = mapped_column(String(255))
    app_password_encrypted: Mapped[str] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MigrationJob(Base):
    __tablename__ = "migration_job"

    id: Mapped[int] = mapped_column(primary_key=True)
    mapping_id: Mapped[int] = mapped_column(ForeignKey("mailbox_mapping.id"))
    job_type: Mapped[str] = mapped_column(String(20), default=JobType.INITIAL.value)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.PENDING.value)
    migrate_mail: Mapped[bool] = mapped_column(Boolean, default=True)
    migrate_calendar: Mapped[bool] = mapped_column(Boolean, default=True)
    migrate_contacts: Mapped[bool] = mapped_column(Boolean, default=True)
    mail_since_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    count_created: Mapped[int] = mapped_column(Integer, default=0)
    count_updated: Mapped[int] = mapped_column(Integer, default=0)
    count_skipped: Mapped[int] = mapped_column(Integer, default=0)
    count_failed: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MigrationItem(Base):
    __tablename__ = "migration_item"
    __table_args__ = (
        UniqueConstraint("mapping_id", "category", "external_id", name="uq_migration_item"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mapping_id: Mapped[int] = mapped_column(ForeignKey("mailbox_mapping.id"))
    category: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str] = mapped_column(String(512))
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_modified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=ItemStatus.DONE.value)
    target_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MailFolderMap(Base):
    __tablename__ = "mail_folder_map"
    __table_args__ = (
        UniqueConstraint("mapping_id", "graph_folder_id", name="uq_mail_folder_map"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mapping_id: Mapped[int] = mapped_column(ForeignKey("mailbox_mapping.id"))
    graph_folder_id: Mapped[str] = mapped_column(String(512))
    graph_path: Mapped[str] = mapped_column(String(1024))
    imap_mailbox_name: Mapped[str] = mapped_column(String(1024))
    well_known_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created: Mapped[bool] = mapped_column(Boolean, default=False)
```

- [ ] **Step 5: Write `app/db/session.py`**

```python
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import Base


def _make_engine():
    settings = get_settings()
    connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    return create_engine(settings.database_url, connect_args=connect_args)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

- [ ] **Step 6: Run to verify it passes**

Run: `pytest tests/test_db_models.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/db/__init__.py app/db/models.py app/db/session.py tests/test_db_models.py
git commit -m "feat: add SQLAlchemy models and DB session"
```

---

### Task 4: Secrets encryption (Fernet)

**Files:**
- Create: `app/security/__init__.py`
- Create: `app/security/crypto.py`
- Test: `tests/test_crypto.py`

**Interfaces:**
- Consumes: `app.config.get_settings`.
- Produces: `encrypt(plaintext: str) -> str`, `decrypt(token: str) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_crypto.py
import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings
from app.security.crypto import decrypt, encrypt


def test_encrypt_decrypt_roundtrip():
    token = encrypt("s3cr3t-app-password")
    assert token != "s3cr3t-app-password"
    assert decrypt(token) == "s3cr3t-app-password"


def test_decrypt_with_wrong_key_fails(monkeypatch):
    token = encrypt("s3cr3t-app-password")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    with pytest.raises(InvalidToken):
        decrypt(token)
    get_settings.cache_clear()
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_crypto.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.security'`

- [ ] **Step 3: Write `app/security/__init__.py`** (empty file)

- [ ] **Step 4: Write `app/security/crypto.py`**

```python
from cryptography.fernet import Fernet

from app.config import get_settings


def encrypt(plaintext: str) -> str:
    settings = get_settings()
    return Fernet(settings.encryption_key.encode()).encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    settings = get_settings()
    return Fernet(settings.encryption_key.encode()).decrypt(token.encode()).decode()
```

- [ ] **Step 5: Run to verify it passes**

Run: `pytest tests/test_crypto.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/security/__init__.py app/security/crypto.py tests/test_crypto.py
git commit -m "feat: add Fernet-based secret encryption"
```

---

### Task 5: Basic Auth + first-run admin bootstrap

**Files:**
- Create: `app/security/auth.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `app.config.get_settings`, `app.db.models.TenantConfig`, `app.db.session.get_db`.
- Produces: `hash_password(password: str) -> str`, `verify_password(password: str, password_hash: str) -> bool`, `bootstrap_admin_from_env(db: Session) -> None`, `require_admin` (FastAPI dependency returning the authenticated username).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auth.py
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, TenantConfig
from app.security.auth import bootstrap_admin_from_env, hash_password, require_admin, verify_password


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_hash_and_verify_password_roundtrip():
    hashed = hash_password("correct-horse")
    assert verify_password("correct-horse", hashed)
    assert not verify_password("wrong-password", hashed)


def test_bootstrap_admin_from_env_creates_row_once():
    db = _session()
    bootstrap_admin_from_env(db)
    assert db.query(TenantConfig).count() == 1

    bootstrap_admin_from_env(db)
    assert db.query(TenantConfig).count() == 1


def test_require_admin_enforces_credentials():
    db = _session()
    bootstrap_admin_from_env(db)

    app = FastAPI()

    @app.get("/protected")
    def protected(user: str = Depends(require_admin)):
        return {"user": user}

    app.dependency_overrides = {}
    from app.db.session import get_db as real_get_db

    def _override_get_db():
        yield db

    app.dependency_overrides[real_get_db] = _override_get_db

    client = TestClient(app)
    assert client.get("/protected").status_code == 401
    assert client.get("/protected", auth=("admin", "wrong")).status_code == 401
    ok = client.get("/protected", auth=("admin", "test-password"))
    assert ok.status_code == 200
    assert ok.json() == {"user": "admin"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_auth.py -v`
Expected: FAIL — `ImportError: cannot import name 'hash_password'`

- [ ] **Step 3: Write `app/security/auth.py`**

```python
import base64
import hmac
import os

from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import TenantConfig
from app.db.session import get_db

_security = HTTPBasic()
_SCRYPT_PARAMS = {"length": 32, "n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    derived = Scrypt(salt=salt, **_SCRYPT_PARAMS).derive(password.encode())
    return f"{base64.b64encode(salt).decode()}${base64.b64encode(derived).decode()}"


def verify_password(password: str, password_hash: str) -> bool:
    salt_b64, _, derived_b64 = password_hash.partition("$")
    salt = base64.b64decode(salt_b64)
    expected = base64.b64decode(derived_b64)
    try:
        Scrypt(salt=salt, **_SCRYPT_PARAMS).verify(password.encode(), expected)
        return True
    except Exception:
        return False


def bootstrap_admin_from_env(db: Session) -> None:
    if db.query(TenantConfig).one_or_none() is not None:
        return
    settings = get_settings()
    db.add(
        TenantConfig(
            admin_user=settings.admin_user,
            admin_password_hash=hash_password(settings.admin_password),
        )
    )
    db.commit()


def require_admin(
    credentials: HTTPBasicCredentials = Depends(_security),
    db: Session = Depends(get_db),
) -> str:
    config = db.query(TenantConfig).one_or_none()
    if config is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not configured")
    valid_user = hmac.compare_digest(credentials.username, config.admin_user)
    valid_password = verify_password(credentials.password, config.admin_password_hash)
    if not (valid_user and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_auth.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/security/auth.py tests/test_auth.py
git commit -m "feat: add Basic Auth with Scrypt password hashing and admin bootstrap"
```

---

### Task 6: Structured logging with secret redaction

**Files:**
- Create: `app/logging_config.py`
- Test: `tests/test_logging_config.py`

**Interfaces:**
- Produces: `configure_logging(log_dir: str, log_level: str = "INFO") -> None`, `SecretRedactionFilter` (logging.Filter).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_logging_config.py
import logging
import os

from app.logging_config import configure_logging


def test_secret_fields_are_redacted_in_log_file(tmp_path):
    log_dir = str(tmp_path / "logs")
    configure_logging(log_dir, "INFO")

    logger = logging.getLogger("test-redaction")
    logger.info("connecting with client_secret=abc123XYZ and app_password=hunter2hunter2")

    log_file = os.path.join(log_dir, "app.log")
    with open(log_file, encoding="utf-8") as f:
        content = f.read()

    assert "abc123XYZ" not in content
    assert "hunter2hunter2" not in content
    assert "REDACTED" in content
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_logging_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.logging_config'`

- [ ] **Step 3: Write `app/logging_config.py`**

```python
import logging
import logging.handlers
import os
import re

from pythonjsonlogger import jsonlogger

_SECRET_KEYS = ("client_secret", "app_password", "authorization", "password")
_REDACT_RE = re.compile(
    r'(' + "|".join(_SECRET_KEYS) + r')(["\']?\s*[:=]\s*["\']?)([^"\'\s,}]+)',
    re.IGNORECASE,
)


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _REDACT_RE.sub(r"\1\2***REDACTED***", record.msg)
        if record.args:
            record.args = tuple(
                _REDACT_RE.sub(r"\1\2***REDACTED***", arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        for key in list(record.__dict__.keys()):
            if key.lower() in _SECRET_KEYS:
                record.__dict__[key] = "***REDACTED***"
        return True


def configure_logging(log_dir: str, log_level: str = "INFO") -> None:
    os.makedirs(log_dir, exist_ok=True)
    formatter = jsonlogger.JsonFormatter("%(asctime)s %(name)s %(levelname)s %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "app.log"), maxBytes=10_000_000, backupCount=5
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(SecretRedactionFilter())

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(SecretRedactionFilter())

    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers = [file_handler, console_handler]
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_logging_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/logging_config.py tests/test_logging_config.py
git commit -m "feat: add JSON logging with secret redaction filter"
```

---

### Task 7: Graph retry/backoff helper

**Files:**
- Create: `app/graph/__init__.py`
- Create: `app/graph/retry.py`
- Test: `tests/test_graph_retry.py`

**Interfaces:**
- Produces: `graph_request(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response`. Retries on HTTP 429 (honors `Retry-After` exactly) and 5xx (exponential backoff with jitter), up to `MAX_RETRIES` attempts, then raises via `response.raise_for_status()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_graph_retry.py
import httpx
import pytest

from app.graph.retry import MAX_RETRIES, graph_request


def test_retries_on_429_and_honors_retry_after(monkeypatch):
    calls = {"count": 0}
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://graph.microsoft.com/v1.0")
    response = graph_request(client, "GET", "/users")

    assert response.status_code == 200
    assert calls["count"] == 2
    assert sleeps == [2.0]


def test_retries_on_5xx_then_succeeds(monkeypatch):
    calls = {"count": 0}
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr("random.uniform", lambda a, b: 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://graph.microsoft.com/v1.0")
    response = graph_request(client, "GET", "/users")

    assert response.status_code == 200
    assert calls["count"] == 3


def test_raises_after_max_retries(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "0"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://graph.microsoft.com/v1.0")
    with pytest.raises(httpx.HTTPStatusError):
        graph_request(client, "GET", "/users")
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_graph_retry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.graph'`

- [ ] **Step 3: Write `app/graph/__init__.py`** (empty file)

- [ ] **Step 4: Write `app/graph/retry.py`**

```python
import random
import time

import httpx

MAX_RETRIES = 5


def graph_request(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    attempt = 0
    while True:
        response = client.request(method, url, **kwargs)
        if response.status_code == 429 or response.status_code >= 500:
            attempt += 1
            if attempt > MAX_RETRIES:
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after is not None else min(2**attempt, 60) + random.uniform(0, 1)
            time.sleep(delay)
            continue
        response.raise_for_status()
        return response
```

- [ ] **Step 5: Run to verify it passes**

Run: `pytest tests/test_graph_retry.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/graph/__init__.py app/graph/retry.py tests/test_graph_retry.py
git commit -m "feat: add Graph 429/5xx retry-backoff helper"
```

---

### Task 8: GraphClient — auth + list_mailboxes

**Files:**
- Create: `app/graph/models.py`
- Create: `app/graph/client.py`
- Test: `tests/test_graph_client.py`

**Interfaces:**
- Consumes: `app.graph.retry.graph_request`.
- Produces: `GraphMailbox` dataclass (`id`, `user_principal_name`, `display_name`, `mail`), `parse_graph_datetime(value: str) -> datetime`, `GraphClient(tenant_id, client_id, client_secret, http_client=None)` with `list_mailboxes(search: str | None = None) -> Iterator[GraphMailbox]`. This task also lands the shared `_get`/`_paged_get` machinery every later GraphClient method reuses.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_graph_client.py
import httpx

from app.graph.client import GraphClient


def _client(handler, monkeypatch) -> GraphClient:
    monkeypatch.setattr(
        "app.graph.client.msal.ConfidentialClientApplication.acquire_token_for_client",
        lambda self, scopes: {"access_token": "fake-token"},
    )
    http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://graph.microsoft.com/v1.0")
    return GraphClient("tenant-id", "client-id", "client-secret", http_client=http_client)


def test_list_mailboxes_single_page(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fake-token"
        assert request.url.path.endswith("/users")
        return httpx.Response(
            200,
            json={
                "value": [
                    {"id": "1", "userPrincipalName": "a@church.org", "displayName": "A", "mail": "a@church.org"},
                ]
            },
        )

    client = _client(handler, monkeypatch)
    mailboxes = list(client.list_mailboxes())

    assert len(mailboxes) == 1
    assert mailboxes[0].user_principal_name == "a@church.org"


def test_list_mailboxes_follows_next_link(monkeypatch):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "1", "userPrincipalName": "a@church.org", "displayName": "A", "mail": "a@church.org"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?$skiptoken=abc",
                },
            )
        return httpx.Response(
            200,
            json={"value": [{"id": "2", "userPrincipalName": "b@church.org", "displayName": "B", "mail": "b@church.org"}]},
        )

    client = _client(handler, monkeypatch)
    mailboxes = list(client.list_mailboxes())

    assert calls["count"] == 2
    assert [m.id for m in mailboxes] == ["1", "2"]


def test_list_mailboxes_escapes_search_quotes(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["filter"] = request.url.params["$filter"]
        return httpx.Response(200, json={"value": []})

    client = _client(handler, monkeypatch)
    list(client.list_mailboxes(search="O'Brien"))

    assert "O''Brien" in captured["filter"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_graph_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.graph.client'`

- [ ] **Step 3: Write `app/graph/models.py`**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def parse_graph_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class GraphMailbox:
    id: str
    user_principal_name: str
    display_name: str
    mail: str | None


@dataclass
class GraphFolder:
    id: str
    display_name: str
    parent_id: str | None
    well_known_name: str | None
    child_folder_count: int


@dataclass
class GraphMessageRef:
    id: str
    received_date_time: datetime
    is_read: bool
    is_flagged: bool


@dataclass
class GraphCalendar:
    id: str
    name: str


@dataclass
class GraphEvent:
    id: str
    ics_uid: str
    last_modified_date_time: datetime
    subject: str
    start: datetime
    end: datetime
    is_all_day: bool
    location: str | None
    body_html: str | None
    organizer_email: str | None
    attendees: list[str] = field(default_factory=list)
    recurrence: dict | None = None


@dataclass
class GraphContact:
    id: str
    last_modified_date_time: datetime
    display_name: str
    email_addresses: list[str] = field(default_factory=list)
    business_phones: list[str] = field(default_factory=list)
    mobile_phone: str | None = None
    company_name: str | None = None
```

- [ ] **Step 4: Write `app/graph/client.py`**

```python
from collections.abc import Iterator

import httpx
import msal

from app.graph.models import GraphMailbox
from app.graph.retry import graph_request

_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
_GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]


class GraphClient:
    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._msal_app = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
        )
        self._http = http_client or httpx.Client(base_url=_GRAPH_BASE_URL, timeout=30.0)

    def _token(self) -> str:
        result = self._msal_app.acquire_token_for_client(scopes=_GRAPH_SCOPE)
        if "access_token" not in result:
            raise RuntimeError(f"Graph auth failed: {result.get('error_description', result)}")
        return result["access_token"]

    def _get(self, url: str, params: dict | None = None, extra_headers: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._token()}"}
        if extra_headers:
            headers.update(extra_headers)
        response = graph_request(self._http, "GET", url, headers=headers, params=params)
        return response.json()

    def _paged_get(self, url: str, params: dict | None = None, extra_headers: dict | None = None) -> Iterator[dict]:
        next_url: str | None = url
        next_params: dict | None = params
        while next_url:
            data = self._get(next_url, params=next_params, extra_headers=extra_headers)
            yield from data.get("value", [])
            next_url = data.get("@odata.nextLink")
            next_params = None

    def list_mailboxes(self, search: str | None = None) -> Iterator[GraphMailbox]:
        params: dict[str, str] = {"$select": "id,userPrincipalName,displayName,mail", "$top": "999"}
        if search:
            escaped = search.replace("'", "''")
            params["$filter"] = f"startswith(displayName,'{escaped}') or startswith(userPrincipalName,'{escaped}')"
        for raw in self._paged_get("/users", params=params):
            yield GraphMailbox(
                id=raw["id"],
                user_principal_name=raw["userPrincipalName"],
                display_name=raw.get("displayName") or raw["userPrincipalName"],
                mail=raw.get("mail"),
            )
```

- [ ] **Step 5: Run to verify it passes**

Run: `pytest tests/test_graph_client.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/graph/models.py app/graph/client.py tests/test_graph_client.py
git commit -m "feat: add GraphClient auth and list_mailboxes"
```

---

### Task 9: GraphClient — mail folders (recursive) + messages

**Files:**
- Modify: `app/graph/client.py` (add methods to the `GraphClient` class from Task 8)
- Modify: `tests/test_graph_client.py` (append tests)

**Interfaces:**
- Consumes: `GraphFolder`, `GraphMessageRef`, `parse_graph_datetime` from `app.graph.models` (Task 8).
- Produces: `GraphClient.list_mail_folders(user_id: str) -> list[GraphFolder]` (fully resolved, including nested children), `GraphClient.list_messages(user_id: str, folder_id: str, since: datetime | None = None) -> Iterator[GraphMessageRef]`, `GraphClient.get_message_raw(user_id: str, message_id: str) -> bytes`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_graph_client.py`)

```python
from datetime import datetime, timezone


def test_list_mail_folders_resolves_nested_children(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/mailFolders"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "inbox", "displayName": "Inbox", "parentFolderId": None,
                         "wellKnownName": "inbox", "childFolderCount": 0},
                        {"id": "root-custom", "displayName": "Projekte", "parentFolderId": None,
                         "wellKnownName": None, "childFolderCount": 1},
                    ]
                },
            )
        if path.endswith("root-custom/childFolders"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "child-1", "displayName": "2024", "parentFolderId": "root-custom",
                         "wellKnownName": None, "childFolderCount": 0},
                    ]
                },
            )
        raise AssertionError(f"unexpected path {path}")

    client = _client(handler, monkeypatch)
    folders = client.list_mail_folders("user-1")

    assert {f.id for f in folders} == {"inbox", "root-custom", "child-1"}
    child = next(f for f in folders if f.id == "child-1")
    assert child.parent_id == "root-custom"


def test_list_messages_maps_flags_and_applies_since_filter(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["filter"] = request.url.params.get("$filter")
        return httpx.Response(
            200,
            json={
                "value": [
                    {"id": "m1", "receivedDateTime": "2026-01-01T10:00:00Z", "isRead": True,
                     "flag": {"flagStatus": "flagged"}},
                    {"id": "m2", "receivedDateTime": "2026-01-02T10:00:00Z", "isRead": False, "flag": {}},
                ]
            },
        )

    client = _client(handler, monkeypatch)
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    messages = list(client.list_messages("user-1", "inbox", since=since))

    assert captured["filter"] == "receivedDateTime ge 2026-01-01T00:00:00Z"
    assert messages[0].is_read is True
    assert messages[0].is_flagged is True
    assert messages[1].is_flagged is False


def test_get_message_raw_returns_bytes(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/messages/m1/$value")
        return httpx.Response(200, content=b"From: a@b\r\nSubject: hi\r\n\r\nbody")

    client = _client(handler, monkeypatch)
    raw = client.get_message_raw("user-1", "m1")

    assert raw.startswith(b"From: a@b")
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_graph_client.py -v`
Expected: FAIL — `AttributeError: 'GraphClient' object has no attribute 'list_mail_folders'`

- [ ] **Step 3: Add methods to `app/graph/client.py`**

Add these imports at the top:

```python
from datetime import datetime

from app.graph.models import GraphFolder, GraphMessageRef, parse_graph_datetime
```

Append these methods to the `GraphClient` class:

```python
    _FOLDER_SELECT = "id,displayName,parentFolderId,wellKnownName,childFolderCount"

    def list_mail_folders(self, user_id: str) -> list[GraphFolder]:
        top_level = list(
            self._paged_get(f"/users/{user_id}/mailFolders", params={"$select": self._FOLDER_SELECT, "$top": "999"})
        )
        folders: list[GraphFolder] = []
        for raw in top_level:
            folders.append(self._to_folder(raw))
            if raw.get("childFolderCount", 0) > 0:
                folders.extend(self._list_child_folders(user_id, raw["id"]))
        return folders

    def _list_child_folders(self, user_id: str, parent_id: str) -> list[GraphFolder]:
        result: list[GraphFolder] = []
        children = list(
            self._paged_get(
                f"/users/{user_id}/mailFolders/{parent_id}/childFolders",
                params={"$select": self._FOLDER_SELECT, "$top": "999"},
            )
        )
        for raw in children:
            result.append(self._to_folder(raw))
            if raw.get("childFolderCount", 0) > 0:
                result.extend(self._list_child_folders(user_id, raw["id"]))
        return result

    @staticmethod
    def _to_folder(raw: dict) -> GraphFolder:
        return GraphFolder(
            id=raw["id"],
            display_name=raw["displayName"],
            parent_id=raw.get("parentFolderId"),
            well_known_name=raw.get("wellKnownName"),
            child_folder_count=raw.get("childFolderCount", 0),
        )

    def list_messages(
        self, user_id: str, folder_id: str, since: datetime | None = None
    ) -> Iterator[GraphMessageRef]:
        params: dict[str, str] = {"$select": "id,receivedDateTime,isRead,flag", "$top": "50"}
        if since is not None:
            params["$filter"] = f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        for raw in self._paged_get(f"/users/{user_id}/mailFolders/{folder_id}/messages", params=params):
            yield GraphMessageRef(
                id=raw["id"],
                received_date_time=parse_graph_datetime(raw["receivedDateTime"]),
                is_read=raw.get("isRead", False),
                is_flagged=(raw.get("flag") or {}).get("flagStatus") == "flagged",
            )

    def get_message_raw(self, user_id: str, message_id: str) -> bytes:
        headers = {"Authorization": f"Bearer {self._token()}"}
        response = graph_request(
            self._http, "GET", f"/users/{user_id}/messages/{message_id}/$value", headers=headers
        )
        return response.content
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_graph_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/graph/client.py tests/test_graph_client.py
git commit -m "feat: add GraphClient recursive mail folders and message listing"
```

---

### Task 10: GraphClient — calendars, events, contacts

**Files:**
- Modify: `app/graph/client.py`
- Modify: `tests/test_graph_client.py` (append tests)

**Interfaces:**
- Consumes: `GraphCalendar`, `GraphEvent`, `GraphContact`, `parse_graph_datetime` from `app.graph.models`.
- Produces: `GraphClient.list_calendars(user_id) -> list[GraphCalendar]`, `GraphClient.list_events(user_id, calendar_id, modified_since=None) -> Iterator[GraphEvent]`, `GraphClient.list_contacts(user_id, modified_since=None) -> Iterator[GraphContact]`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_graph_client.py`)

```python
def test_list_events_requests_utc_and_applies_modified_since_filter(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["prefer"] = request.headers.get("Prefer")
        captured["filter"] = request.url.params.get("$filter")
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "evt1",
                        "iCalUId": "uid-1",
                        "lastModifiedDateTime": "2026-02-01T09:00:00Z",
                        "subject": "Sitzung",
                        "start": {"dateTime": "2026-02-05T10:00:00.0000000", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-02-05T11:00:00.0000000", "timeZone": "UTC"},
                        "isAllDay": False,
                        "location": {"displayName": "Saal"},
                        "body": {"content": "Agenda"},
                        "organizer": {"emailAddress": {"address": "leiter@church.org"}},
                        "attendees": [{"emailAddress": {"address": "mitglied@church.org"}}],
                        "recurrence": None,
                    }
                ]
            },
        )

    client = _client(handler, monkeypatch)
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = list(client.list_events("user-1", "cal-1", modified_since=since))

    assert captured["prefer"] == 'outlook.timezone="UTC"'
    assert captured["filter"] == "lastModifiedDateTime ge 2026-01-01T00:00:00Z"
    assert events[0].ics_uid == "uid-1"
    assert events[0].organizer_email == "leiter@church.org"
    assert events[0].attendees == ["mitglied@church.org"]
    assert events[0].start.tzinfo is not None


def test_list_contacts_maps_fields(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "c1",
                        "lastModifiedDateTime": "2026-02-01T09:00:00Z",
                        "displayName": "Maria Muster",
                        "emailAddresses": [{"address": "maria@example.org"}],
                        "businessPhones": ["+41 44 000 00 00"],
                        "mobilePhone": "+41 79 000 00 00",
                        "companyName": "Kirchgemeinde",
                    }
                ]
            },
        )

    client = _client(handler, monkeypatch)
    contacts = list(client.list_contacts("user-1"))

    assert contacts[0].display_name == "Maria Muster"
    assert contacts[0].email_addresses == ["maria@example.org"]
    assert contacts[0].mobile_phone == "+41 79 000 00 00"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_graph_client.py -v`
Expected: FAIL — `AttributeError: 'GraphClient' object has no attribute 'list_events'`

- [ ] **Step 3: Add methods to `app/graph/client.py`**

Add these imports at the top:

```python
from datetime import timezone

from app.graph.models import GraphCalendar, GraphContact, GraphEvent
```

Append these methods and module-level helpers:

```python
    def list_calendars(self, user_id: str) -> list[GraphCalendar]:
        return [
            GraphCalendar(id=raw["id"], name=raw["name"])
            for raw in self._paged_get(f"/users/{user_id}/calendars", params={"$select": "id,name", "$top": "999"})
        ]

    def list_events(
        self, user_id: str, calendar_id: str, modified_since: datetime | None = None
    ) -> Iterator[GraphEvent]:
        params: dict[str, str] = {
            "$select": "id,iCalUId,lastModifiedDateTime,subject,start,end,isAllDay,location,body,organizer,attendees,recurrence",
            "$top": "50",
        }
        if modified_since is not None:
            params["$filter"] = f"lastModifiedDateTime ge {modified_since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        for raw in self._paged_get(
            f"/users/{user_id}/calendars/{calendar_id}/events",
            params=params,
            extra_headers={"Prefer": 'outlook.timezone="UTC"'},
        ):
            yield _to_event(raw)

    def list_contacts(self, user_id: str, modified_since: datetime | None = None) -> Iterator[GraphContact]:
        params: dict[str, str] = {
            "$select": "id,lastModifiedDateTime,displayName,emailAddresses,businessPhones,mobilePhone,companyName",
            "$top": "50",
        }
        if modified_since is not None:
            params["$filter"] = f"lastModifiedDateTime ge {modified_since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        for raw in self._paged_get(f"/users/{user_id}/contacts", params=params):
            yield _to_contact(raw)
```

Add these module-level functions (outside the class, at the bottom of the file):

```python
def _parse_event_dt(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _to_event(raw: dict) -> GraphEvent:
    organizer = (raw.get("organizer") or {}).get("emailAddress") or {}
    attendees = [
        (a.get("emailAddress") or {}).get("address")
        for a in raw.get("attendees", [])
        if (a.get("emailAddress") or {}).get("address")
    ]
    return GraphEvent(
        id=raw["id"],
        ics_uid=raw["iCalUId"],
        last_modified_date_time=parse_graph_datetime(raw["lastModifiedDateTime"]),
        subject=raw.get("subject") or "",
        start=_parse_event_dt(raw["start"]["dateTime"]),
        end=_parse_event_dt(raw["end"]["dateTime"]),
        is_all_day=raw.get("isAllDay", False),
        location=(raw.get("location") or {}).get("displayName"),
        body_html=(raw.get("body") or {}).get("content"),
        organizer_email=organizer.get("address"),
        attendees=attendees,
        recurrence=raw.get("recurrence"),
    )


def _to_contact(raw: dict) -> GraphContact:
    return GraphContact(
        id=raw["id"],
        last_modified_date_time=parse_graph_datetime(raw["lastModifiedDateTime"]),
        display_name=raw.get("displayName") or "",
        email_addresses=[e["address"] for e in raw.get("emailAddresses", []) if e.get("address")],
        business_phones=raw.get("businessPhones", []),
        mobile_phone=raw.get("mobilePhone"),
        company_name=raw.get("companyName"),
    )
```

Note: `parse_graph_datetime` (for `Z`-suffixed UTC timestamps like `lastModifiedDateTime`) was already imported in Task 9 — reuse it, don't redefine it. `_parse_event_dt` is separate because `start`/`end` come back *without* a timezone suffix; the `Prefer: outlook.timezone="UTC"` header forces Graph to return them already in UTC, so we just attach `tzinfo=timezone.utc` directly instead of doing Windows-timezone-name mapping.

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_graph_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/graph/client.py tests/test_graph_client.py
git commit -m "feat: add GraphClient calendars, events, and contacts"
```

---

### Task 11: ICS conversion (recurrence → RRULE, event → iCalendar)

**Files:**
- Create: `app/conversion/__init__.py`
- Create: `app/conversion/ics.py`
- Test: `tests/test_conversion_ics.py`

**Interfaces:**
- Consumes: `GraphEvent` from `app.graph.models` (Task 8).
- Produces: `graph_recurrence_to_rrule(recurrence: dict) -> str`, `graph_event_to_ics(event: GraphEvent) -> bytes`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_conversion_ics.py
from datetime import datetime, timezone

from app.conversion.ics import graph_event_to_ics, graph_recurrence_to_rrule
from app.graph.models import GraphEvent


def test_weekly_recurrence_with_end_date():
    recurrence = {
        "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["monday", "wednesday", "friday"]},
        "range": {"type": "endDate", "endDate": "2026-12-31"},
    }
    rrule = graph_recurrence_to_rrule(recurrence)
    assert "FREQ=WEEKLY" in rrule
    assert "BYDAY=MO,WE,FR" in rrule
    assert "UNTIL=20261231T235959Z" in rrule


def test_absolute_monthly_recurrence_with_count():
    recurrence = {
        "pattern": {"type": "absoluteMonthly", "interval": 1, "dayOfMonth": 15},
        "range": {"type": "numbered", "numberOfOccurrences": 5},
    }
    rrule = graph_recurrence_to_rrule(recurrence)
    assert "FREQ=MONTHLY" in rrule
    assert "BYMONTHDAY=15" in rrule
    assert "COUNT=5" in rrule


def test_relative_monthly_last_friday_no_end():
    recurrence = {
        "pattern": {"type": "relativeMonthly", "interval": 1, "daysOfWeek": ["friday"], "index": "last"},
        "range": {"type": "noEnd"},
    }
    rrule = graph_recurrence_to_rrule(recurrence)
    assert "FREQ=MONTHLY" in rrule
    assert "BYDAY=FR" in rrule
    assert "BYSETPOS=-1" in rrule
    assert "UNTIL" not in rrule
    assert "COUNT" not in rrule


def _sample_event(recurrence=None) -> GraphEvent:
    return GraphEvent(
        id="evt1",
        ics_uid="uid-1",
        last_modified_date_time=datetime(2026, 2, 1, 9, 0, tzinfo=timezone.utc),
        subject="Sitzung",
        start=datetime(2026, 2, 5, 10, 0, tzinfo=timezone.utc),
        end=datetime(2026, 2, 5, 11, 0, tzinfo=timezone.utc),
        is_all_day=False,
        location="Saal",
        body_html="Agenda",
        organizer_email="leiter@church.org",
        attendees=["mitglied@church.org"],
        recurrence=recurrence,
    )


def test_graph_event_to_ics_contains_core_fields():
    ics_bytes = graph_event_to_ics(_sample_event())
    text = ics_bytes.decode("utf-8")
    assert "UID:uid-1" in text
    assert "SUMMARY:Sitzung" in text
    assert "ORGANIZER:mailto:leiter@church.org" in text
    assert "ATTENDEE:mailto:mitglied@church.org" in text


def test_graph_event_to_ics_includes_rrule_when_recurring():
    recurrence = {
        "pattern": {"type": "daily", "interval": 1},
        "range": {"type": "noEnd"},
    }
    ics_bytes = graph_event_to_ics(_sample_event(recurrence))
    text = ics_bytes.decode("utf-8")
    assert "RRULE" in text
    assert "FREQ=DAILY" in text
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_conversion_ics.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.conversion'`

- [ ] **Step 3: Write `app/conversion/__init__.py`** (empty file)

- [ ] **Step 4: Write `app/conversion/ics.py`**

```python
from icalendar import Calendar, Event, vRecur

from app.graph.models import GraphEvent

_DAY_MAP = {
    "monday": "MO", "tuesday": "TU", "wednesday": "WE", "thursday": "TH",
    "friday": "FR", "saturday": "SA", "sunday": "SU",
}
_INDEX_MAP = {"first": 1, "second": 2, "third": 3, "fourth": 4, "last": -1}


def graph_recurrence_to_rrule(recurrence: dict) -> str:
    pattern = recurrence["pattern"]
    rng = recurrence["range"]
    interval = pattern.get("interval", 1)
    parts: list[str] = []

    pattern_type = pattern["type"]
    if pattern_type == "daily":
        parts.append("FREQ=DAILY")
    elif pattern_type == "weekly":
        parts.append("FREQ=WEEKLY")
        days = ",".join(_DAY_MAP[d] for d in pattern.get("daysOfWeek", []))
        if days:
            parts.append(f"BYDAY={days}")
    elif pattern_type == "absoluteMonthly":
        parts.append("FREQ=MONTHLY")
        parts.append(f"BYMONTHDAY={pattern['dayOfMonth']}")
    elif pattern_type == "relativeMonthly":
        parts.append("FREQ=MONTHLY")
        days = ",".join(_DAY_MAP[d] for d in pattern.get("daysOfWeek", []))
        parts.append(f"BYDAY={days}")
        parts.append(f"BYSETPOS={_INDEX_MAP[pattern['index']]}")
    elif pattern_type == "absoluteYearly":
        parts.append("FREQ=YEARLY")
        parts.append(f"BYMONTH={pattern['month']}")
        parts.append(f"BYMONTHDAY={pattern['dayOfMonth']}")
    elif pattern_type == "relativeYearly":
        parts.append("FREQ=YEARLY")
        parts.append(f"BYMONTH={pattern['month']}")
        days = ",".join(_DAY_MAP[d] for d in pattern.get("daysOfWeek", []))
        parts.append(f"BYDAY={days}")
        parts.append(f"BYSETPOS={_INDEX_MAP[pattern['index']]}")
    else:
        raise ValueError(f"Unsupported recurrence pattern type: {pattern_type}")

    parts.append(f"INTERVAL={interval}")

    range_type = rng["type"]
    if range_type == "endDate":
        end_date = rng["endDate"].replace("-", "")
        parts.append(f"UNTIL={end_date}T235959Z")
    elif range_type == "numbered":
        parts.append(f"COUNT={rng['numberOfOccurrences']}")

    return ";".join(parts)


def graph_event_to_ics(event: GraphEvent) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//exotomailcow//migration//DE")
    cal.add("version", "2.0")

    vevent = Event()
    vevent.add("uid", event.ics_uid)
    vevent.add("summary", event.subject)
    vevent.add("dtstart", event.start)
    vevent.add("dtend", event.end)
    vevent.add("last-modified", event.last_modified_date_time)
    if event.location:
        vevent.add("location", event.location)
    if event.body_html:
        vevent.add("description", event.body_html)
    if event.organizer_email:
        vevent.add("organizer", f"mailto:{event.organizer_email}")
    for attendee in event.attendees:
        vevent.add("attendee", f"mailto:{attendee}")
    if event.recurrence:
        vevent.add("rrule", vRecur.from_ical(graph_recurrence_to_rrule(event.recurrence)))

    cal.add_component(vevent)
    return cal.to_ical()
```

- [ ] **Step 5: Run to verify it passes**

Run: `pytest tests/test_conversion_ics.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/conversion/__init__.py app/conversion/ics.py tests/test_conversion_ics.py
git commit -m "feat: add Graph event to iCalendar conversion with recurrence mapping"
```

---

### Task 12: vCard conversion

**Files:**
- Create: `app/conversion/vcard.py`
- Test: `tests/test_conversion_vcard.py`

**Interfaces:**
- Consumes: `GraphContact` from `app.graph.models` (Task 8).
- Produces: `graph_contact_to_vcard(contact: GraphContact) -> bytes`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_conversion_vcard.py
from app.conversion.vcard import graph_contact_to_vcard
from app.graph.models import GraphContact


def test_graph_contact_to_vcard_contains_core_fields():
    contact = GraphContact(
        id="c1",
        last_modified_date_time=__import__("datetime").datetime(2026, 1, 1),
        display_name="Maria Muster",
        email_addresses=["maria@example.org"],
        business_phones=["+41 44 000 00 00"],
        mobile_phone="+41 79 000 00 00",
        company_name="Kirchgemeinde",
    )
    vcard_bytes = graph_contact_to_vcard(contact)
    text = vcard_bytes.decode("utf-8")

    assert "UID:c1" in text
    assert "FN:Maria Muster" in text
    assert "maria@example.org" in text
    assert "+41 79 000 00 00" in text
    assert "Kirchgemeinde" in text


def test_graph_contact_to_vcard_without_optional_fields():
    contact = GraphContact(
        id="c2",
        last_modified_date_time=__import__("datetime").datetime(2026, 1, 1),
        display_name="Ohne Firma",
    )
    vcard_bytes = graph_contact_to_vcard(contact)
    assert b"UID:c2" in vcard_bytes
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_conversion_vcard.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.conversion.vcard'`

- [ ] **Step 3: Write `app/conversion/vcard.py`**

```python
import vobject

from app.graph.models import GraphContact


def graph_contact_to_vcard(contact: GraphContact) -> bytes:
    card = vobject.vCard()
    card.add("uid").value = contact.id
    card.add("fn").value = contact.display_name
    card.add("n").value = vobject.vcard.Name(family=contact.display_name)

    for email in contact.email_addresses:
        field = card.add("email")
        field.value = email
        field.type_param = "INTERNET"

    for phone in contact.business_phones:
        field = card.add("tel")
        field.value = phone
        field.type_param = "WORK"

    if contact.mobile_phone:
        field = card.add("tel")
        field.value = contact.mobile_phone
        field.type_param = "CELL"

    if contact.company_name:
        card.add("org").value = [contact.company_name]

    return card.serialize().encode("utf-8")
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_conversion_vcard.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/conversion/vcard.py tests/test_conversion_vcard.py
git commit -m "feat: add Graph contact to vCard conversion"
```

---

### Task 13: IMAP Importer

**Files:**
- Create: `app/importers/__init__.py`
- Create: `app/importers/base.py`
- Create: `app/importers/imap_importer.py`
- Test: `tests/test_imap_importer.py`

**Interfaces:**
- Produces: `MailcowTarget` dataclass (`address`, `app_password`, `imap_host`, `dav_base_url`, `imap_port=993`), `MailImporter`/`CalendarImporter`/`ContactImporter` Protocols, `ImapMailImporter` implementing `MailImporter` (`connect(target) -> str`, `ensure_folder(imap_path) -> None`, `append_message(imap_path, raw_mime, flags, internal_date) -> str`, `close() -> None`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_imap_importer.py
from datetime import datetime
from unittest.mock import MagicMock

from app.importers.base import MailcowTarget
from app.importers.imap_importer import ImapMailImporter

_TARGET = MailcowTarget(
    address="user@mailcow.local",
    app_password="app-pass",
    imap_host="mail.example.org",
    dav_base_url="https://mail.example.org",
)


def test_connect_returns_detected_delimiter(monkeypatch):
    mock_client = MagicMock()
    mock_client.list_folders.return_value = [((b"\\HasNoChildren",), b".", "INBOX")]
    monkeypatch.setattr("app.importers.imap_importer.IMAPClient", MagicMock(return_value=mock_client))

    importer = ImapMailImporter()
    delimiter = importer.connect(_TARGET)

    assert delimiter == "."
    mock_client.login.assert_called_once_with("user@mailcow.local", "app-pass")


def test_ensure_folder_creates_when_missing(monkeypatch):
    mock_client = MagicMock()
    mock_client.folder_exists.return_value = False
    monkeypatch.setattr("app.importers.imap_importer.IMAPClient", MagicMock(return_value=mock_client))

    importer = ImapMailImporter()
    importer.connect(_TARGET)
    importer.ensure_folder("Projekte.2024")

    mock_client.create_folder.assert_called_once_with("Projekte.2024")


def test_ensure_folder_skips_when_present(monkeypatch):
    mock_client = MagicMock()
    mock_client.folder_exists.return_value = True
    monkeypatch.setattr("app.importers.imap_importer.IMAPClient", MagicMock(return_value=mock_client))

    importer = ImapMailImporter()
    importer.connect(_TARGET)
    importer.ensure_folder("INBOX")

    mock_client.create_folder.assert_not_called()


def test_append_message_parses_appenduid(monkeypatch):
    mock_client = MagicMock()
    mock_client.append.return_value = b"* OK [APPENDUID 38505 42] Success"
    monkeypatch.setattr("app.importers.imap_importer.IMAPClient", MagicMock(return_value=mock_client))

    importer = ImapMailImporter()
    importer.connect(_TARGET)
    uid = importer.append_message("INBOX", b"raw-mime", ["\\Seen"], datetime(2026, 1, 1))

    assert uid == "42"
    mock_client.append.assert_called_once_with("INBOX", b"raw-mime", flags=["\\Seen"], msg_time=datetime(2026, 1, 1))


def test_append_message_raises_without_appenduid(monkeypatch):
    mock_client = MagicMock()
    mock_client.append.return_value = b"* OK Success"
    monkeypatch.setattr("app.importers.imap_importer.IMAPClient", MagicMock(return_value=mock_client))

    importer = ImapMailImporter()
    importer.connect(_TARGET)

    import pytest

    with pytest.raises(RuntimeError):
        importer.append_message("INBOX", b"raw-mime", [], datetime(2026, 1, 1))


def test_close_logs_out(monkeypatch):
    mock_client = MagicMock()
    mock_client.list_folders.return_value = []
    monkeypatch.setattr("app.importers.imap_importer.IMAPClient", MagicMock(return_value=mock_client))

    importer = ImapMailImporter()
    importer.connect(_TARGET)
    importer.close()

    mock_client.logout.assert_called_once()
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_imap_importer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.importers'`

- [ ] **Step 3: Write `app/importers/__init__.py`** (empty file)

- [ ] **Step 4: Write `app/importers/base.py`**

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass
class MailcowTarget:
    address: str
    app_password: str
    imap_host: str
    dav_base_url: str
    imap_port: int = 993


class MailImporter(Protocol):
    def connect(self, target: MailcowTarget) -> str: ...
    def ensure_folder(self, imap_path: str) -> None: ...
    def append_message(
        self, imap_path: str, raw_mime: bytes, flags: list[str], internal_date: datetime
    ) -> str: ...
    def close(self) -> None: ...


class CalendarImporter(Protocol):
    def put_event(self, target: MailcowTarget, uid: str, ics_data: bytes) -> str: ...


class ContactImporter(Protocol):
    def put_contact(self, target: MailcowTarget, uid: str, vcard_data: bytes) -> str: ...
```

- [ ] **Step 5: Write `app/importers/imap_importer.py`**

```python
import re
from datetime import datetime

from imapclient import IMAPClient

from app.importers.base import MailcowTarget

_APPENDUID_RE = re.compile(rb"APPENDUID \d+ (\d+)")


class ImapMailImporter:
    def __init__(self) -> None:
        self._client: IMAPClient | None = None

    def connect(self, target: MailcowTarget) -> str:
        self._client = IMAPClient(target.imap_host, port=target.imap_port, ssl=True)
        self._client.login(target.address, target.app_password)
        folders = self._client.list_folders(directory="", pattern="*")
        if folders:
            delimiter = folders[0][1]
            return delimiter.decode() if isinstance(delimiter, bytes) else delimiter
        return "."

    def ensure_folder(self, imap_path: str) -> None:
        assert self._client is not None
        if not self._client.folder_exists(imap_path):
            self._client.create_folder(imap_path)

    def append_message(
        self, imap_path: str, raw_mime: bytes, flags: list[str], internal_date: datetime
    ) -> str:
        assert self._client is not None
        response = self._client.append(imap_path, raw_mime, flags=flags, msg_time=internal_date)
        match = _APPENDUID_RE.search(response)
        if not match:
            raise RuntimeError(f"IMAP server did not return APPENDUID for folder {imap_path!r}")
        return match.group(1).decode()

    def close(self) -> None:
        if self._client is not None:
            self._client.logout()
            self._client = None
```

- [ ] **Step 6: Run to verify it passes**

Run: `pytest tests/test_imap_importer.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/importers/__init__.py app/importers/base.py app/importers/imap_importer.py tests/test_imap_importer.py
git commit -m "feat: add IMAP mail importer with delimiter detection and APPENDUID parsing"
```

---

### Task 14: CalDAV + CardDAV Importers

**Files:**
- Create: `app/importers/caldav_importer.py`
- Create: `app/importers/carddav_importer.py`
- Test: `tests/test_dav_importers.py`

**Interfaces:**
- Consumes: `MailcowTarget` from `app.importers.base` (Task 13).
- Produces: `CalDavCalendarImporter` implementing `CalendarImporter` (`put_event(target, uid, ics_data) -> str`), `CardDavContactImporter` implementing `ContactImporter` (`put_contact(target, uid, vcard_data) -> str`). Both PUT directly to SOGo's DAV URL scheme over `httpx`, Basic Auth with the mailbox's app password.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dav_importers.py
import base64

import httpx
import pytest

from app.importers.base import MailcowTarget
from app.importers.caldav_importer import CalDavCalendarImporter
from app.importers.carddav_importer import CardDavContactImporter

_TARGET = MailcowTarget(
    address="user@mailcow.local",
    app_password="app-pass",
    imap_host="mail.example.org",
    dav_base_url="https://mail.example.org",
)


def test_put_event_sends_correct_request():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["content_type"] = request.headers["Content-Type"]
        captured["auth"] = request.headers["Authorization"]
        captured["body"] = request.content
        return httpx.Response(201)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    importer = CalDavCalendarImporter(http_client=http_client)
    href = importer.put_event(_TARGET, "uid-1", b"BEGIN:VCALENDAR...")

    assert captured["method"] == "PUT"
    assert captured["url"] == "https://mail.example.org/SOGo/dav/user@mailcow.local/Calendar/uid-1.ics"
    assert captured["content_type"] == "text/calendar; charset=utf-8"
    expected_auth = "Basic " + base64.b64encode(b"user@mailcow.local:app-pass").decode()
    assert captured["auth"] == expected_auth
    assert href == "https://mail.example.org/SOGo/dav/user@mailcow.local/Calendar/uid-1.ics"


def test_put_event_raises_on_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    importer = CalDavCalendarImporter(http_client=http_client)

    with pytest.raises(httpx.HTTPStatusError):
        importer.put_event(_TARGET, "uid-1", b"BEGIN:VCALENDAR...")


def test_put_contact_sends_correct_request():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["content_type"] = request.headers["Content-Type"]
        return httpx.Response(201)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    importer = CardDavContactImporter(http_client=http_client)
    href = importer.put_contact(_TARGET, "c1", b"BEGIN:VCARD...")

    assert captured["url"] == "https://mail.example.org/SOGo/dav/user@mailcow.local/Contacts/c1.vcf"
    assert captured["content_type"] == "text/vcard; charset=utf-8"
    assert href == captured["url"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_dav_importers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.importers.caldav_importer'`

- [ ] **Step 3: Write `app/importers/caldav_importer.py`**

```python
import httpx

from app.importers.base import MailcowTarget


class CalDavCalendarImporter:
    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._http = http_client or httpx.Client()

    def put_event(self, target: MailcowTarget, uid: str, ics_data: bytes) -> str:
        url = f"{target.dav_base_url}/SOGo/dav/{target.address}/Calendar/{uid}.ics"
        response = self._http.put(
            url,
            content=ics_data,
            auth=(target.address, target.app_password),
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )
        response.raise_for_status()
        return url
```

- [ ] **Step 4: Write `app/importers/carddav_importer.py`**

```python
import httpx

from app.importers.base import MailcowTarget


class CardDavContactImporter:
    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._http = http_client or httpx.Client()

    def put_contact(self, target: MailcowTarget, uid: str, vcard_data: bytes) -> str:
        url = f"{target.dav_base_url}/SOGo/dav/{target.address}/Contacts/{uid}.vcf"
        response = self._http.put(
            url,
            content=vcard_data,
            auth=(target.address, target.app_password),
            headers={"Content-Type": "text/vcard; charset=utf-8"},
        )
        response.raise_for_status()
        return url
```

- [ ] **Step 5: Run to verify it passes**

Run: `pytest tests/test_dav_importers.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/importers/caldav_importer.py app/importers/carddav_importer.py tests/test_dav_importers.py
git commit -m "feat: add CalDAV and CardDAV importers for SOGo"
```

---

### Task 15: migration_item repository (idempotency core)

**Files:**
- Create: `app/db/repositories.py`
- Test: `tests/test_repositories.py`

**Interfaces:**
- Consumes: `MigrationItem`, `ItemStatus`, `Base` from `app.db.models` (Task 3).
- Produces: `get_item(db, mapping_id, category, external_id) -> MigrationItem | None`, `needs_import(item: MigrationItem | None, source_modified_at: datetime | None = None) -> bool`, `record_success(db, mapping_id, category, external_id, target_ref, source_modified_at=None) -> None`, `record_failure(db, mapping_id, category, external_id, error_message) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_repositories.py
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_repositories.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.db.repositories'`

- [ ] **Step 3: Write `app/db/repositories.py`**

```python
from datetime import datetime

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
        return source_modified_at > item.source_modified_at
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
```

Note: `get_or_create_folder_map` and `mark_folder_created` are added to this same file in Task 16 — leave room below `record_failure`.

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_repositories.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/db/repositories.py tests/test_repositories.py
git commit -m "feat: add migration_item idempotency repository"
```

---

### Task 16: mail_folder_map repository + folder-path builder

**Files:**
- Modify: `app/db/repositories.py` (append functions)
- Create: `app/importers/folder_mapping.py`
- Test: `tests/test_repositories.py` (append tests)
- Test: `tests/test_folder_mapping.py`

**Interfaces:**
- Consumes: `MailFolderMap` from `app.db.models` (Task 3), `GraphFolder` from `app.graph.models` (Task 8).
- Produces: `get_or_create_folder_map(db, mapping_id, graph_folder_id, graph_path, imap_mailbox_name, well_known_type) -> MailFolderMap`, `mark_folder_created(db, folder_map) -> None`, `build_folder_paths(folders: list[GraphFolder]) -> dict[str, str]`, `build_imap_path(graph_path: str, well_known_type: str | None, delimiter: str) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_repositories.py`:

```python
from app.db.repositories import get_or_create_folder_map, mark_folder_created


def test_get_or_create_folder_map_is_idempotent():
    db = _session()
    mapping_id = _mapping(db)

    first = get_or_create_folder_map(db, mapping_id, "graph-1", "Projekte", "Projekte", None)
    second = get_or_create_folder_map(db, mapping_id, "graph-1", "Projekte", "Projekte", None)

    assert first.id == second.id
    assert first.created is False


def test_mark_folder_created_persists():
    db = _session()
    mapping_id = _mapping(db)
    folder_map = get_or_create_folder_map(db, mapping_id, "graph-1", "Projekte", "Projekte", None)

    mark_folder_created(db, folder_map)

    reloaded = get_or_create_folder_map(db, mapping_id, "graph-1", "Projekte", "Projekte", None)
    assert reloaded.created is True
```

Write `tests/test_folder_mapping.py`:

```python
from app.graph.models import GraphFolder
from app.importers.folder_mapping import build_folder_paths, build_imap_path


def test_build_folder_paths_resolves_nested_hierarchy():
    folders = [
        GraphFolder(id="root", display_name="Projekte", parent_id=None, well_known_name=None, child_folder_count=1),
        GraphFolder(id="child", display_name="2024", parent_id="root", well_known_name=None, child_folder_count=0),
    ]
    paths = build_folder_paths(folders)
    assert paths["root"] == "Projekte"
    assert paths["child"] == "Projekte/2024"


def test_build_imap_path_maps_well_known_folders():
    assert build_imap_path("Inbox", "inbox", ".") == "INBOX"
    assert build_imap_path("Sent Items", "sentitems", ".") == "Sent"
    assert build_imap_path("Deleted Items", "deleteditems", ".") == "Trash"
    assert build_imap_path("Drafts", "drafts", ".") == "Drafts"
    assert build_imap_path("Archive", "archive", ".") == "Archive"


def test_build_imap_path_joins_custom_path_with_delimiter():
    assert build_imap_path("Projekte/2024", None, ".") == "Projekte.2024"
    assert build_imap_path("Projekte/2024", None, "/") == "Projekte/2024"


def test_build_imap_path_sanitizes_delimiter_collision_in_segment():
    assert build_imap_path("A.B/C", None, ".") == "A_B.C"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_repositories.py tests/test_folder_mapping.py -v`
Expected: FAIL — `ImportError: cannot import name 'get_or_create_folder_map'` and `ModuleNotFoundError: No module named 'app.importers.folder_mapping'`

- [ ] **Step 3: Append to `app/db/repositories.py`**

```python
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
```

- [ ] **Step 4: Write `app/importers/folder_mapping.py`**

```python
from app.graph.models import GraphFolder

_WELL_KNOWN_MAP = {
    "inbox": "INBOX",
    "sentitems": "Sent",
    "deleteditems": "Trash",
    "drafts": "Drafts",
    "archive": "Archive",
}


def build_folder_paths(folders: list[GraphFolder]) -> dict[str, str]:
    by_id = {f.id: f for f in folders}
    paths: dict[str, str] = {}

    def resolve(folder_id: str) -> str:
        if folder_id in paths:
            return paths[folder_id]
        folder = by_id[folder_id]
        if folder.parent_id and folder.parent_id in by_id:
            path = f"{resolve(folder.parent_id)}/{folder.display_name}"
        else:
            path = folder.display_name
        paths[folder_id] = path
        return path

    for folder in folders:
        resolve(folder.id)
    return paths


def build_imap_path(graph_path: str, well_known_type: str | None, delimiter: str) -> str:
    if well_known_type and well_known_type in _WELL_KNOWN_MAP:
        return _WELL_KNOWN_MAP[well_known_type]
    segments = [segment.replace(delimiter, "_") for segment in graph_path.split("/") if segment]
    return delimiter.join(segments)
```

- [ ] **Step 5: Run to verify it passes**

Run: `pytest tests/test_repositories.py tests/test_folder_mapping.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/db/repositories.py app/importers/folder_mapping.py tests/test_repositories.py tests/test_folder_mapping.py
git commit -m "feat: add mail folder mapping repository and IMAP path builder"
```

---

### Task 17: JobRunner core + migrate_mail

**Files:**
- Create: `app/jobs/__init__.py`
- Create: `app/jobs/runner.py`
- Test: `tests/test_jobs_runner.py`

**Interfaces:**
- Consumes: `app.db.repositories.{get_item,needs_import,record_success,record_failure}` (Task 15), `app.importers.folder_mapping.{build_folder_paths,build_imap_path}` (Task 16), `app.importers.base.{MailcowTarget,MailImporter,CalendarImporter,ContactImporter}` (Task 13), `app.db.models.{MigrationJob,MailboxMapping,TenantConfig,JobStatus,JobType,ItemCategory}` (Task 3), `app.security.crypto.decrypt` (Task 4).
- Produces: `JobCancelledError` exception, `MigrationJobRunner` class with constructor signature `__init__(self, db_session_factory, graph_client_factory, mail_importer_factory, calendar_importer_factory, contact_importer_factory, imap_host, dav_base_url, imap_port=993)`, and internal method `_migrate_mail(self, db, job, mapping, graph_client, target)`. `run(job_id)` orchestration (dispatch by category + status transitions) lands in Task 20 — this task only wires the constructor and mail migration so it's independently testable.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jobs_runner.py
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

    def connect(self, target):
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_jobs_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.jobs'`

- [ ] **Step 3: Write `app/jobs/__init__.py`** (empty file)

- [ ] **Step 4: Write `app/jobs/runner.py`**

```python
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
```

- [ ] **Step 5: Run to verify it passes**

Run: `pytest tests/test_jobs_runner.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/jobs/__init__.py app/jobs/runner.py tests/test_jobs_runner.py
git commit -m "feat: add MigrationJobRunner core and mail migration"
```

---

### Task 18: JobRunner — migrate_calendar + migrate_contacts

**Files:**
- Modify: `app/jobs/runner.py` (append methods to `MigrationJobRunner`)
- Modify: `tests/test_jobs_runner.py` (append tests)

**Interfaces:**
- Consumes: `graph_event_to_ics` from `app.conversion.ics` (Task 11), `graph_contact_to_vcard` from `app.conversion.vcard` (Task 12).
- Produces: `MigrationJobRunner._migrate_calendar(self, db, job, mapping, graph_client, target, modified_since)`, `MigrationJobRunner._migrate_contacts(self, db, job, mapping, graph_client, target, modified_since)`. Both use `event.ics_uid` / `contact.id` as the `migration_item.external_id` **and** as the CalDAV/CardDAV UID (matches spec §5's "Duplikaterkennung über UID").

- [ ] **Step 1: Write the failing tests** (append to `tests/test_jobs_runner.py`)

```python
from datetime import timedelta

from app.graph.models import GraphCalendar, GraphContact, GraphEvent


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_jobs_runner.py -v`
Expected: FAIL — `AttributeError: 'MigrationJobRunner' object has no attribute '_migrate_calendar'`

- [ ] **Step 3: Append to `app/jobs/runner.py`**

Add these imports at the top:

```python
from app.conversion.ics import graph_event_to_ics
from app.conversion.vcard import graph_contact_to_vcard
```

Append these methods to `MigrationJobRunner`:

```python
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

        db.commit()

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

        db.commit()
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_jobs_runner.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/jobs/runner.py tests/test_jobs_runner.py
git commit -m "feat: add calendar and contact migration with update detection"
```

---

### Task 19: Resync job creation helper

**Files:**
- Create: `app/jobs/resync.py`
- Test: `tests/test_jobs_resync.py`

**Interfaces:**
- Consumes: `MailboxMapping`, `MigrationJob`, `JobType` from `app.db.models` (Task 3).
- Produces: `RESYNC_BUFFER: timedelta` (15 minutes), `create_resync_job(db, mapping_id: int) -> MigrationJob` (raises `ValueError` if the mapping has never completed an initial migration), `create_resync_jobs_for_all(db) -> list[MigrationJob]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jobs_resync.py
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, JobType, MailboxMapping
from app.jobs.resync import RESYNC_BUFFER, create_resync_job, create_resync_jobs_for_all


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_create_resync_job_derives_mail_since_date_from_last_sync():
    db = _session()
    last_synced = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    mapping = MailboxMapping(
        exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc", last_synced_at=last_synced
    )
    db.add(mapping)
    db.commit()

    job = create_resync_job(db, mapping.id)

    assert job.job_type == JobType.RESYNC.value
    assert job.mail_since_date == last_synced - RESYNC_BUFFER
    assert job.migrate_mail and job.migrate_calendar and job.migrate_contacts


def test_create_resync_job_raises_without_prior_sync():
    db = _session()
    mapping = MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc")
    db.add(mapping)
    db.commit()

    with pytest.raises(ValueError):
        create_resync_job(db, mapping.id)


def test_create_resync_jobs_for_all_only_targets_synced_mappings():
    db = _session()
    synced = MailboxMapping(
        exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc",
        last_synced_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    unsynced = MailboxMapping(exo_upn="x@y", mailcow_address="x@z", app_password_encrypted="enc")
    db.add_all([synced, unsynced])
    db.commit()

    jobs = create_resync_jobs_for_all(db)

    assert len(jobs) == 1
    assert jobs[0].mapping_id == synced.id
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_jobs_resync.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.jobs.resync'`

- [ ] **Step 3: Write `app/jobs/resync.py`**

```python
from datetime import timedelta

from sqlalchemy.orm import Session

from app.db.models import JobType, MailboxMapping, MigrationJob

RESYNC_BUFFER = timedelta(minutes=15)


def create_resync_job(db: Session, mapping_id: int) -> MigrationJob:
    mapping = db.get(MailboxMapping, mapping_id)
    if mapping is None or mapping.last_synced_at is None:
        raise ValueError(f"Mapping {mapping_id} has no completed initial migration yet")
    job = MigrationJob(
        mapping_id=mapping_id,
        job_type=JobType.RESYNC.value,
        migrate_mail=True,
        migrate_calendar=True,
        migrate_contacts=True,
        mail_since_date=mapping.last_synced_at - RESYNC_BUFFER,
        dry_run=False,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def create_resync_jobs_for_all(db: Session) -> list[MigrationJob]:
    mappings = db.query(MailboxMapping).filter(MailboxMapping.last_synced_at.isnot(None)).all()
    return [create_resync_job(db, mapping.id) for mapping in mappings]
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_jobs_resync.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/jobs/resync.py tests/test_jobs_resync.py
git commit -m "feat: add resync job creation with auto-derived mail since-date"
```

---

### Task 20: JobRunner.run() orchestration

**Files:**
- Modify: `app/jobs/runner.py` (append `run()` method + imports)
- Modify: `tests/test_jobs_runner.py` (append tests + session-factory helper)

**Interfaces:**
- Consumes: `RESYNC_BUFFER` from `app.jobs.resync` (Task 19), `decrypt` from `app.security.crypto` (Task 4).
- Produces: `MigrationJobRunner.run(self, job_id: int) -> None`. Transitions `pending → running → completed/failed`, honors `JobCancelledError` by leaving status as `cancelled` (already set by whoever cancelled it), dispatches to `_migrate_mail`/`_migrate_calendar`/`_migrate_contacts` per the job's flags, derives `modified_since` (and, if unset, `job.mail_since_date`) from `mapping.last_synced_at - RESYNC_BUFFER` for resync jobs, and sets `mapping.last_synced_at = job.started_at` on non-dry-run completion.

- [ ] **Step 1: Write the failing tests**

Append this session-factory helper and imports to `tests/test_jobs_runner.py`:

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import TenantConfig
from app.jobs.runner import MigrationJobRunner


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

    db.refresh(mapping)
    assert mapping.last_synced_at is None


def test_run_resync_job_auto_derives_mail_since_date():
    session_factory = _make_session_factory()
    db = session_factory()
    db.add(TenantConfig(admin_user="a", admin_password_hash="h"))
    mapping = MailboxMapping(
        exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc",
        last_synced_at=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
    )
    db.add(mapping)
    db.commit()
    job = MigrationJob(mapping_id=mapping.id, job_type=JobType.RESYNC.value, status=JobStatus.PENDING.value,
                        migrate_mail=True, migrate_calendar=False, migrate_contacts=False)
    db.add(job)
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

    from app.jobs.resync import RESYNC_BUFFER

    reloaded = db.get(MigrationJob, job_id)
    assert reloaded.mail_since_date == mapping.last_synced_at - RESYNC_BUFFER


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

    reloaded = db.get(MigrationJob, job_id)
    assert reloaded.status == JobStatus.CANCELLED.value
    assert reloaded.finished_at is not None
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_jobs_runner.py -v`
Expected: FAIL — `AttributeError: 'MigrationJobRunner' object has no attribute 'run'`

- [ ] **Step 3: Append to `app/jobs/runner.py`**

Add these imports at the top:

```python
from datetime import datetime

from app.db.models import JobType
from app.jobs.resync import RESYNC_BUFFER
from app.security.crypto import decrypt
```

Append this method to `MigrationJobRunner`:

```python
    def run(self, job_id: int) -> None:
        db = self._db_session_factory()
        try:
            job = db.get(MigrationJob, job_id)
            mapping = db.get(MailboxMapping, job.mapping_id)
            tenant_config = db.query(TenantConfig).one()

            job.status = JobStatus.RUNNING.value
            job.started_at = datetime.utcnow()
            db.commit()

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

            try:
                if job.migrate_mail:
                    self._migrate_mail(db, job, mapping, graph_client, target)
                if job.migrate_calendar:
                    self._migrate_calendar(db, job, mapping, graph_client, target, modified_since)
                if job.migrate_contacts:
                    self._migrate_contacts(db, job, mapping, graph_client, target, modified_since)
                job.status = JobStatus.COMPLETED.value
                if not job.dry_run:
                    mapping.last_synced_at = job.started_at
            except JobCancelledError:
                pass
            except Exception:
                job.status = JobStatus.FAILED.value
                raise
            finally:
                job.finished_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_jobs_runner.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/jobs/runner.py tests/test_jobs_runner.py
git commit -m "feat: add JobRunner.run orchestration with resync date derivation"
```

---

### Task 21: Scheduler (ThreadPoolExecutor, resume, cancel)

**Files:**
- Create: `app/jobs/scheduler.py`
- Test: `tests/test_jobs_scheduler.py`

**Interfaces:**
- Consumes: `MigrationJob`, `JobStatus` from `app.db.models` (Task 3).
- Produces: `Scheduler(max_workers: int, db_session_factory, runner)` with `submit(job_id: int) -> None`, `cancel(job_id: int) -> None`, `resume_incomplete_jobs() -> None`, `shutdown(wait: bool = True) -> None`. `runner` only needs to satisfy a `run(job_id: int) -> None` duck-typed interface — tests pass a fake, production passes a `MigrationJobRunner` (Task 20).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jobs_scheduler.py
import threading
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, JobStatus, MailboxMapping, MigrationJob
from app.jobs.scheduler import Scheduler


def _session_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed_jobs(session_factory, count: int, status: str = JobStatus.PENDING.value) -> list[int]:
    db = session_factory()
    mapping = MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc")
    db.add(mapping)
    db.commit()
    ids = []
    for _ in range(count):
        job = MigrationJob(mapping_id=mapping.id, status=status)
        db.add(job)
        db.commit()
        ids.append(job.id)
    db.close()
    return ids


class ConcurrencyTrackingRunner:
    def __init__(self):
        self.lock = threading.Lock()
        self.current = 0
        self.peak = 0
        self.calls: list[int] = []

    def run(self, job_id: int) -> None:
        with self.lock:
            self.current += 1
            self.peak = max(self.peak, self.current)
        time.sleep(0.2)
        with self.lock:
            self.calls.append(job_id)
            self.current -= 1


def test_submit_respects_max_workers_concurrency_limit():
    session_factory = _session_factory()
    job_ids = _seed_jobs(session_factory, 4)
    runner = ConcurrencyTrackingRunner()
    scheduler = Scheduler(max_workers=2, db_session_factory=session_factory, runner=runner)

    for job_id in job_ids:
        scheduler.submit(job_id)
    scheduler.shutdown(wait=True)

    assert runner.peak == 2
    assert sorted(runner.calls) == sorted(job_ids)


class RecordingRunner:
    def __init__(self):
        self.calls: list[int] = []

    def run(self, job_id: int) -> None:
        self.calls.append(job_id)


def test_resume_incomplete_jobs_resets_running_to_pending_and_resubmits():
    session_factory = _session_factory()
    running_ids = _seed_jobs(session_factory, 1, status=JobStatus.RUNNING.value)
    pending_ids = _seed_jobs(session_factory, 1, status=JobStatus.PENDING.value)
    runner = RecordingRunner()
    scheduler = Scheduler(max_workers=2, db_session_factory=session_factory, runner=runner)

    scheduler.resume_incomplete_jobs()
    scheduler.shutdown(wait=True)

    assert sorted(runner.calls) == sorted(running_ids + pending_ids)

    db = session_factory()
    for job_id in running_ids:
        assert db.get(MigrationJob, job_id).status == JobStatus.PENDING.value


def test_cancel_marks_pending_or_running_job_as_cancelled():
    session_factory = _session_factory()
    job_ids = _seed_jobs(session_factory, 1, status=JobStatus.RUNNING.value)
    runner = RecordingRunner()
    scheduler = Scheduler(max_workers=2, db_session_factory=session_factory, runner=runner)

    scheduler.cancel(job_ids[0])

    db = session_factory()
    assert db.get(MigrationJob, job_ids[0]).status == JobStatus.CANCELLED.value
    scheduler.shutdown(wait=True)


def test_cancel_leaves_completed_job_untouched():
    session_factory = _session_factory()
    job_ids = _seed_jobs(session_factory, 1, status=JobStatus.COMPLETED.value)
    runner = RecordingRunner()
    scheduler = Scheduler(max_workers=2, db_session_factory=session_factory, runner=runner)

    scheduler.cancel(job_ids[0])

    db = session_factory()
    assert db.get(MigrationJob, job_ids[0]).status == JobStatus.COMPLETED.value
    scheduler.shutdown(wait=True)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_jobs_scheduler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.jobs.scheduler'`

- [ ] **Step 3: Write `app/jobs/scheduler.py`**

```python
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from app.db.models import JobStatus, MigrationJob

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, max_workers: int, db_session_factory: Callable[[], Session], runner) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._db_session_factory = db_session_factory
        self._runner = runner

    def submit(self, job_id: int) -> None:
        self._pool.submit(self._run_safely, job_id)

    def _run_safely(self, job_id: int) -> None:
        try:
            self._runner.run(job_id)
        except Exception:
            logger.exception("Migration job %s crashed", job_id)

    def cancel(self, job_id: int) -> None:
        db = self._db_session_factory()
        try:
            job = db.get(MigrationJob, job_id)
            if job is not None and job.status in (JobStatus.PENDING.value, JobStatus.RUNNING.value):
                job.status = JobStatus.CANCELLED.value
                db.commit()
        finally:
            db.close()

    def resume_incomplete_jobs(self) -> None:
        db = self._db_session_factory()
        try:
            stuck = db.query(MigrationJob).filter(MigrationJob.status == JobStatus.RUNNING.value).all()
            stuck_ids = [job.id for job in stuck]
            for job in stuck:
                job.status = JobStatus.PENDING.value
            db.commit()

            pending_ids = [
                job.id for job in db.query(MigrationJob).filter(MigrationJob.status == JobStatus.PENDING.value).all()
            ]
        finally:
            db.close()

        for job_id in set(stuck_ids) | set(pending_ids):
            self.submit(job_id)

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait)
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_jobs_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/jobs/scheduler.py tests/test_jobs_scheduler.py
git commit -m "feat: add Scheduler with concurrency limit, resume, and cancel"
```

---

### Task 22: Web — Setup routes (Azure AD credentials + test connection)

**Files:**
- Create: `app/web/__init__.py`
- Create: `app/web/routes/__init__.py`
- Create: `app/web/routes/setup.py`
- Create: `app/web/templates/base.html`
- Create: `app/web/templates/setup.html`
- Create: `app/web/templates/_setup_test_result.html`
- Create: `app/web/static/style.css`
- Test: `tests/test_web_setup.py`

**Interfaces:**
- Consumes: `require_admin` (Task 5), `get_db` (Task 3), `encrypt`/`decrypt` (Task 4), `GraphClient` (Task 8).
- Produces: `app.web.routes.setup.router` (FastAPI `APIRouter`) with `GET /setup`, `POST /setup`, `POST /setup/test-connection`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_setup.py
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, TenantConfig
from app.db.session import get_db
from app.security.auth import require_admin
from app.web.routes import setup


def _app_and_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(TenantConfig(admin_user="admin", admin_password_hash="unused"))
    db.commit()

    app = FastAPI()
    app.include_router(setup.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_admin] = lambda: "admin"
    return app, db


def test_setup_form_renders():
    app, db = _app_and_db()
    client = TestClient(app)
    response = client.get("/setup")
    assert response.status_code == 200
    assert "Tenant ID" in response.text


def test_setup_save_encrypts_client_secret():
    app, db = _app_and_db()
    client = TestClient(app)
    response = client.post(
        "/setup",
        data={"tenant_id": "tid", "client_id": "cid", "client_secret": "s3cret"},
    )
    assert response.status_code == 200

    from app.security.crypto import decrypt

    config = db.query(TenantConfig).one()
    assert config.tenant_id == "tid"
    assert config.client_secret_encrypted != "s3cret"
    assert decrypt(config.client_secret_encrypted) == "s3cret"


def test_test_connection_reports_success(monkeypatch):
    app, db = _app_and_db()
    config = db.query(TenantConfig).one()
    from app.security.crypto import encrypt

    config.tenant_id, config.client_id, config.client_secret_encrypted = "tid", "cid", encrypt("s3cret")
    db.commit()

    monkeypatch.setattr(
        "app.web.routes.setup.GraphClient.list_mailboxes", lambda self, search=None: iter([object()])
    )

    client = TestClient(app)
    response = client.post("/setup/test-connection")
    assert "erfolgreich" in response.text


def test_test_connection_reports_failure(monkeypatch):
    app, db = _app_and_db()
    config = db.query(TenantConfig).one()
    from app.security.crypto import encrypt

    config.tenant_id, config.client_id, config.client_secret_encrypted = "tid", "cid", encrypt("s3cret")
    db.commit()

    def _raise(self, search=None):
        raise RuntimeError("auth failed")

    monkeypatch.setattr("app.web.routes.setup.GraphClient.list_mailboxes", _raise)

    client = TestClient(app)
    response = client.post("/setup/test-connection")
    assert "fehlgeschlagen" in response.text
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_web_setup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.web'`

- [ ] **Step 3: Write `app/web/__init__.py`** and **`app/web/routes/__init__.py`** (both empty files)

- [ ] **Step 4: Write `app/web/templates/base.html`**

```html
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <title>exotomailcow</title>
  <link rel="stylesheet" href="/static/style.css">
  <script src="https://unpkg.com/htmx.org@2.0.3"></script>
</head>
<body>
  <nav>
    <a href="/setup">Setup</a>
    <a href="/mappings">Postfächer</a>
  </nav>
  <main>
    {% block content %}{% endblock %}
  </main>
</body>
</html>
```

- [ ] **Step 5: Write `app/web/templates/setup.html`**

```html
{% extends "base.html" %}
{% block content %}
<h1>Azure AD Setup</h1>
<form hx-post="/setup" hx-target="this" hx-swap="outerHTML">
  <label>Tenant ID <input type="text" name="tenant_id" value="{{ config.tenant_id or '' }}" required></label>
  <label>Client ID <input type="text" name="client_id" value="{{ config.client_id or '' }}" required></label>
  <label>Client Secret <input type="password" name="client_secret" required></label>
  <button type="submit">Speichern</button>
</form>
<button hx-post="/setup/test-connection" hx-target="#test-result" hx-swap="innerHTML">Verbindung testen</button>
<div id="test-result"></div>
{% if test_result == "saved" %}<p>Gespeichert.</p>{% endif %}
{% endblock %}
```

- [ ] **Step 6: Write `app/web/templates/_setup_test_result.html`**

```html
{% if result == "ok" %}
<p class="ok">Verbindung erfolgreich.</p>
{% else %}
<p class="error">Verbindung fehlgeschlagen: {{ error_detail }}</p>
{% endif %}
```

- [ ] **Step 7: Write `app/web/static/style.css`**

```css
body { font-family: sans-serif; margin: 2rem; }
label { display: block; margin-bottom: 0.75rem; }
input { display: block; width: 100%; max-width: 24rem; padding: 0.4rem; }
.ok { color: #1a7f37; }
.error { color: #b3261e; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #ddd; padding: 0.5rem; text-align: left; }
```

- [ ] **Step 8: Write `app/web/routes/setup.py`**

```python
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db.models import TenantConfig
from app.db.session import get_db
from app.graph.client import GraphClient
from app.security.auth import require_admin
from app.security.crypto import decrypt, encrypt

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, db: Session = Depends(get_db), _: str = Depends(require_admin)):
    config = db.query(TenantConfig).one()
    return templates.TemplateResponse("setup.html", {"request": request, "config": config, "test_result": None})


@router.post("/setup", response_class=HTMLResponse)
def setup_save(
    request: Request,
    tenant_id: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    config = db.query(TenantConfig).one()
    config.tenant_id = tenant_id
    config.client_id = client_id
    config.client_secret_encrypted = encrypt(client_secret)
    db.commit()
    return templates.TemplateResponse("setup.html", {"request": request, "config": config, "test_result": "saved"})


@router.post("/setup/test-connection", response_class=HTMLResponse)
def test_connection(request: Request, db: Session = Depends(get_db), _: str = Depends(require_admin)):
    config = db.query(TenantConfig).one()
    result = "error"
    error_detail = None
    if config.tenant_id and config.client_id and config.client_secret_encrypted:
        try:
            client = GraphClient(config.tenant_id, config.client_id, decrypt(config.client_secret_encrypted))
            next(iter(client.list_mailboxes()), None)
            result = "ok"
        except Exception as exc:
            error_detail = str(exc)
    else:
        error_detail = "Bitte zuerst Tenant ID, Client ID und Client Secret speichern."
    return templates.TemplateResponse(
        "_setup_test_result.html", {"request": request, "result": result, "error_detail": error_detail}
    )
```

- [ ] **Step 9: Run to verify it passes**

Run: `pytest tests/test_web_setup.py -v`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add app/web tests/test_web_setup.py
git commit -m "feat: add Azure AD setup GUI with connection test"
```

---

### Task 23: Web — Mapping routes (CRUD + CSV import)

**Files:**
- Create: `app/web/routes/mappings.py`
- Create: `app/web/templates/mappings.html`
- Create: `app/web/templates/_mappings_table.html`
- Test: `tests/test_web_mappings.py`

**Interfaces:**
- Consumes: `require_admin`, `get_db`, `encrypt`, `MailboxMapping`.
- Produces: `app.web.routes.mappings.router` with `GET /mappings`, `POST /mappings`, `POST /mappings/csv-import`, `DELETE /mappings/{mapping_id}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_mappings.py
import io

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, MailboxMapping
from app.db.session import get_db
from app.security.auth import require_admin
from app.web.routes import mappings


def _app_and_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = Session(engine)

    app = FastAPI()
    app.include_router(mappings.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_admin] = lambda: "admin"
    return app, db


def test_add_mapping_encrypts_password_and_lists_it():
    app, db = _app_and_db()
    client = TestClient(app)
    response = client.post(
        "/mappings",
        data={"exo_upn": "user@church.org", "mailcow_address": "user@mailcow.local", "app_password": "pw123"},
    )
    assert response.status_code == 200
    assert "user@church.org" in response.text

    mapping = db.query(MailboxMapping).one()
    assert mapping.app_password_encrypted != "pw123"


def test_csv_import_creates_multiple_mappings():
    app, db = _app_and_db()
    client = TestClient(app)
    csv_content = "exo_upn,mailcow_address,app_password\na@x.org,a@mailcow.local,pw1\nb@x.org,b@mailcow.local,pw2\n"
    response = client.post(
        "/mappings/csv-import",
        files={"file": ("mappings.csv", io.BytesIO(csv_content.encode()), "text/csv")},
    )
    assert response.status_code == 200
    assert db.query(MailboxMapping).count() == 2


def test_delete_mapping_removes_row():
    app, db = _app_and_db()
    db.add(MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc"))
    db.commit()
    mapping_id = db.query(MailboxMapping).one().id

    client = TestClient(app)
    response = client.request("DELETE", f"/mappings/{mapping_id}")

    assert response.status_code == 200
    assert db.query(MailboxMapping).count() == 0


def test_list_mappings_filters_by_search_query():
    app, db = _app_and_db()
    db.add_all(
        [
            MailboxMapping(exo_upn="alice@church.org", mailcow_address="alice@mailcow.local", app_password_encrypted="e"),
            MailboxMapping(exo_upn="bob@church.org", mailcow_address="bob@mailcow.local", app_password_encrypted="e"),
        ]
    )
    db.commit()

    client = TestClient(app)
    response = client.get("/mappings", params={"q": "alice"})

    assert "alice@church.org" in response.text
    assert "bob@church.org" not in response.text
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_web_mappings.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.web.routes.mappings'`

- [ ] **Step 3: Write `app/web/templates/mappings.html`**

```html
{% extends "base.html" %}
{% block content %}
<h1>Postfach-Mappings</h1>
<form hx-get="/mappings" hx-target="body" hx-push-url="true">
  <input type="text" name="q" placeholder="Suche nach EXO-UPN" value="{{ q }}">
  <button type="submit">Suchen</button>
</form>

<form hx-post="/mappings" hx-target="#mappings-table" hx-swap="outerHTML">
  <input type="text" name="exo_upn" placeholder="EXO-UPN" required>
  <input type="text" name="mailcow_address" placeholder="Mailcow-Adresse" required>
  <input type="password" name="app_password" placeholder="App-Passwort" required>
  <button type="submit">Hinzufügen</button>
</form>

<form hx-post="/mappings/csv-import" hx-target="#mappings-table" hx-swap="outerHTML" hx-encoding="multipart/form-data">
  <input type="file" name="file" accept=".csv" required>
  <button type="submit">CSV importieren</button>
</form>

{% include "_mappings_table.html" %}
{% endblock %}
```

- [ ] **Step 4: Write `app/web/templates/_mappings_table.html`**

```html
<table id="mappings-table">
  <thead>
    <tr><th>EXO-UPN</th><th>Mailcow-Adresse</th><th>Letzter Sync</th><th></th></tr>
  </thead>
  <tbody>
    {% for mapping in mappings %}
    <tr>
      <td>{{ mapping.exo_upn }}</td>
      <td>{{ mapping.mailcow_address }}</td>
      <td>{{ mapping.last_synced_at or "–" }}</td>
      <td>
        <button hx-delete="/mappings/{{ mapping.id }}" hx-target="#mappings-table" hx-swap="outerHTML">Löschen</button>
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% if imported_count is defined %}<p>{{ imported_count }} Mapping(s) importiert.</p>{% endif %}
```

- [ ] **Step 5: Write `app/web/routes/mappings.py`**

```python
import csv
import io

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db.models import MailboxMapping
from app.db.session import get_db
from app.security.auth import require_admin
from app.security.crypto import encrypt

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.get("/mappings", response_class=HTMLResponse)
def list_mappings(request: Request, q: str = "", db: Session = Depends(get_db), _: str = Depends(require_admin)):
    query = db.query(MailboxMapping)
    if q:
        query = query.filter(MailboxMapping.exo_upn.contains(q))
    mappings = query.order_by(MailboxMapping.created_at).all()
    return templates.TemplateResponse("mappings.html", {"request": request, "mappings": mappings, "q": q})


@router.post("/mappings", response_class=HTMLResponse)
def add_mapping(
    request: Request,
    exo_upn: str = Form(...),
    mailcow_address: str = Form(...),
    app_password: str = Form(...),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    db.add(
        MailboxMapping(
            exo_upn=exo_upn, mailcow_address=mailcow_address, app_password_encrypted=encrypt(app_password)
        )
    )
    db.commit()
    mappings = db.query(MailboxMapping).order_by(MailboxMapping.created_at).all()
    return templates.TemplateResponse("_mappings_table.html", {"request": request, "mappings": mappings})


@router.post("/mappings/csv-import", response_class=HTMLResponse)
async def import_mappings_csv(
    request: Request, file: UploadFile, db: Session = Depends(get_db), _: str = Depends(require_admin)
):
    content = (await file.read()).decode("utf-8")
    reader = csv.DictReader(io.StringIO(content))
    count = 0
    for row in reader:
        db.add(
            MailboxMapping(
                exo_upn=row["exo_upn"],
                mailcow_address=row["mailcow_address"],
                app_password_encrypted=encrypt(row["app_password"]),
            )
        )
        count += 1
    db.commit()
    mappings = db.query(MailboxMapping).order_by(MailboxMapping.created_at).all()
    return templates.TemplateResponse(
        "_mappings_table.html", {"request": request, "mappings": mappings, "imported_count": count}
    )


@router.delete("/mappings/{mapping_id}", response_class=HTMLResponse)
def delete_mapping(
    request: Request, mapping_id: int, db: Session = Depends(get_db), _: str = Depends(require_admin)
):
    mapping = db.get(MailboxMapping, mapping_id)
    if mapping is not None:
        db.delete(mapping)
        db.commit()
    mappings = db.query(MailboxMapping).order_by(MailboxMapping.created_at).all()
    return templates.TemplateResponse("_mappings_table.html", {"request": request, "mappings": mappings})
```

- [ ] **Step 6: Run to verify it passes**

Run: `pytest tests/test_web_mappings.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/web/routes/mappings.py app/web/templates/mappings.html app/web/templates/_mappings_table.html tests/test_web_mappings.py
git commit -m "feat: add mapping CRUD and CSV bulk import GUI"
```

---

### Task 24: Web — Job routes (create single/batch, cancel) + live progress polling

**Files:**
- Create: `app/web/scheduler_dep.py`
- Create: `app/web/routes/jobs.py`
- Create: `app/web/templates/_job_progress.html`
- Create: `app/web/templates/_jobs_started.html`
- Modify: `app/web/templates/mappings.html` (Task 23) — add the "Migration starten" form
- Test: `tests/test_web_jobs.py`

**Interfaces:**
- Consumes: `MigrationJobRunner` (Task 20), `Scheduler` (Task 21), `ImapMailImporter`/`CalDavCalendarImporter`/`CardDavContactImporter` (Tasks 13–14), `decrypt` (Task 4), `get_settings` (Task 2), `SessionLocal` (Task 3).
- Produces: `app.web.scheduler_dep.get_scheduler() -> Scheduler` (FastAPI dependency, builds the process-wide singleton on first use), `app.web.routes.jobs.router` with `POST /jobs` (accepts one or many `mapping_ids` — spec §10 point 4 requires "jede Zeile einzeln startbar oder alle als Batch-Job", so a single endpoint taking a list covers both: a batch of size 1 is the single-row case), `GET /jobs/{job_id}`, `POST /jobs/{job_id}/cancel`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_jobs.py
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, JobStatus, MailboxMapping, MigrationJob
from app.db.session import get_db
from app.security.auth import require_admin
from app.web.routes import jobs
from app.web.scheduler_dep import get_scheduler


class FakeScheduler:
    def __init__(self):
        self.submitted: list[int] = []
        self.cancelled: list[int] = []

    def submit(self, job_id: int) -> None:
        self.submitted.append(job_id)

    def cancel(self, job_id: int) -> None:
        self.cancelled.append(job_id)


def _app_and_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = Session(engine)
    mapping = MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc")
    db.add(mapping)
    db.commit()

    fake_scheduler = FakeScheduler()
    app = FastAPI()
    app.include_router(jobs.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_admin] = lambda: "admin"
    app.dependency_overrides[get_scheduler] = lambda: fake_scheduler
    return app, db, mapping, fake_scheduler


def test_create_jobs_supports_single_mapping_selection():
    app, db, mapping, fake_scheduler = _app_and_db()
    client = TestClient(app)

    response = client.post(
        "/jobs",
        data={
            "mapping_ids": [str(mapping.id)],
            "migrate_mail": "true",
            "migrate_calendar": "true",
            "migrate_contacts": "false",
            "dry_run": "false",
        },
    )

    assert response.status_code == 200
    job = db.query(MigrationJob).one()
    assert job.migrate_mail is True
    assert job.migrate_contacts is False
    assert fake_scheduler.submitted == [job.id]


def test_create_jobs_supports_batch_selection_of_multiple_mappings():
    app, db, mapping, fake_scheduler = _app_and_db()
    mapping2 = MailboxMapping(exo_upn="b@c", mailcow_address="b@d", app_password_encrypted="enc")
    db.add(mapping2)
    db.commit()

    client = TestClient(app)
    response = client.post(
        "/jobs",
        data={"mapping_ids": [str(mapping.id), str(mapping2.id)], "migrate_mail": "true"},
    )

    assert response.status_code == 200
    jobs = db.query(MigrationJob).order_by(MigrationJob.id).all()
    assert len(jobs) == 2
    assert {job.mapping_id for job in jobs} == {mapping.id, mapping2.id}
    assert sorted(fake_scheduler.submitted) == sorted(job.id for job in jobs)


def test_job_progress_endpoint_returns_current_counts():
    app, db, mapping, fake_scheduler = _app_and_db()
    job = MigrationJob(mapping_id=mapping.id, status=JobStatus.RUNNING.value, count_created=3, count_failed=1)
    db.add(job)
    db.commit()

    client = TestClient(app)
    response = client.get(f"/jobs/{job.id}")

    assert "3" in response.text
    assert "1" in response.text


def test_cancel_job_calls_scheduler_cancel():
    app, db, mapping, fake_scheduler = _app_and_db()
    job = MigrationJob(mapping_id=mapping.id, status=JobStatus.RUNNING.value)
    db.add(job)
    db.commit()

    client = TestClient(app)
    response = client.post(f"/jobs/{job.id}/cancel")

    assert response.status_code == 200
    assert fake_scheduler.cancelled == [job.id]
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_web_jobs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.web.scheduler_dep'`

- [ ] **Step 3: Write `app/web/scheduler_dep.py`**

```python
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
        _scheduler = build_scheduler()
    return _scheduler
```

- [ ] **Step 4: Write `app/web/templates/_job_progress.html`**

```html
{% if job.status in ("pending", "running") %}
<div id="job-{{ job.id }}" hx-get="/jobs/{{ job.id }}" hx-trigger="every 2s" hx-swap="outerHTML">
{% else %}
<div id="job-{{ job.id }}">
{% endif %}
  <p>Status: {{ job.status }}</p>
  <p>
    Neu: {{ job.count_created }} |
    Aktualisiert: {{ job.count_updated }} |
    Übersprungen: {{ job.count_skipped }} |
    Fehlgeschlagen: {{ job.count_failed }}
  </p>
  {% if job.status in ("pending", "running") %}
  <button hx-post="/jobs/{{ job.id }}/cancel" hx-target="#job-{{ job.id }}" hx-swap="outerHTML">Abbrechen</button>
  {% endif %}
  {% if job.status in ("completed", "failed", "cancelled") %}
  <a href="/jobs/{{ job.id }}/report.csv">Bericht (CSV)</a> |
  <a href="/jobs/{{ job.id }}/report.json">Bericht (JSON)</a>
  {% endif %}
</div>
```

- [ ] **Step 5: Write `app/web/templates/_jobs_started.html`**

```html
<div id="jobs-started-container">
  <p>{{ jobs|length }} Migration(s) gestartet.</p>
  {% for job in jobs %}
    {% include "_job_progress.html" %}
  {% endfor %}
</div>
```

- [ ] **Step 6: Modify `app/web/templates/mappings.html`** — add a "Migration starten" section right after `{% include "_mappings_table.html" %}` and before `{% endblock %}`:

```html
<h2>Migration starten</h2>
<form hx-post="/jobs" hx-target="#jobs-started-container" hx-swap="outerHTML">
  <label>Postfächer
    <select name="mapping_ids" multiple required size="6">
      {% for mapping in mappings %}
      <option value="{{ mapping.id }}">{{ mapping.exo_upn }}</option>
      {% endfor %}
    </select>
  </label>
  <label><input type="checkbox" name="migrate_mail" value="true" checked> Mail</label>
  <label><input type="checkbox" name="migrate_calendar" value="true" checked> Kalender</label>
  <label><input type="checkbox" name="migrate_contacts" value="true" checked> Kontakte</label>
  <label>Mail seit (optional) <input type="date" name="mail_since_date"></label>
  <label><input type="checkbox" name="dry_run" value="true"> Dry-Run (nur zählen)</label>
  <button type="submit">Migration starten</button>
</form>
<div id="jobs-started-container"></div>
```

- [ ] **Step 7: Write `app/web/routes/jobs.py`**

```python
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db.models import JobType, MigrationJob
from app.db.session import get_db
from app.security.auth import require_admin
from app.web.scheduler_dep import get_scheduler

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.post("/jobs", response_class=HTMLResponse)
def create_jobs(
    request: Request,
    mapping_ids: list[int] = Form(...),
    migrate_mail: bool = Form(False),
    migrate_calendar: bool = Form(False),
    migrate_contacts: bool = Form(False),
    mail_since_date: str = Form(""),
    dry_run: bool = Form(False),
    db: Session = Depends(get_db),
    scheduler=Depends(get_scheduler),
    _: str = Depends(require_admin),
):
    since = datetime.fromisoformat(mail_since_date) if mail_since_date else None
    jobs: list[MigrationJob] = []
    for mapping_id in mapping_ids:
        job = MigrationJob(
            mapping_id=mapping_id,
            job_type=JobType.INITIAL.value,
            migrate_mail=migrate_mail,
            migrate_calendar=migrate_calendar,
            migrate_contacts=migrate_contacts,
            mail_since_date=since,
            dry_run=dry_run,
        )
        db.add(job)
        jobs.append(job)
    db.commit()
    for job in jobs:
        db.refresh(job)
        scheduler.submit(job.id)
    return templates.TemplateResponse("_jobs_started.html", {"request": request, "jobs": jobs})


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_progress(request: Request, job_id: int, db: Session = Depends(get_db), _: str = Depends(require_admin)):
    job = db.get(MigrationJob, job_id)
    return templates.TemplateResponse("_job_progress.html", {"request": request, "job": job})


@router.post("/jobs/{job_id}/cancel", response_class=HTMLResponse)
def cancel_job(
    request: Request,
    job_id: int,
    db: Session = Depends(get_db),
    scheduler=Depends(get_scheduler),
    _: str = Depends(require_admin),
):
    scheduler.cancel(job_id)
    job = db.get(MigrationJob, job_id)
    return templates.TemplateResponse("_job_progress.html", {"request": request, "job": job})
```

- [ ] **Step 8: Run to verify it passes**

Run: `pytest tests/test_web_jobs.py -v`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add app/web/scheduler_dep.py app/web/routes/jobs.py app/web/templates/_job_progress.html app/web/templates/_jobs_started.html app/web/templates/mappings.html tests/test_web_jobs.py
git commit -m "feat: add single/batch job creation, live progress polling, and cancel GUI"
```

---

### Task 25: Web — Reports, Resync routes, Kill-switch

**Files:**
- Create: `app/web/routes/reports.py`
- Create: `app/web/routes/resync.py`
- Create: `app/web/routes/admin.py`
- Create: `app/web/templates/_resync_all_result.html`
- Create: `app/web/templates/_resync_error.html`
- Create: `app/web/templates/_purge_result.html`
- Test: `tests/test_web_reports_resync_admin.py`

**Interfaces:**
- Consumes: `create_resync_job`/`create_resync_jobs_for_all` (Task 19), `get_scheduler` (Task 24), `MigrationItem`/`ItemStatus`/`MigrationJob`/`TenantConfig`/`MailboxMapping` (Task 3).
- Produces: `app.web.routes.reports.router` (`GET /jobs/{job_id}/report.json`, `GET /jobs/{job_id}/report.csv`), `app.web.routes.resync.router` (`POST /mappings/{mapping_id}/resync`, `POST /mappings/resync-all`), `app.web.routes.admin.router` (`POST /admin/purge-secrets`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_reports_resync_admin.py
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, ItemStatus, MailboxMapping, MigrationItem, MigrationJob, TenantConfig
from app.db.session import get_db
from app.security.auth import require_admin
from app.web.routes import admin, reports, resync
from app.web.scheduler_dep import get_scheduler


class FakeScheduler:
    def __init__(self):
        self.submitted: list[int] = []

    def submit(self, job_id: int) -> None:
        self.submitted.append(job_id)


def _app_and_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = Session(engine)

    fake_scheduler = FakeScheduler()
    app = FastAPI()
    app.include_router(reports.router)
    app.include_router(resync.router)
    app.include_router(admin.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_admin] = lambda: "admin"
    app.dependency_overrides[get_scheduler] = lambda: fake_scheduler
    return app, db, fake_scheduler


def test_report_json_lists_failed_items_and_counts():
    app, db, _ = _app_and_db()
    mapping = MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc")
    db.add(mapping)
    db.commit()
    job = MigrationJob(mapping_id=mapping.id, count_created=2, count_failed=1)
    db.add(job)
    db.add(MigrationItem(mapping_id=mapping.id, category="mail", external_id="m1", status=ItemStatus.FAILED.value, error_message="boom"))
    db.commit()

    client = TestClient(app)
    response = client.get(f"/jobs/{job.id}/report.json")

    body = response.json()
    assert body["count_created"] == 2
    assert body["errors"][0]["error"] == "boom"


def test_report_csv_contains_error_rows():
    app, db, _ = _app_and_db()
    mapping = MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc")
    db.add(mapping)
    db.commit()
    job = MigrationJob(mapping_id=mapping.id)
    db.add(job)
    db.add(MigrationItem(mapping_id=mapping.id, category="calendar", external_id="e1", status=ItemStatus.FAILED.value, error_message="dav down"))
    db.commit()

    client = TestClient(app)
    response = client.get(f"/jobs/{job.id}/report.csv")

    assert "dav down" in response.text


def test_resync_one_submits_job_when_previously_synced():
    app, db, fake_scheduler = _app_and_db()
    mapping = MailboxMapping(
        exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc",
        last_synced_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    db.add(mapping)
    db.commit()

    client = TestClient(app)
    response = client.post(f"/mappings/{mapping.id}/resync")

    assert response.status_code == 200
    job = db.query(MigrationJob).one()
    assert fake_scheduler.submitted == [job.id]


def test_resync_one_rejects_mapping_without_prior_sync():
    app, db, fake_scheduler = _app_and_db()
    mapping = MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc")
    db.add(mapping)
    db.commit()

    client = TestClient(app)
    response = client.post(f"/mappings/{mapping.id}/resync")

    assert response.status_code == 400
    assert fake_scheduler.submitted == []


def test_resync_all_only_submits_for_synced_mappings():
    app, db, fake_scheduler = _app_and_db()
    synced = MailboxMapping(
        exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc",
        last_synced_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    unsynced = MailboxMapping(exo_upn="x@y", mailcow_address="x@z", app_password_encrypted="enc")
    db.add_all([synced, unsynced])
    db.commit()

    client = TestClient(app)
    response = client.post("/mappings/resync-all")

    assert response.status_code == 200
    assert len(fake_scheduler.submitted) == 1


def test_purge_secrets_clears_secrets_but_keeps_history():
    app, db, _ = _app_and_db()
    from app.security.crypto import encrypt

    db.add(TenantConfig(tenant_id="t", client_id="c", client_secret_encrypted=encrypt("s"), admin_user="a", admin_password_hash="h"))
    mapping = MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted=encrypt("pw"))
    db.add(mapping)
    db.commit()
    job = MigrationJob(mapping_id=mapping.id, count_created=1)
    db.add(job)
    db.commit()

    client = TestClient(app)
    response = client.post("/admin/purge-secrets")

    assert response.status_code == 200
    config = db.query(TenantConfig).one()
    assert config.client_secret_encrypted is None
    reloaded_mapping = db.query(MailboxMapping).one()
    assert reloaded_mapping.app_password_encrypted == ""
    assert db.query(MigrationJob).count() == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_web_reports_resync_admin.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.web.routes.reports'`

- [ ] **Step 3: Write `app/web/routes/reports.py`**

```python
import csv
import io
import json

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db.models import ItemStatus, MigrationItem, MigrationJob
from app.db.session import get_db
from app.security.auth import require_admin

router = APIRouter()


def _failed_items(db: Session, job: MigrationJob) -> list[MigrationItem]:
    return (
        db.query(MigrationItem)
        .filter(MigrationItem.mapping_id == job.mapping_id, MigrationItem.status == ItemStatus.FAILED.value)
        .all()
    )


@router.get("/jobs/{job_id}/report.json")
def report_json(job_id: int, db: Session = Depends(get_db), _: str = Depends(require_admin)):
    job = db.get(MigrationJob, job_id)
    failed = _failed_items(db, job)
    payload = {
        "job_id": job.id,
        "status": job.status,
        "count_created": job.count_created,
        "count_updated": job.count_updated,
        "count_skipped": job.count_skipped,
        "count_failed": job.count_failed,
        "errors": [
            {"category": item.category, "external_id": item.external_id, "error": item.error_message}
            for item in failed
        ],
    }
    return Response(content=json.dumps(payload, indent=2), media_type="application/json")


@router.get("/jobs/{job_id}/report.csv")
def report_csv(job_id: int, db: Session = Depends(get_db), _: str = Depends(require_admin)):
    job = db.get(MigrationJob, job_id)
    failed = _failed_items(db, job)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["category", "external_id", "error"])
    for item in failed:
        writer.writerow([item.category, item.external_id, item.error_message])
    return Response(content=buffer.getvalue(), media_type="text/csv")
```

- [ ] **Step 4: Write `app/web/templates/_resync_all_result.html`**

```html
<p>{{ jobs|length }} Resync-Job(s) gestartet.</p>
<ul>
{% for job in jobs %}
  <li><a href="/jobs/{{ job.id }}">Job #{{ job.id }} (Mapping {{ job.mapping_id }})</a></li>
{% endfor %}
</ul>
```

- [ ] **Step 5: Write `app/web/templates/_resync_error.html`**

```html
<p class="error">Resync nicht möglich: {{ error }}</p>
```

- [ ] **Step 6: Write `app/web/routes/resync.py`**

```python
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.jobs.resync import create_resync_job, create_resync_jobs_for_all
from app.security.auth import require_admin
from app.web.scheduler_dep import get_scheduler

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.post("/mappings/{mapping_id}/resync", response_class=HTMLResponse)
def resync_one(
    request: Request,
    mapping_id: int,
    db: Session = Depends(get_db),
    scheduler=Depends(get_scheduler),
    _: str = Depends(require_admin),
):
    try:
        job = create_resync_job(db, mapping_id)
    except ValueError as exc:
        return templates.TemplateResponse(
            "_resync_error.html", {"request": request, "error": str(exc)}, status_code=400
        )
    scheduler.submit(job.id)
    return templates.TemplateResponse("_job_progress.html", {"request": request, "job": job})


@router.post("/mappings/resync-all", response_class=HTMLResponse)
def resync_all(
    request: Request, db: Session = Depends(get_db), scheduler=Depends(get_scheduler), _: str = Depends(require_admin)
):
    jobs = create_resync_jobs_for_all(db)
    for job in jobs:
        scheduler.submit(job.id)
    return templates.TemplateResponse("_resync_all_result.html", {"request": request, "jobs": jobs})
```

- [ ] **Step 7: Write `app/web/templates/_purge_result.html`**

```html
<p>Alle gespeicherten Zugangsdaten wurden gelöscht. Migrations-Historie bleibt erhalten.</p>
```

- [ ] **Step 8: Write `app/web/routes/admin.py`**

```python
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db.models import MailboxMapping, TenantConfig
from app.db.session import get_db
from app.security.auth import require_admin

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.post("/admin/purge-secrets", response_class=HTMLResponse)
def purge_secrets(request: Request, db: Session = Depends(get_db), _: str = Depends(require_admin)):
    config = db.query(TenantConfig).one_or_none()
    if config is not None:
        config.client_id = None
        config.client_secret_encrypted = None
    for mapping in db.query(MailboxMapping).all():
        mapping.app_password_encrypted = ""
    db.commit()
    return templates.TemplateResponse("_purge_result.html", {"request": request})
```

- [ ] **Step 9: Run to verify it passes**

Run: `pytest tests/test_web_reports_resync_admin.py -v`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add app/web/routes/reports.py app/web/routes/resync.py app/web/routes/admin.py app/web/templates/_resync_all_result.html app/web/templates/_resync_error.html app/web/templates/_purge_result.html tests/test_web_reports_resync_admin.py
git commit -m "feat: add reports, resync routes, and secret purge kill-switch"
```

---

### Task 26: Docker deployment + startup wiring

**Files:**
- Modify: `app/main.py`
- Create: `Dockerfile`
- Create: `docker-compose.yml`

**Interfaces:**
- Consumes: every router from Task 22–25, `configure_logging` (Task 6), `init_db`/`SessionLocal` (Task 3), `bootstrap_admin_from_env` (Task 5), `get_scheduler` (Task 24).
- Produces: `app.main.app` fully wired with all routers and a `lifespan` startup hook that configures logging, creates tables, bootstraps the admin user, and resumes incomplete jobs.

- [ ] **Step 1: Rewrite `app/main.py`**

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db.session import SessionLocal, init_db
from app.logging_config import configure_logging
from app.security.auth import bootstrap_admin_from_env
from app.web.routes import admin, jobs, mappings, reports, resync, setup
from app.web.scheduler_dep import get_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_dir, settings.log_level)
    init_db()
    db = SessionLocal()
    try:
        bootstrap_admin_from_env(db)
    finally:
        db.close()
    get_scheduler().resume_incomplete_jobs()
    yield


app = FastAPI(title="exotomailcow", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
app.include_router(setup.router)
app.include_router(mappings.router)
app.include_router(jobs.router)
app.include_router(reports.router)
app.include_router(resync.router)
app.include_router(admin.router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
```

- [ ] **Step 2: Verify existing tests are unaffected**

Run: `pytest tests/test_main.py -v`
Expected: PASS (health check still uses `TestClient(app)`, which now also triggers the `lifespan` startup — this is expected and exercises `init_db`/`bootstrap_admin_from_env` against the real configured SQLite path from `tests/conftest.py`'s `DATABASE_URL=sqlite:///:memory:` env var).

- [ ] **Step 3: Write `Dockerfile`**

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 4: Write `docker-compose.yml`**

```yaml
services:
  exotomailcow:
    build: .
    ports:
      - "8000:8000"
    environment:
      - ENCRYPTION_KEY=${ENCRYPTION_KEY}
      - ADMIN_USER=${ADMIN_USER}
      - ADMIN_PASSWORD=${ADMIN_PASSWORD}
      - MAILCOW_DAV_BASE_URL=${MAILCOW_DAV_BASE_URL}
      - MAILCOW_IMAP_HOST=${MAILCOW_IMAP_HOST}
      - MAILCOW_IMAP_PORT=${MAILCOW_IMAP_PORT:-993}
      - CONCURRENCY=${CONCURRENCY:-4}
      - DATABASE_URL=sqlite:////app/data/exotomailcow.db
      - LOG_DIR=/app/data/logs
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
    volumes:
      - ./data:/app/data
    restart: unless-stopped
```

- [ ] **Step 5: Run the full test suite**

Run: `pytest -v`
Expected: PASS (all tasks so far, ~90+ tests, all green)

- [ ] **Step 6: Manual verification**

```bash
cp .env.example .env
# fill in ENCRYPTION_KEY (python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"),
# ADMIN_PASSWORD, MAILCOW_DAV_BASE_URL, MAILCOW_IMAP_HOST
docker compose build
docker compose up -d
curl -u "$ADMIN_USER:$ADMIN_PASSWORD" http://localhost:8000/healthz
docker compose down
```

Expected: `{"status": "ok"}` from the curl call.

- [ ] **Step 7: Commit**

```bash
git add app/main.py Dockerfile docker-compose.yml
git commit -m "feat: wire full app with startup lifespan, Dockerfile, and docker-compose"
```

---

### Task 27: README

**Files:**
- Create: `README.md`

**Interfaces:**
- None (documentation only).

- [ ] **Step 1: Write `README.md`**

```markdown
# exotomailcow

Migrationstool: Exchange Online (Microsoft Graph, App-Only) → Mailcow
(Dovecot IMAP + SOGo CalDAV/CardDAV). Migriert pro Postfach Mail
(inkl. Ordnerstruktur), Kalender und Kontakte, mit Web-GUI,
Fortschrittsanzeige, Resume-Fähigkeit und Post-Cutover-Resync.

Architektur- und Designentscheidungen: siehe
`docs/superpowers/specs/2026-09-01-exo-to-mailcow-migration-tool-design.md`.

## 1. Azure AD App-Registration

1. Azure Portal → **Azure Active Directory → App registrations → New registration**.
   Name frei wählbar (z.B. `exotomailcow-migration`), kein Redirect-URI nötig.
2. Nach der Erstellung: **Certificates & secrets → New client secret**,
   Wert sofort kopieren (wird nur einmal angezeigt) → das ist `CLIENT_SECRET`
   fürs Setup im Tool.
3. **API permissions → Add a permission → Microsoft Graph → Application permissions**,
   folgende hinzufügen:
   - `Mail.ReadWrite`
   - `Calendars.Read`
   - `Contacts.Read`
   - `MailboxSettings.Read`
   - `User.Read.All`
4. **Grant admin consent for `<tenant>`** klicken (erfordert Global-Admin-
   oder Privileged-Role-Admin-Rechte) — ohne diesen Schritt schlagen alle
   Graph-Aufrufe mit `403 Forbidden` fehl, egal wie die Permissions
   konfiguriert sind.
5. Tenant-ID (**Overview → Directory (tenant) ID**) und Application
   (Client) ID notieren.

Diese drei Werte (Tenant-ID, Client-ID, Client-Secret) werden im Tool
unter **Setup** eingetragen, dort Fernet-verschlüsselt in der DB
abgelegt und nie im Klartext geloggt.

## 2. Mailcow-Voraussetzungen

- **App-Passwörter aktivieren:** Mailcow Admin-UI → *Configuration →
  Access → App Passwords* (bzw. pro Mailbox im User-Bereich unter
  *App Passwords*) — für jedes Ziel-Postfach ein App-Passwort erzeugen,
  das im Mapping als `app_password` eingetragen wird. Normale
  Mailbox-Passwörter funktionieren zwar auch, App-Passwörter sind aber
  die empfohlene, widerrufbare Variante für automatisierte Zugriffe.
- **IMAP** ist bei Mailcow standardmäßig auf Port 993 (SSL) aktiv —
  `MAILCOW_IMAP_HOST` auf den Mailcow-Hostnamen setzen.
- **SOGo-URL:** Mailcow bindet SOGo unter `https://<mailcow-host>/SOGo/`
  ein; `MAILCOW_DAV_BASE_URL` ist die Basis-URL ohne `/SOGo`-Suffix
  (z.B. `https://mail.example.org`), das Tool hängt
  `/SOGo/dav/<adresse>/Calendar/` bzw. `/Contacts/` selbst an.
- **Ziel-Postfächer müssen vorher existieren** — das Tool legt keine
  Mailcow-Mailboxen an (bewusstes Nicht-Ziel, siehe Spec §2).

## 3. Environment-Variablen

| Variable | Pflicht | Default | Beschreibung |
|---|---|---|---|
| `ENCRYPTION_KEY` | ja | – | Fernet-Key (32 Byte, base64) für Secrets-Verschlüsselung. Erzeugen mit `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. **Verlust = alle gespeicherten Secrets unbrauchbar.** |
| `ADMIN_USER` | ja | – | Benutzername fürs GUI-Login (Basic Auth), nur beim allerersten Start wirksam. |
| `ADMIN_PASSWORD` | ja | – | Passwort fürs GUI-Login, nur beim allerersten Start wirksam (danach in der DB gehasht gespeichert; zum Ändern die `tenant_config`-Zeile löschen und neu starten). |
| `MAILCOW_DAV_BASE_URL` | ja | – | Basis-URL der Mailcow-Instanz für SOGo CalDAV/CardDAV, z.B. `https://mail.example.org`. |
| `MAILCOW_IMAP_HOST` | ja | – | Hostname des Mailcow-Dovecot-IMAP-Servers. |
| `MAILCOW_IMAP_PORT` | nein | `993` | IMAP-SSL-Port. |
| `CONCURRENCY` | nein | `4` | Anzahl gleichzeitig laufender Postfach-Migrationen. |
| `DATABASE_URL` | nein | `sqlite:///./data/exotomailcow.db` | SQLite-Pfad (im Container `sqlite:////app/data/exotomailcow.db`, viertes `/` beachten). |
| `LOG_DIR` | nein | `./data/logs` | Verzeichnis für rotierende JSON-Logs. |
| `LOG_LEVEL` | nein | `INFO` | Log-Level. |

## 4. Betrieb

```bash
cp .env.example .env   # Werte oben ausfüllen
docker compose build
docker compose up -d
```

GUI unter `http://<host>:8000`, Login mit `ADMIN_USER`/`ADMIN_PASSWORD`.
Workflow: **Setup** (Azure-AD-Zugangsdaten, Verbindung testen) →
**Postfächer** (Mappings anlegen oder CSV importieren) → pro Mapping
oder als Batch eine Migration starten → Fortschritt live verfolgen →
Abschlussbericht (CSV/JSON) herunterladen.

**Nach der DNS-Umstellung** (MX/Autodiscover → Mailcow): pro Postfach
oder für alle auf einmal **Resync** anstoßen, um Mail/Termine/Kontakte
nachzuziehen, die zwischen Erstmigration und Umstellung in EXO
hinzukamen oder sich änderten (siehe Spec §6a).

**Kill-Switch:** unter *Admin* → "Alle Zugangsdaten löschen" entfernt
Client-Secret und alle App-Passwörter aus der DB, sobald die Migration
abgeschlossen ist. Migrations-Historie und Berichte bleiben erhalten.

## 5. Warum kein Exchange-ActiveSync-Adapter?

IMAP/CalDAV/CardDAV sind die einzigen Ziel-Adapter. Gründe: die
verfügbaren Python-EAS-Bibliotheken sind unausgereift/unmaintained, und
Mailcow/SOGo bieten IMAP, CalDAV und CardDAV nativ und robust an — es
gibt zielseitig ohnehin keinen EAS-Server, gegen den importiert werden
könnte. Graph deckt den Quell-Zugriff bereits vollständig ab.

## 6. Entwicklung & Tests

```bash
pip install -e ".[dev]"
pytest                          # vollständige Unit-/Komponenten-Suite
pytest tests/integration -v     # manuell, siehe unten
```

### Integrationstest gegen ein einzelnes Test-Postfach

`tests/integration/test_single_mailbox_roundtrip.py` läuft nicht in CI
(braucht echte Zugangsdaten) und ist standardmäßig übersprungen. Zum
Ausführen: echtes EXO-Test-Postfach + Test-Mailcow-Postfach anlegen,
Zugangsdaten als Umgebungsvariablen setzen (siehe Kopfkommentar der
Testdatei), dann `pytest tests/integration -v -m integration` — deckt
Mail-Rundlauf inkl. verschachtelter Ordner, Kalender-Rundlauf inkl.
Serientermin, Kontakte-Rundlauf, Resume nach simuliertem Absturz und
einen vollständigen Resync-Rundlauf ab (siehe Spec §12).
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add README with Azure AD setup, Mailcow prerequisites, and env vars"
```

---

## Post-Plan Note

`tests/integration/test_single_mailbox_roundtrip.py` (referenced in the File Structure and Task 27) is intentionally **not** specified task-by-task in this plan: it requires real Exchange Online and Mailcow credentials that don't exist until a human provisions a test tenant and test mailbox, so no fake/mock version of it would be meaningful. Once real test credentials are available, write it by hand following spec §12's scenario list (mail roundtrip with nested folders, recurring calendar event, contacts roundtrip, crash-resume, full resync roundtrip), reusing the exact same `GraphClient`, `ImapMailImporter`, `CalDavCalendarImporter`, `CardDavContactImporter`, and `MigrationJobRunner` built in Tasks 8–20 — no new interfaces needed.


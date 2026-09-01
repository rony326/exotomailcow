from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, TypeDecorator, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """A DateTime that automatically converts naive datetimes to UTC-aware datetimes."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        """Convert timezone-aware datetime to UTC for storage."""
        if value is not None:
            if value.tzinfo is not None:
                return value.astimezone(timezone.utc)
            return value
        return value

    def process_result_value(self, value, dialect):
        """Convert stored datetime back to UTC-aware datetime."""
        if value is not None:
            if value.tzinfo is None:
                # Assume stored values are UTC if they have no timezone
                return value.replace(tzinfo=timezone.utc)
            return value
        return value


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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)


class MailboxMapping(Base):
    __tablename__ = "mailbox_mapping"

    id: Mapped[int] = mapped_column(primary_key=True)
    exo_upn: Mapped[str] = mapped_column(String(255))
    mailcow_address: Mapped[str] = mapped_column(String(255))
    app_password_encrypted: Mapped[str] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)


class MigrationJob(Base):
    __tablename__ = "migration_job"

    id: Mapped[int] = mapped_column(primary_key=True)
    mapping_id: Mapped[int] = mapped_column(ForeignKey("mailbox_mapping.id"))
    job_type: Mapped[str] = mapped_column(String(20), default=JobType.INITIAL.value)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.PENDING.value)
    migrate_mail: Mapped[bool] = mapped_column(Boolean, default=True)
    migrate_calendar: Mapped[bool] = mapped_column(Boolean, default=True)
    migrate_contacts: Mapped[bool] = mapped_column(Boolean, default=True)
    mail_since_date: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    count_created: Mapped[int] = mapped_column(Integer, default=0)
    count_updated: Mapped[int] = mapped_column(Integer, default=0)
    count_skipped: Mapped[int] = mapped_column(Integer, default=0)
    count_failed: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


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
    source_modified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=ItemStatus.DONE.value)
    target_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)


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
